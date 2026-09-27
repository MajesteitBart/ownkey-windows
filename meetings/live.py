"""Incremental recognition of saved audio, with optional live speaker turns."""

import threading
import time

from . import audio
from .segmentation import (CORE_LIMIT, LEFT_CONTEXT, RIGHT_CONTEXT, RATE,
                           next_boundary, turn_boundaries, window_passages)
from .streaming import LiveSpeakerStream

SPEAKER_DRAIN_SECONDS = 15  # after Stop, text waits at most this long for speaker turns


class LiveTranscription:
    def __init__(self, store, job, *, sources, decoder_factory, on_complete, capture=None,
                 diarizer=None, speaker_sources=(), stream_factory=LiveSpeakerStream,
                 timed=True, yield_to=lambda: False):
        self.store, self.job = store, job
        self.meeting_id = job['meeting_id']
        self.sources, self.capture = list(sources), capture
        self.decoder_factory, self.on_complete = decoder_factory, on_complete
        self.timed, self.yield_to = timed, yield_to
        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        self._state, self._error = 'listening', ''
        self._active, self._stream_states, self._speaker_ids = {}, {}, {}
        self._positions = {s: store.processed_samples(self.meeting_id, s) for s in self.sources}
        self.streams = {}
        if diarizer is not None and capture:
            for source in speaker_sources:
                self.streams[source] = stream_factory(source, diarizer=diarizer, on_event=self._speaker_event,
                                                      on_status=self._stream_status, yield_to=yield_to)
        self.thread = threading.Thread(target=self.run, name='meeting-live-transcription', daemon=True)

    def start(self):
        for stream in self.streams.values():
            stream.start()
        self.thread.start()

    def feed(self, source, start, samples):
        if not self._cancelled.is_set() and source in self.streams:
            self.streams[source].feed(start, samples)

    def finish(self):
        for stream in self.streams.values():
            stream.finish()

    def cancel(self):
        self._cancelled.set()
        for stream in self.streams.values():
            stream.cancel()

    def join(self, timeout=2):
        self.thread.join(timeout=timeout)
        for stream in self.streams.values():
            stream.thread.join(timeout=.2)

    @property
    def alive(self):
        return self.thread.is_alive() or any(s.thread.is_alive() for s in self.streams.values())

    def status(self):
        totals, _, paused = self._totals()
        with self._lock:
            active = list(self._active)
            if self.capture and self.capture.state != 'recording':
                active = []
            state = ('paused' if paused else 'listening') if self._state in ('paused', 'listening') else self._state
            return {'state': state, 'error': self._error,
                    'pending_seconds': round(max(0., max((totals.get(s, 0) - p) / RATE
                                                        for s, p in self._positions.items())), 1),
                    'processed': {s: round(p / RATE, 2) for s, p in self._positions.items()},
                    'active_speakers': active, 'streams': dict(self._stream_states)}

    def _stream_status(self, source, state, error, epoch):
        if self._cancelled.is_set():
            return
        with self._lock:
            self._stream_states[source] = {'state': state, 'error': error}
            if state in ('error', 'stopped'):
                ids = [sid for sid, data in self._active.items() if data[0] == source]
                for sid in ids:
                    _source, start = self._active.pop(sid)
                    total = self.capture.summary()['sources'].get(source, 0) / RATE if self.capture else start
                    self.store.speaker_turn(self.meeting_id, source, sid, max(start, total), False)

    def _speaker_event(self, source, epoch, label, at, starting):
        if self._cancelled.is_set() or self.store.get_meeting(self.meeting_id) is None:
            return
        with self._lock:
            key = (source, epoch, label)
            sid = self._speaker_ids.get(key)
            if sid is None:
                sid = f'live-{source}-{epoch}-{len(self._speaker_ids) + 1}'
                existing = self.store.list_speakers(self.meeting_id)
                number = 1 + sum(s['id'].startswith('live-') for s in existing)
                if not self.store.ensure_speaker(self.meeting_id, sid, source, f'Speaker {number}'):
                    return
                self._speaker_ids[key] = sid
            self.store.speaker_turn(self.meeting_id, source, sid, at, starting)
            if starting:
                self._active[sid] = (source, at)
            else:
                self._active.pop(sid, None)

    def _speaker_gate(self, source, paused, ended_at):
        """While recording, cut windows only where speaker turns are final. Pause
        flushes as before, and after Stop a stuck speaker model cannot hold text back."""
        stream = self.streams.get(source)
        if stream is None or paused:
            return None
        if ended_at is not None and time.monotonic() - ended_at > SPEAKER_DRAIN_SECONDS:
            stream.cancel()
            return None
        return stream.settled_sample()

    def _totals(self):
        if self.capture:
            snapshot = self.capture.summary()
            return snapshot['sources'], snapshot['state'] not in ('recording', 'paused'), snapshot['state'] == 'paused'
        totals = {s: 0 for s in self.sources}
        for c in self.store.list_chunks(self.meeting_id):
            totals[c['source']] = max(totals.get(c['source'], 0), c['start_sample'] + c['n_samples'])
        return totals, True, False

    def run(self):
        close_decoder = lambda: None
        try:
            self.store.update_job(self.job['id'], state='running', detail='Listening for a pause')
            decode, close_decoder = self.decoder_factory()
            ended_at = None
            while not self._cancelled.is_set():
                totals, ended, paused = self._totals()
                if ended:
                    self.finish()
                    ended_at = ended_at or time.monotonic()
                progressed = False
                for source in self.sources:
                    if self._cancelled.is_set():
                        return
                    if self.yield_to():
                        continue
                    start = self._positions[source]
                    total = totals.get(source, 0)
                    gate = self._speaker_gate(source, paused, ended_at)
                    available = total if gate is None else min(total, gate)
                    if start >= available:
                        continue
                    left = max(0, start - LEFT_CONTEXT) if self.timed else start
                    count = CORE_LIMIT + RIGHT_CONTEXT + start - left
                    data = (self.capture.read_audio(source, left, count) if self.capture else
                            self.store.read_audio(self.meeting_id, source, left, min(left + count, total)))
                    # Audio past the gate still gives recognition its right context.
                    owned = data[start - left:available - left]
                    segments = self.store.speaker_segments(self.meeting_id, source, start / RATE,
                                                          (start + owned.size) / RATE)
                    boundary = next_boundary(owned, final=(ended or paused) and available >= total,
                                             speaker_cuts=turn_boundaries(segments, start))
                    if boundary is None:
                        continue
                    end = start + boundary.end
                    # The writer, not recognition, owns the files. Its short lock flush
                    # completes before any model or network call below.
                    if self.capture:
                        self.capture.flush_audio()
                    core = owned[:boundary.end]
                    passages = []
                    if core.size >= .08 * RATE and audio.rms_db(core) > -55:
                        with self._lock:
                            self._state = 'finalizing' if ended else 'transcribing'
                        self.store.update_job(self.job['id'], detail='Transcribing recent speech')
                        right = min(data.size, end - left + (RIGHT_CONTEXT if self.timed else 0))
                        result = decode(audio.wav_bytes(data[:right]))
                        if self._cancelled.is_set():
                            return
                        segments = self.store.speaker_segments(self.meeting_id, source, left / RATE,
                                                              (left + right) / RATE)
                        passages = window_passages(result, source=source, offset=left / RATE,
                                                   start=start / RATE, end=end / RATE, segments=segments)
                    if not self.store.commit_window(self.meeting_id, self.job['id'], source, start, end,
                                                    boundary.reason, passages):
                        return
                    with self._lock:
                        self._positions[source] = end
                        self._state = 'finalizing' if ended else 'paused' if paused else 'listening'
                    progressed = True
                if ended and all(self._positions[s] >= totals.get(s, 0) for s in self.sources):
                    # Drain final speaker events without making transcript completion
                    # wait indefinitely on a slow speaker model.
                    for stream in self.streams.values():
                        stream.thread.join(timeout=6)
                        if stream.thread.is_alive():
                            stream.cancel()
                    if not self._cancelled.is_set() and self.store.get_meeting(self.meeting_id):
                        self.on_complete()
                        self.store.update_job(self.job['id'], state='done', progress=1., detail='Transcription complete')
                        with self._lock:
                            self._state = 'done'
                    return
                self._cancelled.wait(.03 if progressed else .15)
        except Exception as exc:
            if not self._cancelled.is_set():
                # Provider errors may include request details; the standard service
                # formatter translates them before they enter this layer.
                message = str(exc) or type(exc).__name__
                with self._lock:
                    self._state, self._error = 'error', message
                self.store.update_job(self.job['id'], state='error', error=message)
        finally:
            close_decoder()
