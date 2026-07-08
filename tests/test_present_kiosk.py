"""
Tests for the Present plugin's local-fullscreen kiosk machinery.

Covers the pure output parsers (xrandr / wlr-randr text), the kiosk argv
builder (current flag set, placement flags, per-family and per-platform
differences), browser discovery overrides, and the KioskManager's
topology-watch policy — launch/place, kill-on-unplug, relaunch-on-return at
fresh coordinates, crash backoff + circuit breaker, quick-exit (profile
handoff) accounting, waiting-for-signin, and config-change relaunches — all
against fake backends, a fake environment, and a fake clock.

The Windows ctypes paths (monitor enumeration, session queries,
CreateProcessAsUserW) and the in-session PowerShell helper are exercised on
the bench, not here.

Run from the openavc-plugins root: pytest tests/test_present_kiosk.py -v
"""

import asyncio
import sys
from pathlib import Path

import pytest

_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))

from integrations.present import hostoutputs, kiosk  # noqa: E402


# ──── xrandr parsing ────

_XRANDR_TEXT = """\
Screen 0: minimum 320 x 200, current 4800 x 1620, maximum 16384 x 16384
HDMI-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 598mm x 336mm
   1920x1080     60.00*+  50.00
   1280x720      60.00
HDMI-2 connected 2880x1620-908-1620 (normal left inverted right x axis y axis) 600mm x 340mm
   2880x1620     60.00*+
DP-1 disconnected (normal left inverted right x axis y axis)
DP-2 connected (normal left inverted right x axis y axis)
"""


def test_parse_xrandr_geometry_primary_and_skips():
    outputs = hostoutputs.parse_xrandr(_XRANDR_TEXT)
    # DP-1 is disconnected; DP-2 is connected but not enabled (no geometry).
    assert [o["id"] for o in outputs] == ["HDMI-1", "HDMI-2"]

    primary = outputs[0]
    assert primary["primary"] is True
    assert (primary["x"], primary["y"]) == (0, 0)
    assert (primary["width"], primary["height"]) == (1920, 1080)

    # Negative coordinates (a monitor above/left of the primary) survive.
    second = outputs[1]
    assert second["primary"] is False
    assert (second["x"], second["y"]) == (-908, -1620)
    assert (second["width"], second["height"]) == (2880, 1620)


def test_parse_xrandr_empty_and_garbage():
    assert hostoutputs.parse_xrandr("") == []
    assert hostoutputs.parse_xrandr("not xrandr output\nat all") == []


# ──── wlr-randr parsing ────

_WLR_TEXT = """\
HDMI-A-1 "BNQ BenQ LCD 1234 (HDMI-A-1)"
  Physical size: 600x340 mm
  Enabled: yes
  Modes:
    1920x1080 px, 60.000000 Hz (preferred, current)
    1280x720 px, 60.000000 Hz
  Position: 0,0
  Transform: normal
  Scale: 1.000000
HDMI-A-2 "Dell Inc. DELL U2515H (HDMI-A-2)"
  Physical size: 553x311 mm
  Enabled: yes
  Modes:
    2560x1440 px, 59.951000 Hz (preferred, current)
  Position: 1920,0
  Transform: normal
  Scale: 2.000000
DP-1 "Unknown"
  Enabled: no
  Modes:
    1920x1080 px, 60.000000 Hz (preferred)
"""


def test_parse_wlr_randr_enabled_position_scale():
    outputs = hostoutputs.parse_wlr_randr(_WLR_TEXT)
    assert [o["id"] for o in outputs] == ["HDMI-A-1", "HDMI-A-2"]

    first = outputs[0]
    assert first["name"] == "BNQ BenQ LCD 1234 (HDMI-A-1)"
    assert (first["x"], first["y"], first["width"], first["height"]) == (0, 0, 1920, 1080)
    # Wayland has no primary flag; the output at the layout origin is the
    # likely console/panel screen, so the picker treats it as primary.
    assert first["primary"] is True

    second = outputs[1]
    # Scale 2: sizes come back in layout (logical) units.
    assert (second["width"], second["height"]) == (1280, 720)
    assert (second["x"], second["y"]) == (1920, 0)
    assert second["primary"] is False


def test_parse_wlr_randr_empty():
    assert hostoutputs.parse_wlr_randr("") == []


