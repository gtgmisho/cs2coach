"""A tiny fake of LM Studio's HTTP API, for tests and demos without a GPU.

    python tests/fake_lmstudio.py --port 1234      # then run coach_app.py normally
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


class FakeLMStudio:
    def __init__(
        self,
        reply: str = "Buy full set, hold B",
        delay: float = 0.0,
        status: int = 200,
        native_api: bool = True,
        loaded: bool = True,
        port: int = 0,
    ) -> None:
        self.reply = reply
        self.delay = delay
        self.status = status
        self.native_api = native_api
        self.loaded = loaded
        self.requests: List[Dict[str, Any]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, code: int, body: Optional[dict]) -> None:
                data = json.dumps(body or {}).encode("utf-8")
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up (timeout tests)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/api/v0/models":
                    if not fake.native_api:
                        return self._send(404, {"error": "Unexpected endpoint"})
                    return self._send(200, {"data": [
                        {"id": "text-embedding-nomic", "type": "embeddings", "state": "loaded"},
                        {"id": "test-model", "type": "llm",
                         "state": "loaded" if fake.loaded else "not-loaded"},
                    ]})
                if self.path == "/v1/models":
                    return self._send(200, {"data": [{"id": "text-embedding-nomic"}, {"id": "test-model"}]})
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append(body)
                if fake.delay:
                    time.sleep(fake.delay)
                if fake.status != 200:
                    return self._send(fake.status, {"error": "No models loaded"})
                self._send(200, {"choices": [{"message": {"role": "assistant", "content": fake.reply}}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "FakeLMStudio":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=1234)
    ap.add_argument("--reply", default="Buy full set, hold B")
    ap.add_argument("--delay", type=float, default=0.5)
    a = ap.parse_args()
    fake = FakeLMStudio(reply=a.reply, delay=a.delay, port=a.port).start()
    print(f"Fake LM Studio on {fake.url} (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        fake.stop()
