"""
Tests for the Present plugin.

Covers the manifest shape, the locked-down MediaMTX config it generates, the
room helpers, the guest display-key gate (the page serve, the status route,
and the WHEP reverse proxy behind it), the rooms CRUD on the authed router,
and the presence poll that drives the state keys and join/leave events.

The SidecarSupervisor is a byte-for-byte copy of the video_panel one, whose
behavior is covered in test_video_panel_plugin.py; it is not re-tested here.

These tests import the plugin class, which pulls in fastapi/httpx; the whole
module skips if those aren't available.

Run from the openavc-plugins root: pytest tests/test_present_plugin.py -v
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

# Plugins root, so the plugin imports by its package path.
_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))

try:
    import yaml
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from integrations.present.present_plugin import PresentPlugin

    _PLUGIN_IMPORTABLE = True
except Exception:  # fastapi/httpx/yaml not installed in this environment
    _PLUGIN_IMPORTABLE = False

pytestmark = pytest.mark.skipif(
    not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available"
)


# ──── Fakes ────


class _FakeApi:
    """Stands in for the scoped PluginAPI in router and poll tests."""

    def __init__(self, config=None):
        self._config = dict(config or {})
        self.saved = []
        self.state = {}
        self.events = []  # (name, payload) from event_emit
        self.proxy_calls = []
        self.proxy_location = None  # canned WHEP Location for POST responses

    @property
    def config(self):
        return dict(self._config)

    async def save_config(self, cfg):
        self._config = dict(cfg)
        self.saved.append(dict(cfg))

    async def state_set(self, key, value):
        self.state[key] = value

    async def event_emit(self, name, payload):
        self.events.append((name, payload))

    def create_task(self, coro, name=None):
        return asyncio.ensure_future(coro)

    def log(self, message, level="info"):
        pass

    async def proxy_to(self, url, request, *, timeout=30.0, allow_internal=False):
        """Stand in for PluginAPI.proxy_to: record the upstream URL, return a
        canned response shaped like MediaMTX's WHEP replies."""
        from starlette.responses import Response as _Resp

        self.proxy_calls.append({
            "url": url,
            "method": request.method,
            "allow_internal": allow_internal,
        })
        if request.method == "POST":
            headers = {"location": self.proxy_location} if self.proxy_location else {}
            return _Resp(content=b"v=0\r\n", status_code=201, headers=headers)
        if request.method == "DELETE":
            return _Resp(content=b"", status_code=200)
        return _Resp(content=b"", status_code=204)  # PATCH


_ROOM = {"id": "room1", "label": "Boardroom", "display_key": "k3y-k3y-k3y"}


def _plugin(rooms=None, auth_pass="sidecarpass"):
    plugin = PresentPlugin()
    plugin.api = _FakeApi({"rooms": [dict(r) for r in (rooms or [])]})
    plugin._auth_pass = auth_pass
    plugin._rooms = [dict(r) for r in (rooms or [])]
    for room in plugin._rooms:
        plugin._rotate_code(room["id"])
        plugin._presence[room["id"]] = {}
    return plugin


def _guest_client(rooms=None):
    plugin = _plugin(rooms)
    app = FastAPI()
    # Mount at root so request.url.path is "/whep/...", which is what the
    # Location-rewrite reflects. The platform mounts it under
    # /api/plugins/present/guest.
    app.include_router(plugin._build_guest_router())
    return TestClient(app), plugin, plugin.api


def _ext_client(rooms=None, live=None):
    plugin = _plugin(rooms)

    async def fake_scan():
        return dict(live or {})

    plugin._scan_presenters = fake_scan
    app = FastAPI()
    app.include_router(plugin._build_ext_router())
    return TestClient(app), plugin, plugin.api


# ──── Manifest + config ────


