"""
Tests for the Stream Deck plugin's network decks: the Cora framing, one TCP
connection to a deck or dock (NetDeckDevice) and the link that keeps a
configured entry connected (DockLink), in net_deck.py; and the library device
classes a network deck is driven by, in deck_classes.py.

Everything here runs without a deck and without the network: sockets are
stand-ins fed scripted bytes, and the link tests swap the connection class
for a recorder. The loopback dock tests live with the rest of the plugin's
tests in test_streamdeck_plugin.py.

Run from the openavc-plugins root: pytest tests/test_streamdeck_net_deck.py -v
"""

import asyncio
import queue
import re
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))

import control_surfaces.streamdeck.deck_classes as deck_classes  # noqa: E402
import control_surfaces.streamdeck.net_deck as net_deck  # noqa: E402

KEEPALIVE = b"\x01\x0a\x00\x00\x00\x07"
ACK_FOR_7 = bytes((0x03, 0x1A, 0x07)) + b"\x00" * 29


def _device2(pid=0x0080, vid=0x0FD9, serial=b"SN1", port=20001, status=0x02):
    payload = bytearray(128)
    payload[0], payload[1], payload[4] = 0x01, 0x0B, status
    struct.pack_into("<HH", payload, 26, vid, pid)
    payload[94:94 + len(serial)] = serial
    struct.pack_into("<H", payload, 126, port)
    return bytes(payload)


class _ScriptedSocket:
    """A socket whose recv() hands out a script of chunks and errors."""

    def __init__(self, script=(), shutdown_error=None, close_error=None):
        self.script = list(script)
        self.sent = []
        self.send_error = None
        self.shutdown_error = shutdown_error
        self.close_error = close_error
        self.shut = False
        self.closed = False

    def recv(self, _n):
        if not self.script:
            return b""
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def sendall(self, data):
        if self.send_error:
            raise self.send_error
        self.sent.append(bytes(data))

    def shutdown(self, _how):
        self.shut = True
        if self.shutdown_error:
            raise self.shutdown_error

    def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


def _device(mode=None, script=()):
    dev = net_deck.NetDeckDevice("192.0.2.10", 5343)
    dev._sock = _ScriptedSocket(script)
    dev._dead = False
    dev._mode = mode
    return dev


def _queued(dev):
    out = []
    while True:
        try:
            out.append(dev._out.get_nowait())
        except queue.Empty:
            return out


def _frames(raw_list):
    frames = []
    for raw in raw_list:
        frames += net_deck.CoraStream().feed(raw)
    return frames


# ──── Cora framing ────


def test_cora_stream_drops_bytes_before_the_magic_marker():
    stream = net_deck.CoraStream()
    frame = net_deck.cora_pack(0x8000, 0x02, 9, b"\x84\x01")
    assert stream.feed(b"\xff\xee" + frame) == [(0x8000, 0x02, 9, b"\x84\x01")]


def test_cora_stream_keeps_a_marker_split_across_chunks():
    stream = net_deck.CoraStream()
    frame = net_deck.cora_pack(0, 0, 3, b"\x01\x02")
    assert stream.feed(b"\x00\x00" + frame[:2]) == []
    assert stream.feed(frame[2:10]) == []
    assert stream.feed(frame[10:]) == [(0, 0, 3, b"\x01\x02")]


def test_cora_pack_header_layout():
    frame = net_deck.cora_pack(0x0200, 0x01, 0x01020304, b"\xaa")
    assert frame[:4] == b"\x43\x93\x8a\x41"
    assert struct.unpack("<HBBII", frame[4:16]) == (0x0200, 0x01, 0, 0x01020304, 1)
    assert frame[16:] == b"\xaa"


def test_parse_device2_status_other_than_ready_is_no_child():
    assert net_deck.parse_device2(_device2(status=0x01)) is None
    assert net_deck.parse_device2(_device2(serial=b"AB\x00CD")) == {
        "vid": 0x0FD9, "pid": 0x0080, "serial": "AB", "port": 20001,
    }


# ──── The library's transport error ────


def test_transport_error_is_the_librarys_when_it_is_loaded():
    transport = pytest.importorskip("StreamDeck.Transport.Transport")
    assert net_deck.transport_error() is transport.TransportError


def test_transport_error_falls_back_to_oserror_without_the_library(monkeypatch):
    monkeypatch.setitem(sys.modules, "StreamDeck.Transport.Transport", None)
    assert net_deck.transport_error() is OSError


# ──── The reader thread ────


def test_read_loop_detects_cora_from_the_marker_and_acks_the_keepalive():
    ka = net_deck.cora_pack(0x4000, 0x01, 99, KEEPALIVE)
    dev = _device(script=[ka])
    dev._read_loop()
    assert dev._mode == "cora"
    assert _frames(_queued(dev)) == [(0x0200, 0x01, 99, ACK_FOR_7)]
    assert dev._connected_evt.is_set()
    assert dev._dead is True  # the script ran out: the peer closed


def test_read_loop_detects_legacy_from_a_keepalive_and_pads_the_ack():
    packet = KEEPALIVE.ljust(512, b"\x00")
    dev = _device(script=[packet])
    dev._read_loop()
    assert dev._mode == "legacy"
    assert _queued(dev) == [ACK_FOR_7.ljust(1024, b"\x00")]


def test_read_loop_waits_for_enough_bytes_to_tell_the_framing():
    ka = net_deck.cora_pack(0x4000, 0x01, 5, KEEPALIVE)
    dev = _device(script=[ka[:1], ka[1:3], ka[3:]])
    dev._read_loop()
    assert dev._mode == "cora"
    assert len(_queued(dev)) == 1


