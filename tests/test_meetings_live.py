"""Synthetic fixtures only: private recordings must never enter this repository."""
import tempfile
import threading
import time
import unittest
import json
import queue
import io
import wave
from types import SimpleNamespace

import numpy as np

from meetings import audio
from meetings.capture import ArraySource, CaptureSession, MIC
from meetings.segmentation import (next_boundary, owned_tokens, turn_boundaries, window_passages,
                                    CORE_LIMIT, RATE)
from meetings.service import ConsentRequired, MeetingService
from meetings.store import MeetingStore
from meetings.streaming import StreamClock, LiveSpeakerStream


def wait_for(predicate, timeout=6):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return True
        time.sleep(.025)
    return False


def speech(seconds):
    return np.full(round(seconds * RATE), 3000, dtype=np.int16)


class SegmentationTests(unittest.TestCase):
    def test_waits_for_a_pause_and_keeps_a_short_reply(self):
        self.assertIsNone(next_boundary(speech(4)))
        signal = np.concatenate((speech(.2), np.zeros(RATE, dtype=np.int16)))
        boundary = next_boundary(signal)
        self.assertEqual(boundary.reason, 'pause')
        self.assertTrue(.2 * RATE < boundary.end < 1.2 * RATE)

    def test_short_breath_is_not_a_boundary(self):
        signal = np.concatenate((speech(4), np.zeros(int(.3 * RATE), dtype=np.int16), speech(4)))
        self.assertIsNone(next_boundary(signal))

    def test_continuous_speech_is_bounded_and_tail_is_not_lost(self):
        data = speech(90)
        position = 0
        ranges = []
        while position < len(data):
            b = next_boundary(data[position:position + 28 * RATE], final=position + 28 * RATE >= len(data))
            self.assertIsNotNone(b)
            self.assertLessEqual(b.end, CORE_LIMIT)
            ranges.append((position, position + b.end))
            position += b.end
        self.assertEqual(position, len(data))
        self.assertTrue(all(a[1] == b[0] for a, b in zip(ranges, ranges[1:])))

    def test_silence_advances_and_empty_audio_does_not(self):
        self.assertIsNone(next_boundary(np.zeros(0, dtype=np.int16), final=True))
        self.assertEqual(next_boundary(np.zeros(6 * RATE, dtype=np.int16)).reason, 'silence')

    def test_only_exclusive_speaker_changes_suggest_a_cut(self):
        segments = [{'start': 0, 'end': 3, 'speaker': 'a'}, {'start': 3.2, 'end': 5, 'speaker': 'b'}]
        self.assertEqual(turn_boundaries(segments, 0), [49600])
        segments.append({'start': 2, 'end': 4, 'speaker': 'c'})
        self.assertEqual(turn_boundaries(segments, 0), [])

    def test_subword_group_is_owned_once_and_repetitions_survive(self):
        tokens = [' hello', 'world', ' yes', ' yes']
        stamps = [0., .3, 1., 1.5]
        durations = [.3, .3, .2, .2]
        first = owned_tokens(tokens, stamps, durations, offset=0, start=0, end=.4)
        second = owned_tokens(tokens, stamps, durations, offset=0, start=.4, end=2)
        self.assertEqual([t[0] for t in first], [' hello', 'world'])
        self.assertEqual([t[0] for t in second], [' yes', ' yes'])

    def test_overlap_and_text_without_timing_keep_uncertain_source_label(self):
        segments = [{'speaker': 'a', 'start': 0, 'end': 2}, {'speaker': 'b', 'start': 0, 'end': 2}]
        for result in [('hello', [], [], []), ('hello', [' hello'], [.2], [.2])]:
            passages = window_passages(result, source='mic', offset=0, start=0, end=1, segments=segments)
            self.assertEqual(passages[0]['speaker_id'], 'mic')

    def test_speaker_event_cannot_split_a_subword_group(self):
        segments = [{'speaker': 'a', 'start': 0, 'end': .3}, {'speaker': 'b', 'start': .3, 'end': 1}]
        result = ('hello', [' hel', 'lo'], [.1, .3], [.2, .2])
        passages = window_passages(result, source='mic', offset=0, start=0, end=1, segments=segments)
        self.assertEqual(len(passages), 1)
        self.assertEqual(passages[0]['text'], 'hello')
        self.assertEqual(passages[0]['speaker_id'], 'mic')

    def test_batch_labels_preserve_live_ids_words_and_existing_assignments(self):
        from meetings.diarization import assign_speakers
        passages = [dict(id='stable-a', source='system', speaker_id='system', start=0, end=2,
                         text='Invented words.', corrected='Edited words.', tokens=[(' Invented', 0, 1), (' words.', 1, 2)]),
                    dict(id='stable-b', source='system', speaker_id='live-system-a', start=2, end=3, text='Yes.')]
        segments = [{'speaker':'A', 'start':0, 'end':3}]
        labelled, _ = assign_speakers(passages, segments, source='system', fallback_speaker='system', preserve_passages=True)
        self.assertEqual([p['id'] for p in labelled], ['stable-a', 'stable-b'])
        self.assertEqual(labelled[0]['corrected'], 'Edited words.')
        self.assertEqual(labelled[1]['speaker_id'], 'live-system-a')
        mixed = segments + [{'speaker':'B', 'start':1, 'end':2}]
        labelled, _ = assign_speakers(passages, mixed, source='system', fallback_speaker='system', preserve_passages=True)
        self.assertEqual(labelled[0]['speaker_id'], 'system')


