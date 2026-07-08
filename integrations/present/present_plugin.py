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
import socket
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
    import encoders
    import output
except ImportError:  # pragma: no cover - exercised only via package-path import
    from . import encoders, output

_PLUGIN_DIR = Path(__file__).resolve().parent

# MediaMTX listeners. Signaling and the control API are loopback-only; three
# listeners face the LAN: the WebRTC media UDP port (browsers connect to it
# directly for low-latency media) and the RTSP + SRT listeners hardware
# decoders pull stream-display outputs from (read-only, out/* paths, see
# _render_config). All ports are distinct from the Video Panel plugin's
# (8889/8189/9997/8556) so both plugins can run side by side.
_API_HOST = "127.0.0.1"
_API_PORT = 9998
_WEBRTC_HOST = "127.0.0.1"
_WEBRTC_PORT = 8890
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

# The auto-detected join address is re-resolved at most this often.
_IP_CACHE_SECONDS = 60.0

# Presenters publish their screen to in/<presenter>; each stream display's
# encoder publishes to out/<display>-<stream_key>. The prefixes keep the two
# namespaces apart on the sidecar — and the sidecar's anonymous LAN read
# permission is scoped to out/*, so a decoder can never pull an ingest.
_INGEST_PREFIX = "in"
_OUTPUT_PREFIX = "out"

# The display kinds. Browser displays open the Display page; stream displays
# are pulled by hardware decoders from the persistent per-display output.
_DISPLAY_KINDS = ("browser", "stream")

# The routing value that means "follow the active presenter" rather than a
# pinned one. Reserved everywhere a presenter or display name could collide
# with it.
_AUTO = "auto"

# Display ids become state-key segments and URL path segments, so keep them to
# a portable, URL-safe character set.
_DISPLAY_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
# Presenter names arrive as the second segment of an ingest path
# (in/<presenter>), chosen by the publisher; constrain before they're spliced
# into proxied sidecar URLs.
_PRESENTER_RE = re.compile(r"[A-Za-z0-9._-]+")
# WHEP session secrets are MediaMTX-minted UUIDs; constrain to a URL-safe set
# before they're spliced into the proxied sidecar URL.
_SECRET_RE = re.compile(r"[A-Za-z0-9-]+")