def test_read_loop_legacy_one_byte_then_the_rest():
    packet = KEEPALIVE.ljust(512, b"\x00")
    dev = _device(script=[packet[:1], packet[1:300], packet[300:]])
    dev._read_loop()
    assert dev._mode == "legacy"
    assert len(_queued(dev)) == 1


def test_read_loop_gives_up_on_bytes_that_are_no_deck_protocol():
    dev = _device(script=[b"HTTP/1.1 200 OK\r\n"])
    dev._read_loop()
    assert dev._mode is None
    assert dev._dead is True
    assert dev._connected_evt.is_set()
    assert _queued(dev) == []


def test_read_loop_rides_out_timeouts_and_stops_on_a_socket_error():
    ka = net_deck.cora_pack(0x4000, 0x01, 1, KEEPALIVE)
    dev = _device(script=[socket.timeout(), ka, OSError("reset"), ka])
    dev._read_loop()
    assert dev._mode == "cora"
    assert len(_queued(dev)) == 1  # the keepalive after the error never read
    assert dev._dead is True


def test_read_loop_legacy_reassembles_packets_across_chunks():
    first = KEEPALIVE.ljust(512, b"\x00")
    report = (b"\x01\x00\x00\x01" + b"\x01" * 8).ljust(512, b"\x00")
    dev = _device(script=[first + report[:100], report[100:]])
    dev._read_loop()
    assert dev._inputs.get_nowait() == report


def test_read_loop_tells_cora_from_the_four_marker_bytes_alone():
    ka = net_deck.cora_pack(0x4000, 0x01, 5, KEEPALIVE)
    dev = _device(script=[ka[:4], ka[4:]])
    dev._read_loop()
    assert dev._mode == "cora"
    assert len(_queued(dev)) == 1


def test_read_loop_stops_reading_after_four_bytes_that_are_no_deck_protocol():
    dev = _device(script=[b"\x02\x00\x00\x00", b"unread"])
    dev._read_loop()
    assert dev._mode is None
    assert dev._sock.script == [b"unread"]


def test_read_loop_legacy_handles_every_packet_in_one_chunk():
    first = KEEPALIVE.ljust(512, b"\x00")
    report = (b"\x01\x00\x00\x01" + b"\x01" * 8).ljust(512, b"\x00")
    dev = _device(script=[first + report])
    dev._read_loop()
    assert len(_queued(dev)) == 1
    assert dev._inputs.get_nowait() == report


def test_read_loop_marks_receive_time():
    dev = _device(script=[net_deck.cora_pack(0, 0, 0, KEEPALIVE)])
    dev._last_rx = 0.0
    before = time.monotonic()
    dev._read_loop()
    assert dev._last_rx >= before


# ──── Routing one payload ────


def test_handle_payload_ignores_a_payload_under_two_bytes():
    dev = _device(mode="cora")
    dev._handle_payload(b"\x01", 0, 0, 0)
    assert _queued(dev) == [] and dev._inputs.empty()
    assert not dev._connected_evt.is_set()


def test_keepalive_ack_without_a_connection_number_or_header_fields():
    dev = _device(mode="cora")
    dev._handle_payload(b"\x01\x0a\x00\x00\x00", None, None, None)
    ack = bytes((0x03, 0x1A, 0x00)) + b"\x00" * 29
    assert _frames(_queued(dev)) == [(0x0200, 0, 0, ack)]
    assert dev._connected_evt.is_set()


def test_keepalive_on_a_closed_link_is_not_acked_and_does_not_connect():
    dev = _device(mode="cora")
    dev._dead = True
    dev._handle_payload(KEEPALIVE, 0, 0, 0)
    assert _queued(dev) == []
    assert not dev._connected_evt.is_set()


def test_a_four_byte_keepalive_is_read_as_an_input_report():
    dev = _device(mode="cora")
    dev._handle_payload(b"\x01\x0a\x00\x00", 0, 0, 0)
    assert dev._inputs.get_nowait() == b"\x01\x0a\x00\x00"
    assert _queued(dev) == []


def test_device2_payload_records_the_child_and_answers_a_pending_read():
    dev = _device(mode="cora")
    evt = threading.Event()
    dev._pending[0x1C] = [evt, None]
    payload = _device2(pid=0x0084, serial=b"PLUS1", port=20002)
    dev._handle_payload(payload, 0, 0, 0)
    assert dev.device2 == {"vid": 0x0FD9, "pid": 0x0084, "serial": "PLUS1", "port": 20002}
    assert dev.device2_seen is True
    assert evt.is_set()
    assert 0x1C not in dev._pending


def test_device2_without_a_child_clears_the_record():
    dev = _device(mode="cora")
    dev.device2 = {"port": 1}
    dev._handle_payload(_device2(status=0x00), 0, 0, 0)
    assert dev.device2 is None and dev.device2_seen is True


def test_a_full_input_queue_drops_the_oldest_report():
    dev = _device(mode="cora")
    for i in range(256):
        dev._inputs.put_nowait(bytes((0x01, 0x00, i)))
    dev._handle_payload(b"\x01\x00\xff", 0, 0, 0)
    assert dev._inputs.qsize() == 256
    assert dev._inputs.get_nowait() == bytes((0x01, 0x00, 1))
    last = None
    while not dev._inputs.empty():
        last = dev._inputs.get_nowait()
    assert last == b"\x01\x00\xff"


def _pending(dev, *report_ids):
    slots = {}
    for rid in report_ids:
        slots[rid] = [threading.Event(), None]
        dev._pending[rid] = slots[rid]
    return slots


def test_verbatim_feature_response_is_keyed_on_its_first_byte():
    dev = _device(mode="cora")
    slots = _pending(dev, 0x03, 0x80)
    dev._handle_payload(b"\x03\x80\x01", 0x8000, 2, 1)
    assert slots[0x03][1] == b"\x03\x80\x01"
    assert slots[0x80][1] is None


