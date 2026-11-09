"""
Pins for the Present plugin: what it does today, in the order it does it.

These hold behaviour the feature tests in test_present_plugin.py leave open:
the order of every side effect a display operation causes (the project save,
the state writes, the output encoders, the kiosk windows, the kiosk profile
removal), start()'s bring-up sequence and its clean-up on failure, the
platform readers behind the join address, the display view's fallbacks, and
the edges of the guest routes.

Some of them pin something that only looks deliberate, such as an edit whose
local output is refused after its label and kind were already changed in
memory. They record the behaviour; they do not endorse it.

Run from the openavc-plugins root: pytest tests/test_present_pins.py -v
"""

import json
import socket
import sys
import time
import types
from pathlib import Path

import pytest

_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from integrations.present import present_plugin
    from integrations.present.present_plugin import PresentPlugin

    _PLUGIN_IMPORTABLE = True
except Exception:  # fastapi/httpx not installed in this environment
    _PLUGIN_IMPORTABLE = False

pytestmark = pytest.mark.skipif(
    not _PLUGIN_IMPORTABLE, reason="fastapi/httpx not available"
)

if _PLUGIN_IMPORTABLE:
    from test_present_plugin import (  # noqa: E402  (helpers, not tests)
        _DISPLAY,
        _DISPLAY2,
        _FakeApi,
        _FakeController,
        _FakeKiosk,
        _FakeSupervisor,
        _plugin,
    )


# ──── A recorder of every side effect, in order ────


class _Log(list):
    """One ordered list of what happened, shared by the fakes below."""


class _RecordingApi(_FakeApi):
    def __init__(self, log, config=None, state=None):
        super().__init__(config, state=state)
        self.log_entries = log
        self.routers = []
        self.guest_routers = []
        self.subscriptions = []
        self.periodic = []
        self.logged = []

    async def save_config(self, cfg):
        self.log_entries.append(("save", [d.get("id") for d in cfg.get("displays", [])]))
        await super().save_config(cfg)

    async def state_set(self, key, value):
        self.log_entries.append(("state", key, value))
        await super().state_set(key, value)

    async def event_emit(self, name, payload):
        self.log_entries.append(("event", name, payload))
        await super().event_emit(name, payload)

    def register_router(self, router):
        self.log_entries.append(("register_router",))
        self.routers.append(router)

    def register_guest_router(self, router):
        self.log_entries.append(("register_guest_router",))
        self.guest_routers.append(router)

    async def state_subscribe(self, pattern, handler):
        self.log_entries.append(("subscribe", pattern))
        self.subscriptions.append((pattern, handler))

    def create_periodic_task(self, fn, interval_seconds, name=None):
        self.log_entries.append(("periodic", name, interval_seconds))
        self.periodic.append(fn)

    def log(self, message, level="info"):
        self.logged.append((level, message))


class _RecordingKiosk(_FakeKiosk):
    def __init__(self, entries, **kwargs):
        super().__init__()
        self.log_entries = entries
        self.kwargs = kwargs

    async def sync(self, specs):
        self.log_entries.append(("kiosk_sync", {k: dict(v) for k, v in specs.items()}))
        await super().sync(specs)

    async def stop(self):
        self.log_entries.append(("kiosk_stop",))
        await super().stop()


def _recording_plugin(displays=None, presence=None, kiosk=True, monkeypatch=None):
    """A plugin wired like test_present_plugin's _ext_client, but with every
    side effect landing in one ordered log."""
    log = _Log()
    plugin = _plugin(displays)
    plugin.api = _RecordingApi(log, plugin.api._config, state=plugin.api.state)
    plugin._presence = dict(presence or {})

    async def start(display):
        log.append(("output_start", display["id"]))
        plugin._controllers[display["id"]] = _FakeController()

    async def stop(display_id):
        if display_id in plugin._controllers:
            log.append(("output_stop", display_id))
        else:
            log.append(("output_stop_none", display_id))
        plugin._controllers.pop(display_id, None)

    async def restart(display):
        log.append(("output_restart", display["id"]))
        plugin._controllers[display["id"]] = _FakeController()

    plugin._start_output = start
    plugin._stop_output = stop
    plugin._restart_output = restart
    for d in plugin._displays:
        if d.get("kind") == "stream":
            plugin._controllers[d["id"]] = _FakeController()
    if kiosk:
        plugin._kiosk = _RecordingKiosk(log)
    if monkeypatch is not None:
        monkeypatch.setattr(
            present_plugin.kiosk,
            "remove_profile",
            lambda data_dir, display_id: log.append(("remove_profile", display_id)),
        )
    app = FastAPI()
    app.include_router(plugin._build_ext_router())
    return TestClient(app), plugin, log


def _kinds(log):
    """The log with state writes folded to their key, for order checks."""
    out = []
    for entry in log:
        if entry[0] == "state":
            out.append(("state", entry[1]))
        else:
            out.append(entry)
    return out


_STREAM = {"id": "feed", "label": "Feed", "display_key": "fk", "kind": "stream", "stream_key": "sk"}
_LOCAL = {"id": "lobby", "label": "Lobby", "display_key": "lk", "kind": "browser", "local_output": "MON-1"}


def _publish_all_writes(display_ids):
    """The state writes _publish_all makes, in order, for these displays."""
    writes = [
        ("state", "code"),
        ("state", "presenters"),
        ("state", "active_presenters"),
        ("state", "displays"),
        ("state", "sources"),
    ]
    for did in display_ids:
        writes += [
            ("state", f"display.{did}.source"),
            ("state", f"display.{did}.showing"),
            ("state", f"display.{did}.output_state"),
        ]
    return writes


# ──── The order of a display operation's side effects ────


