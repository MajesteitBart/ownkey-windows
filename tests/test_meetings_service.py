import http.client
import json
import tempfile
import threading
import time
import unittest

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

    def test_interrupted_source_notifies_and_transcription_can_run_later(self):
        meeting = self.record()
        self.sources[SYSTEM].fail("Speakers disappeared")
        self.assertTrue(wait_for(lambda: not self.service.is_capturing()))
        self.assertEqual(self.store.get_meeting(meeting["id"])["state"], "interrupted")
        self.assertIn("Speakers disappeared", self.notifications[0])
        job = self.service.transcribe(meeting["id"])
        self.assertTrue(wait_for(lambda: self.store.get_job(job["id"])["state"] == "done"))


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
        status, _h, body = self.request("POST", "/api/meetings", {"title": "Via API", "mic": True, "system": False})
        self.assertEqual(status, 201)
        meeting = json.loads(body)["meeting"]
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
