"""
Present and Video Panel each run their own MediaMTX, and both can be installed
on one system. Two sidecars asking for the same port cannot both start: the
second exits at once and its plugin reports that MediaMTX never answered.

These tests read the listeners each plugin's generated MediaMTX config really
opens, plus the ports each declares to the platform, and fail when the two
plugins share one. They also fail when a config switches a MediaMTX server on
without giving it an address, because MediaMTX would then open its own default
port and nothing here could see it.

Run from the openavc-plugins root: pytest tests/test_mediamtx_ports.py -v
"""

import sys
from pathlib import Path

import pytest

_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))

try:
    import yaml

    from integrations.present.present_plugin import PresentPlugin
    from integrations.video_panel.video_panel_plugin import VideoPanelPlugin

    _PLUGINS_IMPORTABLE = True
except Exception:  # fastapi/httpx/yaml not installed in this environment
    _PLUGINS_IMPORTABLE = False

pytestmark = pytest.mark.skipif(
    not _PLUGINS_IMPORTABLE, reason="fastapi/httpx/yaml not available"
)


# Each MediaMTX server switch and the address keys it opens. A switch that is
# on must name every one of its addresses, or MediaMTX picks its default.
_SERVER_ADDRESSES = {
    "api": ["apiAddress"],
    "metrics": ["metricsAddress"],
    "pprof": ["pprofAddress"],
    "playback": ["playbackAddress"],
    "rtsp": ["rtspAddress"],
    "rtmp": ["rtmpAddress"],
    "hls": ["hlsAddress"],
    "webrtc": ["webrtcAddress", "webrtcLocalUDPAddress"],
    "srt": ["srtAddress"],
}

# Address keys whose listener is UDP; every other address is TCP.
_UDP_ADDRESSES = {"webrtcLocalUDPAddress", "srtAddress", "rtpAddress", "rtcpAddress"}


def _rendered(plugin_cls):
    plugin = plugin_cls()
    plugin._auth_pass = "a1b2c3d4e5f6"
    if hasattr(plugin, "_join_address"):  # Present asks its join address
        plugin._join_address.detect_local_ip = lambda: "192.0.2.10"
    else:
        plugin._detect_local_ip = lambda: "192.0.2.10"
    return yaml.safe_load(plugin._render_config())


def _listeners(plugin_cls):
    """Every (protocol, port) the plugin's sidecar opens or the plugin declares."""
    cfg = _rendered(plugin_cls)
    ports = {}
    for key, value in cfg.items():
        if not key.endswith("Address") or not isinstance(value, str) or not value:
            continue
        port = int(value.rsplit(":", 1)[1])
        protocol = "udp" if key in _UDP_ADDRESSES else "tcp"
        ports[(protocol, port)] = key
    for declared in plugin_cls.PLUGIN_INFO.get("network_ports", []):
        ports.setdefault(
            (declared["protocol"], declared["port"]), "PLUGIN_INFO network_ports"
        )
    return ports


@pytest.mark.parametrize("plugin_cls", [PresentPlugin, VideoPanelPlugin] if _PLUGINS_IMPORTABLE else [])
def test_every_server_switched_on_names_its_address(plugin_cls):
    cfg = _rendered(plugin_cls)
    for server, address_keys in _SERVER_ADDRESSES.items():
        if cfg.get(server) is not True:
            continue
        for key in address_keys:
            assert cfg.get(key), (
                f"{plugin_cls.__name__} switches {server} on without {key}: "
                f"MediaMTX would open its default port"
            )
    if cfg.get("rtsp") is True:
        # A UDP transport opens the RTP and RTCP ports; encryption opens RTSPS.
        transports = cfg.get("rtspTransports") or ["udp", "multicast", "tcp"]
        if "udp" in transports:
            assert cfg.get("rtpAddress") and cfg.get("rtcpAddress")
        if cfg.get("rtspEncryption", "no") != "no":
            assert cfg.get("rtspsAddress")


def test_present_and_video_panel_share_no_port():
    present = _listeners(PresentPlugin)
    video_panel = _listeners(VideoPanelPlugin)
    shared = sorted(set(present) & set(video_panel))
    assert not shared, "Present and Video Panel both listen on " + ", ".join(
        f"{protocol.upper()} {port} (Present {present[(protocol, port)]}, "
        f"Video Panel {video_panel[(protocol, port)]})"
        for protocol, port in shared
    )


def test_the_listener_read_finds_each_plugins_ports():
    # Proves the comparison above reads real listeners, not an empty set.
    assert ("tcp", 9998) in _listeners(PresentPlugin)
    assert ("udp", 8190) in _listeners(PresentPlugin)
    assert ("tcp", 9997) in _listeners(VideoPanelPlugin)
    assert ("tcp", 8890) in _listeners(VideoPanelPlugin)
    assert ("udp", 8189) in _listeners(VideoPanelPlugin)
