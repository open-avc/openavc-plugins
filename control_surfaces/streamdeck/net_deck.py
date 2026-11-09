"""
Network-attached Stream Decks: Elgato's Network Dock and the Stream Deck
Studio, which carry the deck's HID reports over raw TCP (default port 5343).

``NetDeckDevice`` is one TCP connection, presenting the device interface the
bundled library's HID transport has (open/close/is_open/connected/vendor_id/
product_id/path/write/read/write_feature/read_feature), so the library's own
device classes drive a network deck unchanged. ``DockLink`` keeps one
configured address connected: the unit itself and, for a dock, the second
connection to the deck docked in it. The plugin's watchdog calls
``DockLink.ensure`` every tick.

Two wire framings exist, told apart by the first bytes the device sends:

  Cora (Network Dock, newer firmware): every message has a 16-byte header:
    offset 0  4B  magic 43 93 8A 41
    offset 4  u16le flags  (VERBATIM 0x8000 = payload addressed to the HID
                            device behind the bridge; REQ_ACK 0x4000;
                            ACK_NAK 0x0200; RESULT 0x0100)
    offset 6  u8  hid op   (0 = write, 1 = send feature, 2 = get feature)
    offset 7  u8  reserved
    offset 8  u32le message id
    offset 12 u32le payload length, then the payload.

  Legacy (Stream Deck Studio): fixed 512-byte receive packets carrying the
  raw HID report from byte 0; writes are raw, padded to 1024 bytes.

In both, the device sends a keepalive (payload starts 01 0A, connection
number at byte 5) every few seconds, and the host must answer each one with
03 1A <connection_no> (a 32-byte Cora ACK_NAK payload, or a raw 1024-byte
packet in legacy mode). More than ~5 s without any bytes means the link is
dead. A dock reports the deck plugged into it with a "Device 2" payload
(01 0B): child vid/pid at offsets 26/28, serial at 94-125, and a dedicated
TCP port for the child at offset 126, which the client dials as a second
connection to drive the deck. Hotplug arrives as an unsolicited Device 2
payload on the primary connection.

Blocking work (connect, probe, feature reads) runs on worker threads, never
the event loop; each connection's reader and writer are threads of their own.
The library device classes built on these connections are deck_classes.py.
"""

import queue
import re
import socket
import struct
import threading
import time

NET_DEFAULT_PORT = 5343
NET_MDNS_SERVICE = "_elg._tcp.local."
NET_IDLE_TIMEOUT = 5.0     # no bytes received in this window -> link dead
NET_GET_TIMEOUT = 5.0      # feature-report read timeout
NET_CONNECT_TIMEOUT = 4.0  # TCP connect timeout
CORA_MAGIC = b"\x43\x93\x8a\x41"
CORA_FLAG_VERBATIM = 0x8000
CORA_FLAG_ACK_NAK = 0x0200
CORA_OP_WRITE = 0x00
CORA_OP_SEND_REPORT = 0x01
CORA_OP_GET_REPORT = 0x02
SDS_PACKET_LEN = 512       # legacy mode: fixed receive packet size
SDS_WRITE_LEN = 1024       # legacy mode: writes padded to this length
# The dock's own bridge endpoint reports this product id; it is not a deck.
NET_DOCK_PID = 0xFFFF

_KEEPALIVE = b"\x01\x0a"
_DEVICE2 = b"\x01\x0b"
_DEVICE2_REPORT = 0x1C
# What a deck says first, besides Cora's marker; four bytes that are neither
# are no deck protocol.
_NOT_A_DECK = "not a deck"
_RECV_TIMED_OUT = object()

# Classifying an endpoint: a unit/bridge endpoint (the dock's own port, a
# Studio's Ethernet port) answers the 0x80 identity query; a deck endpoint
# (the docked deck's own TCP port, which is what docks advertise through
# discovery) ignores 0x80 and answers the verbatim gen2 0x08, or the Mini's
# 0xA1. All three go at once and the first answer decides, as the reference
# implementation does.
_IDENTITY_PROBES = {
    0x80: (bytes((0x03, 0x80)), 0),
    0x08: (bytes((0x08,)), CORA_FLAG_VERBATIM),
    0xA1: (bytes((0xA1,)), CORA_FLAG_VERBATIM),
}
_DECK_PROBES = (0x08, 0xA1)