class StreamClockTests(unittest.TestCase):
    def test_multiple_pauses_preserve_saved_audio_time(self):
        clock = StreamClock()
        clock.append(16000)
        clock.append(None)
        clock.append(None)
        clock.append(17600)
        clock.append(None)
        clock.append(19200)
        self.assertAlmostEqual(clock.at(.05), 1.05)
        self.assertIsNone(clock.at(.15))
        self.assertAlmostEqual(clock.at(.15, ending=True), 1.1)
        self.assertAlmostEqual(clock.at(.35), 1.15)
        self.assertAlmostEqual(clock.at(.55), 1.25)
        self.assertIsNone(clock.at(5))
        self.assertIsNone(clock.at(float('nan')))

    def test_transport_sends_float_frames_paced_and_drains_final_events(self):
        class Socket:
            def __init__(self):
                self.messages = queue.Queue()
                self.frames = []
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()
            def close(self):
                self.messages.put(None)
            def send(self, data):
                if isinstance(data, bytes):
                    self.frames.append((time.monotonic(), data))
                    if len(self.frames) == 1:
                        self.messages.put(json.dumps({'type':'diarization_speaker_start',
                            'data':{'timestamp':0.,'speaker':'A'}}))
                else:
                    self.messages.put(json.dumps({'type':'diarization_speaker_end',
                        'data':{'timestamp':.45,'speaker':'A'}}))
                    self.messages.put(None)
            def recv(self, timeout):
                try:
                    item = self.messages.get(timeout=timeout)
                except queue.Empty:
                    raise TimeoutError
                if item is None:
                    raise EOFError
                return item
        sock = Socket()
        http = SimpleNamespace(post=lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {'url':'wss://example.invalid/session'}))
        events, states = [], []
        stream = LiveSpeakerStream('synthetic-key', 'mic', paused=lambda:False,
            on_event=lambda *a:events.append(a), on_status=lambda *a:states.append(a),
            session=http, connect=lambda *a, **k:sock)
        stream.feed(16000, speech(.5))
        stream.finish()
        stream.start()
        stream.thread.join(4)
        self.assertFalse(stream.thread.is_alive())
        self.assertEqual(len(sock.frames), 5)
        self.assertTrue(all(len(data)==6400 for _,data in sock.frames))
        self.assertGreaterEqual(sock.frames[-1][0] - sock.frames[0][0], .38)
        self.assertEqual([e[-1] for e in events], [True,False])
        self.assertAlmostEqual(events[-1][-2], 1.45)
        self.assertEqual(states[-1][1], 'stopped')


class CapturePauseTests(unittest.TestCase):
    def test_pause_flushes_tail_and_stop_discards_paused_samples(self):
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            meeting = store.create_meeting('Synthetic', {'mic': {}})
            now = [100.]
            source = ArraySource(MIC)
            session = CaptureSession(store, meeting['id'], [source], clock=lambda: now[0])
            session.start()
            session._stop.set()
            session._thread.join(2)
            source.push(speech(1))
            now[0] += 1
            session.pause()
            self.assertEqual(sum(c['n_samples'] for c in store.list_chunks(meeting['id'])), RATE)
            source.push(np.full(1600, 12345, dtype=np.int16))
            now[0] += 2
            session.stop()
            result = audio.concat_chunks(store.list_chunks(meeting['id']))
            self.assertEqual(result.size, RATE)
            self.assertFalse(np.any(result == 12345))
            store.close()


class FakeModels:
    def files_present(self):
        return True


