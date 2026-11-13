"""Minimal GSI receiver built only on Python's standard library.

CS2 POSTs a JSON body to the "uri" in the .cfg file. We:
  1. read and decode the JSON,
  2. check the auth token,
  3. reply 200 straight away (CS2 waits for the reply before sending more),
  4. hand the parsed payload to a callback.

The server binds to 127.0.0.1 only, so no other machine can reach it.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)

PayloadCallback = Callable[[Dict[str, Any]], None]

MAX_BODY_BYTES = 1_000_000  # GSI payloads are a few KB; refuse anything absurd


class _GSIHandler(BaseHTTPRequestHandler):
    """Handles one HTTP request. ``self.server`` is the GSIServer below."""

    server: "GSIServer"  # for type checkers

    def do_POST(self) -> None:  # noqa: N802 (name fixed by http.server)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._reply(400)
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.warning("Ignoring a request that isn't valid JSON")
            self._reply(400)
            return

        if not isinstance(payload, dict) or not self.server.token_ok(payload):
            log.warning("Ignoring a payload with a missing or wrong auth token")
            self._reply(403)
            return

        # Answer first so CS2 is never kept waiting on our processing.
        self._reply(200)
        try:
            self.server.on_payload(payload)
        except Exception:  # a bug in a callback must never kill the server
            log.exception("Payload callback failed")

    def do_GET(self) -> None:  # noqa: N802
        # Handy health check: open http://localhost:3000/ in a browser.
        body = b"CS2 Coach GSI server is running. CS2 sends POST requests here.\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reply(self, code: int) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence http.server's default one-line-per-request access log.
        log.debug("%s - %s", self.address_string(), format % args)


class GSIServer(ThreadingHTTPServer):
    """HTTP server that forwards every valid GSI payload to ``on_payload``."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        on_payload: PayloadCallback,
        host: str = "127.0.0.1",
        port: int = 3000,
        token: Optional[str] = None,
    ) -> None:
        self.on_payload = on_payload
        self.token = token
        super().__init__((host, port), _GSIHandler)

    def token_ok(self, payload: Dict[str, Any]) -> bool:
        if not self.token:
            return True  # token checking disabled
        return (payload.get("auth") or {}).get("token") == self.token

    def start_in_background(self) -> threading.Thread:
        """Serve on a daemon thread so the caller (e.g. the UI) keeps the main thread."""
        thread = threading.Thread(target=self.serve_forever, name="gsi-server", daemon=True)
        thread.start()
        host, port = self.server_address[:2]
        log.info("GSI server listening on http://%s:%s/", host, port)
        return thread
