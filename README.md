# CS2 AI Coach

A read-only, real-time CS2 coach that runs **entirely on your PC**. CS2 streams its game state to the app, a **rules engine** turns it into one clear call, an optional **local LLM in LM Studio** adds a short extra tip, and a floating window on your second monitor shows both.

```
CS2 ──GSI (HTTP POST)──▶ coach_app.py :3000 ──▶ rules engine ──▶ MAIN CALL   "Buy full set and kit"
                                      │
                                      └──▶ LM Studio :1234 ──▶ checks ──▶ AI tip   "Throw utility before you peek"
```

**Why the rules make the main call:** CS2 doesn't tell the coach where anyone is, and small local models confidently invent things they can't know ("rotate B"). The rules only use real facts and answer instantly, so they're the call you can trust. The AI can only add a tip underneath, and only if it passes the checks in [What the AI may say](#what-the-ai-may-say).

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

1. In LM Studio, download and **load** a small, **non-thinking instruct** model (see below). The `Q4_K_M` download is a good size/quality balance.
2. Open the **Developer** tab and click **Start Server** (default port `1234`).

The app picks whichever model is loaded. To choose one explicitly, use `--model <id>`.

#### Which model?

CS2 and the model share your graphics card, so smaller is better. You only need one short sentence back, quickly.

| Your GPU memory (VRAM) | Recommended model | Approx. VRAM used (Q4_K_M) |
|---|---|---|
| 6–8 GB | **Qwen3 4B Instruct 2507** or **Gemma 3 4B Instruct** | ~3 GB |
| 10–12 GB | **Llama 3.1 8B Instruct** or **Qwen2.5 7B Instruct** | ~5 GB |
| 16 GB+ | **Gemma 3 12B Instruct** | ~8 GB |

If you lose FPS, go one size down, or lower the "GPU offload" slider in LM Studio's model settings so part of the model runs on the CPU.

#### Thinking (reasoning) models

The coach asks for at most **35 tokens** so answers stay fast. A *thinking* model spends those tokens on hidden reasoning and never reaches the answer. The coach handles this automatically:

| Model type | Examples | What the coach does |
|---|---|---|
| Instruct (no thinking) | Qwen3 Instruct 2507, Gemma 3, Llama 3.1, Qwen2.5 | Works directly. **Best choice** |
| Hybrid (thinking can be switched off) | original Qwen3 4B / 8B / 14B | Detects the thinking, adds `/no_think` to the prompt, remembers it. Works, but the first answer is a bit slower |
| Always thinking | DeepSeek-R1 and its distills, QwQ, gpt-oss, Qwen3 "Thinking 2507", Phi-4-reasoning, Magistral | Can't answer in 35 tokens. The overlay shows **`● LM Studio: thinking model, use instruct`**; load an instruct model instead |