# Display ids that would collide with the routing "auto" sentinel, the sidecar
# path namespaces, or the guest URL segments.
_RESERVED_DISPLAY_IDS = {
    _AUTO, "connect", "display", "displays", "status", "whep", "whip", "in", "out",
}

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
        "version": "0.4.0",
        "author": "OpenAVC",
        "description": "Wireless presentation: share a laptop screen from the browser to the space's displays.",
        "category": "integration",
        "license": "MIT",
        "platforms": ["win_x64", "linux_x64", "linux_arm64"],
        "min_openavc_version": "0.23.0",
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
                "id": "ffmpeg",
                "name": "FFmpeg (LGPL build)",
                "version": "7.1",
                "license": "LGPL-2.1",
                "required": True,
                "platforms": {
                    "win_x64": {
                        "type": "zip",
                        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n7.1-latest-win64-lgpl-7.1.zip",
                        "extract": "ffmpeg-n7.1-latest-win64-lgpl-7.1/bin/ffmpeg.exe",
                    },
                    "linux_x64": {
                        "type": "tar.xz",
                        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n7.1-latest-linux64-lgpl-7.1.tar.xz",
                        "extract": "ffmpeg-n7.1-latest-linux64-lgpl-7.1/bin/ffmpeg",
                    },
                    "linux_arm64": {
                        "type": "tar.xz",
                        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n7.1-latest-linuxarm64-lgpl-7.1.tar.xz",
                        "extract": "ffmpeg-n7.1-latest-linuxarm64-lgpl-7.1/bin/ffmpeg",
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
            "enters the code and a name, and shares their screen. Route a "
            "specific presenter to a display from this page, a macro's Route "
            "Display step, or a script. Screen capture in the guest's "
            "browser requires HTTPS (enable it in Settings > Security)."
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
        self._detected_ip = ("", 0.0)  # (auto-detected join address, at monotonic)
        # Stream-display output pipeline:
        self._controllers = {}  # display_id -> output.OutputController
        self._card = None  # output.IdleCard once start() has a data_dir
        self._font = None
        self._encoder_name = None  # detected once, first stream display start
        self._encoder_lock = asyncio.Lock()
        self._card_join = None  # last join line written to the card
        self._card_space = None  # last space name written to the card

    # ──── Lifecycle ────

    async def start(self, api):
        self.api = api

        self._mediamtx_bin = self._resolve_dep(self._binary_name("mediamtx"))
        self._ffmpeg_bin = self._resolve_dep(self._binary_name("ffmpeg"))
        if self._mediamtx_bin is None or self._ffmpeg_bin is None:
            missing = "MediaMTX" if self._mediamtx_bin is None else "FFmpeg"
            msg = (
                f"{missing} binary not found in plugin_repo/.deps/. Native "
                "dependencies install when the plugin is installed from the "
                "community repository; reinstall the Present plugin."
            )
            await self.api.state_set("running", False)
            await self.api.state_set("error", msg)
            raise RuntimeError(msg)
        self._ensure_executable(self._mediamtx_bin)
        self._ensure_executable(self._ffmpeg_bin)

        self._auth_pass = self._load_or_create_auth()
        self._config_path = self.api.data_dir / "mediamtx.yml"
        self._config_path.write_text(self._render_config(), encoding="utf-8")

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
            for display in self._displays:
                if display.get("kind") == "stream":
                    await self._start_output(display)
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
        except Exception:
            # Don't leave an orphaned sidecar if start() fails partway.
            await self._supervisor.stop()
            self._supervisor = None
            raise

    async def stop(self):
        # State keys, subscriptions, and managed tasks are cleaned up by the
        # platform; we only need to stop the external processes — output
        # encoders first (they publish into the sidecar), then the sidecar.
        for display_id in list(self._controllers):
            await self._stop_output(display_id)
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
        join = self._join_url()
        if join != self._card_join:
            self._card.write_join(join)
            self._card_join = join

    def _join_url(self):
        """The address a guest types, exactly as the connect cards show it.

        Scheme-less on purpose: guests type it into the address bar, and the
        platform's HTTP->HTTPS redirect upgrades it when TLS is enabled.
        """
        address = (self.api.config.get("join_address") or "").strip()
        if not address:
            address = self._detect_local_ip()
        port = self._http_port()
        host = address if port == 80 else f"{address}:{port}"
        return f"{host}/{_GUEST_ALIAS}"

    def _detect_local_ip(self):
        """Best-guess LAN address via the UDP-connect trick (no packets sent).

        Same method the platform's mDNS advertiser uses. Multi-NIC/VLAN
        installs where this guesses wrong set the join_address config field.
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

    @staticmethod
    def _http_port():
        # The plugin runs in the server process, so the platform config is
        # importable; the fallback keeps unit tests (no server package on the
        # path) and any config surprise on the default port.
        try:
            from server.system_config import get_system_config

            return int(get_system_config().get("network", "http_port"))
        except Exception:
            return 8080

    # ──── Displays ────

    async def _load_displays(self):
        configured = self.api.config.get("displays", [])
        self._displays = []
        if not isinstance(configured, list):
            return
        changed = False
        for display in configured:
            if not isinstance(display, dict):
                continue
            display_id = (display.get("id") or "").strip()
            if not display_id or not _DISPLAY_ID_RE.fullmatch(display_id):
                continue
            if display_id in _RESERVED_DISPLAY_IDS:
                continue
            # A display saved without a key (hand-edited project file, older
            # plugin version) gets one now so its Display URL works.
            if not display.get("display_key"):
                display["display_key"] = self._new_display_key()
                changed = True
            if display.get("kind") not in _DISPLAY_KINDS:
                display["kind"] = "browser"
                changed = True
            if display["kind"] == "stream" and not display.get("stream_key"):
                display["stream_key"] = self._new_stream_key()
                changed = True
            self._displays.append(display)
        if changed:
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
        for d in self._displays:
            if d.get("id") == display_id:
                return d
        return None

    def _unique_display_id(self, label):
        base = self._slugify(label)
        did = base
        n = 2
        while self._find_display(did) or did in _RESERVED_DISPLAY_IDS:
            did = f"{base}_{n}"
            n += 1
        return did

    @staticmethod
    def _slugify(label):
        slug = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")
        return slug or "display"

    @staticmethod
    def _new_display_key():
        return secrets.token_urlsafe(24)

    @staticmethod
    def _new_stream_key():
        # Deliberately short: decoder UIs are often typed with a remote.
        # Guessing runs against MediaMTX directly (no platform rate limiter),
        # but 8 URL-safe chars is far beyond LAN brute-force reach.
        return secrets.token_urlsafe(6)

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
            raise ValueError(f"No display with id '{display_id}'.")
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
        parts = key.split(".")
        if len(parts) != 5 or parts[2] != "display" or parts[4] != "source":
            return
        if value is None:
            return
        display_id = parts[3]
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

        Ingest paths are named ``in/<presenter>`` by the publisher; a path
        counts once its stream is available. Returns ``None`` when the sidecar
        API is unreachable, else the set of live presenter names.
        """
        data = await self._api_get("/v3/paths/list")
        if data is None:
            return None
        live = set()
        for item in data.get("items", []):
            name = item.get("name") or ""
            prefix, sep, presenter = name.partition("/")
            if prefix != _INGEST_PREFIX or not sep or not presenter:
                continue
            # "auto" is the routing sentinel, never a valid presenter.
            if presenter == _AUTO or not _PRESENTER_RE.fullmatch(presenter):
                continue
            # `ready` is deprecated in MediaMTX 1.18.x in favour of `available`.
            if not bool(item.get("available", item.get("ready", False))):
                continue
            live.add(presenter)
        return live

    async def _poll(self):
        live = await self._scan_presenters()
        if live is None:
            await self.api.state_set("running", False)
            return
        await self.api.state_set("running", True)
        now = int(time.time())
        was_live = bool(self._presence)

        for name in sorted(live - set(self._presence)):
            self._presence[name] = now
            await self.api.event_emit(
                "presenter_joined", {"name": name, "label": self._label_for(name)}
            )
        for name in sorted(set(self._presence) - live):
            del self._presence[name]
            label = self._labels.pop(name, name)
            await self.api.event_emit("presenter_left", {"name": name, "label": label})

        is_live = bool(self._presence)
        _code, rotated_at = self._code
        if was_live and not is_live:
            # Session ended: a code seen on the displays during the meeting
            # must not open the next one.
            self._rotate_code()
        elif not is_live and time.monotonic() - rotated_at > _CODE_ROTATE_SECONDS:
            self._rotate_code()

        await self._refresh_card()
        await self._publish_all()

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
        if _DISPLAY_ID_RE.fullmatch(display_id or ""):
            display = self._find_display(display_id)
        supplied = (key or "").strip()
        expected = (display or {}).get("display_key") or ""
        if display is None or not supplied or not secrets.compare_digest(supplied, expected):
            raise HTTPException(401, "Invalid or missing display key")
        return display

    def _build_guest_router(self):
        guest = APIRouter()

        # ── Connect page (the presenter's sender surface) ──
        # The root of both guest mounts; via the top-level alias this is the
        # short URL on every connect card (http://<join-address>:<port>/present
        # redirects here). Deliberately open — the join code is the gate, and
        # it is checked at the POST /connect exchange, not the page serve.
        @guest.get("/")
        async def connect_page():
            html = (_PLUGIN_DIR / "panel" / "connect.html").read_text(encoding="utf-8")
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})

        @guest.post("/connect")
        async def connect(data: ConnectIn):
            code = (data.code or "").strip()
            current = self._current_code()
            if not code or not current or not secrets.compare_digest(code, current):
                # 401 feeds the platform's brute-force accounting.
                raise HTTPException(
                    401, "That join code isn't right. Check the code on the display."
                )
            label = " ".join((data.name or "").split())[:_NAME_MAX_CHARS]
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

        # ── WHIP (WebRTC publish) reverse proxy ──
        # The Connect page POSTs its SDP offer here with the guest token from
        # the /connect exchange; we forward to the sidecar's WHIP endpoint for
        # in/<presenter>. Signaling is loopback-only on the sidecar, and its
        # localhost "any" user carries publish (no read), so the proxied
        # publish needs no credential injection. Same Location rewrite as the
        # WHEP proxy below, so trickle/teardown come back through the token
        # check.
        @guest.post("/whip/{presenter}")
        async def whip_publish(presenter: str, request: Request, token: str = ""):
            self._validate_presenter(presenter)
            self._require_guest_token(token, presenter)
            resp = await self.api.proxy_to(
                self._whip_url(presenter), request, allow_internal=True
            )
            location = resp.headers.get("location")
            if location:
                secret = location.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
                resp.headers["location"] = f"{request.url.path.rstrip('/')}/{secret}"
            return resp

        @guest.patch("/whip/{presenter}/{secret}")
        async def whip_trickle(
            presenter: str, secret: str, request: Request, token: str = ""
        ):
            self._validate_presenter(presenter)
            self._require_guest_token(token, presenter)
            self._validate_secret(secret)
            return await self.api.proxy_to(
                self._whip_url(presenter, secret), request, allow_internal=True
            )

        @guest.delete("/whip/{presenter}/{secret}")
        async def whip_teardown(
            presenter: str, secret: str, request: Request, token: str = ""
        ):
            self._validate_presenter(presenter)
            self._require_guest_token(token, presenter)
            self._validate_secret(secret)
            return await self.api.proxy_to(
                self._whip_url(presenter, secret), request, allow_internal=True
            )

        @guest.get("/display/{display_id}")
        async def display_page(display_id: str, key: str = ""):
            try:
                self._guest_display(display_id, key)
            except HTTPException:
                # Friendly page for a human at the display, still a 401 so
                # bad-key guessing throttles like any other guest failure.
                return HTMLResponse(_DISPLAY_ERROR_HTML, status_code=401)
            html = (_PLUGIN_DIR / "panel" / "display.html").read_text(encoding="utf-8")
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})

        @guest.get("/displays/{display_id}/status")
        async def display_status(display_id: str, key: str = ""):
            display = self._guest_display(display_id, key)
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
                "join_url": self._join_url(),
            }

        # ── WHEP (WebRTC playback) reverse proxy ──
        # The Display page's WHEP client POSTs an SDP offer here; we forward it
        # to the sidecar's WebRTC server (localhost, Basic-authed via the URL
        # userinfo httpx applies) and return the SDP answer. MediaMTX answers
        # with a path-absolute Location (/{path}/whep/{session}); left as-is
        # the browser would resolve the follow-up PATCH/DELETE against the
        # origin root, bypassing this mount. So we rewrite Location to sit
        # under the incoming request path — the trickle and teardown then come
        # back through here (and through the key check).
        @guest.post("/whep/{display_id}/{presenter}")
        async def whep_offer(display_id: str, presenter: str, request: Request, key: str = ""):
            self._guest_display(display_id, key)
            self._validate_presenter(presenter)
            resp = await self.api.proxy_to(
                self._whep_url(presenter), request, allow_internal=True
            )
            location = resp.headers.get("location")
            if location:
                secret = location.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
                resp.headers["location"] = f"{request.url.path.rstrip('/')}/{secret}"
            return resp

        @guest.patch("/whep/{display_id}/{presenter}/{secret}")
        async def whep_trickle(
            display_id: str, presenter: str, secret: str, request: Request, key: str = ""
        ):
            self._guest_display(display_id, key)
            self._validate_presenter(presenter)
            self._validate_secret(secret)
            return await self.api.proxy_to(
                self._whep_url(presenter, secret), request, allow_internal=True
            )

        @guest.delete("/whep/{display_id}/{presenter}/{secret}")
        async def whep_teardown(
            display_id: str, presenter: str, secret: str, request: Request, key: str = ""
        ):
            self._guest_display(display_id, key)
            self._validate_presenter(presenter)
            self._validate_secret(secret)
            return await self.api.proxy_to(
                self._whep_url(presenter, secret), request, allow_internal=True
            )

        return guest

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
            presenters = self._presenters_list(self._presence)
            return {
                "running": bool(self._supervisor and self._supervisor.running),
                "mediamtx_version": _MEDIAMTX_VERSION,
                "space_name": await self._space_name(),
                "code": self._current_code(),
                "join_url": self._join_url(),
                "presenters": presenters,
                "active_presenters": len(presenters),
                "sources": self._sources_options(),
                "display_ids": [d["id"] for d in self._displays],
            }

        @router.get("/displays")
        async def list_displays():
            return [self._display_view(d) for d in self._displays]

        @router.post("/displays")
        async def add_display(data: DisplayIn):
            label = (data.label or "").strip()
            if not label:
                raise HTTPException(422, "Display name is required.")
            kind = self._validate_kind(data.kind)
            display_id = (data.display_id or "").strip() or self._unique_display_id(label)
            self._validate_display_id(display_id)
            if self._find_display(display_id):
                raise HTTPException(409, f"A display with id '{display_id}' already exists.")
            display = {
                "id": display_id,
                "label": label,
                "kind": kind,
                "display_key": self._new_display_key(),
            }
            if kind == "stream":
                display["stream_key"] = self._new_stream_key()
            self._displays.append(display)
            self._routing[display_id] = _AUTO
            await self._persist_displays()
            await self._publish_all()
            if kind == "stream":
                await self._start_output(display)
            return self._display_view(display)

        @router.put("/displays/{display_id}")
        async def edit_display(display_id: str, data: DisplayIn):
            display = self._find_display(display_id)
            if not display:
                raise HTTPException(404, f"No display with id '{display_id}'.")
            label = (data.label or "").strip()
            if not label:
                raise HTTPException(422, "Display name is required.")
            old_kind = display.get("kind", "browser")
            kind = self._validate_kind(data.kind) if data.kind is not None else old_kind
            new_id = (data.display_id or "").strip() or display_id
            self._validate_display_id(new_id)
            if new_id != display_id and self._find_display(new_id):
                raise HTTPException(409, f"A display with id '{new_id}' already exists.")
            display["label"] = label
            display["kind"] = kind
            if kind == "stream" and not display.get("stream_key"):
                display["stream_key"] = self._new_stream_key()
            if new_id != display_id:
                # The id is baked into the Display URL, the stream URLs, and
                # the state keys, so a rename moves the routing and clears old
                # keys. (Any open Display page for the old id stops at its
                # next poll; a decoder on the old stream URL must be repointed.)
                routed = self._routing.get(display_id, _AUTO)
                await self._stop_output(display_id)
                await self._clear_display_state(display_id)
                display["id"] = new_id
                self._routing[new_id] = routed
            await self._persist_displays()
            await self._publish_all()
            # Reconcile the output pipeline with the (possibly new) kind/id.
            if kind == "stream":
                if display["id"] not in self._controllers:
                    await self._start_output(display)
            else:
                await self._stop_output(display["id"])
            return self._display_view(display)

        @router.delete("/displays/{display_id}")
        async def delete_display(display_id: str):
            display = self._find_display(display_id)
            if not display:
                raise HTTPException(404, f"No display with id '{display_id}'.")
            self._displays.remove(display)
            await self._stop_output(display_id)
            await self._persist_displays()
            await self._clear_display_state(display_id)
            await self._publish_all()
            return {"ok": True, "display_id": display_id}

        @router.post("/displays/{display_id}/regenerate_key")
        async def regenerate_key(display_id: str):
            display = self._find_display(display_id)
            if not display:
                raise HTTPException(404, f"No display with id '{display_id}'.")
            # Invalidate every existing Display URL for this display; open
            # Display pages get a 401 on their next poll and show the
            # "link isn't valid" card.
            display["display_key"] = self._new_display_key()
            await self._persist_displays()
            return self._display_view(display)

        @router.post("/displays/{display_id}/regenerate_stream_key")
        async def regenerate_stream_key(display_id: str):
            display = self._find_display(display_id)
            if not display:
                raise HTTPException(404, f"No display with id '{display_id}'.")
            if display.get("kind") != "stream":
                raise HTTPException(422, "Only stream displays have a stream key.")
            # New key = new output path: every decoder holding the old URL
            # loses the stream and must be given the new one.
            display["stream_key"] = self._new_stream_key()
            await self._persist_displays()
            await self._restart_output(display)
            return self._display_view(display)

        @router.post("/displays/{display_id}/route")
        async def route_display(display_id: str, data: RouteIn):
            try:
                await self._route(display_id, data.source)
            except ValueError as e:
                raise HTTPException(404 if self._find_display(display_id) is None else 422, str(e))
            return self._display_view(self._find_display(display_id))

        return router

    def _display_view(self, display):
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
        if view["kind"] == "stream":
            # The IDE composes the copyable URLs from the host it reached the
            # server on plus these; the path embeds the stream key.
            view["stream_path"] = self._stream_path(display)
            view["rtsp_port"] = _RTSP_PORT
            view["srt_port"] = _SRT_PORT
            controller = self._controllers.get(display_id)
            if controller is not None:
                view["encoder_state"] = controller.state
            else:
                # Running plugin but no controller = the output could not be
                # started (missing font); before start() it is simply off.
                view["encoder_state"] = "error" if self._supervisor else "stopped"
        return view

    @staticmethod
    def _validate_display_id(display_id):
        if not _DISPLAY_ID_RE.fullmatch(display_id or ""):
            raise HTTPException(
                422,
                "Display ID may contain only letters, numbers, hyphens, and underscores.",
            )
        if display_id in _RESERVED_DISPLAY_IDS:
            raise HTTPException(422, f"'{display_id}' is a reserved name; pick another display ID.")

    @staticmethod
    def _validate_kind(kind):
        kind = (kind or "browser").strip().lower()
        if kind not in _DISPLAY_KINDS:
            raise HTTPException(422, "Display kind must be 'browser' or 'stream'.")
        return kind

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
            "webrtcAdditionalHosts: []\n"
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
        from server.system_config import PLUGIN_REPO_DIR

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
