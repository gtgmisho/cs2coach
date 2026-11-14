"""Dark-theme colours, shared by the overlay and the status bar.

Kept separate from overlay.py so console-only mode never imports tkinter.
"""

from typing import Tuple

BG_COLOR = "#0D1117"
BAR_COLOR = "#161B22"
DIVIDER_COLOR = "#30363D"
TEXT = "#E6EDF3"
MUTED = "#8B949E"
GREEN = "#3FB950"
AMBER = "#D29922"
RED = "#F85149"
TIP_COLOR = "#79C0FF"   # light blue: the optional AI tip line

# Large command text colour per Advice.level (high contrast on BG_COLOR)
LEVEL_COLORS = {
    "info": "#FFFFFF",
    "warn": "#FFC94D",
    "danger": "#FF5C5C",
}

StatusSegment = Tuple[str, str]   # (text, colour) shown in the overlay's status bar
