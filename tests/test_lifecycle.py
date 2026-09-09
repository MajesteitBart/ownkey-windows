import os
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

import ownkey


@unittest.skipUnless(os.name == "nt", "Windows desktop lifecycle")
class SettingsLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.app = ownkey.OwnkeyApp.__new__(ownkey.OwnkeyApp)
        self.app.cfg = dict(ownkey.DEFAULT_CONFIG)
        self.app._ui_root = ownkey.tk.Tk()
        self.app._ui_root.withdraw()
        self.app._ui_commands = queue.Queue()
        self.app._settings = ownkey.SettingsWindow(self.app)
        self.errors = []
        self.app._ui_root.report_callback_exception = lambda *error: self.errors.append(error)

    def tearDown(self):
        self.app._settings._on_close()
        self.app._ui_root.destroy()

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

    def test_close_during_model_refresh_does_not_touch_destroyed_widgets(self):
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        with patch.object(ownkey, "list_available_models", side_effect=lambda *a: (time.sleep(.1) or ["test"])):
            self.app._settings.open()
            buttons = [w for w in descendants(self.app._settings._win)
                       if isinstance(w, ownkey.tk.Button) and w.cget("text") == "Refresh"]
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
