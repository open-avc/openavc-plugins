"""
Tests for the Video Panel plugin.

Covers the self-contained SidecarSupervisor (drain, clean stop, crash-restart,
and the crash-loop circuit breaker) using throwaway Python child processes, plus
the plugin's pure helpers (credential-injecting RTSP source URLs and the
locked-down MediaMTX config it generates).

The supervisor tests need only the standard library. The helper tests import the
plugin class, which pulls in fastapi/httpx; they skip if those aren't available.

Run from the openavc-plugins root: pytest tests/test_video_panel_plugin.py -v
"""

import asyncio
import json
import sys
from fnmatch import fnmatch
from pathlib import Path

import pytest

# Plugins root, so the plugin imports by its package path.
_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))

from integrations.video_panel import sidecar as sidecar_mod
from integrations.video_panel.sidecar import SidecarSupervisor

try:
    import yaml
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from integrations.video_panel.video_panel_plugin import VideoPanelPlugin

    _PLUGIN_IMPORTABLE = True
except Exception:  # fastapi/httpx/yaml not installed in this environment
    _PLUGIN_IMPORTABLE = False


# A child that prints to both streams then blocks, and one that exits at once.
_LONG_RUNNER = [
    sys.executable,
    "-u",
    "-c",
    "import sys, time; print('alive', flush=True); "
    "sys.stderr.write('warmup\\n'); sys.stderr.flush(); time.sleep(60)",
]
_CRASHER = [sys.executable, "-c", "import sys; sys.exit(7)"]


async def _wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


# ──── SidecarSupervisor ────


@pytest.mark.asyncio
async def test_supervisor_starts_drains_and_stops():
    logs = []
    sup = SidecarSupervisor(_LONG_RUNNER, name="dummy", log=lambda m, lvl: logs.append(m))
    await sup.start()
    try:
        assert sup.running
        assert sup.pid is not None
        # Both stdout and stderr are drained without the child deadlocking.
        assert await _wait_for(lambda: any("alive" in m for m in logs))
        assert await _wait_for(lambda: any("warmup" in m for m in logs))
    finally:
        await sup.stop()
    assert not sup.running


@pytest.mark.asyncio
async def test_supervisor_clean_stop_does_not_count_as_crash(monkeypatch):
    monkeypatch.setattr(sidecar_mod, "_BACKOFF_SCHEDULE", (0.01,))
    broke = False

    async def on_break(_reason):
        nonlocal broke
        broke = True

    sup = SidecarSupervisor(_LONG_RUNNER, name="dummy", on_circuit_break=on_break)
    await sup.start()
    assert sup.running
    await sup.stop()
    assert not sup.running
    # A stop() must not be misread as a crash and trigger a restart.
    await asyncio.sleep(0.1)
    assert broke is False


