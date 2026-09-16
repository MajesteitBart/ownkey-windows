import unittest

from meetings import diarization
from meetings.diarization import DiarizationError, PyannoteClient, assign_speakers, segments_from_output, speaker_for


class Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Scripted HTTP session: records calls, answers from a queue per method."""

    def __init__(self, job_statuses=("running", "succeeded")):
        self.calls = []
        self.job_statuses = list(job_statuses)
        self.output = {"diarization": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}],
                       "exclusiveDiarization": [{"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00"},
                                                {"start": 4.0, "end": 9.0, "speaker": "SPEAKER_01"}]}

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url, headers, json))
        if url.endswith("/v1/media/input"):
            return Response(200, {"url": "https://upload.example/presigned"})
        if url.endswith("/v1/diarize"):
            return Response(200, {"jobId": "job-1", "status": "created"})
        return Response(404, {"message": "no"})

    def put(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("PUT", url, headers, len(data)))
        return Response(200)

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers, None))
        if url.endswith("/v1/test"):
            return Response(200, {"status": "ok"})
        status = self.job_statuses.pop(0) if self.job_statuses else "succeeded"
        payload = {"jobId": "job-1", "status": status}
        if status == "succeeded":
            payload["output"] = self.output
        if status == "failed":
            payload["message"] = "audio too short"
        return Response(200, payload)


class ClientTests(unittest.TestCase):
    def test_upload_diarize_and_poll(self):
        session = FakeSession()
        sleeps = []
        client = PyannoteClient("key", session=session, sleep=sleeps.append)
        statuses = []
        segments = client.diarize_wav(b"RIFF....", "m-1 system.wav", on_status=statuses.append)
        self.assertEqual([c[0] for c in session.calls], ["POST", "PUT", "POST", "GET", "GET"])
        declare = session.calls[0]
        self.assertEqual(declare[2]["Authorization"], "Bearer key")
        self.assertTrue(declare[3]["url"].startswith("media://ownkey/"))
        self.assertNotIn(" ", declare[3]["url"])
        self.assertEqual(session.calls[1][2]["Content-Type"], "application/octet-stream")
        submit = session.calls[2][3]
        self.assertEqual(submit["url"], declare[3]["url"])
        self.assertEqual(submit["model"], "precision-2")
        self.assertTrue(submit["exclusive"])
        self.assertEqual(statuses, ["running", "succeeded"])
        self.assertEqual(sleeps, [diarization.POLL_SECONDS])
        self.assertEqual([s["speaker"] for s in segments], ["SPEAKER_00", "SPEAKER_01"], "exclusive segments win")

    def test_failed_job_and_bad_key_are_user_facing_errors(self):
        client = PyannoteClient("key", session=FakeSession(job_statuses=("failed",)), sleep=lambda s: None)
        with self.assertRaises(DiarizationError) as failed:
            client.diarize_wav(b"RIFF", "x.wav")
        self.assertIn("audio too short", str(failed.exception))

        class Unauthorized(FakeSession):
            def post(self, url, headers=None, json=None, timeout=None):
                return Response(401, {"message": "nope"})

        with self.assertRaises(DiarizationError) as denied:
            PyannoteClient("bad", session=Unauthorized()).upload(b"RIFF", "x.wav")
        self.assertIn("rejected the key", str(denied.exception))
        with self.assertRaises(DiarizationError):
            PyannoteClient("   ")

    def test_cancellation_stops_polling(self):
        client = PyannoteClient("key", session=FakeSession(job_statuses=("running", "running")), sleep=lambda s: None)
        with self.assertRaises(DiarizationError):
            client.wait("job-1", should_stop=lambda: True)

    def test_segments_from_output_drops_bad_items(self):
        segments = segments_from_output({"diarization": [{"start": 2, "end": 1, "speaker": "A"}, {"start": "x"}, {"start": 1, "end": 2, "speaker": "B"}]})
        self.assertEqual(segments, [{"start": 1.0, "end": 2.0, "speaker": "B"}])


SEGMENTS = [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_01"}, {"start": 2.0, "end": 4.0, "speaker": "SPEAKER_00"},
            {"start": 6.0, "end": 8.0, "speaker": "SPEAKER_01"}]


class AlignmentTests(unittest.TestCase):
    def test_speaker_for_overlap_nearest_and_fallback(self):
        self.assertEqual(speaker_for(0.5, 1.5, SEGMENTS), "SPEAKER_01")
        self.assertEqual(speaker_for(1.5, 2.5, SEGMENTS), "SPEAKER_01")  # equal overlap: the earlier segment keeps the token
        self.assertEqual(speaker_for(1.8, 2.8, SEGMENTS), "SPEAKER_00")
        self.assertEqual(speaker_for(4.2, 4.6, SEGMENTS), "SPEAKER_00")  # nearest within a second
        self.assertEqual(speaker_for(4.9, 5.1, SEGMENTS), "SPEAKER_00")
        self.assertIsNone(speaker_for(20.0, 21.0, SEGMENTS))
        self.assertEqual(speaker_for(20.0, 21.0, SEGMENTS, fallback="X"), "X")

    def test_passages_split_at_speaker_changes_and_speakers_are_numbered(self):
        passages = [
            {"id": "p0001", "source": "system", "speaker_id": "system", "start": 0.0, "end": 4.0, "text": "hallo daar hoi terug",
             "quality": "timed", "tokens": [(" hallo", 0.0, 0.5), (" daar", 0.5, 1.0), (" hoi", 2.1, 2.6), (" terug", 2.6, 3.0)]},
            {"id": "p0002", "source": "system", "speaker_id": "system", "start": 6.0, "end": 8.0, "text": "tot morgen",
             "quality": "timed", "tokens": [(" tot", 6.1, 6.5), (" morgen", 6.5, 7.0)]},
            {"id": "p0003", "source": "system", "speaker_id": "system", "start": 30.0, "end": 31.0, "text": "niemand", "tokens": None},
        ]
        labelled, speakers = assign_speakers(passages, SEGMENTS, source="system", fallback_speaker="system")
        self.assertEqual([(p["speaker_id"], p["text"]) for p in labelled],
                         [("system-1", "hallo daar"), ("system-2", "hoi terug"), ("system-1", "tot morgen"), ("system", "niemand")])
        self.assertEqual(labelled[0]["start"], 0.0)
        self.assertEqual(labelled[1]["start"], 2.1)
        self.assertEqual(labelled[1]["end"], 3.0)
        self.assertTrue(all(p["speaker_labelled"] for p in labelled))
        self.assertEqual(speakers, [{"id": "system-1", "name": "Speaker 1", "label": "SPEAKER_01"},
                                    {"id": "system-2", "name": "Speaker 2", "label": "SPEAKER_00"}])

    def test_numbering_continues_across_tracks(self):
        passages = [{"id": "p0001", "source": "mic", "speaker_id": "mic", "start": 0.0, "end": 1.0, "text": "a", "tokens": [(" a", 0.0, 1.0)]}]
        _labelled, speakers = assign_speakers(passages, SEGMENTS, source="mic", fallback_speaker="mic", first_number=3)
        self.assertEqual(speakers, [{"id": "mic-1", "name": "Speaker 3", "label": "SPEAKER_01"}])

    def test_token_without_segment_keeps_previous_speaker(self):
        passages = [{"id": "p0001", "source": "mic", "speaker_id": "mic", "start": 0.0, "end": 12.0, "text": "a b c",
                     "tokens": [(" a", 0.0, 1.0), (" b", 10.0, 10.5), (" c", 10.5, 11.0)]}]
        labelled, speakers = assign_speakers(passages, SEGMENTS, source="mic", fallback_speaker="mic")
        self.assertEqual(len(labelled), 1)
        self.assertEqual(labelled[0]["speaker_id"], "mic-1")
        self.assertEqual(labelled[0]["text"], "a b c")


if __name__ == "__main__":
    unittest.main()
