import os
import queue
import threading
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

import ownkey


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


@unittest.skipUnless(os.name == "nt" or os.environ.get("DISPLAY"), "Requires a desktop session")
class SettingsLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.app = ownkey.OwnkeyApp.__new__(ownkey.OwnkeyApp)
        self.app.cfg = dict(ownkey.DEFAULT_CONFIG)
        self.model_directory = tempfile.TemporaryDirectory()
        self.app.local_models = ownkey.LocalModelManager(self.model_directory.name)
        self.app.local_transcriber = ownkey.LocalTranscriber(self.app.local_models, start_timer=False)
        self.app._ui_root = ownkey.tk.Tk()
        self.app._ui_root.withdraw()
        self.app._ui_commands = queue.Queue()
        self.app._settings = ownkey.SettingsWindow(self.app)
        self.errors = []
        self.app._ui_root.report_callback_exception = lambda *error: self.errors.append(error)

    def tearDown(self):
        self.app._settings._on_close()
        for callback in self.app._ui_root.tk.call("after", "info"):
            self.app._ui_root.after_cancel(callback)
        self.app._ui_root.destroy()
        self.app.local_transcriber.close()
        self.model_directory.cleanup()

    def test_rewrite_menu_keeps_openrouter_and_custom_but_excludes_orukeet(self):
        self.app._settings.open()
        self.app._settings.show_page("rewriting")
        rewrite_page = self.app._settings.pages["rewriting"]
        provider = next(child for child in descendants(rewrite_page)
                        if isinstance(child, ownkey.ttk.Combobox) and "OpenRouter" in child.cget("values"))
        self.assertIn("Custom (OpenAI-compatible)", provider.cget("values"))
        self.assertNotIn("Local (Orukeet)", provider.cget("values"))
        self.assertNotIn("Local (small LLM)", provider.cget("values"))
        provider.set("OpenRouter")
        provider.event_generate("<<ComboboxSelected>>")
        self.app._ui_root.update()
        self.assertEqual(self.errors, [])

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

    def test_every_page_renders_and_dictionary_entries_round_trip_into_config(self):
        self.app.cfg["vocabulary"] = ["Ownkey"]
        self.app.cfg["corrections"] = [{"from": "own key", "to": "Ownkey"}]
        self.app.apply_settings = Mock()
        with patch.object(ownkey, "set_startup"):
            self.app._settings.open()
            for name, _title, _lead in ownkey.SettingsWindow.PAGES:
                self.app._settings.show_page(name)
                self.app._ui_root.update()
                self.assertEqual(self.app._settings.current_page, name)
            self.app._settings.dictionary_entries.append({"word": "Orukeet"})
            self.app._settings.save_button.invoke()
            deadline = time.monotonic() + 1
            while not self.app.apply_settings.called and time.monotonic() < deadline:
                self.app._ui_root.update()
                time.sleep(.01)
        changes = self.app.apply_settings.call_args.args[0]
        self.assertEqual(changes["vocabulary"], ["Ownkey", "Orukeet"])
        self.assertEqual(changes["corrections"], [{"from": "own key", "to": "Ownkey"}])
        self.assertTrue(changes["remove_fillers"])
        self.assertEqual(changes["filler_languages"], ["en", "nl"])
        self.assertEqual(self.errors, [])

    def _visible_dropdowns(self):
        return [child for child in descendants(self.app._settings._win)
                if isinstance(child, ownkey.ttk.Combobox) and child.winfo_ismapped()
                and str(child.cget("state")) == "readonly"]

    def test_a_click_opens_every_dropdown_list(self):
        self.app._settings.open()
        root = self.app._ui_root
        opened = 0
        for page in ("dictation", "transcription", "rewriting", "meetings"):
            self.app._settings.show_page(page)
            root.update()
            for dropdown in self._visible_dropdowns():
                dropdown.event_generate("<Button-1>", x=20, y=dropdown.winfo_height() // 2)
                dropdown.event_generate("<ButtonRelease-1>", x=20, y=dropdown.winfo_height() // 2)
                root.update()
                popdown = f"{dropdown}.popdown"
                self.assertTrue(int(root.tk.call("winfo", "exists", popdown)), f"{page}: no list for {dropdown.get()!r}")
                self.assertTrue(int(root.tk.call("winfo", "ismapped", popdown)), f"{page}: list stayed hidden")
                self.assertEqual(int(root.tk.call(f"{popdown}.f.l", "size")), len(dropdown.cget("values")))
                root.tk.call("ttk::combobox::Unpost", dropdown)
                root.update()
                opened += 1
        self.assertGreaterEqual(opened, 5)
        self.assertEqual(self.errors, [])

    def test_the_wheel_scrolls_the_page_and_never_changes_a_field(self):
        self.app._settings.open()
        self.app._settings.show_page("transcription")
        root = self.app._ui_root
        root.update()
        dropdown = self._visible_dropdowns()[0]
        before = dropdown.get()
        for delta in (-120, -120, 120):
            dropdown.event_generate("<MouseWheel>", delta=delta)
        root.update()
        self.assertEqual(dropdown.get(), before)
        dropdown.event_generate("<Enter>")
        root.update()
        self.assertEqual(str(dropdown.cget("cursor")), "hand2")

    def test_close_during_model_refresh_does_not_touch_destroyed_widgets(self):
        with patch.object(ownkey, "list_available_models", side_effect=lambda *a: (time.sleep(.1) or ["test"])):
            self.app._settings.open()
            buttons = [w for w in descendants(self.app._settings._win)
                       if isinstance(w, ownkey.brand_ui.Button) and w.cget("text") == "Refresh"]
            buttons[0].invoke()
            self.app._settings._on_close()
            self.app._settings.open()
            deadline = time.monotonic() + .25
            while time.monotonic() < deadline:
                self.app._ui_root.update()
                time.sleep(.01)
        self.assertEqual(self.errors, [])


class EntryWidgetTests(unittest.TestCase):
    def setUp(self):
        self.root = ownkey.tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()

    def test_placeholder_is_an_overlay_and_never_the_field_text(self):
        entry = ownkey.brand_ui.Entry(self.root, placeholder="Misspelling")
        entry.pack()
        self.root.update()
        self.assertEqual(entry.value(), "")
        self.assertEqual(entry.get(), "")
        self.assertTrue(entry._placeholder.winfo_manager())
        entry.insert(0, "KinDoc")
        self.root.update()
        self.assertEqual(entry.value(), "KinDoc")
        self.assertFalse(entry._placeholder.winfo_manager())
        entry.set_value("")
        self.root.update()
        self.assertTrue(entry._placeholder.winfo_manager())
        entry.configure(state="disabled")
        self.root.update()
        self.assertFalse(entry._placeholder.winfo_manager())


@unittest.skipUnless(os.name == "nt" or os.environ.get("DISPLAY"), "Requires a desktop session")
class BrandWidgetTests(unittest.TestCase):
    def setUp(self):
        self.root = ownkey.tk.Tk()
        self.root.withdraw()
        self.ui = ownkey.brand_ui

    def tearDown(self):
        self.root.destroy()

    def test_option_database_fonts_keep_a_family_with_spaces_together(self):
        self.assertEqual(self.ui.font_option(("Segoe UI", 10)), "{Segoe UI} 10")
        self.assertEqual(self.ui.font_option(("Segoe UI", 10, "bold")), "{Segoe UI} 10 bold")
        self.ui.style_ttk(self.root, self.ui.Type(self.root))
        dropdown = ownkey.ttk.Combobox(self.root, values=("a", "b"), state="readonly")
        dropdown.pack()
        self.root.update()
        popdown = self.root.tk.call("ttk::combobox::PopdownWindow", dropdown)  # raised TclError before
        family = self.root.tk.call("font", "actual", self.root.tk.call(f"{popdown}.f.l", "cget", "-font"), "-family")
        self.assertEqual(family, self.ui.Type(self.root).body[0])

    def test_rounded_shapes_are_antialiased_images(self):
        image = self.ui.smooth_image(self.root, 40, 20, self.ui.KEY, [(1, 1, 39, 19, 9, self.ui.BONE, "")])
        self.assertEqual((image.width(), image.height()), (40, 20))
        rgb = lambda color: tuple(channel // 257 for channel in self.root.winfo_rgb(color))
        pixel = lambda x, y: tuple(image.get(x, y))
        self.assertEqual(pixel(0, 0), rgb(self.ui.KEY))
        self.assertEqual(pixel(20, 10), rgb(self.ui.BONE))
        self.assertEqual(pixel(20, 1), rgb(self.ui.BONE), "straight edges stay sharp")
        corner = {pixel(x, y) for x in range(1, 8) for y in range(1, 8)}
        self.assertGreater(len(corner - {rgb(self.ui.KEY), rgb(self.ui.BONE)}), 3, "the curve blends")

        button = self.ui.Button(self.root, "Save", variant="solid")
        toggle = self.ui.Toggle(self.root, ownkey.tk.BooleanVar(self.root, value=True))
        for widget in (button, toggle):
            widget.pack()
        self.root.update()
        self.assertEqual(button.type(button.find_all()[0]), "image")
        self.assertEqual(button.type(button.find_all()[-1]), "text")
        self.assertEqual([toggle.type(item) for item in toggle.find_all()], ["image"])
        button.configure(state="disabled")
        button._set_hover(True)
        self.assertLessEqual(len(button._images), 3)

    def test_shapes_fall_back_to_canvas_polygons_without_pillow(self):
        with patch.object(self.ui, "Image", None):
            self.assertIsNone(self.ui.smooth_image(self.root, 10, 10, self.ui.KEY))
            button = self.ui.Button(self.root, "Cancel")
            button.pack()
            self.root.update()
            self.assertEqual(button.type(button.find_all()[0]), "polygon")

    def test_scrollbars_are_wide_enough_to_see(self):
        self.ui.style_ttk(self.root, self.ui.Type(self.root))
        for style, width in (("Ownkey.Vertical.TScrollbar", 6), ("Vertical.TScrollbar", 8)):
            bar = ownkey.ttk.Scrollbar(self.root, orient="vertical", style=style)
            bar.pack()
            self.root.update()
            self.assertEqual(bar.winfo_reqwidth(), width, style)


@unittest.skipUnless(os.name == "nt", "Windows process management")
class OverlayProcessTests(unittest.TestCase):
    def test_application_closes_settings_and_background_workers_on_quit(self):
        code = """
import os
import tempfile
from unittest.mock import Mock, patch
import ownkey
manager_type = ownkey.LocalModelManager
with tempfile.TemporaryDirectory() as directory, \
        patch.object(ownkey, 'load_config', return_value=dict(ownkey.DEFAULT_CONFIG)), \
        patch.object(ownkey, 'is_startup_enabled', return_value=True), \
        patch.object(ownkey, 'LocalModelManager', side_effect=lambda **kwargs: manager_type(directory)), \
        patch.object(ownkey.pystray, 'Icon', return_value=Mock()):
    os.environ['OWNKEY_MEETINGS_LIBRARY'] = os.path.join(directory, 'meetings')  # never the real library
    app = ownkey.OwnkeyApp()
    assert app.meetings is not None, app._meetings_error
    assert str(app.meetings.store.root).startswith(directory), app.meetings.store.root
    app._overlay = Mock()
    app._tauri_overlay_exe = None
    app._open_settings()
    original = app._poll_ui_commands
    errors = []
    def poll():
        app._ui_root.report_callback_exception = lambda *error: errors.append(str(error))
        original()
        app._ui_root.after(200, app._quit)
    app._poll_ui_commands = poll
    with patch.object(app, '_start_connection_monitor'), patch.object(app, '_first_run_prompt'), \
            patch.object(app, 'start_listener'), patch.object(app, '_start_tauri_overlay'), \
            patch.object(app, '_ensure_tauri_overlay'):
        app.run()
    assert app._ui_root is None
    assert app.local_transcriber.snapshot()['state'] == 'Unloaded'
    assert not errors, errors
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)

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