@pytest.mark.asyncio
async def test_supervisor_circuit_breaks_on_crash_loop(monkeypatch):
    # Shrink the schedule so a crash loop trips the breaker in well under a second.
    monkeypatch.setattr(sidecar_mod, "_BACKOFF_SCHEDULE", (0.01,))
    monkeypatch.setattr(sidecar_mod, "_CIRCUIT_FAILURES", 3)
    monkeypatch.setattr(sidecar_mod, "_CIRCUIT_WINDOW", 60.0)

    statuses = []
    broke = asyncio.Event()

    async def on_status(s):
        statuses.append(s)

    async def on_break(_reason):
        broke.set()

    sup = SidecarSupervisor(
        _CRASHER, name="crasher", on_status=on_status, on_circuit_break=on_break
    )
    await sup.start()
    try:
        await asyncio.wait_for(broke.wait(), timeout=5.0)
        assert not sup.running
        assert "failed" in statuses
        assert "restarting" in statuses  # it did retry before giving up
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_bind_to_process_lifetime_smoke():
    """Binding a real child to this process's lifetime must be silent, and an
    unopenable pid must be a no-op (never an exception in a spawn path)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)"
    )
    try:
        sidecar_mod.bind_to_process_lifetime(proc.pid)
    finally:
        proc.kill()
        await proc.wait()
    sidecar_mod.bind_to_process_lifetime(0)  # pid 0 can't be opened: no-op


# ──── Plugin helpers ────


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_stream_source_url_injects_credentials():
    f = VideoPanelPlugin._stream_source_url
    assert (
        f({"rtsp_url": "rtsp://cam/stream1", "username": "admin", "password": "p@ss/word"})
        == "rtsp://admin:p%40ss%2Fword@cam/stream1"
    )
    # Credentials already in the URL are left untouched.
    assert f({"rtsp_url": "rtsp://u:p@cam/s", "username": "x", "password": "y"}) == "rtsp://u:p@cam/s"
    # No username -> URL unchanged.
    assert f({"rtsp_url": "rtsp://cam/s"}) == "rtsp://cam/s"
    # Missing / malformed URLs are rejected.
    assert f({"rtsp_url": ""}) is None
    assert f({"rtsp_url": "not-a-url"}) is None


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_render_config_is_valid_and_locked_down():
    plugin = VideoPanelPlugin()
    plugin._auth_pass = "a1b2c3d4e5f6"
    plugin._detect_local_ip = lambda: "192.0.2.10"
    cfg = yaml.safe_load(plugin._render_config())

    assert cfg["api"] is True and cfg["apiAddress"] == "127.0.0.1:9997"
    assert cfg["webrtc"] is True and cfg["webrtcAddress"] == "127.0.0.1:8889"
    assert cfg["webrtcLocalUDPAddress"] == ":8189"
    # The detected LAN address rides in every WebRTC offer as an extra host
    # candidate; pion's own interface list goes stale after a host IP change.
    assert cfg["webrtcAdditionalHosts"] == ["192.0.2.10"]
    assert plugin._rendered_host == "192.0.2.10"
    # RTSP is on, but only as a localhost TCP loopback for the transcode
    # pipeline; TCP-only keeps the UDP RTP/RTCP listeners closed.
    assert cfg["rtsp"] is True
    assert cfg["rtspAddress"] == "127.0.0.1:8556"
    assert cfg["rtspTransports"] == ["tcp"]
    # HLS is on and idle: it exists for viewers who arrived over the cloud
    # tunnel, where WebRTC's UDP media never lands. MediaMTX only muxes it once
    # a reader asks, so nothing extra is ingested for it.
    assert cfg["hls"] is True
    assert cfg["hlsAddress"] == "127.0.0.1:8890"
    assert cfg["hlsVariant"] == "lowLatency"
    assert cfg["hlsAlwaysRemux"] is False
    # Other protocols we don't use stay disabled. srt stays off because we are
    # the caller, not the listener -- it opens no inbound port.
    assert cfg["rtmp"] is False
    assert cfg["srt"] is False

    users = cfg["authInternalUsers"]
    assert users[0]["user"] == "openavc"
    assert users[0]["pass"] == "a1b2c3d4e5f6"
    assert {p["action"] for p in users[0]["permissions"]} == {"publish", "read", "playback"}
    # The localhost "any" user covers the control API plus the transcode ffmpeg's
    # credential-free read of the raw path and publish of the H.264 result.
    assert users[1]["user"] == "any"
    assert users[1]["ips"] == ["127.0.0.1", "::1"]
    assert {p["action"] for p in users[1]["permissions"]} == {"api", "read", "publish"}


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_plugin_info_manifest_shape():
    info = VideoPanelPlugin.PLUGIN_INFO
    assert info["id"] == "video_panel"
    assert info["category"] == "integration"
    assert info["min_openavc_version"] == "0.25.0"
    assert info["platforms"] == ["win_x64", "linux_x64", "linux_arm64"]
    assert "http_endpoints" in info["capabilities"]

    deps = {d["id"]: d for d in info["native_dependencies"]}
    assert set(deps) == {"mediamtx", "ffmpeg"}
    for dep in deps.values():
        for platform_key in ("win_x64", "linux_x64", "linux_arm64"):
            entry = dep["platforms"][platform_key]
            assert entry["url"].startswith("https://github.com/")
            assert entry["extract"]


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_video_stream_element_exposes_channel_field():
    # Runtime source switching: the Video Stream element must offer a `channel`
    # config field so an integrator can bind it to a selection key. The panel JS
    # then follows plugin.video_panel.selection.<channel> at runtime. A
    # single-line free-text type (not select) is required — the channel name is
    # author-defined. ("string" is the single-line input; "text" now renders as
    # a multi-line textarea in the panel-element Properties form.)
    element = next(
        e for e in VideoPanelPlugin.EXTENSIONS["panel_elements"]
        if e["type"] == "video_stream"
    )
    fields = {f["key"]: f for f in element["config_schema"]}
    assert "stream_id" in fields  # static source still present (the fallback)
    assert fields["channel"]["type"] == "string"


# ──── Probe parsing (pure) ────

_PROBE_HEVC = (
    "Input #0, rtsp, from 'rtsp://cam/1':\n"
    "  Duration: N/A, start: 0.000000, bitrate: N/A\n"
    "    Stream #0:0: Video: hevc (Main), yuvj420p(pc, bt709), 1920x1080, 20 fps, 20 tbr, 90k tbn\n"
    "At least one output file must be specified\n"
)
_PROBE_H264_BASELINE = (
    "Input #0, rtsp, from 'rtsp://cam/2':\n"
    "    Stream #0:0: Video: h264 (Constrained Baseline), yuv420p(progressive), 1280x720, 15 fps, 15 tbr\n"
    "    Stream #0:1: Audio: aac, 48000 Hz, stereo\n"
)
_PROBE_H264_HIGH = (
    "Input #0, rtsp, from 'rtsp://cam/3':\n"
    "    Stream #0:0[0x100]: Video: h264 (High), yuvj420p(pc), 2592x1944, 20 fps\n"
)


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_parse_probe_hevc_recommends_transcode():
    r = VideoPanelPlugin._parse_probe(_PROBE_HEVC)
    assert r["success"] is True
    assert r["codec"] == "hevc" and r["profile"] == "Main"
    assert (r["width"], r["height"]) == (1920, 1080)
    assert r["fps"] == 20.0
    assert r["transcode_recommended"] is True


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_parse_probe_h264_baseline_is_clean():
    r = VideoPanelPlugin._parse_probe(_PROBE_H264_BASELINE)
    assert r["codec"] == "h264" and "Baseline" in r["profile"]
    assert (r["width"], r["height"]) == (1280, 720)
    assert r["transcode_recommended"] is False


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_parse_probe_h264_high_plays_without_transcode():
    r = VideoPanelPlugin._parse_probe(_PROBE_H264_HIGH)
    assert r["codec"] == "h264" and r["profile"] == "High"
    assert (r["width"], r["height"]) == (2592, 1944)
    assert r["transcode_recommended"] is False


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_parse_probe_surfaces_auth_and_unreachable_errors():
    auth = VideoPanelPlugin._parse_probe("rtsp://cam: 401 Unauthorized\n")
    assert auth["success"] is False and "password" in auth["message"].lower()
    down = VideoPanelPlugin._parse_probe("rtsp://cam: Connection refused\n")
    assert down["success"] is False and "reach" in down["message"].lower()


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_slugify_and_entry_shaping():
    assert VideoPanelPlugin._slugify("Front Door Cam!") == "front_door_cam"
    assert VideoPanelPlugin._slugify("   ") == "stream"

    class _In:  # mimic CameraIn attribute access
        name = "  Lobby  "
        rtsp_url = "  rtsp://cam/x  "
        username = "admin"
        password = "p"
        codec_hint = "auto"
        transcode = "always"
        hardware_accel = "auto"

    entry = VideoPanelPlugin._entry_from_input(_In(), "lobby")
    assert entry == {
        "stream_id": "lobby",
        "name": "Lobby",
        "rtsp_url": "rtsp://cam/x",
        "username": "admin",
        "password": "p",
        "codec_hint": "auto",
        "transcode": "always",
        "hardware_accel": "auto",
    }


# ──── Cameras CRUD over HTTP (fake api + mocked MediaMTX) ────


class _FakeApi:
    """Stands in for the scoped PluginAPI in CRUD endpoint tests."""

    def __init__(self, config=None):
        self._config = dict(config or {})
        self.saved = []
        self.state = {}
        self.subscriptions = []  # (pattern, callback) from state_subscribe
        self.proxy_calls = []
        self.proxy_location = None  # canned WHEP Location for POST responses
        self.logs = []  # (message, level) from log()

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

    async def state_get_pattern(self, pattern):
        return {k: v for k, v in self.state.items() if fnmatch(k, pattern)}

    async def state_subscribe(self, pattern, callback):
        self.subscriptions.append((pattern, callback))
        return f"sub-{len(self.subscriptions)}"

    def create_task(self, coro, name=None):
        return asyncio.ensure_future(coro)

    def log(self, message, level="info"):
        # Recorded rather than discarded: several behaviours here exist only to
        # SAY something useful (an unrenderable preview, a source nobody can
        # reach), so the message is the deliverable and worth asserting on.
        self.logs.append((message, level))

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


def _crud_client(monkeypatch, config=None):
    plugin = VideoPanelPlugin()
    plugin.api = _FakeApi(config)
    plugin._ffmpeg_bin = None
    plugin._streams = list((config or {}).get("streams", []))
    added, deleted = [], []

    async def fake_post(path, body):
        added.append((path, body))
        return True

    async def fake_delete(path):
        deleted.append(path)
        return True

    async def fake_get(path):
        return {"items": []}

    monkeypatch.setattr(plugin, "_api_post", fake_post)
    monkeypatch.setattr(plugin, "_api_delete", fake_delete)
    monkeypatch.setattr(plugin, "_api_get", fake_get)

    app = FastAPI()
    app.include_router(plugin._build_router())
    return TestClient(app), plugin, added, deleted


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_crud_add_list_edit_delete(monkeypatch):
    client, plugin, added, deleted = _crud_client(monkeypatch)

    # Add: stream_id auto-derived from the name, MediaMTX path created, persisted.
    r = client.post("/streams", json={"name": "Front Door", "rtsp_url": "rtsp://cam/1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stream_id"] == "front_door" and body["status"] == "idle"
    assert added[-1][0] == "/v3/config/paths/add/front_door"
    assert added[-1][1] == {"source": "rtsp://cam/1", "sourceOnDemand": True}
    assert plugin.api.saved[-1]["streams"][0]["stream_id"] == "front_door"

    # A second stream with the same name gets a unique id.
    r = client.post("/streams", json={"name": "Front Door", "rtsp_url": "rtsp://cam/2"})
    assert r.json()["stream_id"] == "front_door_2"

    # List returns both.
    listing = client.get("/streams").json()["streams"]
    assert {c["stream_id"] for c in listing} == {"front_door", "front_door_2"}

    # Edit changes the source: delete-then-add against the live sidecar.
    deleted.clear(); added.clear()
    r = client.put("/streams/front_door", json={"name": "Front", "rtsp_url": "rtsp://cam/9"})
    assert r.status_code == 200, r.text
    assert "/v3/config/paths/delete/front_door" in deleted
    assert added[-1] == ("/v3/config/paths/add/front_door", {"source": "rtsp://cam/9", "sourceOnDemand": True})

    # Delete removes it from the list, the sidecar, and persists.
    r = client.delete("/streams/front_door")
    assert r.status_code == 200 and r.json()["status"] == "deleted"
    assert "/v3/config/paths/delete/front_door" in deleted
    assert [c["stream_id"] for c in plugin.api.saved[-1]["streams"]] == ["front_door_2"]


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_crud_validation_and_conflicts(monkeypatch):
    client, plugin, _added, _deleted = _crud_client(monkeypatch)

    # Malformed URL is rejected before anything is saved.
    assert client.post("/streams", json={"name": "X", "rtsp_url": "not-a-url"}).status_code == 422
    # An explicit, duplicate stream_id conflicts.
    client.post("/streams", json={"name": "A", "rtsp_url": "rtsp://cam/1", "stream_id": "cam_a"})
    dup = client.post("/streams", json={"name": "B", "rtsp_url": "rtsp://cam/2", "stream_id": "cam_a"})
    assert dup.status_code == 409
    # Editing a missing stream is a 404.
    assert client.put("/streams/nope", json={"name": "N", "rtsp_url": "rtsp://cam/3"}).status_code == 404
    # Bad stream-id characters are rejected.
    bad = client.post("/streams", json={"name": "C", "rtsp_url": "rtsp://cam/4", "stream_id": "bad id!"})
    assert bad.status_code == 422


# ──── Transcode path wiring ────


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_should_transcode_decision():
    st = VideoPanelPlugin._should_transcode
    assert st({"transcode": "never", "codec_hint": "h265"}) is False
    assert st({"transcode": "always", "codec_hint": "h264"}) is True
    assert st({"transcode": "auto", "codec_hint": "h265"}) is True
    assert st({"transcode": "auto", "codec_hint": "hevc"}) is True
    # auto passes through only for confirmed H.264.
    assert st({"transcode": "auto", "codec_hint": "h264"}) is False
    # Unknown/undetermined codec under auto transcodes (safe default), so an
    # HEVC source the probe couldn't read still plays instead of going black.
    assert st({"transcode": "auto", "codec_hint": "auto"}) is True
    assert st({"transcode": "auto", "codec_hint": ""}) is True
    assert st({}) is True


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_src_path_naming():
    assert VideoPanelPlugin._src_path("front_door") == "front_door__src"


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_transcode_creates_paired_source_and_ondemand_paths(monkeypatch):
    client, plugin, added, _deleted = _crud_client(monkeypatch)
    plugin._ffmpeg_bin = "/opt/ffmpeg"

    async def fake_resolve(_ha):
        return "libopenh264"

    monkeypatch.setattr(plugin, "_resolve_encoder", fake_resolve)

    r = client.post("/streams", json={"name": "Cam", "rtsp_url": "rtsp://cam/1", "transcode": "always"})
    assert r.status_code == 200, r.text
    paths = dict(added)

    # The raw source path holds the camera URL (creds handled natively here).
    assert paths["/v3/config/paths/add/cam__src"] == {"source": "rtsp://cam/1", "sourceOnDemand": True}

    # The main path runs ffmpeg on demand to transcode and republish.
    main = paths["/v3/config/paths/add/cam"]
    assert main["runOnDemandRestart"] is True
    cmd = main["runOnDemand"]
    assert "/opt/ffmpeg" in cmd
    assert "rtsp://127.0.0.1:8556/cam__src" in cmd          # reads the raw path
    assert cmd.rstrip().endswith("rtsp://127.0.0.1:8556/cam")  # republishes H.264
    assert "libopenh264" in cmd
    assert "-rtsp_transport tcp" in cmd                      # output forced to TCP
    # The camera URL never appears in the runOnDemand command (it lives on __src).
    assert "rtsp://cam/1" not in cmd


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_transcode_delete_removes_source_sibling(monkeypatch):
    client, plugin, _added, deleted = _crud_client(monkeypatch)
    plugin._ffmpeg_bin = "/opt/ffmpeg"

    async def fake_resolve(_ha):
        return "libopenh264"

    monkeypatch.setattr(plugin, "_resolve_encoder", fake_resolve)

    client.post("/streams", json={"name": "Cam", "rtsp_url": "rtsp://cam/1", "transcode": "always"})
    deleted.clear()
    r = client.delete("/streams/cam")
    assert r.status_code == 200, r.text
    assert "/v3/config/paths/delete/cam" in deleted
    assert "/v3/config/paths/delete/cam__src" in deleted


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_transcode_requested_without_ffmpeg_falls_back_to_passthrough(monkeypatch):
    # _crud_client leaves _ffmpeg_bin = None.
    client, plugin, added, _deleted = _crud_client(monkeypatch)
    r = client.post("/streams", json={"name": "Cam", "rtsp_url": "rtsp://cam/1", "transcode": "always"})
    assert r.status_code == 200, r.text
    paths = dict(added)
    # A single passthrough path, no transcode sibling.
    assert paths["/v3/config/paths/add/cam"] == {"source": "rtsp://cam/1", "sourceOnDemand": True}
    assert "/v3/config/paths/add/cam__src" not in paths


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_probe_and_snapshot_need_ffmpeg(monkeypatch):
    client, _plugin, _added, _deleted = _crud_client(monkeypatch)
    # ffmpeg is unavailable in this fake -> probe reports 503, not a crash.
    r = client.post("/streams/probe", json={"rtsp_url": "rtsp://cam/1"})
    assert r.status_code == 503


# ──── WHEP reverse proxy ────


def _whep_client(streams=None, auth_pass="sidecarpass"):
    plugin = VideoPanelPlugin()
    plugin.api = _FakeApi({"streams": streams or []})
    plugin._auth_pass = auth_pass
    plugin._ffmpeg_bin = None
    plugin._streams = list(streams or [])
    app = FastAPI()
    # Mount at root so request.url.path is "/whep/<id>", which is what the
    # Location-rewrite reflects. The platform mounts it under /api/plugins/<id>/ext.
    app.include_router(plugin._build_router())
    return TestClient(app), plugin, plugin.api


_WHEP_STREAM = {
    "stream_id": "front_door",
    "name": "Front Door",
    "rtsp_url": "rtsp://cam/1",
    "username": "",
    "password": "",
}


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_whep_offer_proxies_to_sidecar_and_rewrites_location():
    client, _plugin, api = _whep_client(streams=[_WHEP_STREAM])
    api.proxy_location = "/front_door/whep/abc-123-uuid"

    r = client.post(
        "/whep/front_door",
        content=b"v=0\r\noffer",
        headers={"Content-Type": "application/sdp"},
    )
    assert r.status_code == 201, r.text
    # Forwarded to the localhost sidecar WHEP endpoint with read creds in
    # userinfo. allow_internal=True opts past proxy_to's SSRF guard (the
    # sidecar is on loopback, which is refused by default).
    assert api.proxy_calls[-1] == {
        "url": "http://openavc:sidecarpass@127.0.0.1:8889/front_door/whep",
        "method": "POST",
        "allow_internal": True,
    }
    # MediaMTX's path-absolute Location is rewritten to live under this mount so
    # the browser's PATCH/DELETE come back through the authenticated proxy.
    assert r.headers["location"] == "/whep/front_door/abc-123-uuid"


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_whep_trickle_and_teardown_target_the_session():
    client, _plugin, api = _whep_client(streams=[_WHEP_STREAM])
    expected = "http://openavc:sidecarpass@127.0.0.1:8889/front_door/whep/abc-123-uuid"

    pr = client.patch(
        "/whep/front_door/abc-123-uuid",
        content=b"a=ice-ufrag:x\r\n",
        headers={"Content-Type": "application/trickle-ice-sdpfrag"},
    )
    assert pr.status_code == 204
    assert api.proxy_calls[-1] == {
        "url": expected, "method": "PATCH", "allow_internal": True,
    }

    dr = client.delete("/whep/front_door/abc-123-uuid")
    assert dr.status_code == 200
    assert api.proxy_calls[-1] == {
        "url": expected, "method": "DELETE", "allow_internal": True,
    }


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_whep_offer_unknown_stream_is_404_without_hitting_sidecar():
    client, _plugin, api = _whep_client(streams=[])
    r = client.post(
        "/whep/ghost", content=b"v=0", headers={"Content-Type": "application/sdp"}
    )
    assert r.status_code == 404
    assert api.proxy_calls == []


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_whep_rejects_malformed_session_id():
    client, _plugin, api = _whep_client(streams=[_WHEP_STREAM])
    # Underscore is outside the UUID-ish secret charset -> rejected before proxying.
    r = client.delete("/whep/front_door/bad_secret")
    assert r.status_code == 422
    assert api.proxy_calls == []


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_the_plugin_asks_for_a_page_in_the_ide_nav():
    """Managing streams was a section part-way down the Program page.

    Under Assets, above Backups, on a page called Program: somewhere nobody
    looking for video would think to scroll to. The nav entry comes from this
    declaration, so a system without the plugin gains nothing.
    """
    views = VideoPanelPlugin.EXTENSIONS["views"]
    assert [v["id"] for v in views] == ["streams"]
    assert views[0]["label"] == "Video Streams"
    assert views[0]["renderer"] == "video_streams"


def test_video_stream_panel_element_extension_shape():
    elements = VideoPanelPlugin.EXTENSIONS["panel_elements"]
    assert len(elements) == 1
    el = elements[0]
    assert el["type"] == "video_stream"
    assert el["label"] == "Video Stream"
    assert el["renderer"] == "iframe"
    assert el["ext_auth"] is True
    assert el["sandbox_permissions"] == ["allow-same-origin"]
    assert el["allow_features"] == ["autoplay"]
    # The stream picker is driven by the plugin's published stream_ids list.
    stream_field = next(f for f in el["config_schema"] if f["key"] == "stream_id")
    assert stream_field["type"] == "select"
    assert stream_field["options_source"] == "plugin.video_panel.stream_ids"


# ──── Auto-discovered preview sources ────


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_discovered_stream_id_is_urlsafe_and_prefixed():
    f = VideoPanelPlugin._discovered_stream_id
    assert f("device.chazyctl.encoder.001") == "auto-chazyctl-encoder-001"
    assert f("device.cam1") == "auto-cam1"
    # Non-url-safe characters in an id collapse to hyphens, so the result stays
    # a valid stream id / MediaMTX path name.
    assert f("device.rm 2.encoder.01") == "auto-rm-2-encoder-01"


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_rebuild_discovers_mjpeg_sources(monkeypatch):
    client, plugin, _added, _deleted = _crud_client(monkeypatch)
    api = plugin.api
    api.state.update({
        "device.chazy.encoder.001.preview_url": "http://169.254.10.1:8080/?action=stream",
        "device.chazy.encoder.001.preview_format": "mjpeg",
        "device.chazy.encoder.001.label": "Podium PC",
        # Offline encoder: empty preview_url -> excluded from the list.
        "device.chazy.encoder.002.preview_url": "",
        "device.chazy.encoder.002.preview_format": "mjpeg",
    })
    await plugin._rebuild_discovered()

    assert set(plugin._discovered) == {"auto-chazy-encoder-001"}
    d = plugin._discovered["auto-chazy-encoder-001"]
    assert d["url"].endswith("?action=stream") and d["format"] == "mjpeg"

    listing = json.loads(api.state["stream_ids"])
    entry = next(e for e in listing if e["value"] == "auto-chazy-encoder-001")
    assert entry["label"] == "Podium PC" and entry["mode"] == "mjpeg"


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_a_source_with_no_stream_is_listed_with_what_is_missing(monkeypatch):
    """The dead end: a preview exists and one setting stands in the way.

    Before this the source published no URL and simply was not in the list,
    which is the same empty picker as a room with no video in it at all.
    """
    client, plugin, *_ = _crud_client(monkeypatch)
    api = plugin.api
    api.state.update({
        "device.vmix.name": "Stage vMix",
        "device.vmix.connected": True,
        "device.vmix.output.2.name": "vMix Output 2 - Preview",
        "device.vmix.output.2.preview_url": "",
        "device.vmix.output.2.preview_status": "needs_setup",
        "device.vmix.output.2.preview_setup_field": "srt_port_2",
        "device.vmix.output.2.preview_status_detail": "Enter the SRT Port.",
    })
    await plugin._rebuild_discovered()

    # Not playable: the play routes must never see it.
    assert plugin._discovered == {}
    assert plugin._is_known_stream("auto-vmix-output-2") is False

    listing = json.loads(api.state["stream_ids"])
    row = next(e for e in listing if e.get("id") == "auto-vmix-output-2")
    assert "value" not in row, "an unplayable row must not be pickable"
    assert row["status"] == "needs_setup"
    assert row["detail"] == "Enter the SRT Port."
    assert row["group"] == "Stage vMix"
    assert row["setup"] == {"device": "vmix", "field": "srt_port_2"}


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_an_unplayable_row_is_invisible_to_the_shared_option_parser(monkeypatch):
    """No version gate is needed, and this is why.

    A picker that predates this reads the list through a parser that drops any
    entry with no `value`. So an older platform shows exactly what it showed
    before -- nothing -- rather than offering a page a tile that can never
    draw. Mirrors normalizeOptionList in the IDE.
    """
    client, plugin, *_ = _crud_client(monkeypatch)
    api = plugin.api
    api.state.update({
        "device.vmix.output.1.preview_status": "unavailable",
        "device.vmix.output.1.preview_status_detail": "SRT is not running.",
        "device.vmix.output.2.preview_url": "srt://10.0.0.5:10000",
        "device.vmix.output.2.preview_format": "srt",
    })
    await plugin._rebuild_discovered()

    listing = json.loads(api.state["stream_ids"])
    old_parser_sees = [e["value"] for e in listing if "value" in e]
    assert old_parser_sees == ["auto-vmix-output-2"]


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_an_offline_device_is_marked_and_stays_pickable(monkeypatch):
    """A page gets built before the room is powered up.

    preview_url is never cleared on a disconnect, so an unplugged encoder used
    to sit in the picker looking exactly like a working one and fail at play
    time. It is marked now -- and still pickable, because hiding it is how a
    camera silently vanishes from a page somebody is in the middle of building.
    """
    client, plugin, *_ = _crud_client(monkeypatch)
    api = plugin.api
    api.state.update({
        "device.chazy.name": "Encoders",
        "device.chazy.connected": False,
        "device.chazy.encoder.001.preview_url": "http://169.254.10.1:8080/?action=stream",
        "device.chazy.encoder.001.preview_format": "mjpeg",
        "device.chazy.encoder.001.label": "Podium PC",
    })
    await plugin._rebuild_discovered()

    listing = json.loads(api.state["stream_ids"])
    row = next(e for e in listing if e.get("value") == "auto-chazy-encoder-001")
    assert row["status"] == "offline"
    assert "not connected" in row["detail"]
    assert row["group"] == "Encoders"


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_one_encoder_going_dark_on_a_live_frame_is_marked(monkeypatch):
    """The frame is ONE connected device carrying many encoders.

    Unplug one and the device's `connected` never moves -- so reading only
    that key showed the dead encoder exactly like its healthy neighbour, and a
    tile pointed at it reconnected to nothing forever. The sub-unit's own
    `online` is the only key that knows.
    """
    client, plugin, *_ = _crud_client(monkeypatch)
    api = plugin.api
    api.state.update({
        "device.chazy.name": "Encoders",
        "device.chazy.connected": True,
        "device.chazy.encoder.001.preview_url": "http://169.254.10.1:8080/?action=stream",
        "device.chazy.encoder.001.preview_format": "mjpeg",
        "device.chazy.encoder.001.label": "Podium PC",
        "device.chazy.encoder.001.online": False,
        "device.chazy.encoder.001.offline_detail":
            "Not answering. Check that it has power and a network connection.",
        "device.chazy.encoder.002.preview_url": "http://169.254.10.2:8080/?action=stream",
        "device.chazy.encoder.002.preview_format": "mjpeg",
        "device.chazy.encoder.002.label": "Rear Camera",
        "device.chazy.encoder.002.online": True,
    })
    await plugin._rebuild_discovered()

    listing = json.loads(api.state["stream_ids"])
    dark = next(e for e in listing if e.get("value") == "auto-chazy-encoder-001")
    assert dark["status"] == "offline"
    # The child's own sentence, not the generic one about the device.
    assert dark["detail"].startswith("Not answering")
    # Still pickable: the encoder is coming back, and the page is for it.
    assert dark["value"] == "auto-chazy-encoder-001"

    healthy = next(e for e in listing if e.get("value") == "auto-chazy-encoder-002")
    assert "status" not in healthy, "its neighbour must not be marked with it"


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_a_child_of_a_lost_frame_is_offline_whatever_it_last_said(monkeypatch):
    """`online: True` is only ever as fresh as the last poll before the link
    dropped. When the frame is gone, so is everything on it."""
    client, plugin, *_ = _crud_client(monkeypatch)
    api = plugin.api
    api.state.update({
        "device.chazy.connected": False,
        "device.chazy.encoder.001.preview_url": "http://169.254.10.1:8080/?action=stream",
        "device.chazy.encoder.001.preview_format": "mjpeg",
        "device.chazy.encoder.001.online": True,
    })
    await plugin._rebuild_discovered()

    row = next(
        e for e in json.loads(api.state["stream_ids"])
        if e.get("value") == "auto-chazy-encoder-001"
    )
    assert row["status"] == "offline"


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_a_devices_own_offline_detail_never_reaches_a_panel(monkeypatch):
    """Same key name at the device level, written for a different reader.

    "Install it and make sure it's on the system PATH" is for whoever is
    configuring the system. A wall panel gets the plain sentence instead.
    """
    client, plugin, *_ = _crud_client(monkeypatch)
    api = plugin.api
    api.state.update({
        "device.cam.connected": False,
        "device.cam.offline_detail":
            "Required client not found. Install it and make sure it's on the "
            "system PATH.",
        "device.cam.preview_url": "rtsp://169.254.10.9/stream",
        "device.cam.preview_format": "rtsp",
    })
    await plugin._rebuild_discovered()

    row = next(
        e for e in json.loads(api.state["stream_ids"])
        if e.get("value") == "auto-cam"
    )
    assert row["status"] == "offline"
    assert "system PATH" not in row["detail"]
    assert "not connected" in row["detail"]


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_a_missing_setting_outranks_an_unreachable_device(monkeypatch):
    """The port can be typed with the device switched off, so say that instead.

    "Connect the device" is not the next step when the next step is a number
    the device was never going to report anyway.
    """
    client, plugin, *_ = _crud_client(monkeypatch)
    api = plugin.api
    api.state.update({
        "device.vmix.connected": False,
        "device.vmix.output.2.preview_status": "needs_setup",
        "device.vmix.output.2.preview_setup_field": "srt_port_2",
        "device.vmix.output.2.preview_status_detail": "Enter the SRT Port.",
        # ...while a source with nothing to fill in defers to the device.
        "device.vmix.output.1.preview_status": "unavailable",
        "device.vmix.output.1.preview_status_detail": "SRT is not running.",
    })
    await plugin._rebuild_discovered()

    listing = json.loads(api.state["stream_ids"])
    rows = {e["id"]: e for e in listing if "id" in e}
    assert rows["auto-vmix-output-2"]["status"] == "needs_setup"
    assert rows["auto-vmix-output-2"]["detail"] == "Enter the SRT Port."
    assert rows["auto-vmix-output-1"]["status"] == "offline"


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_a_ready_source_claims_no_status(monkeypatch):
    """A working source is the quiet one. A badge on it is noise."""
    client, plugin, *_ = _crud_client(monkeypatch)
    api = plugin.api
    api.state.update({
        "device.vmix.connected": True,
        "device.vmix.output.2.preview_url": "srt://10.0.0.5:10000",
        "device.vmix.output.2.preview_format": "srt",
        "device.vmix.output.2.preview_status": "",
    })
    await plugin._rebuild_discovered()

    listing = json.loads(api.state["stream_ids"])
    row = next(e for e in listing if e.get("value") == "auto-vmix-output-2")
    assert "status" not in row and "detail" not in row


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_the_status_keys_are_watched_as_well(monkeypatch):
    """Filling the port has to move the row without a restart."""
    client, plugin, *_ = _crud_client(monkeypatch)
    await plugin._setup_discovery()
    watched = {pattern for pattern, _cb in plugin.api.subscriptions}
    assert {
        "device.*.preview_status",
        "device.*.preview_status_detail",
        "device.*.preview_setup_field",
        "device.*.connected",
    } <= watched


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_discovery_label_falls_back_to_name(monkeypatch):
    client, plugin, *_ = _crud_client(monkeypatch)
    api = plugin.api
    api.state.update({
        "device.chazy.encoder.003.preview_url": "http://x/?action=stream",
        "device.chazy.encoder.003.name": "ENC 3",  # no user label set
    })
    await plugin._rebuild_discovered()
    listing = json.loads(api.state["stream_ids"])
    entry = next(e for e in listing if e["value"] == "auto-chazy-encoder-003")
    assert entry["label"] == "ENC 3"


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_name_change_refreshes_discovered_label(monkeypatch):
    # Regression: the dropdown label falls back to `name` when no user label is
    # set, so a `name` change (the common case — a device-reported encoder
    # rename) must trigger a rebuild. _setup_discovery has to subscribe to
    # `device.*.name`, not just `device.*.label`.
    client, plugin, *_ = _crud_client(monkeypatch)
    api = plugin.api
    api.state.update({
        "device.chazy.encoder.001.preview_url": "http://x/?action=stream",
        "device.chazy.encoder.001.name": "ENC 1",  # no user label set
    })
    await plugin._setup_discovery()

    def _label():
        listing = json.loads(api.state["stream_ids"])
        return next(e["label"] for e in listing if e["value"] == "auto-chazy-encoder-001")

    assert _label() == "ENC 1"

    # The driver reports a new name; fire the matching subscription callback and
    # let the debounced rebuild run.
    name_cbs = [cb for pat, cb in api.subscriptions if pat == "device.*.name"]
    assert name_cbs, "discovery must subscribe to device.*.name"
    api.state["device.chazy.encoder.001.name"] = "Chazy ENC 1"
    for cb in name_cbs:
        cb("device.chazy.encoder.001.name", "Chazy ENC 1", "ENC 1")
    await plugin._rebuild_task

    assert _label() == "Chazy ENC 1"


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_discovered_rtsp_registers_and_removes_mediamtx_path(monkeypatch):
    client, plugin, added, deleted = _crud_client(monkeypatch)
    plugin._ffmpeg_bin = None  # no transcode available -> passthrough path
    api = plugin.api
    api.state.update({
        "device.gen1.encoder.001.preview_url": "rtsp://169.254.5.5:554/sub",
        "device.gen1.encoder.001.preview_format": "rtsp",
        "device.gen1.encoder.001.label": "Cam A",
    })
    await plugin._rebuild_discovered()

    # An RTSP preview rides the MediaMTX -> WHEP pipeline: a path is registered,
    # and it is listed as a webrtc-mode source.
    assert (
        "/v3/config/paths/add/auto-gen1-encoder-001",
        {"source": "rtsp://169.254.5.5:554/sub", "sourceOnDemand": True},
    ) in added
    listing = json.loads(api.state["stream_ids"])
    entry = next(e for e in listing if e["value"] == "auto-gen1-encoder-001")
    assert entry["mode"] == "webrtc"

    # When the source goes away, its sidecar path is torn down.
    api.state["device.gen1.encoder.001.preview_url"] = ""
    await plugin._rebuild_discovered()
    assert "/v3/config/paths/delete/auto-gen1-encoder-001" in deleted
    assert "auto-gen1-encoder-001" not in plugin._discovered


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_configured_stream_takes_precedence_over_discovered(monkeypatch):
    client, plugin, *_ = _crud_client(monkeypatch)
    api = plugin.api
    # A configured stream whose id collides with a discovered source id.
    plugin._streams = [{"stream_id": "auto-x-encoder-001", "name": "Manual"}]
    api.state.update({
        "device.x.encoder.001.preview_url": "http://x/?action=stream",
        "device.x.encoder.001.preview_format": "mjpeg",
    })
    await plugin._rebuild_discovered()

    listing = json.loads(api.state["stream_ids"])
    matching = [e for e in listing if e["value"] == "auto-x-encoder-001"]
    # Listed once, as the configured (webrtc) stream — not duplicated.
    assert len(matching) == 1
    assert matching[0]["label"] == "Manual" and matching[0]["mode"] == "webrtc"


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_resolve_mjpeg_url():
    plugin = VideoPanelPlugin()
    plugin._discovered = {
        "auto-a": {"label": "A", "url": "http://x/?action=stream", "format": "mjpeg"},
        "auto-b": {"label": "B", "url": "rtsp://y/s", "format": "rtsp"},
    }
    assert plugin._resolve_mjpeg_url("auto-a") == "http://x/?action=stream"
    assert plugin._resolve_mjpeg_url("auto-b") is None  # rtsp is served via WHEP
    assert plugin._resolve_mjpeg_url("nope") is None


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_mjpeg_route_resolves_known_and_404s_unknown(monkeypatch):
    client, plugin, *_ = _crud_client(monkeypatch)
    plugin._discovered = {
        "auto-a": {"label": "A", "url": "http://enc/?action=stream", "format": "mjpeg"},
    }
    captured = {}

    async def fake_stream(url):
        from starlette.responses import Response as _Resp

        captured["url"] = url
        return _Resp(content=b"frame", media_type="multipart/x-mixed-replace")

    monkeypatch.setattr(plugin, "_mjpeg_stream_response", fake_stream)

    ok = client.get("/mjpeg/auto-a")
    assert ok.status_code == 200 and captured["url"] == "http://enc/?action=stream"
    # Unknown / non-MJPEG ids 404.
    assert client.get("/mjpeg/auto-missing").status_code == 404


# ──── Address-change sidecar rebuild ────


class _FakeSupervisor:
    """Stands in for the SidecarSupervisor; the address-change path reads
    .running and stops/starts it."""

    def __init__(self, running=True):
        self.running = running
        self.pid = 4242
        self.restarts = 0  # completed stop -> start cycles

    async def stop(self):
        self.running = False

    async def start(self):
        self.running = True
        self.restarts += 1


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_status_poll_restarts_sidecar_when_the_address_moves(tmp_path, monkeypatch):
    """A host IP change under a running plugin (DHCP renewal, a commissioning
    move): the status poll rewrites the sidecar config with the new address
    and bounces MediaMTX — otherwise WebRTC advertises the dead address for
    the life of the process and WHEP playback wedges after the first session.
    The bounce must also re-add every stream path: registrations live in the
    sidecar process, not its config file."""
    stream = {
        "stream_id": "front_door", "name": "Front Door",
        "rtsp_url": "rtsp://cam/1", "username": "", "password": "",
        "codec_hint": "h264", "transcode": "never", "hardware_accel": "auto",
    }
    client, plugin, added, deleted = _crud_client(monkeypatch, {"streams": [stream]})
    plugin._auth_pass = "x"
    plugin._detect_local_ip = lambda: "192.0.2.10"
    plugin._supervisor = _FakeSupervisor()
    plugin._config_path = tmp_path / "mediamtx.yml"
    plugin._config_path.write_text(plugin._render_config(), encoding="utf-8")
    assert "'192.0.2.10'" in plugin._config_path.read_text(encoding="utf-8")

    async def ready():
        return True

    monkeypatch.setattr(plugin, "_wait_until_ready", ready)
    # A discovered RTSP preview holds a sidecar path too; the bounce re-adds it.
    plugin._discovered = {
        "auto-cam": {"label": "Cam", "url": "rtsp://169.254.5.5/sub", "format": "rtsp"},
    }
    plugin._discovered_sidecar = {"auto-cam": "rtsp://169.254.5.5/sub"}

    # Stable address: a normal status poll, no bounce.
    await plugin._poll_statuses()
    assert plugin._supervisor.restarts == 0
    assert plugin.api.state["streams.front_door"] == "idle"

    # The address moves: config rewritten, sidecar bounced, paths re-added.
    added.clear()
    plugin._detect_local_ip = lambda: "203.0.113.7"
    await plugin._poll_statuses()
    assert plugin._supervisor.restarts == 1
    assert "'203.0.113.7'" in plugin._config_path.read_text(encoding="utf-8")
    assert plugin._rendered_host == "203.0.113.7"
    assert (
        "/v3/config/paths/add/front_door",
        {"source": "rtsp://cam/1", "sourceOnDemand": True},
    ) in added
    assert (
        "/v3/config/paths/add/auto-cam",
        {"source": "rtsp://169.254.5.5/sub", "sourceOnDemand": True},
    ) in added

    # Stable at the new address: back to normal polling, no more bounces.
    await plugin._poll_statuses()
    assert plugin._supervisor.restarts == 1


# ──── Surviving a MediaMTX crash ────


def _reattach_plugin(monkeypatch, tmp_path, streams):
    """A started plugin with a stubbed MediaMTX API, ready to be crashed."""
    client, plugin, added, deleted = _crud_client(monkeypatch, {"streams": streams})
    plugin._auth_pass = "x"
    plugin._detect_local_ip = lambda: "192.0.2.10"
    plugin._config_path = tmp_path / "mediamtx.yml"
    plugin._config_path.write_text(plugin._render_config(), encoding="utf-8")

    async def ready():
        return True

    monkeypatch.setattr(plugin, "_wait_until_ready", ready)
    return client, plugin, added, deleted


_STREAM = {
    "stream_id": "front_door", "name": "Front Door",
    "rtsp_url": "rtsp://cam/1", "username": "", "password": "",
    "codec_hint": "h264", "transcode": "never", "hardware_accel": "auto",
}
_ADD_FRONT_DOOR = ("/v3/config/paths/add/front_door",
                   {"source": "rtsp://cam/1", "sourceOnDemand": True})


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_a_crashed_sidecar_comes_back_serving_the_streams(tmp_path, monkeypatch):
    """MediaMTX crashes, the supervisor respawns it, and the replacement process
    has an empty path table -- registrations live in the process, not the config
    file. Nothing else notices: the process is alive so health_check says ok, the
    status poll gets its 200 and puts `running` back to true, and every stream
    reads "idle" exactly as it does when nobody is watching. So the tiles are all
    dead and the plugin looks well. The respawn has to re-register."""
    _client, plugin, added, _deleted = _reattach_plugin(monkeypatch, tmp_path, [_STREAM])
    plugin._discovered = {
        "auto-cam": {"label": "Cam", "url": "rtsp://169.254.5.5/sub", "format": "rtsp"},
    }
    plugin._discovered_sidecar = {"auto-cam": "rtsp://169.254.5.5/sub"}
    added.clear()

    # What the supervisor reports across an unexpected exit and its respawn.
    await plugin._on_sidecar_status("restarting")
    assert plugin.api.state["running"] is False
    assert added == []  # nothing to register against a process that is not there
    await plugin._on_sidecar_status("running")

    assert _ADD_FRONT_DOOR in added, "the configured stream was not re-registered"
    assert (
        "/v3/config/paths/add/auto-cam",
        {"source": "rtsp://169.254.5.5/sub", "sourceOnDemand": True},
    ) in added, "the discovered preview was not re-registered"
    assert plugin.api.state["running"] is True
    assert plugin.api.state["sidecar"] == "running"


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_a_running_status_that_did_not_follow_a_crash_registers_nothing(
    tmp_path, monkeypatch
):
    """The other direction. The supervisor also reports "running" on the very
    first start and on a bounce we asked for, and both of those already register
    every path themselves -- once from start(), once from the restart. Treating
    every "running" as a re-attach would re-POST every path a second time, which
    MediaMTX rejects, so it would fill the log with failures at every startup."""
    _client, plugin, added, _deleted = _reattach_plugin(monkeypatch, tmp_path, [_STREAM])
    added.clear()

    await plugin._on_sidecar_status("running")
    assert added == []
    assert plugin.api.state["sidecar"] == "running"


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_a_circuit_broken_sidecar_is_not_re_attached(tmp_path, monkeypatch):
    """"failed" is the crash-loop breaker: there is no replacement process to
    register against, and the next "running" can only come from a deliberate
    restart that registers on its own."""
    _client, plugin, added, _deleted = _reattach_plugin(monkeypatch, tmp_path, [_STREAM])
    added.clear()

    await plugin._on_sidecar_status("restarting")
    await plugin._on_sidecar_status("failed")
    assert plugin.api.state["running"] is False
    await plugin._on_sidecar_status("running")
    assert added == []


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_the_supervisor_really_drives_the_re_attach(tmp_path, monkeypatch):
    """The wiring, through a real SidecarSupervisor and a real child process:
    a first run that exits non-zero and a second that stays up. Pinned end to
    end because the whole defect was a callback nobody was listening to."""
    monkeypatch.setattr(sidecar_mod, "_BACKOFF_SCHEDULE", (0.01,))
    _client, plugin, added, _deleted = _reattach_plugin(monkeypatch, tmp_path, [_STREAM])

    # Crashes once, then stays up: the flag file is what tells the two apart.
    flag = tmp_path / "spawned-once"
    child = [
        sys.executable, "-c",
        "import os, sys, time\n"
        "flag = sys.argv[1]\n"
        "if os.path.exists(flag):\n"
        "    time.sleep(60)\n"
        "open(flag, 'w').close()\n"
        "sys.exit(7)\n",
        str(flag),
    ]
    sup = SidecarSupervisor(child, name="crash-once", on_status=plugin._on_sidecar_status)
    plugin._supervisor = sup
    await sup.start()
    try:
        assert await _wait_for(lambda: _ADD_FRONT_DOOR in added, timeout=5.0), (
            "the respawned sidecar was never re-registered"
        )
        assert sup.running
        assert plugin.api.state["running"] is True
    finally:
        await sup.stop()


# ──── SRT ingest, and learning what a source actually carries ────


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_preview_format_resolves_declared_then_scheme():
    f = VideoPanelPlugin._preview_format
    # A format we can draw is taken as declared.
    assert f("srt", "srt://vmix:10000") == "srt"
    assert f("rtsp", "rtsp://cam/sub") == "rtsp"
    assert f("mjpeg", "http://enc/?action=stream") == "mjpeg"
    # The convention makes preview_format optional, so a URL alone resolves.
    assert f(None, "srt://vmix:10000") == "srt"
    assert f("", "rtsp://cam/sub") == "rtsp"
    assert f(None, "https://enc/?action=stream") == "mjpeg"
    # A format we do not know falls back to the scheme, which is better
    # evidence than the word: this is a newer driver more often than a typo.
    assert f("webrtc", "srt://vmix:10000") == "srt"
    # Nothing can draw it -> None, and the caller skips it entirely.
    assert f("ndi", "ndi://box/Cam 1") is None
    assert f(None, "ndi://box/Cam 1") is None


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_an_undrawable_preview_is_skipped_not_guessed(monkeypatch):
    """It used to fall back to mjpeg, which put an <img> on an srt:// URL.

    A source we would only ever draw wrong is worse in the picker than absent:
    the tile is dead and nothing says why.
    """
    client, plugin, added, _deleted = _crud_client(monkeypatch)
    plugin.api.state.update({
        "device.box.preview_url": "ndi://box/Cam 1",
        "device.box.preview_format": "ndi",
    })
    await plugin._rebuild_discovered()
    assert plugin._discovered == {}
    assert not any("auto-box" in path for path, _body in added)
    assert any("cannot draw" in m for m, _lvl in plugin.api.logs)


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_an_srt_preview_becomes_an_on_demand_sidecar_path(monkeypatch):
    """The vMix case: a driver publishes srt:// and it just appears."""
    client, plugin, added, deleted = _crud_client(monkeypatch)
    plugin._ffmpeg_bin = None
    api = plugin.api
    api.state.update({
        "device.vmix.output.2.preview_url": "srt://192.168.4.23:10000",
        "device.vmix.output.2.preview_format": "srt",
        "device.vmix.output.2.name": "vMix Output 2 - Program",
    })
    await plugin._rebuild_discovered()

    assert (
        "/v3/config/paths/add/auto-vmix-output-2",
        {"source": "srt://192.168.4.23:10000", "sourceOnDemand": True},
    ) in added
    listing = json.loads(api.state["stream_ids"])
    entry = next(e for e in listing if e["value"] == "auto-vmix-output-2")
    assert entry["label"] == "vMix Output 2 - Program"
    assert entry["mode"] == "webrtc"

    # Output stops SRT -> the path goes with it.
    api.state["device.vmix.output.2.preview_url"] = ""
    await plugin._rebuild_discovered()
    assert "/v3/config/paths/delete/auto-vmix-output-2" in deleted


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_srt_starts_passthrough_and_rtsp_keeps_its_shipped_default(monkeypatch):
    """The one place the two formats differ, and it is deliberate.

    Re-encoding a stream that is already H.264 spends real CPU on every panel
    that shows it. SRT gear overwhelmingly sends H.264, so it starts straight
    through. RTSP keeps the transcode-until-proven default every RTSP source
    in the field already has, because relaxing that on ones this work could not
    test is how somebody's working preview breaks.
    """
    client, plugin, *_ = _crud_client(monkeypatch)
    plugin._discovered = {
        "auto-srt": {"label": "vMix", "url": "srt://h:10000", "format": "srt"},
        "auto-cam": {"label": "Cam", "url": "rtsp://h/sub", "format": "rtsp"},
    }
    assert plugin._discovered_entry("auto-srt", "srt://h:10000")["codec_hint"] == "h264"
    assert plugin._discovered_entry("auto-cam", "rtsp://h/sub")["codec_hint"] == "auto"
    # And neither guess survives contact with what the sidecar reports.
    plugin._learned_codec["auto-srt"] = "other"
    assert plugin._discovered_entry("auto-srt", "srt://h:10000")["codec_hint"] == "other"


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_the_video_track_is_picked_out_of_what_mediamtx_reports():
    f = VideoPanelPlugin._video_codec
    # Measured shape from a live vMix SRT output.
    assert f(["MPEG-4 Audio", "H264"]) == "H264"
    assert f(["H265"]) == "H265"
    # Audio only, or nothing watching yet: no answer, so nothing is decided.
    assert f(["MPEG-4 Audio"]) is None
    assert f([]) is None
    assert f(None) is None


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_a_source_that_is_not_h264_corrects_itself(monkeypatch):
    """The guess is replaced by what the sidecar saw, and the path re-registered."""
    client, plugin, added, deleted = _crud_client(monkeypatch)
    plugin._ffmpeg_bin = "/usr/bin/ffmpeg"
    plugin._discovered = {
        "auto-enc": {"label": "Enc", "url": "srt://h:10000", "format": "srt"},
    }
    plugin._discovered_sidecar = {"auto-enc": "srt://h:10000"}

    async def encoder(_ha):
        return "libopenh264"

    monkeypatch.setattr(plugin, "_resolve_encoder", encoder)

    added.clear()
    deleted.clear()
    await plugin._learn_codecs([
        {"name": "auto-enc", "tracks": ["MPEG-4 Audio", "H265"], "readers": [{}]},
    ])
    assert plugin._learned_codec["auto-enc"] == "other"
    # Re-registered as the two-path transcode pair.
    assert "/v3/config/paths/delete/auto-enc" in deleted
    assert any(p == "/v3/config/paths/add/auto-enc__src" for p, _b in added)
    assert any("H265" in m for m, _lvl in plugin.api.logs)


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_a_confirmed_h264_source_is_left_alone(monkeypatch):
    """Confirming the assumption must not churn a working path."""
    client, plugin, added, deleted = _crud_client(monkeypatch)
    plugin._discovered = {
        "auto-vmix": {"label": "vMix", "url": "srt://h:10000", "format": "srt"},
    }
    plugin._discovered_sidecar = {"auto-vmix": "srt://h:10000"}
    added.clear()
    deleted.clear()
    await plugin._learn_codecs([
        {"name": "auto-vmix", "tracks": ["MPEG-4 Audio", "H264"], "readers": [{}]},
    ])
    # Nothing is recorded for an H264 source, deliberately: see the next test.
    assert "auto-vmix" not in plugin._learned_codec
    assert added == [] and deleted == []


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_learning_can_never_relax_a_source_out_of_transcoding(monkeypatch):
    """The invariant: learning turns transcoding ON, never off.

    MediaMTX names the codec FAMILY, not the profile. "H265" proves a browser
    cannot play it; "H264" does NOT prove a browser can — High 4:2:2 and the
    exotic levels are H264 too, and transcoding is what has been quietly making
    those work. So an RTSP source -- an AV-over-IP encoder, an IP camera,
    whatever a driver has pointed us at -- ships transcoding until proven
    otherwise and must stay that way whatever the sidecar reports.

    Without this the change would have been a silent regression on every
    existing RTSP source, and a delayed one: the switch landed on the next
    rebuild rather than at the moment of learning.
    """
    client, plugin, added, deleted = _crud_client(monkeypatch)
    url = "rtsp://169.254.5.5/sub"
    plugin._discovered = {"auto-cam": {"label": "Cam", "url": url, "format": "rtsp"}}
    plugin._discovered_sidecar = {"auto-cam": url}
    assert plugin._discovered_entry("auto-cam", url)["codec_hint"] == "auto"

    await plugin._learn_codecs([{"name": "auto-cam", "tracks": ["H264"], "readers": [{}]}])

    assert "auto-cam" not in plugin._learned_codec
    # The posture the source shipped with survives a later rebuild.
    entry = plugin._discovered_entry("auto-cam", url)
    assert entry["codec_hint"] == "auto"
    assert VideoPanelPlugin._should_transcode(entry) is True


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
async def test_a_watched_source_that_never_connects_says_so_once(monkeypatch):
    """MediaMTX explains itself on stderr, which is drained at debug level.

    Readers attached and the path never available is exactly the spinning-with-
    no-explanation case, so it gets a line somebody will actually read.
    """
    client, plugin, *_ = _crud_client(monkeypatch)
    plugin._discovered_sidecar = {"auto-vmix": "srt://192.168.4.23:10000"}
    items = [{"name": "auto-vmix", "available": False, "readers": [{}], "tracks": []}]

    plugin.api.logs.clear()
    plugin._warn_unwatchable(items)
    warnings = [m for m, lvl in plugin.api.logs if lvl == "warning"]
    assert len(warnings) == 1
    assert "192.168.4.23:10000" in warnings[0]

    # It does not repeat every five seconds.
    plugin._warn_unwatchable(items)
    assert len([m for m, lvl in plugin.api.logs if lvl == "warning"]) == 1

    # Nobody watching is not a fault, and recovery re-arms the warning.
    plugin._warn_unwatchable([{"name": "auto-vmix", "available": True, "readers": [{}]}])
    assert "auto-vmix" not in plugin._warned_unreachable