def cora_pack(flags, hid_op, message_id, payload):
    """Build one Cora frame (16-byte header + payload)."""
    return (
        CORA_MAGIC
        + struct.pack("<HBBII", flags, hid_op, 0, message_id, len(payload))
        + bytes(payload)
    )


class CoraStream:
    """Incremental Cora frame parser with resync on the magic marker."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        """Consume bytes; return a list of (flags, hid_op, message_id, payload)."""
        self._buf.extend(data)
        frames = []
        while True:
            idx = self._buf.find(CORA_MAGIC)
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


def parse_device2(payload):
    """Parse a Device 2 payload (01 0B). Returns None when no child is attached."""
    if len(payload) < 128 or not payload.startswith(_DEVICE2):
        return None
    if payload[4] != 0x02:  # status: 0x02 = child connected and ready
        return None
    vid, pid = struct.unpack_from("<HH", payload, 26)
    raw_serial = bytes(payload[94:125])
    serial = raw_serial.split(b"\x00", 1)[0].decode("ascii", "replace").strip()
    (port,) = struct.unpack_from("<H", payload, 126)
    return {"vid": vid, "pid": pid, "serial": serial, "port": port}


def transport_error():
    """The bundled library's TransportError, so its reader thread handles our
    failures exactly like a USB unplug. Falls back to OSError when the library
    isn't loaded (unit tests)."""
    try:
        from StreamDeck.Transport.Transport import TransportError
        return TransportError
    except Exception:
        return OSError


def _framing(head):
    """The framing the device's first bytes show: "cora", "legacy", None
    until there are enough to tell, or _NOT_A_DECK."""
    if head.startswith(CORA_MAGIC):
        return "cora"
    if head.startswith(_KEEPALIVE):
        return "legacy"
    return _NOT_A_DECK if len(head) >= 4 else None


def _recv(sock):
    """One read: the bytes, b"" once the link has ended, or _RECV_TIMED_OUT."""
    try:
        return sock.recv(65536)
    except socket.timeout:
        return _RECV_TIMED_OUT
    except OSError:
        return b""


def _sized(payload, length):
    """A report cut or zero-padded to the length the library asked for."""
    return (bytes(payload) + b"\x00" * length)[:length]


def _is_verbatim(flags):
    return flags is not None and bool(flags & CORA_FLAG_VERBATIM)


class _Inbound:
    """Bytes the reader has received and not yet handed on as payloads."""

    def __init__(self):
        self.head = bytearray()     # what arrived before the framing was known
        self._cora = CoraStream()
        self._legacy = bytearray()

    def payloads(self, mode, data):
        """Every whole payload ``data`` completes, as (payload, flags, hid_op,
        message_id); a legacy packet has no header, so those are None."""
        if mode == "cora":
            return [
                (payload, flags, hid_op, mid)
                for flags, hid_op, mid, payload in self._cora.feed(data)
            ]
        self._legacy.extend(data)
        packets = []
        while len(self._legacy) >= SDS_PACKET_LEN:
            packets.append((bytes(self._legacy[:SDS_PACKET_LEN]), None, None, None))
            del self._legacy[:SDS_PACKET_LEN]
        return packets


