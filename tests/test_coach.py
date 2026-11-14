"""Unit tests for parsing, rules, triggers and the GSI server.

Run from the project folder:  python -m unittest discover -s tests -v
"""

import copy
import json
import os
import re
import sys
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from cs2coach.coach import Coach  # noqa: E402
from cs2coach.gsi_server import GSIServer  # noqa: E402
from cs2coach.state import parse_payload  # noqa: E402
from cs2coach.trigger import AnalysisTrigger, situation_key  # noqa: E402
from send_test_payload import BASE, TOKEN, scripted_round  # noqa: E402


def payload(**changes):
    """Copy of BASE with dotted-path overrides, e.g. payload(**{"round.phase": "live"})."""
    p = copy.deepcopy(BASE)
    for path, value in changes.items():
        *parents, leaf = path.split(".")
        node = p
        for part in parents:
            node = node[part]
        node[leaf] = value
    return p


class ParseTests(unittest.TestCase):
    def test_parses_base_payload(self):
        s = parse_payload(BASE)
        self.assertEqual(s.map_name, "de_mirage")
        self.assertEqual(s.round_phase, "freezetime")
        self.assertEqual(s.money, 5200)
        self.assertTrue(s.is_self)
        self.assertEqual(s.active_weapon.short_name, "usp_silencer")
        self.assertFalse(s.has_defuse_kit)
        self.assertEqual((s.kills, s.assists, s.deaths, s.mvps), (4, 1, 3, 1))

    def test_empty_payload_is_safe(self):
        s = parse_payload({})
        self.assertEqual(s.map_name, "")
        self.assertIsNone(s.active_weapon)
        self.assertEqual(Coach().advise(s).key, "menu")


