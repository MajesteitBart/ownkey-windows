import http.client
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from meetings.capture import ArraySource, MIC, SYSTEM
from meetings.server import MeetingServer
from meetings.service import ConsentRequired, MeetingError, MeetingService
from meetings.store import MeetingStore


class FakeModels:
    def __init__(self, installed=True):
        self.installed = installed

    def files_present(self):
        return self.installed


class FakeAttempt:
    def __init__(self, service):
        self.service = service
        self.closed = False

    def transcribe_timed(self, wav_bytes):
        self.service.decoded += 1
        seconds = max(0.5, (len(wav_bytes) - 44) / 2 / 16000)
        return ("hallo wereld.", [" hallo", " wereld", "."], [0.2, 0.6, 0.9], [0.3, 0.3, 0.1]) if seconds > 0 else ("", [], [], [])

    def close(self):
        self.closed = True


class FakeTranscriber:
    def __init__(self):
        self.decoded = 0
        self.attempts = []

    def snapshot(self):
        return {"state": "Loaded", "error": ""}

    def begin_attempt(self, model_id=None):
        attempt = FakeAttempt(self)
        self.attempts.append(attempt)
        return attempt


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MeetingStore(self.directory.name)
        self.addCleanup(self.store.close)
        self.cfg = {"rewrite_provider": "openrouter", "rewrite_endpoint": "https://openrouter.ai/api/v1/chat/completions",
                    "rewrite_model": "vendor/model", "rewrite_api_key": "key", "meetings_remote_policy": "ask"}
        self.sources = {}
        self.chats = []
        self.notifications = []

        def chat(cfg, system, user, max_tokens):
            self.chats.append((system, user))
            if "answerable" in system:
                return json.dumps({"answerable": True, "answer": "Jullie zeiden hallo wereld.", "refs": ["p0001"]})
            if "JSON" in system:
                return json.dumps({"overview": "Kort overleg.", "decisions": [{"text": "hallo wereld", "status": "decided", "refs": ["p0001"]}],
                                   "actions": [], "questions": []})
            return "Hoi,\n\nhallo wereld (00:00)."

        def sources(wants):
            made = [ArraySource(label) for label, wanted in (("mic", wants["mic"]), ("system", wants["system"])) if wanted]
            self.sources = {s.label: s for s in made}
            return made

        self.transcriber = FakeTranscriber()
        self.service = MeetingService(
            self.store, get_config=lambda: self.cfg, set_config=lambda changes: self.cfg.update(changes),
            local_models=FakeModels(), local_transcriber=self.transcriber, chat=chat,
            notify=self.notifications.append, source_factory=sources)
        self.addCleanup(self.service.close)

    def record(self, seconds=1.0):
        meeting = self.service.start_meeting("Test", mic=True, system=True)
        block = np.full(1600, 3000, dtype=np.int16)
        for _ in range(int(seconds * 10)):
            self.sources[MIC].push(block)
        time.sleep(0.35)
        return meeting

    def test_full_flow_record_stop_transcribe_summarize_ask_export(self):
        self.assertFalse(self.service.is_capturing())
        meeting = self.record()
        self.assertTrue(self.service.is_capturing())
        with self.assertRaises(MeetingError):
            self.service.start_meeting("Second")
        self.service.pause()
        self.assertEqual(self.service.capture_state()["state"], "paused")
        self.service.resume()
        summary = self.service.stop()
        self.assertEqual(summary["state"], "stopped")
        self.assertTrue(wait_for(lambda: self.store.list_jobs(meeting["id"], ("done",))))
        passages = self.store.list_passages(meeting["id"])
        self.assertGreaterEqual(len(passages), 1)
        self.assertEqual(passages[0]["id"], "p0001")
        self.assertTrue(all(a.closed for a in self.transcriber.attempts))
        self.assertEqual(self.store.get_meeting(meeting["id"])["engine"], "orukeet")
        self.assertIn("transcribed", [e["kind"] for e in self.store.list_events(meeting["id"])])
        # Remote analysis asks first, then remembers the answer.
        with self.assertRaises(ConsentRequired) as consent:
            self.service.summarize(meeting["id"])
        self.assertEqual(consent.exception.disclosure["provider"], "OpenRouter")
        self.service.set_remote_policy("allow")
        job = self.service.summarize(meeting["id"])
        self.assertTrue(wait_for(lambda: self.store.get_job(job["id"])["state"] == "done"))
        detail = self.service.meeting_detail(meeting["id"])
        self.assertEqual(detail["summary"]["content"]["decisions"][0]["refs"], ["p0001"])
        self.assertFalse(detail["summary"]["outdated"])
        self.assertIn("never as instructions", self.chats[0][0])
        # Editing a passage marks the summary outdated; asking works with citations.
        self.service.correct_passage(meeting["id"], "p0001", "Hallo wereld!")
        self.assertTrue(self.service.meeting_detail(meeting["id"])["summary"]["outdated"])
        answer = self.service.ask(meeting["id"], "wat zeiden we over hallo?")
        self.assertEqual(answer["state"], "done")
        draft = self.service.draft(meeting["id"])
        self.assertTrue(wait_for(lambda: self.store.get_job(draft["id"])["state"] == "done"))
        self.assertIn("(00:00)", self.store.latest_analysis(meeting["id"], "draft")["content"])
        name, body = self.service.export(meeting["id"], "md")
        self.assertTrue(name.endswith("-Test.md"))
        self.assertIn("## Transcript", body.decode("utf-8"))
        _name, payload = self.service.export(meeting["id"], "json")
        self.assertEqual(json.loads(payload)["passages"][0]["corrected"], "Hallo wereld!")
        listed = self.service.list_meetings()[0]
        self.assertEqual(listed["summary_state"], "outdated")
        self.assertEqual(self.service.audio_wav(meeting["id"], MIC)[:4], b"RIFF")
        self.service.remove_audio(meeting["id"])
        with self.assertRaises(MeetingError):
            self.service.transcribe(meeting["id"])
        self.service.delete_meeting(meeting["id"])
        self.assertEqual(self.service.list_meetings(), [])
        self.assertFalse(self.store.meeting_dir(meeting["id"]).exists())

    def test_missing_model_keeps_the_meeting_and_fails_the_job(self):
        self.service.local_models = FakeModels(installed=False)
        meeting = self.record()
        self.service.stop()
        self.assertTrue(wait_for(lambda: self.store.list_jobs(meeting["id"], ("error",))))
        job = self.store.list_jobs(meeting["id"], ("error",))[0]
        self.assertIn("Orukeet is not installed", job["error"])
        self.assertEqual(self.store.get_meeting(meeting["id"])["state"], "stopped")
        self.assertTrue(self.notifications)
        self.service.local_models = FakeModels(installed=True)
        retry = self.service.transcribe(meeting["id"])
        self.assertTrue(wait_for(lambda: self.store.get_job(retry["id"])["state"] == "done"))

    def test_unconfigured_text_model_is_a_clear_error(self):
        self.cfg["rewrite_api_key"] = ""
        meeting = self.record()
        self.service.stop()
        self.assertTrue(wait_for(lambda: self.store.list_jobs(meeting["id"], ("done",))))
        with self.assertRaises(MeetingError) as error:
            self.service.ask(meeting["id"], "hallo?")
        self.assertIn("No text model", str(error.exception))

    def test_local_text_model_needs_no_consent(self):
        self.cfg.update({"rewrite_provider": "ollama", "rewrite_endpoint": "http://localhost:11434/api/chat", "rewrite_api_key": ""})
        self.assertFalse(self.service.text_model_info()["remote"])
        meeting = self.record()
        self.service.stop()
        self.assertTrue(wait_for(lambda: self.store.list_jobs(meeting["id"], ("done",))))
        self.assertEqual(self.service.summarize(meeting["id"])["kind"], "summary")

    def test_speaker_labels_ask_before_upload_then_split_passages(self):
        uploads = []

        class FakeDiarizer:
            def diarize_wav(self, data, name, should_stop=None, on_status=None, **options):
                uploads.append((name, data[:4]))
                on_status("running")
                return [{"start": 0.0, "end": 0.45, "speaker": "SPEAKER_00"}, {"start": 0.45, "end": 5.0, "speaker": "SPEAKER_01"}]

        self.service._diarizer_factory = lambda cfg: FakeDiarizer()
        self.cfg["pyannote_api_key"] = "pk"
        meeting = self.service.start_meeting("Two voices", mic=True, system=True)
        block = np.full(1600, 3000, dtype=np.int16)
        for _ in range(10):
            self.sources[MIC].push(block)
            self.sources[SYSTEM].push(block)
        time.sleep(0.35)
        self.service.stop()
        self.assertTrue(wait_for(lambda: self.store.list_jobs(meeting["id"], ("done",))))
        self.assertTrue(any(p["source"] == "system" for p in self.store.list_passages(meeting["id"])))
        with self.assertRaises(ConsentRequired) as consent:
            self.service.label_speakers(meeting["id"])
        self.assertEqual(consent.exception.disclosure["policy_key"], "upload")
        self.assertIn("Call audio track", consent.exception.disclosure["sent"][0])
        self.assertEqual(uploads, [])
        job = self.service.label_speakers(meeting["id"], remote_ok=True)
        self.assertTrue(wait_for(lambda: self.store.get_job(job["id"])["state"] in ("done", "error")))
        self.assertEqual(self.store.get_job(job["id"])["state"], "done", self.store.get_job(job["id"])["error"])
        self.assertEqual(uploads[0][0], f"{meeting['id']}-system.wav")
        self.assertEqual(uploads[0][1], b"RIFF")
        speakers = {s["id"]: s for s in self.store.list_speakers(meeting["id"])}
        self.assertEqual(speakers["system-1"]["name"], "Speaker 1")
        self.assertEqual(speakers["system-2"]["confirmed"], 0)
        system_passages = [p for p in self.store.list_passages(meeting["id"]) if p["source"] == "system"]
        self.assertEqual({p["speaker_id"] for p in system_passages}, {"system-1", "system-2"})
        self.assertTrue(all(p["source"] != "system" or p["tokens"] for p in self.store.list_passages(meeting["id"])))
        self.assertIn("speakers", [e["kind"] for e in self.store.list_events(meeting["id"])])
        # The mic track keeps its own label and its passages.
        self.assertEqual({p["speaker_id"] for p in self.store.list_passages(meeting["id"]) if p["source"] == "mic"}, {"mic"})
        # Remembered policy skips the question; a missing key is a clear error.
        self.service.set_policy("upload", "allow")
        self.assertEqual(self.service.label_speakers(meeting["id"])["kind"], "speakers")
        self.assertTrue(wait_for(lambda: not self.store.list_jobs(meeting["id"], ("queued", "running"))))
        self.cfg["pyannote_api_key"] = ""
        with self.assertRaises(MeetingError) as missing:
            self.service.label_speakers(meeting["id"])
        self.assertIn("pyannoteAI key", str(missing.exception))
        self.cfg["pyannote_api_key"] = "pk"
        # Several people share the microphone: label that track too, numbering on.
        self.assertEqual(self.service._speaker_targets(meeting["id"]), ["system"])
        self.service.set_mic_shared(meeting["id"], True)
        self.assertTrue(self.store.get_meeting(meeting["id"])["sources"]["mic"]["shared"])
        self.assertEqual(self.service._speaker_targets(meeting["id"]), ["mic", "system"])
        self.service.set_policy("upload", "ask")
        with self.assertRaises(ConsentRequired) as both:
            self.service.label_speakers(meeting["id"], tracks=["mic", "system"])
        self.assertIn("Microphone and Call audio", both.exception.disclosure["sent"][0])
        self.assertNotIn("Microphone track", both.exception.disclosure["not_sent"])
        uploads.clear()
        self.service.rename_speaker(meeting["id"], "system-2", "Femke", confirmed=True)
        job = self.service.label_speakers(meeting["id"], remote_ok=True, tracks=["mic", "system", "bogus"])
        self.assertTrue(wait_for(lambda: self.store.get_job(job["id"])["state"] in ("done", "error")))
        self.assertEqual(self.store.get_job(job["id"])["state"], "done", self.store.get_job(job["id"])["error"])
        self.assertEqual([u[0] for u in uploads], [f"{meeting['id']}-mic.wav", f"{meeting['id']}-system.wav"])
        names = {s["id"]: s["name"] for s in self.store.list_speakers(meeting["id"])}
        self.assertEqual((names["mic-1"], names["mic-2"]), ("Speaker 1", "Speaker 2"))
        self.assertEqual(names["system-1"], "Speaker 3", "numbering continues across tracks; no second Speaker 1")
        self.assertEqual(names["system-2"], "Femke", "a confirmed name survives relabelling")
        self.assertEqual([s["id"] for s in self.store.list_speakers(meeting["id"])],
                         ["mic", "system", "mic-1", "mic-2", "system-1", "system-2"], "chips follow the numbering")
        self.assertEqual({p["speaker_id"] for p in self.store.list_passages(meeting["id"]) if p["source"] == "mic"}, {"mic-1", "mic-2"})
        with self.assertRaises(MeetingError):
            self.service.label_speakers(meeting["id"], remote_ok=True, tracks=["bogus"])

    def test_transcription_engine_follows_dictation_or_its_own_provider(self):
        self.cfg.update({"audio_provider": "orukeet", "audio_endpoint": "", "audio_model": "orukeet-onnx-int8"})
        engine = self.service.transcription_engine()
        self.assertEqual((engine["kind"], engine["provider"], engine["remote"], engine["follows_dictation"]), ("local", "orukeet", False, True))
        self.cfg.update({"audio_provider": "openai", "audio_endpoint": "https://api.openai.com/v1/audio/transcriptions",
                         "audio_model": "whisper-1", "audio_api_key": ""})
        engine = self.service.transcription_engine()
        self.assertEqual((engine["kind"], engine["label"], engine["remote"], engine["configured"]), ("cloud", "OpenAI", True, False))
        self.cfg["audio_api_key"] = "sk"
        self.assertTrue(self.service.transcription_engine()["configured"])
        self.assertNotIn("key", self.service.status()["transcriber"])
        self.cfg.update({"meetings_audio_provider": "custom", "meetings_audio_endpoint": "http://localhost:1234/v1/audio/transcriptions",
                         "meetings_audio_model": "whisper-large", "meetings_audio_api_key": ""})
        engine = self.service.transcription_engine()
        self.assertEqual((engine["kind"], engine["remote"], engine["configured"], engine["follows_dictation"]), ("cloud", False, True, False))
        self.cfg["meetings_audio_provider"] = "orukeet"
        self.assertEqual(self.service.transcription_engine()["kind"], "local")

    def test_cloud_transcription_asks_first_then_makes_window_passages(self):
        calls = []

        def cloud(engine, key, wav_bytes, language, vocabulary):
            calls.append((engine["provider"], key, len(wav_bytes), language, tuple(vocabulary)))
            return "Hallo daar, dit is de cloud."

        self.service._cloud_transcriber = cloud
        self.cfg.update({"meetings_audio_provider": "mistral", "meetings_audio_endpoint": "https://api.mistral.ai/v1/audio/transcriptions",
                         "meetings_audio_model": "voxtral-mini-latest", "meetings_audio_api_key": "mk", "language": "nl",
                         "vocabulary": ["Ownkey"]})
        meeting = self.record()
        self.service.stop()
        time.sleep(0.3)
        self.assertEqual(self.store.list_jobs(meeting["id"]), [], "no upload without consent")
        with self.assertRaises(ConsentRequired) as consent:
            self.service.transcribe(meeting["id"])
        self.assertEqual(consent.exception.disclosure["policy_key"], "transcription")
        self.assertIn("Mistral", consent.exception.disclosure["title"])
        job = self.service.transcribe(meeting["id"], remote_ok=True)
        self.assertTrue(wait_for(lambda: self.store.get_job(job["id"])["state"] in ("done", "error")))
        self.assertEqual(self.store.get_job(job["id"])["state"], "done", self.store.get_job(job["id"])["error"])
        self.assertEqual(calls[0][:2], ("mistral", "mk"))
        self.assertEqual(calls[0][3:], ("nl", ("Ownkey",)))
        passages = self.store.list_passages(meeting["id"])
        self.assertEqual(len(passages), 1)
        self.assertEqual((passages[0]["text"], passages[0]["quality"], passages[0]["tokens"]), ("Hallo daar, dit is de cloud.", "window", None))
        self.assertEqual(self.store.get_meeting(meeting["id"])["engine"], "mistral")
        self.assertIn("Mistral (remote)", self.store.list_events(meeting["id"])[-1]["detail"])
        self.assertEqual(self.transcriber.decoded, 0, "Orukeet was not used")
        # A remembered policy lets Stop queue the upload on its own.
        self.service.set_policy("transcription", "allow")
        meeting = self.record()
        self.service.stop()
        self.assertTrue(wait_for(lambda: self.store.list_jobs(meeting["id"], ("done",))))
        self.cfg["meetings_audio_api_key"] = ""
        with self.assertRaises(MeetingError) as missing:
            self.service.transcribe(meeting["id"])
        self.assertIn("not set up", str(missing.exception))

    def test_dictation_comes_first(self):
        busy = {"dictating": False, "queue": False}
        self.service._dictation_busy = lambda: busy["dictating"]
        self.service._yield_to = lambda: busy["queue"]
        busy["dictating"] = True
        with self.assertRaises(MeetingError) as blocked:
            self.service.start_meeting("Over a held key")
        self.assertIn("dictation key", str(blocked.exception))
        busy["dictating"] = False
        meeting = self.record()
        busy["queue"] = True  # dictation audio is waiting for the decoder
        self.service.stop()
        time.sleep(0.6)
        self.assertEqual(self.transcriber.decoded, 0, "meeting windows wait while dictation is busy")
        busy["queue"] = False
        self.assertTrue(wait_for(lambda: self.store.list_jobs(meeting["id"], ("done",))))
        self.assertGreater(self.transcriber.decoded, 0)

    def test_interrupted_source_notifies_and_transcription_can_run_later(self):
        self.cfg['meetings_retention'] = 'after_transcription'
        meeting = self.record()
        self.sources[SYSTEM].fail("Speakers disappeared")
        self.assertTrue(wait_for(lambda: not self.service.is_capturing()))
        self.assertEqual(self.store.get_meeting(meeting["id"])["state"], "interrupted")
        self.assertTrue(wait_for(lambda: bool(self.notifications)))
        self.assertIn("Speakers disappeared", self.notifications[0])
        job = self.service.transcribe(meeting["id"])
        self.assertTrue(wait_for(lambda: self.store.get_job(job["id"])["state"] == "done"))
        self.assertTrue(wait_for(lambda: self.store.get_meeting(meeting['id'])['audio_state'] == 'removed'))

    def test_analysis_consent_discloses_notes_for_the_selected_action(self):
        mid = self.store.create_meeting('Synthetic consent test', {})['id']
        for action in ('summary', 'question'):
            for notes in (False, True):
                with self.subTest(action=action, notes=notes), self.assertRaises(ConsentRequired) as consent:
                    if action == 'summary':
                        self.service.summarize(mid, include_notes=notes)
                    else:
                        self.service.ask(mid, 'Synthetic question?', include_notes=notes)
                self.assertEqual('My thoughts' in consent.exception.disclosure['sent'], notes)
                self.assertEqual('My thoughts' in consent.exception.disclosure['not_sent'], not notes)
        self.assertEqual(self.chats, [], 'Disclosure happens before contacting the provider')

    def test_diarization_blocks_passage_edits_including_live_meetings(self):
        mid = self.store.create_meeting('Synthetic speaker edit test', {})['id']
        self.store.ensure_speaker(mid, 'mic', 'mic', 'Microphone')
        self.store.ensure_speaker(mid, 'person', 'mic', 'Synthetic speaker')
        self.store.replace_passages(mid, [{'id': 'p0001', 'source': 'mic', 'speaker_id': 'mic',
            'start': 0, 'end': 1, 'text': 'Synthetic passage.'}], 1)
        job = self.store.create_job(mid, 'speakers')
        try:
            for live in (False, True):
                if live:
                    self.service._live[mid] = object()  # Completed live session still has a coordinator entry.
                for state in ('queued', 'running'):
                    self.store.update_job(job['id'], state=state)
                    with self.subTest(live=live, state=state):
                        with self.assertRaisesRegex(MeetingError, 'speaker labelling'):
                            self.service.assign_speaker(mid, 'p0001', 'person')
                        with self.assertRaisesRegex(MeetingError, 'speaker labelling'):
                            self.service.correct_passage(mid, 'p0001', 'Synthetic correction.')
            self.assertEqual(self.store.get_passage(mid, 'p0001')['speaker_id'], 'mic')
            self.store.update_job(job['id'], state='done')
            self.assertEqual(self.service.assign_speaker(mid, 'p0001', 'person')['speaker_id'], 'person')
        finally:
            self.service._live.pop(mid, None)

    def test_batch_retranscription_preserves_edits_until_complete_replacement(self):
        from meetings import audio
        self.cfg.update(meetings_audio_provider='mistral', meetings_audio_model='test',
                        meetings_audio_endpoint='https://example.invalid', meetings_audio_api_key='synthetic')
        for fails in (True, False):
            with self.subTest(fails=fails):
                mid = self.store.create_meeting('Synthetic retranscription', {'mic': {}})['id']
                self.store.update_meeting(mid, state='stopped', transcribed_at=time.time())
                self.store.ensure_speaker(mid, 'person', 'mic', 'Synthetic speaker')
                self.store.replace_passages(mid, [{'id': 'p0001', 'source': 'mic', 'speaker_id': 'mic',
                    'start': 0, 'end': 1, 'text': 'Synthetic existing transcript.'}], 1)
                self.service.correct_passage(mid, 'p0001', 'Synthetic saved correction.')
                self.service.assign_speaker(mid, 'p0001', 'person')
                before = self.store.list_passages(mid)
                path = self.store.audio_dir(mid, MIC) / 'fixture.wav'
                samples = np.full(31 * 16000, 3000, dtype=np.int16)
                audio.write_wav(str(path), samples)
                self.store.add_chunk(mid, MIC, 0, 0, samples.size, str(path))
                entered, release = threading.Event(), threading.Event()
                calls = []
                def decode(*args):
                    calls.append(True)
                    if len(calls) == 1:
                        return 'Synthetic existing transcript.'
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('Test did not release provider')
                    if fails:
                        raise RuntimeError('Synthetic provider failure')
                    return 'Synthetic existing transcript.'
                self.service._cloud_transcriber = decode
                job = self.service.transcribe(mid, remote_ok=True)
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertGreaterEqual(len(calls), 2)
                    self.assertEqual(self.store.list_passages(mid), before)
                finally:
                    release.set()
                self.assertTrue(wait_for(lambda: self.store.get_job(job['id'])['state'] in ('done', 'error')))
                self.assertEqual(self.store.get_job(job['id'])['state'], 'error' if fails else 'done')
                after = self.store.list_passages(mid)
                if fails:
                    self.assertEqual(after, before)
                self.assertEqual(after[0]['corrected'], 'Synthetic saved correction.')
                self.assertEqual(after[0]['speaker_id'], 'person')

    def test_delete_during_cloud_decode_cannot_restore_transcript(self):
        started, release = threading.Event(), threading.Event()
        def cloud(*args):
            started.set()
            if not release.wait(5):
                raise RuntimeError('Test did not release the provider')
            return 'Synthetic delayed response.'
        self.service._cloud_transcriber = cloud
        self.cfg.update(meetings_audio_provider='mistral', meetings_audio_model='test',
                        meetings_audio_endpoint='https://example.invalid/transcriptions',
                        meetings_audio_api_key='synthetic', meetings_transcription_policy='allow')
        meeting = self.record()
        self.service.stop()
        try:
            self.assertTrue(started.wait(3))
            before = time.monotonic()
            self.service.delete_meeting(meeting['id'])
            self.assertLess(time.monotonic() - before, 2, 'Deletion must not wait for the provider')
            self.assertIsNone(self.store.get_meeting(meeting['id']))
        finally:
            release.set()
        self.assertTrue(wait_for(lambda: self.service._running_job is None))
        self.assertEqual(self.store.list_passages(meeting['id']), [])
        self.assertEqual(self.store.list_events(meeting['id']), [])
        self.assertEqual(self.store.list_jobs(meeting['id']), [])
        self.assertFalse(self.store.meeting_dir(meeting['id']).exists())

    def test_after_transcription_retention_waits_for_job_then_removes_audio(self):
        self.cfg['meetings_retention'] = 'after_transcription'
        meeting = self.record()
        self.service.stop()
        self.assertTrue(wait_for(lambda: self.store.get_meeting(meeting['id'])['audio_state'] == 'removed'))
        self.assertEqual(self.store.list_jobs(meeting['id'])[0]['state'], 'done')
        self.assertTrue(self.store.list_passages(meeting['id']))

    def test_drafts_do_not_reuse_stale_or_note_inclusive_summaries(self):
        mid = self.store.create_meeting('Synthetic draft test', {})['id']
        self.store.update_meeting(mid, state='stopped', transcript_rev=2)
        for rev, notes in ((1, False), (2, True)):
            with self.subTest(rev=rev, notes=notes):
                self.store.add_analysis(mid, 'summary', provider='test', model='test', input_rev=rev,
                    include_notes=notes, content='{"overview": "Old summary or private notes"}', refs=[])
                job = self.store.create_job(mid, 'draft')
                with patch('meetings.service.analysis.draft_followup', return_value='Synthetic draft') as draft:
                    self.service._run_draft(job)
                self.assertIsNone(draft.call_args.args[0])