def test_bridge_response_is_keyed_on_its_second_byte():
    dev = _device(mode="cora")
    slots = _pending(dev, 0x03, 0x80)
    dev._handle_payload(b"\x03\x80\x01", 0x0100, 2, 1)
    assert slots[0x80][1] == b"\x03\x80\x01"
    assert slots[0x03][1] is None


def test_other_responses_are_keyed_on_their_first_byte():
    dev = _device(mode="legacy")
    slots = _pending(dev, 0x06)
    dev._handle_payload(b"\x06\x00SERIAL", None, None, None)
    assert slots[0x06][1] == b"\x06\x00SERIAL"
    dev = _device(mode="cora")
    slots = _pending(dev, 0x06)
    dev._handle_payload(b"\x06\x00SERIAL", 0x0100, 2, 1)
    assert slots[0x06][1] == b"\x06\x00SERIAL"


def test_legacy_bridge_response_is_keyed_on_its_second_byte():
    dev = _device(mode="legacy")
    slots = _pending(dev, 0x03, 0x80)
    dev._handle_payload(b"\x03\x80\x01", None, None, None)
    assert slots[0x80][1] == b"\x03\x80\x01"
    assert slots[0x03][1] is None


def test_a_response_nobody_waits_for_is_dropped():
    dev = _device(mode="cora")
    dev._handle_payload(b"\x06\x00", 0x8000, 2, 1)
    assert dev._pending == {} and dev._inputs.empty()


# ──── The send path ────


def test_send_raw_on_a_closed_link_raises():
    dev = _device(mode="cora")
    dev._dead = True
    with pytest.raises(OSError, match="network deck connection is closed"):
        dev._send_raw(b"x")


def test_send_raw_with_a_full_queue_kills_the_link():
    dev = _device(mode="cora")
    for _ in range(512):
        dev._out.put_nowait(b"x")
    with pytest.raises(OSError, match="network deck stopped accepting data"):
        dev._send_raw(b"y")
    assert dev._dead is True


def test_write_loop_sends_queued_frames_then_stops_without_a_socket():
    dev = _device(mode="cora")
    sock = dev._sock
    dev._out.put_nowait(b"one")
    dev._out.put_nowait(b"two")

    def drop_socket_after_two():
        while len(sock.sent) < 2:
            time.sleep(0.01)
        dev._sock = None
        dev._out.put_nowait(b"three")

    helper = threading.Thread(target=drop_socket_after_two)
    helper.start()
    dev._write_loop()
    helper.join()
    assert sock.sent == [b"one", b"two"]
    assert dev._dead is True


def test_write_loop_stops_on_a_send_error():
    dev = _device(mode="cora")
    dev._sock.send_error = OSError("broken pipe")
    dev._out.put_nowait(b"one")
    dev._write_loop()
    assert dev._dead is True


def test_get_report_legacy_sends_the_request_padded():
    dev = _device(mode="legacy")
    sent = []

    def answer(data):
        sent.append(data)
        dev._resolve_pending(0x06, b"\x06answer")

    dev._send_raw = answer
    assert dev._get_report(b"\x06", 0x06, 0) == b"\x06answer"
    assert sent == [b"\x06".ljust(1024, b"\x00")]
    assert dev._pending == {}


def test_get_report_cora_frames_the_request():
    dev = _device(mode="cora")
    sent = []

    def answer(data):
        sent.append(data)
        dev._resolve_pending(0x06, b"\x06answer")

    dev._send_raw = answer
    dev._mid = 41
    assert dev._get_report(b"\x06", 0x06, 0x8000) == b"\x06answer"
    assert _frames(sent) == [(0x8000, 0x02, 42, b"\x06")]


def test_get_report_times_out_with_the_transport_error(monkeypatch):
    monkeypatch.setattr(net_deck, "NET_GET_TIMEOUT", 0.05)
    dev = _device(mode="cora")
    with pytest.raises(net_deck.transport_error(), match="feature report read timed out"):
        dev._get_report(b"\x06", 0x06, 0x8000)
    assert dev._pending == {}


def test_get_report_woken_by_close_is_a_timeout(monkeypatch):
    dev = _device(mode="cora")
    dev._send_raw = lambda data: dev.close()
    with pytest.raises(net_deck.transport_error(), match="feature report read timed out"):
        dev._get_report(b"\x06", 0x06, 0x8000)


def test_message_ids_wrap_at_24_bits():
    dev = _device(mode="cora")
    dev._mid = 0xFFFFFF
    assert dev._next_mid() == 0
    assert dev._next_mid() == 1


def test_read_host_feature_asks_the_bridge():
    dev = _device(mode="cora")
    calls = []
    dev._get_report = lambda payload, rid, flags: calls.append((payload, rid, flags)) or b"r"
    assert dev.read_host_feature(0x80) == b"r"
    assert calls == [(b"\x03\x80", 0x80, 0)]


# ──── Telling a dock from a deck ────


def _probe_device(mode, answers):
    """A device whose sends are answered at once from ``answers``."""
    dev = _device(mode=mode)
    sent = []

    def send(data):
        sent.append(data)
        if len(sent) == 3:
            for rid, payload in answers.items():
                dev._resolve_pending(rid, payload)

    dev._send_raw = send
    return dev, sent


def test_probe_endpoint_primary_reads_the_unit_ids():
    identity = bytearray(16)
    identity[0], identity[1] = 0x03, 0x80
    struct.pack_into("<HH", identity, 12, 0x0FD9, 0xFFFF)
    dev, sent = _probe_device("cora", {0x80: bytes(identity)})
    assert dev.probe_endpoint(timeout=1.0) == "primary"
    assert (dev._vid, dev._pid) == (0x0FD9, 0xFFFF)
    assert [f[0:2] for f in _frames(sent)] == [(0, 0x02), (0x8000, 0x02), (0x8000, 0x02)]
    assert [f[3] for f in _frames(sent)] == [b"\x03\x80", b"\x08", b"\xa1"]
    assert dev._pending == {}


