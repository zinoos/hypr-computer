"""Watch-only viewer for an agent desktop: a window on the user's desktop that shows the agent's screen
(including its cursor) live. It is only a picture: it has no input path into the agent desktop, so the
user's mouse and keyboard can never move the agent's cursor or type into its windows, and vice versa.

    python -m hypr_computer.viewer NAME     (run with the user's WAYLAND_DISPLAY)

Closing the window stops that agent desktop.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gio, Gtk  # noqa: E402

from . import agentdesk  # noqa: E402
from .wlvirt import Connection, ScreenCopy, WaylandError  # noqa: E402

MAX_FPS = 30
FORMATS = {
    0: getattr(Gdk.MemoryFormat, "B8G8R8A8", None),  # ARGB8888 in little-endian memory
    1: getattr(Gdk.MemoryFormat, "B8G8R8X8", None) or getattr(Gdk.MemoryFormat, "B8G8R8A8_PREMULTIPLIED"),  # XRGB8888
}

CSS = b"""
window.agent-viewer { background: #101114; }
.agent-badge { background: rgba(0,0,0,0.55); color: white; border-radius: 8px; padding: 3px 9px;
               margin: 10px; font-weight: 600; font-size: 12px; }
"""


class Viewer:
    def __init__(self, desk: agentdesk.Desk):
        self.desk = desk
        self.stop_on_close = True
        self.closing = False
        self.app = Gtk.Application(application_id="dev.hypr_computer.viewer",
                                   flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.app.connect("activate", self.on_activate)

    def on_activate(self, app):
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        d = self.desk
        place = f" · Super+{agentdesk.place_label(d.place)}" if d.place else ""
        self.win = Gtk.ApplicationWindow(application=app, title=f"Agent desktop {d.name} (watch only)")
        self.win.add_css_class("agent-viewer")
        self.win.set_default_size(d.width // 2, d.height // 2)
        overlay = Gtk.Overlay()
        self.pic = Gtk.Picture(can_shrink=True)
        try:
            self.pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        except AttributeError:
            self.pic.set_keep_aspect_ratio(True)
        overlay.set_child(self.pic)
        badge = Gtk.Label(label=f"● {d.name}{place} · watch only")
        badge.add_css_class("agent-badge")
        badge.set_halign(Gtk.Align.START)
        badge.set_valign(Gtk.Align.START)
        overlay.add_overlay(badge)
        self.win.set_child(overlay)
        self.win.connect("close-request", self.on_close)
        self.win.present()
        threading.Thread(target=self.capture_loop, daemon=True).start()

    def on_close(self, *_):
        self.closing = True
        if self.stop_on_close:
            threading.Thread(target=lambda: agentdesk.stop(self.desk), daemon=True).start()
        return False

    def capture_loop(self):
        try:
            conn = Connection(self.desk.wl_socket)
            sc = ScreenCopy(conn)
        except Exception as ex:
            print(f"viewer: cannot capture agent screen: {ex}", file=sys.stderr)
            GLib.idle_add(self.app.quit)
            return
        first = True
        while not self.closing:
            t0 = time.time()
            try:
                frame = sc.capture(with_damage=not first, timeout=5)
            except WaylandError as ex:
                if "timed out" in str(ex):
                    if not self.desk.alive():
                        break
                    continue  # nothing changed on the agent screen
                break  # agent desktop gone
            except OSError:
                break
            if frame is None:
                time.sleep(0.2)
                continue
            first = False
            GLib.idle_add(self.show, frame)
            dt = time.time() - t0
            if dt < 1 / MAX_FPS:
                time.sleep(1 / MAX_FPS - dt)
        self.stop_on_close = False
        GLib.idle_add(self.app.quit)

    def show(self, frame):
        data, w, h, stride, fmt, inverted = frame
        mf = FORMATS.get(fmt) or FORMATS[1]
        tex = Gdk.MemoryTexture.new(w, h, mf, GLib.Bytes.new(data), stride)
        self.pic.set_paintable(tex)
        if inverted:
            self.pic.set_css_classes(["flipped"])
        return False

    def run(self):
        return self.app.run([sys.argv[0]])


def main():
    name = sys.argv[1]
    d = next((x for x in agentdesk.list_desks() if x.name == name), None)
    if d is None:
        print(f"no running agent desktop {name!r}", file=sys.stderr)
        return 1
    return Viewer(d).run()


if __name__ == "__main__":
    sys.exit(main())
