import sys
import unittest
from unittest.mock import Mock, patch

import linux_keyboard_layout as layout
import linux_desktop as desktop


@unittest.skipUnless(sys.platform.startswith('linux'), 'Requires Linux XKB rules')
class XkbShortcutTests(unittest.TestCase):
    def test_us_and_dvorak_resolve_different_physical_keys(self):
        self.assertEqual(layout.resolve_shortcut('us', '', '', 'c'), (29, 46))
        self.assertEqual(layout.resolve_shortcut('us', '', '', 'v'), (29, 47))
        self.assertEqual(layout.resolve_shortcut('us', 'dvorak', '', 'c'), (29, 23))
        self.assertEqual(layout.resolve_shortcut('us', 'dvorak', '', 'v'), (29, 52))

    def test_control_remapping_is_respected(self):
        self.assertEqual(layout.resolve_shortcut('us', '', 'ctrl:swapcaps', 'v'), (58, 47))

    def test_source_is_read_again_for_each_shortcut(self):
        with patch.object(layout, 'active_layout', side_effect=[('us', '', ''), ('us', 'dvorak', '')]):
            self.assertEqual(layout.clipboard_shortcut('v'), (29, 47))
            self.assertEqual(layout.clipboard_shortcut('v'), (29, 52))

    def test_unsupported_layout_does_not_guess_a_key_position(self):
        with self.assertRaisesRegex(RuntimeError, 'Latin input source'):
            layout.resolve_shortcut('ru', '', '', 'v')


class KeyboardOutputLayoutTests(unittest.TestCase):
    def test_send_uses_resolved_control_and_letter_keys(self):
        import types
        output = desktop.KeyboardOutput()
        output._device = Mock()
        with patch.dict('sys.modules', {'evdev': types.SimpleNamespace(ecodes=types.SimpleNamespace(EV_KEY=1))}), \
             patch.object(desktop, 'is_wayland', return_value=True), \
             patch.object(layout, 'clipboard_shortcut', return_value=(58, 52)) as resolve:
            output.send('ctrl+v')
        resolve.assert_called_once_with('v')
        self.assertEqual([call.args for call in output._device.write.call_args_list],
                         [(1, 58, 1), (1, 52, 1), (1, 52, 0), (1, 58, 0)])

    def test_lookup_failure_does_not_press_any_keys(self):
        import types
        output = desktop.KeyboardOutput()
        output._device = Mock()
        with patch.dict('sys.modules', {'evdev': types.SimpleNamespace(ecodes=types.SimpleNamespace(EV_KEY=1))}), \
             patch.object(desktop, 'is_wayland', return_value=True), \
             patch.object(layout, 'clipboard_shortcut', side_effect=RuntimeError('No active layout')):
            with self.assertRaisesRegex(RuntimeError, 'No active layout'):
                output.send('ctrl+v')
        output._device.write.assert_not_called()
