"""Markdown and JSON export of one meeting."""

from __future__ import annotations

import json
import time

from .transcription import format_time


def passage_text(passage: dict) -> str:
    corrected = passage.get("corrected")
    return corrected if corrected else passage["text"]


def _speaker_names(speakers: list[dict]) -> dict[str, str]:
    return {s["id"]: s["name"] for s in speakers}


def to_markdown(meeting: dict, notes: dict, passages: list[dict], speakers: list[dict],
                summary: dict | None, drafts: list[dict], answers: list[dict], *, summary_rev: int | None = None) -> str:
    names = _speaker_names(speakers)
    title = meeting.get("title") or "Untitled meeting"
    created = time.strftime("%Y-%m-%d %H:%M", time.localtime(meeting.get("created_at", 0)))
    lines = [f"# {title}", "", f"Recorded {created} · {format_time(meeting.get('elapsed', 0))} · "
             f"transcript rev {meeting.get('transcript_rev', 0)} · Ownkey Meetings", ""]
    lines += ["## My thoughts", "", (notes.get("content") or "_No notes._").rstrip(), ""]
    if summary:
        lines += ["## Summary", ""]
        outdated = summary_rev is not None and summary_rev != meeting.get('transcript_rev')
        if outdated:
            lines += ['_Outdated summary: the transcript changed. Regenerate before relying on it._', '']
        lines += summary_markdown(summary, [] if outdated else passages)
        lines.append("")
    lines += ["## Transcript", ""]
    if not passages:
        lines.append("_Not transcribed._")
    for passage in passages:
        speaker = names.get(passage["speaker_id"], passage["speaker_id"])
        marker = " (edited)" if passage.get("corrected") else ""
        lines.append(f"**{format_time(passage['start'])} {speaker}**{marker}: {passage_text(passage)}")
        lines.append("")
    if answers:
        lines += ["## Questions and answers", ""]
        for answer in answers:
            if answer.get('input_rev', meeting.get('transcript_rev')) != meeting.get('transcript_rev'):
                lines += ['_Outdated answer: the transcript changed. Ask again to update it._', '']
            lines += [f"**Q: {answer.get('question', '')}**", "", answer.get("content", ""), ""]
    if drafts:
        lines += ["## Follow-up draft", ""]
        if drafts[-1].get('input_rev', meeting.get('transcript_rev')) != meeting.get('transcript_rev'):
            lines += ['_Outdated draft: the transcript changed. Regenerate before relying on its text or timestamps._', '']
        lines += [drafts[-1].get("content", ""), ""]
    return "\n".join(lines).rstrip() + "\n"


def summary_markdown(summary: dict, passages: list[dict]) -> list[str]:
    times = {p["id"]: format_time(p["start"]) for p in passages}

    def cites(refs):
        marks = [f"[{times[r]}]" for r in refs or [] if r in times]
        return (" " + " ".join(marks)) if marks else ""

    lines = []
    if summary.get("overview"):
        lines += [summary["overview"].strip(), ""]
    sections = (("decisions", "Decisions"), ("actions", "Action items"), ("questions", "Open questions"))
    for key, heading in sections:
        items = summary.get(key) or []
        if not items:
            continue
        lines += [f"### {heading}", ""]
        for item in items:
            text = item.get("text", "").strip()
            extra = []
            if key == "actions":
                extra.append(f"owner: {item.get('owner') or 'unassigned'}")
                extra.append(f"due: {item.get('due') or 'no date'}")
            if key == "decisions" and item.get("status") and item["status"] != "decided":
                extra.append(item["status"])
            suffix = f" ({'; '.join(extra)})" if extra else ""
            lines.append(f"- {text}{suffix}{cites(item.get('refs'))}")
        lines.append("")
    return lines


def to_json(meeting: dict, notes: dict, passages: list[dict], speakers: list[dict], analyses: list[dict],
            events: list[dict]) -> str:
    payload = {
        "format": "ownkey-meeting",
        "version": 1,
        "exported_at": time.time(),
        "meeting": meeting,
        "notes": notes,
        "speakers": speakers,
        "passages": passages,
        "events": events,
        "analyses": analyses,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
