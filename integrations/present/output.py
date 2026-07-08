"""Per-stream-display output pipeline: one always-on encoder fed by pumps.

A stream display is pulled by a hardware decoder that locks onto the stream's
URL and codec parameters — if the published stream ever ends or renegotiates,
the decoder black-screens and re-acquires. So each stream display gets one
**never-restarted encoder**: an ffmpeg that reads MPEG-TS from stdin and
publishes the fixed output profile (H.264 Constrained Baseline 1920x1080
yuv420p 30 fps, AAC-LC 48 kHz stereo) to the sidecar over RTSP. The controller
holds that stdin open for the encoder's whole life and relays TS bytes into it
from a swappable **pump** process:

- the **idle pump** renders the connect card (lavfi color + drawtext, with the
  space name / join line / code read from small text files, ``reload=1`` so a
  rotated code appears without a restart), and
- the **live pump** decodes the routed presenter's ingest off the sidecar and
  normalizes it to 1080p30 yuv420p + 48 kHz stereo.

Pumps encode an intermediate MPEG-TS of mpeg2video (near-free to encode and
decode, visually transparent at 20 Mb/s for this one hop) plus s302m (lossless
PCM), so the only lossy steps are the presenter's own encode and the final
H.264/AAC. TS is the one container built for mid-stream joins, which is what
makes the swap work:

- **Overlap swap:** the next pump starts in standby with its output discarded
  while the current pump stays on air, absorbing RTSP connect time and the
  wait for the presenter's next keyframe (worst case measured ~10 s on a
  long-GOP publisher; sub-second in production, where the sidecar PLIs the
  browser when a reader connects). Cutover forwards the new pump's bytes from
  a 188-byte TS packet boundary; the old pump is then killed. The reader sees
  one continuous stream — the residual seam cost is a sub-second freeze/blip
  while its decoder waits for the next intermediate I-frame.
- **Timeline continuity:** every pump gets ``-output_ts_offset`` = the
  output's stream position at spawn plus a little slack, so its timestamps
  continue the previous pump's timeline instead of restarting at zero. Both
  the airing pump and the standby pump advance at 1x realtime, so an offset
  computed at spawn stays correct however long standby lasts. Without this the
  seams fall to the demuxer's per-stream discontinuity correction, which
  audibly glitches audio.

If the live pump dies (presenter stopped sharing), the controller cuts back to
the idle pump on its own rather than waiting for the routing poll to notice.
If the encoder itself dies, the stream is gone anyway, so it restarts with
backoff and a circuit breaker (the supervisor pattern) — that is the one
transition a decoder must re-acquire.

No OpenAVC imports — dependencies (spawn, task factory, logging, sidecar URLs,
the ingest-track probe) are injected, keeping this unit-testable against fake
processes.
"""

import asyncio
import os
import sys
import time
from collections import deque
from pathlib import Path

try:
    import encoders
    from sidecar import bind_to_process_lifetime
except ImportError:  # pragma: no cover - package-path import (tests/CI)
    from . import encoders
    from .sidecar import bind_to_process_lifetime


async def _spawn_bound(*args, **kwargs):
    """Default process spawn: exec + bind to this process's lifetime, so a
    hard-killed server never leaves encoders/pumps running (see sidecar.py).
    Injected test spawns bypass this on purpose — binding a fake pid would
    hand some unrelated real process to the kill-on-close job."""
    proc = await asyncio.create_subprocess_exec(*args, **kwargs)
    bind_to_process_lifetime(proc.pid)
    return proc


# ──── Fixed output profile (must never change mid-stream) ────

OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080
OUTPUT_FPS = 30
OUTPUT_GOP = 30  # 1 s keyframe interval keeps decoder join/recover fast
OUTPUT_BITRATE = "6M"
AUDIO_RATE = 48000

# ──── Pump intermediate ────

_PUMP_VIDEO_BITRATE = "20M"
# Short pump GOP bounds the seam cost: a mid-GOP cutover discards until the
# next intermediate I-frame, so 15 frames = at most 0.5 s of freeze.
_PUMP_GOP = 15

# TS packets are 188 bytes; cutover must hand the encoder whole packets so the
# demuxer resyncs instantly instead of hunting for a sync byte.
TS_PACKET = 188
_READ_SIZE = TS_PACKET * 64

# The standby pump's first DTS should land just ahead of the seam.
TS_OFFSET_SLACK = 0.5

# A standby pump that produces nothing in this window is declared unusable
# (spike worst case: 9.6 s waiting out a long-GOP publisher's keyframe).
STANDBY_TIMEOUT = 20.0
# Pause between attempts when a swap fails, so a bad source can't spawn-loop.
RETRY_HOLDOFF = 5.0

