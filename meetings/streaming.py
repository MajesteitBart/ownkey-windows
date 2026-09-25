"""Live speaker activity on this PC, isolated from capture and transcription."""

from bisect import bisect_right
import math
import queue
import secrets
import threading

import numpy as np

RATE = 16000
BLOCK = 1600
QUEUE_BLOCKS = 300  # 30 seconds: covers dictation holds and slow model steps


class StreamClock:
    """Map stream sample time to saved audio, excluding generated pause silence."""
    def __init__(self):
        self._lock = threading.Lock()
        self.starts, self.offsets = [], []
        self.sent = 0

    def append(self, sample: int | None):
        with self._lock:
            expected = (self.offsets[-1] + self.sent - self.starts[-1]
                        if self.offsets and self.offsets[-1] is not None else None)
            if not self.starts or sample != expected:
                self.starts.append(self.sent)
                self.offsets.append(sample)
            self.sent += BLOCK

    def at(self, seconds: float, *, ending=False):
        with self._lock:
            if not math.isfinite(seconds):
                return None
            sample = round(seconds * RATE)
            if sample < 0 or sample > self.sent or not self.starts:
                return None
            index = bisect_right(self.starts, sample) - 1
            offset = self.offsets[index]
            if offset is not None:
                return (offset + sample - self.starts[index]) / RATE
            if ending and index:
                previous = self.offsets[index - 1]
                if previous is not None:
                    return (previous + self.starts[index] - self.starts[index - 1]) / RATE
            return None


class LiveSpeakerStream:
    """Feed one source's saved-time audio to a local diarizer stream and report
    speaker starts and ends in saved-audio seconds. Paused audio is never fed,
    so the model sees the meeting without the gaps; the clock maps back.

    ``settled_sample()`` tells transcription how far speaker turns are final,
    so a window is only attributed once its turns are known."""

    def __init__(self, source, *, diarizer, on_event, on_status, yield_to=lambda: False):
        from local_diarization import LIVE_PRESET

        self.source, self.diarizer, self.preset = source, diarizer, LIVE_PRESET
        self.on_event, self.on_status, self.yield_to = on_event, on_status, yield_to
        self._queue = queue.Queue(maxsize=QUEUE_BLOCKS)
        self._lock = threading.Lock()
        self._tail = np.zeros(0, dtype=np.int16)
        self._tail_start = 0
        self._stop = threading.Event()
        self._finish = threading.Event()
        self._overflow = threading.Event()
        self._settled = 0
        self.thread = threading.Thread(target=self._run, name=f'meeting-speakers-{source}', daemon=True)

    def start(self):
        self.thread.start()

    def feed(self, start, data):
        if self._stop.is_set() or self._finish.is_set():
            return
        with self._lock:
            if not self._tail.size:
                self._tail_start = start
            self._tail = np.concatenate((self._tail, data))
            while self._tail.size >= BLOCK:
                self._put(self._tail_start, self._tail[:BLOCK].copy())
                self._tail = self._tail[BLOCK:]
                self._tail_start += BLOCK

    def _put(self, start, block):
        try:
            self._queue.put_nowait((start, block))
        except queue.Full:
            self._overflow.set()

    def finish(self):
        with self._lock:
            if self._finish.is_set():
                return
            if self._tail.size:
                self._put(self._tail_start, np.pad(self._tail, (0, BLOCK - self._tail.size)))
                self._tail = np.zeros(0, dtype=np.int16)
            self._finish.set()

    def cancel(self):
        self._stop.set()

    def settled_sample(self):
        """Saved-audio sample before which every speaker event is reported, or
        None once this stream no longer produces events."""
        with self._lock:
            return None if self._stop.is_set() else self._settled

    def _settle(self, clock, binarizer):
        until = clock.at(binarizer.settled_seconds())
        if until is not None:
            with self._lock:
                if self._settled is not None:
                    self._settled = max(self._settled, round(until * RATE))

    def _emit(self, epoch, clock, events):
        for speaker, seconds, starting in events:
            mapped = clock.at(seconds, ending=not starting)
            if mapped is None and not starting:
                mapped = clock.at(min(seconds, clock.sent / RATE), ending=True)
            if mapped is not None:
                self.on_event(self.source, epoch, str(speaker + 1), mapped, starting)

    def _run(self):
        from local_diarization import LiveBinarizer

        epoch = secrets.token_hex(4)
        self.on_status(self.source, 'connecting', '', epoch)
        try:
            with self.diarizer.lease(self.preset) as model:
                stream = model.open_stream()
                try:
                    binarizer = LiveBinarizer(model.num_speakers, model.seconds_per_frame)
                    clock = StreamClock()
                    self.on_status(self.source, 'listening', '', epoch)
                    while not self._stop.is_set():
                        if self._overflow.is_set():
                            raise OverflowError
                        # Dictation comes first; the queue holds audio meanwhile.
                        if self.yield_to():
                            self._stop.wait(.05)
                            continue
                        try:
                            position, pcm = self._queue.get(timeout=.1)
                        except queue.Empty:
                            # finish() queues the padded tail before setting the flag.
                            if self._finish.is_set() and self._queue.empty():
                                break
                            continue
                        clock.append(position)
                        stream.push(pcm.astype(np.float32) / 32768.)
                        self._emit(epoch, clock, binarizer.feed(stream.new_probs()))
                        self._settle(clock, binarizer)
                    if self._stop.is_set():
                        return
                    stream.finish()
                    self._emit(epoch, clock, binarizer.feed(stream.new_probs()) + binarizer.close())
                    self.on_status(self.source, 'stopped', '', epoch)
                finally:
                    stream.close()
        except OverflowError:
            self.on_status(self.source, 'error', 'Live speaker labels could not keep up on this PC. '
                           'Recording continues; add speaker labels after Stop.', epoch)
        except Exception as exc:
            self.on_status(self.source, 'error', f'Live speaker labels stopped: {exc}'.strip(), epoch)
        finally:
            with self._lock:
                self._settled = None
