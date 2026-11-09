"""Video Panel plugin for OpenAVC.

Shows H.264 / H.265 video streams (IP cameras and any other RTSP/RTMP/SRT
source) on the touch panel. The plugin bundles MediaMTX as a sidecar that pulls
each source's feed and republishes it as browser-playable WebRTC (WHEP), or as
HLS for a viewer who arrived over the cloud tunnel. The sidecar binds to
localhost only; the panel reaches it through OpenAVC's own HTTP server, so
stream traffic inherits the platform's authentication.

This module brings the sidecar up and keeps it serving every stream's paths,
answers the plugin page's and the panel's routes, and holds the plugin's
state. What a configured stream record holds, and its login, is
stream_records.py; the sources devices offer for preview, and the stream
picker's rows, are preview_sources.py; the sidecar process itself is
sidecar.py, and ffmpeg's encoder choice and command line are transcode.py.
"""

import asyncio
import inspect
import json
import os
import re
import secrets
import socket
import stat
import sys
import time
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Sibling module. The plugin loader execs this file with the plugin directory on
# sys.path (so the flat import resolves) but not as a package, so a relative
# import would fail there. Test/CI code imports the plugin by its package path
# (integrations.video_panel.video_panel_plugin), where the flat name isn't on
# the path but the relative import is. Try both so the plugin loads either way.
# A lazy import inside a method would fail under the loader (the path entry is
# removed after load), so this stays at module top level.
try:
    from sidecar import NO_WINDOW, SidecarSupervisor
except ImportError:  # pragma: no cover - exercised only via package-path import
    from .sidecar import NO_WINDOW, SidecarSupervisor

try:
    import preview_sources
    import stream_records
    import transcode
except ImportError:  # pragma: no cover - exercised only via package-path import
    from . import preview_sources, stream_records, transcode

# A sentence raised as PluginRefusal from start() is the one the AI assistant
# repeats to the integrator; on an older OpenAVC the card shows it either way.
try:
    from openavc.core.plugin_api import PluginRefusal
except ImportError:  # OpenAVC before 0.38.0
    PluginRefusal = RuntimeError

# MediaMTX listeners. Everything is on localhost except the WebRTC media UDP
# port, which the browser connects to directly for low-latency LAN playback.
# Every port is distinct from the Present plugin's (API 9998, WebRTC
# signalling 8891, WebRTC media UDP 8190, RTSP 8554, SRT 8899) so both plugins
# can run on one system; tests/test_mediamtx_ports.py fails when the two share
# one.
_API_HOST = "127.0.0.1"
_API_PORT = 9997
_WEBRTC_HOST = "127.0.0.1"
_WEBRTC_PORT = 8889
_WEBRTC_UDP_PORT = 8189
# HLS, for viewers who arrived over the cloud tunnel. WebRTC cannot serve them:
# the signalling proxies fine, but the media is UDP straight to the LAN address
# in webrtcAdditionalHosts, which a browser on the far side of the world cannot
# reach -- so until now a remote viewer got a tile that spun forever. HLS is
# plain HTTP, which is what the tunnel already relays. Localhost-only, and on a
# non-standard port for the same reason the RTSP listener is: another MediaMTX
# on the box must not collide with it.
_HLS_HOST = "127.0.0.1"
_HLS_PORT = 8890
# Internal RTSP listener used only for the transcode pipeline: ffmpeg reads the
# raw source path and republishes the H.264 result here, then WHEP serves it.
# Localhost + TCP-only (no UDP RTP/RTCP ports opened), on a non-standard port so
# it can't collide with another MediaMTX on the box (e.g. a dev test source).
_RTSP_HOST = "127.0.0.1"
_RTSP_PORT = 8556
_INTERNAL_RTSP = f"rtsp://{_RTSP_HOST}:{_RTSP_PORT}"
# Publish-back muxer for transcode. RTSP forced to TCP is the canonical pattern;
# "rtmp" is the proven fallback if RTSP-TCP publish ever stalls on a platform.
_TRANSCODE_PUBLISH = "rtsp"
_READY_TIMEOUT = 10.0
_STATUS_POLL_SECONDS = 5.0
# The auto-detected LAN address (advertised to WebRTC clients) is re-resolved
# at most this often.
_IP_CACHE_SECONDS = 60.0

_MEDIAMTX_VERSION = "1.18.2"
_SIDECAR_USER = "openavc"

# ffmpeg-backed operations can hang on an unreachable source; bound them hard.
_PROBE_TIMEOUT = 15.0
_SNAPSHOT_TIMEOUT = 15.0
# Total time start() will spend picking hardware encoders, across every stream
# it registers. The platform stops waiting for a plugin's start() well before
# an unbounded walk of the candidates could finish, and being killed there is
# worse than encoding in software: it leaves the whole plugin down.
_ENCODER_BUDGET = 10.0

# The routes a standalone room panel may reach with its panel-scoped token.
# A panel on a claimed instance is unauthenticated by design, so a media route
# missing from this list 401s on exactly the surface it exists for. Stream CRUD
# (/streams, /streams/probe) is deliberately absent and stays programmer-only.
_PANEL_PATHS = (
    "/whep/*",
    "GET /mjpeg/*",
    "GET /delivery/*",
    "GET /hls/*",
    # The one write a panel may make. It reports why its own tile has no
    # picture, which no other surface can see: the fault lives in a page that
    # gets reloaded, and reloading is the first thing anybody tries.
    "POST /report",
)

# The routes that carry a stream rather than an answer, so the platform puts
# them on its media budget instead of the one sized for a person clicking.
# A low-latency HLS tile re-asks its playlist for every part and fetches
# every part -- about 390 requests a minute, against a standard 60 -- so
# without this the first remote viewer 429s within seconds and gets a
# connection error where the picture should be.
#
# NOT the same list as _PANEL_PATHS even though it looks like it. That one
# says who may reach a route; this says how much of it is normal. /delivery
# is on that list and not this one: it is asked once per playback and is an
# ordinary call.
_MEDIA_PATHS = (
    "/whep/*",
    "GET /mjpeg/*",
    "GET /hls/*",
)

# HLS playlist and segment names MediaMTX generates. Kept to one flat segment
# with no dots-in-a-row so nothing can climb out of the stream's own path.
_HLS_FILE_RE = re.compile(r"[A-Za-z0-9_-]+\.(m3u8|mp4|ts)")

# A URI inside a playlist attribute list, e.g. EXT-X-MEDIA's URI="audio.m3u8".
_HLS_ATTR_URI_RE = re.compile(r'(URI=")([^"]+)(")')


def _split_plugin_token(query):
    """Separate our own auth parameter from everything the sidecar should see.

    The token rides the query because an <img>, a <video src> and hls.js's own
    segment fetches cannot set a header. It is ours, not MediaMTX's, and
    forwarding it upstream would be handing our credential to a subprocess that
    has no use for it.
    """
    from urllib.parse import parse_qsl, urlencode

    token = ""
    keep = []
    for key, value in parse_qsl(query or "", keep_blank_values=True):
        if key == "_plugin_token":
            token = value
        else:
            keep.append((key, value))
    return urlencode(keep), token


def _with_plugin_token(uri, token):
    """Append our token to one URI out of a playlist, preserving its own query.

    A relative URI REPLACES the query of the document it came from, so a token
    put on the playlist request does not reach the media playlists and segments
    the player derives from it. On an instance with a password that is a 401 on
    everything after the first file -- which is every instance in the field.
    """
    if not token or uri.startswith("#"):
        return uri
    sep = "&" if "?" in uri else "?"
    return f"{uri}{sep}_plugin_token={quote(token)}"


def _rewrite_playlist(text, token):
    """Carry the token onto every URI a player will follow out of this playlist.

    Both shapes: a bare line (media playlists, segments) and the URI="..."
    attribute EXT-X-MEDIA uses for the audio rendition. Comment lines that are
    not attribute lists are left exactly as they are -- a playlist is parsed
    strictly and a stray edit breaks the whole stream, not one line of it.
    """
    if not token:
        return text
    out = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            out.append(
                _HLS_ATTR_URI_RE.sub(
                    lambda m: m.group(1) + _with_plugin_token(m.group(2), token) + m.group(3),
                    line,
                )
            )
        else:
            # Keep the line's own terminator: a playlist is CRLF in places
            # and LF in others, and normalising it is not ours to do.
            out.append(_with_plugin_token(stripped, token) + line[len(line.rstrip()):])
    return "".join(out)


# The three answers PluginAPI.viewer_access gives. Compared as strings: this
# plugin imports stdlib and fastapi only, never the platform.
VIEWER_LOCAL = "local"
VIEWER_REMOTE = "remote"
VIEWER_REMOTE_BLOCKED = "remote_blocked"

# The cloud capability that has to be granted before video crosses the tunnel.
# Local viewing is never gated by it, and the code path that serves a local
# viewer never reaches this name.
_TUNNEL_VIDEO_CAPABILITY = "tunnel_video"

# What a tunnelled viewer is told when the space has no remote-video add-on.
# It says what is true and what still works. It deliberately does not explain
# why we sell it that way -- that is our reasoning, not their next step.
_REMOTE_BLOCKED_DETAIL = (
    "Remote video is not included in this plan. This stream still plays on "
    "panels in the space."
)

