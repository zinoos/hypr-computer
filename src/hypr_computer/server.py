"""hypr-computer: full computer use for Hyprland (Wayland) as an MCP server."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from configparser import ConfigParser
from glob import glob
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image
from pydantic import Field

from . import hypr
from .a11y import A11y, A11yError
from .hypr import HyprError
from .screen import Frame, Frames, ScreenError
from . import screen as scr
from .vinput import InputError, VirtualInput, session_locked

INSTRUCTIONS = """\
Drive the user's Hyprland desktop like a person at the keyboard: see the screen, move/click/drag/scroll
the mouse, type, press any key combo (including compositor binds like super+1), do touchpad gestures,
manage windows, read text (OCR) and operate widgets through the accessibility tree.

Coordinates: every screenshot/zoom returns a frame id (f1, f2, ...). x/y you pass to click, move,
drag, scroll refer to the pixels of the most recent frame unless you pass frame= or coords='screen'
(Hyprland layout coordinates). Instead of pixels you can target text='Save' (OCR finds it, add
index= to pick among several) or ref='e12' (an element from a11y_tree) or ref='t3' (an ocr hit).

Good loop: screenshot -> act (with screenshot_after=true to see the result) -> verify.
Prefer a11y_tree + a11y_act for apps that expose accessibility (GTK, Qt, LibreOffice, Firefox):
it's exact and needs no pixels. Use zoom on small UI before clicking tiny targets.
The user may be using the machine: keep actions purposeful, and stop if they take over.
"""

mcp = MCPServer("hypr-computer", instructions=INSTRUCTIONS)
frames = Frames()
vin = VirtualInput()
ax = A11y()
_ocr_hits: dict[str, scr.TextHit] = {}
_ocr_counter = [0]
_ocr_cache: dict[tuple, list] = {}

Coords = Literal["image", "screen"]


# ====================================================================== helpers


def _err(ex: Exception) -> str:
    return f"error: {ex}"


def _monitors() -> list[dict]:
    return [m for m in hypr.query("monitors") if not m.get("disabled")]


def _focused_monitor(mons: list[dict]) -> dict:
    cx, cy = hypr.cursor_pos()
    return next((m for m in mons if m.get("focused")), None) or hypr.monitor_for_point(mons, cx, cy) or mons[0]


def _pick_monitor(name: str | None, mons: list[dict]) -> dict:
    if not name or name in ("focused", "active", "current"):
        return _focused_monitor(mons)
    if name == "cursor":
        cx, cy = hypr.cursor_pos()
        return hypr.monitor_for_point(mons, cx, cy) or _focused_monitor(mons)
    for m in mons:
        if m["name"].lower() == name.lower() or str(m["id"]) == name:
            return m
    for m in mons:
        if name.lower() in (m.get("description") or "").lower():
            return m
    raise ScreenError(f"no monitor {name!r}; monitors: {', '.join(m['name'] for m in mons)}")


def _union(boxes):
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return x0, y0, x1 - x0, y1 - y0


def _capture(monitor: str | None = None, window: str | None = None, region: list[float] | None = None,
             region_coords: Coords = "image", region_frame: str | None = None,
             cursor: bool = False) -> tuple[Any, tuple[float, float, float, float], str]:
    """Returns (full-res image, layout box, label)."""
    mons = _monitors()
    if region:
        if len(region) != 4:
            raise ScreenError("region must be [x, y, width, height]")
        x, y, w, h = region
        if region_coords == "image":
            f = frames.get(region_frame)
            x, y = f.to_layout(x, y)
            w, h = w * f.w / f.img_w, h * f.h / f.img_h
        box = (x, y, w, h)
        return scr.grab(box=box, cursor=cursor), box, f"region {round(x)},{round(y)} {round(w)}x{round(h)}"
    if window:
        win = hypr.find_window(window)
        canvas = hypr.canvas_state(mons)
        box = hypr.window_screen_box(win, canvas)
        vis = hypr.visible_part(box, mons)
        if vis is None:
            raise ScreenError(f"window {win['class']} {win['title'][:40]!r} is not on any screen right now "
                              "(other workspace / off-canvas): focus it first with window(action='focus')")
        label = f"window {win['class']} {win['title'][:50]!r}" + (" (partly off-screen)" if vis != box else "")
        return scr.grab(box=vis, cursor=cursor), vis, label
    if monitor == "all":
        boxes = [hypr.monitor_box(m) for m in mons]
        return scr.grab(cursor=cursor), _union(boxes), "all monitors"
    m = _pick_monitor(monitor, mons)
    return scr.grab(output=m["name"], cursor=cursor), hypr.monitor_box(m), f"monitor {m['name']}"


def _make_frame(img, box, label, grid: bool = False, max_size: int | None = None,
                fmt: str = "jpeg", upscale_small: bool = False) -> tuple[Frame, list]:
    x, y, w, h = box
    if max_size:
        tw, th = scr.fit_size(img.width, img.height, max_edge=min(max_size, scr.MAX_EDGE))
    else:
        tw, th = scr.fit_size(img.width, img.height)
    if upscale_small:
        # Zooms of small regions: enlarge up to the size limit so small text is legible.
        s = min(scr.MAX_EDGE / max(img.width, img.height), (scr.MAX_PIXELS / (img.width * img.height)) ** 0.5, 4.0)
        if s > 1:
            tw, th = int(img.width * s), int(img.height * s)
    shown = img.resize((tw, th), scr.Image.LANCZOS) if (tw, th) != img.size else img
    f = frames.new(x=x, y=y, w=w, h=h, img_w=tw, img_h=th, label=label, image=img)
    try:
        cx, cy = hypr.cursor_pos()
        cpos = f.from_layout(cx, cy)
    except Exception:
        cpos = None
    if grid:
        shown = scr.draw_grid(shown, 100 if max(tw, th) > 700 else 50)
    data, kind = scr.encode(shown, fmt)
    k = w / tw
    desc = (f"frame {f.id}: {label}, layout box {round(x)},{round(y)} {round(w)}x{round(h)}, "
            f"shown at {tw}x{th}" + (f" (1 image px = {k:.3f} screen px)" if abs(k - 1) > 0.01 else "") + ". "
            f"Coordinates you pass now refer to this image.")
    if cpos:
        desc += f" Mouse cursor is at ({round(cpos[0])}, {round(cpos[1])}) in this image."
    locker = session_locked()
    if locker:
        desc += f" NOTE: the session is LOCKED ({locker}); input is refused until the user unlocks."
    return f, [desc, Image(data=data, format=kind)]


def _resolve_point(x: float | None = None, y: float | None = None, ref: str | None = None,
                   text: str | None = None, index: int = 1, frame: str | None = None,
                   coords: Coords = "image", within: str | None = None) -> tuple[float, float, str]:
    """Any target -> layout coords + a description."""
    if ref:
        if ref.startswith("t") and ref in _ocr_hits:
            h = _ocr_hits[ref]
            bx, by, bw, bh = h.box
            return bx + bw / 2, by + bh / 2, f"OCR hit {ref} {h.text!r}"
        if ref.startswith("e"):
            box = ax.screen_box(ref)
            if box is None:
                raise A11yError(f"{ref} has no usable on-screen position (toolkit doesn't report it); "
                                f"use a11y_act(ref='{ref}', action='click') instead")
            bx, by, bw, bh = box
            return bx + bw / 2, by + bh / 2, f"element {ref}"
        raise ScreenError(f"unknown ref {ref!r}")
    if text:
        hits = _find_text(text, window=within, monitor=None, index=index)
        h = hits[0]
        bx, by, bw, bh = h.box
        return bx + bw / 2, by + bh / 2, f"text {h.text!r}"
    if x is None or y is None:
        raise ScreenError("give x and y, or ref=, or text=")
    if coords == "screen":
        return float(x), float(y), f"screen ({x},{y})"
    f = frames.get(frame)
    lx, ly = f.to_layout(x, y)
    return lx, ly, f"({x},{y}) in {f.id}"


def _find_text(query: str, window: str | None = None, monitor: str | None = None, index: int = 1,
               regex: bool = False, exact: bool = False, all_hits: bool = False,
               region: list[float] | None = None, langs: str | None = None,
               captured: tuple | None = None) -> list[scr.TextHit]:
    img, box, label = captured or _capture(monitor=monitor, window=window, region=region)
    lines = _ocr_lines(img, langs)
    sx, sy = box[2] / img.width, box[3] / img.height
    hits = []
    for run, score in scr.find_in_lines(lines, query, regex=regex, exact=exact):
        rx, ry, rw, rh = scr.run_box(run)
        hits.append(scr.TextHit(" ".join(w.text for w in run),
                                (box[0] + rx * sx, box[1] + ry * sy, rw * sx, rh * sy),
                                min(w.conf for w in run), score))
    if not hits:
        raise ScreenError(f"text {query!r} not found on {label} by OCR. Check spelling, zoom in, or look at a "
                          "screenshot; OCR misses tiny/stylised text and icons.")
    if all_hits:
        return hits
    if index < 1 or index > len(hits):
        raise ScreenError(f"{len(hits)} matches for {query!r}; index must be 1..{len(hits)}")
    return [hits[index - 1]]


def _ocr_lines(img, langs: str | None = None):
    key = (img.size, img.resize((96, 54)).tobytes(), langs)
    if key in _ocr_cache:
        return _ocr_cache[key]
    lines = scr.ocr_image(img, langs=langs or scr.OCR_LANGS)
    if len(_ocr_cache) > 6:
        _ocr_cache.pop(next(iter(_ocr_cache)))
    _ocr_cache[key] = lines
    return lines


def _after(screenshot_after: bool, wait_ms: int, monitor: str | None = None) -> list:
    if wait_ms:
        time.sleep(wait_ms / 1000)
    if not screenshot_after:
        return []
    img, box, label = _capture(monitor=monitor or "cursor")
    _, content = _make_frame(img, box, label)
    return content


def _with_mods(modifiers: list[str] | None):
    class _Ctx:
        def __enter__(self_inner):
            self_inner.combo = "+".join(modifiers) if modifiers else None
            if self_inner.combo:
                vin.hold_combo(self_inner.combo, True)

        def __exit__(self_inner, *a):
            if self_inner.combo:
                vin.hold_combo(self_inner.combo, False)
    return _Ctx()


def _note() -> str:
    n = vin.created_note
    vin.created_note = None
    return f" ({n})" if n else ""


def _guard(fn):
    """Turn known errors into a readable message and always release held input on failure."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except (HyprError, ScreenError, InputError, A11yError, ValueError, TimeoutError) as ex:
            rel = vin.release_all()
            return _err(ex) + (f" (released held: {', '.join(rel)})" if rel else "")
        except Exception as ex:
            rel = vin.release_all()
            return f"error: {type(ex).__name__}: {ex}" + (f" (released held: {', '.join(rel)})" if rel else "")
    return wrapper


