import os
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

import ownkey
import windows_hotkeys

RIGHT_ALT = windows_hotkeys.HOTKEY_VKS["right alt"]
RIGHT_SHIFT = windows_hotkeys.HOTKEY_VKS["right shift"]


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class Keyboard:
    """Physical key state the listener polls."""

    def __init__(self, *held, keys=1):
        self.held = set(held)
        self.keys = keys
        self.polls = 0

    def __call__(self, vk):
        self.polls += 1
        return vk in self.held

    def set(self, vk, down):
        """Change a key and wait until the listener has sampled it."""
        (self.held.add if down else self.held.discard)(vk)
        seen = self.polls
        if not wait_for(lambda: self.polls >= seen + 2 * self.keys):
            raise AssertionError("the listener stopped polling")


class HotkeyListenerTests(unittest.TestCase):
    def start(self, keyboard, on_press, on_release):
        listener = windows_hotkeys.HotkeyListener(
            {RIGHT_ALT: "right alt"}, on_press, on_release, key_down=keyboard, interval=0.005)
        listener.start()
        self.addCleanup(listener.join, 2)
        self.addCleanup(listener.stop)
        return listener

    def test_reports_one_press_and_one_release_per_hold(self):
        keyboard, events = Keyboard(), []
        self.start(keyboard, lambda key: events.append(("press", key)), lambda key: events.append(("release", key)))
        keyboard.set(RIGHT_ALT, True)
        self.assertTrue(wait_for(lambda: events == [("press", "right alt")]))
        time.sleep(0.05)
        self.assertEqual(events, [("press", "right alt")])
        keyboard.set(RIGHT_ALT, False)
        self.assertTrue(wait_for(lambda: events == [("press", "right alt"), ("release", "right alt")]))

    def test_changes_during_a_slow_press_callback_arrive_in_order(self):
        keyboard, events, opening = Keyboard(), [], threading.Event()

        def press(key):
            events.append(("press", key))
            if len(events) == 1:
                opening.wait(2)  # the microphone is slow to open

        self.start(keyboard, press, lambda key: events.append(("release", key)))
        keyboard.set(RIGHT_ALT, True)
        self.assertTrue(wait_for(lambda: len(events) == 1))
        for down in (False, True, False):  # let go, hold again, let go
            keyboard.set(RIGHT_ALT, down)
        opening.set()
        self.assertTrue(wait_for(lambda: events == [("press", "right alt"), ("release", "right alt")] * 2))

    def test_hold_during_a_slow_release_callback_is_not_lost(self):
        keyboard, events, closing = Keyboard(), [], threading.Event()

        def release(key):
            events.append(("release", key))
            if len(events) == 2:
                closing.wait(2)  # the microphone is slow to close

        self.start(keyboard, lambda key: events.append(("press", key)), release)
        keyboard.set(RIGHT_ALT, True)
        keyboard.set(RIGHT_ALT, False)
        self.assertTrue(wait_for(lambda: len(events) == 2))
        keyboard.set(RIGHT_ALT, True)
        keyboard.set(RIGHT_ALT, False)
        closing.set()
        self.assertTrue(wait_for(lambda: events == [("press", "right alt"), ("release", "right alt")] * 2))

    def test_key_already_held_counts_as_a_press(self):
        keyboard, events = Keyboard(RIGHT_ALT), []
        self.start(keyboard, lambda key: events.append(("press", key)), lambda key: events.append(("release", key)))
        self.assertTrue(wait_for(lambda: events == [("press", "right alt")]))
        keyboard.set(RIGHT_ALT, False)
        self.assertTrue(wait_for(lambda: events == [("press", "right alt"), ("release", "right alt")]))

    def test_callback_can_stop_its_own_listener(self):
        keyboard, events = Keyboard(), []
        listener = None

        def release_and_stop(key):
            events.append(key)
            listener.stop()

        listener = self.start(keyboard, lambda key: None, release_and_stop)
        keyboard.set(RIGHT_ALT, True)
        keyboard.held.clear()  # the release stops polling, so do not wait for samples
        listener.join(2)
        self.assertFalse(listener.is_alive())
        keyboard.held.add(RIGHT_ALT)
        keyboard.held.clear()
        time.sleep(0.05)
        self.assertEqual(events, ["right alt"])

    def test_failing_callback_does_not_end_push_to_talk(self):
        keyboard, presses, releases = Keyboard(), [], []

        def press(key):
            presses.append(key)
            raise RuntimeError("microphone vanished")

        with patch.object(windows_hotkeys.traceback, "print_exc"):
            self.start(keyboard, press, releases.append)
            for count in (1, 2):
                keyboard.set(RIGHT_ALT, True)
                self.assertTrue(wait_for(lambda: len(presses) == count))
                keyboard.set(RIGHT_ALT, False)
                self.assertTrue(wait_for(lambda: len(releases) == count))


