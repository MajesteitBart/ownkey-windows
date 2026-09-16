"""Ownkey brand widget kit for tkinter.

Palette, type, and controls follow the website (ownkey.bvdm.ai) and the
brand book in assets/. Everything draws on plain tk widgets so the settings
window needs no extra runtime.
"""

from __future__ import annotations

import ctypes
import glob
import math
import os
import sys
import tkinter as tk
from tkinter import font as tkfont, ttk

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


def style_ttk(root: tk.Misc, type_: Type) -> ttk.Style:
    """Theme the few ttk widgets that tk cannot draw itself."""
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TCombobox", fieldbackground=SLATE, background=SLATE, bordercolor=LINE,
                    arrowcolor=ASH, lightcolor=SLATE, darkcolor=SLATE, foreground=BONE,
                    selectbackground=SLATE, selectforeground=BONE, padding=(8, 5), font=type_.body)
    style.map("TCombobox", fieldbackground=[("readonly", SLATE), ("disabled", GRAPHITE)],
              foreground=[("readonly", BONE), ("disabled", ASH)],
              bordercolor=[("focus", ORANGE)], arrowcolor=[("disabled", LINE)],
              selectbackground=[("readonly", SLATE)], selectforeground=[("readonly", BONE)])
    style.configure("TSpinbox", fieldbackground=SLATE, background=SLATE, bordercolor=LINE,
                    arrowcolor=ASH, lightcolor=SLATE, darkcolor=SLATE, foreground=BONE,
                    selectbackground=ORANGE, selectforeground=KEY, padding=(8, 4), arrowsize=12)
    style.map("TSpinbox", bordercolor=[("focus", ORANGE)])
    style.configure("Ownkey.Horizontal.TProgressbar", troughcolor=SLATE, background=ORANGE,
                    bordercolor=SLATE, lightcolor=ORANGE, darkcolor=ORANGE, thickness=6)
    style.layout("Ownkey.Vertical.TScrollbar", [
        ("Vertical.Scrollbar.trough", {"children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})], "sticky": "ns"})])
    style.configure("Ownkey.Vertical.TScrollbar", troughcolor=KEY, background=LINE, bordercolor=KEY,
                    lightcolor=LINE, darkcolor=LINE, arrowsize=0, width=6, relief="flat")
    style.map("Ownkey.Vertical.TScrollbar", background=[("active", HAIRLINE)])
    root.option_add("*TCombobox*Listbox.background", SLATE)
    root.option_add("*TCombobox*Listbox.foreground", BONE)
    root.option_add("*TCombobox*Listbox.selectBackground", ORANGE)
    root.option_add("*TCombobox*Listbox.selectForeground", KEY)
    root.option_add("*TCombobox*Listbox.font", " ".join(str(part) for part in type_.body))
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
        self.inner.bind("<Configure>", self._layout)
        self.bind("<Configure>", self._layout)

    def _layout(self, _event=None):
        width = self.winfo_width()
        height = self.inner.winfo_reqheight() + 2 * self.padding[1]
        if int(self.cget("height")) != height:
            self.configure(height=height)
        self.itemconfigure(self._window, width=max(1, width - 2 * self.padding[0]))
        if self._shape is not None:
            self.delete(self._shape)
        self._shape = round_rect(self, 1, 1, width - 1, height - 1, self.radius,
                                 fill=self.fill, outline=self.border, width=1)
        self.tag_lower(self._shape)


class Toggle(tk.Canvas):
    """Switch bound to a BooleanVar. Orange means on, as in the brand's active state."""

    WIDTH, HEIGHT, PAD = 42, 24, 3

    def __init__(self, master, variable: tk.BooleanVar, command=None, **kwargs):
        super().__init__(master, width=self.WIDTH, height=self.HEIGHT, bg=master.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2", **kwargs)
        self.variable, self.command = variable, command
        self._enabled = True
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
        round_rect(self, 1, 1, w - 1, h - 1, (h - 2) / 2, fill=track, outline="")
        knob_size = h - 2 * pad
        x = pad + self._position * (w - 2 * pad - knob_size)
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
        if self.active:
            round_rect(self, 0, 0, width, height, 10, fill=SLATE, outline="")
            round_rect(self, 0, 10, 3, height - 10, 1.5, fill=ORANGE, outline="")
        color = BONE if (self.active or self._hover) else ASH
        self.create_text(16, height / 2, text=self.text, anchor="w", fill=color, font=self.font)


class Entry(tk.Entry):
    """Slate input with an orange focus ring and optional placeholder."""

    def __init__(self, master, placeholder="", font=None, show="", **kwargs):
        options = dict(bg=SLATE, fg=BONE, insertbackground=ORANGE, relief="flat",
                       highlightthickness=1, highlightbackground=LINE, highlightcolor=ORANGE,
                       selectbackground=ORANGE, selectforeground=KEY, disabledbackground=GRAPHITE,
                       disabledforeground=ASH, font=font)
        options.update(kwargs)
        super().__init__(master, **options)
        self._placeholder, self._show, self._showing = placeholder, show, False
        if show:
            self.configure(show=show)
        # The placeholder stays visible while the field has focus and is
        # empty; it disappears on the first typed or pasted character.
        self.bind("<FocusIn>", lambda _e: self.after_idle(self._park_cursor), add="+")
        self.bind("<FocusOut>", self._refresh_placeholder, add="+")
        self.bind("<KeyPress>", self._key, add="+")
        self.bind("<<Paste>>", lambda _e: self._clear_placeholder(), add="+")
        self._refresh_placeholder()

    def value(self) -> str:
        return "" if self._showing else self.get()

    def set_value(self, text: str):
        self._clear_placeholder()
        self.delete(0, tk.END)
        self.insert(0, text)
        self._refresh_placeholder()

    def _park_cursor(self):
        if self._showing:
            self.icursor(0)
            self.selection_clear()

    def _key(self, event):
        if not self._showing:
            return None
        if event.char and event.char.isprintable():
            self._clear_placeholder()
            return None
        if event.keysym in ("BackSpace", "Delete", "Left", "Right", "Home", "End"):
            return "break"
        return None

    def _clear_placeholder(self):
        if self._showing:
            self._showing = False
            self.delete(0, tk.END)
            self.configure(fg=BONE, show=self._show)

    def _refresh_placeholder(self, _event=None):
        if self._showing:
            if self.focus_get() is self or not self._placeholder:
                return
            self.delete(0, tk.END)
        if self._placeholder and not self.get() and str(self.cget("state")) == "normal":
            self._showing = True
            self.configure(fg=ASH, show="")
            self.insert(0, self._placeholder)
            self.icursor(0)


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
            self.scrollbar.pack(side="right", fill="y")

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


def divider(master, pady=(10, 10)):
    line = tk.Frame(master, height=1, bg=LINE)
    line.pack(fill="x", pady=pady)
    return line