def test_plugin_info_manifest_shape():
    info = PresentPlugin.PLUGIN_INFO
    assert info["id"] == "present"
    assert info["category"] == "integration"
    assert info["min_openavc_version"] == "0.23.0"
    assert info["platforms"] == ["win_x64", "linux_x64", "linux_arm64"]
    # Guest routes are the point of this plugin; both HTTP grants are needed.
    assert "http_endpoints" in info["capabilities"]
    assert "guest_endpoints" in info["capabilities"]

    deps = {d["id"]: d for d in info["native_dependencies"]}
    assert set(deps) == {"mediamtx"}  # no ffmpeg: the display path plays WebRTC as-is
    for dep in deps.values():
        for platform_key in ("win_x64", "linux_x64", "linux_arm64"):
            entry = dep["platforms"][platform_key]
            assert entry["url"].startswith("https://github.com/")
            assert entry["extract"]


def test_render_config_is_valid_and_locked_down():
    plugin = PresentPlugin()
    plugin._auth_pass = "a1b2c3d4e5f6"
    cfg = yaml.safe_load(plugin._render_config())

    # Distinct ports from the video_panel plugin so both can run side by side.
    assert cfg["api"] is True and cfg["apiAddress"] == "127.0.0.1:9998"
    assert cfg["webrtc"] is True and cfg["webrtcAddress"] == "127.0.0.1:8890"
    assert cfg["webrtcLocalUDPAddress"] == ":8190"
    # Protocols we don't use stay disabled — WebRTC in, WebRTC out.
    assert cfg["rtsp"] is False
    assert cfg["rtmp"] is False
    assert cfg["hls"] is False
    assert cfg["srt"] is False

    users = cfg["authInternalUsers"]
    assert users[0]["user"] == "openavc"
    assert users[0]["pass"] == "a1b2c3d4e5f6"
    assert {p["action"] for p in users[0]["permissions"]} == {"publish", "read", "playback"}
    # Localhost may publish (bench WHIP sender) and use the control API, but
    # gets no read permission — playback always rides the credentialed proxy.
    assert users[1]["user"] == "any"
    assert users[1]["ips"] == ["127.0.0.1", "::1"]
    assert {p["action"] for p in users[1]["permissions"]} == {"api", "publish"}

    # Ingest paths (<room>/<presenter>) are dynamic, so the catch-all must exist.
    assert "all_others" in cfg["paths"]


# ──── Room helpers ────


def test_slugify_and_unique_room_id():
    assert PresentPlugin._slugify("Main Boardroom!") == "main_boardroom"
    assert PresentPlugin._slugify("   ") == "room"

    plugin = _plugin(rooms=[_ROOM])
    assert plugin._unique_room_id("Boardroom") == "boardroom"
    plugin._rooms.append({"id": "boardroom", "label": "x", "display_key": "k"})
    assert plugin._unique_room_id("Boardroom") == "boardroom_2"
    # Reserved names are skipped, not produced.
    assert plugin._unique_room_id("Rooms") == "rooms_2"


def test_validate_room_id_rejects_bad_and_reserved():
    PresentPlugin._validate_room_id("room1")
    with pytest.raises(HTTPException) as e:
        PresentPlugin._validate_room_id("bad id!")
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        PresentPlugin._validate_room_id("rooms")
    assert e.value.status_code == 422


def test_pick_presenter_prefers_earliest_joined():
    plugin = _plugin(rooms=[_ROOM])
    plugin._presence["room1"] = {"bob": 200, "alice": 100}
    assert plugin._pick_presenter("room1", {"alice", "bob"}) == "alice"
    # A presenter the poll hasn't recorded yet sorts last, not first.
    assert plugin._pick_presenter("room1", {"zoe", "bob"}) == "bob"
    assert plugin._pick_presenter("room1", set()) == ""


# ──── Guest key gate ────


