"""Subprocess supervisor for the bundled MediaMTX sidecar.

Self-contained on purpose: it spawns a child process, drains its stdout and
stderr concurrently (an undrained OS pipe deadlocks the child once ~64 KB of
output buffers up), watches for exit, and restarts with exponential backoff. A
burst of crashes trips a circuit breaker so a broken binary can't restart-loop
forever.

Every spawned child is also bound to this process's lifetime with a Windows
kill-on-close job object (``bind_to_process_lifetime``, exported for the
plugin's other child processes). On POSIX a dead service's children are
reaped by systemd/the session; on Windows nothing kills children when the
server dies hard (console window closed, ``taskkill /f``) — the orphan keeps
running, keeps its ports, and the next start crash-loops against it while
the half-dead orphan keeps half-serving traffic.

No OpenAVC imports — the host plugin injects logging and notification callbacks,
which keeps this module reusable and unit-testable against a trivial dummy
process.
"""

import asyncio
import sys
import time
from collections import deque
from typing import Awaitable, Callable, Optional, Sequence, Union

# Restart backoff schedule in seconds; the final value repeats for later retries.
_BACKOFF_SCHEDULE = (1.0, 2.0, 5.0, 10.0)
# Circuit breaker: this many crashes inside the rolling window stops restarts.
_CIRCUIT_FAILURES = 5
_CIRCUIT_WINDOW = 60.0
# Grace period between terminate (SIGTERM on POSIX) and kill (SIGKILL) on stop.
_TERMINATE_GRACE = 5.0

LogFn = Callable[[str, str], None]
StatusFn = Callable[[str], Union[Awaitable[None], None]]
ReasonFn = Callable[[str], Union[Awaitable[None], None]]
TaskFactory = Callable[..., "asyncio.Task"]


async def _maybe_await(result) -> None:
    if asyncio.iscoroutine(result):
        await result


# ──── Child lifetime binding (Windows job object) ────

# The job handle is created once and deliberately never closed: the kernel
# closes it when this process exits, which is what kills the members.
_job = None


def bind_to_process_lifetime(pid) -> None:
    """Make the OS kill ``pid`` when this process exits (Windows; no-op
    elsewhere). Call it right after spawning a real child — never with a
    fake/test pid, since the kernel will kill whatever owns it."""
    if sys.platform != "win32":
        return
    job = _kill_on_close_job()
    if job is None:
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
    handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return
    try:
        kernel32.AssignProcessToJobObject(job, handle)
    finally:
        kernel32.CloseHandle(handle)


def _kill_on_close_job():  # pragma: no cover - Windows kernel plumbing
    global _job
    if _job is not None:
        return _job or None  # 0 = tried and failed; stay a no-op
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9

    kernel32 = ctypes.windll.kernel32
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        _job = 0
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info),
    )
    if not ok:
        kernel32.CloseHandle(job)
        _job = 0
        return None
    _job = job
    return job


