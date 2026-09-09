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


@unittest.skipUnless(os.name == "nt" and os.environ.get("OWNKEY_TEST_OVERLAY_EXE"),
                     "Requires a Windows overlay build")
class OverlayIntegrationTests(unittest.TestCase):
    def test_overlay_exits_on_parent_crash_and_can_restart(self):
        executable = str(Path(os.environ["OWNKEY_TEST_OVERLAY_EXE"]).resolve())
        for _ in range(2):
            parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            overlay = None
            try:
                overlay = subprocess.Popen([executable],
                    env={**os.environ, "OWNKEY_PARENT_PID": str(parent.pid)},
                    creationflags=subprocess.CREATE_NO_WINDOW)
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
                creationflags=subprocess.CREATE_NO_WINDOW)
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