def test_probe_endpoint_primary_with_a_short_identity_keeps_the_ids():
    dev, _sent = _probe_device("cora", {0x80: b"\x03\x80\x00"})
    dev._pid = 0x1234
    assert dev.probe_endpoint(timeout=1.0) == "primary"
    assert (dev._vid, dev._pid) == (0x0FD9, 0x1234)


def test_probe_endpoint_primary_wins_over_a_deck_answer():
    dev, _sent = _probe_device("cora", {0x08: b"\x08", 0x80: b"\x03\x80"})
    assert dev.probe_endpoint(timeout=1.0) == "primary"


@pytest.mark.parametrize("rid", [0x08, 0xA1])
def test_probe_endpoint_deck_answers(rid):
    dev, _sent = _probe_device("cora", {rid: bytes((rid,))})
    assert dev.probe_endpoint(timeout=1.0) == "deck"
    assert dev._pending == {}


def test_probe_endpoint_legacy_sends_padded_probes():
    dev, sent = _probe_device("legacy", {0x08: b"\x08"})
    assert dev.probe_endpoint(timeout=1.0) == "deck"
    assert sent == [
        b"\x03\x80".ljust(1024, b"\x00"),
        b"\x08".ljust(1024, b"\x00"),
        b"\xa1".ljust(1024, b"\x00"),
    ]


def test_probe_endpoint_with_no_answer_raises():
    dev, _sent = _probe_device("cora", {})
    started = time.monotonic()
    with pytest.raises(net_deck.transport_error(), match="unit did not answer identification"):
        dev.probe_endpoint(timeout=0.3)
    assert time.monotonic() - started >= 0.3
    assert dev._pending == {}


def test_probe_endpoint_on_a_dead_link_raises_at_once():
    dev, _sent = _probe_device("cora", {})
    dev._send_raw = lambda data: setattr(dev, "_dead", True)
    started = time.monotonic()
    with pytest.raises(net_deck.transport_error(), match="unit did not answer identification"):
        dev.probe_endpoint(timeout=5.0)
    assert time.monotonic() - started < 1.0


def test_probe_endpoint_wakes_when_an_answer_arrives_late():
    dev = _device(mode="cora")
    dev._send_raw = lambda data: None

    def answer_later():
        time.sleep(0.4)
        dev._resolve_pending(0xA1, b"\xa1")

    threading.Thread(target=answer_later, daemon=True).start()
    assert dev.probe_endpoint(timeout=3.0) == "deck"


# ──── The device interface the library consumes ────


def test_open_on_a_closed_link_raises():
    dev = _device(mode="cora")
    assert dev.open() is None
    dev._dead = True
    with pytest.raises(net_deck.transport_error(), match="network deck connection is closed"):
        dev.open()


def test_path_names_the_tcp_endpoint():
    assert _device().path() == "tcp:192.0.2.10:5343"


def test_connected_needs_bytes_within_the_idle_window():
    dev = _device(mode="cora")
    dev._last_rx = time.monotonic()
    assert dev.connected() is True
    dev._last_rx = time.monotonic() - net_deck.NET_IDLE_TIMEOUT - 0.5
    assert dev.connected() is False
    dev._last_rx = time.monotonic()
    dev._dead = True
    assert dev.connected() is False


@pytest.mark.parametrize("method, op", [("write", 0x00), ("write_feature", 0x01)])
def test_writes_are_verbatim_cora_frames(method, op):
    dev = _device(mode="cora")
    assert getattr(dev, method)(b"\x02\x01\x02") == 3
    assert _frames(_queued(dev)) == [(0x8000, op, 0, b"\x02\x01\x02")]


@pytest.mark.parametrize("method", ["write", "write_feature"])
def test_legacy_writes_are_padded(method):
    dev = _device(mode="legacy")
    assert getattr(dev, method)(b"\x02\x01") == 2
    assert _queued(dev) == [b"\x02\x01".ljust(1024, b"\x00")]


@pytest.mark.parametrize("method", ["write", "write_feature"])
def test_writes_on_a_closed_link_raise_the_transport_error(method):
    dev = _device(mode="cora")
    dev._dead = True
    with pytest.raises(net_deck.transport_error(), match="network deck connection is closed"):
        getattr(dev, method)(b"\x02")


@pytest.mark.parametrize("mode, flags", [("cora", 0x8000), ("legacy", 0)])
def test_read_feature_pads_and_truncates(mode, flags):
    dev = _device(mode=mode)
    calls = []
    dev._get_report = lambda payload, rid, f: calls.append((payload, rid, f)) or b"\x06abc"
    assert dev.read_feature(0x06, 6) == b"\x06abc\x00\x00"
    assert dev.read_feature(0x06, 2) == b"\x06a"
    assert calls[0] == (b"\x06", 0x06, flags)


def test_read_feature_on_a_closed_link_raises():
    dev = _device(mode="cora")
    dev._dead = True
    with pytest.raises(net_deck.transport_error(), match="network deck connection is closed"):
        dev.read_feature(0x06, 32)


def test_read_returns_one_queued_report_sized_to_the_request():
    dev = _device(mode="cora")
    assert dev.read(4) is None
    dev._inputs.put_nowait(b"\x01\x00\x01")
    dev._inputs.put_nowait(b"\x01\x00\x01\x01\x01\x01")
    assert dev.read(5) == b"\x01\x00\x01\x00\x00"
    assert dev.read(4) == b"\x01\x00\x01\x01"
    dev._dead = True
    with pytest.raises(net_deck.transport_error(), match="network deck connection is closed"):
        dev.read(4)


