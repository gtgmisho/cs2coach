#!/usr/bin/env python3
"""CS2 AI Coach: a read-only, local, real-time coach for Counter-Strike 2.

    python coach_app.py                       # GSI server + LM Studio + overlay
    python coach_app.py --geometry +1920+40   # put the overlay on a second monitor
    python coach_app.py --no-llm              # rule-based coach only, no LM Studio
    python coach_app.py --no-overlay          # console only

Data flow (every arrow is a separate, non-blocking hand-off):

    CS2 ──HTTP POST──▶ GSIServer thread ──▶ CoachApp.on_payload()
                                               │  parse → GameSnapshot
                                               │  rules  → instant fallback text
                                               │  AnalysisTrigger: meaningful change?
                                               ▼
                                   LLMWorker thread ──HTTP──▶ LM Studio (127.0.0.1:1234)
                                               │  short IGL command
                                               ▼
                                   Overlay (Tk main thread, via queue)

Safety: data comes ONLY from Valve's official Game State Integration. There is no
memory reading, no injection, no input simulation; the overlay is a normal window.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cs2coach.coach import Coach
from cs2coach.gsi_server import GSIServer
from cs2coach.llm import AnalysisJob, LLMWorker, LMStudioClient, build_state_report
from cs2coach.theme import AMBER, GREEN, MUTED, RED, TEXT, StatusSegment
from cs2coach.state import GameSnapshot, format_summary, parse_payload
from cs2coach.trigger import AnalysisTrigger, situation_key

DEFAULT_PORT = 3000
DEFAULT_TOKEN = "cs2coach_local_token"   # must match "auth" > "token" in the .cfg
DEFAULT_LMSTUDIO = "http://127.0.0.1:1234"
GSI_STALE_S = 15.0                       # GSI heartbeat is 10 s, so 15 s of silence = lost

log = logging.getLogger("cs2coach")


def _ts() -> str:
    return time.strftime("%H:%M:%S")


class CoachApp:
    """Glue between GSI data, the rule coach, the LLM worker and the outputs.

    Called from several threads (GSI server, LLM worker, ticker, UI status
    refresh), so all shared state is guarded by ``self._lock``.

    What the overlay shows:
      * an AI command, while it still matches the current situation
        (same round, phase, bomb state, alive/dead);
      * otherwise the instant rule-based command (LM Studio offline, still
        thinking, or the situation just changed).
    """

    def __init__(self, dump_dir: Optional[Path] = None, trigger: Optional[AnalysisTrigger] = None) -> None:
        self.rules = Coach()
        self.trigger = trigger or AnalysisTrigger()
        self.worker: Optional[LLMWorker] = None   # set by main(); None = --no-llm
        self.overlay = None                       # set by main(); None = --no-overlay
        self.dump_dir = dump_dir

        self._lock = threading.RLock()
        self._snapshot: Optional[GameSnapshot] = None
        self._last_payload_at: Optional[float] = None
        self._ai_key: Optional[tuple] = None      # situation the shown AI command belongs to
        self._shown: Tuple[str, str, str] = ("", "", "")   # (text, level, source)
        self._last_summary = ""

    # ---- inputs --------------------------------------------------------------

    def on_payload(self, payload: Dict[str, Any]) -> None:
        """GSI server thread: one JSON payload from CS2."""
        snapshot = parse_payload(payload)
        with self._lock:
            if self.dump_dir:
                self._dump(payload)
            self._snapshot = snapshot
            self._last_payload_at = time.monotonic()

            summary = format_summary(snapshot)
            if summary != self._last_summary:   # heartbeats repeat the same state
                print(f"{_ts()}  {summary}", flush=True)
                self._last_summary = summary
            self._evaluate()

    def tick(self) -> None:
        """Ticker thread, ~1/s: lets periodic analysis fire even if CS2 is quiet."""
        with self._lock:
            if self._snapshot is not None:
                self._evaluate()

    def on_llm_result(self, job: AnalysisJob, command: str, elapsed: float) -> None:
        """LLM worker thread: a command for the situation in ``job``."""
        with self._lock:
            s = self._snapshot
            if s is None or situation_key(s) != job.key:
                log.debug("Dropping stale AI command %r (%s)", command, job.reason)
                return
            self._ai_key = job.key
            print(f"{'':10}AI [{job.reason}, {elapsed:.1f}s]: {command}", flush=True)
            self._show(command, self.rules.advise(s).level, "AI")

    # ---- core logic (caller holds the lock) -----------------------------------

    def _evaluate(self) -> None:
        s = self._snapshot
        rule = self.rules.advise(s)
        key = situation_key(s)

        reason = self.trigger.check(s, time.monotonic())
        if reason and self.worker is not None:
            self.worker.submit(AnalysisJob(build_state_report(s), key, reason))

        ai_fresh = (
            self.worker is not None and self.worker.status.usable and self._ai_key == key
        )
        if not ai_fresh:
            self._show(rule.text, rule.level, "rules")

    def _show(self, text: str, level: str, source: str) -> None:
        if (text, level, source) == self._shown:
            return
        self._shown = (text, level, source)
        if source == "rules":
            print(f"{'':10}rules: {text}", flush=True)
        if self.overlay is not None:
            self.overlay.post(text, level)

    # ---- status bar ----------------------------------------------------------

    def status_segments(self) -> List[StatusSegment]:
        """UI thread, every 500 ms: coloured segments for the overlay's status bar."""
        with self._lock:
            last = self._last_payload_at
            s = self._snapshot
            source = self._shown[2]

        if last is None:
            segments = [("● CS2 GSI: waiting", AMBER)]
        elif time.monotonic() - last > GSI_STALE_S:
            segments = [("● CS2 GSI: no data", RED)]
        else:
            segments = [("● CS2 GSI", GREEN)]

        segments.append(self._llm_segment())
        if s is not None and s.has_player_data:
            segments.append((f"HP {s.health}   ${s.money}", TEXT))
        if source:
            segments.append(("AI" if source == "AI" else "rules", MUTED))
        return segments

    def _llm_segment(self) -> StatusSegment:
        if self.worker is None:
            return ("LM Studio: disabled", MUTED)
        st = self.worker.status
        if st.usable:
            model = st.model.rsplit("/", 1)[-1]
            model = model if len(model) <= 28 else model[:27] + "…"
            return (f"● LM Studio: {model}{' …' if st.busy else ''}", GREEN)
        labels = {
            "unknown": ("● LM Studio: connecting", AMBER),
            "offline": ("● LM Studio: offline", RED),
            "no_model": ("● LM Studio: no model loaded", AMBER),
            "timeout": ("● LM Studio: timeout", AMBER),
        }
        return labels.get(st.state, ("● LM Studio: error", RED))

    def _dump(self, payload: Dict[str, Any]) -> None:
        """Save raw payloads; useful for debugging or tuning the prompt."""
        self.dump_dir.mkdir(parents=True, exist_ok=True)
        path = self.dump_dir / f"gsi_{time.time():.3f}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# --- startup -----------------------------------------------------------------


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read-only local CS2 AI coach (GSI + LM Studio).")
    g = p.add_argument_group("CS2 Game State Integration")
    g.add_argument("--host", default="127.0.0.1", help="interface to bind (keep it local)")
    g.add_argument("--port", type=int, default=DEFAULT_PORT, help="must match the .cfg uri")
    g.add_argument("--token", default=DEFAULT_TOKEN, help="auth token from the .cfg ('' disables check)")
    g.add_argument("--dump-payloads", type=Path, metavar="DIR", help="save every raw payload to DIR")

    g = p.add_argument_group("LM Studio")
    g.add_argument("--no-llm", action="store_true", help="rule-based coach only")
    g.add_argument("--lmstudio-url", default=DEFAULT_LMSTUDIO, help="LM Studio server base URL")
    g.add_argument("--model", help="model id to use (default: the model loaded in LM Studio)")
    g.add_argument("--temperature", type=float, default=0.3)
    g.add_argument("--max-tokens", type=int, default=35)
    g.add_argument("--llm-timeout", type=float, default=15.0, help="seconds per request")
    g.add_argument("--interval", type=float, default=5.0, help="re-analyse every N s in a live round")

    g = p.add_argument_group("Overlay")
    g.add_argument("--no-overlay", action="store_true", help="console output only")
    g.add_argument("--geometry", help="WIDTHxHEIGHT+X+Y or +X+Y, e.g. +1920+40 for a right-hand monitor")
    g.add_argument("--alpha", type=float, default=0.92, help="opacity 0.2-1.0")
    g.add_argument("--font-size", type=int, default=34)
    g.add_argument("--transparent", action="store_true", help="Windows: see-through background")

    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    # Non-ASCII model output must never crash printing on a non-UTF-8 console/redirect.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    app = CoachApp(dump_dir=args.dump_payloads, trigger=AnalysisTrigger(periodic_s=args.interval))

    if not args.no_llm:
        client = LMStudioClient(
            base_url=args.lmstudio_url, model=args.model, temperature=args.temperature,
            max_tokens=args.max_tokens, timeout=args.llm_timeout,
        )
        app.worker = LLMWorker(client, on_result=app.on_llm_result)

    if not args.no_overlay:
        try:
            from cs2coach.overlay import Overlay

            app.overlay = Overlay(
                geometry=args.geometry, alpha=args.alpha, font_size=args.font_size,
                transparent_bg=args.transparent, status_fn=app.status_segments,
            )
        except Exception as exc:  # no display, tkinter missing, bad --geometry...
            log.warning("Overlay unavailable (%s). Continuing in console-only mode.", exc)

    try:
        server = GSIServer(app.on_payload, host=args.host, port=args.port, token=args.token or None)
    except OSError as exc:
        log.error("Could not open port %s: %s (is another copy already running?)", args.port, exc)
        return 1

    server.start_in_background()
    if app.worker is not None:
        app.worker.start()
        log.info("Using LM Studio at %s", args.lmstudio_url)

    stop_ticker = threading.Event()

    def ticker() -> None:
        while not stop_ticker.wait(1.0):
            try:
                app.tick()
            except Exception:
                log.exception("tick failed")

    threading.Thread(target=ticker, name="ticker", daemon=True).start()
    print("CS2 AI Coach ready. Start CS2. Ctrl+C (or right-click the overlay) to quit.", flush=True)

    try:
        if app.overlay is not None:
            # Tk must own the main thread. Ctrl+C in the console closes it too.
            overlay = app.overlay
            signal.signal(signal.SIGINT, lambda *_: overlay.root.after(0, overlay.close))
            overlay.run()
        else:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            while True:          # short sleeps so Ctrl+C is noticed on Windows too
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_ticker.set()
        if app.worker is not None:
            app.worker.stop()
        server.shutdown()
        server.server_close()
        print("\nCS2 AI Coach stopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
