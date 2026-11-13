# CS2 Coach

A read-only, real-time coaching assistant for Counter-Strike 2. It runs locally on your PC.

```
CS2 ──(Game State Integration, HTTP POST)──▶ localhost:3000 ──▶ parser ──▶ rule-based coach
                                                                     │              │
                                                              console summary   floating overlay
```

* **Data source:** Valve's official [Game State Integration](https://developer.valvesoftware.com/wiki/Counter-Strike:_Global_Offensive_Game_State_Integration) (GSI) only. CS2 itself pushes JSON to a local web server.
* **No dependencies:** Python standard library only (`http.server`, `json`, `threading`, `tkinter`).
* **Overlay:** a normal, separate, always-on-top window. It never draws inside the game.

## Why this is anti-cheat safe

| What anti-cheat looks for | What CS2 Coach does |
|---|---|
| Reading or writing game memory | ❌ Never. All data arrives from CS2 over HTTP via GSI |
| Injecting DLLs / hooking DirectX to draw in-game | ❌ Never. The overlay is its own OS window |
| Sending keyboard/mouse input | ❌ Never. It only displays text |
| Modifying game files | ❌ Only adds a GSI `.cfg`, which Valve built for exactly this (it's how tournament HUDs, RGB keyboards and streaming tools work) |

GSI is designed for spectators and peripherals, so it only shows what **you** can already see: your own health, money and weapons, the score, round phase and bomb state. There are no enemy positions and no hidden information.

> Third-party anti-cheats (FACEIT, ESEA) have their own rules. They generally allow GSI and separate overlay windows, but check their current policy before using any tool there.

## Project layout

```
cfg/gamestate_integration_cs2coach.cfg   GSI config (copy this into CS2)
cs2coach/state.py        GSI JSON → typed GameSnapshot (+ console summary)
cs2coach/coach.py        rule engine → one Advice (text + priority + colour level)
cs2coach/gsi_server.py   stdlib HTTP server on 127.0.0.1:3000, token check
cs2coach/overlay.py      tkinter always-on-top floating text window
main.py                  wires everything together, CLI options
tools/send_test_payload.py   fake CS2: replays a scripted round to test without the game
tests/test_coach.py      unit tests (python -m unittest discover -s tests)
```

## Setup

### 1. Install the GSI config file

Copy `cfg/gamestate_integration_cs2coach.cfg` into CS2's **cfg** folder:

```
C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg\
```

To find the folder if CS2 is installed elsewhere: in Steam, right-click **Counter-Strike 2** → **Manage** → **Browse local files**, then open `game\csgo\cfg`.

> **You do not need `+exec` or any launch option.** CS2 automatically loads every file named `gamestate_integration_*.cfg` from that folder **when the game starts**. `exec gamestate_integration_cs2coach.cfg` would only run it as a list of console commands, which does nothing for GSI. If you add or edit the file while CS2 is running, **restart CS2**.

### 2. Python

Python 3.8+ is required. The Windows installer from python.org includes tkinter by default. On Linux you may need `sudo apt install python3-tk`.

### 3. Run it

From this folder:

```bash
python main.py
```

You can start CS2 before or after this. CS2 keeps retrying until the server answers. You'll see:

```
CS2 Coach ready. Start CS2 (or run tools/send_test_payload.py). ...
09:45:14  de_mirage R6 CT 3-2 T  phase=freezetime  [CT | you]  HP 100 AR 0  $5200  weapon=usp_silencer 12/12
          >> COACH: Full buy: rifle + armor + nades + kit
```

…and a floating window with the current instruction in large text.

### 4. Make the overlay visible in game

Every always-on-top window, this one included, is hidden while CS2 runs in **exclusive Fullscreen**. Pick one:

* **Second monitor (recommended):** place the overlay on your other screen, e.g. for a monitor to the right of a 1920-px main screen:
  ```bash
  python main.py --geometry 900x160+1920+40
  ```
  (`WIDTHxHEIGHT+X+Y`; use a negative X such as `+-1500+40` for a monitor on the left.) You can also just drag it there.
* **Same monitor:** set CS2 to *Settings → Video → Display Mode → Fullscreen Windowed*. Then the overlay floats on top of the game.

### Overlay controls

| Action | Effect |
|---|---|
| Left-drag | Move the window |
| Mouse wheel | Bigger / smaller text |
| Double-click | Hide / show the small status line |
| Right-click or Esc | Quit |

### All options

```
python main.py --help
  --port 3000              must match the "uri" in the .cfg
  --token TOKEN            must match "auth" > "token" in the .cfg ('' disables the check)
  --no-overlay             console output only
  --geometry 900x160+X+Y   overlay size/position (or just +X+Y)
  --alpha 0.85             overlay opacity
  --font-size 34
  --transparent            Windows only: see-through background, text only
  --dump-payloads DIR      save every raw GSI payload (useful for writing new rules)
  -v                       debug logging
```

## Test without CS2

In one terminal:

```bash
python main.py
```

In a second terminal, replay a scripted round (buy → flashed → low ammo → low HP → bomb planted with no kit → death → round lost):

```bash
python tools/send_test_payload.py
```

Run the unit tests:

```bash
python -m unittest discover -s tests -v
```

## Adding coaching rules

Every rule in `cs2coach/coach.py` is a function that takes a `GameSnapshot` and returns `Advice(priority, text, level)` or `None`. The highest priority wins. To add one:

```python
def rule_save_rifle(s: GameSnapshot) -> Optional[Advice]:
    if _alive_self_live(s) and s.bomb == "planted" and s.team == "CT" and not s.has_defuse_kit and s.health < 50:
        return Advice(620, "Save your rifle: hide, don't retake", "warn")
    return None

RULES.append(rule_save_rifle)
```

### Limits of GSI data (what the coach can and can't know)

While you are **playing**, GSI gives your own state, the score, round phase and bomb state. It does **not** give your position, teammates' states, enemy info or the round timer (the timer needs the `phase_countdowns` block, which you can add to the `.cfg`). So map-specific calls like *"rotate B"* can't be derived from GSI alone. They would need input from you or your team, or a more advanced engine plugged into `Coach.advise()` (for example an LLM that you feed the snapshot plus your team's comms).

## Troubleshooting

* **Console shows nothing after CS2 starts:** confirm the `.cfg` is in `game\csgo\cfg` (not `csgo\cfg` at the top level), that the filename starts with `gamestate_integration_`, and that you restarted CS2. Open http://localhost:3000/ in a browser to confirm the server is up. If it still fails, change the cfg's `"uri"` to `"http://127.0.0.1:3000/"`.
* **"Ignoring a payload with a missing or wrong auth token":** the token in the `.cfg` and `--token` don't match.
* **"Could not open port 3000":** another copy (or another program) is using the port. Close it, or use `--port 3001` and change the `.cfg` to match.
* **Overlay not visible over the game:** see step 4. Exclusive fullscreen hides it.