# ──── Delivery: how THIS viewer plays THIS stream ────
#
# WebRTC's media is UDP straight to a LAN address, so it serves a panel in the
# room and never serves somebody on the far side of the cloud tunnel. HLS does.
# Which one a viewer gets is therefore per viewer, and the plugin asks the
# platform rather than reading the tunnel header itself.


def _delivery_client(monkeypatch, viewer_access=None, streams=None, discovered=None):
    """A client whose PluginAPI answers `viewer_access` however a test says.

    `viewer_access=None` stands for an OLDER platform that has no such method,
    which is a case worth covering on its own: the plugin must keep working and
    must gate nothing.
    """
    plugin = VideoPanelPlugin()
    api = _FakeApi({"streams": streams or []})
    if viewer_access is not None:
        api.viewer_access = lambda request, capability: viewer_access
    plugin.api = api
    plugin._ffmpeg_bin = None
    plugin._streams = list(streams or [])
    plugin._discovered = dict(discovered or {})

    async def fake_get(path):
        return {"items": []}

    monkeypatch.setattr(plugin, "_api_get", fake_get)
    app = FastAPI()
    app.include_router(plugin._build_router())
    return TestClient(app), plugin, api


_ONE_STREAM = [{"stream_id": "cam1", "name": "Lectern", "url": "rtsp://10.0.0.5/s"}]


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_a_local_viewer_gets_webrtc(monkeypatch):
    client, _, _ = _delivery_client(monkeypatch, "local", streams=_ONE_STREAM)
    body = client.get("/delivery/cam1").json()
    assert body == {"stream_id": "cam1", "mode": "webrtc"}


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_a_tunnelled_viewer_gets_hls(monkeypatch):
    client, _, _ = _delivery_client(monkeypatch, "remote", streams=_ONE_STREAM)
    body = client.get("/delivery/cam1").json()
    assert body == {"stream_id": "cam1", "mode": "hls"}


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_a_tunnelled_viewer_without_the_add_on_is_told_so(monkeypatch):
    """Not a spinner and not a bare failure. The tile has to say what is true
    and what still works, because a viewer who sees nothing concludes the
    system is broken rather than unsold."""
    client, _, _ = _delivery_client(monkeypatch, "remote_blocked", streams=_ONE_STREAM)
    body = client.get("/delivery/cam1").json()
    assert body["mode"] == "blocked"
    assert "not included in this plan" in body["detail"]
    assert "still plays on panels in the space" in body["detail"]


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_an_older_platform_gates_nothing(monkeypatch):
    """No viewer_access on the API at all. Everybody is treated as local, which
    is exactly how this plugin behaved before the tunnel path existed. Failing
    the other way would take out panels on the LAN."""
    client, _, _ = _delivery_client(monkeypatch, None, streams=_ONE_STREAM)
    body = client.get("/delivery/cam1").json()
    assert body == {"stream_id": "cam1", "mode": "webrtc"}


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_an_mjpeg_source_stays_mjpeg_either_side_of_the_tunnel(monkeypatch):
    """MJPEG is ordinary multipart HTTP, so it crosses the tunnel as-is. There
    is no reason to remux it into HLS -- only a reason to gate it, below."""
    discovered = {
        "auto-enc1": {"label": "Encoder 1", "url": "http://10.0.0.9/?action=stream",
                      "format": "mjpeg"},
    }
    client, _, _ = _delivery_client(monkeypatch, "local", discovered=discovered)
    assert client.get("/delivery/auto-enc1").json()["mode"] == "mjpeg"
    client, _, _ = _delivery_client(monkeypatch, "remote", discovered=discovered)
    assert client.get("/delivery/auto-enc1").json()["mode"] == "mjpeg"


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_delivery_404s_for_a_stream_that_does_not_exist(monkeypatch):
    client, _, _ = _delivery_client(monkeypatch, "local", streams=_ONE_STREAM)
    assert client.get("/delivery/nope").status_code == 404


