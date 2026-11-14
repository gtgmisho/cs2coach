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
from cs2coach.coach import Coach  # noqa: E402
from cs2coach.llm import (  # noqa: E402
    AnalysisJob, LLMError, LLMWorker, LMStudioClient, build_state_report, clean_command,
)
from cs2coach.state import parse_payload  # noqa: E402
from cs2coach.validate import check_tip  # noqa: E402
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
            "<|channel|>analysis<|message|>The player is CT": "",
            "<|channel|>analysis<|message|>x<|end|><|start|>assistant<|channel|>final<|message|>Hold B<|return|>": "Hold B",
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

    def test_hybrid_thinking_model_gets_no_think(self):
        # Qwen3-style: thinks by default, answers directly with /no_think.
        self.fake.thinking = "hybrid"
        c = LMStudioClient(self.fake.url)
        with self.assertLogs("cs2coach.llm", level="INFO"):
            self.assertEqual(c.get_command("r", "test-model"), "Buy full set, hold B")
        self.assertEqual(len(self.fake.requests), 2)            # 1 failed + 1 retry
        self.assertEqual(c.get_command("r", "test-model"), "Buy full set, hold B")
        self.assertEqual(len(self.fake.requests), 3)            # remembered: no retry
        self.assertTrue(self.fake.requests[-1]["messages"][-1]["content"].endswith("/no_think"))

    def test_always_thinking_model(self):
        for separate in (False, True):
            with self.subTest(separate_reasoning=separate):
                self.fake.thinking, self.fake.separate_reasoning = "always", separate
                with self.assertRaises(LLMError) as ctx:
                    LMStudioClient(self.fake.url).get_command("r", "test-model")
                self.assertEqual(ctx.exception.kind, "thinking")
                self.assertIn("non-thinking instruct model", str(ctx.exception))

    def test_instruct_model_sends_no_extra_prompt(self):
        LMStudioClient(self.fake.url).get_command("r", "test-model")
        self.assertEqual(self.fake.requests[-1]["messages"][-1]["content"], "r")

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


class ThinkingWorkerTests(unittest.TestCase):
    def test_thinking_model_status_and_recovery(self):
        fake = FakeLMStudio(thinking="always").start()
        results = []
        w = LLMWorker(LMStudioClient(fake.url), lambda j, c, t: results.append(c),
                      health_interval=0.1, thinking_retry=1.0)
        try:
            with self.assertLogs("cs2coach.llm", level="INFO") as logs:
                w.start()
                self.assertTrue(wait_for(lambda: w.status.usable))
                w.submit(AnalysisJob("report", ("k",), "test"))
                self.assertTrue(wait_for(lambda: w.status.state == "thinking"))
                # Health checks keep running but must not flip the status back to "online".
                time.sleep(0.4)
                self.assertEqual(w.status.state, "thinking")
                # Jobs right after a failed attempt are skipped (no request spam).
                n = len(fake.requests)
                w.submit(AnalysisJob("report", ("k",), "test"))
                time.sleep(0.1)
                self.assertEqual(len(fake.requests), n)
                # The user loads an instruct model: the next retry succeeds.
                fake.thinking = "off"
                time.sleep(0.7)                 # past thinking_retry since the last attempt
                w.submit(AnalysisJob("report", ("k",), "test"))
                self.assertTrue(wait_for(lambda: results == ["Buy full set, hold B"]))
                self.assertTrue(w.status.usable)
            self.assertTrue(any("thinking" in line for line in logs.output))
        finally:
            w.stop()
            fake.stop()


class ValidatorTests(unittest.TestCase):
    """AI tips that don't fit the facts must never reach the overlay."""

    def snap(self, frame):
        s = parse_payload(scripted_round()[frame])
        return s, Coach().advise(s)

    def test_rejections(self):
        cases = [
            # (frame, tip, expected reason fragment or None if allowed)
            (0, "Throw a flash before peeking", None),
            (0, "Rotate B fast", "map area"),
            (0, "Hold A site with AWP", "map area"),
            (0, "Play mid with your team", "map area"),
            (0, "Defuse now, you have kit", "without a planted bomb"),
            (0, "Keep a flash for the retake", None),             # CT planning a retake: fine
            (0, "NONE", "no tip"),
            (0, "Buy full set and kit", "repeats"),
            (1, "Buy an AWP next round", "buy advice during a live round"),
            (6, "Throw utility before you peek", "stay alive"),   # main call: save your rifle
            (6, "Hide and keep the rifle quiet", None),
            (7, "Push with your team", "you are dead"),           # dead
            (7, "Tell your team what killed you", None),
        ]
        for frame, tip, expected in cases:
            with self.subTest(tip=tip):
                s, main = self.snap(frame)
                problem = check_tip(tip, s, main)
                if expected is None:
                    self.assertIsNone(problem)
                else:
                    self.assertIsNotNone(problem)
                    self.assertIn(expected, problem)

    def test_contradicting_the_save_call(self):
        p = dict(BASE, player=dict(BASE["player"], state=dict(BASE["player"]["state"], money=1200)))
        s = parse_payload(p)
        main = Coach().advise(s)
        self.assertEqual(main.key, "eco")
        self.assertIn("contradicts", check_tip("Force buy pistols and armor", s, main))
        self.assertIsNone(check_tip("Stack up and trade kills", s, main))


