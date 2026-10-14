"""AT-SPI accessibility: read the widget tree of apps and act on widgets by reference (no pixels needed).

Covers GTK3/GTK4, Qt (with accessibility on), LibreOffice, Firefox, and Chromium/Electron when started
with accessibility enabled. Terminals like kitty and most games expose nothing.
"""

from __future__ import annotations

import itertools
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

from . import hypr  # noqa: E402

INTERACTIVE_ROLES = {
    "push button", "toggle button", "check box", "radio button", "menu item", "check menu item",
    "radio menu item", "menu", "link", "entry", "password text", "combo box", "list item", "tree item",
    "page tab", "slider", "spin button", "button", "switch", "text", "editbar", "table cell",
    "icon", "tool bar", "menu bar", "search box", "popup menu", "toggle", "push button menu",
}
CONTAINER_ROLES = {"frame", "window", "dialog", "alert", "file chooser", "panel", "filler",
                   "scroll pane", "viewport", "section", "document frame", "document web", "split pane",
                   "page tab list", "layered pane", "tool bar", "menu bar", "list", "tree", "table",
                   "grouping", "form", "landmark", "application", "internal frame", "option pane"}
TOPLEVEL_ROLES = {"frame", "window", "dialog", "alert", "file chooser"}

S = Atspi.StateType


class A11yError(RuntimeError):
    pass


@dataclass
class Ref:
    acc: Atspi.Accessible
    role: str
    name: str
    pid: int
    toplevel_title: str