# WHEP session secrets are MediaMTX-minted UUIDs; constrain to a URL-safe set
# before they're spliced into the proxied sidecar URL.
_SECRET_RE = re.compile(r"[A-Za-z0-9-]+")


def _path_available(item):
    """Whether MediaMTX reports a path as serving. `ready` is deprecated in
    MediaMTX 1.18.x in favour of `available`; prefer the new field, fall back
    for older builds."""
    return bool(item.get("available", item.get("ready", False)))


def _live_by_name(items):
    """path name -> whether it is serving, from a paths/list reply's items."""
    return {item["name"]: _path_available(item) for item in items if item.get("name")}


def _no_such_stream(stream_id):
    """The 404 for a stream id that names nothing the route can serve."""
    return HTTPException(404, f"No stream with id '{stream_id}'.")

# Auto-discovered preview sources (preview_sources.py) are rescanned on a
# trailing debounce: coalesce a burst of preview-key changes (e.g. a controller
# enumerating dozens of encoders at once) into a single rebuild.
_DISCOVER_DEBOUNCE_SECONDS = 0.3

# What MediaMTX names a video track a browser can decode natively. Anything
# else has to go through ffmpeg. MediaMTX reports this for every source type it
# ingests, SRT included -- measured against a live vMix SRT output, which comes
# back as tracks ['MPEG-4 Audio', 'H264'].
_BROWSER_VIDEO_CODECS = ("H264",)
# Video codecs MediaMTX may name. Used to tell "this path carries video we
# cannot draw" from "this path has not reported a video track yet".
_VIDEO_CODECS = ("H264", "H265", "VP8", "VP9", "AV1", "M-JPEG", "MPEG-4 Video")


class StreamIn(BaseModel):
    """Add/edit payload for a video stream. The IDE Video Streams form posts this."""

    name: str
    stream_id: str | None = None
    rtsp_url: str
    username: str = ""
    password: str = ""
    codec_hint: str = "auto"      # auto | h264 | h265
    transcode: str = "auto"       # auto | always | never
    hardware_accel: str = "auto"  # auto | none | qsv | nvenc | vaapi | v4l2m2m


class ReportIn(BaseModel):
    """A tile's account of why it has no picture, posted when playback fails."""

    reason: str
    stream_id: str = ""


class ProbeIn(BaseModel):
    """Probe payload — tests an RTSP URL before it is saved as a stream.

    ``password`` is the saved-login marker to test with a stream's saved
    one: the stream named by ``stream_id``, or, from a Programmer that does
    not send one, the stream with the same address and username
    (``stream_records.password_for_test``)."""

    rtsp_url: str
    username: str = ""
    password: str = ""
    stream_id: str = ""