class AppTests(unittest.TestCase):
    """CoachApp with a real worker against the fake LM Studio (console mode)."""

    def setUp(self):
        self.fake = FakeLMStudio(reply="Throw a flash before peeking", delay=0.2).start()
        self.app = CoachApp()
        self.app.worker = LLMWorker(LMStudioClient(self.fake.url), self.app.on_llm_result,
                                    health_interval=0.2)
        self.shown = []   # (main text, level, tip) as posted to the overlay

        class Recorder:  # stands in for the Tk overlay
            def post(_self, text, level="info", tip=""):
                self.shown.append((text, level, tip))
        self.app.overlay = Recorder()
        # Keep the app's console output and logs out of the test report.
        self.out = io.StringIO()
        self._quiet = contextlib.ExitStack()
        self._quiet.enter_context(contextlib.redirect_stdout(self.out))
        self._quiet.enter_context(self.assertLogs("cs2coach", level="INFO"))
        self.app.worker.start()
        self.assertTrue(wait_for(lambda: self.app.worker.status.usable))

    def tearDown(self):
        self.app.worker.stop()
        self.fake.stop()
        self._quiet.close()

    def test_main_call_is_rules_and_ai_adds_a_tip(self):
        frames = scripted_round()
        self.app.on_payload(frames[0])                       # freezetime
        self.assertEqual(self.shown[0], ("Buy full set and kit", "info", ""))   # instant
        self.assertTrue(wait_for(lambda: self.shown[-1] == ("Buy full set and kit", "info", "Throw a flash before peeking")))
        self.assertEqual(len(self.fake.requests), 1)
        self.assertIn("MAIN CALL already shown to the player: Buy full set and kit",
                      self.fake.requests[0]["messages"][1]["content"])

        # Money-only change, same call: no new request, tip kept.
        p = dict(frames[0], player=dict(frames[0]["player"], state=dict(frames[0]["player"]["state"], money=5100)))
        self.app.on_payload(p)
        time.sleep(0.3)
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.shown[-1][2], "Throw a flash before peeking")

        segs = dict(self.app.status_segments())
        self.assertIn("● CS2 GSI", segs)
        self.assertIn("● LM Studio: test-model", segs)

    def test_tip_disappears_when_the_call_changes(self):
        frames = scripted_round()
        self.app.on_payload(frames[0])
        self.assertTrue(wait_for(lambda: self.shown[-1][2] == "Throw a flash before peeking"))
        self.fake.delay = 1.0                                # next answer is slow
        self.app.on_payload(frames[2])                       # live and flashed
        self.assertEqual(self.shown[-1], ("Blind: fall back now", "danger", ""))

    def test_invented_location_is_never_shown(self):
        self.fake.reply = "Rotate B fast"
        self.app.on_payload(scripted_round()[0])
        self.assertTrue(wait_for(lambda: "AI tip rejected (mentions a map area" in self.out.getvalue()))
        self.assertTrue(all(tip == "" for _, _, tip in self.shown))

    def test_stale_ai_answer_is_dropped(self):
        frames = scripted_round()
        self.app.on_payload(frames[0])        # request goes out for freezetime...
        self.assertTrue(wait_for(lambda: len(self.fake.requests) == 1))
        self.app.on_payload(frames[1])        # ...but the round goes live before it returns
        # Both answers arrive; only the one for the live situation may be shown.
        self.assertTrue(wait_for(lambda: any(x[2] for x in self.shown)))
        time.sleep(0.3)                       # give a wrongly accepted stale tip time to show
        self.assertEqual(len(self.fake.requests), 2)
        with_tip = [x for x in self.shown if x[2]]
        self.assertEqual(len(with_tip), 1)
        self.assertEqual(with_tip[0][0], "Stay near a teammate, trade")

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