Model names with *R1*, *Thinking*, *Reasoning* or *QwQ* are thinking models. If your model has a thinking/reasoning on-off setting in LM Studio, turning it off works too.

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
│                 Buy full set and kit                  │
│            AI: Throw utility before you peek          │
├───────────────────────────────────────────────────────┤
│ ● CS2 GSI   ● LM Studio: gemma-3-4b   HP 100   $5200  │
└───────────────────────────────────────────────────────┘
```

* **Big line, the main call:** always from the rules. White = normal, amber = caution (eco, save, low HP), red = danger (no kit after plant, blind, in fire).
* **Blue italic line, the AI tip:** optional. It appears only for the exact call above it and disappears as soon as the call changes.
* **Status dots:** green = connected, amber = waiting / no model loaded / slow, red = offline.
* **Controls:** left-drag = move · mouse wheel = text size · double-click = hide status bar · right-click or Esc = quit.

Exclusive **Fullscreen** CS2 hides every other window. Use a second monitor, or set CS2 to *Fullscreen Windowed*.

## What the main call covers

| Moment | Calls |
|---|---|
| Buy time | pistol buys · **won/lost pistol** follow-up · full buy (+ kit on CT) · eco · **save now if the loss bonus gets you a full buy next round** · force on the last round of the half and at **enemy match point** · then step by step as you buy: armor → kit → utility → **drop a rifle for a teammate** → ready |
| Live round | out of the fire · blind · bomb carrier · low ammo · low HP · post-plant (T) · defuse with kit / no-kit retake · **save your rifle** (CT, no kit, low HP, expensive loadout) · dead: give info |
| Round end | won / lost |

## What the AI may say

The AI gets your facts plus the main call, and is told what it cannot know. Before a tip is shown, the coach checks it and **rejects** any tip that:

* mentions a map area, site, callout or rotation (CS2 doesn't send positions)
* repeats or contradicts the main call (e.g. "buy" when the call is "save money", or "peek" when the call is "save your rifle")
* gives buy advice during a live round
* tells you to defuse without a planted bomb, gives retake advice on T side, or tells you to plant when that doesn't fit
* tells a dead player to peek, push or hold

Rejected tips are printed in the console (`AI tip rejected (reason): ...`) so you can see what the model tried. There's no AI tip for blind/fire moments (they pass too fast) or during warmup.

The AI is asked on meaningful changes only: new round phase, bomb change, a big HP drop, death, a new main call, or every 8 s alive in a live round (`--interval`). An answer that arrives after the situation changed is discarded.

## Files

```
gamestate_integration_cs2coach.cfg   GSI config → copy into CS2 (step 1)
coach_app.py                         run this: wires everything together
cs2coach/gsi_server.py   ThreadingHTTPServer on 127.0.0.1:3000 (background thread)
cs2coach/state.py        GSI JSON → GameSnapshot
cs2coach/coach.py        rules engine: the MAIN CALL (economy, bomb, survival)
cs2coach/trigger.py      filtering & debouncing: when to ask the AI
cs2coach/llm.py          LM Studio client, AI-tip prompt, background LLM worker
cs2coach/validate.py     checks every AI tip against the real game state
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
  --interval 8           seconds between AI tips in a live round
  --no-llm               rules only, no AI tip line
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

Unit tests (45, covering rules, AI-tip checks, triggers, LM Studio errors and timeouts, stale answers):

```bash
python -m unittest discover -s tests -v
```

## Troubleshooting

* **`● CS2 GSI: waiting` forever:** the `.cfg` isn't in `game\csgo\cfg`, the filename doesn't start with `gamestate_integration_`, or CS2 wasn't restarted. Check http://127.0.0.1:3000/ in a browser to confirm the app is listening.
* **`● LM Studio: offline`:** the LM Studio server isn't started (Developer tab → Start Server) or uses a different port (`--lmstudio-url`).
* **`● LM Studio: no model loaded`:** load a model in LM Studio.
* **`● LM Studio: thinking model, use instruct`:** the loaded model always reasons before answering. Load a non-thinking instruct model (see *Which model?*). The coach retries every 15 s, so it recovers on its own once you switch.
* **`● LM Studio: timeout` / slow answers:** use a smaller model, or raise `--llm-timeout`. The first request after loading can be slow.
* **No AI tip line appears:** the model's tips are being rejected or it replies `NONE`. Check the console for `AI tip rejected (...)`; a stronger instruct model (Llama 3.1 8B, Qwen2.5 7B) gets rejected less. The main call works either way.
* **"Ignoring a payload with a missing or wrong auth token":** the `.cfg` token and `--token` differ.

## Limits

While you are alive, GSI doesn't include your position, teammates' or enemies' state, their money, or the round timer. So the coach cannot make map calls (rotations, sites, stacks) or team-economy calls. Those still come from your IGL. Loss-bonus advice uses CS2's own `consecutive_round_losses` value when CS2 sends it, and is skipped otherwise.

**Official matches:** this tool doesn't read game memory or touch the game, but league and tournament rules (FACEIT, ESL and others) often forbid *any* outside real-time assistance during a match, whatever anti-cheat detects. Check the rules of the competition before using it there. It's best used in matchmaking/practice and for building habits.
