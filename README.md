# CS2 AI Coach

A read-only, real-time CS2 coach that runs **entirely on your PC**. CS2 streams its game state to the app, a **local LLM in LM Studio** turns it into a short IGL command, and a floating window on your second monitor shows it.

```
CS2 ──GSI (HTTP POST)──▶ coach_app.py :3000 ──▶ LM Studio :1234 (local LLM)
                              │                        │
                              └──── overlay window ◀───┘   "Buy full set, hold B"
```

## Safe for VAC / FACEIT

| | |
|---|---|
| Game data | Only Valve's official **Game State Integration** (GSI): CS2 itself sends JSON to `127.0.0.1`. This is the same mechanism tournament HUDs, RGB keyboards and streaming tools use |
| Memory reading / injection | **None.** No process access, no DLLs, no hooks, no packet sniffing |
| Input | **None.** It never sends keys or mouse movement |
| Overlay | A normal separate desktop window. Nothing is drawn inside the game |
| Network | Everything stays on `127.0.0.1`. No internet, no cloud AI |

GSI only gives what you can already see (your HP, money, weapons, score, round phase, bomb state), so there is no hidden information.

## Setup

### 1. Copy the config into CS2

Copy **`gamestate_integration_cs2coach.cfg`** to:

```
C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg\
```

(Different Steam library? In Steam, right-click **Counter-Strike 2** → **Manage** → **Browse local files**, then open `game\csgo\cfg`.)

CS2 loads `gamestate_integration_*.cfg` files **automatically at startup**, so there's no `+exec` or launch option to add. **Restart CS2** after copying or editing the file.

### 2. Python packages

**No `pip install` needed.** The app uses only Python's standard library (`urllib` instead of `requests`, `tkinter` for the overlay). Python 3.8+ is required; the python.org Windows installer already includes `tkinter`.

### 3. Start LM Studio

1. In LM Studio, download and **load** a small *instruct* model. Good choices: **Llama 3.1 8B Instruct**, **Qwen2.5 7B Instruct** or **Gemma 3 4B Instruct**.
   *Avoid "thinking" models (Qwen3, DeepSeek-R1…) or switch thinking off: they spend the 35-token limit on reasoning and return nothing.*
2. Open the **Developer** tab and click **Start Server** (default port `1234`).

The app picks whichever model is loaded. To choose one explicitly, use `--model <id>`.

> CS2 and the LLM share your GPU. If you lose FPS, use a smaller model (e.g. 4B) or offload some layers to the CPU in LM Studio.

### 4. Run the coach

```bash
python coach_app.py
```

Drag the window to your second monitor, or place it directly:

```bash
python coach_app.py --geometry +1920+40
```

(`+X+Y`: X=1920 is a monitor to the right of a 1920-px screen; use `+-1500+40` for one on the left.) Start order doesn't matter: CS2 retries, and the app reconnects to LM Studio by itself.

## The overlay

```
┌───────────────────────────────────────────────────────┐
│                 Buy full set, hold B                  │
├───────────────────────────────────────────────────────┤
│ ● CS2 GSI  ● LM Studio: gemma-3-4b  HP 100  $4750  AI │
└───────────────────────────────────────────────────────┘
```

* **Text colour:** white = normal, amber = caution (eco, low HP), red = danger (no kit after plant, flashed, in fire).
* **Status dots:** green = connected, amber = waiting / no model loaded / slow, red = offline.
* **Last tag:** `AI` = command from the LLM; `rules` = instant built-in fallback (LM Studio offline, or the AI hasn't answered for this situation yet).
* **Controls:** left-drag = move · mouse wheel = text size · double-click = hide status bar · right-click or Esc = quit.

Exclusive **Fullscreen** CS2 hides every other window. Use a second monitor, or set CS2 to *Fullscreen Windowed*.

## When the AI is asked

CS2 sends updates up to twice a second. The app only queries the LLM on **meaningful** changes, so it doesn't overload your GPU:

* round phase change (freezetime → live → over) or a new round, warmup/half-time/map changes
* bomb planted / defused / exploded
* a big HP drop (≥ 25 since the last analysis), dying, side switch
* every 5 s while you're alive in a live round (`--interval`)

Only the **newest** situation is ever queued. An AI answer that arrives after the situation changed (e.g. the round went live) is discarded, not shown.

## Files

```
gamestate_integration_cs2coach.cfg   GSI config → copy into CS2 (step 1)
coach_app.py                         run this: wires everything together
cs2coach/gsi_server.py   ThreadingHTTPServer on 127.0.0.1:3000 (background thread)
cs2coach/state.py        GSI JSON → GameSnapshot
cs2coach/trigger.py      filtering & debouncing: when to ask the AI
cs2coach/llm.py          LM Studio client, IGL system prompt, background LLM worker
cs2coach/coach.py        instant rule-based fallback
cs2coach/overlay.py      tkinter dark always-on-top window (theme.py = colours)
tools/send_test_payload.py   fake CS2: replays a scripted round
tests/fake_lmstudio.py       fake LM Studio for testing without a GPU
```

## Options

```
python coach_app.py --help
  --port 3000            GSI port (must match the .cfg)
  --token TOKEN          must match "auth" > "token" in the .cfg
  --lmstudio-url URL     default http://127.0.0.1:1234
  --model ID             LM Studio model id (default: the loaded one)
  --temperature 0.3      --max-tokens 35      --llm-timeout 15
  --interval 5           seconds between periodic analyses in a live round
  --no-llm               rule-based coach only
  --no-overlay           console only
  --geometry +X+Y        overlay position (or WIDTHxHEIGHT+X+Y)
  --font-size 34   --alpha 0.92   --transparent (Windows: see-through background)
  --dump-payloads DIR    save raw GSI JSON (for tuning the prompt)
  -v                     debug logging
```

The system prompt is `SYSTEM_PROMPT` in `cs2coach/llm.py`, and the game-state text sent with it is built by `build_state_report()` in the same file.

## Test without CS2 or LM Studio

Run each command in its own terminal, in this order:

```bash
python tests/fake_lmstudio.py
```

```bash
python coach_app.py
```

```bash
python tools/send_test_payload.py
```

Unit tests (31, covering parsing, triggers, LM Studio errors and timeouts, and stale answers):

```bash
python -m unittest discover -s tests -v
```

## Troubleshooting

* **`● CS2 GSI: waiting` forever:** the `.cfg` isn't in `game\csgo\cfg`, the filename doesn't start with `gamestate_integration_`, or CS2 wasn't restarted. Check http://127.0.0.1:3000/ in a browser to confirm the app is listening.
* **`● LM Studio: offline`:** the LM Studio server isn't started (Developer tab → Start Server) or uses a different port (`--lmstudio-url`).
* **`● LM Studio: no model loaded`:** load a model in LM Studio.
* **`● LM Studio: timeout` / slow answers:** use a smaller model, or raise `--llm-timeout`. The first request after loading can be slow.
* **AI answers are vague or too long:** try a stronger instruct model (Llama 3.1 8B, Qwen2.5 7B). Replies are trimmed to one line of at most 8 words.
* **"Ignoring a payload with a missing or wrong auth token":** the `.cfg` token and `--token` differ.

## Limits

While you are alive, GSI doesn't include your position, teammates' or enemies' state, or the round timer. The AI only knows your own situation, economy, score and bomb state. Calls like "rotate B" are its tactical judgement from that data, not something it can see.