class PushToTalkTests(unittest.TestCase):
    """The app-level failures from issue #10, driven through the polling listener."""

    def setUp(self):
        self.app = ownkey.OwnkeyApp.__new__(ownkey.OwnkeyApp)
        self.app.cfg = {**ownkey.DEFAULT_CONFIG, "hotkey": "right alt", "rewrite_hotkey": "right shift"}
        self.app._record_cfg = dict(self.app.cfg)
        self.app._recording = False
        self.app._record_mode = "dictate"
        self.app._down = False
        self.started = []
        self.opening = threading.Event()
        self.app._start_recording = self.slow_start
        self.app._stop_recording = Mock(side_effect=lambda: setattr(self.app, "_recording", False))
        keys = self.app._windows_hotkey_keys()
        self.keyboard = Keyboard(keys=len(keys))
        listener = windows_hotkeys.HotkeyListener(
            keys, self.app._on_press, self.app._on_release, key_down=self.keyboard, interval=0.005)
        listener.start()
        self.addCleanup(listener.join, 2)
        self.addCleanup(listener.stop)
        self.addCleanup(self.opening.set)

    def slow_start(self, mode):
        self.app._recording = True
        self.app._record_mode = mode
        self.app._record_cfg = dict(self.app.cfg)
        self.started.append(mode)
        self.opening.wait(2)  # the microphone is slow to open

    def test_quick_tap_during_slow_start_stops_the_recording(self):
        self.keyboard.set(RIGHT_ALT, True)
        self.assertTrue(wait_for(lambda: self.started == ["dictate"]))
        self.keyboard.set(RIGHT_ALT, False)
        self.opening.set()
        self.assertTrue(wait_for(lambda: self.app._stop_recording.called))
        self.assertFalse(self.app._down)
        self.assertFalse(self.app._recording)

    def test_dictation_works_after_a_quick_rewrite_tap(self):
        # A capital letter typed with Right Shift used to leave dictation dead.
        self.keyboard.set(RIGHT_SHIFT, True)
        self.assertTrue(wait_for(lambda: self.started == ["rewrite"]))
        self.keyboard.set(RIGHT_SHIFT, False)
        self.opening.set()
        self.assertTrue(wait_for(lambda: self.app._stop_recording.call_count == 1))
        self.keyboard.set(RIGHT_ALT, True)
        self.assertTrue(wait_for(lambda: self.started == ["rewrite", "dictate"]))
        self.keyboard.set(RIGHT_ALT, False)
        self.assertTrue(wait_for(lambda: self.app._stop_recording.call_count == 2))
        self.assertFalse(self.app._down)

    def test_second_hold_during_slow_start_is_a_separate_recording(self):
        self.keyboard.set(RIGHT_ALT, True)
        self.assertTrue(wait_for(lambda: self.started == ["dictate"]))
        self.keyboard.set(RIGHT_ALT, False)
        self.keyboard.set(RIGHT_ALT, True)
        self.opening.set()
        self.assertTrue(wait_for(lambda: self.started == ["dictate", "dictate"]))
        self.assertEqual(self.app._stop_recording.call_count, 1)
        self.assertTrue(self.app._recording)
        self.keyboard.set(RIGHT_ALT, False)
        self.assertTrue(wait_for(lambda: self.app._stop_recording.call_count == 2))
        self.assertFalse(self.app._down)
        self.assertFalse(self.app._recording)