# ──── Linux session detection ────


def test_detect_linux_session_probes_sockets(monkeypatch, tmp_path):
    runtime = tmp_path / "run-user"
    runtime.mkdir()
    (runtime / "wayland-1").touch()  # labwc alongside another session
    (runtime / "wayland-1.lock").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    real_listdir = hostoutputs.os.listdir

    def fake_listdir(path):
        if str(path) == "/tmp/.X11-unix":
            return ["X0"]
        return real_listdir(path)

    monkeypatch.setattr(hostoutputs.os, "listdir", fake_listdir)
    session = hostoutputs.detect_linux_session()
    assert session["wayland"] == "wayland-1"  # never a hardcoded wayland-0
    assert session["x11"] == ":0"
    assert session["runtime_dir"] == str(runtime)


def test_detect_linux_session_none_found(monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(empty))

    def fake_listdir(path):
        if str(path) == "/tmp/.X11-unix":
            raise FileNotFoundError(path)
        return []

    monkeypatch.setattr(hostoutputs.os, "listdir", fake_listdir)
    session = hostoutputs.detect_linux_session()
    assert session == {"wayland": None, "x11": None, "runtime_dir": None}


# ──── Browser discovery + argv ────


def test_find_browser_override(tmp_path):
    fake = tmp_path / "my-chromium"
    fake.write_text("")
    path, family = kiosk.find_browser(str(fake))
    assert path == str(fake)
    assert family == "chromium"

    missing, reason = kiosk.find_browser(str(tmp_path / "gone"))
    assert missing is None
    assert "does not exist" in reason


def test_browser_family():
    assert kiosk.browser_family(r"C:\Program Files\Microsoft\Edge\msedge.exe") == "edge"
    assert kiosk.browser_family("/usr/bin/chromium-browser") == "chromium"
    assert kiosk.browser_family(r"C:\Program Files\Google\Chrome\chrome.exe") == "chromium"


_RECT = {"x": -908, "y": -1620, "width": 2880, "height": 1620}


def test_build_kiosk_args_chromium_linux():
    args = kiosk.build_kiosk_args(
        "/usr/bin/chromium", "chromium", "http://127.0.0.1:8080/present/display/main?key=k",
        _RECT, "/data/kiosk/main", windows=False,
    )
    assert args[0] == "/usr/bin/chromium"
    assert args[-1] == "http://127.0.0.1:8080/present/display/main?key=k"
    assert "--kiosk" in args
    assert "--user-data-dir=/data/kiosk/main" in args
    # Placement: exact origin (negative coords fine) and the FULL output
    # size — a default-sized window straddling outputs can land wrong.
    assert "--window-position=-908,-1620" in args
    assert "--window-size=2880,1620" in args
    assert "--password-store=basic" in args
    assert "--allow-insecure-localhost" in args
    assert "--autoplay-policy=no-user-gesture-required" in args
    # Flags that are dead in current Chromium must not ride along.
    for dead in ("--disable-infobars", "--disable-session-crashed-bubble",
                 "--disable-translate", "--start-fullscreen"):
        assert not any(a.startswith(dead) for a in args), dead
    # Not an Edge or Wayland launch.
    assert not any(a.startswith("--edge-kiosk-type") for a in args)
    assert not any(a.startswith("--ozone-platform") for a in args)


def test_build_kiosk_args_edge_windows():
    args = kiosk.build_kiosk_args(
        r"C:\Program Files\Microsoft\Edge\msedge.exe", "edge",
        "http://127.0.0.1:8080/x", {"x": 0, "y": 0, "width": 1920, "height": 1080},
        r"C:\data\kiosk\main", windows=True,
    )
    assert "--edge-kiosk-type=fullscreen" in args
    assert "--kiosk" in args
    assert "--password-store=basic" not in args


def test_build_kiosk_args_xwayland():
    args = kiosk.build_kiosk_args(
        "/usr/bin/chromium", "chromium", "http://x", _RECT, "/p",
        ozone_x11=True, windows=False,
    )
    assert "--ozone-platform=x11" in args


# ──── KioskManager fakes ────


class _FakeProc:
    def __init__(self, pid=1000):
        self.pid = pid
        self.returncode = None
        self.terminated = False

    def die(self, code=1):
        self.returncode = code

    def alive(self):
        return self.returncode is None

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.terminated = True
        self.returncode = -9