class VideoPanelPlugin:

    PLUGIN_INFO = {
        "id": "video_panel",
        "name": "Video Panel",
        "version": "0.23.2",
        "author": "OpenAVC",
        "description": "Show H.264 and H.265 video streams (IP cameras and other RTSP sources) on the panel.",
        "category": "integration",
        "license": "MIT",
        "platforms": ["win_x64", "linux_x64", "linux_arm64"],
        # 0.37.0: the MJPEG preview binds to api.local_address (Settings >
        # Network > Reach devices through), which earlier platforms do not
        # have. Covered by the same floor: 0.31.0, where the platform reads
        # `network_ports` (below) and opens the WebRTC media port in the host
        # firewall, without which a panel in the room gets no picture on a
        # firewalled host; and register_router(panel_paths=...), 0.24.0.
        "min_openavc_version": "0.37.0",
        # The sidecar binds this for WebRTC media, and it is the ONLY path a
        # panel in the room has. Neither installer covered it: Windows scopes
        # its firewall rule to openavc-server.exe and this port belongs to
        # mediamtx.exe, and the Linux helper opened TCP only. Declared here so
        # the platform opens it, and closes it again when this plugin is gone.
        "network_ports": [
            {
                "port": 8189,
                "protocol": "udp",
                "reason": "WebRTC video from the sidecar to panels on the network",
            },
        ],
        "capabilities": [
            "state_read",
            "state_write",
            "event_emit",
            "network_listen",
            "http_endpoints",
        ],
        "has_native_dependencies": True,
        "native_dependencies": [
            {
                "id": "mediamtx",
                "name": "MediaMTX",
                "version": "1.18.2",
                "license": "MIT",
                "required": True,
                "platforms": {
                    "win_x64": {
                        "type": "zip",
                        "url": "https://github.com/bluenviron/mediamtx/releases/download/v1.18.2/mediamtx_v1.18.2_windows_amd64.zip",
                        "extract": "mediamtx.exe",
                    },
                    "linux_x64": {
                        "type": "tar.gz",
                        "url": "https://github.com/bluenviron/mediamtx/releases/download/v1.18.2/mediamtx_v1.18.2_linux_amd64.tar.gz",
                        "extract": "mediamtx",
                    },
                    "linux_arm64": {
                        "type": "tar.gz",
                        "url": "https://github.com/bluenviron/mediamtx/releases/download/v1.18.2/mediamtx_v1.18.2_linux_arm64.tar.gz",
                        "extract": "mediamtx",
                    },
                },
            },
            {
                # PIN AN IMMUTABLE, MONTH-END autobuild TAG. Not `latest`.
                #
                # These URLs 404'd for every platform at once (found 2026-08-25)
                # and a fresh install of this plugin could not fetch ffmpeg at
                # all. The old ones pointed at the `latest` RELEASE, whose asset
                # set ROTATES -- so `ffmpeg-n7.1-latest-...` was pinned in name
                # only, and stopped existing the day BtbN retired n7.1 from it.
                #
                # `autobuild-*` tags are immutable, but they are not all kept:
                # BtbN retains roughly the last fortnight of dailies and then
                # only MONTH-END builds, going back years. So pin a month-end
                # one (this is 2026-07-31) or it gets pruned in a fortnight and
                # we are back here.
                #
                # When bumping, re-verify the binary rather than trusting the
                # name -- LGPL (--disable-libx264/libx265) and the encoder set
                # transcode.py's priority lists ask for. Checked on all three
                # for this build: libsrt present everywhere; linux64 and win64
                # carry vaapi/qsv/nvenc/amf; arm64 has v4l2m2m + nvenc and NO
                # vaapi or qsv, which is what platform_priority() already
                # assumes. You cannot execute the other platforms' binaries, so
                # read the embedded configure string and the AVCodec names.
                "id": "ffmpeg",
                "name": "FFmpeg (LGPL build)",
                "version": "8.1",
                "license": "LGPL-2.1",
                "required": True,
                "platforms": {
                    "win_x64": {
                        "type": "zip",
                        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-07-31-14-10/ffmpeg-n8.1.2-34-g9b6c8969e0-win64-lgpl-8.1.zip",
                        "extract": "ffmpeg-n8.1.2-34-g9b6c8969e0-win64-lgpl-8.1/bin/ffmpeg.exe",
                    },
                    "linux_x64": {
                        "type": "tar.xz",
                        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-07-31-14-10/ffmpeg-n8.1.2-34-g9b6c8969e0-linux64-lgpl-8.1.tar.xz",
                        "extract": "ffmpeg-n8.1.2-34-g9b6c8969e0-linux64-lgpl-8.1/bin/ffmpeg",
                    },
                    "linux_arm64": {
                        "type": "tar.xz",
                        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-07-31-14-10/ffmpeg-n8.1.2-34-g9b6c8969e0-linuxarm64-lgpl-8.1.tar.xz",
                        "extract": "ffmpeg-n8.1.2-34-g9b6c8969e0-linuxarm64-lgpl-8.1/bin/ffmpeg",
                    },
                },
            },
        ],
    }

    # Each stream's password is a login: OpenAVC keeps it in the system's
    # login file rather than in the project, and hands it back in
    # `api.config`. An older OpenAVC ignores this and keeps it in the project.
    CONFIG_SECRETS = {
        "streams": {"id": "stream_id", "fields": ["password"]},
    }

    # Panel element: an iframe that plays one stream over WHEP/WebRTC. The
    # panel runtime serves panel/video_stream.html (named for the element
    # type) and injects config + theme + a plugin-scoped ext_token via
    # postMessage; the iframe presents that token to the /ext/whep routes.
    EXTENSIONS = {
        # A page of its own in the IDE. Managing streams used to be a section
        # part-way down the Program page, under Assets and above Backups, which
        # is somewhere nobody looking for video would think to scroll to. Being
        # in the nav is the whole feature: it is where you go when a tile has
        # nothing to show.
        "views": [
            {
                "id": "streams",
                "label": "Video Streams",
                "renderer": "video_streams",
            }
        ],
        "panel_elements": [
            {
                "type": "video_stream",
                "label": "Video Stream",
                "renderer": "iframe",
                "default_size": {"w": 50, "h": 50},
                # allow-same-origin: the iframe is served same-origin and must
                # send a fetch with the platform session so its /ext/* calls
                # authenticate. autoplay: start playback without a user gesture.
                "sandbox_permissions": ["allow-same-origin"],
                "allow_features": ["autoplay"],
                "ext_auth": True,
                "config_schema": [
                    {
                        "key": "stream_id",
                        "label": "Stream",
                        "type": "select",
                        "options_source": "plugin.video_panel.stream_ids",
                    },
                    {
                        # Optional. Leave blank to always show the stream above.
                        # Set a channel name (e.g. "front") and the element
                        # instead follows the state key
                        # plugin.video_panel.selection.<channel> — set that from
                        # a macro, script, or the API to switch the source at
                        # runtime. The stream above becomes the fallback shown
                        # until (and whenever) the selection is empty.
                        "key": "channel",
                        "label": "Source channel (optional)",
                        "type": "string",
                    },
                    {
                        "key": "fit",
                        "label": "Fit",
                        "type": "select",
                        "options": ["contain", "cover"],
                        "default": "contain",
                    },
                    {
                        "key": "show_label",
                        "label": "Show stream name overlay",
                        "type": "boolean",
                        "default": False,
                    },
                    {
                        "key": "reconnect_on_idle",
                        "label": "Auto-reconnect when tab regains focus",
                        "type": "boolean",
                        "default": True,
                    },
                ],
            }
        ]
    }

    def __init__(self):
        self.api = None
        self._supervisor = None
        self._mediamtx_bin = None
        self._ffmpeg_bin = None
        self._auth_pass = ""
        self._config_path = None
        # ONE client for the whole HLS hop, kept for the plugin's lifetime,
        # because MediaMTX's HLS server is stateful in two ways a per-request
        # client cannot satisfy. It answers the first request with a redirect
        # carrying a `cookieCheck` cookie (its proof the caller keeps cookies,
        # so credentials cannot be lifted cross-site), and it then ties a
        # playlist to the segments that follow it with an `hlsSession` cookie.
        # A fresh client per request failed the first hurdle outright -- every
        # playlist came back 302 and the viewer got a 502 -- and would have
        # started a new session on every segment even after clearing it.
        #
        # Those cookies stay on THIS side. MediaMTX marks them Secure and
        # SameSite=None, which a browser reaching a room over plain HTTP would
        # discard, so relaying them to the viewer could never have worked. The
        # cost is that the sidecar counts the plugin as one HLS reader however
        # many people are watching; nothing reads that number.
        self._hls_client = None
        self._streams = []  # configured stream dicts (the source of truth)
        self._encoder_cache = {}  # hardware_accel value -> resolved ffmpeg encoder
        # Auto-discovered preview sources, keyed by derived stream id:
        #   {stream_id: {"label", "url", "format", "device", "group", "status"}}
        # Everything in here is playable. An OFFLINE source stays in it on
        # purpose -- a camera that is switched off is still the camera a page
        # is being built against, and hiding it is how one silently vanishes.
        self._discovered = {}
        # Sources that exist and cannot be played: a driver said `needs_setup`
        # or `unavailable`. Listed in the picker with what is missing, never
        # offered to the panel, and deliberately NOT in _discovered -- that map
        # is what the play routes check, and a source with no URL must not
        # reach them.
        #   {stream_id: {"label", "device", "group", "status", "detail",
        #                "setup_field"}}
        self._unusable = {}
        # Discovered previews we've registered a MediaMTX path for, sid->url.
        self._discovered_sidecar = {}
        # What the sidecar has told us a discovered source actually carries,
        # sid -> "h264" | "other". Empty until a viewer has held a path open
        # long enough for MediaMTX to report its tracks; see _learn_codecs.
        self._learned_codec = {}
        # Streams we've already complained about being unwatchable, so a source
        # that stays down doesn't write the same line every five seconds.
        self._warned_unreachable = set()
        # Formats we've already refused, so an unrenderable preview key does
        # not log on every rebuild.
        self._warned_format = set()
        self._rebuild_task = None  # debounce handle for discovery rebuilds
        # True between the supervisor announcing a crash-restart and the
        # replacement process coming up. What comes back is a NEW MediaMTX with
        # an empty path table, so that next "running" is a re-attach, not a
        # no-op.
        self._sidecar_crashed = False
        self._detected_ip = ("", 0.0)  # (auto-detected LAN address, at monotonic)
        self._rendered_host = None  # LAN address baked into the sidecar config
        # Monotonic instant after which encoder detection stops probing and
        # settles for software, set only while start() is running. None means
        # no hurry: an edit through the API can afford the full walk.
        self._encoder_deadline = None

    # ──── Lifecycle ────

    async def start(self, api):
        self.api = api

        if not self._find_binaries():
            raise await self._refusal_no_mediamtx()

        self._auth_pass = self._load_or_create_auth()
        self._config_path = self.api.data_dir / "mediamtx.yml"
        self._config_path.write_text(self._render_config(), encoding="utf-8")

        await self.api.state_set("running", False)
        await self.api.state_set("error", "")
        await self.api.state_set("sidecar", "starting")

        busy = await self._port_already_in_use()
        if busy:
            raise await self._refusal_port_taken(busy)

        self._supervisor = SidecarSupervisor(
            [str(self._mediamtx_bin), str(self._config_path)],
            name="mediamtx",
            log=self.api.log,
            on_status=self._on_sidecar_status,
            on_circuit_break=self._on_sidecar_circuit_break,
            task_factory=self.api.create_task,
        )

        self._encoder_deadline = time.monotonic() + _ENCODER_BUDGET
        try:
            await self._bring_up()
        except BaseException:
            # Don't leave an orphaned sidecar if start() fails partway --
            # BaseException, not Exception, because the likeliest way out of
            # here is a cancellation: the platform caps start() and everything
            # in the bring-up waits on something external. A CancelledError
            # walked straight past an `except Exception` and left MediaMTX
            # running on all five ports, with nothing supervising it and the
            # next start crash-looping against it.
            await self._stop_sidecar_now()
            raise
        finally:
            self._encoder_deadline = None

    def _find_binaries(self):
        """Find MediaMTX and FFmpeg in plugin_repo/.deps/ and make both
        executable; False, looking no further, when MediaMTX is missing.

        FFmpeg may be missing: it is bundled for HEVC transcoding, the Test
        probe and snapshots, and the passthrough path never invokes it.
        """
        self._mediamtx_bin = self._resolve_dep(self._binary_name("mediamtx"))
        if self._mediamtx_bin is None:
            return False
        self._ffmpeg_bin = self._resolve_dep(self._binary_name("ffmpeg"))
        self._ensure_executable(self._mediamtx_bin)
        if self._ffmpeg_bin is not None:
            self._ensure_executable(self._ffmpeg_bin)
        return True

    async def _refusal_no_mediamtx(self):
        """Mark the plugin not running for a missing MediaMTX; the refusal
        start() raises."""
        msg = (
            "MediaMTX binary not found in plugin_repo/.deps/. Native "
            "dependencies install when the plugin is installed from the "
            "community repository; reinstall the Video Panel plugin."
        )
        await self.api.state_set("running", False)
        await self.api.state_set("error", msg)
        return PluginRefusal(msg)

    async def _refusal_port_taken(self, port):
        """Mark the sidecar failed for a port another process holds; the
        refusal start() raises. (`running` is already false: start() wrote it
        before it looked.)"""
        msg = (
            f"Port {port} on this machine is already in use, so the video "
            "server cannot start. Another copy of it is most likely still "
            "running. Stop it, then start the Video Panel plugin again."
        )
        await self.api.state_set("error", msg)
        await self.api.state_set("sidecar", "failed")
        return PluginRefusal(msg)

    async def _bring_up(self):
        """Everything after the sidecar is created, in order. start() stops
        the sidecar if any of it fails or is cancelled."""
        await self._supervisor.start()
        if not await self._wait_until_ready():
            raise RuntimeError(
                f"MediaMTX did not respond on {_API_HOST}:{_API_PORT} "
                f"within {int(_READY_TIMEOUT)}s"
            )
        await self.api.state_set("running", True)
        self.api.register_router(self._build_router(), **self._router_options())
        await self._load_streams()
        await self._publish_streams()
        await self._setup_discovery()
        self.api.create_periodic_task(
            self._poll_statuses, interval_seconds=_STATUS_POLL_SECONDS, name="status_poll"
        )
        self.api.log(
            f"Video Panel started: MediaMTX {_MEDIAMTX_VERSION} on "
            f"{_API_HOST}:{_API_PORT}, {len(self._streams)} stream(s)"
        )

    def _router_options(self):
        """The routes a room panel may reach, and, where the platform takes
        it, the routes that carry a stream.

        media_paths arrived in platform 0.31.0. Asked for by signature
        rather than by version, and rather than by catching the TypeError
        an older platform would raise -- the router is built before this
        and a TypeError out of THAT would look identical.

        Feature-detected instead of gated because the plugin has to stay
        installable: an older platform simply rate-limits remote video,
        which is the state it was already in, and a panel in the room
        never touches these routes at all.
        """
        options = {"panel_paths": list(_PANEL_PATHS)}
        if "media_paths" in inspect.signature(self.api.register_router).parameters:
            options["media_paths"] = list(_MEDIA_PATHS)
        return options

    async def _stop_sidecar_now(self):
        """Stop the sidecar even while this task is being cancelled.

        A plain ``await stop()`` is not enough on the cancellation path: a
        second cancellation would land inside it and abandon a half-terminated
        child. The shield lets the stop finish on its own task, and a kill is
        the last resort if even that is cut short.
        """
        sup, self._supervisor = self._supervisor, None
        if sup is None:
            return
        try:
            await asyncio.shield(sup.stop())
        except asyncio.CancelledError:
            # Cancelled again mid-teardown. The shielded stop carries on by
            # itself; the kill is what guarantees the child is gone even if
            # the loop stops caring about that task.
            sup.kill_now()
            raise
        except Exception as e:
            sup.kill_now()
            self.api.log(f"MediaMTX did not stop cleanly ({e}); killed it", "warning")

    async def _port_already_in_use(self):
        """The first sidecar port something else is already listening on, if any.

        MediaMTX's API, WebRTC and HLS ports are the ones a second copy would
        answer on, and an answer there is indistinguishable from our own child
        once it arrives: the ready check would adopt a stranger, register paths
        into it, and report a healthy plugin while the process we actually
        supervise crash-looped against the ports it could not bind.
        """
        listeners = (
            (_API_HOST, _API_PORT),
            (_WEBRTC_HOST, _WEBRTC_PORT),
            (_HLS_HOST, _HLS_PORT),
            (_RTSP_HOST, _RTSP_PORT),
        )
        for host, port in listeners:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=1.0
                )
            except (OSError, asyncio.TimeoutError):
                continue
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:  # pragma: no cover - platform-specific reset
                pass
            return port
        return None

    async def stop(self):
        # State keys, subscriptions, and managed tasks are cleaned up by the
        # platform; we only need to stop the external process. The platform
        # bounds this hook too, so it goes out the same cancellation-proof way
        # start()'s failure path does -- a child that ignores its terminate is
        # exactly how a stop runs out of time.
        await self._stop_sidecar_now()
        if self._hls_client is not None:
            client, self._hls_client = self._hls_client, None
            await client.aclose()

    async def health_check(self):
        if self._supervisor is not None and self._supervisor.running:
            return {"status": "ok", "message": f"MediaMTX running (pid {self._supervisor.pid})"}
        return {"status": "error", "message": "MediaMTX sidecar is not running"}

    # ──── Sidecar callbacks ────

    async def _on_sidecar_status(self, status):
        await self.api.state_set("sidecar", status)
        if status in ("restarting", "failed"):
            await self.api.state_set("running", False)
            # "restarting" means the supervisor is about to spawn a replacement
            # process. Path registrations live in the MediaMTX process, not in
            # its config file, so that replacement comes up serving nothing --
            # and nothing else would ever notice: the process is alive, so
            # health_check reports ok, the status poll gets a 200 and puts
            # `running` back to true, and every stream reads "idle" exactly as
            # it does when nobody is watching. Every tile is dead and the whole
            # plugin looks well. Remember the crash here; re-attach below.
            self._sidecar_crashed = status == "restarting"
        elif status == "running" and self._sidecar_crashed:
            self._sidecar_crashed = False
            await self._reattach_sidecar("an unexpected exit")

    async def _on_sidecar_circuit_break(self, reason):
        await self.api.state_set("running", False)
        await self.api.state_set("error", reason)
        await self.api.event_emit("error", {"reason": reason})

    async def _maybe_apply_address_change(self):
        """Rebuild the sidecar when the host's LAN address has moved (True if
        it did). Every WHEP session live at that moment already carries the
        old address in its ICE candidates, so the brief outage from the
        restart is strictly better than advertising a dead address until the
        next plugin restart."""
        if self._supervisor is None or not self._supervisor.running:
            return False
        host = self._detect_local_ip()
        if host == self._rendered_host:
            return False
        self.api.log(
            f"LAN address changed ({self._rendered_host} -> {host}); "
            "restarting MediaMTX so WebRTC advertises the new address",
            "warning",
        )
        await self._restart_sidecar()
        return True

    async def _restart_sidecar(self):
        """Bounce MediaMTX on a freshly rendered config, then re-attach to it."""
        await self._supervisor.stop()
        self._config_path.write_text(self._render_config(), encoding="utf-8")
        await self._supervisor.start()
        await self._reattach_sidecar("an address-change restart")

    async def _reattach_sidecar(self, why):
        """Wait for a just-started MediaMTX and register every path again.

        Path registrations live in the sidecar process, not its config file, so
        a replacement process starts with an empty path table and serves 404 to
        every viewer. Two things bring one up -- our own deliberate bounce and
        the supervisor's auto-restart after a crash -- and both land here, so
        there is one re-attach rather than one per caller.
        """
        if not await self._wait_until_ready():
            self.api.log(
                f"MediaMTX did not respond on {_API_HOST}:{_API_PORT} within "
                f"{int(_READY_TIMEOUT)}s of {why}",
                "error",
            )
            await self.api.state_set("running", False)
            return
        for stream in self._streams:
            await self._sync_path_add(stream)
        self._discovered_sidecar = {}
        self._learned_codec = {}
        await self._sync_discovered_sidecar()
        await self.api.state_set("running", True)

    # ──── Streams ────

    async def _load_streams(self):
        # self._streams is the authored/configured list (the source of truth the
        # CRUD endpoints edit and persist). MediaMTX registration is attempted for
        # each but a sidecar rejection doesn't drop the stream from the list —
        # it's still configured, just not currently playable, which the status
        # poller reflects.
        configured = self.api.config.get("streams", [])
        self._streams = []
        if not isinstance(configured, list):
            return
        changed = False
        for stream in configured:
            if not isinstance(stream, dict):
                continue
            stream_id = (stream.get("stream_id") or "").strip()
            if not stream_id:
                continue
            # A copy: api.config is shallow, and the entry may be rewritten.
            stream = dict(stream)
            # A login written into the address, saved by an earlier version,
            # moves into Username and Password, so the password is kept with
            # the logins rather than in the address.
            if stream_records.move_url_login(stream, self.api.log):
                changed = True
            self._streams.append(stream)
            await self._sync_path_add(stream)
        if changed:
            await self._persist_streams()

    async def _sync_path_add(self, stream):
        """Register (or re-register) a stream's path(s) with the running sidecar.

        Passthrough streams get one path that pulls the source directly.
        Transcode streams get two: ``<id>__src`` pulls the raw source (the
        camera credentials live here, handled natively by MediaMTX), and
        ``<id>`` runs ffmpeg on demand to re-encode to H.264 and republish.
        """
        stream_id = stream["stream_id"]
        source = stream_records.stream_source_url(stream)
        if not source:
            self.api.log(
                f"stream '{stream_id}' has no usable rtsp_url; not registered with sidecar",
                "warning",
            )
            return False

        want_transcode = self._should_transcode(stream)
        if want_transcode and not self._ffmpeg_bin:
            self.api.log(
                f"stream '{stream_id}' needs transcoding but ffmpeg is unavailable; "
                "serving the source as-is (it may not play in browsers). Reinstall "
                "the Video Panel plugin to fetch ffmpeg.",
                "warning",
            )
            want_transcode = False

        if want_transcode:
            return await self._sync_transcode_paths(stream, source)

        ok = await self._api_post(
            f"/v3/config/paths/add/{stream_id}",
            {"source": source, "sourceOnDemand": True},
        )
        if not ok:
            self.api.log(
                f"sidecar rejected stream '{stream_id}'; check the source URL",
                "warning",
            )
        return ok

    async def _sync_transcode_paths(self, stream, source):
        """Add the raw-source path + the on-demand ffmpeg transcode path."""
        stream_id = stream["stream_id"]
        src_name = self._src_path(stream_id)
        ok_src = await self._api_post(
            f"/v3/config/paths/add/{src_name}",
            {"source": source, "sourceOnDemand": True},
        )
        encoder = await self._resolve_encoder(stream.get("hardware_accel") or "auto")
        command = transcode.build_transcode_command(
            str(self._ffmpeg_bin),
            encoder,
            f"{_INTERNAL_RTSP}/{src_name}",
            f"{_INTERNAL_RTSP}/{stream_id}",
            publish=_TRANSCODE_PUBLISH,
        )
        ok_main = await self._api_post(
            f"/v3/config/paths/add/{stream_id}",
            {
                "runOnDemand": command,
                "runOnDemandRestart": True,
                # Generous start window: ffmpeg must connect to the raw path,
                # which in turn pulls the (possibly slow) camera on demand.
                "runOnDemandStartTimeout": "15s",
                "runOnDemandCloseAfter": "10s",
            },
        )
        if not (ok_src and ok_main):
            self.api.log(
                f"sidecar rejected transcode setup for '{stream_id}'; check the source URL",
                "warning",
            )
        return ok_src and ok_main

    async def _sync_path_delete(self, stream_id):
        """Remove a stream's path(s) from the running sidecar (idempotent).

        Deletes both the main path and any transcode raw-source sibling, so a
        passthrough<->transcode switch on edit can't leave a stale path behind.
        """
        ok = await self._api_delete(f"/v3/config/paths/delete/{stream_id}")
        await self._api_delete(f"/v3/config/paths/delete/{self._src_path(stream_id)}")
        return ok

    @staticmethod
    def _src_path(stream_id):
        """The raw-source path name for a transcode stream."""
        return f"{stream_id}__src"

    @staticmethod
    def _should_transcode(stream):
        """Whether a stream's settings call for transcoding to H.264.

        ``auto`` passes through only when the source is *confirmed* browser-
        playable H.264; HEVC -- or a codec the probe couldn't determine --
        transcodes. Defaulting the unknown case to transcode is the safe choice:
        guessing passthrough for an undecodable source (e.g. an HEVC camera the
        save-time probe couldn't reach in time) sends video the browser can't
        play, which shows as a silent black tile rather than an error.
        """
        mode = (stream.get("transcode") or "auto").strip().lower()
        if mode == "never":
            return False
        if mode == "always":
            return True
        return (stream.get("codec_hint") or "auto").strip().lower() != "h264"

    async def _resolve_encoder(self, hardware_accel):
        """Pick (and cache) the ffmpeg H.264 encoder for a hardware_accel value.

        Detection test-encodes each candidate, and a wedged GPU driver makes
        that take the full timeout -- long enough, across several candidates
        and several streams, to outlast the budget the platform allows start().
        So during start the whole of it runs against one deadline; past it,
        every remaining stream takes software, which plays everywhere and is
        where an undetectable encoder would have landed anyway.
        """
        ha = (hardware_accel or "auto").strip().lower()
        if ha in self._encoder_cache:
            return self._encoder_cache[ha]
        left = None
        if self._encoder_deadline is not None:
            left = self._encoder_deadline - time.monotonic()
            if left <= 0:
                self._encoder_cache[ha] = transcode.SOFTWARE_ENCODER
                return transcode.SOFTWARE_ENCODER
        try:
            picked = await self._detect_encoder(ha, left)
        except asyncio.TimeoutError:
            picked = transcode.SOFTWARE_ENCODER
            self.api.log(
                "hardware encoder detection is taking too long on this machine; "
                f"using software ({transcode.SOFTWARE_ENCODER})",
                "warning",
            )
        self._encoder_cache[ha] = picked
        return picked

    async def _detect_encoder(self, hardware_accel, budget):
        selecting = transcode.select_encoder(
            str(self._ffmpeg_bin), hardware_accel, self.api.log
        )
        if budget is None:
            return await selecting
        return await asyncio.wait_for(selecting, timeout=budget)

    async def _persist_streams(self):
        """Write the current stream list back to the project file via the platform."""
        cfg = self.api.config  # a copy; safe to mutate
        cfg["streams"] = self._streams
        await self.api.save_config(cfg)

    async def _live_status_map(self):
        data = await self._api_get("/v3/paths/list")
        return _live_by_name(data.get("items", [])) if data else {}

    async def _publish_streams(self):
        await self._publish_stream_list()
        # Seed per-configured-stream status; the poller refreshes it from the
        # sidecar. Only the configured streams have a managed status key.
        for s in self._streams:
            await self.api.state_set(f"streams.{s['stream_id']}", "idle")

    async def _publish_stream_list(self):
        # Kept separate from the status seeding above so a discovery rebuild
        # doesn't flicker configured-stream status badges.
        rows = preview_sources.picker_rows(self._streams, self._discovered, self._unusable)
        await self.api.state_set("stream_ids", json.dumps(rows))

    async def _poll_statuses(self):
        if await self._maybe_apply_address_change():
            return  # sidecar just bounced; statuses refresh on the next tick
        data = await self._api_get("/v3/paths/list")
        # Same rule as the ready check: an answer from a stranger on the API
        # port must not report this plugin as running, or a circuit-broken
        # sidecar reads as healthy for as long as the stranger is up.
        if data is None or not self._sidecar_is_ours():
            await self.api.state_set("running", False)
            return
        await self.api.state_set("running", True)
        items = data.get("items", []) or []
        live = _live_by_name(items)
        for s in self._streams:
            stream_id = s["stream_id"]
            await self.api.state_set(
                f"streams.{stream_id}", "streaming" if live.get(stream_id) else "idle"
            )
        await self._learn_codecs(items)
        self._warn_unwatchable(items)

    async def _learn_codecs(self, items):
        """Correct a discovered source's codec guess from what the sidecar sees.

        MediaMTX names the tracks of every path it has ingested, whatever the
        protocol -- an SRT source comes back as ['MPEG-4 Audio', 'H264'], same
        shape as an RTSP one. That is strictly better evidence than probing the
        source ourselves: it needs no second protocol implementation, it costs
        nothing (the poll already runs), and it works for anything MediaMTX can
        ingest rather than only what the bundled ffmpeg was compiled for.

        A path only reports tracks while somebody is watching it, because these
        are on-demand sources. So the first viewer of a stream we guessed wrong
        about sees a few seconds of nothing, the guess is replaced, the path is
        re-registered, and the iframe's own reconnect brings it up correct. It
        does not repeat: the answer is kept for as long as the source stands.

        **This may only ever TIGHTEN -- turn transcoding on, never off.** The
        asymmetry is not caution, it is what the evidence actually supports:
        MediaMTX names the codec FAMILY, not the profile. "H265" is proof a
        browser cannot play it. "H264" is NOT proof a browser can -- High 4:2:2
        and the more exotic levels are H264 too, and transcoding is what has
        been quietly making those work. So a source already being transcoded
        stays that way, and the only thing learned here is that something needs
        transcoding after all.
        """
        for item in items:
            sid = item.get("name")
            if not sid or sid not in self._discovered_sidecar:
                continue
            codec = self._video_codec(item.get("tracks"))
            if codec is None or codec in _BROWSER_VIDEO_CODECS:
                continue
            if self._learned_codec.get(sid) == "other":
                continue  # already known and already transcoding
            self._learned_codec[sid] = "other"
            self.api.log(
                f"'{sid}' carries {codec}, which browsers cannot play; "
                f"transcoding it to H.264",
                "info",
            )
            url = self._discovered_sidecar.get(sid)
            if url:
                await self._sync_path_delete(sid)
                await self._sync_path_add(self._discovered_entry(sid, url))

    @staticmethod
    def _video_codec(tracks):
        """The video codec among a path's tracks, or None if it has none yet."""
        if not isinstance(tracks, list):
            return None
        for track in tracks:
            if isinstance(track, str) and track in _VIDEO_CODECS:
                return track
        return None

    def _warn_unwatchable(self, items):
        """Say when somebody is watching a source that will not connect.

        Readers attached and the path never becoming available is exactly the
        "spinning with no explanation" case: MediaMTX says why on its stderr,
        but that is drained at debug level, so nothing reaches a log anybody
        reads. Named host and port, once per outage rather than every poll.
        """
        for item in items:
            sid = item.get("name")
            if not sid or sid not in self._discovered_sidecar:
                continue
            available = _path_available(item)
            readers = len(item.get("readers") or [])
            if available or not readers:
                self._warned_unreachable.discard(sid)
                continue
            if sid in self._warned_unreachable:
                continue
            self._warned_unreachable.add(sid)
            url = self._discovered_sidecar.get(sid, "")
            self.api.log(
                f"'{sid}' has a viewer but its source is not answering: "
                f"{url}. The OpenAVC server has to be able to reach that "
                f"address itself -- the panel never connects to it directly. "
                f"Check the device is streaming and that nothing between the "
                f"two is blocking it.",
                "warning",
            )

    # ──── Auto-discovered preview sources ────

    async def _setup_discovery(self):
        """Watch every key of the preview-source convention, then seed the list."""
        for pattern in preview_sources.WATCHED:
            await self.api.state_subscribe(pattern, self._on_discovery_change)
        await self._rebuild_discovered()

    def _on_discovery_change(self, key, value, old_value):
        """State callback: (re)schedule a debounced rebuild (trailing edge)."""
        if self._rebuild_task and not self._rebuild_task.done():
            self._rebuild_task.cancel()
        self._rebuild_task = self.api.create_task(
            self._debounced_rebuild(), name="discovery_rebuild"
        )

    async def _debounced_rebuild(self):
        try:
            await asyncio.sleep(_DISCOVER_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return  # superseded by a newer change; the later task rebuilds
        await self._rebuild_discovered()

    async def _rebuild_discovered(self):
        """Rescan preview-source keys and republish the merged stream list."""
        self._discovered, self._unusable = await preview_sources.scan(
            self.api, self._warned_format
        )
        await self._sync_discovered_sidecar()
        await self._publish_stream_list()

    async def _sync_discovered_sidecar(self):
        """Register/unregister MediaMTX paths for discovered RTSP + SRT previews.

        MJPEG previews are served by the plugin's own streaming proxy and need
        no sidecar path. Everything else rides the existing MediaMTX->WHEP
        pipeline, so each gets an on-demand path.

        **MediaMTX's own SRT listener stays off.** We are the caller, dialling
        out to the device, so SRT ingest opens no inbound port and needs no
        firewall rule -- unlike the WebRTC UDP port this plugin already has to
        explain. Only letting devices PUSH to us would need a listener, and
        that is a separate decision with a separate cost.
        """
        want = {
            sid: d["url"]
            for sid, d in self._discovered.items()
            if d["format"] in preview_sources.SIDECAR_FORMATS
        }
        for sid in set(self._discovered_sidecar) - set(want):
            await self._sync_path_delete(sid)
            self._learned_codec.pop(sid, None)
            self._warned_unreachable.discard(sid)
        for sid, url in want.items():
            if self._discovered_sidecar.get(sid) == url:
                continue  # already registered with this URL
            self._learned_codec.pop(sid, None)  # a new URL is a new source
            await self._sync_path_add(self._discovered_entry(sid, url))
        self._discovered_sidecar = want

    def _discovered_entry(self, sid, url):
        """A discovered source in the shape _sync_path_add reads.

        The `rtsp_url` key is historical -- it is just "the source URL", and
        MediaMTX takes an srt:// one in the same field.

        `codec_hint` is where the two formats deliberately differ, and the
        reason is worth stating because the asymmetry looks like an oversight:

        - **RTSP keeps `auto`**, which transcodes until proven otherwise. That
          is the shipped behaviour for every RTSP source already in the field
          -- AV-over-IP encoders, IP cameras, whatever a driver has pointed us
          at -- and relaxing it on ones this session cannot test is the
          kind of change that breaks somebody's working preview for a CPU win.
        - **SRT starts at `h264`**, i.e. passthrough. SRT is the broadcast
          world's contribution transport and the switchers and hardware
          encoders that speak it send H.264 far more often than not; vMix
          measurably does. Re-encoding a stream that is already H.264 spends
          real CPU on every panel that shows it, for nothing.

        The SRT guess is not left to stand: _learn_codecs replaces it as soon as
        anybody actually watches, so a source that turns out to be H.265
        corrects itself within one poll. That correction only ever runs one way
        -- see _learn_codecs -- so this cannot become a back door that relaxes
        an RTSP source out of the transcoding it ships with.
        """
        hint = self._learned_codec.get(sid)
        if hint is None:
            hint = "h264" if self._discovered.get(sid, {}).get("format") == "srt" else "auto"
        return {
            "stream_id": sid, "rtsp_url": url, "username": "", "password": "",
            "codec_hint": hint, "transcode": "auto", "hardware_accel": "auto",
        }

    def _is_known_stream(self, stream_id):
        """A stream the panel may play: configured or auto-discovered."""
        return (
            stream_records.find(self._streams, stream_id) is not None
            or stream_id in self._discovered
        )

    def _resolve_mjpeg_url(self, stream_id):
        """Upstream MJPEG URL for a discovered source, or None if not MJPEG."""
        d = self._discovered.get(stream_id)
        return d["url"] if d and d["format"] == "mjpeg" else None

    # ──── HTTP router (mounted at /api/plugins/video_panel/ext/) ────

    def _build_router(self):
        router = APIRouter()

        @router.get("/status")
        async def status():
            return self._status()

        @router.get("/streams")
        async def list_streams():
            return await self._list_streams()

        @router.post("/streams")
        async def add_stream(data: StreamIn):
            return await self._add_stream(data)

        @router.put("/streams/{stream_id}")
        async def edit_stream(stream_id: str, data: StreamIn):
            return await self._edit_stream(stream_id, data)

        @router.delete("/streams/{stream_id}")
        async def delete_stream(stream_id: str):
            return await self._delete_stream(stream_id)

        @router.post("/streams/probe")
        async def probe_stream(body: ProbeIn):
            return await self._probe_stream(body)

        @router.get("/streams/{stream_id}/snapshot.jpg")
        async def snapshot(stream_id: str):
            return await self._snapshot(stream_id)

        # ── How should THIS viewer play THIS stream? ──
        # The element used to answer that itself, off the shared stream list.
        # It cannot any more: the answer now depends on where the viewer is,
        # and a state key is one value for everybody. So the element asks, per
        # playback, and dispatches on what comes back.
        @router.get("/delivery/{stream_id}")
        async def delivery(stream_id: str, request: Request):
            self._check_playable(stream_id)
            return self._delivery_for(stream_id, request)

        # Why a tile has no picture, kept where it outlives the tile. Every
        # failure path in the element ends at the same spinner, so without this
        # the one fact that identifies the fault -- a 404 from delivery, a
        # refused WHEP offer, ICE never completing -- exists only in a console
        # nobody had open, and is gone the moment the panel reloads.
        @router.post("/report")
        async def report_failure(body: ReportIn):
            return await self._report_failure(body)

        # ── HLS reverse proxy (tunnelled viewers only) ──
        # Playlists and segments are ordinary small GETs, which is the whole
        # reason this exists: the tunnel relays them and cannot relay WebRTC's
        # UDP media. MediaMTX writes playlist entries as plain relative names,
        # so the browser resolves them against this same mount and every
        # follow-up comes back through here authenticated.
        @router.get("/hls/{stream_id}/{filename}")
        async def hls(stream_id: str, filename: str, request: Request):
            return await self._hls(stream_id, filename, request)

        # ── MJPEG (multipart-over-HTTP) reverse proxy ──
        # Auto-discovered MJPEG previews (e.g. a Chazy encoder's ?action=stream
        # secondary stream) are rendered by an <img> in the panel element. An
        # <img> can't send the plugin-token header, so the panel presents it on
        # the query string (?_plugin_token=...), which the platform's ext-auth
        # already accepts. We stream the upstream multipart body straight back so
        # only the OpenAVC host needs a route to the AV/video LAN.
        @router.get("/mjpeg/{stream_id}")
        async def mjpeg(stream_id: str, request: Request):
            return await self._mjpeg(stream_id, request)

        # ── WHEP (WebRTC playback) reverse proxy ──
        # The panel iframe's WHEP client POSTs an SDP offer here; we forward it
        # to the sidecar's WebRTC server (localhost, Basic-authed via the URL
        # userinfo httpx applies) and return the SDP answer. The trickle and
        # teardown come back through here too (see _whep_offer).
        @router.post("/whep/{stream_id}")
        async def whep_offer(stream_id: str, request: Request):
            return await self._whep_offer(stream_id, request)

        @router.patch("/whep/{stream_id}/{secret}")
        async def whep_trickle(stream_id: str, secret: str, request: Request):
            return await self._whep_session(stream_id, secret, request)

        @router.delete("/whep/{stream_id}/{secret}")
        async def whep_teardown(stream_id: str, secret: str, request: Request):
            return await self._whep_session(stream_id, secret, request)

        return router

    # The plugin page's operations. Each returns what its route answers, or
    # raises the HTTPException the page shows; the record rules are
    # stream_records', what a change does to the sidecar and the state keys is
    # here.

    def _status(self):
        return {
            "running": bool(self._supervisor and self._supervisor.running),
            "mediamtx_version": _MEDIAMTX_VERSION,
            "stream_ids": [s["stream_id"] for s in self._streams],
        }

    async def _list_streams(self):
        live = await self._live_status_map()
        return {"streams": [stream_records.programmer_view(s, live) for s in self._streams]}

    def _stream_or_404(self, stream_id):
        stream = stream_records.find(self._streams, stream_id)
        if not stream:
            raise _no_such_stream(stream_id)
        return stream

    async def _add_stream(self, data):
        entry = stream_records.new_record(self._streams, data, self.api.log)
        self._streams.append(entry)
        await self._persist_streams()
        await self._sync_path_add(entry)
        await self._publish_streams()
        live = await self._live_status_map()
        return stream_records.programmer_view(entry, live)

    async def _edit_stream(self, stream_id, data):
        existing = self._stream_or_404(stream_id)
        entry = stream_records.edited_record(self._streams, existing, data, self.api.log)
        self._streams[self._streams.index(existing)] = entry
        await self._persist_streams()
        # save_config does not restart the plugin, so the MediaMTX path must be
        # re-synced live. Delete-then-add covers both source edits and renames.
        await self._sync_path_delete(stream_id)
        await self._sync_path_add(entry)
        await self._publish_streams()
        live = await self._live_status_map()
        return stream_records.programmer_view(entry, live)

    async def _delete_stream(self, stream_id):
        existing = self._stream_or_404(stream_id)
        self._streams.remove(existing)
        await self._persist_streams()
        await self._sync_path_delete(stream_id)
        await self.api.state_set(f"streams.{stream_id}", None)
        await self._publish_streams()
        return {"status": "deleted", "stream_id": stream_id}

    async def _probe_stream(self, body):
        stream_records.validate_url(body.rtsp_url)
        if not self._ffmpeg_bin:
            raise HTTPException(
                503,
                "ffmpeg is not available. Reinstall the Video Panel plugin to "
                "fetch its native dependencies.",
            )
        password = stream_records.password_for_test(self._streams, body)
        url = stream_records.source_url(body.rtsp_url, body.username, password)
        return await self._probe_rtsp(url)

    async def _snapshot(self, stream_id):
        stream = self._stream_or_404(stream_id)
        if not self._ffmpeg_bin:
            raise HTTPException(503, "ffmpeg is not available.")
        data = await self._snapshot_rtsp(stream_records.stream_source_url(stream))
        if not data:
            raise HTTPException(502, "Could not capture a frame from the stream.")
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    async def _report_failure(self, body):
        reason = " ".join(body.reason.split())[:200]
        stream_id = body.stream_id or ""
        if stream_id:
            stream_records.validate_stream_id(stream_id)
        await self.api.state_set("playback_error", reason)
        await self.api.state_set("playback_error_stream", stream_id)
        await self.api.state_set(
            "playback_error_at", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        )
        return {"ok": True}

    # The play routes. Each checks the stream id's form first; the delivery,
    # HLS and WHEP offer routes then that the panel may play it at all.

    def _check_playable(self, stream_id):
        """A well-formed id of a stream the panel may play (configured or
        discovered), or the refusal: 422, then 404."""
        stream_records.validate_stream_id(stream_id)
        if not self._is_known_stream(stream_id):
            raise _no_such_stream(stream_id)

    def _check_viewer_plan(self, request):
        """Refuse (402) a tunnelled viewer whose plan has no remote video."""
        if self._viewer_access(request) == VIEWER_REMOTE_BLOCKED:
            raise HTTPException(402, _REMOTE_BLOCKED_DETAIL)

    async def _hls(self, stream_id, filename, request):
        self._check_playable(stream_id)
        if not _HLS_FILE_RE.fullmatch(filename or ""):
            raise HTTPException(422, "Invalid HLS file name.")
        # Re-checked on every fetch, not just at the delivery call: a plan
        # can be revoked mid-session, and the segment fetches are the part
        # that actually spends the bandwidth.
        self._check_viewer_plan(request)
        # The query is not decoration. MediaMTX puts the HLS session in it,
        # and under lowLatency the part requests carry _HLS_msn/_HLS_part --
        # which IS the low latency. Dropping it left every derived request
        # asking for something else than the player meant.
        upstream_query, token = _split_plugin_token(request.url.query)
        return await self._hls_stream_response(
            self._hls_url(stream_id, filename, upstream_query),
            token=token,
        )

    async def _mjpeg(self, stream_id, request):
        stream_records.validate_stream_id(stream_id)
        url = self._resolve_mjpeg_url(stream_id)
        if not url:
            raise HTTPException(404, f"No MJPEG preview for '{stream_id}'.")
        # MJPEG crosses the tunnel perfectly well on its own -- which is
        # exactly why it is gated too. It carries no interframe compression,
        # so it is the most expensive of the three paths per minute, and a
        # switch that let the dearest one through would not be a switch.
        self._check_viewer_plan(request)
        return await self._mjpeg_stream_response(url)

    async def _whep_offer(self, stream_id, request):
        """Forward an SDP offer and rewrite the answer's Location.

        MediaMTX answers with a path-absolute Location
        (/{stream_id}/whep/{session}); left as-is the browser would resolve
        the follow-up PATCH/DELETE against the origin root, bypassing this
        authenticated mount. So Location is rewritten to sit under the
        incoming request path, and the trickle and teardown come back here.
        """
        self._check_playable(stream_id)
        # allow_internal: the sidecar is on localhost; proxy_to refuses
        # internal hosts by default (SSRF guard).
        resp = await self.api.proxy_to(
            self._whep_url(stream_id), request, allow_internal=True
        )
        location = resp.headers.get("location")
        if location:
            secret = location.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            resp.headers["location"] = f"{request.url.path.rstrip('/')}/{secret}"
        return resp

    async def _whep_session(self, stream_id, secret, request):
        """A WHEP session's trickle (PATCH) or teardown (DELETE), passed to the
        sidecar session it names. The id and the session are checked for form
        only; the stream is not looked up."""
        stream_records.validate_stream_id(stream_id)
        self._validate_secret(secret)
        return await self.api.proxy_to(
            self._whep_url(stream_id, secret), request, allow_internal=True
        )

    def _viewer_access(self, request):
        """Where this viewer is, as far as tunnel video is concerned.

        Feature-detected: an instance older than 0.31.0 cannot tell us where a
        caller came from, so everybody is local, nothing is gated, and the
        plugin behaves exactly as it shipped. Degrading the other way -- gating
        when we cannot tell -- would take out panels on the LAN, which is the
        one thing this must never do.
        """
        ask = getattr(self.api, "viewer_access", None)
        if ask is None:
            return VIEWER_LOCAL
        return ask(request, _TUNNEL_VIDEO_CAPABILITY)

    def _delivery_for(self, stream_id, request):
        """How this viewer should play this stream.

        The local answer is decided first and returns before the entitlement is
        anywhere in the picture -- deliberately, and matching the shape the
        platform call already has. A panel in the room does not care what the
        account is paying for.
        """
        where = self._viewer_access(request)
        is_mjpeg = self._resolve_mjpeg_url(stream_id) is not None
        if where == VIEWER_LOCAL:
            return {"stream_id": stream_id, "mode": "mjpeg" if is_mjpeg else "webrtc"}
        if where == VIEWER_REMOTE_BLOCKED:
            return {
                "stream_id": stream_id,
                "mode": "blocked",
                "detail": _REMOTE_BLOCKED_DETAIL,
            }
        # Remote and allowed. MJPEG already crosses the tunnel as-is (it is
        # ordinary multipart HTTP); everything else goes as HLS, because WebRTC
        # media never arrives.
        return {"stream_id": stream_id, "mode": "mjpeg" if is_mjpeg else "hls"}

    def _hls_url(self, stream_id, filename, query=""):
        """Build the localhost sidecar HLS URL, with read creds in the userinfo.

        Same shape as _whep_url -- httpx turns the userinfo into a Basic header
        for the sidecar's read/playback user.
        """
        cred = f"{_SIDECAR_USER}:{self._auth_pass}@" if self._auth_pass else ""
        url = f"http://{cred}{_HLS_HOST}:{_HLS_PORT}/{stream_id}/{filename}"
        return f"{url}?{query}" if query else url

    def _hls_session_client(self):
        """The shared client for the sidecar HLS hop. See __init__ for why.

        Redirects are followed because MediaMTX's cookie check IS a redirect;
        capped low because the only legitimate one is that single same-host hop
        and anything further is a sidecar we no longer recognise.
        """
        if self._hls_client is None:
            self._hls_client = httpx.AsyncClient(
                timeout=httpx.Timeout(5.0, read=30.0),
                follow_redirects=True,
                max_redirects=3,
            )
        return self._hls_client

    async def _hls_stream_response(self, url, token=""):
        """Pass an HLS playlist or segment back to the viewer.

        A playlist is read whole and rewritten so the token reaches everything
        the player follows out of it; a segment is passed through untouched.
        Playlists are small and the player is waiting on them either way.

        Streamed rather than buffered because a low-latency playlist request is
        one MediaMTX deliberately holds open until the next part exists, and a
        part can arrive as chunked transfer while it is still being written.
        The read timeout is generous for that reason; the connect timeout is
        not, because the sidecar is on loopback.
        """
        client = self._hls_session_client()
        try:
            upstream = await client.send(client.build_request("GET", url), stream=True)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Could not reach the video stream: {exc}")
        if upstream.status_code != 200:
            status = upstream.status_code
            await upstream.aclose()
            # 404 while the source spins up is normal and not worth a 502: the
            # player retries, and MediaMTX starts an on-demand source on the
            # first request.
            raise HTTPException(
                503 if status == 404 else 502,
                "The video stream is not ready yet."
                if status == 404
                else f"Video stream returned HTTP {status}.",
            )

        content_type = upstream.headers.get("content-type", "application/octet-stream")
        if token and "mpegurl" in content_type.lower():
            try:
                body = await upstream.aread()
            finally:
                await upstream.aclose()
            return Response(
                content=_rewrite_playlist(body.decode("utf-8", "replace"), token),
                media_type=content_type,
                headers={"Cache-Control": "no-store"},
            )

        async def _pump():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                # Only the response. The client is shared and outlives it.
                await upstream.aclose()

        return StreamingResponse(
            _pump(),
            media_type=content_type,
            # Playlists change every part; segments are named uniquely and are
            # gone shortly after. Nothing here is worth a cache.
            headers={"Cache-Control": "no-store"},
        )

    def _whep_url(self, stream_id, secret=None):
        """Build the localhost sidecar WHEP URL, with read creds in the userinfo.

        httpx turns the userinfo into a Basic ``Authorization`` header, which is
        how the sidecar's ``openavc`` (read/playback) user is authenticated.
        """
        cred = f"{_SIDECAR_USER}:{self._auth_pass}@" if self._auth_pass else ""
        base = f"http://{cred}{_WEBRTC_HOST}:{_WEBRTC_PORT}/{stream_id}/whep"
        return f"{base}/{secret}" if secret else base

    async def _mjpeg_stream_response(self, url):
        """Stream an upstream multipart-MJPEG body back to the panel ``<img>``.

        Opens the upstream with an unbounded read timeout (MJPEG is continuous)
        and pumps raw chunks through a ``StreamingResponse``. The httpx client
        and the upstream response are both closed when the panel disconnects
        (Starlette closes the generator), so a closed tab can't leak the
        connection to the AV-LAN encoder.

        The connection leaves through the adapter Settings > Network > Reach
        devices through chose (api.local_address), when one is chosen. httpx
        ignores a client's own transport settings once it is given a
        transport, so any added to the client go on the transport too; a
        client with a transport also stops reading proxy settings from the
        environment, which is right for a device on the AV network.
        """
        try:
            ip = self.api.local_address(httpx.URL(url).host)
        except ConnectionError as exc:
            # The adapter chosen in Reach devices through has no address.
            raise HTTPException(502, str(exc))
        transport = httpx.AsyncHTTPTransport(local_address=ip) if ip else None
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, read=None), transport=transport
        )
        try:
            request = client.build_request("GET", url)
            upstream = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(502, f"Could not reach the preview stream: {exc}")
        if upstream.status_code != 200:
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(502, f"Preview stream returned HTTP {upstream.status_code}.")

        media_type = upstream.headers.get("content-type", "multipart/x-mixed-replace")

        async def _pump():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            _pump(), media_type=media_type, headers={"Cache-Control": "no-store"}
        )

    @staticmethod
    def _validate_secret(secret):
        if not _SECRET_RE.fullmatch(secret or ""):
            raise HTTPException(422, "Invalid WHEP session id.")

    # ──── MediaMTX control API ────

    async def _api_get(self, path):
        url = f"http://{_API_HOST}:{_API_PORT}{path}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        return None

    async def _api_post(self, path, body):
        url = f"http://{_API_HOST}:{_API_PORT}{path}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, json=body)
            return resp.status_code in (200, 201)
        except httpx.HTTPError:
            return False

    async def _api_delete(self, path):
        url = f"http://{_API_HOST}:{_API_PORT}{path}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.delete(url)
            # 404 means the path is already gone — fine for an idempotent delete.
            return resp.status_code in (200, 204, 404)
        except httpx.HTTPError:
            return False

    async def _wait_until_ready(self):
        """Wait for OUR sidecar to answer -- not merely for an answer.

        The API port is a MediaMTX default, so an answer on it can come from a
        stranger: a second copy someone installed, or one of ours orphaned by a
        hard kill. Adopting it looks like success and is not -- paths would be
        registered into a process nobody supervises, while the child we did
        spawn exits on the port it cannot bind. So a reply only counts while
        our own child is alive.
        """
        deadline = time.monotonic() + _READY_TIMEOUT
        while time.monotonic() < deadline:
            if self._sidecar_is_ours() and await self._api_get("/v3/paths/list") is not None:
                return True
            await asyncio.sleep(0.3)
        return False

    def _sidecar_is_ours(self):
        """Whether the process we supervise is alive to be answering at all."""
        return self._supervisor is not None and self._supervisor.running

    # ──── ffmpeg: probe + snapshot ────

    async def _probe_rtsp(self, url):
        """Run `ffmpeg -i <url>` and parse the stream info it prints to stderr.

        ffprobe isn't bundled (the native dep extracts only ffmpeg), so we use
        ffmpeg with no output file: it prints the input's stream details, then
        exits non-zero because no output was given. The details are what we want.
        """
        args = [str(self._ffmpeg_bin), "-hide_banner", "-rtsp_transport", "tcp", "-i", url]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                **NO_WINDOW,
            )
        except OSError as e:
            return {"success": False, "message": f"Could not run ffmpeg: {e}"}
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "success": False,
                "message": "Timed out connecting to the source. Check the URL, "
                "credentials, and that the device is reachable on the network.",
            }
        return self._parse_probe(stderr.decode("utf-8", "replace"))

    @staticmethod
    def _parse_probe(text):
        vid_line = next(
            (ln for ln in text.splitlines() if re.search(r"Stream #\d+:\d+.*: Video:", ln)),
            None,
        )
        if not vid_line:
            lowered = text.lower()
            if "401 unauthorized" in lowered or "authorization failed" in lowered:
                return {"success": False, "message": "Authentication failed. Check the username and password."}
            if any(s in lowered for s in ("connection refused", "no route to host", "timed out", "timeout")):
                return {"success": False, "message": "Could not reach the source. Check the URL and that the device is online."}
            return {"success": False, "message": "No video stream was found at that URL."}
        m = re.search(r"Video:\s*([A-Za-z0-9_]+)(?:\s*\(([^)]*)\))?", vid_line)
        codec = m.group(1).lower() if m else ""
        profile = (m.group(2) or "").strip() if m else ""
        res = re.search(r"(\d{2,5})x(\d{2,5})", vid_line)
        fps_m = re.search(r"(\d+(?:\.\d+)?)\s*fps", vid_line)
        result = {
            "success": True,
            "codec": codec,
            "profile": profile,
            "width": int(res.group(1)) if res else None,
            "height": int(res.group(2)) if res else None,
            "fps": float(fps_m.group(1)) if fps_m else None,
        }
        result.update(VideoPanelPlugin._transcode_recommendation(codec, profile))
        return result

    @staticmethod
    def _transcode_recommendation(codec, profile):
        p = (profile or "").lower()
        if codec in ("hevc", "h265"):
            return {
                "transcode_recommended": True,
                "advice": "This source streams H.265/HEVC, which most browsers can't play. "
                "It will be transcoded to H.264 for the panel.",
            }
        if codec == "h264":
            if "baseline" in p:
                return {
                    "transcode_recommended": False,
                    "advice": "H.264 Baseline plays directly in every browser. No transcoding needed.",
                }
            label = f"H.264 {profile}".strip()
            return {
                "transcode_recommended": False,
                "advice": f"{label} should play in most browsers. If it stutters or fails on a "
                "panel, set Transcode to Always.",
            }
        if not codec:
            return {
                "transcode_recommended": True,
                "advice": "Could not identify the video codec; it may be transcoded.",
            }
        return {
            "transcode_recommended": True,
            "advice": f"Codec '{codec}' may not play in browsers and will be transcoded to H.264.",
        }

    async def _snapshot_rtsp(self, url):
        if not url:
            return None
        args = [
            str(self._ffmpeg_bin), "-hide_banner", "-rtsp_transport", "tcp", "-i", url,
            "-frames:v", "1", "-q:v", "4", "-f", "image2", "-",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **NO_WINDOW,
            )
        except OSError:
            return None
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_SNAPSHOT_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None
        return stdout or None

    # ──── Config + binaries ────

    def _render_config(self):
        # The sidecar's pion stack enumerates interface IPs once at process
        # start and advertises them as WebRTC host candidates for its whole
        # life; after a host IP change (DHCP renewal, a commissioning move,
        # WiFi/Ethernet swap) browsers aim their ICE checks at dead addresses
        # and WHEP playback wedges. Advertising the detected LAN address keeps
        # a reachable candidate in every offer; the status poll rewrites this
        # file and bounces the sidecar when that address changes.
        host = self._detect_local_ip()
        self._rendered_host = host
        # MediaMTX 1.18.x uses true/false booleans (not yes/no). The password is
        # hex (token_hex) and single-quoted, so it is always a safe YAML scalar.
        return (
            "# Generated by the OpenAVC Video Panel plugin. Rewritten on every\n"
            "# plugin start; manual edits are lost.\n"
            "logLevel: info\n"
            "logDestinations: [stdout]\n"
            "\n"
            "api: true\n"
            f"apiAddress: {_API_HOST}:{_API_PORT}\n"
            "\n"
            # RTSP is on, but only as a localhost TCP loopback for the transcode
            # pipeline (ffmpeg republishes H.264 here). TCP-only suppresses the
            # UDP RTP/RTCP listeners, so nothing extra is exposed.
            "rtsp: true\n"
            f"rtspAddress: {_RTSP_HOST}:{_RTSP_PORT}\n"
            "rtspTransports: [tcp]\n"
            "rtmp: false\n"
            # On, but idle: MediaMTX only muxes HLS once a reader arrives, and
            # the only reader is the tunnel proxy route below. Nothing extra is
            # ingested for it -- it hangs off the same source WebRTC already uses.
            "hls: true\n"
            f"hlsAddress: {_HLS_HOST}:{_HLS_PORT}\n"
            "hlsVariant: lowLatency\n"
            "hlsAlwaysRemux: false\n"
            "srt: false\n"
            "metrics: false\n"
            "pprof: false\n"
            "playback: false\n"
            "\n"
            "webrtc: true\n"
            f"webrtcAddress: {_WEBRTC_HOST}:{_WEBRTC_PORT}\n"
            f"webrtcLocalUDPAddress: :{_WEBRTC_UDP_PORT}\n"
            f"webrtcAdditionalHosts: ['{host}']\n"
            "\n"
            "authInternalUsers:\n"
            f"- user: {_SIDECAR_USER}\n"
            f"  pass: '{self._auth_pass}'\n"
            "  ips: []\n"
            "  permissions:\n"
            "  - action: publish\n"
            "  - action: read\n"
            "  - action: playback\n"
            "- user: any\n"
            "  pass:\n"
            "  ips: ['127.0.0.1', '::1']\n"
            "  permissions:\n"
            "  - action: api\n"
            # read + publish let the transcode ffmpeg (spawned by MediaMTX on
            # localhost) read the raw source path and publish the H.264 result
            # with no credentials in the command line. Localhost-only, same
            # trust level as the api permission above.
            "  - action: read\n"
            "  - action: publish\n"
            "\n"
            "paths: {}\n"
        )

    def _detect_local_ip(self):
        """Best-guess LAN address via the UDP-connect trick (no packets sent).

        Same method the platform's mDNS advertiser uses. Always an IP literal,
        which matters here: MediaMTX treats a webrtcAdditionalHosts hostname
        that fails DNS as fatal for that session.
        """
        ip, at = self._detected_ip
        if ip and time.monotonic() - at < _IP_CACHE_SECONDS:
            return ip
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.settimeout(0.5)
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
            finally:
                s.close()
        except OSError:
            ip = "127.0.0.1"
        self._detected_ip = (ip, time.monotonic())
        return ip

    def _load_or_create_auth(self):
        auth_file = self.api.data_dir / "sidecar.auth"
        try:
            existing = auth_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
        password = secrets.token_hex(24)
        try:
            auth_file.write_text(password, encoding="utf-8")
        except OSError as e:
            self.api.log(f"could not persist sidecar auth file: {e}", "warning")
        return password

    @staticmethod
    def _resolve_dep(filename):
        from openavc.system_config import PLUGIN_REPO_DIR

        path = PLUGIN_REPO_DIR / ".deps" / filename
        return path if path.exists() else None

    @staticmethod
    def _binary_name(stem):
        return f"{stem}.exe" if sys.platform == "win32" else stem

    @staticmethod
    def _ensure_executable(path):
        # Tar-sourced binaries keep their exec bit through extraction; zip-sourced
        # ones (and dev drop-ins) don't, so set it defensively on POSIX.
        if sys.platform == "win32":
            return
        try:
            mode = os.stat(path).st_mode
            os.chmod(path, mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass
