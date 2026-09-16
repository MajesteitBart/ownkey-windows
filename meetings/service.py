"""The meeting service: capture, jobs, analysis and the library in one place.

The backend owns meeting state and capture lifetime. The HTTP server and
the tray both talk to this object; nothing here depends on a window.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
from urllib.parse import urlparse

from . import analysis, audio, capture, diarization, export
from .capture import CaptureSession, MIC, SYSTEM, SOURCE_LABELS
from .store import MeetingStore, RETENTION_CHOICES
from .transcription import merge_tracks, transcribe_track

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
DEFAULT_MEETING_CONFIG = {
    "meetings_remote_policy": "ask",      # ask | allow: text to the rewrite provider
    "meetings_upload_policy": "ask",      # ask | allow: audio to pyannoteAI
    "meetings_auto_summary": False,
    "meetings_auto_speakers": False,
    "meetings_retention": "days7",
    "pyannote_api_key": "",
}
POLICY_KEYS = {"remote": "meetings_remote_policy", "upload": "meetings_upload_policy"}


class MeetingError(RuntimeError):
    """A user-facing problem: shown as a message, not a traceback."""


class ConsentRequired(MeetingError):
    """Remote analysis needs the user's go-ahead first."""

    def __init__(self, disclosure: dict):
        super().__init__("Remote analysis needs your confirmation first.")
        self.disclosure = disclosure