# Encoder restart backoff + circuit breaker (supervisor pattern).
_BACKOFF_SCHEDULE = (1.0, 2.0, 5.0, 10.0)
_CIRCUIT_FAILURES = 5
_CIRCUIT_WINDOW = 60.0
_TERMINATE_GRACE = 5.0

# ──── Connect-card look (matches the HTML card: dark + sage) ────

CARD_BG = "0x101512"
_CARD_TEXT = "0xE8EDE9"
_CARD_ACCENT = "0x8AB493"
_CARD_MUTED = "0xADB8B0"

_FONT_CANDIDATES_WIN = (
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
)
_FONT_CANDIDATES_LINUX = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Debian/Ubuntu/Pi/Docker
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",  # Fedora
    "/usr/share/fonts/TTF/DejaVuSans.ttf",  # Arch
    # Liberation/Noto cover Debian-family installs without DejaVu (chromium
    # kiosk images commonly pull Liberation in as a dependency).
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
)


def find_font():
    """A usable system font for the card, or None (no bundled font: licensing)."""
    candidates = _FONT_CANDIDATES_WIN if sys.platform == "win32" else _FONT_CANDIDATES_LINUX
    for path in candidates:
        if Path(path).exists():
            return path
    return None


def lavfi_escape(path):
    """A filesystem path made safe for a filtergraph option value.

    Filtergraph escaping is two-level: ``:`` separates option values (level 1,
    escaped with a backslash), and the graph parser itself consumes one more
    backslash (level 2) — so the argv must carry ``\\\\:`` for a literal colon.
    Forward slashes keep Windows paths out of backslash-escaping entirely.
    """
    return str(path).replace("\\", "/").replace(":", "\\\\:")


class IdleCard:
    """The connect card's dynamic text, as drawtext-readable files.

    One card per space, shared by every stream display's idle pump. The code
    (and join line) files are re-read every frame (``reload=1``), so rotation
    shows up on air without touching the pump. Writes go through a temp file +
    ``os.replace`` so a mid-write frame never reads a torn file.
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.space_file = self.directory / "space.txt"
        self.join_file = self.directory / "join.txt"
        self.code_file = self.directory / "code.txt"

    def write_all(self, space_name, join_line, code):
        self.write_space(space_name)
        self.write_join(join_line)
        self.write_code(code)

    def write_space(self, space_name):
        self._write(self.space_file, space_name)

    def write_join(self, join_line):
        self._write(self.join_file, join_line)

    def write_code(self, code):
        self._write(self.code_file, code)

    @staticmethod
    def _write(path, text):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text or "", encoding="utf-8")
        os.replace(tmp, path)


# ──── ffmpeg argv builders ────


def build_encoder_args(ffmpeg_bin, encoder, publish_url):
    """The never-restarted encoder: MPEG-TS on stdin -> fixed profile -> RTSP."""
    args = [encoders.norm_bin(ffmpeg_bin), "-hide_banner", "-loglevel", "warning"]
    args += encoders.init_flags(encoder)
    args += ["-f", "mpegts", "-i", "pipe:0"]
    args += encoders.filter_flags(encoder)
    args += ["-map", "0:v:0", "-map", "0:a:0"]
    args += encoders.encode_flags(encoder, OUTPUT_BITRATE, OUTPUT_GOP)
    args += ["-maxrate", OUTPUT_BITRATE, "-bufsize", OUTPUT_BITRATE]
    args += ["-c:a", "aac", "-b:a", "128k", "-ar", str(AUDIO_RATE), "-ac", "2"]
    args += ["-f", "rtsp", "-rtsp_transport", "tcp", publish_url]
    return args


def _pump_tail(ts_offset):
    # Shared pump encode + mux leg: the intermediate the encoder demuxes.
    return [
        "-c:v", "mpeg2video", "-b:v", _PUMP_VIDEO_BITRATE, "-g", str(_PUMP_GOP),
        "-c:a", "s302m", "-strict", "experimental",
        "-output_ts_offset", f"{ts_offset:.3f}",
        "-f", "mpegts", "pipe:1",
    ]


def _drawtext(font, *, y, size, color, textfile=None, text=None, reload=False):
    source = (
        f"textfile={lavfi_escape(textfile)}" if textfile is not None
        else f"text={text}"
    )
    opts = [
        f"drawtext={source}",
        f"fontfile={lavfi_escape(font)}",
        f"fontcolor={color}",
        f"fontsize={size}",
        "x=(w-text_w)/2",
        f"y={y}",
    ]
    if reload:
        opts.insert(2, "reload=1")
    return ":".join(opts)


def card_filter(card, font):
    """The idle card's drawtext chain over the background color source."""
    return ",".join([
        _drawtext(font, textfile=card.space_file, reload=True,
                  size=64, color=_CARD_TEXT, y=260),
        _drawtext(font, text="Share your screen at",
                  size=40, color=_CARD_MUTED, y=470),
        _drawtext(font, textfile=card.join_file, reload=True,
                  size=64, color=_CARD_ACCENT, y=540),
        _drawtext(font, text="Join code",
                  size=40, color=_CARD_MUTED, y=710),
        _drawtext(font, textfile=card.code_file, reload=True,
                  size=120, color=_CARD_ACCENT, y=780),
    ])


