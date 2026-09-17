import io
import json
import queue
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
import ownkey
import providers
from local_models import MODEL_ID, LocalModelManager, ModelError
from local_transcription import LocalTranscriber
from test_local_models import fixture
from test_local_transcription import Recognizer, wav


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"
        self.config_patch = patch.object(ownkey, "CONFIG_FILE", str(self.path))
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        model, _, payload = fixture()
        models = LocalModelManager(self.directory.name, model)
        models.path.mkdir(parents=True)
        (models.path / "tokens.txt").write_bytes(payload)
        self.app = ownkey.OwnkeyApp.__new__(ownkey.OwnkeyApp)
        self.app.cfg = dict(ownkey.DEFAULT_CONFIG)
        self.app.local_models = models
        self.loader = Mock(return_value=Recognizer())
        self.app.local_transcriber = LocalTranscriber(models, loader=self.loader, start_timer=False)
        self.addCleanup(self.app.local_transcriber.close)
        self.app._lock = threading.Lock()
        self.app._config_lock = threading.Lock()
        self.app._recording = False
        self.app._pending_listener_restart = False
        self.app.restart_listener = Mock()
        self.app.refresh_connection_status = Mock()
        self.app._set_state = Mock()
        self.app._overlay = Mock()
        self.app._connection_state = "online"
        self.app._target_status = Mock(return_value="selected")
        self.app._notify_error = Mock()
        self.changes = {"audio_provider": "orukeet", "audio_model": "test-model", "audio_api_key": "", "audio_endpoint": ""}

    def test_save_prepares_then_commits_and_rebases_on_live_config(self):
        self.app.cfg["key_written_outside_settings"] = "keep"
        self.app.apply_settings(self.changes)
        disk = json.loads(self.path.read_text())
        self.assertEqual(disk["audio_provider"], "orukeet")
        self.assertEqual(disk["key_written_outside_settings"], "keep")
        self.assertEqual(self.app.local_transcriber.snapshot()["state"], "Loaded")
        self.assertTrue(self.app.local_models._active)

    def test_failed_config_replace_preserves_disk_and_active_provider(self):
        ownkey.save_config(self.app.cfg)
        previous_disk = self.path.read_bytes()
        previous_cfg = self.app.cfg
        with patch.object(ownkey.os, "replace", side_effect=OSError("write denied")), self.assertRaises(OSError):
            self.app.apply_settings(self.changes)
        self.assertIs(self.app.cfg, previous_cfg)
        self.assertEqual(self.path.read_bytes(), previous_disk)
        self.assertEqual(self.app.local_transcriber.snapshot()["state"], "Unloaded")
        self.assertEqual(list(self.path.parent.glob(".config-*")), [])
        self.assertFalse(self.app.local_models._active)

    def test_failed_save_keeps_previously_active_local_recognizer_usable(self):
        self.app.apply_settings(self.changes)
        previous_cfg = self.app.cfg
        with patch.object(ownkey, "save_config", side_effect=OSError("full disk")), self.assertRaises(OSError):
            self.app.apply_settings({"audio_provider": "mistral"})
        self.assertIs(self.app.cfg, previous_cfg)
        attempt = self.app.local_transcriber.begin_attempt("test-model")
        self.addCleanup(attempt.close)
        self.assertEqual(attempt.transcribe(wav()), "local text")

    def alternate_location(self):
        folder = Path(self.directory.name) / "chosen model folder"
        folder.mkdir()
        (folder / "tokens.txt").write_bytes((self.app.local_models.path / "tokens.txt").read_bytes())
        with patch.object(ownkey, "LocalTranscriber", side_effect=lambda manager: LocalTranscriber(manager, loader=self.loader, start_timer=False)):
            manager, service = self.app.local_audio_at(str(folder))
        self.addCleanup(service.close)
        return manager, service

    def test_folder_switch_preserves_recording_and_persists_timeout(self):
        self.app.apply_settings(self.changes)
        previous = self.app.local_transcriber
        attempt = previous.begin_attempt("test-model")
        self.addCleanup(attempt.close)
        manager, service = self.alternate_location()
        self.app.apply_settings({**self.changes, "local_model_directory": str(manager.path), "local_model_idle_timeout_minutes": 0})
        self.assertIs(self.app.local_transcriber, service)
        self.assertEqual(attempt.transcribe(wav()), "local text")
        attempt.close()
        self.assertEqual(previous.snapshot()["state"], "Unloaded")
        disk = json.loads(self.path.read_text())
        self.assertEqual(disk["local_model_directory"], str(manager.path))
        self.assertEqual(service._timeout, 0)

    def test_meetings_follow_selected_model_directory(self):
        from meetings.service import MeetingService
        from meetings.store import MeetingStore
        meetings = MeetingService(MeetingStore(Path(self.directory.name) / 'meetings'),
            get_config=lambda: self.app.cfg, get_local_audio=self.app._meeting_local_audio)
        self.addCleanup(meetings.close)
        previous_models = self.app.local_models
        manager, transcriber = self.alternate_location()
        self.app.apply_settings({**self.changes, 'local_model_directory': str(manager.path)})
        self.assertEqual(meetings._get_local_audio(), (manager, transcriber))
        # Removing the retired model cannot make the selected location unavailable.
        (previous_models.path / 'tokens.txt').unlink()
        self.assertTrue(meetings.local_model_info()['installed'])
        self.assertEqual(meetings.local_model_info()['state'], 'Loaded')

    def test_failed_folder_save_keeps_previous_model_active(self):
        self.app.apply_settings(self.changes)
        previous = self.app.local_transcriber
        manager, service = self.alternate_location()
        with patch.object(ownkey, "save_config", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.app.apply_settings({**self.changes, "local_model_directory": str(manager.path)})
        self.assertIs(self.app.local_transcriber, previous)
        self.assertTrue(previous.models._active)
        self.assertEqual(service.snapshot()["state"], "Unloaded")

    def test_invalid_timeout_never_persists(self):
        for timeout in (-1, float("nan"), float("inf")):
            with patch.object(ownkey, "save_config") as save, self.assertRaises(ModelError):
                self.app.apply_settings({"local_model_idle_timeout_minutes": timeout})
            save.assert_not_called()

    def test_retired_local_rewrite_never_falls_back_to_http(self):
        cfg = {**self.app.cfg, "rewrite_provider": "local_rewrite", "rewrite_endpoint": "https://example.invalid"}
        with patch.object(providers.requests, "post") as post:
            with self.assertRaises(providers.ProviderConfigurationError):
                ownkey.chat_complete(cfg, "edit", "text")
        post.assert_not_called()

    def test_retired_local_config_disables_edits_and_preserves_orukeet(self):
        for provider in ("local_rewrite", "Local (small LLM)"):
            with self.subTest(provider=provider):
                saved = {**self.app.cfg, "audio_provider": "orukeet", "audio_model": MODEL_ID,
                         "local_model_directory": "C:/speech", "local_model_idle_timeout_minutes": 7,
                         "rewrite_provider": provider, "rewrite_model": "qwen3-1.7b-q4",
                         "rewrite_endpoint": "", "rewrite_api_key": "old-key",
                         "auto_rewrite": True, "rewrite_hotkey": "ctrl_r",
                         "local_rewrite_directory": "C:/rewrite", "local_rewrite_idle_timeout_minutes": 3}
                self.path.write_text(json.dumps(saved))
                loaded = ownkey.load_config()
                self.assertFalse(loaded["auto_rewrite"])
                self.assertEqual(loaded["rewrite_hotkey"], "off")
                for key in ("rewrite_provider", "rewrite_api_key", "rewrite_endpoint", "rewrite_model"):
                    self.assertEqual(loaded[key], ownkey.DEFAULT_CONFIG[key])
                for key in ("audio_provider", "audio_model", "local_model_directory", "local_model_idle_timeout_minutes"):
                    self.assertEqual(loaded[key], saved[key])
                self.assertNotIn("local_rewrite_directory", loaded)
                self.assertNotIn("local_rewrite_idle_timeout_minutes", loaded)

    def test_existing_rewrite_providers_keep_their_settings(self):
        for provider in ("openrouter", "custom", "ollama"):
            with self.subTest(provider=provider):
                saved = {**self.app.cfg, "rewrite_provider": provider, "rewrite_model": "chosen-model",
                         "rewrite_endpoint": "https://example.invalid/v1/chat/completions",
                         "rewrite_api_key": "chosen-key", "auto_rewrite": True,
                         "rewrite_hotkey": ownkey.DEFAULT_CONFIG["rewrite_hotkey"],
                         "local_rewrite_directory": "C:/old-rewrite"}
                self.path.write_text(json.dumps(saved))
                loaded = ownkey.load_config()
                for key in ("rewrite_provider", "rewrite_model", "rewrite_endpoint", "rewrite_api_key",
                            "auto_rewrite", "rewrite_hotkey"):
                    self.assertEqual(loaded[key], saved[key])

    def test_cancel_or_load_failure_does_not_persist(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(ownkey, "save_config") as save, self.assertRaises(ModelError):
            self.app.apply_settings(self.changes, cancelled)
        save.assert_not_called()
        self.assertEqual(self.app.cfg["audio_provider"], "mistral")
        self.loader.side_effect = RuntimeError("broken runtime")
        with patch.object(ownkey, "save_config") as save, self.assertRaises(ModelError):
            self.app.apply_settings(self.changes)
        save.assert_not_called()

    def test_saving_while_recording_defers_listener_restart(self):
        self.app._recording = True
        self.app.apply_settings({"audio_provider": "mistral", "hotkey": "f13"})
        self.assertTrue(self.app._pending_listener_restart)
        self.app.restart_listener.assert_not_called()

    def test_releasing_original_hotkey_after_saving_new_hotkey_stops_recording(self):
        self.app._record_cfg = dict(self.app.cfg)
        self.app._recording = True
        self.app._down = True
        self.app._record_mode = "dictate"
        self.app._stop_recording = Mock()
        self.app.apply_settings({"hotkey": "f13"})
        self.app._on_release(ownkey.pynput_keyboard.Key.alt_r)
        self.app._stop_recording.assert_called_once()
        self.assertFalse(self.app._down)

    def test_local_routing_ignores_endpoint_key_language_and_never_posts_audio(self):
        cfg = {**self.app.cfg, **self.changes, "audio_endpoint": "https://example.invalid", "language": "ja"}
        attempt = Mock()
        attempt.transcribe.return_value = "offline text"
        with patch.object(providers.requests, "post") as post:
            self.assertEqual(ownkey.transcribe(wav(), cfg, attempt), "offline text")
        post.assert_not_called()
        self.assertFalse(providers.provider_requires_key("orukeet"))
        with self.assertRaises(ModelError):
            ownkey.transcribe(wav(), cfg)
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.transcribe_audio("orukeet", "", "", MODEL_ID, wav())
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.complete_rewrite("orukeet", "", "", MODEL_ID, "", "")

    def test_openrouter_rewrite_selection_survives_save_and_restart_with_local_audio(self):
        changes = {**self.changes, "rewrite_provider": "openrouter",
                   "rewrite_endpoint": providers.default_endpoint("openrouter", "rewrite"),
                   "rewrite_api_key": "test-openrouter-key", "rewrite_model": "openai/test-model"}
        self.app.apply_settings(changes)
        restarted = ownkey.load_config()
        self.assertEqual(restarted["audio_provider"], "orukeet")
        self.assertEqual(restarted["rewrite_provider"], "openrouter")
        self.assertEqual(restarted["rewrite_api_key"], "test-openrouter-key")
        self.assertEqual(restarted["rewrite_endpoint"], "https://openrouter.ai/api/v1/chat/completions")

    def test_transcription_queue_keeps_recording_order(self):
        self.app._transcription_queue = queue.Queue()
        first, second = ([], True, "dictate", {"audio_provider": "mistral"}, None), ([], True, "dictate", self.changes, Mock())
        for recording in (first, second, None):
            self.app._transcription_queue.put(recording)
        self.app._transcribe_and_type = Mock()
        self.app._transcription_loop()
        self.assertEqual([call.args for call in self.app._transcribe_and_type.call_args_list], [first, second])

    def test_shutdown_during_transcription_releases_attempt_without_inserting_text(self):
        attempt = Mock()

        def transcribe(*args, **kwargs):
            self.app._shutting_down = True
            return "do not insert after quit"

        attempt.transcribe.side_effect = transcribe
        frames = [np.ones((16000, 1), dtype=np.int16)]
        with patch.object(ownkey, "type_text") as insert:
            self.app._transcribe_and_type(frames, True, "dictate", self.changes, attempt)
        insert.assert_not_called()
        attempt.close.assert_called_once()

    def test_saved_local_provider_survives_restart_without_eager_load(self):
        ownkey.save_config({**ownkey.DEFAULT_CONFIG, "audio_provider": "orukeet", "audio_model": MODEL_ID})
        with patch("local_transcription.load_recognizer") as load:
            cfg = ownkey.load_config()
        load.assert_not_called()
        self.assertEqual(cfg["audio_provider"], "orukeet")
        self.assertEqual(cfg["local_model_idle_timeout_minutes"], 20)
        self.assertEqual(cfg["audio_model"], MODEL_ID)

    def test_local_audio_forces_16khz_and_cloud_keeps_configured_rate(self):
        self.assertEqual(ownkey.audio_sample_rate({"audio_provider": "orukeet", "sample_rate": 48000}), 16000)
        self.assertEqual(ownkey.audio_sample_rate({"audio_provider": "mistral", "sample_rate": 48000}), 48000)

    def test_recording_snapshot_routes_dictation_after_provider_changes(self):
        cfg = {**self.app.cfg, **self.changes}
        attempt = Mock()
        attempt.transcribe.return_value = "recorded locally"
        frames = [np.ones((16000, 1), dtype=np.int16)]
        with patch.object(ownkey, "type_text") as insert, patch.object(providers.requests, "post") as post:
            self.app._transcribe_and_type(frames, True, "dictate", cfg, attempt)
        insert.assert_called_once_with("recorded locally", True)
        post.assert_not_called()
        attempt.close.assert_called_once()

    def test_spoken_rewrite_instructions_use_local_audio_with_separate_text_provider(self):
        cfg = {**self.app.cfg, **self.changes, "rewrite_provider": "ollama"}
        capture = {"text": "selected text", "done": threading.Event()}
        capture["done"].set()
        attempt = Mock()
        attempt.transcribe.return_value = "make it shorter"
        with patch.object(ownkey, "rewrite_selection_text", return_value="short") as rewrite, patch.object(ownkey, "type_text") as insert:
            self.app._run_rewrite_command(wav(), cfg, attempt, capture)
        rewrite.assert_called_once_with("selected text", "make it shorter", cfg)
        insert.assert_called_once_with("short", True)
        attempt.transcribe.assert_called_once()

    def test_microtap_releases_local_attempt(self):
        attempt = Mock()
        self.app._transcribe_and_type([], False, "dictate", self.changes, attempt)
        attempt.transcribe.assert_not_called()
        attempt.close.assert_called_once()

    def test_loading_message_does_not_replace_a_decode_or_recording(self):
        with patch.object(self.app.local_transcriber, "snapshot", return_value={"decoding": True}):
            self.app._local_loading_message()
        self.app._overlay.update.assert_not_called()
        self.app._recording = True
        self.app._local_loading_message()
        self.app._overlay.update.assert_not_called()

    def test_local_connection_monitor_does_not_probe_network(self):
        self.app.cfg.update(self.changes)
        self.app.local_models.check_installation()
        self.app._connection_stop = threading.Event()
        self.app._connection_kick = Mock()
        self.app._connection_kick.wait.side_effect = lambda *_: self.app._connection_stop.set()
        with patch.object(ownkey, "endpoint_reachable") as probe:
            self.app._connection_loop()
        probe.assert_not_called()
        self.assertEqual(self.app._connection_state, "online")


if __name__ == "__main__":
    unittest.main()


class CleanupPipelineTests(IntegrationTests):
    def test_dictation_gets_fillers_removed_and_corrections_applied_before_insertion(self):
        cfg = {**self.app.cfg, **self.changes, "vocabulary": ["Ownkey"],
               "corrections": [{"from": "own key", "to": "Ownkey"}], "remove_fillers": True}
        attempt = Mock()
        attempt.transcribe.return_value = "Um, own key uh works."
        frames = [np.ones((16000, 1), dtype=np.int16)]
        with patch.object(ownkey, "type_text") as insert:
            self.app._transcribe_and_type(frames, True, "dictate", cfg, attempt)
        insert.assert_called_once_with("Ownkey works.", True)
        self.assertEqual(attempt.transcribe.call_args.kwargs["vocabulary"], ["Ownkey"])
