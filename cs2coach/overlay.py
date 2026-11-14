"""Always-on-top, dark, draggable second-screen window built on tkinter (stdlib).

It is an ordinary, separate OS window: nothing is injected into or drawn
inside CS2. It shows on top of CS2 only in "Fullscreen Windowed"/"Windowed"
mode. Exclusive fullscreen hides every other window, so put it on a second monitor.

Layout:
    ┌──────────────────────────────────────────────┐
    │            Buy full set and kit               │  <- large centred main call (rules)
    │        AI: Keep a flash for the retake        │  <- optional, smaller AI tip
    ├──────────────────────────────────────────────┤
    │ ● GSI   ● LM Studio: qwen2.5   HP 100  $4750 │  <- status bar
    └──────────────────────────────────────────────┘

Controls: left-drag = move, mouse wheel = text size, double-click = hide or
show the status bar, right-click or Esc = quit.

Thread safety: tkinter may only be touched from the thread that created it.
Other threads call ``Overlay.post(...)``, which only puts the text on a queue.
The status bar is refreshed on the UI thread by calling ``status_fn``, which
must itself be thread-safe and quick.
"""

from __future__ import annotations

import logging
import queue
import re
import sys
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable, List, Optional, Tuple

from .theme import (
    BAR_COLOR, BG_COLOR, DIVIDER_COLOR, GREEN, LEVEL_COLORS, MUTED, RED, TIP_COLOR, StatusSegment,
)

log = logging.getLogger(__name__)

DEFAULT_WIDTH = 900

StatusFn = Callable[[], List[StatusSegment]]


