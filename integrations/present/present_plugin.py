"""Present plugin for OpenAVC — wireless BYOD presentation.

A guest shares a laptop screen from the browser (WebRTC/WHIP, no install) and
it appears on the space's displays. The plugin bundles MediaMTX as a sidecar
that ingests each presenter's screen and republishes it as browser-playable
WebRTC (WHEP). Each display runs the plugin's standalone Display page: an idle
"connect" card (space name, server address, join code) that cuts to the routed
presenter and back, driven by a 2-second status poll.

The space is the OpenAVC instance itself — one join code, nothing to create.
Displays are the routable outputs, presenters are the inputs, and the routing
between them behaves like an internal matrix switcher: every display follows a
source ("auto" = the active presenter, or a pinned presenter), drivable from
macros (``present.route``), scripts (``openavc.plugins.present.route``), and
writes to the ``plugin.present.display.<id>.source`` state key.

Displays come in two kinds. A **browser display** opens the plugin's Display
page (below) — sub-second, seamless switching, no transcode. A **stream
display** is for hardware decoders that pull a stream URL: the plugin runs a
persistent fixed-profile output per display (see ``output.py``) that a decoder
locks onto over RTSP or SRT, showing the connect card when nothing is routed.
The decoder-facing RTSP/SRT listeners are LAN-facing with anonymous read
granted on ``out/*`` paths only — each output path embeds a per-display
stream key, so the URL itself is the secret, revocable like a display key.

Because neither surface has an OpenAVC login, both ride the plugin's guest
routes (``/api/plugins/present/guest/*``, also mounted at the top-level
``/present/*`` via the ``guest_alias``):

- The **Display page** (a bare browser on a mini PC, a stick PC, a TV) is
  gated by a persistent per-display key carried in the Display URL. Displays
  and their keys are managed from the plugin's page in the Programmer IDE
  through the authed ``/ext`` routes.
- The **Connect page** (the presenter's own laptop) is the short URL on every
  connect card (``http://<join-address>:<port>/present``). The guest enters
  the join code shown on the displays plus a name; the plugin verifies the
  code and exchanges it for a platform-minted guest token scoped to that
  presenter's ingest name, which then gates the WHIP publish routes the page
  uses to share the screen.

This module owns the sidecar lifecycle, the displays and routing model,
presence polling (state keys + events for space automation), and both HTTP
routers.
"""

import asyncio
import json
import os
import re
import secrets
import stat
import sys
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# Sibling module. The plugin loader execs this file with the plugin directory on
# sys.path (so the flat import resolves) but not as a package, so a relative
# import would fail there. Test/CI code imports the plugin by its package path
# (integrations.present.present_plugin), where the flat name isn't on the path
# but the relative import is. Try both so the plugin loads either way.
try:
    from sidecar import SidecarSupervisor
except ImportError:  # pragma: no cover - exercised only via package-path import
    from .sidecar import SidecarSupervisor

try:
    import display_records
    import encoders
    import join_address
    import kiosk
    import output
except ImportError:  # pragma: no cover - exercised only via package-path import
    from . import display_records, encoders, join_address, kiosk, output

# A sentence raised as PluginRefusal from start() is the one the AI assistant
# repeats to the integrator; on an older OpenAVC the card shows it either way.
try:
    from openavc.core.plugin_api import PluginRefusal
except ImportError:  # OpenAVC before 0.38.0
    PluginRefusal = RuntimeError

_PLUGIN_DIR = Path(__file__).resolve().parent

# MediaMTX listeners. Signaling and the control API are loopback-only; three
# listeners face the LAN: the WebRTC media UDP port (browsers connect to it
# directly for low-latency media) and the RTSP + SRT listeners hardware
# decoders pull stream-display outputs from (read-only, out/* paths, see
# _render_config). Every port is distinct from the Video Panel plugin's (API
# 9997, WebRTC signalling 8889, WebRTC media UDP 8189, HLS 8890, RTSP 8556) so
# both plugins can run on one system; tests/test_mediamtx_ports.py fails when
# the two share one.
_API_HOST = "127.0.0.1"
_API_PORT = 9998
_WEBRTC_HOST = "127.0.0.1"
_WEBRTC_PORT = 8891
_WEBRTC_UDP_PORT = 8190
_RTSP_PORT = 8554
_SRT_PORT = 8899
_READY_TIMEOUT = 10.0
# Presence poll cadence. Also the ceiling on how stale a Display page's
# idle/live decision can be (it polls its status route at the same rate).
_POLL_SECONDS = 2.0

_MEDIAMTX_VERSION = "1.18.2"
_SIDECAR_USER = "openavc"

# Join code shown on every idle card. Rotates when the space's last presenter
# leaves (session end) and on a timer while idle, so a code seen on a display
# is always fresh. The Connect page's POST /connect verifies it and exchanges
# it for a guest token; a wrong code is a 401 that feeds the platform's
# brute-force accounting.
_CODE_LENGTH = 4
_CODE_ROTATE_SECONDS = 300.0

# The top-level short route (PLUGIN_INFO guest_alias) the platform also
# mounts the guest router at — the address a human types from the card.
_GUEST_ALIAS = "present"

# Guest tokens gate WHIP signaling only (established media keeps flowing when
# one expires), so size the TTL for a long meeting rather than a request.
_GUEST_TOKEN_TTL_SECONDS = 4 * 3600

# Presenter display names are trimmed to this before slugging; keeps the
# presenters state JSON and on-screen badges sane.
_NAME_MAX_CHARS = 60

# Presenters publish their screen to in/<presenter>; each stream display's
# encoder publishes to out/<display>-<stream_key>. The prefixes keep the two
# namespaces apart on the sidecar — and the sidecar's anonymous LAN read
# permission is scoped to out/*, so a decoder can never pull an ingest.
_INGEST_PREFIX = "in"
_OUTPUT_PREFIX = "out"

# The routing value that means "follow the active presenter" rather than a
# pinned one. Reserved everywhere a presenter or display name could collide
# with it.
_AUTO = "auto"

# Presenter names arrive as the second segment of an ingest path
# (in/<presenter>), chosen by the publisher; constrain before they're spliced
# into proxied sidecar URLs.
_PRESENTER_RE = re.compile(r"[A-Za-z0-9._-]+")
# WHEP session secrets are MediaMTX-minted UUIDs; constrain to a URL-safe set
# before they're spliced into the proxied sidecar URL.
_SECRET_RE = re.compile(r"[A-Za-z0-9-]+")

