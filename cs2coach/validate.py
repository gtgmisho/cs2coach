"""Reject AI tips that don't fit the actual game state.

Small local models write confident advice about things they cannot know.
A tip is only shown if it passes every check here; otherwise the overlay
shows the rule-based call alone. ``check_tip`` returns None if the tip is OK,
or a short reason why it was rejected (printed to the console).
"""

from __future__ import annotations

import re
from typing import Optional

from .coach import Advice
from .state import GameSnapshot

# GSI has NO positions, so any map area or movement between areas is invented.
_SITE = re.compile(r"(?<![\w'])[AB](?![\w'])")  # a standalone capital A or B
_LOCATION_WORDS = re.compile(
    r"\b(site|sites|mid|middle|long|short|banana|apps|apartments|connector|palace|ramp|"
    r"heaven|hell|tunnels?|catwalk|cat|jungle|window|pit|ivy|lower|upper|spawn|"
    r"rotate|rotating|rotation|flank|flanking|lurk|lurking)\b",
    re.I,
)
_BUY = re.compile(r"\b(buy|buying|purchase|force|full[- ]?buy|upgrade|drop)\b", re.I)
_SAVE_MONEY = re.compile(r"\b(eco|save money|save cash|don'?t buy|no buy)\b", re.I)
_DEFUSE = re.compile(r"\b(defuse|defusing|retake|retaking)\b", re.I)
_DEFUSE_NOW = re.compile(r"\b(defuse|defusing)\b", re.I)
_PLANT = re.compile(r"\b(plant|planting)\b", re.I)
_ALIVE_ACTIONS = re.compile(
    r"\b(peek|push|plant|defuse|retake|reload|hold|shoot|spray|jump|buy|rush|entry|clutch)\b", re.I,
)

_AGGRESSIVE = re.compile(
    r"\b(peek|peeking|push|pushing|rush|rushing|entry|swing|challenge|duel|retake|retaking|aggressive)\b",
    re.I,
)

_ECO_CALLS = {"eco", "save_for_next"}
_STAY_ALIVE_CALLS = {"save_weapon", "low_hp", "flashed", "burning", "bomb_ct_no_kit"}
_BUY_CALLS = {"full_buy", "force_last_round", "force_match_point", "anti_eco", "pistol"}


def check_tip(tip: str, s: GameSnapshot, main: Advice) -> Optional[str]:
    """None if the tip may be shown, else the reason it was rejected."""
    text = tip.strip()
    if not text or text.upper().startswith("NONE"):
        return "no tip"
    if text.lower() == main.text.lower():
        return "repeats the main call"

    if _SITE.search(text) or _LOCATION_WORDS.search(text):
        return "mentions a map area (the coach has no position data)"

    alive_live = s.round_phase == "live" and s.is_self and s.is_alive
    if s.round_phase == "live" and not alive_live and _ALIVE_ACTIONS.search(text):
        return "action for an alive player, but you are dead"

    if _AGGRESSIVE.search(text) and main.key in _STAY_ALIVE_CALLS:
        return "aggressive tip while the main call says stay alive"

    if _BUY.search(text) and s.round_phase == "live":
        return "buy advice during a live round"
    if _BUY.search(text) and main.key in _ECO_CALLS:
        return "contradicts the save call"
    if _SAVE_MONEY.search(text) and main.key in _BUY_CALLS:
        return "contradicts the buy call"

    if _DEFUSE.search(text) and s.team != "CT":
        return "defuse/retake advice on the T side"
    if _DEFUSE_NOW.search(text) and s.bomb != "planted":
        return "defuse advice without a planted bomb"
    if _PLANT.search(text) and not (s.team == "T" and s.bomb != "planted" and alive_live):
        return "plant advice that doesn't fit"
    return None
