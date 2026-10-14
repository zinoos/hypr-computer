"""Minimal pure-Python Wayland client for zwlr_virtual_pointer_v1 + zwp_virtual_keyboard_v1.

Used to drive an agent desktop (a nested compositor): input created this way belongs to that
compositor's seat only, so it never touches the user's own pointer, keyboard focus or windows.
"""

from __future__ import annotations

import array
import os
import socket
import struct
import tempfile
import threading
import time


class WaylandError(RuntimeError):
    pass


def _pad(b: bytes) -> bytes:
    return b + b"\0" * (-len(b) % 4)


def _string(s: str) -> bytes:
    raw = s.encode() + b"\0"
    return struct.pack("<I", len(raw)) + _pad(raw)


def _fixed(v: float) -> bytes:
    return struct.pack("<i", int(round(v * 256)))


class Connection:
    def __init__(self, display: str):
        path = display if display.startswith("/") else os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), display)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.sock.settimeout(5)
        self._next = 2  # 1 is wl_display
        self._buf = b""
        self._lock = threading.RLock()
        self.globals: dict[str, tuple[int, int]] = {}  # interface -> (name, version)
        self.outputs: list[int] = []
        self._callbacks_done: set[int] = set()
        self._watched: dict[int, list] = {}  # object id -> queued (opcode, body) events
        self.registry = self._new_id()
        self.send(1, 1, struct.pack("<I", self.registry))  # wl_display.get_registry
        self.roundtrip()

    def _new_id(self) -> int:
        i = self._next
        self._next += 1
        return i

    def send(self, obj: int, opcode: int, payload: bytes = b"", fds: list[int] | None = None):
        size = 8 + len(payload)
        msg = struct.pack("<II", obj, (size << 16) | opcode) + payload
        with self._lock:
            if fds:
                self.sock.sendmsg([msg], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))])
            else:
                self.sock.sendall(msg)

    def bind(self, interface: str, version: int) -> int:
        if interface not in self.globals:
            raise WaylandError(f"compositor does not offer {interface}")
        name, have = self.globals[interface]
        new = self._new_id()
        v = min(version, have)
        self.send(self.registry, 0, struct.pack("<I", name) + _string(interface) + struct.pack("<II", v, new))
        return new

    def roundtrip(self, timeout: float = 5.0):
        cb = self._new_id()
        self.send(1, 0, struct.pack("<I", cb))  # wl_display.sync
        end = time.time() + timeout
        while cb not in self._callbacks_done:
            if time.time() > end:
                raise WaylandError("roundtrip timed out")
            self._read_events(end - time.time())
        self._callbacks_done.discard(cb)

    def _read_events(self, timeout: float):
        self.sock.settimeout(max(0.01, timeout))
        try:
            data = self.sock.recv(65536)
        except TimeoutError:
            raise WaylandError("timed out waiting for the compositor") from None
        if not data:
            raise WaylandError("compositor closed the connection")
        self._buf += data
        while len(self._buf) >= 8:
            obj, word = struct.unpack_from("<II", self._buf)
            size, opcode = word >> 16, word & 0xFFFF
            if len(self._buf) < size:
                break
            body = self._buf[8:size]
            self._buf = self._buf[size:]
            self._dispatch(obj, opcode, body)

    def _dispatch(self, obj: int, opcode: int, body: bytes):
        if obj == 1 and opcode == 0:  # wl_display.error
            oid, code, ln = struct.unpack_from("<III", body)
            msg = body[12:12 + ln - 1].decode(errors="replace")
            raise WaylandError(f"protocol error on object {oid} code {code}: {msg}")
        if obj == self.registry and opcode == 0:  # wl_registry.global
            name, ln = struct.unpack_from("<II", body)
            iface = body[8:8 + ln - 1].decode()
            off = 8 + ln + (-ln % 4)
            (version,) = struct.unpack_from("<I", body, off)
            if iface == "wl_output":
                self.outputs.append(name)
            self.globals.setdefault(iface, (name, version))
            return
        if obj in self._watched:
            self._watched[obj].append((opcode, body))
            return
        if opcode == 0 and len(body) == 4:
            # wl_callback.done(serial); other objects' events are ignored (roundtrip only checks its own id)
            self._callbacks_done.add(obj)

    def new_object(self, watch: bool = False) -> int:
        i = self._new_id()
        if watch:
            self._watched[i] = []
        return i

    def unwatch(self, obj: int):
        self._watched.pop(obj, None)

    def next_event(self, obj: int, timeout: float = 5.0) -> tuple[int, bytes]:
        """Block until the watched object `obj` gets an event."""
        end = time.time() + timeout
        q = self._watched[obj]
        while not q:
            if time.time() > end:
                raise WaylandError("timed out waiting for an event")
            self._read_events(end - time.time())
        return q.pop(0)

    def bind_name(self, name: int, interface: str, version: int) -> int:
        new = self._new_id()
        self.send(self.registry, 0, struct.pack("<I", name) + _string(interface) + struct.pack("<II", version, new))
        return new

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class VirtualPointer:
    """Absolute + relative pointer, buttons, wheel."""

    def __init__(self, conn: Connection):
        self.c = conn
        seat = conn.bind("wl_seat", 1)
        mgr = conn.bind("zwlr_virtual_pointer_manager_v1", 1)
        self.id = conn._new_id()
        conn.send(mgr, 0, struct.pack("<II", seat, self.id))  # create_virtual_pointer
        conn.roundtrip()

    @staticmethod
    def _t() -> int:
        return int(time.monotonic() * 1000) & 0xFFFFFFFF

    def motion_absolute(self, x: float, y: float, w: int, h: int):
        x = min(max(0, int(round(x))), w - 1)
        y = min(max(0, int(round(y))), h - 1)
        self.c.send(self.id, 1, struct.pack("<IIIII", self._t(), x, y, w, h))
        self.c.send(self.id, 4)  # frame

    def motion(self, dx: float, dy: float):
        self.c.send(self.id, 0, struct.pack("<I", self._t()) + _fixed(dx) + _fixed(dy))
        self.c.send(self.id, 4)

    def button(self, code: int, down: bool):
        self.c.send(self.id, 2, struct.pack("<III", self._t(), code, 1 if down else 0))
        self.c.send(self.id, 4)

    def axis(self, axis: int, value: float, discrete: int | None = None):
        """axis 0 = vertical, 1 = horizontal. value in surface px (15 per notch is conventional)."""
        self.c.send(self.id, 5, struct.pack("<I", 0))  # axis_source wheel
        if discrete is not None:
            self.c.send(self.id, 7, struct.pack("<II", self._t(), axis) + _fixed(value) + struct.pack("<i", discrete))
        else:
            self.c.send(self.id, 3, struct.pack("<II", self._t(), axis) + _fixed(value))
        self.c.send(self.id, 4)


