"""Instant rule-based coach: the offline fallback for the local LLM.

The overlay shows these commands while LM Studio is offline, still loading,
or hasn't answered for the current game situation yet. They follow the same
style as the AI: short, starting with an action verb.

Every rule is a plain function ``rule(snapshot) -> Advice | None``. All rules
run on every update, and the Advice with the highest priority wins. The
``level`` also sets the overlay's text colour for AI commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from .state import GameSnapshot

# --- Tunable thresholds -----------------------------------------------------
# Rough "full buy" costs: rifle + kevlar/helmet + some utility (+ kit for CT).
FULL_BUY_T = 4100
FULL_BUY_CT = 4700
ECO_BELOW = 2000            # under this, saving is usually right
LOW_HP = 25
HEAVY_FLASH = 100           # GSI flashed value runs 0..255
LOW_CLIP_RATIO = 0.3
LAST_ROUND_OF_HALF = 11     # MR12: money resets after this round


@dataclass(frozen=True)
class Advice:
    priority: int           # higher wins
    text: str               # what the overlay shows
    level: str = "info"     # "info" | "warn" | "danger": the overlay picks a colour from this
    key: str = ""           # stable id of the rule outcome (used by tests and logs)


Rule = Callable[[GameSnapshot], Optional[Advice]]


# --- Rules: match-level -----------------------------------------------------

def rule_not_in_match(s: GameSnapshot) -> Optional[Advice]:
    if not s.map_name:
        return Advice(1000, "Waiting for a match...", key="menu")
    return None


def rule_warmup(s: GameSnapshot) -> Optional[Advice]:
    if s.map_phase == "warmup":
        return Advice(900, "Warm up: aim for heads", key="warmup")
    return None


def rule_game_over(s: GameSnapshot) -> Optional[Advice]:
    if s.map_phase == "gameover":
        return Advice(900, "GG. Review your demo", key="gameover")
    return None


def rule_dead_or_spectating(s: GameSnapshot) -> Optional[Advice]:
    if s.round_phase == "live" and (not s.is_self or not s.is_alive):
        return Advice(800, "Dead: give short, clear info", key="dead")
    return None


# --- Rules: buy phase (freezetime) ------------------------------------------

def rule_buy_phase(s: GameSnapshot) -> Optional[Advice]:
    if s.round_phase != "freezetime":
        return None

    if s.is_pistol_round:
        if s.team == "CT":
            return Advice(500, "Buy kevlar or kit, play together", key="pistol")
        return Advice(500, "Buy kevlar and flash, hit together", key="pistol")

    if s.map_round == LAST_ROUND_OF_HALF:
        return Advice(500, "Spend everything, last round of half", "warn", key="last_round_of_half")

    full_buy = FULL_BUY_CT if s.team == "CT" else FULL_BUY_T
    has_primary = s.primary is not None and s.primary.type != "Pistol"

    if has_primary:
        if s.armor < 50 and s.money >= 1000:
            return Advice(500, "Keep your gun, buy armor", key="keep_gun")
        return Advice(500, "Top up your utility", key="keep_gun")

    if s.money >= full_buy:
        if s.team == "CT" and not s.has_defuse_kit:
            return Advice(500, "Buy full set and kit", key="full_buy")
        return Advice(500, "Buy full set", key="full_buy")

    if s.money < ECO_BELOW:
        return Advice(500, "Save money, eco round", "warn", key="eco")

    return Advice(500, "Follow team call: force or save", "warn", key="follow_team")


# --- Rules: live round ------------------------------------------------------

def _alive_self_live(s: GameSnapshot) -> bool:
    return s.round_phase == "live" and s.is_self and s.is_alive


def rule_burning(s: GameSnapshot) -> Optional[Advice]:
    if _alive_self_live(s) and s.burning > 0:
        return Advice(700, "Get out of the fire!", "danger", key="burning")
    return None


def rule_flashed(s: GameSnapshot) -> Optional[Advice]:
    if _alive_self_live(s) and s.flashed >= HEAVY_FLASH:
        return Advice(690, "Blind: fall back now", "danger", key="flashed")
    return None


def rule_bomb_planted(s: GameSnapshot) -> Optional[Advice]:
    if not _alive_self_live(s) or s.bomb != "planted":
        return None
    if s.team == "T":
        return Advice(600, "Guard the bomb, hold crossfire", key="bomb_t")
    if s.has_defuse_kit:
        return Advice(600, "Clear angles, then defuse", "warn", key="bomb_ct_kit")
    return Advice(610, "No kit! Retake only with time", "danger", key="bomb_ct_no_kit")


def rule_low_hp(s: GameSnapshot) -> Optional[Advice]:
    if _alive_self_live(s) and s.health <= LOW_HP:
        return Advice(550, "Low HP, avoid duels", "warn", key="low_hp")
    return None


def rule_low_ammo(s: GameSnapshot) -> Optional[Advice]:
    if not _alive_self_live(s):
        return None
    w = s.active_weapon
    if not w or not w.is_gun or not w.ammo_clip_max or w.state == "reloading":
        return None
    if w.ammo_clip / w.ammo_clip_max <= LOW_CLIP_RATIO and (w.ammo_reserve or 0) > 0:
        return Advice(400, "Reload behind cover", "warn", key="low_ammo")
    return None


def rule_live_default(s: GameSnapshot) -> Optional[Advice]:
    if s.round_phase == "live":
        return Advice(10, "Stay near a teammate, trade", key="live")
    return None


# --- Rules: round over ------------------------------------------------------

def rule_round_over(s: GameSnapshot) -> Optional[Advice]:
    if s.round_phase != "over":
        return None
    if s.win_team and s.win_team == s.team:
        return Advice(500, "Round won, same plan", key="round_won")
    return Advice(500, "Check your team's money", key="round_lost")


# Order doesn't matter: priority decides. Keep it grouped for readability.
RULES: List[Rule] = [
    rule_not_in_match,
    rule_warmup,
    rule_game_over,
    rule_dead_or_spectating,
    rule_buy_phase,
    rule_burning,
    rule_flashed,
    rule_bomb_planted,
    rule_low_hp,
    rule_low_ammo,
    rule_live_default,
    rule_round_over,
]

FALLBACK = Advice(0, "Keep crosshair at head level", key="fallback")


class Coach:
    """Runs every rule and returns the single most important piece of advice."""

    def __init__(self, rules: Optional[List[Rule]] = None) -> None:
        self.rules = list(rules) if rules is not None else list(RULES)

    def advise(self, snapshot: GameSnapshot) -> Advice:
        candidates = [a for a in (rule(snapshot) for rule in self.rules) if a is not None]
        return max(candidates, key=lambda a: a.priority, default=FALLBACK)