def test_ids_default_to_elgato_with_no_product():
    dev = net_deck.NetDeckDevice("h", "5343")
    assert (dev.vendor_id(), dev.product_id()) == (0x0FD9, 0)
    assert dev.address == "h:5343"
    assert dev._port == 5343
    assert dev.is_network is True
    assert dev.is_open() is False


def test_close_wakes_every_pending_read_and_tolerates_socket_errors():
    dev = _device(mode="cora")
    sock = _ScriptedSocket(shutdown_error=OSError("gone"), close_error=OSError("gone"))
    dev._sock = sock
    slots = _pending(dev, 0x06, 0x80)
    dev.close()
    assert sock.shut and sock.closed
    assert dev._sock is None and dev._dead is True
    assert all(slot[0].is_set() for slot in slots.values())
    assert dev._pending == {}
    dev.close()  # a second close is harmless


def test_connect_to_something_that_is_not_a_deck_times_out_and_closes():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def speak_http():
        conn, _ = server.accept()
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\n")
        time.sleep(0.5)
        conn.close()

    threading.Thread(target=speak_http, daemon=True).start()
    dev = net_deck.NetDeckDevice("127.0.0.1", port)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="device sent no keepalive after connect"):
            dev.connect(timeout=1.0)
        assert time.monotonic() - started < 2.5
        assert dev._dead is True and dev._sock is None
    finally:
        server.close()


def test_connect_to_a_deck_that_hangs_up_before_a_keepalive_fails():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def frame_then_hang_up():
        conn, _ = server.accept()
        conn.sendall(net_deck.cora_pack(0x8000, 0x00, 0, b"\x01\x00\x00\x01"))
        conn.close()

    threading.Thread(target=frame_then_hang_up, daemon=True).start()
    dev = net_deck.NetDeckDevice("127.0.0.1", port)
    try:
        with pytest.raises(TimeoutError, match="device sent no keepalive after connect"):
            dev.connect(timeout=1.0)
        assert dev._mode == "cora"
        assert dev._dead is True and dev._sock is None
    finally:
        server.close()


# ──── What a failed connection is called ────


@pytest.mark.parametrize("exc, text", [
    (ConnectionRefusedError(), "connection refused"),
    (TimeoutError(), "unreachable (no response)"),
    (socket.timeout(), "unreachable (no response)"),
    (socket.gaierror(8, "nodename nor servname"), "address not found"),
    (OSError("no route"), "unreachable"),
    (ConnectionResetError(), "unreachable"),
    (RuntimeError("no deck present at this address"), "no deck present at this address"),
    (RuntimeError(), "connection failed"),
])
def test_classify_net_error(exc, text):
    assert net_deck.classify_net_error(exc) == text


# ──── The link: connecting one configured entry ────


class _FakeEndpoint:
    """Stands in for NetDeckDevice in the link tests."""

    made = []

    def __init__(self, host, port, role="deck", local_address=None):
        self.host, self._port, self.role = host, int(port), role
        self.local_address = local_address
        self.link = None
        self.kind = "primary"
        self.device2 = None
        self.host_feature = None       # payload, or an exception to raise
        self.probe_error = None
        self.connect_error = None
        self.connected_calls = 0
        self.closed = False
        self.up = True
        self._vid, self._pid = 0x0FD9, 0
        self.host_feature_reads = []
        _FakeEndpoint.made.append(self)
        if _FakeEndpoint.setup:
            _FakeEndpoint.setup(self)

    setup = None

    def connect(self):
        self.connected_calls += 1
        if self.connect_error:
            raise self.connect_error

    def probe_endpoint(self):
        if self.probe_error:
            raise self.probe_error
        return self.kind

    def read_host_feature(self, report_id):
        self.host_feature_reads.append(report_id)
        if isinstance(self.host_feature, BaseException):
            raise self.host_feature
        return self.host_feature

    def close(self):
        self.closed = True

    def connected(self):
        return self.up and not self.closed

    def product_id(self):
        return self._pid


@pytest.fixture
def fake_endpoints(monkeypatch):
    _FakeEndpoint.made = []
    _FakeEndpoint.setup = None
    monkeypatch.setattr(net_deck, "NetDeckDevice", _FakeEndpoint)
    yield _FakeEndpoint
    _FakeEndpoint.setup = None


def _local_address(_host):
    return "10.0.0.5"


def test_connect_a_deck_endpoint_from_its_device2(fake_endpoints):
    def setup(ep):
        ep.kind = "deck"
        ep.device2 = {"vid": 0x0FD9, "pid": 0x0084, "serial": "ALIAS1", "port": 20001}

    fake_endpoints.setup = setup
    link = net_deck.DockLink("192.0.2.4", 5343, _local_address)
    link._connect_blocking()
    (ep,) = fake_endpoints.made
    assert (ep.host, ep._port, ep.role, ep.local_address) == ("192.0.2.4", 5343, "primary", _local_address)
    assert ep.link is link and ep.connected_calls == 1
    assert link.primary is ep and link.is_deck_endpoint is True and link.child is None
    assert (ep._vid, ep._pid) == (0x0FD9, 0x0084)
    assert link.advertised_serial == "ALIAS1"
    assert ep.host_feature_reads == []


def test_connect_a_deck_endpoint_asks_for_device2_when_none_arrived(fake_endpoints):
    def setup(ep):
        ep.kind = "deck"
        ep.host_feature = _device2(pid=0x009A, serial=b"")

    fake_endpoints.setup = setup
    link = net_deck.DockLink("192.0.2.4", 5343)
    link.advertised_serial = "KEPT"
    link._connect_blocking()
    (ep,) = fake_endpoints.made
    assert ep.host_feature_reads == [0x1C]
    assert ep._pid == 0x009A
    assert link.advertised_serial == "KEPT"  # an empty serial changes nothing


