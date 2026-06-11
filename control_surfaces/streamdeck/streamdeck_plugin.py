"""
Elgato Stream Deck plugin for OpenAVC.

Turns any Elgato Stream Deck into a physical control surface for AV room control.
Button presses execute macros; button images update based on system state.

Supported models: Neo, Mini, MK.2/Original V2, XL, Plus, Pedal.
"""

import asyncio
import functools
import io
import json
import os
import platform as platform_mod
import queue
import re
import socket
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

# StreamDeck and PIL are imported lazily in start() to ensure
# the .deps/ DLL search path is set up by the plugin loader first.
StreamDeck = None
PILHelper = None
Image = None
ImageDraw = None
ImageFont = None


def _lazy_import():
    """Import streamdeck and PIL after DLL paths are configured."""
    global StreamDeck, PILHelper, Image, ImageDraw, ImageFont
    if StreamDeck is not None:
        return

    from StreamDeck import DeviceManager as _DM
    from StreamDeck.ImageHelpers import PILHelper as _PH
    from PIL import Image as _Img, ImageDraw as _ID, ImageFont as _IF

    StreamDeck = _DM
    PILHelper = _PH
    Image = _Img
    ImageDraw = _ID
    ImageFont = _IF

    # On Linux, LD_LIBRARY_PATH set after process start is not picked up by
    # dlopen (glibc caches it at startup).  Extend the StreamDeck library's
    # HIDAPI search to also try full paths into .deps/, which makes dlopen
    # treat them as path lookups instead of name-only lookups.
    if platform_mod.system() == "Linux":
        _patch_hidapi_search()


def _patch_hidapi_search():
    """Extend HIDAPI library search to find .so files bundled in .deps/."""
    try:
        from StreamDeck.Transport.LibUSBHIDAPI import LibUSBHIDAPI
    except ImportError:
        return

    deps_dir = str(Path(__file__).parent.parent / ".deps")
    original_load = LibUSBHIDAPI.Library._load_hidapi_library

    def _extended_load(self, search_list):
        # Try the standard system search first
        result = original_load(self, search_list)
        if result is not None:
            return result
        # Fall back to full paths in .deps/
        deps_paths = [os.path.join(deps_dir, os.path.basename(n)) for n in search_list]
        return original_load(self, deps_paths)

    LibUSBHIDAPI.Library._load_hidapi_library = _extended_load


def _unwrap_binding(value):
    """Unwrap a binding value that may be a single dict or an array of dicts.

    The Surface Configurator stores press/release/hold as arrays to support
    multiple sequential actions. The plugin handler expects a single dict
    (the first action, which carries the mode and config).
    """
    if isinstance(value, list) and len(value) > 0:
        return value[0] if isinstance(value[0], dict) else None
    if isinstance(value, dict):
        return value
    return None


# ──── Condition Evaluation ────
#
# Self-contained copy of the platform's condition evaluator
# (server/core/condition_eval.py). Vendored rather than imported so this
# community plugin stays portable and doesn't couple to server internals.
# Semantics are kept identical to the platform evaluator so a `visible_when`
# or `auto_page` condition behaves the same here as in a macro skip_if or a
# trigger guard.

_CONDITION_OPERATOR_ALIASES = {
    "equals": "eq", "not_equals": "ne", "==": "eq", "!=": "ne",
    ">": "gt", "<": "lt", ">=": "gte", "<=": "lte",
    "equal": "eq", "not_equal": "ne", "greater_than": "gt", "less_than": "lt",
    "greater_or_equal": "gte", "less_or_equal": "lte",
}


def _coerce_numeric(value):
    """Try to coerce a value to a number for comparison."""
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    return None


def _coerce_bool(value):
    """Normalize boolean-like values for comparison."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    return None


def _eval_operator(op, actual, target):
    """Evaluate a comparison operator with alias normalization and type coercion.

    Raises ValueError on an unknown operator (callers treat that as no match).
    """
    op = _CONDITION_OPERATOR_ALIASES.get(op, op)

    if op in ("eq", "ne"):
        if isinstance(actual, bool) or isinstance(target, bool):
            a_bool = _coerce_bool(actual)
            t_bool = _coerce_bool(target)
            if a_bool is not None and t_bool is not None:
                return (a_bool == t_bool) if op == "eq" else (a_bool != t_bool)
        if type(actual) is not type(target):
            a_num = _coerce_numeric(actual)
            t_num = _coerce_numeric(target)
            if a_num is not None and t_num is not None:
                return (a_num == t_num) if op == "eq" else (a_num != t_num)
        return (actual == target) if op == "eq" else (actual != target)

    if op in ("gt", "lt", "gte", "lte"):
        if actual is None or target is None:
            return False
        a_num = _coerce_numeric(actual)
        t_num = _coerce_numeric(target)
        if a_num is not None and t_num is not None:
            if op == "gt":
                return a_num > t_num
            if op == "lt":
                return a_num < t_num
            if op == "gte":
                return a_num >= t_num
            return a_num <= t_num
        try:
            if op == "gt":
                return actual > target
            if op == "lt":
                return actual < target
            if op == "gte":
                return actual >= target
            return actual <= target
        except TypeError:
            return False

    if op == "truthy":
        return bool(actual)
    if op == "falsy":
        return not bool(actual)
    raise ValueError(f"Unknown condition operator: {op!r}")


def _condition_state_keys(cond):
    """Collect every state key referenced by a condition.

    Handles a single ``{key, operator, value}`` condition plus the compound
    ``{all: [...]}`` and ``{any: [...]}`` forms (recursively).
    """
    keys = []
    if not isinstance(cond, dict):
        return keys
    for group in ("all", "any"):
        sub = cond.get(group)
        if isinstance(sub, list):
            for child in sub:
                keys.extend(_condition_state_keys(child))
    key = cond.get("key")
    if key:
        keys.append(key)
    return keys


# ──── Virtual decks ────
#
# A virtual deck is a software stand-in implementing the same device interface
# the sessions consume — it opens, renders, and receives input exactly like
# attached hardware, except the "display" is the live mirror served over the
# plugin's HTTP routes and input arrives via the simulate_input context
# action. Lets layouts be built and tested with no deck attached.

_NO_DISPLAY = {"size": (0, 0), "format": "", "flip": (False, False), "rotation": 0}

# Geometry/format presets per virtual model. These mirror the bundled
# library's per-device constants (Devices/StreamDeck*.py) so a virtual deck
# renders byte-identically to the real hardware path.
_VIRTUAL_MODELS = {
    "Stream Deck Neo": {
        "key_count": 8, "rows": 2, "cols": 4, "dials": 0, "touch_keys": 2,
        "touch": False, "visual": True,
        "key_format": {"size": (96, 96), "format": "JPEG", "flip": (True, True), "rotation": 0},
        "screen_format": {"size": (248, 58), "format": "JPEG", "flip": (True, True), "rotation": 0},
        "touchscreen_format": _NO_DISPLAY,
    },
    "Stream Deck Mini": {
        "key_count": 6, "rows": 2, "cols": 3, "dials": 0, "touch_keys": 0,
        "touch": False, "visual": True,
        "key_format": {"size": (80, 80), "format": "BMP", "flip": (False, True), "rotation": 90},
        "screen_format": _NO_DISPLAY,
        "touchscreen_format": _NO_DISPLAY,
    },
    "Stream Deck MK.2": {
        "key_count": 15, "rows": 3, "cols": 5, "dials": 0, "touch_keys": 0,
        "touch": False, "visual": True,
        "key_format": {"size": (72, 72), "format": "JPEG", "flip": (True, True), "rotation": 0},
        "screen_format": _NO_DISPLAY,
        "touchscreen_format": _NO_DISPLAY,
    },
    "Stream Deck XL": {
        "key_count": 32, "rows": 4, "cols": 8, "dials": 0, "touch_keys": 0,
        "touch": False, "visual": True,
        "key_format": {"size": (96, 96), "format": "JPEG", "flip": (True, True), "rotation": 0},
        "screen_format": _NO_DISPLAY,
        "touchscreen_format": _NO_DISPLAY,
    },
    "Stream Deck +": {
        "key_count": 8, "rows": 2, "cols": 4, "dials": 4, "touch_keys": 0,
        "touch": True, "visual": True,
        "key_format": {"size": (120, 120), "format": "JPEG", "flip": (False, False), "rotation": 0},
        "screen_format": _NO_DISPLAY,
        "touchscreen_format": {"size": (800, 100), "format": "JPEG", "flip": (False, False), "rotation": 0},
    },
    "Stream Deck Pedal": {
        "key_count": 3, "rows": 1, "cols": 3, "dials": 0, "touch_keys": 0,
        "touch": False, "visual": False,
        "key_format": _NO_DISPLAY,
        "screen_format": _NO_DISPLAY,
        "touchscreen_format": _NO_DISPLAY,
    },
}


class _VirtualDeck:
    """Software deck implementing the device interface the sessions consume."""

    def __init__(self, model, serial, preset):
        self._model = model
        self._serial = serial
        self._preset = preset
        self._open = False
        self.brightness = None

    def id(self):
        return f"virtual:{self._serial}"

    def open(self):
        self._open = True

    def close(self):
        self._open = False

    def reset(self):
        pass

    def is_open(self):
        return self._open

    def connected(self):
        return True

    def is_visual(self):
        return self._preset["visual"]

    def is_touch(self):
        return self._preset["touch"]

    def deck_type(self):
        return self._model

    def get_serial_number(self):
        return self._serial

    def key_count(self):
        return self._preset["key_count"]

    def key_layout(self):
        return (self._preset["rows"], self._preset["cols"])

    def dial_count(self):
        return self._preset["dials"]

    def touch_key_count(self):
        return self._preset["touch_keys"]

    def key_image_format(self):
        return dict(self._preset["key_format"])

    def screen_image_format(self):
        return dict(self._preset["screen_format"])

    def touchscreen_image_format(self):
        return dict(self._preset["touchscreen_format"])

    def set_brightness(self, level):
        self.brightness = level

    # Display writes are swallowed — the live mirror taps the pre-encoded
    # images upstream, so nothing needs storing here.
    def set_key_image(self, key, image):
        pass

    def set_key_color(self, key, r, g, b):
        pass

    def set_screen_image(self, image):
        pass

    def set_touchscreen_image(self, image, x_pos=0, y_pos=0, width=0, height=0):
        pass

    # Input callbacks are unused — simulated input dispatches straight to the
    # plugin's session handlers.
    def set_key_callback_async(self, cb, loop=None):
        pass

    def set_dial_callback_async(self, cb, loop=None):
        pass

    def set_touchscreen_callback_async(self, cb, loop=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ──── Network-attached decks ────
#
# Elgato's Network Dock and the Stream Deck Studio expose the deck's HID
# reports over raw TCP (default port 5343). Two wire framings exist, detected
# from the first bytes the device sends after connect:
#
#   Cora (Network Dock, newer firmware) — every message has a 16-byte header:
#     offset 0  4B  magic 43 93 8A 41
#     offset 4  u16le flags  (VERBATIM 0x8000 = payload addressed to the HID
#                             device behind the bridge; REQ_ACK 0x4000;
#                             ACK_NAK 0x0200; RESULT 0x0100)
#     offset 6  u8  hid op   (0 = write, 1 = send feature, 2 = get feature)
#     offset 7  u8  reserved
#     offset 8  u32le message id
#     offset 12 u32le payload length, then the payload.
#
#   Legacy (Stream Deck Studio) — fixed 512-byte receive packets carrying the
#   raw HID report from byte 0; writes are raw, padded to 1024 bytes.
#
# In both modes the device sends a keepalive (payload starts 01 0A, connection
# number at byte 5) every few seconds; the host must answer each one with
# 03 1A <connection_no> (a 32-byte Cora ACK_NAK payload, or a raw 1024-byte
# packet in legacy mode). More than ~5 s without any bytes means the link is
# dead. A dock reports the deck plugged into it via a "Device 2" payload
# (01 0B): child vid/pid at offsets 26/28, serial at 94-125, and a dedicated
# TCP port for the child at offset 126 — the client opens a second connection
# to that port and drives the deck there. Hotplug arrives as an unsolicited
# Device 2 payload on the primary connection.

_NET_DEFAULT_PORT = 5343
_NET_MDNS_SERVICE = "_elg._tcp.local."
_NET_IDLE_TIMEOUT = 5.0     # no bytes received in this window -> link dead
_NET_GET_TIMEOUT = 5.0      # feature-report read timeout
_NET_CONNECT_TIMEOUT = 4.0  # TCP connect timeout
_CORA_MAGIC = b"\x43\x93\x8a\x41"
_CORA_FLAG_VERBATIM = 0x8000
_CORA_FLAG_ACK_NAK = 0x0200
_CORA_OP_WRITE = 0x00
_CORA_OP_SEND_REPORT = 0x01
_CORA_OP_GET_REPORT = 0x02
_SDS_PACKET_LEN = 512       # legacy mode: fixed receive packet size
_SDS_WRITE_LEN = 1024       # legacy mode: writes padded to this length
# The dock's own bridge endpoint reports this product id; it is not a deck.
_NET_DOCK_PID = 0xFFFF


def _cora_pack(flags, hid_op, message_id, payload):
    """Build one Cora frame (16-byte header + payload)."""
    return (
        _CORA_MAGIC
        + struct.pack("<HBBII", flags, hid_op, 0, message_id, len(payload))
        + bytes(payload)
    )


class _CoraStream:
    """Incremental Cora frame parser with resync on the magic marker."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        """Consume bytes; return a list of (flags, hid_op, message_id, payload)."""
        self._buf.extend(data)
        frames = []
        while True:
            idx = self._buf.find(_CORA_MAGIC)
            if idx < 0:
                # Keep the tail in case a magic marker straddles the chunk edge
                del self._buf[:-3]
                break
            if idx > 0:
                del self._buf[:idx]
            if len(self._buf) < 16:
                break
            flags, hid_op, _res, message_id, length = struct.unpack(
                "<HBBII", self._buf[4:16]
            )
            if len(self._buf) < 16 + length:
                break
            payload = bytes(self._buf[16:16 + length])
            del self._buf[:16 + length]
            frames.append((flags, hid_op, message_id, payload))
        return frames


def _parse_device2(payload):
    """Parse a Device 2 payload (01 0B). Returns None when no child is attached."""
    if len(payload) < 128 or payload[0] != 0x01 or payload[1] != 0x0B:
        return None
    if payload[4] != 0x02:  # status: 0x02 = child connected and ready
        return None
    vid, pid = struct.unpack_from("<HH", payload, 26)
    raw_serial = bytes(payload[94:125])
    serial = raw_serial.split(b"\x00", 1)[0].decode("ascii", "replace").strip()
    (port,) = struct.unpack_from("<H", payload, 126)
    return {"vid": vid, "pid": pid, "serial": serial, "port": port}


def _transport_error():
    """The bundled library's TransportError, so its reader thread handles our
    failures exactly like a USB unplug. Falls back to OSError when the library
    isn't loaded (unit tests)."""
    try:
        from StreamDeck.Transport.Transport import TransportError
        return TransportError
    except Exception:
        return OSError


class _NetDeckDevice:
    """One TCP connection to a network-attached deck or dock, presenting the
    same device interface as the bundled library's HID transport (open/close/
    is_open/connected/vendor_id/product_id/path/write/read/write_feature/
    read_feature) so the library's device classes work over it unchanged.

    A reader thread owns the socket's receive side: it answers keepalives,
    resolves pending feature-report reads, queues input reports for the
    library's poll loop, and surfaces Device 2 (child hotplug) payloads.
    """

    is_network = True

    def __init__(self, host, port, role="deck"):
        self._host = host
        self._port = int(port)
        self._role = role
        self._sock = None
        self._mode = None           # "cora" | "legacy"
        self._dead = True
        self._last_rx = 0.0
        self._connected_evt = threading.Event()
        self._write_lock = threading.Lock()
        # Outbound frames are queued and sent by a dedicated writer thread:
        # device writes are called from the asyncio event loop (renders), and
        # a blocking sendall against a slow unit would stall the whole
        # plugin — input handling included.
        self._out = queue.Queue(maxsize=512)
        self._writer = None
        self._inputs = queue.Queue(maxsize=256)
        self._pending = {}           # report_id -> [threading.Event, payload]
        self._pending_lock = threading.Lock()
        self._reader = None
        self._mid = 0
        self._vid = 0x0FD9
        self._pid = 0
        self.device2 = None          # latest parsed Device 2 payload (or None)
        self.device2_seen = False    # True once any Device 2 payload arrived
        self.address = f"{host}:{port}"
        # Back-reference set by _DockLink so _open_deck can find the link
        self.link = None

    # ── connection lifecycle (blocking; call from an executor) ──

    def connect(self, timeout=_NET_CONNECT_TIMEOUT):
        """Open the socket and wait for the device's first keepalive (which
        also reveals the framing mode). Raises on failure."""
        self._sock = socket.create_connection(
            (self._host, self._port), timeout=timeout
        )
        self._sock.settimeout(1.0)
        self._dead = False
        self._last_rx = time.monotonic()
        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"streamdeck-net-{self._host}:{self._port}",
            daemon=True,
        )
        self._reader.start()
        self._writer = threading.Thread(
            target=self._write_loop,
            name=f"streamdeck-net-w-{self._host}:{self._port}",
            daemon=True,
        )
        self._writer.start()
        ok = self._connected_evt.wait(timeout + 2.0)
        # The event also fires when the reader dies (so a connect against a
        # dead link never hangs) — success means alive AND mode detected.
        if not ok or self._dead or self._mode is None:
            self.close()
            raise TimeoutError("device sent no keepalive after connect")

    def close(self):
        self._dead = True
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        with self._pending_lock:
            for evt_payload in self._pending.values():
                evt_payload[0].set()
            self._pending.clear()

    # ── reader thread ──

    def _read_loop(self):
        cora = _CoraStream()
        sds = bytearray()
        head = bytearray()
        sock = self._sock
        while not self._dead and sock is not None:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            self._last_rx = time.monotonic()
            if self._mode is None:
                head.extend(data)
                if len(head) >= 4 and head[:4] == _CORA_MAGIC:
                    self._mode = "cora"
                elif len(head) >= 2 and head[0] == 0x01 and head[1] == 0x0A:
                    self._mode = "legacy"
                elif len(head) >= 4:
                    break  # not a deck protocol
                else:
                    continue
                data, head = bytes(head), bytearray()
            if self._mode == "cora":
                for flags, hid_op, mid, payload in cora.feed(data):
                    self._handle_payload(payload, flags, hid_op, mid)
            else:
                sds.extend(data)
                while len(sds) >= _SDS_PACKET_LEN:
                    packet = bytes(sds[:_SDS_PACKET_LEN])
                    del sds[:_SDS_PACKET_LEN]
                    self._handle_payload(packet, None, None, None)
        self._dead = True
        self._connected_evt.set()  # unblock a connect() waiting on a dead link

    def _handle_payload(self, payload, flags, hid_op, mid):
        if len(payload) < 2:
            return
        # Keepalive: answer every one; the first marks the link connected.
        if payload[0] == 0x01 and payload[1] == 0x0A and len(payload) > 4:
            conn_no = payload[5] if len(payload) > 5 else 0
            ack = bytes((0x03, 0x1A, conn_no)) + b"\x00" * 29
            try:
                if self._mode == "cora":
                    self._send_raw(_cora_pack(
                        _CORA_FLAG_ACK_NAK, hid_op or 0, mid or 0, ack
                    ))
                else:
                    self._send_raw(ack.ljust(_SDS_WRITE_LEN, b"\x00"))
            except OSError:
                return
            self._connected_evt.set()
            return
        # Device 2: the dock reporting its attached deck (also hotplug events)
        if payload[0] == 0x01 and payload[1] == 0x0B:
            self.device2 = _parse_device2(payload)
            self.device2_seen = True
            self._resolve_pending(0x1C, payload)
            return
        # Input report from the deck (delivered whole, report id first)
        if payload[0] == 0x01:
            try:
                self._inputs.put_nowait(payload)
            except queue.Full:
                try:
                    self._inputs.get_nowait()
                    self._inputs.put_nowait(payload)
                except queue.Empty:
                    pass
            return
        # Feature-report response. Verbatim responses key on payload[0];
        # bridge ("host") responses are 0x03-prefixed and key on payload[1].
        if self._mode == "cora" and flags is not None and flags & _CORA_FLAG_VERBATIM:
            self._resolve_pending(payload[0], payload)
        elif payload[0] == 0x03:
            self._resolve_pending(payload[1], payload)
        else:
            self._resolve_pending(payload[0], payload)

    def _resolve_pending(self, report_id, payload):
        with self._pending_lock:
            slot = self._pending.pop(report_id, None)
        if slot is not None:
            slot[1] = payload
            slot[0].set()

    # ── shared send path ──

    def _send_raw(self, data):
        if self._dead:
            raise OSError("network deck connection is closed")
        try:
            self._out.put_nowait(bytes(data))
        except queue.Full:
            # The unit has stopped draining its socket faster than we can
            # back off — the link is effectively dead. Dropping frames
            # instead would corrupt multi-packet image streams.
            self._dead = True
            raise OSError("network deck stopped accepting data") from None

    def _write_loop(self):
        while not self._dead:
            try:
                data = self._out.get(timeout=0.5)
            except queue.Empty:
                continue
            sock = self._sock
            if sock is None:
                break
            try:
                with self._write_lock:
                    sock.sendall(data)
            except OSError:
                break
        self._dead = True

    def _next_mid(self):
        self._mid = (self._mid + 1) & 0xFFFFFF
        return self._mid

    def _get_report(self, request_payload, report_id, flags):
        evt = threading.Event()
        slot = [evt, None]
        with self._pending_lock:
            self._pending[report_id] = slot
        try:
            if self._mode == "cora":
                self._send_raw(_cora_pack(
                    flags, _CORA_OP_GET_REPORT, self._next_mid(), request_payload
                ))
            else:
                self._send_raw(bytes(request_payload).ljust(_SDS_WRITE_LEN, b"\x00"))
            if not evt.wait(_NET_GET_TIMEOUT) or slot[1] is None:
                raise _transport_error()("feature report read timed out")
            return slot[1]
        finally:
            with self._pending_lock:
                self._pending.pop(report_id, None)

    def read_host_feature(self, report_id):
        """Bridge-level feature read (0x03-prefixed, non-verbatim): dock/unit
        identity (0x80), Device 2 (0x1C), firmware/serial/MAC."""
        return self._get_report(bytes((0x03, report_id)), report_id, 0)

    def probe_endpoint(self, timeout=_NET_GET_TIMEOUT):
        """Classify this endpoint: ``"primary"`` or ``"deck"``.

        A unit/bridge endpoint (the dock's own port, a Studio's Ethernet
        port) answers the 0x80 identity query; a deck endpoint (the docked
        deck's dedicated TCP port — which is what docks advertise via
        discovery) ignores 0x80 and answers the verbatim gen2 0x08 (or Mini
        0xA1) query instead. All three are sent at once and the first answer
        decides, mirroring the reference implementation. On "primary" the
        unit's vid/pid are parsed from the 0x80 payload (offsets 12/14).
        """
        probes = {
            0x80: (bytes((0x03, 0x80)), 0),
            0x08: (bytes((0x08,)), _CORA_FLAG_VERBATIM),
            0xA1: (bytes((0xA1,)), _CORA_FLAG_VERBATIM),
        }
        slots = {}
        with self._pending_lock:
            for rid in probes:
                slots[rid] = [threading.Event(), None]
                self._pending[rid] = slots[rid]
        try:
            for rid, (payload, flags) in probes.items():
                if self._mode == "cora":
                    self._send_raw(_cora_pack(
                        flags, _CORA_OP_GET_REPORT, self._next_mid(), payload
                    ))
                else:
                    self._send_raw(bytes(payload).ljust(_SDS_WRITE_LEN, b"\x00"))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not self._dead:
                if slots[0x80][1] is not None:
                    payload = slots[0x80][1]
                    if len(payload) >= 16:
                        self._vid, self._pid = struct.unpack_from("<HH", payload, 12)
                    return "primary"
                if slots[0x08][1] is not None or slots[0xA1][1] is not None:
                    return "deck"
                time.sleep(0.05)
            raise _transport_error()("unit did not answer identification")
        finally:
            with self._pending_lock:
                for rid in probes:
                    self._pending.pop(rid, None)

    # ── the device interface the bundled library's classes consume ──

    def open(self):
        if self._dead:
            raise _transport_error()("network deck connection is closed")

    def is_open(self):
        return not self._dead

    def connected(self):
        return (
            not self._dead
            and (time.monotonic() - self._last_rx) <= _NET_IDLE_TIMEOUT
        )

    def vendor_id(self):
        return self._vid

    def product_id(self):
        return self._pid

    def path(self):
        return f"tcp:{self._host}:{self._port}"

    def write(self, payload):
        try:
            if self._mode == "cora":
                self._send_raw(_cora_pack(
                    _CORA_FLAG_VERBATIM, _CORA_OP_WRITE, 0, bytes(payload)
                ))
            else:
                self._send_raw(bytes(payload).ljust(_SDS_WRITE_LEN, b"\x00"))
        except OSError as e:
            raise _transport_error()(str(e)) from e
        return len(payload)

    def write_feature(self, payload):
        try:
            if self._mode == "cora":
                self._send_raw(_cora_pack(
                    _CORA_FLAG_VERBATIM, _CORA_OP_SEND_REPORT, 0, bytes(payload)
                ))
            else:
                self._send_raw(bytes(payload).ljust(_SDS_WRITE_LEN, b"\x00"))
        except OSError as e:
            raise _transport_error()(str(e)) from e
        return len(payload)

    def read_feature(self, report_id, length):
        if self._dead:
            raise _transport_error()("network deck connection is closed")
        if self._mode == "cora":
            payload = self._get_report(bytes((report_id,)), report_id, _CORA_FLAG_VERBATIM)
        else:
            payload = self._get_report(bytes((report_id,)), report_id, 0)
        return (bytes(payload) + b"\x00" * length)[:length]

    def read(self, length):
        if self._dead:
            raise _transport_error()("network deck connection is closed")
        try:
            payload = self._inputs.get_nowait()
        except queue.Empty:
            return None
        return (payload + b"\x00" * length)[:length]


