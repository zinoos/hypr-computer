"""GTK4 test window that logs every input event it receives (JSON lines) for checking hypr-computer.

usage: python3 testapp.py LOGFILE
"""

import json
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Graphene, Gtk  # noqa: E402

LOG = open(sys.argv[1] if len(sys.argv) > 1 else "/dev/stdout", "a", buffering=1)


def log(kind, **kw):
    LOG.write(json.dumps({"t": round(time.time(), 3), "ev": kind, **kw}) + "\n")


def in_window(widget, x, y):
    ok, p = widget.compute_point(widget.get_root(), Graphene.Point().init(x, y))
    return (round(p.x, 1), round(p.y, 1)) if ok else (None, None)


def on_activate(app):
    win = Gtk.ApplicationWindow(application=app, title="hypr-computer test")
    win.set_default_size(700, 620)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=10,
                  margin_start=10, margin_end=10)
    win.set_child(box)

    entry = Gtk.Entry(placeholder_text="type here")
    entry.connect("changed", lambda e: log("entry", text=e.get_text()))
    box.append(entry)

    row = Gtk.Box(spacing=10)
    count = {"n": 0}
    label = Gtk.Label(label="presses: 0")
    btn = Gtk.Button(label="Press me")

    def pressed(_b):
        count["n"] += 1
        label.set_label(f"presses: {count['n']}")
        log("button", n=count["n"])
    btn.connect("clicked", pressed)
    chk = Gtk.CheckButton(label="Check me")
    chk.connect("toggled", lambda c: log("check", active=c.get_active()))
    scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
    scale.set_hexpand(True)
    scale.connect("value-changed", lambda s: log("scale", value=s.get_value()))
    for w in (btn, label, chk, scale):
        row.append(w)
    box.append(row)

    pad = Gtk.DrawingArea(content_height=200, hexpand=True)
    pad.set_draw_func(lambda a, cr, w, h: (cr.set_source_rgb(0.15, 0.3, 0.5), cr.paint(),
                                          cr.set_source_rgb(1, 1, 1), cr.move_to(10, 24), cr.set_font_size(18),
                                          cr.show_text("EVENT PAD")))
    click = Gtk.GestureClick(button=0)
    click.connect("pressed", lambda g, n, x, y: log("press", button=g.get_current_button(), n=n,
                                                    x=round(x, 1), y=round(y, 1), win=in_window(pad, x, y),
                                                    mods=int(g.get_current_event_state())))
    click.connect("released", lambda g, n, x, y: log("release", button=g.get_current_button(), n=n,
                                                     x=round(x, 1), y=round(y, 1)))
    pad.add_controller(click)
    drag = Gtk.GestureDrag()
    drag.connect("drag-begin", lambda g, x, y: log("drag-begin", x=round(x, 1), y=round(y, 1)))
    drag.connect("drag-end", lambda g, dx, dy: log("drag-end", dx=round(dx, 1), dy=round(dy, 1)))
    pad.add_controller(drag)
    scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
    scroll.connect("scroll", lambda c, dx, dy: (log("scroll", dx=dx, dy=dy, unit=str(c.get_unit())), True)[1])
    pad.add_controller(scroll)
    motion = Gtk.EventControllerMotion()
    motion.connect("enter", lambda c, x, y: log("enter", x=round(x, 1), y=round(y, 1)))
    pad.add_controller(motion)
    zoom = Gtk.GestureZoom()
    zoom.connect("scale-changed", lambda g, s: log("pinch", scale=round(s, 3)))
    pad.add_controller(zoom)
    box.append(pad)

    sw = Gtk.ScrolledWindow(vexpand=True)
    lst = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    for i in range(200):
        lst.append(Gtk.Label(label=f"row {i}", xalign=0))
    sw.set_child(lst)
    sw.get_vadjustment().connect("value-changed", lambda a: log("list-scroll", value=round(a.get_value(), 1)))
    box.append(sw)

    keys = Gtk.EventControllerKey()
    keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    keys.connect("key-pressed", lambda c, kv, code, st: (log("key", name=Gdk.keyval_name(kv), code=code,
                                                              mods=int(st)), False)[1])
    win.add_controller(keys)
    win.present()
    log("ready")


app = Gtk.Application(application_id="dev.hyprcomputer.test")
app.connect("activate", on_activate)
app.run([sys.argv[0]])
