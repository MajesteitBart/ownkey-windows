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
from .preview import MicPreview
from .live import LiveTranscription
from .store import MeetingStore, RETENTION_CHOICES
from .transcription import merge_tracks, transcribe_track

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
DEFAULT_MEETING_CONFIG = {
    "meetings_remote_policy": "ask",         # ask | allow: text to the rewrite provider
    "meetings_upload_policy": "ask",         # ask | allow: audio to pyannoteAI
    "meetings_transcription_policy": "ask",  # ask | allow: audio to a cloud transcription provider
    "meetings_auto_summary": False,
    "meetings_auto_speakers": False,
    "meetings_retention": "days7",
    "pyannote_api_key": "",
    "meetings_audio_provider": "same",       # same | orukeet | openai | mistral | google | custom
    "meetings_audio_api_key": "",
    "meetings_audio_endpoint": "",
    "meetings_audio_model": "",
    "meetings_live_transcription_policy": "ask",
    "meetings_live_speakers_policy": "ask",
}
POLICY_KEYS = {"remote": "meetings_remote_policy", "upload": "meetings_upload_policy",
               "transcription": "meetings_transcription_policy",
               "live_transcription": "meetings_live_transcription_policy",
               "live_speakers": "meetings_live_speakers_policy"}
CLOUD_WINDOW_NOTE = "windows of up to 28 seconds"
RETENTION_SWEEP_SECONDS = 60


class MeetingError(RuntimeError):
    """A user-facing problem: shown as a message, not a traceback."""


class ConsentRequired(MeetingError):
    """Remote analysis needs the user's go-ahead first."""

    def __init__(self, disclosure: dict):
        super().__init__("Remote analysis needs your confirmation first.")
        self.disclosure = disclosure