class A11y:
    def __init__(self):
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="atspi")
        self._refs: dict[str, Ref] = {}
        self._counter = itertools.count(1)
        self._lock = threading.Lock()
        self.enabled_note: str | None = None

    def _run(self, fn, *a, timeout: float = 30.0, **kw):
        return self._pool.submit(fn, *a, **kw).result(timeout=timeout)

    # ------------------------------------------------------------ setup

    def ensure_enabled(self) -> str | None:
        """Turn on the accessibility bus flag so toolkits export their trees (affects newly started apps)."""
        try:
            out = subprocess.run(
                ["gdbus", "call", "--session", "--dest", "org.a11y.Bus", "--object-path", "/org/a11y/bus",
                 "--method", "org.freedesktop.DBus.Properties.Get", "org.a11y.Status", "IsEnabled"],
                capture_output=True, text=True, timeout=5).stdout
            if "true" not in out:
                subprocess.run(
                    ["gdbus", "call", "--session", "--dest", "org.a11y.Bus", "--object-path", "/org/a11y/bus",
                     "--method", "org.freedesktop.DBus.Properties.Set", "org.a11y.Status", "IsEnabled", "<true>"],
                    capture_output=True, text=True, timeout=5)
                self.enabled_note = ("Accessibility was off; it is now on. Apps that were already running "
                                     "may need a restart before they show up (Chromium/Electron: start with "
                                     "--force-renderer-accessibility).")
                return self.enabled_note
        except Exception as ex:  # gdbus missing etc.
            return f"could not enable accessibility bus: {ex}"
        return None

    # ------------------------------------------------------------ queries

    def apps(self) -> list[dict]:
        return self._run(self._apps)

    def _apps(self) -> list[dict]:
        d = Atspi.get_desktop(0)
        out = []
        for i in range(d.get_child_count()):
            a = d.get_child_at_index(i)
            if a is None:
                continue
            try:
                wins = []
                for j in range(min(a.get_child_count(), 30)):
                    w = a.get_child_at_index(j)
                    if w is not None:
                        wins.append(f"{w.get_role_name()} {w.get_name()!r}")
                out.append({"name": a.get_name(), "pid": a.get_process_id(), "toplevels": wins})
            except Exception:
                continue
        return out

    def tree(self, window: str | None = None, app: str | None = None, query: str | None = None,
             role: str | None = None, interactive_only: bool = True, max_nodes: int = 300,
             max_depth: int = 40, include_hidden: bool = False) -> str:
        return self._run(self._tree, window, app, query, role, interactive_only, max_nodes, max_depth,
                         include_hidden, timeout=60)

    def _roots(self, window: str | None, app: str | None) -> list[tuple[Atspi.Accessible, int, str]]:
        """(toplevel accessible, pid, title) for the requested window/app; default: the focused window."""
        d = Atspi.get_desktop(0)
        target_pid, target_title = None, None
        if app is None:
            try:
                win = hypr.find_window(window or "active")
                target_pid, target_title = win["pid"], win["title"]
            except hypr.HyprError as ex:
                raise A11yError(str(ex)) from None
        roots = []
        for i in range(d.get_child_count()):
            a = d.get_child_at_index(i)
            if a is None:
                continue
            try:
                pid = a.get_process_id()
                aname = a.get_name() or ""
            except Exception:
                continue
            if app is not None:
                if app.lower() not in aname.lower() and app != str(pid):
                    continue
            elif pid != target_pid:
                continue
            for j in range(a.get_child_count()):
                top = a.get_child_at_index(j)
                if top is None:
                    continue
                try:
                    title = top.get_name() or ""
                    showing = top.get_state_set().contains(S.SHOWING) or top.get_state_set().contains(S.ACTIVE)
                except Exception:
                    continue
                roots.append((top, pid, title, showing))
        if not roots:
            hint = f"window {window or 'active'!r} (pid {target_pid})" if app is None else f"app {app!r}"
            raise A11yError(
                f"no accessibility tree for {hint}. The app may not support AT-SPI (terminals, games, many "
                "Electron/Chromium apps unless started with --force-renderer-accessibility), or it was started "
                "before accessibility was enabled; restart it. a11y_apps lists what is exposed.")
        if app is None and target_title:
            exact = [r for r in roots if r[2] == target_title]
            if exact:
                return [(r[0], r[1], r[2]) for r in exact]
            showing = [r for r in roots if r[3]]
            if showing:
                return [(r[0], r[1], r[2]) for r in showing]
        return [(r[0], r[1], r[2]) for r in roots]

    def _tree(self, window, app, query, role, interactive_only, max_nodes, max_depth, include_hidden) -> str:
        roots = self._roots(window, app)
        q = (query or "").casefold()
        rq = (role or "").casefold()
        lines: list[str] = []
        count = 0
        truncated = False
        flat = bool(q or rq)

        def walk(acc, depth, pid, title, path):
            nonlocal count, truncated
            if count >= max_nodes:
                truncated = True
                return
            try:
                states = acc.get_state_set()
                if not include_hidden and depth > 0 and not states.contains(S.SHOWING):
                    return
                r = acc.get_role_name()
                name = acc.get_name() or ""
                nchild = acc.get_child_count()
            except Exception:
                return
            info = _describe(acc, r, name, states)
            interesting = _is_interactive(acc, r, states) or (not interactive_only and (name or r not in CONTAINER_ROLES))
            if depth == 0:
                interesting = True
            if flat:
                hay = f"{name} {info['desc']} {info['value']}".casefold()
                match = (not q or q in hay) and (not rq or rq in r.casefold())
                if match:
                    ref = self._add(acc, r, name, pid, title)
                    crumbs = " > ".join(path[-3:])
                    lines.append(f"{ref} {_fmt(r, name, info)}" + (f"   (in {crumbs})" if crumbs else ""))
                    count += 1
            elif interesting:
                ref = self._add(acc, r, name, pid, title)
                lines.append("  " * min(depth, 30) + f"{ref} {_fmt(r, name, info)}")
                count += 1
            if depth >= max_depth:
                return
            label = f"{r} {name!r}" if name else r
            for i in range(min(nchild, 2000)):
                if count >= max_nodes:
                    truncated = True
                    return
                try:
                    ch = acc.get_child_at_index(i)
                except Exception:
                    continue
                if ch is not None:
                    walk(ch, depth + (1 if (interesting and not flat) else 0), pid, title, path + [label])

        for top, pid, title in roots:
            walk(top, 0, pid, title, [])
        if not lines:
            return "no matching elements" if flat else "the tree is empty"
        if truncated:
            lines.append(f"... truncated at {max_nodes} nodes: narrow with query=/role= or raise max_nodes")
        return "\n".join(lines)

    def _add(self, acc, role, name, pid, title) -> str:
        with self._lock:
            ref = f"e{next(self._counter)}"
            self._refs[ref] = Ref(acc, role, name, pid, title)
            if len(self._refs) > 5000:
                for k in list(self._refs)[:1000]:
                    del self._refs[k]
            return ref

    def get(self, ref: str) -> Ref:
        r = self._refs.get(ref)
        if r is None:
            raise A11yError(f"unknown element {ref!r}; refs come from a11y_tree and expire after many calls")
        return r

    # ------------------------------------------------------------ geometry

    def screen_box(self, ref: str) -> tuple[float, float, float, float] | None:
        """Layout-space box of an element, if the toolkit reports usable extents."""
        return self._run(self._screen_box, ref)

    def _screen_box(self, ref):
        r = self.get(ref)
        comp = r.acc.get_component_iface()
        if comp is None:
            return None
        try:
            ext = comp.get_extents(Atspi.CoordType.WINDOW)
        except Exception:
            return None
        if ext.width <= 0 or ext.height <= 0:
            return None
        # Find the Hyprland window of this element and offset by its on-screen origin.
        clients = [c for c in hypr.query("clients") if c["pid"] == r.pid]
        if not clients:
            return None
        win = next((c for c in clients if c["title"] == r.toplevel_title), None) or \
            min(clients, key=lambda c: c["focusHistoryID"])
        canvas = hypr.canvas_state()
        wx, wy, ww, wh = hypr.window_screen_box(win, canvas)
        z = canvas.zoom if canvas.active else 1.0
        if ext.x > ww / z + 5 or ext.y > wh / z + 5:
            return None  # toolkit gave absolute/garbage coords
        return wx + ext.x * z, wy + ext.y * z, ext.width * z, ext.height * z

    # ------------------------------------------------------------ actions

    def act(self, ref: str, action: str, value: str | None = None) -> str:
        return self._run(self._act, ref, action, value)

    def _act(self, ref, action, value) -> str:
        r = self.get(ref)
        acc = r.acc
        act = action.lower().replace("-", "_").replace(" ", "_")
        names = _action_names(acc)
        if act in ("actions", "list"):
            return f"{ref} {r.role} {r.name!r}: actions = {names or 'none'}"
        if act in ("click", "press", "activate", "default", "toggle", "open", "invoke"):
            for want in ("click", "press", "activate", "toggle", "jump", "open", "select", "default"):
                for i, n in enumerate(names):
                    if n.lower() == want:
                        acc.get_action_iface().do_action(i)
                        return f"did {n!r} on {ref} {r.role} {r.name!r}"
            if names:
                acc.get_action_iface().do_action(0)
                return f"did {names[0]!r} on {ref} {r.role} {r.name!r}"
            raise A11yError(f"{ref} has no actions; click it with the mouse instead (click ref={ref})")
        if act == "focus":
            comp = acc.get_component_iface()
            if comp and comp.grab_focus():
                return f"focused {ref}"
            raise A11yError(f"{ref} cannot take focus")
        if act in ("set_text", "set"):
            et = acc.get_editable_text_iface()
            if et is None:
                raise A11yError(f"{ref} is not editable text; focus it and use type_text")
            if et.set_text_contents(value or ""):
                return f"set text of {ref} to {value!r}"
            raise A11yError(f"{ref} refused set_text; focus it and use type_text")
        if act in ("insert_text", "append"):
            et = acc.get_editable_text_iface()
            ti = acc.get_text_iface()
            if et is None or ti is None:
                raise A11yError(f"{ref} is not editable text")
            pos = ti.get_character_count()
            et.insert_text(pos, value or "", len((value or "").encode()))
            return f"inserted text into {ref}"
        if act in ("get_text", "text", "read"):
            ti = acc.get_text_iface()
            if ti is None:
                return f"{ref} {r.role} {r.name!r} (no text interface)"
            return ti.get_text(0, min(ti.get_character_count(), 20000))
        if act in ("set_value", "value"):
            vi = acc.get_value_iface()
            if vi is None:
                raise A11yError(f"{ref} has no value interface")
            vi.set_current_value(float(value))
            return f"value of {ref} = {vi.get_current_value()} (range {vi.get_minimum_value()}..{vi.get_maximum_value()})"
        if act == "select":
            parent = acc.get_parent()
            sel = parent.get_selection_iface() if parent else None
            if sel is not None:
                sel.select_child(acc.get_index_in_parent())
                return f"selected {ref}"
            for i, n in enumerate(names):
                if n.lower() in ("select", "click", "activate"):
                    acc.get_action_iface().do_action(i)
                    return f"did {n!r} on {ref}"
            raise A11yError(f"{ref} is not selectable")
        if act in ("expand", "collapse"):
            for i, n in enumerate(names):
                if "expand" in n.lower() or "contract" in n.lower() or n.lower() == act:
                    acc.get_action_iface().do_action(i)
                    return f"did {n!r} on {ref}"
            raise A11yError(f"{ref} cannot {act}")
        if act in ("scroll_into_view", "scroll_to", "show"):
            comp = acc.get_component_iface()
            if comp and comp.scroll_to(Atspi.ScrollType.ANYWHERE):
                return f"scrolled {ref} into view"
            raise A11yError(f"{ref} cannot be scrolled to")
        if act in ("menu", "context_menu", "showmenu"):
            for i, n in enumerate(names):
                if "menu" in n.lower():
                    acc.get_action_iface().do_action(i)
                    return f"did {n!r} on {ref}"
            raise A11yError(f"{ref} has no menu action; right-click it instead")
        for i, n in enumerate(names):
            if n.lower().replace(" ", "_") == act:
                acc.get_action_iface().do_action(i)
                return f"did {n!r} on {ref}"
        raise A11yError(f"unknown action {action!r} for {ref}; its actions: {names or 'none'}")

    def info(self, ref: str) -> str:
        return self._run(self._info, ref)

    def _info(self, ref):
        r = self.get(ref)
        acc = r.acc
        states = acc.get_state_set()
        d = _describe(acc, r.role, r.name, states)
        return f"{ref} {_fmt(r.role, r.name, d)} actions={_action_names(acc)}"


