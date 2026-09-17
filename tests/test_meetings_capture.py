import os
import tempfile
import threading
import unittest

import numpy as np

from meetings import audio
from meetings.capture import ArraySource, CaptureSession, MIC, SYSTEM
from meetings.store import MeetingStore


class AudioHelperTests(unittest.TestCase):
    def test_resample_and_mono(self):
        stereo = np.stack([np.ones(48000, dtype=np.float32), -np.ones(48000, dtype=np.float32)], axis=1)
        self.assertTrue(np.allclose(audio.to_mono_float(stereo), 0.0))
        tone = np.sin(np.linspace(0, 2 * np.pi * 440, 48000)).astype(np.float32)
        self.assertEqual(audio.resample(tone, 48000, 16000).size, 16000)
        self.assertEqual(audio.resample(tone[:44100], 44100, 16000).size, 16000)
        self.assertEqual(audio.resample(tone, 16000, 16000).size, 48000)
        self.assertEqual(audio.resample(np.zeros(0, dtype=np.float32), 44100).size, 0)

    def test_levels(self):
        self.assertEqual(audio.meter_level(np.zeros(1600, dtype=np.int16)), 0.0)
        loud = (np.sin(np.linspace(0, 200, 1600)) * 20000).astype(np.int16)
        self.assertGreater(audio.meter_level(loud), 0.9)

    def test_wav_round_trip_and_concat_with_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            samples = (np.arange(1000) % 200 * 100).astype(np.int16)
            path = os.path.join(directory, "a.wav")
            audio.write_wav(path, samples)
            data, rate = audio.read_wav(path)
            self.assertEqual(rate, 16000)
            self.assertTrue(np.array_equal(data, samples))
            self.assertFalse(os.path.exists(path + ".part"))
            other = os.path.join(directory, "b.wav")
            audio.write_wav(other, samples[:500])
            joined = audio.concat_chunks([
                {"seq": 1, "start_sample": 1500, "path": other},
                {"seq": 0, "start_sample": 0, "path": path},
            ])
            self.assertEqual(joined.size, 2000)
            self.assertTrue(np.all(joined[1000:1500] == 0))
            self.assertTrue(np.array_equal(joined[1500:], samples[:500]))

    def test_chunk_writer_commits_full_chunks_then_flushes_rest(self):
        with tempfile.TemporaryDirectory() as directory:
            commits = []
            writer = audio.ChunkWriter(directory, lambda *args: commits.append(args), chunk_samples=100)
            self.assertEqual(writer.append(np.ones(250, dtype=np.int16)), 2)
            self.assertEqual([c[:3] for c in commits], [(0, 0, 100), (1, 100, 100)])
            self.assertEqual(writer.total_samples, 250)
            self.assertTrue(writer.flush())
            self.assertEqual(commits[-1][:3], (2, 200, 50))
            self.assertFalse(writer.flush())
            for commit in commits:
                self.assertTrue(os.path.exists(commit[3]))


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class CaptureSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MeetingStore(self.directory.name)
        self.addCleanup(self.store.close)
        self.meeting = self.store.create_meeting("Test", {"mic": {}, "system": {}})
        self.clock = FakeClock()
        self.mic = ArraySource(MIC)
        self.system = ArraySource(SYSTEM)
        self.session = CaptureSession(self.store, self.meeting["id"], [self.mic, self.system],
                                      clock=self.clock, chunk_samples=16000)

    def tearDown(self):
        if self.session.state in ("recording", "paused"):
            self.session.stop()

    def block(self, seconds=0.1, value=1000):
        return np.full(int(16000 * seconds), value, dtype=np.int16)

    def test_start_pause_stop_keeps_timeline_and_pads_silent_loopback(self):
        self.session.start()
        self.assertEqual(self.session.state, "recording")
        for _ in range(20):
            self.mic.push(self.block())
        self.clock.now += 2.0
        self.session._tick(final=False)
        self.assertEqual(self.session._writers[MIC].total_samples, 32000)
        # The loopback delivered nothing for two seconds: it is padded, not lost.
        self.assertEqual(self.session._writers[SYSTEM].total_samples, 32000)
        self.assertTrue(self.session.pause())
        self.assertAlmostEqual(self.session.elapsed, 2.0)
        self.clock.now += 5.0
        self.mic.push(self.block())  # heard while paused: dropped on resume
        self.assertAlmostEqual(self.session.elapsed, 2.0)
        self.assertTrue(self.session.resume())
        self.clock.now += 1.0
        for _ in range(10):
            self.mic.push(self.block())
        summary = self.session.stop()
        self.assertEqual(summary["state"], "stopped")
        self.assertAlmostEqual(summary["elapsed"], 3.0)
        self.assertEqual(summary["sources"][MIC], 48000)
        self.assertEqual(summary["sources"][SYSTEM], 48000)
        meeting = self.store.get_meeting(self.meeting["id"])
        self.assertEqual(meeting["state"], "stopped")
        self.assertAlmostEqual(meeting["elapsed"], 3.0)
        kinds = [e["kind"] for e in self.store.list_events(self.meeting["id"])]
        self.assertEqual(kinds, ["start", "pause", "resume", "stop"])
        chunks = self.store.list_chunks(self.meeting["id"], MIC)
        self.assertEqual([c["start_sample"] for c in chunks], [0, 16000, 32000])
        self.assertTrue(np.array_equal(audio.concat_chunks(chunks)[:100], self.block()[:100]))

    def test_overrun_becomes_a_visible_gap(self):
        self.session.start()
        self.mic.dropped_samples = 8000
        self.clock.now += 0.6
        self.session._tick(final=False)
        events = self.store.list_events(self.meeting["id"])
        self.assertEqual(events[-1]["kind"], "gap")
        self.assertEqual(self.session.gaps, 1)
        self.session.stop()

    def test_dead_source_interrupts_the_meeting(self):
        interruptions = []
        interrupted = threading.Event()
        def on_interrupted(session, reason):
            interruptions.append(reason)
            interrupted.set()
        session = CaptureSession(self.store, self.meeting["id"], [self.mic], clock=self.clock,
                                 chunk_samples=16000, on_interrupted=on_interrupted)
        session.start()
        try:
            self.mic.fail("Microphone unplugged")
            # State is set before the database writes and callback complete.
            self.assertTrue(interrupted.wait(3), "Capture did not report the failed source")
            self.assertEqual(session.summary()["state"], "interrupted")
            self.assertEqual(interruptions, ["Microphone unplugged"])
            self.assertEqual(self.store.get_meeting(self.meeting["id"])["state"], "interrupted")
        finally:
            session.stop()

    def test_stop_is_idempotent_and_start_failure_cleans_up(self):
        self.session.start()
        first = self.session.stop()
        self.assertEqual(self.session.stop(), first)

        class Broken(ArraySource):
            def start(self):
                raise RuntimeError("no device")

        stopped = []
        good = ArraySource(MIC)
        good.stop = lambda: stopped.append(True)
        broken = Broken(SYSTEM)
        session = CaptureSession(self.store, self.meeting["id"], [good, broken], clock=self.clock)
        with self.assertRaises(RuntimeError):
            session.start()
        self.assertEqual(stopped, [True])
        self.assertEqual(session.state, "idle")

    def test_interruption_callback_does_not_hold_capture_lock(self):
        read = threading.Event()
        readers = []
        observed = []
        def callback(session, reason):
            def inspect():
                session.summary()
                read.set()
            reader = threading.Thread(target=inspect, daemon=True)
            readers.append(reader)
            reader.start()
            observed.append(read.wait(1))
        self.session._on_interrupted = callback
        self.session.start()
        self.session.stop('Source failed')
        for reader in readers:
            reader.join(1)
        self.assertEqual(observed, [True], 'Status polling must not deadlock the callback')


if __name__ == "__main__":
    unittest.main()
