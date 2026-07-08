"""Host video-output enumeration for the local-fullscreen display window.

A browser display can be shown on one of this server's own video outputs (the
plugin launches and places a fullscreen browser there — see ``kiosk.py``).
This module answers "what outputs does this host have, where are they, and
which one is the one the user picked" — per platform:

- **Windows**: stdlib ctypes over ``EnumDisplayMonitors`` /
  ``GetMonitorInfoW`` / ``EnumDisplayDevicesW``. The stable output identity is
  the monitor's device interface path (monitor-on-connector: it survives
  reboots and replugs on the same port; a cable moved to another port is a
  different id on purpose — with two identical projectors, following the
  model across ports would be guesswork).
- **Linux / X11**: ``xrandr --query``. Identity is the connector name
  (``HDMI-2``).
- **Linux / Wayland (wlroots: labwc, sway)**: ``wlr-randr``. Identity is the
  connector name (``HDMI-A-2``). GNOME/KDE Wayland don't speak
  wlr-output-management, so enumeration reports an honest "can't enumerate
  on this desktop" instead of guessing.

Graphical-session detection probes for actual sockets
(``/run/user/<uid>/wayland-*``, ``/tmp/.X11-unix/X*``) — never hardcoded
names, because the compositor picks them (labwc defaults to ``wayland-0``;
a session started alongside another lands on ``wayland-1``).

Every output is a plain dict — ``{id, name, x, y, width, height, primary}``
— so the parsers stay pure and unit-testable with captured text.
"""

import asyncio
import os
import re
import sys

# How long an xrandr/wlr-randr run may take before it counts as broken.
_TOOL_TIMEOUT = 10.0

# ──── Windows (ctypes) ────