def test_add_browser_display_with_local_output_order(monkeypatch):
    client, plugin, log = _recording_plugin(displays=[_DISPLAY], monkeypatch=monkeypatch)
    r = client.post("/displays", json={"label": "Lobby", "local_output": " MON-1 "})
    assert r.status_code == 200, r.text
    stored = plugin._find_display("lobby")
    assert list(stored) == ["id", "label", "kind", "display_key", "local_output"]
    assert stored["local_output"] == "MON-1"
    assert _kinds(log) == (
        [("save", ["main", "lobby"])]
        + _publish_all_writes(["main", "lobby"])
        + [("kiosk_sync", {"lobby": {"output": "MON-1", "url": "http://127.0.0.1:8080" + plugin._display_path(stored)}})]
    )


def test_add_stream_display_order(monkeypatch):
    client, plugin, log = _recording_plugin(monkeypatch=monkeypatch)
    r = client.post("/displays", json={"label": "Feed", "kind": " STREAM "})
    assert r.status_code == 200, r.text
    stored = plugin._find_display("feed")
    assert list(stored) == ["id", "label", "kind", "display_key", "stream_key"]
    assert stored["kind"] == "stream"
    assert plugin._routing["feed"] == "auto"
    assert _kinds(log) == (
        [("save", ["feed"])]
        + _publish_all_writes(["feed"])
        + [("output_start", "feed"), ("kiosk_sync", {})]
    )


def test_add_display_refusals_change_nothing(monkeypatch):
    client, plugin, log = _recording_plugin(displays=[_DISPLAY, _LOCAL], monkeypatch=monkeypatch)
    before = json.dumps(plugin._displays, sort_keys=True)
    cases = [
        ({"label": " "}, 422, "Display name is required."),
        ({"label": "X", "kind": "hologram"}, 422, "Display kind must be 'browser' or 'stream'."),
        ({"label": "X", "display_id": "a b"}, 422, "Display ID may contain only letters, numbers, hyphens, and underscores."),
        ({"label": "X", "display_id": "whep"}, 422, "'whep' is a reserved name; pick another display ID."),
        ({"label": "X", "display_id": "main"}, 409, "A display with id 'main' already exists."),
        ({"label": "X", "kind": "stream", "local_output": "MON-2"}, 422,
         "Only a browser display can be shown on one of this server's video outputs."),
        ({"label": "X", "local_output": "MON-1"}, 409, "That output is already showing display 'Lobby'."),
    ]
    for payload, status, detail in cases:
        r = client.post("/displays", json=payload)
        assert (r.status_code, r.json()["detail"]) == (status, detail), payload
    assert log == []
    assert json.dumps(plugin._displays, sort_keys=True) == before


def test_add_display_checks_kind_before_the_id():
    client, _plugin_, _log = _recording_plugin(displays=[_DISPLAY])
    r = client.post("/displays", json={"label": "X", "kind": "nope", "display_id": "bad id"})
    assert r.json()["detail"] == "Display kind must be 'browser' or 'stream'."
    # A blank display_id derives one from the label.
    r = client.post("/displays", json={"label": "Main", "display_id": "  "})
    assert r.json()["id"] == "main_2"


def test_edit_rename_stream_display_order(monkeypatch):
    client, plugin, log = _recording_plugin(displays=[_STREAM, _DISPLAY], monkeypatch=monkeypatch)
    plugin._routing["feed"] = "alice"
    r = client.put("/displays/feed", json={"label": "Stage", "display_id": "stage"})
    assert r.status_code == 200, r.text
    assert plugin._routing == {"main": "auto", "stage": "alice"}
    assert _kinds(log) == (
        [
            ("output_stop", "feed"),
            ("state", "display.feed.source"),
            ("state", "display.feed.showing"),
            ("state", "display.feed.output_state"),
            ("remove_profile", "feed"),
            ("save", ["stage", "main"]),
        ]
        + _publish_all_writes(["stage", "main"])
        + [("output_start", "stage"), ("kiosk_sync", {})]
    )
    # The cleared keys were written as None.
    assert ("state", "display.feed.source", None) in log


def test_edit_rename_browser_display_still_stops_its_output(monkeypatch):
    client, _plugin_, log = _recording_plugin(displays=[_LOCAL], monkeypatch=monkeypatch)
    r = client.put("/displays/lobby", json={"label": "Lobby", "display_id": "hall"})
    assert r.status_code == 200, r.text
    assert _kinds(log)[0] == ("output_stop_none", "lobby")
    # A browser display after the edit: its output is stopped again, by its new id.
    assert ("output_stop_none", "hall") in log
    assert _kinds(log)[-1][0] == "kiosk_sync"


def test_edit_kind_change_orders(monkeypatch):
    client, plugin, log = _recording_plugin(displays=[_LOCAL], monkeypatch=monkeypatch)
    r = client.put("/displays/lobby", json={"label": "Lobby", "kind": "stream"})
    assert r.status_code == 200, r.text
    stored = plugin._find_display("lobby")
    assert "local_output" not in stored and stored["stream_key"]
    assert list(stored) == ["id", "label", "display_key", "kind", "stream_key"]
    assert _kinds(log) == (
        [("save", ["lobby"])] + _publish_all_writes(["lobby"])
        + [("output_start", "lobby"), ("kiosk_sync", {})]
    )
    log.clear()
    r = client.put("/displays/lobby", json={"label": "Lobby", "kind": "browser"})
    assert r.status_code == 200, r.text
    # The stream key stays on the record after the change back.
    assert plugin._find_display("lobby")["stream_key"]
    assert _kinds(log) == (
        [("save", ["lobby"])] + _publish_all_writes(["lobby"])
        + [("output_stop", "lobby"), ("kiosk_sync", {})]
    )


def test_edit_keeps_kind_and_local_output_when_not_sent(monkeypatch):
    client, plugin, _log = _recording_plugin(displays=[_LOCAL], monkeypatch=monkeypatch)
    r = client.put("/displays/lobby", json={"label": "New Lobby"})
    assert r.status_code == 200, r.text
    assert plugin._find_display("lobby") == {**_LOCAL, "label": "New Lobby"}
    # A stored kind of None reads as browser for the edit.
    client, plugin, _log = _recording_plugin(
        displays=[{"id": "x", "label": "X", "display_key": "k"}], monkeypatch=monkeypatch
    )
    r = client.put("/displays/x", json={"label": "X"})
    assert plugin._find_display("x")["kind"] == "browser"


