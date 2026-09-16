import unittest

import numpy as np

from meetings import audio
from meetings.transcription import (format_time, merge_tracks, tokens_to_passages, transcribe_track,
                                    window_bounds)


class WindowTests(unittest.TestCase):
    def test_windows_end_at_the_first_pause_after_six_seconds(self):
        rate = 16000
        noise = (np.random.default_rng(1).normal(0, 3000, rate * 60)).astype(np.int16)
        noise[rate * 3: rate * 4] = 0   # too early: ignored
        noise[rate * 9: rate * 10] = 0  # first pause after six seconds
        noise[rate * 40: rate * 41] = 0
        bounds = window_bounds(noise, rate)
        self.assertEqual(bounds[0][0], 0)
        self.assertEqual(bounds[-1][1], noise.size)
        for (a, b), (c, d) in zip(bounds, bounds[1:]):
            self.assertEqual(b, c)
        self.assertTrue(rate * 9 <= bounds[0][1] <= rate * 10)
        # no pause between 10 s and 38 s: the second window is forced at the quietest frame before 38 s
        self.assertTrue(rate * 22 <= bounds[1][1] <= rate * 38)
        self.assertEqual(window_bounds(np.zeros(0, dtype=np.int16)), [])

    def test_windows_never_exceed_the_limit_without_pauses(self):
        rate = 16000
        noise = (np.random.default_rng(2).normal(0, 3000, rate * 90)).astype(np.int16)
        bounds = window_bounds(noise, rate)
        self.assertTrue(all(b - a <= rate * 28 for a, b in bounds))
        self.assertTrue(all(b - a >= rate * 12 for a, b in bounds[:-1]))

    def test_short_track_is_one_window(self):
        self.assertEqual(window_bounds(np.ones(16000 * 5, dtype=np.int16)), [(0, 16000 * 5)])


class PassageTests(unittest.TestCase):
    def test_tokens_split_at_pauses_and_sentence_ends(self):
        tokens = [" Hallo", " daar", ".", " Hoe", " gaat", " het", "?", " Goed", "."]
        stamps = [0.0, 0.3, 0.5, 1.2, 1.4, 1.6, 1.8, 4.0, 4.3]
        durations = [0.2] * len(tokens)
        passages = tokens_to_passages(tokens, stamps, durations, offset=10.0, window_end=40.0)
        self.assertEqual([p["text"] for p in passages], ["Hallo daar.", "Hoe gaat het?", "Goed."])
        self.assertEqual(passages[0]["start"], 10.0)
        self.assertEqual(passages[1]["start"], 11.2)
        self.assertEqual(passages[2]["start"], 14.0)
        self.assertLessEqual(passages[-1]["end"], 40.0)

    def test_transcribe_track_numbers_passages_and_reports_progress(self):
        rate = 16000
        track = np.zeros(rate * 40, dtype=np.int16)
        track[rate * 2: rate * 30] = 4000  # speech-like energy in the first window only
        calls, progress = [], []

        def decode(wav):
            calls.append(len(wav))
            return "one two", [" one", " two"], [0.5, 1.0], [0.3, 0.3]

        passages = transcribe_track(track, decode, source="mic", first_index=3,
                                    on_progress=lambda done, total, new: progress.append((round(done), round(total), len(new))))
        self.assertEqual([p["id"] for p in passages], ["p0003", "p0004"])
        self.assertEqual(passages[0]["speaker_id"], "mic")
        self.assertEqual(passages[0]["start"], 0.5)
        self.assertEqual(len(calls), 2, "the silent tail window must still be decoded when it has energy")
        self.assertEqual(progress[-1][1], 40)

    def test_silent_windows_are_skipped_without_decoding(self):
        track = np.zeros(16000 * 20, dtype=np.int16)
        decode = lambda wav: (_ for _ in ()).throw(AssertionError("must not decode silence"))
        self.assertEqual(transcribe_track(track, decode, source="system"), [])

    def test_merge_renumbers_in_time_order(self):
        merged = merge_tracks({
            "mic": [{"id": "p0001", "source": "mic", "start": 5.0, "end": 6.0, "text": "b"}],
            "system": [{"id": "p0001", "source": "system", "start": 1.0, "end": 2.0, "text": "a"}],
        })
        self.assertEqual([(p["id"], p["text"]) for p in merged], [("p0001", "a"), ("p0002", "b")])

    def test_format_time(self):
        self.assertEqual(format_time(65.7), "01:05")
        self.assertEqual(format_time(3725), "1:02:05")
        self.assertEqual(format_time(-3), "00:00")


if __name__ == "__main__":
    unittest.main()