# ──── The gate sits where the bytes are, not only at the delivery call ────


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_a_blocked_viewer_cannot_fetch_hls_directly(monkeypatch):
    """Re-checked per fetch: a plan can be revoked mid-session, and the segment
    requests are the part that actually spends the bandwidth."""
    client, _, _ = _delivery_client(monkeypatch, "remote_blocked", streams=_ONE_STREAM)
    res = client.get("/hls/cam1/index.m3u8")
    assert res.status_code == 402
    assert "not included in this plan" in res.json()["detail"]


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_a_blocked_viewer_cannot_fetch_mjpeg_either(monkeypatch):
    """MJPEG carries no interframe compression, so it is the dearest of the
    three paths per minute. A switch that let the dearest one through would not
    be a switch."""
    discovered = {
        "auto-enc1": {"label": "Encoder 1", "url": "http://10.0.0.9/?action=stream",
                      "format": "mjpeg"},
    }
    client, _, _ = _delivery_client(monkeypatch, "remote_blocked", discovered=discovered)
    assert client.get("/mjpeg/auto-enc1").status_code == 402


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_a_local_viewer_is_never_refused_mjpeg(monkeypatch):
    """The LAN path must be unreachable from the entitlement, not merely
    allowed by it. Here the API answers "local" and the fetch proceeds to the
    upstream -- the 502 is the unreachable fake encoder, not a refusal."""
    discovered = {
        "auto-enc1": {"label": "Encoder 1", "url": "http://127.0.0.1:1/?action=stream",
                      "format": "mjpeg"},
    }
    client, _, _ = _delivery_client(monkeypatch, "local", discovered=discovered)
    assert client.get("/mjpeg/auto-enc1").status_code == 502


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_hls_file_names_cannot_climb_out_of_the_stream_path(monkeypatch):
    client, _, _ = _delivery_client(monkeypatch, "remote", streams=_ONE_STREAM)
    for bad in ("index.m3u8/../..", "..", "index.txt", "index"):
        assert client.get("/hls/cam1/" + bad).status_code in (404, 422)


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_the_hls_url_carries_the_sidecar_read_credentials(monkeypatch):
    _, plugin, _ = _delivery_client(monkeypatch, "remote", streams=_ONE_STREAM)
    plugin._auth_pass = "a1b2c3d4e5f6"
    url = plugin._hls_url("cam1", "index.m3u8")
    assert url == "http://openavc:a1b2c3d4e5f6@127.0.0.1:8890/cam1/index.m3u8"


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_a_standalone_panel_can_reach_the_new_routes():
    """A wall panel on a claimed instance holds a panel-scoped token, which
    opens only what the plugin declared. Both new routes have to be on that
    list or remote video 401s on exactly the surface it is for."""
    from integrations.video_panel.video_panel_plugin import _PANEL_PATHS

    assert "GET /delivery/*" in _PANEL_PATHS
    assert "GET /hls/*" in _PANEL_PATHS
    # Media routes only. Nothing that writes a stream is reachable this way.
    assert not any("streams" in entry for entry in _PANEL_PATHS)


