import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import local_diarization
from local_diarization import (BATCH_BLOCK, BATCH_PRESET, LIVE_PRESET, MODEL_FILE, NEMOTRON_DIARIZATION,
                               DiarizationError, LiveBinarizer, LocalDiarizer, exclusive_segments)
from local_models import LocalModelManager, ModelError
from meetings.diarization import assign_speakers, speaker_for


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

    def test_a_word_and_its_punctuation_stay_with_one_speaker(self):
        # A turn boundary inside a word once produced "file. B" and "ack into".
        segments = [{"start": 0.0, "end": 1.05, "speaker": "SPEAKER_00"}, {"start": 1.05, "end": 3.0, "speaker": "SPEAKER_01"}]
        passages = [{"id": "p0001", "source": "system", "speaker_id": "system", "start": 0.0, "end": 3.0,
                     "text": "file. Back into", "tokens": [(" file", 0.2, 0.9), (".", 0.9, 1.0), (" B", 1.0, 1.1),
                                                           ("ack", 1.1, 1.4), (" into", 1.5, 1.9)]}]
        labelled, _ = assign_speakers(passages, segments, source="system", fallback_speaker="system")
        self.assertEqual([p["text"] for p in labelled], ["file.", "Back into"])
        self.assertEqual([p["speaker_id"] for p in labelled], ["system-1", "system-2"])

    def test_token_without_segment_keeps_previous_speaker(self):
        passages = [{"id": "p0001", "source": "mic", "speaker_id": "mic", "start": 0.0, "end": 12.0, "text": "a b c",
                     "tokens": [(" a", 0.0, 1.0), (" b", 10.0, 10.5), (" c", 10.5, 11.0)]}]
        labelled, speakers = assign_speakers(passages, SEGMENTS, source="mic", fallback_speaker="mic")
        self.assertEqual(len(labelled), 1)
        self.assertEqual(labelled[0]["speaker_id"], "mic-1")
        self.assertEqual(labelled[0]["text"], "a b c")


class ExclusiveSegmentTests(unittest.TestCase):
    def test_overlap_goes_to_the_more_probable_speaker_and_silence_stays_empty(self):
        probs = np.zeros((10, 3), dtype=np.float32)
        probs[0:6, 0] = .9
        probs[4:9, 1] = .7
        probs[4:6, 1] = .95   # the second voice dominates the overlap
        segments = [(0.0, 0.6, 0), (0.4, 0.9, 1)]
        self.assertEqual(exclusive_segments(segments, probs, .1), [
            {"start": 0.0, "end": 0.4, "speaker": "SPEAKER_00"},
            {"start": 0.4, "end": 0.9, "speaker": "SPEAKER_01"},
        ])

    def test_no_frames_or_segments_means_no_labels(self):
        self.assertEqual(exclusive_segments([], np.zeros((5, 2), np.float32), .1), [])
        self.assertEqual(exclusive_segments([(0, 1, 0)], np.zeros((0, 2), np.float32), .1), [])


def offline_segments(probs, spf, *, onset=0.641, offset=0.561, pad_onset=0.229, pad_offset=0.079,
                     min_on=0.511, min_off=0.296):
    """Port of NeMo-Speech.cpp diar_segments_from_probs, the batch reference."""
    out = []
    total = len(probs) * spf
    for s in range(probs.shape[1]):
        segs, active, start = [], False, 0
        for f, p in enumerate(probs[:, s]):
            if not active and p > onset:
                active, start = True, f
            elif active and p < offset:
                active = False
                segs.append([max(0.0, start * spf - pad_onset), min(total, f * spf + pad_offset)])
        if active:
            segs.append([max(0.0, start * spf - pad_onset), total])
        merged = []
        for seg in segs:
            if merged and seg[0] - merged[-1][1] < min_off:
                merged[-1][1] = max(merged[-1][1], seg[1])
            else:
                merged.append(seg)
        out.extend((round(a, 6), round(b, 6), s) for a, b in merged if b - a >= min_on)
    return sorted(out)


def causal_segments(events):
    open_at, out = {}, []
    for speaker, seconds, starting in events:
        if starting:
            assert speaker not in open_at, "a speaker started twice"
            open_at[speaker] = seconds
        else:
            out.append((round(open_at.pop(speaker), 6), round(seconds, 6), speaker))
    assert not open_at, "every start has an end"
    return sorted(out)


