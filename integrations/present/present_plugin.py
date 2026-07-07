"""Present plugin for OpenAVC — wireless BYOD presentation.

A guest shares a laptop screen from the browser (WebRTC/WHIP, no install) and
it appears on the room display. The plugin bundles MediaMTX as a sidecar that
ingests each presenter's screen and republishes it as browser-playable WebRTC
(WHEP). A room's display runs the plugin's standalone Display page: an idle
"connect" card (room name, server address, join code) that cuts to the live
presenter and back, driven by a 2-second status poll.

Because a room display has no OpenAVC login — it may be a bare browser on a
mini PC, a stick PC, or a TV — the Display page and its API ride the plugin's
guest routes (``/api/plugins/present/guest/*``), gated by a persistent
per-room display key carried in the Display URL. Rooms and their keys are
managed from the Programmer IDE through the authed ``/ext`` routes.

This module owns the sidecar lifecycle, the rooms model, presence polling
(state keys + events for room automation), and both HTTP routers.
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

_PLUGIN_DIR = Path(__file__).resolve().parent

# MediaMTX listeners. Everything is on localhost except the WebRTC media UDP
# port, which browsers connect to directly for low-latency LAN media. All
# ports are distinct from the Video Panel plugin's (8889/8189/9997/8556) so
# both plugins can run side by side.
_API_HOST = "127.0.0.1"
_API_PORT = 9998
_WEBRTC_HOST = "127.0.0.1"
_WEBRTC_PORT = 8890
_WEBRTC_UDP_PORT = 8190
_READY_TIMEOUT = 10.0
# Presence poll cadence. Also the ceiling on how stale the Display page's
# idle/live decision can be (it polls its status route at the same rate).
_POLL_SECONDS = 2.0

_MEDIAMTX_VERSION = "1.18.2"
_SIDECAR_USER = "openavc"

# Join code shown on the idle card. Rotates when a session ends (live -> idle)
# and on a timer while idle, so a code seen on a display is always fresh.
# Verification of the code happens with the guest Connect flow; until then it
# is display-only.
_CODE_LENGTH = 4
_CODE_ROTATE_SECONDS = 300.0

# Room ids become MediaMTX path prefixes, state-key segments, and URL path
# segments, so keep them to a portable, URL-safe character set.
_ROOM_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
# Presenter names arrive as the second segment of an ingest path
# (<room>/<presenter>), chosen by the publisher; constrain before they're
# spliced into proxied sidecar URLs.
_PRESENTER_RE = re.compile(r"[A-Za-z0-9._-]+")
# WHEP session secrets are MediaMTX-minted UUIDs; constrain to a URL-safe set
# before they're spliced into the proxied sidecar URL.
_SECRET_RE = re.compile(r"[A-Za-z0-9-]+")

# Room ids that would collide with the plugin's own flat state keys
# (plugin.present.rooms) or read confusingly in URLs.
_RESERVED_ROOM_IDS = {"rooms", "status", "display", "whep"}

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
  <p>The link may be incomplete, or the room's display key may have been
  regenerated. Open the Present section in the OpenAVC Programmer and copy
  the room's display link again.</p>
</div></body></html>
"""


class RoomIn(BaseModel):
    """Add/edit payload for a room. The IDE Present form posts this."""

    label: str
    room_id: str | None = None