class NetDeckDevice:
    """One TCP connection to a network-attached deck or dock.

    A reader thread owns the socket's receive side: it answers keepalives,
    resolves pending feature-report reads, queues input reports for the
    library's poll loop, and records Device 2 (child hotplug) payloads.
    """

    is_network = True

    def __init__(self, host, port, role="deck", local_address=None):
        self._host = host
        self._port = int(port)
        self._role = role
        # api.local_address: where Settings > Network > Reach devices through
        # says a connection to this host leaves from. Asked at every connect.
        self._local_address = local_address
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
        # Back-reference set by DockLink so the plugin can find the link
        self.link = None

    # ── connection lifecycle (blocking; call from an executor) ──

    def connect(self, timeout=NET_CONNECT_TIMEOUT):
        """Open the socket and wait for the device's first keepalive (which
        also reveals the framing mode). Raises on failure."""
        ip = self._local_address(self._host) if self._local_address else None
        self._sock = socket.create_connection(
            (self._host, self._port),
            timeout=timeout,
            source_address=(ip, 0) if ip else None,
        )
        self._sock.settimeout(1.0)
        self._dead = False
        self._last_rx = time.monotonic()
        self._reader = self._start_thread(self._read_loop, "streamdeck-net")
        self._writer = self._start_thread(self._write_loop, "streamdeck-net-w")
        ok = self._connected_evt.wait(timeout + 2.0)
        # The event also fires when the reader dies (so a connect against a
        # dead link never hangs) — success means alive AND mode detected.
        if not (ok and self._is_up()):
            self.close()
            raise TimeoutError("device sent no keepalive after connect")

    def _start_thread(self, target, prefix):
        thread = threading.Thread(
            target=target, name=f"{prefix}-{self._host}:{self._port}", daemon=True,
        )
        thread.start()
        return thread

    def _is_up(self):
        """Alive with its framing known: what a connect waits for."""
        return not self._dead and self._mode is not None

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
        sock = self._sock
        inbound = _Inbound()
        while not self._dead and sock is not None:
            data = _recv(sock)
            if data is _RECV_TIMED_OUT:
                continue
            if not data:
                break
            self._last_rx = time.monotonic()
            if not self._receive(inbound, data):
                break
        self._dead = True
        self._connected_evt.set()  # unblock a connect() waiting on a dead link

    def _receive(self, inbound, data):
        """Hand one received chunk on; False when the bytes are no deck protocol."""
        if self._mode is None:
            inbound.head.extend(data)
            mode = _framing(inbound.head)
            if mode is None:
                return True
            if mode == _NOT_A_DECK:
                return False
            self._mode = mode
            data, inbound.head = bytes(inbound.head), bytearray()
        for payload, flags, hid_op, mid in inbound.payloads(self._mode, data):
            self._handle_payload(payload, flags, hid_op, mid)
        return True

    def _handle_payload(self, payload, flags, hid_op, mid):
        if len(payload) < 2:
            return
        if payload.startswith(_KEEPALIVE) and len(payload) > 4:
            self._answer_keepalive(payload, hid_op, mid)
        elif payload.startswith(_DEVICE2):
            # The dock reporting its attached deck (also hotplug events)
            self.device2 = parse_device2(payload)
            self.device2_seen = True
            self._resolve_pending(_DEVICE2_REPORT, payload)
        elif payload[0] == 0x01:
            # Input report from the deck (delivered whole, report id first)
            self._queue_input(payload)
        else:
            self._resolve_pending(self._response_id(payload, flags), payload)

    def _answer_keepalive(self, payload, hid_op, mid):
        """Answer every keepalive; the first one answered marks the link connected."""
        conn_no = payload[5] if len(payload) > 5 else 0
        ack = bytes((0x03, 0x1A, conn_no)) + b"\x00" * 29
        try:
            self._send_payload(ack, CORA_FLAG_ACK_NAK, hid_op or 0, mid or 0)
        except OSError:
            return
        self._connected_evt.set()

    def _queue_input(self, payload):
        """Queue an input report for the library's poll loop; when the queue
        is full, the oldest report makes room."""
        try:
            self._inputs.put_nowait(payload)
            return
        except queue.Full:
            pass
        try:
            self._inputs.get_nowait()
            self._inputs.put_nowait(payload)
        except queue.Empty:
            pass

    def _response_id(self, payload, flags):
        """The report a feature-report response answers. Verbatim responses
        key on their first byte; bridge ("host") responses are 0x03-prefixed
        and key on their second."""
        if self._mode == "cora" and _is_verbatim(flags):
            return payload[0]
        if payload[0] == 0x03:
            return payload[1]
        return payload[0]

    def _resolve_pending(self, report_id, payload):
        with self._pending_lock:
            slot = self._pending.pop(report_id, None)
        if slot is not None:
            slot[1] = payload
            slot[0].set()

    def _wait_for(self, report_ids, event):
        """Register a slot per report id, all woken by ``event``."""
        slots = {rid: [event, None] for rid in report_ids}
        with self._pending_lock:
            self._pending.update(slots)
        return slots

    def _stop_waiting(self, report_ids):
        with self._pending_lock:
            for rid in report_ids:
                self._pending.pop(rid, None)

    # ── shared send path ──

    def _send_payload(self, payload, flags, hid_op, mid=None):
        """Send one payload in this link's framing: a Cora frame (``mid``
        None takes the next message id), or the raw bytes padded to the
        legacy write length."""
        if self._mode == "cora":
            if mid is None:
                mid = self._next_mid()
            self._send_raw(cora_pack(flags, hid_op, mid, payload))
        else:
            self._send_raw(bytes(payload).ljust(SDS_WRITE_LEN, b"\x00"))

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
        slot = self._wait_for((report_id,), evt)[report_id]
        try:
            self._send_payload(request_payload, flags, CORA_OP_GET_REPORT)
            if not evt.wait(NET_GET_TIMEOUT) or slot[1] is None:
                raise transport_error()("feature report read timed out")
            return slot[1]
        finally:
            self._stop_waiting((report_id,))

    def read_host_feature(self, report_id):
        """Bridge-level feature read (0x03-prefixed, non-verbatim): dock/unit
        identity (0x80), Device 2 (0x1C), firmware/serial/MAC."""
        return self._get_report(bytes((0x03, report_id)), report_id, 0)

    def probe_endpoint(self, timeout=NET_GET_TIMEOUT):
        """Classify this endpoint: ``"primary"`` (a unit or bridge) or
        ``"deck"``. On "primary" the unit's vid/pid are read from the 0x80
        identity payload (offsets 12/14)."""
        # One shared event: whichever probe the unit answers wakes the wait
        # immediately (this runs on a connect worker thread, never the loop).
        answered = threading.Event()
        slots = self._wait_for(_IDENTITY_PROBES, answered)
        try:
            for payload, flags in _IDENTITY_PROBES.values():
                self._send_payload(payload, flags, CORA_OP_GET_REPORT)
            return self._await_identity(slots, answered, timeout)
        finally:
            self._stop_waiting(_IDENTITY_PROBES)

    def _await_identity(self, slots, answered, timeout):
        deadline = time.monotonic() + timeout
        while not self._dead:
            kind = self._identity(slots)
            if kind is not None:
                return kind
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            answered.wait(min(remaining, 0.25))
            answered.clear()
        raise transport_error()("unit did not answer identification")

    def _identity(self, slots):
        """"primary" or "deck" once a probe is answered (the unit's identity
        wins), else None."""
        identity = slots[0x80][1]
        if identity is None:
            deck_answered = any(slots[rid][1] is not None for rid in _DECK_PROBES)
            return "deck" if deck_answered else None
        if len(identity) >= 16:
            self._vid, self._pid = struct.unpack_from("<HH", identity, 12)
        return "primary"

    # ── the device interface the bundled library's classes consume ──

    def _require_open(self):
        if self._dead:
            raise transport_error()("network deck connection is closed")

    def open(self):
        self._require_open()

    def is_open(self):
        return not self._dead

    def connected(self):
        return (
            not self._dead
            and (time.monotonic() - self._last_rx) <= NET_IDLE_TIMEOUT
        )

    def vendor_id(self):
        return self._vid

    def product_id(self):
        return self._pid

    def path(self):
        return f"tcp:{self._host}:{self._port}"

    def write(self, payload):
        return self._write_verbatim(payload, CORA_OP_WRITE)

    def write_feature(self, payload):
        return self._write_verbatim(payload, CORA_OP_SEND_REPORT)

    def _write_verbatim(self, payload, hid_op):
        try:
            self._send_payload(payload, CORA_FLAG_VERBATIM, hid_op, 0)
        except OSError as e:
            raise transport_error()(str(e)) from e
        return len(payload)

    def read_feature(self, report_id, length):
        self._require_open()
        flags = CORA_FLAG_VERBATIM if self._mode == "cora" else 0
        payload = self._get_report(bytes((report_id,)), report_id, flags)
        return _sized(payload, length)

    def read(self, length):
        self._require_open()
        try:
            payload = self._inputs.get_nowait()
        except queue.Empty:
            return None
        return _sized(payload, length)