class _FakeBackend:
    def __init__(self):
        self.spawned = []  # (argv, env)
        self.procs = []

    async def spawn(self, argv, env=None, hidden=False):
        proc = _FakeProc(pid=1000 + len(self.procs))
        self.spawned.append((argv, env))
        self.procs.append(proc)
        return proc


class _FakeEnv:
    supported = True
    reason = ""
    label = "x11"
    ozone_x11 = False

    def __init__(self, outputs=None):
        self.backend = _FakeBackend()
        self.outputs = outputs if outputs is not None else []
        self.err = ""
        self.console = True
        self.raise_calls = []
        self.raise_result = True

    def launch_env(self):
        return None

    async def enumerate(self):
        return list(self.outputs), self.err

    def console_user_available(self):
        return self.console

    def window_rect_for_pid(self, pid):
        return None

    def raise_window_for_pid(self, pid):
        self.raise_calls.append(pid)
        return self.raise_result


class _DroppedTask:
    """Stands in for the watch task; tests drive reconcile() directly."""

    def done(self):
        return True


def _drop_task(coro):
    coro.close()
    return _DroppedTask()


_OUT_A = {"id": "HDMI-A-2", "name": "Projector", "x": 1920, "y": 0,
          "width": 1920, "height": 1080, "primary": False}
_OUT_PRIMARY = {"id": "HDMI-A-1", "name": "Console", "x": 0, "y": 0,
                "width": 1920, "height": 1080, "primary": True}


def _manager(tmp_path, outputs=None, resolver=None):
    clock = {"t": 100.0}
    env = _FakeEnv(outputs=outputs)
    manager = kiosk.KioskManager(
        environment=env,
        data_dir=tmp_path,
        log=lambda *a, **k: None,
        task_factory=_drop_task,
        browser_resolver=resolver or (lambda: ("/usr/bin/chromium", "chromium")),
        monotonic=lambda: clock["t"],
    )
    return manager, env, clock


_SPEC = {"main": {"output": "HDMI-A-2", "url": "http://127.0.0.1:8080/present/display/main?key=k"}}


def test_manager_launches_on_present_output(tmp_path):
    async def run():
        manager, env, clock = _manager(tmp_path, outputs=[_OUT_PRIMARY, _OUT_A])
        await manager.sync(_SPEC)
        await manager.reconcile()
        assert len(env.backend.spawned) == 1
        argv, _ = env.backend.spawned[0]
        assert "--window-position=1920,0" in argv
        assert "--window-size=1920,1080" in argv
        assert argv[-1].endswith("?key=k")
        assert manager.state_for("main") == "starting"
        # Survives the quick-exit window -> running.
        clock["t"] += 15
        await manager.reconcile()
        assert manager.state_for("main") == "running"
        # The topology snapshot answers the view helpers.
        assert manager.output_name("HDMI-A-2") == "Projector"
        assert manager.output_connected("HDMI-A-2") is True
        assert manager.output_connected("DP-9") is False

    asyncio.run(run())


def test_manager_kills_on_unplug_and_relaunches_at_new_coords(tmp_path):
    async def run():
        manager, env, clock = _manager(tmp_path, outputs=[_OUT_A])
        await manager.sync(_SPEC)
        await manager.reconcile()
        proc = env.backend.procs[0]
        clock["t"] += 15
        await manager.reconcile()
        assert manager.state_for("main") == "running"

        # Unplug: the window must die NOW, not migrate to another screen.
        env.outputs = []
        await manager.reconcile()
        assert proc.terminated
        assert manager.state_for("main") == "waiting_for_output"

        # Replug at a different position: relaunch at fresh coordinates.
        env.outputs = [{**_OUT_A, "x": 3840, "y": 200}]
        clock["t"] += 5
        await manager.reconcile()
        assert len(env.backend.spawned) == 2
        argv, _ = env.backend.spawned[1]
        assert "--window-position=3840,200" in argv

    asyncio.run(run())


