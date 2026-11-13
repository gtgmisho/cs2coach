"""Local LLM integration through LM Studio's OpenAI-compatible server.

* ``build_state_report``  GameSnapshot -> short English text the model reads
* ``LMStudioClient``      talks HTTP to LM Studio (stdlib urllib, no proxies)
* ``LLMWorker``           ONE background thread that owns every LLM call:
                          it analyses the newest queued situation, health-checks
                          LM Studio while idle, and never blocks the GSI server
                          or the UI.

All network failures become ``LLMError``. They are caught in the worker and
shown as a status ("offline", "timeout", ...), never as a crash.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional

from .state import GameSnapshot

log = logging.getLogger(__name__)

# Written in English because small local models follow English instructions
# best. The OUTPUT is required to be Bulgarian.
SYSTEM_PROMPT = """\
You are an elite Counter-Strike 2 in-game leader (IGL) coaching ONE player in real time.
You receive a short report of that player's current game state.

Reply with exactly ONE tactical command, written in Bulgarian:
- 4 to 6 words maximum.
- Start with an imperative action verb (Купи, Пази, Играй, Ротирай, Спаси, Чакай, Влез, Дефузни, Пести, Отстъпи...).
- CS2 jargon may stay in English (rush, eco, force, AWP, smoke, flash, crossfire, retake).
- Output ONLY the command: no explanation, no quotes, no emojis, no English sentences.

Decide from: side (CT defends sites, T attacks and plants), round phase, economy, HP and armor,
weapons, bomb state and score. In freezetime give a buy call. After a plant, play post-plant (T)
or retake/defuse (CT). With low HP or a bad gun, play for info or save.