def test_edit_local_output_to_itself_is_allowed(monkeypatch):
    client, plugin, _log = _recording_plugin(displays=[_LOCAL], monkeypatch=monkeypatch)
    r = client.put("/displays/lobby", json={"label": "Lobby", "local_output": "MON-1"})
    assert r.status_code == 200, r.text
    r = client.put("/displays/lobby", json={"label": "Lobby", "local_output": ""})
    assert "local_output" not in plugin._find_display("lobby")


def test_edit_refusals_before_any_change(monkeypatch):
    client, plugin, log = _recording_plugin(displays=[_DISPLAY, _DISPLAY2], monkeypatch=monkeypatch)
    before = json.dumps(plugin._displays, sort_keys=True)
    cases = [
        ("/displays/ghost", {"label": "X"}, 404, "No display with id 'ghost'."),
        ("/displays/main", {"label": "  "}, 422, "Display name is required."),
        ("/displays/main", {"label": "X", "kind": "nope"}, 422, "Display kind must be 'browser' or 'stream'."),
        ("/displays/main", {"label": "X", "display_id": "a/b"}, 422, None),
        ("/displays/main", {"label": "X", "display_id": "a b"}, 422,
         "Display ID may contain only letters, numbers, hyphens, and underscores."),
        ("/displays/main", {"label": "X", "display_id": "status"}, 422,
         "'status' is a reserved name; pick another display ID."),
        ("/displays/main", {"label": "X", "display_id": "overflow"}, 409,
         "A display with id 'overflow' already exists."),
    ]
    for path, payload, status, detail in cases:
        r = client.put(path, json=payload)
        assert r.status_code == status, (path, payload)
        if detail is not None:
            assert r.json()["detail"] == detail
    assert log == []
    assert json.dumps(plugin._displays, sort_keys=True) == before


def test_edit_local_output_refusal_after_label_and_kind_changed(monkeypatch):
    """A refused local output is checked after the label, kind and stream
    key were written to the record in memory; nothing is saved or synced."""
    client, plugin, log = _recording_plugin(
        displays=[_DISPLAY, _LOCAL], monkeypatch=monkeypatch
    )
    r = client.put("/displays/main", json={"label": "Changed", "local_output": "MON-1"})
    assert (r.status_code, r.json()["detail"]) == (409, "That output is already showing display 'Lobby'.")
    assert plugin._find_display("main")["label"] == "Changed"
    r = client.put("/displays/main", json={"label": "Again", "kind": "stream", "local_output": "MON-9"})
    assert r.status_code == 422
    stored = plugin._find_display("main")
    assert stored["label"] == "Again" and stored["kind"] == "stream" and stored["stream_key"]
    assert log == []


def test_delete_order(monkeypatch):
    client, plugin, log = _recording_plugin(displays=[_STREAM, _LOCAL], monkeypatch=monkeypatch)
    r = client.delete("/displays/feed")
    assert r.json() == {"status": "deleted", "display_id": "feed"}
    assert _kinds(log) == (
        [
            ("output_stop", "feed"),
            ("save", ["lobby"]),
            ("state", "display.feed.source"),
            ("state", "display.feed.showing"),
            ("state", "display.feed.output_state"),
        ]
        + _publish_all_writes(["lobby"])
        + [
            ("kiosk_sync", {"lobby": {"output": "MON-1", "url": "http://127.0.0.1:8080" + plugin._display_path(_LOCAL)}}),
            ("remove_profile", "feed"),
        ]
    )
    assert "feed" not in plugin._routing


def test_regenerate_key_order_and_404(monkeypatch):
    client, plugin, log = _recording_plugin(displays=[_LOCAL], monkeypatch=monkeypatch)
    r = client.post("/displays/lobby/regenerate_key")
    new_key = plugin._find_display("lobby")["display_key"]
    assert new_key != "lk" and r.json()["display_key"] == new_key
    assert _kinds(log) == [
        ("save", ["lobby"]),
        ("kiosk_sync", {"lobby": {"output": "MON-1", "url": f"http://127.0.0.1:8080/present/display/lobby?key={new_key}"}}),
    ]
    r = client.post("/displays/ghost/regenerate_key")
    assert (r.status_code, r.json()["detail"]) == (404, "No display with id 'ghost'.")


def test_regenerate_stream_key_order(monkeypatch):
    client, plugin, log = _recording_plugin(displays=[_STREAM], monkeypatch=monkeypatch)
    r = client.post("/displays/feed/regenerate_stream_key")
    assert r.status_code == 200
    assert _kinds(log) == [("save", ["feed"]), ("output_restart", "feed")]
    r = client.post("/displays/ghost/regenerate_stream_key")
    assert r.json()["detail"] == "No display with id 'ghost'."
    client, _p, _l = _recording_plugin(displays=[_DISPLAY])
    r = client.post("/displays/main/regenerate_stream_key")
    assert r.json()["detail"] == "Only stream displays have a stream key."


def test_route_route_order_and_status_mapping(monkeypatch):
    client, plugin, log = _recording_plugin(
        displays=[_STREAM], presence={"alice": 1}, monkeypatch=monkeypatch
    )
    r = client.post("/displays/feed/route", json={"source": "alice"})
    assert r.status_code == 200 and r.json()["showing"] == "alice"
    assert _kinds(log) == [
        ("state", "display.feed.source"),
        ("event", "route_changed", {"display": "feed", "source": "alice"}),
        ("state", "display.feed.source"),
        ("state", "display.feed.showing"),
        ("state", "display.feed.output_state"),
    ]
    assert plugin._controllers["feed"].shown == ["alice"]
    log.clear()
    # The same take again is not a change.
    assert client.post("/displays/feed/route", json={"source": "alice"}).status_code == 200
    assert log == []
    r = client.post("/displays/ghost/route", json={"source": "alice"})
    assert (r.status_code, r.json()["detail"]) == (404, "No display with id 'ghost'.")
    r = client.post("/displays/feed/route", json={"source": "x y"})
    assert (r.status_code, r.json()["detail"]) == (422, "Source must be 'auto' or a presenter name.")