def test_manager_relaunches_when_output_geometry_changes(tmp_path):
    async def run():
        manager, env, clock = _manager(tmp_path, outputs=[_OUT_A])
        await manager.sync(_SPEC)
        await manager.reconcile()
        first = env.backend.procs[0]

        # Resolution change on the same output while the kiosk is up.
        env.outputs = [{**_OUT_A, "width": 3840, "height": 2160}]
        await manager.reconcile()
        assert first.terminated
        assert len(env.backend.spawned) == 2
        argv, _ = env.backend.spawned[1]
        assert "--window-size=3840,2160" in argv

    asyncio.run(run())


def test_manager_crash_backoff_then_circuit_breaker(tmp_path):
    async def run():
        manager, env, clock = _manager(tmp_path, outputs=[_OUT_A])
        await manager.sync(_SPEC)
        await manager.reconcile()

        # Crash: no relaunch inside the backoff window.
        env.backend.procs[-1].die(1)
        await manager.reconcile()
        assert len(env.backend.spawned) == 1
        clock["t"] += 3  # first backoff is 2 s
        await manager.reconcile()
        assert len(env.backend.spawned) == 2

        # A crash burst trips the breaker: state error, no more launches.
        for _ in range(10):
            env.backend.procs[-1].die(1)
            clock["t"] += 20
            await manager.reconcile()
            clock["t"] += 20
            await manager.reconcile()
        assert manager.state_for("main") == "error"
        launches = len(env.backend.spawned)
        clock["t"] += 500
        await manager.reconcile()
        assert len(env.backend.spawned) == launches

        # A config change (new URL) is a fresh start: breaker resets.
        await manager.sync({"main": {"output": "HDMI-A-2", "url": "http://new"}})
        await manager.reconcile()
        assert len(env.backend.spawned) == launches + 1

    asyncio.run(run())


def test_manager_quick_exit_counts_as_failure(tmp_path):
    async def run():
        manager, env, clock = _manager(tmp_path, outputs=[_OUT_A])
        await manager.sync(_SPEC)
        await manager.reconcile()
        # Near-instant exit = profile handoff / broken launch.
        env.backend.procs[-1].die(0)
        clock["t"] += 1
        await manager.reconcile()
        assert manager.state_for("main") in ("starting", "error")
        # Backoff applies: nothing relaunches this tick.
        assert len(env.backend.spawned) == 1

    asyncio.run(run())


def test_manager_waiting_for_signin(tmp_path):
    async def run():
        manager, env, clock = _manager(tmp_path, outputs=[_OUT_A])
        env.console = False  # service mode, nobody at the console
        await manager.sync(_SPEC)
        await manager.reconcile()
        assert env.backend.spawned == []
        assert manager.state_for("main") == "waiting_for_signin"

        env.console = True  # user signs in -> kiosk comes up
        await manager.reconcile()
        assert len(env.backend.spawned) == 1

    asyncio.run(run())


def test_manager_enumeration_error_is_error_state(tmp_path):
    async def run():
        manager, env, clock = _manager(tmp_path, outputs=[])
        env.err = "this desktop doesn't support output enumeration"
        await manager.sync(_SPEC)
        await manager.reconcile()
        assert env.backend.spawned == []
        assert manager.state_for("main") == "error"

    asyncio.run(run())


def test_manager_sync_changes_relaunch_and_teardown(tmp_path):
    async def run():
        manager, env, clock = _manager(tmp_path, outputs=[_OUT_A, _OUT_PRIMARY])
        await manager.sync(_SPEC)
        await manager.reconcile()
        first = env.backend.procs[0]

        # URL change (key regenerated): relaunch with the new URL.
        await manager.sync({"main": {"output": "HDMI-A-2", "url": "http://new-url"}})
        assert first.terminated
        await manager.reconcile()
        assert env.backend.spawned[-1][0][-1] == "http://new-url"

        # Removal (display deleted / local_output cleared): teardown.
        second = env.backend.procs[-1]
        await manager.sync({})
        assert second.terminated
        assert manager.state_for("main") == "stopped"

    asyncio.run(run())


def test_manager_no_browser_is_error_not_crash_loop(tmp_path):
    async def run():
        manager, env, clock = _manager(
            tmp_path, outputs=[_OUT_A],
            resolver=lambda: (None, "no Chromium or Chrome was found on PATH"),
        )
        await manager.sync(_SPEC)
        await manager.reconcile()
        assert env.backend.spawned == []
        assert manager.state_for("main") == "error"

    asyncio.run(run())


