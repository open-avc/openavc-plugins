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
    cfg = yaml.safe_load(plugin._render_config())

    assert cfg["api"] is True and cfg["apiAddress"] == "127.0.0.1:9997"
    assert cfg["webrtc"] is True and cfg["webrtcAddress"] == "127.0.0.1:8889"
    assert cfg["webrtcLocalUDPAddress"] == ":8189"
    # RTSP is on, but only as a localhost TCP loopback for the transcode
    # pipeline; TCP-only keeps the UDP RTP/RTCP listeners closed.
    assert cfg["rtsp"] is True
    assert cfg["rtspAddress"] == "127.0.0.1:8556"
    assert cfg["rtspTransports"] == ["tcp"]
    # Other protocols we don't use stay disabled.
    assert cfg["rtmp"] is False
    assert cfg["hls"] is False
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
    assert info["min_openavc_version"] == "0.15.0"
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
    # then follows plugin.video_panel.selection.<channel> at runtime. A free-text
    # type (not select) is required — the channel name is author-defined.
    element = next(
        e for e in VideoPanelPlugin.EXTENSIONS["panel_elements"]
        if e["type"] == "video_stream"
    )
    fields = {f["key"]: f for f in element["config_schema"]}
    assert "stream_id" in fields  # static source still present (the fallback)
    assert fields["channel"]["type"] == "text"


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
    assert r["ok"] is True
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
    assert auth["ok"] is False and "password" in auth["message"].lower()
    down = VideoPanelPlugin._parse_probe("rtsp://cam: Connection refused\n")
    assert down["ok"] is False and "reach" in down["message"].lower()


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
    listing = client.get("/streams").json()
    assert {c["stream_id"] for c in listing} == {"front_door", "front_door_2"}

    # Edit changes the source: delete-then-add against the live sidecar.
    deleted.clear(); added.clear()
    r = client.put("/streams/front_door", json={"name": "Front", "rtsp_url": "rtsp://cam/9"})
    assert r.status_code == 200, r.text
    assert "/v3/config/paths/delete/front_door" in deleted
    assert added[-1] == ("/v3/config/paths/add/front_door", {"source": "rtsp://cam/9", "sourceOnDemand": True})

    # Delete removes it from the list, the sidecar, and persists.
    r = client.delete("/streams/front_door")
    assert r.status_code == 200 and r.json()["ok"] is True
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