_DISPLAY_ERROR_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Present</title>
<style>
  body { margin: 0; height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #101512; color: #e8ede9; font-family: system-ui, sans-serif; text-align: center; }
  .card { max-width: 34em; padding: 2em; }
  h1 { color: #8AB493; font-size: 1.6em; margin-bottom: 0.4em; }
  p { line-height: 1.5; opacity: 0.85; }
</style></head>
<body><div class="card">
  <h1>This display link isn't valid</h1>
  <p>The link may be incomplete, or the display's key may have been
  regenerated. Open the Present plugin page in the OpenAVC Programmer and
  copy the display's link again.</p>
</div></body></html>
"""


class DisplayIn(BaseModel):
    """Add/edit payload for a display. The plugin page's form posts this."""

    label: str
    display_id: str | None = None
    kind: str | None = None  # "browser" | "stream"; None keeps/defaults
    # Show this browser display on one of this server's own video outputs.
    # None keeps the current value; "" clears it.
    local_output: str | None = None


class RouteIn(BaseModel):
    """Route payload: 'auto' or a presenter name."""

    source: str


class ConnectIn(BaseModel):
    """Connect page's code->token exchange payload."""

    name: str
    code: str


class PresentPlugin:

    PLUGIN_INFO = {
        "id": "present",
        "name": "Present",
        "version": "0.7.5",
        "author": "OpenAVC",
        "description": "Wireless presentation: share a laptop screen from the browser to the space's displays.",
        "category": "integration",
        "license": "MIT",
        "platforms": ["win_x64", "linux_x64", "linux_arm64"],
        "min_openavc_version": "0.25.0",
        "capabilities": [
            "state_read",
            "state_write",
            "event_emit",
            "network_listen",
            "http_endpoints",
            "guest_endpoints",
        ],
        "guest_alias": _GUEST_ALIAS,
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
                # These URLs 404'd for every platform at once: they pointed at
                # the `latest` RELEASE, whose asset set ROTATES, so
                # `ffmpeg-n7.1-latest-...` was pinned in name only and stopped
                # existing the day BtbN retired n7.1 from it. Install fetched
                # nothing and start failed with "FFmpeg binary not found",
                # which reinstalling could not fix.
                #
                # `autobuild-*` tags are immutable, but they are not all kept:
                # BtbN retains roughly the last fortnight of dailies and then
                # only MONTH-END builds, going back years. So pin a month-end
                # one (this is 2026-07-31) or it gets pruned in a fortnight and
                # we are back here.
                #
                # KEEP THIS BUILD IDENTICAL TO THE VIDEO PANEL PLUGIN'S. Both
                # extract to the same shared plugin_repo/.deps/ffmpeg, so two
                # different pins means whichever installs last wins and the
                # other plugin silently runs a build it never validated.
                #
                # When bumping, re-verify the binary rather than trusting the
                # name. Checked on all three for this build: LGPL (--enable-
                # version3, --disable-libx264/libx265, no --enable-gpl);
                # libfreetype + libharfbuzz for the connect card's drawtext;
                # libopenh264 for the software encoder; libsrt for stream
                # displays; s302m, mpeg2video and aac for the pump and encoder
                # legs. Hardware H.264: win64 has qsv/nvenc/amf/mf, linux64
                # vaapi/qsv/nvenc/amf, arm64 v4l2m2m + nvenc and NO vaapi or
                # qsv -- which is what platform_priority() already assumes.
                # You cannot execute the other platforms' binaries, so read the
                # embedded configure string and the AVCodec names.
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
        "usage": (
            "Add a display on this plugin page, then put it on glass: a "
            "browser display's link opens in a browser on the device driving "
            "that screen (full screen); a stream display publishes a "
            "persistent RTSP/SRT stream a hardware decoder pulls by URL. "
            "Either way the display shows a connect card with the join "
            "address and code; a guest types that address into a browser, "
            "enters the code and a name, and shares their screen. A browser "
            "display can also be shown by this server itself, fullscreen on "
            "one of its own video outputs. Route a specific presenter to a "
            "display from this page, a macro's Route Display step, or a "
            "script. Screen capture in the guest's browser requires HTTPS "
            "(enable it in Settings > Security); on a cloud-paired system, "
            "installing a trusted certificate there also removes the guest "
            "browser warning entirely."
        ),
    }

    CONFIG_SCHEMA = {
        "space_name": {
            "type": "string",
            "label": "Space Name",
            "description": (
                "Shown on every display's connect card. Leave blank to use "
                "the project name."
            ),
            "default": "",
        },
        "join_address": {
            "type": "string",
            "label": "Join Address",
            "description": (
                "IP address or hostname shown on the connect cards — what "
                "guests type to reach this server. Leave blank to auto-detect "
                "the LAN address. Set it on multi-network installs where "
                "guests reach the server on a different network than the "
                "displays."
            ),
            "default": "",
        },
        "local_browser_path": {
            "type": "string",
            "label": "Browser Path",
            "description": (
                "Full path to the Chrome, Chromium, or Edge executable used "
                "when a display is shown on this server's own video output. "
                "Leave blank to find an installed browser automatically."
            ),
            "default": "",
        },
    }

    # Each display's key and stream key are logins the plugin makes itself.
    # OpenAVC keeps them in the system's login file rather than in the project
    # and hands them back in `api.config`; one that is not saved on this
    # system comes back empty, and _load_displays makes a new one. An older
    # OpenAVC ignores this and keeps them in the project.
    CONFIG_SECRETS = {
        "displays": {"id": "id", "fields": ["display_key", "stream_key"], "generated": True},
    }

    MACRO_ACTIONS = {
        "present.route": {
            "label": "Route Display",
            "description": "Route a source (a presenter, or Auto) to a display.",
            "icon": "monitor",
            "handler": "action_route",
            "params": [
                {
                    "key": "display",
                    "type": "select",
                    "label": "Display",
                    "required": True,
                    "options_source": "plugin.present.displays",
                },
                {
                    "key": "source",
                    "type": "select",
                    "label": "Source",
                    "required": True,
                    "options_source": "plugin.present.sources",
                    "description": "Auto follows the active presenter; a name pins that presenter.",
                },
            ],
        },
    }

    SCRIPT_API = {
        "route": {
            "handler": "script_route",
            "doc": "Route a source ('auto' or a presenter name) to a display.",
        },
    }

    def __init__(self):
        self.api = None
        self._supervisor = None
        self._mediamtx_bin = None
        self._ffmpeg_bin = None
        self._auth_pass = ""
        self._config_path = None
        self._displays = []  # configured display dicts (the source of truth)
        # Runtime:
        self._routing = {}  # display_id -> "auto" | pinned presenter name
        self._presence = {}  # presenter_name -> since_epoch
        self._code = ("", 0.0)  # (join code, rotated_at monotonic)
        self._labels = {}  # presenter slug -> the display name the guest typed
        self._join_address = join_address.JoinAddress(_GUEST_ALIAS)
        self._rendered_join_host = None  # join host baked into the sidecar config
        # Stream-display output pipeline:
        self._controllers = {}  # display_id -> output.OutputController
        self._card = None  # output.IdleCard once start() has a data_dir
        self._font = None
        self._encoder_name = None  # detected once, first stream display start
        self._encoder_lock = asyncio.Lock()
        self._card_join = None  # last join line written to the card
        self._card_space = None  # last space name written to the card
        # Local-fullscreen kiosk browsers (browser displays with local_output):
        self._kiosk = None  # kiosk.KioskManager once start() has a data_dir

    # ──── Lifecycle ────

    async def start(self, api):
        self.api = api

        self._mediamtx_bin = self._resolve_dep(self._binary_name("mediamtx"))
        self._ffmpeg_bin = self._resolve_dep(self._binary_name("ffmpeg"))
        if self._mediamtx_bin is None or self._ffmpeg_bin is None:
            missing = "MediaMTX" if self._mediamtx_bin is None else "FFmpeg"
            await self._refuse(
                f"{missing} binary not found in plugin_repo/.deps/. Native "
                "dependencies install when the plugin is installed from the "
                "community repository; reinstall the Present plugin."
            )
        self._ensure_executable(self._mediamtx_bin)
        self._ensure_executable(self._ffmpeg_bin)

        self._auth_pass = self._load_or_create_auth()
        self._config_path = self.api.data_dir / "mediamtx.yml"
        self._config_path.write_text(self._render_config(), encoding="utf-8")

        # If something already answers on the control port, our MediaMTX
        # could never bind — it would crash-loop into the circuit breaker
        # while the imposter (usually an orphan from an unclean shutdown)
        # half-serves traffic. Refuse loudly instead.
        if await self._api_get("/v3/paths/list") is not None:
            await self._refuse(
                f"Another process is already using this plugin's media ports "
                f"(something answers on {_API_HOST}:{_API_PORT}) — usually a "
                "MediaMTX left over from an unclean shutdown. Close it (or "
                "restart this computer), then enable the plugin again."
            )

        await self.api.state_set("running", False)
        await self.api.state_set("error", "")
        await self.api.state_set("sidecar", "starting")

        self._supervisor = SidecarSupervisor(
            [str(self._mediamtx_bin), str(self._config_path)],
            name="mediamtx",
            log=self.api.log,
            on_status=self._on_sidecar_status,
            on_circuit_break=self._on_sidecar_circuit_break,
            task_factory=self.api.create_task,
        )

        try:
            await self._bring_up()
        except Exception:
            # Don't leave orphaned processes if start() fails partway.
            if self._kiosk is not None:
                await self._kiosk.stop()
                self._kiosk = None
            await self._supervisor.stop()
            self._supervisor = None
            raise

    async def _refuse(self, msg):
        """Mark the plugin not running with ``msg`` as its error, and refuse
        to start with it. A sentence raised as PluginRefusal is the one the
        AI assistant repeats to the integrator."""
        await self.api.state_set("running", False)
        await self.api.state_set("error", msg)
        raise PluginRefusal(msg)

    async def _bring_up(self):
        """Everything after the sidecar is spawned, in order: start() stops
        whatever this got to (the kiosk windows, then the sidecar) if any
        step raises."""
        await self._supervisor.start()
        if not await self._wait_until_ready():
            raise RuntimeError(
                f"MediaMTX did not respond on {_API_HOST}:{_API_PORT} "
                f"within {int(_READY_TIMEOUT)}s"
            )
        await self.api.state_set("running", True)
        self.api.register_router(self._build_ext_router())
        self.api.register_guest_router(self._build_guest_router())
        await self._load_displays()
        # The connect card's dynamic text lives in small files the stream
        # displays' idle pumps re-read every frame; set them up before the
        # first code rotation so it lands on the card.
        self._card = output.IdleCard(self.api.data_dir / "card")
        self._font = output.find_font()
        self._rotate_code()
        await self._refresh_card()
        await self._publish_all()
        await self._start_stream_outputs()
        # Local-fullscreen windows: one supervised kiosk browser per
        # browser display pinned to one of this host's video outputs.
        self._kiosk = kiosk.KioskManager(
            environment=kiosk.detect_environment(
                _PLUGIN_DIR, self.api.data_dir, self.api.log
            ),
            data_dir=self.api.data_dir,
            log=self.api.log,
            task_factory=self.api.create_task,
            browser_resolver=self._resolve_browser,
        )
        await self._sync_kiosks()
        # The routing state key is the matrix's write surface: macros
        # (state.set), scripts, and the API drive it directly. Subscribe
        # after the initial publish; our own writes no-op in the handler.
        await self.api.state_subscribe(
            "plugin.present.display.*.source", self._on_source_write
        )
        self.api.create_periodic_task(
            self._poll, interval_seconds=_POLL_SECONDS, name="presence_poll"
        )
        self.api.log(
            f"Present started: MediaMTX {_MEDIAMTX_VERSION} on "
            f"{_API_HOST}:{_API_PORT}, {len(self._displays)} display(s)"
        )

    async def stop(self):
        # State keys, subscriptions, and managed tasks are cleaned up by the
        # platform; we only need to stop the external processes — kiosk
        # browsers and output encoders first (the encoders publish into the
        # sidecar), then the sidecar.
        if self._kiosk is not None:
            await self._kiosk.stop()
            self._kiosk = None
        await self._stop_all_outputs()
        if self._supervisor is not None:
            await self._supervisor.stop()
            self._supervisor = None

    async def health_check(self):
        if self._supervisor is not None and self._supervisor.running:
            return {"status": "ok", "message": f"MediaMTX running (pid {self._supervisor.pid})"}
        return {"status": "error", "message": "MediaMTX sidecar is not running"}

    # ──── Sidecar callbacks ────

    async def _on_sidecar_status(self, status):
        await self.api.state_set("sidecar", status)
        if status in ("restarting", "failed"):
            await self.api.state_set("running", False)

    async def _on_sidecar_circuit_break(self, reason):
        await self.api.state_set("running", False)
        await self.api.state_set("error", reason)
        await self.api.event_emit("error", {"reason": reason})

    async def _maybe_apply_join_host_change(self):
        """Rebuild the sidecar when the join host has moved (True if it did).

        Every session live at that moment already carries the old address in
        its ICE candidates, so the brief outage from the restart is strictly
        better than advertising a dead address until the next plugin restart.
        """
        host = self._join_host()
        if host == self._rendered_join_host:
            return False
        self.api.log(
            f"join address changed ({self._rendered_join_host} -> {host}); "
            "restarting MediaMTX so WebRTC advertises the new address",
            "warning",
        )
        await self._restart_sidecar()
        return True

    async def _restart_sidecar(self):
        """Bounce MediaMTX on a freshly rendered config, plus the output
        encoders that publish into it (left running they'd only crash-loop
        through their backoff while it's down). Stream-display decoders
        re-acquire on their own retry once the output path is back."""
        await self._stop_all_outputs()
        await self._supervisor.stop()
        self._config_path.write_text(self._render_config(), encoding="utf-8")
        await self._supervisor.start()
        if not await self._wait_until_ready():
            self.api.log(
                f"MediaMTX did not respond on {_API_HOST}:{_API_PORT} within "
                f"{int(_READY_TIMEOUT)}s of an address-change restart",
                "error",
            )
            await self.api.state_set("running", False)
            return
        await self._start_stream_outputs()

    # ──── Space ────

    async def _space_name(self):
        configured = (self.api.config.get("space_name") or "").strip()
        if configured:
            return configured
        # Fall back to the loaded project's name (published by the engine).
        return (await self.api.state_get("system.project_name")) or ""

    def _rotate_code(self):
        code = "".join(secrets.choice("0123456789") for _ in range(_CODE_LENGTH))
        self._code = (code, time.monotonic())
        if self._card is not None:
            # Stream displays' idle pumps re-read this file every frame, so
            # the rotated code shows on air without touching the pipeline.
            self._card.write_code(code)
        return code

    def _current_code(self):
        return self._code[0]

    async def _refresh_card(self):
        """Keep the connect card's space name and join line current.

        Cheap enough to call from the poll: writes only happen when the value
        actually changed (the join line can move when the auto-detected LAN
        address changes; the space name when the project is renamed).
        """
        if self._card is None:
            return
        space = await self._space_name()
        if space != self._card_space:
            self._card.write_space(space)
            self._card_space = space
        join = await self._join_url()
        if join != self._card_join:
            self._card.write_join(join)
            self._card_join = join

    async def _join_url(self):
        """The address a guest types, exactly as the connect cards show it."""
        return await self._join_address.url(
            self._join_host(),
            lambda: self.api.state_get("system.cloud.cert_status"),
        )

    def _join_host(self):
        """The host guests reach this instance at (join_address.JoinAddress.host)."""
        configured = self.api.config.get("join_address") if self.api is not None else None
        return self._join_address.host(configured)

    # ──── Displays ────

    async def _load_displays(self):
        configured = self.api.config.get("displays", [])
        self._displays = []
        if not isinstance(configured, list):
            return
        if display_records.load_into(self._displays, configured):
            await self._persist_displays()
        # Routing is runtime state: every display comes up following the
        # active presenter. Pins don't survive a restart (revisit if that
        # proves sticky-worthy).
        self._routing = {d["id"]: _AUTO for d in self._displays}

    async def _persist_displays(self):
        """Write the current display list back to the project file via the platform."""
        cfg = self.api.config  # a copy; safe to mutate
        cfg["displays"] = self._displays
        cfg.pop("rooms", None)  # pre-0.2.0 config carried a rooms list
        await self.api.save_config(cfg)

    def _find_display(self, display_id):
        return display_records.find(self._displays, display_id)

    def _display_path(self, display):
        """Site-relative Display URL; the plugin page prepends the origin.

        Uses the short guest-alias mount (the canonical
        /api/plugins/present/guest/... path serves the same page).
        """
        return (
            f"/{_GUEST_ALIAS}/display/{display['id']}"
            f"?key={display.get('display_key', '')}"
        )

    @staticmethod
    def _stream_path(display):
        """The sidecar path a stream display's encoder publishes to.

        The embedded stream key is the read secret: the sidecar grants
        anonymous LAN read on out/* only, so knowing the URL is what
        authorizes a decoder — same model as the Display URLs, revocable per
        display by regenerating the key.
        """
        return f"{_OUTPUT_PREFIX}/{display['id']}-{display.get('stream_key', '')}"

    # ──── Local-fullscreen kiosks (browser displays on this host's outputs) ────

    def _resolve_browser(self):
        """The kiosk browser, honoring the Browser Path config override."""
        override = (self.api.config.get("local_browser_path") or "").strip()
        return kiosk.find_browser(override or None)

    def _local_display_url(self, display):
        """The Display URL the kiosk browser opens — this host, so localhost."""
        return f"{self._join_address.local_origin()}{self._display_path(display)}"

    async def _sync_kiosks(self):
        """Hand the kiosk manager the current local-display set.

        Called on start and after every display change that affects a local
        window: local_output edits, kind changes, renames (new URL + state
        keys), display-key regeneration (new URL), deletes.
        """
        if self._kiosk is None:
            return
        specs = {}
        for display in self._displays:
            if display.get("kind") == "browser" and display.get("local_output"):
                specs[display["id"]] = {
                    "output": display["local_output"],
                    "url": self._local_display_url(display),
                }
        await self._kiosk.sync(specs)

    # ──── Routing (the internal matrix) ────

    @staticmethod
    def _normalize_source(source):
        """Canonical routing value, or None when invalid.

        Empty/None mean "auto" — writing '' to a display's source key clears
        a pin, mirroring how an unset selection falls back elsewhere.
        """
        if source is None or source == "":
            return _AUTO
        if not isinstance(source, str):
            return None
        if source == _AUTO or _PRESENTER_RE.fullmatch(source):
            return source
        return None

    def _resolve_source(self, display_id, live_names):
        """The presenter this display should show ('' = the idle card).

        "auto" follows the earliest-joined active presenter; a pinned
        presenter who isn't sharing resolves to the idle card, not to
        another presenter.
        """
        routed = self._routing.get(display_id, _AUTO)
        if routed == _AUTO:
            return self._earliest_presenter(live_names)
        return routed if routed in live_names else ""

    def _earliest_presenter(self, names):
        if not names:
            return ""
        # A presenter the poll hasn't recorded yet sorts last, not first.
        return min(sorted(names), key=lambda n: self._presence.get(n, float("inf")))

    async def _route(self, display_id, source):
        """The one matrix take, shared by every write surface (macro action,
        script API, ext route, state-key writes). Raises ValueError with a
        user-facing message on an unknown display or malformed source."""
        display = self._find_display(display_id)
        if display is None:
            raise ValueError(display_records.no_such_display(display_id))
        normalized = self._normalize_source(source)
        if normalized is None:
            raise ValueError("Source must be 'auto' or a presenter name.")
        if self._routing.get(display_id) == normalized:
            return  # already routed there; not a change
        self._routing[display_id] = normalized
        await self.api.state_set(f"display.{display_id}.source", normalized)
        await self.api.event_emit(
            "route_changed", {"display": display_id, "source": normalized}
        )
        await self._publish_display(display_id, set(self._presence))

    async def _on_source_write(self, key, value, old_value):
        """Honor writes to plugin.present.display.<id>.source.

        Fires for our own publishes too; those match the routing table and
        return early. A cleared key (display removed) is not a route request.
        """
        display_id = self._source_key_display(key)
        if display_id is None or value is None:
            return
        normalized = self._normalize_source(value)
        if normalized is not None and normalized == self._routing.get(display_id):
            return
        try:
            await self._route(display_id, value)
        except ValueError as e:
            self.api.log(f"rejected write to {key} ({value!r}): {e}", "warning")
            # Put the truth back so the bad value doesn't linger in the store.
            current = self._routing.get(display_id)
            if current is not None:
                await self.api.state_set(f"display.{display_id}.source", current)

    @staticmethod
    def _source_key_display(key):
        """The display id in a plugin.present.display.<id>.source key, else None."""
        parts = key.split(".")
        if len(parts) != 5 or parts[2] != "display" or parts[4] != "source":
            return None
        return parts[3]

    async def action_route(self, params, _context):
        await self._route((params.get("display") or "").strip(), params.get("source"))

    async def script_route(self, display: str, source: str = _AUTO) -> None:
        await self._route(display, source)

    # ──── Stream-display outputs (the per-display encoder pipeline) ────

    async def _start_output(self, display):
        """Bring up the persistent output for a stream display."""
        display_id = display["id"]
        if display_id in self._controllers:
            return
        if self._font is None:
            # No system font means no renderable connect card. Surface it
            # loudly; the display's encoder_state reads "error" until fixed.
            self.api.log(
                f"stream display '{display_id}': no usable system font for "
                "the connect card (install DejaVu fonts); output not started",
                "error",
            )
            return
        await self._ensure_encoder()
        controller = output.OutputController(
            display_id=display_id,
            ffmpeg_bin=self._ffmpeg_bin,
            encoder=self._encoder_name,
            publish_url=f"rtsp://127.0.0.1:{_RTSP_PORT}/{self._stream_path(display)}",
            ingest_url_for=self._ingest_url,
            tracks_for=self._ingest_tracks,
            card=self._card,
            font=self._font,
            log=self.api.log,
            task_factory=self.api.create_task,
        )
        self._controllers[display_id] = controller
        await controller.start()
        # Catch up with current routing (a presenter may already be live).
        await self._publish_display(display_id, set(self._presence))

    async def _start_stream_outputs(self):
        """Bring up the output of every stream display, in the displays' order."""
        for display in self._displays:
            if display.get("kind") == "stream":
                await self._start_output(display)

    async def _stop_all_outputs(self):
        for display_id in list(self._controllers):
            await self._stop_output(display_id)

    async def _stop_output(self, display_id):
        controller = self._controllers.pop(display_id, None)
        if controller is not None:
            await controller.stop()

    async def _restart_output(self, display):
        """Stream path changed (id or key): the old URL dies, a new one starts."""
        await self._stop_output(display["id"])
        await self._start_output(display)

    async def _ensure_encoder(self):
        """Detect the H.264 encoder once (hardware when present, else
        libopenh264). Serialized: parallel display adds must not both probe."""
        async with self._encoder_lock:
            if self._encoder_name is None:
                self._encoder_name = await encoders.select_encoder(
                    self._ffmpeg_bin, "auto", self.api.log
                )

    def _ingest_url(self, presenter):
        """The live pump's read URL for a presenter's ingest.

        Reads require credentials (anonymous loopback carries publish only),
        so inject the internal user — same pattern as the WHEP proxy.
        """
        cred = f"{_SIDECAR_USER}:{self._auth_pass}@" if self._auth_pass else ""
        return f"rtsp://{cred}127.0.0.1:{_RTSP_PORT}/{_INGEST_PREFIX}/{presenter}"

    async def _ingest_tracks(self, presenter):
        """Track names on a presenter's ingest (None when the API is down).

        The live pump maps real audio only when the ingest actually carries
        an audio track; otherwise it maps silence, keeping the intermediate's
        stream layout constant for the never-restarted encoder.
        """
        data = await self._api_get(f"/v3/paths/get/{_INGEST_PREFIX}/{presenter}")
        if data is None:
            return None
        return data.get("tracks") or []

    # ──── Presence (sidecar paths -> state keys + events) ────

    async def _scan_presenters(self):
        """Live presenters from the sidecar's path list.

        Returns ``None`` when the sidecar API is unreachable, else the set of
        live presenter names.
        """
        data = await self._api_get("/v3/paths/list")
        if data is None:
            return None
        live = set()
        for item in data.get("items", []):
            presenter = self._live_presenter(item)
            if presenter is not None:
                live.add(presenter)
        return live

    @staticmethod
    def _live_presenter(item):
        """The presenter a sidecar path belongs to, or None.

        Ingest paths are named ``in/<presenter>`` by the publisher; a path
        counts once its stream is available.
        """
        name = item.get("name") or ""
        prefix, sep, presenter = name.partition("/")
        if prefix != _INGEST_PREFIX or not sep or not presenter:
            return None
        # "auto" is the routing sentinel, never a valid presenter.
        if presenter == _AUTO or not _PRESENTER_RE.fullmatch(presenter):
            return None
        # `ready` is deprecated in MediaMTX 1.18.x in favour of `available`.
        if not bool(item.get("available", item.get("ready", False))):
            return None
        return presenter

    async def _poll(self):
        # Only OUR sidecar counts. Without this guard, an imposter answering
        # the API port (an orphaned MediaMTX from an unclean shutdown) would
        # keep flipping running back to True while the supervisor sits
        # circuit-broken — masking the failure until someone presents.
        if self._supervisor is None or not self._supervisor.running:
            await self.api.state_set("running", False)
            return
        if await self._maybe_apply_join_host_change():
            return  # sidecar just bounced; the next poll rebuilds presence
        live = await self._scan_presenters()
        if live is None:
            await self.api.state_set("running", False)
            return
        await self.api.state_set("running", True)
        was_live = bool(self._presence)
        await self._apply_presence(live)
        self._rotate_code_when_due(was_live)
        await self._refresh_card()
        await self._publish_all()

    async def _apply_presence(self, live):
        """Record who joined and who left since the last poll, announcing each
        (joins first, each group in name order). A leaver's typed name goes
        with them."""
        now = int(time.time())
        for name in sorted(live - set(self._presence)):
            self._presence[name] = now
            await self.api.event_emit(
                "presenter_joined", {"name": name, "label": self._label_for(name)}
            )
        for name in sorted(set(self._presence) - live):
            del self._presence[name]
            label = self._labels.pop(name, name)
            await self.api.event_emit("presenter_left", {"name": name, "label": label})

    def _rotate_code_when_due(self, was_live):
        """The join code rotates when a session ends (a code seen on the
        displays during the meeting must not open the next one) and while
        idle, once it is older than _CODE_ROTATE_SECONDS."""
        is_live = bool(self._presence)
        _code, rotated_at = self._code
        if was_live and not is_live:
            self._rotate_code()
        elif not is_live and time.monotonic() - rotated_at > _CODE_ROTATE_SECONDS:
            self._rotate_code()

    def _label_for(self, presenter):
        """The display name the guest typed at connect time, else the slug.

        A presenter who bypassed the Connect page (a bench WHIP publish
        straight to the sidecar) has no label; their ingest name stands in.
        """
        return self._labels.get(presenter, presenter)

    def _presenters_list(self, presence):
        return [
            {"name": n, "label": self._label_for(n), "since": s}
            for n, s in sorted(presence.items(), key=lambda kv: (kv[1], kv[0]))
        ]

    def _sources_options(self):
        """The routable-source list, in the {value, label} shape the macro
        builder's options_source picker parses. Routing values stay the
        path-safe ingest names; the labels are the typed display names."""
        return [{"value": _AUTO, "label": "Auto (active presenter)"}] + [
            {"value": p["name"], "label": p["label"]}
            for p in self._presenters_list(self._presence)
        ]

    async def _publish_all(self):
        """(Re)publish the space keys, the pick lists, and every display.

        Derives from the runtime presence bookkeeping, so re-publishing after
        a display CRUD action never stomps a live display back to idle (which
        would fire a spurious presentation-off trigger).
        """
        presenters = self._presenters_list(self._presence)
        await self.api.state_set("code", self._current_code())
        await self.api.state_set("presenters", json.dumps(presenters))
        await self.api.state_set("active_presenters", len(presenters))
        # Entries carry value=id so the same key feeds the Route Display
        # dropdown (options_source wants {value, label}).
        await self.api.state_set(
            "displays",
            json.dumps([
                {"id": d["id"], "value": d["id"], "label": d.get("label") or d["id"]}
                for d in self._displays
            ]),
        )
        await self.api.state_set("sources", json.dumps(self._sources_options()))
        live_names = set(self._presence)
        for display in self._displays:
            await self._publish_display(display["id"], live_names)

    async def _publish_display(self, display_id, live_names):
        showing = self._resolve_source(display_id, live_names)
        await self.api.state_set(
            f"display.{display_id}.source", self._routing.get(display_id, _AUTO)
        )
        await self.api.state_set(f"display.{display_id}.showing", showing)
        await self.api.state_set(
            f"display.{display_id}.output_state", "live" if showing else "idle"
        )
        # A stream display's output follows the same resolution: hand the
        # target to its controller (a no-op when nothing changed). Routing
        # changes land here immediately via _route; presence changes via the
        # 2 s poll.
        controller = self._controllers.get(display_id)
        if controller is not None:
            controller.show(showing)

    async def _clear_display_state(self, display_id):
        for suffix in ("source", "showing", "output_state"):
            await self.api.state_set(f"display.{display_id}.{suffix}", None)
        self._routing.pop(display_id, None)

    # ──── Guest router (mounted at /api/plugins/present/guest/) ────

    def _guest_display(self, display_id, key):
        """Resolve a display for a guest call, or raise 401.

        A missing display and a bad key both return 401 — guest callers learn
        nothing about which display ids exist, and every failure feeds the
        platform rate limiter's brute-force accounting.
        """
        display = None
        if display_records.ID_RE.fullmatch(display_id or ""):
            display = self._find_display(display_id)
        if display is None or not self._key_matches(key, display):
            raise HTTPException(401, "Invalid or missing display key")
        return display

    @staticmethod
    def _key_matches(key, display):
        supplied = (key or "").strip()
        expected = display.get("display_key") or ""
        return bool(supplied) and secrets.compare_digest(supplied, expected)

    def _build_guest_router(self):
        guest = APIRouter()

        # ── Connect page (the presenter's sender surface) ──
        # The root of both guest mounts; via the top-level alias this is the
        # short URL on every connect card (http://<join-address>:<port>/present
        # redirects here). Deliberately open — the join code is the gate, and
        # it is checked at the POST /connect exchange, not the page serve.
        @guest.get("/")
        async def connect_page():
            return self._panel_page("connect.html")

        @guest.post("/connect")
        async def connect(data: ConnectIn):
            return await self._connect_guest(data)

        # ── WHIP (WebRTC publish) reverse proxy ──
        # The Connect page POSTs its SDP offer here with the guest token from
        # the /connect exchange; we forward to the sidecar's WHIP endpoint for
        # in/<presenter>. Signaling is loopback-only on the sidecar, and its
        # localhost "any" user carries publish (no read), so the proxied
        # publish needs no credential injection.
        @guest.post("/whip/{presenter}")
        async def whip_publish(presenter: str, request: Request, token: str = ""):
            self._check_publisher(presenter, token)
            return await self._proxy_offer(self._whip_url(presenter), request)

        @guest.patch("/whip/{presenter}/{secret}")
        async def whip_trickle(
            presenter: str, secret: str, request: Request, token: str = ""
        ):
            return await self._whip_session(presenter, secret, request, token)

        @guest.delete("/whip/{presenter}/{secret}")
        async def whip_teardown(
            presenter: str, secret: str, request: Request, token: str = ""
        ):
            return await self._whip_session(presenter, secret, request, token)

        @guest.get("/display/{display_id}")
        async def display_page(display_id: str, key: str = ""):
            try:
                self._guest_display(display_id, key)
            except HTTPException:
                # Friendly page for a human at the display, still a 401 so
                # bad-key guessing throttles like any other guest failure.
                return HTMLResponse(_DISPLAY_ERROR_HTML, status_code=401)
            return self._panel_page("display.html")

        @guest.get("/displays/{display_id}/status")
        async def display_status(display_id: str, key: str = ""):
            return await self._display_status(self._guest_display(display_id, key))

        # ── WHEP (WebRTC playback) reverse proxy ──
        # The Display page's WHEP client POSTs an SDP offer here; we forward it
        # to the sidecar's WebRTC server (localhost, Basic-authed via the URL
        # userinfo httpx applies) and return the SDP answer.
        @guest.post("/whep/{display_id}/{presenter}")
        async def whep_offer(display_id: str, presenter: str, request: Request, key: str = ""):
            self._check_viewer(display_id, key, presenter)
            return await self._proxy_offer(self._whep_url(presenter), request)

        @guest.patch("/whep/{display_id}/{presenter}/{secret}")
        async def whep_trickle(
            display_id: str, presenter: str, secret: str, request: Request, key: str = ""
        ):
            return await self._whep_session(display_id, presenter, secret, request, key)

        @guest.delete("/whep/{display_id}/{presenter}/{secret}")
        async def whep_teardown(
            display_id: str, presenter: str, secret: str, request: Request, key: str = ""
        ):
            return await self._whep_session(display_id, presenter, secret, request, key)

        return guest

    @staticmethod
    def _panel_page(name):
        """One of the plugin's own pages, never cached by the browser."""
        html = (_PLUGIN_DIR / "panel" / name).read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def _connect_guest(self, data):
        """The Connect page's exchange: the join code shown on the displays
        and a name, for a guest token scoped to that presenter's ingest name."""
        self._check_join_code(data.code)
        label = " ".join((data.name or "").split())[:_NAME_MAX_CHARS]
        presenter = self._new_presenter_name(label)
        self._labels[presenter] = label
        # The scope binds the token to this ingest name: it can publish
        # in/<presenter> and nothing else.
        token, expires_at = self.api.mint_guest_token(
            f"whip:{presenter}", ttl=_GUEST_TOKEN_TTL_SECONDS
        )
        return {
            "token": token,
            "expires_at": expires_at,
            "presenter": presenter,
            "label": label,
            "space_name": await self._space_name(),
        }

    def _check_join_code(self, code):
        code = (code or "").strip()
        current = self._current_code()
        if not code or not current or not secrets.compare_digest(code, current):
            # 401 feeds the platform's brute-force accounting.
            raise HTTPException(
                401, "That join code isn't right. Check the code on the display."
            )

    def _new_presenter_name(self, label):
        """The ingest name for a guest typing ``label``, or the refusal."""
        presenter = self._slugify_presenter(label)
        if not presenter:
            raise HTTPException(422, "Enter your name.")
        if presenter == _AUTO:
            raise HTTPException(422, "That name is reserved. Use a different name.")
        if presenter in self._presence:
            raise HTTPException(
                409,
                f'Someone is already presenting as "{self._label_for(presenter)}". '
                "Use a different name.",
            )
        return presenter

    async def _display_status(self, display):
        """What a Display page polls: who it shows, and what its idle card says."""
        live = await self._scan_presenters()
        if live is None:
            raise HTTPException(503, "Media service is not running")
        presenter = self._resolve_source(display["id"], live)
        return {
            "display": display["id"],
            "label": display.get("label") or display["id"],
            "space_name": await self._space_name(),
            "state": "live" if presenter else "idle",
            "presenter": presenter,
            "presenter_label": self._label_for(presenter) if presenter else "",
            "path": f"{_INGEST_PREFIX}/{presenter}" if presenter else "",
            "code": self._current_code(),
            # What the idle card tells guests to type — plugin-chosen, not
            # derived from the display's own vantage (which is wrong on
            # the server host and on multi-network installs).
            "join_url": await self._join_url(),
        }

    # ──── The WebRTC signalling proxy ────
    # Each check runs before anything reaches the sidecar, in the order
    # written. A publisher (WHIP) is checked by name, then token; a viewer
    # (WHEP) by display key, then the presenter's name; a session's trickle
    # and teardown also by the session id.

    def _check_publisher(self, presenter, token):
        self._validate_presenter(presenter)
        self._require_guest_token(token, presenter)

    def _check_viewer(self, display_id, key, presenter):
        self._guest_display(display_id, key)
        self._validate_presenter(presenter)

    async def _proxy_offer(self, url, request):
        """Forward an SDP offer to the sidecar and return its answer.

        MediaMTX answers with a path-absolute Location (/{path}/whep/{session});
        left as-is the browser would resolve the follow-up PATCH/DELETE
        against the origin root, bypassing this mount. So Location is
        rewritten to sit under the incoming request path: the trickle and
        teardown then come back through here, and through the checks.
        """
        resp = await self.api.proxy_to(url, request, allow_internal=True)
        location = resp.headers.get("location")
        if location:
            secret = location.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            resp.headers["location"] = f"{request.url.path.rstrip('/')}/{secret}"
        return resp

    async def _whip_session(self, presenter, secret, request, token):
        self._check_publisher(presenter, token)
        self._validate_secret(secret)
        return await self.api.proxy_to(
            self._whip_url(presenter, secret), request, allow_internal=True
        )

    async def _whep_session(self, display_id, presenter, secret, request, key):
        self._check_viewer(display_id, key, presenter)
        self._validate_secret(secret)
        return await self.api.proxy_to(
            self._whep_url(presenter, secret), request, allow_internal=True
        )

    def _whep_url(self, presenter, secret=None):
        """Build the localhost sidecar WHEP URL, with read creds in the userinfo.

        httpx turns the userinfo into a Basic ``Authorization`` header, which is
        how the sidecar's ``openavc`` (read/playback) user is authenticated.
        """
        cred = f"{_SIDECAR_USER}:{self._auth_pass}@" if self._auth_pass else ""
        base = f"http://{cred}{_WEBRTC_HOST}:{_WEBRTC_PORT}/{_INGEST_PREFIX}/{presenter}/whep"
        return f"{base}/{secret}" if secret else base

    @staticmethod
    def _whip_url(presenter, secret=None):
        """Build the localhost sidecar WHIP (publish) URL — no credentials.

        The proxied request originates on this host, and the sidecar's
        localhost ``any`` user carries publish (and only publish).
        """
        base = f"http://{_WEBRTC_HOST}:{_WEBRTC_PORT}/{_INGEST_PREFIX}/{presenter}/whip"
        return f"{base}/{secret}" if secret else base

    def _require_guest_token(self, token, presenter):
        """401 unless ``token`` was minted for exactly this presenter's ingest."""
        if not token or not self.api.verify_guest_token(token, f"whip:{presenter}"):
            raise HTTPException(
                401, "Invalid or expired session. Enter the join code again."
            )

    @staticmethod
    def _slugify_presenter(name):
        """Path-safe ingest name from a typed display name ('' when nothing
        survives). Distinct from _slugify: no fallback value — an empty name
        is rejected, not defaulted."""
        return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")

    # ──── Ext router (authed, mounted at /api/plugins/present/ext/) ────

    def _build_ext_router(self):
        router = APIRouter()

        @router.get("/status")
        async def status():
            return await self._status()

        @router.get("/displays")
        async def list_displays():
            return {"displays": [self._display_view(d) for d in self._displays]}

        @router.get("/outputs")
        async def list_outputs():
            """This server's video outputs, for the local-output picker."""
            return await self._host_outputs()

        @router.post("/displays")
        async def add_display(data: DisplayIn):
            return await self._add_display(data)

        @router.put("/displays/{display_id}")
        async def edit_display(display_id: str, data: DisplayIn):
            return await self._edit_display(display_id, data)

        @router.delete("/displays/{display_id}")
        async def delete_display(display_id: str):
            return await self._delete_display(display_id)

        @router.post("/displays/{display_id}/regenerate_key")
        async def regenerate_key(display_id: str):
            return await self._regenerate_key(display_id)

        @router.post("/displays/{display_id}/regenerate_stream_key")
        async def regenerate_stream_key(display_id: str):
            return await self._regenerate_stream_key(display_id)

        @router.post("/displays/{display_id}/route")
        async def route_display(display_id: str, data: RouteIn):
            return await self._route_display(display_id, data.source)

        return router

    # The plugin page's operations. Each returns what its route answers, or
    # raises the HTTPException the page shows; the record rules are
    # display_records', what a change does to the running system is here.

    async def _status(self):
        presenters = self._presenters_list(self._presence)
        return {
            "running": bool(self._supervisor and self._supervisor.running),
            "mediamtx_version": _MEDIAMTX_VERSION,
            "space_name": await self._space_name(),
            "code": self._current_code(),
            "join_url": await self._join_url(),
            "presenters": presenters,
            "active_presenters": len(presenters),
            "sources": self._sources_options(),
            "display_ids": [d["id"] for d in self._displays],
        }

    async def _host_outputs(self):
        if self._kiosk is None:
            return {
                "supported": False,
                "reason": "The plugin is not running.",
                "outputs": [],
            }
        in_use = {
            d["local_output"]: d["id"]
            for d in self._displays
            if d.get("local_output")
        }
        return await self._kiosk.describe_outputs(in_use=in_use)

    def _display_or_404(self, display_id):
        display = self._find_display(display_id)
        if not display:
            raise HTTPException(404, display_records.no_such_display(display_id))
        return display

    async def _add_display(self, data):
        display = display_records.new_record(self._displays, data)
        self._displays.append(display)
        self._routing[display["id"]] = _AUTO
        await self._persist_displays()
        await self._publish_all()
        if display["kind"] == "stream":
            await self._start_output(display)
        await self._sync_kiosks()
        return self._display_view(display)

    async def _edit_display(self, display_id, data):
        display = self._display_or_404(display_id)
        kind, new_id = display_records.apply_edit(self._displays, display, data)
        if new_id != display_id:
            await self._move_display(display, new_id)
        await self._persist_displays()
        await self._publish_all()
        await self._reconcile_output(display, kind)
        await self._sync_kiosks()
        return self._display_view(display)

    async def _move_display(self, display, new_id):
        """A rename: the id is baked into the Display URL, the stream URLs and
        the state keys, so the routing moves, the old keys clear, and the old
        output and kiosk profile go. (Any open Display page for the old id
        stops at its next poll; a decoder on the old stream URL must be
        repointed.)"""
        old_id = display["id"]
        routed = self._routing.get(old_id, _AUTO)
        await self._stop_output(old_id)
        await self._clear_display_state(old_id)
        display["id"] = new_id
        self._routing[new_id] = routed
        kiosk.remove_profile(self.api.data_dir, old_id)

    async def _reconcile_output(self, display, kind):
        """A stream display has a running output; any other kind has none."""
        if kind == "stream":
            if display["id"] not in self._controllers:
                await self._start_output(display)
        else:
            await self._stop_output(display["id"])

    async def _delete_display(self, display_id):
        display = self._display_or_404(display_id)
        self._displays.remove(display)
        await self._stop_output(display_id)
        await self._persist_displays()
        await self._clear_display_state(display_id)
        await self._publish_all()
        await self._sync_kiosks()
        kiosk.remove_profile(self.api.data_dir, display_id)
        return {"status": "deleted", "display_id": display_id}

    async def _regenerate_key(self, display_id):
        """Invalidate every existing Display URL for this display; open
        Display pages get a 401 on their next poll and show the "link isn't
        valid" card. A local window relaunches on the new URL (its old one
        just died with the key)."""
        display = self._display_or_404(display_id)
        display["display_key"] = display_records.new_display_key()
        await self._persist_displays()
        await self._sync_kiosks()
        return self._display_view(display)

    async def _regenerate_stream_key(self, display_id):
        """New key = new output path: every decoder holding the old URL loses
        the stream and must be given the new one."""
        display = self._display_or_404(display_id)
        if display.get("kind") != "stream":
            raise HTTPException(422, "Only stream displays have a stream key.")
        display["stream_key"] = display_records.new_stream_key()
        await self._persist_displays()
        await self._restart_output(display)
        return self._display_view(display)

    async def _route_display(self, display_id, source):
        try:
            await self._route(display_id, source)
        except ValueError as e:
            raise HTTPException(404 if self._find_display(display_id) is None else 422, str(e))
        return self._display_view(self._find_display(display_id))

    def _display_view(self, display):
        """What the plugin page shows for a display (presentClient.ts reads it)."""
        display_id = display["id"]
        showing = self._resolve_source(display_id, set(self._presence))
        view = {
            "id": display_id,
            "label": display.get("label") or display_id,
            "kind": display.get("kind", "browser"),
            "display_key": display.get("display_key", ""),
            "display_path": self._display_path(display),
            "source": self._routing.get(display_id, _AUTO),
            "showing": showing,
            "output_state": "live" if showing else "idle",
        }
        local_output = display.get("local_output") or ""
        if view["kind"] == "browser":
            view["local_output"] = local_output
        if local_output:
            view.update(self._local_window_view(display_id, local_output))
        if view["kind"] == "stream":
            view.update(self._stream_view(display))
        return view

    def _local_window_view(self, display_id, local_output):
        """View-only, like encoder_state: the IDE's status chip reads these;
        automation triggers on output_state/showing as usual."""
        if self._kiosk is None:
            return {
                "local_state": "stopped",
                "local_output_name": "",
                "local_output_connected": False,
            }
        return {
            "local_state": self._kiosk.state_for(display_id),
            "local_output_name": self._kiosk.output_name(local_output),
            "local_output_connected": self._kiosk.output_connected(local_output),
        }

    def _stream_view(self, display):
        """The IDE composes the copyable URLs from the host it reached the
        server on plus these; the path embeds the stream key."""
        controller = self._controllers.get(display["id"])
        if controller is not None:
            encoder_state = controller.state
        else:
            # Running plugin but no controller = the output could not be
            # started (missing font); before start() it is simply off.
            encoder_state = "error" if self._supervisor else "stopped"
        return {
            "stream_path": self._stream_path(display),
            "rtsp_port": _RTSP_PORT,
            "srt_port": _SRT_PORT,
            "encoder_state": encoder_state,
        }

    @staticmethod
    def _validate_presenter(presenter):
        if not _PRESENTER_RE.fullmatch(presenter or ""):
            raise HTTPException(
                422,
                "Presenter name may contain only letters, numbers, dots, hyphens, and underscores.",
            )
        if presenter == _AUTO:
            raise HTTPException(422, "'auto' is a reserved name.")

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

    async def _wait_until_ready(self):
        deadline = time.monotonic() + _READY_TIMEOUT
        while time.monotonic() < deadline:
            if await self._api_get("/v3/paths/list") is not None:
                return True
            await asyncio.sleep(0.3)
        return False

    # ──── Config + binaries ────

    def _render_config(self):
        # The sidecar's pion stack enumerates interface IPs once at process
        # start and advertises them as WebRTC host candidates for its whole
        # life; after a host IP change (DHCP renewal, bench-to-rack move,
        # WiFi/Ethernet swap) browsers aim their ICE checks at dead addresses
        # and connects wedge. Advertising the join host keeps a reachable
        # candidate in every offer; the presence poll rewrites this file and
        # bounces the sidecar when that host changes.
        host = self._join_host()
        log = self.api.log if self.api is not None else None
        advertised = ", ".join(
            f"'{ip}'" for ip in self._join_address.advertised_ips(host, log)
        )
        self._rendered_join_host = host
        # MediaMTX 1.18.x uses true/false booleans (not yes/no). The password is
        # hex (token_hex) and single-quoted, so it is always a safe YAML scalar.
        return (
            "# Generated by the OpenAVC Present plugin. Rewritten on every\n"
            "# plugin start; manual edits are lost.\n"
            "logLevel: info\n"
            "logDestinations: [stdout]\n"
            "\n"
            "api: true\n"
            f"apiAddress: {_API_HOST}:{_API_PORT}\n"
            "\n"
            # RTSP and SRT face the LAN so hardware decoders can pull the
            # stream-display outputs; what they may pull is limited to out/*
            # by the anonymous user below. RTSP is TCP-only: one predictable
            # port for the network guide instead of a UDP RTP port range.
            "rtsp: true\n"
            f"rtspAddress: :{_RTSP_PORT}\n"
            "rtspTransports: [tcp]\n"
            'rtspEncryption: "no"\n'
            "srt: true\n"
            f"srtAddress: :{_SRT_PORT}\n"
            "rtmp: false\n"
            "hls: false\n"
            "metrics: false\n"
            "pprof: false\n"
            "playback: false\n"
            "\n"
            "webrtc: true\n"
            f"webrtcAddress: {_WEBRTC_HOST}:{_WEBRTC_PORT}\n"
            f"webrtcLocalUDPAddress: :{_WEBRTC_UDP_PORT}\n"
            f"webrtcAdditionalHosts: [{advertised}]\n"
            "\n"
            # MediaMTX picks the FIRST entry whose credentials and source IP
            # match, then enforces that entry's permissions — there is no
            # fall-through, so entry order is load-bearing (verified against
            # the real binary).
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
            # Localhost publish lets a WHIP sender on this host and the
            # stream-display encoders feed the sidecar with no credentials —
            # signaling and RTSP-from-loopback both resolve to this entry.
            # Guest publishing from the LAN arrives through the plugin's own
            # proxied routes.
            "  - action: publish\n"
            # Loopback read of the outputs keeps rtsp://localhost:8554/out/...
            # working on the server host itself (bench checks, kiosk debug).
            "  - action: read\n"
            f"    path: ~^{_OUTPUT_PREFIX}/\n"
            # Decoders: anonymous LAN read of the stream-display outputs ONLY.
            # The per-display stream key embedded in each out/ path is the
            # secret (cheap decoders take a URL, not credentials); ingests and
            # everything else stay unreadable without the internal user.
            "- user: any\n"
            "  pass:\n"
            "  ips: []\n"
            "  permissions:\n"
            "  - action: read\n"
            f"    path: ~^{_OUTPUT_PREFIX}/\n"
            "\n"
            # Ingest paths (in/<presenter>) come and go with publishers and
            # can't be pre-registered per name, so accept any path. Who may
            # publish/read is still governed by the auth users above.
            "paths:\n"
            "  all_others:\n"
        )

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