def test_connect_a_deck_endpoint_with_no_deck_raises_and_closes(fake_endpoints):
    def setup(ep):
        ep.kind = "deck"
        ep.host_feature = _device2(status=0x00)

    fake_endpoints.setup = setup
    link = net_deck.DockLink("192.0.2.4", 5343)
    with pytest.raises(net_deck.transport_error(), match="no deck present at this address"):
        link._connect_blocking()
    (ep,) = fake_endpoints.made
    assert ep.closed is True
    assert link.primary is None and link.is_deck_endpoint is False


def test_connect_a_failed_probe_closes_the_endpoint(fake_endpoints):
    def setup(ep):
        ep.probe_error = OSError("probe failed")

    fake_endpoints.setup = setup
    link = net_deck.DockLink("192.0.2.4", 5343)
    with pytest.raises(OSError, match="probe failed"):
        link._connect_blocking()
    assert fake_endpoints.made[0].closed is True
    assert link.primary is None


def test_connect_a_bridge_dials_the_docked_deck(fake_endpoints):
    def setup(ep):
        if ep.role == "primary":
            ep.kind = "primary"
            ep.host_feature = RuntimeError("Studio has no Device 2")
            ep.device2 = {"vid": 0x0FD9, "pid": 0x0080, "serial": "DOCKED", "port": 20007}

    fake_endpoints.setup = setup
    link = net_deck.DockLink("192.0.2.4", 5343, _local_address)
    link._connect_blocking()
    primary, child = fake_endpoints.made
    assert primary.host_feature_reads == [0x1C]
    assert link.primary is primary and link.is_deck_endpoint is False
    assert (child.host, child._port, child.role, child.local_address) == (
        "192.0.2.4", 20007, "child", _local_address,
    )
    assert child.link is link and child.connected_calls == 1
    assert (child._vid, child._pid) == (0x0FD9, 0x0080)
    assert link.child is child and link.advertised_serial == "DOCKED"


def test_connect_a_bridge_with_nothing_docked(fake_endpoints):
    link = net_deck.DockLink("192.0.2.4", 5343)
    link._connect_blocking()
    assert len(fake_endpoints.made) == 1
    assert link.primary is fake_endpoints.made[0] and link.child is None


def test_connect_child_without_a_primary_does_nothing(fake_endpoints):
    link = net_deck.DockLink("192.0.2.4", 5343)
    link._connect_child_blocking()
    assert fake_endpoints.made == [] and link.child is None


def test_connect_child_keeps_the_alias_when_the_child_has_no_serial(fake_endpoints):
    link = net_deck.DockLink("192.0.2.4", 5343)
    link.primary = _FakeEndpoint("192.0.2.4", 5343)
    link.primary.device2 = {"vid": 1, "pid": 2, "serial": "", "port": 20009}
    link.advertised_serial = "KEPT"
    link._connect_child_blocking()
    assert link.child._port == 20009 and link.advertised_serial == "KEPT"


def test_link_names_its_state_key_from_the_address():
    link = net_deck.DockLink("fe80::1%en0", "5343")
    assert link.key == "fe80::1%en0:5343"
    assert link.state_key == "fe80__1_en0_5343"
    assert link.port == 5343 and link.status == "connecting"


def test_link_close_closes_both_connections():
    link = net_deck.DockLink("192.0.2.4", 5343)
    link.primary = _FakeEndpoint("h", 1)
    link.child = _FakeEndpoint("h", 2)
    primary, child = link.primary, link.child
    link.close()
    assert primary.closed and child.closed
    assert link.primary is None and link.child is None


def test_live_transports_skip_the_dock_itself_and_dead_or_unknown_ones():
    link = net_deck.DockLink("192.0.2.4", 5343)
    link.primary = _FakeEndpoint("h", 1)
    link.child = _FakeEndpoint("h", 2)
    link.primary._pid, link.child._pid = 0xFFFF, 0x0080
    assert link._live_transports() == [link.child]
    link.primary._pid = 0x0084
    assert link._live_transports() == [link.primary, link.child]
    link.child.up = False
    assert link._live_transports() == [link.primary]
    link.primary._pid = 0
    assert link._live_transports() == []


# ──── The link: the watchdog tick ────


def _logger():
    logs = []
    return logs, lambda message, level="info": logs.append((message, level))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro(loop))
    finally:
        loop.close()


def test_ensure_while_a_connect_is_running_only_reports():
    link = net_deck.DockLink("192.0.2.4", 5343)
    link._connecting = True
    link.primary = _FakeEndpoint("h", 1)
    link.primary._pid = 0x0080
    calls = []
    link._connect_blocking = lambda: calls.append("connect")
    logs, log = _logger()
    out = _run(lambda loop: link.ensure(loop, log))
    assert out == [link.primary] and calls == [] and logs == []
    assert link._connecting is True


def test_ensure_waits_out_the_backoff():
    link = net_deck.DockLink("192.0.2.4", 5343)
    stale = _FakeEndpoint("h", 1)
    stale.up = False
    link.primary = stale
    link.next_attempt = time.monotonic() + 60
    calls = []
    link._connect_blocking = lambda: calls.append("connect")
    logs, log = _logger()
    assert _run(lambda loop: link.ensure(loop, log)) == []
    assert calls == [] and logs == []
    assert stale.closed is True and link.primary is None


def test_ensure_reconnects_and_resets_the_failure_count():
    link = net_deck.DockLink("192.0.2.4", 5343)
    link.fail_count = 4
    link.status = "unreachable"
    link._resolve_attempted = True
    fresh = _FakeEndpoint("h", 1)
    fresh._pid = 0x0084

    def connect():
        assert link._connecting is True
        link.primary = fresh

    link._connect_blocking = connect
    logs, log = _logger()
    assert _run(lambda loop: link.ensure(loop, log)) == [fresh]
    assert link.fail_count == 0 and link.status == "connected"
    assert link._resolve_attempted is False and link._connecting is False
    assert logs == []


