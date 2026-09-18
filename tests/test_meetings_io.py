"""Synthetic regressions for provider consent, playback and disk deletion."""

import json
import tempfile
import threading
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from meetings import audio
from meetings.service import MeetingError, MeetingService
from meetings.store import MeetingStore


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ProviderSnapshotTests(unittest.TestCase):
    def test_queued_jobs_keep_approved_provider_model_key_and_vocabulary(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {'meetings_audio_provider': 'mistral', 'meetings_audio_model': 'approved-audio',
                   'meetings_audio_endpoint': 'https://approved.invalid/audio',
                   'meetings_audio_api_key': 'synthetic-audio-secret',
                   'rewrite_provider': 'openrouter', 'rewrite_model': 'approved-text',
                   'rewrite_endpoint': 'https://approved.invalid/text', 'rewrite_api_key': 'synthetic-text-secret',
                   'pyannote_api_key': 'synthetic-speaker-secret', 'vocabulary': ['SyntheticApproved']}
            calls = []
            def cloud(engine, key, wav, language, vocabulary):
                calls.append(('audio', engine, key, vocabulary))
                return 'Synthetic approved transcript.'
            def chat(snapshot, system, user, tokens):
                calls.append(('text', snapshot))
                return json.dumps({'overview': 'Synthetic summary.', 'decisions': [], 'actions': [], 'questions': []})
            class Diarizer:
                def diarize_wav(self, *args, **kwargs):
                    return []
            def diarizer(snapshot):
                calls.append(('speakers', snapshot))
                return Diarizer()
            store = MeetingStore(directory)
            service = MeetingService(store, get_config=lambda: cfg, cloud_transcriber=cloud,
                                     chat=chat, diarizer_factory=diarizer)
            started, release = threading.Event(), threading.Event()
            original = service._run_draft
            blocker = store.create_job(store.create_meeting('Synthetic queue blocker', {})['id'], 'draft')
            def block(job):
                if job['id'] == blocker['id']:
                    started.set()
                    if not release.wait(5):
                        raise RuntimeError('Test did not release queue')
                    return True
                return original(job)
            service._run_draft = block
            try:
                service._enqueue(blocker, service.config())
                self.assertTrue(started.wait(3))
                queued = []
                for kind in ('transcribe', 'summarize', 'draft', 'label_speakers'):
                    mid = store.create_meeting('Synthetic queued ' + kind, {'mic': {}})['id']
                    store.update_meeting(mid, state='stopped')
                    path = store.audio_dir(mid, 'mic') / 'test.wav'
                    audio.write_wav(path, np.full(16000, 3000, dtype=np.int16))
                    store.add_chunk(mid, 'mic', 0, 0, 16000, str(path))
                    store.replace_passages(mid, [{'id': 'p0001', 'source': 'mic', 'speaker_id': 'mic',
                                                 'start': 0, 'end': 1, 'text': 'Synthetic source.'}])
                    queued.append(getattr(service, kind)(mid, remote_ok=True))
                cfg.update(meetings_audio_provider='openai', meetings_audio_model='unapproved-audio',
                           meetings_audio_endpoint='https://unapproved.invalid/audio', meetings_audio_api_key='changed',
                           rewrite_provider='openai', rewrite_model='unapproved-text',
                           rewrite_endpoint='https://unapproved.invalid/text', rewrite_api_key='changed',
                           pyannote_api_key='changed')
                cfg['vocabulary'].append('Unapproved')
                release.set()
                self.assertTrue(wait_for(lambda: all(store.get_job(j['id'])['state'] in ('done', 'error') for j in queued)))
                self.assertEqual([store.get_job(j['id'])['state'] for j in queued], ['done'] * 4)
                self.assertEqual({c[0] for c in calls}, {'audio', 'text', 'speakers'})
                for call in calls:
                    if call[0] == 'audio':
                        self.assertEqual(call[1]['provider'], 'mistral')
                        self.assertEqual(call[1]['endpoint'], 'https://approved.invalid/audio')
                        self.assertEqual(call[1]['model'], 'approved-audio')
                        self.assertEqual(call[2:], ('synthetic-audio-secret', ['SyntheticApproved']))
                    else:
                        self.assertEqual(call[1]['rewrite_provider'], 'openrouter')
                        self.assertEqual(call[1]['rewrite_model'], 'approved-text')
                        self.assertEqual(call[1]['rewrite_api_key'], 'synthetic-text-secret')
                        self.assertEqual(call[1]['pyannote_api_key'], 'synthetic-speaker-secret')
                self.assertNotIn('-secret', json.dumps(store.list_jobs()))
            finally:
                release.set()
                service.close()


class ChunkedPlaybackTests(unittest.TestCase):
    def test_ranges_match_wav_with_gaps_and_odd_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            chunks = []
            for seq, start in enumerate((0, 10003)):
                path = Path(directory) / f'{seq}.wav'
                audio.write_wav(path, np.arange(9000, dtype=np.int16))
                chunks.append(dict(seq=seq, start_sample=start, n_samples=9000, path=str(path)))
            expected = audio.wav_bytes(audio.concat_chunks(chunks))
            stream = audio.ChunkedWav(chunks)
            self.assertEqual(stream.size, len(expected))
            for start, end in ((0, 3), (0, len(expected)-1), (41, 48), (47, 99),
                               (18040, 20058), (len(expected)-7, len(expected)-1)):
                with self.subTest(start=start, end=end):
                    self.assertEqual(b''.join(stream.iter_range(start, end)), expected[start:end+1])

    def test_two_hour_playback_and_seek_have_bounded_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tail.wav'
            samples = np.arange(16000, dtype=np.int16)
            audio.write_wav(path, samples)
            end_sample = 2 * 60 * 60 * audio.SAMPLE_RATE
            stream = audio.ChunkedWav([dict(seq=0, start_sample=end_sample-16000,
                                          n_samples=16000, path=str(path))])
            tracemalloc.start()
            try:
                with patch('meetings.audio.concat_chunks', side_effect=AssertionError('Whole track allocation')):
                    total, largest = 0, 0
                    for block in stream.iter_range():
                        total += len(block)
                        largest = max(largest, len(block))
                    tail = b''.join(stream.iter_range(stream.size-7))
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertEqual(total, 44 + 2 * end_sample)
            self.assertLessEqual(largest, stream.BLOCK_BYTES)
            self.assertLess(peak, 2 * 1024 * 1024)
            self.assertEqual(tail, samples.tobytes()[-7:])


class DeletionTests(unittest.TestCase):
    def test_failed_delete_allows_new_jobs_without_reviving_cancelled_provider_calls(self):
        for operation in ('transcribe', 'summarize', 'draft', 'label_speakers'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                started, release = threading.Event(), threading.Event()
                calls = []
                def provider(*args, **kwargs):
                    calls.append(True)
                    if len(calls) == 1:
                        started.set()
                        if not release.wait(5):
                            raise RuntimeError('Test did not release cancelled provider')
                        return [] if operation == 'label_speakers' else 'Cancelled output must not be saved.'
                    if operation == 'label_speakers':
                        return []
                    return json.dumps({'overview': 'Synthetic new output.', 'decisions': [], 'actions': [], 'questions': []})
                class Diarizer:
                    diarize_wav = staticmethod(provider)
                store = MeetingStore(directory)
                service = MeetingService(store, get_config=lambda: {
                    'meetings_audio_provider': 'mistral', 'meetings_audio_model': 'synthetic',
                    'meetings_audio_endpoint': 'https://example.invalid/audio', 'meetings_audio_api_key': 'synthetic',
                    'rewrite_provider': 'openrouter', 'rewrite_endpoint': 'https://example.invalid/text',
                    'rewrite_model': 'synthetic', 'rewrite_api_key': 'synthetic', 'pyannote_api_key': 'synthetic'},
                    cloud_transcriber=provider, chat=provider, diarizer_factory=lambda cfg: Diarizer())
                try:
                    mid = store.create_meeting('Synthetic cancelled job test', {'mic': {}})['id']
                    store.update_meeting(mid, state='stopped')
                    path = store.audio_dir(mid, 'mic') / 'locked.wav'
                    audio.write_wav(path, np.full(16000, 3000, dtype=np.int16))
                    store.add_chunk(mid, 'mic', 0, 0, 16000, str(path))
                    store.replace_passages(mid, [{'id': 'p0001', 'source': 'mic', 'speaker_id': 'mic',
                                                 'start': 0, 'end': 1, 'text': 'Synthetic saved source.'}])
                    old = getattr(service, operation)(mid, remote_ok=True)
                    self.assertTrue(started.wait(3))
                    with patch('meetings.service.shutil.rmtree', side_effect=PermissionError('Synthetic file lock')):
                        with self.assertRaises(MeetingError):
                            service.delete_meeting(mid)
                    self.assertEqual(store.get_job(old['id'])['state'], 'cancelled')
                    fresh = getattr(service, operation)(mid, remote_ok=True)
                    self.assertNotEqual(old['id'], fresh['id'])
                    release.set()
                    self.assertTrue(wait_for(lambda: store.get_job(fresh['id'])['state'] in ('done', 'error', 'cancelled')))
                    self.assertEqual(store.get_job(old['id'])['state'], 'cancelled')
                    self.assertEqual(store.get_job(fresh['id'])['state'], 'done')
                    self.assertGreaterEqual(len(calls), 2)
                    self.assertNotIn('Cancelled output', json.dumps(store.list_passages(mid) + store.list_analyses(mid)))
                finally:
                    release.set()
                    service.close()

    def test_failed_disk_deletion_keeps_metadata_and_can_be_retried(self):
        for operation in ('audio', 'meeting', 'retention'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                store = MeetingStore(directory)
                service = MeetingService(store, get_config=lambda: {})
                try:
                    mid = store.create_meeting('Synthetic deletion test', {'mic': {}}, retention='after_transcription')['id']
                    store.update_meeting(mid, state='stopped', transcribed_at=1)
                    path = store.audio_dir(mid, 'mic') / 'locked.wav'
                    audio.write_wav(path, np.zeros(160, dtype=np.int16))
                    store.add_chunk(mid, 'mic', 0, 0, 160, str(path))
                    action = (lambda: service.delete_meeting(mid)) if operation == 'meeting' else (
                        service.sweep_retention if operation == 'retention' else lambda: service.remove_audio(mid))
                    with patch('meetings.service.shutil.rmtree', side_effect=PermissionError('Synthetic locked file')):
                        if operation == 'retention':
                            self.assertEqual(action(), [])
                        else:
                            with self.assertRaisesRegex(MeetingError, 'retry deletion'):
                                action()
                    self.assertTrue(path.exists())
                    self.assertEqual(store.get_meeting(mid)['audio_state'], 'kept')
                    self.assertEqual(len(store.list_chunks(mid)), 1)
                    self.assertFalse(any(e['kind'] == 'audio_removed' for e in store.list_events(mid)))
                    action()
                    self.assertFalse(path.exists())
                    self.assertEqual(store.list_chunks(mid), [])
                    if operation == 'meeting':
                        self.assertIsNone(store.get_meeting(mid))
                    else:
                        self.assertEqual(store.get_meeting(mid)['audio_state'], 'removed')
                finally:
                    service.close()


if __name__ == '__main__':
    unittest.main()
