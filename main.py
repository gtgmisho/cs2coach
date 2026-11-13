#!/usr/bin/env python3
"""CS2 Coach entry point.

    python main.py                  # server + console summary + overlay
    python main.py --no-overlay     # server + console summary only
    python main.py --geometry 900x160+1920+40   # overlay on a second monitor

Data flow:
    CS2 --HTTP POST--> GSIServer (background thread)
                          -> parse_payload() -> GameSnapshot
                          -> console summary (only when something changed)
                          -> Coach.advise() -> Advice
                          -> Overlay.post()  (queue -> Tk main thread)
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
from typing import Any, Dict, Optional

from cs2coach.coach import Advice, Coach
from cs2coach.gsi_server import GSIServer
from cs2coach.state import GameSnapshot, format_summary, parse_payload

DEFAULT_PORT = 3000
DEFAULT_TOKEN = "cs2coach_local_token"  # must match "auth" > "token" in the .cfg

log = logging.getLogger("cs2coach")


def overlay_status(s: GameSnapshot) -> str:
    """Short status line for under the big instruction text."""
    if not s.map_name:
        return ""
    parts = [f"{s.map_name}  R{s.map_round + 1}  CT {s.score_ct}-{s.score_t} T"]
    if s.has_player_data:
        parts.append(f"HP {s.health}  ${s.money}")
    if not s.is_self and s.player_name:
        parts.append(f"watching {s.player_name}")
    return "   |   ".join(parts)


class CoachApp:
    """Glue between the server, the coach and the outputs (console + overlay)."""

    def __init__(self, overlay=None, dump_dir: Optional[Path] = None) -> None:
        self.coach = Coach()
        self.overlay = overlay
        self.dump_dir = dump_dir
        self._lock = threading.Lock()   # payloads may arrive on different server threads
        self._last_summary = ""
        self._last_advice: Optional[Advice] = None

    def handle_payload(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            if self.dump_dir:
                self._dump(payload)

            snapshot = parse_payload(payload)
            advice = self.coach.advise(snapshot)

            # Heartbeats repeat the same state, so only print when something changed.
            summary = format_summary(snapshot)
            if summary != self._last_summary:
                print(f"{time.strftime('%H:%M:%S')}  {summary}", flush=True)
                self._last_summary = summary

            if advice != self._last_advice:
                print(f"{'':10}>> COACH: {advice.text}", flush=True)
                self._last_advice = advice

            if self.overlay is not None:
                self.overlay.post(advice.text, advice.level, overlay_status(snapshot))

    def _dump(self, payload: Dict[str, Any]) -> None:
        """Save raw payloads, useful for writing new rules or debugging."""
        self.dump_dir.mkdir(parents=True, exist_ok=True)
        path = self.dump_dir / f"gsi_{time.time():.3f}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read-only CS2 coach using Game State Integration.")
    p.add_argument("--host", default="127.0.0.1", help="interface to bind (keep it local)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--token", default=DEFAULT_TOKEN, help="auth token from the .cfg ('' disables the check)")
    p.add_argument("--no-overlay", action="store_true", help="console output only")
    p.add_argument("--geometry", help="overlay size/position as a Tk geometry string, e.g. 900x160+1920+40")
    p.add_argument("--alpha", type=float, default=0.85, help="overlay opacity, 0.2-1.0")
    p.add_argument("--font-size", type=int, default=34)
    p.add_argument("--transparent", action="store_true", help="Windows: fully see-through background")
    p.add_argument("--dump-payloads", type=Path, metavar="DIR", help="save every raw payload to DIR")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    overlay = None
    if not args.no_overlay:
        try:
            from cs2coach.overlay import Overlay

            overlay = Overlay(
                geometry=args.geometry,
                alpha=args.alpha,
                font_size=args.font_size,
                transparent_bg=args.transparent,
            )
        except Exception as exc:  # e.g. no display or tkinter missing
            log.warning("Overlay unavailable (%s). Continuing in console-only mode.", exc)

    app = CoachApp(overlay=overlay, dump_dir=args.dump_payloads)

    try:
        server = GSIServer(app.handle_payload, host=args.host, port=args.port, token=args.token or None)
    except OSError as exc:
        log.error("Could not open port %s: %s (is another copy already running?)", args.port, exc)
        return 1

    server.start_in_background()
    print("CS2 Coach ready. Start CS2 (or run tools/send_test_payload.py). Ctrl+C or right-click overlay to quit.")

    try:
        if overlay is not None:
            # Tk must own the main thread. Ctrl+C in the console also closes it.
            signal.signal(signal.SIGINT, lambda *_: overlay.root.after(0, overlay.root.destroy))
            overlay.run()
        else:
            # Short sleeps (not Event().wait()) so Ctrl+C is noticed on Windows too.
            signal.signal(signal.SIGINT, signal.default_int_handler)
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        print("\nCS2 Coach stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