def test_ensure_a_failed_connect_backs_off_and_says_why():
    link = net_deck.DockLink("192.0.2.4", 5343)
    half = _FakeEndpoint("h", 1)

    def connect():
        link.primary = half
        raise ConnectionRefusedError()

    link._connect_blocking = connect
    logs, log = _logger()
    before = time.monotonic()
    assert _run(lambda loop: link.ensure(loop, log)) == []
    after = time.monotonic()
    assert link.fail_count == 1 and link.status == "connection refused"
    assert before + 3.0 <= link.next_attempt <= after + 3.0
    assert half.closed is True and link.primary is None
    assert link._connecting is False
    ((message, level),) = logs
    assert level == "warning"
    assert re.fullmatch(r"Network deck 192\.0\.2\.4:5343: connection refused \(retry in [23]s\)", message)


@pytest.mark.parametrize("fails_before, delay", [(1, 6.0), (2, 12.0), (3, 24.0), (4, 30.0), (9, 30.0)])
def test_ensure_backoff_doubles_to_a_cap(fails_before, delay):
    link = net_deck.DockLink("192.0.2.4", 5343)
    link.fail_count = fails_before

    def connect():
        raise TimeoutError()

    link._connect_blocking = connect
    _logs, log = _logger()
    before = time.monotonic()
    _run(lambda loop: link.ensure(loop, log))
    after = time.monotonic()
    assert link.fail_count == fails_before + 1
    assert before + delay <= link.next_attempt <= after + delay
    assert link.status == "unreachable (no response)"


def _healthy_link(device2=None, child_port=None, child_up=True):
    link = net_deck.DockLink("192.0.2.4", 5343)
    link.primary = _FakeEndpoint("h", 5343)
    link.primary._pid = 0xFFFF
    link.primary.device2 = device2
    if child_port is not None:
        link.child = _FakeEndpoint("h", child_port)
        link.child._pid = 0x0080
        link.child.up = child_up
    return link


def test_ensure_on_a_deck_endpoint_manages_no_child():
    link = _healthy_link(device2={"vid": 1, "pid": 0x0084, "serial": "", "port": 20001})
    link.primary._pid = 0x0084
    link.is_deck_endpoint = True
    calls = []
    link._connect_child_blocking = lambda: calls.append("child")
    _logs, log = _logger()
    assert _run(lambda loop: link.ensure(loop, log)) == [link.primary]
    assert calls == []


def test_ensure_drops_the_child_when_the_deck_is_undocked():
    link = _healthy_link(device2=None, child_port=20001)
    child = link.child
    calls = []
    link._connect_child_blocking = lambda: calls.append("child")
    _logs, log = _logger()
    assert _run(lambda loop: link.ensure(loop, log)) == []
    assert child.closed is True and link.child is None and calls == []


def test_ensure_redials_a_child_that_went_down():
    link = _healthy_link(
        device2={"vid": 1, "pid": 0x0080, "serial": "", "port": 20001},
        child_port=20001, child_up=False,
    )
    old = link.child
    fresh = _FakeEndpoint("h", 20001)
    fresh._pid = 0x0080

    def connect_child():
        assert link._connecting is True and link.child is None
        link.child = fresh

    link._connect_child_blocking = connect_child
    _logs, log = _logger()
    assert _run(lambda loop: link.ensure(loop, log)) == [fresh]
    assert old.closed is True and link._connecting is False


def test_ensure_redials_when_the_deck_moves_to_another_port():
    link = _healthy_link(
        device2={"vid": 1, "pid": 0x0080, "serial": "", "port": 20002},
        child_port=20001,
    )
    old = link.child
    calls = []
    link._connect_child_blocking = lambda: calls.append("child")
    _logs, log = _logger()
    _run(lambda loop: link.ensure(loop, log))
    assert old.closed is True and calls == ["child"] and link.child is None


def test_ensure_dials_a_newly_docked_deck():
    link = _healthy_link(device2={"vid": 1, "pid": 0x0080, "serial": "", "port": 20001})
    calls = []
    link._connect_child_blocking = lambda: calls.append("child")
    _logs, log = _logger()
    _run(lambda loop: link.ensure(loop, log))
    assert calls == ["child"]


def test_ensure_leaves_a_healthy_child_alone():
    link = _healthy_link(
        device2={"vid": 1, "pid": 0x0080, "serial": "", "port": 20001},
        child_port=20001,
    )
    child = link.child
    calls = []
    link._connect_child_blocking = lambda: calls.append("child")
    _logs, log = _logger()
    assert _run(lambda loop: link.ensure(loop, log)) == [child]
    assert calls == [] and child.closed is False


def test_ensure_a_failed_child_dial_is_logged_and_the_link_stays_up():
    link = _healthy_link(device2={"vid": 1, "pid": 0x0080, "serial": "", "port": 20001})

    def connect_child():
        raise ConnectionRefusedError()

    link._connect_child_blocking = connect_child
    logs, log = _logger()
    assert _run(lambda loop: link.ensure(loop, log)) == []
    assert logs == [("Network deck 192.0.2.4:5343: docked deck connection refused", "warning")]
    assert link._connecting is False and link.primary is not None


# ──── Library device classes for network decks ────


def test_net_deck_class_needs_the_library():
    assert deck_classes.net_deck_class(0x0080, None) is None