def tool(**kw):
    def deco(fn):
        return mcp.tool(structured_output=False, **kw)(_guard(fn))
    return deco


XArg = Annotated[float | None, Field(description="x in the current frame's image pixels (or layout px with coords='screen')")]
YArg = Annotated[float | None, Field(description="y, same space as x")]
RefArg = Annotated[str | None, Field(description="target an a11y element (e12) or OCR hit (t3) instead of x/y")]
TextArg = Annotated[str | None, Field(description="target visible text found by OCR instead of x/y, e.g. 'Save'")]
IndexArg = Annotated[int, Field(description="which OCR match when text= matches several (1 = best/first)")]
FrameArg = Annotated[str | None, Field(description="frame id the coordinates refer to (default: latest)")]
CoordsArg = Annotated[Coords, Field(description="'image' = pixels of the frame, 'screen' = Hyprland layout px")]
WithinArg = Annotated[str | None, Field(description="limit text= search to this window (class/title/address)")]
ShotArg = Annotated[bool, Field(description="return a screenshot of the cursor's monitor after acting")]
WaitArg = Annotated[int, Field(description="milliseconds to wait after acting (before screenshot_after)")]
ModsArg = Annotated[list[str] | None, Field(description="modifiers held during the action, e.g. ['ctrl'], ['shift','super']")]


# ====================================================================== seeing


@tool()
def screenshot(
    monitor: Annotated[str | None, Field(description="monitor name (eDP-1, ...), 'all', 'cursor'; default focused")] = None,
    window: Annotated[str | None, Field(description="capture only this window (class, title part, address, 'active')")] = None,
    region: Annotated[list[float] | None, Field(description="[x, y, w, h] in the latest frame's image px (or layout px with region_coords='screen')")] = None,
    region_coords: CoordsArg = "image",
    grid: Annotated[bool, Field(description="overlay a labelled coordinate grid to help pick exact points")] = False,
    cursor: Annotated[bool, Field(description="draw a marker where the mouse pointer is")] = False,
    max_size: Annotated[int | None, Field(description="max long edge in px (smaller = cheaper)")] = None,
):
    """See the screen. Returns the image plus a frame id; later x/y refer to this image's pixels."""
    img, box, label = _capture(monitor, window, region, region_coords, None)
    f, content = _make_frame(img, box, label, grid=grid, max_size=max_size)
    if cursor:
        cx, cy = hypr.cursor_pos()
        p = f.from_layout(cx, cy)
        if p:
            shown = scr.Image.open(__import__("io").BytesIO(content[1].data)).convert("RGB")
            data, kind = scr.encode(scr.draw_cursor(shown, *p))
            content[1] = Image(data=data, format=kind)
    return content


