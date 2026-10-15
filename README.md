# hypr-computer

Computer use for Hyprland on Wayland, as an MCP server. An agent can see every screen and press
anything a person can: mouse, keyboard (compositor binds too), touchpad gestures, windows, the
clipboard, OCR, and widgets through the accessibility tree.

Built for Hyprland 0.56+ (Lua config). It also works with the
[Phantomat](https://github.com/kaolti/phantomat) infinite-canvas plugin: window positions are mapped from
canvas coordinates to the screen. Pairs with [agentControl](https://github.com/zinoos/agent-control), which
streams each agent's desktop next to its Claude Code terminal.

## Agent desktops (the default)

Every agent gets its **own desktop**: a nested Hyprland with its own seat (cursor and keyboard focus),
clipboard, Xwayland, D-Bus session and accessibility bus. It renders to a headless output, so it keeps
working whether or not anyone watches, and nothing it does touches your pointer, focus or windows.

```
your Hyprland (one seat: your mouse + keyboard)
 ├─ places 1-3: yours (Super+1..3)
 ├─ place 4: viewer window  ◄── mirrors ──  agent desktop "desktop-4" (nested Hyprland)
 │                                             ├─ headless output AGENT-1 (the agent's screen)
 │                                             ├─ virtual pointer + keyboard (Wayland protocols, its own seat)
 │                                             └─ private D-Bus + AT-SPI, own clipboard, own apps
 └─ place 5…0: more agents
```

- On first use the server starts an agent desktop on the first free place 4-9/0 (`agent_desktop(start,
  place=N)` to pick one). Press Super+N to watch it live: the agent's cursor (software-rendered, so
  visible) and windows, 1:1.
- Without extra setup a viewer goes to plain workspace N. The optional
  [`contrib/agent-places.lua`](contrib/agent-places.lua) helper puts each viewer full-size into its
  place without splitting the place you're on or taking focus (see [Install](#install)).
- Viewers are capture-only: they show the agent's screen, and your input never reaches it.
- Other viewer modes: `pip` (small, pinned over everything), `window`, `tile`, `hidden`.
- Closing the viewer stops that agent desktop (a small watchdog process notices).
- Browsers launched on an agent desktop get their own profile under
  `~/.local/share/hypr-computer/agents/<name>/`, otherwise they'd open in your running instance.
- `agent_desktop(action="use_host")` switches the tools to your real desktop, read-only: screenshots, OCR,
  window list. `HYPR_COMPUTER_TARGET=host` makes that the default. Input to your real desktop (shared
  mouse and keyboard via uinput, refused while the screen is locked) is off unless you set
  `HYPR_COMPUTER_ALLOW_HOST_INPUT=1`.

## Tools

| Area | Tools |
|---|---|
| See | `screenshot` (monitor / all / window / region, optional grid + cursor marker), `zoom` (full-res magnifier), `ocr` (read all text, or find text → refs `t1`…) |
| Mouse | `click` (any button, double/triple, held modifiers), `move_mouse` (hover), `drag` (waypoints, modifiers: super+drag moves windows), `scroll` (hi-res, both axes), `mouse_button` |
| Keyboard | `key` (chords and sequences: `ctrl+s`, `super+1`, `alt+Tab alt+Tab`, media keys, any XKB keysym), `type_text` (layout-aware real keys; wtype for other characters; paste mode), `hold_key`, `release_all` |
| Touchpad | `gesture` (1–5 finger swipes, pinch/rotate) on a virtual multitouch pad |
| Batch | `actions` runs many steps in one call; stops when the real mouse moves (the user took over) |
| Windows | `windows`, `window` (focus, close, kill, fullscreen, maximize, float, pin, move, resize, center, to_workspace), `desktop` (monitors, workspaces, layers, keyboard, submap, canvas camera) |
| Apps | `apps` (installed .desktop apps), `launch` (command or app; `accessibility=true` adds a11y flags) |
| Accessibility | `a11y_tree` (buttons, fields, menus… with refs `e1`…), `a11y_act` (click, focus, set_text, set_value, select, expand, get_text…), `a11y_apps` |
| Agent desktops | `agent_desktop` (status, start on a place, stop, viewer mode / hands-on, use_host / use_agent) |
| Misc | `clipboard` (text and images, primary selection), `wait` (time, text appears/disappears, window appears, screen changes), `hyprland` (raw Lua eval / dispatch / JSON query) |

### Targets and coordinates

Every `screenshot`/`zoom` returns a frame id. Unless you say otherwise, `x`/`y` refer to the pixels
of the latest frame, so the image the model sees is the coordinate space it clicks in. Images are
kept under Claude's resize limits (1568 px long edge, 1.15 MP) so they aren't rescaled again on the
way in. You can also pass:

- `coords="screen"`: Hyprland layout pixels
- `text="Save"`: OCR finds it (`index=` picks among several matches, `within=` limits the search to one window)
- `ref="e12"` / `ref="t3"`: an accessibility element / an OCR hit

## How input works

On an agent desktop: a pure-Python Wayland client creates `zwlr_virtual_pointer_v1` (absolute motion,
buttons, wheel) and `zwp_virtual_keyboard_v1` (keymap uploaded from libxkbcommon, modifiers tracked with
an xkb state) on the nested compositor. No touchpad gestures there.

On the host (`use_host`, only with `HYPR_COMPUTER_ALLOW_HOST_INPUT=1`):

- Three virtual devices on `/dev/uinput`: a keyboard, a pointer and a touchpad. They are created on
  first use and live as long as the server. Hyprland refocuses when a device is plugged in, so the
  server puts focus back afterwards.
- Keys are real evdev keycodes. The text→key map comes from libxkbcommon using the layout Hyprland
  reports (e.g. German: `z` = KEY_Y, `@` = AltGr+Q). Characters the layout can't produce
  (emoji, dead-key-only characters like `~`) go through `wtype`.
- The pointer moves with real relative motion (hover and drag targets see the path). Hyprland sets
  this device to flat acceleration. A final `hl.dsp.cursor.move` warp fixes any leftover error,
  e.g. from gaps between monitors.
- `release_all` runs on every error and at exit, so modifier keys can't get stuck.

## Install

Requirements: Hyprland 0.56+ with a Lua config, Python 3.12+, [uv](https://docs.astral.sh/uv/), and these
system packages (PyGObject and evdev come from the system Python, which the venv shares):

| Distro | Install |
|---|---|
| Fedora | `sudo dnf install grim tesseract tesseract-langpack-eng wtype wl-clipboard dbus-daemon at-spi2-core gtk4 python3-gobject python3-evdev python3-pillow` |
| Debian / Ubuntu | `sudo apt install grim tesseract-ocr wtype wl-clipboard dbus-daemon at-spi2-core gir1.2-gtk-4.0 gir1.2-atspi-2.0 python3-gi python3-evdev python3-pil` |
| Arch | `sudo pacman -S grim tesseract tesseract-data-eng wtype wl-clipboard dbus at-spi2-core gtk4 python-gobject python-evdev python-pillow` |

```sh
git clone https://github.com/zinoos/hypr-computer.git ~/projects/hypr-computer
cd ~/projects/hypr-computer
uv venv --python /usr/bin/python3 --system-site-packages .venv
uv pip install --python .venv/bin/python -e .
claude mcp add --scope user hypr-computer -- "$PWD/.venv/bin/python" -m hypr_computer
```

Other MCP clients: run `<checkout>/.venv/bin/python -m hypr_computer` as a stdio server.

Optional, for viewers that drop neatly into their place (recommended with Phantomat):

```sh
cp contrib/agent-places.lua ~/.config/hypr/
echo 'dofile(os.getenv("HOME") .. "/.config/hypr/agent-places.lua")' >> ~/.config/hypr/hyprland.lua
hyprctl reload
```

Agent desktops take workspaces/places 4-9 and 0; 1-3 stay yours. Your own windows on 4-0 aren't
touched, but each agent's viewer opens there.

Optional, only if you want agents to drive your real desktop (`HYPR_COMPUTER_ALLOW_HOST_INPUT=1`):
write access to `/dev/uinput`, for example

```sh
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' | sudo tee /etc/udev/rules.d/60-uinput.rules
sudo usermod -aG input "$USER"   # then log out and back in
sudo udevadm control --reload && sudo udevadm trigger
```

Tests (over stdio; images go to `$OUT`):
- `tests/agent_live.py`: full input test on a fresh agent desktop, safe while you work (11 checks)
- `tests/live.py`: the same against your real desktop. Injects input into your session; only when you're away
- `tests/smoke.py`: read-only tools

## Environment knobs

- `HYPR_COMPUTER_MAX_EDGE` / `HYPR_COMPUTER_MAX_PIXELS`: screenshot size limits (defaults 1568 / 1150000)
- `HYPR_COMPUTER_OCR_LANGS`: tesseract languages (default `eng`; `eng+deu` is slower but reads umlauts)
- `HYPR_COMPUTER_TARGET`: `agent` (default) or `host`: where the tools act on start
- `HYPR_COMPUTER_ALLOW_HOST_INPUT=1`: allow mouse/keyboard input on your real desktop (off by default)

## Notes

- Accessibility: the first a11y call sets `org.a11y.Status.IsEnabled`. Apps that were already
  running may need a restart before they show up. Chromium/Electron need
  `--force-renderer-accessibility` (`launch(accessibility=true)` adds it). kitty exposes nothing.
- On the canvas, `window(action="focus")` brings a window on screen. A window that is off screen
  can't be captured.

## License

[MIT](LICENSE)
