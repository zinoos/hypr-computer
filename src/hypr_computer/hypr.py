"""Hyprland IPC: queries, Lua dispatchers, Lua eval with results, Phantomat canvas projection."""

from __future__ import annotations

import glob
import json
import os
import socket
import tempfile
import threading
from dataclasses import dataclass


class HyprError(RuntimeError):
    pass


_instance: str | None = None


def set_instance(sig: str | None):
    """Talk to this Hyprland instance (host session or an agent desktop) from now on."""
    global _instance
    _instance = sig or None


def _socket_path(inst: str | None = None) -> str:
    run = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    sig = inst or _instance or os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if sig and os.path.exists(f"{run}/hypr/{sig}/.socket.sock"):
        return f"{run}/hypr/{sig}/.socket.sock"
    # Started outside the Hyprland session (no env): take the newest live instance.
    socks = sorted(glob.glob(f"{run}/hypr/*/.socket.sock"), key=os.path.getmtime, reverse=True)
    if not socks:
        raise HyprError("no Hyprland instance socket found")
    return socks[0]


_lock = threading.Lock()


def request(cmd: str, timeout: float = 10.0, inst: str | None = None) -> str:
    with _lock:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(_socket_path(inst))
            s.sendall(cmd.encode())
            chunks = []
            while True:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
        finally:
            s.close()
    return b"".join(chunks).decode(errors="replace")


def query(what: str, inst: str | None = None):
    """JSON query: clients, monitors, workspaces, activewindow, activeworkspace, layers, devices, binds, ..."""
    out = request("j/" + what, inst=inst)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise HyprError(f"hyprctl {what}: {out.strip()}") from None


def dispatch(lua: str, inst: str | None = None) -> str:
    """Run a Lua dispatcher expression, e.g. hl.dsp.window.close()."""
    out = request("dispatch " + lua, inst=inst).strip()
    if out != "ok":
        raise HyprError(out)
    return out


_LUA_JSON = r"""
local function __enc(v, d)
  d = d or 0
  local t = type(v)
  if v == nil then return "null"
  elseif t == "boolean" then return tostring(v)
  elseif t == "number" then
    if v ~= v or v == math.huge or v == -math.huge then return "null" end
    if math.type and math.type(v) == "integer" then return string.format("%d", v) end
    return string.format("%.10g", v)
  elseif t == "string" then
    return '"' .. v:gsub('[%c"\\]', function(c) return string.format("\\u%04x", c:byte()) end) .. '"'
  elseif t == "table" then
    if d > 12 then return '"<deep table>"' end
    local n = #v
    local is_arr = n > 0 or next(v) == nil
    if is_arr then for k in pairs(v) do if type(k) ~= "number" then is_arr = false break end end end
    local parts = {}
    if is_arr then
      for i = 1, n do parts[#parts + 1] = __enc(v[i], d + 1) end
      return "[" .. table.concat(parts, ",") .. "]"
    end
    for k, val in pairs(v) do parts[#parts + 1] = __enc(tostring(k)) .. ":" .. __enc(val, d + 1) end
    return "{" .. table.concat(parts, ",") .. "}"
  else
    return __enc(tostring(v))
  end
end
"""


def eval_lua(code: str, inst: str | None = None):
    """Run Lua inside Hyprland and return its result (JSON-converted). `code` is a function body: use `return`."""
    fd, path = tempfile.mkstemp(prefix="hypr-computer-", suffix=".json", dir=os.environ.get("XDG_RUNTIME_DIR"))
    os.close(fd)
    try:
        wrapped = (
            _LUA_JSON
            + f"local __ok, __res = pcall(function()\n{code}\nend)\n"
            + f"local __f = io.open({json.dumps(path)}, 'w')\n"
            + "if __ok then __f:write('{\"ok\":true,\"result\":' .. __enc(__res) .. '}')\n"
            + "else __f:write('{\"ok\":false,\"error\":' .. __enc(tostring(__res)) .. '}') end\n"
            + "__f:close()"
        )
        out = request("eval " + wrapped, inst=inst).strip()
        with open(path) as f:
            data = f.read()
        if not data:
            raise HyprError(f"eval failed: {out}")
        res = json.loads(data)
        if not res["ok"]:
            raise HyprError(f"Lua error: {res['error']}")
        return res.get("result")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def cursor_pos(inst: str | None = None) -> tuple[float, float]:
    p = query("cursorpos", inst)
    return float(p["x"]), float(p["y"])


def warp_cursor(x: float, y: float) -> None:
    dispatch(f"hl.dsp.cursor.move({{x={x:.2f}, y={y:.2f}}})")


def lua_str(s: str) -> str:
    return json.dumps(s)  # JSON string literals are valid Lua strings for our purposes


# ---------------------------------------------------------------- canvas