def _classify_net_error(exc):
    """Human connection status for the deck inspector / ghost cards."""
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "unreachable (no response)"
    if isinstance(exc, socket.gaierror):
        return "address not found"
    if isinstance(exc, OSError):
        return "unreachable"
    return str(exc) or "connection failed"


class _DockLink:
    """One configured network deck entry: owns the primary TCP connection
    (keepalive, identity, Device 2 watch) and, when the unit is a dock, the
    second connection that drives the docked deck. Reconnects with capped
    exponential backoff; the plugin watchdog drives it every tick."""

    def __init__(self, host, port):
        self.host = host
        self.port = int(port)
        self.key = f"{host}:{port}"
        self.state_key = re.sub(r"[^A-Za-z0-9_-]", "_", self.key)
        self.primary = None          # _NetDeckDevice (the unit itself)
        self.child = None            # _NetDeckDevice (deck docked in it)
        self.status = "connecting"
        self.fail_count = 0
        self.next_attempt = 0.0
        self.observed_serial = ""    # deck serial, once known (for re-resolve)
        # True when the configured address IS a deck endpoint (docks
        # advertise the docked deck's own port via discovery) — there is no
        # separate child connection to manage in that case.
        self.is_deck_endpoint = False
        self._connecting = False
        self._resolve_attempted = False

    def close(self):
        for dev in (self.child, self.primary):
            if dev is not None:
                dev.close()
        self.primary = None
        self.child = None

    def _connect_blocking(self):
        """Connect the configured endpoint, classify it, and bring up the
        deck behind it. Runs in an executor (blocking sockets).

        Two shapes exist in the field: the unit's bridge port (dock at its
        base port, Studio Ethernet) where the deck arrives as a Device-2
        child on a second connection — and the deck's own endpoint (the
        port docks advertise via discovery), where this one socket IS the
        deck and its identity comes from the Device-2 payload in place.
        """
        endpoint = _NetDeckDevice(self.host, self.port, role="primary")
        endpoint.link = self
        endpoint.connect()
        try:
            kind = endpoint.probe_endpoint()
            if kind == "deck":
                info = endpoint.device2
                if info is None:
                    payload = endpoint.read_host_feature(0x1C)
                    info = _parse_device2(payload)
                if info is None:
                    raise _transport_error()("no deck present at this address")
                endpoint._vid, endpoint._pid = info["vid"], info["pid"]
                if info.get("serial"):
                    self.observed_serial = info["serial"]
                self.primary = endpoint
                self.is_deck_endpoint = True
                return
            # Bridge/unit endpoint: learn the current child (docks answer;
            # deck-units like the Studio may not have one attached).
            try:
                endpoint.read_host_feature(0x1C)
            except Exception:
                pass
        except Exception:
            endpoint.close()
            raise
        self.primary = endpoint
        self.is_deck_endpoint = False
        self._connect_child_blocking()

    def _connect_child_blocking(self):
        info = self.primary.device2 if self.primary else None
        if not info:
            return
        child = _NetDeckDevice(self.host, info["port"], role="child")
        child.link = self
        child.connect()
        child._vid, child._pid = info["vid"], info["pid"]
        if info.get("serial"):
            self.observed_serial = info["serial"]
        self.child = child

    async def ensure(self, loop, log):
        """Watchdog tick: reconnect what's down (respecting backoff) and
        reconcile the child against the latest Device 2 report. Returns the
        transports that are up; the caller turns them into deck objects."""
        now = time.monotonic()
        if self._connecting:
            return self._live_transports()

        primary_up = self.primary is not None and self.primary.connected()
        if not primary_up:
            self.close()
            if now < self.next_attempt:
                return []
            self._connecting = True
            try:
                await loop.run_in_executor(None, self._connect_blocking)
                self.fail_count = 0
                self.status = "connected"
                self._resolve_attempted = False
            except Exception as e:
                self.close()
                self.fail_count += 1
                self.next_attempt = now + min(3.0 * (2 ** min(self.fail_count - 1, 4)), 30.0)
                self.status = _classify_net_error(e)
                log(
                    f"Network deck {self.key}: {self.status} "
                    f"(retry in {int(self.next_attempt - now)}s)",
                    "warning",
                )
                return []
            finally:
                self._connecting = False
            return self._live_transports()

        # On a deck endpoint there is no child connection to manage — its
        # Device-2 payload describes the deck on THIS socket, not a second
        # port to dial.
        if self.is_deck_endpoint:
            return self._live_transports()

        # Primary healthy: reconcile the child with the latest hotplug report.
        info = self.primary.device2
        child_up = self.child is not None and self.child.connected()
        want_port = info["port"] if info else None
        have_port = self.child._port if self.child else None
        if info is None and self.child is not None:
            # Deck removed from the dock
            self.child.close()
            self.child = None
        elif info is not None and (not child_up or have_port != want_port):
            if self.child is not None:
                self.child.close()
                self.child = None
            self._connecting = True
            try:
                await loop.run_in_executor(None, self._connect_child_blocking)
            except Exception as e:
                log(
                    f"Network deck {self.key}: docked deck {_classify_net_error(e)}",
                    "warning",
                )
            finally:
                self._connecting = False
        return self._live_transports()

    def _live_transports(self):
        out = []
        for dev in (self.primary, self.child):
            if dev is not None and dev.connected() and dev.product_id() not in (0, _NET_DOCK_PID):
                out.append(dev)
        return out


_StudioClass = None


def _studio_deck_class():
    """Vendored Stream Deck Studio device definition (32 keys, 2 dials),
    matching the upstream library's gen2 image protocol. The bundled library
    predates the Studio; this subclass supplies its constants and input
    parsing so the rack unit works over its built-in Ethernet."""
    global _StudioClass
    if _StudioClass is not None:
        return _StudioClass
    from StreamDeck.Devices.StreamDeck import ControlType, DialEventType
    from StreamDeck.Devices.StreamDeckOriginalV2 import StreamDeckOriginalV2

    class StreamDeckStudio(StreamDeckOriginalV2):
        KEY_COUNT = 32
        KEY_COLS = 16
        KEY_ROWS = 2
        DIAL_COUNT = 2
        TOUCH_KEY_COUNT = 0
        KEY_PIXEL_WIDTH = 80
        KEY_PIXEL_HEIGHT = 120
        KEY_IMAGE_FORMAT = "JPEG"
        KEY_FLIP = (False, False)
        KEY_ROTATION = 0
        DECK_TYPE = "Stream Deck Studio"
        DECK_VISUAL = True

        _DIAL_EVENT_TRANSFORM = {
            DialEventType.TURN: lambda v: v if v < 0x80 else -(0x100 - v),
            DialEventType.PUSH: bool,
        }

        def _read_control_states(self):
            data = self.device.read(43)
            if data is None:
                return None
            states = data[1:]
            if states[0] == 0x00:  # key event
                return {
                    ControlType.KEY: [bool(s) for s in states[3:3 + self.KEY_COUNT]]
                }
            if states[0] == 0x03:  # dial event
                if states[3] == 0x01:
                    event_type = DialEventType.TURN
                elif states[3] == 0x00:
                    event_type = DialEventType.PUSH
                else:
                    return None
                transform = self._DIAL_EVENT_TRANSFORM[event_type]
                return {
                    ControlType.DIAL: {
                        event_type: [
                            transform(s)
                            for s in states[4:4 + self.DIAL_COUNT]
                        ],
                    }
                }
            return None

        def get_serial_number(self):
            serial = self.device.read_feature(0x06, 32)
            return self._extract_string(serial[5:])

        def get_firmware_version(self):
            version = self.device.read_feature(0x05, 32)
            return self._extract_string(version[5:])

    _StudioClass = StreamDeckStudio
    return _StudioClass


def _net_deck_class(pid):
    """Map a child/unit product id to the library device class driving it."""
    if StreamDeck is None:
        return None
    if pid == 0x00AA:
        return _studio_deck_class()
    ids = StreamDeck.USBProductIDs
    mapping = {
        ids.USB_PID_STREAMDECK_ORIGINAL: StreamDeck.StreamDeckOriginal,
        ids.USB_PID_STREAMDECK_ORIGINAL_V2: StreamDeck.StreamDeckOriginalV2,
        ids.USB_PID_STREAMDECK_MK2_SCISSOR: StreamDeck.StreamDeckOriginalV2,
        ids.USB_PID_STREAMDECK_MK2_MODULE: StreamDeck.StreamDeckOriginalV2,
        ids.USB_PID_STREAMDECK_MINI: StreamDeck.StreamDeckMini,
        ids.USB_PID_STREAMDECK_NEO: StreamDeck.StreamDeckNeo,
        ids.USB_PID_STREAMDECK_XL: StreamDeck.StreamDeckXL,
        ids.USB_PID_STREAMDECK_MK2: StreamDeck.StreamDeckOriginalV2,
        ids.USB_PID_STREAMDECK_MK2_V2: StreamDeck.StreamDeckOriginalV2,
        ids.USB_PID_STREAMDECK_PEDAL: StreamDeck.StreamDeckPedal,
        ids.USB_PID_STREAMDECK_MINI_MK2: StreamDeck.StreamDeckMini,
        ids.USB_PID_STREAMDECK_MINI_MK2_MODULE: StreamDeck.StreamDeckMini,
        ids.USB_PID_STREAMDECK_XL_V2: StreamDeck.StreamDeckXL,
        ids.USB_PID_STREAMDECK_XL_V2_MODULE: StreamDeck.StreamDeckXL,
        ids.USB_PID_STREAMDECK_PLUS: StreamDeck.StreamDeckPlus,
    }
    return mapping.get(pid)


class _DeckSession:
    """Per-deck runtime state for one connected Stream Deck.

    The plugin holds one session per attached deck; every handler and render
    method takes the session it acts on, so multiple decks run independently
    (own page, own pressed keys, own subscriptions, own idle timer).
    """

    def __init__(self, deck):
        self.deck = deck
        self.device_id = None      # HID path — stable while attached
        self.serial = "unknown"
        self.model = ""
        self.geometry = {}         # key_count/rows/columns/dials/touch/screens
        self.current_page = 0
        self.pressed_keys = set()  # keys currently held (momentary highlight)
        self.hold_tasks = {}       # key_index -> periodic task ID (hold-repeat)
        self.press_times = {}      # key_index -> timestamp (tap/hold mode)
        self.dial_press_times = {} # dial index -> push timestamp (deferred click)
        self.dial_turned = set()   # dials that turned while held (chord, no click)
        self.feedback_subs = []    # state subscription IDs for this deck
        self.auto_page_keys = set()
        self.touch_strip_keys = set()
        self.info_strip_keys = set()
        self.brightness_keys = set()
        self.last_input = 0.0      # loop time of the last key/dial/touch input
        self.idle_dimmed = False   # True while idle_dim has lowered brightness
        self.is_virtual = False
        self.render_version = 0    # bumped when mirrored images change
        self.touch_key_colors = {}  # key index -> current hex color (mirror)
        self.mirror_bump_handle = None  # debounce timer for render_version
        self.overlay_active = False     # show_message overlay suppresses renders
        self.overlay_handle = None      # auto-restore timer for the overlay
        self.strip_image = None         # cached full-strip PIL render (partial updates)
        self.strip_render_task = None   # debounced strip-redraw task
        self.strip_dirty = None         # zone indexes pending redraw, or "all"
        self.flash_zones = {}           # zone index -> clear timer (touch flash)
        self.clock_minute = -1          # last minute an idle clock rendered
        self.macro_keys = {}            # macro_id -> {(page, key_index), ...}
        self.macro_marks = {}           # (page, key_index) -> running|done|error
        self.macro_clear_handles = {}   # (page, key_index) -> flash-clear timer
        self.input_seq = 0              # monotonic counter for the input echo


class StreamDeckPlugin:

    PLUGIN_INFO = {
        "id": "streamdeck",
        "name": "Elgato Stream Deck",
        "version": "1.32.0",
        "author": "OpenAVC",
        "description": "Use Elgato Stream Deck hardware as a physical control surface.",
        "usage": (
            "Plug in a Stream Deck over USB, add one on the network (Elgato "
            "Network Dock or a Stream Deck Studio's Ethernet port), or add a "
            "virtual deck to design without hardware. Open the **Stream "
            "Deck** view in the sidebar: the picture of your deck is live. "
            "Click any key to set what it does (run a macro, send a device "
            "command, set a variable, switch pages); Shift+click presses it "
            "for real. Add pages with the + tab, and lock keys you want on "
            "every page — like page switchers. Dials, the touch strip, and "
            "the info screen are set up by clicking them in the picture."
        ),
        "category": "control_surface",
        "license": "MIT",
        "platforms": ["win_x64", "linux_x64", "linux_arm64"],
        # Verified against the v0.15.1 tag: every API this plugin calls
        # (variable_set, register_router, subscriptions, periodic tasks) and
        # both gated capabilities (variable_write, http_endpoints) exist
        # there. on_config_changed simply isn't called on older cores.
        "min_openavc_version": "0.15.1",
        "dependencies": ["streamdeck", "pillow>=10.0"],
        "native_dependencies": [
            {
                "id": "hidapi",
                "name": "HIDAPI",
                "version": "0.15.0",
                "license": "BSD-3-Clause",
                "required": True,
                "check": {
                    "type": "library_load",
                    "names": {
                        "Windows": "hidapi.dll",
                        "Linux": "libhidapi-libusb.so",
                    },
                },
                "platforms": {
                    "win_x64": {
                        "url": "https://github.com/libusb/hidapi/releases/download/hidapi-0.15.0/hidapi-win.zip",
                        "type": "zip",
                        "extract": "x64/hidapi.dll",
                    },
                    "linux_x64": {
                        "url": "https://github.com/open-avc/openavc-plugins/releases/download/hidapi-0.15.0/hidapi-linux-x86_64.zip",
                        "type": "zip",
                        "extract": "libhidapi-libusb.so",
                    },
                    "linux_arm64": {
                        "url": "https://github.com/open-avc/openavc-plugins/releases/download/hidapi-0.15.0/hidapi-linux-aarch64.zip",
                        "type": "zip",
                        "extract": "libhidapi-libusb.so",
                    },
                },
            },
        ],
        "capabilities": [
            "state_read",
            "state_write",
            "variable_write",
            "event_emit",
            "event_subscribe",
            "macro_execute",
            "device_command",
            "usb_access",
            # Network-attached decks: outbound TCP to the deck plus mDNS
            # browse (api.mdns_browse, feature-detected — older cores fall
            # back to manual add-by-address).
            "network_listen",
            "http_endpoints",
        ],
    }

    # No CONFIG_SCHEMA: every setting is edited in context inside the
    # Stream Deck view (brightness on the deck, colors under Appearance,
    # pages as tabs), so the generic settings form has nothing to show.
    # The config keys themselves (brightness, button_color, text_color,
    # deck_settings, ...) are documented in AI_GUIDE and the README.

    SURFACE_LAYOUT = {
        "type": "grid",
        "rows": 2,
        "columns": 4,
        "key_size_px": 72,
        "key_spacing_px": 4,
        "supports_pages": True,
        # Device-backed surface: the IDE editor renders only real units
        # (USB, network, or virtual). With none connected it shows a connect /
        # add-deck state instead of this fallback grid.
        "requires_device": True,
        "device_label": "Stream Deck",
        # The editor offers "Add network deck" (ext/network/scan + /test
        # routes, network_decks config array).
        "network": True,
        # Must match the _VIRTUAL_MODELS preset table below.
        "virtual_models": [
            "Stream Deck Neo",
            "Stream Deck Mini",
            "Stream Deck MK.2",
            "Stream Deck XL",
            "Stream Deck +",
            "Stream Deck Pedal",
        ],
    }

    AI_GUIDE = (
        "The default SURFACE_LAYOUT is for the Neo (2x4). The actual hardware "
        "is detected at runtime and published to state: plugin.streamdeck.model, "
        "rows, columns, key_count, dial_count, touch_key_count, "
        "has_touchscreen, and has_info_screen. Read these keys to learn what "
        "the connected deck offers before configuring it — dials and a "
        "touchscreen exist only on the Stream Deck + (dial_count 4, "
        "has_touchscreen true); side touch keys and the info screen only on "
        "the Neo (touch_key_count 2, has_info_screen true). When connected is "
        "false, no deck is attached and the geometry keys are stale or zero. "
        "Touch keys are configured as ordinary 'buttons' entries at the "
        "indices after the LCD keys (Neo: 8 and 9). They have no display — "
        "only their bg_color (and feedback bg colors) show, as an RGB glow; "
        "label and icon are ignored. "
        "When has_info_screen is true, a top-level 'info_strip' object renders "
        "the small info screen: {\"source\": \"state\", \"key\": "
        "\"var.room_temp\", \"label\": \"Temp\"} shows the key's live value "
        "under the label, or {\"source\": \"text\", \"text\": \"Room A\"} "
        "shows static text. Info elements also accept 'icon', 'unit', "
        "'meter', and 'feedback' (see the display fields below), and an "
        "'items' array of up to two such elements renders them side by side "
        "({\"items\": [{\"label\": \"Temp\", \"key\": \"var.room_temp\", "
        "\"unit\": \"F\"}, {\"source\": \"text\", \"text\": \"Room A\"}]}). "
        "With no info_strip configured the screen shows a clock; "
        "{\"source\": \"clock\"} asks for it explicitly and "
        "{\"source\": \"blank\"} turns the screen off. "
        "Brightness can follow state: a top-level 'auto_brightness' array of "
        "{\"level\": 0-100, \"when\": {condition}} rules (same operator "
        "schema; first match wins, no match falls back to the base "
        "'brightness' config). A top-level 'idle_dim' object "
        "{\"after_seconds\": N, \"level\": 0-100} dims the deck after N "
        "seconds without any key/dial/touch input; any input wakes it. "
        "Example: dim to 10 when device.projector_1.power is off, and "
        "idle-dim to 5 after 600 seconds. "
        "Base brightness is the top-level 'brightness' (0-100, default 70). "
        "Per-deck base levels live in a top-level 'deck_settings' map: "
        "{\"deck_settings\": {\"ABC123\": {\"brightness\": 40}}} — a unit "
        "property that never creates a decks override. Default key colors "
        "are the top-level 'button_color' and 'text_color' hex values. "
        "Multiple decks: every connected deck is listed in "
        "plugin.streamdeck.deck_serials (comma-separated) with per-deck state "
        "at plugin.streamdeck.<serial>.* (connected, model, rows, columns, "
        "key_count, dial_count, touch_key_count, has_touchscreen, "
        "has_info_screen, current_page). The un-prefixed singleton keys track "
        "the primary (earliest-connected) deck. By default every deck mirrors "
        "the main config; to give a specific deck its own assignments, add a "
        "top-level 'decks' map keyed by serial — the entry fully replaces the "
        "per-deck sections (buttons, global_buttons, auto_page, dials, "
        "touchscreen, info_strip, auto_brightness, idle_dim, page_names) for "
        "that deck: {\"decks\": {\"ABC123\": {\"buttons\": [...]}}}. "
        "Macros can drive the deck with an event.emit step targeting "
        "plugin.streamdeck.action.<name> — actions: set_page {page}, "
        "set_brightness {level} (holds until the next brightness rule, idle "
        "dim, or wake), flash_key {index, times?}, show_message {text, "
        "seconds?} (splashes the text across the whole deck; the first press "
        "dismisses it without firing that key), identify_deck {}. All accept "
        "an optional serial to target one deck; omitted means every deck. "
        "Example macro step: {\"action\": \"event.emit\", \"event\": "
        "\"plugin.streamdeck.action.show_message\", \"payload\": {\"text\": "
        "\"Mics are LIVE\", \"seconds\": 10}}. "
        "To work with no hardware attached, add a top-level 'virtual_decks' "
        "array: [{\"model\": \"Stream Deck +\", \"serial\": \"VIRT-1\"}] — "
        "models: Stream Deck Neo, Stream Deck Mini, Stream Deck MK.2, Stream "
        "Deck XL, Stream Deck +, Stream Deck Pedal. A virtual deck behaves "
        "exactly like attached hardware (sessions, pages, dials, state, decks "
        "overrides); the user sees and clicks it in the IDE's Live View, and "
        "per-serial state marks it with <serial>.virtual = true. "
        "Decks reached over the network (Elgato Network Dock, or a Stream "
        "Deck Studio's built-in Ethernet) are added with a top-level "
        "'network_decks' array: [{\"host\": \"192.168.1.40\", \"port\": 5343}] "
        "— port is optional (default 5343), and the plugin records the deck's "
        "serial on the entry after the first connect so it can follow the "
        "deck to a new DHCP address. Network decks never attach on their own; "
        "an entry here is the explicit opt-in. Once connected they behave "
        "exactly like USB decks (same buttons/pages/decks config, same state "
        "keys) with <serial>.transport = \"network\" and <serial>.address "
        "set. Connection progress per entry is published at "
        "plugin.streamdeck.net.<host_port>.status (host_port has non-"
        "alphanumerics replaced with _, e.g. 192_168_1_40_5343). "
        "Button indices go left-to-right, "
        "top-to-bottom (e.g. Neo: 0-3 top row, 4-7 bottom row; MK.2: 0-4 top, "
        "5-9 middle, 10-14 bottom). Use page 0 unless multi-page is requested. "
        "A button's 'press' is an array of one or more actions, run in order. "
        "Supported press actions are exactly: macro, device.command, state.set, "
        "and navigate (deck page: page \"__next_page__\", \"__prev_page__\", or a "
        "page index). state.set may write only this plugin's own "
        "plugin.streamdeck.* state or a var.* user variable; writes to device.*, "
        "ui.*, or system.* are ignored. script.call and value_map are panel-only "
        "and do not run on surface buttons — to call a script from a button, run "
        "a one-line macro instead. "
        "Keys that must stay the same on every page (page switchers, "
        "mute-all, help) go in a top-level 'global_buttons' array: same entry "
        "shape as 'buttons' but with NO 'page' field. A global_buttons entry "
        "at an index wins over any per-page button at that index, on every "
        "page. Example page-switcher pair: {\"global_buttons\": [{\"index\": "
        "6, \"icon\": \"chevron-left\", \"bindings\": {\"press\": "
        "[{\"action\": \"navigate\", \"page\": \"__prev_page__\"}]}}, "
        "{\"index\": 7, \"icon\": \"chevron-right\", \"bindings\": "
        "{\"press\": [{\"action\": \"navigate\", \"page\": "
        "\"__next_page__\"}]}}]}. A key that navigates to a specific page "
        "index is automatically highlighted while that page is showing. "
        "Common AV icons: power, volume-2, volume-x, play, pause, square (stop), "
        "skip-back, skip-forward, mic, mic-off, monitor, tv, sun, moon, "
        "thermometer, fan, camera, video, airplay, cast. "
        "Always set a label OR icon (or both) so the button isn't blank. "
        "To hide a button based on state, add a 'visible_when' object to its "
        "bindings (same shape as panel UI visible_when): "
        "{\"key\": \"device.projector_1.power\", \"operator\": \"eq\", "
        "\"value\": \"on\"} — operators eq/ne/gt/lt/gte/lte/truthy/falsy, plus "
        "an 'any':[...] array for OR logic. A hidden button shows as a blank "
        "black key and ignores presses. "
        "To switch pages automatically, add a top-level 'auto_page' array to "
        "the config (alongside 'buttons'): each entry is {\"page\": N, \"when\": "
        "{condition}} using the same operator schema. Rules are evaluated in "
        "order and the first match wins, so put more specific conditions first. "
        "Pages exist by being used — there is no page-count setting: placing "
        "a button, auto_page rule, page name, or numeric navigate target on "
        "page N creates pages 0 through N (every deck always has page 0). "
        "Pages can be labeled with a per-deck 'page_names' section "
        "({\"0\": \"Sources\"}), and decks with a top-level 'deck_names' map "
        "({\"<serial>\": \"Lectern\"}) — names are display-only. "
        "Display fields (shared by touchscreen zones, info elements, and "
        "buttons): 'label_source' (state key whose live value replaces the "
        "label), 'value_source' (state key shown as a live value) with "
        "optional 'unit' (\"dB\", \"%\"), 'icon', and 'meter' — a live level "
        "bar driven by value_source. A meter draws automatically when the "
        "element's adjust/drag_adjust declares min and max; set \"meter\": "
        "{\"min\": 0, \"max\": 100, \"color\": \"#8ab493\", \"thresholds\": "
        "[{\"above\": 90, \"color\": \"#e05341\"}]} to control it, "
        "\"meter\": false to suppress it (button meters are never automatic "
        "and default to 0..100 when bounds are omitted). Zones and info "
        "elements also accept 'feedback' with the same schema as button "
        "feedback (condition or states map) for state-driven colors — e.g. "
        "a zone that turns red while device.amp.clip is true. "
        "When dial_count > 0, a top-level 'dials' array configures the rotary "
        "encoders (not paged — dials keep their assignment on every page): "
        "{\"index\": 0, \"label\": \"Volume\", \"icon\": \"volume-2\", "
        "\"unit\": \"%\", \"adjust\": {\"key\": "
        "\"var.volume\", \"step\": 2, \"min\": 0, \"max\": 100}, \"cw\": "
        "[actions], \"ccw\": [actions], \"press\": [actions], \"long_press\": "
        "[actions], \"hold_threshold_ms\": 500, \"pressed_adjust\": {\"key\": "
        "\"var.volume\", \"step\": 1, \"min\": 0, \"max\": 100}}. 'adjust' "
        "increments the key by step per detent turned (clamped to min/max) — "
        "ideal for volume, mic gain, or camera pan/tilt speed; the key must be "
        "a var.* variable or this plugin's own state, and a macro/trigger can "
        "watch it to drive the device. 'cw'/'ccw' actions run once per detent "
        "turned (capped at 8 per event, so command-per-click mappings track "
        "fast spins). 'press' runs on dial push; with 'long_press' or any "
        "pressed_* field configured, a quick release fires 'press', a release "
        "past hold_threshold_ms fires 'long_press', and turning while held "
        "routes to 'pressed_adjust'/'pressed_cw'/'pressed_ccw' (fine trim) "
        "and fires no click. All actions use the same format as button press "
        "arrays. "
        "When has_touchscreen is true, the touch strip shows one zone per dial "
        "by default: the dial's label, icon, live adjust value, and an "
        "automatic meter when the adjust has bounds — no config needed. "
        "Tapping a dial's zone presses the dial (its 'press'/'long_press'), "
        "or set dial-level 'touch'/'long_touch' overrides; \"fader\": true on "
        "the dial makes taps and swipes on its zone jump the adjust value to "
        "the touched position. With nothing configured at all the strip shows "
        "a clock ({\"touchscreen\": {\"idle\": \"blank\"}} opts out). "
        "To take over the strip, set a top-level 'touchscreen' object: "
        "{\"zones\": [{\"label\": \"Mics\", \"value_source\": \"var.mic_gain\", "
        "\"unit\": \"dB\", \"icon\": \"mic\", \"meter\": {...}, \"feedback\": "
        "{...}, \"touch\": [actions], "
        "\"long_touch\": [actions], \"drag_adjust\": {\"key\": \"var.x\", "
        "\"step\": 1, \"min\": 0, \"max\": 100, \"fader\": true}, "
        "\"bg_color\": \"#1a1a2e\", \"text_color\": \"#e0e0e0\"}]}. Zones "
        "split the strip evenly (or set explicit 'x'/'w' pixel bounds, strip "
        "is 800x100); 'touch' runs on tap, 'long_touch' on long-press "
        "(falls back to 'touch' when absent), and a horizontal swipe steps "
        "'drag_adjust' like turning a dial — or jumps straight to the touched "
        "position when its 'fader' is true (needs min and max). Every touch "
        "briefly flashes the touched zone on the glass. Custom zones replace "
        "ALL default per-dial zones, so carry over any readouts you still "
        "want. "
        "Buttons can show live data too: 'value_source' (+ 'unit') renders "
        "the value under the label, 'label_source' makes the label itself "
        "live, and 'meter' adds a level bar along the key's bottom edge — "
        "e.g. a mic-level key on a Stream Deck XL dashboard."
    )

    EXTENSIONS = {
        "status_cards": [
            {
                "id": "deck_status",
                "label": "Stream Deck",
                "icon": "gamepad",
                "metrics": [
                    {
                        "key": "plugin.streamdeck.connected",
                        "label": "Connected",
                        "format": "boolean",
                    },
                    {
                        "key": "plugin.streamdeck.model",
                        "label": "Model",
                        "format": "string",
                    },
                    {
                        "key": "plugin.streamdeck.serial",
                        "label": "Serial",
                        "format": "string",
                    },
                    {
                        "key": "plugin.streamdeck.current_page",
                        "label": "Page",
                        "format": "number",
                    },
                ],
            },
        ],
        "context_actions": [
            {
                "id": "identify_deck",
                "label": "Identify Stream Deck",
                "icon": "eye",
                "context": "plugin",
                "event": "action.identify",
            },
        ],
        "views": [
            {
                "id": "surface",
                "label": "Stream Deck",
                "icon": "gamepad",
                "renderer": "surface",
            },
        ],
    }

    def __init__(self):
        self.api = None
        # device_id -> _DeckSession, in connect order (first = primary deck)
        self._sessions = {}
        self._loop = None
        self._opening = False    # re-entrancy guard while decks are being opened
        # Live mirror: (serial, item) -> (png bytes, media type). Virtual decks
        # always mirror; physical decks mirror while the Live View is open.
        self._mirror_blobs = {}
        self._mirror_physical = False
        self._icon_font = None   # Loaded Lucide TTF font for icon rendering
        self._icon_map = {}      # icon-name -> unicode code point
        self._icon_cache = {}    # (icon_name, size, color_hex) -> PIL Image
        self._text_font_path = None  # Bundled label font (legible on Linux/Pi)
        self._font_cache = {}    # size -> ImageFont
        self._label_cache = {}   # (label, w, h, color, max_font, max_lines) -> RGBA
        # "host:port" -> _DockLink for each configured network deck
        self._dock_links = {}

    async def start(self, api):
        """Initialize and connect to the Stream Deck."""
        self.api = api
        self._loop = asyncio.get_event_loop()

        # Lazy-import after DLL paths are set up by plugin loader
        try:
            _lazy_import()
        except ImportError as e:
            if "hidapi" in str(e).lower() or "hid" in str(e).lower():
                raise RuntimeError(self._hidapi_error_message()) from e
            raise RuntimeError(
                "Failed to import Stream Deck library. "
                "Make sure the 'streamdeck' and 'pillow' packages are installed."
            ) from e

        # Load Lucide icon font for button icon rendering
        self._load_icon_font()
        # Load the bundled text font for legible labels on every platform
        self._load_text_font()

        # Set initial state (geometry keys are filled in by _open_deck once a
        # deck is detected; the Surface Configurator falls back to the static
        # SURFACE_LAYOUT while connected is false)
        await self.api.state_set("connected", False)
        await self.api.state_set("model", "")
        await self.api.state_set("serial", "")
        await self.api.state_set("current_page", 0)
        await self.api.state_set("key_count", 0)
        await self.api.state_set("rows", 0)
        await self.api.state_set("columns", 0)
        await self.api.state_set("dial_count", 0)
        await self.api.state_set("touch_key_count", 0)
        await self.api.state_set("has_touchscreen", False)
        await self.api.state_set("has_info_screen", False)
        await self.api.state_set("deck_count", 0)
        await self.api.state_set("deck_serials", "")

        # Validate HIDAPI is loadable now, so a missing native library fails
        # the plugin with a clear message instead of silently looping in the
        # watchdog below.
        try:
            StreamDeck.DeviceManager().enumerate()
        except Exception as e:
            if "hidapi" in str(e).lower() or "hid" in str(e).lower():
                raise RuntimeError(self._hidapi_error_message()) from e
            raise

        # Subscribe to context actions (independent of any connected deck, and
        # kept across reconnects).
        await self.api.event_subscribe(
            "plugin.streamdeck.action.*", self._on_context_action
        )

        # HTTP routes serving the live-mirror images (rendered key/strip
        # images for the IDE's Live View and for virtual decks).
        self.api.register_router(self._build_ext_router())

        # Macro lifecycle events drive run/done/error marks on any key whose
        # bindings reference the macro — no matter who started it.
        await self.api.event_subscribe("macro.*", self._on_macro_event)

        # A single watchdog opens every deck present now, recovers decks after
        # a mid-session unplug, and connects decks that appear later. Its
        # first iteration runs almost immediately.
        self.api.create_periodic_task(
            self._watchdog, interval_seconds=3, name="deck_watchdog"
        )

    async def stop(self):
        """Release every connected Stream Deck."""
        for session in list(self._sessions.values()):
            if session.mirror_bump_handle is not None:
                session.mirror_bump_handle.cancel()
                session.mirror_bump_handle = None
            if session.overlay_handle is not None:
                session.overlay_handle.cancel()
                session.overlay_handle = None
            if session.strip_render_task is not None:
                session.strip_render_task.cancel()
                session.strip_render_task = None
            for handle in session.flash_zones.values():
                handle.cancel()
            session.flash_zones.clear()
            for handle in session.macro_clear_handles.values():
                handle.cancel()
            session.macro_clear_handles.clear()
            try:
                session.deck.reset()
                session.deck.close()
            except Exception as e:
                self.api.log(f"Error closing deck: {e}", level="warning")
        self._sessions.clear()
        self._mirror_blobs.clear()
        for link in self._dock_links.values():
            link.close()
        self._dock_links.clear()
        self.api.log("Stream Deck plugin stopped")

    async def health_check(self):
        """Report deck connection health."""
        connected = [
            s for s in self._sessions.values()
            if s.deck and s.deck.is_open()
        ]
        if connected:
            models = ", ".join(f"{s.model} ({s.serial})" for s in connected)
            return {"status": "ok", "message": f"Connected: {models}"}
        return {"status": "degraded", "message": "No Stream Deck connected"}

    async def on_config_changed(self, new_config):
        """Hot-apply a config change without restarting (no USB churn).

        The platform has already swapped ``self.api.config`` when this runs.
        Each session rebuilds its subscriptions against the new config view,
        clamps/re-evaluates its page, re-applies brightness, and re-renders —
        physical deck handles are never closed, so the deck doesn't blank.
        Virtual deck additions/removals reconcile on the next watchdog tick
        (within seconds). Returning True tells the platform to skip the
        stop/start restart.
        """
        for session in list(self._sessions.values()):
            # The config under any in-flight press just changed — end hold
            # repeats now rather than trusting the (possibly re-bound)
            # release to find them. A still-held key simply re-presses.
            for task_id in list(session.hold_tasks.values()):
                self.api.cancel_task(task_id)
            session.hold_tasks.clear()
            for sub_id in session.feedback_subs:
                try:
                    await self.api.state_unsubscribe(sub_id)
                except Exception:
                    pass
            session.feedback_subs = []
            await self._setup_feedback_subscriptions(session)

            max_pages = self._effective_page_count(session)
            if session.current_page >= max_pages:
                await self._change_page(session, max_pages - 1)
            target = await self._evaluate_auto_page(session)
            if target is not None and target != session.current_page:
                await self._change_page(session, target)

            await self._apply_active_brightness(session)
            await self._render_all_buttons(session)
            await self._render_touchscreen(session)
            await self._render_info_strip(session)

        await self._publish_deck_state()
        return True

    # ──── Device Management ────

    def _primary_session(self):
        """The earliest-connected deck still attached (drives singleton keys)."""
        return next(iter(self._sessions.values()), None)

    def _deck_config(self, session):
        """Per-deck config view.

        A ``decks[serial]`` override fully replaces the per-deck sections
        (buttons, global_buttons, auto_page, dials, touchscreen, info_strip,
        auto_brightness, idle_dim, page_names) for that deck. Decks without
        an override mirror the flat top-level config — so a single deck never
        deals with serials, and a second deck shows the same controls until
        it's given its own layout. Unit properties (``deck_settings``,
        ``deck_names``) live outside this view on purpose: setting them never
        forks a layout.
        """
        decks = self.api.config.get("decks")
        if isinstance(decks, dict):
            override = decks.get(session.serial)
            if isinstance(override, dict):
                return override
        return self.api.config

    def _deck_setting(self, session, key, default):
        """A scalar setting from the deck's config view, falling back to the
        flat config (so an override can omit e.g. colors and inherit them)."""
        cfg = self._deck_config(session)
        if isinstance(cfg, dict) and key in cfg:
            return cfg[key]
        return self.api.config.get(key, default)

    async def _publish_deck_state(self):
        """Publish per-deck state keys and the primary-deck singleton keys.

        Every deck gets ``plugin.streamdeck.<serial>.*`` keys; the un-prefixed
        singleton keys mirror the primary deck so the status card and simple
        automations keep working unchanged with one deck.
        """
        sessions = list(self._sessions.values())
        await self.api.state_set("deck_count", len(sessions))
        await self.api.state_set("deck_serials", ",".join(s.serial for s in sessions))

        primary = self._primary_session()
        if primary is None:
            await self.api.state_set("connected", False)
        else:
            await self.api.state_set("connected", True)
            await self.api.state_set("model", primary.model)
            await self.api.state_set("serial", primary.serial)
            for key, value in primary.geometry.items():
                await self.api.state_set(key, value)
            await self.api.state_set("current_page", primary.current_page)

        deck_names = self.api.config.get("deck_names")
        deck_names = deck_names if isinstance(deck_names, dict) else {}
        for session in sessions:
            prefix = session.serial
            await self.api.state_set(f"{prefix}.connected", True)
            await self.api.state_set(f"{prefix}.model", session.model)
            await self.api.state_set(
                f"{prefix}.name", str(deck_names.get(session.serial, ""))
            )
            await self.api.state_set(
                f"{prefix}.address", getattr(session, "address", "")
            )
            for key, value in session.geometry.items():
                await self.api.state_set(f"{prefix}.{key}", value)
            await self.api.state_set(f"{prefix}.current_page", session.current_page)

    async def _open_deck(self, deck):
        """Open a deck, configure it, and set up its callbacks."""
        deck.open()
        deck.reset()

        session = _DeckSession(deck)
        session.is_virtual = isinstance(deck, _VirtualDeck)
        net_device = getattr(deck, "device", None)
        session.is_network = bool(getattr(net_device, "is_network", False))
        session.address = net_device.address if session.is_network else ""
        session.transport = (
            "virtual" if session.is_virtual
            else "network" if session.is_network
            else "usb"
        )
        try:
            session.device_id = deck.id()
        except Exception:
            session.device_id = f"deck-{id(deck)}"
        session.serial = deck.get_serial_number() or "unknown"
        session.model = deck.deck_type()
        if session.is_network and getattr(net_device, "link", None) is not None:
            if session.serial and session.serial != "unknown":
                # Recorded on the link; the watchdog persists it to the
                # config entry so discovery can follow the deck if its
                # address changes later (DHCP renumbering).
                net_device.link.observed_serial = session.serial

        # Geometry comes from the live hardware, not a static model table, so
        # any deck the library enumerates renders correctly — including models
        # added after this plugin was written.
        rows, columns = deck.key_layout()
        try:
            # A secondary info screen reports a non-zero size (e.g. Neo 248x58)
            has_info_screen = deck.screen_image_format()["size"][0] > 0
        except Exception:
            has_info_screen = False
        try:
            is_visual = bool(deck.is_visual())
        except Exception:
            is_visual = True
        session.geometry = {
            "key_count": deck.key_count(),
            "rows": rows,
            "columns": columns,
            "dial_count": deck.dial_count(),
            "touch_key_count": deck.touch_key_count(),
            "has_touchscreen": deck.is_touch(),
            "has_info_screen": has_info_screen,
            # False for display-less decks (foot pedals): keys fire actions
            # but nothing renders, so the editor skips display-only flows.
            "visual": is_visual,
            "virtual": session.is_virtual,
            "transport": session.transport,
        }

        self._sessions[session.device_id] = session

        # Apply brightness (base config level or the first matching
        # auto_brightness rule), and start the idle timer fresh.
        session.last_input = asyncio.get_event_loop().time()
        deck.set_brightness(await self._current_brightness_level(session))

        # Wire callbacks, each bound to this deck's session (async variants —
        # they fire on our event loop)
        deck.set_key_callback_async(
            functools.partial(self._on_key_change, session), loop=self._loop
        )
        if session.geometry["dial_count"] > 0:
            deck.set_dial_callback_async(
                functools.partial(self._on_dial_event, session), loop=self._loop
            )
        if session.geometry["has_touchscreen"]:
            deck.set_touchscreen_callback_async(
                functools.partial(self._on_touchscreen_event, session),
                loop=self._loop,
            )

        # Publish the detected hardware to state. The Surface Configurator
        # prefers these keys over the static SURFACE_LAYOUT while connected,
        # so the editor always draws the surface that's actually plugged in.
        await self._publish_deck_state()

        geometry = session.geometry
        touch_keys = geometry["touch_key_count"]
        extras = ", touchscreen" if geometry["has_touchscreen"] else ""
        if touch_keys:
            extras += f", {touch_keys} touch keys"
        self.api.log(
            f"Connected to {session.model} (S/N: {session.serial}, "
            f"{geometry['key_count']} keys, {rows}x{columns}, "
            f"{geometry['dial_count']} dials{extras})"
        )
        await self.api.event_emit(
            "connected", {"model": session.model, "serial": session.serial}
        )

        # Subscribe to state changes for feedback, visibility, and auto-page keys
        await self._setup_feedback_subscriptions(session)

        # Apply the initial auto-page selection before the first render so we
        # don't briefly show page 0 and then immediately switch.
        initial_page = await self._evaluate_auto_page(session)
        if initial_page is not None:
            session.current_page = initial_page
            await self.api.state_set(f"{session.serial}.current_page", initial_page)
            if self._primary_session() is session:
                await self.api.state_set("current_page", initial_page)

        # Render all buttons for the current page, then the secondary displays
        await self._render_all_buttons(session)
        await self._render_touchscreen(session)
        await self._render_info_strip(session)

    async def _watchdog(self):
        """Keep every deck connected: open new ones, recover after unplug.

        Runs on a single periodic task for the plugin's whole lifetime. A deck
        unplugged mid-session is detected by its dead session and torn down
        (the bundled library closes the deck object on a transport error but
        never re-opens it); newly attached decks are recognized by their HID
        path and opened. Periodic ticks never overlap, but an ``_opening``
        guard is kept so a slow open can't be re-entered.
        """
        if self._opening:
            return

        declared_virtual = self._virtual_deck_entries()
        declared_serials = {entry["serial"] for entry in declared_virtual}

        # Health-check the decks we hold; tear down any that went away (or
        # virtual decks no longer declared in config). The healthy ticks
        # double as each deck's idle-dim clock.
        for session in list(self._sessions.values()):
            try:
                healthy = session.deck.is_open() and session.deck.connected()
            except Exception:
                healthy = False
            if session.is_virtual and session.serial not in declared_serials:
                healthy = False
            if healthy:
                await self._check_idle_dim(session)
                await self._tick_clock(session)
            else:
                await self._handle_deck_lost(session)

        # Open any attached decks we don't hold yet (matched by HID path).
        try:
            decks = StreamDeck.DeviceManager().enumerate()
        except Exception:
            decks = []
        new_decks = []
        for deck in decks:
            try:
                device_id = deck.id()
            except Exception:
                device_id = None
            if device_id is None or device_id not in self._sessions:
                new_decks.append(deck)

        # Materialize declared virtual decks that aren't running yet.
        for entry in declared_virtual:
            if f"virtual:{entry['serial']}" not in self._sessions:
                new_decks.append(
                    _VirtualDeck(
                        entry["model"], entry["serial"], _VIRTUAL_MODELS[entry["model"]]
                    )
                )

        # Network decks: reconnect configured units (with backoff) and collect
        # any decks whose connection came up since the last tick.
        try:
            await self._reconcile_network_decks(new_decks)
        except Exception as e:
            self.api.log(f"Network deck reconcile error: {e}", level="warning")

        if not new_decks:
            return

        self._opening = True
        try:
            for deck in new_decks:
                try:
                    await self._open_deck(deck)
                except Exception as e:
                    self.api.log(f"Failed to open Stream Deck: {e}", level="warning")
        finally:
            self._opening = False

    async def _handle_deck_lost(self, session):
        """Tear down one deck that has gone away (others keep running).

        Cancels in-flight hold-repeat tasks, drops the deck's feedback
        subscriptions (re-created on re-open), closes the stale deck object,
        and publishes the disconnect. The context-action subscription is left
        intact — it lives for the plugin's whole lifetime, not per-deck.
        """
        self.api.log(
            f"Stream Deck disconnected ({session.model} S/N: {session.serial})",
            level="warning",
        )

        for task_id in list(session.hold_tasks.values()):
            self.api.cancel_task(task_id)
        session.hold_tasks.clear()
        session.press_times.clear()
        session.dial_press_times.clear()
        session.dial_turned.clear()

        for sub_id in session.feedback_subs:
            try:
                await self.api.state_unsubscribe(sub_id)
            except Exception:
                pass
        session.feedback_subs = []

        self._sessions.pop(session.device_id, None)
        try:
            session.deck.close()
        except Exception:
            pass

        await self.api.state_set(f"{session.serial}.connected", False)
        if session.mirror_bump_handle is not None:
            session.mirror_bump_handle.cancel()
            session.mirror_bump_handle = None
        if session.overlay_handle is not None:
            session.overlay_handle.cancel()
            session.overlay_handle = None
        if session.strip_render_task is not None:
            session.strip_render_task.cancel()
            session.strip_render_task = None
        session.strip_image = None
        session.strip_dirty = None
        for handle in session.flash_zones.values():
            handle.cancel()
        session.flash_zones.clear()
        session.overlay_active = False
        for handle in session.macro_clear_handles.values():
            handle.cancel()
        session.macro_clear_handles.clear()
        self._drop_mirror_blobs(session.serial)
        await self._publish_deck_state()
        await self.api.event_emit("disconnected", {"serial": session.serial})

    # ──── Virtual decks + live mirror ────

    def _virtual_deck_entries(self):
        """Validated ``virtual_decks`` config entries.

        Each entry needs a ``model`` from the preset table and a ``serial``;
        serials are sanitized to state-key-safe characters and deduplicated.
        """
        raw = self.api.config.get("virtual_decks", [])
        if not isinstance(raw, list):
            return []
        entries = []
        seen = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            model = item.get("model")
            serial = re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("serial", "")))
            if model not in _VIRTUAL_MODELS or not serial or serial in seen:
                continue
            seen.add(serial)
            entries.append({"model": model, "serial": serial})
        return entries

    # ──── Network decks ────

    def _network_deck_entries(self):
        """Validated ``network_decks`` config entries.

        Each entry needs a ``host`` (IP or hostname); ``port`` defaults to
        5343. ``serial`` is recorded automatically after the first connect so
        a deck that moves to a new DHCP address can be found again via
        discovery. Entries are explicit opt-in — the plugin never attaches to
        a deck on the network that hasn't been added here.
        """
        raw = self.api.config.get("network_decks", [])
        if not isinstance(raw, list):
            return []
        entries = []
        seen = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            host = str(item.get("host", "")).strip()
            try:
                port = int(item.get("port", _NET_DEFAULT_PORT))
            except (TypeError, ValueError):
                continue
            if not host or not (0 < port < 65536):
                continue
            key = f"{host}:{port}"
            if key in seen:
                continue
            seen.add(key)
            entries.append({
                "host": host,
                "port": port,
                "key": key,
                # The deck's own serial (recorded after the first connect)
                "serial": str(item.get("serial", "") or ""),
                # The serial the unit advertises via discovery (recorded at
                # add time) — for a dock this is the dock's serial, not the
                # docked deck's, so re-resolve accepts either.
                "mdns_sn": str(item.get("mdns_sn", "") or ""),
            })
        return entries

    async def _reconcile_network_decks(self, new_decks):
        """Watchdog half of network decks.

        Keeps one _DockLink per configured entry, publishes each link's
        status for the editor (``plugin.streamdeck.net.<key>.status``),
        appends deck objects for transports that came up, and self-heals the
        configured host via discovery when a known deck stops answering.
        """
        entries = self._network_deck_entries()
        declared = {e["key"]: e for e in entries}

        for key, link in list(self._dock_links.items()):
            if key not in declared:
                link.close()
                del self._dock_links[key]
                await self.api.state_set(f"net.{link.state_key}.status", "removed")

        serial_updates = False
        for key, entry in declared.items():
            link = self._dock_links.get(key)
            if link is None:
                link = _DockLink(entry["host"], entry["port"])
                if entry["serial"]:
                    link.observed_serial = entry["serial"]
                self._dock_links[key] = link
            transports = await link.ensure(self._loop, self.api.log)
            for transport in transports:
                if transport.path() not in self._sessions:
                    cls = _net_deck_class(transport.product_id())
                    if cls is not None:
                        new_decks.append(cls(transport))
            await self.api.state_set(f"net.{link.state_key}.address", key)
            await self.api.state_set(f"net.{link.state_key}.status", link.status)
            if link.observed_serial and link.observed_serial != entry["serial"]:
                serial_updates = True
            known_serials = {
                s
                for s in (link.observed_serial, entry["serial"], entry["mdns_sn"])
                if s
            }
            if (
                known_serials
                and link.fail_count >= 3
                and not link._resolve_attempted
                and callable(getattr(self.api, "mdns_browse", None))
            ):
                link._resolve_attempted = True
                self.api.create_task(
                    self._net_reresolve(key, known_serials),
                    name=f"net_reresolve_{link.state_key}",
                )
        if serial_updates:
            await self._persist_network_serials()

    async def _persist_network_serials(self):
        """Record each connected deck's serial on its ``network_decks`` entry
        (so discovery can follow the deck if its address changes later)."""
        raw = self.api.config.get("network_decks")
        if not isinstance(raw, list):
            return
        updated = []
        changed = False
        for item in raw:
            entry = dict(item) if isinstance(item, dict) else item
            if isinstance(entry, dict):
                host = str(entry.get("host", "")).strip()
                try:
                    port = int(entry.get("port", _NET_DEFAULT_PORT))
                except (TypeError, ValueError):
                    port = _NET_DEFAULT_PORT
                link = self._dock_links.get(f"{host}:{port}")
                if (
                    link
                    and link.observed_serial
                    and entry.get("serial") != link.observed_serial
                ):
                    entry["serial"] = link.observed_serial
                    changed = True
            updated.append(entry)
        if changed:
            config = dict(self.api.config)
            config["network_decks"] = updated
            await self.api.save_config(config)

    async def _net_reresolve(self, key, serials):
        """A configured deck stopped answering: browse discovery for any of
        its known serials (deck or advertised unit) and follow it to its
        new address."""
        try:
            results = await self.api.mdns_browse([_NET_MDNS_SERVICE], duration=5.0)
        except Exception as e:
            self.api.log(f"Network deck discovery failed: {e}", level="warning")
            return
        for r in results:
            txt = r.get("txt") or {}
            if str(txt.get("sn", "")) not in serials:
                continue
            new_host = r.get("ip")
            new_port = r.get("port") or _NET_DEFAULT_PORT
            if not new_host or f"{new_host}:{new_port}" == key:
                return
            raw = self.api.config.get("network_decks")
            if not isinstance(raw, list):
                return
            updated = []
            changed = False
            for item in raw:
                entry = dict(item) if isinstance(item, dict) else item
                if isinstance(entry, dict):
                    e_host = str(entry.get("host", "")).strip()
                    try:
                        e_port = int(entry.get("port", _NET_DEFAULT_PORT))
                    except (TypeError, ValueError):
                        e_port = _NET_DEFAULT_PORT
                    if f"{e_host}:{e_port}" == key:
                        entry["host"] = new_host
                        entry["port"] = new_port
                        changed = True
                updated.append(entry)
            if changed:
                self.api.log(
                    f"Network deck {str(txt.get('sn', ''))} answered at "
                    f"{new_host}:{new_port} (was {key}); following it"
                )
                config = dict(self.api.config)
                config["network_decks"] = updated
                await self.api.save_config(config)
            return

    def _build_ext_router(self):
        """FastAPI router serving the live-mirror images.

        ``GET /api/plugins/streamdeck/ext/live/{serial}/{item}`` returns the
        most recent rendered image for a key (``key_<n>``), the touchscreen
        strip (``touchscreen``), or the info screen (``screen``).
        """
        from fastapi import APIRouter
        from fastapi.responses import Response

        router = APIRouter()

        @router.get("/live/{serial}/{item}")
        async def live_image(serial: str, item: str):
            blob = self._mirror_blobs.get((serial, item))
            if blob is None:
                return Response(status_code=404)
            data, media_type = blob
            return Response(
                content=data,
                media_type=media_type,
                headers={"Cache-Control": "no-store"},
            )

        @router.post("/network/scan")
        async def network_scan():
            """Find network decks on the LAN. Returns browse_available=False
            where multicast discovery can't run (older cores, Docker bridge
            networks) — the manual add-by-address path always works."""
            entries = self._network_deck_entries()
            configured_keys = {e["key"] for e in entries}
            configured_hosts = {e["host"] for e in entries}
            browse = getattr(self.api, "mdns_browse", None)
            if not callable(browse):
                return {"browse_available": False, "found": []}
            try:
                results = await browse([_NET_MDNS_SERVICE], duration=4.0)
            except Exception as e:
                self.api.log(f"Network deck scan failed: {e}", level="warning")
                return {"browse_available": False, "found": []}
            found = []
            for r in results:
                service = str(r.get("service_type") or "")
                if not service.startswith("_elg."):
                    continue
                host = r.get("ip") or ""
                if not host:
                    continue
                port = r.get("port") or _NET_DEFAULT_PORT
                txt = r.get("txt") or {}
                found.append({
                    "host": host,
                    "port": port,
                    "name": r.get("instance_name") or r.get("hostname") or host,
                    "serial": str(txt.get("sn", "")),
                    "kind": (
                        "Network Dock"
                        if str(txt.get("dt", "")) == "215"
                        else "Stream Deck"
                    ),
                    "already_added": (
                        f"{host}:{port}" in configured_keys
                        or host in configured_hosts
                    ),
                })
            return {"browse_available": True, "found": found}

        @router.post("/network/test")
        async def network_test(payload: dict):
            """Try a TCP connection to host:port (the manual-add Test button)."""
            host = str(payload.get("host", "")).strip()
            try:
                port = int(payload.get("port", _NET_DEFAULT_PORT))
            except (TypeError, ValueError):
                return {"ok": False, "error": "invalid port"}
            if not host or not (0 < port < 65536):
                return {"ok": False, "error": "enter a host or IP address"}
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=3.0
                )
            except Exception as e:
                return {"ok": False, "error": _classify_net_error(e)}
            writer.close()
            return {"ok": True}

        return router

    def _mirror_enabled(self, session):
        """Virtual decks always mirror; physical decks only while a Live View
        has turned mirroring on (set_live_mirror context action)."""
        return session.is_virtual or self._mirror_physical

    def _mirror(self, session, item, image):
        """Store a rendered PIL image for the Live View and schedule a bump.

        Captures the pre-native image (the native bytes are model-flipped /
        rotated). Failures never break rendering.
        """
        if not self._mirror_enabled(session) or Image is None:
            return
        try:
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
        except Exception:
            return
        # Soft cap so a pathological config can't grow memory unbounded; a
        # full XL with strips is ~35 entries per deck.
        if len(self._mirror_blobs) > 512:
            self._mirror_blobs.pop(next(iter(self._mirror_blobs)), None)
        self._mirror_blobs[(session.serial, item)] = (buffer.getvalue(), "image/png")
        self._schedule_mirror_bump(session)

    def _drop_mirror_blobs(self, serial):
        for key in [k for k in self._mirror_blobs if k[0] == serial]:
            self._mirror_blobs.pop(key, None)

    def _schedule_mirror_bump(self, session):
        """Debounce render_version so a full-page render bumps state once."""
        if session.mirror_bump_handle is not None:
            session.mirror_bump_handle.cancel()
        loop = asyncio.get_event_loop()
        session.mirror_bump_handle = loop.call_later(
            0.05, lambda: asyncio.ensure_future(self._publish_mirror_version(session))
        )

    async def _publish_mirror_version(self, session):
        session.mirror_bump_handle = None
        if session.device_id not in self._sessions:
            return
        session.render_version += 1
        await self.api.state_set(
            f"{session.serial}.render_version", session.render_version
        )
        for index, color in list(session.touch_key_colors.items()):
            await self.api.state_set(f"{session.serial}.touch_key.{index}", color)

    async def _simulate_input(self, payload):
        """Dispatch a simulated key/dial/touch input into a session.

        Used by the IDE workbench (clicking a virtual or physical deck's
        image). Runs the exact same handler paths as hardware input.
        Payload: ``{serial, type: key|dial_turn|dial_push|touch, index?,
        pressed?, amount?, x?, y?, touch_type?: short|long|drag, x_out?}``.
        A ``key`` or ``dial_push`` without ``pressed`` is a full tap
        (press + release); ``touch`` defaults to a short tap and a drag
        needs ``x_out``.
        """
        serial = str(payload.get("serial", ""))
        session = next(
            (s for s in self._sessions.values() if s.serial == serial), None
        )
        if session is None:
            self.api.log(
                f"simulate_input: no connected deck with serial '{serial}'",
                level="warning",
            )
            return

        kind = payload.get("type")
        if kind == "key":
            try:
                index = int(payload.get("index"))
            except (TypeError, ValueError):
                return
            total = session.deck.key_count() + session.deck.touch_key_count()
            if not 0 <= index < total:
                return
            pressed = payload.get("pressed")
            if pressed is None:
                # Full tap: press then release, like a quick physical press.
                await self._on_key_change(session, session.deck, index, True)
                await self._on_key_change(session, session.deck, index, False)
            else:
                await self._on_key_change(session, session.deck, index, bool(pressed))

        elif kind == "dial_turn":
            try:
                index = int(payload.get("index"))
                amount = int(payload.get("amount", 1))
            except (TypeError, ValueError):
                return
            await self._on_dial_event(
                session, session.deck, index, SimpleNamespace(name="TURN"), amount
            )

        elif kind == "dial_push":
            try:
                index = int(payload.get("index"))
            except (TypeError, ValueError):
                return
            pressed = payload.get("pressed")
            if pressed is None:
                # Full tap: push then release, like a quick physical press.
                await self._on_dial_event(
                    session, session.deck, index, SimpleNamespace(name="PUSH"), True
                )
                await self._on_dial_event(
                    session, session.deck, index, SimpleNamespace(name="PUSH"), False
                )
            else:
                await self._on_dial_event(
                    session, session.deck, index,
                    SimpleNamespace(name="PUSH"), bool(pressed),
                )

        elif kind == "touch":
            try:
                x = int(payload.get("x"))
            except (TypeError, ValueError):
                return
            try:
                y = int(payload.get("y", 50))
            except (TypeError, ValueError):
                y = 50
            touch_type = str(payload.get("touch_type", "short")).lower()
            if touch_type == "drag":
                try:
                    x_out = int(payload.get("x_out"))
                except (TypeError, ValueError):
                    return
                await self._on_touchscreen_event(
                    session, session.deck, SimpleNamespace(name="DRAG"),
                    {"x": x, "y": y, "x_out": x_out, "y_out": y},
                )
            else:
                name = "LONG" if touch_type == "long" else "SHORT"
                await self._on_touchscreen_event(
                    session, session.deck, SimpleNamespace(name=name),
                    {"x": x, "y": y},
                )

    async def _set_live_mirror(self, payload):
        """Toggle mirroring of physical decks (virtual decks always mirror)."""
        turn_on = bool(payload.get("on"))
        if turn_on == self._mirror_physical:
            return
        self._mirror_physical = turn_on
        if turn_on:
            # Populate the mirror with the current rendering of every deck.
            for session in list(self._sessions.values()):
                await self._render_all_buttons(session)
                await self._render_touchscreen(session)
                await self._render_info_strip(session)
        else:
            for session in list(self._sessions.values()):
                if not session.is_virtual:
                    self._drop_mirror_blobs(session.serial)

    # ──── Automation actions (driven by macro event.emit steps) ────

    def _sessions_for(self, serial):
        """Sessions an automation action targets: one serial, or every deck."""
        sessions = list(self._sessions.values())
        if serial:
            return [s for s in sessions if s.serial == serial]
        return sessions

    async def _flash_key(self, session, index, times):
        """Flash one key white to draw attention to it."""
        deck = session.deck
        if not deck or not deck.is_visual() or session.overlay_active:
            return
        if not 0 <= index < deck.key_count() + deck.touch_key_count():
            return
        is_touch = self._is_touch_key(session, index)
        white = None
        if not is_touch and Image is not None:
            white = self._create_button_image(session, "", "#ffffff", "#ffffff")
        for _ in range(times):
            if is_touch:
                self._apply_key_color(session, index, "#ffffff")
            elif white is not None:
                self._apply_key_image(session, index, white)
            await asyncio.sleep(0.18)
            await self._render_button(session, index)
            await asyncio.sleep(0.12)

    async def _show_message(self, session, text, seconds):
        """Splash a message across the whole deck, restoring it afterwards.

        While the overlay is up, normal renders are suppressed and the first
        key/dial/touch input dismisses it without firing any action.
        """
        if session.overlay_handle is not None:
            session.overlay_handle.cancel()
            session.overlay_handle = None
        session.overlay_active = True
        await self._draw_overlay(session, text)
        loop = asyncio.get_event_loop()
        session.overlay_handle = loop.call_later(
            seconds, lambda: asyncio.ensure_future(self._clear_overlay(session))
        )

    async def _clear_overlay(self, session):
        """Drop a show_message overlay and restore the normal rendering."""
        if session.overlay_handle is not None:
            session.overlay_handle.cancel()
            session.overlay_handle = None
        if not session.overlay_active:
            return
        session.overlay_active = False
        await self._render_all_buttons(session)
        await self._render_touchscreen(session)
        await self._render_info_strip(session)

    async def _draw_overlay(self, session, text):
        """Render ``text`` wrapped across the key grid (and strips) as one
        canvas split into key tiles."""
        deck = session.deck
        if not deck or not deck.is_visual() or Image is None:
            return
        key_format = deck.key_image_format()
        key_w, key_h = key_format["size"]
        if not key_w or not key_h:
            return
        rows, cols = deck.key_layout()

        canvas = Image.new("RGB", (cols * key_w, rows * key_h), "#101018")
        self._paste_label(
            canvas, text, "#ffffff",
            (key_w // 4, key_h // 4, cols * key_w - key_w // 2, rows * key_h - key_h // 2),
            max_font=key_h, max_lines=max(2, rows),
        )
        for index in range(deck.key_count()):
            row, col = divmod(index, cols)
            tile = canvas.crop(
                (col * key_w, row * key_h, (col + 1) * key_w, (row + 1) * key_h)
            )
            self._apply_key_image(session, index, tile)

        # Touch keys glow white while the message is up.
        for index in range(
            deck.key_count(), deck.key_count() + deck.touch_key_count()
        ):
            self._apply_key_color(session, index, "#ffffff")

        # Strips carry the text too.
        if deck.is_touch():
            try:
                strip_w, strip_h = deck.touchscreen_image_format()["size"]
            except Exception:
                strip_w = strip_h = 0
            if strip_w and strip_h:
                strip = Image.new("RGB", (strip_w, strip_h), "#101018")
                self._paste_label(
                    strip, text, "#ffffff", (8, 4, strip_w - 16, strip_h - 8),
                    max_font=max(16, strip_h // 2), max_lines=2,
                )
                self._mirror(session, "touchscreen", strip)
                try:
                    native = PILHelper.to_native_touchscreen_format(deck, strip)
                    with deck:
                        deck.set_touchscreen_image(native, 0, 0, strip_w, strip_h)
                except Exception:
                    pass
        try:
            screen_w, screen_h = deck.screen_image_format()["size"]
        except Exception:
            screen_w = screen_h = 0
        if screen_w and screen_h:
            screen = Image.new("RGB", (screen_w, screen_h), "#101018")
            self._paste_label(
                screen, text, "#ffffff", (4, 2, screen_w - 8, screen_h - 4),
                max_font=max(14, screen_h // 2), max_lines=2,
            )
            self._mirror(session, "screen", screen)
            try:
                native = PILHelper.to_native_screen_format(deck, screen)
                with deck:
                    deck.set_screen_image(native)
            except Exception:
                pass

    # ──── Key Handling ────

    async def _on_key_change(self, session, deck, key_index, pressed):
        """Handle a physical button press/release with mode support."""
        await self._note_input(session)

        # A release ALWAYS ends the press — no matter what changed between
        # press and release (page flipped, buttons edited or deleted,
        # overlay appeared, visibility changed). Skipping this is how keys
        # get stuck visually pressed and how a hold-repeat task leaks and
        # fires its action forever. Runs before every early return below.
        if not pressed:
            task_id = session.hold_tasks.pop(key_index, None)
            if task_id:
                self.api.cancel_task(task_id)
            was_pressed = key_index in session.pressed_keys
            session.pressed_keys.discard(key_index)
            if was_pressed and session.deck and session.deck.is_visual():
                try:
                    await self._render_button(session, key_index)
                except Exception:
                    pass

        # A show_message overlay is dismissed by the first press, which is
        # swallowed (it must not fire the key's own action). The matching
        # release is swallowed too (cleanup above already ran).
        if session.overlay_active:
            if pressed:
                await self._clear_overlay(session)
            return

        if pressed:
            await self._publish_input_echo(session, "key", key_index)

        page = session.current_page

        event_type = "press" if pressed else "release"
        await self.api.event_emit(
            f"button.{event_type}",
            {"key": key_index, "page": page, "serial": session.serial},
        )

        assignment = self._get_button_assignment(session, page, key_index)
        if not assignment:
            return

        # A hidden button (visible_when false) is inert: fire no action. Also
        # clear any in-flight hold/tap-hold state so a button that became
        # hidden mid-press can't leak a periodic task or fire a stale action.
        if not await self._is_button_visible(assignment):
            task_id = session.hold_tasks.pop(key_index, None)
            if task_id:
                self.api.cancel_task(task_id)
            session.press_times.pop(key_index, None)
            session.pressed_keys.discard(key_index)  # don't leak a press highlight
            return

        # Momentary press highlight: mark the key as held and redraw. The mark
        # lives in the render path so a feedback/toggle re-render keeps the
        # highlight rather than fighting it; the redraw is a no-op without a
        # visual deck. (Release-side bookkeeping already ran above.)
        if pressed:
            session.pressed_keys.add(key_index)
            if session.deck and session.deck.is_visual():
                await self._render_button(session, key_index)

        # Get press binding. The UI stores press as an array of actions; mode
        # and toggle/hold config live on the first entry, while a default tap
        # button fires every entry in order.
        bindings = assignment.get("bindings", {})
        press_actions = self._press_actions(bindings)
        press = press_actions[0] if press_actions else None

        if not press or not isinstance(press, dict):
            return

        mode = press.get("mode", "tap")

        if mode == "hold_repeat":
            if pressed:
                # Cancel any existing hold task for this key first
                old_task = session.hold_tasks.pop(key_index, None)
                if old_task:
                    self.api.cancel_task(old_task)
                # Store task ID synchronously BEFORE any await to prevent
                # race condition where release fires during _execute_action
                # and can't find the task to cancel.  The periodic task's
                # first iteration fires the action almost immediately.
                interval = press.get("hold_repeat_ms", 200) / 1000.0
                session.hold_tasks[key_index] = self.api.create_periodic_task(
                    lambda: self._execute_action(session, press, f"key {key_index}"),
                    interval_seconds=interval,
                    name=f"hold_repeat_{session.serial}_{key_index}",
                )
            # Release: the unconditional cleanup at the top already
            # cancelled the task.
            return

        if mode == "toggle":
            if not pressed:
                return
            # Toggle: read toggle_key state to determine on/off
            off_action = press.get("off_action")
            toggle_key = press.get("toggle_key", "")
            toggle_value = press.get("toggle_value")
            is_active = False
            if toggle_key:
                value = await self.api.state_get(toggle_key)
                if toggle_value is not None:
                    is_active = str(value).lower() == str(toggle_value).lower()
                else:
                    is_active = bool(value)

            if is_active and off_action and isinstance(off_action, dict):
                await self._execute_action(session, off_action, f"key {key_index}")
            else:
                await self._execute_action(session, press, f"key {key_index}")

            # Update button label if on_label/off_label configured
            on_label = press.get("on_label", "")
            off_label = press.get("off_label", "")
            if on_label or off_label:
                # Re-render after action (state may have changed)
                await asyncio.sleep(0.1)
                await self._render_button(session, key_index)
            return

        if mode == "tap_hold":
            threshold = press.get("hold_threshold_ms", 500) / 1000.0
            hold_action = press.get("hold_action")
            if pressed:
                session.press_times[key_index] = asyncio.get_event_loop().time()
            else:
                if key_index not in session.press_times:
                    # Release with no recorded press (e.g. the press was
                    # suppressed while the button was hidden) — fire nothing.
                    return
                press_time = session.press_times.pop(key_index)
                held = asyncio.get_event_loop().time() - press_time
                if held >= threshold and hold_action and isinstance(hold_action, dict):
                    await self._execute_action(session, hold_action, f"key {key_index}")
                else:
                    await self._execute_action(session, press, f"key {key_index}")
            return

        # Default: tap mode — fire every configured action in order, on press
        if pressed:
            await self._execute_actions(session, press_actions, f"key {key_index}")

    @staticmethod
    def _press_actions(bindings):
        """Return the press binding as a list of action dicts.

        The Surface Configurator stores ``press`` as an array of action
        objects (mode/toggle/hold config lives on the first entry). A single
        dict is wrapped; anything else yields an empty list.
        """
        if not isinstance(bindings, dict):
            return []
        press = bindings.get("press")
        if isinstance(press, list):
            return [a for a in press if isinstance(a, dict)]
        if isinstance(press, dict):
            return [press]
        return []

    async def _execute_actions(self, session, actions, source):
        """Execute a list of action bindings sequentially, in order."""
        for action in actions:
            if isinstance(action, dict):
                await self._execute_action(session, action, source)

    async def _execute_action(self, session, action_binding, source):
        """Execute a single surface action binding.

        Supports the documented surface action set: ``macro``,
        ``device.command``, ``state.set`` (scoped like the panel plugin
        bridge), and ``navigate`` (deck page: next/previous or a page index).
        ``source`` only labels log lines (e.g. ``key 3``, ``dial 0``).
        """
        action = action_binding.get("action", "")

        if action == "navigate":
            page_id = action_binding.get("page", "")
            if page_id == "__next_page__":
                await self._change_page(session, session.current_page + 1)
            elif page_id == "__prev_page__":
                await self._change_page(session, session.current_page - 1)
            else:
                # A specific page index (int or numeric string).
                try:
                    await self._change_page(session, int(page_id))
                except (TypeError, ValueError):
                    pass

        elif action == "macro":
            macro = action_binding.get("macro", "")
            if macro:
                try:
                    await self.api.macro_execute(macro)
                    self.api.log(f"Executed macro '{macro}' from {source}", level="debug")
                except Exception as e:
                    self.api.log(f"Error executing macro '{macro}': {e}", level="error")

        elif action == "device.command":
            device = action_binding.get("device", "")
            command = action_binding.get("command", "")
            params = action_binding.get("params")
            if device and command:
                try:
                    await self.api.device_command(device, command, params if isinstance(params, dict) else None)
                    self.api.log(f"Sent {command} to {device} from {source}", level="debug")
                except Exception as e:
                    self.api.log(f"Error sending command: {e}", level="error")

        elif action == "state.set":
            key = action_binding.get("key", "")
            if key:
                try:
                    await self._apply_state_set(key, action_binding.get("value"))
                except Exception as e:
                    self.api.log(f"Error setting state '{key}': {e}", level="error")

    async def _apply_state_set(self, key, value):
        """Write a state value, mirroring the panel plugin-bridge scope rule.

        A surface button may write only its own ``plugin.<id>.*`` namespace
        (via ``state_set``) or a ``var.*`` user variable (via ``variable_set``).
        Anything else is a confused-deputy write and is dropped with a warning —
        exactly the scope rule the panel plugin bridge enforces in panel.js.
        """
        prefix = f"plugin.{self.api.plugin_id}."
        if key.startswith(prefix):
            await self.api.state_set(key, value)
        elif key.startswith("var."):
            await self.api.variable_set(key[len("var."):], value)
        else:
            self.api.log(
                f"Ignoring state.set to '{key}': a surface button may only write "
                f"its own {prefix}* state or a var.* user variable.",
                level="warning",
            )

    # ──── Dials (decks with rotary encoders) ────

    @staticmethod
    def _action_list(value):
        """Normalize an action binding (single dict or list of dicts) to a list."""
        if isinstance(value, list):
            return [a for a in value if isinstance(a, dict)]
        if isinstance(value, dict):
            return [value]
        return []

    def _get_dial_config(self, session, index):
        """Look up the dial config entry for a dial index."""
        dials = self._deck_config(session).get("dials", [])
        if not isinstance(dials, list):
            return None
        for dial in dials:
            if isinstance(dial, dict) and dial.get("index") == index:
                return dial
        return None

    @classmethod
    def _dial_push_deferred(cls, cfg):
        """True when a dial's push must wait for the release.

        Configuring ``long_press`` or any ``pressed_*`` (push-and-turn)
        field defers the click to the release so the gesture can be told
        apart; a plain ``press``-only dial keeps firing on push-down.
        """
        if not cfg:
            return False
        return bool(
            cls._action_list(cfg.get("long_press"))
            or isinstance(cfg.get("pressed_adjust"), dict)
            or cls._action_list(cfg.get("pressed_cw"))
            or cls._action_list(cfg.get("pressed_ccw"))
        )

    async def _execute_per_detent(self, session, actions, detents, source):
        """Run a turn's action list once per detent moved (capped at 8 per
        event) so a command-per-click mapping tracks fast spins."""
        action_list = self._action_list(actions)
        if not action_list:
            return
        for _ in range(min(abs(int(detents)), 8)):
            await self._execute_actions(session, action_list, source)

    async def _on_dial_event(self, session, deck, dial, event, value):
        """Handle a dial turn or push.

        Push semantics: a ``press``-only dial fires on push-down (snappy).
        With ``long_press`` or push-and-turn (``pressed_adjust`` /
        ``pressed_cw`` / ``pressed_ccw``) configured, the click defers to
        the release — a quick release fires ``press``, a held release fires
        ``long_press``, and a push during which the dial turned fires
        neither (it was a grip, not a click).

        Event kinds are matched by enum name (``TURN``/``PUSH``) so this
        module never needs the StreamDeck enums at import time (they only
        exist after the lazy import, and tests run without the library).
        """
        await self._note_input(session)
        if session.overlay_active:
            await self._clear_overlay(session)
            return  # the input that dismisses an overlay never fires actions
        kind = getattr(event, "name", None)
        if kind == "TURN" or (kind == "PUSH" and value):
            await self._publish_input_echo(session, "dial", dial)
        cfg = self._get_dial_config(session, dial)

        if kind == "PUSH":
            deferred = self._dial_push_deferred(cfg)
            if value:
                await self.api.event_emit(
                    "dial.press", {"dial": dial, "serial": session.serial}
                )
                if not deferred:
                    if cfg:
                        await self._execute_actions(
                            session, self._action_list(cfg.get("press")),
                            f"dial {dial}",
                        )
                    return
                session.dial_press_times[dial] = asyncio.get_running_loop().time()
                session.dial_turned.discard(dial)
                return
            # Release
            press_time = session.dial_press_times.pop(dial, None)
            turned = dial in session.dial_turned
            session.dial_turned.discard(dial)
            if press_time is None or not deferred:
                return
            if turned:
                return  # push-and-turn was a grip, not a click
            held = asyncio.get_running_loop().time() - press_time
            threshold = _coerce_numeric(cfg.get("hold_threshold_ms"))
            threshold = (threshold if threshold and threshold > 0 else 500) / 1000.0
            long_actions = self._action_list(cfg.get("long_press"))
            if held >= threshold and long_actions:
                await self._execute_actions(session, long_actions, f"dial {dial}")
            else:
                await self._execute_actions(
                    session, self._action_list(cfg.get("press")), f"dial {dial}"
                )
            return

        if kind == "TURN":
            try:
                detents = int(value)
            except (TypeError, ValueError):
                return
            if detents == 0:
                return
            await self.api.event_emit(
                "dial.turn",
                {"dial": dial, "amount": detents, "serial": session.serial},
            )
            if not cfg:
                return
            if dial in session.dial_press_times:
                # Turning while held: chord. Suppress the click, and route
                # to the pressed_* config when any is set (fine adjust /
                # alternate function); otherwise the turn acts normally.
                session.dial_turned.add(dial)
                pressed_adjust = cfg.get("pressed_adjust")
                pressed_actions = (
                    cfg.get("pressed_cw") if detents > 0 else cfg.get("pressed_ccw")
                )
                if (
                    isinstance(pressed_adjust, dict) and pressed_adjust.get("key")
                ) or self._action_list(cfg.get("pressed_cw")) \
                        or self._action_list(cfg.get("pressed_ccw")):
                    if isinstance(pressed_adjust, dict) and pressed_adjust.get("key"):
                        await self._apply_dial_adjust(pressed_adjust, detents)
                    await self._execute_per_detent(
                        session, pressed_actions, detents, f"dial {dial}"
                    )
                    return
            adjust = cfg.get("adjust")
            if isinstance(adjust, dict) and adjust.get("key"):
                await self._apply_dial_adjust(adjust, detents)
            actions = cfg.get("cw") if detents > 0 else cfg.get("ccw")
            await self._execute_per_detent(session, actions, detents, f"dial {dial}")

    async def _apply_dial_adjust(self, adjust, detents):
        """Increment a numeric state value by ``step * detents``, clamped.

        The turn magnitude (detents moved since the last event) scales the
        step, so spinning a dial fast moves the value proportionally faster.
        Writes go through the same scope rule as state.set actions (own
        ``plugin.<id>.*`` state or ``var.*`` user variables only).
        """
        key = adjust.get("key", "")
        step = _coerce_numeric(adjust.get("step"))
        if step is None:
            step = 1
        minimum = _coerce_numeric(adjust.get("min"))
        maximum = _coerce_numeric(adjust.get("max"))

        current = _coerce_numeric(await self.api.state_get(key))
        if current is None:
            current = minimum if minimum is not None else 0

        new_value = current + step * detents
        if minimum is not None:
            new_value = max(minimum, new_value)
        if maximum is not None:
            new_value = min(maximum, new_value)
        if float(new_value).is_integer():
            new_value = int(new_value)
        await self._apply_state_set(key, new_value)

    # ──── Touchscreen strip (decks with a touch strip) ────

    def _touch_zones(self, session):
        """Return the effective touchscreen zones with resolved pixel bounds.

        Explicit ``touchscreen.zones`` config wins; zones missing ``x``/``w``
        are laid out by splitting the strip evenly. With no zones configured,
        one zone is created per dial (aligned under it) showing the dial's
        label and its adjust value — so a configured dial gets a live readout
        with zero extra config.
        """
        if not session.deck:
            return []
        try:
            width = session.deck.touchscreen_image_format()["size"][0]
        except Exception:
            width = 800

        ts = self._deck_config(session).get("touchscreen", {})
        zones = ts.get("zones") if isinstance(ts, dict) else None
        zones = [z for z in zones if isinstance(z, dict)] if isinstance(zones, list) else []

        if not zones:
            for i in range(session.deck.dial_count()):
                cfg = self._get_dial_config(session, i) or {}
                adjust = cfg.get("adjust")
                adjust = adjust if isinstance(adjust, dict) else {}
                drag = None
                if adjust.get("key"):
                    drag = dict(adjust)
                    if cfg.get("fader"):
                        drag["fader"] = True
                zones.append({
                    "label": cfg.get("label", ""),
                    "icon": cfg.get("icon"),
                    "unit": cfg.get("unit"),
                    "meter": cfg.get("meter"),
                    "value_source": adjust.get("key", ""),
                    # The zone is the dial's touch surface: tapping it presses
                    # the dial unless the dial declares its own touch actions.
                    "touch": cfg.get("touch") if cfg.get("touch") is not None
                    else cfg.get("press"),
                    "long_touch": cfg.get("long_touch")
                    if cfg.get("long_touch") is not None
                    else cfg.get("long_press"),
                    # Swiping under a dial does what turning it does.
                    "drag_adjust": drag,
                })

        if not zones:
            return []

        slot = width // len(zones)
        resolved = []
        for i, zone in enumerate(zones):
            x = zone.get("x")
            w = zone.get("w")
            x = int(x) if isinstance(x, (int, float)) else i * slot
            w = int(w) if isinstance(w, (int, float)) else slot
            resolved.append({**zone, "x": x, "w": w, "index": i})
        return resolved

    def _zone_at(self, session, x):
        """Return the touchscreen zone containing pixel ``x``, or None."""
        for zone in self._touch_zones(session):
            if zone["x"] <= x < zone["x"] + zone["w"]:
                return zone
        return None

    async def _apply_touch_fader(self, drag, zone, x_px):
        """Set a drag_adjust value absolutely from a touch position.

        A fader zone maps the touched fraction of its width linearly onto
        min..max (snapped to the step grid). Returns True when applied;
        False (with a debug log) when the bounds are missing, so the caller
        can fall back to relative stepping.
        """
        minimum = _coerce_numeric(drag.get("min"))
        maximum = _coerce_numeric(drag.get("max"))
        if minimum is None or maximum is None or maximum <= minimum:
            self.api.log(
                "Touch fader needs min and max on its adjust; ignoring touch",
                level="debug",
            )
            return False
        step = _coerce_numeric(drag.get("step"))
        if step is None or step <= 0:
            step = 1
        zx = zone.get("x", 0)
        zw = max(1, zone.get("w", 1))
        frac = max(0.0, min(1.0, (x_px - zx) / zw))
        value = minimum + frac * (maximum - minimum)
        value = minimum + round((value - minimum) / step) * step
        value = max(minimum, min(maximum, value))
        if float(value).is_integer():
            value = int(value)
        await self._apply_state_set(drag.get("key", ""), value)
        return True

    async def _flash_touch_zone(self, session, zone_index):
        """Brief lighten + border flash on a touched zone.

        The glass always acknowledges a touch, configured or not — the same
        role the momentary press highlight plays on keys.
        """
        if not session.deck or not session.deck.is_visual():
            return
        old = session.flash_zones.pop(zone_index, None)
        if old is not None:
            old.cancel()
        session.flash_zones[zone_index] = asyncio.get_running_loop().call_later(
            0.15, self._end_zone_flash, session, zone_index
        )
        await self._render_strip_zone(session, zone_index)

    def _end_zone_flash(self, session, zone_index):
        session.flash_zones.pop(zone_index, None)
        self._schedule_strip_render(session, [zone_index])

    async def _on_touchscreen_event(self, session, deck, event, value):
        """Handle a touchscreen tap, long-press, or drag.

        SHORT runs the zone's ``touch`` actions; LONG runs ``long_touch``
        (falling back to ``touch`` when not configured). DRAG arrives as one
        event carrying start and end x — a horizontal swipe adjusts the
        zone's ``drag_adjust`` value like turning a dial (one detent per
        8 px of travel).
        """
        await self._note_input(session)
        if session.overlay_active:
            await self._clear_overlay(session)
            return  # the input that dismisses an overlay never fires actions
        kind = getattr(event, "name", None)
        if kind in ("SHORT", "LONG", "DRAG") and isinstance(value, dict):
            touch_x = value.get("x")
            if isinstance(touch_x, (int, float)):
                await self._publish_input_echo(session, "touch", int(touch_x))

        if kind == "DRAG":
            if not isinstance(value, dict):
                return
            x = value.get("x")
            x_out = value.get("x_out")
            if not isinstance(x, (int, float)) or not isinstance(x_out, (int, float)):
                return
            zone = self._zone_at(session, int(x))
            if zone:
                await self._flash_touch_zone(session, zone["index"])
            drag = zone.get("drag_adjust") if zone else None
            if isinstance(drag, dict) and drag.get("key"):
                applied = False
                if drag.get("fader"):
                    # Fader zones: the swipe end position sets the value.
                    applied = await self._apply_touch_fader(drag, zone, int(x_out))
                if not applied:
                    detents = int((x_out - x) / 8)  # truncate toward zero
                    if detents:
                        await self._apply_dial_adjust(drag, detents)
            return

        if kind not in ("SHORT", "LONG"):
            return
        x = value.get("x") if isinstance(value, dict) else None
        if not isinstance(x, (int, float)):
            return
        await self.api.event_emit(
            "touchscreen.touch", {"x": int(x), "serial": session.serial}
        )
        zone = self._zone_at(session, int(x))
        if zone:
            await self._flash_touch_zone(session, zone["index"])
            drag = zone.get("drag_adjust")
            if (
                kind == "SHORT"
                and isinstance(drag, dict)
                and drag.get("key")
                and drag.get("fader")
            ):
                # Fader zones: a tap jumps the value to the tapped position
                # (replaces the tap action for this zone).
                if await self._apply_touch_fader(drag, zone, int(x)):
                    return
            actions = zone.get("long_touch") if kind == "LONG" else None
            if not actions:
                actions = zone.get("touch")
            await self._execute_actions(
                session, self._action_list(actions), "touchscreen"
            )

    # ──── Display elements (shared by zones, info items, and keys) ────

    _METER_DEFAULT_COLOR = "#8ab493"

    @staticmethod
    def _meter_bounds(meter, bounds_source):
        """Resolve a meter's min/max: explicit > surrounding adjust > 0..100."""
        meter = meter if isinstance(meter, dict) else {}
        bounds = bounds_source if isinstance(bounds_source, dict) else {}
        minimum = _coerce_numeric(meter.get("min"))
        maximum = _coerce_numeric(meter.get("max"))
        if minimum is None:
            minimum = _coerce_numeric(bounds.get("min"))
        if maximum is None:
            maximum = _coerce_numeric(bounds.get("max"))
        if minimum is None:
            minimum = 0
        if maximum is None:
            maximum = 100
        return (minimum, maximum) if maximum > minimum else (0, 100)

    def _resolve_meter(self, meter, raw_value, bounds_source=None):
        """Resolve a meter config to ``(fill_fraction, color)`` or None.

        A meter draws when explicitly enabled (``"meter": {}``/``true``), or
        automatically when the surrounding adjust/drag_adjust declares both
        bounds — so a volume dial gets a level bar with zero extra config.
        ``"meter": false`` always disables; a non-numeric value never draws.
        Color: the highest matching ``thresholds`` entry ({above, color})
        wins, else ``color``, else the accent default.
        """
        if meter is False:
            return None
        explicit = meter is True or isinstance(meter, dict)
        bounds = bounds_source if isinstance(bounds_source, dict) else {}
        auto = (
            _coerce_numeric(bounds.get("min")) is not None
            and _coerce_numeric(bounds.get("max")) is not None
        )
        if not explicit and not auto:
            return None
        value = _coerce_numeric(raw_value)
        if value is None:
            return None
        minimum, maximum = self._meter_bounds(meter, bounds_source)
        frac = max(0.0, min(1.0, (value - minimum) / (maximum - minimum)))
        meter = meter if isinstance(meter, dict) else {}
        color = meter.get("color") or self._METER_DEFAULT_COLOR
        thresholds = meter.get("thresholds")
        if isinstance(thresholds, list):
            best = None
            for rule in thresholds:
                if not isinstance(rule, dict) or not rule.get("color"):
                    continue
                above = _coerce_numeric(rule.get("above"))
                if above is None or value < above:
                    continue
                if best is None or above > best[0]:
                    best = (above, rule["color"])
            if best:
                color = best[1]
        return frac, color

    async def _apply_feedback_styles(self, feedback, label, icon, bg_color, text_color):
        """Evaluate a feedback config and return the styled
        ``(label, icon, bg_color, text_color)``.

        The one conditional-styling path, shared by keys, touchscreen zones,
        and info items: a multi-state ``states`` map keyed by the watched
        value, or the simple active/inactive condition pair.
        """
        if not (isinstance(feedback, dict) and feedback.get("key")):
            return label, icon, bg_color, text_color
        value = await self.api.state_get(feedback["key"])

        states = feedback.get("states")
        if states and isinstance(states, dict):
            state_str = str(value) if value is not None else ""
            appearance = states.get(state_str) or states.get(
                feedback.get("default_state", "")
            )
            if appearance and isinstance(appearance, dict):
                bg_color = appearance.get("bg_color", bg_color) or bg_color
                text_color = appearance.get("text_color", text_color) or text_color
                if appearance.get("label"):
                    label = appearance["label"]
                if appearance.get("icon"):
                    icon = appearance["icon"]
            return label, icon, bg_color, text_color

        condition = feedback.get("condition", {})
        style_active = feedback.get("style_active", {})
        style_inactive = feedback.get("style_inactive", {})
        expected = condition.get("equals") if isinstance(condition, dict) else None
        is_active = (
            (str(value).lower() == str(expected).lower())
            if expected is not None else bool(value)
        )
        if is_active and isinstance(style_active, dict):
            bg_color = style_active.get("bg_color", bg_color) or bg_color
            text_color = style_active.get("text_color", text_color) or text_color
            if feedback.get("label_active"):
                label = feedback["label_active"]
            if style_active.get("icon"):
                icon = style_active["icon"]
        elif not is_active and isinstance(style_inactive, dict):
            bg_color = style_inactive.get("bg_color", bg_color) or bg_color
            text_color = style_inactive.get("text_color", text_color) or text_color
            if feedback.get("label_inactive"):
                label = feedback["label_inactive"]
            if style_inactive.get("icon"):
                icon = style_inactive["icon"]
        return label, icon, bg_color, text_color

    async def _resolve_display(self, element, default_bg, default_fg):
        """Resolve a display element (zone / info item) to drawables.

        Shared schema: ``label`` / ``label_source`` (live override), ``icon``
        (Lucide or asset://), ``value_source`` + ``unit`` (live value text;
        ``value_static`` for fixed text), ``meter`` (bounds defaulting from
        the element's drag_adjust), and ``feedback`` (conditional styling).
        """
        label = element.get("label", "")
        if element.get("label_source"):
            live = await self.api.state_get(element["label_source"])
            if live is not None:
                label = str(live)
        icon = element.get("icon") or None
        bg = element.get("bg_color") or default_bg
        fg = element.get("text_color") or default_fg

        raw_value = None
        value_text = ""
        if element.get("value_static") is not None:
            value_text = str(element["value_static"])
        elif element.get("value_source"):
            raw_value = await self.api.state_get(element["value_source"])
            if raw_value is not None:
                value_text = str(raw_value)
                unit = str(element.get("unit") or "")
                if unit:
                    value_text += unit if unit == "%" else f" {unit}"

        label, icon, bg, fg = await self._apply_feedback_styles(
            element.get("feedback"), label, icon, bg, fg
        )
        meter = self._resolve_meter(
            element.get("meter"), raw_value, element.get("drag_adjust")
        )
        return {
            "label": label, "icon": icon, "value_text": value_text,
            "bg": bg, "fg": fg, "meter": meter,
        }

    async def _draw_strip_zone(self, session, img, zone, height):
        """Draw one display element (background, icon, label, value, meter)
        into a strip image at the element's pixel bounds."""
        draw = ImageDraw.Draw(img)
        zx, zw = zone["x"], zone["w"]
        bg = self._deck_setting(session, "button_color", "#1a1a2e")
        fg = self._deck_setting(session, "text_color", "#e0e0e0")
        resolved = await self._resolve_display(zone, bg, fg)

        draw.rectangle([zx, 0, zx + zw - 1, height - 1], fill=resolved["bg"])

        meter = resolved["meter"]
        text_bottom = height - 18 if meter else height - 4

        text_x = zx + 4
        if resolved["icon"]:
            icon_img = self._render_icon(resolved["icon"], resolved["fg"], 72)
            if icon_img:
                icon_size = max(16, min(40, zw // 4, text_bottom - 8))
                icon_img = icon_img.resize((icon_size, icon_size), Image.LANCZOS)
                icon_y = max(2, (text_bottom - icon_size) // 2)
                img.paste(icon_img, (zx + 8, icon_y), icon_img)
                text_x = zx + 8 + icon_size + 6
        text_w = zx + zw - 4 - text_x

        label = resolved["label"]
        value_text = resolved["value_text"]
        if label and value_text:
            self._paste_label(
                img, label, resolved["fg"],
                (text_x, 4, text_w, height // 3),
                max_font=16, max_lines=1,
            )
            self._paste_label(
                img, value_text, resolved["fg"],
                (text_x, height // 3 + 2, text_w, text_bottom - height // 3 - 4),
                max_font=30, max_lines=1,
            )
        elif label or value_text:
            self._paste_label(
                img, label or value_text, resolved["fg"],
                (text_x, 4, text_w, text_bottom - 8),
                max_font=24, max_lines=2,
            )

        if meter:
            frac, color = meter
            draw.rectangle(
                [zx + 8, height - 14, zx + zw - 9, height - 7], fill="#2a2a3e"
            )
            fill_w = int((zw - 17) * frac)
            if fill_w > 0:
                draw.rectangle(
                    [zx + 8, height - 14, zx + 8 + fill_w, height - 7], fill=color
                )

        # Momentary touch flash (the strip's press highlight).
        if zone.get("index") in session.flash_zones:
            region = img.crop((zx, 0, zx + zw, height))
            overlay = Image.new("RGB", region.size, (255, 255, 255))
            img.paste(Image.blend(region, overlay, 0.22), (zx, 0))
            draw.rectangle(
                [zx + 1, 1, zx + zw - 2, height - 2],
                outline=(255, 255, 255), width=2,
            )

        if zx > 0:
            draw.line([(zx, 8), (zx, height - 8)], fill="#3a3a4e", width=1)

    # ──── Idle clock (unconfigured displays are never dead black) ────

    @staticmethod
    def _clock_text():
        """Current time + date strings (12-hour where the locale has an
        AM/PM notion, 24-hour elsewhere)."""
        now = time.localtime()
        ampm = time.strftime("%p", now)
        if ampm:
            hour = now.tm_hour % 12 or 12
            time_str = f"{hour}:{now.tm_min:02d} {ampm}"
        else:
            time_str = time.strftime("%H:%M", now)
        return time_str, time.strftime("%a %b %d", now)

    def _draw_clock(self, img, width, height, fg):
        """Draw the idle clock — proof the glass works, useful in a rack."""
        time_str, date_str = self._clock_text()
        split = max(10, int(height * 0.62))
        self._paste_label(
            img, time_str, fg,
            (0, 2, width, split),
            max_font=max(16, int(height * 0.5)), max_lines=1,
        )
        self._paste_label(
            img, date_str, "#8a8a9a",
            (0, split + 2, width, max(8, height - split - 4)),
            max_font=max(10, int(height * 0.2)), max_lines=1,
        )

    def _strip_idle_mode(self, session):
        """``"clock"``/``"blank"`` when nothing is configured for the touch
        strip (no custom zones, no dial worth a readout), else None."""
        cfg = self._deck_config(session)
        ts = cfg.get("touchscreen", {})
        ts = ts if isinstance(ts, dict) else {}
        zones = ts.get("zones")
        if isinstance(zones, list) and any(isinstance(z, dict) for z in zones):
            return None
        dials = cfg.get("dials", [])
        if isinstance(dials, list):
            for dial in dials:
                if isinstance(dial, dict) and any(
                    dial.get(field) for field in (
                        "label", "icon", "adjust", "press", "long_press",
                        "cw", "ccw", "touch", "long_touch",
                        "pressed_adjust", "pressed_cw", "pressed_ccw",
                    )
                ):
                    return None
        return "blank" if ts.get("idle") == "blank" else "clock"

    def _info_idle_mode(self, session):
        """``"clock"``/``"blank"`` when the info screen has nothing
        configured (or explicitly asks for the clock), else None."""
        cfg = self._deck_config(session).get("info_strip")
        if not isinstance(cfg, dict):
            return "clock"
        source = cfg.get("source")
        if source == "clock":
            return "clock"
        if source == "blank":
            return "blank"
        items = cfg.get("items")
        if isinstance(items, list) and any(isinstance(i, dict) for i in items):
            return None
        if any(
            cfg.get(field) for field in (
                "key", "text", "label", "label_source", "value_source",
                "icon", "meter",
            )
        ):
            return None
        return "clock"

    async def _tick_clock(self, session):
        """Re-render idle clocks when the wall-clock minute changes
        (rides the watchdog tick)."""
        minute = time.localtime().tm_min
        if minute == session.clock_minute:
            return
        session.clock_minute = minute
        if not session.deck or not session.deck.is_visual():
            return
        if session.deck.is_touch() and self._strip_idle_mode(session) == "clock":
            await self._render_touchscreen(session)
        if self._info_idle_mode(session) == "clock":
            await self._render_info_strip(session)

    async def _render_touchscreen(self, session):
        """Render the touchscreen strip: one display element per zone.

        Zones carry the full display schema (live label, icon, value + unit,
        meter bar, conditional feedback styling). With nothing configured
        the strip shows the idle clock (or stays blank when asked). The
        rendered image is cached on the session so a single-zone change can
        redraw partially.
        """
        deck = session.deck
        if not deck or not deck.is_visual() or not deck.is_touch():
            return
        if session.overlay_active:
            return  # a show_message overlay owns the display right now
        try:
            width, height = deck.touchscreen_image_format()["size"]
        except Exception:
            return

        bg = self._deck_setting(session, "button_color", "#1a1a2e")
        img = Image.new("RGB", (width, height), bg)

        idle = self._strip_idle_mode(session)
        if idle:
            if idle == "clock":
                fg = self._deck_setting(session, "text_color", "#e0e0e0")
                self._draw_clock(img, width, height, fg)
            if session.flash_zones:
                # Touch acknowledgment while idle: lighten the whole strip.
                overlay = Image.new("RGB", img.size, (255, 255, 255))
                img = Image.blend(img, overlay, 0.18)
        else:
            for zone in self._touch_zones(session):
                await self._draw_strip_zone(session, img, zone, height)

        session.strip_image = img
        self._mirror(session, "touchscreen", img)
        try:
            native = PILHelper.to_native_touchscreen_format(deck, img)
            with deck:
                deck.set_touchscreen_image(native, 0, 0, width, height)
        except Exception as e:
            self.api.log(f"Error setting touchscreen image: {e}", level="debug")

    def _schedule_strip_render(self, session, zones=None):
        """Coalesce strip redraws (~40 ms debounce): collect dirty zones,
        then redraw only those regions when possible, the whole strip when
        not (``zones=None`` means everything)."""
        if zones is None or session.strip_dirty == "all":
            session.strip_dirty = "all"
        else:
            if not isinstance(session.strip_dirty, set):
                session.strip_dirty = set()
            session.strip_dirty.update(zones)
        if session.strip_render_task and not session.strip_render_task.done():
            return
        session.strip_render_task = asyncio.create_task(
            self._flush_strip_render(session)
        )

    async def _flush_strip_render(self, session):
        try:
            while session.strip_dirty:
                await asyncio.sleep(0.04)
                dirty = session.strip_dirty
                session.strip_dirty = None
                if dirty == "all" or session.strip_image is None:
                    await self._render_touchscreen(session)
                else:
                    for index in sorted(dirty):
                        await self._render_strip_zone(session, index)
        finally:
            session.strip_render_task = None

    async def _render_strip_zone(self, session, zone_index):
        """Redraw one zone into the cached strip image and ship only that
        region to the deck (the hardware accepts partial-region updates)."""
        deck = session.deck
        if not deck or not deck.is_visual() or not deck.is_touch():
            return
        if session.overlay_active or session.strip_image is None:
            return
        if self._strip_idle_mode(session):
            # Idle clock/blank has no zone regions — repaint the whole strip
            # (this is also how the idle touch flash draws and clears).
            await self._render_touchscreen(session)
            return
        try:
            fmt = deck.touchscreen_image_format()
            width, height = fmt["size"]
        except Exception:
            return
        # A flipped/rotated native format transforms whole-image coordinates;
        # partial regions would land in the wrong place, so redraw fully.
        if fmt.get("flip", (False, False)) != (False, False) or fmt.get("rotation", 0):
            await self._render_touchscreen(session)
            return
        zones = self._touch_zones(session)
        if not 0 <= zone_index < len(zones):
            await self._render_touchscreen(session)
            return
        zone = zones[zone_index]
        img = session.strip_image
        await self._draw_strip_zone(session, img, zone, height)
        self._mirror(session, "touchscreen", img)
        zx = max(0, int(zone["x"]))
        zw = min(int(zone["w"]), width - zx)
        if zw <= 0:
            return
        region = img.crop((zx, 0, zx + zw, height))
        try:
            native = PILHelper.to_native_touchscreen_format(deck, region)
            with deck:
                deck.set_touchscreen_image(native, zx, 0, zw, height)
        except Exception as e:
            self.api.log(f"Error setting touchscreen region: {e}", level="debug")

    # ──── Info strip (decks with a secondary info screen) ────

    @staticmethod
    def _info_strip_items(cfg, width):
        """Normalize ``info_strip`` config to display elements with pixel
        bounds across the screen.

        Accepts the display-element shape directly ({label, label_source,
        icon, value_source, unit, meter, feedback, ...}); the legacy
        ``{source: "state"|"text", key, text}`` shape maps onto it (a text
        source becomes a static value).
        """
        if not isinstance(cfg, dict):
            return []

        def _normalize(element):
            if element.get("source") == "text":
                element["value_static"] = str(element.get("text", ""))
                element.pop("value_source", None)
            elif not element.get("value_source"):
                element["value_source"] = element.get("key", "")
            return element

        items = cfg.get("items")
        if isinstance(items, list):
            items = [_normalize(dict(i)) for i in items if isinstance(i, dict)]
            # The screen physically fits two items side by side.
            items = items[:2]
            if items:
                slot = width // len(items)
                for idx, item in enumerate(items):
                    item["x"] = idx * slot
                    item["w"] = slot
                return items

        element = _normalize(dict(cfg))
        element["x"] = 0
        element["w"] = width
        return [element]

    async def _render_info_strip(self, session):
        """Render the secondary info screen from the ``info_strip`` config.

        The config is a display element (live label, icon, value + unit,
        meter, feedback styling); the legacy ``{"source": "state"|"text",
        "key"/"text", "label"}`` shape still works. Re-rendered when a
        watched key changes.
        """
        deck = session.deck
        if not deck or not deck.is_visual():
            return
        if session.overlay_active:
            return  # a show_message overlay owns the display right now
        try:
            width, height = deck.screen_image_format()["size"]
        except Exception:
            return
        if not width or not height:
            return  # this deck has no info screen

        bg = self._deck_setting(session, "button_color", "#1a1a2e")
        img = Image.new("RGB", (width, height), bg)

        idle = self._info_idle_mode(session)
        if idle == "clock":
            fg = self._deck_setting(session, "text_color", "#e0e0e0")
            self._draw_clock(img, width, height, fg)
        elif idle is None:
            cfg = self._deck_config(session).get("info_strip")
            for element in self._info_strip_items(cfg, width):
                await self._draw_strip_zone(session, img, element, height)
        # idle == "blank": leave the plain background

        self._mirror(session, "screen", img)
        try:
            native = PILHelper.to_native_screen_format(deck, img)
            with deck:
                deck.set_screen_image(native)
        except Exception as e:
            self.api.log(f"Error setting info strip image: {e}", level="debug")

    # ──── Brightness (auto rules + idle dim) ────

    async def _current_brightness_level(self, session):
        """Return the active brightness: the first matching ``auto_brightness``
        rule's level, else the base ``brightness`` config value. Clamped 0-100."""
        rules = self._deck_config(session).get("auto_brightness", [])
        if isinstance(rules, list):
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                when = rule.get("when")
                level = _coerce_numeric(rule.get("level"))
                if when is None or level is None:
                    continue
                if await self._eval_condition(when):
                    return max(0, min(100, int(level)))
        base = _coerce_numeric(self._unit_brightness(session))
        return max(0, min(100, int(base if base is not None else 70)))

    def _unit_brightness(self, session):
        """Base brightness for one deck.

        A unit property, not a layout property: ``deck_settings[serial]``
        wins, then the flat plugin-wide ``brightness``, then 70. Never read
        from a ``decks[serial]`` layout override — a deck shouldn't need its
        own layout just to sit at a different brightness.
        """
        settings = self.api.config.get("deck_settings")
        if isinstance(settings, dict):
            entry = settings.get(session.serial)
            if isinstance(entry, dict):
                level = _coerce_numeric(entry.get("brightness"))
                if level is not None:
                    return level
        return self.api.config.get("brightness", 70)

    def _set_deck_brightness(self, session, level):
        """Apply a brightness level to a deck (best-effort)."""
        if not session.deck:
            return
        try:
            session.deck.set_brightness(level)
        except Exception as e:
            self.api.log(f"Error setting brightness: {e}", level="debug")

    async def _apply_active_brightness(self, session):
        """Re-apply the rule-or-base brightness (used on wake and rule change)."""
        if not session.deck:
            return
        self._set_deck_brightness(session, await self._current_brightness_level(session))

    async def _check_idle_dim(self, session):
        """Dim a deck when no input has arrived for ``idle_dim.after_seconds``.

        Runs on the watchdog tick. Any key/dial/touch input wakes the deck via
        ``_note_input``.
        """
        cfg = self._deck_config(session).get("idle_dim")
        if not isinstance(cfg, dict) or session.idle_dimmed:
            return
        after = _coerce_numeric(cfg.get("after_seconds"))
        level = _coerce_numeric(cfg.get("level"))
        if after is None or after <= 0 or level is None:
            return
        now = asyncio.get_event_loop().time()
        if now - session.last_input >= after:
            session.idle_dimmed = True
            self._set_deck_brightness(session, max(0, min(100, int(level))))

    async def _note_input(self, session):
        """Record user input: resets the idle timer and wakes a dimmed deck."""
        session.last_input = asyncio.get_event_loop().time()
        if session.idle_dimmed:
            session.idle_dimmed = False
            await self._apply_active_brightness(session)

    async def _publish_input_echo(self, session, kind, index):
        """Publish ``<serial>.last_input`` so the IDE can flash the control.

        Format ``"<kind>:<index>:<seq>"`` — the monotonic seq makes repeated
        presses of the same control still produce a state change (the state
        store dedupes writes of an unchanged value).
        """
        session.input_seq += 1
        await self.api.state_set(
            f"{session.serial}.last_input", f"{kind}:{int(index)}:{session.input_seq}"
        )

    # ──── Page Navigation ────

    def _effective_page_count(self, session):
        """Pages exist by being used — no page-count setting.

        The count is 1 + the highest page index referenced anywhere in the
        deck's config view: button placements, page rules, page names, and
        numeric navigate targets in any action list (key presses including
        off/hold actions, locked keys, dial turns/presses, strip zones).
        Every deck has at least one page.
        """
        cfg = self._deck_config(session)
        highest = 0

        def note(value):
            nonlocal highest
            try:
                idx = int(value)
            except (TypeError, ValueError):
                return
            if idx > highest:
                highest = idx

        def scan_actions(value):
            for action in self._action_list(value):
                if action.get("action") == "navigate":
                    note(action.get("page"))
                for nested in ("off_action", "hold_action"):
                    sub = action.get(nested)
                    if isinstance(sub, dict) and sub.get("action") == "navigate":
                        note(sub.get("page"))

        buttons = cfg.get("buttons", [])
        for btn in buttons if isinstance(buttons, list) else []:
            if not isinstance(btn, dict):
                continue
            note(btn.get("page", 0))
            bindings = btn.get("bindings", {})
            if isinstance(bindings, dict):
                scan_actions(bindings.get("press"))
        for btn in self._global_buttons(cfg):
            bindings = btn.get("bindings", {})
            if isinstance(bindings, dict):
                scan_actions(bindings.get("press"))

        rules = cfg.get("auto_page", [])
        for rule in rules if isinstance(rules, list) else []:
            if isinstance(rule, dict):
                note(rule.get("page"))

        names = cfg.get("page_names", {})
        for key in names.keys() if isinstance(names, dict) else []:
            note(key)

        dials = cfg.get("dials", [])
        for dial in dials if isinstance(dials, list) else []:
            if isinstance(dial, dict):
                for field in ("cw", "ccw", "press"):
                    scan_actions(dial.get(field))

        touchscreen = cfg.get("touchscreen", {})
        zones = touchscreen.get("zones") if isinstance(touchscreen, dict) else None
        for zone in zones if isinstance(zones, list) else []:
            if isinstance(zone, dict):
                for field in ("touch", "long_touch"):
                    scan_actions(zone.get(field))

        return highest + 1

    async def _change_page(self, session, new_page):
        """Switch a deck to a different button page."""
        max_pages = self._effective_page_count(session)
        if new_page < 0:
            new_page = 0
        elif new_page >= max_pages:
            new_page = max_pages - 1

        if new_page == session.current_page:
            return

        session.current_page = new_page
        await self.api.state_set(f"{session.serial}.current_page", new_page)
        if self._primary_session() is session:
            await self.api.state_set("current_page", new_page)
        await self._render_all_buttons(session)
        self.api.log(f"Switched to page {new_page}", level="debug")

    # ──── Button Rendering ────

    async def _render_all_buttons(self, session):
        """Render every key for a deck's current page (LCD keys + touch keys)."""
        if not session.deck or not session.deck.is_visual():
            return

        key_count = session.deck.key_count() + session.deck.touch_key_count()
        for key_index in range(key_count):
            await self._render_button(session, key_index)

    @staticmethod
    def _is_touch_key(session, key_index):
        """True for the color-only touch keys indexed after the LCD keys."""
        if not session.deck:
            return False
        key_count = session.deck.key_count()
        return key_count <= key_index < key_count + session.deck.touch_key_count()

    async def _render_button(self, session, key_index):
        """Render a single button image based on its assignment and state."""
        if not session.deck or not session.deck.is_visual():
            return
        if session.overlay_active:
            return  # a show_message overlay owns the display right now

        is_touch_key = self._is_touch_key(session, key_index)
        assignment = self._get_button_assignment(
            session, session.current_page, key_index
        )

        # Touch keys have no LCD — an unassigned one just goes dark.
        if is_touch_key and not assignment:
            self._apply_key_color(session, key_index, "#000000")
            return

        global_bg = self._deck_setting(session, "button_color", "#1a1a2e")
        global_text = self._deck_setting(session, "text_color", "#e0e0e0")
        label = ""
        icon = None
        bg_color = global_bg
        text_color = global_text
        value_text = ""
        meter = None

        if assignment:
            # Hidden by visible_when → blank black key; render nothing else.
            bindings = assignment.get("bindings", {})
            visible_when = bindings.get("visible_when") if isinstance(bindings, dict) else None
            if visible_when is not None and not await self._eval_condition(visible_when):
                if is_touch_key:
                    self._apply_key_color(session, key_index, "#000000")
                else:
                    self._apply_key_image(
                        session, key_index,
                        self._create_button_image(session, "", "#000000", "#000000"),
                    )
                return

            label = assignment.get("label", "")
            icon = assignment.get("icon") or None

            # Live label / live value lines (display-element fields)
            if assignment.get("label_source"):
                live_label = await self.api.state_get(assignment["label_source"])
                if live_label is not None:
                    label = str(live_label)
            raw_value = None
            if assignment.get("value_source"):
                raw_value = await self.api.state_get(assignment["value_source"])
                if raw_value is not None:
                    value_text = str(raw_value)
                    unit = str(assignment.get("unit") or "")
                    if unit:
                        value_text += unit if unit == "%" else f" {unit}"
            # Keys have no surrounding adjust, so a key meter renders only
            # when explicitly enabled (bounds default to 0..100).
            meter = self._resolve_meter(assignment.get("meter"), raw_value)

            # Per-button default colors (override global defaults)
            bg_color = assignment.get("bg_color") or global_bg
            text_color = assignment.get("text_color") or global_text

            # Toggle mode: on_label/off_label override static label
            press_binding = _unwrap_binding(bindings.get("press")) if isinstance(bindings, dict) else None
            if press_binding and isinstance(press_binding, dict) and press_binding.get("mode") == "toggle":
                tk = press_binding.get("toggle_key", "")
                tv = press_binding.get("toggle_value")
                if tk:
                    tval = await self.api.state_get(tk)
                    t_active = (str(tval).lower() == str(tv).lower()) if tv is not None else bool(tval)
                    on_lbl = press_binding.get("on_label", "")
                    off_lbl = press_binding.get("off_label", "")
                    if t_active and on_lbl:
                        label = on_lbl
                    elif not t_active and off_lbl:
                        label = off_lbl

            # Conditional feedback styling (shared resolver)
            feedback = bindings.get("feedback") if isinstance(bindings, dict) else None
            label, icon, bg_color, text_color = await self._apply_feedback_styles(
                feedback, label, icon, bg_color, text_color
            )

        # Locked keys store their macro mark under page None (lit on every
        # page); page keys under their page.
        macro_mark = (
            session.macro_marks.get((None, key_index))
            or session.macro_marks.get((session.current_page, key_index))
        )
        # A key that navigates to the page currently showing gets a "you are
        # here" treatment automatically — page-switcher rows read as tabs.
        nav_target = self._nav_target_page(assignment) if assignment else None
        nav_active = nav_target is not None and nav_target == session.current_page

        # Touch keys show color only (no LCD): the effective background color
        # after feedback/toggle evaluation, brightened while held; a macro
        # run/result mark overrides the color outright.
        if is_touch_key:
            color = self._MACRO_MARK_COLORS.get(macro_mark, bg_color)
            if nav_active and macro_mark is None:
                color = self._lighten_hex(color, 0.2)
            if key_index in session.pressed_keys:
                color = self._lighten_hex(color, 0.25)
            self._apply_key_color(session, key_index, color)
            return

        # Generate the button image and set it on the deck. While the key is
        # physically held, draw the momentary-press highlight on top; a macro
        # run/result mark draws a colored border (+ loader glyph).
        image = self._create_button_image(
            session, label, bg_color, text_color, icon,
            value_text=value_text, meter=meter,
        )
        if nav_active:
            image = self._apply_nav_active(image)
        if key_index in session.pressed_keys:
            image = self._apply_press_highlight(image)
        if macro_mark:
            image = self._apply_macro_mark(image, macro_mark)
        self._apply_key_image(session, key_index, image)

    @staticmethod
    def _nav_target_page(assignment):
        """The numeric deck page a key's first press action navigates to.

        None for non-navigate keys and for the relative next/previous targets
        (those have no single page to light up for).
        """
        bindings = assignment.get("bindings", {})
        if not isinstance(bindings, dict):
            return None
        press = _unwrap_binding(bindings.get("press"))
        if not isinstance(press, dict) or press.get("action") != "navigate":
            return None
        try:
            return int(press.get("page"))
        except (TypeError, ValueError):
            return None

    def _apply_nav_active(self, image):
        """Subtle highlight for a page key whose target page is showing."""
        try:
            overlay = Image.new("RGB", image.size, (255, 255, 255))
            out = Image.blend(image, overlay, 0.12)
            draw = ImageDraw.Draw(out)
            w, h = out.size
            draw.rectangle([0, 0, w - 1, h - 1], outline=(138, 180, 147), width=2)
            return out
        except Exception:
            return image

    @staticmethod
    def _hex_to_rgb(color):
        """Parse a #rrggbb (or #rgb) hex color to an (r, g, b) tuple."""
        value = str(color or "").lstrip("#")
        try:
            if len(value) == 3:
                return tuple(int(c * 2, 16) for c in value)
            if len(value) == 6:
                return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            pass
        return (0, 0, 0)

    @staticmethod
    def _lighten_hex(color, amount):
        """Blend a hex color toward white by ``amount`` (0..1), as hex."""
        r, g, b = StreamDeckPlugin._hex_to_rgb(color)
        r = int(r + (255 - r) * amount)
        g = int(g + (255 - g) * amount)
        b = int(b + (255 - b) * amount)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _apply_key_color(self, session, key_index, color):
        """Set a touch key's RGB backlight from a hex color (thread-safe)."""
        if self._mirror_enabled(session):
            session.touch_key_colors[key_index] = color
            self._schedule_mirror_bump(session)
        r, g, b = self._hex_to_rgb(color)
        try:
            with session.deck:
                session.deck.set_key_color(key_index, r, g, b)
        except Exception as e:
            self.api.log(f"Error setting key {key_index} color: {e}", level="debug")

    def _apply_press_highlight(self, image):
        """Return a lightened, inset-bordered variant of a button image.

        Used as brief tactile feedback while a key is physically held. Any
        failure falls back to the original image so a press never blanks a key.
        """
        try:
            overlay = Image.new("RGB", image.size, (255, 255, 255))
            highlighted = Image.blend(image, overlay, 0.25)
            draw = ImageDraw.Draw(highlighted)
            w, h = image.size
            draw.rectangle([1, 1, w - 2, h - 2], outline=(255, 255, 255), width=2)
            return highlighted
        except Exception:
            return image

    def _apply_key_image(self, session, key_index, image):
        """Encode a PIL image and set it on a deck key (thread-safe)."""
        self._mirror(session, f"key_{key_index}", image)
        try:
            native_image = PILHelper.to_native_key_format(session.deck, image)
            with session.deck:
                session.deck.set_key_image(key_index, native_image)
        except Exception as e:
            self.api.log(f"Error setting key {key_index} image: {e}", level="debug")

    def _create_button_image(self, session, label, bg_color, text_color,
                             icon_name=None, value_text="", meter=None):
        """Create a PIL image for a button: icon, wrapped label, optional
        live value line, optional meter bar along the bottom."""
        image_format = session.deck.key_image_format()
        width = image_format["size"][0]
        height = image_format["size"][1]

        img = Image.new("RGB", (width, height), bg_color)

        # A meter reserves a strip along the bottom edge.
        usable_h = height - 10 if meter else height

        # Load icon image if specified
        icon_img = self._render_icon(icon_name, text_color, width) if icon_name else None

        # An icon carries the identity, so with an icon plus both texts the
        # live value wins the text slot (two text lines + icon turn to mush
        # on a small key).
        if icon_img and value_text:
            label = value_text
            value_text = ""

        if icon_img and label:
            # Icon in the upper area, wrapped label below it.
            icon_size = min(width, usable_h) // 2
            icon_img = icon_img.resize((icon_size, icon_size), Image.LANCZOS)
            icon_x = (width - icon_size) // 2
            icon_y = max(4, (usable_h // 2) - icon_size + 4)
            img.paste(icon_img, (icon_x, icon_y), icon_img)

            text_top = icon_y + icon_size + 2
            self._paste_label(
                img, label, text_color,
                (2, text_top, width - 4, max(0, usable_h - text_top - 2)),
                max_font=14, max_lines=2,
            )

        elif icon_img:
            # Icon only, centered
            icon_size = int(min(width, usable_h) * 0.6)
            icon_img = icon_img.resize((icon_size, icon_size), Image.LANCZOS)
            icon_x = (width - icon_size) // 2
            icon_y = (usable_h - icon_size) // 2
            img.paste(icon_img, (icon_x, icon_y), icon_img)

        elif label and value_text:
            # Name on top, live value as the main element below it.
            self._paste_label(
                img, label, text_color,
                (2, 2, width - 4, usable_h // 3),
                max_font=14, max_lines=1,
            )
            self._paste_label(
                img, value_text, text_color,
                (2, usable_h // 3 + 2, width - 4,
                 max(0, usable_h - usable_h // 3 - 4)),
                max_font=max(16, usable_h // 3), max_lines=1,
            )

        elif label or value_text:
            # Single text — wrapped and shrunk to fill the key.
            pad = max(2, width // 12)
            self._paste_label(
                img, label or value_text, text_color,
                (pad, pad, width - 2 * pad, usable_h - 2 * pad),
                max_font=max(14, usable_h // 4), max_lines=3,
            )

        if meter:
            frac, color = meter
            draw = ImageDraw.Draw(img)
            draw.rectangle([3, height - 8, width - 4, height - 3], fill="#2a2a3e")
            fill_w = int((width - 7) * frac)
            if fill_w > 0:
                draw.rectangle([3, height - 8, 3 + fill_w, height - 3], fill=color)

        return img

    # ──── Text Rendering (bundled font, word-wrap + shrink-to-fit) ────

    def _load_text_font(self):
        """Resolve the bundled text-font path used for button labels.

        ``arial.ttf`` exists only on Windows, so a bundled font keeps labels
        legible on Linux and the Pi. ``_text_font`` falls back to arial.ttf
        then PIL's bitmap default if this file is somehow missing.
        """
        text_ttf = Path(__file__).parent / "fonts" / "DejaVuSans.ttf"
        self._text_font_path = str(text_ttf) if text_ttf.exists() else None
        if self._text_font_path is None:
            self.api.log(
                "Bundled text font not found; labels will use a small default font",
                level="warning",
            )

    def _text_font(self, size):
        """Return a cached text font at ``size`` (bundled font, then fallbacks)."""
        font = self._font_cache.get(size)
        if font is not None:
            return font
        font = None
        if self._text_font_path:
            try:
                font = ImageFont.truetype(self._text_font_path, size)
            except (IOError, OSError):
                font = None
        if font is None:
            try:
                font = ImageFont.truetype("arial.ttf", size)
            except (IOError, OSError):
                font = ImageFont.load_default()
        self._font_cache[size] = font
        return font

    @staticmethod
    def _wrap_greedy(draw, text, font, max_width):
        """Greedy word-wrap: pack words onto lines no wider than ``max_width``.

        A single word longer than ``max_width`` keeps its own (overflowing)
        line — there's nothing to break it on; shrink-to-fit handles the rest.
        """
        words = text.split()
        if not words:
            return []
        lines = []
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}"
            if draw.textlength(trial, font=font) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    def _paste_label(self, img, label, color, box, max_font, max_lines):
        """Draw ``label`` centered in ``box``=(x, y, w, h), wrapped + shrunk."""
        bx, by, bw, bh = box
        if not label or bw <= 0 or bh <= 0:
            return
        layer = self._render_label_layer(label, color, bw, bh, max_font, max_lines)
        img.paste(layer, (bx, by), layer)

    def _render_label_layer(self, label, color, w, h, max_font, max_lines):
        """Render ``label`` to an RGBA layer of size (w, h), wrapped and shrunk
        to fit in at most ``max_lines`` lines. Cached like rendered icons."""
        cache_key = (label, w, h, color, max_font, max_lines)
        cached = self._label_cache.get(cache_key)
        if cached is not None:
            return cached.copy()

        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)

        # Shrink the font from max_font down to an 8px floor until the wrapped
        # text fits the box in both dimensions and the line count.
        min_font = 8
        font = self._text_font(min_font)
        lines = self._wrap_greedy(draw, label, font, w)
        for size in range(max_font, min_font - 1, -1):
            candidate = self._text_font(size)
            wrapped = self._wrap_greedy(draw, label, candidate, w)
            if len(wrapped) > max_lines:
                continue
            ascent, descent = candidate.getmetrics()
            line_h = ascent + descent
            widest = max((draw.textlength(ln, font=candidate) for ln in wrapped), default=0)
            if line_h * len(wrapped) <= h and widest <= w:
                font, lines = candidate, wrapped
                break

        lines = lines[:max_lines]
        ascent, descent = font.getmetrics()
        line_h = ascent + descent
        total_h = line_h * len(lines)
        y = max(0, (h - total_h) // 2)
        for line in lines:
            line_w = draw.textlength(line, font=font)
            x = max(0, int((w - line_w) // 2))
            draw.text((x, y), line, fill=color, font=font)
            y += line_h

        self._label_cache[cache_key] = layer.copy()
        return layer

    # ──── Icon Rendering ────

    def _load_icon_font(self):
        """Load the bundled Lucide icon font and code point map."""
        fonts_dir = Path(__file__).parent / "fonts"
        ttf_path = fonts_dir / "lucide.ttf"
        info_path = fonts_dir / "lucide-info.json"

        if not ttf_path.exists():
            self.api.log("Lucide icon font not found, icons will not render", level="warning")
            return

        try:
            with open(info_path, "r", encoding="utf-8") as f:
                info = json.load(f)
            # Build name -> unicode char map
            # encodedCode values are like "\e589" — a backslash followed by
            # the full hex code point. Skip only the leading backslash.
            for name, data in info.items():
                encoded = data.get("encodedCode", "")
                if encoded.startswith("\\"):
                    code_point = int(encoded[1:], 16)
                    self._icon_map[name] = chr(code_point)
            self.api.log(f"Loaded {len(self._icon_map)} icon glyphs", level="debug")
        except Exception as e:
            self.api.log(f"Failed to load icon map: {e}", level="warning")

        self._icon_font_path = str(ttf_path)

    def _render_icon(self, icon_name, color, button_size):
        """Render an icon as an RGBA PIL Image.

        Supports:
          - Lucide icon names (rendered from the bundled TTF font)
          - asset:// references (loaded from project assets directory)
          - File paths to PNG/JPG images
        """
        if not icon_name:
            return None

        # Asset reference — load image file
        if icon_name.startswith("asset://"):
            return self._load_asset_icon(icon_name[8:], button_size)

        # Lucide icon — render from font
        return self._render_lucide_icon(icon_name, color, button_size)

    def _render_lucide_icon(self, icon_name, color, button_size):
        """Render a Lucide icon glyph to an RGBA image."""
        if not self._icon_map or not hasattr(self, "_icon_font_path"):
            return None

        char = self._icon_map.get(icon_name)
        if not char:
            return None

        # Check cache
        icon_size = int(button_size * 0.6)
        cache_key = (icon_name, icon_size, color)
        cached = self._icon_cache.get(cache_key)
        if cached is not None:
            return cached.copy()

        try:
            font = ImageFont.truetype(self._icon_font_path, icon_size)
            # Render glyph onto transparent image
            img = Image.new("RGBA", (icon_size, icon_size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)

            # Measure and center the glyph
            bbox = draw.textbbox((0, 0), char, font=font)
            glyph_w = bbox[2] - bbox[0]
            glyph_h = bbox[3] - bbox[1]
            x = (icon_size - glyph_w) // 2 - bbox[0]
            y = (icon_size - glyph_h) // 2 - bbox[1]
            draw.text((x, y), char, fill=color, font=font)

            self._icon_cache[cache_key] = img.copy()
            return img
        except Exception as e:
            self.api.log(f"Failed to render icon '{icon_name}': {e}", level="debug")
            return None

    def _load_asset_icon(self, filename, button_size):
        """Load a custom icon from the project assets directory."""
        try:
            # Assets are stored relative to the project directory
            # The plugin API provides the project path via config
            assets_dir = Path(self.api.config.get("_project_dir", "")) / "assets"
            icon_path = assets_dir / filename
            if not icon_path.exists():
                return None
            icon = Image.open(icon_path).convert("RGBA")
            return icon
        except Exception as e:
            self.api.log(f"Failed to load asset icon '{filename}': {e}", level="debug")
            return None

    # ──── Conditions, Visibility & Auto-Page ────

    async def _eval_condition(self, cond) -> bool:
        """Evaluate a visible_when / auto_page condition against current state.

        Supports a single ``{key, operator, value}`` condition, a compound
        ``{all: [...]}`` (every sub-condition must be true) and ``{any: [...]}``
        (at least one true) — matching the panel UI visible_when schema. An
        empty ``all`` is vacuously true and an empty ``any`` is false. Operator
        semantics mirror the platform condition evaluator.
        """
        if not isinstance(cond, dict):
            return False

        all_list = cond.get("all")
        if isinstance(all_list, list):
            for child in all_list:
                if not await self._eval_condition(child):
                    return False
            return True

        any_list = cond.get("any")
        if isinstance(any_list, list):
            for child in any_list:
                if await self._eval_condition(child):
                    return True
            return False

        key = cond.get("key")
        if not key:
            return False
        actual = await self.api.state_get(key)
        try:
            return _eval_operator(cond.get("operator", "eq"), actual, cond.get("value"))
        except ValueError:
            self.api.log(
                f"Ignoring unknown condition operator {cond.get('operator')!r}",
                level="debug",
            )
            return False

    async def _is_button_visible(self, assignment) -> bool:
        """True unless the button's visible_when condition evaluates false."""
        bindings = assignment.get("bindings", {})
        if not isinstance(bindings, dict):
            return True
        visible_when = bindings.get("visible_when")
        if visible_when is None:
            return True
        return await self._eval_condition(visible_when)

    async def _evaluate_auto_page(self, session):
        """Return the page of the first matching auto_page rule, or None.

        Rules are evaluated in array order; the first whose ``when`` condition
        is true wins. The page index is clamped to the valid range.
        """
        rules = self._deck_config(session).get("auto_page", [])
        if not isinstance(rules, list):
            return None
        max_pages = self._effective_page_count(session)
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            when = rule.get("when")
            page = rule.get("page")
            if when is None or page is None:
                continue
            if await self._eval_condition(when):
                try:
                    target = int(page)
                except (TypeError, ValueError):
                    continue
                return max(0, min(target, max_pages - 1))
        return None

    # ──── State Subscriptions ────

    async def _setup_feedback_subscriptions(self, session):
        """Subscribe to every state key that can change a deck's buttons,
        displays, brightness, or page.

        Covers feedback keys, toggle keys, visible_when condition keys, and
        auto_page rule keys. Auto_page keys are also tracked separately so that
        only an auto_page-watched change can drive automatic paging. Each deck
        subscribes independently against its own config view.
        """
        cfg = self._deck_config(session)
        buttons = cfg.get("buttons", [])
        page_buttons = (
            [b for b in buttons if isinstance(b, dict)]
            if isinstance(buttons, list) else []
        )
        global_buttons = self._global_buttons(cfg)
        locked = self._locked_indexes(cfg)
        # A page entry shadowed by a locked key never renders or fires, so
        # its watch keys and macro references are inert too.
        page_buttons = [b for b in page_buttons if b.get("index") not in locked]
        watch_keys = set()
        session.auto_page_keys = set()

        for btn in page_buttons + global_buttons:
            # Live display sources (entry level, like label/icon)
            for field in ("label_source", "value_source"):
                if btn.get(field):
                    watch_keys.add(btn[field])
            bindings = btn.get("bindings", {})
            if isinstance(bindings, dict):
                # Feedback key
                feedback = bindings.get("feedback", {})
                if isinstance(feedback, dict) and feedback.get("key"):
                    watch_keys.add(feedback["key"])
                # Toggle key (for label/icon updates on toggle state change)
                press = _unwrap_binding(bindings.get("press"))
                if isinstance(press, dict) and press.get("toggle_key"):
                    watch_keys.add(press["toggle_key"])
                # Visibility condition keys
                watch_keys.update(_condition_state_keys(bindings.get("visible_when")))

        # Auto-page rule keys (tracked separately — only these drive paging)
        auto_page = cfg.get("auto_page", [])
        if isinstance(auto_page, list):
            for rule in auto_page:
                if isinstance(rule, dict):
                    session.auto_page_keys.update(_condition_state_keys(rule.get("when")))
        watch_keys |= session.auto_page_keys

        # Touchscreen strip keys (tracked separately — a change re-renders the
        # strip): explicit zone label/value sources, plus every dial adjust key
        # since the default zones display those values.
        session.touch_strip_keys = set()
        touchscreen = cfg.get("touchscreen", {})
        zones = touchscreen.get("zones") if isinstance(touchscreen, dict) else None
        if isinstance(zones, list):
            for zone in zones:
                if isinstance(zone, dict):
                    for field in ("label_source", "value_source"):
                        if zone.get(field):
                            session.touch_strip_keys.add(zone[field])
                    zone_fb = zone.get("feedback")
                    if isinstance(zone_fb, dict) and zone_fb.get("key"):
                        session.touch_strip_keys.add(zone_fb["key"])
        dials = cfg.get("dials", [])
        if isinstance(dials, list):
            for dial in dials:
                if isinstance(dial, dict):
                    adjust = dial.get("adjust")
                    if isinstance(adjust, dict) and adjust.get("key"):
                        session.touch_strip_keys.add(adjust["key"])
        watch_keys |= session.touch_strip_keys

        # Info-strip keys (tracked separately — a change re-renders the strip)
        session.info_strip_keys = set()
        info_strip = cfg.get("info_strip")
        for element in self._info_strip_items(info_strip, 0):
            for field in ("label_source", "value_source"):
                if element.get(field):
                    session.info_strip_keys.add(element[field])
            item_fb = element.get("feedback")
            if isinstance(item_fb, dict) and item_fb.get("key"):
                session.info_strip_keys.add(item_fb["key"])
        watch_keys |= session.info_strip_keys

        # Auto-brightness rule keys (tracked separately — a change re-applies
        # the brightness level)
        session.brightness_keys = set()
        auto_brightness = cfg.get("auto_brightness", [])
        if isinstance(auto_brightness, list):
            for rule in auto_brightness:
                if isinstance(rule, dict):
                    session.brightness_keys.update(_condition_state_keys(rule.get("when")))
        watch_keys |= session.brightness_keys

        # Macro feedback map: which (page, key) bindings reference which
        # macro, so macro.started/completed/error events can mark those keys.
        # Only keys have a display to mark — dial/zone macro actions aren't
        # mapped.
        session.macro_keys = {}

        def _note_macro(action, page, index):
            if (
                isinstance(action, dict)
                and action.get("action") == "macro"
                and action.get("macro")
            ):
                session.macro_keys.setdefault(str(action["macro"]), set()).add(
                    (page, index)
                )

        # Locked keys are marked with page None: their macro runs light the
        # key on whatever page is showing.
        entries = [(btn, btn.get("page", 0)) for btn in page_buttons]
        entries += [(btn, None) for btn in global_buttons]
        for btn, page in entries:
            index = btn.get("index")
            if index is None:
                continue
            bindings = btn.get("bindings", {})
            if not isinstance(bindings, dict):
                continue
            for action in self._press_actions(bindings):
                _note_macro(action, page, index)
                _note_macro(action.get("off_action"), page, index)
                _note_macro(action.get("hold_action"), page, index)

        callback = functools.partial(self._on_state_change, session)
        for key in watch_keys:
            sub_id = await self.api.state_subscribe(key, callback)
            session.feedback_subs.append(sub_id)

    async def _on_state_change(self, session, key, value, old_value):
        """React to a watched state change: auto-page switch and/or re-render."""
        if not session.deck or not session.deck.is_visual():
            return

        # Auto-page: re-evaluate only when an auto_page-watched key changes, so
        # an ordinary feedback/visibility change never overrides manual paging.
        if key in session.auto_page_keys:
            target = await self._evaluate_auto_page(session)
            if target is not None and target != session.current_page:
                await self._change_page(session, target)
                return  # _change_page re-rendered the whole new page

        # Touchscreen strip shows live values — schedule a debounced redraw
        # of just the zones that watch this key (partial-region update).
        if key in session.touch_strip_keys:
            zones = [
                i for i, z in enumerate(self._touch_zones(session))
                if self._zone_watches_key(z, key)
            ]
            self._schedule_strip_render(session, zones or None)

        # Info strip shows a live value — re-render it on change
        if key in session.info_strip_keys:
            await self._render_info_strip(session)

        # Auto-brightness rules — re-apply unless the deck is idle-dimmed
        # (waking on input restores the rule level anyway)
        if key in session.brightness_keys and not session.idle_dimmed:
            await self._apply_active_brightness(session)

        # Re-render keys that depend on this state key: locked keys on any
        # page, plus the current page's buttons (entries shadowed by a lock
        # are inert and skipped).
        cfg = self._deck_config(session)
        locked = self._locked_indexes(cfg)
        to_render = list(self._global_buttons(cfg))
        buttons = cfg.get("buttons", [])
        for btn in buttons if isinstance(buttons, list) else []:
            if not isinstance(btn, dict):
                continue
            if btn.get("page", 0) != session.current_page:
                continue
            if btn.get("index") in locked:
                continue
            to_render.append(btn)

        for btn in to_render:
            if self._button_watches_key(btn, key):
                key_index = btn.get("index")
                if key_index is not None:
                    await self._render_button(session, key_index)

    @staticmethod
    def _zone_watches_key(zone, key):
        """True when a zone's rendering depends on a state key."""
        if key in (zone.get("label_source"), zone.get("value_source")):
            return True
        feedback = zone.get("feedback")
        if isinstance(feedback, dict) and feedback.get("key") == key:
            return True
        drag = zone.get("drag_adjust")
        return isinstance(drag, dict) and drag.get("key") == key

    @classmethod
    def _button_watches_key(cls, btn, key):
        """True when a button entry's rendering depends on a state key
        (entry-level live sources or its binding's watch keys)."""
        if key in (btn.get("label_source"), btn.get("value_source")):
            return True
        return cls._binding_watches_key(btn.get("bindings", {}), key)

    @staticmethod
    def _binding_watches_key(bindings, key):
        """True when a key binding's rendering depends on a state key."""
        if not isinstance(bindings, dict):
            return False
        feedback = bindings.get("feedback", {})
        if isinstance(feedback, dict) and feedback.get("key") == key:
            return True
        press = _unwrap_binding(bindings.get("press"))
        if isinstance(press, dict) and press.get("toggle_key") == key:
            return True
        return key in _condition_state_keys(bindings.get("visible_when"))

    # ──── Macro run feedback ────

    _MACRO_MARK_COLORS = {
        "running": "#f59e0b",
        "done": "#22c55e",
        "error": "#ef4444",
    }

    async def _on_macro_event(self, event_name, payload):
        """Mark keys whose bindings reference a macro that changed state."""
        for prefix, mark in (
            ("macro.started.", "running"),
            ("macro.completed.", "done"),
            ("macro.cancelled.", None),
            ("macro.error.", "error"),
        ):
            if event_name.startswith(prefix):
                await self._apply_macro_mark_event(event_name[len(prefix):], mark)
                return

    async def _apply_macro_mark_event(self, macro_id, mark):
        for session in list(self._sessions.values()):
            targets = session.macro_keys.get(macro_id)
            if not targets:
                continue
            for page, key_index in targets:
                handle = session.macro_clear_handles.pop((page, key_index), None)
                if handle is not None:
                    handle.cancel()
                if mark is None:
                    session.macro_marks.pop((page, key_index), None)
                else:
                    session.macro_marks[(page, key_index)] = mark
                    if mark in ("done", "error"):
                        # Brief result flash, then back to the normal render.
                        delay = 0.8 if mark == "done" else 1.2
                        session.macro_clear_handles[(page, key_index)] = (
                            asyncio.get_event_loop().call_later(
                                delay,
                                lambda s=session, p=page, k=key_index:
                                    asyncio.ensure_future(self._clear_macro_mark(s, p, k)),
                            )
                        )
                # Page None = a locked key; it shows on every page.
                if page is None or page == session.current_page:
                    await self._render_button(session, key_index)

    async def _clear_macro_mark(self, session, page, key_index):
        session.macro_clear_handles.pop((page, key_index), None)
        if session.device_id not in self._sessions:
            return
        if (
            session.macro_marks.pop((page, key_index), None) is not None
            and (page is None or page == session.current_page)
        ):
            await self._render_button(session, key_index)

    def _apply_macro_mark(self, image, mark):
        """Border (+ loader glyph while running) showing a macro's run state."""
        color = self._MACRO_MARK_COLORS.get(mark)
        if color is None:
            return image
        try:
            out = image.copy()
            draw = ImageDraw.Draw(out)
            w, h = out.size
            draw.rectangle([0, 0, w - 1, h - 1], outline=color, width=3)
            if mark == "running":
                icon = self._render_icon("loader", color, max(16, w // 3))
                if icon is not None:
                    size = max(10, w // 4)
                    icon = icon.resize((size, size), Image.LANCZOS)
                    out.paste(icon, (w - size - 3, 3), icon)
            return out
        except Exception:
            return image

    # ──── Context Actions ────

    async def _on_context_action(self, event_name, payload):
        """Handle context action events."""
        data = payload if isinstance(payload, dict) else {}
        if "action.simulate_input" in event_name:
            await self._simulate_input(data)
        elif "action.set_live_mirror" in event_name:
            await self._set_live_mirror(data)
        elif event_name.endswith(".set_page"):
            try:
                page = int(data.get("page"))
            except (TypeError, ValueError):
                return
            for session in self._sessions_for(data.get("serial")):
                await self._change_page(session, page)
        elif event_name.endswith(".set_brightness"):
            level = _coerce_numeric(data.get("level"))
            if level is None:
                return
            level = max(0, min(100, int(level)))
            for session in self._sessions_for(data.get("serial")):
                self._set_deck_brightness(session, level)
        elif event_name.endswith(".flash_key"):
            try:
                index = int(data.get("index"))
            except (TypeError, ValueError):
                return
            try:
                times = max(1, min(10, int(data.get("times", 2))))
            except (TypeError, ValueError):
                times = 2
            for session in self._sessions_for(data.get("serial")):
                await self._flash_key(session, index, times)
        elif event_name.endswith(".show_message"):
            text = str(data.get("text", "")).strip()
            if not text:
                return
            seconds = _coerce_numeric(data.get("seconds"))
            seconds = float(seconds) if seconds is not None and seconds > 0 else 5.0
            for session in self._sessions_for(data.get("serial")):
                if session.deck and session.deck.is_visual():
                    await self._show_message(session, text, seconds)
        elif "action.identify" in event_name:
            # A serial in the payload identifies one specific deck (the deck
            # picker's per-deck Identify); without one, flash every deck.
            serial = data.get("serial")
            for session in list(self._sessions.values()):
                if serial and session.serial != serial:
                    continue
                await self._identify_deck(session)

    async def _identify_deck(self, session):
        """Flash a deck's buttons to identify which physical deck it is."""
        deck = session.deck
        if not deck or not deck.is_visual():
            return

        self.api.log(
            f"Identifying Stream Deck {session.serial} (flashing all buttons)"
        )
        key_count = deck.key_count()
        touch_keys = range(key_count, key_count + deck.touch_key_count())

        # Flash white
        white_img = self._create_button_image(session, "", "#ffffff", "#ffffff")
        native_white = PILHelper.to_native_key_format(deck, white_img)

        for flash in range(3):
            with deck:
                for k in range(key_count):
                    deck.set_key_image(k, native_white)
            for k in touch_keys:
                self._apply_key_color(session, k, "#ffffff")
            await asyncio.sleep(0.3)

            with deck:
                for k in range(key_count):
                    deck.set_key_image(k, b"\x00" * len(native_white))
            for k in touch_keys:
                self._apply_key_color(session, k, "#000000")
            await asyncio.sleep(0.3)

        # Restore current page
        await self._render_all_buttons(session)

    # ──── Helpers ────

    @staticmethod
    def _global_buttons(cfg):
        """Validated ``global_buttons`` entries from a config view.

        Same entry shape as ``buttons`` but with no ``page`` field: a locked
        key keeps one assignment on every page (page switchers, mute-all).
        """
        entries = cfg.get("global_buttons", []) if isinstance(cfg, dict) else []
        if not isinstance(entries, list):
            return []
        return [b for b in entries if isinstance(b, dict)]

    def _locked_indexes(self, cfg):
        """Key indexes reserved deck-wide by ``global_buttons`` entries."""
        return {
            b.get("index") for b in self._global_buttons(cfg)
            if b.get("index") is not None
        }

    def _get_button_assignment(self, session, page, key_index):
        """Look up the key assignment for a page/key index.

        A ``global_buttons`` entry for the index wins on every page — a
        locked key is reserved deck-wide, and a per-page entry at the same
        index never renders or fires while the lock exists. Page-scoped
        ``buttons`` entries fill the rest.
        """
        cfg = self._deck_config(session)
        for btn in self._global_buttons(cfg):
            if btn.get("index") == key_index:
                return btn
        buttons = cfg.get("buttons", [])
        for btn in buttons if isinstance(buttons, list) else []:
            if not isinstance(btn, dict):
                continue
            if btn.get("index") == key_index and btn.get("page", 0) == page:
                return btn
        return None

    @staticmethod
    def _hidapi_error_message() -> str:
        """Return a user-friendly error message for missing HIDAPI library."""
        system = platform_mod.system()
        if system == "Windows":
            return (
                "HIDAPI library not found. It should have been installed automatically. "
                "Check that plugin_repo/.deps/hidapi.dll exists. If not, reinstall "
                "the Stream Deck plugin from the community repository."
            )
        elif system == "Linux":
            return (
                "HIDAPI library not found. Run these commands on the server, "
                "then restart the plugin:\n"
                "  sudo apt-get install -y libhidapi-libusb0\n"
                "  echo 'SUBSYSTEM==\"usb\", ATTRS{idVendor}==\"0fd9\", MODE=\"0666\"' "
                "| sudo tee /etc/udev/rules.d/99-streamdeck.rules\n"
                "  sudo udevadm control --reload-rules && sudo udevadm trigger"
            )
        return "HIDAPI library not found. See the Stream Deck plugin README for install instructions."