if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import ctypes
    from ctypes import wintypes

    _MONITORINFOF_PRIMARY = 0x1
    _EDD_GET_DEVICE_INTERFACE_NAME = 0x1

    class _RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class _MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", _RECT),
            ("rcWork", _RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", ctypes.c_wchar * 32),
        ]

    class _DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("DeviceName", ctypes.c_wchar * 32),
            ("DeviceString", ctypes.c_wchar * 128),
            ("StateFlags", wintypes.DWORD),
            ("DeviceID", ctypes.c_wchar * 128),
            ("DeviceKey", ctypes.c_wchar * 128),
        ]

    _MonitorEnumProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HANDLE,  # HMONITOR
        wintypes.HDC,
        ctypes.POINTER(_RECT),
        wintypes.LPARAM,
    )

    # Monitor DeviceStrings that carry no information; fall back to the EDID
    # model code from the device path for those.
    _GENERIC_MONITOR_NAMES = {"generic pnp monitor", "generic non-pnp monitor"}

    def _monitor_display_name(device_string, device_id):
        name = (device_string or "").strip()
        if name and name.lower() not in _GENERIC_MONITOR_NAMES:
            return name
        # \\?\DISPLAY#NEC68C9#5&...#{...} -> the EDID vendor+model code.
        parts = (device_id or "").split("#")
        if len(parts) >= 2 and parts[1]:
            return parts[1]
        return name or "Display"

    def enumerate_windows():
        """All active monitors, from this process's window station.

        In a session-0 service this sees the fake console display, not the
        signed-in user's real monitors — the service path enumerates through
        the in-session helper instead (see ``kiosk.py``).
        """
        user32 = ctypes.windll.user32
        handles = []

        @_MonitorEnumProc
        def _collect(hmonitor, _hdc, _rect, _lparam):
            handles.append(hmonitor)
            return True

        if not user32.EnumDisplayMonitors(None, None, _collect, 0):
            return [], "EnumDisplayMonitors failed"

        outputs = []
        for hmon in handles:
            info = _MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
            if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                continue
            dd = _DISPLAY_DEVICEW()
            dd.cb = ctypes.sizeof(_DISPLAY_DEVICEW)
            have_monitor_device = user32.EnumDisplayDevicesW(
                info.szDevice, 0, ctypes.byref(dd), _EDD_GET_DEVICE_INTERFACE_NAME
            )
            if have_monitor_device and dd.DeviceID:
                output_id = dd.DeviceID
                name = _monitor_display_name(dd.DeviceString, dd.DeviceID)
            else:
                # No attached monitor device (some remote/virtual setups):
                # the adapter name still identifies the output, just less
                # stably across reconfigurations.
                output_id = info.szDevice
                name = info.szDevice
            outputs.append({
                "id": output_id,
                "name": name,
                "x": info.rcMonitor.left,
                "y": info.rcMonitor.top,
                "width": info.rcMonitor.right - info.rcMonitor.left,
                "height": info.rcMonitor.bottom - info.rcMonitor.top,
                "primary": bool(info.dwFlags & _MONITORINFOF_PRIMARY),
            })
        if not outputs:
            return [], "no active displays found"
        return outputs, ""

    def process_session_id():
        """The Windows session this process runs in (0 = service session)."""
        kernel32 = ctypes.windll.kernel32
        sid = wintypes.DWORD()
        kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(sid))
        return sid.value

    def console_session_id():
        """The session attached to the physical console, or None if nobody
        is signed in (WTSGetActiveConsoleSessionId returns 0xFFFFFFFF)."""
        sid = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
        return None if sid == 0xFFFFFFFF else sid

    def window_rect_for_pid(pid):
        """The largest visible top-level window rect owned by ``pid``.

        Used after a kiosk launch to verify the window actually landed on
        the intended output (some window managers override client
        positioning) — a mismatch is logged, never silently covered up.
        Returns ``(x, y, width, height)`` or None.
        """
        user32 = ctypes.windll.user32
        _EnumWindowsProc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
        rects = []

        @_EnumWindowsProc
        def _collect(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                r = _RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                    w, h = r.right - r.left, r.bottom - r.top
                    if w > 0 and h > 0:
                        rects.append((r.left, r.top, w, h))
            return True

        user32.EnumWindows(_collect, 0)
        if not rects:
            return None
        return max(rects, key=lambda r: r[2] * r[3])

    def raise_window_for_pid(pid):
        """Pin the pid's main window above normal windows, without focus.

        Windows denies foreground activation to windows created by a
        background process (the server), so a kiosk browser can open BEHIND
        whatever was already on that screen. HWND_TOPMOST changes z-order
        without needing foreground rights, and SWP_NOACTIVATE keeps the
        user's keyboard focus where it was. Returns True once a window was
        raised; False when the pid has no visible window yet (retry later).
        """
        user32 = ctypes.windll.user32
        _EnumWindowsProc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
        windows = []

        @_EnumWindowsProc
        def _collect(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                r = _RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                    w, h = r.right - r.left, r.bottom - r.top
                    if w > 0 and h > 0:
                        windows.append((hwnd, w * h))
            return True

        user32.EnumWindows(_collect, 0)
        if not windows:
            return False
        hwnd = max(windows, key=lambda item: item[1])[0]
        SetWindowPos = user32.SetWindowPos
        SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.UINT,
        ]
        HWND_TOPMOST = wintypes.HWND(-1)
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
        SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                     SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        return True


# ──── Linux graphical-session detection ────


def detect_linux_session():
    """Probe for the host's graphical session sockets.

    Returns ``{"wayland": <socket name or None>, "x11": <display or None>,
    "runtime_dir": <XDG_RUNTIME_DIR>}``. Both can be present at once (a
    Wayland compositor with XWayland); the kiosk launches through XWayland
    in that case because Wayland clients can't position themselves.
    """
    session = {"wayland": None, "x11": None, "runtime_dir": None}
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    try:
        for entry in sorted(os.listdir(runtime_dir)):
            if re.fullmatch(r"wayland-\d+", entry):
                session["wayland"] = entry
                break
    except OSError:
        pass
    if session["wayland"]:
        session["runtime_dir"] = runtime_dir
    try:
        for entry in sorted(os.listdir("/tmp/.X11-unix")):
            m = re.fullmatch(r"X(\d+)", entry)
            if m:
                session["x11"] = f":{m.group(1)}"
                break
    except OSError:
        pass
    return session


# ──── X11 (xrandr) ────

# "HDMI-1 connected primary 1920x1080+1920+0 (normal ...) 598mm x 336mm"
_XRANDR_CONNECTED_RE = re.compile(
    r"^(?P<name>\S+) connected(?P<primary> primary)? "
    r"(?P<w>\d+)x(?P<h>\d+)(?P<x>[+-]\d+)(?P<y>[+-]\d+)"
)


def parse_xrandr(text):
    """Outputs from ``xrandr --query`` text.

    Disconnected outputs are skipped. So is a connected output with no
    geometry (plugged in but not enabled in the OS's display settings) —
    there is no framebuffer space to place a window on until it's enabled.
    """
    outputs = []
    for line in (text or "").splitlines():
        m = _XRANDR_CONNECTED_RE.match(line)
        if not m:
            continue
        outputs.append({
            "id": m.group("name"),
            "name": m.group("name"),
            "x": int(m.group("x")),
            "y": int(m.group("y")),
            "width": int(m.group("w")),
            "height": int(m.group("h")),
            "primary": bool(m.group("primary")),
        })
    return outputs


async def enumerate_x11(display, extra_env=None):
    """Run ``xrandr --query`` against the given X display and parse it."""
    env = dict(os.environ)
    env["DISPLAY"] = display
    env.update(extra_env or {})
    ok, out, err = await _run_tool(["xrandr", "--query"], env)
    if not ok:
        return [], err
    outputs = parse_xrandr(out)
    if not outputs:
        return [], "xrandr reported no enabled outputs"
    return outputs, ""


# ──── Wayland / wlroots (wlr-randr) ────

# Header: 'HDMI-A-1 "BNQ BenQ LCD (HDMI-A-1)"'; properties indented below.
_WLR_HEADER_RE = re.compile(r'^(?P<name>\S+)\s+"(?P<desc>.*)"\s*$')
_WLR_POSITION_RE = re.compile(r"^\s+Position:\s+(?P<x>-?\d+),(?P<y>-?\d+)")
_WLR_MODE_CURRENT_RE = re.compile(r"^\s+(?P<w>\d+)x(?P<h>\d+)\s+px.*\bcurrent\b")
_WLR_ENABLED_RE = re.compile(r"^\s+Enabled:\s+(?P<val>yes|no)")
_WLR_SCALE_RE = re.compile(r"^\s+Scale:\s+(?P<val>[\d.]+)")


def parse_wlr_randr(text):
    """Outputs from ``wlr-randr`` text output (0.2.0 has no --json).

    Sizes are divided by the output scale so positions and sizes agree with
    the compositor's layout coordinates. Wayland has no primary-output
    concept; the output at the layout origin is flagged as the likely
    console/panel screen so the picker can suggest a different one.
    """
    outputs = []
    current = None

    def _finish(entry):
        if entry is None or not entry.pop("enabled", False):
            return
        if entry.get("width") is None or entry.get("x") is None:
            return
        scale = entry.pop("scale", 1.0) or 1.0
        entry["width"] = int(round(entry["width"] / scale))
        entry["height"] = int(round(entry["height"] / scale))
        outputs.append(entry)

    for line in (text or "").splitlines():
        header = _WLR_HEADER_RE.match(line)
        if header:
            _finish(current)
            desc = header.group("desc").strip()
            current = {
                "id": header.group("name"),
                "name": desc or header.group("name"),
                "x": None,
                "y": None,
                "width": None,
                "height": None,
                "primary": False,
                "enabled": False,
                "scale": 1.0,
            }
            continue
        if current is None:
            continue
        m = _WLR_ENABLED_RE.match(line)
        if m:
            current["enabled"] = m.group("val") == "yes"
            continue
        m = _WLR_POSITION_RE.match(line)
        if m:
            current["x"] = int(m.group("x"))
            current["y"] = int(m.group("y"))
            continue
        m = _WLR_MODE_CURRENT_RE.match(line)
        if m:
            current["width"] = int(m.group("w"))
            current["height"] = int(m.group("h"))
            continue
        m = _WLR_SCALE_RE.match(line)
        if m:
            try:
                current["scale"] = float(m.group("val"))
            except ValueError:
                pass

    _finish(current)
    for output in outputs:
        output["primary"] = output["x"] == 0 and output["y"] == 0
    return outputs


async def enumerate_wayland(wayland_display, runtime_dir):
    """Run ``wlr-randr`` against the given Wayland socket and parse it."""
    env = dict(os.environ)
    env["WAYLAND_DISPLAY"] = wayland_display
    if runtime_dir:
        env["XDG_RUNTIME_DIR"] = runtime_dir
    ok, out, err = await _run_tool(["wlr-randr"], env)
    if not ok:
        if "wlr-randr" in err and "No such file" in err:
            return [], (
                "wlr-randr is not installed (needed to list outputs on a "
                "Wayland desktop)"
            )
        # GNOME and KDE Wayland don't implement wlr-output-management.
        return [], (
            "this desktop doesn't support output enumeration "
            f"(wlr-randr: {err or 'failed'})"
        )
    outputs = parse_wlr_randr(out)
    if not outputs:
        return [], "wlr-randr reported no enabled outputs"
    return outputs, ""


async def _run_tool(argv, env):
    """Run a query tool; ``(ok, stdout, error_reason)``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "", f"{argv[0]} is not installed (No such file)"
    except OSError as e:
        return False, "", f"{argv[0]} failed to start: {e}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False, "", f"{argv[0]} timed out"
    if proc.returncode != 0:
        detail = (err or out or b"").decode("utf-8", "replace").strip()
        return False, "", detail.splitlines()[0] if detail else f"exit {proc.returncode}"
    return True, out.decode("utf-8", "replace"), ""
