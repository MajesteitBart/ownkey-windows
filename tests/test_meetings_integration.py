import json
import os
import queue
import socket
import tempfile
import threading
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


class FakeOverlay:
    """Answers meeting-window requests on a local UDP port like the overlay process does."""

    def __init__(self, answer="opened"):
        self.answer, self.requests = answer, []
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(5)
        self.address = self.socket.getsockname()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            while True:
                payload, sender = self.socket.recvfrom(8192)
                self.requests.append(json.loads(payload.decode("utf-8")))
                if self.answer is not None:
                    self.socket.sendto(json.dumps({"meetings": self.answer}).encode("utf-8"), sender)
        except OSError:
            pass

    def close(self):
        self.socket.close()
        self.thread.join(timeout=2)


class MeetingWindowTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.app._tauri_overlay_exe = "ownkey-overlay.exe"
        self.app._tauri_overlay_process = None
        self.app._ensure_tauri_overlay = Mock()

    def _with_overlay(self, answer):
        overlay = FakeOverlay(answer)
        self.addCleanup(overlay.close)
        bridge = patch.object(ownkey, "OVERLAY_BRIDGE_ADDR", overlay.address)
        bridge.start()
        self.addCleanup(bridge.stop)
        return overlay

    def test_meetings_open_in_ownkeys_own_window(self):
        overlay = self._with_overlay("opened")
        with patch.object(ownkey.webbrowser, "open") as browser:
            self.app._show_meeting_window("http://127.0.0.1:1/?token=abc", False)
            self.app._show_meeting_window("http://127.0.0.1:1/?token=abc&view=new", True)
        browser.assert_not_called()
        self.app._ensure_tauri_overlay.assert_called()
        self.assertEqual(overlay.requests, [
            {"meetings": {"url": "http://127.0.0.1:1/?token=abc&shell=app", "navigate": False}},
            {"meetings": {"url": "http://127.0.0.1:1/?token=abc&view=new&shell=app", "navigate": True}},
        ])

    def test_tray_entries_only_navigate_when_they_name_a_view(self):
        with patch.object(ownkey.threading, "Thread") as thread:
            self.app._open_meetings()
            self.app._open_new_meeting()
        calls = [call.kwargs["args"] for call in thread.call_args_list]
        self.assertEqual(calls, [("http://127.0.0.1:1/?token=abc", False),
                                 ("http://127.0.0.1:1/?token=abc&view=new", True)])

    def test_the_browser_is_the_fallback(self):
        url = "http://127.0.0.1:1/?token=abc"
        with patch.object(ownkey.webbrowser, "open") as browser:
            self._with_overlay("refused")
            self.app._show_meeting_window(url, False)  # the overlay said no: do not ask again
            browser.assert_called_once_with(url)
            browser.reset_mock()
            self.app._tauri_overlay_exe = None  # running from source without the overlay build
            self.app._show_meeting_window(url, False)
            browser.assert_called_once_with(url)

    def test_a_silent_overlay_falls_back_after_the_wait(self):
        overlay = self._with_overlay(None)
        with patch.object(ownkey, "MEETING_WINDOW_WAIT_SECONDS", 0.3), patch.object(ownkey.webbrowser, "open") as browser:
            self.assertIsNone(ownkey.request_meeting_window("http://127.0.0.1:1/", False, timeout=0.1))
            self.app._show_meeting_window("http://127.0.0.1:1/?token=abc", False)
        browser.assert_called_once_with("http://127.0.0.1:1/?token=abc")
        self.assertGreaterEqual(len(overlay.requests), 2)

    def test_bridge_address_can_move_for_a_development_copy(self):
        with patch.dict(os.environ, {"OWNKEY_OVERLAY_UDP": "127.0.0.1:38499"}):
            self.assertEqual(ownkey._overlay_bridge_addr(), ("127.0.0.1", 38499))
        with patch.dict(os.environ, {"OWNKEY_OVERLAY_UDP": "nonsense"}):
            self.assertEqual(ownkey._overlay_bridge_addr(), ("127.0.0.1", 38485))
        # On its own port, the overlay of an installed Ownkey must not count as ours.
        with patch.dict(os.environ, {"OWNKEY_OVERLAY_UDP": "127.0.0.1:38499"}),                 patch.object(ownkey.subprocess, "run", return_value=Mock(stdout="ownkey-overlay.exe")) as run:
            self.assertFalse(ownkey.is_tauri_overlay_process_running())
            run.assert_not_called()