class FakeRecognizer:
    def __init__(self):
        self.calls = 0
        self.fail_at = None
        self.entered = threading.Event()
        self.block = None
        self.closed = 0

    def snapshot(self):
        return {'state': 'Loaded'}

    def begin_attempt(self):
        return self

    def transcribe_timed(self, wav, vocabulary=()):
        self.calls += 1
        self.entered.set()
        if self.block:
            self.block.wait(5)
        if self.calls == self.fail_at:
            raise RuntimeError('Synthetic decoder failure')
        return ('hello yes.', [' hello', ' yes', '.'], [.7, 1., 1.2], [.2, .2, .1])

    def close(self):
        self.closed += 1


class LiveServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MeetingStore(self.directory.name)
        self.cfg = {}
        self.recognizer = FakeRecognizer()
        self.sources = {}

        def sources(wants):
            self.sources = {s: ArraySource(s) for s in ('mic', 'system') if wants[s]}
            return list(self.sources.values())

        self.service = MeetingService(self.store, get_config=lambda: self.cfg, local_models=FakeModels(),
            local_transcriber=self.recognizer, source_factory=sources)
        self.addCleanup(self.service.close)

    def start(self, **options):
        return self.service.start_meeting('Synthetic', system=False, live_transcription=True, **options)

    def phrase(self):
        self.sources['mic'].push(np.concatenate((speech(4), np.zeros(RATE, dtype=np.int16))))

    def test_passages_arrive_before_stop_and_stop_keeps_ids_and_corrections(self):
        meeting = self.start()
        mid = meeting['id']
        self.phrase()
        self.assertTrue(wait_for(lambda: self.store.list_passages(mid)))
        self.assertTrue(self.service.is_capturing())
        first = self.store.list_passages(mid)[0]
        self.service.correct_passage(mid, first['id'], 'Corrected synthetic words.')
        self.service.pause()
        self.assertEqual(self.service._live[mid].status()['state'], 'paused')
        self.service.resume()
        self.assertNotEqual(self.service._live[mid].status()['state'], 'paused')
        self.sources['mic'].push(speech(2))
        self.service.stop()
        self.assertTrue(wait_for(lambda: self.store.list_jobs(mid, ('done',))))
        saved = self.store.get_passage(mid, first['id'])
        self.assertEqual(saved['corrected'], 'Corrected synthetic words.')
        self.assertEqual(self.store.processed_samples(mid, 'mic'), 7 * RATE)
        self.assertEqual(self.recognizer.calls, 2)

    def test_retry_continues_after_last_committed_window(self):
        mid = self.start()['id']
        self.phrase()
        self.assertTrue(wait_for(lambda: self.store.list_passages(mid)))
        first = self.store.list_passages(mid)[0]
        progress = self.store.processed_samples(mid, 'mic')
        self.recognizer.fail_at = 2
        self.sources['mic'].push(speech(2))
        self.service.stop()
        self.assertTrue(wait_for(lambda: self.store.list_jobs(mid, ('error',))))
        self.assertEqual(self.store.processed_samples(mid, 'mic'), progress)
        retry = self.service.transcribe(mid)
        self.assertTrue(wait_for(lambda: self.store.get_job(retry['id'])['state'] == 'done'))
        self.assertEqual(self.store.processed_samples(mid, 'mic'), 7 * RATE)
        self.assertIsNotNone(self.store.get_passage(mid, first['id']))
        self.assertEqual(self.recognizer.calls, 3)

    def test_restart_exposes_partial_transcript_and_retry_resumes_checkpoint(self):
        mid = self.start()['id']
        self.phrase()
        self.assertTrue(wait_for(lambda: self.store.list_passages(mid)))
        first = self.store.list_passages(mid)[0]
        self.service.correct_passage(mid, first['id'], 'Synthetic saved correction.')
        self.recognizer.entered.clear()
        self.recognizer.block = threading.Event()
        self.sources['mic'].push(speech(2))
        self.service.stop()
        self.assertTrue(self.recognizer.entered.wait(3))
        closer = threading.Thread(target=self.service.close)
        closer.start()
        try:
            self.assertTrue(wait_for(lambda: self.service._closed))
        finally:
            self.recognizer.block.set()
            closer.join(4)
        self.assertFalse(closer.is_alive())
        recovered = MeetingStore(self.directory.name)
        restarted = MeetingService(recovered, get_config=lambda: {}, local_models=FakeModels(),
                                   local_transcriber=FakeRecognizer())
        try:
            detail = restarted.meeting_detail(mid)
            self.assertEqual(detail['jobs'][-1]['state'], 'interrupted')
            self.assertEqual(detail['meeting']['state'], 'stopped')
            retry = restarted.transcribe(mid)
            self.assertTrue(wait_for(lambda: recovered.get_job(retry['id'])['state'] == 'done'))
            self.assertEqual(recovered.processed_samples(mid, 'mic'), 7 * RATE)
            self.assertEqual(recovered.get_passage(mid, first['id'])['corrected'], 'Synthetic saved correction.')
            self.assertEqual(len(recovered.list_passages(mid)), 2)
        finally:
            restarted.close()

    def test_cloud_and_speaker_permissions_are_separate_and_precede_capture(self):
        self.cfg.update(meetings_audio_provider='mistral', meetings_audio_endpoint='https://example.invalid/audio',
                        meetings_audio_model='test', meetings_audio_api_key='test', pyannote_api_key='test')
        with self.assertRaises(ConsentRequired) as ask:
            self.start(mic_shared=True, live_speakers=True)
        self.assertEqual(ask.exception.disclosure['policy_key'], 'live_transcription')
        self.assertEqual(self.store.list_meetings(), [])
        self.assertEqual(self.sources, {})
        with self.assertRaises(ConsentRequired) as ask:
            self.start(mic_shared=True, live_speakers=True, live_transcription_ok=True)
        self.assertEqual(ask.exception.disclosure['policy_key'], 'live_speakers')
        self.assertEqual(self.store.list_meetings(), [])

    def test_delete_while_decoder_runs_cannot_recreate_passages(self):
        self.recognizer.block = threading.Event()
        mid = self.start()['id']
        self.phrase()
        self.assertTrue(self.recognizer.entered.wait(4))
        live = self.service._live[mid]
        self.service.delete_meeting(mid)
        self.recognizer.block.set()
        live.join(3)
        self.assertIsNone(self.store.get_meeting(mid))
        self.assertEqual(self.store.list_passages(mid), [])

    def test_cloud_live_uses_disjoint_windows_and_keeps_audio_until_completion(self):
        self.cfg.update(meetings_audio_provider='mistral', meetings_audio_endpoint='https://example.invalid/audio',
                        meetings_audio_model='test', meetings_audio_api_key='test')
        durations = []
        def decode(engine, key, wav, language, vocabulary):
            with wave.open(io.BytesIO(wav)) as handle:
                durations.append(handle.getnframes() / handle.getframerate())
            return 'Invented cloud phrase.'
        self.service._cloud_transcriber = decode
        mid = self.start(live_transcription_ok=True, retention='after_transcription')['id']
        self.phrase()
        self.assertTrue(wait_for(lambda: self.store.list_passages(mid)))
        self.assertEqual(self.store.get_meeting(mid)['audio_state'], 'kept')
        self.sources['mic'].push(speech(2))
        self.service.stop()
        self.assertTrue(wait_for(lambda: self.store.get_meeting(mid)['audio_state'] == 'removed'))
        self.assertAlmostEqual(sum(durations), 7.)
        self.assertTrue(all(p['quality']=='window' for p in self.store.list_passages(mid)))

    def test_window_commit_is_atomic_and_duplicate_range_does_not_replace_edits(self):
        mid = self.store.create_meeting('Synthetic', {'mic': {}})['id']
        job = self.store.create_job(mid, 'transcribe')
        self.store.update_job(job['id'], state='running')
        p = {'source': 'mic', 'speaker_id': 'mic', 'start': 0, 'end': 1, 'text': 'Synthetic.'}
        self.assertTrue(self.store.commit_window(mid, job['id'], 'mic', 0, RATE, 'pause', [p]))
        pid = self.store.list_passages(mid)[0]['id']
        self.store.correct_passage(mid, pid, 'Edited.')
        self.assertFalse(self.store.commit_window(mid, job['id'], 'mic', 0, RATE, 'pause', [p]))
        self.assertEqual(self.store.get_passage(mid, pid)['corrected'], 'Edited.')

    def test_pause_resume_stop_keep_the_polling_http_connection_usable(self):
        from http.client import HTTPConnection
        from meetings.server import MeetingServer
        mid = self.start()['id']
        server = MeetingServer(self.service)
        server.start()
        connection = HTTPConnection('127.0.0.1', server.port, timeout=4)
        try:
            connection.request('GET', f'/?token={server.token}', headers={'Cookie':'ownkey_meetings=old-process'})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            for action in ('pause', 'resume', 'stop'):
                connection.request('POST', f'/api/meetings/{mid}/{action}', '{}',
                    {'X-Ownkey-Token':server.token, 'Content-Type':'application/json'})
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
                connection.request('GET', f'/api/meetings/{mid}/live', headers={'X-Ownkey-Token':server.token})
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
        finally:
            connection.close()
            server.stop()


if __name__ == '__main__':
    unittest.main()