@tool()
def zoom(
    x: Annotated[float, Field(description="left of the region (latest frame's image px unless coords='screen')")],
    y: Annotated[float, Field(description="top of the region")],
    width: Annotated[float, Field(description="region width")],
    height: Annotated[float, Field(description="region height")],
    frame: FrameArg = None,
    coords: CoordsArg = "image",
    grid: bool = True,
):
    """Magnify part of the screen at full resolution (enlarged up to 4x) to read small text or hit tiny
    targets. Creates a new frame: coordinates afterwards refer to the zoomed image."""
    if coords == "image":
        f = frames.get(frame)
        lx, ly = f.to_layout(x, y)
        lw, lh = width * f.w / f.img_w, height * f.h / f.img_h
    else:
        lx, ly, lw, lh = x, y, width, height
    img = scr.grab(box=(lx, ly, lw, lh))
    _, content = _make_frame(img, (lx, ly, lw, lh), f"zoom {round(lx)},{round(ly)} {round(lw)}x{round(lh)}",
                             grid=grid, fmt="png", upscale_small=True)
    return content


@tool()
def ocr(
    query: Annotated[str | None, Field(description="text to find; omit to read all text")] = None,
    window: Annotated[str | None, Field(description="limit to this window")] = None,
    monitor: Annotated[str | None, Field(description="monitor name or 'all' (default focused)")] = None,
    region: Annotated[list[float] | None, Field(description="[x,y,w,h] in latest frame image px")] = None,
    regex: bool = False,
    exact: Annotated[bool, Field(description="whole-text match instead of substring")] = False,
    langs: Annotated[str | None, Field(description="tesseract languages, default 'eng' (e.g. 'eng+deu')")] = None,
    show: Annotated[bool, Field(description="return a screenshot with the hits outlined")] = False,
):
    """Read text on screen with OCR. With query: list matches as refs (t1, t2...) usable as click(ref=),
    with their positions in the latest frame. Without query: all text lines, top to bottom."""
    img, box, label = _capture(monitor=monitor, window=window, region=region)
    lines = _ocr_lines(img, langs)
    sx, sy = box[2] / img.width, box[3] / img.height
    last = frames.last
    if not query:
        out = [f"OCR of {label} ({len(lines)} lines; positions in layout px):"]
        for ws in lines:
            t = " ".join(w.text for w in ws)
            if sum(c.isalnum() for c in t) < 2:
                continue
            rx, ry, _, _ = scr.run_box(ws)
            out.append(f"  @{round(box[0] + rx * sx)},{round(box[1] + ry * sy)}  {t}")
        return "\n".join(out)
    try:
        hits = _find_text(query, regex=regex, exact=exact, all_hits=True, langs=langs, captured=(img, box, label))
    except ScreenError as ex:
        return str(ex)
    out = [f"{len(hits)} match(es) for {query!r} on {label}:"]
    marks = []
    for h in hits[:40]:
        _ocr_counter[0] += 1
        ref = f"t{_ocr_counter[0]}"
        _ocr_hits[ref] = h
        bx, by, bw, bh = h.box
        cx, cy = bx + bw / 2, by + bh / 2
        where = f"screen center ({round(cx)},{round(cy)})"
        if last:
            p = last.from_layout(cx, cy)
            if p:
                where += f", in {last.id} at ({round(p[0])},{round(p[1])})"
        out.append(f"  {ref} {h.text!r} {where}" + (f" score={h.score:.2f}" if h.score < 1 else ""))
        marks.append((ref, bx, by, bw, bh))
    if len(_ocr_hits) > 500:
        for k in list(_ocr_hits)[:200]:
            del _ocr_hits[k]
    if show:
        f, content = _make_frame(img, box, label)
        shown = scr.Image.open(__import__("io").BytesIO(content[1].data)).convert("RGB")
        k = f.img_w / f.w
        shown = scr.draw_marks(shown, [(r, (bx - f.x) * k, (by - f.y) * k, bw * k, bh * k) for r, bx, by, bw, bh in marks])
        data, kind = scr.encode(shown)
        return ["\n".join(out), content[0], Image(data=data, format=kind)]
    return "\n".join(out)


# ====================================================================== mouse


@tool()
def click(
    x: XArg = None, y: YArg = None, ref: RefArg = None, text: TextArg = None, index: IndexArg = 1,
    button: Annotated[Literal["left", "right", "middle", "back", "forward"], Field(description="mouse button")] = "left",
    count: Annotated[int, Field(description="2 = double click, 3 = triple click")] = 1,
    modifiers: ModsArg = None,
    frame: FrameArg = None, coords: CoordsArg = "image", within: WithinArg = None,
    screenshot_after: ShotArg = False, wait_ms: WaitArg = 150,
):
    """Move the mouse to a target and click it (any button, multi-click, with held modifiers)."""
    lx, ly, what = _resolve_point(x, y, ref, text, index, frame, coords, within)
    vin.move_to(lx, ly)
    with _with_mods(modifiers):
        vin.click(button, count)
    mods = "+".join(modifiers) + "+" if modifiers else ""
    return [f"{mods}{button} click x{count} at {what} -> screen ({round(lx)},{round(ly)}){_note()}"] + \
        _after(screenshot_after, wait_ms)


@tool()
def move_mouse(
    x: XArg = None, y: YArg = None, ref: RefArg = None, text: TextArg = None, index: IndexArg = 1,
    frame: FrameArg = None, coords: CoordsArg = "image", within: WithinArg = None,
    duration_ms: Annotated[int, Field(description="travel time; 0 = jump")] = 150,
    screenshot_after: ShotArg = False, wait_ms: WaitArg = 200,
):
    """Move the pointer (hover: tooltips, menus, focus-follows-mouse, edge triggers)."""
    lx, ly, what = _resolve_point(x, y, ref, text, index, frame, coords, within)
    vin.move_to(lx, ly, duration=duration_ms / 1000)
    return [f"pointer at {what} -> screen ({round(lx)},{round(ly)}){_note()}"] + _after(screenshot_after, wait_ms)


