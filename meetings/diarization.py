"""Speaker labels through the pyannoteAI API, aligned to timed passages.

Upload a track, submit a diarization job, poll it, then give every token
the speaker whose exclusive segment overlaps it most. Passages are split
where the speaker changes. Labels start as Speaker 1, Speaker 2, … in
order of first appearance; the user confirms names later.

The track leaves the PC for this step. The service only calls this after
the user agreed to the upload, and never for the transcript or notes.
"""

from __future__ import annotations

import re
import time

API_BASE = "https://api.pyannote.ai"
DEFAULT_MODEL = "precision-2"
POLL_SECONDS = 3.0
JOB_TIMEOUT_SECONDS = 45 * 60
TERMINAL = {"succeeded", "failed", "canceled"}
NEAREST_SECONDS = 1.0        # a token this close to a segment adopts its speaker
MEDIA_KEY = re.compile(r"[^a-zA-Z0-9\-_.]")


class DiarizationError(RuntimeError):
    """A user-facing problem with the speaker-labelling service."""


class PyannoteClient:
    """Thin client for https://docs.pyannote.ai (media upload, diarize, jobs)."""

    def __init__(self, api_key: str, *, session=None, base: str = API_BASE, sleep=time.sleep):
        if not str(api_key or "").strip():
            raise DiarizationError("No pyannoteAI key. Add one in Settings > Meetings.")
        self.api_key = str(api_key).strip()
        self.base = base.rstrip("/")
        self._sleep = sleep
        if session is None:
            import requests

            session = requests.Session()
        self._session = session

    def _headers(self, json_body: bool = True) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _check(self, response, what: str):
        if response.status_code == 401:
            raise DiarizationError("pyannoteAI rejected the key. Check Settings > Meetings.")
        if response.status_code == 429:
            raise DiarizationError("pyannoteAI rate limit reached. Try again in a minute.")
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str(response.json().get("message") or response.json().get("error") or "")
            except Exception:
                detail = (response.text or "")[:200]
            raise DiarizationError(f"pyannoteAI {what} failed ({response.status_code}). {detail}".strip())
        return response.json()

    def test(self) -> bool:
        response = self._session.get(f"{self.base}/v1/test", headers=self._headers(False), timeout=30)
        return response.status_code == 200

    def upload(self, data: bytes, name: str) -> str:
        """Put bytes in pyannoteAI temporary storage; returns the media:// url."""
        key = MEDIA_KEY.sub("-", name).strip("-") or "track"
        media_url = f"media://ownkey/{int(time.time())}-{key}"
        declared = self._check(self._session.post(f"{self.base}/v1/media/input", headers=self._headers(),
                                                  json={"url": media_url}, timeout=60), "media declaration")
        target = declared.get("url")
        if not target:
            raise DiarizationError("pyannoteAI did not return an upload URL.")
        put = self._session.put(target, data=data, headers={"Content-Type": "application/octet-stream"}, timeout=600)
        if put.status_code >= 400:
            raise DiarizationError(f"Uploading the track failed ({put.status_code}).")
        return media_url

    def diarize(self, media_url: str, *, model: str = DEFAULT_MODEL, num_speakers: int | None = None,
                min_speakers: int | None = None, max_speakers: int | None = None, exclusive: bool = True) -> str:
        body = {"url": media_url, "model": model, "exclusive": bool(exclusive)}
        if num_speakers:
            body["numSpeakers"] = int(num_speakers)
        if min_speakers:
            body["minSpeakers"] = int(min_speakers)
        if max_speakers:
            body["maxSpeakers"] = int(max_speakers)
        result = self._check(self._session.post(f"{self.base}/v1/diarize", headers=self._headers(), json=body,
                                                timeout=60), "job submission")
        job_id = result.get("jobId")
        if not job_id:
            raise DiarizationError("pyannoteAI did not return a job id.")
        return str(job_id)

    def wait(self, job_id: str, *, timeout: float = JOB_TIMEOUT_SECONDS, interval: float = POLL_SECONDS,
             should_stop=None, on_status=None) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            if should_stop is not None and should_stop():
                raise DiarizationError("Speaker labelling was cancelled.")
            job = self._check(self._session.get(f"{self.base}/v1/jobs/{job_id}", headers=self._headers(False),
                                                timeout=60), "job status")
            status = str(job.get("status", ""))
            if on_status is not None:
                on_status(status)
            if status == "succeeded":
                return job.get("output") or {}
            if status in TERMINAL:
                message = str(job.get("error") or job.get("message") or status)
                raise DiarizationError(f"pyannoteAI job {status}: {message}")
            if time.monotonic() >= deadline:
                raise DiarizationError("pyannoteAI took too long. Try again later.")
            self._sleep(interval)

    def diarize_wav(self, data: bytes, name: str, **options) -> list[dict]:
        """Upload, diarize and wait. Returns exclusive segments when available."""
        should_stop = options.pop("should_stop", None)
        on_status = options.pop("on_status", None)
        media_url = self.upload(data, name)
        job_id = self.diarize(media_url, **options)
        output = self.wait(job_id, should_stop=should_stop, on_status=on_status)
        return segments_from_output(output)


def segments_from_output(output: dict) -> list[dict]:
    raw = output.get("exclusiveDiarization") or output.get("diarization") or []
    segments = []
    for item in raw:
        try:
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        segments.append({"start": start, "end": end, "speaker": str(item.get("speaker", "SPEAKER_00"))})
    segments.sort(key=lambda s: s["start"])
    return segments


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
                    fallback_speaker: str) -> tuple[list[dict], list[dict]]:
    """Label one source's passages, splitting at speaker changes.

    Returns ``(new_passages, speakers)`` where speakers are
    ``{"id": "<source>-1", "name": "Speaker 1", "label": "SPEAKER_00"}`` in
    order of first appearance. Passages nobody claims keep the fallback
    speaker (the source label).
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
        tokens = passage.get("tokens") or []
        if not tokens:
            label = speaker_for(passage["start"], passage["end"], segments)
            output.append(dict(passage, speaker_id=speaker_id(label), speaker_labelled=True))
            continue
        runs: list[dict] = []
        previous = None
        for piece, start, end in tokens:
            label = speaker_for(float(start), float(end), segments, fallback=previous)
            previous = label
            if runs and runs[-1]["label"] == label:
                runs[-1]["tokens"].append((piece, float(start), float(end)))
            else:
                runs.append({"label": label, "tokens": [(piece, float(start), float(end))]})
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
                for index, (label, sid) in enumerate(labels.items(), start=1)]
    return output, speakers
