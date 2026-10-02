"""Windows push-to-talk hotkeys read from physical key state."""

from __future__ import annotations

import ctypes
import queue
import threading
import traceback

# Virtual-key codes for the hotkeys offered in Settings. Only the
# left/right-specific codes tell the right-hand modifiers apart.
HOTKEY_VKS = {
    "right alt": 0xA5,  # VK_RMENU, also AltGr
    "right ctrl": 0xA3,  # VK_RCONTROL
    "right shift": 0xA1,  # VK_RSHIFT
    "f13": 0x7C,
    "f14": 0x7D,
    "f15": 0x7E,
    "pause": 0x13,
    "scroll lock": 0x91,
}
# 25 polls a second cost about 0.1% of one core; 20 ms cost 0.65%.
POLL_SECONDS = 0.04


def async_key_down():
    """Return a function reporting whether a virtual key is physically held."""
    user32 = ctypes.WinDLL("user32")
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    return lambda vk: bool(user32.GetAsyncKeyState(vk) & 0x8000)


class HotkeyListener(threading.Thread):
    """Report hotkey presses and releases by polling physical key state.

    Windows stops waiting for a low-level keyboard hook after
    LowLevelHooksTimeout, and the hook never sees the events it missed. A
    missed hotkey release left push-to-talk stuck until the key was pressed
    again. This thread only samples key state and queues each change; a
    second thread runs the callbacks in order, so a slow callback delays
    later presses and releases but cannot hide them. Other apps never wait
    for Ownkey's input handling.
    """

    def __init__(self, keys, on_press, on_release, *, key_down=None, interval=POLL_SECONDS):
        super().__init__(daemon=True, name="hotkeys")
        self.on_press = on_press
        self.on_release = on_release
        self._keys = dict(keys)
        self._key_down = key_down or async_key_down()
        self._interval = interval
        self._stop_event = threading.Event()
        self._changes = queue.Queue()

    def stop(self):
        # Callbacks may stop their own listener, so never join here.
        self._stop_event.set()
        self._changes.put(None)

    def run(self):
        threading.Thread(target=self._dispatch, daemon=True, name="hotkey-callbacks").start()
        # A key that is already held counts as a press, as key repeat did
        # for the hook.
        held = set()
        while not self._stop_event.is_set():
            for vk, key in self._keys.items():
                down = self._key_down(vk)
                if down == (vk in held):
                    continue
                if down:
                    held.add(vk)
                    self._changes.put((self.on_press, key))
                else:
                    held.discard(vk)
                    self._changes.put((self.on_release, key))
            self._stop_event.wait(self._interval)

    def _dispatch(self):
        while True:
            change = self._changes.get()
            if change is None or self._stop_event.is_set():
                return
            callback, key = change
            try:
                callback(key)
            except Exception:
                # One failed recording must not end push-to-talk.
                traceback.print_exc()
