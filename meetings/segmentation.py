"""Bounded, causal speech windows shared by meetings and long local dictation."""

from dataclasses import dataclass

import numpy as np

RATE = 16000
FRAME = 800
CORE_LIMIT = 27 * RATE
LEFT_CONTEXT = int(.6 * RATE)
RIGHT_CONTEXT = int(.2 * RATE)


@dataclass(frozen=True)
class Boundary:
    end: int
    reason: str


def next_boundary(samples: np.ndarray, *, final=False, speaker_cuts=()) -> Boundary | None:
    """Return the first available boundary; offsets are relative to samples[0]."""
    total = samples.size
    if not total:
        return None
    frames = total // FRAME
    energy = np.abs(samples[:frames * FRAME].astype(np.float32)).reshape(-1, FRAME).mean(axis=1)
    run, heard, boundary = 0, False, None
    for i in range(min(frames, CORE_LIMIT // FRAME)):
        history = energy[max(0, i - 199):i + 1]
        nonzero = history[history > 0]
        threshold = max(60., .08 * float(np.percentile(nonzero, 90))) if nonzero.size else 60.
        quiet = energy[i] < threshold
        heard |= not quiet
        run = run + 1 if quiet else 0
        needed = .3 if (i + 1) * FRAME >= 12 * RATE else .8
        if heard and run * FRAME >= round(needed * RATE):
            end = (i + 1) * FRAME - run * FRAME // 2
            boundary = Boundary(end, 'pause')
            break
        if not heard and (i + 1) * FRAME >= 5 * RATE:
            boundary = Boundary((i + 1) * FRAME, 'silence')
            break
    # A cut must have arrived with enough following audio to survive overlap and jitter.
    cuts = [int(c) for c in speaker_cuts if .15 * RATE < c <= CORE_LIMIT and c + .4 * RATE <= total]
    if cuts and (boundary is None or min(cuts) < boundary.end):
        boundary = Boundary(min(cuts), 'speaker')
    if boundary is None and total >= CORE_LIMIT + RIGHT_CONTEXT:
        lo, hi = (CORE_LIMIT - 8 * RATE) // FRAME, CORE_LIMIT // FRAME
        end = (lo + int(np.argmin(energy[lo:hi]))) * FRAME + FRAME // 2
        boundary = Boundary(end, 'limit')
    if boundary is None and final:
        boundary = Boundary(min(total, CORE_LIMIT), 'tail')
    if boundary and not final and boundary.end + RIGHT_CONTEXT > total:
        return None
    return boundary


def turn_boundaries(segments: list[dict], start_sample: int) -> list[int]:
    """Only non-overlapping changes between different speakers suggest a cut."""
    ordered = sorted(segments, key=lambda s: s['start'])
    cuts = []
    for before, after in zip(ordered, ordered[1:]):
        if before['speaker'] != after['speaker'] and 0 <= after['start'] - before['end'] <= 2:
            # Other overlapping voices must also have ended before this change.
            at = (before['end'] + after['start']) / 2
            if not any(s['start'] < at < s['end'] for s in ordered):
                cuts.append(round(at * RATE) - start_sample)
    return cuts


def owned_tokens(tokens, timestamps, durations, *, offset: float, start: float, end: float):
    """Assign complete whitespace-delimited token groups to one owned interval."""
    words, current = [], []
    for i, piece in enumerate(tokens):
        at = offset + (float(timestamps[i]) if i < len(timestamps) else 0.)
        until = at + max(.02, float(durations[i]) if i < len(durations) else .2)
        if current and piece[:1].isspace():
            words.append(current)
            current = []
        current.append((piece, at, until))
    if current:
        words.append(current)
    result = []
    for word in words:
        midpoint = (word[0][1] + word[-1][2]) / 2
        if start <= midpoint < end:
            result.extend(word)
    return result


def window_passages(result, *, source: str, offset: float, start: float, end: float,
                    segments=()) -> list[dict]:
    """Group owned words by speaker without changing existing passage IDs."""
    text, tokens, stamps, durations = result
    owned = owned_tokens(tokens, stamps, durations, offset=offset, start=start, end=end)
    if not tokens:
        if not text.strip():
            return []
        # Text-only providers cannot attribute a mixed turn word by word.
        speakers = {s['speaker'] for s in segments if s['start'] < end and s['end'] > start}
        return [{'source': source, 'speaker_id': next(iter(speakers)) if len(speakers) == 1 else source,
                 'start': start, 'end': end, 'text': ' '.join(text.split()), 'quality': 'window'}]
    passages = []
    words = []
    for token in owned:
        if not words or token[0][:1].isspace():
            words.append([])
        words[-1].append(token)
    for word in words:
        at, until = word[0][1], word[-1][2]
        overlaps = {s['speaker'] for s in segments if s['start'] < until and s['end'] > at}
        speaker = next(iter(overlaps)) if len(overlaps) == 1 else source
        if not passages or passages[-1]['speaker_id'] != speaker or at - passages[-1]['end'] >= .9:
            passages.append({'source': source, 'speaker_id': speaker, 'start': max(0., at),
                             'end': until, 'text': '', 'tokens': [], 'quality': 'timed'})
        p = passages[-1]
        p['text'] += ''.join(token[0] for token in word)
        p['end'] = max(p['end'], until)
        p['tokens'].extend(word)
    for p in passages:
        p['text'] = ' '.join(p['text'].split())
    return [p for p in passages if p['text']]