def test_outputs_route_maps_in_use():
    client, plugin, _log = _recording_plugin(displays=[_LOCAL, _DISPLAY])
    body = client.get("/outputs").json()
    assert body["outputs"][0]["in_use_by"] == "lobby"


def test_status_route_shape():
    client, plugin, _log = _recording_plugin(displays=[_DISPLAY], presence={"bob": 5})
    plugin._labels["bob"] = "Bob B"
    body = client.get("/status").json()
    assert body == {
        "running": True,
        "mediamtx_version": "1.18.2",
        "space_name": "",
        "code": plugin._current_code(),
        "join_url": "192.0.2.10:8080/present",
        "presenters": [{"name": "bob", "label": "Bob B", "since": 5}],
        "active_presenters": 1,
        "sources": [
            {"value": "auto", "label": "Auto (active presenter)"},
            {"value": "bob", "label": "Bob B"},
        ],
        "display_ids": ["main"],
    }
    plugin._supervisor = _FakeSupervisor(running=False)
    assert client.get("/status").json()["running"] is False
    plugin._supervisor = None
    assert client.get("/status").json()["running"] is False


# ──── The display view ────


def test_display_view_fallbacks():
    plugin = _plugin(displays=[_LOCAL, _STREAM, {"id": "bare", "display_key": "b"}])
    plugin._kiosk = None
    assert plugin._display_view(plugin._find_display("lobby")) == {
        "id": "lobby", "label": "Lobby", "kind": "browser", "display_key": "lk",
        "display_path": "/present/display/lobby?key=lk", "source": "auto",
        "showing": "", "output_state": "idle", "local_output": "MON-1",
        "local_state": "stopped", "local_output_name": "", "local_output_connected": False,
    }
    # A stream display with no controller: "error" while running, "stopped" before start.
    view = plugin._display_view(plugin._find_display("feed"))
    assert view["encoder_state"] == "error" and "local_output" not in view
    assert view["stream_path"] == "out/feed-sk"
    plugin._supervisor = None
    assert plugin._display_view(plugin._find_display("feed"))["encoder_state"] == "stopped"
    bare = plugin._display_view(plugin._find_display("bare"))
    assert bare["label"] == "bare" and bare["kind"] == "browser" and bare["local_output"] == ""
    assert list(bare) == ["id", "label", "kind", "display_key", "display_path", "source",
                          "showing", "output_state", "local_output"]


def test_display_view_with_kiosk_and_controller():
    plugin = _plugin(displays=[_LOCAL, _STREAM], )
    plugin._kiosk = _FakeKiosk()
    plugin._kiosk.states["lobby"] = "running"
    plugin._controllers["feed"] = _FakeController(state="live")
    plugin._presence = {"zed": 1}
    view = plugin._display_view(plugin._find_display("lobby"))
    assert (view["local_state"], view["local_output_name"], view["local_output_connected"]) == (
        "running", "Projector", True
    )
    assert view["showing"] == "zed" and view["output_state"] == "live"
    stream = plugin._display_view(plugin._find_display("feed"))
    assert list(stream) == ["id", "label", "kind", "display_key", "display_path", "source",
                            "showing", "output_state", "stream_path", "rtsp_port", "srt_port",
                            "encoder_state"]
    assert stream["encoder_state"] == "live"


def test_display_view_of_a_stream_record_holding_a_local_output():
    """Only reachable in memory (load drops it); the view still reports the window."""
    plugin = _plugin(displays=[{**_STREAM, "local_output": "MON-1"}])
    plugin._kiosk = None
    view = plugin._display_view(plugin._displays[0])
    assert "local_output" not in view and view["local_state"] == "stopped"


# ──── Loading displays ────


@pytest.mark.asyncio
async def test_load_displays_with_a_non_list_keeps_the_old_routing():
    plugin = _plugin(displays=[_DISPLAY])
    plugin.api._config["displays"] = {"not": "a list"}
    plugin._routing = {"old": "alice"}
    await plugin._load_displays()
    assert plugin._displays == [] and plugin._routing == {"old": "alice"}
    assert plugin.api.saved == []


@pytest.mark.asyncio
async def test_load_displays_unchanged_saves_nothing_and_resets_routing():
    full = {"id": "a", "label": "A", "display_key": "k", "kind": "browser"}
    plugin = _plugin()
    plugin.api._config["displays"] = [full, "junk", {"id": " "}, {"id": "auto", "display_key": "k"}]
    plugin._routing = {"a": "bob"}
    await plugin._load_displays()
    assert plugin.api.saved == []
    assert plugin._routing == {"a": "auto"}
    assert [d["id"] for d in plugin._displays] == ["a"]


@pytest.mark.asyncio
async def test_load_displays_repairs_in_order_and_saves_once():
    plugin = _plugin()
    plugin.api._config["displays"] = [
        {"id": " s ", "kind": "stream", "local_output": "M1"},
        {"id": "b1", "display_key": "k", "kind": "browser", "local_output": "M2"},
        {"id": "b2", "display_key": "k", "kind": "odd", "local_output": "M2"},
    ]
    plugin.api._config["rooms"] = ["old"]
    await plugin._load_displays()
    s, b1, b2 = plugin._displays
    assert s["id"] == " s "  # the stripped id is checked; the stored one is kept
    assert list(s) == ["id", "kind", "display_key", "stream_key"]
    assert b1["local_output"] == "M2" and "local_output" not in b2
    assert b2["kind"] == "browser"
    assert len(plugin.api.saved) == 1 and "rooms" not in plugin.api.saved[0]
    assert plugin._routing == {" s ": "auto", "b1": "auto", "b2": "auto"}


# ──── start() ────


