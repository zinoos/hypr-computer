<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo/lockup-animated-dark.svg">
    <img src="docs/logo/lockup-animated.svg" alt="hypr-computer" width="440">
  </picture>
</p>

<p align="center">
  <b>Computer use for Hyprland on Wayland, as an MCP server.</b><br>
  Give Claude (or any MCP client) a Linux desktop it can actually use.
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#what-it-looks-like">What it looks like</a> ·
  <a href="#tools">Tools</a> ·
  <a href="#targets-and-coordinates">Targets</a> ·
  <a href="#your-real-desktop">Your real desktop</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#troubleshooting">Troubleshooting</a>
</p>

It sees the screen, moves and clicks the mouse, types on a real keyboard layout, swipes on a touchpad,
manages windows, reads text with OCR and operates widgets through the accessibility tree.

By default every agent works on **its own desktop**: a nested Hyprland with its own cursor, focus,
clipboard and apps. You watch it live with Super+N and keep working undisturbed on yours.

- Built for **Hyprland 0.56+** with a Lua config
- Works with the [Phantomat](https://github.com/kaolti/phantomat) infinite-canvas plugin (canvas
  coordinates are mapped to the screen)
- Pairs with [agentControl](https://github.com/zinoos/agent-control), which streams each agent's
  desktop next to its Claude Code terminal

---

## Quick start

**1. System packages.** PyGObject, evdev and Pillow come from the system Python, which the venv shares.

| Distro | Install |
|---|---|
| Fedora | `sudo dnf install grim tesseract tesseract-langpack-eng wtype wl-clipboard dbus-daemon at-spi2-core gtk4 python3-gobject python3-evdev python3-pillow` |
| Debian / Ubuntu | `sudo apt install grim tesseract-ocr wtype wl-clipboard dbus-daemon at-spi2-core gir1.2-gtk-4.0 gir1.2-atspi-2.0 python3-gi python3-evdev python3-pil` |
| Arch | `sudo pacman -S grim tesseract tesseract-data-eng wtype wl-clipboard dbus at-spi2-core gtk4 python-gobject python-evdev python-pillow` |

You also need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

**2. Install and register with Claude Code.**

```sh
git clone https://github.com/zinoos/hypr-computer.git ~/projects/hypr-computer
cd ~/projects/hypr-computer
uv venv --python /usr/bin/python3 --system-site-packages .venv
uv pip install --python .venv/bin/python -e .
claude mcp add --scope user hypr-computer -- "$PWD/.venv/bin/python" -m hypr_computer
```

Other MCP clients: run `<checkout>/.venv/bin/python -m hypr_computer` as a stdio server.

**3. Try it.** Start a new Claude Code session and ask for something visual:

> Open Firefox on your desktop, search for "Hyprland wiki" and tell me the first heading.

On first use an agent desktop starts on place 4 (or the next free one). Press **Super+4** to watch.

**Optional: tidy viewers.** With [`contrib/agent-places.lua`](contrib/agent-places.lua) each viewer
fills its place without splitting the one you're on or taking focus (recommended with Phantomat):

```sh
cp contrib/agent-places.lua ~/.config/hypr/
echo 'dofile(os.getenv("HOME") .. "/.config/hypr/agent-places.lua")' >> ~/.config/hypr/hyprland.lua
hyprctl reload
```

## What it looks like

```
your Hyprland (one seat: your mouse + keyboard)
 ├─ places 1-3: yours (Super+1..3)
 ├─ place 4: viewer window  ◄── mirrors ──  agent desktop "desktop-4" (nested Hyprland)
 │                                             ├─ headless output AGENT-1 (the agent's screen)
 │                                             ├─ virtual pointer + keyboard (Wayland protocols, its own seat)
 │                                             └─ private D-Bus + AT-SPI, own clipboard, own apps
 └─ place 5…0: more agents
```

The agent desktop renders to a headless output, so it keeps working whether or not anyone watches,
and nothing it does touches your pointer, focus or windows.

- **Places 4-9 and 0** belong to agents, 1-3 stay yours. Your own windows on 4-0 aren't touched,
  but each agent's viewer opens there. Pick a place with `agent_desktop(action="start", place=N)`.
- **Viewers only show.** The agent's cursor is software-rendered, so you see it 1:1. Your input
  never reaches the agent desktop.
- **Viewer modes:** full place (default), `pip` (small, pinned over everything), `window`, `tile`, `hidden`.
- **Closing the viewer stops that agent desktop** (a small watchdog notices).
- **Browsers get their own profile** under `~/.local/share/hypr-computer/agents/<name>/`, otherwise
  they'd open inside your running instance. Your logins aren't there.

## Tools

| Area | Tools |
|---|---|
| See | `screenshot` (monitor / all / window / region, optional grid + cursor marker), `zoom` (full-res magnifier), `ocr` (read all text, or find text → refs `t1`…) |
| Mouse | `click` (any button, double/triple, held modifiers), `move_mouse` (hover), `drag` (waypoints, modifiers: super+drag moves windows), `scroll` (hi-res, both axes), `mouse_button` |
| Keyboard | `key` (chords and sequences: `ctrl+s`, `super+1`, `alt+Tab alt+Tab`, media keys, any XKB keysym), `type_text` (layout-aware real keys; wtype for other characters; paste mode), `hold_key`, `release_all` |
| Touchpad | `gesture` (1–5 finger swipes, pinch/rotate) on a virtual multitouch pad (host only) |
| Batch | `actions` runs many steps in one call; stops when the real mouse moves (the user took over) |
| Windows | `windows`, `window` (focus, close, kill, fullscreen, maximize, float, pin, move, resize, center, to_workspace), `desktop` (monitors, workspaces, layers, keyboard, submap, canvas camera) |
| Apps | `apps` (installed .desktop apps), `launch` (command or app; `accessibility=true` adds a11y flags) |
| Accessibility | `a11y_tree` (buttons, fields, menus… with refs `e1`…), `a11y_act` (click, focus, set_text, set_value, select, expand, get_text…), `a11y_apps` |
| Agent desktops | `agent_desktop` (status, start on a place, stop, viewer mode / hands-on, use_host / use_agent) |
| Misc | `clipboard` (text and images, primary selection), `wait` (time, text appears/disappears, window appears, screen changes), `hyprland` (raw Lua eval / dispatch / JSON query) |

**Cheapest first.** Reading the UI as text (`a11y_tree` + `a11y_act`, `ocr`) is exact and costs no
image tokens. `wait(until_text=…)` beats fixed sleeps. Action tools take `screenshot_after=true` for
a small check image (long edge 900 px); call `screenshot` when you need detail.

## Targets and coordinates

Every `screenshot`/`zoom` returns a frame id (`f1`, `f2`, …). Unless you say otherwise, `x`/`y`
refer to the pixels of the latest frame, so the image the model sees is the space it clicks in.
Images stay under Claude's resize limits (1568 px long edge, 1.15 MP) so they aren't rescaled again
on the way in. Instead of pixels you can also pass:

| Argument | Meaning |
|---|---|
| `frame="f3"` | coordinates refer to an older frame |
| `coords="screen"` | Hyprland layout pixels |
| `text="Save"` | OCR finds it (`index=` picks among matches, `within=` limits to one window) |
| `ref="e12"` / `ref="t3"` | an accessibility element / an OCR hit |

## Your real desktop

`agent_desktop(action="use_host")` switches the tools to your real desktop, **read-only**:
screenshots, OCR, window list. `HYPR_COMPUTER_TARGET=host` makes that the default.

Input on your real desktop (shared mouse and keyboard via uinput) is **off** unless you set
`HYPR_COMPUTER_ALLOW_HOST_INPUT=1`, and it is refused while the screen is locked. It needs write
access to `/dev/uinput`, for example:

```sh
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' | sudo tee /etc/udev/rules.d/60-uinput.rules
sudo usermod -aG input "$USER"   # then log out and back in
sudo udevadm control --reload && sudo udevadm trigger
```

## Configuration

Environment variables for the server process:

| Variable | Default | Effect |
|---|---|---|
| `HYPR_COMPUTER_TARGET` | `agent` | where the tools act on start: `agent` or `host` |
| `HYPR_COMPUTER_ALLOW_HOST_INPUT` | unset | `1` allows mouse/keyboard input on your real desktop |
| `HYPR_COMPUTER_MAX_EDGE` | `1568` | screenshot long edge in px |
| `HYPR_COMPUTER_MAX_PIXELS` | `1150000` | screenshot pixel budget |
| `HYPR_COMPUTER_AFTER_EDGE` | `900` | long edge of the `screenshot_after=true` check image |
| `HYPR_COMPUTER_OCR_LANGS` | `eng` | tesseract languages; `eng+deu` is slower but reads umlauts |

With Claude Code, pass them on registration, e.g.
`claude mcp add --scope user -e HYPR_COMPUTER_OCR_LANGS=eng+deu hypr-computer -- …`.

## Troubleshooting

| Problem | Fix |
|---|---|
| An app doesn't show up in `a11y_tree` | The first a11y call enables accessibility; apps started before that need a restart. Chromium/Electron need `--force-renderer-accessibility` (`launch(accessibility=true)` adds it). kitty exposes nothing, use OCR there. |
| OCR misreads ä/ö/ü | Set `HYPR_COMPUTER_OCR_LANGS=eng+deu` (and install the `deu` language pack). |
| A window on the Phantomat canvas can't be captured | It's off screen. `window(action="focus")` brings it on screen first. |
| The agent desktop vanished | Its viewer was closed, which stops it. The next tool call starts a new one. |
| The browser on the agent desktop isn't logged in | Intended: it uses its own profile, separate from yours. |
| Input on the real desktop is refused | Host input is off by default, see [Your real desktop](#your-real-desktop). |

## How input works

**On an agent desktop:** a pure-Python Wayland client creates `zwlr_virtual_pointer_v1` (absolute
motion, buttons, wheel) and `zwp_virtual_keyboard_v1` (keymap uploaded from libxkbcommon, modifiers
tracked with an xkb state) on the nested compositor. No touchpad gestures there.

**On the host** (`use_host`, only with `HYPR_COMPUTER_ALLOW_HOST_INPUT=1`):

- Three virtual devices on `/dev/uinput`: a keyboard, a pointer and a touchpad. They are created on
  first use and live as long as the server. Hyprland refocuses when a device is plugged in, so the
  server puts focus back afterwards.
- Keys are real evdev keycodes. The text→key map comes from libxkbcommon using the layout Hyprland
  reports (e.g. German: `z` = KEY_Y, `@` = AltGr+Q). Characters the layout can't produce
  (emoji, dead-key-only characters like `~`) go through `wtype`.
- The pointer moves with real relative motion, so hover and drag targets see the path. Hyprland sets
  this device to flat acceleration. A final `hl.dsp.cursor.move` warp fixes any leftover error,
  e.g. from gaps between monitors.

`release_all` runs on every error and at exit, so modifier keys can't get stuck.

## Development

```
src/hypr_computer/
  server.py      MCP server: all tools and the instructions sent to the model
  agentdesk.py   agent desktops: nested Hyprland, places, viewer, watchdog
  viewer.py      the watch-only viewer window
  wlvirt.py      Wayland virtual pointer/keyboard client (agent desktops)
  agentinput.py  input on agent desktops
  vinput.py      uinput keyboard/pointer/touchpad (host)
  xkb.py         text → keys via libxkbcommon
  screen.py      screenshots, frames, zoom, OCR
  a11y.py        accessibility tree (AT-SPI)
  hypr.py        Hyprland IPC (Lua eval, dispatch, queries, Phantomat canvas projection)
  target.py      which desktop the tools act on
contrib/agent-places.lua   optional Hyprland helper for viewer placement
```

Tests talk to the server over stdio; images go to `$OUT`:

```sh
OUT=/tmp/hc .venv/bin/python tests/agent_live.py   # full input test on a fresh agent desktop, safe while you work
OUT=/tmp/hc .venv/bin/python tests/smoke.py        # read-only tools
OUT=/tmp/hc .venv/bin/python tests/live.py         # same as agent_live on your REAL desktop: injects input, run only when you're away
```

[![M8ven Score](https://m8ven.ai/badge/mcp/zinoos-hypr-computer-16ae9t?v=c7006f5d7c5a455951028abe2bdf51ae)](https://m8ven.ai/mcp/zinoos-hypr-computer-16ae9t?s=readme)

## License

[MIT](LICENSE)
