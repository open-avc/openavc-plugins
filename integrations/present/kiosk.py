"""Local fullscreen display windows: launch, place, watch, restart.

A browser display with a ``local_output`` set is shown by this server itself:
the plugin launches a Chromium-family browser in kiosk mode, placed on the
chosen video output, pointed at that display's own Display page — the same
page a separate receiver box would open, so switching stays sub-second and
seamless. The usual shape is a mini PC or appliance whose primary output is
the panel/console and whose second output feeds the projector or TV.

Mechanism facts this module is built on (all source-verified or proven live;
see the plugin README for the user-facing story):

- **A dedicated profile directory per display is mandatory.** A second
  Chromium/Edge launch sharing a profile dir hands off to the existing
  instance and silently ignores every placement flag. Each kiosk gets
  ``<data_dir>/kiosk/<display_id>``; the spawned PID then owns the window,
  and a near-instant exit is how a handoff (a leftover instance still holding
  the profile) is detected.
- **Placement is coordinates, not a monitor picker.** ``--window-position``
  at the target output's origin plus ``--window-size`` of its full size make
  Chromium fullscreen onto the monitor containing that rect. Off-layout
  coordinates silently fall back to the primary monitor, so coordinates are
  recomputed from live topology before every launch, and the size is always
  passed (a default-sized window straddling outputs can land wrong).
- **Wayland clients can't position themselves**, so on a Wayland desktop the
  browser is launched through XWayland (``--ozone-platform=x11``), where the
  compositor honors the initial position. Without an XWayland socket the
  launch still happens but placement is best-effort (single-output hosts
  don't care; multi-output hosts get a logged warning).
- **A vanished output kills the kiosk immediately** — never let the OS
  migrate the window onto the console/panel screen — and it relaunches at
  freshly computed coordinates when the output returns (covers replug at a
  different position, resolution changes, and overnight TV power cycles).
- **Windows services live in session 0** and can neither draw on the desktop
  nor see the real monitors. When the server runs as a service, the browser
  is launched into the signed-in console user's session
  (``WTSQueryUserToken`` + ``CreateProcessAsUserW``), and enumeration runs
  in-session through a small PowerShell helper that writes JSON to a file.
  With nobody signed in the display waits (``waiting_for_signin``).

Browser stdout/stderr go to DEVNULL on purpose: kiosk Chromium logs a
constant stream of GPU chatter that would flood the in-memory log ring.
Failures are surfaced through exit codes, states, and explicit log lines.

No OpenAVC imports — logging, task creation, and (in tests) the process
backend, enumeration, and clock are injected.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

try:
    import hostoutputs
except ImportError:  # pragma: no cover - package-path import (tests/CI)
    from . import hostoutputs

# Topology watch cadence. Also bounds how quickly a crash or unplug is seen.
_WATCH_INTERVAL = 5.0
# A kiosk that exits within this window of launch counts as a failed launch
# (typical causes: a leftover instance holding the profile dir, a bad flag,
# a broken browser install).
_QUICK_EXIT_WINDOW = 10.0
# Don't verify window placement until the browser has had time to map and
# fullscreen its window.
_PLACEMENT_VERIFY_AFTER = 8.0
_PLACEMENT_TOLERANCE = 2  # px; fullscreen should land exactly

# Relaunch backoff + circuit breaker (the supervisor pattern used across the
# plugin's external processes).
_BACKOFF_SCHEDULE = (2.0, 5.0, 10.0, 30.0)
_CIRCUIT_FAILURES = 5
_CIRCUIT_WINDOW = 300.0
_TERMINATE_GRACE = 5.0

# How long the in-session enumeration helper may take.
_HELPER_TIMEOUT = 20.0
# The helper is a PowerShell spawn with a C# compile — too heavy to run on
# every 5 s watch tick. Its answer is cached this long; unplug/replug
# detection in service mode is bounded by watch interval + this.
_HELPER_CACHE_SECONDS = 10.0

# Linux browser binaries, in discovery order.
_LINUX_BROWSERS = ("chromium-browser", "chromium", "google-chrome", "google-chrome-stable")
# Windows App Paths registry lookups, in preference order. Chrome first:
# Edge's first-run suppression has regressed across versions (its reliable
# suppressor is an HKLM policy this plugin must not set).
_WINDOWS_BROWSERS = ("chrome.exe", "msedge.exe")


# ──── Browser discovery ────


def browser_family(path):
    """"edge" or "chromium" (Chrome and Chromium take identical flags)."""
    stem = Path(path).stem.lower()
    return "edge" if "edge" in stem else "chromium"


def find_browser(override=None):
    """Locate the kiosk browser; ``(path, family)`` or ``(None, reason)``.

    ``override`` is the plugin's Browser Path config field — used verbatim
    when set (and reported missing rather than silently substituted).
    """
    if override:
        if Path(override).exists():
            return override, browser_family(override)
        return None, f"the configured browser path does not exist: {override}"
    if sys.platform == "win32":
        for exe in _WINDOWS_BROWSERS:
            path = _app_paths_lookup(exe)
            if path:
                return path, browser_family(path)
        return None, (
            "no Chrome or Edge installation was found (App Paths registry); "
            "set Browser Path in the plugin configuration"
        )
    for name in _LINUX_BROWSERS:
        path = shutil.which(name)
        if path:
            return path, browser_family(path)
    return None, (
        "no Chromium or Chrome was found on PATH; install chromium or set "
        "Browser Path in the plugin configuration"
    )


def _app_paths_lookup(exe):  # pragma: no cover - Windows registry
    """The registered install path for an executable, via App Paths."""
    if sys.platform != "win32":
        return None
    import winreg

    subkey = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, subkey) as key:
                path, _ = winreg.QueryValueEx(key, None)
            if path and Path(path).exists():
                return path
        except OSError:
            continue
    return None


# ──── Kiosk argv ────


def build_kiosk_args(browser, family, url, rect, profile_dir, *,
                     ozone_x11=False, windows=None):
    """The kiosk launch argv, placed on the output described by ``rect``.

    The flag set is current for Chromium/Edge ~M125+; flags that are dead in
    current builds (``--disable-infobars``, ``--disable-session-crashed-bubble``,
    ``--disable-translate``) are deliberately absent. ``--allow-insecure-localhost``
    keeps the self-signed HTTPS interstitial away from the localhost Display
    URL (same flag the platform's own kiosk launcher uses).
    """
    if windows is None:
        windows = sys.platform == "win32"
    args = [str(browser)]
    if family == "edge":
        args.append("--edge-kiosk-type=fullscreen")
    if ozone_x11:
        args.append("--ozone-platform=x11")
    args += [
        "--kiosk",
        f"--user-data-dir={profile_dir}",
        f"--window-position={rect['x']},{rect['y']}",
        f"--window-size={rect['width']},{rect['height']}",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
        "--disable-component-update",
        "--disable-search-engine-choice-screen",
        "--autoplay-policy=no-user-gesture-required",
        "--allow-insecure-localhost",
    ]
    if not windows:
        args.append("--password-store=basic")
    args.append(url)
    return args


# ──── Process backends ────


class AsyncioProcessBackend:
    """Spawn/track children with asyncio (Windows interactive + Linux)."""

    async def spawn(self, argv, env=None, hidden=False):
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        return _AsyncioProc(proc)


class _AsyncioProc:
    def __init__(self, proc):
        self._proc = proc
        self.pid = proc.pid

    @property
    def returncode(self):
        return self._proc.returncode

    def alive(self):
        return self._proc.returncode is None

    async def wait(self):
        return await self._proc.wait()

    def terminate(self):
        try:
            self._proc.terminate()
        except ProcessLookupError:
            pass

    def kill(self):
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass


class Win32SessionBackend:  # pragma: no cover - exercised on Windows services
    """Spawn into the console user's session from a session-0 service.

    ``WTSQueryUserToken`` needs SE_TCB, which the service's LocalSystem
    account has. The child runs on the user's desktop (``winsta0\\default``)
    with the user's environment block.
    """

    async def spawn(self, argv, env=None, hidden=False):
        # The Win32 calls are quick and synchronous; keep them off the loop.
        pid, handle = await asyncio.to_thread(_create_process_in_console_session,
                                              argv, hidden)
        return _Win32Proc(pid, handle)


class _Win32Proc:  # pragma: no cover - exercised on Windows services
    _STILL_ACTIVE = 259

    def __init__(self, pid, handle):
        self.pid = pid
        self._handle = handle
        self._returncode = None

    @property
    def returncode(self):
        self._poll()
        return self._returncode

    def alive(self):
        self._poll()
        return self._returncode is None

    def _poll(self):
        if self._returncode is not None or self._handle is None:
            return
        import ctypes
        from ctypes import wintypes

        code = wintypes.DWORD()
        if ctypes.windll.kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code)):
            if code.value != self._STILL_ACTIVE:
                self._returncode = code.value
                ctypes.windll.kernel32.CloseHandle(self._handle)
                self._handle = None

    async def wait(self):
        while self.alive():
            await asyncio.sleep(0.2)
        return self._returncode

    def terminate(self):
        self._terminate()

    def kill(self):
        self._terminate()

    def _terminate(self):
        if self._handle is None:
            return
        import ctypes

        ctypes.windll.kernel32.TerminateProcess(self._handle, 1)


def _create_process_in_console_session(argv, hidden):  # pragma: no cover
    """CreateProcessAsUserW into the active console session; ``(pid, handle)``."""
    import ctypes
    from ctypes import wintypes

    session = hostoutputs.console_session_id()
    if session is None:
        raise OSError("no user is signed in on the console")

    wtsapi32 = ctypes.windll.wtsapi32
    advapi32 = ctypes.windll.advapi32
    userenv = ctypes.windll.userenv
    kernel32 = ctypes.windll.kernel32

    token = wintypes.HANDLE()
    if not wtsapi32.WTSQueryUserToken(session, ctypes.byref(token)):
        raise ctypes.WinError()

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000

    env_block = ctypes.c_void_p()
    have_env = userenv.CreateEnvironmentBlock(ctypes.byref(env_block), token, False)

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.lpDesktop = "winsta0\\default"
    pi = PROCESS_INFORMATION()

    flags = CREATE_UNICODE_ENVIRONMENT | CREATE_NEW_PROCESS_GROUP
    if hidden:
        flags |= CREATE_NO_WINDOW
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))

    try:
        ok = advapi32.CreateProcessAsUserW(
            token, None, cmdline, None, None, False, flags,
            env_block if have_env else None, None,
            ctypes.byref(si), ctypes.byref(pi),
        )
        if not ok:
            raise ctypes.WinError()
    finally:
        if have_env:
            userenv.DestroyEnvironmentBlock(env_block)
        kernel32.CloseHandle(token)

    kernel32.CloseHandle(pi.hThread)
    return pi.dwProcessId, pi.hProcess


# ──── Host environments ────


class _BaseEnvironment:
    supported = True
    reason = ""
    label = ""
    ozone_x11 = False

    def __init__(self):
        self.backend = AsyncioProcessBackend()

    def launch_env(self):
        return None  # inherit

    async def enumerate(self):
        return [], "not implemented"

    def console_user_available(self):
        return True

    def window_rect_for_pid(self, pid):
        """Actual on-screen rect for a launched kiosk, when checkable."""
        return None


class _UnsupportedEnvironment(_BaseEnvironment):
    supported = False
    label = "unsupported"

    def __init__(self, reason):
        super().__init__()
        self.reason = reason

    async def enumerate(self):
        return [], self.reason


class _WindowsEnvironment(_BaseEnvironment):  # pragma: no cover - Windows only
    label = "windows"

    async def enumerate(self):
        return hostoutputs.enumerate_windows()

    def window_rect_for_pid(self, pid):
        try:
            return hostoutputs.window_rect_for_pid(pid)
        except OSError:
            return None


class _WindowsServiceEnvironment(_BaseEnvironment):  # pragma: no cover - Windows only
    """Session-0 service: launch and enumerate inside the console session."""

    label = "windows-service"

    def __init__(self, plugin_dir, data_dir, log):
        super().__init__()
        self.backend = Win32SessionBackend()
        self._ps1 = Path(plugin_dir) / "enum_outputs.ps1"
        self._out_file = Path(data_dir) / "kiosk" / "outputs.json"
        self._log = log
        self._cached = None  # ((outputs, err), at_monotonic)

    def console_user_available(self):
        return hostoutputs.console_session_id() is not None

    async def enumerate(self):
        if self._cached is not None:
            result, at = self._cached
            if time.monotonic() - at < _HELPER_CACHE_SECONDS:
                return result
        result = await self._enumerate_in_session()
        self._cached = (result, time.monotonic())
        return result

    async def _enumerate_in_session(self):
        if not self.console_user_available():
            return [], "no user is signed in on this server's console"
        self._out_file.parent.mkdir(parents=True, exist_ok=True)
        powershell = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
        )
        argv = [
            powershell, "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", str(self._ps1), "-OutFile", str(self._out_file),
        ]
        try:
            proc = await self.backend.spawn(argv, hidden=True)
            rc = await asyncio.wait_for(proc.wait(), timeout=_HELPER_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            return [], "the in-session display enumeration helper timed out"
        except OSError as e:
            return [], f"could not run the display enumeration helper: {e}"
        if rc != 0:
            return [], f"the display enumeration helper failed (exit {rc})"
        try:
            data = json.loads(self._out_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            return [], f"could not read the enumerated display list: {e}"
        outputs = []
        for item in data if isinstance(data, list) else []:
            try:
                outputs.append({
                    "id": str(item["id"]),
                    "name": str(item.get("name") or item["id"]),
                    "x": int(item["x"]),
                    "y": int(item["y"]),
                    "width": int(item["width"]),
                    "height": int(item["height"]),
                    "primary": bool(item.get("primary")),
                })
            except (KeyError, TypeError, ValueError):
                continue
        if not outputs:
            return [], "the display enumeration helper found no displays"
        return outputs, ""


class _X11Environment(_BaseEnvironment):
    label = "x11"

    def __init__(self, display):
        super().__init__()
        self._display = display

    def launch_env(self):
        env = dict(os.environ)
        env["DISPLAY"] = self._display
        return env

    async def enumerate(self):
        return await hostoutputs.enumerate_x11(self._display)


class _WaylandEnvironment(_BaseEnvironment):
    """wlroots Wayland; the kiosk launches through XWayland when present."""

    label = "wayland"

    def __init__(self, wayland_display, runtime_dir, x11_display):
        super().__init__()
        self._wayland_display = wayland_display
        self._runtime_dir = runtime_dir
        self._x11_display = x11_display
        self.ozone_x11 = bool(x11_display)

    def launch_env(self):
        env = dict(os.environ)
        env["WAYLAND_DISPLAY"] = self._wayland_display
        if self._runtime_dir:
            env["XDG_RUNTIME_DIR"] = self._runtime_dir
        if self._x11_display:
            env["DISPLAY"] = self._x11_display
        return env

    async def enumerate(self):
        return await hostoutputs.enumerate_wayland(
            self._wayland_display, self._runtime_dir
        )


def detect_environment(plugin_dir, data_dir, log):
    """Pick the host environment for enumeration + kiosk launches."""
    if sys.platform == "win32":  # pragma: no cover - Windows only
        if hostoutputs.process_session_id() == 0:
            return _WindowsServiceEnvironment(plugin_dir, data_dir, log)
        return _WindowsEnvironment()
    if sys.platform.startswith("linux"):
        session = hostoutputs.detect_linux_session()
        if session["wayland"]:
            return _WaylandEnvironment(
                session["wayland"], session["runtime_dir"], session["x11"]
            )
        if session["x11"]:
            return _X11Environment(session["x11"])
        return _UnsupportedEnvironment(
            "no graphical session was found on this server (a signed-in "
            "desktop session is required to show a display locally)"
        )
    return _UnsupportedEnvironment(
        f"local display windows aren't supported on this platform ({sys.platform})"
    )


# ──── Manager ────


class _Kiosk:
    """Runtime record for one local display's browser."""

    def __init__(self, spec):
        self.spec = spec  # {"output": <output id>, "url": <display URL>}
        self.state = "starting"
        self.proc = None
        self.launched_at = 0.0
        self.launched_rect = None
        self.failures = deque()
        self.backoff_index = 0
        self.next_attempt = 0.0
        self.circuit_broken = False
        self.placement_checked = False
        self.last_log = ""  # dedupes repeating per-tick log lines


class KioskManager:
    """One supervised kiosk browser per local display, driven by a topology
    watch: every ~5 s (and immediately on config changes) the host's outputs
    are enumerated and each display is reconciled — launch when its output is
    present, kill immediately when it vanishes, relaunch at fresh coordinates
    when it returns or moves, restart with backoff on crashes, and hold
    honest waiting states (``waiting_for_output``, ``waiting_for_signin``)
    the IDE can show."""

    def __init__(self, *, environment, data_dir, log, task_factory,
                 browser_resolver=None, watch_interval=_WATCH_INTERVAL,
                 monotonic=time.monotonic):
        self._env = environment
        self._data_dir = Path(data_dir)
        self._log_fn = log
        self._make_task = task_factory
        self._resolve_browser = browser_resolver or find_browser
        self._interval = watch_interval
        self._now = monotonic

        self._kiosks = {}  # display_id -> _Kiosk
        self._outputs = []  # last enumerated topology
        self._enum_error = ""
        self._enumerated_once = False
        self._watch = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._reconcile_lock = asyncio.Lock()

    # ── Public surface ──

    async def sync(self, specs):
        """Reconcile the desired set: ``{display_id: {"output", "url"}}``.

        Removed displays are torn down; a changed URL or output relaunches
        (and resets the failure trail — a config change is a fresh start).
        """
        for display_id in list(self._kiosks):
            if display_id not in specs:
                await self._ensure_dead(self._kiosks.pop(display_id))
        for display_id, spec in specs.items():
            kiosk = self._kiosks.get(display_id)
            if kiosk is None:
                self._kiosks[display_id] = _Kiosk(dict(spec))
            elif kiosk.spec != spec:
                await self._ensure_dead(kiosk)
                self._kiosks[display_id] = _Kiosk(dict(spec))
        if self._kiosks and self._watch is None and not self._stopping:
            self._watch = self._make_task(self._watch_loop())
        self._wake.set()

    async def stop(self):
        self._stopping = True
        self._wake.set()
        if self._watch is not None and not self._watch.done():
            self._watch.cancel()
            try:
                await self._watch
            except BaseException:
                pass
        self._watch = None
        for kiosk in self._kiosks.values():
            await self._ensure_dead(kiosk)
            kiosk.state = "stopped"
        self._kiosks = {}

    def state_for(self, display_id):
        if not self._env.supported:
            return "unsupported"
        kiosk = self._kiosks.get(display_id)
        return kiosk.state if kiosk is not None else "stopped"

    def output_name(self, output_id):
        for output in self._outputs:
            if output["id"] == output_id:
                return output["name"]
        return ""

    def output_connected(self, output_id):
        return any(o["id"] == output_id for o in self._outputs)

    async def describe_outputs(self, in_use=None):
        """Fresh enumeration for the IDE's output picker.

        ``in_use`` maps output id -> display id for taken outputs; the picker
        disables those. ``supported`` is False when this host can never show
        a local display (no session, unsupported desktop/platform).
        """
        if not self._env.supported:
            return {"supported": False, "reason": self._env.reason, "outputs": []}
        outputs, err = await self._env.enumerate()
        self._remember_topology(outputs, err)
        described = [
            {**output, "in_use_by": (in_use or {}).get(output["id"], "")}
            for output in outputs
        ]
        return {"supported": not err, "reason": err, "outputs": described}

    # ── Watch loop ──

    async def _watch_loop(self):
        while not self._stopping:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let one bad tick kill the watch
                self._log(f"kiosk watch error: {e}", "warning")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def reconcile(self):
        """One watch tick. Public so tests (and sync paths) can drive it."""
        async with self._reconcile_lock:
            await self._reconcile_locked()

    async def _reconcile_locked(self):
        if self._stopping or not self._kiosks:
            return
        if not self._env.supported:
            for kiosk in self._kiosks.values():
                kiosk.state = "unsupported"
            self._log_once_all(self._env.reason, "warning")
            return
        if not self._env.console_user_available():
            for kiosk in self._kiosks.values():
                await self._ensure_dead(kiosk)
                kiosk.state = "waiting_for_signin"
            return

        outputs, err = await self._env.enumerate()
        self._remember_topology(outputs, err)
        by_id = {output["id"]: output for output in outputs}
        now = self._now()

        for display_id, kiosk in self._kiosks.items():
            output = by_id.get(kiosk.spec["output"])
            if output is None:
                # Unplugged or powered off: kill NOW so the OS can't migrate
                # the fullscreen window onto whatever screen remains.
                if kiosk.proc is not None and kiosk.proc.alive():
                    self._log(
                        f"display '{display_id}': output disappeared; closing "
                        "its window until the output returns"
                    )
                await self._ensure_dead(kiosk)
                if err:
                    kiosk.state = "error"
                    self._log_once(kiosk, f"display '{display_id}': cannot list "
                                          f"this server's outputs: {err}", "warning")
                else:
                    kiosk.state = "waiting_for_output"
                continue

            rect = {k: output[k] for k in ("x", "y", "width", "height")}
            if kiosk.proc is not None and kiosk.proc.alive():
                if rect != kiosk.launched_rect:
                    # Moved or changed resolution: relaunch at fresh coords.
                    self._log(f"display '{display_id}': output geometry changed; "
                              "relaunching its window")
                    await self._ensure_dead(kiosk)
                else:
                    if kiosk.state == "starting" and now - kiosk.launched_at > _QUICK_EXIT_WINDOW:
                        kiosk.state = "running"
                    self._verify_placement(display_id, kiosk, output, now)
                    continue

            if kiosk.proc is not None:
                # Died since the last tick.
                self._record_exit(display_id, kiosk, now)
                if kiosk.circuit_broken:
                    continue

            if kiosk.circuit_broken or now < kiosk.next_attempt:
                continue
            await self._launch(display_id, kiosk, output, rect, now)

    # ── Launch / teardown ──

    async def _launch(self, display_id, kiosk, output, rect, now):
        browser, family = self._resolve_browser()
        if browser is None:
            kiosk.state = "error"
            self._log_once(kiosk, f"display '{display_id}': {family}", "error")
            return
        profile_dir = self._data_dir / "kiosk" / display_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        argv = build_kiosk_args(
            browser, family, kiosk.spec["url"], rect, profile_dir,
            ozone_x11=self._env.ozone_x11,
        )
        if self._env.ozone_x11 is False and self._env.label == "wayland":
            self._log_once(kiosk, f"display '{display_id}': no XWayland socket; "
                           "the window may open on the wrong output", "warning")
        try:
            kiosk.proc = await self._env.backend.spawn(argv, env=self._env.launch_env())
        except OSError as e:
            kiosk.state = "error"
            kiosk.proc = None
            self._record_failure(kiosk, now)
            self._log(f"display '{display_id}': failed to launch the browser: {e}",
                      "error")
            return
        kiosk.launched_at = now
        kiosk.launched_rect = rect
        kiosk.placement_checked = False
        kiosk.state = "starting"
        kiosk.last_log = ""
        self._log(
            f"display '{display_id}': opened fullscreen on "
            f"{output['name']} ({rect['width']}x{rect['height']} at "
            f"{rect['x']},{rect['y']}), pid {kiosk.proc.pid}"
        )

    def _record_exit(self, display_id, kiosk, now):
        lifetime = now - kiosk.launched_at
        rc = kiosk.proc.returncode
        kiosk.proc = None
        if lifetime <= _QUICK_EXIT_WINDOW:
            # ProcessSingleton handoff or an instantly-crashing launch. The
            # handoff case means something else holds this profile dir — a
            # leftover browser from an unclean shutdown; it must be closed
            # before the kiosk can own its window.
            self._log(
                f"display '{display_id}': browser exited {lifetime:.0f}s after "
                f"launch (code {rc}) — if this repeats, close any leftover "
                "browser window using this display's profile", "warning",
            )
        else:
            self._log(f"display '{display_id}': browser exited unexpectedly "
                      f"(code {rc}); restarting", "warning")
            # A long healthy run clears the crash trail (supervisor pattern).
            if lifetime > _CIRCUIT_WINDOW:
                kiosk.failures.clear()
                kiosk.backoff_index = 0
        self._record_failure(kiosk, now)
        if kiosk.circuit_broken:
            kiosk.state = "error"
            self._log(
                f"display '{display_id}': browser failed {_CIRCUIT_FAILURES} "
                f"times within {int(_CIRCUIT_WINDOW)}s; giving up (edit the "
                "display or restart the plugin to retry)", "error",
            )
        else:
            kiosk.state = "starting"

    def _record_failure(self, kiosk, now):
        kiosk.failures.append(now)
        while kiosk.failures and now - kiosk.failures[0] > _CIRCUIT_WINDOW:
            kiosk.failures.popleft()
        if len(kiosk.failures) >= _CIRCUIT_FAILURES:
            kiosk.circuit_broken = True
            return
        delay = _BACKOFF_SCHEDULE[min(kiosk.backoff_index, len(_BACKOFF_SCHEDULE) - 1)]
        kiosk.backoff_index += 1
        kiosk.next_attempt = now + delay

    async def _ensure_dead(self, kiosk):
        """Terminate the browser and wait until it is really gone.

        Launching against a *hung* instance still holding the profile dir
        SIGKILLs it or pops a blocking dialog, so a respawn must never race
        a dying predecessor: terminate, grace, kill, wait.
        """
        proc = kiosk.proc
        kiosk.proc = None
        if proc is None or not proc.alive():
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE)
            except asyncio.TimeoutError:
                self._log("a kiosk browser did not exit after kill", "warning")

    def _verify_placement(self, display_id, kiosk, output, now):
        """Log (once per launch) when the window isn't where it was told to be.

        Some desktops override client positioning; better an explicit log
        line than a window silently covering the console screen.
        """
        if kiosk.placement_checked or now - kiosk.launched_at < _PLACEMENT_VERIFY_AFTER:
            return
        kiosk.placement_checked = True
        rect = self._env.window_rect_for_pid(kiosk.proc.pid)
        if rect is None:
            return
        x, y, w, h = rect
        expected = kiosk.launched_rect
        if (abs(x - expected["x"]) > _PLACEMENT_TOLERANCE
                or abs(y - expected["y"]) > _PLACEMENT_TOLERANCE):
            self._log(
                f"display '{display_id}': the browser window is at {x},{y} "
                f"({w}x{h}) instead of {expected['x']},{expected['y']} — the "
                f"desktop may have overridden placement; it may be covering "
                "the wrong screen", "warning",
            )

    # ── Topology bookkeeping ──

    def _remember_topology(self, outputs, err):
        self._outputs = outputs
        self._enum_error = err
        self._enumerated_once = True

    def _log_once(self, kiosk, msg, level="info"):
        if kiosk.last_log != msg:
            kiosk.last_log = msg
            self._log(msg, level)

    def _log_once_all(self, msg, level="info"):
        for kiosk in self._kiosks.values():
            self._log_once(kiosk, msg, level)
            break  # one line for the lot; per-kiosk dedupe uses the first

    def _log(self, msg, level="info"):
        if self._log_fn:
            try:
                self._log_fn(msg, level)
            except Exception:
                pass


def remove_profile(data_dir, display_id):
    """Best-effort cleanup of a deleted display's browser profile dir."""
    try:
        shutil.rmtree(Path(data_dir) / "kiosk" / display_id)
    except OSError:
        pass
