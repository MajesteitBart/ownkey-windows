"""Meeting capture: microphone and system audio into durable chunks.

Audio callbacks only enqueue blocks. A writer thread drains the queues four
times a second, converts everything to mono 16 kHz, keeps each track aligned
to the active timeline, and hands full chunks to the store. Pause discards
new audio from every source and is recorded as an event so playback and
transcript times stay aligned. A source that dies, a full queue or a write
error ends capture safely and marks the meeting interrupted.
"""

from __future__ import annotations

import queue
import threading
import time

import numpy as np

from . import audio

MIC = "mic"
SYSTEM = "system"
SOURCE_LABELS = {MIC: "Microphone", SYSTEM: "Call audio"}

QUEUE_BLOCKS = 600          # ~60 s of 100 ms blocks per source before an overrun
BLOCK_SECONDS = 0.1
WRITER_INTERVAL = 0.25
PAD_THRESHOLD_SECONDS = 0.5  # a track this far behind the timeline gets silence
LEVEL_HOLD = 0.7             # meter decay between writer ticks


class SourceError(RuntimeError):
    """A capture source could not start or stopped delivering audio."""


class BaseSource:
    """A capture source pushes ``(int16 mono 16 kHz samples, monotonic time)``."""

    label: str = ""
    rate_out = audio.SAMPLE_RATE

    def __init__(self) -> None:
        self.blocks: queue.Queue = queue.Queue(maxsize=QUEUE_BLOCKS)
        self.dropped_samples = 0
        self.error: str | None = None
        self._alive = False

    @property
    def alive(self) -> bool:
        return self._alive and self.error is None

    def start(self) -> None:
        self._alive = True

    def stop(self) -> None:
        self._alive = False

    def push(self, samples: np.ndarray, at: float | None = None) -> None:
        try:
            self.blocks.put_nowait((np.asarray(samples, dtype=np.int16), time.monotonic() if at is None else at))
        except queue.Full:
            self.dropped_samples += int(np.asarray(samples).size)

    def fail(self, message: str) -> None:
        self.error = message
        self._alive = False


class ArraySource(BaseSource):
    """A source fed from code; used by tests and the recovery smoke test."""

    def __init__(self, label: str = MIC) -> None:
        super().__init__()
        self.label = label


class MicrophoneSource(BaseSource):
    """PortAudio input stream at 16 kHz mono, like dictation."""

    label = MIC

    def __init__(self, device=None) -> None:
        super().__init__()
        self.device = device
        self._stream = None

    def start(self) -> None:
        import sounddevice as sd

        blocksize = int(audio.SAMPLE_RATE * BLOCK_SECONDS)

        def callback(indata, frames, time_info, status):
            if status and status.input_overflow:
                self.dropped_samples += int(frames)
            self.push(indata[:, 0].copy())

        try:
            self._stream = sd.InputStream(
                samplerate=audio.SAMPLE_RATE, channels=1, dtype="int16", blocksize=blocksize,
                device=self.device, callback=callback, finished_callback=self._finished, latency="low",
            )
            self._stream.start()
        except Exception as exc:
            raise SourceError(f"Microphone could not start: {exc}") from exc
        super().start()

    def _finished(self) -> None:
        if self._alive:
            self.fail("Microphone stopped delivering audio (device removed or sleep).")

    def stop(self) -> None:
        super().stop()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass


class SystemAudioSource(BaseSource):
    """WASAPI loopback of an output device through the soundcard package.

    Loopback delivers nothing while the output is silent; the session pads
    the track from the timeline so it never drifts against the microphone.
    """

    label = SYSTEM

    def __init__(self, device_id: str | None = None, samplerate: int = 48000) -> None:
        super().__init__()
        self.device_id = device_id
        self.samplerate = int(samplerate)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        try:
            import soundcard as sc

            speaker = sc.default_speaker() if not self.device_id else sc.get_speaker(self.device_id)
            self._mic = sc.get_microphone(speaker.id, include_loopback=True)
        except Exception as exc:
            raise SourceError(f"System audio could not start: {exc}") from exc
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="meeting-loopback")
        super().start()
        self._thread.start()

    def _run(self) -> None:
        frames = int(self.samplerate * BLOCK_SECONDS)
        try:
            with self._mic.recorder(samplerate=self.samplerate, blocksize=frames) as recorder:
                while not self._stop.is_set():
                    data = recorder.record(numframes=frames)
                    if data is None or len(data) == 0:
                        continue
                    mono = audio.to_mono_float(data)
                    self.push(audio.float_to_int16(audio.resample(mono, self.samplerate, audio.SAMPLE_RATE)))
        except Exception as exc:
            if not self._stop.is_set():
                self.fail(f"System audio stopped: {exc}")

    def stop(self) -> None:
        super().stop()
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)