# ── HLS through the tunnel: what a player derives from a playlist ──
#
# None of this reaches a panel in the room -- that one plays over WebRTC and
# never asks for a playlist. It is the remote viewer's whole path, and it was
# broken in three independent places at once, so each gets its own case.


def test_the_token_reaches_every_url_a_player_follows():
    """A relative URI REPLACES the query of the document it came from.

    So a token put on the playlist request reaches nothing after it, and on an
    instance with a password every media playlist and every segment is a 401 --
    which is every instance in the field.
    """
    from integrations.video_panel.video_panel_plugin import _rewrite_playlist

    playlist = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:9\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",URI="audio2_stream.m3u8?session=abc"\n'
        "#EXT-X-STREAM-INF:BANDWIDTH=2375980\n"
        "video1_stream.m3u8?session=abc\n"
    )
    out = _rewrite_playlist(playlist, "tok123")

    assert 'URI="audio2_stream.m3u8?session=abc&_plugin_token=tok123"' in out
    assert "video1_stream.m3u8?session=abc&_plugin_token=tok123" in out
    # The directives themselves are untouched: a playlist is parsed strictly.
    assert "#EXT-X-VERSION:9" in out
    assert "#EXT-X-STREAM-INF:BANDWIDTH=2375980" in out


def test_a_uri_with_no_query_of_its_own_gets_a_question_mark():
    from integrations.video_panel.video_panel_plugin import _rewrite_playlist

    out = _rewrite_playlist("#EXTM3U\nseg0.mp4\n", "tok")
    assert "seg0.mp4?_plugin_token=tok" in out