class MeetingService:
    def __init__(self, store: MeetingStore, *, get_config, set_config=None, local_models=None,
                 local_transcriber=None, chat=None, get_rewrite_key=None, notify=None,
                 open_settings=None, source_factory=None, on_capture_change=None, diarizer_factory=None,
                 clock=time.monotonic):
        self.store = store
        self._on_capture_change = on_capture_change or (lambda: None)
        self._diarizer_factory = diarizer_factory or self._default_diarizer
        self._get_config = get_config
        self._set_config = set_config or (lambda changes: None)
        self.local_models = local_models
        self.local_transcriber = local_transcriber
        self._chat = chat or self._default_chat
        self._get_rewrite_key = get_rewrite_key or (lambda cfg: str(cfg.get("rewrite_api_key", "") or ""))
        self.notify = notify or (lambda message: None)
        self.open_settings = open_settings or (lambda: None)
        self._source_factory = source_factory or self._default_sources
        self._clock = clock
        self._lock = threading.RLock()
        self._session: CaptureSession | None = None
        self._session_meeting: str | None = None
        self._cancelled: set[str] = set()
        self._running_job: dict | None = None
        self._audio_cache: dict[tuple, bytes] = {}
        self._closed = False
        self.interrupted_on_start = self.store.reconcile_on_start()
        self._jobs: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="meeting-jobs")
        self._worker.start()
        try:
            self.sweep_retention()
        except Exception:
            pass

    # ── config helpers ─────────────────────────────────────────────
    def config(self) -> dict:
        cfg = dict(DEFAULT_MEETING_CONFIG)
        cfg.update(self._get_config() or {})
        return cfg

    def remote_policy(self) -> str:
        return "allow" if self.config().get("meetings_remote_policy") == "allow" else "ask"

    def upload_policy(self) -> str:
        return "allow" if self.config().get("meetings_upload_policy") == "allow" else "ask"

    def set_policy(self, kind: str, policy: str) -> None:
        if kind not in POLICY_KEYS:
            raise MeetingError("Unknown policy.")
        if policy not in ("ask", "allow"):
            raise MeetingError("Policy must be 'ask' or 'allow'.")
        self._set_config({POLICY_KEYS[kind]: policy})

    def set_remote_policy(self, policy: str) -> None:
        self.set_policy("remote", policy)

    def pyannote_key(self, cfg: dict | None = None) -> str:
        cfg = cfg or self.config()
        return str(cfg.get("pyannote_api_key") or os.environ.get("PYANNOTEAI_API_KEY", "") or "").strip()

    def speaker_labels_info(self, cfg: dict | None = None) -> dict:
        return {"configured": bool(self.pyannote_key(cfg)), "provider": "pyannoteAI",
                "model": diarization.DEFAULT_MODEL, "host": "api.pyannote.ai", "remote": True}

    def _default_diarizer(self, cfg: dict):
        return diarization.PyannoteClient(self.pyannote_key(cfg))

    def text_model_info(self, cfg: dict | None = None) -> dict:
        from providers import provider_label, provider_requires_key

        cfg = cfg or self.config()
        provider = str(cfg.get("rewrite_provider", "") or "")
        endpoint = str(cfg.get("rewrite_endpoint", "") or "")
        model = str(cfg.get("rewrite_model", "") or "")
        host = (urlparse(endpoint).hostname or "").lower()
        remote = provider != "ollama" and host not in LOCAL_HOSTS
        try:
            needs_key = provider_requires_key(provider, endpoint)
            label = provider_label(provider)
        except Exception:
            needs_key, label = True, provider
        configured = bool(model) and bool(endpoint) and (not needs_key or bool(self._get_rewrite_key(cfg)))
        return {"provider": provider, "label": label, "model": model, "host": host, "remote": remote,
                "configured": configured}

    def local_model_info(self) -> dict:
        installed = bool(self.local_models and self.local_models.files_present())
        state = self.local_transcriber.snapshot() if self.local_transcriber else {"state": "Unavailable"}
        return {"installed": installed, "state": state.get("state", ""), "error": state.get("error", "")}

    def _default_chat(self, cfg: dict, system: str, user: str, max_tokens: int) -> str:
        from providers import complete_rewrite

        return complete_rewrite(cfg.get("rewrite_provider", ""), self._get_rewrite_key(cfg),
                                cfg.get("rewrite_endpoint", ""), cfg.get("rewrite_model", ""),
                                system, user, timeout=180, max_tokens=max_tokens)

    def _chat_call(self, cfg: dict, system: str, user: str, max_tokens: int) -> str:
        """One model call; a reply cut off at the output limit is retried once
        with double the budget (reasoning models spend hidden tokens)."""
        try:
            return self._chat(cfg, system, user, max_tokens)
        except Exception as exc:
            if "output limit" not in str(exc).lower():
                raise
            return self._chat(cfg, system, user, max_tokens * 2)

    # ── status ─────────────────────────────────────────────────────
    def is_capturing(self) -> bool:
        with self._lock:
            return self._session is not None and self._session.state in ("recording", "paused")

    def capture_state(self) -> dict | None:
        with self._lock:
            if self._session is None:
                return None
            summary = self._session.summary()
            summary["title"] = (self.store.get_meeting(self._session_meeting) or {}).get("title", "")
            return summary

    def status(self) -> dict:
        cfg = self.config()
        current = self.capture_state()
        return {
            "capturing": bool(current and current["state"] in ("recording", "paused")),
            "capture": current,
            "local_model": self.local_model_info(),
            "text_model": self.text_model_info(cfg),
            "speaker_labels": self.speaker_labels_info(cfg),
            "remote_policy": self.remote_policy(),
            "upload_policy": self.upload_policy(),
            "auto_summary": bool(cfg.get("meetings_auto_summary")),
            "auto_speakers": bool(cfg.get("meetings_auto_speakers")),
            "default_retention": cfg.get("meetings_retention") if cfg.get("meetings_retention") in RETENTION_CHOICES else "days7",
            "library": {"root": str(self.store.root), "bytes": self._library_bytes()},
            "running_job": self._running_job,
        }

    def _library_bytes(self) -> int:
        total = 0
        try:
            for root, _dirs, files in os.walk(self.store.root):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    # ── capture ────────────────────────────────────────────────────
    def _default_sources(self, wants: dict) -> list:
        sources = []
        if wants.get("mic", True):
            sources.append(capture.MicrophoneSource(device=wants.get("mic_device")))
        if wants.get("system", True):
            sources.append(capture.SystemAudioSource(device_id=wants.get("system_device") or None))
        return sources

    def start_meeting(self, title: str = "", *, mic: bool = True, system: bool = True, mic_device=None,
                      system_device=None, retention: str | None = None) -> dict:
        if not mic and not system:
            raise MeetingError("Choose at least one source.")
        with self._lock:
            if self.is_capturing():
                raise MeetingError("A meeting is already recording. Stop it first.")
            retention = retention if retention in RETENTION_CHOICES else self.status()["default_retention"]
            wants = {"mic": mic, "system": system, "mic_device": mic_device, "system_device": system_device}
            sources = self._source_factory(wants)
            if not sources:
                raise MeetingError("No capture source is available.")
            described = {s.label: {"device": wants.get(f"{s.label}_device")} for s in sources}
            meeting = self.store.create_meeting(title, described, retention, engine="orukeet")
            for source in sources:
                self.store.ensure_speaker(meeting["id"], source.label, source.label, SOURCE_LABELS[source.label])
            session = CaptureSession(self.store, meeting["id"], sources, on_interrupted=self._interrupted)
            try:
                session.start()
            except Exception as exc:
                self.store.delete_meeting(meeting["id"])
                shutil.rmtree(self.store.meeting_dir(meeting["id"]), ignore_errors=True)
                raise MeetingError(str(exc)) from exc
            self._session, self._session_meeting = session, meeting["id"]
        self._capture_changed()
        return self.store.get_meeting(meeting["id"])

    def pause(self) -> dict:
        with self._lock:
            if self._session is None or not self._session.pause():
                raise MeetingError("Nothing is recording.")
        self._capture_changed()
        return self.capture_state()

    def resume(self) -> dict:
        with self._lock:
            if self._session is None or not self._session.resume():
                raise MeetingError("Nothing is paused.")
        self._capture_changed()
        return self.capture_state()

    def stop(self) -> dict:
        with self._lock:
            session, meeting_id = self._session, self._session_meeting
            if session is None:
                raise MeetingError("Nothing is recording.")
            summary = session.stop()
            self._session, self._session_meeting = None, None
        self._audio_cache.clear()
        self._capture_changed()
        if summary["state"] == "stopped":
            self.transcribe(meeting_id)
        return summary

    def _interrupted(self, session: CaptureSession, reason: str) -> None:
        with self._lock:
            if self._session is session:
                self._session, self._session_meeting = None, None
        self._capture_changed()
        self.notify(f"Meeting capture stopped: {reason}")

    def _capture_changed(self) -> None:
        try:
            self._on_capture_change()
        except Exception:
            pass

    # ── library ────────────────────────────────────────────────────
    def list_meetings(self) -> list[dict]:
        items = self.store.list_meetings()
        jobs = {}
        for job in self.store.list_jobs(states=("queued", "running")):
            jobs.setdefault(job["meeting_id"], job)
        for meeting in items:
            meeting["job"] = jobs.get(meeting["id"])
            meeting["passages"] = len(self.store.list_passages(meeting["id"]))
            summary = self.store.latest_analysis(meeting["id"], "summary")
            meeting["summary_state"] = (
                "none" if summary is None else "outdated" if summary["input_rev"] < meeting["transcript_rev"] else "ready")
        return items

    def meeting_detail(self, meeting_id: str) -> dict:
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            raise MeetingError("This meeting no longer exists.")
        summary = self.store.latest_analysis(meeting_id, "summary")
        if summary is not None:
            try:
                summary["content"] = json.loads(summary["content"])
            except ValueError:
                summary["content"] = {"overview": summary["content"]}
            summary["outdated"] = summary["input_rev"] < meeting["transcript_rev"]
        capture_state = self.capture_state() if self._session_meeting == meeting_id else None
        return {
            "meeting": meeting,
            "capture": capture_state,
            "notes": self.store.get_notes(meeting_id),
            "speakers": self.store.list_speakers(meeting_id),
            "passages": self.store.list_passages(meeting_id),
            "events": self.store.list_events(meeting_id),
            "jobs": self.store.list_jobs(meeting_id),
            "summary": summary,
            "answers": self.store.list_analyses(meeting_id, "answer"),
            "drafts": self.store.list_analyses(meeting_id, "draft"),
        }

    def rename(self, meeting_id: str, title: str) -> dict:
        self.store.update_meeting(meeting_id, title=" ".join(str(title).split()))
        return self.store.get_meeting(meeting_id)

    def save_notes(self, meeting_id: str, content: str) -> dict:
        if self.store.get_meeting(meeting_id) is None:
            raise MeetingError("This meeting no longer exists.")
        return self.store.save_notes(meeting_id, str(content))

    def correct_passage(self, meeting_id: str, passage_id: str, corrected: str | None) -> dict:
        self._require_editable(meeting_id)
        row = self.store.correct_passage(meeting_id, passage_id, corrected)
        if row is None:
            raise MeetingError("Unknown passage.")
        return row

    def assign_speaker(self, meeting_id: str, passage_id: str, speaker_id: str) -> dict:
        self._require_editable(meeting_id)
        try:
            row = self.store.assign_passage_speaker(meeting_id, passage_id, speaker_id)
        except ValueError as exc:
            raise MeetingError(str(exc)) from exc
        if row is None:
            raise MeetingError("Unknown passage.")
        return row

    def rename_speaker(self, meeting_id: str, speaker_id: str, name: str, confirmed: bool | None = None) -> dict:
        try:
            row = self.store.rename_speaker(meeting_id, speaker_id, name, confirmed)
        except ValueError as exc:
            raise MeetingError(str(exc)) from exc
        if row is None:
            raise MeetingError("Unknown speaker.")
        self.store.bump_transcript_rev(meeting_id)
        return row

    def _require_editable(self, meeting_id: str) -> None:
        for job in self.store.list_jobs(meeting_id, ("queued", "running")):
            if job["kind"] == "transcribe":
                raise MeetingError("Wait for transcription to finish before editing passages.")

    def delete_meeting(self, meeting_id: str) -> None:
        with self._lock:
            self._cancelled.add(meeting_id)
            if self._session_meeting == meeting_id and self._session is not None:
                self._session.stop("Deleted while recording")
                self._session, self._session_meeting = None, None
        deadline = time.monotonic() + 30
        while self._running_job and self._running_job.get("meeting_id") == meeting_id and time.monotonic() < deadline:
            time.sleep(0.05)
        for job in self.store.list_jobs(meeting_id, ("queued", "running")):
            self.store.update_job(job["id"], state="cancelled")
        self.store.delete_meeting(meeting_id)
        self._audio_cache = {k: v for k, v in self._audio_cache.items() if k[0] != meeting_id}
        directory = self.store.meeting_dir(meeting_id)
        for _attempt in range(5):
            shutil.rmtree(directory, ignore_errors=True)
            if not directory.exists():
                break
            time.sleep(0.2)

    # ── jobs ───────────────────────────────────────────────────────
    def transcribe(self, meeting_id: str) -> dict:
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            raise MeetingError("This meeting no longer exists.")
        if meeting["state"] in ("recording", "paused"):
            raise MeetingError("Stop the meeting before transcribing.")
        if meeting["audio_state"] != "kept":
            raise MeetingError("The audio was removed, so this meeting cannot be transcribed again.")
        if not self.store.list_chunks(meeting_id):
            raise MeetingError("No audio was recorded for this meeting.")
        for job in self.store.list_jobs(meeting_id, ("queued", "running")):
            if job["kind"] == "transcribe":
                return job
        job = self.store.create_job(meeting_id, "transcribe", "Waiting for the model")
        self._jobs.put(job["id"])
        return job

    def summarize(self, meeting_id: str, *, include_notes: bool = False, remote_ok: bool = False) -> dict:
        self._check_analysis_allowed(remote_ok)
        if not self.store.list_passages(meeting_id):
            raise MeetingError("Transcribe the meeting first.")
        for job in self.store.list_jobs(meeting_id, ("queued", "running")):
            if job["kind"] == "summary":
                return job
        job = self.store.create_job(meeting_id, "summary", json.dumps({"include_notes": bool(include_notes)}))
        self._jobs.put(job["id"])
        return job

    def draft(self, meeting_id: str, *, remote_ok: bool = False) -> dict:
        self._check_analysis_allowed(remote_ok)
        if not self.store.list_passages(meeting_id):
            raise MeetingError("Transcribe the meeting first.")
        for job in self.store.list_jobs(meeting_id, ("queued", "running")):
            if job["kind"] == "draft":
                return job
        job = self.store.create_job(meeting_id, "draft")
        self._jobs.put(job["id"])
        return job

    def ask(self, meeting_id: str, question: str, *, include_notes: bool = False, remote_ok: bool = False) -> dict:
        self._check_analysis_allowed(remote_ok)
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            raise MeetingError("This meeting no longer exists.")
        passages = self.store.list_passages(meeting_id)
        if not passages:
            raise MeetingError("Transcribe the meeting first.")
        cfg = self.config()
        info = self.text_model_info(cfg)
        notes = self.store.get_notes(meeting_id)["content"] if include_notes else None
        try:
            result = analysis.answer_question(
                question, passages, self.store.list_speakers(meeting_id),
                lambda system, user, max_tokens: self._chat_call(cfg, system, user, max_tokens), notes=notes)
        except ValueError as exc:
            raise MeetingError(str(exc)) from exc
        except Exception as exc:
            raise MeetingError(f"The text model did not answer: {describe_error(exc)}") from exc
        row = self.store.add_analysis(
            meeting_id, "answer", provider=info["provider"], model=info["model"], input_rev=meeting["transcript_rev"],
            include_notes=include_notes, content=result["answer"], refs=result["refs"], question=" ".join(question.split()),
            state="done" if result["answerable"] else "unanswerable")
        return row

    def _check_analysis_allowed(self, remote_ok: bool) -> None:
        info = self.text_model_info()
        if not info["configured"]:
            raise MeetingError("No text model is configured. Open Settings > Rewriting and choose a provider.")
        if info["remote"] and not remote_ok and self.remote_policy() != "allow":
            raise ConsentRequired(self.disclosure(info))

    def disclosure(self, info: dict | None = None) -> dict:
        info = info or self.text_model_info()
        return {
            "kind": "remote", "policy_key": "remote",
            "title": f"Ownkey will send meeting text to {info['label']}",
            "intro": "Audio never leaves this PC. The transcript text does, over your own key. "
                     "Check what goes out, then decide how to handle this next time.",
            "provider": info["label"], "model": info["model"], "host": info["host"],
            "sent": ["Transcript text with passage ids and speaker names"],
            "not_sent": ["Audio", "My thoughts (unless you include them for one question)"],
            "retention": "Long meetings go out in bounded sections with their passage ids. Nothing is cut off silently.",
            "policy": self.remote_policy(),
        }

    def upload_disclosure(self, sources: list[str] | None = None) -> dict:
        sources = sources or [SYSTEM]
        tracks = " and ".join(SOURCE_LABELS.get(s, s) for s in sources)
        others = [SOURCE_LABELS[s] + " track" for s in (MIC, SYSTEM) if s not in sources]
        return {
            "kind": "upload", "policy_key": "upload",
            "title": "Ownkey will upload audio to pyannoteAI",
            "intro": f"Speaker labels come from pyannoteAI's hosted diarization. The {tracks} track is uploaded "
                     "for this step only; nothing else leaves this PC.",
            "provider": "pyannoteAI", "model": diarization.DEFAULT_MODEL, "host": "api.pyannote.ai",
            "sent": [f"{tracks} track (WAV, mono 16 kHz)"],
            "not_sent": others + ["Transcript", "My thoughts"],
            "retention": "pyannoteAI deletes uploads within 48 hours and results within 24 hours, "
                         "and does not train on them.",
            "policy": self.upload_policy(),
        }

    def _speaker_targets(self, meeting_id: str) -> list[str]:
        sources = {chunk["source"] for chunk in self.store.list_chunks(meeting_id)}
        if SYSTEM in sources:
            return [SYSTEM]
        return [MIC] if MIC in sources else []

    def label_speakers(self, meeting_id: str, *, remote_ok: bool = False) -> dict:
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            raise MeetingError("This meeting no longer exists.")
        if meeting["state"] in ("recording", "paused"):
            raise MeetingError("Stop the meeting first.")
        if meeting["audio_state"] != "kept":
            raise MeetingError("The audio was removed, so speakers cannot be labelled.")
        if not self.store.list_passages(meeting_id):
            raise MeetingError("Transcribe the meeting first.")
        if not self.speaker_labels_info()["configured"]:
            raise MeetingError("Speaker labels need a pyannoteAI key. Add one in Settings > Meetings.")
        targets = self._speaker_targets(meeting_id)
        if not targets:
            raise MeetingError("No audio track to label.")
        if not remote_ok and self.upload_policy() != "allow":
            raise ConsentRequired(self.upload_disclosure(targets))
        for job in self.store.list_jobs(meeting_id, ("queued", "running")):
            if job["kind"] in ("speakers", "transcribe"):
                return job
        job = self.store.create_job(meeting_id, "speakers", "Waiting")
        self._jobs.put(job["id"])
        return job

    def _run_speakers(self, job: dict) -> None:
        meeting_id = job["meeting_id"]
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            return
        cfg = self.config()
        client = self._diarizer_factory(cfg)
        targets = self._speaker_targets(meeting_id)
        passages = self.store.list_passages(meeting_id)
        should_stop = lambda: meeting_id in self._cancelled or self._closed
        found = []
        for source in targets:
            label = SOURCE_LABELS.get(source, source)
            self.store.update_job(job["id"], detail=f"Uploading {label}", progress=0.1)
            segments = client.diarize_wav(
                self.audio_wav(meeting_id, source), f"{meeting_id}-{source}.wav", should_stop=should_stop,
                on_status=lambda status, label=label: self.store.update_job(
                    job["id"], detail=f"{label} · pyannoteAI {status}", progress=0.5))
            mine = [p for p in passages if p["source"] == source]
            others = [p for p in passages if p["source"] != source]
            labelled, speakers = diarization.assign_speakers(mine, segments, source=source, fallback_speaker=source)
            for speaker in speakers:
                self.store.ensure_speaker(meeting_id, speaker["id"], source, speaker["name"], confirmed=False)
            found.extend(speakers)
            passages = others + labelled
        if should_stop():
            return
        merged = merge_tracks({"all": passages})
        rev = self.store.get_meeting(meeting_id)["transcript_rev"] + 1
        self.store.replace_passages(meeting_id, merged, rev)
        self.store.add_event(meeting_id, meeting.get("elapsed", 0.0), "speakers",
                             f"{len(found)} speaker{'s' if len(found) != 1 else ''} · pyannoteAI {diarization.DEFAULT_MODEL}")

    def _worker_loop(self) -> None:
        while not self._closed:
            try:
                job_id = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            job = self.store.get_job(job_id)
            if job is None or job["state"] != "queued":
                continue
            if job["meeting_id"] in self._cancelled:
                self.store.update_job(job_id, state="cancelled")
                continue
            self._running_job = {"id": job_id, "kind": job["kind"], "meeting_id": job["meeting_id"]}
            self.store.update_job(job_id, state="running", attempts=job["attempts"] + 1)
            try:
                if job["kind"] == "transcribe":
                    self._run_transcribe(job)
                elif job["kind"] == "summary":
                    self._run_summary(job)
                elif job["kind"] == "draft":
                    self._run_draft(job)
                elif job["kind"] == "speakers":
                    self._run_speakers(job)
                else:
                    raise MeetingError(f"Unknown job {job['kind']}")
                if job["meeting_id"] in self._cancelled:
                    self.store.update_job(job_id, state="cancelled")
                else:
                    self.store.update_job(job_id, state="done", progress=1.0, error="")
            except Exception as exc:
                message = describe_error(exc)
                self.store.update_job(job_id, state="error", error=message)
                self.notify(f"Meeting {job['kind']} failed: {message}")
            finally:
                self._running_job = None

    def _run_transcribe(self, job: dict) -> None:
        meeting_id = job["meeting_id"]
        if self.local_models is None or self.local_transcriber is None or not self.local_models.files_present():
            raise MeetingError("Orukeet is not installed. Open Settings > Transcription, download it, then retry. "
                               "Ownkey never sends meeting audio to a cloud provider.")
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            return
        self.store.update_job(job["id"], detail="Loading Orukeet")
        attempt = self.local_transcriber.begin_attempt()
        try:
            self.store.clear_passages(meeting_id)
            chunks_by_source = {}
            for chunk in self.store.list_chunks(meeting_id):
                chunks_by_source.setdefault(chunk["source"], []).append(chunk)
            totals = {source: sum(c["n_samples"] for c in chunks) / audio.SAMPLE_RATE
                      for source, chunks in chunks_by_source.items()}
            total_seconds = max(1e-6, sum(totals.values()))
            done_before = 0.0
            tracks = {}
            should_stop = lambda: meeting_id in self._cancelled or self._closed
            for source, chunks in chunks_by_source.items():
                self.store.ensure_speaker(meeting_id, source, source, SOURCE_LABELS.get(source, source))
                samples = audio.concat_chunks(chunks)

                def progress(done, total, new, source=source, done_before=done_before):
                    fraction = min(0.99, (done_before + done) / total_seconds)
                    self.store.update_job(job["id"], progress=fraction,
                                          detail=f"{SOURCE_LABELS.get(source, source)} · {int(done // 60)}:{int(done % 60):02d} of {int(total // 60)}:{int(total % 60):02d}")
                    if new:
                        provisional = [dict(p, id=f"{source}-{p['id']}") for p in new]
                        self.store.append_passages(meeting_id, provisional, meeting["transcript_rev"] + 1)

                tracks[source] = transcribe_track(samples, attempt.transcribe_timed, source=source,
                                                  on_progress=progress, should_stop=should_stop)
                done_before += totals[source]
                if should_stop():
                    return
            merged = merge_tracks(tracks)
            rev = meeting["transcript_rev"] + 1
            self.store.replace_passages(meeting_id, merged, rev)
            self.store.update_meeting(meeting_id, transcribed_at=time.time(), engine="orukeet")
            self.store.add_event(meeting_id, meeting.get("elapsed", 0.0), "transcribed",
                                 f"{len(merged)} passages · Orukeet on this PC")
        finally:
            attempt.close()
        self._audio_cache.clear()
        latest = self.store.get_meeting(meeting_id)
        if latest and latest["retention"] == "after_transcription":
            self.remove_audio(meeting_id)
        cfg = self.config()
        if (cfg.get("meetings_auto_speakers") and self.speaker_labels_info(cfg)["configured"]
                and self.upload_policy() == "allow" and self._speaker_targets(meeting_id)
                and latest and latest["audio_state"] == "kept"):
            self._jobs.put(self.store.create_job(meeting_id, "speakers", "Waiting")["id"])
        if cfg.get("meetings_auto_summary") and self.text_model_info(cfg)["configured"]:
            info = self.text_model_info(cfg)
            if not info["remote"] or self.remote_policy() == "allow":
                self._jobs.put(self.store.create_job(meeting_id, "summary", json.dumps({"include_notes": False}))["id"])

    def _run_summary(self, job: dict) -> None:
        meeting_id = job["meeting_id"]
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            return
        options = {}
        try:
            options = json.loads(job.get("detail") or "{}")
        except ValueError:
            pass
        include_notes = bool(options.get("include_notes"))
        cfg = self.config()
        info = self.text_model_info(cfg)
        self.store.update_job(job["id"], detail=f"Asking {info['label'] or info['provider']}", progress=0.2)
        notes = self.store.get_notes(meeting_id)["content"] if include_notes else None
        summary = analysis.generate_summary(
            self.store.list_passages(meeting_id), self.store.list_speakers(meeting_id),
            lambda system, user, max_tokens: self._chat_call(cfg, system, user, max_tokens), notes=notes)
        if meeting_id in self._cancelled:
            return
        self.store.add_analysis(meeting_id, "summary", provider=info["provider"], model=info["model"],
                                input_rev=meeting["transcript_rev"], include_notes=include_notes,
                                content=json.dumps(summary, ensure_ascii=False), refs=analysis.summary_refs(summary))

    def _run_draft(self, job: dict) -> None:
        meeting_id = job["meeting_id"]
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            return
        cfg = self.config()
        info = self.text_model_info(cfg)
        self.store.update_job(job["id"], detail=f"Asking {info['label'] or info['provider']}", progress=0.2)
        latest = self.store.latest_analysis(meeting_id, "summary")
        summary = None
        if latest is not None:
            try:
                summary = json.loads(latest["content"])
            except ValueError:
                summary = None
        text = analysis.draft_followup(summary, self.store.list_passages(meeting_id), self.store.list_speakers(meeting_id),
                                       lambda system, user, max_tokens: self._chat_call(cfg, system, user, max_tokens))
        if meeting_id in self._cancelled:
            return
        self.store.add_analysis(meeting_id, "draft", provider=info["provider"], model=info["model"],
                                input_rev=meeting["transcript_rev"], include_notes=False, content=text,
                                refs=analysis.summary_refs(summary) if summary else [])

    # ── audio, export, retention ───────────────────────────────────
    def audio_wav(self, meeting_id: str, source: str) -> bytes:
        chunks = self.store.list_chunks(meeting_id, source)
        if not chunks:
            raise MeetingError("No audio for this source.")
        key = (meeting_id, source, len(chunks))
        cached = self._audio_cache.get(key)
        if cached is None:
            cached = audio.wav_bytes(audio.concat_chunks(chunks))
            self._audio_cache = {key: cached}
        return cached

    def remove_audio(self, meeting_id: str) -> dict:
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            raise MeetingError("This meeting no longer exists.")
        if meeting["state"] in ("recording", "paused"):
            raise MeetingError("Stop the meeting first.")
        self.store.delete_chunks(meeting_id)
        shutil.rmtree(self.store.meeting_dir(meeting_id) / "audio", ignore_errors=True)
        self.store.update_meeting(meeting_id, audio_state="removed")
        self.store.add_event(meeting_id, meeting.get("elapsed", 0.0), "audio_removed",
                             "Audio deleted; playback and re-transcription are no longer possible")
        self._audio_cache = {k: v for k, v in self._audio_cache.items() if k[0] != meeting_id}
        return self.store.get_meeting(meeting_id)

    def sweep_retention(self) -> list[str]:
        removed = []
        for meeting in self.store.list_meetings():
            if self.store.audio_expired(meeting):
                self.remove_audio(meeting["id"])
                removed.append(meeting["id"])
        return removed

    def export(self, meeting_id: str, fmt: str = "md") -> tuple[str, bytes]:
        detail = self.meeting_detail(meeting_id)
        meeting = detail["meeting"]
        stem = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in (meeting.get("title") or "meeting")).strip()
        stem = (stem or "meeting").replace(" ", "-")[:60]
        stamp = time.strftime("%Y-%m-%d", time.localtime(meeting.get("created_at", 0)))
        if fmt == "json":
            analyses = self.store.list_analyses(meeting_id)
            body = export.to_json(meeting, detail["notes"], detail["passages"], detail["speakers"], analyses,
                                  detail["events"])
            return f"{stamp}-{stem}.json", body.encode("utf-8")
        summary = detail["summary"]["content"] if detail["summary"] else None
        body = export.to_markdown(meeting, detail["notes"], detail["passages"], detail["speakers"], summary,
                                  detail["drafts"], detail["answers"])
        return f"{stamp}-{stem}.md", body.encode("utf-8")

    # ── shutdown ───────────────────────────────────────────────────
    def close(self, *, stop_recording: bool = True) -> None:
        self._closed = True
        with self._lock:
            session = self._session
            if session is not None and stop_recording:
                try:
                    session.stop()
                except Exception:
                    pass
            self._session, self._session_meeting = None, None
        self._worker.join(timeout=2.0)


def describe_error(exc: Exception) -> str:
    try:
        import requests
        from providers import describe_api_error

        if isinstance(exc, requests.HTTPError):
            return describe_api_error(exc)
        if isinstance(exc, requests.ConnectionError):
            return "Network error. Check your connection; the transcript and notes are unchanged."
    except Exception:
        pass
    message = str(exc).strip() or exc.__class__.__name__
    return message
