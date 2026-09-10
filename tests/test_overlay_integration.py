"""Opt in with OWNKEY_TEST_OVERLAY_EXE pointing to a freshly built overlay."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import ownkey


@unittest.skipUnless(os.environ.get("OWNKEY_TEST_OVERLAY_EXE"),
                     "Requires a desktop overlay build")
class OverlayIntegrationTests(unittest.TestCase):
    def test_overlay_exits_on_parent_crash_and_can_restart(self):
        executable = str(Path(os.environ["OWNKEY_TEST_OVERLAY_EXE"]).resolve())
        for _ in range(2):
            parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            overlay = None
            try:
                overlay = subprocess.Popen([executable],
                    env={**os.environ, "OWNKEY_PARENT_PID": str(parent.pid)},
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                time.sleep(1.5)
                self.assertIsNone(overlay.poll(), "Overlay exited while parent was alive")
                parent.kill()
                parent.wait(timeout=5)
                self.assertEqual(overlay.wait(timeout=5), 0)
            finally:
                for process in (overlay, parent):
                    if process and process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)

    @unittest.skipUnless(sys.platform.startswith("linux") and os.environ.get("DISPLAY"),
                         "Requires Linux XWayland/X11 desktop")
    def test_linux_overlay_transparency_focus_and_hide(self):
        import socket
        from Xlib import X, display
        from PIL import Image
        executable = str(Path(os.environ["OWNKEY_TEST_OVERLAY_EXE"]).resolve())
        connection = display.Display()
        root = connection.screen().root
        overlay = subprocess.Popen([executable],
            env={**os.environ, "OWNKEY_PARENT_PID": str(os.getpid())})
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            window = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                self.assertIsNone(overlay.poll(), "Overlay crashed during startup")
                sender.sendto(b'{"visible":true,"connection":"online","listening":"listening","level":0.6}',
                              ("127.0.0.1", 38485))
                clients = root.get_full_property(connection.intern_atom('_NET_CLIENT_LIST'), X.AnyPropertyType)
                for wid in clients.value if clients is not None else []:
                    candidate = connection.create_resource_object('window', wid)
                    pid = candidate.get_full_property(connection.intern_atom('_NET_WM_PID'), X.AnyPropertyType)
                    if pid is not None and pid.value[0] == overlay.pid:
                        window = candidate
                if window and window.get_attributes().map_state == X.IsViewable:
                    break
                time.sleep(.1)
            self.assertIsNotNone(window, "Overlay did not appear")
            time.sleep(.5)  # Allow the webview to render and GTK draw handlers to run.
            self.assertEqual(window.get_wm_hints().input, 0, "Overlay must never accept focus")
            active = root.get_full_property(connection.intern_atom('_NET_ACTIVE_WINDOW'), X.AnyPropertyType)
            self.assertNotEqual(list(active.value) if active else [], [window.id])
            geometry = window.get_geometry()
            capture = window.get_image(0, 0, geometry.width, geometry.height, X.ZPixmap, 0xffffffff)
            self.assertEqual(capture.depth, 32)
            pixels = Image.frombytes('RGBA', (geometry.width, geometry.height), capture.data, 'raw', 'BGRA')
            self.assertEqual(pixels.getpixel((0, 0))[3], 0, "Opaque rectangle behind the pill")
            self.assertEqual(pixels.getchannel('A').getextrema(), (0, 255), "Pill did not render")
            sender.sendto(b'{"visible":false}', ("127.0.0.1", 38485))
            time.sleep(.5)
            self.assertNotEqual(window.get_attributes().map_state, X.IsViewable)
        finally:
            sender.close()
            if overlay.poll() is None:
                overlay.terminate()
                overlay.wait(timeout=5)
            connection.close()

    @unittest.skipUnless(os.name == "nt", "Windows orphan cleanup")
    def test_old_orphan_cleanup_is_limited_to_matching_path(self):
        executable = Path(os.environ["OWNKEY_TEST_OVERLAY_EXE"]).resolve()
        with tempfile.TemporaryDirectory() as directory:
            test_executable = Path(directory) / "ownkey-overlay.exe"
            shutil.copy2(executable, test_executable)
            # Simulate an old backend that launches an overlay without a parent monitor.
            environment = dict(os.environ)
            environment.pop("OWNKEY_PARENT_PID", None)
            parent = subprocess.run([sys.executable, "-c",
                "import subprocess, sys; p = subprocess.Popen([sys.argv[1]], "
                "creationflags=subprocess.CREATE_NO_WINDOW); print(p.pid)", str(test_executable)],
                env=environment, capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(parent.returncode, 0, parent.stderr)
            pid = int(parent.stdout.strip())
            kernel32 = ownkey.ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ownkey.wintypes.DWORD, ownkey.wintypes.BOOL, ownkey.wintypes.DWORD]
            kernel32.OpenProcess.restype = ownkey.wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [ownkey.wintypes.HANDLE, ownkey.wintypes.DWORD]
            kernel32.CloseHandle.argtypes = [ownkey.wintypes.HANDLE]
            kernel32.TerminateProcess.argtypes = [ownkey.wintypes.HANDLE, ownkey.wintypes.UINT]
            handle = kernel32.OpenProcess(0x00100001, False, pid)
            self.assertTrue(handle)
            try:
                time.sleep(1)
                ownkey.stop_orphaned_tauri_overlays(str(Path(directory) / "different.exe"))
                self.assertEqual(kernel32.WaitForSingleObject(handle, 0), 258)
                ownkey.stop_orphaned_tauri_overlays(str(test_executable))
                self.assertEqual(kernel32.WaitForSingleObject(handle, 5000), 0)
            finally:
                if kernel32.WaitForSingleObject(handle, 0) == 258:
                    kernel32.TerminateProcess(handle, 1)
                    kernel32.WaitForSingleObject(handle, 5000)
                kernel32.CloseHandle(handle)