def test_manager_stop_kills_everything(tmp_path):
    async def run():
        manager, env, clock = _manager(tmp_path, outputs=[_OUT_A])
        await manager.sync(_SPEC)
        await manager.reconcile()
        proc = env.backend.procs[0]
        await manager.stop()
        assert proc.terminated
        assert manager.state_for("main") == "stopped"

    asyncio.run(run())


def test_manager_unsupported_environment(tmp_path):
    async def run():
        manager, env, clock = _manager(tmp_path)
        env.supported = False
        env.reason = "no graphical session was found on this server"
        await manager.sync(_SPEC)
        await manager.reconcile()
        assert manager.state_for("main") == "unsupported"
        described = await manager.describe_outputs()
        assert described["supported"] is False
        assert "graphical session" in described["reason"]
        assert described["outputs"] == []

    asyncio.run(run())


def test_describe_outputs_marks_in_use(tmp_path):
    async def run():
        manager, env, clock = _manager(tmp_path, outputs=[_OUT_PRIMARY, _OUT_A])
        described = await manager.describe_outputs(in_use={"HDMI-A-2": "main"})
        assert described["supported"] is True
        by_id = {o["id"]: o for o in described["outputs"]}
        assert by_id["HDMI-A-2"]["in_use_by"] == "main"
        assert by_id["HDMI-A-1"]["in_use_by"] == ""
        assert by_id["HDMI-A-1"]["primary"] is True

    asyncio.run(run())


def test_remove_profile_best_effort(tmp_path):
    profile = tmp_path / "kiosk" / "main"
    profile.mkdir(parents=True)
    (profile / "Preferences").write_text("{}")
    kiosk.remove_profile(tmp_path, "main")
    assert not profile.exists()
    # Nonexistent is fine too.
    kiosk.remove_profile(tmp_path, "never-existed")


def test_manager_sync_serializes_with_inflight_tick(tmp_path):
    """A config change while a tick is mid-launch must not orphan the
    launching browser: without the lock, the tick's launch lands in a record
    sync() just retired — an untracked process that keeps the profile dir,
    so every relaunch hands off to it and instantly exits."""
    async def run():
        manager, env, clock = _manager(tmp_path, outputs=[_OUT_A])
        await manager.sync(_SPEC)

        release = asyncio.Event()
        real_spawn = env.backend.spawn

        async def slow_spawn(argv, env=None, hidden=False):
            await release.wait()
            return await real_spawn(argv, env=env, hidden=hidden)

        env.backend.spawn = slow_spawn
        tick = asyncio.create_task(manager.reconcile())
        await asyncio.sleep(0.05)  # tick is now blocked inside the launch
        sync_task = asyncio.create_task(
            manager.sync({"main": {"output": "HDMI-A-2", "url": "http://new"}})
        )
        await asyncio.sleep(0.05)
        release.set()
        await tick
        await sync_task
        env.backend.spawn = real_spawn
        await manager.reconcile()  # launches the new spec

        alive = [p for p in env.backend.procs if p.alive()]
        assert len(alive) == 1  # exactly one browser, no orphan
        assert env.backend.spawned[-1][0][-1] == "http://new"

    asyncio.run(run())


def test_manager_raises_window_after_launch(tmp_path):
    """The kiosk window is brought above other windows once it exists —
    retried each tick until the environment reports success, once per
    launch."""
    async def run():
        manager, env, clock = _manager(tmp_path, outputs=[_OUT_A])
        env.raise_result = False  # window not up yet
        await manager.sync(_SPEC)
        await manager.reconcile()  # launch
        assert env.raise_calls == []
        await manager.reconcile()  # alive -> raise attempt
        await manager.reconcile()  # still not found -> retried
        assert len(env.raise_calls) == 2
        env.raise_result = True
        await manager.reconcile()  # found + raised
        await manager.reconcile()  # done; no more attempts
        assert len(env.raise_calls) == 3

        # A relaunch raises again: unplug, replug.
        env.outputs = []
        await manager.reconcile()
        env.outputs = [_OUT_A]
        clock["t"] += 10
        await manager.reconcile()  # relaunch
        await manager.reconcile()
        assert len(env.raise_calls) == 4

    asyncio.run(run())
