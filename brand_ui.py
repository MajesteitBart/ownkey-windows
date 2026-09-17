"""Ownkey brand widget kit for tkinter.

Palette, type, and controls follow the website (ownkey.bvdm.ai) and the
brand book in assets/. Everything draws on plain tk widgets so the settings
window needs no extra runtime.
"""

from __future__ import annotations

import base64
import ctypes
import glob
import io
import math
import os
import sys
import tkinter as tk
from tkinter import font as tkfont, ttk

try:
    from PIL import Image, ImageDraw
except Exception:  # Pillow ships with Ownkey; without it the kit draws plain canvas shapes
    Image = ImageDraw = None

KEY = "#0E0E0E"        # keycap black, window background
GRAPHITE = "#171717"   # cards and panels
SLATE = "#202020"      # inputs, elevated controls
LINE = "#2C2C2C"       # borders
HAIRLINE = "#3A3A3A"   # kbd chips, toggle track
BONE = "#F3F1EC"       # primary text
ASH = "#8E8A7F"        # muted text
ORANGE = "#DE5F14"     # voice / active / signal
AMBER = "#F4A23C"      # warm highlight
GREEN = "#9DCB3B"      # confirmations
RED = "#E2574B"        # errors
WHITE = "#FFFFFF"

_FONTS_LOADED = set()


def load_fonts(directory: str) -> None:
    """Register bundled TTF files for this process only (Windows)."""
    if not sys.platform.startswith("win"):
        return
    for path in glob.glob(os.path.join(directory, "*.ttf")):
        path = os.path.abspath(path)
        if path in _FONTS_LOADED:
            continue
        try:
            ctypes.windll.gdi32.AddFontResourceExW(path, 0x10, 0)  # FR_PRIVATE
            _FONTS_LOADED.add(path)
        except Exception:
            pass


class Type:
    """Resolved font tuples: Bricolage Grotesque for display, system sans for body."""

    def __init__(self, root: tk.Misc):
        try:
            families = set(tkfont.families(root))
        except tk.TclError:
            families = set()

        def pick(*names, default):
            return next((name for name in names if name in families), default)

        base = pick("Segoe UI", "Cantarell", "DejaVu Sans", default="TkDefaultFont")
        heavy = pick("Bricolage Grotesque ExtraBold", "Bricolage Grotesque", default=base)
        bold = pick("Bricolage Grotesque", default=base)
        semibold = pick("Bricolage Grotesque SemiBold", "Bricolage Grotesque", default=base)
        mono = pick("JetBrains Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono", default="Courier New")
        self.wordmark = (heavy, 18, "bold")
        self.title = (bold, 17, "bold")
        self.heading = (semibold, 11, "bold" if semibold == base else "normal")
        self.nav = (semibold, 10, "bold" if semibold == base else "normal")
        self.button = (semibold, 10, "bold" if semibold == base else "normal")
        self.body = (base, 10)
        self.strong = (base, 10, "bold")
        self.small = (base, 9)
        self.mono = (mono, 8)
        self.mono_body = (mono, 9)
        self.kbd = (mono, 9)


def font_option(font) -> str:
    """Font description for the Tk option database.

    The family goes in braces. Unbraced, "Segoe UI 10" reads as family "Segoe"
    with size "UI", and every widget that takes its font from the option
    database fails to build.
    """
    family, *rest = font if isinstance(font, (tuple, list)) else (font,)
    return " ".join(["{%s}" % family, *(str(part) for part in rest)])