def build_idle_pump_args(ffmpeg_bin, card, font, ts_offset):
    """The connect-card pump: lavfi color + drawtext + silence -> MPEG-TS."""
    return [
        encoders.norm_bin(ffmpeg_bin), "-hide_banner", "-loglevel", "warning",
        "-re", "-f", "lavfi", "-i",
        f"color=c={CARD_BG}:s={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}:r={OUTPUT_FPS}",
        "-re", "-f", "lavfi", "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo",
        "-map", "0:v", "-map", "1:a",
        "-vf", card_filter(card, font),
        "-af", "aformat=sample_fmts=s16:channel_layouts=stereo",
        *_pump_tail(ts_offset),
    ]


def build_live_pump_args(ffmpeg_bin, ingest_url, has_audio, ts_offset):
    """The presenter pump: sidecar ingest -> normalized 1080p30 -> MPEG-TS.

    The input flags stay deliberately light: heavier low-latency tuning
    (probesize/analyzeduration/reorder_queue_size) was measured to drop WebRTC
    video packets and produce choppy output. A presenter with no audio track
    gets mapped silence, so the intermediate's stream layout — which the
    encoder locked onto at its own start — never varies.
    """
    args = [
        encoders.norm_bin(ffmpeg_bin), "-hide_banner", "-loglevel", "warning",
        "-rtsp_transport", "tcp", "-fflags", "nobuffer", "-flags", "low_delay",
        "-i", ingest_url,
    ]
    if not has_audio:
        args += ["-f", "lavfi", "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"]
    scale = (
        f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color={CARD_BG},"
        f"fps={OUTPUT_FPS},format=yuv420p"
    )
    if has_audio:
        resample = (
            f"aresample=async=1:out_sample_rate={AUDIO_RATE},"
            "aformat=sample_fmts=s16:channel_layouts=stereo"
        )
        args += ["-map", "0:v:0", "-map", "0:a:0", "-vf", scale, "-af", resample]
    else:
        args += [
            "-map", "0:v:0", "-map", "1:a:0", "-vf", scale,
            "-af", "aformat=sample_fmts=s16:channel_layouts=stereo",
        ]
    return args + _pump_tail(ts_offset)


# Sidecar track names that carry audio (MediaMTX /v3/paths API). Anything
# unrecognized is treated as video-only — mapping silence when real audio
# exists loses sound, but mapping a nonexistent audio stream kills the pump.
_AUDIO_TRACKS = {
    "opus", "aac", "mpeg-4 audio", "mpeg-1/2 audio", "g711", "g722",
    "lpcm", "vorbis", "speex", "ac-3",
}


def tracks_have_audio(tracks):
    return any((t or "").strip().lower() in _AUDIO_TRACKS for t in (tracks or []))


# ──── Pump ────


