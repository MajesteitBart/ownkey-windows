from concurrent.futures import ThreadPoolExecutor
import io
import tempfile
import threading
from types import SimpleNamespace
import unittest
import wave
from unittest.mock import Mock, patch

from local_models import LocalModelManager, ModelError
from local_transcription import LocalTranscriber, wav_samples
from test_local_models import fixture


def wav(rate=16000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        target.writeframes(b"\x00\x80\xff\x7f" * 400)
    return output.getvalue()


class Recognizer:
    def create_stream(self):
        return SimpleNamespace(accept_waveform=lambda *a: None, result=SimpleNamespace(text=" local text "))

    def decode_stream(self, stream):
        pass


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        model, _, payload = fixture()
        self.models = LocalModelManager(self.directory.name, model)
        self.models.path.mkdir(parents=True)
        (self.models.path / "tokens.txt").write_bytes(payload)
        self.now = 0
        self.loader = Mock(return_value=Recognizer())
        self.service = LocalTranscriber(self.models, loader=self.loader, clock=lambda: self.now, start_timer=False)
        self.service.configure(active=True)
        self.addCleanup(self.service.close)

    def attempt(self):
        attempt = self.service.begin_attempt("test-model")
        self.addCleanup(attempt.close)
        return attempt

    def test_startup_is_lazy_and_idle_unloads_then_hotkey_reloads(self):
        self.loader.assert_not_called()
        self.assertEqual(self.service.snapshot()["state"], "Unloaded")
        attempt = self.attempt()
        self.assertEqual(attempt.transcribe(wav()), "local text")
        attempt.close()
        self.now = 1199
        self.assertFalse(self.service.check_idle())
        self.now = 1200
        self.assertTrue(self.service.check_idle())
        self.assertTrue(self.models.path.exists())
        self.assertTrue(self.models._active)
        self.assertEqual(self.attempt().transcribe(wav()), "local text")
        self.assertEqual(self.loader.call_count, 2)

    def test_zero_disables_idle_unload(self):
        self.service.configure(active=True, idle_minutes=0)
        attempt = self.attempt()
        attempt.ready.result(2)
        attempt.close()
        self.now = 100000
        self.assertFalse(self.service.check_idle())

    def test_long_dictation_uses_bounded_windows_and_returns_one_string(self):
        import numpy as np
        from meetings.audio import wav_bytes

        lengths = []
        class TimedRecognizer:
            def create_stream(self):
                return SimpleNamespace(accept_waveform=lambda rate, samples: lengths.append(len(samples)),
                    result=SimpleNamespace(text=' synthetic words.', tokens=[' synthetic', ' words', '.'],
                                           timestamps=[.7, 1., 1.3], durations=[.2, .2, .1]))
            def decode_stream(self, stream):
                pass
        self.loader.return_value = TimedRecognizer()
        result = self.attempt().transcribe(wav_bytes(np.full(16000 * 65, 3000, dtype=np.int16)))
        self.assertGreater(len(lengths), 1)
        self.assertLessEqual(max(lengths), 28 * 16000)
        self.assertIsInstance(result, str)
        self.assertEqual(result.count('synthetic'), len(lengths))

    def test_cancelled_recording_resets_timer_and_prevents_unload_while_held(self):
        attempt = self.attempt()
        attempt.ready.result(2)
        self.now = 5000
        self.assertFalse(self.service.check_idle())
        attempt.close()
        self.now = 6199
        self.assertFalse(self.service.check_idle())
        self.now = 6200
        self.assertTrue(self.service.check_idle())

    def test_attempt_failure_resets_idle_timer(self):
        attempt = self.attempt()
        attempt.ready.result(2)
        self.now = 5000
        with self.assertRaises(ModelError):
            attempt.transcribe(wav(rate=44100))
        attempt.close()
        self.now = 6199
        self.assertFalse(self.service.check_idle())
        self.now = 6200
        self.assertTrue(self.service.check_idle())

    def test_loading_overlaps_recording_and_load_requests_are_shared(self):
        entered, release = threading.Event(), threading.Event()

        def slow_load(_):
            entered.set()
            release.wait(5)
            return Recognizer()

        self.loader.side_effect = slow_load
        first = self.attempt()
        self.assertTrue(entered.wait(2))
        second = self.attempt()
        self.assertIs(first.ready, second.ready)
        self.now = 5000
        first.close()  # Cancelling one recording cannot abort another load user.
        self.assertFalse(self.service.check_idle())
        with ThreadPoolExecutor() as executor:
            loading = Mock()
            result = executor.submit(second.transcribe, wav(), loading)
            release.set()
            self.assertEqual(result.result(3), "local text")
        self.assertEqual(self.loader.call_count, 1)

    def test_switching_provider_defers_release_until_pending_recording_finishes(self):
        attempt = self.attempt()
        attempt.ready.result(2)
        self.service.configure(active=False)
        self.assertEqual(self.service.snapshot()["state"], "Loaded")
        with self.assertRaises(ModelError):
            self.models.remove()
        self.assertEqual(attempt.transcribe(wav()), "local text")
        attempt.close()
        self.assertEqual(self.service.snapshot()["state"], "Unloaded")
        self.models.remove()

    def test_decode_requests_queue_and_cannot_race_idle_unload(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def decode(stream):
            calls.append("decode")
            entered.set()
            release.wait(5)

        self.loader.return_value.decode_stream = decode
        first, second = self.attempt(), self.attempt()
        with ThreadPoolExecutor() as executor:
            one = executor.submit(first.transcribe, wav())
            self.assertTrue(entered.wait(2))
            two = executor.submit(second.transcribe, wav())
            self.now = 10000
            self.assertFalse(self.service.check_idle())
            self.assertEqual(len(calls), 1)
            release.set()
            self.assertEqual(one.result(3), "local text")
            self.assertEqual(two.result(3), "local text")
        self.assertEqual(len(calls), 2)

    def test_load_error_can_be_retried_without_http(self):
        self.loader.side_effect = RuntimeError("runtime unavailable")
        first = self.attempt()
        with self.assertRaises(ModelError):
            first.transcribe(wav())
        first.close()
        self.assertEqual(self.service.snapshot()["state"], "Error")
        self.loader.side_effect = None
        self.assertEqual(self.attempt().transcribe(wav()), "local text")

    def test_cancel_then_switch_away_cannot_remove_files_during_load(self):
        entered, release = threading.Event(), threading.Event()

        def slow_load(_):
            entered.set()
            release.wait(5)
            return Recognizer()

        self.loader.side_effect = slow_load
        attempt = self.attempt()
        self.assertTrue(entered.wait(2))
        attempt.close()
        self.service.configure(active=False)
        with self.assertRaises(ModelError):
            self.models.remove()
        release.set()
        attempt.ready.result(3)
        self.assertEqual(self.service.snapshot()["state"], "Unloaded")
        self.models.remove()

    def test_missing_files_fail_with_no_cloud_fallback(self):
        (self.models.path / "tokens.txt").unlink()
        with patch("requests.post") as post, self.assertRaises(ModelError):
            self.attempt().transcribe(wav())
        post.assert_not_called()
        self.loader.assert_not_called()

    def test_quit_can_stop_accepting_work_without_waiting_for_native_load(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()

        def slow_load(_):
            entered.set()
            release.wait(5)
            return Recognizer()

        self.loader.side_effect = slow_load
        attempt = self.attempt()
        self.assertTrue(entered.wait(2))

        def close():
            self.service.close(wait=False)
            closed.set()

        thread = threading.Thread(target=close)
        thread.start()
        try:
            self.assertTrue(closed.wait(2))
            with self.assertRaises(ModelError):
                self.service.begin_attempt("test-model")
        finally:
            release.set()
            thread.join(3)
        attempt.ready.result(3)

    def test_int16_conversion_and_format_validation(self):
        samples = wav_samples(wav())
        self.assertEqual(samples.dtype.name, "float32")
        self.assertEqual(samples[0], -1)
        self.assertAlmostEqual(samples[1], 32767 / 32768)
        with self.assertRaises(ModelError):
            wav_samples(wav(channels=2))


if __name__ == "__main__":
    unittest.main()


class HotwordTests(unittest.TestCase):
    def test_vocabulary_reaches_the_stream_as_slash_separated_hotwords(self):
        from local_transcription import hotwords_string

        self.assertEqual(hotwords_string(["Ownkey", " Bart  van der Meeren ", "", "a/b"]), "Ownkey/Bart van der Meeren/a b")
        self.assertEqual(hotwords_string(()), "")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        model, _, payload = fixture()
        models = LocalModelManager(directory.name, model)
        models.path.mkdir(parents=True)
        (models.path / "tokens.txt").write_bytes(payload)
        recognizer = Recognizer()
        recognizer.create_stream = Mock(side_effect=lambda *args: Recognizer().create_stream())
        service = LocalTranscriber(models, loader=Mock(return_value=recognizer), clock=lambda: 0, start_timer=False)
        self.addCleanup(service.close)
        service.configure(active=True)
        attempt = service.begin_attempt("test-model")
        self.assertEqual(attempt.transcribe(wav(), vocabulary=["Ownkey", "Orukeet"]), "local text")
        self.assertEqual(attempt.transcribe(wav()), "local text")
        attempt.close()
        self.assertEqual([call.args for call in recognizer.create_stream.call_args_list], [("Ownkey/Orukeet",), ()])
