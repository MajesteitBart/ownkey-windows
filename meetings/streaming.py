"""Paced pyannoteAI streaming, isolated from capture and transcription."""

from bisect import bisect_right
import json
import math
import queue
import secrets
import threading
import time

import numpy as np

RATE = 16000
BLOCK = 1600


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
    def __init__(self, key, source, *, paused, on_event, on_status, connect=None, session=None):
        self.key, self.source = key, source
        self.paused, self.on_event, self.on_status = paused, on_event, on_status
        self._connect, self._http = connect, session
        self._queue = queue.Queue(maxsize=40)
        self._lock = threading.Lock()
        self._tail = np.zeros(0, dtype=np.int16)
        self._tail_start = 0
        self._stop = threading.Event()
        self._finish = threading.Event()
        self._overflow = threading.Event()
        self._socket = None
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
        sock = self._socket
        if sock:
            try:
                sock.close()
            except Exception:
                pass

    def _run(self):
        import requests
        from websockets.sync.client import connect

        http = self._http or requests.Session()
        connector = self._connect or connect
        for attempt in range(3):
            if self._stop.is_set():
                break
            epoch = secrets.token_hex(4)
            self.on_status(self.source, 'connecting', '', epoch)
            try:
                response = http.post('https://api.pyannote.ai/v1/live',
                    headers={'Authorization': f'Bearer {self.key}'}, json={}, timeout=20)
                if response.status_code != 200:
                    message = ('pyannoteAI rejected the key.' if response.status_code == 401 else
                               'pyannoteAI could not open live speaker labels. Recording continues.')
                    self.on_status(self.source, 'error', message, epoch)
                    break
                if self._stop.is_set():
                    break
                with connector(response.json()['url'], open_timeout=25, close_timeout=2) as sock:
                    self._socket = sock
                    clock = StreamClock()
                    receiver_done = threading.Event()
                    remote_error = threading.Event()

                    def receive(sock=sock, clock=clock, epoch=epoch,
                                remote_error=remote_error, receiver_done=receiver_done):
                        try:
                            while not self._stop.is_set():
                                try:
                                    raw = sock.recv(timeout=.5)
                                except TimeoutError:
                                    continue
                                item = json.loads(raw)
                                kind = item.get('type')
                                if kind == 'error':
                                    remote_error.set()
                                    return
                                if kind not in ('diarization_speaker_start', 'diarization_speaker_end'):
                                    continue
                                data = item.get('data') or {}
                                timestamp = float(data.get('timestamp', -1))
                                if not math.isfinite(timestamp):
                                    continue
                                ending = kind == 'diarization_speaker_end'
                                mapped = clock.at(timestamp, ending=ending)
                                label = str(data.get('speaker', ''))
                                if mapped is not None and label and len(label) <= 80:
                                    self.on_event(self.source, epoch, label, mapped, not ending)
                        except Exception:
                            pass  # errors can contain the credential-bearing socket URL
                        finally:
                            receiver_done.set()

                    receiver = threading.Thread(target=receive, name=f'speaker-events-{self.source}', daemon=True)
                    receiver.start()
                    self.on_status(self.source, 'listening', '', epoch)
                    due = time.monotonic()
                    finalized = False
                    while not self._stop.is_set():
                        if self._overflow.is_set() or time.monotonic() - due > 3:
                            raise RuntimeError('stream behind')
                        if receiver_done.is_set() or remote_error.is_set():
                            raise RuntimeError('stream closed')
                        try:
                            position, pcm = self._queue.get(timeout=.1)
                        except queue.Empty:
                            if self._finish.is_set():
                                sock.send(json.dumps({'type': 'end_of_stream'}))
                                receiver_done.wait(5)
                                if remote_error.is_set() or not receiver_done.is_set():
                                    raise RuntimeError('stream did not finalize')
                                finalized = True
                                break
                            if self.paused():
                                position, pcm = None, np.zeros(BLOCK, dtype=np.int16)
                            else:
                                due = max(due, time.monotonic())
                                continue
                        if self._stop.wait(max(0., due - time.monotonic())):
                            break
                        clock.append(position)
                        sock.send((pcm.astype('<f4') / 32768.).tobytes())
                        due = max(due + .1, time.monotonic())
                    sock.close()
                    receiver.join(timeout=2)
                    if finalized or self._stop.is_set():
                        self.on_status(self.source, 'stopped', '', epoch)
                        break
            except Exception:
                self.on_status(self.source, 'error',
                    'Live speaker labels disconnected. Pause-based transcription continues.', epoch)
                # A fresh stream starts at current audio. Its labels have a new namespace.
                while True:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        break
                self._overflow.clear()
                if self._finish.is_set() or self._stop.wait(2):
                    break
            finally:
                self._socket = None
        if self._http is None:
            http.close()