class _RecordingSupervisor:
    instances = []

    def __init__(self, cmd, *, name, log, on_status, on_circuit_break, task_factory, _log=None):
        self.cmd = cmd
        self.name = name
        self.on_status = on_status
        self.on_circuit_break = on_circuit_break
        self.running = False
        self.pid = 7
        _RecordingSupervisor.instances.append(self)

    async def start(self):
        _RecordingSupervisor.log.append(("supervisor_start",))
        self.running = True

    async def stop(self):
        _RecordingSupervisor.log.append(("supervisor_stop",))
        self.running = False


class _RecordingCard:
    def __init__(self, path):
        _RecordingSupervisor.log.append(("card", Path(path).name))

    def write_code(self, code):
        _RecordingSupervisor.log.append(("card_code",))

    def write_space(self, space):
        _RecordingSupervisor.log.append(("card_space", space))

    def write_join(self, join):
        _RecordingSupervisor.log.append(("card_join", join))


def _start_harness(tmp_path, monkeypatch, displays=None, ready=True, deps=("mediamtx", "ffmpeg")):
    log = _Log()
    _RecordingSupervisor.log = log
    _RecordingSupervisor.instances = []
    plugin = PresentPlugin()
    api = _RecordingApi(log, {"displays": [dict(d) for d in (displays or [])]})
    api.data_dir = tmp_path
    plugin._join_address.detect_local_ip = lambda: "192.0.2.10"
    plugin._join_address.http_port = lambda: 8080
    plugin._join_address.port80_active = lambda: False
    plugin._join_address.tls_state = lambda: (False, 0)
    plugin._join_address.redirect_http_enabled = lambda: False
    monkeypatch.setattr(
        PresentPlugin, "_resolve_dep",
        staticmethod(lambda name: (tmp_path / name) if name in deps else None),
    )
    monkeypatch.setattr(
        PresentPlugin, "_ensure_executable",
        staticmethod(lambda p: log.append(("chmod", Path(p).name))),
    )
    monkeypatch.setattr(present_plugin, "SidecarSupervisor", _RecordingSupervisor)
    monkeypatch.setattr(present_plugin.output, "IdleCard", _RecordingCard)
    monkeypatch.setattr(present_plugin.output, "find_font", lambda: "font.ttf")
    monkeypatch.setattr(
        present_plugin.kiosk, "detect_environment",
        lambda plugin_dir, data_dir, log_fn: log.append(("detect_environment",)) or "env",
    )
    monkeypatch.setattr(
        present_plugin.kiosk, "KioskManager",
        lambda **kw: _RecordingKiosk(log, **kw),
    )
    calls = {"api_get": 0}

    async def api_get(path):
        calls["api_get"] += 1
        log.append(("api_get", path))
        return None  # nothing answers before our sidecar starts

    async def wait_ready():
        log.append(("wait_ready",))
        return ready

    async def start_output(display):
        log.append(("output_start", display["id"]))

    plugin._api_get = api_get
    plugin._wait_until_ready = wait_ready
    plugin._start_output = start_output
    return plugin, api, log


@pytest.mark.asyncio
async def test_start_brings_everything_up_in_order(tmp_path, monkeypatch):
    plugin, api, log = _start_harness(
        tmp_path, monkeypatch, displays=[_STREAM, _LOCAL, _DISPLAY]
    )
    await plugin.start(api)
    sup = _RecordingSupervisor.instances[0]
    assert sup.cmd == [str(tmp_path / "mediamtx"), str(tmp_path / "mediamtx.yml")]
    assert sup.name == "mediamtx"
    assert sup.on_status == plugin._on_sidecar_status
    assert sup.on_circuit_break == plugin._on_sidecar_circuit_break
    assert (tmp_path / "mediamtx.yml").read_text().startswith("# Generated by the OpenAVC Present plugin")
    assert len((tmp_path / "sidecar.auth").read_text()) == 48
    publish = []
    for e in _publish_all_writes(["feed", "lobby", "main"]):
        publish.append(e)
    assert _kinds(log) == [
        ("chmod", "mediamtx"),
        ("chmod", "ffmpeg"),
        ("api_get", "/v3/paths/list"),
        ("state", "running"),
        ("state", "error"),
        ("state", "sidecar"),
        ("supervisor_start",),
        ("wait_ready",),
        ("state", "running"),
        ("register_router",),
        ("register_guest_router",),
        ("save", ["feed", "lobby", "main"]),  # main had no kind: repaired
        ("card", "card"),
        ("card_code",),
        ("card_space", ""),
        ("card_join", "192.0.2.10:8080/present"),
        *publish,
        ("output_start", "feed"),
        ("detect_environment",),
        ("kiosk_sync", {"lobby": {"output": "MON-1", "url": "http://127.0.0.1:8080/present/display/lobby?key=lk"}}),
        ("subscribe", "plugin.present.display.*.source"),
        ("periodic", "presence_poll", 2.0),
    ]
    assert [e[2] for e in log if e[0] == "state" and e[1] in ("running", "error", "sidecar")][:4] == [
        False, "", "starting", True
    ]
    assert api.logged[-1] == ("info", "Present started: MediaMTX 1.18.2 on 127.0.0.1:9998, 3 display(s)")
    assert plugin._font == "font.ttf"
    assert plugin._kiosk.kwargs["environment"] == "env"
    assert plugin._kiosk.kwargs["browser_resolver"] == plugin._resolve_browser
    assert plugin._kiosk.kwargs["data_dir"] == tmp_path
    assert api.periodic == [plugin._poll]
    assert api.subscriptions[0][1] == plugin._on_source_write


@pytest.mark.parametrize(
    "deps, missing",
    [((), "MediaMTX"), (("ffmpeg",), "MediaMTX"), (("mediamtx",), "FFmpeg")],
)
@pytest.mark.asyncio
async def test_start_refuses_without_a_binary(tmp_path, monkeypatch, deps, missing):
    plugin, api, log = _start_harness(tmp_path, monkeypatch, deps=deps)
    with pytest.raises(present_plugin.PluginRefusal) as excinfo:
        await plugin.start(api)
    msg = (
        f"{missing} binary not found in plugin_repo/.deps/. Native "
        "dependencies install when the plugin is installed from the "
        "community repository; reinstall the Present plugin."
    )
    assert str(excinfo.value) == msg
    assert log == [("state", "running", False), ("state", "error", msg)]
    assert not (tmp_path / "mediamtx.yml").exists()