@tool()
def drag(
    to_x: Annotated[float | None, Field(description="drop x")] = None,
    to_y: Annotated[float | None, Field(description="drop y")] = None,
    from_x: Annotated[float | None, Field(description="start x (default: current pointer position)")] = None,
    from_y: Annotated[float | None, Field(description="start y")] = None,
    from_ref: Annotated[str | None, Field(description="start at element/OCR ref")] = None,
    from_text: Annotated[str | None, Field(description="start at OCR text")] = None,
    to_ref: Annotated[str | None, Field(description="drop on element/OCR ref")] = None,
    to_text: Annotated[str | None, Field(description="drop on OCR text")] = None,
    path: Annotated[list[list[float]] | None, Field(description="extra waypoints [[x,y],...] between start and drop")] = None,
    button: Literal["left", "right", "middle"] = "left",
    modifiers: ModsArg = None,
    duration_ms: Annotated[int, Field(description="total movement time")] = 600,
    hold_ms: Annotated[int, Field(description="pause after pressing and before releasing (some apps need it)")] = 120,
    frame: FrameArg = None, coords: CoordsArg = "image",
    screenshot_after: ShotArg = False, wait_ms: WaitArg = 200,
):
    """Press, move and release: drag-and-drop, selecting text, sliders, moving/resizing windows
    (e.g. modifiers=['super'] to move a window, button='right' + super to resize)."""
    if from_x is not None or from_ref or from_text:
        sx, sy, swhat = _resolve_point(from_x, from_y, from_ref, from_text, 1, frame, coords)
        vin.move_to(sx, sy)
    else:
        sx, sy = hypr.cursor_pos()
        swhat = "pointer"
    tx, ty, twhat = _resolve_point(to_x, to_y, to_ref, to_text, 1, frame, coords)
    pts = []
    for p in path or []:
        px, py, _ = _resolve_point(p[0], p[1], None, None, 1, frame, coords)
        pts.append((px, py))
    pts.append((tx, ty))
    seg = max(0.05, duration_ms / 1000 / len(pts))
    with _with_mods(modifiers):
        vin.button(button, True)
        try:
            time.sleep(hold_ms / 1000)
            # Get past drag thresholds first with a small move.
            vin.move_to(sx + (4 if tx >= sx else -4), sy + (4 if ty >= sy else -4), duration=0.05)
            for px, py in pts:
                vin.move_to(px, py, duration=seg)
            time.sleep(hold_ms / 1000)
        finally:
            vin.button(button, False)
    return [f"dragged {button} from {swhat} to {twhat} -> screen ({round(tx)},{round(ty)}){_note()}"] + \
        _after(screenshot_after, wait_ms)


@tool()
def scroll(
    direction: Annotated[Literal["up", "down", "left", "right"], Field(description="scroll direction")] = "down",
    amount: Annotated[float, Field(description="wheel notches (fractions allowed); ~3 lines each")] = 3,
    x: XArg = None, y: YArg = None, ref: RefArg = None, text: TextArg = None,
    modifiers: ModsArg = None,
    frame: FrameArg = None, coords: CoordsArg = "image",
    screenshot_after: ShotArg = False, wait_ms: WaitArg = 250,
):
    """Turn the mouse wheel, optionally after moving to a target. ctrl+scroll usually zooms,
    shift+scroll scrolls sideways."""
    where = "pointer"
    if x is not None or ref or text:
        lx, ly, where = _resolve_point(x, y, ref, text, 1, frame, coords)
        vin.move_to(lx, ly)
    dy = amount if direction == "down" else -amount if direction == "up" else 0
    dx = amount if direction == "right" else -amount if direction == "left" else 0
    with _with_mods(modifiers):
        vin.scroll(dy=dy, dx=dx)
    return [f"scrolled {direction} {amount} at {where}{_note()}"] + _after(screenshot_after, wait_ms)


@tool()
def mouse_button(
    action: Annotated[Literal["down", "up"], Field(description="press or release")],
    button: Literal["left", "right", "middle", "back", "forward"] = "left",
):
    """Low-level: hold or release a mouse button (combine with move_mouse for custom drags)."""
    vin.button(button, action == "down")
    return f"{button} {action}{_note()}"


# ====================================================================== keyboard


@tool()
def type_text(
    text: Annotated[str, Field(description="text to type; \\n presses Enter, \\t Tab")],
    method: Annotated[Literal["auto", "keys", "wtype", "paste"], Field(
        description="auto: real key presses for characters on the keyboard layout, wtype for the rest; "
                    "paste: put on clipboard and press ctrl+v (fast for long text; restores the clipboard)")] = "auto",
    delay_ms: Annotated[int, Field(description="pause between keys")] = 12,
    submit: Annotated[bool, Field(description="press Enter afterwards")] = False,
    screenshot_after: ShotArg = False, wait_ms: WaitArg = 100,
):
    """Type text into the focused widget."""
    if method == "paste":
        old = _clip_get_bytes()
        _clip_set(text)
        time.sleep(0.05)
        vin.press_combo("ctrl+v")
        time.sleep(0.15)
        if old is not None:
            _clip_restore(old)
        used = "paste"
    else:
        used = vin.type_text(text, method, delay_ms / 1000)
    if submit:
        vin.press_combo("Return")
    return [f"typed {len(text)} chars via {used}{' + Enter' if submit else ''}{_note()}"] + \
        _after(screenshot_after, wait_ms)


@tool()
def key(
    keys: Annotated[str, Field(description="chord(s) separated by spaces, e.g. 'ctrl+s', 'super+1', "
                                           "'alt+Tab alt+Tab', 'Escape', 'XF86AudioPlay', 'ctrl+shift+t', 'F11'")],
    repeat: Annotated[int, Field(description="press the whole sequence this many times")] = 1,
    hold_ms: Annotated[int | None, Field(description="hold the last key this long (long-press)")] = None,
    screenshot_after: ShotArg = False, wait_ms: WaitArg = 150,
):
    """Press keys / shortcuts exactly like the physical keyboard (compositor binds work too).
    Modifier names: ctrl, shift, alt, altgr, super. Keys: letters/digits/symbols of the layout, Return,
    Escape, Tab, BackSpace, Delete, Home, End, Page_Up, Up/Down/Left/Right, F1-F24, Print, Menu,
    media keys (XF86Audio*), any XKB keysym name."""
    chords = keys.split() if keys.strip() not in ("+", " ") else [keys.strip()]
    for _ in range(max(1, repeat)):
        for c in chords:
            vin.press_combo(c, hold=hold_ms / 1000 if hold_ms else None)
    return [f"pressed {keys}" + (f" x{repeat}" if repeat > 1 else "") + _note()] + _after(screenshot_after, wait_ms)