Examples of valid replies:
Купи пълен сет, пази B
Бърз rush през A
Спаси оръжието, не влизай
Дефузни веднага, имаш кит
Пести парите, eco рунд"""

_PHASE_TEXT = {
    "freezetime": "freezetime (buy phase)",
    "live": "live (round in progress)",
    "over": "round over",
}


def build_state_report(s: GameSnapshot) -> str:
    """A compact, factual description of the current situation for the LLM."""
    side = {"CT": "CT (defending)", "T": "T (attacking)"}.get(s.team, s.team or "unknown")
    phase = _PHASE_TEXT.get(s.round_phase, s.round_phase or "unknown")
    if s.round_phase == "over" and s.win_team:
        phase += f", won by {s.win_team}" + (" (my team)" if s.win_team == s.team else " (enemy)")
    if s.map_phase in ("warmup", "intermission", "gameover"):
        phase += f"; match phase: {s.map_phase}"

    lines = [
        f"Map: {s.map_name} ({s.map_mode or 'unknown mode'})",
        f"Round {s.map_round + 1}, score CT {s.score_ct} - {s.score_t} T, my side: {side}",
        f"Round phase: {phase}",
        f"Bomb: {s.bomb or 'not planted'}" + ("; I am carrying the bomb" if s.has_bomb else ""),
    ]

    if not s.is_self:
        lines.append(f"I am DEAD, spectating teammate {s.player_name or '?'} (stats below are theirs)")
    elif not s.is_alive:
        lines.append("I am DEAD this round")

    armor = f"armor {s.armor}" + (" + helmet" if s.helmet else "")
    kit = ", defuse kit: yes" if s.has_defuse_kit else (", defuse kit: no" if s.team == "CT" else "")
    lines.append(f"HP {s.health}, {armor}{kit}")
    lines.append(f"Money ${s.money}, equipment value ${s.equip_value}")

    weapons: List[str] = []
    for w in s.weapons:
        if w.type in ("Knife", "Fists", "Melee"):
            continue
        desc = w.short_name
        if w.is_active:
            desc += " [active]"
        if w.ammo_clip is not None and w.ammo_clip_max:
            desc += f" {w.ammo_clip}/{w.ammo_clip_max}"
        weapons.append(desc)
    lines.append("Weapons: " + (", ".join(weapons) if weapons else "none"))

    status = []
    if s.flashed:
        status.append(f"flashed ({s.flashed}/255)")
    if s.burning:
        status.append("standing in fire")
    if s.smoked:
        status.append("inside smoke")
    if status:
        lines.append("Right now: " + ", ".join(status))

    lines.append(
        f"Kills this round: {s.round_kills}; match K/A/D {s.kills}/{s.assists}/{s.deaths}"
    )
    if s.is_pistol_round:
        lines.append("Note: pistol round")
    elif s.map_round == 11:
        lines.append("Note: last round of the half, money resets after it")
    return "\n".join(lines)


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)
_LEADING_JUNK = re.compile(r"^(?:[-*•>#]+\s*|\d+[.)]\s*|(?:команда|command|отговор|answer)\s*:\s*)+", re.I)
_QUOTES = "\"'`«»„“”*_ "


def clean_command(text: str, max_words: int = 8) -> str:
    """Reduce a model reply to one short command line. Returns "" if unusable."""
    text = _THINK_BLOCK.sub("", text or "")
    if re.search(r"<think>", text, re.I):
        return ""  # the reply was cut off while the model was still "thinking"
    for line in text.splitlines():
        line = _LEADING_JUNK.sub("", line.strip().strip(_QUOTES)).strip(_QUOTES)
        if line:
            break
    else:
        return ""
    words = line.split()
    if len(words) > max_words:
        line = " ".join(words[:max_words])
    return line.rstrip(" .,;:")


class LLMError(Exception):
    """A failed LLM call. ``kind`` is one of: offline, timeout, http, no_model, bad_reply."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class LMStudioClient:
    """Minimal client for LM Studio's local server (default http://127.0.0.1:1234)."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:1234",
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 35,
        timeout: float = 15.0,
        probe_timeout: float = 2.0,
    ) -> None:
        base_url = base_url.rstrip("/")
        for suffix in ("/v1/chat/completions", "/v1"):  # accept any of the usual spellings
            if base_url.endswith(suffix):
                base_url = base_url[: -len(suffix)]
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.probe_timeout = probe_timeout
        # Never route localhost traffic through a system/corporate proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ---- HTTP --------------------------------------------------------------

    def _request(self, method: str, path: str, body: Any = None, timeout: float = 5.0) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:  # must come before URLError (it's a subclass)
            try:
                detail = e.read(300).decode("utf-8", "replace").strip()
            except Exception:
                detail = ""
            finally:
                e.close()
            raise LLMError(f"HTTP {e.code} from LM Studio: {detail[:200]}", "http") from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise LLMError("LM Studio timed out", "timeout") from None
            raise LLMError(f"LM Studio not reachable ({e.reason})", "offline") from None
        except TimeoutError:
            raise LLMError("LM Studio timed out", "timeout") from None
        except OSError as e:  # connection reset mid-response, etc.
            raise LLMError(f"LM Studio connection error ({e})", "offline") from None

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise LLMError("LM Studio returned invalid JSON", "bad_reply") from None
        if not isinstance(parsed, dict):
            raise LLMError("LM Studio returned unexpected JSON", "bad_reply")
        return parsed

    # ---- API ---------------------------------------------------------------

    def detect_model(self) -> str:
        """Check LM Studio is up and return the model id to use.

        With --model given, that id is used. Otherwise the first LOADED chat
        model is picked via LM Studio's native /api/v0/models (which says which
        models are loaded), with a fallback to the OpenAI /v1/models list.
        """
        if self.model:
            self._request("GET", "/v1/models", timeout=self.probe_timeout)
            return self.model

        try:
            native = self._request("GET", "/api/v0/models", timeout=self.probe_timeout).get("data") or []
            chat = [m for m in native if m.get("type") in ("llm", "vlm")]
            loaded = [m["id"] for m in chat if m.get("state") == "loaded" and m.get("id")]
            if loaded:
                return loaded[0]
            if chat:
                raise LLMError("no model loaded: load one in LM Studio", "no_model")
        except LLMError as e:
            if e.kind != "http":  # http = older LM Studio without /api/v0, so fall back
                raise

        listed = self._request("GET", "/v1/models", timeout=self.probe_timeout).get("data") or []
        ids = [m.get("id", "") for m in listed if "embed" not in str(m.get("id", "")).lower()]
        ids = [i for i in ids if i]
        if not ids:
            raise LLMError("no model loaded: load one in LM Studio", "no_model")
        return ids[0]

    def get_command(self, report: str, model: str) -> str:
        """Ask the model for one short Bulgarian command. Raises LLMError."""
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": report},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        data = self._request("POST", "/v1/chat/completions", body, timeout=self.timeout)
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("unexpected reply format from LM Studio", "bad_reply") from None
        command = clean_command(content)
        if not command:
            raise LLMError(
                "empty reply (a 'thinking' model? use an instruct model or raise --max-tokens)",
                "bad_reply",
            )
        return command


# --- Background worker --------------------------------------------------------


@dataclass(frozen=True)
class LLMStatus:
    state: str = "unknown"   # unknown | online | offline | timeout | no_model | http | bad_reply
    model: str = ""
    detail: str = ""
    busy: bool = False       # a request is in flight right now

    @property
    def usable(self) -> bool:
        return self.state == "online"


@dataclass(frozen=True)
class AnalysisJob:
    report: str              # text sent to the model
    key: tuple               # situation_key() at the time of the request
    reason: str              # why it was triggered (for the console)


ResultCallback = Callable[[AnalysisJob, str, float], None]


class LLMWorker(threading.Thread):
    """Single thread that performs every LLM request, newest situation first.

    ``submit`` never blocks. If a job is still waiting when a newer one
    arrives, the older one is dropped, because only the latest game state matters.
    """

    def __init__(
        self,
        client: LMStudioClient,
        on_result: ResultCallback,
        health_interval: float = 5.0,
        offline_retry: float = 2.0,
    ) -> None:
        super().__init__(name="llm-worker", daemon=True)
        self.client = client
        self.on_result = on_result
        self.health_interval = health_interval
        self.offline_retry = offline_retry
        self._cond = threading.Condition()
        self._pending: Optional[AnalysisJob] = None
        self._stopping = False
        self._status = LLMStatus()
        self._last_probe = -1e9

    # ---- thread-safe API ---------------------------------------------------

    def submit(self, job: AnalysisJob) -> None:
        with self._cond:
            self._pending = job
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify()

    @property
    def status(self) -> LLMStatus:
        with self._cond:
            return self._status

    # ---- worker loop -------------------------------------------------------

    def run(self) -> None:
        while True:
            with self._cond:
                if self._pending is None and not self._stopping:
                    wait = self._last_probe + self.health_interval - time.monotonic()
                    self._cond.wait(timeout=max(0.05, wait))
                if self._stopping:
                    return
                job, self._pending = self._pending, None

            try:
                self._step(job)
            except Exception:  # never let a bug kill the worker thread
                log.exception("LLM worker error")

    def _step(self, job: Optional[AnalysisJob]) -> None:
        now = time.monotonic()
        usable = self.status.usable
        if job is None:
            if now - self._last_probe >= self.health_interval:
                self._probe()
            return
        if not usable:
            if now - self._last_probe < self.offline_retry:
                return  # LM Studio was just found down. Drop the job; the rules cover it
            self._probe()
            if not self.status.usable:
                return
        self._analyze(job)

    def _probe(self) -> None:
        self._last_probe = time.monotonic()
        try:
            model = self.client.detect_model()
        except LLMError as e:
            self._set_status(state=e.kind, detail=str(e), busy=False)
        else:
            self._set_status(state="online", model=model, detail="", busy=False)

    def _analyze(self, job: AnalysisJob) -> None:
        model = self.status.model
        self._set_status(busy=True)
        started = time.monotonic()
        try:
            command = self.client.get_command(job.report, model)
        except LLMError as e:
            self._set_status(state=e.kind, detail=str(e), busy=False)
            return
        self._set_status(state="online", detail="", busy=False)
        # Successful requests count as a health check too.
        self._last_probe = time.monotonic()
        try:
            self.on_result(job, command, time.monotonic() - started)
        except Exception:
            log.exception("LLM result callback failed")

    def _set_status(self, **changes: Any) -> None:
        with self._cond:
            old = self._status
            self._status = new = replace(old, **changes)
        if (old.state, old.model) != (new.state, new.model):
            if new.usable:
                log.info("LM Studio: online, model '%s'", new.model)
            else:
                log.warning("LM Studio: %s (%s)", new.state, new.detail)