def test_an_open_instance_leaves_the_playlist_exactly_as_it_came():
    """No password means no token, and a playlist we do not need to touch is
    one we must not touch."""
    from integrations.video_panel.video_panel_plugin import _rewrite_playlist

    playlist = "#EXTM3U\nvideo1_stream.m3u8?session=abc\n"
    assert _rewrite_playlist(playlist, "") == playlist


def test_our_token_is_not_forwarded_to_the_sidecar():
    """It is our credential and MediaMTX has no use for it. Everything else in
    the query is the sidecar's and has to survive."""
    from integrations.video_panel.video_panel_plugin import _split_plugin_token

    query, token = _split_plugin_token("session=abc&_plugin_token=secret&_HLS_msn=7")
    assert token == "secret"
    assert "secret" not in query
    assert "session=abc" in query
    assert "_HLS_msn=7" in query


def test_the_upstream_query_is_carried_through():
    """MediaMTX puts the HLS session in the query, and under lowLatency the
    part requests carry _HLS_msn / _HLS_part -- which IS the low latency."""
    plugin = VideoPanelPlugin()
    plugin._auth_pass = ""
    url = plugin._hls_url("cam1", "video1_stream.m3u8", "session=abc&_HLS_part=3")
    assert url.endswith("/cam1/video1_stream.m3u8?session=abc&_HLS_part=3")
    # And an empty query leaves no dangling separator.
    assert plugin._hls_url("cam1", "index.m3u8", "").endswith("/cam1/index.m3u8")
