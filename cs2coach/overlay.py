"""Always-on-top floating overlay built on tkinter, which ships with Python.

It is an ordinary, separate OS window: nothing is injected into or drawn
inside CS2. That means it shows on top of CS2 only when the game runs in
"Fullscreen Windowed" or "Windowed" mode. Exclusive fullscreen hides every
other window, so in that case put the overlay on your second monitor.

Controls:
  * left-drag        move the window
  * mouse wheel      make the text bigger or smaller
  * double-click     hide or show the status line
  * right-click      quit

Thread safety: tkinter may only be touched from the thread that created it.
Other threads (the GSI server) call ``Overlay.post(...)``, which only puts a
message on a queue. The UI thread drains that queue every 100 ms.
"""

from __future__ import annotations

import queue
import re
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from typing import Optional, Tuple

# Text colours for each Advice.level
LEVEL_COLORS = {
    "info": "#7CFC9A",    # green
    "warn": "#FFC94D",    # amber
    "danger": "#FF5C5C",  # red
}
BG_COLOR = "#101418"
STATUS_COLOR = "#9AA4AE"
STALE_AFTER_S = 15.0      # GSI heartbeat is 10 s, so no data for 15 s means "lost"
DEFAULT_WIDTH = 900


class Overlay:
    def __init__(
        self,
        geometry: Optional[str] = None,
        alpha: float = 0.85,
        font_size: int = 34,
        transparent_bg: bool = False,
    ) -> None:
        self._queue: "queue.Queue[Tuple[str, str, str]]" = queue.Queue()
        self._last_update = 0.0
        self._drag_offset = (0, 0)

        self.root = tk.Tk()
        self.root.title("CS2 Coach")
        self.root.overrideredirect(True)            # no title bar or borders
        self.root.attributes("-topmost", True)      # stay above other windows
        self.root.configure(bg=BG_COLOR)
        self._apply_transparency(alpha, transparent_bg)

        self.big_font = tkfont.Font(family=self._pick_font(), size=font_size, weight="bold")
        self.small_font = tkfont.Font(family=self._pick_font(), size=12)

        # The single large instruction field.
        self.instruction = tk.Label(
            self.root,
            text="Waiting for CS2...",
            font=self.big_font,
            fg=LEVEL_COLORS["info"],
            bg=BG_COLOR,
            padx=24,
            pady=10,
            justify="center",
        )
        self.instruction.pack(fill="both", expand=True)

        # A small status line under it (map, HP, money...).
        self.status = tk.Label(
            self.root, text="", font=self.small_font, fg=STATUS_COLOR, bg=BG_COLOR, pady=4
        )
        self.status.pack(fill="x")

        self._place_window(geometry)
        self._bind_controls()

    # ---- public API (safe to call from any thread) -------------------------

    def post(self, text: str, level: str = "info", status: str = "") -> None:
        """Queue a new instruction for display."""
        self._queue.put((text, level, status))

    def run(self) -> None:
        """Start the Tk event loop. Blocks until the window is closed."""
        self.root.after(100, self._poll_queue)
        self.root.after(2000, self._keep_on_top)
        self.root.mainloop()

    # ---- internals ---------------------------------------------------------

    def _poll_queue(self) -> None:
        latest = None
        try:
            while True:  # take everything queued and show only the newest message
                latest = self._queue.get_nowait()
        except queue.Empty:
            pass

        if latest is not None:
            text, level, status = latest
            self.instruction.config(text=text, fg=LEVEL_COLORS.get(level, LEVEL_COLORS["info"]))
            self.status.config(text=status)
            self._last_update = time.monotonic()
        elif self._last_update and time.monotonic() - self._last_update > STALE_AFTER_S:
            self.status.config(text="No data from CS2. Is the game running with the .cfg installed?")

        self.root.after(100, self._poll_queue)

    def _keep_on_top(self) -> None:
        # Some window managers drop "topmost" when another app goes fullscreen. Re-assert it.
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.after(2000, self._keep_on_top)

    def _apply_transparency(self, alpha: float, transparent_bg: bool) -> None:
        if transparent_bg and sys.platform == "win32":
            # Windows only: make the background colour fully see-through, so only text shows.
            self.root.attributes("-transparentcolor", BG_COLOR)
            return
        try:
            self.root.attributes("-alpha", max(0.2, min(alpha, 1.0)))
        except tk.TclError:
            pass  # some Linux window managers don't support alpha; that's fine

    def _place_window(self, geometry: Optional[str]) -> None:
        """Position the window. The width is fixed and the height grows with the text.

        ``geometry`` is a Tk geometry string: "900x160+1920+40" (size and position)
        or "+1920+40" (position only). Position x=1920 puts the window on a monitor
        to the right of a 1920-px-wide main screen.
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

        # Long instructions wrap onto extra lines instead of being cut off.
        self.instruction.config(wraplength=width - 48)
        self.root.minsize(width, min_height)
        self.root.maxsize(width, 10_000)
        self.root.geometry(pos)

    def _bind_controls(self) -> None:
        for widget in (self.root, self.instruction, self.status):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<Button-3>", lambda e: self.root.destroy())
            widget.bind("<Double-Button-1>", self._toggle_status)
            widget.bind("<MouseWheel>", self._wheel)                        # Windows / macOS
            widget.bind("<Button-4>", lambda e: self._resize_font(+2))      # Linux scroll up
            widget.bind("<Button-5>", lambda e: self._resize_font(-2))      # Linux scroll down
        self.root.bind("<Escape>", lambda e: self.root.destroy())

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
        if self.status.winfo_ismapped():
            self.status.pack_forget()
        else:
            self.status.pack(fill="x")

    def _pick_font(self) -> str:
        available = set(tkfont.families(self.root))
        for name in ("Segoe UI", "Helvetica Neue", "DejaVu Sans", "Arial"):
            if name in available:
                return name
        return "TkDefaultFont"


if __name__ == "__main__":
    # Quick visual check without CS2: python -m cs2coach.overlay
    demo = Overlay()
    demo.post("Do: Save weapons & rotate B", "warn", "de_mirage R14  CT 7-6 T  HP 64  $1850")
    demo.run()