@tool()
def hold_key(
    keys: Annotated[str, Field(description="chord to hold or release, e.g. 'shift', 'ctrl+alt', 'w'")],
    action: Annotated[Literal["down", "up"], Field(description="press and keep held, or release")],
):
    """Low-level: keep keys held across other actions (e.g. hold shift while clicking, hold w in a game).
    Always release afterwards (release_all does everything)."""
    vin.hold_combo(keys, action == "down")
    return f"{keys} {action}; held now: {sorted(_held_names())}{_note()}"


@tool()
def release_all():
    """Release every key and mouse button this server is holding (safety reset)."""
    rel = vin.release_all()
    return f"released: {', '.join(rel) if rel else 'nothing was held'}"


def _held_names():
    from evdev import ecodes as e
    out = []
    for c in vin.held_keys:
        n = e.KEY.get(c, c)
        out.append(n[0] if isinstance(n, list) else n)
    return out


# ====================================================================== gestures


@tool()
def gesture(
    kind: Annotated[Literal["swipe", "pinch"], Field(description="touchpad gesture type")],
    fingers: Annotated[int, Field(description="1-5 fingers (3/4-finger swipes are the usual workspace gestures)")] = 3,
    direction: Annotated[Literal["left", "right", "up", "down"] | None, Field(description="swipe direction")] = None,
    distance: Annotated[float, Field(description="swipe length as a fraction of the touchpad (0.1-1)")] = 0.5,
    scale: Annotated[float, Field(description="pinch: >1 spreads fingers (zoom in), <1 pinches (zoom out)")] = 1.5,
    rotate: Annotated[float, Field(description="pinch rotation in degrees")] = 0.0,
    duration_ms: int = 350,
    screenshot_after: ShotArg = False, wait_ms: WaitArg = 400,
):
    """Perform a multi-finger touchpad gesture on a virtual touchpad (workspace swipes, pinch zoom)."""
    dx = dy = 0.0
    if kind == "swipe":
        if not direction:
            raise InputError("swipe needs direction")
        dx = {"left": -distance, "right": distance}.get(direction, 0.0)
        dy = {"up": -distance, "down": distance}.get(direction, 0.0)
    vin.gesture(kind, fingers, dx, dy, scale, rotate, duration_ms / 1000)
    return [f"{fingers}-finger {kind} {direction or ''}{_note()}"] + _after(screenshot_after, wait_ms)


# ====================================================================== batch


@tool()
def actions(
    steps: Annotated[list[dict[str, Any]], Field(description=(
        "list of steps, each {'do': <tool name>, ...that tool's arguments}. Allowed: click, move_mouse, drag, "
        "scroll, mouse_button, type_text, key, hold_key, gesture, wait (seconds), window, launch. Example: "
        "[{'do':'click','text':'File'},{'do':'click','text':'Save As'},{'do':'type_text','text':'notes.txt'},"
        "{'do':'key','keys':'Return'}]"))],
    stop_if_user_moves: Annotated[bool, Field(description="abort when the real mouse moves between steps")] = True,
    screenshot_after: ShotArg = True, wait_ms: WaitArg = 300,
):
    """Run several actions in one go (fewer round trips). Stops at the first error."""
    table = {"click": click, "move_mouse": move_mouse, "drag": drag, "scroll": scroll,
             "mouse_button": mouse_button, "type_text": type_text, "key": key, "hold_key": hold_key,
             "gesture": gesture, "window": window, "launch": launch}
    log = []
    expected = None
    for i, step in enumerate(steps, 1):
        step = dict(step)
        name = step.pop("do", None) or step.pop("action", None) or step.pop("tool", None)
        if expected is not None and stop_if_user_moves:
            cx, cy = hypr.cursor_pos()
            if abs(cx - expected[0]) > 3 or abs(cy - expected[1]) > 3:
                vin.release_all()
                log.append(f"{i}. stopped: the pointer moved by itself (user took over?)")
                break
        if name == "wait":
            time.sleep(float(step.get("seconds", step.get("ms", 500) / 1000)))
            log.append(f"{i}. waited")
            expected = hypr.cursor_pos()
            continue
        fn = table.get(name)
        if fn is None:
            log.append(f"{i}. unknown step {name!r}; stopped")
            break
        if name not in ("mouse_button", "hold_key"):
            step.setdefault("screenshot_after", False)
        res = fn(**step)
        text = res[0] if isinstance(res, list) else res
        log.append(f"{i}. {text}")
        if isinstance(text, str) and text.startswith("error"):
            break
        expected = hypr.cursor_pos()
    return ["\n".join(log)] + _after(screenshot_after, wait_ms)


# ====================================================================== windows & desktop


@tool()
def windows(
    all_workspaces: Annotated[bool, Field(description="include windows on hidden workspaces")] = True,
):
    """List windows: address, class, title, workspace, monitor, state and where they are on screen."""
    clients = hypr.query("clients")
    mons = _monitors()
    canvas = hypr.canvas_state(mons)
    active = (hypr.query("activewindow") or {}).get("address")
    last = frames.last
    rows = []
    for c in sorted(clients, key=lambda c: c["focusHistoryID"]):
        if not c.get("mapped", True):
            continue
        box = hypr.window_screen_box(c, canvas)
        vis = hypr.visible_part(box, mons)
        flags = [f for f, on in (("FOCUSED", c["address"] == active), ("floating", c["floating"]),
                                 ("fullscreen", c["fullscreen"]), ("pinned", c["pinned"]),
                                 ("xwayland", c["xwayland"]), ("hidden", c.get("hidden"))) if on]
        where = "off-screen"
        if vis:
            where = f"on screen at {round(vis[0])},{round(vis[1])} {round(vis[2])}x{round(vis[3])}"
            if last:
                p = last.from_layout(vis[0] + vis[2] / 2, vis[1] + vis[3] / 2)
                if p:
                    where += f" (center in {last.id}: {round(p[0])},{round(p[1])})"
        rows.append(f"{c['address']} {c['class']!r} {c['title'][:70]!r} ws={c['workspace']['name']} "
                    f"pid={c['pid']} {' '.join(flags)} {where}")
    head = f"{len(rows)} windows" + (" (Phantomat canvas active)" if canvas.active else "")
    return head + "\n" + "\n".join(rows)