class RetentionTests(unittest.TestCase):
    def test_periodic_sweep_expires_audio_without_restart_and_skips_busy_meeting(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1000.]
            store = MeetingStore(directory, clock=lambda: now[0])
            with patch('meetings.service.RETENTION_SWEEP_SECONDS', 0.02):
                service = MeetingService(store, get_config=lambda: {})
                try:
                    ids = [store.create_meeting('Synthetic retention test', {})['id'] for _ in range(2)]
                    for mid in ids:
                        store.update_meeting(mid, state='stopped', transcribed_at=now[0])
                        store.audio_dir(mid, MIC).mkdir(parents=True)
                        (store.audio_dir(mid, MIC) / 'test.wav').write_bytes(b'synthetic')
                    job = store.create_job(ids[0], 'speakers')
                    now[0] += 7 * 86400 + 1
                    self.assertTrue(wait_for(lambda: store.get_meeting(ids[1])['audio_state'] == 'removed'))
                    self.assertFalse(store.audio_dir(ids[1], MIC).exists())
                    self.assertEqual(store.get_meeting(ids[0])['audio_state'], 'kept')
                    store.update_job(job['id'], state='done')
                    self.assertTrue(wait_for(lambda: store.get_meeting(ids[0])['audio_state'] == 'removed'))
                finally:
                    service.close()
                self.assertFalse(service._maintenance.is_alive())


