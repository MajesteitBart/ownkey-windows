"""Live input meter for the New meeting sheet.

The preview opens the chosen microphone through the same source a recording
uses, turns every block into one level number and drops the audio. Nothing is
kept or written. It stops by itself when nobody has asked for the level for a
few seconds, so a closed window cannot leave the microphone open.
"""

from __future__ import annotations

import queue
import threading
import time

from . import audio

_NOT_STARTED = object()


class MicPreview:
    IDLE_SECONDS = 2.5      # no reader for this long: close the microphone
    RETRY_SECONDS = 2.0     # a device that failed to open is not hammered by the poll
    TICK_SECONDS = 0.05
    DECAY = 0.82            # per tick, so the meter falls back in about half a second

    def __init__(self, make_source, *, clock=time.monotonic) -> None:
        self._make_source = make_source  # device -> capture source with start(), stop(), blocks
        self._clock = clock
        self._lock = threading.Lock()
        self._source = None
        self._device = _NOT_STARTED
        self._level = 0.0
        self._error = ""
        self._asked = 0.0
        self._retry_at = 0.0

    @property
    def active(self) -> bool:
        return self._source is not None

    def read(self, device=None) -> dict:
        """The current level for this device. Starts or switches the preview as needed."""
        with self._lock:
            now = self._clock()
            self._asked = now
            if device != self._device:
                self._stop_locked()
                self._device, self._retry_at = device, 0.0
            if self._source is None and now >= self._retry_at:
                self._start_locked(device, now)
            return {"active": self._source is not None, "level": round(self._level, 3), "error": self._error}

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()
            self._device = _NOT_STARTED
            self._error = ""

    def _start_locked(self, device, now: float) -> None:
        try:
            source = self._make_source(device)
            source.start()
        except Exception as exc:
            self._error = str(exc) or exc.__class__.__name__
            self._retry_at = now + self.RETRY_SECONDS
            return
        self._source, self._error, self._level = source, "", 0.0
        threading.Thread(target=self._run, args=(source,), daemon=True, name="meeting-mic-preview").start()

    def _stop_locked(self) -> None:
        source, self._source = self._source, None
        self._level = 0.0
        if source is not None:
            try:
                source.stop()
            except Exception:
                pass

    def _run(self, source) -> None:
        while True:
            time.sleep(self.TICK_SECONDS)
            with self._lock:
                if self._source is not source:
                    return  # stopped or replaced by another device
                if self._clock() - self._asked > self.IDLE_SECONDS:
                    self._stop_locked()
                    self._device = _NOT_STARTED
                    return
                peak = 0.0
                while True:
                    try:
                        samples, _at = source.blocks.get_nowait()
                    except queue.Empty:
                        break
                    peak = max(peak, audio.meter_level(samples))  # the samples go no further than this
                self._level = max(peak, self._level * self.DECAY)
                if source.error:
                    self._error = source.error
                    self._retry_at = self._clock() + self.RETRY_SECONDS
                    self._stop_locked()
                    return
