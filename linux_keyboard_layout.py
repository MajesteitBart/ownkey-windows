"""Resolve GNOME Wayland shortcuts using its current IBus source and XKB rules."""
import ctypes
from functools import lru_cache


def active_layout():
    # GNOME updates the global IBus engine for XKB sources too, including
    # per-window source switches. XWayland's group can be stale while a native
    # Wayland window has focus, so do not read its keyboard state here.
    import gi
    gi.require_version("IBus", "1.0")
    from gi.repository import Gio, IBus
    bus = IBus.Bus.new()
    engine = bus.get_global_engine() if bus.is_connected() else None
    if engine is None or engine.get_layout() in (None, "", "default"):
        raise RuntimeError("Cannot determine the active keyboard layout from IBus. "
                           "Select an XKB input source in GNOME Settings and try again.")
    options = list(Gio.Settings.new("org.gnome.desktop.input-sources").get_strv("xkb-options"))
    if engine.get_layout_option():
        options.append(engine.get_layout_option())
    return engine.get_layout(), engine.get_layout_variant() or "", ",".join(options)


class _RuleNames(ctypes.Structure):
    _fields_ = [(field, ctypes.c_char_p) for field in
                ("rules", "model", "layout", "variant", "options")]


@lru_cache(maxsize=1)
def _xkb():
    lib = ctypes.CDLL("libxkbcommon.so.0")
    pointer, uint = ctypes.c_void_p, ctypes.c_uint32
    signatures = {
        "xkb_context_new": ([ctypes.c_int], pointer),
        "xkb_context_unref": ([pointer], None),
        "xkb_keymap_new_from_names": ([pointer, ctypes.POINTER(_RuleNames), ctypes.c_int], pointer),
        "xkb_keymap_unref": ([pointer], None),
        "xkb_state_new": ([pointer], pointer),
        "xkb_state_unref": ([pointer], None),
        "xkb_state_key_get_one_sym": ([pointer, uint], uint),
        "xkb_state_update_key": ([pointer, uint, ctypes.c_int], ctypes.c_int),
        "xkb_keymap_key_by_name": ([pointer, ctypes.c_char_p], uint),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(lib, name)
        function.argtypes = arguments
        function.restype = result
    return lib


@lru_cache(maxsize=64)
def resolve_shortcut(layout, variant, options, letter):
    """Return evdev Control and letter codes; honor Ctrl-specific layout rules."""
    if letter not in ("c", "v"):
        raise ValueError(f"Unsupported clipboard shortcut: {letter}")
    lib = _xkb()
    context = lib.xkb_context_new(0)
    keymap = state = None
    try:
        if not context:
            raise RuntimeError("Cannot initialize XKB")
        names = _RuleNames(b"evdev", b"pc105", layout.encode(), variant.encode(), options.encode())
        keymap = lib.xkb_keymap_new_from_names(context, ctypes.byref(names), 0)
        if not keymap:
            raise RuntimeError(f"Cannot load XKB layout {layout} ({variant})")
        state = lib.xkb_state_new(keymap)
        if not state:
            raise RuntimeError("Cannot initialize XKB keyboard state")
        # XKB evdev keycodes are Linux input codes + 8. The output device
        # advertises keyboard keys 1..255, not mouse/gamepad button codes.
        codes = range(9, 264)
        preferred = lib.xkb_keymap_key_by_name(keymap, b"LCTL")
        control = next((code for code in [preferred, *codes]
                        if code in codes and lib.xkb_state_key_get_one_sym(state, code)
                        in (0xffe3, 0xffe4)), None)  # Control_L / Control_R
        if control is None:
            raise RuntimeError("The active layout has no usable Control key")
        lib.xkb_state_update_key(state, control, 1)  # XKB_KEY_DOWN
        key = next((code for code in codes
                    if lib.xkb_state_key_get_one_sym(state, code)
                    in (ord(letter), ord(letter.upper()))), None)
        if key is None:
            raise RuntimeError(f"The active layout has no Ctrl+{letter.upper()} shortcut; "
                               "select a Latin input source and try again.")
        return control - 8, key - 8
    finally:
        if state:
            lib.xkb_state_unref(state)
        if keymap:
            lib.xkb_keymap_unref(keymap)
        if context:
            lib.xkb_context_unref(context)


def clipboard_shortcut(letter):
    return resolve_shortcut(*active_layout(), letter)