@tool()
def window(
    target: Annotated[str, Field(description="window: address 0x..., class, title substring, or 'active'")],
    action: Annotated[Literal["focus", "close", "kill", "fullscreen", "maximize", "float", "pin", "move",
                              "resize", "center", "bring_to_top", "to_workspace"], Field(description="what to do")],
    x: Annotated[float | None, Field(description="move: x / resize: width")] = None,
    y: Annotated[float | None, Field(description="move: y / resize: height")] = None,
    workspace: Annotated[str | None, Field(description="to_workspace: workspace id or name")] = None,
    screenshot_after: ShotArg = False, wait_ms: WaitArg = 300,
):
    """Manage a window through Hyprland (focus also brings it on screen / flies the canvas to it)."""
    win = hypr.find_window(target)
    sel = f'"address:{win["address"]}"'
    d = {
        "focus": f"hl.dsp.focus({{window = {sel}}})",
        "close": f"hl.dsp.window.close({{window = {sel}}})",
        "kill": f"hl.dsp.window.kill({{window = {sel}}})",
        "fullscreen": f'hl.dsp.window.fullscreen({{action = "toggle", mode = 0, window = {sel}}})',
        "maximize": f'hl.dsp.window.fullscreen({{action = "toggle", mode = 1, window = {sel}}})',
        "float": f'hl.dsp.window.float({{action = "toggle", window = {sel}}})',
        "pin": f'hl.dsp.window.pin({{action = "toggle", window = {sel}}})',
        "center": f"hl.dsp.window.center({{window = {sel}}})",
        "bring_to_top": f"hl.dsp.window.bring_to_top({{window = {sel}}})",
    }
    if action in d:
        hypr.dispatch(d[action])
    elif action == "move":
        if x is None or y is None:
            raise ValueError("move needs x and y (layout px)")
        hypr.dispatch(f"hl.dsp.window.move({{x = {int(x)}, y = {int(y)}, window = {sel}}})")
    elif action == "resize":
        if x is None or y is None:
            raise ValueError("resize needs x (width) and y (height)")
        hypr.dispatch(f"hl.dsp.window.resize({{x = {int(x)}, y = {int(y)}, relative = false, window = {sel}}})")
    elif action == "to_workspace":
        if not workspace:
            raise ValueError("to_workspace needs workspace")
        hypr.dispatch(f"hl.dsp.window.move({{workspace = {hypr.lua_str(workspace)}, follow = false, window = {sel}}})")
    return [f"{action}: {win['class']} {win['title'][:60]!r}"] + _after(screenshot_after, wait_ms)


@tool()
def desktop():
    """Overview of the desktop: monitors, workspaces, focused window, pointer, canvas camera, layer
    surfaces (bars, launchers, notifications), keyboard layout and submap."""
    mons = _monitors()
    out = ["Monitors (layout coords):"]
    for m in mons:
        x, y, w, h = hypr.monitor_box(m)
        out.append(f"  {m['name']} {m.get('description', '')[:40]!r} at {round(x)},{round(y)} {round(w)}x{round(h)} "
                   f"scale={m['scale']} ws={m['activeWorkspace']['name']}" + (" FOCUSED" if m.get("focused") else ""))
    aw = hypr.query("activewindow") or {}
    out.append(f"Focused window: {aw.get('class')} {aw.get('title', '')[:80]!r} {aw.get('address', '')}")
    cx, cy = hypr.cursor_pos()
    out.append(f"Pointer: {round(cx)},{round(cy)}")
    ws = hypr.query("workspaces")
    out.append("Workspaces: " + ", ".join(f"{w['name']}({w['windows']} win, {w['monitor']})" for w in ws))
    canvas = hypr.canvas_state(mons)
    if canvas.active:
        out.append(f"Phantomat canvas: camera {round(canvas.view_x)},{round(canvas.view_y)} zoom {canvas.zoom:.2f}")
    try:
        layers = hypr.query("layers")
        names = []
        for mon, d in layers.items():
            for lvl, items in d.get("levels", {}).items():
                for it in items:
                    names.append(f"{it.get('namespace')}@{mon}/L{lvl}")
        out.append("Layer surfaces: " + ", ".join(sorted(set(names))))
    except Exception:
        pass
    try:
        dev = hypr.query("devices")
        kb = next((k for k in dev["keyboards"] if k.get("main")), dev["keyboards"][0])
        out.append(f"Keyboard: {kb.get('active_keymap')} (layout {kb.get('layout')} {kb.get('variant')})")
        out.append(f"Submap: {hypr.eval_lua('return hl.get_current_submap()') or 'default'}")
    except Exception:
        pass
    return "\n".join(out)


@tool()
def launch(
    command: Annotated[str | None, Field(description="shell command to run detached, e.g. 'nautilus ~/Downloads'")] = None,
    app: Annotated[str | None, Field(description="desktop app by name or id, e.g. 'Files', 'firefox'")] = None,
    wait_for_window: Annotated[bool, Field(description="wait up to 10 s for a new window to appear")] = True,
    accessibility: Annotated[bool, Field(description="start with accessibility on (adds Chromium/Electron/Qt flags) so a11y_tree works")] = False,
    screenshot_after: ShotArg = False, wait_ms: WaitArg = 300,
):
    """Start an application through Hyprland (so it opens like a launcher would)."""
    if not command and not app:
        raise ValueError("give command or app")
    env_prefix = ""
    if accessibility:
        ax.ensure_enabled()
        env_prefix = "env QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 ACCESSIBILITY_ENABLED=1 "
    if app:
        entry = _find_desktop_entry(app)
        if not entry:
            raise ValueError(f"no desktop app matching {app!r}; try apps(query=...)")
        cmd = _desktop_exec(entry)
        label = f"{entry['name']} ({os.path.basename(entry['path'])})"
    else:
        cmd = command
        label = command
    if accessibility and re.search(r"chrom|electron|code|brave|edge|slack|discord|obsidian", cmd, re.I):
        cmd += " --force-renderer-accessibility"
    before = {c["address"] for c in hypr.query("clients")}
    hypr.dispatch(f"hl.dsp.exec_cmd({hypr.lua_str(env_prefix + cmd)})")
    msg = f"launched {label}"
    if wait_for_window:
        t0 = time.time()
        while time.time() - t0 < 10:
            time.sleep(0.25)
            new = [c for c in hypr.query("clients") if c["address"] not in before]
            if new:
                c = new[0]
                msg += f"; new window {c['address']} {c['class']!r} {c['title'][:60]!r}"
                break
        else:
            msg += "; no new window within 10 s (it may reuse an existing one or still be starting)"
    return [msg] + _after(screenshot_after, wait_ms)


