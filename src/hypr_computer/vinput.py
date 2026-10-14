"""Virtual input devices on /dev/uinput: a full keyboard, a mouse (buttons, hi-res wheels) and a
multitouch touchpad for gestures. Devices are created lazily and kept for the server's lifetime,
because every hot-plug makes Hyprland refocus (and the Phantomat camera follow)."""

from __future__ import annotations

import atexit
import math
import os
import shutil
import subprocess
import threading
import time

from evdev import AbsInfo, UInput
from evdev import ecodes as e

from . import hypr
from .xkb import Keymap, Stroke

KBD_NAME = "hypr-computer keyboard"
PTR_NAME = "hypr-computer pointer"
PAD_NAME = "hypr-computer touchpad"

MODIFIERS = {
    "ctrl": e.KEY_LEFTCTRL, "control": e.KEY_LEFTCTRL, "ctl": e.KEY_LEFTCTRL, "lctrl": e.KEY_LEFTCTRL,
    "rctrl": e.KEY_RIGHTCTRL,
    "shift": e.KEY_LEFTSHIFT, "lshift": e.KEY_LEFTSHIFT, "rshift": e.KEY_RIGHTSHIFT,
    "alt": e.KEY_LEFTALT, "lalt": e.KEY_LEFTALT, "option": e.KEY_LEFTALT, "opt": e.KEY_LEFTALT,
    "altgr": e.KEY_RIGHTALT, "alt_gr": e.KEY_RIGHTALT, "ralt": e.KEY_RIGHTALT,
    "super": e.KEY_LEFTMETA, "win": e.KEY_LEFTMETA, "windows": e.KEY_LEFTMETA, "meta": e.KEY_LEFTMETA,
    "cmd": e.KEY_LEFTMETA, "command": e.KEY_LEFTMETA, "logo": e.KEY_LEFTMETA, "mod4": e.KEY_LEFTMETA,
    "mod": e.KEY_LEFTMETA, "lsuper": e.KEY_LEFTMETA, "rsuper": e.KEY_RIGHTMETA,
}
MODIFIER_CODES = set(MODIFIERS.values())

KEY_ALIASES = {
    "enter": e.KEY_ENTER, "return": e.KEY_ENTER, "ret": e.KEY_ENTER, "kp_enter": e.KEY_KPENTER,
    "esc": e.KEY_ESC, "escape": e.KEY_ESC, "tab": e.KEY_TAB, "space": e.KEY_SPACE, "spacebar": e.KEY_SPACE,
    "backspace": e.KEY_BACKSPACE, "bksp": e.KEY_BACKSPACE, "bs": e.KEY_BACKSPACE,
    "delete": e.KEY_DELETE, "del": e.KEY_DELETE, "insert": e.KEY_INSERT, "ins": e.KEY_INSERT,
    "home": e.KEY_HOME, "end": e.KEY_END,
    "pageup": e.KEY_PAGEUP, "page_up": e.KEY_PAGEUP, "pgup": e.KEY_PAGEUP, "prior": e.KEY_PAGEUP,
    "pagedown": e.KEY_PAGEDOWN, "page_down": e.KEY_PAGEDOWN, "pgdn": e.KEY_PAGEDOWN, "next": e.KEY_PAGEDOWN,
    "up": e.KEY_UP, "down": e.KEY_DOWN, "left": e.KEY_LEFT, "right": e.KEY_RIGHT,
    "arrowup": e.KEY_UP, "arrowdown": e.KEY_DOWN, "arrowleft": e.KEY_LEFT, "arrowright": e.KEY_RIGHT,
    "print": e.KEY_SYSRQ, "printscreen": e.KEY_SYSRQ, "prtsc": e.KEY_SYSRQ, "sysrq": e.KEY_SYSRQ,
    "capslock": e.KEY_CAPSLOCK, "caps_lock": e.KEY_CAPSLOCK, "numlock": e.KEY_NUMLOCK, "num_lock": e.KEY_NUMLOCK,
    "scrolllock": e.KEY_SCROLLLOCK, "scroll_lock": e.KEY_SCROLLLOCK, "pause": e.KEY_PAUSE, "break": e.KEY_PAUSE,
    "menu": e.KEY_COMPOSE, "compose": e.KEY_COMPOSE, "apps": e.KEY_COMPOSE,
    "volumeup": e.KEY_VOLUMEUP, "volume_up": e.KEY_VOLUMEUP, "volumedown": e.KEY_VOLUMEDOWN,
    "volume_down": e.KEY_VOLUMEDOWN, "mute": e.KEY_MUTE, "micmute": e.KEY_MICMUTE,
    "playpause": e.KEY_PLAYPAUSE, "play": e.KEY_PLAYPAUSE, "nexttrack": e.KEY_NEXTSONG, "next_track": e.KEY_NEXTSONG,
    "prevtrack": e.KEY_PREVIOUSSONG, "previous_track": e.KEY_PREVIOUSSONG, "stop": e.KEY_STOPCD,
    "brightnessup": e.KEY_BRIGHTNESSUP, "brightnessdown": e.KEY_BRIGHTNESSDOWN,
    "plus": None, "minus": None,  # resolved through the layout (see _resolve_token)
}
_CHAR_NAMES = {"plus": "+", "minus": "-", "comma": ",", "period": ".", "dot": ".", "slash": "/",
               "backslash": "\\", "semicolon": ";", "colon": ":", "apostrophe": "'", "quote": "'",
               "doublequote": '"', "equal": "=", "equals": "=", "less": "<", "greater": ">",
               "question": "?", "exclam": "!", "at": "@", "numbersign": "#", "hash": "#", "dollar": "$",
               "percent": "%", "ampersand": "&", "asterisk": "*", "underscore": "_", "bar": "|", "pipe": "|",
               "bracketleft": "[", "bracketright": "]", "braceleft": "{", "braceright": "}",
               "parenleft": "(", "parenright": ")", "grave": "`", "tilde": "~", "asciicircum": "^", "caret": "^"}

