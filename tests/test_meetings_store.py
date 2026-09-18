import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from meetings.store import MeetingStore, default_library_root


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

    def test_job_result_rolls_back_nested_writes_and_child_jobs_together(self):
        mid = self.store.create_meeting('Synthetic result transaction', {})['id']
        job = self.store.create_job(mid, 'transcribe')
        self.store.update_job(job['id'], state='running')
        with self.assertRaisesRegex(RuntimeError, 'Synthetic interruption'):
            with self.store.job_result(job['id']) as active:
                self.assertTrue(active)
                self.store.replace_passages(mid, [{'id': 'p0001', 'source': 'mic', 'speaker_id': 'mic',
                                                  'start': 0, 'end': 1, 'text': 'Synthetic result.'}])
                self.store.update_meeting(mid, transcribed_at=123)
                self.store.add_event(mid, 1, 'transcribed', 'Synthetic completion')
                self.store.create_job(mid, 'summary')
                raise RuntimeError('Synthetic interruption')
        self.assertEqual(self.store.list_passages(mid), [])
        self.assertFalse(self.store.get_meeting(mid)['transcribed_at'])
        self.assertEqual(self.store.list_events(mid), [])
        self.assertEqual(len(self.store.list_jobs(mid)), 1)

    def test_cancellation_serializes_with_result_and_cannot_be_revived(self):
        mid = self.store.create_meeting('Synthetic cancellation transaction', {})['id']
        job = self.store.create_job(mid, 'transcribe')
        self.store.update_job(job['id'], state='running')
        started, cancelled = threading.Event(), threading.Event()
        def cancel():
            started.set()
            self.store.update_job(job['id'], state='cancelled')
            cancelled.set()
        thread = threading.Thread(target=cancel)
        with self.store.job_result(job['id']) as active:
            self.assertTrue(active)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(cancelled.wait(.05))
            self.store.add_event(mid, 1, 'transcribed', 'Synthetic completed before cancellation')
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(cancelled.is_set())
        with self.store.job_result(job['id']) as active:
            self.assertFalse(active)
        for state in ('running', 'done', 'error'):
            self.store.update_job(job['id'], state=state)
            self.assertEqual(self.store.get_job(job['id'])['state'], 'cancelled')

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

    def test_replacement_allocates_a_revision_after_concurrent_edits(self):
        mid = self.store.create_meeting('Synthetic revision test', {})['id']
        passages = [{'id': 'p0001', 'source': 'mic', 'speaker_id': 'mic',
                     'start': 0, 'end': 1, 'text': 'Synthetic passage.'}]
        self.store.replace_passages(mid, passages, 1)
        requested_rev = self.store.get_meeting(mid)['transcript_rev'] + 1
        self.store.bump_transcript_rev(mid)
        edited_rev = self.store.bump_transcript_rev(mid)
        self.store.replace_passages(mid, passages, requested_rev)
        self.assertEqual(self.store.get_meeting(mid)['transcript_rev'], edited_rev + 1)
        self.assertEqual(self.store.list_passages(mid)[0]['rev'], edited_rev + 1)
        self.store.replace_passages(mid, passages)
        self.assertEqual(self.store.get_meeting(mid)['transcript_rev'], edited_rev + 2)

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

    def test_deleted_meeting_rejects_late_writes(self):
        mid = self.store.create_meeting('Synthetic deletion test', {})['id']
        job = self.store.create_job(mid, 'transcribe')
        self.store.delete_meeting(mid)
        passages = [{'id': 'p0001', 'source': 'mic', 'speaker_id': 'mic',
                     'start': 0., 'end': 1., 'text': 'Synthetic delayed response.'}]
        self.store.append_passages(mid, passages, 1)
        self.store.replace_passages(mid, passages, 1)
        self.store.add_event(mid, 1., 'transcribed')
        self.store.add_chunk(mid, 'mic', 0, 0, 16000, 'unused.wav')
        self.store.set_live_options(mid, {})
        self.store.ensure_speaker(mid, 'mic', 'mic', 'Microphone')
        self.store.speaker_turn(mid, 'mic', 'mic', 0., True)
        self.assertFalse(self.store.commit_window(mid, job['id'], 'mic', 0, 16000, 'stop', passages))
        with self.assertRaises(ValueError):
            self.store.save_notes(mid, 'Delayed edit')
        with self.assertRaises(ValueError):
            self.store.add_analysis(mid, 'answer', provider='test', model='test', input_rev=1,
                                    include_notes=False, content='Delayed answer', refs=[])
        with self.assertRaises(ValueError):
            self.store.create_job(mid, 'summary')
        for table in ('chunks', 'events', 'speakers', 'passages', 'notes', 'analyses', 'jobs',
                      'live_sessions', 'transcription_windows', 'speaker_turns'):
            with self.subTest(table=table):
                self.assertEqual(self.store._conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

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

    def test_recovered_interrupted_audio_expires_only_after_successful_transcription(self):
        for policy in ('after_transcription', 'days7', 'keep'):
            with self.subTest(policy=policy):
                mid = self.store.create_meeting('Synthetic recovery', {}, policy)['id']
                self.store.update_meeting(mid, state='interrupted')
                self.assertFalse(self.store.audio_expired(self.store.get_meeting(mid)))
                self.store.update_meeting(mid, transcribed_at=self.now)
                self.assertEqual(self.store.audio_expired(self.store.get_meeting(mid)), policy == 'after_transcription')
                self.assertEqual(self.store.audio_expired(self.store.get_meeting(mid), now=self.now + 7 * 86400), policy != 'keep')


class LibraryRootTests(unittest.TestCase):
    def test_environment_override_wins_over_localappdata(self):
        local = os.path.join(os.sep, 'Users', 'x', 'AppData', 'Local')
        with patch.dict(os.environ, {'OWNKEY_MEETINGS_LIBRARY': '', 'LOCALAPPDATA': local}):
            self.assertEqual(default_library_root(), Path(local) / 'Ownkey' / 'meetings')
        with patch.dict(os.environ, {'OWNKEY_MEETINGS_LIBRARY': os.path.join(os.sep, 'lib')}):
            self.assertEqual(default_library_root(), Path(os.sep) / 'lib')


if __name__ == "__main__":
    unittest.main()