def _style_combobox(root: tk.Misc, style: ttk.Style, type_: Type) -> None:
    """A dropdown that looks like one: a chevron instead of the boxed arrow of
    the clam theme, a hover border, a hand cursor, and a list in brand colours."""
    style.configure("TCombobox", fieldbackground=SLATE, background=SLATE, bordercolor=LINE,
                    arrowcolor=BONE, arrowsize=14, lightcolor=SLATE, darkcolor=SLATE, foreground=BONE,
                    selectbackground=SLATE, selectforeground=BONE, padding=(8, 7), font=type_.body)
    style.map("TCombobox", fieldbackground=[("disabled", GRAPHITE), ("readonly", SLATE)],
              foreground=[("disabled", ASH), ("readonly", BONE)],
              bordercolor=[("disabled", LINE), ("focus", ORANGE), ("hover", ASH)],
              arrowcolor=[("disabled", LINE)],
              selectbackground=[("readonly", SLATE)], selectforeground=[("readonly", BONE)])
    # Settings is a new Toplevel on every open, but elements, class bindings and
    # the option database belong to the interpreter. Set those up once, and keep
    # the chevron images on the Tk root: the element only borrows them, and an
    # image dies with the last Python reference to it.
    owner = root._root()
    if not getattr(owner, "_ownkey_combobox_ready", False):
        chevron = ((9, 6), (14, 11), (19, 6))
        images = {name: smooth_image(owner, 28, 17, background, strokes=[(chevron, color, 1.7)])
                  for name, color, background in (("normal", ASH, SLATE), ("active", BONE, SLATE),
                                                  ("disabled", HAIRLINE, GRAPHITE))}
        if all(images.values()) and "Ownkey.Combobox.chevron" not in style.element_names():
            owner._ownkey_chevrons = images
            style.element_create("Ownkey.Combobox.chevron", "image", images["normal"],
                                 ("disabled", images["disabled"]), ("pressed", images["active"]),
                                 ("hover", images["active"]), ("focus", images["active"]), sticky="")
        # A Tcl script, so no Python callback is registered per window.
        owner.tk.call("bind", "TCombobox", "<Enter>",
                      "+%W configure -cursor [expr {[%W instate disabled] ? {arrow} : {hand2}}]")
        # The page scrolls under the pointer. A wheel turn must not also change
        # the value of whatever field happens to be there.
        for widget_class in ("TCombobox", "TSpinbox"):
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                owner.unbind_class(widget_class, sequence)
        for option, value in (("background", SLATE), ("foreground", BONE), ("selectBackground", ORANGE),
                              ("selectForeground", KEY), ("font", font_option(type_.body)), ("borderWidth", 6),
                              ("highlightThickness", 0), ("relief", "flat"), ("activeStyle", "none")):
            owner.option_add(f"*TCombobox*Listbox.{option}", value)
        owner._ownkey_combobox_ready = True
    if "Ownkey.Combobox.chevron" in style.element_names():
        style.layout("TCombobox", [("Combobox.field", {"sticky": "nswe", "children": [
            ("Ownkey.Combobox.chevron", {"side": "right", "sticky": "ns"}),
            ("Combobox.padding", {"sticky": "nswe", "children": [("Combobox.textarea", {"sticky": "nswe"})]}),
        ]})])
    style.configure("ComboboxPopdownFrame", relief="solid", borderwidth=1, bordercolor=HAIRLINE,
                    lightcolor=HAIRLINE, darkcolor=HAIRLINE, background=SLATE)
    # The scrollbar inside the list uses the default style.
    style.layout("Vertical.TScrollbar", [
        ("Vertical.Scrollbar.trough", {"children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})], "sticky": "ns"})])
    # In the clam theme arrowsize is the bar's thickness, also without arrows.
    style.configure("Vertical.TScrollbar", troughcolor=SLATE, background=HAIRLINE, bordercolor=SLATE,
                    lightcolor=HAIRLINE, darkcolor=HAIRLINE, arrowsize=8, gripcount=0, relief="flat")
    style.map("Vertical.TScrollbar", background=[("active", ASH)])


def style_ttk(root: tk.Misc, type_: Type) -> ttk.Style:
    """Theme the few ttk widgets that tk cannot draw itself."""
    style = ttk.Style(root)
    style.theme_use("clam")
    _style_combobox(root, style, type_)
    style.configure("TSpinbox", fieldbackground=SLATE, background=SLATE, bordercolor=LINE,
                    arrowcolor=ASH, lightcolor=SLATE, darkcolor=SLATE, foreground=BONE,
                    selectbackground=ORANGE, selectforeground=KEY, padding=(8, 4), arrowsize=12)
    style.map("TSpinbox", bordercolor=[("focus", ORANGE)])
    style.configure("Ownkey.Horizontal.TProgressbar", troughcolor=SLATE, background=ORANGE,
                    bordercolor=SLATE, lightcolor=ORANGE, darkcolor=ORANGE, thickness=6)
    style.layout("Ownkey.Vertical.TScrollbar", [
        ("Vertical.Scrollbar.trough", {"children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})], "sticky": "ns"})])
    # arrowsize is the thickness here too; 0 left a bar of one invisible pixel.
    style.configure("Ownkey.Vertical.TScrollbar", troughcolor=KEY, background=HAIRLINE, bordercolor=KEY,
                    lightcolor=HAIRLINE, darkcolor=HAIRLINE, arrowsize=6, gripcount=0, relief="flat")
    style.map("Ownkey.Vertical.TScrollbar", background=[("active", ASH)])
    return style


def rounded_points(x1, y1, x2, y2, radius, steps=6):
    """Polygon points for a rounded rectangle with exact circular corners."""
    radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    corners = ((x2 - radius, y1 + radius, -90), (x2 - radius, y2 - radius, 0),
               (x1 + radius, y2 - radius, 90), (x1 + radius, y1 + radius, 180))
    points = []
    for cx, cy, start in corners:
        for step in range(steps + 1):
            angle = math.radians(start + 90 * step / steps)
            points.extend((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def round_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    return canvas.create_polygon(rounded_points(x1, y1, x2, y2, radius), **kwargs)


SUPERSAMPLE = 8


def _rgb(widget: tk.Misc, color: str) -> tuple:
    return tuple(channel // 257 for channel in widget.winfo_rgb(color))


def smooth_image(master, width, height, background, shapes=(), strokes=()):
    """Antialiased shapes on a solid background as a PhotoImage; None without Pillow.

    The Tk canvas draws polygons and ovals without antialiasing, which shows as
    stair-steps on a bone pill against the black window. Here the shapes are
    drawn SUPERSAMPLE times larger and reduced with an exact box filter, so
    straight edges stay sharp and only the curves blend.

    shapes:  (x1, y1, x2, y2, radius, fill, outline) rounded rectangles in canvas
             coordinates. Empty fill or outline means none; the outline is 1 px.
    strokes: (points, color, width) open polylines with round ends.
    """
    if Image is None:
        return None
    try:
        return _render(master, width, height, background, shapes, strokes)
    except Exception:
        # A broken Pillow (a frozen build without its PNG plugin, say) costs the
        # smooth edges, never the Settings window: callers draw polygons instead.
        return None


def _render(master, width, height, background, shapes, strokes):
    scale = SUPERSAMPLE
    width, height = max(1, int(round(width))), max(1, int(round(height)))
    image = Image.new("RGB", (width * scale, height * scale), _rgb(master, background))
    draw = ImageDraw.Draw(image)
    for x1, y1, x2, y2, radius, fill, outline in shapes:
        box = (round(x1 * scale), round(y1 * scale), round(x2 * scale) - 1, round(y2 * scale) - 1)
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        radius = max(0, min(radius * scale, (box[2] - box[0]) / 2, (box[3] - box[1]) / 2))
        draw.rounded_rectangle(box, radius=radius, fill=_rgb(master, fill) if fill else None,
                               outline=_rgb(master, outline) if outline else None,
                               width=scale if outline else 0)
    for points, color, line_width in strokes:
        scaled = [(x * scale, y * scale) for x, y in points]
        ink, half = _rgb(master, color), line_width * scale / 2
        draw.line(scaled, fill=ink, width=max(1, round(line_width * scale)), joint="curve")
        for x, y in (scaled[0], scaled[-1]):
            draw.ellipse((x - half, y - half, x + half, y + half), fill=ink)
    buffer = io.BytesIO()
    image.reduce(scale).save(buffer, "PNG")
    return tk.PhotoImage(master=master, data=base64.b64encode(buffer.getvalue()))


class Card(tk.Canvas):
    """Graphite panel with a 1px line border and rounded corners.

    Put content in ``card.inner`` (a plain frame). The card follows the
    content height and stretches to the width it is packed or gridded at.
    """

    def __init__(self, master, *, fill=GRAPHITE, border=LINE, radius=16, padding=(20, 14), **kwargs):
        parent_bg = kwargs.pop("bg", None) or master.cget("bg")
        super().__init__(master, bg=parent_bg, highlightthickness=0, bd=0, **kwargs)
        self.fill, self.border, self.radius, self.padding = fill, border, radius, padding
        self.inner = tk.Frame(self, bg=fill)
        self._window = self.create_window(padding[0], padding[1], window=self.inner, anchor="nw")
        self._shape = None
        self._corners = None
        self.inner.bind("<Configure>", self._layout)
        self.bind("<Configure>", self._layout)

    def _corner_images(self):
        """Four antialiased corner tiles. The card is too large to redraw as one
        image on every resize, so the polygon keeps the body and the tiles cover
        its stair-stepped corners."""
        if self._corners is None:
            tile = int(math.ceil(self.radius)) + 2
            far = 4 * tile  # the far sides fall outside the tile: only one corner shows
            self._corners = tuple(
                smooth_image(self, tile, tile, self.cget("bg"),
                             [(1 if west else tile - far, 1 if north else tile - far,
                               far if west else tile - 1, far if north else tile - 1,
                               self.radius, self.fill, self.border)])
                for north, west in ((True, True), (True, False), (False, True), (False, False)))
        return self._corners

    def _layout(self, _event=None):
        width = self.winfo_width()
        height = self.inner.winfo_reqheight() + 2 * self.padding[1]
        if int(self.cget("height")) != height:
            self.configure(height=height)
        self.itemconfigure(self._window, width=max(1, width - 2 * self.padding[0]))
        self.delete("shape")
        self._shape = round_rect(self, 1, 1, width - 2, height - 2, self.radius,
                                 fill=self.fill, outline=self.border, width=1, tags="shape")
        corners = self._corner_images()
        if all(corners) and width > 4 * self.radius and height > 2 * self.radius + 4:
            for image, x, y, anchor in zip(corners, (0, width, 0, width), (0, 0, height, height),
                                           ("nw", "ne", "sw", "se")):
                self.create_image(x, y, image=image, anchor=anchor, tags="shape")
        self.tag_lower("shape")


class Toggle(tk.Canvas):
    """Switch bound to a BooleanVar. Orange means on, as in the brand's active state."""

    WIDTH, HEIGHT, PAD = 42, 24, 3

    def __init__(self, master, variable: tk.BooleanVar, command=None, **kwargs):
        super().__init__(master, width=self.WIDTH, height=self.HEIGHT, bg=master.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2", **kwargs)
        self.variable, self.command = variable, command
        self._enabled = True
        self._images = {}
        self._position = 1.0 if variable.get() else 0.0
        self._trace = variable.trace_add("write", lambda *_: self._animate())
        self.bind("<Button-1>", self._click)
        self.bind("<space>", self._click)
        self.bind("<Destroy>", lambda _e: self._forget_trace(), add="+")
        self._draw()

    def _forget_trace(self):
        try:
            self.variable.trace_remove("write", self._trace)
        except Exception:
            pass

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._draw()

    def _click(self, _event=None):
        if self._enabled:
            self.variable.set(not self.variable.get())
            if self.command:
                self.command()

    def _animate(self):
        try:
            if not self.winfo_exists():
                return
            target = 1.0 if self.variable.get() else 0.0
            step = 0.34 if target > self._position else -0.34
            self._position = max(0.0, min(1.0, self._position + step))
            self._draw()
            if abs(self._position - target) > 0.01:
                self.after(30, self._animate)
            else:
                self._position = target
                self._draw()
        except tk.TclError:
            # The window closed mid-animation.
            pass

    def _draw(self):
        self.delete("all")
        w, h, pad = self.WIDTH, self.HEIGHT, self.PAD
        on = self._position >= 0.5
        track = ORANGE if on else HAIRLINE
        knob = BONE
        if not self._enabled:
            track, knob = (LINE if not on else "#6B3A1E"), ASH
        knob_size = h - 2 * pad
        x = pad + self._position * (w - 2 * pad - knob_size)
        key = (round(x, 2), track, knob)
        if key not in self._images:
            self._images[key] = smooth_image(self, w, h, self.cget("bg"), [
                (1, 1, w - 1, h - 1, (h - 2) / 2, track, ""),
                (x, pad, x + knob_size, pad + knob_size, knob_size / 2, knob, "")])
        if self._images[key] is not None:
            self.create_image(0, 0, image=self._images[key], anchor="nw")
            return
        round_rect(self, 1, 1, w - 1, h - 1, (h - 2) / 2, fill=track, outline="")
        self.create_oval(x, pad, x + knob_size, pad + knob_size, fill=knob, outline="")


class Button(tk.Canvas):
    """Pill button. Variants: solid (bone), ghost (line border), quiet (text only), danger."""

    def __init__(self, master, text, command=None, *, variant="ghost", font=None, padx=18, pady=7, **kwargs):
        super().__init__(master, bg=master.cget("bg"), highlightthickness=0, bd=0, cursor="hand2", **kwargs)
        self.command, self.variant = command, variant
        self.font = tkfont.Font(font=font or ("Segoe UI", 10, "bold"))
        self.padx, self.pady = padx, pady
        self._text = text
        self._state = "normal"
        self._hover = False
        self._images = {}
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", self._press)
        self.bind("<space>", self._press)
        self.bind("<Return>", self._press)
        self._resize()

    def _resize(self):
        width = self.font.measure(self._text) + 2 * self.padx
        height = self.font.metrics("linespace") + 2 * self.pady
        self.configure(width=width, height=height)
        self._draw(width, height)

    def set_text(self, text):
        if text != self._text:
            self._text = text
            self._resize()

    def cget(self, key):
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        return super().cget(key)

    def configure(self, cnf=None, **kwargs):
        if "text" in kwargs:
            self.set_text(kwargs.pop("text"))
        if "state" in kwargs:
            self._state = kwargs.pop("state")
            self.configure(cursor="hand2" if self._state == "normal" else "arrow")
            self._draw(int(self.winfo_reqwidth()), int(self.winfo_reqheight()))
        if "command" in kwargs:
            self.command = kwargs.pop("command")
        if cnf or kwargs:
            super().configure(cnf, **kwargs)

    config = configure

    def invoke(self):
        if self._state == "normal" and self.command:
            self.command()

    def _press(self, _event=None):
        self.invoke()

    def _set_hover(self, hover):
        self._hover = hover
        self._draw(int(self.winfo_reqwidth()), int(self.winfo_reqheight()))

    def _draw(self, width, height):
        self.delete("all")
        disabled = self._state != "normal"
        hover = self._hover and not disabled
        if self.variant == "solid":
            fill = ASH if disabled else (WHITE if hover else BONE)
            outline, text = fill, KEY
        elif self.variant == "danger":
            fill, outline, text = "", (LINE if disabled else (RED if hover else HAIRLINE)), (ASH if disabled else RED)
        elif self.variant == "quiet":
            fill, outline, text = "", "", (LINE if disabled else (BONE if hover else ASH))
        else:
            fill = SLATE if hover else ""
            outline, text = (LINE if disabled else (ORANGE if hover else HAIRLINE)), (ASH if disabled else BONE)
        if fill or outline:
            key = (width, height, fill, outline)
            if key not in self._images:
                if len(self._images) > 8:  # a label that keeps changing must not pile up images
                    self._images.clear()
                self._images[key] = smooth_image(self, width, height, self.cget("bg"), [
                    (1, 1, width - 1, height - 1, (height - 2) / 2, fill, "" if outline == fill else outline)])
            if self._images[key] is not None:
                self.create_image(0, 0, image=self._images[key], anchor="nw")
            else:
                round_rect(self, 1, 1, width - 1, height - 1, (height - 2) / 2,
                           fill=fill or self.cget("bg"), outline=outline or self.cget("bg"), width=1)
        self.create_text(width / 2, height / 2, text=self._text, fill=text, font=self.font)


class NavItem(tk.Canvas):
    """Sidebar entry with a rounded active state and an orange marker."""

    HEIGHT = 36

    def __init__(self, master, text, command, font, **kwargs):
        super().__init__(master, height=self.HEIGHT, bg=master.cget("bg"), highlightthickness=0,
                         bd=0, cursor="hand2", **kwargs)
        self.text, self.command, self.font = text, command, font
        self.active = False
        self._hover = False
        self._images = {}
        self.bind("<Button-1>", lambda _e: command())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Configure>", lambda _e: self._draw())

    def set_active(self, active: bool):
        self.active = active
        self._draw()

    def _set_hover(self, hover):
        self._hover = hover
        self._draw()

    def _draw(self):
        self.delete("all")
        width, height = self.winfo_width(), self.HEIGHT
        if self.active and width > 1:
            if width not in self._images:
                self._images.clear()
                self._images[width] = smooth_image(self, width, height, self.cget("bg"), [
                    (0, 0, width, height, 10, SLATE, ""), (0, 10, 3, height - 10, 1.5, ORANGE, "")])
            if self._images[width] is not None:
                self.create_image(0, 0, image=self._images[width], anchor="nw")
            else:
                round_rect(self, 0, 0, width, height, 10, fill=SLATE, outline="")
                round_rect(self, 0, 10, 3, height - 10, 1.5, fill=ORANGE, outline="")
        color = BONE if (self.active or self._hover) else ASH
        self.create_text(16, height / 2, text=self.text, anchor="w", fill=color, font=self.font)


class Entry(tk.Frame):
    """Padded input with an orange focus ring and a real placeholder.

    The placeholder is a label drawn over the empty field, so the field's text
    is never the placeholder. Text methods are delegated to the inner Entry.
    """

    def __init__(self, master, placeholder="", font=None, show="", textvariable=None,
                 bg=SLATE, border=LINE, padx=12, pady=7, **kwargs):
        super().__init__(master, bg=bg, bd=0, highlightthickness=1, highlightbackground=border,
                         highlightcolor=border)
        self._border = border
        self.variable = textvariable if textvariable is not None else tk.StringVar(master)
        self.entry = tk.Entry(self, textvariable=self.variable, bg=bg, fg=BONE, insertbackground=ORANGE,
                              relief="flat", bd=0, highlightthickness=0, selectbackground=ORANGE,
                              selectforeground=KEY, disabledbackground=bg, disabledforeground=ASH,
                              font=font, show=show, **kwargs)
        self.entry.pack(fill="both", expand=True, padx=padx, pady=pady)
        self._padx = padx
        self._placeholder = tk.Label(self, text=placeholder, bg=bg, fg=ASH, font=font, anchor="w",
                                     cursor="xterm")
        self._placeholder.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self.entry.bind("<FocusIn>", lambda _e: self._ring(True), add="+")
        self.entry.bind("<FocusOut>", lambda _e: self._ring(False), add="+")
        self.variable.trace_add("write", lambda *_a: self._refresh_placeholder())
        self._refresh_placeholder()

    # Text API of tk.Entry, forwarded to the inner widget.
    def get(self) -> str:
        return self.entry.get()

    def insert(self, index, text):
        self.entry.insert(index, text)

    def delete(self, first, last=None):
        self.entry.delete(first, last)

    def icursor(self, index):
        self.entry.icursor(index)

    def selection_clear(self):
        self.entry.selection_clear()

    def focus_set(self):
        self.entry.focus_set()

    focus = focus_set

    def bind(self, sequence=None, func=None, add=None):
        return self.entry.bind(sequence, func, add)

    def value(self) -> str:
        return self.entry.get()

    def set_value(self, text: str):
        self.entry.delete(0, tk.END)
        self.entry.insert(0, text)

    _ENTRY_OPTIONS = {"state", "show", "fg", "font", "textvariable", "insertbackground"}

    def configure(self, cnf=None, **kwargs):
        inner = {key: kwargs.pop(key) for key in list(kwargs) if key in self._ENTRY_OPTIONS}
        if inner:
            self.entry.configure(**inner)
            self._refresh_placeholder()
        if cnf is not None or kwargs:
            return super().configure(cnf, **kwargs)
        return None

    config = configure

    def cget(self, key):
        if key in self._ENTRY_OPTIONS:
            return self.entry.cget(key)
        return super().cget(key)

    def _ring(self, focused: bool):
        try:
            super().configure(highlightbackground=ORANGE if focused else self._border)
        except tk.TclError:
            pass

    def _refresh_placeholder(self):
        try:
            empty = not self.entry.get()
            enabled = str(self.entry.cget("state")) == "normal"
        except tk.TclError:
            return
        if self._placeholder.cget("text") and empty and enabled:
            self._placeholder.place(x=self._padx, rely=0.5, anchor="w")
        else:
            self._placeholder.place_forget()


class ScrollFrame(tk.Frame):
    """Vertical scrolling container; add content to ``.content``."""

    def __init__(self, master, **kwargs):
        bg = kwargs.pop("bg", master.cget("bg"))
        super().__init__(master, bg=bg, **kwargs)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                       style="Ownkey.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._on_content)
        self.canvas.bind("<Configure>", self._on_canvas)
        for widget in (self.canvas, self.content):
            widget.bind("<Enter>", lambda _e: self._bind_wheel(True))
            widget.bind("<Leave>", lambda _e: self._bind_wheel(False))

    def _on_scroll(self, first, last):
        self.scrollbar.set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            self.scrollbar.pack_forget()
        elif not self.scrollbar.winfo_ismapped():
            self.scrollbar.pack(side="right", fill="y", padx=(6, 0))

    def _on_content(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self, bind):
        if bind:
            self.canvas.bind_all("<MouseWheel>", self._wheel)
            self.canvas.bind_all("<Button-4>", self._wheel)
            self.canvas.bind_all("<Button-5>", self._wheel)
        else:
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.canvas.unbind_all(sequence)

    def _wheel(self, event):
        if not self.scrollbar.winfo_ismapped():
            return
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta, "units")

    def scroll_top(self):
        self.canvas.yview_moveto(0)


class Tag(tk.Frame):
    """Mono spec label with the site's short orange rule in front."""

    def __init__(self, master, text, font, color=ORANGE, **kwargs):
        super().__init__(master, bg=master.cget("bg"), **kwargs)
        tk.Frame(self, width=18, height=1, bg=color).pack(side="left", pady=1)
        tk.Label(self, text=text.upper(), bg=self.cget("bg"), fg=ASH, font=font).pack(side="left", padx=(8, 0))


class Kbd(tk.Label):
    """Keyboard chip like <kbd> on the site."""

    def __init__(self, master, text, font, **kwargs):
        super().__init__(master, text=text, bg=SLATE, fg=BONE, font=font, padx=8, pady=2,
                         highlightthickness=1, highlightbackground=HAIRLINE, **kwargs)


def run_smoke_test(argv, fonts_directory=None) -> int:
    """Check the widget kit inside the actual frozen runtime: no tray, no hotkeys,
    no config, no visible window. Writes a JSON report like the local smoke test."""
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    report = {"success": False, "pillow": getattr(Image, "__version__", None)}
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        if fonts_directory:
            load_fonts(fonts_directory)
        type_ = Type(root)
        style_ttk(root, type_)
        report["body_font"] = type_.body[0]
        button = Button(root, "Save", variant="solid", font=type_.button)
        toggle = Toggle(root, tk.BooleanVar(root, value=True))
        card = Card(root)
        dropdown = ttk.Combobox(root, values=("auto", "en", "nl"), state="readonly", font=type_.body)
        bar = ttk.Scrollbar(root, orient="vertical", style="Ownkey.Vertical.TScrollbar")
        for widget in (button, toggle, card, dropdown, bar):
            widget.pack()
        root.update()
        popdown = root.tk.call("ttk::combobox::PopdownWindow", dropdown)
        root.tk.call("ttk::combobox::ConfigureListbox", dropdown)  # fills the list without showing it
        report.update(
            button_shape=button.type(button.find_all()[0]),
            toggle_shape=toggle.type(toggle.find_all()[0]),
            card_corners=sum(1 for image in card._corner_images() if image is not None),
            dropdown_items=int(root.tk.call(f"{popdown}.f.l", "size")),
            dropdown_chevron="Ownkey.Combobox.chevron" in str(ttk.Style(root).layout("TCombobox")),
            scrollbar_width=bar.winfo_reqwidth(),
        )
        report["success"] = (report["button_shape"] == "image" and report["toggle_shape"] == "image"
                             and report["card_corners"] == 4
                             and report["dropdown_items"] == 3 and report["dropdown_chevron"]
                             and report["scrollbar_width"] >= 6)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    return 0 if report["success"] else 1


def divider(master, pady=(10, 10)):
    line = tk.Frame(master, height=1, bg=LINE)
    line.pack(fill="x", pady=pady)
    return line