BUTTONS = {
    "left": e.BTN_LEFT, "right": e.BTN_RIGHT, "middle": e.BTN_MIDDLE,
    "back": e.BTN_SIDE, "side": e.BTN_SIDE, "forward": e.BTN_EXTRA, "extra": e.BTN_EXTRA,
    "task": e.BTN_TASK,
}


class InputError(RuntimeError):
    pass


LOCKERS = {"gtklock", "hyprlock", "swaylock", "waylock", "i3lock", "physlock"}


def session_locked() -> str | None:
    """Name of a running screen locker, if any. Input must never be sent to a lock screen."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                name = f.read().strip()
        except OSError:
            continue
        if name in LOCKERS:
            return name
    return None


def ensure_unlocked():
    name = session_locked()
    if name:
        raise InputError(f"the session is locked ({name} is running): refusing to send input to the lock "
                         "screen. Ask the user to unlock, then retry.")


def _xkb_from_hyprland() -> Keymap:
    try:
        devs = hypr.query("devices")
        kbs = [k for k in devs.get("keyboards", []) if k.get("main")] or devs.get("keyboards", [])
        k = next((k for k in kbs if k.get("layout")), None)
        if k:
            # Multi-layout setups list "us,ch": use the first; Hyprland gives each device all of them.
            layout = k["layout"].split(",")[0]
            variant = (k.get("variant") or "").split(",")[0]
            return Keymap(layout, variant, k.get("model") or "pc105", k.get("options") or "", k.get("rules") or "evdev")
    except Exception:
        pass
    return Keymap("us")


class VirtualInput:
    def __init__(self):
        self._lock = threading.RLock()
        self._kbd: UInput | None = None
        self._ptr: UInput | None = None
        self._pad: UInput | None = None
        self.keymap: Keymap | None = None
        self.held_keys: set[int] = set()
        self.held_buttons: set[int] = set()
        self.key_delay = 0.012
        self.created_note: str | None = None
        atexit.register(self.close)

    # ------------------------------------------------------------ devices

    def _restore_focus_after_hotplug(self, before: dict | None):
        """Device hot-plug makes Hyprland refocus; put focus back where it was."""
        time.sleep(0.25)
        try:
            now = hypr.query("activewindow")
            if before and before.get("address") and (not now or now.get("address") != before["address"]):
                hypr.dispatch(f'hl.dsp.focus({{window = "address:{before["address"]}"}})')
                self.created_note = "virtual device created; focus restored to " + before.get("class", "?")
        except Exception:
            pass

    def _configure_device(self, name: str, **opts):
        hname = name.lower().replace(" ", "-")
        fields = ", ".join(f"{k} = {v}" for k, v in opts.items())
        try:
            hypr.eval_lua(f'hl.device({{name = "{hname}", {fields}}})')
        except Exception:
            pass

    @property
    def kbd(self) -> UInput:
        with self._lock:
            if self._kbd is None:
                before = _safe_active()
                keys = [c for c in range(1, 0x2FF) if c < e.BTN_MISC or c >= e.KEY_OK]
                keys = [c for c in keys if c in e.KEY]
                self._kbd = UInput({e.EV_KEY: keys}, name=KBD_NAME, bustype=e.BUS_USB, vendor=0x1D6B, product=0x0104)
                self.keymap = _xkb_from_hyprland()
                self._restore_focus_after_hotplug(before)
            return self._kbd

    @property
    def ptr(self) -> UInput:
        with self._lock:
            if self._ptr is None:
                before = _safe_active()
                caps = {
                    e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.BTN_SIDE, e.BTN_EXTRA,
                               e.BTN_FORWARD, e.BTN_BACK, e.BTN_TASK],
                    e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL, e.REL_WHEEL_HI_RES, e.REL_HWHEEL_HI_RES],
                }
                self._ptr = UInput(caps, name=PTR_NAME, bustype=e.BUS_USB, vendor=0x1D6B, product=0x0105)
                # 1:1 relative motion and unmodified wheel direction for this device only.
                self._configure_device(PTR_NAME, accel_profile='"flat"', sensitivity=0, natural_scroll="false",
                                       scroll_factor=1.0)
                self._restore_focus_after_hotplug(before)
            return self._ptr

    @property
    def pad(self) -> UInput:
        with self._lock:
            if self._pad is None:
                before = _safe_active()
                W, H, RES = 4000, 2600, 40  # 100mm x 65mm at 40 units/mm
                caps = {
                    e.EV_KEY: [e.BTN_LEFT, e.BTN_TOUCH, e.BTN_TOOL_FINGER, e.BTN_TOOL_DOUBLETAP,
                               e.BTN_TOOL_TRIPLETAP, e.BTN_TOOL_QUADTAP, e.BTN_TOOL_QUINTTAP],
                    e.EV_ABS: [
                        (e.ABS_X, AbsInfo(0, 0, W, 0, 0, RES)),
                        (e.ABS_Y, AbsInfo(0, 0, H, 0, 0, RES)),
                        (e.ABS_MT_SLOT, AbsInfo(0, 0, 4, 0, 0, 0)),
                        (e.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
                        (e.ABS_MT_POSITION_X, AbsInfo(0, 0, W, 0, 0, RES)),
                        (e.ABS_MT_POSITION_Y, AbsInfo(0, 0, H, 0, 0, RES)),
                        (e.ABS_MT_TOOL_TYPE, AbsInfo(0, 0, 2, 0, 0, 0)),
                    ],
                }
                self._pad = UInput(caps, name=PAD_NAME, bustype=e.BUS_I2C, vendor=0x06CB, product=0xCE7E,
                                   input_props=[e.INPUT_PROP_POINTER, e.INPUT_PROP_BUTTONPAD])
                self._pad_size = (W, H)
                self._restore_focus_after_hotplug(before)
                time.sleep(0.3)
            return self._pad

    def close(self):
        try:
            self.release_all()
        except Exception:
            pass
        for d in (self._kbd, self._ptr, self._pad):
            try:
                if d:
                    d.close()
            except Exception:
                pass
        self._kbd = self._ptr = self._pad = None

    # ------------------------------------------------------------ keys

    def resolve(self, combo: str) -> list[Stroke]:
        """'ctrl+shift+t' -> strokes. Returns a single chord as [Stroke(main, mods)]; a combo that is
        only modifiers ('super') becomes a stroke of the last modifier."""
        ensure_unlocked()
        self.kbd  # ensures keymap
        tokens = _split_combo(combo)
        if not tokens:
            raise InputError(f"empty key combo {combo!r}")
        mods: list[int] = []
        for tok in tokens[:-1]:
            if tok.lower() not in MODIFIERS:
                raise InputError(f"{combo!r}: only the last key of a chord may be a non-modifier "
                                 f"({tok!r} is not one of {', '.join(sorted(set(MODIFIERS)))}); "
                                 "send chords one at a time")
            mods.append(MODIFIERS[tok.lower()])
        last = tokens[-1]
        # A trailing modifier is the key itself ("super" alone taps Super, "ctrl+shift" taps Shift with Ctrl).
        main = Stroke(MODIFIERS[last.lower()], ()) if last.lower() in MODIFIERS else self._resolve_token(last)
        allmods = tuple(dict.fromkeys(list(mods) + [m for m in main.mods if m not in mods]))
        return [Stroke(main.code, allmods)]

    def _resolve_token(self, tok: str) -> Stroke:
        km = self.keymap
        low = tok.lower()
        if len(tok) == 1:
            s = km.stroke_for_char(tok)
            if s:
                return s
            raise InputError(f"character {tok!r} is not on the {km.layout} layout; use type_text for it")
        if low in _CHAR_NAMES:
            s = km.stroke_for_char(_CHAR_NAMES[low])
            if s:
                return s
        if low in KEY_ALIASES and KEY_ALIASES[low] is not None:
            return Stroke(KEY_ALIASES[low], ())
        if low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
            return Stroke(getattr(e, f"KEY_F{int(low[1:])}"), ())
        name = "KEY_" + tok.upper().removeprefix("KEY_")
        if hasattr(e, name):
            return Stroke(getattr(e, name), ())
        s = km.stroke_for_name(tok)  # XKB keysym names: XF86AudioPlay, udiaeresis, dead_acute, KP_5...
        if s:
            return s
        raise InputError(f"unknown key {tok!r}")

    def key_down(self, code: int):
        ensure_unlocked()
        self.kbd.write(e.EV_KEY, code, 1)
        self.kbd.syn()
        self.held_keys.add(code)

    def key_up(self, code: int):
        self.kbd.write(e.EV_KEY, code, 0)
        self.kbd.syn()
        self.held_keys.discard(code)

    def tap(self, stroke: Stroke, hold: float | None = None):
        with self._lock:
            pressed = []
            try:
                for m in stroke.mods:
                    if m not in self.held_keys:
                        self.key_down(m)
                        pressed.append(m)
                        time.sleep(self.key_delay)
                self.key_down(stroke.code)
                time.sleep(hold if hold is not None else self.key_delay)
                self.key_up(stroke.code)
            finally:
                for m in reversed(pressed):
                    time.sleep(self.key_delay / 2)
                    self.key_up(m)
            time.sleep(self.key_delay)

    def press_combo(self, combo: str, repeat: int = 1, hold: float | None = None):
        strokes = self.resolve(combo)
        for _ in range(max(1, repeat)):
            for s in strokes:
                self.tap(s, hold)

    def hold_combo(self, combo: str, down: bool):
        (s,) = self.resolve(combo)
        codes = list(s.mods) + [s.code]
        with self._lock:
            if down:
                for c in codes:
                    if c not in self.held_keys:
                        self.key_down(c)
                        time.sleep(self.key_delay)
            else:
                for c in reversed(codes):
                    if c in self.held_keys:
                        self.key_up(c)
                        time.sleep(self.key_delay)

    def type_text(self, text: str, method: str = "auto", delay: float | None = None) -> str:
        """Type text. auto = real key events for everything on the layout, wtype for the rest."""
        delay = self.key_delay if delay is None else delay
        if method == "wtype":
            _wtype(text, delay)
            return "wtype"
        self.kbd
        km = self.keymap
        used = set()
        run: list[str] = []

        def flush():
            if run:
                _wtype("".join(run), delay)
                run.clear()
                used.add("wtype")

        old = self.key_delay
        self.key_delay = delay
        try:
            for ch in text:
                s = km.stroke_for_char(ch)
                if s is None:
                    if method == "keys":
                        raise InputError(f"character {ch!r} is not on the {km.layout} layout (method='keys')")
                    run.append(ch)
                    continue
                flush()
                self.tap(s)
                used.add("keys")
            flush()
        finally:
            self.key_delay = old
        return "+".join(sorted(used)) or "nothing"

    # ------------------------------------------------------------ pointer

    def move_to(self, x: float, y: float, duration: float = 0.15, steps: int | None = None):
        """Move the pointer to layout coords with real relative motion events (hover, drag targets,
        canvas input hooks all see it), then correct any residual exactly with a warp."""
        ensure_unlocked()
        with self._lock:
            ptr = self.ptr
            cx, cy = hypr.cursor_pos()
            dx, dy = x - cx, y - cy
            dist = math.hypot(dx, dy)
            if dist >= 1:
                n = steps or max(1, min(60, int(dist / 25)))
                if duration <= 0:
                    n = 1
                sent_x = sent_y = 0
                for i in range(1, n + 1):
                    t = _ease(i / n)
                    tx, ty = round(dx * t), round(dy * t)
                    ptr.write(e.EV_REL, e.REL_X, tx - sent_x)
                    ptr.write(e.EV_REL, e.REL_Y, ty - sent_y)
                    ptr.syn()
                    sent_x, sent_y = tx, ty
                    if duration > 0:
                        time.sleep(duration / n)
            time.sleep(0.01)
            fx, fy = hypr.cursor_pos()
            if abs(fx - x) > 0.75 or abs(fy - y) > 0.75:
                # Blocked by a monitor gap / pointer constraint / acceleration: place it exactly.
                hypr.warp_cursor(x, y)
                time.sleep(0.01)
                # A 1px out-and-back nudge so clients get a motion event at the final spot.
                ptr.write(e.EV_REL, e.REL_X, 1)
                ptr.syn()
                ptr.write(e.EV_REL, e.REL_X, -1)
                ptr.syn()
                time.sleep(0.01)

    def button(self, name: str, down: bool):
        code = BUTTONS.get(name.lower())
        if code is None:
            raise InputError(f"unknown button {name!r}; use one of {', '.join(BUTTONS)}")
        if down:
            ensure_unlocked()
        with self._lock:
            self.ptr.write(e.EV_KEY, code, 1 if down else 0)
            self.ptr.syn()
            (self.held_buttons.add if down else self.held_buttons.discard)(code)

    def click(self, button: str = "left", count: int = 1, hold: float = 0.04, interval: float = 0.07):
        for i in range(max(1, count)):
            self.button(button, True)
            time.sleep(hold)
            self.button(button, False)
            if i < count - 1:
                time.sleep(interval)

    def scroll(self, dy: float = 0.0, dx: float = 0.0, step_delay: float = 0.03):
        """dy > 0 scrolls down (content moves up, like a wheel turned towards you), dx > 0 scrolls right.
        Units are wheel notches; fractions use hi-res events."""
        ensure_unlocked()
        with self._lock:
            ptr = self.ptr
            for axis_lo, axis_hi, amount, sign in ((e.REL_WHEEL, e.REL_WHEEL_HI_RES, dy, -1),
                                                    (e.REL_HWHEEL, e.REL_HWHEEL_HI_RES, dx, 1)):
                if not amount:
                    continue
                remaining = abs(amount)
                direction = sign * (1 if amount > 0 else -1)
                while remaining > 1e-6:
                    part = min(1.0, remaining)
                    hi = round(120 * part) * direction
                    if part >= 0.999:
                        ptr.write(e.EV_REL, axis_lo, direction)
                    ptr.write(e.EV_REL, axis_hi, hi)
                    ptr.syn()
                    remaining -= part
                    time.sleep(step_delay)

    # ------------------------------------------------------------ touchpad gestures

    def gesture(self, kind: str, fingers: int = 3, dx: float = 0, dy: float = 0, scale: float = 1.0,
                rotate: float = 0.0, duration: float = 0.35, hold_end: float = 0.0):
        """Multi-finger touchpad gesture. dx/dy in fraction of pad size for swipes (1.0 = full width).
        pinch: scale > 1 spreads fingers (zoom in), < 1 pinches together; rotate in degrees."""
        ensure_unlocked()
        fingers = max(1, min(5, fingers))
        pad = self.pad
        W, H = self._pad_size
        cx, cy = W / 2, H / 2
        radius = min(W, H) * 0.18
        start = []
        for i in range(fingers):
            a = 2 * math.pi * i / fingers - math.pi / 2
            start.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
        tool = {1: e.BTN_TOOL_FINGER, 2: e.BTN_TOOL_DOUBLETAP, 3: e.BTN_TOOL_TRIPLETAP,
                4: e.BTN_TOOL_QUADTAP, 5: e.BTN_TOOL_QUINTTAP}[fingers]
        n = max(8, int(duration / 0.008))

        def pos(t):
            out = []
            for (sx, sy) in start:
                if kind == "pinch":
                    vx, vy = sx - cx, sy - cy
                    s = 1 + (scale - 1) * t
                    r = math.radians(rotate) * t
                    rx = vx * math.cos(r) - vy * math.sin(r)
                    ry = vx * math.sin(r) + vy * math.cos(r)
                    out.append((cx + rx * s, cy + ry * s))
                else:
                    out.append((sx + dx * W * t, sy + dy * H * t))
            return [(min(max(px, 0), W), min(max(py, 0), H)) for px, py in out]

        def frame(points, first=False):
            for slot, (px, py) in enumerate(points):
                pad.write(e.EV_ABS, e.ABS_MT_SLOT, slot)
                if first:
                    pad.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, 100 + slot)
                pad.write(e.EV_ABS, e.ABS_MT_POSITION_X, int(px))
                pad.write(e.EV_ABS, e.ABS_MT_POSITION_Y, int(py))
            pad.write(e.EV_ABS, e.ABS_X, int(points[0][0]))
            pad.write(e.EV_ABS, e.ABS_Y, int(points[0][1]))
            if first:
                pad.write(e.EV_KEY, e.BTN_TOUCH, 1)
                pad.write(e.EV_KEY, tool, 1)
            pad.syn()

        with self._lock:
            frame(pos(0), first=True)
            time.sleep(0.03)
            for i in range(1, n + 1):
                frame(pos(_ease(i / n)))
                time.sleep(duration / n)
            if hold_end:
                time.sleep(hold_end)
            for slot in range(fingers):
                pad.write(e.EV_ABS, e.ABS_MT_SLOT, slot)
                pad.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
            pad.write(e.EV_KEY, e.BTN_TOUCH, 0)
            pad.write(e.EV_KEY, tool, 0)
            pad.syn()

    # ------------------------------------------------------------ safety

    def release_all(self) -> list[str]:
        released = []
        with self._lock:
            if self._kbd:
                for c in list(self.held_keys):
                    self.key_up(c)
                    released.append(_keyname(c))
            if self._ptr:
                for b in list(self.held_buttons):
                    self._ptr.write(e.EV_KEY, b, 0)
                    self._ptr.syn()
                    released.append(e.BTN.get(b, str(b)) if isinstance(e.BTN.get(b), str) else str(b))
                self.held_buttons.clear()
        return released


def _split_combo(combo: str) -> list[str]:
    combo = combo.strip()
    if combo in ("+", "-"):
        return [combo]
    parts = combo.replace(" + ", "+").split("+")
    out = []
    for i, p in enumerate(parts):
        if p == "" and i == len(parts) - 1 and out:
            out.append("+")  # "ctrl++"
        elif p:
            out.append(p.strip())
    return out


def _ease(t: float) -> float:
    return t * t * (3 - 2 * t)


def _keyname(code: int) -> str:
    n = e.KEY.get(code, str(code))
    return n[0] if isinstance(n, list) else n


def _safe_active():
    try:
        return hypr.query("activewindow")
    except Exception:
        return None


def _wtype(text: str, delay: float):
    ensure_unlocked()
    if not shutil.which("wtype"):
        raise InputError("wtype is not installed; cannot type characters outside the keyboard layout")
    args = ["wtype", "-d", str(max(0, int(delay * 1000))), "--", text]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise InputError(f"wtype failed: {r.stderr.strip()}")