def _desktop_dirs():
    dirs = [os.path.expanduser("~/.local/share/applications"), os.path.expanduser("~/.nix-profile/share/applications")]
    for d in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"):
        dirs.append(os.path.join(d, "applications"))
    dirs += ["/var/lib/flatpak/exports/share/applications",
             os.path.expanduser("~/.local/share/flatpak/exports/share/applications")]
    seen, out = set(), []
    for d in dirs:
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def _desktop_entries() -> list[dict]:
    entries, ids = [], set()
    for d in _desktop_dirs():
        for path in glob(os.path.join(d, "**/*.desktop"), recursive=True):
            did = os.path.relpath(path, d).replace("/", "-")
            if did in ids:
                continue
            cp = ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(path, encoding="utf-8")
                s = cp["Desktop Entry"]
            except Exception:
                continue
            if s.get("Type", "Application") != "Application" or s.get("NoDisplay", "false") == "true" \
                    or s.get("Hidden", "false") == "true" or "Exec" not in s:
                continue
            ids.add(did)
            entries.append({"id": did, "name": s.get("Name", did), "generic": s.get("GenericName", ""),
                            "exec": s["Exec"], "path": path, "keywords": s.get("Keywords", ""),
                            "comment": s.get("Comment", "")})
    return entries


def _find_desktop_entry(q: str) -> dict | None:
    ql = q.lower().removesuffix(".desktop")
    ents = _desktop_entries()
    for key in ("id", "name"):
        for e in ents:
            v = e[key].lower().removesuffix(".desktop")
            if v == ql or v.split(".")[-1] == ql:
                return e
    for e in ents:
        if ql in e["name"].lower() or ql in e["id"].lower() or ql in e["generic"].lower() or ql in e["keywords"].lower():
            return e
    return None


def _desktop_exec(e: dict) -> str:
    parts = [p for p in shlex.split(e["exec"]) if not re.fullmatch(r"%[fFuUdDnNickvm]", p)]
    return " ".join(shlex.quote(p) for p in parts)


@tool()
def apps(query: Annotated[str | None, Field(description="filter by name/keyword")] = None):
    """List installed desktop applications (for launch(app=...))."""
    ents = _desktop_entries()
    if query:
        q = query.lower()
        ents = [e for e in ents if q in (e["name"] + e["id"] + e["generic"] + e["keywords"] + e["comment"]).lower()]
    ents.sort(key=lambda e: e["name"].lower())
    return "\n".join(f"{e['name']}  [{e['id']}]  {e['generic'] or e['comment'][:50]}" for e in ents[:300]) or "no apps"


# ====================================================================== accessibility


@tool()
def a11y_tree(
    window: Annotated[str | None, Field(description="window (class/title/address); default focused window")] = None,
    app: Annotated[str | None, Field(description="or an accessibility app name/pid (see a11y_apps)")] = None,
    query: Annotated[str | None, Field(description="only elements whose name/value/description contains this")] = None,
    role: Annotated[str | None, Field(description="only this role, e.g. 'push button', 'entry', 'menu item', 'link'")] = None,
    interactive_only: Annotated[bool, Field(description="hide static containers/labels")] = True,
    max_nodes: int = 300,
):
    """Widget tree of a window via AT-SPI: every button, field, menu item, tab, link... with refs (e12).
    Act on them with a11y_act (exact, no pixels) or click(ref='e12')."""
    note = ax.ensure_enabled()
    res = ax.tree(window=window, app=app, query=query, role=role, interactive_only=interactive_only,
                  max_nodes=max_nodes)
    return (note + "\n" if note else "") + res


@tool()
def a11y_act(
    ref: Annotated[str, Field(description="element ref from a11y_tree, e.g. e12")],
    action: Annotated[str, Field(description="click | focus | set_text | insert_text | get_text | set_value | "
                                             "select | expand | collapse | scroll_into_view | menu | actions | "
                                             "<any action name the element lists>")] = "click",
    value: Annotated[str | None, Field(description="text for set_text/insert_text, number for set_value")] = None,
    screenshot_after: ShotArg = False, wait_ms: WaitArg = 250,
):
    """Operate a widget directly through accessibility: press buttons, toggle checkboxes, fill fields,
    pick list items, expand trees, read text. Works even if the widget is covered."""
    try:
        msg = ax.act(ref, action, value)
    except A11yError as ex:
        # Some toolkits (GTK4 check boxes, many custom widgets) export no actions: fall back to the mouse.
        if action.lower() not in ("click", "press", "activate", "default", "toggle", "open", "invoke") \
                or "no actions" not in str(ex):
            raise
        box = ax.screen_box(ref)
        if box is None:
            raise A11yError(f"{ref} has no actions and no on-screen position; click it from a screenshot") from None
        bx, by, bw, bh = box
        vin.move_to(bx + bw / 2, by + bh / 2)
        vin.click("left")
        msg = f"{ref} has no a11y actions; clicked its center at screen ({round(bx + bw / 2)},{round(by + bh / 2)}){_note()}"
    return [msg] + _after(screenshot_after, wait_ms)


@tool()
def a11y_apps():
    """Applications currently exposing an accessibility tree."""
    note = ax.ensure_enabled()
    rows = [f"{a['name']} pid={a['pid']}: {', '.join(a['toplevels']) or '(no windows)'}" for a in ax.apps()]
    return (note + "\n" if note else "") + "\n".join(rows)


# ====================================================================== clipboard


def _clip_get_bytes(primary: bool = False) -> tuple[str, bytes] | None:
    args = ["wl-paste", "--no-newline"] + (["--primary"] if primary else [])
    types = subprocess.run(args[:1] + ["--list-types"] + (["--primary"] if primary else []),
                           capture_output=True, text=True, timeout=5)
    if types.returncode:
        return None
    tlist = types.stdout.split()
    mime = next((t for t in tlist if t.startswith("text/plain")), None) or (tlist[0] if tlist else None)
    if not mime:
        return None
    r = subprocess.run(args + ["--type", mime], capture_output=True, timeout=5)
    return (mime, r.stdout) if r.returncode == 0 else None


def _clip_set(text: str, primary: bool = False):
    subprocess.run(["wl-copy"] + (["--primary"] if primary else []), input=text.encode(), timeout=5, check=True)


def _clip_restore(old: tuple[str, bytes]):
    mime, data = old
    subprocess.run(["wl-copy", "--type", mime], input=data, timeout=5)


