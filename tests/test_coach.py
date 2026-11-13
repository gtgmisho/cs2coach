"""Unit tests: python -m unittest discover -s tests"""

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from cs2coach.coach import Coach  # noqa: E402
from cs2coach.gsi_server import GSIServer  # noqa: E402
from cs2coach.state import parse_payload  # noqa: E402
from send_test_payload import BASE, TOKEN, scripted_round  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_parses_base_payload(self):
        s = parse_payload(BASE)
        self.assertEqual(s.map_name, "de_mirage")
        self.assertEqual(s.round_phase, "freezetime")
        self.assertEqual(s.money, 5200)
        self.assertTrue(s.is_self)
        self.assertEqual(s.active_weapon.short_name, "usp_silencer")
        self.assertFalse(s.has_defuse_kit)

    def test_empty_payload_is_safe(self):
        s = parse_payload({})
        self.assertEqual(s.map_name, "")
        self.assertIsNone(s.active_weapon)
        self.assertEqual(Coach().advise(s).text, "Waiting for a match...")


class CoachTests(unittest.TestCase):
    def test_scripted_round_advice(self):
        coach = Coach()
        texts = [coach.advise(parse_payload(p)).text for p in scripted_round()]
        self.assertTrue(texts[0].startswith("Full buy"))
        self.assertTrue(texts[2].startswith("Blind"))
        self.assertTrue(texts[4].startswith("Low ammo"))
        self.assertTrue(texts[5].startswith("19 HP"))
        self.assertIn("NO KIT", texts[6])
        self.assertTrue(texts[7].startswith("Dead"))
        self.assertTrue(texts[8].startswith("Round lost"))

    def test_eco(self):
        p = json.loads(json.dumps(BASE))
        p["player"]["state"]["money"] = 1200
        self.assertTrue(Coach().advise(parse_payload(p)).text.startswith("Eco"))

    def test_spectating_after_death(self):
        p = json.loads(json.dumps(BASE))
        p["round"]["phase"] = "live"
        p["player"]["steamid"] = "someone_else"
        self.assertTrue(Coach().advise(parse_payload(p)).text.startswith("Dead"))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.server = GSIServer(self.received.append, port=0, token=TOKEN)
        self.server.start_in_background()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self, payload):
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status

    def test_accepts_valid_payload(self):
        self.assertEqual(self._post(BASE), 200)
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
