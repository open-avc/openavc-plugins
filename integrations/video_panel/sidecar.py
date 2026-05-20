"""Subprocess supervisor for the bundled MediaMTX sidecar.

Self-contained on purpose: it spawns a child process, drains its stdout and
stderr concurrently (an undrained OS pipe deadlocks the child once ~64 KB of
output buffers up), watches for exit, and restarts with exponential backoff. A
burst of crashes trips a circuit breaker so a broken binary can't restart-loop
forever.

No OpenAVC imports — the host plugin injects logging and notification callbacks,
which keeps this module reusable and unit-testable against a trivial dummy
process.
"""

import asyncio
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