def classify_net_error(exc):
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


def _backoff(fail_count):
    """Seconds before the next connect after ``fail_count`` failures in a
    row: 3, 6, 12, 24, then 30."""
    return min(3.0 * (2 ** min(fail_count - 1, 4)), 30.0)


class DockLink:
    """One configured network deck entry: owns the primary TCP connection
    (keepalive, identity, Device 2 watch) and, when the unit is a dock, the
    second connection that drives the docked deck. Reconnects with capped
    exponential backoff; the plugin watchdog drives it every tick."""

    def __init__(self, host, port, local_address=None):
        self.host = host
        self.port = int(port)
        self._local_address = local_address  # api.local_address, for both connections
        self.key = f"{host}:{port}"
        self.state_key = re.sub(r"[^A-Za-z0-9_-]", "_", self.key)
        self.primary = None          # NetDeckDevice (the unit itself)
        self.child = None            # NetDeckDevice (deck docked in it)
        self.status = "connecting"
        self.fail_count = 0
        self.next_attempt = 0.0
        # The deck's OWN serial, read from the opened deck (get_serial_number)
        # — this is the deck's identity everywhere (card, state keys, the
        # config entry's `serial`). Set only once the deck is open.
        self.observed_serial = ""
        # The serial the unit advertises over the wire / mDNS (Device-2
        # payload). A Network Dock reports the deck under a DIFFERENT serial
        # than the deck reports for itself (e.g. "A00WA6111J9SAM" vs
        # "WA6111J9SAM"). This is an alias used ONLY for discovery re-resolve
        # matching — it must never become the deck's identity, or the same
        # physical deck shows up twice.
        self.advertised_serial = ""
        # True when the configured address IS a deck endpoint (docks
        # advertise the docked deck's own port via discovery) — there is no
        # separate child connection to manage in that case.
        self.is_deck_endpoint = False
        self._connecting = False
        self._resolve_attempted = False

    def remember_serials(self, serial, mdns_sn):
        """Start from the serials the deck's config entry recorded on an
        earlier connect, so discovery can find it before it answers."""
        if serial:
            self.observed_serial = serial
        if mdns_sn:
            self.advertised_serial = mdns_sn

    def answers_to(self, *recorded):
        """Every serial this deck answers to: its own, the one it advertises,
        and any ``recorded`` on its config entry."""
        return {s for s in (self.observed_serial, self.advertised_serial, *recorded) if s}

    def claim_look_up(self):
        """True once per outage, from the third failed connect in a row: the
        caller then looks the deck up by serial. The next good connect allows
        another."""
        if self.fail_count < 3 or self._resolve_attempted:
            return False
        self._resolve_attempted = True
        return True

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
        endpoint = NetDeckDevice(
            self.host, self.port, role="primary", local_address=self._local_address
        )
        endpoint.link = self
        endpoint.connect()
        try:
            is_deck = endpoint.probe_endpoint() == "deck"
            if is_deck:
                self._take_deck_identity(endpoint)
            else:
                _ask_for_docked_deck(endpoint)
        except Exception:
            endpoint.close()
            raise
        self.primary = endpoint
        self.is_deck_endpoint = is_deck
        if not is_deck:
            self._connect_child_blocking()

    def _take_deck_identity(self, endpoint):
        """On a deck endpoint: the deck's ids from the Device-2 payload on
        this socket, asked for when none has arrived yet."""
        info = endpoint.device2
        if info is None:
            info = parse_device2(endpoint.read_host_feature(_DEVICE2_REPORT))
        if info is None:
            raise transport_error()("no deck present at this address")
        endpoint._vid, endpoint._pid = info["vid"], info["pid"]
        self._note_advertised_serial(info)

    def _note_advertised_serial(self, info):
        # The Device-2 serial is the unit's ADVERTISED alias, not the deck's
        # own serial: kept only for re-resolve matching.
        if info.get("serial"):
            self.advertised_serial = info["serial"]

    def _connect_child_blocking(self):
        info = self.primary.device2 if self.primary else None
        if not info:
            return
        child = NetDeckDevice(
            self.host, info["port"], role="child", local_address=self._local_address
        )
        child.link = self
        child.connect()
        child._vid, child._pid = info["vid"], info["pid"]
        self._note_advertised_serial(info)
        self.child = child

    async def ensure(self, loop, log):
        """Watchdog tick: reconnect what's down (respecting backoff) and
        reconcile the child against the latest Device 2 report. Returns the
        transports that are up; the caller turns them into deck objects."""
        if self._connecting:
            return self._live_transports()
        if not self._primary_up():
            return await self._reconnect(loop, log)
        # On a deck endpoint there is no child connection to manage — its
        # Device-2 payload describes the deck on THIS socket, not a second
        # port to dial.
        if not self.is_deck_endpoint:
            await self._follow_docked_deck(loop, log)
        return self._live_transports()

    def _primary_up(self):
        return self.primary is not None and self.primary.connected()

    async def _reconnect(self, loop, log):
        now = time.monotonic()
        self.close()
        if now < self.next_attempt:
            return []
        self._connecting = True
        try:
            await loop.run_in_executor(None, self._connect_blocking)
        except Exception as e:
            self._connect_failed(e, now, log)
            return []
        else:
            self.fail_count = 0
            self.status = "connected"
            self._resolve_attempted = False
        finally:
            self._connecting = False
        return self._live_transports()

    def _connect_failed(self, exc, now, log):
        self.close()
        self.fail_count += 1
        self.next_attempt = now + _backoff(self.fail_count)
        self.status = classify_net_error(exc)
        log(
            f"Network deck {self.key}: {self.status} "
            f"(retry in {int(self.next_attempt - now)}s)",
            "warning",
        )

    async def _follow_docked_deck(self, loop, log):
        """Match the child connection to the dock's latest Device-2 report:
        drop it when the deck is undocked, dial it when the deck is new,
        down, or on another port."""
        info = self.primary.device2
        if info is None:
            self._drop_child()
            return
        if self._child_serves(info):
            return
        self._drop_child()
        self._connecting = True
        try:
            await loop.run_in_executor(None, self._connect_child_blocking)
        except Exception as e:
            log(
                f"Network deck {self.key}: docked deck {classify_net_error(e)}",
                "warning",
            )
        finally:
            self._connecting = False

    def _child_serves(self, info):
        """The child connection is up, on the port the dock reports."""
        child = self.child
        if child is None or not child.connected():
            return False
        return child._port == info["port"]

    def _drop_child(self):
        if self.child is not None:
            self.child.close()
            self.child = None

    def _live_transports(self):
        return [
            dev for dev in (self.primary, self.child)
            if dev is not None and _is_live_deck(dev)
        ]


def _ask_for_docked_deck(endpoint):
    """Ask a bridge for its Device-2 report: docks answer; deck-units like
    the Studio may have nothing attached."""
    try:
        endpoint.read_host_feature(_DEVICE2_REPORT)
    except Exception:
        pass


def _is_live_deck(dev):
    """Up, and a deck (the dock's own bridge endpoint is not one)."""
    return dev.connected() and dev.product_id() not in (0, NET_DOCK_PID)