@tool()
def clipboard(
    action: Annotated[Literal["get", "set", "types"], Field(description="read, write, or list offered types")] = "get",
    text: Annotated[str | None, Field(description="text to put on the clipboard (set)")] = None,
    primary: Annotated[bool, Field(description="use the primary selection (middle-click paste)")] = False,
):
    """Read or write the Wayland clipboard. Images on the clipboard come back as images."""
    if action == "set":
        _clip_set(text or "", primary)
        return f"clipboard set ({len(text or '')} chars)"
    r = subprocess.run(["wl-paste", "--list-types"] + (["--primary"] if primary else []),
                       capture_output=True, text=True, timeout=5)
    tlist = r.stdout.split()
    if action == "types":
        return "offered types: " + (", ".join(tlist) or "(empty)")
    img_t = next((t for t in tlist if t in ("image/png", "image/jpeg", "image/webp")), None)
    txt_t = next((t for t in tlist if t.startswith("text/plain") or t in ("UTF8_STRING", "STRING", "TEXT")), None)
    if txt_t:
        out = subprocess.run(["wl-paste", "--no-newline", "--type", txt_t] + (["--primary"] if primary else []),
                             capture_output=True, timeout=5).stdout.decode(errors="replace")
        return out if len(out) < 50000 else out[:50000] + f"\n... ({len(out)} chars total)"
    if img_t:
        data = subprocess.run(["wl-paste", "--type", img_t], capture_output=True, timeout=5).stdout
        img = scr.Image.open(__import__("io").BytesIO(data)).convert("RGB")
        tw, th = scr.fit_size(img.width, img.height)
        data2, kind = scr.encode(img.resize((tw, th)) if (tw, th) != img.size else img)
        return [f"clipboard image {img.width}x{img.height}", Image(data=data2, format=kind)]
    return "clipboard is empty" if not tlist else f"clipboard holds {', '.join(tlist)} (no text/image)"


# ====================================================================== waiting


@tool()
def wait(
    seconds: Annotated[float, Field(description="plain wait, or the timeout for a condition")] = 1.0,
    until_text: Annotated[str | None, Field(description="wait until this text is visible (OCR)")] = None,
    until_text_gone: Annotated[str | None, Field(description="wait until this text is no longer visible")] = None,
    until_window: Annotated[str | None, Field(description="wait until a window matching this exists")] = None,
    until_change: Annotated[bool, Field(description="wait until the screen changes noticeably")] = False,
    monitor: Annotated[str | None, Field(description="monitor to watch (default focused)")] = None,
    window: Annotated[str | None, Field(description="limit text watching to this window")] = None,
    screenshot_after: ShotArg = False,
):
    """Wait for time to pass or for something to happen on screen."""
    t0 = time.time()
    if not (until_text or until_text_gone or until_window or until_change):
        time.sleep(seconds)
        return [f"waited {seconds}s"] + _after(screenshot_after, 0, monitor)
    base = scr.grab(output=_pick_monitor(monitor, _monitors())["name"]) if until_change else None
    while True:
        try:
            if until_window:
                try:
                    w = hypr.find_window(until_window)
                    return [f"window appeared after {time.time() - t0:.1f}s: {w['class']} {w['title'][:60]!r}"] + \
                        _after(screenshot_after, 0, monitor)
                except HyprError:
                    pass
            if until_change:
                cur = scr.grab(output=_pick_monitor(monitor, _monitors())["name"])
                frac = scr.changed_fraction(base, cur)
                if frac > 0.005:
                    return [f"screen changed ({frac:.1%}) after {time.time() - t0:.1f}s"] + \
                        _after(screenshot_after, 0, monitor)
            if until_text or until_text_gone:
                q = until_text or until_text_gone
                try:
                    _find_text(q, window=window, monitor=monitor)
                    found = True
                except ScreenError:
                    found = False
                if until_text and found:
                    return [f"{until_text!r} visible after {time.time() - t0:.1f}s"] + _after(screenshot_after, 0, monitor)
                if until_text_gone and not found:
                    return [f"{until_text_gone!r} gone after {time.time() - t0:.1f}s"] + _after(screenshot_after, 0, monitor)
        except (HyprError, ScreenError):
            pass
        if time.time() - t0 > seconds:
            return [f"timed out after {seconds}s"] + _after(screenshot_after, 0, monitor)
        time.sleep(0.3 if not (until_text or until_text_gone) else 0.1)


# ====================================================================== escape hatch


@tool()
def hyprland(
    lua: Annotated[str | None, Field(description="Lua run inside Hyprland; `return` a value to get it back as JSON. "
                                                 "Example: return hl.get_active_window().title")] = None,
    dispatch: Annotated[str | None, Field(description="a dispatcher expression, e.g. "
                                                      "hl.dsp.focus({workspace = \"3\"})")] = None,
    query: Annotated[str | None, Field(description="hyprctl JSON query: clients, monitors, workspaces, layers, "
                                                   "devices, binds, activewindow, cursorpos, ...")] = None,
):
    """Direct access to Hyprland 0.56's Lua API and IPC for anything the other tools don't cover
    (binds list, config options, plugins like the Phantomat canvas: hl.plugin.spatialoverview...)."""
    if lua:
        return json.dumps(hypr.eval_lua(lua), indent=1, ensure_ascii=False)
    if dispatch:
        return hypr.dispatch(dispatch)
    if query:
        res = hypr.query(query)
        s = json.dumps(res, indent=1, ensure_ascii=False)
        return s if len(s) < 60000 else s[:60000] + "\n... (truncated)"
    raise ValueError("give lua, dispatch or query")


def _session_env():
    """MCP clients may start us with a scrubbed environment; recover the Wayland/D-Bus session."""
    for d in (os.path.expanduser("~/.nix-profile/bin"), "/usr/local/bin", "/usr/bin"):
        if d not in os.environ.get("PATH", "").split(":"):
            os.environ["PATH"] = os.environ.get("PATH", "") + ":" + d
    run = os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if "WAYLAND_DISPLAY" not in os.environ or "HYPRLAND_INSTANCE_SIGNATURE" not in os.environ:
        try:
            inst = max(json.loads(subprocess.run(["hyprctl", "instances", "-j"], capture_output=True,
                                                 text=True, timeout=5).stdout), key=lambda i: i["time"])
            os.environ.setdefault("HYPRLAND_INSTANCE_SIGNATURE", inst["instance"])
            os.environ.setdefault("WAYLAND_DISPLAY", inst["wl_socket"])
        except Exception:
            socks = sorted((p for p in glob(f"{run}/wayland-*") if not p.endswith(".lock")), key=os.path.getmtime)
            if socks:
                os.environ.setdefault("WAYLAND_DISPLAY", os.path.basename(socks[-1]))
    if "DBUS_SESSION_BUS_ADDRESS" not in os.environ and os.path.exists(f"{run}/bus"):
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={run}/bus"


def main():
    _session_env()
    if not shutil.which("grim"):
        raise SystemExit("hypr-computer needs grim on PATH")
    mcp.run()


if __name__ == "__main__":
    main()
