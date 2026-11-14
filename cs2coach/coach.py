"""Rule-based coach: the MAIN call shown on the overlay.

Rules are deterministic, instant, and only use facts CS2 actually sends, so
they never invent things (enemy positions, map areas). The optional AI only
adds a small extra tip under this call (see validate.py).

Every rule is a plain function ``rule(snapshot) -> Advice | None``. All rules
run on every update and the Advice with the highest priority wins.

Priority bands (higher wins):
    1000 menu · 900 match state · 800 dead · 700 immediate danger
    600 bomb · 550 low HP · 500 buy phase / round over · 400 ammo · 10-30 live defaults
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from .state import GameSnapshot

# --- Tunable thresholds -----------------------------------------------------
# Rough full-buy costs: rifle + kevlar/helmet + utility (+ kit for CT).
FULL_BUY_T = 4100
FULL_BUY_CT = 4700
ECO_BELOW = 2000            # under this, saving is almost always right
DROP_ABOVE = 4500           # money left after a full kit: enough to buy a teammate a rifle
KIT_PRICE = 400
MIN_UTILITY = 2             # grenades a full buy should have
LOW_HP = 25
SAVE_HP = 40                # CT, bomb down, no kit, this HP or less: save the gun
SAVE_EQUIP = 3500           # ...if the equipment is worth at least this
HEAVY_FLASH = 100           # GSI flashed value runs 0..255
LOW_CLIP_RATIO = 0.3
LAST_ROUND_OF_HALF = 11     # MR12: money resets after this round
MATCH_POINT = 12            # MR12: 13 rounds wins


def loss_bonus(losses: int) -> int:
    """Approximate CS2 loss bonus after ``losses`` consecutive lost rounds."""
    return 1400 + 500 * (min(max(losses, 1), 5) - 1)


@dataclass(frozen=True)
class Advice:
    priority: int           # higher wins
    text: str               # what the overlay shows
    level: str = "info"     # "info" | "warn" | "danger": the overlay picks a colour from this
    key: str = ""           # stable id of the rule outcome (tests, logs, AI-tip checks)


Rule = Callable[[GameSnapshot], Optional[Advice]]


def _full_buy(s: GameSnapshot) -> int:
    return FULL_BUY_CT if s.team == "CT" else FULL_BUY_T


def _has_primary(s: GameSnapshot) -> bool:
    return s.primary is not None and s.primary.type != "Pistol"


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
# CS2 updates your money and inventory live while you buy, so this advice
# moves on step by step: buy gun/armor -> kit -> utility -> drop -> ready.

def rule_buy_phase(s: GameSnapshot) -> Optional[Advice]:
    if s.round_phase != "freezetime" or not s.has_player_data:
        return None
    a = _buy_advice(s)
    return Advice(500, a[0], a[1], key=a[2]) if a else None


def _buy_advice(s: GameSnapshot):
    full_buy = _full_buy(s)
    has_primary = _has_primary(s)

    # 1. Pistol rounds
    if s.is_pistol_round and not s.is_overtime:
        if s.armor > 0 or s.has_defuse_kit or s.money < 300:
            return ("Pistol round: play together, trade", "info", "pistol_ready")
        if s.team == "CT":
            return ("Pistol round: buy kevlar or kit", "info", "pistol")
        return ("Pistol round: buy kevlar and flash", "info", "pistol")

    # 2. Rounds where saving makes no sense
    must_spend = (
        s.map_round == LAST_ROUND_OF_HALF
        or (not s.is_overtime and s.enemy_score == MATCH_POINT)
    )
    if must_spend and not has_primary and s.money < full_buy:
        if s.map_round == LAST_ROUND_OF_HALF:
            return ("Last round of half: force buy", "warn", "force_last_round")
        return ("Enemy match point: force buy", "warn", "force_match_point")

    # 3. Already equipped: kit, armor, utility, drop
    if has_primary:
        if s.armor < 50 and s.money >= 1000:
            return ("Keep your gun, buy armor", "info", "buy_armor")
        if s.team == "CT" and not s.has_defuse_kit and s.money >= KIT_PRICE:
            return ("Buy a defuse kit", "info", "buy_kit")
        if s.utility_count < MIN_UTILITY and s.money >= 300:
            return ("Buy utility: smoke and flash", "info", "buy_utility")
        if s.money >= DROP_ABOVE:
            return ("Drop a rifle for a teammate", "info", "drop")
        return ("Ready: play your position", "info", "ready")

    # 4. Second round after a pistol round
    if s.map_round in (1, 13) and not s.is_overtime:
        won = s.round_winner(s.map_round)          # map_round == number of the pistol round
        if not won and s.map_round == 1:
            won = s.team if s.my_score == 1 else ("T" if s.team == "CT" else "CT")
        if won == s.team:
            return ("Won pistol: armor and SMG", "info", "anti_eco")
        if won:
            return ("Lost pistol: full save", "warn", "eco")

    # 5. Normal rounds: full buy, eco or save for next round
    if s.money >= full_buy:
        if s.team == "CT":
            return ("Buy full set and kit", "info", "full_buy")
        return ("Buy full set", "info", "full_buy")

    streak = s.my_loss_streak
    if streak is not None and s.money + loss_bonus(streak + 1) >= full_buy:
        # Even if this round is lost, saving guarantees a full buy next round.
        return ("Save now, full buy next round", "warn", "save_for_next")
    if s.money < ECO_BELOW:
        return ("Save money, eco round", "warn", "eco")
    return ("Follow team call: force or save", "warn", "follow_team")


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
    if s.health <= SAVE_HP and s.equip_value >= SAVE_EQUIP:
        return Advice(615, "Save your rifle, don't retake", "warn", key="save_weapon")
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


def rule_bomb_carrier(s: GameSnapshot) -> Optional[Advice]:
    if _alive_self_live(s) and s.team == "T" and s.has_bomb and s.bomb != "planted":
        return Advice(30, "Bomb carrier: stay with team", key="bomb_carrier")
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
    rule_bomb_carrier,
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