def test_guest_room_gate():
    plugin = _plugin(rooms=[_ROOM])
    assert plugin._guest_room("room1", "k3y-k3y-k3y") is plugin._rooms[0]
    # Wrong key, missing key, unknown room, and malformed id all read the same
    # from outside: 401, so callers can't probe which room ids exist.
    for room_id, key in [
        ("room1", "wrong"),
        ("room1", ""),
        ("ghost", "k3y-k3y-k3y"),
        ("bad id!", "k3y-k3y-k3y"),
    ]:
        with pytest.raises(HTTPException) as e:
            plugin._guest_room(room_id, key)
        assert e.value.status_code == 401


def test_display_page_serves_html_with_valid_key_and_friendly_401_without():
    client, _plugin_, _api = _guest_client(rooms=[_ROOM])

    ok = client.get("/display/room1", params={"key": "k3y-k3y-k3y"})
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/html")
    assert "display.js" in ok.text  # the real page, not the error card

    bad = client.get("/display/room1", params={"key": "nope"})
    assert bad.status_code == 401  # feeds the platform's brute-force accounting
    assert "isn't valid" in bad.text  # a human-readable card, not a JSON error


def test_room_status_reports_live_and_idle(monkeypatch):
    client, plugin, _api = _guest_client(rooms=[_ROOM])

    async def scan_live():
        return {"room1": {"alice"}}

    monkeypatch.setattr(plugin, "_scan_presenters", scan_live)
    r = client.get("/rooms/room1/status", params={"key": "k3y-k3y-k3y"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "live"
    assert body["presenter"] == "alice"
    assert body["path"] == "room1/alice"
    assert body["label"] == "Boardroom"
    assert body["code"] == plugin._current_code("room1")

    async def scan_idle():
        return {}

    monkeypatch.setattr(plugin, "_scan_presenters", scan_idle)
    body = client.get("/rooms/room1/status", params={"key": "k3y-k3y-k3y"}).json()
    assert body["state"] == "idle" and body["presenter"] == "" and body["path"] == ""

    assert client.get("/rooms/room1/status", params={"key": "bad"}).status_code == 401


def test_room_status_503_when_sidecar_down(monkeypatch):
    client, plugin, _api = _guest_client(rooms=[_ROOM])

    async def scan_down():
        return None

    monkeypatch.setattr(plugin, "_scan_presenters", scan_down)
    r = client.get("/rooms/room1/status", params={"key": "k3y-k3y-k3y"})
    assert r.status_code == 503


# ──── Guest WHEP reverse proxy ────


def test_whep_offer_requires_key_and_rewrites_location():
    client, _plugin_, api = _guest_client(rooms=[_ROOM])
    api.proxy_location = "/room1/alice/whep/abc-123-uuid"

    # No/bad key: rejected before anything reaches the sidecar.
    denied = client.post(
        "/whep/room1/alice", content=b"v=0", headers={"Content-Type": "application/sdp"}
    )
    assert denied.status_code == 401
    assert api.proxy_calls == []

    r = client.post(
        "/whep/room1/alice",
        params={"key": "k3y-k3y-k3y"},
        content=b"v=0\r\noffer",
        headers={"Content-Type": "application/sdp"},
    )
    assert r.status_code == 201, r.text
    # Forwarded to the localhost sidecar WHEP endpoint with read creds in
    # userinfo. allow_internal=True opts past proxy_to's SSRF guard.
    assert api.proxy_calls[-1] == {
        "url": "http://openavc:sidecarpass@127.0.0.1:8890/room1/alice/whep",
        "method": "POST",
        "allow_internal": True,
    }
    # MediaMTX's path-absolute Location is rewritten to live under this mount
    # so the browser's PATCH/DELETE come back through the key-checked proxy.
    assert r.headers["location"] == "/whep/room1/alice/abc-123-uuid"


def test_whep_trickle_and_teardown_target_the_session():
    client, _plugin_, api = _guest_client(rooms=[_ROOM])
    expected = "http://openavc:sidecarpass@127.0.0.1:8890/room1/alice/whep/abc-123-uuid"

    pr = client.patch(
        "/whep/room1/alice/abc-123-uuid",
        params={"key": "k3y-k3y-k3y"},
        content=b"a=ice-ufrag:x\r\n",
        headers={"Content-Type": "application/trickle-ice-sdpfrag"},
    )
    assert pr.status_code == 204
    assert api.proxy_calls[-1] == {
        "url": expected, "method": "PATCH", "allow_internal": True,
    }

    dr = client.delete("/whep/room1/alice/abc-123-uuid", params={"key": "k3y-k3y-k3y"})
    assert dr.status_code == 200
    assert api.proxy_calls[-1] == {
        "url": expected, "method": "DELETE", "allow_internal": True,
    }


def test_whep_rejects_malformed_presenter_and_secret():
    client, _plugin_, api = _guest_client(rooms=[_ROOM])
    bad_presenter = client.post(
        "/whep/room1/bad%20name",
        params={"key": "k3y-k3y-k3y"},
        content=b"v=0",
        headers={"Content-Type": "application/sdp"},
    )
    assert bad_presenter.status_code == 422
    # Underscore is outside the UUID-ish secret charset -> rejected before proxying.
    bad_secret = client.delete(
        "/whep/room1/alice/bad_secret", params={"key": "k3y-k3y-k3y"}
    )
    assert bad_secret.status_code == 422
    assert api.proxy_calls == []


# ──── Rooms CRUD (authed /ext router) ────


def test_crud_add_list_edit_delete():
    client, plugin, api = _ext_client()

    # Add: id auto-derived from the label, display key + code generated, persisted.
    r = client.post("/rooms", json={"label": "Main Boardroom"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "main_boardroom"
    assert body["output_state"] == "idle"
    assert len(body["display_key"]) >= 24
    assert body["display_path"] == (
        f"/api/plugins/present/guest/display/main_boardroom?key={body['display_key']}"
    )
    assert body["code"].isdigit() and len(body["code"]) == 4
    assert api.saved[-1]["rooms"][0]["id"] == "main_boardroom"
    # The room list state key is republished for the IDE / triggers.
    assert json.loads(api.state["rooms"]) == [
        {"id": "main_boardroom", "label": "Main Boardroom"}
    ]

    # A second room with the same label gets a unique id.
    r = client.post("/rooms", json={"label": "Main Boardroom"})
    assert r.json()["id"] == "main_boardroom_2"

    # List returns both.
    listing = client.get("/rooms").json()
    assert {x["id"] for x in listing} == {"main_boardroom", "main_boardroom_2"}

    # Edit: rename keeps the key; an id change clears the old state keys.
    old_key = body["display_key"]
    r = client.put("/rooms/main_boardroom", json={"label": "Boardroom", "room_id": "board"})
    assert r.status_code == 200, r.text
    edited = r.json()
    assert edited["id"] == "board" and edited["label"] == "Boardroom"
    assert edited["display_key"] == old_key  # a rename must not break display links
    assert api.state["main_boardroom.output_state"] is None  # old keys cleared

    # Delete removes it from the list and persists.
    r = client.delete("/rooms/board")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert [x["id"] for x in api.saved[-1]["rooms"]] == ["main_boardroom_2"]
    assert api.state["board.output_state"] is None


def test_crud_validation_and_conflicts():
    client, _plugin_, _api = _ext_client(rooms=[_ROOM])

    assert client.post("/rooms", json={"label": "   "}).status_code == 422
    assert client.post("/rooms", json={"label": "X", "room_id": "bad id!"}).status_code == 422
    assert client.post("/rooms", json={"label": "X", "room_id": "rooms"}).status_code == 422
    assert client.post("/rooms", json={"label": "X", "room_id": "room1"}).status_code == 409
    assert client.put("/rooms/nope", json={"label": "N"}).status_code == 404
    assert client.delete("/rooms/nope").status_code == 404


def test_regenerate_key_invalidates_old_display_links():
    client, plugin, _api = _ext_client(rooms=[_ROOM])
    r = client.post("/rooms/room1/regenerate_key")
    assert r.status_code == 200
    new_key = r.json()["display_key"]
    assert new_key and new_key != "k3y-k3y-k3y"
    # The old key no longer passes the guest gate.
    with pytest.raises(HTTPException):
        plugin._guest_room("room1", "k3y-k3y-k3y")
    assert plugin._guest_room("room1", new_key) is plugin._rooms[0]


# ──── Presence scan + poll ────


@pytest.mark.asyncio
async def test_scan_presenters_filters_paths(monkeypatch):
    plugin = _plugin(rooms=[_ROOM])

    async def fake_get(path):
        return {
            "items": [
                {"name": "room1/alice", "available": True},
                {"name": "room1/bob", "available": False},  # not publishing yet
                {"name": "room1/bad name", "available": True},  # unsafe segment
                {"name": "ghost/carol", "available": True},  # not a configured room
                {"name": "room1", "available": True},  # no presenter segment
            ]
        }

    monkeypatch.setattr(plugin, "_api_get", fake_get)
    assert await plugin._scan_presenters() == {"room1": {"alice"}}


@pytest.mark.asyncio
async def test_poll_publishes_state_and_events_and_rotates_code(monkeypatch):
    plugin = _plugin(rooms=[_ROOM])
    api = plugin.api
    scan_result = {"room1": {"alice"}}

    async def fake_scan():
        return {k: set(v) for k, v in scan_result.items()}

    monkeypatch.setattr(plugin, "_scan_presenters", fake_scan)
    code_before, rotated_before = plugin._codes["room1"]

    # Presenter joins.
    await plugin._poll()
    assert api.state["running"] is True
    assert api.state["room1.output_state"] == "live"
    assert api.state["room1.active_presenters"] == 1
    assert json.loads(api.state["room1.presenters"])[0]["name"] == "alice"
    assert api.events[-1][0] == "presenter_joined"
    assert api.events[-1][1] == {"room": "room1", "name": "alice"}
    # A live session keeps its code (the display isn't showing it anyway).
    assert plugin._codes["room1"] == (code_before, rotated_before)

    # Presenter leaves: back to idle, and the session's code is retired.
    scan_result = {}
    await plugin._poll()
    assert api.state["room1.output_state"] == "idle"
    assert api.state["room1.active_presenters"] == 0
    assert api.events[-1][0] == "presenter_left"
    assert plugin._codes["room1"][1] != rotated_before  # rotated
    assert api.state["room1.code"] == plugin._codes["room1"][0]


@pytest.mark.asyncio
async def test_poll_marks_not_running_when_sidecar_unreachable(monkeypatch):
    plugin = _plugin(rooms=[_ROOM])

    async def fake_scan():
        return None

    monkeypatch.setattr(plugin, "_scan_presenters", fake_scan)
    await plugin._poll()
    assert plugin.api.state["running"] is False


@pytest.mark.asyncio
async def test_stale_idle_code_rotates(monkeypatch):
    plugin = _plugin(rooms=[_ROOM])

    async def fake_scan():
        return {}

    monkeypatch.setattr(plugin, "_scan_presenters", fake_scan)
    code = plugin._codes["room1"][0]
    # Age the code past the idle rotation window.
    plugin._codes["room1"] = (code, -10_000.0)
    await plugin._poll()
    assert plugin._codes["room1"][1] > 0  # re-stamped now


# ──── Config loading ────


@pytest.mark.asyncio
async def test_load_rooms_backfills_missing_display_key():
    plugin = PresentPlugin()
    plugin.api = _FakeApi({
        "rooms": [
            {"id": "room1", "label": "Boardroom"},  # hand-edited: no key
            {"id": "bad id!", "label": "Rejected"},
            "not-a-dict",
        ]
    })
    await plugin._load_rooms()
    assert [r["id"] for r in plugin._rooms] == ["room1"]
    assert plugin._rooms[0]["display_key"]  # generated
    assert plugin.api.saved  # and persisted back
    assert plugin._current_code("room1")  # a code exists from load
