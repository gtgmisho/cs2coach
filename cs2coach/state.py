"""Turn a raw GSI JSON payload into a small, typed snapshot.

CS2 sends nested dictionaries, and any block may be missing (main menu,
spectating, between maps). Everything here is defensive: missing data
becomes a sensible default instead of a KeyError.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Weapon "type" values CS2 uses for things that are not real guns.
NON_GUN_TYPES = {"Knife", "Grenade", "C4", "StackableItem", "Fists", "Melee", "Tablet"}


@dataclass
class Weapon:
    name: str                       # e.g. "weapon_ak47"
    type: str = ""                  # e.g. "Rifle", "Pistol", "Knife", "Grenade"
    state: str = ""                 # "active", "holstered" or "reloading"
    ammo_clip: Optional[int] = None
    ammo_clip_max: Optional[int] = None
    ammo_reserve: Optional[int] = None

    @property
    def short_name(self) -> str:
        """'weapon_ak47' -> 'ak47'."""
        return self.name.replace("weapon_", "", 1)

    @property
    def is_gun(self) -> bool:
        return self.type not in NON_GUN_TYPES and self.ammo_clip_max is not None

    @property
    def is_active(self) -> bool:
        return self.state in ("active", "reloading")


@dataclass
class GameSnapshot:
    """One parsed GSI update. Field names mirror the GSI blocks they come from."""

    # provider
    provider_steamid: str = ""      # the steamid of whoever is running this CS2 client
    timestamp: int = 0

    # map
    map_name: str = ""
    map_mode: str = ""              # "competitive", "premier", "casual", ...
    map_phase: str = ""             # "warmup", "live", "intermission", "gameover"
    map_round: int = 0              # completed rounds so far (0 = pistol round)
    score_ct: int = 0
    score_t: int = 0

    # round
    round_phase: str = ""           # "freezetime", "live", "over"
    bomb: str = ""                  # "", "planted", "exploded", "defused"
    win_team: str = ""              # "CT" or "T" once the round is over

    # player_id
    player_steamid: str = ""
    player_name: str = ""
    team: str = ""                  # "CT" or "T"
    activity: str = ""              # "playing", "menu", "textinput"

    # player_state
    health: int = 0
    armor: int = 0
    helmet: bool = False
    flashed: int = 0                # 0..255, how blind you are
    smoked: int = 0
    burning: int = 0
    money: int = 0
    round_kills: int = 0
    equip_value: int = 0
    has_defuse_kit: bool = False

    # player_weapons
    weapons: List[Weapon] = field(default_factory=list)

    has_player_data: bool = False   # False on the main menu

    # ---- convenience views -------------------------------------------------

    @property
    def is_self(self) -> bool:
        """True if the player in the payload is you rather than someone you spectate.

        When you die, GSI's "player" block switches to whoever you are watching.
        provider.steamid is always you, so comparing the two tells them apart.
        """
        return bool(self.player_steamid) and self.player_steamid == self.provider_steamid

    @property
    def is_alive(self) -> bool:
        return self.health > 0

    @property
    def active_weapon(self) -> Optional[Weapon]:
        return next((w for w in self.weapons if w.is_active), None)

    @property
    def primary(self) -> Optional[Weapon]:
        """The best gun carried: a rifle/SMG/heavy weapon over a pistol."""
        guns = [w for w in self.weapons if w.is_gun]
        non_pistols = [w for w in guns if w.type != "Pistol"]
        if non_pistols:
            return non_pistols[0]
        return guns[0] if guns else None

    @property
    def grenades(self) -> List[str]:
        return [w.short_name for w in self.weapons if w.type == "Grenade"]

    @property
    def is_pistol_round(self) -> bool:
        # MR12: round 0 and round 12 (first round of the second half) are pistol rounds.
        return self.map_round in (0, 12)


# ---- parsing ---------------------------------------------------------------


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _opt_int(value: Any) -> Optional[int]:
    return None if value is None else _int(value)


def _parse_weapons(raw: Dict[str, Any]) -> List[Weapon]:
    # GSI sends {"weapon_0": {...}, "weapon_1": {...}}. Sort by slot number.
    def slot(key: str) -> int:
        return _int(key.rsplit("_", 1)[-1], 99)

    weapons = []
    for key in sorted(raw, key=slot):
        w = raw[key] or {}
        weapons.append(
            Weapon(
                name=str(w.get("name", "")),
                type=str(w.get("type", "")),
                state=str(w.get("state", "")),
                ammo_clip=_opt_int(w.get("ammo_clip")),
                ammo_clip_max=_opt_int(w.get("ammo_clip_max")),
                ammo_reserve=_opt_int(w.get("ammo_reserve")),
            )
        )
    return weapons


def parse_payload(payload: Dict[str, Any]) -> GameSnapshot:
    """Build a GameSnapshot from one GSI JSON payload."""
    provider = payload.get("provider") or {}
    map_ = payload.get("map") or {}
    round_ = payload.get("round") or {}
    player = payload.get("player") or {}
    state = player.get("state") or {}

    return GameSnapshot(
        provider_steamid=str(provider.get("steamid", "")),
        timestamp=_int(provider.get("timestamp")),
        map_name=str(map_.get("name", "")),
        map_mode=str(map_.get("mode", "")),
        map_phase=str(map_.get("phase", "")),
        map_round=_int(map_.get("round")),
        score_ct=_int((map_.get("team_ct") or {}).get("score")),
        score_t=_int((map_.get("team_t") or {}).get("score")),
        round_phase=str(round_.get("phase", "")),
        bomb=str(round_.get("bomb", "")),
        win_team=str(round_.get("win_team", "")),
        player_steamid=str(player.get("steamid", "")),
        player_name=str(player.get("name", "")),
        team=str(player.get("team", "")),
        activity=str(player.get("activity", "")),
        health=_int(state.get("health")),
        armor=_int(state.get("armor")),
        helmet=bool(state.get("helmet", False)),
        flashed=_int(state.get("flashed")),
        smoked=_int(state.get("smoked")),
        burning=_int(state.get("burning")),
        money=_int(state.get("money")),
        round_kills=_int(state.get("round_kills")),
        equip_value=_int(state.get("equip_value")),
        # GSI only includes "defusekit" when the player actually has one.
        has_defuse_kit=bool(state.get("defusekit", False)),
        weapons=_parse_weapons(player.get("weapons") or {}),
        has_player_data=bool(state),
    )


def format_summary(s: GameSnapshot) -> str:
    """One compact console line describing the snapshot."""
    if not s.map_name:
        return f"[menu] activity={s.activity or '?'}"

    who = "you" if s.is_self else f"spectating {s.player_name or '?'}"
    active = s.active_weapon
    if active and active.ammo_clip is not None:
        weapon = f"{active.short_name} {active.ammo_clip}/{active.ammo_clip_max}"
    else:
        weapon = active.short_name if active else "-"

    parts = [
        f"{s.map_name} R{s.map_round + 1} CT {s.score_ct}-{s.score_t} T",
        f"phase={s.round_phase or s.map_phase}",
        f"bomb={s.bomb}" if s.bomb else "",
        f"[{s.team or '?'} | {who}]",
        f"HP {s.health} AR {s.armor}{'+H' if s.helmet else ''}",
        f"${s.money}",
        f"weapon={weapon}",
        f"nades={','.join(s.grenades)}" if s.grenades else "",
        "KIT" if s.has_defuse_kit else "",
        f"FLASHED {s.flashed}" if s.flashed else "",
    ]
    return "  ".join(p for p in parts if p)