class SidecarSupervisor:
    """Spawn, drain, monitor, and auto-restart a single child process."""

    def __init__(
        self,
        cmd: Sequence[str],
        *,
        name: str = "sidecar",
        log: Optional[LogFn] = None,
        on_status: Optional[StatusFn] = None,
        on_circuit_break: Optional[ReasonFn] = None,
        task_factory: Optional[TaskFactory] = None,
    ):
        self._cmd = list(cmd)
        self._name = name
        self._log = log
        self._on_status = on_status
        self._on_circuit_break = on_circuit_break
        # Default to asyncio.create_task; the plugin passes api.create_task so
        # drain/monitor tasks are tracked and auto-cancelled if the plugin dies
        # without a clean stop().
        self._make_task: TaskFactory = task_factory or (lambda coro: asyncio.create_task(coro))

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._monitor: Optional[asyncio.Task] = None
        self._drains: list[asyncio.Task] = []
        self._stopping = False
        self._failures: deque[float] = deque()
        self._backoff_index = 0
        self._spawn_time = 0.0

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    async def start(self) -> None:
        """Spawn the process and begin supervising it."""
        self._stopping = False
        await self._spawn()
        self._monitor = self._make_task(self._monitor_loop())
        await self._status("running")

    async def stop(self) -> None:
        """Stop supervising, then terminate (and if needed kill) the process."""
        self._stopping = True
        if self._monitor is not None and not self._monitor.done():
            self._monitor.cancel()
            try:
                await self._monitor
            except BaseException:  # CancelledError + any teardown error
                pass
        self._monitor = None

        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            except Exception as e:  # pragma: no cover - platform-specific
                self._emit_log(f"error terminating {self._name}: {e}", "warning")
            try:
                await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE)
            except asyncio.TimeoutError:
                self._emit_log(f"{self._name} did not exit in {_TERMINATE_GRACE:.0f}s; killing", "warning")
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                except Exception:  # pragma: no cover
                    pass
                try:
                    await proc.wait()
                except Exception:  # pragma: no cover
                    pass

        await self._cancel_drains()
        self._proc = None
        self._emit_log(f"{self._name} stopped", "info")

    # ──── Internals ────

    async def _spawn(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        bind_to_process_lifetime(self._proc.pid)
        self._spawn_time = time.monotonic()
        self._drains = [
            self._make_task(self._drain(self._proc.stdout, "out")),
            self._make_task(self._drain(self._proc.stderr, "err")),
        ]
        self._emit_log(f"{self._name} started (pid {self._proc.pid})", "info")

    async def _drain(self, stream: Optional[asyncio.StreamReader], label: str) -> None:
        if stream is None:
            return
        while True:
            try:
                line = await stream.readline()
            except asyncio.CancelledError:
                raise
            except (asyncio.LimitOverrunError, ValueError):
                # Overlong line with no newline (rare): discard a chunk, keep going.
                try:
                    await stream.read(65536)
                except Exception:
                    return
                continue
            except Exception:
                return
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self._emit_log(f"[{self._name}/{label}] {text}", "debug")

    async def _monitor_loop(self) -> None:
        while True:
            proc = self._proc
            if proc is None:
                return
            rc = await proc.wait()
            # stop() sets _stopping before terminating, so an intentional exit
            # ends the loop here without counting as a failure.
            if self._stopping:
                return

            await self._cancel_drains()
            self._emit_log(f"{self._name} exited unexpectedly (code {rc})", "warning")

            now = time.monotonic()
            # A long, healthy run clears the crash trail so a single late crash
            # restarts fast instead of inheriting an old backoff.
            if now - self._spawn_time > _CIRCUIT_WINDOW:
                self._failures.clear()
                self._backoff_index = 0
            self._failures.append(now)
            while self._failures and now - self._failures[0] > _CIRCUIT_WINDOW:
                self._failures.popleft()

            if len(self._failures) >= _CIRCUIT_FAILURES:
                reason = (
                    f"{self._name} crashed {_CIRCUIT_FAILURES} times within "
                    f"{int(_CIRCUIT_WINDOW)}s; not restarting"
                )
                self._emit_log(reason, "error")
                await self._status("failed")
                if self._on_circuit_break is not None:
                    await _maybe_await(self._on_circuit_break(reason))
                return

            delay = _BACKOFF_SCHEDULE[min(self._backoff_index, len(_BACKOFF_SCHEDULE) - 1)]
            self._backoff_index += 1
            await self._status("restarting")
            self._emit_log(f"restarting {self._name} in {delay:.0f}s", "warning")
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if self._stopping:
                return
            try:
                await self._spawn()
            except Exception as e:
                self._emit_log(f"failed to respawn {self._name}: {e}", "error")
                continue
            await self._status("running")

    async def _cancel_drains(self) -> None:
        for t in self._drains:
            if not t.done():
                t.cancel()
        for t in self._drains:
            try:
                await t
            except BaseException:  # CancelledError or a late drain error
                pass
        self._drains = []

    async def _status(self, status: str) -> None:
        if self._on_status is not None:
            await _maybe_await(self._on_status(status))

    def _emit_log(self, msg: str, level: str = "info") -> None:
        if self._log is not None:
            try:
                self._log(msg, level)
            except Exception:
                pass
