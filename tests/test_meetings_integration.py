import json
import os
import queue
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ownkey


def make_app(capturing=False, paused=False):
    app = ownkey.OwnkeyApp.__new__(ownkey.OwnkeyApp)
    app.cfg = dict(ownkey.DEFAULT_CONFIG)
    app._down = False
    app._recording = False
    app._state = "idle"
    app._connection_state = "online"
    app._overlay = Mock()
    app._tray = Mock()
    app._ensure_tauri_overlay = Mock()
    app._target_status = lambda: "unknown"
    app._start_recording = Mock()
    app._ui_commands = queue.Queue()
    app._meetings_error = ""
    app.meeting_server = SimpleNamespace(url="http://127.0.0.1:1/?token=abc")
    state = {"state": "paused" if paused else "recording", "elapsed": 61, "title": "Weekly sync"}
    app.meetings = SimpleNamespace(
        is_capturing=lambda: capturing,
        capture_state=lambda: state if capturing else None,
        stop=Mock(return_value={"state": "stopped"}),
        pause=Mock(), resume=Mock(),
    )
    return app


class HotkeyGateTests(unittest.TestCase):
    def test_hotkey_shows_meeting_status_instead_of_recording(self):
        app = make_app(capturing=True)
        with patch.object(app, "_hotkey_mode_for", return_value="dictate"):
            app._on_press(object())
        app._start_recording.assert_not_called()
        self.assertFalse(app._down)
        message = app._overlay.update.call_args.kwargs["message"]
        self.assertEqual(message, "Meeting recording · 01:01")
        app._overlay.show.assert_called_once()
        app._overlay.hide_later.assert_called_once()

    def test_hotkey_records_when_no_meeting_runs(self):
        app = make_app(capturing=False)
        with patch.object(app, "_hotkey_mode_for", return_value="dictate"):
            app._on_press(object())
        app._start_recording.assert_called_once_with("dictate")
        self.assertTrue(app._down)

    def test_disabled_meetings_never_block_hotkeys(self):
        app = make_app()
        app.meetings = None
        with patch.object(app, "_hotkey_mode_for", return_value="rewrite"):
            app._on_press(object())
        app._start_recording.assert_called_once_with("rewrite")


class TrayTests(unittest.TestCase):
    def test_tray_icon_shows_a_recording_meeting_while_dictation_is_idle(self):
        app = make_app(capturing=True, paused=True)
        with patch.object(ownkey, "make_icon", side_effect=lambda state: state) as icon:
            app._set_state("idle")
        icon.assert_called_with("recording")
        self.assertEqual(app._tray.title, "Ownkey — Meeting paused")
        app = make_app(capturing=False)
        with patch.object(ownkey, "make_icon", side_effect=lambda state: state):
            app._set_state("idle")
        self.assertEqual(app._tray.title, "Ownkey — Idle")

    def test_menu_has_meetings_entries_that_follow_capture_state(self):
        app = make_app(capturing=True)
        menu = app._build_tray_menu()
        items = list(menu.items)
        self.assertEqual(items[0].text, "Meetings")
        submenu = [item for item in items[0].submenu.items if item.text != "- - - -"]
        self.assertEqual([item.text for item in submenu], ["Open Meetings", "New meeting", "Pause meeting", "Stop meeting and save"])
        self.assertTrue(all(item.visible for item in submenu))
        app.meetings.is_capturing = lambda: False
        submenu = [item for item in app._build_tray_menu().items[0].submenu.items if item.text != "- - - -"]
        self.assertEqual([item.text for item in submenu if item.visible], ["Open Meetings", "New meeting"])

    def test_pause_resume_and_stop_from_the_tray(self):
        app = make_app(capturing=True)
        app._meeting_pause_resume()
        app.meetings.pause.assert_called_once()
        app = make_app(capturing=True, paused=True)
        app._meeting_pause_resume()
        app.meetings.resume.assert_called_once()

    def test_open_meetings_uses_the_token_url(self):
        app = make_app()
        with patch.object(ownkey.webbrowser, "open") as browser, patch.object(ownkey.threading, "Thread") as thread:
            thread.side_effect = lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args))
            app._open_new_meeting()
        browser.assert_called_once_with("http://127.0.0.1:1/?token=abc&view=new")
        app.meeting_server = None
        app._meetings_error = "boom"
        app._notify_error = Mock()
        app._open_meetings()
        app._notify_error.assert_called_once()


class QuitTests(unittest.TestCase):
    def test_quit_asks_while_recording_and_respects_keep_recording(self):
        app = make_app(capturing=True)
        with patch.object(app, "_confirm_stop_meeting", return_value=False):
            app._quit()
        self.assertTrue(app._ui_commands.empty())
        with patch.object(app, "_confirm_stop_meeting", return_value=True):
            app._quit()
        self.assertEqual(app._ui_commands.get_nowait(), "quit")

    def test_confirm_stop_meeting_stops_on_yes_and_keeps_on_no(self):
        app = make_app(capturing=True)
        if os.name != "nt":
            self.skipTest("Windows message box")
        with patch.object(ownkey.ctypes.windll.user32, "MessageBoxW", return_value=7):
            self.assertFalse(app._confirm_stop_meeting())
        app.meetings.stop.assert_not_called()
        with patch.object(ownkey.ctypes.windll.user32, "MessageBoxW", return_value=6):
            self.assertTrue(app._confirm_stop_meeting())
        app.meetings.stop.assert_called_once()


class ConfigTests(unittest.TestCase):
    def test_meeting_settings_are_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"meetings_remote_policy": "always", "meetings_auto_summary": "yes", "meetings_retention": "forever"}, handle)
            with patch.object(ownkey, "CONFIG_FILE", path):
                cfg = ownkey.load_config()
        self.assertEqual(cfg["meetings_remote_policy"], "ask")
        self.assertTrue(cfg["meetings_auto_summary"])
        self.assertEqual(cfg["meetings_retention"], "days7")
        self.assertEqual(ownkey.DEFAULT_CONFIG["meetings_remote_policy"], "ask")

    def test_config_changes_from_the_meeting_window_are_saved(self):
        app = make_app()
        app._config_lock = ownkey.threading.Lock()
        with patch.object(ownkey, "save_config") as save:
            app._meeting_config_changed({"meetings_remote_policy": "allow"})
        self.assertEqual(app.cfg["meetings_remote_policy"], "allow")
        save.assert_called_once_with(app.cfg)


if __name__ == "__main__":
    unittest.main()
