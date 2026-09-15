import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import linux_desktop as desktop


class LinuxDesktopTests(unittest.TestCase):
    def test_startup_uses_xdg_and_can_be_removed(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
            desktop.set_startup(True)
            path = Path(directory) / "autostart/ownkey.desktop"
            text = path.read_text()
            self.assertIn("PYSTRAY_BACKEND=appindicator", text)
            self.assertNotIn("--settings", text)
            self.assertEqual(desktop.config_dir(), Path(directory) / "ownkey")
            desktop.set_startup(False)
            desktop.set_startup(False)
            self.assertFalse(path.exists())

    def test_desktop_exec_escapes_paths(self):
        with patch.object(desktop.sys, "executable", '/tmp/a $b`c"d%/python'):
            text = desktop.desktop_entry()
        self.assertIn('"/tmp/a \\$b\\`c\\"d%%/python"', text)

    def test_wayland_clipboard_preserves_unicode_and_newlines(self):
        with patch.object(desktop.subprocess, "run", return_value=Mock(returncode=0, stdout='héllo\n'.encode())) as run:
            desktop.Clipboard.copy('héllo\n')
            self.assertEqual(run.call_args.kwargs['input'], 'héllo\n'.encode())
            self.assertEqual(desktop.Clipboard.paste(), 'héllo\n')
            self.assertIn('--no-newline', run.call_args.args[0])

    def test_empty_clipboard(self):
        with patch.object(desktop.subprocess, "run", return_value=Mock(returncode=1)):
            self.assertEqual(desktop.Clipboard.paste(), '')

    def test_wayland_text_uses_clipboard_for_unicode(self):
        output = desktop.KeyboardOutput()
        with patch.object(desktop, "is_wayland", return_value=True), \
             patch.object(desktop.Clipboard, "copy") as copy, \
             patch.object(output, "send") as send:
            output.write('日本語 €')
        copy.assert_called_once_with('日本語 €')
        send.assert_called_once_with('ctrl+v')

    def test_virtual_keyboard_releases_keys_after_write_failure(self):
        import types
        ecodes = types.SimpleNamespace(KEY_C=46, KEY_V=47, KEY_LEFTCTRL=29, EV_KEY=1)
        output = desktop.KeyboardOutput()
        output._device = Mock()
        output._device.write.side_effect = [None, OSError('test'), None, None]
        with patch.dict('sys.modules', {'evdev': types.SimpleNamespace(ecodes=ecodes)}), \
             patch.object(desktop, 'is_wayland', return_value=True), \
             patch('linux_keyboard_layout.clipboard_shortcut', return_value=(29, 47)):
            with self.assertRaises(OSError):
                output.send('ctrl+v')
        self.assertEqual(output._device.write.call_args_list[-2].args, (1, 47, 0))
        self.assertEqual(output._device.write.call_args_list[-1].args, (1, 29, 0))


@unittest.skipUnless(os.sys.platform.startswith("linux"), "Linux single-instance lock")
class SingleInstanceTests(unittest.TestCase):
    def test_second_launch_activates_owner_and_lock_is_reusable(self):
        import threading
        with tempfile.TemporaryDirectory() as directory:
            first = desktop.SingleInstance(directory)
            second = desktop.SingleInstance(directory)
            activated = threading.Event()
            try:
                self.assertTrue(first.acquire())
                first.listen(activated.set)
                self.assertFalse(second.acquire())
                second.activate()
                self.assertTrue(activated.wait(2))
                second.close()  # Must not unlink the owner's socket.
                self.assertTrue(Path(first._address).exists())
                first.close()
                replacement = desktop.SingleInstance(directory)
                try:
                    self.assertTrue(replacement.acquire())
                finally:
                    replacement.close()
            finally:
                first.close()
                second.close()

    def test_crashed_owner_does_not_leave_a_stale_lock(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            code = "import linux_desktop, os, sys; s=linux_desktop.SingleInstance(sys.argv[1]); assert s.acquire(); os._exit(0)"
            subprocess.run([sys.executable, '-c', code, directory], check=True)
            replacement = desktop.SingleInstance(directory)
            try:
                self.assertTrue(replacement.acquire())
            finally:
                replacement.close()


@unittest.skipUnless(os.sys.platform.startswith("linux") and os.environ.get("DISPLAY"),
                     "Requires a Linux desktop session")
class LinuxLifecycleTests(unittest.TestCase):
    def test_overlay_and_settings_exit_without_tcl_thread_crash(self):
        import subprocess
        import sys
        code = """
import ownkey
from unittest.mock import patch
app = ownkey.OwnkeyApp()
app._tauri_overlay_exe = None
app._open_settings()
original = app._poll_ui_commands
errors = []
def poll():
    app._ui_root.report_callback_exception = lambda *error: errors.append(str(error))
    original()
    if app._ui_root:
        app._ui_root.after(200, app._quit)
app._poll_ui_commands = poll
with patch.object(app, '_start_connection_monitor'), patch.object(app, '_first_run_prompt'), patch.object(app, 'start_listener'), patch.object(ownkey.kb, 'prepare'):
    app.run()
assert not errors, errors
"""
        result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                text=True, timeout=15,
                                env={**os.environ, 'PYSTRAY_BACKEND': 'appindicator',
                                     'OWNKEY_TAURI_OVERLAY_ONLY': '0'})
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