class Overlay:
    def __init__(
        self,
        geometry: Optional[str] = None,
        alpha: float = 0.92,
        font_size: int = 34,
        transparent_bg: bool = False,
        status_fn: Optional[StatusFn] = None,
        status_interval_ms: int = 500,
    ) -> None:
        self._queue: "queue.Queue[Tuple[str, str, str]]" = queue.Queue()
        self._status_fn = status_fn
        self._status_interval_ms = status_interval_ms
        self._drag_offset = (0, 0)
        self._status_labels: List[tk.Label] = []

        self.root = tk.Tk()
        self.root.title("CS2 AI Coach")
        self.root.overrideredirect(True)            # frameless: no title bar or borders
        self.root.attributes("-topmost", True)      # stay above other windows
        self.root.configure(bg=BG_COLOR)
        self._apply_transparency(alpha, transparent_bg)

        family = self._pick_font()
        self.big_font = tkfont.Font(family=family, size=font_size, weight="bold")
        self.small_font = tkfont.Font(family=family, size=11)
        self.tip_font = tkfont.Font(family=family, size=max(11, font_size // 2), slant="italic")

        # The single large instruction field.
        self.instruction = tk.Label(
            self.root, text="Waiting for CS2...", font=self.big_font,
            fg=LEVEL_COLORS["info"], bg=BG_COLOR, padx=24, pady=14, justify="center",
        )
        self.instruction.pack(fill="both", expand=True)

        # Optional smaller line for the AI's extra tip (hidden when empty).
        self.tip = tk.Label(
            self.root, text="", font=self.tip_font, fg=TIP_COLOR, bg=BG_COLOR,
            padx=24, pady=0, justify="center",
        )

        # Status bar: a 1-px divider, then a row of coloured segments.
        self.divider = tk.Frame(self.root, height=1, bg=DIVIDER_COLOR)
        self.divider.pack(fill="x")
        self.status_bar = tk.Frame(self.root, bg=BAR_COLOR, padx=10, pady=5)
        self.status_bar.pack(fill="x")

        self._place_window(geometry)
        self._bind_controls(self.root, self.instruction, self.tip, self.status_bar)

    # ---- public API (post() is safe to call from any thread) ---------------

    def post(self, text: str, level: str = "info", tip: str = "") -> None:
        """Queue a new instruction for display."""
        self._queue.put((text, level, tip))

    def run(self) -> None:
        """Start the Tk event loop. Blocks until the window is closed."""
        self.root.after(50, self._poll_queue)
        self.root.after(50, self._refresh_status)
        self.root.after(2000, self._keep_on_top)
        self.root.mainloop()

    def close(self) -> None:
        """Close the window. Call from the UI thread (e.g. via root.after)."""
        self.root.destroy()

    # ---- periodic UI-thread tasks -------------------------------------------

    def _poll_queue(self) -> None:
        latest = None
        try:
            while True:  # drain the queue and show only the newest message
                latest = self._queue.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            text, level, tip = latest
            self.instruction.config(text=text, fg=LEVEL_COLORS.get(level, LEVEL_COLORS["info"]))
            self._set_tip(tip)
        self.root.after(100, self._poll_queue)

    def _refresh_status(self) -> None:
        if self._status_fn is not None:
            try:
                segments = self._status_fn()
            except Exception:
                log.exception("status_fn failed")
                segments = [("status error", RED)]
            self._render_status(segments)
        self.root.after(self._status_interval_ms, self._refresh_status)

    def _render_status(self, segments: List[StatusSegment]) -> None:
        # Reuse labels; create more or remove extras only when the count changes.
        while len(self._status_labels) < len(segments):
            label = tk.Label(self.status_bar, font=self.small_font, bg=BAR_COLOR, padx=6)
            label.pack(side="left")
            self._bind_controls(label)
            self._status_labels.append(label)
        while len(self._status_labels) > len(segments):
            self._status_labels.pop().destroy()
        for label, (text, color) in zip(self._status_labels, segments):
            if label.cget("text") != text or label.cget("fg") != color:
                label.config(text=text, fg=color)

    def _set_tip(self, tip: str) -> None:
        if tip:
            self.tip.config(text=f"AI: {tip}")
            if not self.tip.winfo_ismapped():
                self.tip.pack(after=self.instruction, fill="x", pady=(0, 10))
        elif self.tip.winfo_ismapped():
            self.tip.pack_forget()

    def _keep_on_top(self) -> None:
        # Some window managers drop "topmost" when another app goes fullscreen.
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.after(2000, self._keep_on_top)

    # ---- setup helpers ------------------------------------------------------

    def _apply_transparency(self, alpha: float, transparent_bg: bool) -> None:
        if transparent_bg and sys.platform == "win32":
            # Windows only: the background colour becomes fully see-through.
            self.root.attributes("-transparentcolor", BG_COLOR)
            return
        try:
            self.root.attributes("-alpha", max(0.2, min(alpha, 1.0)))
        except tk.TclError:
            pass  # some Linux window managers don't support alpha

    def _place_window(self, geometry: Optional[str]) -> None:
        """Fixed width; the height grows with the text.

        ``geometry`` is a Tk geometry string: "900x160+1920+40" (size and position)
        or "+1920+40" (position only). x=1920 puts the window on a monitor to
        the right of a 1920-px-wide main screen; a negative x such as "+-1500+40"
        puts it on a monitor to the left.
        """
        width, min_height, pos = DEFAULT_WIDTH, 1, None
        if geometry:
            m = re.fullmatch(r"(?:(\d+)x(\d+))?([+-]-?\d+[+-]-?\d+)?", geometry.strip())
            if not m:
                raise ValueError(f"Bad geometry {geometry!r}; expected e.g. 900x160+1920+40")
            if m.group(1):
                width, min_height = int(m.group(1)), int(m.group(2))
            pos = m.group(3)
        if pos is None:
            pos = f"+{(self.root.winfo_screenwidth() - width) // 2}+40"  # top-centre

        self.instruction.config(wraplength=width - 48)  # wrap instead of cutting off
        self.tip.config(wraplength=width - 48)
        self.root.minsize(width, min_height)
        self.root.maxsize(width, 10_000)
        self.root.geometry(pos)

    def _bind_controls(self, *widgets: tk.Misc) -> None:
        for widget in widgets:
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<Button-3>", lambda e: self.close())
            widget.bind("<Double-Button-1>", self._toggle_status)
            widget.bind("<MouseWheel>", self._wheel)                        # Windows / macOS
            widget.bind("<Button-4>", lambda e: self._resize_font(+2))      # Linux scroll up
            widget.bind("<Button-5>", lambda e: self._resize_font(-2))      # Linux scroll down
        self.root.bind("<Escape>", lambda e: self.close())

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag(self, event: tk.Event) -> None:
        dx, dy = self._drag_offset
        self.root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _wheel(self, event: tk.Event) -> None:
        self._resize_font(+2 if event.delta > 0 else -2)

    def _resize_font(self, step: int) -> None:
        self.big_font.configure(size=max(12, min(96, self.big_font.cget("size") + step)))

    def _toggle_status(self, _event: tk.Event) -> None:
        if self.status_bar.winfo_ismapped():
            self.divider.pack_forget()
            self.status_bar.pack_forget()
        else:
            self.divider.pack(fill="x")
            self.status_bar.pack(fill="x")

    def _pick_font(self) -> str:
        available = set(tkfont.families(self.root))
        for name in ("Segoe UI", "Helvetica Neue", "DejaVu Sans", "Arial"):
            if name in available:
                return name
        return "TkDefaultFont"


if __name__ == "__main__":
    # Quick visual check without CS2: python -m cs2coach.overlay
    demo = Overlay(status_fn=lambda: [
        ("● CS2 GSI", GREEN), ("● LM Studio: demo-model", GREEN),
        ("HP 64   $1850", MUTED), ("AI", MUTED),
    ])
    demo.post("Buy full set and kit", "info", "Keep a flash for the retake")
    demo.run()
