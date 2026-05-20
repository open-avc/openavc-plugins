"""Video Panel plugin for OpenAVC.

Shows H.264 / H.265 IP camera streams (and any other RTSP source) on the
touch panel. The plugin bundles MediaMTX as a sidecar that pulls each camera's
RTSP feed and republishes it as browser-playable WebRTC (WHEP). The sidecar
binds to localhost only; the panel reaches it through OpenAVC's own HTTP
server, so camera traffic inherits the platform's authentication.

This module owns the sidecar lifecycle and the plugin's state. Camera
management UI and WHEP playback are layered on in later parts of the plugin.
"""

import asyncio
import json
import os
import re
import secrets
import stat
import sys
import time
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

# Sibling module. The plugin loader execs this file with the plugin directory on
# sys.path (so the flat import resolves) but not as a package, so a relative
# import would fail there. Test/CI code imports the plugin by its package path
# (integrations.video_panel.video_panel_plugin), where the flat name isn't on
# the path but the relative import is. Try both so the plugin loads either way.
# A lazy import inside a method would fail under the loader (the path entry is
# removed after load), so this stays at module top level.
try:
    from sidecar import SidecarSupervisor
except ImportError:  # pragma: no cover - exercised only via package-path import
    from .sidecar import SidecarSupervisor

# MediaMTX listeners. Everything is on localhost except the WebRTC media UDP
# port, which the browser connects to directly for low-latency LAN playback.
_API_HOST = "127.0.0.1"
_API_PORT = 9997
_WEBRTC_HOST = "127.0.0.1"
_WEBRTC_PORT = 8889
_WEBRTC_UDP_PORT = 8189
_READY_TIMEOUT = 10.0
_STATUS_POLL_SECONDS = 5.0

_MEDIAMTX_VERSION = "1.18.2"
_SIDECAR_USER = "openavc"

# ffmpeg-backed operations can hang on an unreachable camera; bound them hard.
_PROBE_TIMEOUT = 15.0
_SNAPSHOT_TIMEOUT = 15.0

# Stream ids become MediaMTX path names and panel-element binding values, so
# keep them to a portable, URL-safe character set.
_STREAM_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


class CameraIn(BaseModel):
    """Add/edit payload for a camera. The IDE Cameras form posts this shape."""

    name: str
    stream_id: str | None = None
    rtsp_url: str
    username: str = ""
    password: str = ""
    codec_hint: str = "auto"      # auto | h264 | h265
    transcode: str = "auto"       # auto | always | never
    hardware_accel: str = "auto"  # auto | none | qsv | nvenc | vaapi | v4l2m2m


class ProbeIn(BaseModel):
    """Probe payload — tests an RTSP URL before it is saved as a camera."""

    rtsp_url: str
    username: str = ""
    password: str = ""