class PresentPlugin:

    PLUGIN_INFO = {
        "id": "present",
        "name": "Present",
        "version": "0.1.0",
        "author": "OpenAVC",
        "description": "Wireless presentation: share a laptop screen from the browser to the room display.",
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
        ],
        "usage": (
            "Create a room in the Present section of the Programmer, then open "
            "the room's display link in a browser on the device driving the "
            "room display (full screen). The display shows a connect card with "
            "the room's join code, and switches to the presenter's screen when "
            "someone shares."
        ),
    }

    def __init__(self):
        self.api = None
        self._supervisor = None
        self._mediamtx_bin = None
        self._auth_pass = ""
        self._config_path = None
        self._rooms = []  # configured room dicts (the source of truth)
        # Runtime, per room id:
        self._codes = {}  # room_id -> (code, rotated_at monotonic)
        self._presence = {}  # room_id -> {presenter_name: since_epoch}

    # ──── Lifecycle ────

    async def start(self, api):
        self.api = api

        self._mediamtx_bin = self._resolve_dep(self._binary_name("mediamtx"))
        if self._mediamtx_bin is None:
            msg = (
                "MediaMTX binary not found in plugin_repo/.deps/. Native "
                "dependencies install when the plugin is installed from the "
                "community repository; reinstall the Present plugin."
            )
            await self.api.state_set("running", False)
            await self.api.state_set("error", msg)
            raise RuntimeError(msg)
        self._ensure_executable(self._mediamtx_bin)

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
            await self._load_rooms()
            await self._publish_rooms()
            self.api.create_periodic_task(
                self._poll, interval_seconds=_POLL_SECONDS, name="presence_poll"
            )
            self.api.log(
                f"Present started: MediaMTX {_MEDIAMTX_VERSION} on "
                f"{_API_HOST}:{_API_PORT}, {len(self._rooms)} room(s)"
            )
        except Exception:
            # Don't leave an orphaned sidecar if start() fails partway.
            await self._supervisor.stop()
            self._supervisor = None
            raise

    async def stop(self):
        # State keys, subscriptions, and managed tasks are cleaned up by the
        # platform; we only need to stop the external process.
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

    # ──── Rooms ────

    async def _load_rooms(self):
        configured = self.api.config.get("rooms", [])
        self._rooms = []
        if not isinstance(configured, list):
            return
        changed = False
        for room in configured:
            if not isinstance(room, dict):
                continue
            room_id = (room.get("id") or "").strip()
            if not room_id or not _ROOM_ID_RE.fullmatch(room_id):
                continue
            # A room saved without a display key (hand-edited project file,
            # older plugin version) gets one now so its Display URL works.
            if not room.get("display_key"):
                room["display_key"] = self._new_display_key()
                changed = True
            self._rooms.append(room)
        if changed:
            await self._persist_rooms()
        for room in self._rooms:
            self._rotate_code(room["id"])
            self._presence[room["id"]] = {}

    async def _persist_rooms(self):
        """Write the current room list back to the project file via the platform."""
        cfg = self.api.config  # a copy; safe to mutate
        cfg["rooms"] = self._rooms
        await self.api.save_config(cfg)

    def _find_room(self, room_id):
        for r in self._rooms:
            if r.get("id") == room_id:
                return r
        return None

    def _unique_room_id(self, label):
        base = self._slugify(label)
        rid = base
        n = 2
        while self._find_room(rid) or rid in _RESERVED_ROOM_IDS:
            rid = f"{base}_{n}"
            n += 1
        return rid

    @staticmethod
    def _slugify(label):
        slug = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")
        return slug or "room"

    @staticmethod
    def _new_display_key():
        return secrets.token_urlsafe(24)

    def _rotate_code(self, room_id):
        code = "".join(secrets.choice("0123456789") for _ in range(_CODE_LENGTH))
        self._codes[room_id] = (code, time.monotonic())
        return code

    def _current_code(self, room_id):
        entry = self._codes.get(room_id)
        return entry[0] if entry else ""

    def _display_path(self, room):
        """Site-relative Display URL for a room; the IDE prepends the origin."""
        return (
            f"/api/plugins/present/guest/display/{room['id']}"
            f"?key={room.get('display_key', '')}"
        )

    # ──── Presence (sidecar paths -> state keys + events) ────

    async def _scan_presenters(self):
        """Live presenters per room from the sidecar's path list.

        Ingest paths are named ``<room>/<presenter>`` by the publisher; a path
        counts once its stream is available. Returns ``None`` when the sidecar
        API is unreachable, else ``{room_id: {presenter, ...}}``.
        """
        data = await self._api_get("/v3/paths/list")
        if data is None:
            return None
        live = {}
        for item in data.get("items", []):
            name = item.get("name") or ""
            room_id, sep, presenter = name.partition("/")
            if not sep or not presenter or self._find_room(room_id) is None:
                continue
            if not _PRESENTER_RE.fullmatch(presenter):
                continue
            # `ready` is deprecated in MediaMTX 1.18.x in favour of `available`.
            if not bool(item.get("available", item.get("ready", False))):
                continue
            live.setdefault(room_id, set()).add(presenter)
        return live

    async def _poll(self):
        live = await self._scan_presenters()
        if live is None:
            await self.api.state_set("running", False)
            return
        await self.api.state_set("running", True)
        now = int(time.time())
        for room in self._rooms:
            room_id = room["id"]
            current = live.get(room_id, set())
            presence = self._presence.setdefault(room_id, {})
            was_live = bool(presence)

            for name in sorted(current - set(presence)):
                presence[name] = now
                await self.api.event_emit(
                    "presenter_joined", {"room": room_id, "name": name}
                )
            for name in sorted(set(presence) - current):
                del presence[name]
                await self.api.event_emit(
                    "presenter_left", {"room": room_id, "name": name}
                )

            is_live = bool(presence)
            code, rotated_at = self._codes.get(room_id, ("", 0.0))
            if was_live and not is_live:
                # Session ended: a code seen on the display during the meeting
                # must not open the next one.
                code = self._rotate_code(room_id)
            elif not is_live and time.monotonic() - rotated_at > _CODE_ROTATE_SECONDS:
                code = self._rotate_code(room_id)

            presenters = self._presenters_list(presence)
            await self.api.state_set(f"{room_id}.presenters", json.dumps(presenters))
            await self.api.state_set(f"{room_id}.active_presenters", len(presenters))
            await self.api.state_set(f"{room_id}.output_state", "live" if is_live else "idle")
            await self.api.state_set(f"{room_id}.code", code)

    @staticmethod
    def _presenters_list(presence):
        return [
            {"name": n, "since": s}
            for n, s in sorted(presence.items(), key=lambda kv: (kv[1], kv[0]))
        ]

    def _pick_presenter(self, room_id, names):
        """The presenter the Display shows: earliest joined, per presence."""
        if not names:
            return ""
        presence = self._presence.get(room_id, {})
        return min(sorted(names), key=lambda n: presence.get(n, float("inf")))

    async def _publish_rooms(self):
        """(Re)publish the room list and every room's current keys.

        Derives from the runtime presence bookkeeping, so re-publishing after
        a room CRUD action never stomps a live room back to idle (which would
        fire a spurious presentation-off trigger).
        """
        await self.api.state_set(
            "rooms",
            json.dumps([{"id": r["id"], "label": r.get("label") or r["id"]} for r in self._rooms]),
        )
        for room in self._rooms:
            room_id = room["id"]
            presenters = self._presenters_list(self._presence.get(room_id, {}))
            await self.api.state_set(f"{room_id}.code", self._current_code(room_id))
            await self.api.state_set(f"{room_id}.presenters", json.dumps(presenters))
            await self.api.state_set(f"{room_id}.active_presenters", len(presenters))
            await self.api.state_set(f"{room_id}.output_state", "live" if presenters else "idle")

    async def _clear_room_state(self, room_id):
        for suffix in ("code", "presenters", "active_presenters", "output_state"):
            await self.api.state_set(f"{room_id}.{suffix}", None)
        self._codes.pop(room_id, None)
        self._presence.pop(room_id, None)

    # ──── Guest router (mounted at /api/plugins/present/guest/) ────

    def _guest_room(self, room_id, key):
        """Resolve a room for a guest call, or raise 401.

        A missing room and a bad key both return 401 — guest callers learn
        nothing about which room ids exist, and every failure feeds the
        platform rate limiter's brute-force accounting.
        """
        room = None
        if _ROOM_ID_RE.fullmatch(room_id or ""):
            room = self._find_room(room_id)
        supplied = (key or "").strip()
        expected = (room or {}).get("display_key") or ""
        if room is None or not supplied or not secrets.compare_digest(supplied, expected):
            raise HTTPException(401, "Invalid or missing display key")
        return room

    def _build_guest_router(self):
        guest = APIRouter()

        @guest.get("/display/{room_id}")
        async def display_page(room_id: str, key: str = ""):
            try:
                self._guest_room(room_id, key)
            except HTTPException:
                # Friendly page for a human at the display, still a 401 so
                # bad-key guessing throttles like any other guest failure.
                return HTMLResponse(_DISPLAY_ERROR_HTML, status_code=401)
            html = (_PLUGIN_DIR / "panel" / "display.html").read_text(encoding="utf-8")
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})

        @guest.get("/rooms/{room_id}/status")
        async def room_status(room_id: str, key: str = ""):
            room = self._guest_room(room_id, key)
            live = await self._scan_presenters()
            if live is None:
                raise HTTPException(503, "Media service is not running")
            names = live.get(room["id"], set())
            presenter = self._pick_presenter(room["id"], names)
            return {
                "room": room["id"],
                "label": room.get("label") or room["id"],
                "state": "live" if presenter else "idle",
                "presenter": presenter,
                "path": f"{room['id']}/{presenter}" if presenter else "",
                "code": self._current_code(room["id"]),
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
        @guest.post("/whep/{room_id}/{presenter}")
        async def whep_offer(room_id: str, presenter: str, request: Request, key: str = ""):
            self._guest_room(room_id, key)
            self._validate_presenter(presenter)
            resp = await self.api.proxy_to(
                self._whep_url(room_id, presenter), request, allow_internal=True
            )
            location = resp.headers.get("location")
            if location:
                secret = location.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
                resp.headers["location"] = f"{request.url.path.rstrip('/')}/{secret}"
            return resp

        @guest.patch("/whep/{room_id}/{presenter}/{secret}")
        async def whep_trickle(
            room_id: str, presenter: str, secret: str, request: Request, key: str = ""
        ):
            self._guest_room(room_id, key)
            self._validate_presenter(presenter)
            self._validate_secret(secret)
            return await self.api.proxy_to(
                self._whep_url(room_id, presenter, secret), request, allow_internal=True
            )

        @guest.delete("/whep/{room_id}/{presenter}/{secret}")
        async def whep_teardown(
            room_id: str, presenter: str, secret: str, request: Request, key: str = ""
        ):
            self._guest_room(room_id, key)
            self._validate_presenter(presenter)
            self._validate_secret(secret)
            return await self.api.proxy_to(
                self._whep_url(room_id, presenter, secret), request, allow_internal=True
            )

        return guest

    def _whep_url(self, room_id, presenter, secret=None):
        """Build the localhost sidecar WHEP URL, with read creds in the userinfo.

        httpx turns the userinfo into a Basic ``Authorization`` header, which is
        how the sidecar's ``openavc`` (read/playback) user is authenticated.
        """
        cred = f"{_SIDECAR_USER}:{self._auth_pass}@" if self._auth_pass else ""
        base = f"http://{cred}{_WEBRTC_HOST}:{_WEBRTC_PORT}/{room_id}/{presenter}/whep"
        return f"{base}/{secret}" if secret else base

    # ──── Ext router (authed, mounted at /api/plugins/present/ext/) ────

    def _build_ext_router(self):
        router = APIRouter()

        @router.get("/status")
        async def status():
            return {
                "running": bool(self._supervisor and self._supervisor.running),
                "mediamtx_version": _MEDIAMTX_VERSION,
                "room_ids": [r["id"] for r in self._rooms],
            }

        @router.get("/rooms")
        async def list_rooms():
            live = await self._scan_presenters() or {}
            return [self._room_view(r, live) for r in self._rooms]

        @router.post("/rooms")
        async def add_room(data: RoomIn):
            label = (data.label or "").strip()
            if not label:
                raise HTTPException(422, "Room name is required.")
            room_id = (data.room_id or "").strip() or self._unique_room_id(label)
            self._validate_room_id(room_id)
            if self._find_room(room_id):
                raise HTTPException(409, f"A room with id '{room_id}' already exists.")
            room = {"id": room_id, "label": label, "display_key": self._new_display_key()}
            self._rooms.append(room)
            self._rotate_code(room_id)
            self._presence[room_id] = {}
            await self._persist_rooms()
            await self._publish_rooms()
            return self._room_view(room, {})

        @router.put("/rooms/{room_id}")
        async def edit_room(room_id: str, data: RoomIn):
            room = self._find_room(room_id)
            if not room:
                raise HTTPException(404, f"No room with id '{room_id}'.")
            label = (data.label or "").strip()
            if not label:
                raise HTTPException(422, "Room name is required.")
            new_id = (data.room_id or "").strip() or room_id
            self._validate_room_id(new_id)
            if new_id != room_id and self._find_room(new_id):
                raise HTTPException(409, f"A room with id '{new_id}' already exists.")
            room["label"] = label
            if new_id != room_id:
                # The id is baked into the Display URL and the state keys, so
                # a rename moves the runtime bookkeeping and clears old keys.
                # (Any open Display page for the old id stops at its next poll.)
                await self._clear_room_state(room_id)
                room["id"] = new_id
                self._rotate_code(new_id)
                self._presence[new_id] = {}
            await self._persist_rooms()
            await self._publish_rooms()
            live = await self._scan_presenters() or {}
            return self._room_view(room, live)

        @router.delete("/rooms/{room_id}")
        async def delete_room(room_id: str):
            room = self._find_room(room_id)
            if not room:
                raise HTTPException(404, f"No room with id '{room_id}'.")
            self._rooms.remove(room)
            await self._persist_rooms()
            await self._clear_room_state(room_id)
            await self._publish_rooms()
            return {"ok": True, "room_id": room_id}

        @router.post("/rooms/{room_id}/regenerate_key")
        async def regenerate_key(room_id: str):
            room = self._find_room(room_id)
            if not room:
                raise HTTPException(404, f"No room with id '{room_id}'.")
            # Invalidate every existing Display URL for this room; open
            # Display pages get a 401 on their next poll and show the
            # "link isn't valid" card.
            room["display_key"] = self._new_display_key()
            await self._persist_rooms()
            live = await self._scan_presenters() or {}
            return self._room_view(room, live)

        return router

    def _room_view(self, room, live):
        room_id = room["id"]
        names = live.get(room_id, set())
        return {
            "id": room_id,
            "label": room.get("label") or room_id,
            "display_key": room.get("display_key", ""),
            "display_path": self._display_path(room),
            "code": self._current_code(room_id),
            "output_state": "live" if names else "idle",
            "active_presenters": len(names),
        }

    @staticmethod
    def _validate_room_id(room_id):
        if not _ROOM_ID_RE.fullmatch(room_id or ""):
            raise HTTPException(
                422,
                "Room ID may contain only letters, numbers, hyphens, and underscores.",
            )
        if room_id in _RESERVED_ROOM_IDS:
            raise HTTPException(422, f"'{room_id}' is a reserved name; pick another room ID.")

    @staticmethod
    def _validate_presenter(presenter):
        if not _PRESENTER_RE.fullmatch(presenter or ""):
            raise HTTPException(
                422,
                "Presenter name may contain only letters, numbers, dots, hyphens, and underscores.",
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
            "rtsp: false\n"
            "rtmp: false\n"
            "hls: false\n"
            "srt: false\n"
            "metrics: false\n"
            "pprof: false\n"
            "playback: false\n"
            "\n"
            "webrtc: true\n"
            f"webrtcAddress: {_WEBRTC_HOST}:{_WEBRTC_PORT}\n"
            f"webrtcLocalUDPAddress: :{_WEBRTC_UDP_PORT}\n"
            "webrtcAdditionalHosts: []\n"
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
            # Localhost publish lets a WHIP sender on this host feed an ingest
            # path with no credentials — the signaling port is loopback-only,
            # so nothing off-box can reach it. Guest publishing from the LAN
            # arrives through the plugin's own proxied routes.
            "  - action: publish\n"
            "\n"
            # Ingest paths (<room>/<presenter>) come and go with publishers and
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
