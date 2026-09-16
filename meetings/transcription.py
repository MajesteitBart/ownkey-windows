"""Turn recorded tracks into timed passages with Orukeet.

Each track is decoded in bounded windows that end at the quietest moment
near the window limit. The recognizer returns per-token timestamps, so a
window's tokens become passages split at pauses. Passage ids are stable
per meeting (``p0001`` upwards, per source in decode order) and carry the
audio time of the track, which is also the playback time.
"""

from __future__ import annotations

import math
import re

import numpy as np

from . import audio

WINDOW_SECONDS = 28.0        # decode at most this much audio at once
WINDOW_MIN_SECONDS = 12.0    # never cut a window shorter than this
BOUNDARY_SEARCH_SECONDS = 8.0
QUIET_FRAME_SECONDS = 0.05
SILENT_WINDOW_DB = -55.0     # a window this quiet is skipped without decoding
PASSAGE_PAUSE_SECONDS = 0.9  # a gap between tokens this long starts a new passage
PASSAGE_MAX_SECONDS = 40.0
SENTENCE_END = re.compile(r"[.!?…]$")


def _sanitize_text(text: str) -> str:
    """Collapse whitespace; the recognizer's own punctuation is kept."""
    return " ".join(str(text).split())


def window_bounds(samples: np.ndarray, rate: int = audio.SAMPLE_RATE) -> list[tuple[int, int]]:
    """Split a track into (start, end) sample ranges at quiet moments."""
    total = int(samples.size)
    max_len = int(WINDOW_SECONDS * rate)
    min_len = int(WINDOW_MIN_SECONDS * rate)
    search = int(BOUNDARY_SEARCH_SECONDS * rate)
    frame = max(1, int(QUIET_FRAME_SECONDS * rate))
    bounds = []
    start = 0
    while start < total:
        end = min(total, start + max_len)
        if end < total:
            region_start = max(start + min_len, end - search)
            region = samples[region_start:end]
            if region.size >= frame:
                usable = (region.size // frame) * frame
                energy = np.abs(region[:usable].astype(np.float32)).reshape(-1, frame).mean(axis=1)
                quietest = int(np.argmin(energy))
                end = region_start + quietest * frame + frame // 2
        bounds.append((start, end))
        start = end
    return bounds


def tokens_to_passages(tokens: list[str], timestamps: list[float], durations: list[float], *,
                       offset: float, window_end: float) -> list[dict]:
    """Group tokens into passages split at pauses. Times are track seconds."""
    if not tokens:
        return []
    groups: list[dict] = []
    current = None
    for index, token in enumerate(tokens):
        start = float(timestamps[index]) if index < len(timestamps) else (current["end"] if current else 0.0)
        duration = float(durations[index]) if index < len(durations) else 0.2
        end = start + max(duration, 0.02)
        text_piece = token
        if current is not None:
            pause = start - current["end"]
            sentence_done = bool(SENTENCE_END.search(current["text"].rstrip()))
            too_long = start - current["start"] >= PASSAGE_MAX_SECONDS
            if pause >= PASSAGE_PAUSE_SECONDS or (sentence_done and pause >= 0.35) or too_long:
                groups.append(current)
                current = None
        if current is None:
            current = {"start": start, "end": end, "text": text_piece.lstrip()}
        else:
            current["text"] += text_piece
            current["end"] = max(current["end"], end)
    if current is not None:
        groups.append(current)
    passages = []
    for group in groups:
        text = _sanitize_text(group["text"])
        if not text:
            continue
        passages.append({
            "start": round(offset + group["start"], 2),
            "end": round(min(offset + group["end"], window_end), 2),
            "text": text,
        })
    return passages


def transcribe_track(samples: np.ndarray, decode, *, source: str, first_index: int = 1, on_progress=None,
                     should_stop=None) -> list[dict]:
    """Decode one int16 16 kHz track into passages.

    ``decode(wav_bytes)`` returns ``(text, tokens, timestamps, durations)``.
    ``on_progress(done_seconds, total_seconds, new_passages)`` is called per
    window so callers can store passages early and report progress.
    """
    rate = audio.SAMPLE_RATE
    passages: list[dict] = []
    index = first_index
    total = samples.size / rate
    for start, end in window_bounds(samples, rate):
        if should_stop is not None and should_stop():
            break
        window = samples[start:end]
        offset = start / rate
        window_end = end / rate
        new: list[dict] = []
        if window.size >= rate * 0.3 and audio.rms_db(window) > SILENT_WINDOW_DB:
            text, tokens, timestamps, durations = decode(audio.wav_bytes(window, rate))
            if tokens:
                new = tokens_to_passages(tokens, timestamps, durations, offset=offset, window_end=window_end)
            elif text.strip():
                new = [{"start": round(offset, 2), "end": round(window_end, 2), "text": _sanitize_text(text)}]
        for passage in new:
            passage["id"] = f"p{index:04d}"
            passage["source"] = source
            passage["speaker_id"] = source
            passage["quality"] = "timed" if tokens_have_timing(passage) else "window"
            index += 1
        passages.extend(new)
        if on_progress is not None:
            on_progress(min(total, window_end), total, new)
    return passages


def tokens_have_timing(passage: dict) -> bool:
    return passage["end"] > passage["start"]


def merge_tracks(tracks: dict[str, list[dict]]) -> list[dict]:
    """Interleave passages from several tracks by start time, then renumber
    ids in that order so citations read in transcript order."""
    merged = sorted((p for items in tracks.values() for p in items), key=lambda p: (p["start"], p["source"]))
    renumbered = []
    for position, passage in enumerate(merged, start=1):
        copy = dict(passage)
        copy["id"] = f"p{position:04d}"
        renumbered.append(copy)
    return renumbered


def format_time(seconds: float) -> str:
    seconds = max(0, int(math.floor(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