class CoachTests(unittest.TestCase):
    def test_scripted_round_advice(self):
        coach = Coach()
        keys = [coach.advise(parse_payload(p)).key for p in scripted_round()]
        self.assertEqual(keys, [
            "full_buy", "live", "flashed", "live", "low_ammo",
            "low_hp", "save_weapon", "dead", "round_lost",
        ])

    def test_eco(self):
        s = parse_payload(payload(**{"player.state.money": 1200}))
        self.assertEqual(Coach().advise(s).key, "eco")

    def test_spectating_after_death(self):
        s = parse_payload(payload(**{"round.phase": "live", "player.steamid": "someone_else"}))
        self.assertEqual(Coach().advise(s).key, "dead")

    def key(self, **changes):
        return Coach().advise(parse_payload(payload(**changes))).key

    def test_buy_phase_step_by_step(self):
        """CS2 updates inventory live while buying; the call moves on each step."""
        rifle = {"name": "weapon_m4a1", "type": "Rifle", "state": "active",
                 "ammo_clip": 30, "ammo_clip_max": 30, "ammo_reserve": 90}
        smoke = {"name": "weapon_smokegrenade", "type": "Grenade", "state": "holstered"}
        flash = {"name": "weapon_flashbang", "type": "Grenade", "state": "holstered"}
        w = dict(BASE["player"]["weapons"], weapon_2=rifle)
        armored = {"player.state.armor": 100, "player.state.helmet": True}
        self.assertEqual(self.key(**{"player.weapons": w, "player.state.money": 2200}), "buy_armor")
        self.assertEqual(self.key(**{"player.weapons": w, "player.state.money": 1200, **armored}), "buy_kit")
        kit = {"player.state.defusekit": True, **armored}
        self.assertEqual(self.key(**{"player.weapons": w, "player.state.money": 800, **kit}), "buy_utility")
        w2 = dict(w, weapon_3=smoke, weapon_4=flash)
        self.assertEqual(self.key(**{"player.weapons": w2, "player.state.money": 5000, **kit}), "drop")
        self.assertEqual(self.key(**{"player.weapons": w2, "player.state.money": 300, **kit}), "ready")

    def test_loss_bonus_save_for_next_round(self):
        # $2500 now; after another loss (3rd in a row) the bonus gets CT to a full buy.
        self.assertEqual(self.key(**{"player.state.money": 2500, "map.team_ct.consecutive_round_losses": 2}),
                         "save_for_next")
        # Without loss-streak data the coach falls back to "follow the team".
        self.assertEqual(self.key(**{"player.state.money": 2500}), "follow_team")

    def test_force_rounds(self):
        self.assertEqual(self.key(**{"player.state.money": 2500, "map.round": 11}), "force_last_round")
        self.assertEqual(self.key(**{"player.state.money": 2500, "map.team_t.score": 12}), "force_match_point")

    def test_pistol_rounds(self):
        self.assertEqual(self.key(**{"map.round": 0, "player.state.money": 800}), "pistol")
        self.assertEqual(self.key(**{"map.round": 0, "player.state.money": 150, "player.state.armor": 100}),
                         "pistol_ready")
        self.assertEqual(self.key(**{"map.round": 24}), "full_buy")          # overtime: no pistol round
        won = {"map.round": 1, "player.state.money": 3000, "map.round_wins": {"1": "ct_win_elimination"}}
        lost = {"map.round": 1, "player.state.money": 3000, "map.round_wins": {"1": "t_win_bomb"}}
        self.assertEqual(self.key(**won), "anti_eco")
        self.assertEqual(self.key(**lost), "eco")
        self.assertEqual(self.key(**{"map.round": 1, "player.state.money": 3000,
                                     "map.team_ct.score": 0, "map.team_t.score": 1}), "eco")

    def test_live_round_rules(self):
        live = {"round.phase": "live"}
        self.assertEqual(self.key(**live, **{"player.state.burning": 200}), "burning")
        self.assertEqual(self.key(**live, **{"round.bomb": "planted", "player.state.defusekit": True}),
                         "bomb_ct_kit")
        c4 = dict(BASE["player"]["weapons"], weapon_5={"name": "weapon_c4", "type": "C4", "state": "holstered"})
        self.assertEqual(self.key(**live, **{"player.team": "T", "player.weapons": c4}), "bomb_carrier")
        self.assertEqual(self.key(**live, **{"player.team": "T", "round.bomb": "planted"}), "bomb_t")

    def test_all_texts_are_short(self):
        import inspect
        from cs2coach import coach
        texts = set(re.findall(r'\("([^"]+)", "(?:info|warn|danger)"', inspect.getsource(coach)))
        texts |= set(re.findall(r'Advice\(\d+, "([^"]+)"', inspect.getsource(coach)))
        self.assertGreater(len(texts), 25)
        for text in texts:
            self.assertLessEqual(len(text.split()), 6, text)


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.t = AnalysisTrigger(periodic_s=5.0, hp_drop=25, min_gap_s=1.0)
        self.live = payload(**{"round.phase": "live"})

    def check(self, p, now):
        return self.t.check(parse_payload(p), now)

    def test_menu_never_triggers(self):
        self.assertIsNone(self.check({}, 0))

    def test_first_then_quiet(self):
        self.assertEqual(self.check(self.live, 0), "first data")
        self.assertIsNone(self.check(self.live, 1))
        self.assertIsNone(self.check(payload(**{"round.phase": "live", "player.state.money": 999}), 2))

    def test_phase_change(self):
        self.check(BASE, 0)
        self.assertIn("freezetime -> live", self.check(self.live, 0.5))

    def test_bomb_change(self):
        self.check(self.live, 0)
        self.assertIn("planted", self.check(payload(**{"round.phase": "live", "round.bomb": "planted"}), 0.2))

    def test_hp_drop_is_cumulative_and_debounced(self):
        self.check(self.live, 0)
        hp = lambda v: payload(**{"round.phase": "live", "player.state.health": v})  # noqa: E731
        self.assertIsNone(self.check(hp(80), 0.5))       # -20: not significant yet
        self.assertIsNone(self.check(hp(70), 0.9))       # -30, but within the 1 s min gap
        self.assertEqual(self.check(hp(70), 2.0), "HP 100 -> 70")

    def test_periodic_only_when_alive_and_live(self):
        self.check(self.live, 0)
        self.assertIsNone(self.check(self.live, 4.9))
        self.assertEqual(self.check(self.live, 5.0), "periodic")
        frozen = payload()
        self.check(frozen, 6)
        self.assertIsNone(self.check(frozen, 20))   # no periodic spam in freezetime

    def test_death(self):
        self.check(self.live, 0)
        self.assertEqual(self.check(payload(**{"round.phase": "live", "player.state.health": 0}), 0.1), "died")

    def test_situation_key_changes(self):
        a = situation_key(parse_payload(self.live))
        b = situation_key(parse_payload(payload(**{"round.phase": "live", "round.bomb": "planted"})))
        c = situation_key(parse_payload(payload(**{"round.phase": "live", "player.state.money": 1})))
        self.assertNotEqual(a, b)
        self.assertEqual(a, c)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.server = GSIServer(self.received.append, port=0, token=TOKEN)
        self.server.start_in_background()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self, body):
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status

    def test_accepts_valid_payload(self):
        self.assertEqual(self._post(BASE), 200)
        # The server answers CS2 *before* handing the payload on (so CS2 never
        # waits on us), so allow a moment for the hand-off to happen.
        deadline = time.monotonic() + 5
        while not self.received and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.received), 1)

    def test_rejects_wrong_token(self):
        bad = dict(BASE, auth={"token": "nope"})
        # assertLogs captures the server's expected warning so it isn't printed.
        with self.assertLogs("cs2coach.gsi_server", level="WARNING") as logs, \
                self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(bad)
        ctx.exception.close()  # release the response, avoids a ResourceWarning
        self.assertEqual(ctx.exception.code, 403)
        self.assertIn("auth token", logs.output[0])
        self.assertEqual(self.received, [])


if __name__ == "__main__":
    unittest.main()