class VideoPanelPlugin:

    PLUGIN_INFO = {
        "id": "video_panel",
        "name": "Video Panel",
        "version": "0.2.0",
        "author": "OpenAVC",
        "description": "Display H.264 and H.265 IP camera streams on the panel.",
        "category": "integration",
        "license": "MIT",
        "platforms": ["win_x64", "linux_x64", "linux_arm64"],
        "min_openavc_version": "0.13.0",
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
    }

    def __init__(self):
        self.api = None
        self._supervisor = None
        self._mediamtx_bin = None
        self._ffmpeg_bin = None
        self._auth_pass = ""
        self._config_path = None
        self._cameras = []  # camera dicts that registered successfully with the sidecar

    # ──── Lifecycle ────

    async def start(self, api):
        self.api = api

        self._mediamtx_bin = self._resolve_dep(self._binary_name("mediamtx"))
        if self._mediamtx_bin is None:
            msg = (
                "MediaMTX binary not found in plugin_repo/.deps/. Native "
                "dependencies install when the plugin is installed from the "
                "community repository; reinstall the Video Panel plugin."
            )
            await self.api.state_set("running", False)
            await self.api.state_set("error", msg)
            raise RuntimeError(msg)

        # ffmpeg is bundled for HEVC transcode/snapshots used by later parts of
        # the plugin; resolve it now so the path is ready, but it's not invoked
        # by the passthrough path.
        self._ffmpeg_bin = self._resolve_dep(self._binary_name("ffmpeg"))
        self._ensure_executable(self._mediamtx_bin)
        if self._ffmpeg_bin is not None:
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
            self.api.register_router(self._build_router())
            await self._load_cameras()
            await self._publish_streams()
            self.api.create_periodic_task(
                self._poll_statuses, interval_seconds=_STATUS_POLL_SECONDS, name="status_poll"
            )
            self.api.log(
                f"Video Panel started: MediaMTX {_MEDIAMTX_VERSION} on "
                f"{_API_HOST}:{_API_PORT}, {len(self._cameras)} camera(s)"
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

    # ──── Cameras / streams ────

    async def _load_cameras(self):
        # self._cameras is the authored/configured list (the source of truth the
        # CRUD endpoints edit and persist). MediaMTX registration is attempted for
        # each but a sidecar rejection doesn't drop the camera from the list —
        # it's still configured, just not currently streamable, which the status
        # poller reflects.
        cams = self.api.config.get("cameras", [])
        self._cameras = []
        if not isinstance(cams, list):
            return
        for cam in cams:
            if not isinstance(cam, dict):
                continue
            stream_id = (cam.get("stream_id") or "").strip()
            if not stream_id:
                continue
            self._cameras.append(cam)
            await self._sync_path_add(cam)

    async def _sync_path_add(self, cam):
        """Register (or re-register) a camera's path with the running sidecar."""
        stream_id = cam["stream_id"]
        source = self._camera_source_url(cam)
        if not source:
            self.api.log(
                f"camera '{stream_id}' has no usable rtsp_url; not registered with sidecar",
                "warning",
            )
            return False
        ok = await self._api_post(
            f"/v3/config/paths/add/{stream_id}",
            {"source": source, "sourceOnDemand": True},
        )
        if not ok:
            self.api.log(
                f"sidecar rejected camera '{stream_id}'; check the RTSP URL",
                "warning",
            )
        return ok

    async def _sync_path_delete(self, stream_id):
        """Remove a camera's path from the running sidecar (idempotent)."""
        return await self._api_delete(f"/v3/config/paths/delete/{stream_id}")

    async def _persist_cameras(self):
        """Write the current camera list back to the project file via the platform."""
        cfg = self.api.config  # a copy; safe to mutate
        cfg["cameras"] = self._cameras
        await self.api.save_config(cfg)

    def _find_camera(self, stream_id):
        for c in self._cameras:
            if c.get("stream_id") == stream_id:
                return c
        return None

    def _unique_stream_id(self, name):
        base = self._slugify(name)
        sid = base
        n = 2
        while self._find_camera(sid):
            sid = f"{base}_{n}"
            n += 1
        return sid

    @staticmethod
    def _slugify(name):
        slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
        return slug or "camera"

    @staticmethod
    def _entry_from_input(cam, stream_id):
        return {
            "stream_id": stream_id,
            "name": (cam.name or "").strip() or stream_id,
            "rtsp_url": (cam.rtsp_url or "").strip(),
            "username": cam.username or "",
            "password": cam.password or "",
            "codec_hint": cam.codec_hint or "auto",
            "transcode": cam.transcode or "auto",
            "hardware_accel": cam.hardware_accel or "auto",
        }

    async def _live_status_map(self):
        data = await self._api_get("/v3/paths/list")
        live = {}
        if data:
            for item in data.get("items", []):
                name = item.get("name")
                if name:
                    # `ready` is deprecated in 1.18.x in favour of `available`.
                    live[name] = bool(item.get("available", item.get("ready", False)))
        return live

    @staticmethod
    def _camera_view(cam, live):
        return {**cam, "status": "streaming" if live.get(cam["stream_id"]) else "idle"}

    async def _publish_streams(self):
        listing = [
            {"value": c["stream_id"], "label": c.get("name") or c["stream_id"]}
            for c in self._cameras
        ]
        await self.api.state_set("stream_ids", json.dumps(listing))
        for c in self._cameras:
            await self.api.state_set(f"streams.{c['stream_id']}", "idle")

    async def _poll_statuses(self):
        data = await self._api_get("/v3/paths/list")
        if data is None:
            await self.api.state_set("running", False)
            return
        await self.api.state_set("running", True)
        live = {}
        for item in data.get("items", []):
            name = item.get("name")
            if name:
                # `ready` is deprecated in MediaMTX 1.18.x in favour of
                # `available`; prefer the new field, fall back for older builds.
                live[name] = bool(item.get("available", item.get("ready", False)))
        for c in self._cameras:
            stream_id = c["stream_id"]
            await self.api.state_set(
                f"streams.{stream_id}", "streaming" if live.get(stream_id) else "idle"
            )

    @staticmethod
    def _camera_source_url(cam):
        return VideoPanelPlugin._source_url(
            cam.get("rtsp_url") or "", cam.get("username") or "", cam.get("password") or ""
        )

    @staticmethod
    def _source_url(url, username, password):
        url = (url or "").strip()
        if not url or "://" not in url:
            return None
        if not username:
            return url
        scheme, _, rest = url.partition("://")
        authority = rest.split("/", 1)[0]
        if "@" in authority:
            # Credentials already embedded in the URL — leave it as authored.
            return url
        cred = f"{quote(username, safe='')}:{quote(password, safe='')}@"
        return f"{scheme}://{cred}{rest}"

    # ──── HTTP router (mounted at /api/plugins/video_panel/ext/) ────

    def _build_router(self):
        router = APIRouter()

        @router.get("/status")
        async def status():
            return {
                "running": bool(self._supervisor and self._supervisor.running),
                "mediamtx_version": _MEDIAMTX_VERSION,
                "stream_ids": [c["stream_id"] for c in self._cameras],
            }

        @router.get("/streams")
        async def list_streams():
            live = await self._live_status_map()
            return [self._camera_view(c, live) for c in self._cameras]

        @router.post("/streams")
        async def add_stream(cam: CameraIn):
            self._validate_url(cam.rtsp_url)
            stream_id = (cam.stream_id or "").strip() or self._unique_stream_id(cam.name)
            self._validate_stream_id(stream_id)
            if self._find_camera(stream_id):
                raise HTTPException(409, f"A camera with id '{stream_id}' already exists.")
            entry = self._entry_from_input(cam, stream_id)
            self._cameras.append(entry)
            await self._persist_cameras()
            await self._sync_path_add(entry)
            await self._publish_streams()
            live = await self._live_status_map()
            return self._camera_view(entry, live)

        @router.put("/streams/{stream_id}")
        async def edit_stream(stream_id: str, cam: CameraIn):
            existing = self._find_camera(stream_id)
            if not existing:
                raise HTTPException(404, f"No camera with id '{stream_id}'.")
            self._validate_url(cam.rtsp_url)
            new_id = (cam.stream_id or "").strip() or stream_id
            self._validate_stream_id(new_id)
            if new_id != stream_id and self._find_camera(new_id):
                raise HTTPException(409, f"A camera with id '{new_id}' already exists.")
            entry = self._entry_from_input(cam, new_id)
            self._cameras[self._cameras.index(existing)] = entry
            await self._persist_cameras()
            # save_config does not restart the plugin, so the MediaMTX path must be
            # re-synced live. Delete-then-add covers both source edits and renames.
            await self._sync_path_delete(stream_id)
            await self._sync_path_add(entry)
            await self._publish_streams()
            live = await self._live_status_map()
            return self._camera_view(entry, live)

        @router.delete("/streams/{stream_id}")
        async def delete_stream(stream_id: str):
            existing = self._find_camera(stream_id)
            if not existing:
                raise HTTPException(404, f"No camera with id '{stream_id}'.")
            self._cameras.remove(existing)
            await self._persist_cameras()
            await self._sync_path_delete(stream_id)
            await self.api.state_set(f"streams.{stream_id}", None)
            await self._publish_streams()
            return {"ok": True, "stream_id": stream_id}

        @router.post("/streams/probe")
        async def probe_stream(body: ProbeIn):
            self._validate_url(body.rtsp_url)
            if not self._ffmpeg_bin:
                raise HTTPException(
                    503,
                    "ffmpeg is not available. Reinstall the Video Panel plugin to "
                    "fetch its native dependencies.",
                )
            url = self._source_url(body.rtsp_url, body.username, body.password)
            return await self._probe_rtsp(url)

        @router.get("/streams/{stream_id}/snapshot.jpg")
        async def snapshot(stream_id: str):
            cam = self._find_camera(stream_id)
            if not cam:
                raise HTTPException(404, f"No camera with id '{stream_id}'.")
            if not self._ffmpeg_bin:
                raise HTTPException(503, "ffmpeg is not available.")
            data = await self._snapshot_rtsp(self._camera_source_url(cam))
            if not data:
                raise HTTPException(502, "Could not capture a frame from the camera.")
            return Response(
                content=data,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store"},
            )

        return router

    @staticmethod
    def _validate_url(url):
        if not url or "://" not in url:
            raise HTTPException(422, "RTSP URL must be a full URL, e.g. rtsp://host:554/stream.")

    @staticmethod
    def _validate_stream_id(stream_id):
        if not _STREAM_ID_RE.fullmatch(stream_id or ""):
            raise HTTPException(
                422,
                "Stream ID may contain only letters, numbers, hyphens, and underscores.",
            )

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
        deadline = time.monotonic() + _READY_TIMEOUT
        while time.monotonic() < deadline:
            if await self._api_get("/v3/paths/list") is not None:
                return True
            await asyncio.sleep(0.3)
        return False

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
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
        except OSError as e:
            return {"ok": False, "message": f"Could not run ffmpeg: {e}"}
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "ok": False,
                "message": "Timed out connecting to the camera. Check the URL, "
                "credentials, and that the camera is reachable on the network.",
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
                return {"ok": False, "message": "Authentication failed. Check the username and password."}
            if any(s in lowered for s in ("connection refused", "no route to host", "timed out", "timeout")):
                return {"ok": False, "message": "Could not reach the camera. Check the URL and that the device is online."}
            return {"ok": False, "message": "No video stream was found at that URL."}
        m = re.search(r"Video:\s*([A-Za-z0-9_]+)(?:\s*\(([^)]*)\))?", vid_line)
        codec = m.group(1).lower() if m else ""
        profile = (m.group(2) or "").strip() if m else ""
        res = re.search(r"(\d{2,5})x(\d{2,5})", vid_line)
        fps_m = re.search(r"(\d+(?:\.\d+)?)\s*fps", vid_line)
        result = {
            "ok": True,
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
                "advice": "This camera streams H.265/HEVC, which most browsers can't play. "
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
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
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
            "\n"
            "paths: {}\n"
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
