"""Which desktop the tools act on: the user's own session ("host") or an agent desktop (a nested
Hyprland with its own seat, cursor, focus, clipboard and D-Bus)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import hypr


@dataclass
class Target:
    kind: str  # "host" | "agent"
    name: str
    wayland_display: str
    hypr_sig: str
    dbus: str | None = None
    extra_env: dict = field(default_factory=dict)

    def env(self) -> dict:
        e = dict(os.environ)
        e["WAYLAND_DISPLAY"] = self.wayland_display
        e["HYPRLAND_INSTANCE_SIGNATURE"] = self.hypr_sig
        if self.dbus:
            e["DBUS_SESSION_BUS_ADDRESS"] = self.dbus
        e.update(self.extra_env)
        return e


_current: Target | None = None
_host: Target | None = None


def host() -> Target:
    global _host
    if _host is None:
        _host = Target("host", "host", os.environ.get("WAYLAND_DISPLAY", "wayland-1"),
                       os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", ""),
                       os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
    return _host


def current() -> Target:
    return _current or host()


def activate(t: Target):
    global _current
    _current = t
    hypr.set_instance(t.hypr_sig)


def env() -> dict:
    return current().env()


def is_agent() -> bool:
    return current().kind == "agent"
