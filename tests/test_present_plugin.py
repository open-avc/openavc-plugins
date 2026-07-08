"""
Tests for the Present plugin.

Covers the manifest shape, the locked-down MediaMTX config it generates, the
display helpers, the routing engine (auto / pinned / pinned-but-absent, and
every write surface: macro action, script API, ext route, state-key writes),
the guest display-key gate (the page serve, the status route, and the WHEP
reverse proxy behind it), the Connect flow (code->token exchange, the
token-gated WHIP publish proxy, presenter labels, the join address), the
displays CRUD on the authed router, and the presence poll that drives the
state keys and join/leave events.

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
    """Stands in for the scoped PluginAPI in router, routing, and poll tests."""

    def __init__(self, config=None, state=None):
        self._config = dict(config or {})
        self.saved = []
        self.state = dict(state or {})
        self.events = []  # (name, payload) from event_emit
        self.proxy_calls = []
        self.proxy_location = None  # canned WHEP/WHIP Location for POST responses
        self.minted = []  # (scope, ttl) from mint_guest_token
        self.tokens = {}  # token -> scope
        # Kiosk profile cleanup rmtree()s under here; nonexistent = no-op.
        self.data_dir = Path("nonexistent-plugin-data")

    @property
    def config(self):
        return dict(self._config)

    async def save_config(self, cfg):
        self._config = dict(cfg)
        self.saved.append(dict(cfg))

    async def state_set(self, key, value):
        self.state[key] = value

    async def state_get(self, key):
        return self.state.get(key)

    async def event_emit(self, name, payload):
        self.events.append((name, payload))

    def create_task(self, coro, name=None):
        return asyncio.ensure_future(coro)

    def log(self, message, level="info"):
        pass

    def mint_guest_token(self, scope, ttl=3600):
        """Stand in for the platform's HMAC guest tokens: opaque token bound
        to the scope, verified by exact scope match (the real contract)."""
        token = f"tok{len(self.minted)}-{scope}"
        self.minted.append((scope, ttl))
        self.tokens[token] = scope
        return token, 1_900_000_000

    def verify_guest_token(self, token, scope):
        return self.tokens.get(token) == scope

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


_DISPLAY = {"id": "main", "label": "Main Screen", "display_key": "k3y-k3y-k3y"}
_DISPLAY2 = {"id": "overflow", "label": "Overflow TV", "display_key": "0th3r-k3y"}


class _FakeSupervisor:
    """Stands in for the SidecarSupervisor; the poll only reads .running."""

    def __init__(self, running=True):
        self.running = running
        self.pid = 4242


def _plugin(displays=None, auth_pass="sidecarpass", state=None, config=None):
    plugin = PresentPlugin()
    cfg = {"displays": [dict(d) for d in (displays or [])]}
    cfg.update(config or {})
    plugin.api = _FakeApi(cfg, state=state)
    plugin._auth_pass = auth_pass
    plugin._supervisor = _FakeSupervisor()
    plugin._displays = [dict(d) for d in (displays or [])]
    plugin._routing = {d["id"]: "auto" for d in plugin._displays}
    plugin._rotate_code()
    # Pin the join-address inputs so join_url is deterministic regardless of
    # the test host's NICs or whether the server package is importable.
    plugin._detect_local_ip = lambda: "192.0.2.10"
    plugin._http_port = lambda: 8080
    plugin._tls_state = lambda: (False, 0)
    plugin._redirect_http_enabled = lambda: False
    return plugin


def _guest_client(displays=None):
    plugin = _plugin(displays)
    app = FastAPI()
    # Mount at root so request.url.path is "/whep/...", which is what the
    # Location-rewrite reflects. The platform mounts it under
    # /api/plugins/present/guest.
    app.include_router(plugin._build_guest_router())
    return TestClient(app), plugin, plugin.api


class _FakeController:
    """Stands in for output.OutputController in plugin wiring tests."""

    def __init__(self, state="idle"):
        self.state = state
        self.shown = []

    def show(self, source):
        self.shown.append(source)


def _stub_outputs(plugin):
    """Replace the output-pipeline lifecycle with recorders.

    CRUD tests exercise when the plugin starts/stops/restarts a stream
    display's output, not the pipeline itself (that has its own tests below
    against fake processes). The stubs keep _controllers consistent so the
    kind-reconciliation logic ("already running -> don't restart") behaves.
    """
    calls = []

    async def start(display):
        calls.append(("start", display["id"]))
        plugin._controllers[display["id"]] = _FakeController()

    async def stop(display_id):
        plugin._controllers.pop(display_id, None)
        calls.append(("stop", display_id))

    async def restart(display):
        calls.append(("restart", display["id"]))
        plugin._controllers[display["id"]] = _FakeController()

    plugin._start_output = start
    plugin._stop_output = stop
    plugin._restart_output = restart
    # Pre-seed controllers for stream displays that "already run".
    for d in plugin._displays:
        if d.get("kind") == "stream":
            plugin._controllers[d["id"]] = _FakeController()
    return calls


def _ext_client(displays=None, presence=None):
    plugin = _plugin(displays)
    plugin._presence = dict(presence or {})
    plugin._output_calls = _stub_outputs(plugin)
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
    # The short typed URL on every connect card.
    assert info["guest_alias"] == "present"
    # The join-address override must be a declared config field.
    assert "join_address" in PresentPlugin.CONFIG_SCHEMA

    deps = {d["id"]: d for d in info["native_dependencies"]}
    # MediaMTX carries the WebRTC/RTSP/SRT paths; ffmpeg drives the
    # stream-display output encoders (idle card + live transcode).
    assert set(deps) == {"mediamtx", "ffmpeg"}
    assert deps["ffmpeg"]["license"] == "LGPL-2.1"  # never the GPL build
    for dep in deps.values():
        for platform_key in ("win_x64", "linux_x64", "linux_arm64"):
            entry = dep["platforms"][platform_key]
            assert entry["url"].startswith("https://github.com/")
            assert entry["extract"]


def test_matrix_verbs_are_declared():
    # The routing surface the macro builder and scripts consume.
    action = PresentPlugin.MACRO_ACTIONS["present.route"]
    assert action["handler"] == "action_route"
    params = {p["key"]: p for p in action["params"]}
    assert params["display"]["options_source"] == "plugin.present.displays"
    assert params["source"]["options_source"] == "plugin.present.sources"

    assert PresentPlugin.SCRIPT_API["route"]["handler"] == "script_route"


def test_render_config_is_valid_and_locked_down():
    plugin = PresentPlugin()
    plugin._auth_pass = "a1b2c3d4e5f6"
    cfg = yaml.safe_load(plugin._render_config())

    # Distinct ports from the video_panel plugin so both can run side by side.
    assert cfg["api"] is True and cfg["apiAddress"] == "127.0.0.1:9998"
    assert cfg["webrtc"] is True and cfg["webrtcAddress"] == "127.0.0.1:8890"
    assert cfg["webrtcLocalUDPAddress"] == ":8190"
    # RTSP + SRT face the LAN for stream-display decoder pulls. RTSP is
    # TCP-only (one predictable firewall port, no UDP RTP range) and
    # unencrypted (decoder compatibility; the LAN read grant is out/* only).
    assert cfg["rtsp"] is True
    assert cfg["rtspAddress"] == ":8554"
    assert cfg["rtspTransports"] == ["tcp"]
    assert cfg["rtspEncryption"] == "no"
    assert cfg["srt"] is True
    assert cfg["srtAddress"] == ":8899"
    # Protocols we don't use stay disabled.
    assert cfg["rtmp"] is False
    assert cfg["hls"] is False

    # MediaMTX picks the first entry whose credentials + source IP match and
    # enforces only that entry's permissions (no fall-through), so the order
    # and per-entry grants here are all load-bearing.
    users = cfg["authInternalUsers"]
    assert users[0]["user"] == "openavc"
    assert users[0]["pass"] == "a1b2c3d4e5f6"
    assert {p["action"] for p in users[0]["permissions"]} == {"publish", "read", "playback"}
    # Localhost may publish (bench WHIP sender, output encoders) and use the
    # control API; its read grant covers only the out/* outputs so
    # rtsp://localhost/out/... works on the server host. Ingest playback
    # always rides the credentialed proxy.
    assert users[1]["user"] == "any"
    assert users[1]["ips"] == ["127.0.0.1", "::1"]
    perms1 = {p["action"]: p for p in users[1]["permissions"]}
    assert set(perms1) == {"api", "publish", "read"}
    assert perms1["read"]["path"] == "~^out/"
    assert "path" not in perms1["publish"]
    # Anonymous LAN (decoders): read out/* only — the stream key in the URL
    # is the secret. No publish, no ingest read, no API.
    assert users[2]["user"] == "any"
    assert users[2]["ips"] == []
    perms2 = {p["action"]: p for p in users[2]["permissions"]}
    assert set(perms2) == {"read"}
    assert perms2["read"]["path"] == "~^out/"

    # Ingest paths (in/<presenter>) are dynamic, so the catch-all must exist.
    assert "all_others" in cfg["paths"]


# ──── Display helpers ────


def test_slugify_and_unique_display_id():
    assert PresentPlugin._slugify("Main Screen!") == "main_screen"
    assert PresentPlugin._slugify("   ") == "display"

    plugin = _plugin(displays=[_DISPLAY])
    assert plugin._unique_display_id("Main Screen") == "main_screen"
    plugin._displays.append({"id": "main_screen", "label": "x", "display_key": "k"})
    assert plugin._unique_display_id("Main Screen") == "main_screen_2"
    # Reserved names are skipped, not produced.
    assert plugin._unique_display_id("Auto") == "auto_2"


def test_validate_display_id_rejects_bad_and_reserved():
    PresentPlugin._validate_display_id("main")
    with pytest.raises(HTTPException) as e:
        PresentPlugin._validate_display_id("bad id!")
    assert e.value.status_code == 422
    for reserved in ("auto", "connect", "display", "displays", "in", "out", "status", "whep", "whip"):
        with pytest.raises(HTTPException) as e:
            PresentPlugin._validate_display_id(reserved)
        assert e.value.status_code == 422


# ──── Routing resolution ────


def test_normalize_source():
    assert PresentPlugin._normalize_source("auto") == "auto"
    assert PresentPlugin._normalize_source("alice") == "alice"
    # Empty and None mean auto (clearing a pin).
    assert PresentPlugin._normalize_source("") == "auto"
    assert PresentPlugin._normalize_source(None) == "auto"
    # Anything else is invalid, not coerced.
    assert PresentPlugin._normalize_source("bad name!") is None
    assert PresentPlugin._normalize_source(7) is None


def test_resolve_source_auto_follows_earliest_presenter():
    plugin = _plugin(displays=[_DISPLAY])
    plugin._presence = {"bob": 200, "alice": 100}
    assert plugin._resolve_source("main", {"alice", "bob"}) == "alice"
    # A presenter the poll hasn't recorded yet sorts last, not first.
    assert plugin._resolve_source("main", {"zoe", "bob"}) == "bob"
    assert plugin._resolve_source("main", set()) == ""


def test_resolve_source_pinned_and_pinned_absent():
    plugin = _plugin(displays=[_DISPLAY])
    plugin._presence = {"alice": 100, "bob": 200}
    plugin._routing["main"] = "bob"
    # Pinned: shows bob even though alice joined first.
    assert plugin._resolve_source("main", {"alice", "bob"}) == "bob"
    # Pinned presenter not sharing: the idle card, NOT a fall-through to alice.
    assert plugin._resolve_source("main", {"alice"}) == ""


@pytest.mark.asyncio
async def test_route_updates_state_and_emits_event():
    plugin = _plugin(displays=[_DISPLAY, _DISPLAY2])
    api = plugin.api
    plugin._presence = {"alice": 100, "bob": 200}

    await plugin._route("overflow", "bob")
    assert plugin._routing["overflow"] == "bob"
    assert api.state["display.overflow.source"] == "bob"
    assert api.state["display.overflow.showing"] == "bob"
    assert api.state["display.overflow.output_state"] == "live"
    assert ("route_changed", {"display": "overflow", "source": "bob"}) in api.events

    # Re-routing to the same source is not a change — no second event.
    events_before = len(api.events)
    await plugin._route("overflow", "bob")
    assert len(api.events) == events_before

    # Clearing the pin: empty means auto.
    await plugin._route("overflow", "")
    assert plugin._routing["overflow"] == "auto"
    assert api.state["display.overflow.source"] == "auto"
    assert api.events[-1] == ("route_changed", {"display": "overflow", "source": "auto"})

    with pytest.raises(ValueError):
        await plugin._route("ghost", "alice")
    with pytest.raises(ValueError):
        await plugin._route("main", "bad name!")


@pytest.mark.asyncio
async def test_macro_action_and_script_api_route():
    plugin = _plugin(displays=[_DISPLAY])
    plugin._presence = {"alice": 100}

    await plugin.action_route({"display": "main", "source": "alice"}, {})
    assert plugin._routing["main"] == "alice"

    await plugin.script_route("main", "auto")
    assert plugin._routing["main"] == "auto"

    # A failed step surfaces the user-facing message through the macro engine.
    with pytest.raises(ValueError):
        await plugin.action_route({"display": "ghost", "source": "alice"}, {})


@pytest.mark.asyncio
async def test_state_key_write_drives_routing():
    plugin = _plugin(displays=[_DISPLAY])
    api = plugin.api
    plugin._presence = {"alice": 100}

    # An external state.set (macro / script / API) pins the display.
    await plugin._on_source_write("plugin.present.display.main.source", "alice", "auto")
    assert plugin._routing["main"] == "alice"
    assert ("route_changed", {"display": "main", "source": "alice"}) in api.events

    # The plugin's own publish of that key round-trips as a no-op.
    events_before = len(api.events)
    await plugin._on_source_write("plugin.present.display.main.source", "alice", "auto")
    assert len(api.events) == events_before

    # A cleared key (display removed) is not a route request.
    await plugin._on_source_write("plugin.present.display.main.source", None, "alice")
    assert plugin._routing["main"] == "alice"

    # An invalid value is rejected and the truth republished.
    await plugin._on_source_write("plugin.present.display.main.source", "bad name!", "alice")
    assert plugin._routing["main"] == "alice"
    assert api.state["display.main.source"] == "alice"

    # A write for an unknown display is ignored (no crash, no routing entry).
    await plugin._on_source_write("plugin.present.display.ghost.source", "alice", None)
    assert "ghost" not in plugin._routing


# ──── Guest key gate ────


def test_guest_display_gate():
    plugin = _plugin(displays=[_DISPLAY])
    assert plugin._guest_display("main", "k3y-k3y-k3y") is plugin._displays[0]
    # Wrong key, missing key, unknown display, and malformed id all read the
    # same from outside: 401, so callers can't probe which ids exist.
    for display_id, key in [
        ("main", "wrong"),
        ("main", ""),
        ("ghost", "k3y-k3y-k3y"),
        ("bad id!", "k3y-k3y-k3y"),
    ]:
        with pytest.raises(HTTPException) as e:
            plugin._guest_display(display_id, key)
        assert e.value.status_code == 401


def test_display_page_serves_html_with_valid_key_and_friendly_401_without():
    client, _plugin_, _api = _guest_client(displays=[_DISPLAY])

    ok = client.get("/display/main", params={"key": "k3y-k3y-k3y"})
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/html")
    assert "display.js" in ok.text  # the real page, not the error card

    bad = client.get("/display/main", params={"key": "nope"})
    assert bad.status_code == 401  # feeds the platform's brute-force accounting
    assert "isn't valid" in bad.text  # a human-readable card, not a JSON error


def test_display_status_reports_routed_source(monkeypatch):
    client, plugin, api = _guest_client(displays=[_DISPLAY, _DISPLAY2])
    api.state["system.project_name"] = "Lecture Hall"
    plugin._presence = {"alice": 100, "bob": 200}

    async def scan_live():
        return {"alice", "bob"}

    monkeypatch.setattr(plugin, "_scan_presenters", scan_live)

    # Auto: the earliest presenter.
    r = client.get("/displays/main/status", params={"key": "k3y-k3y-k3y"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "live"
    assert body["presenter"] == "alice"
    assert body["presenter_label"] == "alice"  # no typed label: slug stands in
    assert body["path"] == "in/alice"
    assert body["label"] == "Main Screen"
    assert body["space_name"] == "Lecture Hall"  # project-name fallback
    assert body["code"] == plugin._current_code()
    # The plugin-chosen join line the idle card renders (not the page's own
    # location.host).
    assert body["join_url"] == "192.0.2.10:8080/present"

    # Pinned: this display shows bob regardless of who joined first.
    plugin._routing["overflow"] = "bob"
    body = client.get("/displays/overflow/status", params={"key": "0th3r-k3y"}).json()
    assert body["state"] == "live" and body["presenter"] == "bob"

    # Pinned-but-absent: the idle card.
    plugin._routing["overflow"] = "carol"
    body = client.get("/displays/overflow/status", params={"key": "0th3r-k3y"}).json()
    assert body["state"] == "idle" and body["presenter"] == "" and body["path"] == ""

    assert client.get("/displays/main/status", params={"key": "bad"}).status_code == 401


def test_display_status_prefers_configured_space_name(monkeypatch):
    client, plugin, api = _guest_client(displays=[_DISPLAY])
    api._config["space_name"] = "Boardroom"
    api.state["system.project_name"] = "Lecture Hall"

    async def scan_idle():
        return set()

    monkeypatch.setattr(plugin, "_scan_presenters", scan_idle)
    body = client.get("/displays/main/status", params={"key": "k3y-k3y-k3y"}).json()
    assert body["space_name"] == "Boardroom"
    assert body["state"] == "idle"


def test_display_status_503_when_sidecar_down(monkeypatch):
    client, plugin, _api = _guest_client(displays=[_DISPLAY])

    async def scan_down():
        return None

    monkeypatch.setattr(plugin, "_scan_presenters", scan_down)
    r = client.get("/displays/main/status", params={"key": "k3y-k3y-k3y"})
    assert r.status_code == 503


# ──── Guest WHEP reverse proxy ────


def test_whep_offer_requires_key_and_rewrites_location():
    client, _plugin_, api = _guest_client(displays=[_DISPLAY])
    api.proxy_location = "/in/alice/whep/abc-123-uuid"

    # No/bad key: rejected before anything reaches the sidecar.
    denied = client.post(
        "/whep/main/alice", content=b"v=0", headers={"Content-Type": "application/sdp"}
    )
    assert denied.status_code == 401
    assert api.proxy_calls == []

    r = client.post(
        "/whep/main/alice",
        params={"key": "k3y-k3y-k3y"},
        content=b"v=0\r\noffer",
        headers={"Content-Type": "application/sdp"},
    )
    assert r.status_code == 201, r.text
    # Forwarded to the localhost sidecar's in/<presenter> WHEP endpoint with
    # read creds in userinfo. allow_internal=True opts past the SSRF guard.
    assert api.proxy_calls[-1] == {
        "url": "http://openavc:sidecarpass@127.0.0.1:8890/in/alice/whep",
        "method": "POST",
        "allow_internal": True,
    }
    # MediaMTX's path-absolute Location is rewritten to live under this mount
    # so the browser's PATCH/DELETE come back through the key-checked proxy.
    assert r.headers["location"] == "/whep/main/alice/abc-123-uuid"


def test_whep_trickle_and_teardown_target_the_session():
    client, _plugin_, api = _guest_client(displays=[_DISPLAY])
    expected = "http://openavc:sidecarpass@127.0.0.1:8890/in/alice/whep/abc-123-uuid"

    pr = client.patch(
        "/whep/main/alice/abc-123-uuid",
        params={"key": "k3y-k3y-k3y"},
        content=b"a=ice-ufrag:x\r\n",
        headers={"Content-Type": "application/trickle-ice-sdpfrag"},
    )
    assert pr.status_code == 204
    assert api.proxy_calls[-1] == {
        "url": expected, "method": "PATCH", "allow_internal": True,
    }

    dr = client.delete("/whep/main/alice/abc-123-uuid", params={"key": "k3y-k3y-k3y"})
    assert dr.status_code == 200
    assert api.proxy_calls[-1] == {
        "url": expected, "method": "DELETE", "allow_internal": True,
    }


def test_whep_rejects_malformed_presenter_and_secret():
    client, _plugin_, api = _guest_client(displays=[_DISPLAY])
    bad_presenter = client.post(
        "/whep/main/bad%20name",
        params={"key": "k3y-k3y-k3y"},
        content=b"v=0",
        headers={"Content-Type": "application/sdp"},
    )
    assert bad_presenter.status_code == 422
    # "auto" is the routing sentinel, never a playable ingest.
    reserved = client.post(
        "/whep/main/auto",
        params={"key": "k3y-k3y-k3y"},
        content=b"v=0",
        headers={"Content-Type": "application/sdp"},
    )
    assert reserved.status_code == 422
    # Underscore is outside the UUID-ish secret charset -> rejected before proxying.
    bad_secret = client.delete(
        "/whep/main/alice/bad_secret", params={"key": "k3y-k3y-k3y"}
    )
    assert bad_secret.status_code == 422
    assert api.proxy_calls == []


# ──── Connect flow (code -> token -> WHIP publish) ────


def test_connect_page_served_at_guest_root():
    client, _plugin_, _api = _guest_client()
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "connect.js" in r.text
    assert r.headers["cache-control"] == "no-store"


def test_connect_exchange_mints_scoped_token():
    client, plugin, api = _guest_client()
    api.state["system.project_name"] = "Lecture Hall"
    code = plugin._current_code()

    r = client.post("/connect", json={"name": "  Aaron   Todd ", "code": code})
    assert r.status_code == 200, r.text
    body = r.json()
    # The typed name is trimmed/collapsed for the label and slugged for the
    # ingest name; routing values use the slug, humans see the label.
    assert body["presenter"] == "aaron_todd"
    assert body["label"] == "Aaron Todd"
    assert body["space_name"] == "Lecture Hall"
    assert body["token"] and body["expires_at"]
    assert plugin._labels["aaron_todd"] == "Aaron Todd"
    # The token is bound to exactly this ingest name.
    assert api.minted == [("whip:aaron_todd", 4 * 3600)]


def test_connect_exchange_rejects_bad_code_name_and_active_presenter():
    client, plugin, api = _guest_client()
    code = plugin._current_code()

    # Wrong/missing code: 401 (feeds brute-force accounting), nothing minted.
    assert client.post("/connect", json={"name": "Alice", "code": "0000" if code != "0000" else "1111"}).status_code == 401
    assert client.post("/connect", json={"name": "Alice", "code": ""}).status_code == 401
    assert api.minted == []

    # A name that slugs to nothing, and the routing sentinel: 422.
    assert client.post("/connect", json={"name": "   ", "code": code}).status_code == 422
    assert client.post("/connect", json={"name": "Auto", "code": code}).status_code == 422

    # A name already presenting: 409, friendly, nothing minted for it.
    plugin._presence["alice"] = 100
    plugin._labels["alice"] = "Alice"
    r = client.post("/connect", json={"name": "alice", "code": code})
    assert r.status_code == 409
    assert "Alice" in r.json()["detail"]


def test_whip_publish_requires_token_and_rewrites_location():
    client, plugin, api = _guest_client()
    api.proxy_location = "/in/alice/whip/abc-123-uuid"
    token, _ = api.mint_guest_token("whip:alice")

    # No token / a token for a different presenter: rejected before the proxy.
    assert client.post("/whip/alice", content=b"v=0").status_code == 401
    other, _ = api.mint_guest_token("whip:bob")
    assert client.post("/whip/alice", params={"token": other}, content=b"v=0").status_code == 401
    assert api.proxy_calls == []

    r = client.post(
        "/whip/alice",
        params={"token": token},
        content=b"v=0\r\noffer",
        headers={"Content-Type": "application/sdp"},
    )
    assert r.status_code == 201, r.text
    # Forwarded to the sidecar's in/<presenter> WHIP endpoint with NO
    # credentials — the localhost "any" user carries publish.
    assert api.proxy_calls[-1] == {
        "url": "http://127.0.0.1:8890/in/alice/whip",
        "method": "POST",
        "allow_internal": True,
    }
    # Location rewritten under this mount so PATCH/DELETE come back through
    # the token check.
    assert r.headers["location"] == "/whip/alice/abc-123-uuid"


def test_whip_trickle_and_teardown_target_the_session():
    client, _plugin_, api = _guest_client()
    token, _ = api.mint_guest_token("whip:alice")
    expected = "http://127.0.0.1:8890/in/alice/whip/abc-123-uuid"

    pr = client.patch(
        "/whip/alice/abc-123-uuid",
        params={"token": token},
        content=b"a=ice-ufrag:x\r\n",
        headers={"Content-Type": "application/trickle-ice-sdpfrag"},
    )
    assert pr.status_code == 204
    assert api.proxy_calls[-1] == {"url": expected, "method": "PATCH", "allow_internal": True}

    dr = client.delete("/whip/alice/abc-123-uuid", params={"token": token})
    assert dr.status_code == 200
    assert api.proxy_calls[-1] == {"url": expected, "method": "DELETE", "allow_internal": True}

    # Reserved/malformed presenter and malformed secret: rejected pre-proxy.
    calls_before = len(api.proxy_calls)
    assert client.post("/whip/auto", params={"token": token}, content=b"v=0").status_code == 422
    assert client.delete("/whip/alice/bad_secret", params={"token": token}).status_code == 422
    assert len(api.proxy_calls) == calls_before


@pytest.mark.asyncio
async def test_presenter_labels_flow_and_prune(monkeypatch):
    plugin = _plugin(displays=[_DISPLAY])
    api = plugin.api
    plugin._labels["aaron_todd"] = "Aaron Todd"
    scan_result = {"aaron_todd"}

    async def fake_scan():
        return set(scan_result)

    monkeypatch.setattr(plugin, "_scan_presenters", fake_scan)

    await plugin._poll()
    # Labels ride the events, the presenters JSON, and the sources options;
    # routing values stay the slugs.
    assert ("presenter_joined", {"name": "aaron_todd", "label": "Aaron Todd"}) in api.events
    presenters = json.loads(api.state["presenters"])
    assert presenters[0] == {"name": "aaron_todd", "label": "Aaron Todd", "since": presenters[0]["since"]}
    sources = json.loads(api.state["sources"])
    assert {"value": "aaron_todd", "label": "Aaron Todd"} in sources

    # Leave: the label map is pruned and the event carries the label.
    scan_result = set()
    await plugin._poll()
    assert api.events[-1] == ("presenter_left", {"name": "aaron_todd", "label": "Aaron Todd"})
    assert "aaron_todd" not in plugin._labels


@pytest.mark.asyncio
async def test_join_url_configured_address_and_port_handling():
    # Configured join_address wins over detection.
    plugin = _plugin(config={"join_address": "av.example.edu"})
    assert await plugin._join_url() == "av.example.edu:8080/present"
    # Port 80 is implicit in what a human types.
    plugin._http_port = lambda: 80
    assert await plugin._join_url() == "av.example.edu/present"
    # Blank config falls back to the (pinned) auto-detected LAN address.
    plugin2 = _plugin()
    assert await plugin2._join_url() == "192.0.2.10:8080/present"


def test_slugify_presenter():
    assert PresentPlugin._slugify_presenter("Aaron Todd") == "aaron_todd"
    assert PresentPlugin._slugify_presenter("J.-P. O'Neil") == "j_p_o_neil"
    # No fallback: an empty result is the caller's cue to reject.
    assert PresentPlugin._slugify_presenter("   ") == ""
    assert PresentPlugin._slugify_presenter("!!!") == ""


# ──── Displays CRUD (authed /ext router) ────


def test_crud_add_list_edit_delete():
    client, plugin, api = _ext_client()

    # Add: id auto-derived from the label, display key generated, persisted,
    # routing starts in auto.
    r = client.post("/displays", json={"label": "Main Screen"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "main_screen"
    assert body["source"] == "auto"
    assert body["showing"] == "" and body["output_state"] == "idle"
    assert len(body["display_key"]) >= 24
    # The short guest-alias form; the canonical /api/plugins/... path serves
    # the same page.
    assert body["display_path"] == (
        f"/present/display/main_screen?key={body['display_key']}"
    )
    assert api.saved[-1]["displays"][0]["id"] == "main_screen"
    # The displays state key is republished; entries carry value=id so the
    # same key feeds the Route Display dropdown.
    assert json.loads(api.state["displays"]) == [
        {"id": "main_screen", "value": "main_screen", "label": "Main Screen"}
    ]
    assert api.state["display.main_screen.source"] == "auto"

    # A second display with the same label gets a unique id.
    r = client.post("/displays", json={"label": "Main Screen"})
    assert r.json()["id"] == "main_screen_2"

    # List returns both.
    listing = client.get("/displays").json()
    assert {x["id"] for x in listing} == {"main_screen", "main_screen_2"}

    # Edit: rename keeps the key; an id change clears the old state keys and
    # carries the routing assignment over.
    plugin._routing["main_screen"] = "alice"
    old_key = body["display_key"]
    r = client.put("/displays/main_screen", json={"label": "Stage Left", "display_id": "left"})
    assert r.status_code == 200, r.text
    edited = r.json()
    assert edited["id"] == "left" and edited["label"] == "Stage Left"
    assert edited["display_key"] == old_key  # a rename must not break display links
    assert edited["source"] == "alice"  # the pin moved with the id
    assert api.state["display.main_screen.source"] is None  # old keys cleared

    # Delete removes it from the list, persists, and clears its state keys.
    r = client.delete("/displays/left")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert [x["id"] for x in api.saved[-1]["displays"]] == ["main_screen_2"]
    assert api.state["display.left.output_state"] is None
    assert "left" not in plugin._routing


def test_crud_validation_and_conflicts():
    client, _plugin_, _api = _ext_client(displays=[_DISPLAY])

    assert client.post("/displays", json={"label": "   "}).status_code == 422
    assert client.post("/displays", json={"label": "X", "display_id": "bad id!"}).status_code == 422
    assert client.post("/displays", json={"label": "X", "display_id": "auto"}).status_code == 422
    assert client.post("/displays", json={"label": "X", "display_id": "main"}).status_code == 409
    assert client.put("/displays/nope", json={"label": "N"}).status_code == 404
    assert client.delete("/displays/nope").status_code == 404


def test_regenerate_key_invalidates_old_display_links():
    client, plugin, _api = _ext_client(displays=[_DISPLAY])
    r = client.post("/displays/main/regenerate_key")
    assert r.status_code == 200
    new_key = r.json()["display_key"]
    assert new_key and new_key != "k3y-k3y-k3y"
    # The old key no longer passes the guest gate.
    with pytest.raises(HTTPException):
        plugin._guest_display("main", "k3y-k3y-k3y")
    assert plugin._guest_display("main", new_key) is plugin._displays[0]


def test_route_endpoint_and_status():
    client, plugin, api = _ext_client(
        displays=[_DISPLAY, _DISPLAY2], presence={"alice": 100, "bob": 200}
    )

    r = client.post("/displays/overflow/route", json={"source": "bob"})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "bob" and r.json()["showing"] == "bob"

    assert client.post("/displays/ghost/route", json={"source": "bob"}).status_code == 404
    assert client.post("/displays/main/route", json={"source": "bad name!"}).status_code == 422

    body = client.get("/status").json()
    assert body["active_presenters"] == 2
    assert body["join_url"] == "192.0.2.10:8080/present"  # for the IDE panel
    assert [p["name"] for p in body["presenters"]] == ["alice", "bob"]
    # Sources: auto first, then live presenters, in the {value, label} shape
    # the options_source picker parses.
    assert body["sources"][0]["value"] == "auto"
    assert [s["value"] for s in body["sources"][1:]] == ["alice", "bob"]
    assert body["display_ids"] == ["main", "overflow"]
    assert body["code"] == plugin._current_code()


# ──── Presence scan + poll ────


@pytest.mark.asyncio
async def test_scan_presenters_filters_paths(monkeypatch):
    plugin = _plugin(displays=[_DISPLAY])

    async def fake_get(path):
        return {
            "items": [
                {"name": "in/alice", "available": True},
                {"name": "in/bob", "available": False},  # not publishing yet
                {"name": "in/bad name", "available": True},  # unsafe segment
                {"name": "in/auto", "available": True},  # reserved sentinel
                {"name": "out/main", "available": True},  # not an ingest path
                {"name": "in", "available": True},  # no presenter segment
                {"name": "rooms/carol", "available": True},  # pre-0.2.0 naming
            ]
        }

    monkeypatch.setattr(plugin, "_api_get", fake_get)
    assert await plugin._scan_presenters() == {"alice"}


@pytest.mark.asyncio
async def test_poll_publishes_state_and_events_and_rotates_code(monkeypatch):
    plugin = _plugin(displays=[_DISPLAY])
    api = plugin.api
    scan_result = {"alice"}

    async def fake_scan():
        return set(scan_result)

    monkeypatch.setattr(plugin, "_scan_presenters", fake_scan)
    code_before, rotated_before = plugin._code

    # Presenter joins.
    await plugin._poll()
    assert api.state["running"] is True
    assert api.state["active_presenters"] == 1
    assert json.loads(api.state["presenters"])[0]["name"] == "alice"
    assert ("presenter_joined", {"name": "alice", "label": "alice"}) in api.events
    # The auto-routed display follows them.
    assert api.state["display.main.showing"] == "alice"
    assert api.state["display.main.output_state"] == "live"
    # The sources pick list gained the presenter.
    assert [s["value"] for s in json.loads(api.state["sources"])] == ["auto", "alice"]
    # A live session keeps its code (the displays aren't showing it anyway).
    assert plugin._code == (code_before, rotated_before)

    # Presenter leaves: back to idle, and the session's code is retired.
    scan_result = set()
    await plugin._poll()
    assert api.state["active_presenters"] == 0
    assert api.state["display.main.showing"] == ""
    assert api.state["display.main.output_state"] == "idle"
    assert api.events[-1] == ("presenter_left", {"name": "alice", "label": "alice"})
    assert plugin._code[1] != rotated_before  # rotated
    assert api.state["code"] == plugin._current_code()


@pytest.mark.asyncio
async def test_poll_marks_not_running_when_sidecar_unreachable(monkeypatch):
    plugin = _plugin(displays=[_DISPLAY])

    async def fake_scan():
        return None

    monkeypatch.setattr(plugin, "_scan_presenters", fake_scan)
    await plugin._poll()
    assert plugin.api.state["running"] is False


@pytest.mark.asyncio
async def test_stale_idle_code_rotates(monkeypatch):
    plugin = _plugin(displays=[_DISPLAY])

    async def fake_scan():
        return set()

    monkeypatch.setattr(plugin, "_scan_presenters", fake_scan)
    code = plugin._code[0]
    # Age the code past the idle rotation window.
    plugin._code = (code, -10_000.0)
    await plugin._poll()
    assert plugin._code[1] > 0  # re-stamped now


# ──── Config loading ────


@pytest.mark.asyncio
async def test_load_displays_backfills_key_and_drops_invalid():
    plugin = PresentPlugin()
    plugin.api = _FakeApi({
        "displays": [
            {"id": "main", "label": "Main Screen"},  # hand-edited: no key
            {"id": "bad id!", "label": "Rejected"},
            {"id": "auto", "label": "Reserved"},
            "not-a-dict",
        ],
        "rooms": [{"id": "room1"}],  # pre-0.2.0 leftover
    })
    await plugin._load_displays()
    assert [d["id"] for d in plugin._displays] == ["main"]
    assert plugin._displays[0]["display_key"]  # generated
    assert plugin._routing == {"main": "auto"}
    assert plugin.api.saved  # and persisted back
    assert "rooms" not in plugin.api.saved[-1]  # legacy list dropped


# ──── Display kinds (CRUD + views) ────


def test_add_display_defaults_to_browser():
    client, plugin, api = _ext_client()
    resp = client.post("/displays", json={"label": "Lobby TV"})
    assert resp.status_code == 200
    view = resp.json()
    assert view["kind"] == "browser"
    assert "stream_path" not in view
    assert plugin._output_calls == []  # no encoder pipeline for browser kind


def test_add_stream_display_mints_key_and_starts_output():
    client, plugin, api = _ext_client()
    resp = client.post("/displays", json={"label": "Decoder Feed", "kind": "stream"})
    assert resp.status_code == 200
    view = resp.json()
    assert view["kind"] == "stream"
    stored = plugin._find_display(view["id"])
    assert stored["stream_key"]
    assert view["stream_path"] == f"out/{view['id']}-{stored['stream_key']}"
    assert view["rtsp_port"] == 8554
    assert view["srt_port"] == 8899
    assert view["encoder_state"] == "idle"  # the (stubbed) controller's state
    # The persistent output comes up with the display.
    assert ("start", view["id"]) in plugin._output_calls
    # Stream displays keep a Display-page link too (handy for debugging).
    assert view["display_path"].startswith("/present/display/")


def test_add_display_rejects_unknown_kind():
    client, _plugin_, _api = _ext_client()
    resp = client.post("/displays", json={"label": "X", "kind": "hologram"})
    assert resp.status_code == 422


def test_edit_kind_browser_to_stream_and_back():
    client, plugin, _api = _ext_client(displays=[_DISPLAY])
    resp = client.put("/displays/main", json={"label": "Main Screen", "kind": "stream"})
    assert resp.status_code == 200
    assert resp.json()["kind"] == "stream"
    assert plugin._find_display("main")["stream_key"]
    assert ("start", "main") in plugin._output_calls

    resp = client.put("/displays/main", json={"label": "Main Screen", "kind": "browser"})
    assert resp.status_code == 200
    assert resp.json()["kind"] == "browser"
    assert "stream_path" not in resp.json()
    assert ("stop", "main") in plugin._output_calls


def test_edit_stream_display_label_only_does_not_restart_output():
    stream_display = {**_DISPLAY, "kind": "stream", "stream_key": "s3cret"}
    client, plugin, _api = _ext_client(displays=[stream_display])
    resp = client.put("/displays/main", json={"label": "Renamed", "kind": "stream"})
    assert resp.status_code == 200
    # Controller was pre-seeded as running; a label edit must not bounce it.
    assert plugin._output_calls == []


def test_edit_stream_display_rename_restarts_output():
    stream_display = {**_DISPLAY, "kind": "stream", "stream_key": "s3cret"}
    client, plugin, _api = _ext_client(displays=[stream_display])
    resp = client.put("/displays/main", json={"label": "Main", "display_id": "stage", "kind": "stream"})
    assert resp.status_code == 200
    # The id is part of the output path, so the old URL dies with the rename.
    assert ("stop", "main") in plugin._output_calls
    assert ("start", "stage") in plugin._output_calls


def test_delete_stream_display_stops_output():
    stream_display = {**_DISPLAY, "kind": "stream", "stream_key": "s3cret"}
    client, plugin, _api = _ext_client(displays=[stream_display])
    resp = client.delete("/displays/main")
    assert resp.status_code == 200
    assert ("stop", "main") in plugin._output_calls


def test_regenerate_stream_key():
    stream_display = {**_DISPLAY, "kind": "stream", "stream_key": "0ld-k3y"}
    client, plugin, _api = _ext_client(displays=[stream_display])
    resp = client.post("/displays/main/regenerate_stream_key")
    assert resp.status_code == 200
    new_key = plugin._find_display("main")["stream_key"]
    assert new_key and new_key != "0ld-k3y"
    assert resp.json()["stream_path"] == f"out/main-{new_key}"
    assert ("restart", "main") in plugin._output_calls

    # Browser displays have no stream key.
    client2, _p2, _a2 = _ext_client(displays=[_DISPLAY2])
    assert client2.post("/displays/overflow/regenerate_stream_key").status_code == 422
    assert client2.post("/displays/ghost/regenerate_stream_key").status_code == 404


@pytest.mark.asyncio
async def test_load_displays_normalizes_kind_and_mints_stream_key():
    plugin = PresentPlugin()
    plugin.api = _FakeApi({
        "displays": [
            {"id": "tv", "label": "TV", "kind": "stream"},  # no keys at all
            {"id": "lobby", "label": "Lobby", "display_key": "k", "kind": "bogus"},
        ],
    })
    await plugin._load_displays()
    by_id = {d["id"]: d for d in plugin._displays}
    assert by_id["tv"]["kind"] == "stream"
    assert by_id["tv"]["stream_key"]
    assert by_id["lobby"]["kind"] == "browser"
    assert plugin.api.saved  # normalization persisted


def test_ingest_url_injects_sidecar_credentials():
    plugin = _plugin()
    assert plugin._ingest_url("alice") == (
        "rtsp://openavc:sidecarpass@127.0.0.1:8554/in/alice"
    )


@pytest.mark.asyncio
async def test_publish_display_drives_stream_controller():
    plugin = _plugin(displays=[_DISPLAY])
    controller = _FakeController()
    plugin._controllers["main"] = controller
    plugin._presence = {"alice": 100}
    await plugin._publish_display("main", {"alice"})
    assert controller.shown == ["alice"]
    await plugin._publish_display("main", set())
    assert controller.shown == ["alice", ""]


# ──── Idle card files ────


def test_idle_card_writes_and_rotation(tmp_path):
    from integrations.present import output as output_mod

    card = output_mod.IdleCard(tmp_path / "card")
    card.write_all("Bench Space", "192.0.2.10:8080/present", "1234")
    assert (tmp_path / "card" / "space.txt").read_text(encoding="utf-8") == "Bench Space"
    assert (tmp_path / "card" / "join.txt").read_text(encoding="utf-8") == "192.0.2.10:8080/present"
    assert (tmp_path / "card" / "code.txt").read_text(encoding="utf-8") == "1234"
    card.write_code("9999")
    assert (tmp_path / "card" / "code.txt").read_text(encoding="utf-8") == "9999"
    # No stray temp file left behind (writes are atomic replaces).
    assert sorted(p.name for p in (tmp_path / "card").iterdir()) == [
        "code.txt", "join.txt", "space.txt",
    ]


def test_rotate_code_updates_card_file(tmp_path):
    from integrations.present import output as output_mod

    plugin = _plugin()
    plugin._card = output_mod.IdleCard(tmp_path / "card")
    code = plugin._rotate_code()
    assert (tmp_path / "card" / "code.txt").read_text(encoding="utf-8") == code


@pytest.mark.asyncio
async def test_refresh_card_writes_only_on_change(tmp_path, monkeypatch):
    from integrations.present import output as output_mod

    plugin = _plugin(config={"space_name": "Bench"})
    plugin._card = output_mod.IdleCard(tmp_path / "card")
    writes = []
    monkeypatch.setattr(plugin._card, "write_space", lambda v: writes.append(("space", v)))
    monkeypatch.setattr(plugin._card, "write_join", lambda v: writes.append(("join", v)))
    await plugin._refresh_card()
    await plugin._refresh_card()
    assert writes == [("space", "Bench"), ("join", "192.0.2.10:8080/present")]


# ──── Join URL vs TLS state ────


@pytest.mark.asyncio
async def test_join_url_plain_http():
    plugin = _plugin()
    assert await plugin._join_url() == "192.0.2.10:8080/present"
    # Port 80 drops from the short form.
    plugin._http_port = lambda: 80
    assert await plugin._join_url() == "192.0.2.10/present"


@pytest.mark.asyncio
async def test_join_url_https_is_scheme_qualified():
    """With TLS on the card must show the full https URL. A scheme-less
    host:8080 gets rewritten to https-on-8080 by browsers with automatic
    HTTPS upgrades (TLS handshake into the plain listener); a scheme-less
    host:8443 defaults to http against the TLS listener. Only the explicit
    https form works everywhere."""
    plugin = _plugin()
    plugin._tls_state = lambda: (True, 8443)
    assert await plugin._join_url() == "https://192.0.2.10:8443/present"
    # Port 443 drops (the https default).
    plugin._tls_state = lambda: (True, 443)
    assert await plugin._join_url() == "https://192.0.2.10/present"


@pytest.mark.asyncio
async def test_join_url_honors_configured_join_address_with_tls():
    plugin = _plugin(config={"join_address": "present.example.org"})
    plugin._tls_state = lambda: (True, 8443)
    assert await plugin._join_url() == "https://present.example.org:8443/present"


@pytest.mark.asyncio
async def test_join_url_trusted_cert_rides_the_redirect():
    """With a cloud-issued trusted certificate installed and the platform's
    http->https redirect on, the card shows the explicit http URL: the
    redirect lands guests on the certified hostname (real CA cert, no
    browser warning). The direct https form would serve the self-signed
    cert and bring the interstitial back."""
    plugin = _plugin(state={"system.cloud.cert_status": "installed"})
    plugin._tls_state = lambda: (True, 8443)
    plugin._redirect_http_enabled = lambda: True
    assert await plugin._join_url() == "http://192.0.2.10:8080/present"
    # Port 80 drops (the http default).
    plugin._http_port = lambda: 80
    assert await plugin._join_url() == "http://192.0.2.10/present"
    # A configured join address rides the redirect the same way.
    plugin2 = _plugin(
        state={"system.cloud.cert_status": "installed"},
        config={"join_address": "av.example.edu"},
    )
    plugin2._tls_state = lambda: (True, 8443)
    plugin2._redirect_http_enabled = lambda: True
    assert await plugin2._join_url() == "http://av.example.edu:8080/present"


@pytest.mark.asyncio
async def test_join_url_trusted_cert_needs_redirect_and_installed():
    # Cert installed but the redirect is off: an http address would dead-end
    # on the plain listener, so the https form stays.
    plugin = _plugin(state={"system.cloud.cert_status": "installed"})
    plugin._tls_state = lambda: (True, 8443)
    assert await plugin._join_url() == "https://192.0.2.10:8443/present"
    # Redirect on but no installed cert (absent or mid-issuance): the
    # redirect would land on the self-signed cert, so the https form stays.
    plugin = _plugin()
    plugin._tls_state = lambda: (True, 8443)
    plugin._redirect_http_enabled = lambda: True
    assert await plugin._join_url() == "https://192.0.2.10:8443/present"
    plugin = _plugin(state={"system.cloud.cert_status": "issuing"})
    plugin._tls_state = lambda: (True, 8443)
    plugin._redirect_http_enabled = lambda: True
    assert await plugin._join_url() == "https://192.0.2.10:8443/present"
    # TLS off: the short form is untouched by cert state.
    plugin = _plugin(state={"system.cloud.cert_status": "installed"})
    plugin._redirect_http_enabled = lambda: True
    assert await plugin._join_url() == "192.0.2.10:8080/present"


# ──── Local outputs (browser display on one of this host's video outputs) ────


class _FakeKiosk:
    """Stands in for kiosk.KioskManager in plugin wiring tests."""

    def __init__(self):
        self.synced = []  # every spec set handed to sync()
        self.states = {}
        self.names = {"MON-1": "Projector"}
        self.stopped = False

    async def sync(self, specs):
        self.synced.append({k: dict(v) for k, v in specs.items()})

    async def stop(self):
        self.stopped = True

    def state_for(self, display_id):
        return self.states.get(display_id, "starting")

    def output_name(self, output_id):
        return self.names.get(output_id, "")

    def output_connected(self, output_id):
        return output_id in self.names

    async def describe_outputs(self, in_use=None):
        return {
            "supported": True,
            "reason": "",
            "outputs": [{
                "id": "MON-1", "name": "Projector", "x": 1920, "y": 0,
                "width": 1920, "height": 1080, "primary": False,
                "in_use_by": (in_use or {}).get("MON-1", ""),
            }],
        }


def test_add_display_with_local_output_syncs_kiosk():
    client, plugin, api = _ext_client()
    plugin._kiosk = _FakeKiosk()
    resp = client.post("/displays", json={"label": "Lobby TV", "local_output": "MON-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "browser"
    assert body["local_output"] == "MON-1"
    assert body["local_state"] == "starting"
    assert body["local_output_name"] == "Projector"
    assert body["local_output_connected"] is True
    # The kiosk manager got the spec: this display's own URL on that output.
    spec = plugin._kiosk.synced[-1]["lobby_tv"]
    assert spec["output"] == "MON-1"
    assert spec["url"].startswith("http://127.0.0.1:8080/present/display/lobby_tv?key=")


def test_browser_display_without_local_output_reports_empty():
    client, plugin, api = _ext_client([_DISPLAY])
    plugin._kiosk = _FakeKiosk()
    body = client.get("/displays").json()[0]
    assert body["local_output"] == ""
    # The window-status fields only exist when a local output is set.
    assert "local_state" not in body


def test_local_output_rejected_on_stream_display():
    client, plugin, api = _ext_client()
    plugin._kiosk = _FakeKiosk()
    resp = client.post(
        "/displays",
        json={"label": "Decoder Feed", "kind": "stream", "local_output": "MON-1"},
    )
    assert resp.status_code == 422
    assert "browser display" in resp.json()["detail"]


def test_local_output_conflict_is_409():
    client, plugin, api = _ext_client(
        [{**_DISPLAY, "kind": "browser", "local_output": "MON-1"}]
    )
    plugin._kiosk = _FakeKiosk()
    resp = client.post("/displays", json={"label": "Second", "local_output": "MON-1"})
    assert resp.status_code == 409
    assert "Main Screen" in resp.json()["detail"]
    # Editing the holder itself is not a conflict.
    resp = client.put(
        "/displays/main",
        json={"label": "Main Screen", "local_output": "MON-1"},
    )
    assert resp.status_code == 200


def test_edit_kind_change_clears_local_output():
    client, plugin, api = _ext_client(
        [{**_DISPLAY, "kind": "browser", "local_output": "MON-1"}]
    )
    plugin._kiosk = _FakeKiosk()
    resp = client.put("/displays/main", json={"label": "Main Screen", "kind": "stream"})
    assert resp.status_code == 200
    assert "local_output" not in resp.json()  # stream views carry no local fields
    assert plugin._find_display("main").get("local_output") is None
    assert plugin._kiosk.synced[-1] == {}  # and the window is torn down


def test_edit_clears_local_output_with_empty_string():
    client, plugin, api = _ext_client(
        [{**_DISPLAY, "kind": "browser", "local_output": "MON-1"}]
    )
    plugin._kiosk = _FakeKiosk()
    # None (field omitted) keeps the assignment.
    resp = client.put("/displays/main", json={"label": "Main Screen"})
    assert resp.json()["local_output"] == "MON-1"
    # "" clears it.
    resp = client.put("/displays/main", json={"label": "Main Screen", "local_output": ""})
    assert resp.json()["local_output"] == ""
    assert plugin._find_display("main").get("local_output") is None
    assert plugin._kiosk.synced[-1] == {}


def test_outputs_route_marks_in_use():
    client, plugin, api = _ext_client(
        [{**_DISPLAY, "kind": "browser", "local_output": "MON-1"}]
    )
    plugin._kiosk = _FakeKiosk()
    body = client.get("/outputs").json()
    assert body["supported"] is True
    assert body["outputs"][0]["in_use_by"] == "main"


def test_outputs_route_before_start():
    client, plugin, api = _ext_client()
    body = client.get("/outputs").json()
    assert body["supported"] is False
    assert body["outputs"] == []


def test_local_display_url_follows_tls_state():
    plugin = _plugin([_DISPLAY])
    display = plugin._displays[0]
    url = plugin._local_display_url(display)
    assert url == "http://127.0.0.1:8080/present/display/main?key=k3y-k3y-k3y"
    plugin._tls_state = lambda: (True, 8443)
    url = plugin._local_display_url(display)
    assert url == "https://127.0.0.1:8443/present/display/main?key=k3y-k3y-k3y"


@pytest.mark.asyncio
async def test_load_displays_normalizes_local_output():
    plugin = PresentPlugin()
    plugin.api = _FakeApi({
        "displays": [
            {"id": "a", "label": "A", "display_key": "k", "kind": "browser",
             "local_output": "MON-1"},
            # Hand-edited: a stream display can't hold a local output.
            {"id": "b", "label": "B", "display_key": "k", "kind": "stream",
             "stream_key": "s", "local_output": "MON-2"},
            # Hand-edited: one output, one window — the duplicate loses it.
            {"id": "c", "label": "C", "display_key": "k", "kind": "browser",
             "local_output": "MON-1"},
        ],
    })
    await plugin._load_displays()
    by_id = {d["id"]: d for d in plugin._displays}
    assert by_id["a"].get("local_output") == "MON-1"
    assert "local_output" not in by_id["b"]
    assert "local_output" not in by_id["c"]


# ──── Sidecar ownership (orphan/imposter protection) ────


@pytest.mark.asyncio
async def test_poll_ignores_imposter_sidecar():
    """A reachable media API is not enough: if OUR supervisor isn't running,
    whatever answers the port is an imposter (an orphan from an unclean
    shutdown) and must not flip running back to True."""
    plugin = _plugin()
    plugin._supervisor = _FakeSupervisor(running=False)
    scans = []

    async def fake_scan():
        scans.append(1)
        return set()

    plugin._scan_presenters = fake_scan
    await plugin._poll()
    assert plugin.api.state["running"] is False
    assert not scans  # doesn't even ask the imposter


@pytest.mark.asyncio
async def test_start_refuses_when_media_ports_already_answer(tmp_path, monkeypatch):
    """Something already answering the control port means our sidecar could
    never bind — refuse with a clear message instead of crash-looping into
    the circuit breaker behind a half-working orphan."""
    plugin = _plugin()
    plugin._supervisor = None  # start() must refuse before ever creating one
    plugin.api.data_dir = tmp_path
    monkeypatch.setattr(
        PresentPlugin, "_resolve_dep", staticmethod(lambda name: tmp_path / name)
    )
    monkeypatch.setattr(PresentPlugin, "_ensure_executable", staticmethod(lambda p: None))

    async def imposter_answers(path):
        return {"items": []}

    plugin._api_get = imposter_answers
    with pytest.raises(RuntimeError, match="already using"):
        await plugin.start(plugin.api)
    assert plugin.api.state["running"] is False
    assert "unclean shutdown" in plugin.api.state["error"]
    assert plugin._supervisor is None  # never spawned anything