@dataclass
class Canvas:
    """Phantomat infinite-canvas camera. Windows report canvas-space `at`; the screen shows
    layout = anchor + (canvas - view.xy) * zoom."""

    active: bool
    view_x: float = 0.0
    view_y: float = 0.0
    zoom: float = 1.0
    anchor_x: float = 0.0
    anchor_y: float = 0.0
    raw: dict | None = None

    def to_layout(self, x: float, y: float) -> tuple[float, float]:
        if not self.active:
            return x, y
        return self.anchor_x + (x - self.view_x) * self.zoom, self.anchor_y + (y - self.view_y) * self.zoom


def canvas_state(monitors: list[dict] | None = None, inst: str | None = None) -> Canvas:
    try:
        raw = eval_lua(
            """
            local so = hl.plugin and hl.plugin.spatialoverview
            if not so or not so.view then return nil end
            local ok, v = pcall(so.view)
            local ok2, p = pcall(so.places)
            return { view = ok and v or nil, places = ok2 and p or nil }
            """, inst
        )
    except HyprError:
        return Canvas(False)
    if not raw or not raw.get("view") or not raw.get("places"):
        return Canvas(False, raw=raw)
    view, places = raw["view"], raw["places"]
    if monitors is None:
        monitors = query("monitors", inst)
    mons = {m["name"]: m for m in monitors}
    screens = places.get("screens") or []
    anchor = None
    for s in screens:
        m = mons.get(s["name"])
        if m:
            anchor = (m["x"] - s["x"], m["y"] - s["y"])
            break
    if anchor is None:
        return Canvas(False, raw=raw)
    # view() is the canvas rect shown by the screen under the pointer. All screens share one camera,
    # so derive the camera (canvas point at the anchor's layout origin) from that screen.
    try:
        cx, cy = cursor_pos(inst)
    except HyprError:
        cx, cy = anchor
    scr_mon = monitor_for_point(monitors, cx, cy) or next(iter(mons.values()))
    sx, sy, sw, sh = monitor_box(scr_mon)
    zoom = sw / view["w"] if view.get("w") else 1.0
    cam_x = view["x"] - (sx - anchor[0]) / zoom
    cam_y = view["y"] - (sy - anchor[1]) / zoom
    return Canvas(True, cam_x, cam_y, zoom, anchor[0], anchor[1], raw)


def window_screen_box(win: dict, canvas: Canvas) -> tuple[float, float, float, float]:
    x, y = canvas.to_layout(win["at"][0], win["at"][1])
    z = canvas.zoom if canvas.active else 1.0
    return x, y, win["size"][0] * z, win["size"][1] * z


def screens(monitors: list[dict]) -> list[dict]:
    """Enabled monitors that show their own content (mirrors, e.g. an agent desktop's viewer, excluded)."""
    return [m for m in monitors if not m.get("disabled") and str(m.get("mirrorOf", "none")) in ("none", "", "None")]


def monitor_for_point(monitors: list[dict], x: float, y: float) -> dict | None:
    for m in monitors:
        mw, mh = m["width"] / m["scale"], m["height"] / m["scale"]
        if m.get("transform", 0) % 2 == 1:
            mw, mh = mh, mw
        if m["x"] <= x < m["x"] + mw and m["y"] <= y < m["y"] + mh:
            return m
    return None


def monitor_box(m: dict) -> tuple[float, float, float, float]:
    w, h = m["width"] / m["scale"], m["height"] / m["scale"]
    if m.get("transform", 0) % 2 == 1:
        w, h = h, w
    return float(m["x"]), float(m["y"]), w, h


def visible_part(box, monitors) -> tuple[float, float, float, float] | None:
    """Intersection of a layout box with the union bounding of monitors it touches (largest overlap)."""
    bx, by, bw, bh = box
    best, area = None, 0.0
    for m in monitors:
        mx, my, mw, mh = monitor_box(m)
        x0, y0 = max(bx, mx), max(by, my)
        x1, y1 = min(bx + bw, mx + mw), min(by + bh, my + mh)
        if x1 > x0 and y1 > y0 and (x1 - x0) * (y1 - y0) > area:
            best, area = (x0, y0, x1 - x0, y1 - y0), (x1 - x0) * (y1 - y0)
    return best


def find_window(selector: str, clients: list[dict] | None = None) -> dict:
    """Match by address (0x..), 'active', exact class, or case-insensitive substring of class/title."""
    clients = clients if clients is not None else query("clients")
    sel = selector.strip()
    if sel in ("active", "focused", ""):
        aw = query("activewindow")
        if not aw:
            raise HyprError("no active window")
        return aw
    low = sel.lower()
    if low.startswith("address:"):
        low = low[8:]
    for c in clients:
        if c["address"].lower() == low:
            return c
    for key in ("class", "initialClass"):
        hits = [c for c in clients if c[key].lower() == low]
        if hits:
            return min(hits, key=lambda c: c["focusHistoryID"])
    hits = [c for c in clients if low in c["class"].lower() or low in c["title"].lower()]
    if hits:
        return min(hits, key=lambda c: c["focusHistoryID"])
    raise HyprError(f"no window matches {selector!r}; use the windows tool to list them")