class _Pump:
    """One spawned pump process and its read loop.

    Starts in standby: output is read and discarded (counted, so cutover can
    align to a TS packet boundary). ``begin_relay`` flips it on air; from then
    every chunk is written to the encoder's stdin.
    """

    def __init__(self, args, *, spawn, make_task, write, log, name, on_end):
        self._args = args
        self._spawn = spawn
        self._make_task = make_task
        self._write = write
        self._log = log
        self.name = name
        self._on_end = on_end

        self.proc = None
        self.produced = False  # produced at least one output byte
        self.first_output = asyncio.Event()
        self.ended = asyncio.Event()
        self._relay = False
        self._seen = 0  # bytes discarded during standby
        self._skip = 0  # bytes still to drop after cutover, for alignment
        self._stopping = False

    async def start(self):
        self.proc = await self._spawn(
            *self._args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._make_task(self._run())
        self._make_task(_drain(self.proc.stderr, self._log, self.name))

    def begin_relay(self):
        # Called between reads (single reader task), so _seen is settled.
        self._skip = (TS_PACKET - self._seen % TS_PACKET) % TS_PACKET
        self._relay = True

    def end_relay(self):
        self._relay = False

    async def stop(self):
        self._stopping = True
        self._relay = False
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            except Exception:  # pragma: no cover - platform-specific
                pass
        await self.ended.wait()

    async def _run(self):
        proc = self.proc
        try:
            while True:
                chunk = await proc.stdout.read(_READ_SIZE)
                if not chunk:
                    break
                if not self.produced:
                    self.produced = True
                    self.first_output.set()
                if self._relay:
                    if self._skip:
                        take = min(self._skip, len(chunk))
                        self._skip -= take
                        chunk = chunk[take:]
                        if not chunk:
                            continue
                    try:
                        await self._write(chunk)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Encoder pipe gone; the encoder monitor owns recovery.
                        break
                else:
                    self._seen += len(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            _log(self._log, f"{self.name}: read loop error: {e}", "warning")
        finally:
            self.ended.set()
            self.first_output.set()  # unblock any standby waiter
            if not self._stopping:
                self._on_end(self)


# ──── Controller ────


class OutputController:
    """The per-stream-display state machine: encoder + pump swaps.

    ``show(source)`` requests what should be on air ("" = the connect card);
    a reconcile task performs the swaps, so callers never block and a newer
    request supersedes an in-flight one. ``state`` is what is actually on air:
    ``starting`` (no pump relaying yet), ``idle``, ``live``, ``error``
    (encoder down), ``stopped``.
    """

    def __init__(self, *, display_id, ffmpeg_bin, encoder, publish_url,
                 ingest_url_for, tracks_for, card, font, log=None,
                 task_factory=None, spawn=None):
        self.display_id = display_id
        self._ffmpeg = ffmpeg_bin
        self._encoder_name = encoder
        self._publish_url = publish_url
        self._ingest_url_for = ingest_url_for
        self._tracks_for = tracks_for
        self._card = card
        self._font = font
        self._log_fn = log
        self._make_task = task_factory or (lambda coro: asyncio.create_task(coro))
        self._spawn = spawn or _spawn_bound

        self.state = "stopped"
        self._stopping = False
        self._target = ""  # what should be on air ("" = card)
        self._current = None  # what is on air (None = nothing relaying yet)
        self._wake = asyncio.Event()

        self._encoder = None
        self._encoder_epoch = 0.0
        self._active = None  # the relaying _Pump
        self._standby = None
        self._failures = deque()
        self._backoff_index = 0

    # ── Public surface ──

    def show(self, source):
        """Request a source on air: "" for the card, else a presenter name."""
        source = source or ""
        if source == self._target:
            return
        self._target = source
        self._wake.set()

    async def start(self):
        self._stopping = False
        self.state = "starting"
        await self._spawn_encoder()
        self._make_task(self._encoder_loop())
        self._make_task(self._reconcile_loop())
        self._wake.set()
        self._log(f"output encoder started for display '{self.display_id}'")

    async def stop(self):
        self._stopping = True
        self._wake.set()
        await self._kill_pumps()
        proc = self._encoder
        if proc is not None and proc.returncode is None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            except Exception:  # pragma: no cover - platform-specific
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE)
            except asyncio.TimeoutError:
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
        self.state = "stopped"
        self._log(f"output encoder stopped for display '{self.display_id}'")

    # ── Encoder lifecycle ──

    async def _spawn_encoder(self):
        args = build_encoder_args(self._ffmpeg, self._encoder_name, self._publish_url)
        self._encoder = await self._spawn(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._encoder_epoch = time.monotonic()
        self._make_task(_drain(self._encoder.stderr, self._log_fn,
                               f"encoder/{self.display_id}"))

    async def _encoder_loop(self):
        """Supervise the encoder: restart with backoff, circuit-break on a
        crash burst. An encoder restart is the one transition where the
        published stream ends and a locked decoder must re-acquire."""
        while not self._stopping:
            proc = self._encoder
            if proc is None:
                return
            rc = await proc.wait()
            if self._stopping:
                return

            self._log(
                f"output encoder for '{self.display_id}' exited unexpectedly "
                f"(code {rc})", "warning",
            )
            await self._kill_pumps()
            self._current = None
            self.state = "error"

            now = time.monotonic()
            if now - self._encoder_epoch > _CIRCUIT_WINDOW:
                self._failures.clear()
                self._backoff_index = 0
            self._failures.append(now)
            while self._failures and now - self._failures[0] > _CIRCUIT_WINDOW:
                self._failures.popleft()
            if len(self._failures) >= _CIRCUIT_FAILURES:
                self._log(
                    f"output encoder for '{self.display_id}' crashed "
                    f"{_CIRCUIT_FAILURES} times within {int(_CIRCUIT_WINDOW)}s; "
                    "not restarting", "error",
                )
                return

            delay = _BACKOFF_SCHEDULE[min(self._backoff_index, len(_BACKOFF_SCHEDULE) - 1)]
            self._backoff_index += 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if self._stopping:
                return
            try:
                await self._spawn_encoder()
            except OSError as e:
                self._log(f"failed to respawn output encoder: {e}", "error")
                continue  # the dead proc's wait() returns at once; counts again
            self.state = "starting"
            self._wake.set()

    async def _write_to_encoder(self, data):
        stdin = self._encoder.stdin
        stdin.write(data)
        await stdin.drain()

    # ── Pump swaps ──

    async def _reconcile_loop(self):
        while not self._stopping:
            await self._wake.wait()
            self._wake.clear()
            while (
                not self._stopping
                and self._encoder is not None
                and self._encoder.returncode is None
                and self._current != self._target
            ):
                target = self._target
                ok = await self._swap_to(target)
                if not ok and not self._stopping and self._target == target:
                    # Same target still requested: hold off, but let a newer
                    # request (or stop) interrupt the wait.
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=RETRY_HOLDOFF)
                        self._wake.clear()
                    except asyncio.TimeoutError:
                        pass

    async def _swap_to(self, source):
        offset = (time.monotonic() - self._encoder_epoch) + TS_OFFSET_SLACK
        if source:
            tracks = await self._tracks_for(source)
            args = build_live_pump_args(
                self._ffmpeg, self._ingest_url_for(source),
                tracks_have_audio(tracks), offset,
            )
            name = f"pump-live/{self.display_id}"
        else:
            args = build_idle_pump_args(self._ffmpeg, self._card, self._font, offset)
            name = f"pump-idle/{self.display_id}"

        pump = _Pump(
            args, spawn=self._spawn, make_task=self._make_task,
            write=self._write_to_encoder, log=self._log_fn, name=name,
            on_end=self._pump_ended,
        )
        try:
            await pump.start()
        except OSError as e:
            self._log(f"{name}: failed to start: {e}", "error")
            return False

        # Standby: the pump connects/decodes with its output discarded while
        # the current pump stays on air, so the card never goes dark waiting
        # for a slow source.
        self._standby = pump
        try:
            try:
                await asyncio.wait_for(pump.first_output.wait(), timeout=STANDBY_TIMEOUT)
            except asyncio.TimeoutError:
                self._log(f"{name}: no output within {int(STANDBY_TIMEOUT)}s; "
                          "keeping the current source on air", "warning")
                await pump.stop()
                return False
            if not pump.produced or pump.ended.is_set():
                # Exited before producing anything usable (e.g. the presenter
                # vanished between routing and connect).
                self._log(f"{name}: ended before producing output", "warning")
                await pump.stop()
                return False
        finally:
            self._standby = None

        # Cutover: stop forwarding the old pump, then put the new one on air
        # from a TS packet boundary. Order matters — exactly one pump may
        # write to the encoder at any moment.
        old = self._active
        if old is not None:
            old.end_relay()
        pump.begin_relay()
        self._active = pump
        self._current = source
        self.state = "live" if source else "idle"
        if old is not None:
            await old.stop()
        return True

    def _pump_ended(self, pump):
        """Unexpected pump end (not via stop()). Runs inside the pump task."""
        if self._stopping or pump is not self._active:
            return
        self._active = None
        self._current = None
        if pump.name.startswith("pump-live") and self._target:
            # The presenter's stream ended: cut back to the card now instead
            # of waiting for the routing poll to notice they left.
            self._target = ""
        self._wake.set()

    async def _kill_pumps(self):
        for pump in (self._active, self._standby):
            if pump is not None:
                await pump.stop()
        self._active = None
        self._standby = None

    def _log(self, msg, level="info"):
        _log(self._log_fn, msg, level)


async def _drain(stream, log, name):
    """Keep a child's stderr pipe empty (an undrained pipe deadlocks it)."""
    if stream is None:
        return
    while True:
        try:
            line = await stream.readline()
        except asyncio.CancelledError:
            raise
        except (asyncio.LimitOverrunError, ValueError):
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
            _log(log, f"[{name}] {text}", "debug")


def _log(log, msg, level="info"):
    if log:
        try:
            log(msg, level)
        except Exception:
            pass