class MeetingService:
    def __init__(self, store: MeetingStore, *, get_config, set_config=None, local_models=None,
                 local_transcriber=None, chat=None, get_rewrite_key=None, get_audio_key=None, notify=None,
                 open_settings=None, source_factory=None, on_capture_change=None, diarizer_factory=None,
                 cloud_transcriber=None, dictation_busy=None, yield_to=None, clock=time.monotonic,
                 stream_factory=None, get_local_audio=None):
        self.store = store
        self._on_capture_change = on_capture_change or (lambda: None)
        # Dictation comes first: a meeting cannot start over a held hotkey, and
        # background decoding pauses between windows while dictation is busy.
        self._dictation_busy = dictation_busy or (lambda: False)
        self._yield_to = yield_to or (lambda: False)
        self._diarizer_factory = diarizer_factory or self._default_diarizer
        self._cloud_transcriber = cloud_transcriber or self._default_cloud_transcriber
        self._get_audio_key = get_audio_key or (lambda cfg: str(cfg.get("audio_api_key", "") or ""))
        self._get_config = get_config
        self._set_config = set_config or (lambda changes: None)
        self.local_models = local_models
        self.local_transcriber = local_transcriber
        self._get_local_audio = get_local_audio or (lambda: (self.local_models, self.local_transcriber))
        self._chat = chat or self._default_chat
        self._get_rewrite_key = get_rewrite_key or (lambda cfg: str(cfg.get("rewrite_api_key", "") or ""))
        self.notify = notify or (lambda message: None)
        self.open_settings = open_settings or (lambda: None)
        self._source_factory = source_factory or self._default_sources
        self._mic_preview = MicPreview(self._preview_source)
        self._clock = clock
        self._lock = threading.RLock()
        self._session: CaptureSession | None = None
        self._session_meeting: str | None = None
        self._live: dict[str, LiveTranscription] = {}
        self._stream_factory = stream_factory
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
        self._maintenance_stop = threading.Event()
        self._maintenance = threading.Thread(target=self._retention_loop, daemon=True, name="meeting-retention")
        self._maintenance.start()

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
        models, transcriber = self._get_local_audio()
        installed = bool(models and models.files_present())
        state = transcriber.snapshot() if transcriber else {"state": "Unavailable"}
        return {"installed": installed, "state": state.get("state", ""), "error": state.get("error", "")}

    def transcription_policy(self) -> str:
        return "allow" if self.config().get("meetings_transcription_policy") == "allow" else "ask"

    def transcription_engine(self, cfg: dict | None = None) -> dict:
        """Which engine transcribes meetings: Orukeet on this PC, or the same
        cloud providers dictation can use. Never includes the API key."""
        from providers import normalize_provider, provider_label, provider_requires_key

        cfg = cfg or self.config()
        choice = str(cfg.get("meetings_audio_provider") or "same")
        if choice == "same":
            provider = normalize_provider(cfg.get("audio_provider", "orukeet"), "orukeet")
            endpoint = str(cfg.get("audio_endpoint", "") or "")
            model = str(cfg.get("audio_model", "") or "")
            has_key = bool(self._get_audio_key(cfg))
        else:
            provider = normalize_provider(choice, "orukeet")
            endpoint = str(cfg.get("meetings_audio_endpoint", "") or "")
            model = str(cfg.get("meetings_audio_model", "") or "")
            has_key = bool(str(cfg.get("meetings_audio_api_key", "") or "").strip())
        if provider == "orukeet":
            return {"kind": "local", "provider": "orukeet", "label": "Orukeet", "model": "", "endpoint": "", "host": "",
                    "remote": False, "configured": self.local_model_info()["installed"], "follows_dictation": choice == "same"}
        host = (urlparse(endpoint).hostname or "").lower()
        try:
            needs_key = provider_requires_key(provider, endpoint)
            label = provider_label(provider)
        except Exception:
            needs_key, label = True, provider
        return {"kind": "cloud", "provider": provider, "label": label, "model": model, "endpoint": endpoint, "host": host,
                "remote": host not in LOCAL_HOSTS, "configured": bool(endpoint) and bool(model) and (not needs_key or has_key),
                "follows_dictation": choice == "same"}

    def _transcription_key(self, cfg: dict) -> str:
        if str(cfg.get("meetings_audio_provider") or "same") == "same":
            return self._get_audio_key(cfg)
        return str(cfg.get("meetings_audio_api_key", "") or "").strip()

    def _default_cloud_transcriber(self, engine: dict, key: str, wav_bytes: bytes, language: str, vocabulary) -> str:
        from providers import transcribe_audio

        return transcribe_audio(engine["provider"], key, engine["endpoint"], engine["model"], wav_bytes, language,
                                vocabulary, timeout=180)

    def transcription_disclosure(self, engine: dict | None = None) -> dict:
        engine = engine or self.transcription_engine()
        return {
            "kind": "transcribe", "policy_key": "transcription",
            "title": f"Ownkey will send this meeting's audio to {engine['label']}",
            "intro": f"Meetings are transcribed with the provider you chose in Settings. The recorded tracks go to "
                     f"{engine['label']} in {CLOUD_WINDOW_NOTE}, over your own key. Pick Orukeet in Settings > Meetings "
                     "to keep audio on this PC.",
            "provider": engine["label"], "model": engine["model"], "host": engine["host"],
            "sent": [f"Every recorded track, as WAV in {CLOUD_WINDOW_NOTE}"],
            "not_sent": ["My thoughts"],
            "retention": "What the provider keeps is set by its data policy; Ownkey has no control after upload.",
            "policy": self.transcription_policy(),
        }

    def _default_chat(self, cfg: dict, system: str, user: str, max_tokens: int) -> str:
        from providers import complete_rewrite

        return complete_rewrite(cfg.get("rewrite_provider", ""), self._get_rewrite_key(cfg),
                                cfg.get("rewrite_endpoint", ""), cfg.get("rewrite_model", ""),
                                system, user, timeout=180, max_tokens=max_tokens)

    def _chat_call(self, cfg: dict, system: str, user: str, max_tokens: int) -> str:
        """One model call; a reply cut off at the output limit is retried once
        with double the budget (reasoning models spend hidden tokens)."""
        if self._closed:
            raise MeetingError('Ownkey closed before processing finished.')
        try:
            return self._chat(cfg, system, user, max_tokens)
        except Exception as exc:
            if self._closed or "output limit" not in str(exc).lower():
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
            "transcriber": self.transcription_engine(cfg),
            "text_model": self.text_model_info(cfg),
            "speaker_labels": self.speaker_labels_info(cfg),
            "remote_policy": self.remote_policy(),
            "upload_policy": self.upload_policy(),
            "transcription_policy": self.transcription_policy(),
            "live_transcription_policy": cfg['meetings_live_transcription_policy'],
            "live_speakers_policy": cfg['meetings_live_speakers_policy'],
            "auto_summary": bool(cfg.get("meetings_auto_summary")),
            "auto_speakers": bool(cfg.get("meetings_auto_speakers")),
            "default_retention": cfg.get("meetings_retention") if cfg.get("meetings_retention") in RETENTION_CHOICES else "days7",
            "library": {"root": str(self.store.root), "bytes": self._library_bytes()},
            "running_job": self._running_job,
        }

    def _library_bytes(self) -> int:
        measured, cached = getattr(self, '_size_cache', (0, 0))
        if time.monotonic() - measured < 5:
            return cached
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
        self._size_cache = (time.monotonic(), total)
        return total

    # ── capture ────────────────────────────────────────────────────
    def _default_sources(self, wants: dict) -> list:
        sources = []
        if wants.get("mic", True):
            sources.append(capture.MicrophoneSource(device=wants.get("mic_device")))
        if wants.get("system", True):
            sources.append(capture.SystemAudioSource(device_id=wants.get("system_device") or None))
        return sources

    def _preview_source(self, device):
        sources = self._source_factory({"mic": True, "system": False, "mic_device": device, "system_device": None})
        source = next((item for item in sources if item.label == MIC), None)
        if source is None:
            raise MeetingError("No microphone is available.")
        return source

    def mic_preview(self, device=None) -> dict:
        """Live input level for the New meeting sheet, so the right microphone is
        picked before Start. The audio becomes a number and is dropped; the
        preview closes the microphone when the window stops asking."""
        if self.is_capturing():
            self._mic_preview.stop()  # the recording has its own meter
            return {"active": False, "level": 0.0, "error": ""}
        return self._mic_preview.read(device)

    def stop_mic_preview(self) -> None:
        self._mic_preview.stop()

    def start_meeting(self, title: str = "", *, mic: bool = True, system: bool = True, mic_device=None,
                      system_device=None, retention: str | None = None, mic_shared: bool = False,
                      live_transcription: bool = False, live_speakers: bool = False,
                      live_transcription_ok: bool = False, live_speakers_ok: bool = False) -> dict:
        if not mic and not system:
            raise MeetingError("Choose at least one source.")
        cfg = self.config()
        engine = self.transcription_engine(cfg)
        if live_speakers and not live_transcription:
            raise MeetingError('Enable live transcription to use live speaker labels.')
        if live_transcription and engine['kind'] == 'cloud':
            if not engine['configured']:
                raise MeetingError('Set up the meeting transcription provider in Settings first.')
            if engine['remote'] and not live_transcription_ok and cfg['meetings_live_transcription_policy'] != 'allow':
                disclosure = self.transcription_disclosure(engine)
                disclosure.update(policy_key='live_transcription',
                    intro='Audio is sent in short speech windows while this meeting is recording. '
                          'Pause stops sending new audio. Choose Orukeet to transcribe on this PC.')
                raise ConsentRequired(disclosure)
        if live_speakers:
            if not self.pyannote_key(cfg):
                raise MeetingError('Add a pyannoteAI key in Settings > Meetings first.')
            if not system and not (mic and mic_shared):
                raise MeetingError('Live speaker labels need call audio or a shared microphone.')
            if not live_speakers_ok and cfg['meetings_live_speakers_policy'] != 'allow':
                raise ConsentRequired({
                    'kind': 'upload', 'policy_key': 'live_speakers', 'provider': 'pyannoteAI',
                    'model': 'Live-1', 'host': 'api.pyannote.ai',
                    'title': 'Show speaker changes while recording',
                    'intro': 'Selected audio streams to pyannoteAI during the meeting. During Pause, only '
                             'generated silence is sent to preserve speaker labels; paused time still counts as usage.',
                    'sent': ([SOURCE_LABELS[SYSTEM]] if system else []) +
                            ([SOURCE_LABELS[MIC]] if mic and mic_shared else []),
                    'not_sent': ['My thoughts', 'Transcript text', 'Audio heard during Pause'],
                    'retention': 'pyannoteAI processes live audio without storing the audio or stream outputs.',
                })
        self._mic_preview.stop()  # hand the microphone over to the recording
        with self._lock:
            if self.is_capturing():
                raise MeetingError("A meeting is already recording. Stop it first.")
            if self._dictation_busy():
                raise MeetingError("Release the dictation key first; the microphone is in use.")
            retention = retention if retention in RETENTION_CHOICES else self.status()["default_retention"]
            wants = {"mic": mic, "system": system, "mic_device": mic_device, "system_device": system_device}
            sources = self._source_factory(wants)
            if not sources:
                raise MeetingError("No capture source is available.")
            described = {s.label: {"device": wants.get(f"{s.label}_device")} for s in sources}
            if MIC in described:
                # Several people around one microphone: label that track too.
                described[MIC]["shared"] = bool(mic_shared)
            meeting = self.store.create_meeting(title, described, retention, engine=engine['provider'])
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
            if live_transcription:
                self.store.set_live_options(meeting['id'], {'speakers': bool(live_speakers),
                    'provider': engine['provider'], 'model': engine['model']})
                self._start_live(meeting['id'], cfg, engine, session=session,
                                 speaker_sources=([SYSTEM] if system and live_speakers else []) +
                                 ([MIC] if mic and mic_shared and live_speakers else []))
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
            if meeting_id in self._live:
                self._live[meeting_id].finish()
                return summary
            try:
                self.transcribe(meeting_id)
            except ConsentRequired:
                pass  # a cloud engine waits for the user's go-ahead in the window
            except MeetingError as exc:
                self.notify(str(exc))
        return summary

    def _interrupted(self, session: CaptureSession, reason: str) -> None:
        live = self._live.get(session.meeting_id)
        if live:
            live.finish()
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
            meeting['incremental'] = self.store.live_options(meeting['id']) is not None
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
            "live": self._live[meeting_id].status() if meeting_id in self._live else None,
            "notes": self.store.get_notes(meeting_id),
            "speakers": self.store.list_speakers(meeting_id),
            "passages": self.store.list_passages(meeting_id),
            "events": self.store.list_events(meeting_id),
            "jobs": self.store.list_jobs(meeting_id),
            "summary": summary,
            "answers": self.store.list_analyses(meeting_id, "answer"),
            "drafts": self.store.list_analyses(meeting_id, "draft"),
        }

    def live_status(self, meeting_id: str) -> dict:
        live = self._live.get(meeting_id)
        state = live.status() if live else None
        if state:
            speakers = {s['id']: s['name'] for s in self.store.list_speakers(meeting_id)}
            state['active_names'] = [speakers.get(s, 'Speaker') for s in state['active_speakers']]
        return {'live': state, 'capture': self.capture_state() if self._session_meeting == meeting_id else None}

    def rename(self, meeting_id: str, title: str) -> dict:
        self.store.update_meeting(meeting_id, title=" ".join(str(title).split()))
        return self.store.get_meeting(meeting_id)

    def set_mic_shared(self, meeting_id: str, shared: bool) -> dict:
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            raise MeetingError("This meeting no longer exists.")
        sources = dict(meeting.get("sources") or {})
        sources[MIC] = dict(sources.get(MIC) or {}, shared=bool(shared))
        self.store.update_meeting(meeting_id, sources=sources)
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
        jobs = self.store.list_jobs(meeting_id, ("queued", "running"))
        if any(job['kind'] == 'speakers' for job in jobs):
            raise MeetingError("Wait for speaker labelling to finish before editing passages.")
        if meeting_id in self._live:
            return  # incremental commits never overwrite a completed passage
        for job in jobs:
            if job["kind"] == "transcribe":
                raise MeetingError("Wait for transcription to finish before editing passages.")

    def delete_meeting(self, meeting_id: str) -> None:
        live = self._live.get(meeting_id)
        if live:
            live.cancel()
        with self._lock:
            self._cancelled.add(meeting_id)
            if self._session_meeting == meeting_id and self._session is not None:
                self._session.stop("Deleted while recording")
                self._session, self._session_meeting = None, None
        # In-flight provider calls can finish later; store transactions reject
        # writes for deleted meetings, so deletion need not wait for the network.
        for job in self.store.list_jobs(meeting_id, ("queued", "running")):
            self.store.update_job(job["id"], state="cancelled")
        if live:
            live.join()
            self._live.pop(meeting_id, None)
        self.store.delete_meeting(meeting_id)
        self._audio_cache = {k: v for k, v in self._audio_cache.items() if k[0] != meeting_id}
        directory = self.store.meeting_dir(meeting_id)
        for _attempt in range(5):
            shutil.rmtree(directory, ignore_errors=True)
            if not directory.exists():
                break
            time.sleep(0.2)

    # ── jobs ───────────────────────────────────────────────────────
    def transcribe(self, meeting_id: str, *, remote_ok: bool = False) -> dict:
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
        engine = self.transcription_engine()
        if engine["kind"] == "cloud":
            if not engine["configured"]:
                raise MeetingError(f"{engine['label']} is not set up for meeting transcription. "
                                   "Check the key, endpoint and model in Settings > Meetings, or pick Orukeet.")
            if engine["remote"] and not remote_ok and self.transcription_policy() != "allow":
                raise ConsentRequired(self.transcription_disclosure(engine))
        job = self.store.create_job(meeting_id, "transcribe", "Waiting for the model")
        if self.store.live_options(meeting_id) is not None:
            self._start_live(meeting_id, self.config(), engine, job=job)
            return job
        self._jobs.put(job["id"])
        return job

    def _start_live(self, meeting_id, cfg, engine, *, session=None, speaker_sources=(), job=None):
        from .streaming import LiveSpeakerStream

        job = job or self.store.create_job(meeting_id, 'transcribe', 'Listening for a pause')
        cfg = dict(cfg)
        def decoder_factory():
            if engine['kind'] == 'local':
                models, transcriber = self._get_local_audio()
                if not models or not models.files_present() or not transcriber:
                    raise MeetingError('Orukeet is not installed. Audio is saved; install it in Settings and retry.')
                attempt = transcriber.begin_attempt()
                def decode(wav):
                    return attempt.transcribe_timed(wav, vocabulary=cfg.get('vocabulary', ()))
                return decode, attempt.close
            key = self._transcription_key(cfg)
            def decode(wav):
                try:
                    return (self._cloud_transcriber(engine, key, wav, cfg.get('language', 'auto'),
                                                    cfg.get('vocabulary', ())), [], [], [])
                except Exception as exc:
                    raise MeetingError(describe_error(exc)) from None
            return decode, lambda: None

        def complete():
            if self._closed or meeting_id in self._cancelled:
                return
            meeting = self.store.get_meeting(meeting_id)
            if not meeting:
                return
            self.store.update_meeting(meeting_id, transcribed_at=time.time(), engine=engine['provider'])
            self.store.add_event(meeting_id, meeting['elapsed'], 'transcribed', 'Live transcription complete')
            self.store.update_job(job['id'], state='done', progress=1.)
            self._after_transcribe(meeting_id)

        live = LiveTranscription(self.store, job, sources=self.store.get_meeting(meeting_id)['sources'],
            decoder_factory=decoder_factory, capture=session, on_complete=complete,
            speaker_key=self.pyannote_key(cfg), speaker_sources=speaker_sources,
            stream_factory=self._stream_factory or LiveSpeakerStream, timed=engine['kind'] == 'local',
            yield_to=lambda: self._yield_to() or self._closed)
        self._live[meeting_id] = live
        if session:
            session.on_audio = live.feed
        live.start()

    def summarize(self, meeting_id: str, *, include_notes: bool = False, remote_ok: bool = False) -> dict:
        self._check_analysis_allowed(remote_ok, include_notes=include_notes)
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
        self._check_analysis_allowed(remote_ok, include_notes=include_notes)
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

    def _check_analysis_allowed(self, remote_ok: bool, *, include_notes: bool = False) -> None:
        info = self.text_model_info()
        if not info["configured"]:
            raise MeetingError("No text model is configured. Open Settings > Rewriting and choose a provider.")
        if info["remote"] and not remote_ok and self.remote_policy() != "allow":
            raise ConsentRequired(self.disclosure(info, include_notes=include_notes))

    def disclosure(self, info: dict | None = None, *, include_notes: bool = False) -> dict:
        info = info or self.text_model_info()
        return {
            "kind": "remote", "policy_key": "remote",
            "title": f"Ownkey will send meeting text to {info['label']}",
            "intro": "This action sends transcript text to the selected text provider using your key. "
                     "Check what goes out, then decide how to handle this next time.",
            "provider": info["label"], "model": info["model"], "host": info["host"],
            "sent": ["Transcript text with passage ids and speaker names"] + (["My thoughts"] if include_notes else []),
            "not_sent": ["Audio"] + ([] if include_notes else ["My thoughts"]),
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

    def speaker_tracks_available(self, meeting_id: str) -> list[str]:
        sources = {chunk["source"] for chunk in self.store.list_chunks(meeting_id)}
        return [source for source in (MIC, SYSTEM) if source in sources]

    def _speaker_targets(self, meeting_id: str, meeting: dict | None = None) -> list[str]:
        """Default tracks to label: the call audio, plus the microphone when the
        meeting says several people share it; a lone microphone is labelled."""
        available = self.speaker_tracks_available(meeting_id)
        meeting = meeting or self.store.get_meeting(meeting_id) or {}
        shared = bool((meeting.get("sources") or {}).get(MIC, {}).get("shared"))
        if SYSTEM in available and MIC in available:
            return [MIC, SYSTEM] if shared else [SYSTEM]
        return available

    def label_speakers(self, meeting_id: str, *, remote_ok: bool = False, tracks: list[str] | None = None) -> dict:
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
        available = self.speaker_tracks_available(meeting_id)
        wanted = [str(t) for t in (tracks or [])] or self._speaker_targets(meeting_id, meeting)
        targets = [source for source in (MIC, SYSTEM) if source in wanted and source in available]
        if not targets:
            raise MeetingError("No audio track to label." if not available else "Choose at least one recorded track.")
        if not remote_ok and self.upload_policy() != "allow":
            raise ConsentRequired(self.upload_disclosure(targets))
        for job in self.store.list_jobs(meeting_id, ("queued", "running")):
            if job["kind"] in ("speakers", "transcribe"):
                return job
        job = self.store.create_job(meeting_id, "speakers", json.dumps({"tracks": targets}))
        self._jobs.put(job["id"])
        return job

    def _run_speakers(self, job: dict) -> bool | None:
        meeting_id = job["meeting_id"]
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            return
        cfg = self.config()
        client = self._diarizer_factory(cfg)
        targets = []
        try:
            targets = [t for t in json.loads(job.get("detail") or "{}").get("tracks", []) if t in (MIC, SYSTEM)]
        except (ValueError, AttributeError):
            targets = []
        available = self.speaker_tracks_available(meeting_id)
        targets = [t for t in targets if t in available] or self._speaker_targets(meeting_id, meeting)
        passages = self.store.list_passages(meeting_id)
        should_stop = lambda: meeting_id in self._cancelled or self._closed
        incremental = bool(self.store.live_options(meeting_id))
        found = []
        for source in targets:
            if should_stop():
                return
            label = SOURCE_LABELS.get(source, source)
            self.store.update_job(job["id"], detail=f"Uploading {label}", progress=0.1)
            segments = client.diarize_wav(
                self.audio_wav(meeting_id, source), f"{meeting_id}-{source}.wav", should_stop=should_stop,
                on_status=lambda status, label=label: self.store.update_job(
                    job["id"], detail=f"{label} · pyannoteAI {status}", progress=0.5))
            if should_stop():
                return
            mine = [p for p in passages if p["source"] == source]
            others = [p for p in passages if p["source"] != source]
            labelled, speakers = diarization.assign_speakers(mine, segments, source=source, fallback_speaker=source,
                                                             first_number=len(found) + 1,
                                                             preserve_passages=incremental)
            for speaker in speakers:
                existing = self.store.get_speaker(meeting_id, speaker["id"])
                if existing is None:
                    self.store.ensure_speaker(meeting_id, speaker["id"], source, speaker["name"], confirmed=False)
                elif not existing["confirmed"]:
                    # unconfirmed placeholders take the fresh numbering; confirmed names stay
                    self.store.rename_speaker(meeting_id, speaker["id"], speaker["name"], confirmed=False)
            found.extend(speakers)
            passages = others + labelled
        if should_stop():
            return
        self.store.order_speakers(meeting_id, [s["id"] for s in found])  # chips follow the numbering
        merged = sorted(passages, key=lambda p: p['start']) if incremental else merge_tracks({"all": passages})
        self.store.replace_passages(meeting_id, merged)
        self.store.add_event(meeting_id, meeting.get("elapsed", 0.0), "speakers",
                             f"{len(found)} speaker{'s' if len(found) != 1 else ''} · pyannoteAI {diarization.DEFAULT_MODEL}")
        if meeting['retention'] == 'after_transcription':
            self.sweep_retention()
        return True

    def _worker_loop(self) -> None:
        while not self._closed:
            try:
                job_id = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            job = self.store.get_job(job_id)
            if job is None or job["state"] != "queued":
                continue
            if self._closed:
                self.store.update_job(job_id, state='interrupted', detail='Ownkey closed before processing finished')
                continue
            if job["meeting_id"] in self._cancelled:
                self.store.update_job(job_id, state="cancelled")
                continue
            self._running_job = {"id": job_id, "kind": job["kind"], "meeting_id": job["meeting_id"]}
            self.store.update_job(job_id, state="running", attempts=job["attempts"] + 1)
            try:
                if job["kind"] == "transcribe":
                    completed = self._run_transcribe(job)
                elif job["kind"] == "summary":
                    completed = self._run_summary(job)
                elif job["kind"] == "draft":
                    completed = self._run_draft(job)
                elif job["kind"] == "speakers":
                    completed = self._run_speakers(job)
                else:
                    raise MeetingError(f"Unknown job {job['kind']}")
                if job["meeting_id"] in self._cancelled:
                    self.store.update_job(job_id, state="cancelled")
                elif self._closed and not completed:
                    self.store.update_job(job_id, state='interrupted', detail='Ownkey closed before processing finished')
                else:
                    # A completed handler has saved its full result, even if
                    # shutdown began while the provider was responding.
                    self.store.update_job(job_id, state="done", progress=1.0, error="")
                    if job['kind'] in ('transcribe', 'speakers'):
                        self.sweep_retention()
            except Exception as exc:
                if job["meeting_id"] in self._cancelled:
                    self.store.update_job(job_id, state="cancelled")
                    continue
                if self._closed:
                    self.store.update_job(job_id, state='interrupted', detail='Ownkey closed before processing finished')
                    continue
                message = describe_error(exc)
                self.store.update_job(job_id, state="error", error=message)
                self.notify(f"Meeting {job['kind']} failed: {message}")
            finally:
                self._running_job = None

    def _run_transcribe(self, job: dict) -> bool | None:
        meeting_id = job["meeting_id"]
        cfg = self.config()
        engine = self.transcription_engine(cfg)
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            return
        attempt = None
        if engine["kind"] == "local":
            models, transcriber = self._get_local_audio()
            if models is None or transcriber is None or not models.files_present():
                raise MeetingError("Orukeet is not installed. Open Settings > Transcription, download it, then retry, "
                                   "or choose a cloud provider for meetings in Settings > Meetings.")
            self.store.update_job(job["id"], detail="Loading Orukeet")
            attempt = transcriber.begin_attempt()
            decode = attempt.transcribe_timed
            where = "Orukeet on this PC"
        else:
            if not engine["configured"]:
                raise MeetingError(f"{engine['label']} is not set up for meeting transcription. Check Settings > Meetings.")
            key = self._transcription_key(cfg)
            language = str(cfg.get("language", "auto") or "auto")
            vocabulary = list(cfg.get("vocabulary") or [])
            self.store.update_job(job["id"], detail=f"Sending windows to {engine['label']}")

            def decode(wav_bytes, engine=engine, key=key):
                # Cloud providers return text without timing: one passage per window.
                return (self._cloud_transcriber(engine, key, wav_bytes, language, vocabulary), [], [], [])

            where = f"{engine['label']} ({'remote' if engine['remote'] else 'local endpoint'})"
        decode = self._yielding(decode, meeting_id)
        try:
            # Keep an existing transcript and its edits until the complete
            # replacement can be committed in one store transaction.
            replacing_existing = bool(self.store.list_passages(meeting_id))
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
                    if should_stop():
                        return
                    fraction = min(0.99, (done_before + done) / total_seconds)
                    self.store.update_job(job["id"], progress=fraction,
                                          detail=f"{SOURCE_LABELS.get(source, source)} · {int(done // 60)}:{int(done % 60):02d} of {int(total // 60)}:{int(total % 60):02d}")
                    if new and not replacing_existing:
                        provisional = [dict(p, id=f"{source}-{p['id']}") for p in new]
                        self.store.append_passages(meeting_id, provisional, meeting["transcript_rev"] + 1)

                tracks[source] = transcribe_track(samples, decode, source=source,
                                                  on_progress=progress, should_stop=should_stop)
                done_before += totals[source]
                if should_stop():
                    return
            merged = merge_tracks(tracks)
            self.store.replace_passages(meeting_id, merged)
            self.store.update_meeting(meeting_id, transcribed_at=time.time(), engine=engine["provider"])
            self.store.add_event(meeting_id, meeting.get("elapsed", 0.0), "transcribed",
                                 f"{len(merged)} passages · {where}")
        finally:
            if attempt is not None:
                attempt.close()
        self._after_transcribe(meeting_id)
        return True

    def _after_transcribe(self, meeting_id):
        self._audio_cache.clear()
        latest = self.store.get_meeting(meeting_id)
        if not latest or meeting_id in self._cancelled or self._closed:
            return
        cfg = self.config()
        auto_speakers = (cfg.get("meetings_auto_speakers") and self.speaker_labels_info(cfg)["configured"]
                and self.upload_policy() == "allow" and latest and latest["audio_state"] == "kept"
                and self._speaker_targets(meeting_id, latest)
                and not (self.store.live_options(meeting_id) or {}).get('speakers'))
        if auto_speakers:
            self._jobs.put(self.store.create_job(
                meeting_id, "speakers", json.dumps({"tracks": self._speaker_targets(meeting_id, latest)}))["id"])
        elif latest and latest['retention'] == 'after_transcription':
            self.sweep_retention()
        if cfg.get("meetings_auto_summary") and self.text_model_info(cfg)["configured"]:
            info = self.text_model_info(cfg)
            if not info["remote"] or self.remote_policy() == "allow":
                self._jobs.put(self.store.create_job(meeting_id, "summary", json.dumps({"include_notes": False}))["id"])

    def _yielding(self, decode, meeting_id: str, *, max_wait: float = 120.0):
        """Wrap a window decoder so it waits, between windows, while dictation
        is recording or has audio waiting; a meeting never delays typing."""

        def wrapped(wav_bytes):
            waited = 0.0
            while self._yield_to() and waited < max_wait and meeting_id not in self._cancelled and not self._closed:
                time.sleep(0.1)
                waited += 0.1
            if meeting_id in self._cancelled or self._closed:
                return '', [], [], []
            return decode(wav_bytes)

        return wrapped

    def _run_summary(self, job: dict) -> bool | None:
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
        return True

    def _run_draft(self, job: dict) -> bool | None:
        meeting_id = job["meeting_id"]
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            return
        cfg = self.config()
        info = self.text_model_info(cfg)
        self.store.update_job(job["id"], detail=f"Asking {info['label'] or info['provider']}", progress=0.2)
        latest = self.store.latest_analysis(meeting_id, "summary")
        summary = None
        if latest is not None and latest['input_rev'] == meeting['transcript_rev'] and not latest['include_notes']:
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
        return True

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

    def remove_audio(self, meeting_id: str, *, processing_complete: bool = False) -> dict:
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            raise MeetingError("This meeting no longer exists.")
        if meeting["state"] in ("recording", "paused"):
            raise MeetingError("Stop the meeting first.")
        if not processing_complete and any(j['kind'] in ('transcribe', 'speakers')
                for j in self.store.list_jobs(meeting_id, ('queued', 'running'))):
            raise MeetingError('Wait for transcription and speaker labels to finish before removing audio.')
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
                try:
                    self.remove_audio(meeting["id"])
                except MeetingError:
                    continue  # Active jobs keep their audio until completion.
                removed.append(meeting["id"])
        return removed

    def _retention_loop(self) -> None:
        while not self._maintenance_stop.wait(RETENTION_SWEEP_SECONDS):
            try:
                self.sweep_retention()
            except Exception:
                # Retry on the next sweep if the disk is temporarily unavailable.
                pass

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
                                  detail["drafts"], detail["answers"],
                                  summary_rev=detail['summary']['input_rev'] if detail['summary'] else None)
        return f"{stamp}-{stem}.md", body.encode("utf-8")

    # ── shutdown ───────────────────────────────────────────────────
    def close(self, *, stop_recording: bool = True) -> None:
        """Stops capture, waits briefly for the job worker and closes the library.
        A worker still busy with a job keeps the database open until the process ends."""
        self._closed = True
        self._maintenance_stop.set()
        self._maintenance.join(timeout=2.0)
        self._mic_preview.stop()
        for live in list(self._live.values()):
            live.cancel()
        with self._lock:
            session = self._session
            if session is not None and stop_recording:
                try:
                    session.stop()
                except Exception:
                    pass
            self._session, self._session_meeting = None, None
        self._worker.join(timeout=2.0)
        for live in list(self._live.values()):
            live.join()
        if not self._maintenance.is_alive() and not self._worker.is_alive() and not any(l.alive for l in self._live.values()):
            try:
                self.store.close()
            except Exception:
                pass


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
