import os
import queue
import threading
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

import ownkey


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


@unittest.skipUnless(os.name == "nt" or os.environ.get("DISPLAY"), "Requires a desktop session")
class SettingsLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.app = ownkey.OwnkeyApp.__new__(ownkey.OwnkeyApp)
        self.app.cfg = dict(ownkey.DEFAULT_CONFIG)
        self.model_directory = tempfile.TemporaryDirectory()
        self.app.local_models = ownkey.LocalModelManager(self.model_directory.name)
        self.app.local_transcriber = ownkey.LocalTranscriber(self.app.local_models, start_timer=False)
        self.app._ui_root = ownkey.tk.Tk()
        self.app._ui_root.withdraw()
        self.app._ui_commands = queue.Queue()
        self.app._settings = ownkey.SettingsWindow(self.app)
        self.errors = []
        self.app._ui_root.report_callback_exception = lambda *error: self.errors.append(error)

    def tearDown(self):
        self.app._settings._on_close()
        for callback in self.app._ui_root.tk.call("after", "info"):
            self.app._ui_root.after_cancel(callback)
        self.app._ui_root.destroy()
        self.app.local_transcriber.close()
        self.model_directory.cleanup()

    def test_rewrite_menu_keeps_openrouter_and_custom_but_excludes_orukeet(self):
        self.app._settings.open()
        self.app._settings.show_page("rewriting")
        rewrite_page = self.app._settings.pages["rewriting"]
        provider = next(child for child in descendants(rewrite_page)
                        if isinstance(child, ownkey.ttk.Combobox) and "OpenRouter" in child.cget("values"))
        self.assertIn("Custom (OpenAI-compatible)", provider.cget("values"))
        self.assertNotIn("Local (Orukeet)", provider.cget("values"))
        self.assertNotIn("Local (small LLM)", provider.cget("values"))
        provider.set("OpenRouter")
        provider.event_generate("<<ComboboxSelected>>")
        self.app._ui_root.update()
        self.assertEqual(self.errors, [])

    def test_repeated_tray_clicks_reuse_window_and_reopen_on_ui_thread(self):
        for _ in range(12):
            worker = threading.Thread(target=lambda: [self.app._open_settings() for _ in range(5)])
            worker.start()
            worker.join()
            self.assertIsNone(self.app._settings._win)
            self.app._poll_ui_commands()
            self.app._ui_root.update()
            window = self.app._settings._win
            self.assertIsInstance(window, ownkey.tk.Toplevel)
            self.app._settings.open()
            self.assertIs(self.app._settings._win, window)
            self.app._settings._on_close()
            self.app._ui_root.update()
        self.assertEqual(self.errors, [])

    def test_every_page_renders_and_dictionary_entries_round_trip_into_config(self):
        self.app.cfg["vocabulary"] = ["Ownkey"]
        self.app.cfg["corrections"] = [{"from": "own key", "to": "Ownkey"}]
        self.app.apply_settings = Mock()
        with patch.object(ownkey, "set_startup"):
            self.app._settings.open()
            for name, _title, _lead in ownkey.SettingsWindow.PAGES:
                self.app._settings.show_page(name)
                self.app._ui_root.update()
                self.assertEqual(self.app._settings.current_page, name)
            self.app._settings.dictionary_entries.append({"word": "Orukeet"})
            self.app._settings.save_button.invoke()
            deadline = time.monotonic() + 1
            while not self.app.apply_settings.called and time.monotonic() < deadline:
                self.app._ui_root.update()
                time.sleep(.01)
        changes = self.app.apply_settings.call_args.args[0]
        self.assertEqual(changes["vocabulary"], ["Ownkey", "Orukeet"])
        self.assertEqual(changes["corrections"], [{"from": "own key", "to": "Ownkey"}])
        self.assertTrue(changes["remove_fillers"])
        self.assertEqual(changes["filler_languages"], ["en", "nl"])
        self.assertEqual(self.errors, [])

    def test_close_during_model_refresh_does_not_touch_destroyed_widgets(self):
        with patch.object(ownkey, "list_available_models", side_effect=lambda *a: (time.sleep(.1) or ["test"])):
            self.app._settings.open()
            buttons = [w for w in descendants(self.app._settings._win)
                       if isinstance(w, ownkey.brand_ui.Button) and w.cget("text") == "Refresh"]
            buttons[0].invoke()
            self.app._settings._on_close()
            self.app._settings.open()
            deadline = time.monotonic() + .25
            while time.monotonic() < deadline:
                self.app._ui_root.update()
                time.sleep(.01)
        self.assertEqual(self.errors, [])


@unittest.skipUnless(os.name == "nt", "Windows process management")
class OverlayProcessTests(unittest.TestCase):
    def test_application_closes_settings_and_background_workers_on_quit(self):
        code = """
import tempfile
from unittest.mock import Mock, patch
import ownkey
manager_type = ownkey.LocalModelManager
with tempfile.TemporaryDirectory() as directory, \
        patch.object(ownkey, 'load_config', return_value=dict(ownkey.DEFAULT_CONFIG)), \
        patch.object(ownkey, 'is_startup_enabled', return_value=True), \
        patch.object(ownkey, 'LocalModelManager', side_effect=lambda **kwargs: manager_type(directory)), \
        patch.object(ownkey.pystray, 'Icon', return_value=Mock()):
    app = ownkey.OwnkeyApp()
    app._overlay = Mock()
    app._tauri_overlay_exe = None
    app._open_settings()
    original = app._poll_ui_commands
    errors = []
    def poll():
        app._ui_root.report_callback_exception = lambda *error: errors.append(str(error))
        original()
        app._ui_root.after(200, app._quit)
    app._poll_ui_commands = poll
    with patch.object(app, '_start_connection_monitor'), patch.object(app, '_first_run_prompt'), \
            patch.object(app, 'start_listener'), patch.object(app, '_start_tauri_overlay'), \
            patch.object(app, '_ensure_tauri_overlay'):
        app.run()
    assert app._ui_root is None
    assert app.local_transcriber.snapshot()['state'] == 'Unloaded'
    assert not errors, errors
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_process_probe_never_creates_a_console(self):
        with patch.object(ownkey.subprocess, "run", return_value=Mock(stdout="ownkey-overlay.exe")) as run:
            self.assertTrue(ownkey.is_tauri_overlay_process_running())
        self.assertEqual(run.call_args.kwargs["creationflags"], ownkey.subprocess.CREATE_NO_WINDOW)

    def test_owned_overlay_receives_parent_pid_and_is_reused(self):
        app = ownkey.OwnkeyApp.__new__(ownkey.OwnkeyApp)
        app._tauri_overlay_exe = "C:/test/ownkey-overlay.exe"
        app._tauri_overlay_process = None
        app._tauri_overlay_started_by_app = False
        app._overlay = Mock()
        process = Mock()
        process.poll.return_value = None
        with patch.object(ownkey, "is_tauri_overlay_process_running", return_value=False) as probe, \
                patch.object(ownkey.subprocess, "Popen", return_value=process) as launch:
            app._start_tauri_overlay()
            for _ in range(10):
                app._ensure_tauri_overlay(resync=True)
        launch.assert_called_once()
        probe.assert_called_once()
        self.assertEqual(launch.call_args.kwargs["env"]["OWNKEY_PARENT_PID"], str(os.getpid()))
        self.assertEqual(launch.call_args.kwargs["creationflags"], ownkey.subprocess.CREATE_NO_WINDOW)
        app._stop_tauri_overlay()
        process.terminate.assert_called_once()
