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
import sys
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
def test_camera_source_url_injects_credentials():
    f = VideoPanelPlugin._camera_source_url
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
    # Every protocol we don't use is disabled.
    assert cfg["rtsp"] is False
    assert cfg["rtmp"] is False
    assert cfg["hls"] is False
    assert cfg["srt"] is False

    users = cfg["authInternalUsers"]
    assert users[0]["user"] == "openavc"
    assert users[0]["pass"] == "a1b2c3d4e5f6"
    assert {p["action"] for p in users[0]["permissions"]} == {"publish", "read", "playback"}
    # The API stays reachable from localhost without the stream password.
    assert users[1]["user"] == "any"
    assert users[1]["ips"] == ["127.0.0.1", "::1"]
    assert {p["action"] for p in users[1]["permissions"]} == {"api"}


@pytest.mark.skipif(not _PLUGIN_IMPORTABLE, reason="fastapi/httpx/yaml not available")
def test_plugin_info_manifest_shape():
    info = VideoPanelPlugin.PLUGIN_INFO
    assert info["id"] == "video_panel"
    assert info["category"] == "integration"
    assert info["min_openavc_version"] == "0.13.0"
    assert info["platforms"] == ["win_x64", "linux_x64", "linux_arm64"]
    assert "http_endpoints" in info["capabilities"]

    deps = {d["id"]: d for d in info["native_dependencies"]}
    assert set(deps) == {"mediamtx", "ffmpeg"}
    for dep in deps.values():
        for platform_key in ("win_x64", "linux_x64", "linux_arm64"):
            entry = dep["platforms"][platform_key]
            assert entry["url"].startswith("https://github.com/")
            assert entry["extract"]