def test_net_deck_class_maps_each_product_id():
    dm = pytest.importorskip("StreamDeck.DeviceManager")
    ids = dm.USBProductIDs
    expected = {
        ids.USB_PID_STREAMDECK_ORIGINAL: dm.StreamDeckOriginal,
        ids.USB_PID_STREAMDECK_ORIGINAL_V2: dm.StreamDeckOriginalV2,
        ids.USB_PID_STREAMDECK_MK2_SCISSOR: dm.StreamDeckOriginalV2,
        ids.USB_PID_STREAMDECK_MK2_MODULE: dm.StreamDeckOriginalV2,
        ids.USB_PID_STREAMDECK_MINI: dm.StreamDeckMini,
        ids.USB_PID_STREAMDECK_NEO: dm.StreamDeckNeo,
        ids.USB_PID_STREAMDECK_XL: dm.StreamDeckXL,
        ids.USB_PID_STREAMDECK_MK2: dm.StreamDeckOriginalV2,
        ids.USB_PID_STREAMDECK_PEDAL: dm.StreamDeckPedal,
        ids.USB_PID_STREAMDECK_MINI_MK2: dm.StreamDeckMini,
        ids.USB_PID_STREAMDECK_MINI_MK2_MODULE: dm.StreamDeckMini,
        ids.USB_PID_STREAMDECK_XL_V2: dm.StreamDeckXL,
        ids.USB_PID_STREAMDECK_XL_V2_MODULE: dm.StreamDeckXL,
        ids.USB_PID_STREAMDECK_PLUS: dm.StreamDeckPlus,
    }
    for pid, cls in expected.items():
        assert deck_classes.net_deck_class(pid, dm) is cls, hex(pid)
    assert deck_classes.net_deck_class(0x00AA, dm) is deck_classes.studio_deck_class()
    assert deck_classes.net_deck_class(ids.USB_PID_STREAMDECK_PLUS_XL, dm) is None
    assert deck_classes.net_deck_class(0x1234, dm) is None


class _ReportDevice:
    def __init__(self, reports=(), features=None):
        self.reports = list(reports)
        self.features = features or {}
        self.feature_reads = []

    def read(self, length):
        assert length == 43
        return self.reports.pop(0) if self.reports else None

    def read_feature(self, report_id, length):
        self.feature_reads.append((report_id, length))
        return self.features[report_id]

    def close(self):
        pass


def _studio(device):
    pytest.importorskip("StreamDeck.Devices.StreamDeckOriginalV2")
    return deck_classes.studio_deck_class()(device)


def test_studio_class_is_built_once_on_the_v2_protocol():
    v2 = pytest.importorskip("StreamDeck.Devices.StreamDeckOriginalV2")
    cls = deck_classes.studio_deck_class()
    assert cls is deck_classes.studio_deck_class()
    assert issubclass(cls, v2.StreamDeckOriginalV2)
    assert (cls.KEY_COUNT, cls.KEY_COLS, cls.KEY_ROWS, cls.DIAL_COUNT, cls.TOUCH_KEY_COUNT) == (32, 16, 2, 2, 0)
    assert (cls.KEY_PIXEL_WIDTH, cls.KEY_PIXEL_HEIGHT, cls.KEY_IMAGE_FORMAT) == (80, 120, "JPEG")
    assert (cls.KEY_FLIP, cls.KEY_ROTATION, cls.DECK_TYPE, cls.DECK_VISUAL) == (
        (False, False), 0, "Stream Deck Studio", True,
    )


def test_studio_reads_key_states():
    from_lib = pytest.importorskip("StreamDeck.Devices.StreamDeck")
    keys = [0] * 32
    keys[0], keys[31] = 1, 2
    report = bytes((0x01, 0x00, 0x00, 0x00)) + bytes(keys) + b"\x00" * 7
    deck = _studio(_ReportDevice([report]))
    states = deck._read_control_states()
    assert list(states) == [from_lib.ControlType.KEY]
    assert states[from_lib.ControlType.KEY] == [True] + [False] * 30 + [True]


def test_studio_reads_dial_turns_as_signed_steps():
    from_lib = pytest.importorskip("StreamDeck.Devices.StreamDeck")
    report = bytes((0x01, 0x03, 0x00, 0x00, 0x01, 0x02, 0xFE)) + b"\x00" * 36
    deck = _studio(_ReportDevice([report]))
    states = deck._read_control_states()
    assert states == {from_lib.ControlType.DIAL: {from_lib.DialEventType.TURN: [2, -2]}}


def test_studio_reads_dial_pushes_as_booleans():
    from_lib = pytest.importorskip("StreamDeck.Devices.StreamDeck")
    report = bytes((0x01, 0x03, 0x00, 0x00, 0x00, 0x01, 0x00)) + b"\x00" * 36
    deck = _studio(_ReportDevice([report]))
    states = deck._read_control_states()
    assert states == {from_lib.ControlType.DIAL: {from_lib.DialEventType.PUSH: [True, False]}}


@pytest.mark.parametrize("report", [
    None,
    bytes((0x01, 0x03, 0x00, 0x00, 0x07)) + b"\x00" * 38,   # unknown dial event
    bytes((0x01, 0x05)) + b"\x00" * 41,                     # unknown report
])
def test_studio_ignores_what_it_cannot_read(report):
    deck = _studio(_ReportDevice([report] if report is not None else []))
    assert deck._read_control_states() is None


def test_studio_serial_and_firmware_come_from_their_feature_reports():
    features = {
        0x06: b"\x06\x00\x00\x00\x00STUDIO123\x00garbage".ljust(32, b"\x00"),
        0x05: b"\x05\x00\x00\x00\x001.05.007\x00".ljust(32, b"\x00"),
    }
    device = _ReportDevice(features=features)
    deck = _studio(device)
    assert deck.get_serial_number() == "STUDIO123"
    assert deck.get_firmware_version() == "1.05.007"
    assert device.feature_reads == [(0x06, 32), (0x05, 32)]