def _action_names(acc) -> list[str]:
    ai = acc.get_action_iface()
    if ai is None:
        return []
    try:
        return [ai.get_action_name(i) for i in range(ai.get_n_actions())]
    except Exception:
        return []


def _is_interactive(acc, role, states) -> bool:
    if role in ("label", "static", "caption", "heading", "paragraph", "image", "separator"):
        return role == "label" and any(n.lower() in ("click", "activate") for n in _action_names(acc))
    if role in INTERACTIVE_ROLES and role not in ("text", "table cell", "icon", "tool bar", "menu bar"):
        return True
    if states.contains(S.FOCUSABLE) or states.contains(S.EDITABLE) or states.contains(S.SELECTABLE):
        return True
    if role in ("text", "table cell", "icon") and acc.get_action_iface() is not None:
        return True
    return bool(_action_names(acc)) and role not in CONTAINER_ROLES


def _describe(acc, role, name, states) -> dict:
    flags = []
    for st, label in ((S.FOCUSED, "focused"), (S.CHECKED, "checked"), (S.PRESSED, "pressed"),
                      (S.SELECTED, "selected"), (S.EXPANDED, "expanded"), (S.ACTIVE, "active"),
                      (S.MODAL, "modal"), (S.INDETERMINATE, "mixed")):
        if states.contains(st):
            flags.append(label)
    if states.contains(S.EXPANDABLE) and not states.contains(S.EXPANDED):
        flags.append("collapsed")
    if not states.contains(S.SENSITIVE) and not states.contains(S.ENABLED):
        flags.append("disabled")
    if states.contains(S.EDITABLE):
        flags.append("editable")
    value = ""
    try:
        if states.contains(S.EDITABLE) or role in ("entry", "password text", "text", "spin button", "combo box"):
            ti = acc.get_text_iface()
            if ti is not None and role != "password text":
                value = ti.get_text(0, min(ti.get_character_count(), 120))
        if not value:
            vi = acc.get_value_iface()
            if vi is not None:
                value = f"{vi.get_current_value():g}"
    except Exception:
        pass
    desc = ""
    try:
        desc = acc.get_description() or ""
    except Exception:
        pass
    return {"flags": flags, "value": value, "desc": desc}


def _fmt(role, name, info) -> str:
    s = f"[{role}]"
    if name:
        s += f" {name!r}"
    if info["value"] and info["value"] != name:
        v = info["value"].replace("\n", "⏎")
        s += f" value={v[:120]!r}"
    if info["desc"] and info["desc"] != name:
        s += f" desc={info['desc'][:80]!r}"
    if info["flags"]:
        s += " (" + ", ".join(info["flags"]) + ")"
    return s
