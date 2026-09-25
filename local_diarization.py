"""Speaker diarization on this PC with NVIDIA Nemotron 3 Diarization.

The model runs through NeMo-Speech.cpp's stable C ABI (nemo_speech/diar.h),
loaded with ctypes from the runtime that ships next to Ownkey. Audio never
leaves the PC. Speaker channels are numbered by first arrival, up to eight.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import sys
import threading

import numpy as np

from local_models import Model, ModelFile, ModelError

REVISION = "f667ed73aee57d40cc39428eb768b4fd87a0a29e"
MODEL_FILE = "Nemotron-3-Diarization.q8_0.gguf"
MODEL_SIZE = 107012128
MODEL_SHA256 = "08456d9e22cd9a323c0364d98375f3746d6e68507ebb705cd46438c534c7a3a1"
MODEL_URL = f"https://huggingface.co/nvidia/Nemotron-3-Diarization/resolve/{REVISION}/{MODEL_FILE}"
NEMOTRON_DIARIZATION = Model(
    "nemotron-3-diarization", REVISION, MODEL_URL, MODEL_SIZE, MODEL_SHA256, "",
    (ModelFile(MODEL_FILE, MODEL_SIZE, MODEL_SHA256),), file_urls=(MODEL_URL,),
    label="Nemotron 3 Diarization", settings_tab="Meetings",
)
LABEL = "Nemotron 3 Diarization"
SAMPLE_RATE = 16000
# NeMo-Speech.cpp geometry as (preset, chunk frames, right context frames) on
# its 80 ms grid; 0 keeps the preset value. Batch labels use the 30.4 s
# "offline" geometry the model card scores best. Live labels use 3.04 s chunks:
# on the 60 s AMI fixture that ran 8x faster than real time on four CPU threads
# with the same error rate as batch; the 1.04 s default ran only 3x.
BATCH_PRESET = ("v3-offline", 0, 0)
LIVE_PRESET = ("v3-streaming", 38, 1)
BATCH_BLOCK = 30 * SAMPLE_RATE
RUNTIME_ENV = "OWNKEY_NEMO_SPEECH_DIR"
LIBRARY_NAMES = {"win32": "nemo_speech_asr_c.dll", "linux": "libnemo_speech_asr_c.so"}


class DiarizationError(RuntimeError):
    """A user-facing problem with local speaker labels."""


# ── C ABI ──────────────────────────────────────────────────────────────
class _ModelConfig(ctypes.Structure):
    _fields_ = [("size", ctypes.c_size_t), ("model_path", ctypes.c_char_p), ("gpu", ctypes.c_int32),
                ("preset", ctypes.c_char_p), ("chunk_frames", ctypes.c_int32),
                ("right_context_frames", ctypes.c_int32), ("left_context_frames", ctypes.c_int32),
                ("fifo_frames", ctypes.c_int32), ("spkcache_frames", ctypes.c_int32),
                ("update_period_frames", ctypes.c_int32)]


class _Segment(ctypes.Structure):
    _fields_ = [("start_time", ctypes.c_double), ("end_time", ctypes.c_double), ("speaker", ctypes.c_int32)]


def runtime_dir() -> Path:
    """Where the NeMo-Speech.cpp libraries live: an explicit override, the
    frozen app bundle, or the repository's vendor folder for development."""
    if os.environ.get(RUNTIME_ENV):
        return Path(os.environ[RUNTIME_ENV])
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "nemo_speech"
    platform = "windows-x64" if sys.platform == "win32" else "linux-x64"
    return Path(__file__).resolve().parent / "vendor" / "nemo-speech" / platform


def cpu_supported() -> bool:
    """The bundled runtime is built for AVX2 with FMA and F16C (2013 and later
    x64 CPUs). Loading it elsewhere would crash the process, not raise."""
    if sys.platform == "win32":
        return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))  # PF_AVX2_INSTRUCTIONS_AVAILABLE
    try:
        flags = next(line for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("flags"))
    except (OSError, StopIteration):
        return False
    return {"avx2", "fma", "f16c"} <= set(flags.split(":", 1)[-1].split())