@unittest.skipUnless(os.name == "nt", "Windows hotkeys")
class SettingsRestartTests(unittest.TestCase):
    """Saving Settings keeps the Windows listener without dropping holds or leaving push-to-talk debounced."""

    def setUp(self):
        self.app = ownkey.OwnkeyApp.__new__(ownkey.OwnkeyApp)
        self.app.cfg = dict(ownkey.DEFAULT_CONFIG)
        self.app._lock = threading.Lock()
        self.app._audio_lock = threading.Lock()
        self.app._audio_frames = []
        self.app._heard_audio_in_session = False
        self.app._transcription_queue = queue.Queue()
        self.app._recording = False
        self.app._record_mode = "dictate"
        self.app._record_cfg = dict(self.app.cfg)
        self.app._record_local_attempt = None
        self.app._record_capture = None
        self.app._record_local_error = None
        self.app._pending_listener_restart = False
        self.app._down = False
        self.app._start_recording = self.start_recording
        self.closing = threading.Event()
        self.app._stop_audio_stream = lambda: self.closing.wait(2)  # the microphone is slow to close
        self.addCleanup(self.closing.set)
        keys = self.app._windows_hotkey_keys()
        self.keyboard = Keyboard(keys=len(keys))
        self.listener = windows_hotkeys.HotkeyListener(
            keys, self.app._on_press, self.app._on_release, key_down=self.keyboard, interval=0.005)
        self.app._listener = self.listener
        self.listener.start()
        self.addCleanup(self.listener.join, 2)
        self.addCleanup(self.listener.stop)

    def start_recording(self, mode):
        with self.app._lock:
            self.app._recording = True
            self.app._record_mode = mode
            self.app._record_cfg = dict(self.app.cfg)

    def test_hold_during_slow_close_survives_the_deferred_restart(self):
        self.keyboard.set(RIGHT_ALT, True)
        self.assertTrue(wait_for(lambda: self.app._recording))
        self.app._pending_listener_restart = True  # Settings were saved while recording
        self.keyboard.set(RIGHT_ALT, False)
        self.keyboard.set(RIGHT_ALT, True)
        self.keyboard.set(RIGHT_ALT, False)
        self.closing.set()
        self.assertTrue(wait_for(lambda: self.app._transcription_queue.qsize() == 2))
        self.assertIs(self.app._listener, self.listener)
        self.assertFalse(self.app._pending_listener_restart)
        self.assertFalse(self.app._down)
        self.assertFalse(self.app._recording)

    def test_disabling_a_held_rewrite_key_after_a_failed_start_keeps_dictation_working(self):
        self.app.cfg = {**self.app.cfg, "rewrite_hotkey": "right shift"}

        def failed_start(mode):  # the microphone could not open
            self.app._record_mode = mode

        self.app._start_recording = failed_start
        self.keyboard.set(RIGHT_SHIFT, True)
        self.assertTrue(wait_for(lambda: self.app._record_mode == "rewrite"))
        with self.app._lock:  # what apply_settings does when idle
            self.app.cfg = {**self.app.cfg, "rewrite_hotkey": "off"}
            self.app.restart_listener()
        self.keyboard.set(RIGHT_SHIFT, False)
        self.app._start_recording = self.start_recording
        self.keyboard.set(RIGHT_ALT, True)
        self.assertTrue(wait_for(lambda: self.app._recording))
        self.assertEqual(self.app._record_mode, "dictate")
        self.assertIs(self.app._listener, self.listener)


@unittest.skipUnless(os.name == "nt", "Windows hotkeys")
class WindowsListenerTests(unittest.TestCase):
    def test_windows_polls_every_hotkey_instead_of_hooking_the_keyboard(self):
        app = ownkey.OwnkeyApp.__new__(ownkey.OwnkeyApp)
        app.cfg = dict(ownkey.DEFAULT_CONFIG)
        app._recording = False
        app._listener = None
        with patch.object(ownkey.pynput_keyboard, "Listener") as hook, \
                patch.object(windows_hotkeys.HotkeyListener, "start"):
            app.start_listener()
        hook.assert_not_called()
        self.assertIsInstance(app._listener, windows_hotkeys.HotkeyListener)
        self.assertEqual(set(windows_hotkeys.HOTKEY_VKS), set(ownkey.HOTKEY_LIST))
        for name, vk in windows_hotkeys.HOTKEY_VKS.items():
            key = app._listener._keys[vk]
            self.assertIn(key, app._resolve_pynput_keys(name))
            self.assertEqual(key.value.vk, vk)

    def test_reads_physical_key_state(self):
        self.assertIs(windows_hotkeys.async_key_down()(0x87), False)  # F24 is not held


if __name__ == "__main__":
    unittest.main()
