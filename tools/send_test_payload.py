#!/usr/bin/env python3
"""Send fake GSI payloads to the coach, so you can test it without launching CS2.

    python tools/send_test_payload.py              # play a short scripted round
    python tools/send_test_payload.py --delay 0.5  # faster
    python tools/send_test_payload.py --file my_payload.json   # replay a saved payload

The payloads use the same shape CS2 sends with the provided .cfg.
"""

from __future__ import annotations

import argparse
import copy
import json
import time
import urllib.request
from typing import Any, Dict, List

ME = "76561198000000001"
TOKEN = "cs2coach_local_token"

BASE: Dict[str, Any] = {
    "provider": {"name": "Counter-Strike 2", "appid": 730, "version": 14000,
                 "steamid": ME, "timestamp": 0},
    "map": {"mode": "competitive", "name": "de_mirage", "phase": "live", "round": 5,
            "team_ct": {"score": 3}, "team_t": {"score": 2}},
    "round": {"phase": "freezetime"},
    "player": {
        "steamid": ME, "name": "You", "team": "CT", "activity": "playing",
        "state": {"health": 100, "armor": 0, "helmet": False, "flashed": 0, "smoked": 0,
                  "burning": 0, "money": 5200, "round_kills": 0, "round_killhs": 0,
                  "equip_value": 200},
        "weapons": {
            "weapon_0": {"name": "weapon_knife", "type": "Knife", "state": "holstered"},
            "weapon_1": {"name": "weapon_usp_silencer", "type": "Pistol", "state": "active",
                         "ammo_clip": 12, "ammo_clip_max": 12, "ammo_reserve": 24},
        },
    },
    "auth": {"token": TOKEN},
}

M4 = {"name": "weapon_m4a1", "type": "Rifle", "state": "active",
      "ammo_clip": 30, "ammo_clip_max": 30, "ammo_reserve": 90}


def scripted_round() -> List[Dict[str, Any]]:
    """A short CT round: buy, fight, take damage, bomb planted with no kit, lose."""
    frames = []

    def frame(mutator) -> None:
        p = copy.deepcopy(frames[-1] if frames else BASE)
        mutator(p)
        p["provider"]["timestamp"] = int(time.time())
        frames.append(p)

    frame(lambda p: None)  # buy phase, $5200 -> full buy

    def bought(p):
        st = p["player"]["state"]
        st.update(money=1000, armor=100, helmet=True, equip_value=4200)
        p["player"]["weapons"]["weapon_1"]["state"] = "holstered"
        p["player"]["weapons"]["weapon_2"] = dict(M4)
        p["player"]["weapons"]["weapon_3"] = {"name": "weapon_smokegrenade", "type": "Grenade",
                                              "state": "holstered"}
        p["round"]["phase"] = "live"
    frame(bought)

    frame(lambda p: p["player"]["state"].update(flashed=230))
    frame(lambda p: p["player"]["state"].update(flashed=0))
    frame(lambda p: p["player"]["weapons"]["weapon_2"].update(ammo_clip=6))
    frame(lambda p: p["player"]["state"].update(health=19, armor=60))
    frame(lambda p: p["round"].update(bomb="planted"))

    def lost(p):
        p["round"].update(phase="over", bomb="exploded", win_team="T")
        p["map"]["team_t"]["score"] = 3
    frame(lambda p: p["player"]["state"].update(health=0))
    frame(lost)
    return frames


def send(url: str, payload: Dict[str, Any]) -> int:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:3000/")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between payloads")
    ap.add_argument("--file", help="send this one JSON payload instead of the scripted round")
    args = ap.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            frames = [json.load(fh)]
    else:
        frames = scripted_round()

    for i, payload in enumerate(frames, 1):
        print(f"[{i}/{len(frames)}] POST -> HTTP {send(args.url, payload)}")
        if i < len(frames):
            time.sleep(args.delay)


if __name__ == "__main__":
    main()
