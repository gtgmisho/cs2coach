"""Tests for the LM Studio client, the LLM worker and the app glue (no GPU needed).

Run from the project folder:  python -m unittest discover -s tests -v
"""

import contextlib
import io
import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
sys.path.insert(0, HERE)

from coach_app import CoachApp  # noqa: E402
from cs2coach.llm import (  # noqa: E402
    AnalysisJob, LLMError, LLMWorker, LMStudioClient, build_state_report, clean_command,
)
from cs2coach.state import parse_payload  # noqa: E402
from fake_lmstudio import FakeLMStudio  # noqa: E402
from send_test_payload import BASE, scripted_round  # noqa: E402


def free_port() -> int:
    """A port with nothing listening on it (for 'LM Studio offline' tests)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout=8.0):  # generous: Windows refuses connections slowly
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class CleanCommandTests(unittest.TestCase):
    def test_cleaning(self):
        cases = {
            "Buy full set, hold B": "Buy full set, hold B",
            '"Rush A fast together."': "Rush A fast together",
            "Command: Hold B\nExplanation: ...": "Hold B",
            "- **Save your rifle**": "Save your rifle",
            "<think>hmm</think>\nDefuse now": "Defuse now",
            "<think>still thinking and cut off": "",
            "": "",
            "one two three four five six seven eight nine ten": "one two three four five six seven eight",
        }
        for raw, expected in cases.items():
            self.assertEqual(clean_command(raw), expected, raw)


class ReportTests(unittest.TestCase):
    def test_report_has_key_facts(self):
        frames = scripted_round()
        report = build_state_report(parse_payload(frames[6]))  # bomb planted, CT, no kit
        for fact in ("de_mirage", "CT (defending)", "live", "Bomb: planted", "HP 19",
                     "defuse kit: no", "Money $1000", "m4a1 [active] 6/30", "K/A/D 4/1/3"):
            self.assertIn(fact, report)

    def test_report_for_spectating(self):
        p = dict(BASE, player=dict(BASE["player"], steamid="other", name="Mate"))
        self.assertIn("spectating teammate Mate", build_state_report(parse_payload(p)))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLMStudio().start()

    def tearDown(self):
        self.fake.stop()

    def test_command_and_request_parameters(self):
        c = LMStudioClient(self.fake.url + "/v1/chat/completions")  # full URL is accepted too
        model = c.detect_model()
        self.assertEqual(model, "test-model")       # embeddings model skipped
        self.assertEqual(c.get_command("report", model), "Buy full set, hold B")
        body = self.fake.requests[-1]
        self.assertEqual(body["temperature"], 0.3)
        self.assertEqual(body["max_tokens"], 35)
        self.assertIn("in English", body["messages"][0]["content"])
        self.assertEqual(body["messages"][1], {"role": "user", "content": "report"})

    def test_no_model_loaded(self):
        self.fake.loaded = False
        with self.assertRaises(LLMError) as ctx:
            LMStudioClient(self.fake.url).detect_model()
        self.assertEqual(ctx.exception.kind, "no_model")

    def test_old_lmstudio_without_native_api(self):
        self.fake.native_api = False
        self.assertEqual(LMStudioClient(self.fake.url).detect_model(), "test-model")

    def test_explicit_model(self):
        self.assertEqual(LMStudioClient(self.fake.url, model="mine").detect_model(), "mine")

    def test_http_error(self):
        self.fake.status = 400
        with self.assertRaises(LLMError) as ctx:
            LMStudioClient(self.fake.url).get_command("r", "test-model")
        self.assertEqual(ctx.exception.kind, "http")

    def test_timeout(self):
        self.fake.delay = 1.0
        with self.assertRaises(LLMError) as ctx:
            LMStudioClient(self.fake.url, timeout=0.2).get_command("r", "test-model")
        self.assertEqual(ctx.exception.kind, "timeout")

    def test_thinking_model_cut_off(self):
        self.fake.reply = "<think>Let me analyse the economy of"
        with self.assertRaises(LLMError) as ctx:
            LMStudioClient(self.fake.url).get_command("r", "test-model")
        self.assertEqual(ctx.exception.kind, "bad_reply")

    def test_connect_timeout_counts_as_offline(self):
        # Windows: a closed localhost port often times out on CONNECT (~2 s)
        # instead of refusing. That means "not running", not "slow model".
        c = LMStudioClient(self.fake.url)
        with mock.patch.object(c._opener, "open", side_effect=urllib.error.URLError(TimeoutError("timed out"))):
            with self.assertRaises(LLMError) as ctx:
                c.detect_model()
        self.assertEqual(ctx.exception.kind, "offline")

    def test_read_timeout_counts_as_timeout(self):
        c = LMStudioClient(self.fake.url)
        with mock.patch.object(c._opener, "open", side_effect=TimeoutError("timed out")):
            with self.assertRaises(LLMError) as ctx:
                c.get_command("r", "test-model")
        self.assertEqual(ctx.exception.kind, "timeout")

    def test_offline(self):
        with self.assertRaises(LLMError) as ctx:
            LMStudioClient(f"http://127.0.0.1:{free_port()}").detect_model()
        self.assertEqual(ctx.exception.kind, "offline")


class WorkerTests(unittest.TestCase):
    def test_worker_answers_and_survives_offline(self):
        results = []
        fake = FakeLMStudio().start()
        try:
            w = LLMWorker(LMStudioClient(fake.url), lambda j, c, t: results.append(c),
                          health_interval=0.2, offline_retry=0.1)
            with self.assertLogs("cs2coach.llm", level="INFO"):
                w.start()
                self.assertTrue(wait_for(lambda: w.status.usable))
                w.submit(AnalysisJob("report", ("k",), "test"))
                self.assertTrue(wait_for(lambda: results == ["Buy full set, hold B"]))
        finally:
            fake.stop()

        # LM Studio goes away: the worker reports it and keeps running.
        with self.assertLogs("cs2coach.llm", level="WARNING"):
            w.submit(AnalysisJob("report", ("k",), "test"))
            self.assertTrue(wait_for(lambda: w.status.state == "offline"))
        self.assertTrue(w.is_alive())
        w.stop()
        w.join(2)
        self.assertFalse(w.is_alive())


class AppTests(unittest.TestCase):
    """CoachApp with a real worker against the fake LM Studio (console mode)."""

    def setUp(self):
        self.fake = FakeLMStudio(reply="Defuse now, you have kit", delay=0.2).start()
        self.app = CoachApp()
        self.app.worker = LLMWorker(LMStudioClient(self.fake.url), self.app.on_llm_result,
                                    health_interval=0.2)
        self.shown = []

        class Recorder:  # stands in for the Tk overlay
            def post(_self, text, level="info"):
                self.shown.append(text)
        self.app.overlay = Recorder()
        # Keep the app's console output and logs out of the test report.
        self._quiet = contextlib.ExitStack()
        self._quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self._quiet.enter_context(self.assertLogs("cs2coach", level="INFO"))
        self.app.worker.start()
        self.assertTrue(wait_for(lambda: self.app.worker.status.usable))

    def tearDown(self):
        self.app.worker.stop()
        self.fake.stop()
        self._quiet.close()

    def test_rules_first_then_ai(self):
        frames = scripted_round()
        self.app.on_payload(frames[0])                       # freezetime
        self.assertEqual(self.shown[0], "Buy full set and kit")   # instant rule text
        self.assertTrue(wait_for(lambda: self.shown[-1] == "Defuse now, you have kit"))
        self.assertEqual(len(self.fake.requests), 1)

        # Ammo/money-only changes in the same situation: no new request, AI text kept.
        p = dict(frames[0], player=dict(frames[0]["player"], state=dict(frames[0]["player"]["state"], money=5100)))
        self.app.on_payload(p)
        time.sleep(0.3)
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.shown[-1], "Defuse now, you have kit")

        segs = dict(self.app.status_segments())
        self.assertIn("● CS2 GSI", segs)
        self.assertIn("● LM Studio: test-model", segs)

    def test_stale_ai_answer_is_dropped(self):
        frames = scripted_round()
        self.app.on_payload(frames[0])        # request goes out for freezetime...
        self.assertTrue(wait_for(lambda: len(self.fake.requests) == 1))
        self.app.on_payload(frames[1])        # ...but the round goes live before it returns
        time.sleep(0.8)
        # Both answers arrive; only the one matching the live situation may be shown.
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual(self.shown.count("Defuse now, you have kit"), 1)

    def test_burst_of_payloads_is_debounced(self):
        threads = [threading.Thread(target=self.app.on_payload, args=(BASE,)) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(0.5)
        self.assertEqual(len(self.fake.requests), 1)


if __name__ == "__main__":
    unittest.main()