@pytest.mark.asyncio
async def test_start_refusal_when_the_port_answers_is_exact(tmp_path, monkeypatch):
    plugin, api, log = _start_harness(tmp_path, monkeypatch)

    async def answers(path):
        return {"items": []}

    plugin._api_get = answers
    with pytest.raises(present_plugin.PluginRefusal) as excinfo:
        await plugin.start(api)
    msg = (
        "Another process is already using this plugin's media ports "
        "(something answers on 127.0.0.1:9998) — usually a "
        "MediaMTX left over from an unclean shutdown. Close it (or "
        "restart this computer), then enable the plugin again."
    )
    assert str(excinfo.value) == msg
    assert _kinds(log)[-2:] == [("state", "running"), ("state", "error")]
    assert log[-1] == ("state", "error", msg)
    assert (tmp_path / "mediamtx.yml").exists()


@pytest.mark.asyncio
async def test_start_not_ready_stops_the_sidecar_and_raises(tmp_path, monkeypatch):
    plugin, api, log = _start_harness(tmp_path, monkeypatch, ready=False)
    with pytest.raises(RuntimeError) as excinfo:
        await plugin.start(api)
    assert type(excinfo.value) is RuntimeError  # not a refusal
    assert str(excinfo.value) == "MediaMTX did not respond on 127.0.0.1:9998 within 10s"
    assert _kinds(log)[-3:] == [("supervisor_start",), ("wait_ready",), ("supervisor_stop",)]
    assert plugin._supervisor is None and plugin._kiosk is None


@pytest.mark.asyncio
async def test_start_failure_after_the_kiosk_stops_kiosk_then_sidecar(tmp_path, monkeypatch):
    plugin, api, log = _start_harness(tmp_path, monkeypatch)

    async def boom(pattern, handler):
        log.append(("subscribe_failed",))
        raise ValueError("subscribe failed")

    api.state_subscribe = boom
    with pytest.raises(ValueError, match="subscribe failed"):
        await plugin.start(api)
    assert _kinds(log)[-3:] == [("subscribe_failed",), ("kiosk_stop",), ("supervisor_stop",)]
    assert plugin._supervisor is None and plugin._kiosk is None


@pytest.mark.asyncio
async def test_stop_order_and_health(tmp_path, monkeypatch):
    plugin, api, log = _start_harness(tmp_path, monkeypatch)
    await plugin.start(api)
    assert await plugin.health_check() == {"status": "ok", "message": "MediaMTX running (pid 7)"}
    plugin._controllers = {"a": object(), "b": object()}

    async def stop_output(display_id):
        log.append(("output_stop", display_id))
        plugin._controllers.pop(display_id)

    plugin._stop_output = stop_output
    log.clear()
    await plugin.stop()
    assert log == [("kiosk_stop",), ("output_stop", "a"), ("output_stop", "b"), ("supervisor_stop",)]
    assert plugin._kiosk is None and plugin._supervisor is None
    assert await plugin.health_check() == {"status": "error", "message": "MediaMTX sidecar is not running"}
    await plugin.stop()  # a second stop is harmless


@pytest.mark.asyncio
async def test_sidecar_callbacks():
    log = _Log()
    plugin = PresentPlugin()
    plugin.api = _RecordingApi(log)
    await plugin._on_sidecar_status("running")
    await plugin._on_sidecar_status("restarting")
    await plugin._on_sidecar_status("failed")
    await plugin._on_sidecar_circuit_break("too many crashes")
    assert log == [
        ("state", "sidecar", "running"),
        ("state", "sidecar", "restarting"),
        ("state", "running", False),
        ("state", "sidecar", "failed"),
        ("state", "running", False),
        ("state", "running", False),
        ("state", "error", "too many crashes"),
        ("event", "error", {"reason": "too many crashes"}),
    ]


# ──── The sidecar restart on an address change ────


@pytest.mark.asyncio
async def test_restart_sidecar_bounces_outputs_around_the_sidecar(tmp_path):
    log = _Log()
    plugin = _plugin(displays=[_STREAM, _DISPLAY])
    plugin.api = _RecordingApi(log, plugin.api._config)
    plugin._config_path = tmp_path / "mediamtx.yml"
    plugin._controllers = {"feed": object()}

    class Sup:
        async def stop(self):
            log.append(("supervisor_stop",))

        async def start(self):
            log.append(("supervisor_start", plugin._config_path.exists()))

    plugin._supervisor = Sup()

    async def stop_output(display_id):
        log.append(("output_stop", display_id))
        plugin._controllers.pop(display_id)

    async def start_output(display):
        log.append(("output_start", display["id"]))

    plugin._stop_output = stop_output
    plugin._start_output = start_output

    async def ready():
        return True

    plugin._wait_until_ready = ready
    await plugin._restart_sidecar()
    assert log == [("output_stop", "feed"), ("supervisor_stop",), ("supervisor_start", True), ("output_start", "feed")]

    log.clear()

    async def not_ready():
        return False

    plugin._wait_until_ready = not_ready
    await plugin._restart_sidecar()
    assert log == [("supervisor_stop",), ("supervisor_start", True), ("state", "running", False)]


# ──── The platform readers behind the join address ────


def _fresh_plugin():
    plugin = PresentPlugin()
    plugin.api = _FakeApi({})
    return plugin


def _fake_openavc(monkeypatch, config=None, port80=None, raise_config=False):
    pkg = types.ModuleType("openavc")
    pkg.__path__ = []
    sc = types.ModuleType("openavc.system_config")

    class Cfg:
        def get(self, section, key):
            return (config or {}).get(section, {}).get(key)

    def get_system_config():
        if raise_config:
            raise RuntimeError("no config")
        return Cfg()

    sc.get_system_config = get_system_config
    rf = types.ModuleType("openavc.runtime_flags")
    if port80 is not None:
        rf.port80_active = port80
    pkg.system_config = sc
    pkg.runtime_flags = rf
    monkeypatch.setitem(sys.modules, "openavc", pkg)
    monkeypatch.setitem(sys.modules, "openavc.system_config", sc)
    monkeypatch.setitem(sys.modules, "openavc.runtime_flags", rf)