_runtime = None
_runtime_failure = ""  # Settings polls often; a failed load is not retried until restart.
_runtime_lock = threading.Lock()


def load_runtime(directory: Path | None = None, *, cpu_check=cpu_supported):
    """Load the diarization ABI once per process."""
    global _runtime, _runtime_failure
    with _runtime_lock:
        if directory is None:
            if _runtime is not None:
                return _runtime
            if _runtime_failure:
                raise DiarizationError(_runtime_failure)
        try:
            lib = _open_library(Path(directory or runtime_dir()), cpu_check)
        except DiarizationError as exc:
            if directory is None:
                _runtime_failure = str(exc)
            raise
        if directory is None:
            _runtime = lib
        return lib


def _open_library(directory: Path, cpu_check):
    name = LIBRARY_NAMES.get("linux" if sys.platform.startswith("linux") else sys.platform)
    path = directory / (name or "")
    if not name or not path.is_file():
        raise DiarizationError("The speaker label runtime is missing from this Ownkey build.")
    if not cpu_check():
        raise DiarizationError("Speaker labels need a processor with AVX2 (most PCs from 2013 onward).")
    try:
        lib = ctypes.CDLL(str(path))
    except OSError as exc:
        raise DiarizationError(f"The speaker label runtime could not load: {exc}") from None
    p, status = ctypes.c_void_p, ctypes.c_int
    for fn, args, result in (
        ("nemo_speech_diar_create", [ctypes.POINTER(_ModelConfig), ctypes.POINTER(p)], status),
        ("nemo_speech_diar_destroy", [p], None),
        ("nemo_speech_diar_num_speakers", [p], ctypes.c_int32),
        ("nemo_speech_diar_seconds_per_frame", [p], ctypes.c_double),
        ("nemo_speech_diar_stream_open", [p, ctypes.POINTER(p)], status),
        ("nemo_speech_diar_stream_push_f32", [p, ctypes.POINTER(ctypes.c_float), ctypes.c_size_t,
                                              ctypes.c_int32], status),
        ("nemo_speech_diar_stream_finish", [p], status),
        ("nemo_speech_diar_stream_close", [p], None),
        ("nemo_speech_diar_frame_count", [p], ctypes.c_int64),
        ("nemo_speech_diar_frame_probs_start", [p], ctypes.c_int64),
        ("nemo_speech_diar_frame_probs", [p, ctypes.POINTER(ctypes.c_float), ctypes.c_size_t], status),
        ("nemo_speech_diar_segments", [p, p, ctypes.POINTER(_Segment), ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t)], status),
        ("nemo_speech_asr_last_error", [], ctypes.c_char_p),
    ):
        function = getattr(lib, fn)
        function.argtypes, function.restype = args, result
    return lib


def _check(lib, status: int, what: str) -> None:
    if status != 0:
        detail = (lib.nemo_speech_asr_last_error() or b"").decode("utf-8", "replace").strip()
        raise DiarizationError(f"Speaker labels failed while {what}. {detail}".strip())


class DiarModel:
    """One loaded GGUF with a fixed streaming geometry. Streams opened from it
    may run on different threads; the runtime serializes compute."""

    def __init__(self, lib, path: Path, geometry: tuple[str, int, int]):
        self._lib = lib
        preset, chunk, right = geometry
        config = _ModelConfig(size=ctypes.sizeof(_ModelConfig), model_path=str(path).encode("utf-8"),
                              gpu=-1, preset=preset.encode("ascii"), chunk_frames=chunk,
                              right_context_frames=right, left_context_frames=-1)
        handle = ctypes.c_void_p()
        _check(lib, lib.nemo_speech_diar_create(ctypes.byref(config), ctypes.byref(handle)), "loading the model")
        self._handle = handle
        self.num_speakers = int(lib.nemo_speech_diar_num_speakers(handle))
        self.seconds_per_frame = float(lib.nemo_speech_diar_seconds_per_frame(handle))

    def open_stream(self) -> "DiarStream":
        return DiarStream(self)

    def close(self) -> None:
        if self._handle:
            self._lib.nemo_speech_diar_destroy(self._handle)
            self._handle = ctypes.c_void_p()