class VirtualKeyboard:
    def __init__(self, conn: Connection, keymap_text: str):
        self.c = conn
        seat = conn.bind("wl_seat", 1)
        mgr = conn.bind("zwp_virtual_keyboard_manager_v1", 1)
        self.id = conn._new_id()
        conn.send(mgr, 0, struct.pack("<II", seat, self.id))  # create_virtual_keyboard
        data = keymap_text.encode() + b"\0"
        fd, path = tempfile.mkstemp(prefix="hc-keymap-", dir=os.environ.get("XDG_RUNTIME_DIR"))
        try:
            os.write(fd, data)
            os.unlink(path)
            conn.send(self.id, 0, struct.pack("<II", 1, len(data)), fds=[fd])  # keymap(xkb_v1, fd, size)
            conn.roundtrip()
        finally:
            os.close(fd)

    def key(self, evcode: int, down: bool):
        self.c.send(self.id, 1, struct.pack("<III", int(time.monotonic() * 1000) & 0xFFFFFFFF, evcode, 1 if down else 0))

    def modifiers(self, depressed: int, latched: int = 0, locked: int = 0, group: int = 0):
        self.c.send(self.id, 2, struct.pack("<IIII", depressed, latched, locked, group))


class ScreenCopy:
    """zwlr_screencopy_manager_v1 into shared memory: frames of one output, with the cursor drawn in."""

    def __init__(self, conn: Connection, output_name: int | None = None):
        import mmap
        self._mmap = mmap
        self.c = conn
        self.mgr = conn.bind("zwlr_screencopy_manager_v1", 3)
        self.shm = conn.bind("wl_shm", 1)
        name = output_name if output_name is not None else conn.outputs[0]
        self.output = conn.bind_name(name, "wl_output", 1)
        self.pool = self.buf = self.mem = None
        self.geom = None  # (width, height, stride, format)
        self.fd = None
        conn.roundtrip()

    def _ensure_buffer(self, w, h, stride, fmt):
        if self.geom == (w, h, stride, fmt):
            return
        if self.buf:
            self.c.send(self.buf, 0)  # wl_buffer.destroy
            self.c.send(self.pool, 1)  # wl_shm_pool.destroy
            self.mem.close()
            os.close(self.fd)
        size = stride * h
        self.fd = os.memfd_create("hc-viewer", 0)
        os.ftruncate(self.fd, size)
        self.mem = self._mmap.mmap(self.fd, size)
        self.pool = self.c._new_id()
        self.c.send(self.shm, 0, struct.pack("<Ii", self.pool, size), fds=[self.fd])  # create_pool(id, fd, size)
        self.buf = self.c._new_id()
        self.c.send(self.pool, 0, struct.pack("<IiiiiI", self.buf, 0, w, h, stride, fmt))
        self.geom = (w, h, stride, fmt)

    def capture(self, with_damage: bool = False, timeout: float = 10.0):
        """One frame -> (bytes, width, height, stride, format, y_inverted), or None if it failed.
        with_damage waits until something on screen changed (cheap when idle)."""
        frame = self.c.new_object(watch=True)
        self.c.send(self.mgr, 0, struct.pack("<IiI", frame, 1, self.output))  # capture_output(frame, cursor, output)
        try:
            spec = None
            while True:
                op, body = self.c.next_event(frame, timeout)
                if op == 0:  # buffer(format, width, height, stride)
                    fmt, w, h, stride = struct.unpack_from("<IIII", body)
                    spec = spec or (w, h, stride, fmt)
                elif op == 3:
                    return None
                elif op == 6:  # buffer_done
                    break
            w, h, stride, fmt = spec
            self._ensure_buffer(w, h, stride, fmt)
            self.c.send(frame, 2 if with_damage else 0, struct.pack("<I", self.buf))
            flags = 0
            while True:
                op, body = self.c.next_event(frame, timeout)
                if op == 1:
                    (flags,) = struct.unpack_from("<I", body)
                elif op == 2:  # ready
                    break
                elif op == 3:
                    return None
            return bytes(self.mem[: stride * h]), w, h, stride, fmt, bool(flags & 1)
        finally:
            self.c.send(frame, 1)  # destroy
            self.c.unwatch(frame)
