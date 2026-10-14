"""Input for an agent desktop: Wayland virtual pointer/keyboard on the nested compositor's own seat.
Same interface as VirtualInput (uinput), but nothing reaches the user's seat."""

from __future__ import annotations

import math
import time

from evdev import ecodes as e

from . import hypr
from .vinput import BUTTONS, InputError, VirtualInput, _ease, _wtype
from .wlvirt import Connection, VirtualKeyboard, VirtualPointer
from .xkb import Keymap


class AgentInput(VirtualInput):
    guard_lock = False  # the agent desktop has no lock screen; the user's lock doesn't matter here

    def __init__(self, wayland_display: str, env: dict, layout: str = "us", variant: str = "",
                 screen: tuple[int, int, int, int] = (0, 0, 1920, 1080)):
        super().__init__()
        self.display = wayland_display
        self.env = env
        self.layout, self.variant = layout, variant
        self.screen = screen  # agent layout box the absolute pointer spans
        self._conn: Connection | None = None
        self._vk: VirtualKeyboard | None = None
        self._vp: VirtualPointer | None = None
        self._mods = None

    def _c(self) -> Connection:
        if self._conn is None:
            self._conn = Connection(self.display)
        return self._conn

    @property
    def kbd(self):
        with self._lock:
            if self._vk is None:
                self.keymap = Keymap(self.layout, self.variant)
                self._vk = VirtualKeyboard(self._c(), self.keymap.as_string())
                self._mods = self.keymap.new_state()
            return self._vk

    @property
    def ptr(self):
        with self._lock:
            if self._vp is None:
                self._vp = VirtualPointer(self._c())
            return self._vp

    @property
    def pad(self):
        raise InputError("touchpad gestures are not available on an agent desktop (no virtual touchpad protocol); "
                         "use keys or the hyprland tool for workspace changes")

    def _wtype(self, text: str, delay: float):
        _wtype(text, delay, self.env, guard=False)

    def close(self):
        try:
            self.release_all()
        except Exception:
            pass
        if self._conn:
            self._conn.close()
        self._conn = self._vk = self._vp = None

    # ------------------------------------------------------------ keys

    def key_down(self, code: int):
        kb = self.kbd
        kb.key(code, True)
        kb.modifiers(*self._mods.update(code, True))
        self._c().roundtrip()
        self.held_keys.add(code)

    def key_up(self, code: int):
        kb = self.kbd
        kb.key(code, False)
        kb.modifiers(*self._mods.update(code, False))
        self._c().roundtrip()
        self.held_keys.discard(code)

    # ------------------------------------------------------------ pointer

    def _extents(self) -> tuple[float, float, float, float]:
        """Absolute pointer motion spans the whole layout of the agent compositor."""
        try:
            boxes = [hypr.monitor_box(m) for m in hypr.screens(hypr.query("monitors"))]
            if boxes:
                x0 = min(b[0] for b in boxes)
                y0 = min(b[1] for b in boxes)
                return x0, y0, max(b[0] + b[2] for b in boxes) - x0, max(b[1] + b[3] for b in boxes) - y0
        except Exception:
            pass
        return self.screen

    def move_to(self, x: float, y: float, duration: float = 0.15, steps: int | None = None):
        with self._lock:
            vp = self.ptr
            bx, by, bw, bh = self._extents()
            ext = (int(round(bw)), int(round(bh)))
            try:
                cx, cy = hypr.cursor_pos()
            except Exception:
                cx, cy = x, y
            dist = math.hypot(x - cx, y - cy)
            n = 1 if duration <= 0 or dist < 2 else (steps or max(2, min(60, int(dist / 25))))
            for i in range(1, n + 1):
                t = _ease(i / n)
                vp.motion_absolute(cx + (x - cx) * t - bx, cy + (y - cy) * t - by, *ext)
                if n > 1:
                    time.sleep(duration / n)
            self._c().roundtrip()
            time.sleep(0.01)
            try:
                fx, fy = hypr.cursor_pos()
            except Exception:
                return
            if abs(fx - x) > 1 or abs(fy - y) > 1:
                # Correct for rounding/scaling in the compositor's absolute mapping.
                vp.motion_absolute(x - bx + (x - fx), y - by + (y - fy), *ext)
                self._c().roundtrip()
                time.sleep(0.01)

    def button(self, name: str, down: bool):
        code = BUTTONS.get(name.lower())
        if code is None:
            raise InputError(f"unknown button {name!r}; use one of {', '.join(BUTTONS)}")
        with self._lock:
            self.ptr.button(code, down)
            self._c().roundtrip()
            (self.held_buttons.add if down else self.held_buttons.discard)(code)

    def scroll(self, dy: float = 0.0, dx: float = 0.0, step_delay: float = 0.03):
        with self._lock:
            vp = self.ptr
            for axis, amount in ((0, dy), (1, dx)):
                remaining = abs(amount)
                sign = 1 if amount > 0 else -1
                while remaining > 1e-6:
                    part = min(1.0, remaining)
                    vp.axis(axis, 15.0 * part * sign, sign if part >= 0.999 else None)
                    remaining -= part
                    time.sleep(step_delay)
            self._c().roundtrip()

    def release_all(self) -> list[str]:
        released = []
        with self._lock:
            if self._vk:
                for c in list(self.held_keys):
                    self.key_up(c)
                    n = e.KEY.get(c, str(c))
                    released.append(n[0] if isinstance(n, list) else n)
            if self._vp:
                for b in list(self.held_buttons):
                    self._vp.button(b, False)
                    released.append(str(b))
                self.held_buttons.clear()
                self._c().roundtrip()
        return released