class DiarStream:
    """Push mono audio, then read committed per-frame probabilities and
    segments. V3 never revises a frame once it is committed."""

    def __init__(self, model: DiarModel):
        self._lib, self.model = model._lib, model
        handle = ctypes.c_void_p()
        _check(self._lib, self._lib.nemo_speech_diar_stream_open(model._handle, ctypes.byref(handle)), "starting")
        self._handle = handle
        self.read_frames = 0  # frames already returned by new_probs()

    def push(self, samples: np.ndarray) -> None:
        data = np.ascontiguousarray(samples, dtype=np.float32)
        if data.size:
            pointer = data.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            _check(self._lib, self._lib.nemo_speech_diar_stream_push_f32(self._handle, pointer, data.size,
                                                                          SAMPLE_RATE), "processing audio")

    def finish(self) -> None:
        _check(self._lib, self._lib.nemo_speech_diar_stream_finish(self._handle), "finishing")

    def frame_count(self) -> int:
        return int(self._lib.nemo_speech_diar_frame_count(self._handle))

    def new_probs(self) -> np.ndarray:
        """Probabilities for frames committed since the last call, shape (n, speakers).
        Call at least every few minutes: the runtime compacts frames older than
        about 20 minutes into segments."""
        count = self.frame_count()
        speakers = self.model.num_speakers
        if count <= self.read_frames:
            return np.zeros((0, speakers), dtype=np.float32)
        first = int(self._lib.nemo_speech_diar_frame_probs_start(self._handle))
        if first > self.read_frames:
            raise DiarizationError("Speaker labels fell behind the runtime's retained frames.")
        out = np.empty((count - first, speakers), dtype=np.float32)
        _check(self._lib, self._lib.nemo_speech_diar_frame_probs(
            self._handle, out.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), out.size), "reading probabilities")
        fresh = out[self.read_frames - first:].copy()
        self.read_frames = count
        return fresh

    def segments(self) -> list[tuple[float, float, int]]:
        """Library-postprocessed segments as (start, end, speaker index from 0)."""
        count = ctypes.c_size_t()
        _check(self._lib, self._lib.nemo_speech_diar_segments(self._handle, None, None, 0, ctypes.byref(count)),
               "reading segments")
        items = (_Segment * max(1, count.value))()
        _check(self._lib, self._lib.nemo_speech_diar_segments(self._handle, None, items, count.value,
                                                              ctypes.byref(count)), "reading segments")
        return [(float(s.start_time), float(s.end_time), int(s.speaker) - 1) for s in items[:count.value]]

    def close(self) -> None:
        if self._handle:
            self._lib.nemo_speech_diar_stream_close(self._handle)
            self._handle = ctypes.c_void_p()


# ── segments ───────────────────────────────────────────────────────────
def speaker_label(index: int) -> str:
    return f"SPEAKER_{index:02d}"


def exclusive_segments(segments, probs: np.ndarray, seconds_per_frame: float) -> list[dict]:
    """One speaker at a time: where postprocessed segments overlap, the speaker
    with the highest probability in that frame keeps it."""
    frames, speakers = probs.shape
    if not segments or not frames:
        return []
    active = np.zeros((frames, speakers), dtype=bool)
    for start, end, speaker in segments:
        if 0 <= speaker < speakers:
            active[max(0, int(start / seconds_per_frame)):min(frames, int(np.ceil(end / seconds_per_frame))), speaker] = True
    covered = active.any(axis=1)
    winner = np.where(active, probs, -1.0).argmax(axis=1)
    winner[~covered] = -1
    changes = np.flatnonzero(np.diff(winner)) + 1
    result = []
    for first, last in zip(np.concatenate(([0], changes)), np.concatenate((changes, [frames]))):
        if winner[first] >= 0:
            result.append({"start": round(first * seconds_per_frame, 3), "end": round(last * seconds_per_frame, 3),
                           "speaker": speaker_label(int(winner[first]))})
    return result


