"""Keysym/character -> (evdev keycode, modifiers) for the active XKB layout, via libxkbcommon (ctypes).

uinput sends physical keycodes; the compositor turns them into symbols with the user's layout
(e.g. Swiss German, where 'z' sits on KEY_Y and '@' is AltGr+2). This module inverts that map.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from dataclasses import dataclass

from evdev import ecodes as e

_lib = ctypes.CDLL(ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0")


class _RuleNames(ctypes.Structure):
    _fields_ = [(n, ctypes.c_char_p) for n in ("rules", "model", "layout", "variant", "options")]


_lib.xkb_context_new.restype = ctypes.c_void_p
_lib.xkb_context_new.argtypes = [ctypes.c_int]
_lib.xkb_keymap_new_from_names.restype = ctypes.c_void_p
_lib.xkb_keymap_new_from_names.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RuleNames), ctypes.c_int]
_lib.xkb_keymap_min_keycode.argtypes = [ctypes.c_void_p]
_lib.xkb_keymap_min_keycode.restype = ctypes.c_uint32
_lib.xkb_keymap_max_keycode.argtypes = [ctypes.c_void_p]
_lib.xkb_keymap_max_keycode.restype = ctypes.c_uint32
_lib.xkb_keymap_num_layouts.argtypes = [ctypes.c_void_p]
_lib.xkb_keymap_num_layouts.restype = ctypes.c_uint32
_lib.xkb_keymap_num_levels_for_key.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
_lib.xkb_keymap_num_levels_for_key.restype = ctypes.c_uint32
_lib.xkb_keymap_key_get_syms_by_level.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.POINTER(ctypes.c_uint32))]
_lib.xkb_keymap_key_get_syms_by_level.restype = ctypes.c_int
_lib.xkb_keymap_key_get_mods_for_level.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t]
_lib.xkb_keymap_key_get_mods_for_level.restype = ctypes.c_size_t
_lib.xkb_keymap_num_mods.argtypes = [ctypes.c_void_p]
_lib.xkb_keymap_num_mods.restype = ctypes.c_uint32
_lib.xkb_keymap_mod_get_name.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
_lib.xkb_keymap_mod_get_name.restype = ctypes.c_char_p
_lib.xkb_utf32_to_keysym.argtypes = [ctypes.c_uint32]
_lib.xkb_utf32_to_keysym.restype = ctypes.c_uint32
_lib.xkb_keysym_from_name.argtypes = [ctypes.c_char_p, ctypes.c_int]
_lib.xkb_keysym_from_name.restype = ctypes.c_uint32
_lib.xkb_keysym_get_name.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t]
_lib.xkb_keysym_get_name.restype = ctypes.c_int
_lib.xkb_keymap_unref.argtypes = [ctypes.c_void_p]
_lib.xkb_keymap_get_as_string.argtypes = [ctypes.c_void_p, ctypes.c_int]
_lib.xkb_keymap_get_as_string.restype = ctypes.c_void_p
_lib.xkb_state_new.argtypes = [ctypes.c_void_p]
_lib.xkb_state_new.restype = ctypes.c_void_p
_lib.xkb_state_update_key.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int]
_lib.xkb_state_update_key.restype = ctypes.c_int
_lib.xkb_state_serialize_mods.argtypes = [ctypes.c_void_p, ctypes.c_int]
_lib.xkb_state_serialize_mods.restype = ctypes.c_uint32
_lib.xkb_state_serialize_layout.argtypes = [ctypes.c_void_p, ctypes.c_int]
_lib.xkb_state_serialize_layout.restype = ctypes.c_uint32
_libc = ctypes.CDLL(None)
_libc.free.argtypes = [ctypes.c_void_p]

KEYSYM_CASE_INSENSITIVE = 1

# Real modifier name -> evdev key that produces it (Mod5 is AltGr / ISO_Level3_Shift on most layouts).
_MOD_KEYS = {
    "Shift": e.KEY_LEFTSHIFT,
    "Control": e.KEY_LEFTCTRL,
    "Mod1": e.KEY_LEFTALT,
    "Mod4": e.KEY_LEFTMETA,
    "Mod5": e.KEY_RIGHTALT,
    "Mod3": None,
    "Lock": None,
    "Mod2": None,  # NumLock
}


@dataclass(frozen=True)
class Stroke:
    code: int  # evdev keycode
    mods: tuple[int, ...]  # evdev keycodes of modifiers to hold


class Keymap:
    def __init__(self, layout: str = "us", variant: str = "", model: str = "pc105", options: str = "", rules: str = "evdev"):
        self.layout, self.variant = layout, variant
        self._ctx = _lib.xkb_context_new(0)
        names = _RuleNames(rules.encode(), model.encode(), layout.encode(), variant.encode(), options.encode())
        km = _lib.xkb_keymap_new_from_names(self._ctx, ctypes.byref(names), 0)
        if not km:
            raise ValueError(f"cannot compile XKB keymap layout={layout!r} variant={variant!r}")
        self._map: dict[int, Stroke] = {}
        mod_names = [(_lib.xkb_keymap_mod_get_name(km, i) or b"").decode() for i in range(_lib.xkb_keymap_num_mods(km))]
        lo, hi = _lib.xkb_keymap_min_keycode(km), _lib.xkb_keymap_max_keycode(km)
        masks = (ctypes.c_uint32 * 16)()
        for kc in range(lo, hi + 1):
            evcode = kc - 8
            if evcode <= 0 or evcode >= e.BTN_MISC and evcode < e.KEY_OK:
                continue
            for lvl in range(_lib.xkb_keymap_num_levels_for_key(km, kc, 0)):
                syms = ctypes.POINTER(ctypes.c_uint32)()
                n = _lib.xkb_keymap_key_get_syms_by_level(km, kc, 0, lvl, ctypes.byref(syms))
                if n != 1:
                    continue
                sym = syms[0]
                nm = _lib.xkb_keymap_key_get_mods_for_level(km, kc, 0, lvl, masks, 16)
                best = None
                for i in range(nm):
                    mods = []
                    ok = True
                    for bit, name in enumerate(mod_names):
                        if masks[i] & (1 << bit):
                            key = _MOD_KEYS.get(name)
                            if key is None:
                                ok = False
                                break
                            mods.append(key)
                    if ok and (best is None or len(mods) < len(best)):
                        best = mods
                if best is None:
                    continue
                stroke = Stroke(evcode, tuple(best))
                old = self._map.get(sym)
                # Prefer fewer modifiers, then the main block over keypad keys.
                if old is None or (len(stroke.mods), _is_keypad(stroke.code)) < (len(old.mods), _is_keypad(old.code)):
                    self._map[sym] = stroke
        self._km = km

    def as_string(self) -> str:
        """Full XKB keymap text (for a Wayland virtual keyboard)."""
        p = _lib.xkb_keymap_get_as_string(self._km, 1)
        try:
            return ctypes.string_at(p).decode()
        finally:
            _libc.free(p)

    def new_state(self) -> "ModState":
        return ModState(self._km)

    def stroke_for_keysym(self, sym: int) -> Stroke | None:
        return self._map.get(sym)

    def stroke_for_char(self, ch: str) -> Stroke | None:
        if ch == "\n":
            return Stroke(e.KEY_ENTER, ())
        if ch == "\t":
            return Stroke(e.KEY_TAB, ())
        sym = _lib.xkb_utf32_to_keysym(ord(ch))
        return self._map.get(sym) if sym else None

    @staticmethod
    def keysym_from_name(name: str) -> int:
        sym = _lib.xkb_keysym_from_name(name.encode(), 0)
        if not sym:
            sym = _lib.xkb_keysym_from_name(name.encode(), KEYSYM_CASE_INSENSITIVE)
        return sym

    def stroke_for_name(self, name: str) -> Stroke | None:
        sym = self.keysym_from_name(name)
        return self._map.get(sym) if sym else None


def _is_keypad(code: int) -> bool:
    return code in _KEYPAD


_KEYPAD = {getattr(e, n) for n in dir(e) if n.startswith("KEY_KP")}


class ModState:
    """Tracks modifier state from key presses, serialized for zwp_virtual_keyboard.modifiers."""

    def __init__(self, km):
        self._st = _lib.xkb_state_new(km)

    def update(self, evcode: int, down: bool) -> tuple[int, int, int, int]:
        _lib.xkb_state_update_key(self._st, evcode + 8, 1 if down else 0)  # XKB_KEY_UP=0, XKB_KEY_DOWN=1
        return (_lib.xkb_state_serialize_mods(self._st, 1), _lib.xkb_state_serialize_mods(self._st, 2),
                _lib.xkb_state_serialize_mods(self._st, 4), _lib.xkb_state_serialize_layout(self._st, 64))
