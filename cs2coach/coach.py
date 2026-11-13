"""Instant rule-based coach: the offline fallback for the local LLM.

The overlay shows these commands while LM Studio is offline, still loading,
or hasn't answered for the current game situation yet. They are in Bulgarian
so the overlay doesn't switch language when the AI takes over.

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
        return Advice(1000, "Чакам мач...", key="menu")
    return None


def rule_warmup(s: GameSnapshot) -> Optional[Advice]:
    if s.map_phase == "warmup":
        return Advice(900, "Загрей, цели се в главата", key="warmup")
    return None


def rule_game_over(s: GameSnapshot) -> Optional[Advice]:
    if s.map_phase == "gameover":
        return Advice(900, "GG. Прегледай демото си", key="gameover")
    return None


def rule_dead_or_spectating(s: GameSnapshot) -> Optional[Advice]:
    if s.round_phase == "live" and (not s.is_self or not s.is_alive):
        return Advice(800, "Давай кратко и ясно инфо", key="dead")
    return None


# --- Rules: buy phase (freezetime) ------------------------------------------

def rule_buy_phase(s: GameSnapshot) -> Optional[Advice]:
    if s.round_phase != "freezetime":
        return None

    if s.is_pistol_round:
        if s.team == "CT":
            return Advice(500, "Купи броня или кит, играйте заедно", key="pistol")
        return Advice(500, "Купи броня и флаш, влезте заедно", key="pistol")

    if s.map_round == LAST_ROUND_OF_HALF:
        return Advice(500, "Похарчи всичко, последен рунд", "warn", key="last_round_of_half")

    full_buy = FULL_BUY_CT if s.team == "CT" else FULL_BUY_T
    has_primary = s.primary is not None and s.primary.type != "Pistol"

    if has_primary:
        if s.armor < 50 and s.money >= 1000:
            return Advice(500, "Пази оръжието, купи броня", key="keep_gun")
        return Advice(500, "Допълни гранатите", key="keep_gun")

    if s.money >= full_buy:
        if s.team == "CT" and not s.has_defuse_kit:
            return Advice(500, "Купи пълен сет и кит", key="full_buy")
        return Advice(500, "Купи пълен сет", key="full_buy")

    if s.money < ECO_BELOW:
        return Advice(500, "Пести парите, eco рунд", "warn", key="eco")

    return Advice(500, "Следвай отбора: force или save", "warn", key="follow_team")


# --- Rules: live round ------------------------------------------------------

def _alive_self_live(s: GameSnapshot) -> bool:
    return s.round_phase == "live" and s.is_self and s.is_alive


def rule_burning(s: GameSnapshot) -> Optional[Advice]:
    if _alive_self_live(s) and s.burning > 0:
        return Advice(700, "Излез от огъня!", "danger", key="burning")
    return None


def rule_flashed(s: GameSnapshot) -> Optional[Advice]:
    if _alive_self_live(s) and s.flashed >= HEAVY_FLASH:
        return Advice(690, "Ослепен си, отстъпи назад", "danger", key="flashed")
    return None


def rule_bomb_planted(s: GameSnapshot) -> Optional[Advice]:
    if not _alive_self_live(s) or s.bomb != "planted":
        return None
    if s.team == "T":
        return Advice(600, "Пази бомбата от crossfire", key="bomb_t")
    if s.has_defuse_kit:
        return Advice(600, "Изчисти ъглите, после дефузни", "warn", key="bomb_ct_kit")
    return Advice(610, "Без кит! Ретейк само с време", "danger", key="bomb_ct_no_kit")


def rule_low_hp(s: GameSnapshot) -> Optional[Advice]:
    if _alive_self_live(s) and s.health <= LOW_HP:
        return Advice(550, "Малко HP, избягвай дуели", "warn", key="low_hp")
    return None


def rule_low_ammo(s: GameSnapshot) -> Optional[Advice]:
    if not _alive_self_live(s):
        return None
    w = s.active_weapon
    if not w or not w.is_gun or not w.ammo_clip_max or w.state == "reloading":
        return None
    if w.ammo_clip / w.ammo_clip_max <= LOW_CLIP_RATIO and (w.ammo_reserve or 0) > 0:
        return Advice(400, "Презареди зад прикритие", "warn", key="low_ammo")
    return None


def rule_live_default(s: GameSnapshot) -> Optional[Advice]:
    if s.round_phase == "live":
        return Advice(10, "Играй до тиймейт, трейдвай", key="live")
    return None


# --- Rules: round over ------------------------------------------------------

def rule_round_over(s: GameSnapshot) -> Optional[Advice]:
    if s.round_phase != "over":
        return None
    if s.win_team and s.win_team == s.team:
        return Advice(500, "Спечелен рунд, същият план", key="round_won")
    return Advice(500, "Виж парите на отбора", key="round_lost")


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

FALLBACK = Advice(0, "Мерника на нивото на главата", key="fallback")


class Coach:
    """Runs every rule and returns the single most important piece of advice."""

    def __init__(self, rules: Optional[List[Rule]] = None) -> None:
        self.rules = list(rules) if rules is not None else list(RULES)

    def advise(self, snapshot: GameSnapshot) -> Advice:
        candidates = [a for a in (rule(snapshot) for rule in self.rules) if a is not None]
        return max(candidates, key=lambda a: a.priority, default=FALLBACK)