def test_platform_readers_read_the_system_config(monkeypatch):
    _fake_openavc(monkeypatch, {
        "network": {"http_port": "9090"},
        "tls": {"enabled": True, "port": "", "redirect_http": 1},
    }, port80=1)
    p = _fresh_plugin()
    assert p._join_address.http_port() == 9090
    assert p._join_address.tls_state() == (True, 8443)
    assert p._join_address.redirect_http_enabled() is True
    assert p._join_address.port80_active() is True
    _fake_openavc(monkeypatch, {"tls": {"enabled": True, "port": "7443"}})
    assert p._join_address.tls_state() == (True, 7443)
    _fake_openavc(monkeypatch, {"tls": {"enabled": True, "port": "x"}})
    assert p._join_address.tls_state() == (False, 0)
    _fake_openavc(monkeypatch, {"tls": {"enabled": 0, "port": "7443"}})
    assert p._join_address.tls_state() == (False, 0)


def test_platform_readers_fall_back(monkeypatch):
    _fake_openavc(monkeypatch, raise_config=True)
    p = _fresh_plugin()
    assert p._join_address.http_port() == 8080
    assert p._join_address.tls_state() == (False, 0)
    assert p._join_address.redirect_http_enabled() is False
    assert p._join_address.port80_active() is False  # the flag is missing
    _fake_openavc(monkeypatch, {"network": {"http_port": None}})
    assert p._join_address.http_port() == 8080


def test_detect_local_ip_caches_and_falls_back(monkeypatch):
    opened = []

    class FakeSocket:
        def __init__(self, family, kind):
            opened.append((family, kind))

        def settimeout(self, t):
            assert t == 0.5

        def connect(self, addr):
            assert addr == ("8.8.8.8", 80)

        def getsockname(self):
            return ("10.1.2.3", 5555)

        def close(self):
            opened.append("closed")

    monkeypatch.setattr(socket, "socket", FakeSocket)
    p = _fresh_plugin()
    assert p._join_address.detect_local_ip() == "10.1.2.3"
    assert p._join_address.detect_local_ip() == "10.1.2.3"
    assert opened == [(socket.AF_INET, socket.SOCK_DGRAM), "closed"]
    # Older than the cache window: resolved again.
    p._join_address._detected = ("10.1.2.3", time.monotonic() - 61)
    p._join_address.detect_local_ip()
    assert len(opened) == 4

    class NoNetwork(FakeSocket):
        def connect(self, addr):
            raise OSError("unreachable")

    monkeypatch.setattr(socket, "socket", NoNetwork)
    p._join_address._detected = ("", 0.0)
    assert p._join_address.detect_local_ip() == "127.0.0.1"
    assert p._join_address._detected[0] == "127.0.0.1"


def test_advertised_ips_edges(monkeypatch):
    p = _fresh_plugin()
    p._join_address.detect_local_ip = lambda: "192.0.2.10"
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, family: [])
    assert p._join_address.advertised_ips("empty.example", p.api.log) == ["192.0.2.10"]
    assert p._join_address.advertised_ips("empty.example", None) == ["192.0.2.10"]
    assert p._join_address.advertised_ips("2001:db8::1") == ["2001:db8::1"]
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, family: [(0, 0, 0, "", ("10.0.0.2", 0)), (0, 0, 0, "", ("10.0.0.1", 0)),
                                    (0, 0, 0, "", ("10.0.0.2", 0))],
    )
    assert p._join_address.advertised_ips("multi.example") == ["10.0.0.2", "10.0.0.1"]


def test_join_host_without_an_api():
    p = PresentPlugin()
    p._join_address.detect_local_ip = lambda: "198.51.100.4"
    assert p._join_host() == "198.51.100.4"
    p.api = _FakeApi({"join_address": "  "})
    assert p._join_host() == "198.51.100.4"
    p.api = _FakeApi({"join_address": " av.example "})
    assert p._join_host() == "av.example"


@pytest.mark.asyncio
async def test_join_url_reads_the_cert_only_with_tls():
    p = _plugin()
    reads = []

    async def state_get(key):
        reads.append(key)
        return "installed"

    p.api.state_get = state_get
    assert await p._join_url() == "192.0.2.10:8080/present"
    assert reads == []
    p._join_address.tls_state = lambda: (True, 443)
    assert await p._join_url() == "https://192.0.2.10/present"
    assert reads == ["system.cloud.cert_status"]
    p._join_address.redirect_http_enabled = lambda: True
    p._join_address.http_port = lambda: 8081
    assert await p._join_url() == "http://192.0.2.10:8081/present"


def test_local_display_url_with_tls_on_443():
    p = _plugin(displays=[_LOCAL])
    p._join_address.tls_state = lambda: (True, 443)
    assert p._local_display_url(_LOCAL) == "https://127.0.0.1:443/present/display/lobby?key=lk"


# ──── Guest route edges ────


def _guest(displays=None):
    plugin = _plugin(displays)
    app = FastAPI()
    app.include_router(plugin._build_guest_router())
    return TestClient(app), plugin, plugin.api


def test_proxied_offers_without_a_location_pass_through():
    client, plugin, api = _guest(displays=[_DISPLAY])
    token, _ = api.mint_guest_token("whip:al")
    r = client.post("/whip/al", params={"token": token}, content=b"v=0")
    assert r.status_code == 201 and "location" not in r.headers
    r = client.post("/whep/main/al", params={"key": "k3y-k3y-k3y"}, content=b"v=0")
    assert r.status_code == 201 and "location" not in r.headers
    assert r.content == b"v=0\r\n"


