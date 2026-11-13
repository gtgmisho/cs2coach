"""Decide WHEN the local LLM should analyse the game (filtering and debouncing).

CS2 sends a payload every time anything changes (ammo count, money,
crosshair...), as often as every 0.5 s. Sending each one to an LLM would queue
up stale requests and keep the GPU busy. ``AnalysisTrigger.check`` returns a
reason string only for *meaningful* changes:

* first data from a match, map change, map phase change (warmup -> live)
* round phase change (freezetime -> live -> over) or a new round
* bomb state change (planted / defused / exploded)
* side switch at half time
* you died / you are alive again
* a significant HP drop since the last analysis
* every N seconds while you are alive in a live round

Call ``check`` for each payload, and also about once a second from a timer
(CS2 can go quiet for up to its 10 s heartbeat, but the periodic trigger
should still fire).
"""

from __future__ import annotations

import math
from typing import Optional

from .state import GameSnapshot


def situation_key(s: GameSnapshot) -> tuple:
    """Identifies the game situation an AI command was produced for.

    When this key changes, an older command is stale and is discarded.
    """
    return (s.map_name, s.map_round, s.round_phase, s.bomb, s.is_self and s.is_alive)


class AnalysisTrigger:
    def __init__(self, periodic_s: float = 5.0, hp_drop: int = 25, min_gap_s: float = 1.0) -> None:
        self.periodic_s = periodic_s     # re-analyse this often during a live round
        self.hp_drop = hp_drop           # HP lost since last analysis that counts as "significant"
        self.min_gap_s = min_gap_s       # debounce for HP-drop triggers
        self._last: Optional[GameSnapshot] = None
        self._last_time = -math.inf

    def check(self, s: GameSnapshot, now: float) -> Optional[str]:
        """Return why an analysis should run now, or None. Records the trigger."""
        reason = self._reason(s, now)
        if reason:
            self._last = s
            self._last_time = now
        return reason

    def _reason(self, s: GameSnapshot, now: float) -> Optional[str]:
        if not s.map_name or not s.has_player_data:
            return None                  # main menu / loading: nothing to analyse

        prev = self._last
        if prev is None:
            return "first data"
        if s.map_name != prev.map_name:
            return f"map {s.map_name}"
        if s.map_phase != prev.map_phase:
            return f"map phase {prev.map_phase} -> {s.map_phase}"
        if s.map_round != prev.map_round or s.round_phase != prev.round_phase:
            return f"round phase {prev.round_phase} -> {s.round_phase}"
        if s.bomb != prev.bomb:
            return f"bomb {s.bomb or 'reset'}"
        if s.team != prev.team:
            return f"side switch -> {s.team}"

        alive = s.is_self and s.is_alive
        was_alive = prev.is_self and prev.is_alive
        if alive != was_alive:
            return "alive" if alive else "died"

        since_last = now - self._last_time
        if alive and since_last >= self.min_gap_s and prev.health - s.health >= self.hp_drop:
            return f"HP {prev.health} -> {s.health}"

        if (
            alive
            and s.map_phase == "live"
            and s.round_phase == "live"
            and since_last >= self.periodic_s
        ):
            return "periodic"
        return None
