"""Align local speaker segments to timed passages.

Nemotron 3 Diarization runs on this PC (see local_diarization.py) and
returns exclusive segments. Every token takes the speaker whose segment
overlaps it most, and passages split where the speaker changes. Labels
start as Speaker 1, Speaker 2, … in order of first appearance; the user
confirms names later.
"""

from __future__ import annotations

NEAREST_SECONDS = 1.0        # a token this close to a segment adopts its speaker


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def speaker_for(start: float, end: float, segments: list[dict], fallback: str | None = None) -> str | None:
    """Speaker with the most overlap; otherwise the nearest segment within a
    second; otherwise the fallback."""
    best, best_overlap = None, 0.0
    nearest, nearest_distance = None, NEAREST_SECONDS
    for segment in segments:
        overlap = _overlap(start, end, segment["start"], segment["end"])
        if overlap > best_overlap:
            best, best_overlap = segment["speaker"], overlap
        distance = max(segment["start"] - end, start - segment["end"], 0.0)
        if overlap == 0.0 and distance < nearest_distance:
            nearest, nearest_distance = segment["speaker"], distance
    return best or nearest or fallback


def assign_speakers(passages: list[dict], segments: list[dict], *, source: str,
                    fallback_speaker: str, first_number: int = 1,
                    preserve_passages: bool = False) -> tuple[list[dict], list[dict]]:
    """Label one source's passages, splitting at speaker changes.

    Returns ``(new_passages, speakers)`` where speakers are
    ``{"id": "<source>-1", "name": "Speaker 1", "label": "SPEAKER_00"}`` in
    order of first appearance. ``first_number`` continues the numbering when
    several tracks are labelled, so a meeting never shows two Speaker 1s.
    Passages nobody claims keep the fallback speaker (the source label).
    """
    labels: dict[str, str] = {}

    def speaker_id(label: str | None) -> str:
        if label is None:
            return fallback_speaker
        if label not in labels:
            labels[label] = f"{source}-{len(labels) + 1}"
        return labels[label]

    output: list[dict] = []
    for passage in sorted(passages, key=lambda p: p["start"]):
        if preserve_passages:
            # A live passage may already have edits or citations. Only fill an
            # unassigned, unambiguous label; never split or renumber its words.
            labels_here = {s['speaker'] for s in segments
                           if s['start'] < passage['end'] and s['end'] > passage['start']}
            if passage['speaker_id'] == fallback_speaker and len(labels_here) == 1:
                output.append(dict(passage, speaker_id=speaker_id(next(iter(labels_here))), speaker_labelled=True))
            else:
                output.append(passage)
            continue
        tokens = passage.get("tokens") or []
        if not tokens:
            label = speaker_for(passage["start"], passage["end"], segments)
            output.append(dict(passage, speaker_id=speaker_id(label), speaker_labelled=True))
            continue
        # Subword pieces and punctuation follow the word they belong to, as in
        # live transcription: a new word starts with whitespace.
        words: list[list[tuple]] = []
        for piece, start, end in tokens:
            token = (piece, float(start), float(end))
            if words and not piece[:1].isspace():
                words[-1].append(token)
            else:
                words.append([token])
        runs: list[dict] = []
        previous = None
        for word in words:
            label = speaker_for(word[0][1], word[-1][2], segments, fallback=previous)
            previous = label
            if runs and runs[-1]["label"] == label:
                runs[-1]["tokens"].extend(word)
            else:
                runs.append({"label": label, "tokens": list(word)})
        for run in runs:
            text = " ".join("".join(t[0] for t in run["tokens"]).split())
            if not text:
                continue
            output.append({
                "source": source,
                "speaker_id": speaker_id(run["label"]),
                "speaker_labelled": True,
                "start": round(run["tokens"][0][1], 2),
                "end": round(run["tokens"][-1][2], 2),
                "text": text,
                "quality": passage.get("quality", "timed"),
                "tokens": [(t[0], round(t[1], 2), round(t[2], 2)) for t in run["tokens"]],
            })
    speakers = [{"id": sid, "name": f"Speaker {index}", "label": label}
                for index, (label, sid) in enumerate(labels.items(), start=first_number)]
    return output, speakers