class LiveBinarizerTests(unittest.TestCase):
    def speech(self, frames, speakers, rng):
        """Bursts, short gaps, blips and overlaps on a 10 ms grid."""
        probs = np.zeros((frames, speakers), dtype=np.float32)
        for s in range(speakers):
            f = int(rng.integers(0, 80))
            while f < frames:
                length = int(rng.choice([2, 8, 25, 60, 150]))
                probs[f:f + length, s] = rng.uniform(.62, 1.0)
                f += length + int(rng.choice([1, 5, 20, 35, 120]))
        noise = rng.uniform(-.08, .08, probs.shape).astype(np.float32)
        return np.clip(probs + noise, 0, 1)

    def test_causal_events_match_the_runtime_postprocessing(self):
        rng = np.random.default_rng(7)
        for trial in range(30):
            probs = self.speech(int(rng.integers(50, 900)), 3, rng)
            with self.subTest(trial=trial):
                binarizer = LiveBinarizer(3, .01)
                events = []
                for first in range(0, len(probs), 13):  # model chunks arrive in irregular slices
                    events += binarizer.feed(probs[first:first + 13])
                events += binarizer.close()
                self.assertEqual(causal_segments(events), offline_segments(probs, .01))

    def test_no_event_arrives_for_time_that_was_already_settled(self):
        rng = np.random.default_rng(11)
        for trial in range(30):
            probs = self.speech(int(rng.integers(50, 900)), 3, rng)
            with self.subTest(trial=trial):
                binarizer, settled, step = LiveBinarizer(3, .01), 0.0, int(rng.integers(1, 40))
                for first in range(0, len(probs), step):
                    for _speaker, seconds, _starting in binarizer.feed(probs[first:first + step]):
                        self.assertGreaterEqual(seconds, settled - 1e-9)
                    self.assertGreaterEqual(binarizer.settled_seconds(), settled - 1e-9, "the horizon never moves back")
                    settled = binarizer.settled_seconds()

    def test_a_blip_is_ignored_and_a_short_gap_is_filled(self):
        probs = np.zeros((300, 1), dtype=np.float32)
        probs[10:15] = .9     # 50 ms: shorter than the minimum duration even with padding
        probs[100:160] = .9
        probs[170:230] = .9   # 100 ms gap: filled
        binarizer = LiveBinarizer(1, .01)
        events = binarizer.feed(probs) + binarizer.close()
        self.assertEqual([(s, round(t, 3), start) for s, t, start in events], [(0, .771, True), (0, 2.379, False)])

    def test_start_is_reported_before_the_speaker_stops(self):
        probs = np.zeros((100, 1), dtype=np.float32)
        probs[20:] = .9
        events = LiveBinarizer(1, .01).feed(probs)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0][2])


class FakeDiarModel:
    created = []

    def __init__(self, lib, path, preset):
        self.path, self.preset, self.closed = path, preset, False
        self.num_speakers, self.seconds_per_frame = 2, .08
        FakeDiarModel.created.append(self)

    def open_stream(self):
        return FakeDiarStream(self)

    def close(self):
        self.closed = True


class FakeDiarStream:
    def __init__(self, model):
        self.model, self.pushed, self.finished, self.closed = model, [], False, False

    def push(self, samples):
        assert samples.dtype == np.float32 and np.abs(samples).max(initial=0) <= 1
        self.pushed.append(len(samples))

    def new_probs(self):
        frames = int(self.pushed[-1] / 16000 / .08) if self.pushed and not self.finished else 0
        probs = np.zeros((frames, 2), dtype=np.float32)
        probs[:, len(self.pushed) % 2] = .9
        return probs

    def finish(self):
        self.finished = True

    def segments(self):
        seconds = sum(self.pushed) / 16000
        return [(0.0, min(seconds, 30.0), 1), (30.0, seconds, 0)]

    def close(self):
        self.closed = True


class LocalDiarizerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.models = LocalModelManager(model=NEMOTRON_DIARIZATION, directory=self.directory.name)
        FakeDiarModel.created = []
        patcher = patch.object(local_diarization, "DiarModel", FakeDiarModel)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.directory.cleanup)
        self.diarizer = LocalDiarizer(self.models, runtime=lambda: object())

    def install(self):
        with (Path(self.directory.name) / MODEL_FILE).open("wb") as target:
            target.truncate(NEMOTRON_DIARIZATION.files[0].size)

    def test_missing_model_and_runtime_are_explained(self):
        with self.assertRaisesRegex(DiarizationError, "Settings > Meetings"):
            with self.diarizer.lease(LIVE_PRESET):
                pass
        self.assertFalse(self.diarizer.info()["configured"])

        def missing():
            raise DiarizationError("The speaker label runtime is missing from this Ownkey build.")

        self.install()
        info = LocalDiarizer(self.models, runtime=missing).info()
        self.assertEqual((info["installed"], info["runtime"], info["configured"]), (True, False, False))
        self.assertIn("runtime", info["error"])
        self.assertFalse(info["remote"])

    def test_leases_share_one_model_per_preset_and_block_removal(self):
        self.install()
        with self.diarizer.lease(LIVE_PRESET) as first, self.diarizer.lease(LIVE_PRESET) as second:
            self.assertIs(first, second)
            with self.assertRaisesRegex(ModelError, "Speaker labels are using this model"):
                self.models.remove()
            with self.diarizer.lease(BATCH_PRESET) as batch:
                self.assertIsNot(batch, first)
        self.assertEqual([m.preset for m in FakeDiarModel.created], [LIVE_PRESET, BATCH_PRESET])
        self.assertTrue(all(m.closed for m in FakeDiarModel.created))
        self.models.remove()
        self.assertFalse(self.models.path.exists())

    def test_batch_pushes_bounded_blocks_reports_progress_and_returns_exclusive_labels(self):
        self.install()
        samples = np.full(BATCH_BLOCK * 2 + 16000, 12000, dtype=np.int16)
        progress = []
        segments = self.diarizer.diarize(samples, on_progress=lambda done, total: progress.append((done, total)))
        self.assertEqual(progress, [(30.0, 61.0), (60.0, 61.0), (61.0, 61.0)])
        self.assertEqual([s["speaker"] for s in segments], ["SPEAKER_01", "SPEAKER_00"])
        self.assertEqual(segments[0]["start"], 0.0)
        self.assertTrue(FakeDiarModel.created[0].closed)

    def test_batch_stops_between_blocks(self):
        self.install()
        calls = []
        result = self.diarizer.diarize(np.zeros(BATCH_BLOCK * 3, np.int16),
                                       should_stop=lambda: calls.append(1) or len(calls) > 1)
        self.assertEqual(result, [])
        self.assertEqual(len(calls), 2)


class RuntimeTests(unittest.TestCase):
    def test_missing_runtime_is_a_user_facing_error(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(DiarizationError, "runtime is missing"):
                local_diarization.load_runtime(Path(empty))

    def test_a_failed_load_is_remembered_until_restart(self):
        # Settings polls several times a second; it must not retry LoadLibrary each time.
        with tempfile.TemporaryDirectory() as empty, patch.object(local_diarization, "_runtime", None), \
                patch.object(local_diarization, "_runtime_failure", ""), \
                patch.object(local_diarization, "runtime_dir", return_value=Path(empty)):
            with self.assertRaisesRegex(DiarizationError, "missing"):
                local_diarization.load_runtime()
            with patch.object(local_diarization, "_open_library", side_effect=AssertionError("retried")):
                with self.assertRaisesRegex(DiarizationError, "missing"):
                    local_diarization.load_runtime()

    def test_a_cpu_without_avx2_never_loads_the_library(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / local_diarization.LIBRARY_NAMES["win32"]).write_bytes(b"not a library")
            (Path(directory) / local_diarization.LIBRARY_NAMES["linux"]).write_bytes(b"not a library")
            with self.assertRaisesRegex(DiarizationError, "AVX2"):
                local_diarization.load_runtime(Path(directory), cpu_check=lambda: False)

    def test_model_is_pinned_to_a_revision_and_checksum(self):
        (file,) = NEMOTRON_DIARIZATION.files
        self.assertEqual(file.name, "Nemotron-3-Diarization.q8_0.gguf")
        self.assertIn(NEMOTRON_DIARIZATION.revision, NEMOTRON_DIARIZATION.file_urls[0])
        self.assertEqual(len(file.sha256), 64)
        self.assertEqual(NEMOTRON_DIARIZATION.settings_tab, "Meetings")


if __name__ == "__main__":
    unittest.main()