class LiveBinarizer:
    """Causal version of the runtime's segment postprocessing: onset/offset
    hysteresis, edge padding, gap filling and a minimum duration. Emits
    ``(speaker index, seconds, starting)`` once a decision can no longer change,
    so persisted turns never need rewriting. Defaults match NeMo-Speech.cpp's
    DiarSegmentationCfg."""

    def __init__(self, speakers: int, seconds_per_frame: float, *, onset=0.641, offset=0.561, pad_onset=0.229,
                 pad_offset=0.079, min_on=0.511, min_off=0.296):
        self.spf = seconds_per_frame
        self.onset, self.offset = onset, offset
        self.pad_onset, self.pad_offset = pad_onset, pad_offset
        self.min_on, self.min_off = min_on, min_off
        self.frame = 0
        self._raw = [False] * speakers
        self._open: list[dict | None] = [None] * speakers  # {"start", "end" (None while active), "confirmed"}

    def _start(self, run) -> float:
        return max(0.0, run["start"] * self.spf - self.pad_onset)

    def _end(self, run) -> float:
        return run["end"] * self.spf + self.pad_offset

    def settled_seconds(self) -> float:
        """Every start and end before this time has been reported. A short run
        can still merge with later speech and be reported from its own start."""
        pending = [self._start(run) for run in self._open if run is not None and not run["confirmed"]]
        return max(0.0, min([self.frame * self.spf - max(self.min_on, self.pad_onset + self.min_off)] + pending))

    def _gap_closed(self, run, frame) -> bool:
        """No speech starting at ``frame`` or later can merge with this run."""
        return (frame * self.spf - self.pad_onset) - self._end(run) >= self.min_off

    def _confirm(self, run, speaker, end, events) -> None:
        if not run["confirmed"] and end - self._start(run) >= self.min_on:
            run["confirmed"] = True
            events.append((speaker, self._start(run), True))

    def feed(self, probs: np.ndarray) -> list[tuple[int, float, bool]]:
        events = []
        for row in probs:
            for speaker, p in enumerate(row):
                run = self._open[speaker]
                if not self._raw[speaker] and p > self.onset:
                    self._raw[speaker] = True
                    if run is not None:
                        run["end"] = None  # the gap is short enough to fill, as the runtime merges it
                    else:
                        run = self._open[speaker] = {"start": self.frame, "end": None, "confirmed": False}
                elif self._raw[speaker] and p < self.offset:
                    self._raw[speaker] = False
                    run["end"] = self.frame
                if run is None:
                    continue
                # The runtime ends a run at the stream end without padding, so only
                # audio seen so far counts toward the minimum duration.
                seen = (self.frame + 1) * self.spf
                self._confirm(run, speaker, min(seen, self._end(run)) if run["end"] is not None else seen, events)
                if run["end"] is not None and self._gap_closed(run, self.frame + 1):
                    if run["confirmed"]:
                        events.append((speaker, self._end(run), False))
                    self._open[speaker] = None
            self.frame += 1
        return events

    def close(self) -> list[tuple[int, float, bool]]:
        """End every open run at the last frame."""
        events = []
        last = self.frame * self.spf
        for speaker, run in enumerate(self._open):
            if run is None:
                continue
            end = min(last, self._end(run)) if run["end"] is not None else last
            self._confirm(run, speaker, end, events)
            if run["confirmed"]:
                events.append((speaker, end, False))
            self._open[speaker] = None
        self._raw = [False] * len(self._raw)
        return events


