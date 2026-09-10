"""Linux desktop integration. The application itself always runs as the user."""
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time


def is_wayland():
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ownkey"


def startup_file():
    return config_dir().parent / "autostart" / "ownkey.desktop"


def desktop_entry():
    # Desktop Entry Exec quoting also escapes field codes in paths.
    def quote(value):
        value = str(value).replace("%", "%%")
        for char in ('\\', '"', '`', '$'):
            value = value.replace(char, '\\' + char)
        return '"' + value + '"'
    script = Path(__file__).resolve().with_name("ownkey.py")
    return ("[Desktop Entry]\nType=Application\nName=Ownkey\n"
            "Comment=Push-to-talk dictation and AI rewrite\n"
            f"Exec=env PYSTRAY_BACKEND=appindicator {quote(sys.executable)} {quote(script)} --settings\n"
            f"Icon={script.parent / 'assets' / 'ownkey-app-icon-1024.png'}\n"
            "Terminal=false\nCategories=Utility;Accessibility;\n")


def set_startup(enabled):
    path = startup_file()
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(desktop_entry().replace(" --settings\n", "\n"))
    else:
        path.unlink(missing_ok=True)


class Clipboard:
    @staticmethod
    def copy(text):
        subprocess.run(["wl-copy", "--type", "text/plain;charset=utf-8"],
                       input=text.encode(), check=True, timeout=3)

    @staticmethod
    def paste():
        result = subprocess.run(["wl-paste", "--no-newline"],
                                capture_output=True, timeout=3)
        if result.returncode:
            return ""
        return result.stdout.decode("utf-8")


class KeyboardOutput:
    def __init__(self):
        self._device = None
        self._lock = threading.Lock()

    def prepare(self):
        if is_wayland():
            from evdev import UInput, ecodes
            self._device = UInput({ecodes.EV_KEY: list(range(1, 256))},
                                  name="Ownkey text output")
            # Allow the compositor to discover the virtual keyboard.
            time.sleep(.3)
        else:
            from pynput.keyboard import Controller
            self._device = Controller()

    def send(self, shortcut):
        if self._device is None:
            self.prepare()
        with self._lock:
            if is_wayland():
                from evdev import ecodes as e
                from linux_keyboard_layout import clipboard_shortcut
                control, key = clipboard_shortcut({"ctrl+c": "c", "ctrl+v": "v"}[shortcut])
                try:
                    for code in (control, key):
                        self._device.write(e.EV_KEY, code, 1)
                        self._device.syn()
                    time.sleep(.02)
                finally:
                    for code in (key, control):
                        self._device.write(e.EV_KEY, code, 0)
                        self._device.syn()
            else:
                from pynput.keyboard import Key
                with self._device.pressed(Key.ctrl):
                    self._device.press(shortcut[-1])
                    self._device.release(shortcut[-1])

    def write(self, text, delay=.005):
        if is_wayland():
            Clipboard.copy(text)
            time.sleep(.05)
            self.send("ctrl+v")
        else:
            if self._device is None:
                self.prepare()
            self._device.type(text)

    def close(self):
        if is_wayland() and self._device is not None:
            self._device.close()
            self._device = None


# Linux input codes are stable across keyboard layouts.
HOTKEY_CODES = {"right alt": 100, "right ctrl": 97, "right shift": 54,
                "f13": 183, "f14": 184, "f15": 185, "pause": 119,
                "scroll lock": 70}


class HotkeyListener(threading.Thread):
    """Observe physical keyboards without grabbing them; support hotplug."""
    def __init__(self, on_press, on_release):
        super().__init__(daemon=True)
        self.on_press = on_press
        self.on_release = on_release
        self._stop_event = threading.Event()
        self._devices = {}
        self._held = set()
        self._refresh()
        if not self._devices:
            raise RuntimeError("No readable keyboards. Run scripts/setup-linux.sh to grant "
                               "the active desktop session keyboard access.")

    def _refresh(self):
        from evdev import InputDevice, list_devices, ecodes
        for path in list_devices():
            if path in self._devices:
                continue
            try:
                device = InputDevice(path)
                keys = device.capabilities().get(ecodes.EV_KEY, [])
                if device.name != "Ownkey text output" and ecodes.KEY_A in keys:
                    self._devices[path] = device
                else:
                    device.close()
            except OSError:
                continue

    def stop(self):
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=2)
        if not self.is_alive():
            for device in self._devices.values():
                device.close()
            self._devices.clear()

    def run(self):
        from evdev import ecodes
        refresh_at = 0
        try:
            while not self._stop_event.is_set():
                if time.monotonic() >= refresh_at:
                    self._refresh()
                    refresh_at = time.monotonic() + 2
                ready, _, _ = select.select(list(self._devices.values()), [], [], .2)
                for device in ready:
                    try:
                        for event in device.read():
                            if event.type != ecodes.EV_KEY or event.code not in HOTKEY_CODES.values():
                                continue
                            token = (device.path, event.code)
                            if event.value == 1:
                                already_held = any(code == event.code for _, code in self._held)
                                self._held.add(token)
                                if not already_held:
                                    self.on_press(event.code)
                            elif event.value == 0:
                                self._held.discard(token)
                                if not any(code == event.code for _, code in self._held):
                                    self.on_release(event.code)
                    except OSError:
                        self._devices.pop(device.path, None)
                        for token in list(self._held):
                            if token[0] == device.path:
                                self._held.remove(token)
                                self.on_release(token[1])
                        device.close()
        finally:
            for device in self._devices.values():
                device.close()
            self._devices.clear()


class SingleInstance:
    """One backend per user; later launches activate the owner's Settings."""
    def __init__(self, directory=None):
        import socket
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/ownkey-{os.getuid()}"))
        self.directory = Path(directory) if directory else runtime / "ownkey"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock_file = None
        self._socket = None
        self._thread = None
        self._stop = threading.Event()
        self._address = str(self.directory / "control.sock")

    def acquire(self):
        import fcntl
        import socket
        lock = (self.directory / "instance.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return False
        self._lock_file = lock
        try:
            Path(self._address).unlink(missing_ok=True)
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.bind(self._address)
            self._socket.listen(4)
            self._socket.settimeout(.2)
        except Exception:
            self.close()
            raise
        return True

    def activate(self):
        import socket
        deadline = time.monotonic() + 3
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(1)
                    client.connect(self._address)
                    client.sendall(b"settings")
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Ownkey is already running but did not respond. Quit it and try again.")
                time.sleep(.05)

    def listen(self, on_activate):
        import socket
        def serve():
            while not self._stop.is_set():
                try:
                    client, _ = self._socket.accept()
                    with client:
                        client.settimeout(.5)
                        if client.recv(64) == b"settings":
                            on_activate()
                except (OSError, socket.timeout):
                    continue
        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._socket:
            self._socket.close()
            self._socket = None
        if self._lock_file:
            Path(self._address).unlink(missing_ok=True)
            self._lock_file.close()
            self._lock_file = None
