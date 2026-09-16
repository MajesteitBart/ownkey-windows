import os
import tempfile
import unittest

from meetings.store import MeetingStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.0
        self.store = MeetingStore(self.directory.name, clock=lambda: self.now)
        self.addCleanup(self.store.close)

    def test_create_list_and_update(self):
        meeting = self.store.create_meeting("Kickoff", {"mic": {"name": "USB"}}, "keep", engine="orukeet")
        self.assertEqual(meeting["state"], "recording")
        self.assertEqual(meeting["sources"], {"mic": {"name": "USB"}})
        self.assertEqual(meeting["retention"], "keep")
        self.assertTrue(os.path.isdir(self.store.meeting_dir(meeting["id"])))
        self.store.update_meeting(meeting["id"], state="stopped", elapsed=12.5, title="Renamed")
        listed = self.store.list_meetings()
        self.assertEqual([m["title"] for m in listed], ["Renamed"])
        self.assertEqual(listed[0]["elapsed"], 12.5)
        with self.assertRaises(ValueError):
            self.store.update_meeting(meeting["id"], state="bogus")

    def test_notes_keep_revisions_and_never_lose_content(self):
        meeting = self.store.create_meeting("", {})
        self.assertEqual(self.store.get_notes(meeting["id"])["content"], "")
        first = self.store.save_notes(meeting["id"], "hello")
        second = self.store.save_notes(meeting["id"], "hello world")
        self.assertEqual((first["rev"], second["rev"]), (1, 2))
        self.assertEqual(self.store.get_notes(meeting["id"])["content"], "hello world")

    def test_passages_corrections_survive_a_retranscription(self):
        meeting = self.store.create_meeting("", {})
        mid = meeting["id"]
        self.store.ensure_speaker(mid, "mic", "mic", "Microphone")
        self.store.ensure_speaker(mid, "system", "system", "Call audio")
        passages = [
            {"id": "p0001", "source": "mic", "speaker_id": "mic", "start": 0.0, "end": 1.5, "text": "hallo"},
            {"id": "p0002", "source": "system", "speaker_id": "system", "start": 1.5, "end": 3.0, "text": "hoi"},
        ]
        self.store.replace_passages(mid, passages, rev=1)
        corrected = self.store.correct_passage(mid, "p0001", "Hallo!")
        self.assertEqual(corrected["corrected"], "Hallo!")
        self.assertEqual(corrected["text"], "hallo")
        self.assertEqual(self.store.get_meeting(mid)["transcript_rev"], 2)
        # A correction equal to the recognition text clears the revision.
        self.assertIsNone(self.store.correct_passage(mid, "p0001", "hallo")["corrected"])
        self.store.correct_passage(mid, "p0001", "Hallo!")
        self.store.assign_passage_speaker(mid, "p0002", "mic")
        with self.assertRaises(ValueError):
            self.store.assign_passage_speaker(mid, "p0002", "nobody")
        # Re-running recognition with identical text keeps corrections and speakers.
        self.store.replace_passages(mid, passages, rev=5)
        rows = {p["id"]: p for p in self.store.list_passages(mid)}
        self.assertEqual(rows["p0001"]["corrected"], "Hallo!")
        self.assertEqual(rows["p0002"]["speaker_id"], "mic")
        # Changed recognition text drops a stale correction.
        passages[0]["text"] = "hallo daar"
        self.store.replace_passages(mid, passages, rev=6)
        self.assertIsNone(self.store.get_passage(mid, "p0001")["corrected"])

    def test_speakers_rename_and_confirm(self):
        meeting = self.store.create_meeting("", {})
        self.store.ensure_speaker(meeting["id"], "mic", "mic", "Microphone")
        renamed = self.store.rename_speaker(meeting["id"], "mic", "  You ", confirmed=True)
        self.assertEqual((renamed["name"], renamed["confirmed"]), ("You", 1))
        with self.assertRaises(ValueError):
            self.store.rename_speaker(meeting["id"], "mic", "   ")

    def test_analyses_and_jobs(self):
        meeting = self.store.create_meeting("", {})
        mid = meeting["id"]
        summary = self.store.add_analysis(mid, "summary", provider="openrouter", model="m", input_rev=1,
                                          include_notes=False, content="{}", refs=["p0001"])
        self.assertEqual(self.store.latest_analysis(mid, "summary")["id"], summary["id"])
        self.assertEqual(summary["refs"], ["p0001"])
        job = self.store.create_job(mid, "transcribe")
        self.store.update_job(job["id"], state="running", progress=0.5)
        self.assertEqual(self.store.list_jobs(mid, ("running",))[0]["progress"], 0.5)
        self.store.delete_meeting(mid)
        self.assertEqual(self.store.list_meetings(), [])
        self.assertEqual(self.store.list_jobs(mid), [])
        self.assertIsNone(self.store.get_analysis(summary["id"]))

    def test_reconcile_marks_interrupted_and_drops_missing_chunks(self):
        meeting = self.store.create_meeting("", {})
        mid = meeting["id"]
        path = os.path.join(self.directory.name, "gone.wav")
        self.store.add_chunk(mid, "mic", 0, 0, 100, path)
        kept = os.path.join(self.directory.name, "kept.wav")
        with open(kept, "wb") as handle:
            handle.write(b"RIFF")
        self.store.add_chunk(mid, "mic", 1, 100, 100, kept)
        job = self.store.create_job(mid, "transcribe")
        self.store.update_job(job["id"], state="running")
        self.assertEqual(self.store.reconcile_on_start(), [mid])
        self.assertEqual(self.store.get_meeting(mid)["state"], "interrupted")
        self.assertEqual([c["seq"] for c in self.store.list_chunks(mid, "mic")], [1])
        self.assertEqual(self.store.get_job(job["id"])["state"], "interrupted")
        self.assertEqual(self.store.list_events(mid)[-1]["kind"], "interrupted")

    def test_retention(self):
        meeting = self.store.create_meeting("", {}, "days7")
        mid = meeting["id"]
        self.assertFalse(self.store.audio_expired(self.store.get_meeting(mid)))
        self.store.update_meeting(mid, state="stopped", transcribed_at=self.now)
        self.assertFalse(self.store.audio_expired(self.store.get_meeting(mid)))
        self.now += 7 * 86400
        self.assertTrue(self.store.audio_expired(self.store.get_meeting(mid)))
        self.store.update_meeting(mid, retention="keep")
        self.assertFalse(self.store.audio_expired(self.store.get_meeting(mid)))
        self.store.update_meeting(mid, retention="after_transcription", audio_state="removed")
        self.assertFalse(self.store.audio_expired(self.store.get_meeting(mid)))


if __name__ == "__main__":
    unittest.main()