def test_proxied_location_drops_query_and_trailing_slash():
    client, plugin, api = _guest(displays=[_DISPLAY])
    token, _ = api.mint_guest_token("whip:al")
    api.proxy_location = "/in/al/whip/sess-1/?x=1"
    r = client.post("/whip/al/", params={"token": token}, content=b"v=0")
    assert r.headers["location"] == "/whip/al/sess-1"
    api.proxy_location = "http://127.0.0.1:8891/in/al/whep/sess-2"
    r = client.post("/whep/main/al", params={"key": "k3y-k3y-k3y"}, content=b"v=0")
    assert r.headers["location"] == "/whep/main/al/sess-2"
    api.proxy_location = "/in/al/whep/sess-3/?y=2"
    r = client.post("/whep/main/al/", params={"key": "k3y-k3y-k3y"}, content=b"v=0")
    assert r.headers["location"] == "/whep/main/al/sess-3"


def test_guest_session_routes_check_in_order():
    client, plugin, api = _guest(displays=[_DISPLAY])
    token, _ = api.mint_guest_token("whip:al")
    # WHIP: presenter, then token, then secret.
    assert client.patch("/whip/a%20l/x", params={"token": token}).status_code == 422
    assert client.patch("/whip/al/x!", params={"token": "bad"}).status_code == 401
    r = client.delete("/whip/al/x!", params={"token": token})
    assert (r.status_code, r.json()["detail"]) == (422, "Invalid WHEP session id.")
    r = client.patch("/whip/al/x!", params={"token": token})
    assert (r.status_code, r.json()["detail"]) == (422, "Invalid WHEP session id.")
    r = client.post("/whip/auto", params={"token": token})
    assert (r.status_code, r.json()["detail"]) == (422, "'auto' is a reserved name.")
    # WHEP: key, then presenter, then secret.
    assert client.patch("/whep/main/a%20l/x!", params={"key": "no"}).status_code == 401
    assert client.delete("/whep/main/a%20l/x!", params={"key": "no"}).status_code == 401
    assert client.post("/whep/main/a%20l", params={"key": "no"}).status_code == 401
    r = client.delete("/whep/main/a%20l/x!", params={"key": "k3y-k3y-k3y"})
    assert r.json()["detail"] == (
        "Presenter name may contain only letters, numbers, dots, hyphens, and underscores."
    )
    assert api.proxy_calls == []
    r = client.delete("/whep/main/al/s-1", params={"key": "k3y-k3y-k3y"})
    assert r.status_code == 200
    assert api.proxy_calls[-1]["url"] == "http://openavc:sidecarpass@127.0.0.1:8891/in/al/whep/s-1"
    plugin._auth_pass = ""
    client.patch("/whep/main/al/s-1", params={"key": "k3y-k3y-k3y"})
    assert api.proxy_calls[-1]["url"] == "http://127.0.0.1:8891/in/al/whep/s-1"


def test_connect_name_is_collapsed_and_cut():
    client, plugin, api = _guest()
    code = plugin._current_code()
    name = "Ä" + "b" * 70
    r = client.post("/connect", json={"name": name, "code": f" {code} "})
    body = r.json()
    assert body["label"] == name[:60]
    assert body["presenter"] == "b" * 59
    plugin._code = ("", 0.0)
    assert client.post("/connect", json={"name": "x", "code": ""}).json()["detail"] == (
        "That join code isn't right. Check the code on the display."
    )
    r = client.post("/connect", json={"name": "x", "code": "1234"})
    assert r.status_code == 401


def test_connect_refusal_words():
    client, plugin, api = _guest()
    code = plugin._current_code()
    assert client.post("/connect", json={"name": "!!", "code": code}).json()["detail"] == "Enter your name."
    assert client.post("/connect", json={"name": "AUTO", "code": code}).json()["detail"] == (
        "That name is reserved. Use a different name."
    )
    plugin._presence["al"] = 1
    assert client.post("/connect", json={"name": "Al", "code": code}).json()["detail"] == (
        'Someone is already presenting as "al". Use a different name.'
    )
    assert plugin._labels == {}


def test_display_status_shape_and_bad_id():
    client, plugin, api = _guest(displays=[{**_DISPLAY, "label": ""}])

    async def scan():
        return {"al"}

    plugin._scan_presenters = scan
    plugin._labels["al"] = "Al L"
    body = client.get("/displays/main/status", params={"key": "k3y-k3y-k3y"}).json()
    assert body == {
        "display": "main", "label": "main", "space_name": "", "state": "live",
        "presenter": "al", "presenter_label": "Al L", "path": "in/al",
        "code": plugin._current_code(), "join_url": "192.0.2.10:8080/present",
    }
    assert client.get("/displays/ma%20in/status", params={"key": "k3y-k3y-k3y"}).status_code == 401
    r = client.get("/display/main", params={"key": " k3y-k3y-k3y "})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    r = client.get("/")
    assert r.headers["cache-control"] == "no-store" and "<html" in r.text.lower()


# ──── Small edges ────


@pytest.mark.asyncio
async def test_source_write_to_a_malformed_key_is_ignored():
    log = _Log()
    plugin = _plugin(displays=[_DISPLAY])
    plugin.api = _RecordingApi(log, plugin.api._config)
    for key in ("plugin.present.display.main", "plugin.present.displays.main.source",
                "plugin.present.display.main.showing", "x.present.display.main.source.y"):
        await plugin._on_source_write(key, "alice", None)
    assert log == [] and plugin._routing == {"main": "auto"}
    await plugin._on_source_write("plugin.present.display.ghost.source", "al", None)
    assert log == []  # no display, nothing routed, nothing to put back


@pytest.mark.asyncio
async def test_scan_presenters_when_the_api_is_down():
    plugin = _plugin()

    async def down(path):
        return None

    plugin._api_get = down
    assert await plugin._scan_presenters() is None
    assert await plugin._ingest_tracks("al") is None

    async def deprecated(path):
        return {"items": [{"name": "in/old", "ready": True}, {"name": "in/new", "available": True, "ready": False},
                          {"name": "in/off", "available": False, "ready": True}]}

    plugin._api_get = deprecated
    assert await plugin._scan_presenters() == {"old", "new"}