class PillTests(unittest.TestCase):
    def test_capture_transitions_show_in_the_pill_once(self):
        app = make_app(capturing=True)
        app._meeting_seen_state = None
        app._meeting_capture_changed()
        self.assertEqual(app._overlay.update.call_args.kwargs["message"], "Meeting recording")
        app._overlay.reset_mock()
        app._meeting_capture_changed()  # same state again: no pill message, no show
        app._overlay.show.assert_not_called()
        self.assertFalse(any(str(c.kwargs.get("message", "")).startswith("Meeting") for c in app._overlay.update.call_args_list))
        app = make_app(capturing=True, paused=True)
        app._meeting_seen_state = "recording"
        app._meeting_capture_changed()
        self.assertEqual(app._overlay.update.call_args.kwargs["message"], "Meeting paused")
        app = make_app(capturing=False)
        app._meeting_seen_state = "recording"
        app._meeting_capture_changed()
        self.assertEqual(app._overlay.update.call_args.kwargs["message"], "Meeting saved")
        # show() cancels the tray refresh's short hide; the 2.2 s hide wins
        app._overlay.show.assert_called_once()
        app._overlay.hide_later.assert_called_with(2200)

    def test_pill_stays_quiet_while_dictation_records(self):
        app = make_app(capturing=True)
        app._recording = True
        app._meeting_seen_state = None
        app._meeting_capture_changed()
        app._overlay.show.assert_not_called()


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
                json.dump({"meetings_remote_policy": "always", "meetings_auto_summary": "yes", "meetings_retention": "forever",
                           "meetings_upload_policy": "allow", "pyannote_api_key": " pk-1 ", "meetings_auto_speakers": 1,
                           "meetings_audio_provider": "OpenAI", "meetings_audio_model": " whisper-1 ",
                           "meetings_transcription_policy": "nope"}, handle)
            with patch.object(ownkey, "CONFIG_FILE", path):
                cfg = ownkey.load_config()
        self.assertEqual(cfg["meetings_remote_policy"], "ask")
        self.assertEqual(cfg["meetings_upload_policy"], "allow")
        self.assertTrue(cfg["meetings_auto_summary"])
        self.assertTrue(cfg["meetings_auto_speakers"])
        self.assertEqual(cfg["meetings_retention"], "days7")
        self.assertEqual(cfg["pyannote_api_key"], "pk-1")
        self.assertEqual(cfg["meetings_audio_provider"], "openai")
        self.assertEqual(cfg["meetings_audio_model"], "whisper-1")
        self.assertEqual(cfg["meetings_transcription_policy"], "ask")
        self.assertEqual(ownkey.DEFAULT_CONFIG["meetings_remote_policy"], "ask")
        self.assertEqual(ownkey.DEFAULT_CONFIG["pyannote_api_key"], "")
        self.assertEqual(ownkey.DEFAULT_CONFIG["meetings_audio_provider"], "same")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"meetings_audio_provider": "openrouter"}, handle)  # no audio API: falls back
            with patch.object(ownkey, "CONFIG_FILE", path):
                self.assertEqual(ownkey.load_config()["meetings_audio_provider"], "same")

    def test_config_changes_from_the_meeting_window_are_saved(self):
        app = make_app()
        app._config_lock = ownkey.threading.Lock()
        with patch.object(ownkey, "save_config") as save:
            app._meeting_config_changed({"meetings_remote_policy": "allow"})
        self.assertEqual(app.cfg["meetings_remote_policy"], "allow")
        save.assert_called_once_with(app.cfg)


if __name__ == "__main__":
    unittest.main()