class ShutdownTests(unittest.TestCase):
    def test_shutdown_outcome_matches_committed_provider_results(self):
        from meetings import audio
        for kind in ('transcribe', 'speakers', 'summary', 'draft'):
            for fails in (False, True):
                with self.subTest(kind=kind, fails=fails), tempfile.TemporaryDirectory() as directory:
                    started, release = threading.Event(), threading.Event()
                    def provider(*args, **kwargs):
                        started.set()
                        if not release.wait(5):
                            raise RuntimeError('Test did not release provider')
                        if fails:
                            raise RuntimeError('Provider stopped during shutdown')
                        if kind == 'summary':
                            return json.dumps({'overview': 'Synthetic completed summary.', 'decisions': [], 'actions': [], 'questions': []})
                        return [] if kind == 'speakers' else 'Synthetic delayed text.'
                    class Diarizer:
                        diarize_wav = staticmethod(provider)
                    store = MeetingStore(directory)
                    service = MeetingService(store, get_config=lambda: {
                        'meetings_audio_provider': 'mistral', 'meetings_audio_model': 'test',
                        'meetings_audio_endpoint': 'https://example.invalid', 'meetings_audio_api_key': 'synthetic',
                        'rewrite_provider': 'openrouter', 'rewrite_endpoint': 'https://example.invalid',
                        'rewrite_model': 'test', 'rewrite_api_key': 'synthetic'},
                        cloud_transcriber=provider, diarizer_factory=lambda cfg: Diarizer(), chat=provider)
                    mid = store.create_meeting('Synthetic shutdown test', {'mic': {}})['id']
                    store.update_meeting(mid, state='stopped')
                    path = store.audio_dir(mid, MIC) / 'test.wav'
                    audio.write_wav(str(path), np.full(16000, 3000, dtype=np.int16))
                    store.add_chunk(mid, MIC, 0, 0, 16000, str(path))
                    is_analysis = kind in ('summary', 'draft')
                    had_transcript = kind != 'speakers'
                    if had_transcript:
                        store.replace_passages(mid, [{'id': 'p0001', 'source': 'mic', 'speaker_id': 'mic',
                            'start': 0, 'end': 1, 'text': 'Synthetic saved passage.'}], 1)
                        store.correct_passage(mid, 'p0001', 'Synthetic saved correction.')
                    job = store.create_job(mid, kind)
                    service._jobs.put(job['id'])
                    closer = None
                    try:
                        self.assertTrue(started.wait(3))
                        closer = threading.Thread(target=service.close)
                        closer.start()
                        self.assertTrue(wait_for(lambda: service._closed))
                        release.set()
                        closer.join(3)
                        self.assertFalse(closer.is_alive())
                        recovered = MeetingStore(directory)
                        try:
                            # Inspect the persisted result before startup reconciliation.
                            saved = is_analysis and not fails
                            self.assertEqual(recovered.get_job(job['id'])['state'], 'done' if saved else 'interrupted')
                            self.assertEqual(len(recovered.list_analyses(mid)), int(saved))
                            self.assertEqual(recovered.get_meeting(mid)['audio_state'], 'kept')
                            self.assertEqual(len(recovered.list_passages(mid)), int(had_transcript))
                            if had_transcript:
                                self.assertEqual(recovered.get_passage(mid, 'p0001')['corrected'], 'Synthetic saved correction.')
                            self.assertTrue(path.exists())
                        finally:
                            recovered.close()
                    finally:
                        release.set()
                        if closer:
                            closer.join(3)
                        service.close()


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MeetingStore(self.directory.name)
        self.addCleanup(self.store.close)
        self.sources = {}

        def sources(wants):
            made = [ArraySource(MIC)] if wants["mic"] else []
            self.sources = {s.label: s for s in made}
            return made

        self.service = MeetingService(self.store, get_config=lambda: {"rewrite_provider": "openrouter",
                                      "rewrite_endpoint": "https://openrouter.ai/api/v1/chat/completions",
                                      "rewrite_model": "m", "rewrite_api_key": "k"},
                                      local_models=FakeModels(), local_transcriber=FakeTranscriber(),
                                      chat=lambda *a: "{}", source_factory=sources)
        self.addCleanup(self.service.close)
        self.server = MeetingServer(self.service)
        self.server.start()
        self.addCleanup(self.server.stop)

    def request(self, method, path, body=None, token=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        extra = {"Content-Type": "application/json"}
        if token:
            extra["X-Ownkey-Token"] = self.server.token
        extra.update(headers or {})
        conn.request(method, path, body=payload, headers=extra)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, response.getheaders(), data

    def test_token_gate_static_and_api_flow(self):
        status, _headers, body = self.request("GET", "/api/status", token=False)
        self.assertEqual(status, 401)
        status, headers, body = self.request("GET", f"/?token={self.server.token}", token=False)
        self.assertEqual(status, 200)
        self.assertIn("ownkey_meetings=", dict(headers)["Set-Cookie"])
        self.assertIn(b"<!DOCTYPE html>", body)
        status, _h, body = self.request("GET", "/", token=False)
        self.assertEqual(status, 401)
        status, _h, body = self.request("GET", "/app.js", token=False)
        self.assertEqual(status, 200)
        status, _h, body = self.request("GET", "/../store.py", token=False)
        self.assertIn(status, (400, 404))

        status, _h, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["capturing"])
        status, _h, body = self.request("POST", "/api/meetings", {"title": "Via API", "mic": True, "system": False, "mic_shared": True})
        self.assertEqual(status, 201)
        meeting = json.loads(body)["meeting"]
        self.assertTrue(meeting["sources"]["mic"]["shared"])
        status, _h, body = self.request("PUT", f"/api/meetings/{meeting['id']}", {"mic_shared": False})
        self.assertFalse(json.loads(body)["meeting"]["sources"]["mic"]["shared"])
        self.sources[MIC].push(np.full(16000, 3000, dtype=np.int16))
        time.sleep(0.35)
        status, _h, body = self.request("POST", f"/api/meetings/{meeting['id']}/stop")
        self.assertEqual(status, 200)
        self.assertTrue(wait_for(lambda: self.store.list_jobs(meeting["id"], ("done",))))
        status, _h, body = self.request("PUT", f"/api/meetings/{meeting['id']}/notes", {"content": "note [00:01]"})
        self.assertEqual(json.loads(body)["notes"]["rev"], 1)
        status, _h, body = self.request("GET", f"/api/meetings/{meeting['id']}")
        detail = json.loads(body)
        self.assertEqual(detail["notes"]["content"], "note [00:01]")
        self.assertEqual(detail["speakers"][0]["name"], "Microphone")
        status, _h, body = self.request("PUT", f"/api/meetings/{meeting['id']}/speakers/mic", {"name": "You", "confirmed": True})
        self.assertEqual(json.loads(body)["speaker"]["confirmed"], 1)
        status, _h, body = self.request("POST", f"/api/meetings/{meeting['id']}/summary", {})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["consent"]["provider"], "OpenRouter")
        status, _h, body = self.request("GET", f"/api/meetings/{meeting['id']}/audio/mic.wav",
                                        headers={"Range": "bytes=0-3", "Cookie": f"ownkey_meetings={self.server.token}"}, token=False)
        self.assertEqual(status, 206)
        self.assertEqual(body, b"RIFF")
        status, headers, body = self.request("GET", f"/api/meetings/{meeting['id']}/export?format=json")
        self.assertIn("attachment", dict(headers)["Content-Disposition"])
        status, _h, body = self.request("POST", f"/api/meetings/{meeting['id']}/nonsense")
        self.assertEqual(status, 404)
        status, _h, body = self.request("DELETE", f"/api/meetings/{meeting['id']}")
        self.assertEqual(status, 200)
        status, _h, body = self.request("GET", f"/api/meetings/{meeting['id']}")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