# ── service ────────────────────────────────────────────────────────────
class LocalDiarizer:
    """Owns the downloaded model and shares loaded copies between live streams
    and batch jobs. A model stays loaded only while something uses it."""

    def __init__(self, models, *, runtime=load_runtime):
        self.models = models
        self._runtime = runtime
        self._lock = threading.Lock()
        self._loaded: dict[tuple, list] = {}  # geometry -> [DiarModel, users, file lease]

    def runtime_error(self) -> str:
        try:
            self._runtime()
            return ""
        except DiarizationError as exc:
            return str(exc)

    def info(self) -> dict:
        error = self.runtime_error()
        installed = bool(self.models.files_present())
        return {"configured": installed and not error, "installed": installed, "runtime": not error,
                "error": error, "provider": LABEL, "model": MODEL_FILE.removesuffix(".gguf"), "host": "",
                "remote": False}

    @contextmanager
    def lease(self, geometry: tuple[str, int, int]):
        with self._lock:
            entry = self._loaded.get(geometry)
            if entry is None:
                if not self.models.files_present():
                    raise DiarizationError("The speaker model is not downloaded. Download it in Settings > Meetings.")
                use = self.models.use()
                path = use.__enter__()
                try:
                    entry = [DiarModel(self._runtime(), Path(path) / MODEL_FILE, geometry), 0, use]
                except BaseException:
                    use.__exit__(None, None, None)
                    raise
                self._loaded[geometry] = entry
            entry[1] += 1
        try:
            yield entry[0]
        finally:
            with self._lock:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._loaded[geometry]
                    entry[0].close()
                    entry[2].__exit__(None, None, None)

    def diarize(self, samples: np.ndarray, *, should_stop=None, on_progress=None) -> list[dict]:
        """Label one saved mono 16 kHz int16 track. Returns exclusive segments
        ``{"start", "end", "speaker": "SPEAKER_00"}`` sorted by start."""
        total = len(samples)
        with self.lease(BATCH_PRESET) as model:
            stream = model.open_stream()
            try:
                probs = []
                for start in range(0, total, BATCH_BLOCK):
                    if should_stop is not None and should_stop():
                        return []
                    stream.push(samples[start:start + BATCH_BLOCK].astype(np.float32) / 32768.0)
                    probs.append(stream.new_probs())
                    if on_progress is not None:
                        on_progress(min(total, start + BATCH_BLOCK) / SAMPLE_RATE, total / SAMPLE_RATE)
                stream.finish()
                probs.append(stream.new_probs())
                timeline = np.concatenate(probs) if probs else np.zeros((0, model.num_speakers), np.float32)
                return exclusive_segments(stream.segments(), timeline, model.seconds_per_frame)
            finally:
                stream.close()


def run_smoke_test(argv) -> int:
    """Label one WAV with the bundled runtime, without tray or user config:
    Ownkey.exe --diarization-smoke-test --model-dir DIR --wav FILE --output REPORT.json"""
    import argparse
    import json
    import time
    import wave

    from local_models import LocalModelManager

    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--wav", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    report = {"success": False, "runtime_dir": str(runtime_dir())}
    try:
        with wave.open(args.wav, "rb") as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, SAMPLE_RATE):
                raise DiarizationError("Expected mono 16-bit WAV at 16 kHz.")
            samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
        diarizer = LocalDiarizer(LocalModelManager(model=NEMOTRON_DIARIZATION, directory=args.model_dir))
        began = time.perf_counter()
        segments = diarizer.diarize(samples)
        report.update(success=bool(segments), seconds=round(time.perf_counter() - began, 2),
                      audio_seconds=round(len(samples) / SAMPLE_RATE, 2), info=diarizer.info(),
                      speakers=sorted({s["speaker"] for s in segments}), segments=segments)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(run_smoke_test(sys.argv[1:]))