def list_devices() -> dict:
    """Input devices for the microphone picker and outputs for system audio."""
    inputs, outputs, note = [], [], ""
    try:
        import sounddevice as sd

        apis = sd.query_hostapis()
        for index, device in enumerate(sd.query_devices()):
            if device["max_input_channels"] > 0:
                api = apis[device["hostapi"]]["name"] if device["hostapi"] < len(apis) else ""
                inputs.append({"id": index, "name": device["name"], "api": api,
                               "default": index == sd.default.device[0]})
    except Exception as exc:
        note = f"Microphone list unavailable: {exc}"
    try:
        import soundcard as sc

        for speaker in sc.all_speakers():
            outputs.append({"id": speaker.id, "name": speaker.name})
        try:
            default_id = sc.default_speaker().id
            for item in outputs:
                item["default"] = item["id"] == default_id
        except Exception:
            pass
    except Exception as exc:
        note = (note + " " if note else "") + f"System audio unavailable: {exc}"
    return {"inputs": inputs, "outputs": outputs, "note": note.strip()}


class CaptureSession:
    """Owns the sources, the writer thread and the meeting's audio timeline."""

    def __init__(self, store, meeting_id: str, sources: list[BaseSource], *, clock=time.monotonic,
                 chunk_samples: int = audio.CHUNK_SAMPLES, on_interrupted=None):
        self.store = store
        self.meeting_id = meeting_id
        self.sources = {source.label: source for source in sources}
        self._clock = clock
        self._chunk_samples = chunk_samples
        self._on_interrupted = on_interrupted
        self._lock = threading.RLock()
        self._writers: dict[str, audio.ChunkWriter] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.state = "idle"          # idle | recording | paused | stopped | interrupted
        self.reason = ""
        self.levels = {label: 0.0 for label in self.sources}
        self.gaps = 0
        self._active_before = 0.0    # seconds captured before the current run
        self._run_started: float | None = None
        self._elapsed_written = 0.0

    # ── timeline ───────────────────────────────────────────────────
    @property
    def elapsed(self) -> float:
        with self._lock:
            if self._run_started is None:
                return self._active_before
            return self._active_before + (self._clock() - self._run_started)

    # ── lifecycle ──────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            if self.state != "idle":
                raise RuntimeError("Capture already started.")
            started = []
            try:
                for label, source in self.sources.items():
                    self._writers[label] = audio.ChunkWriter(
                        self.store.audio_dir(self.meeting_id, label),
                        lambda seq, start, count, path, label=label: self.store.add_chunk(
                            self.meeting_id, label, seq, start, count, path),
                        chunk_samples=self._chunk_samples,
                    )
                    source.start()
                    started.append(source)
            except Exception:
                for source in started:
                    source.stop()
                raise
            self._run_started = self._clock()
            self.state = "recording"
            self.store.add_event(self.meeting_id, 0.0, "start", ", ".join(SOURCE_LABELS.get(l, l) for l in self.sources))
            self._stop.clear()
            self._thread = threading.Thread(target=self._writer_loop, daemon=True, name="meeting-writer")
            self._thread.start()

    def pause(self) -> bool:
        with self._lock:
            if self.state != "recording":
                return False
            self._tick(final=False)
            self._active_before = self.elapsed
            self._run_started = None
            self.state = "paused"
            self.store.add_event(self.meeting_id, self._active_before, "pause")
            self.store.update_meeting(self.meeting_id, state="paused", elapsed=self._active_before)
            return True

    def resume(self) -> bool:
        with self._lock:
            if self.state != "paused":
                return False
            for source in self.sources.values():
                self._drain(source)  # audio heard while paused is not persisted
            self._run_started = self._clock()
            self.state = "recording"
            self.store.add_event(self.meeting_id, self._active_before, "resume")
            self.store.update_meeting(self.meeting_id, state="recording")
            return True

    def stop(self, reason: str = "") -> dict:
        """Close capture before anything else happens. Idempotent."""
        with self._lock:
            if self.state in ("stopped", "interrupted"):
                return self.summary()
            interrupted = bool(reason)
            elapsed = self.elapsed
            self._active_before = elapsed
            self._run_started = None
            self._stop.set()
            for source in self.sources.values():
                source.stop()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        with self._lock:
            try:
                self._tick(final=True)
            except OSError as exc:
                interrupted, reason = True, reason or f"Write error while closing: {exc}"
            self.state = "interrupted" if interrupted else "stopped"
            self.reason = reason
            self.store.add_event(self.meeting_id, elapsed, "interrupted" if interrupted else "stop", reason)
            self.store.update_meeting(self.meeting_id, state=self.state, elapsed=elapsed)
            if interrupted and self._on_interrupted is not None:
                try:
                    self._on_interrupted(self, reason)
                except Exception:
                    pass
            return self.summary()

    def summary(self) -> dict:
        with self._lock:
            return {
                "meeting_id": self.meeting_id,
                "state": self.state,
                "reason": self.reason,
                "elapsed": round(self.elapsed, 3),
                "levels": dict(self.levels),
                "gaps": self.gaps,
                "sources": {label: writer.total_samples for label, writer in self._writers.items()},
            }

    # ── writer ─────────────────────────────────────────────────────
    def _writer_loop(self) -> None:
        while not self._stop.wait(WRITER_INTERVAL):
            with self._lock:
                if self.state not in ("recording", "paused"):
                    return
                try:
                    self._tick(final=False)
                except OSError as exc:
                    self._stop.set()
                    threading.Thread(target=self.stop, args=(f"Disk write failed: {exc}",), daemon=True).start()
                    return
                dead = [label for label, source in self.sources.items() if not source.alive]
                if dead and self.state == "recording":
                    source = self.sources[dead[0]]
                    reason = source.error or f"{SOURCE_LABELS.get(dead[0], dead[0])} stopped."
                    self._stop.set()
                    threading.Thread(target=self.stop, args=(reason,), daemon=True).start()
                    return

    def _drain(self, source: BaseSource) -> list[np.ndarray]:
        blocks = []
        while True:
            try:
                samples, _at = source.blocks.get_nowait()
            except queue.Empty:
                return blocks
            blocks.append(samples)

    def _tick(self, *, final: bool) -> None:
        """Move queued audio into chunk files and keep tracks on the timeline."""
        paused = self.state == "paused"
        elapsed = self.elapsed
        expected = int(elapsed * audio.SAMPLE_RATE)
        for label, source in self.sources.items():
            blocks = self._drain(source)
            if paused and not final:
                continue
            writer = self._writers[label]
            if blocks:
                data = np.concatenate(blocks) if len(blocks) > 1 else blocks[0]
                self.levels[label] = max(audio.meter_level(data), self.levels[label] * LEVEL_HOLD)
                writer.append(data)
            else:
                self.levels[label] *= LEVEL_HOLD
            if source.dropped_samples:
                lost = source.dropped_samples
                source.dropped_samples = 0
                self.gaps += 1
                writer.append(np.zeros(lost, dtype=np.int16))
                self.store.add_event(self.meeting_id, writer.total_samples / audio.SAMPLE_RATE, "gap",
                                     f"{SOURCE_LABELS.get(label, label)}: {lost / audio.SAMPLE_RATE:.1f} s lost (overrun)")
            deficit = expected - writer.total_samples
            if deficit > PAD_THRESHOLD_SECONDS * audio.SAMPLE_RATE or (final and deficit > 0):
                # A silent loopback delivers no frames; keep the track aligned.
                writer.append(np.zeros(int(deficit), dtype=np.int16))
            if final:
                writer.flush()
        if not paused and elapsed - self._elapsed_written >= 1.0:
            self._elapsed_written = elapsed
            self.store.update_meeting(self.meeting_id, elapsed=elapsed)
