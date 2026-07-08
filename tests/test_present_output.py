"""
Tests for the Present plugin's stream-display output pipeline (output.py).

The OutputController is exercised against fake processes: the encoder and the
pumps are recorded spawns whose stdout is a real asyncio.StreamReader the test
feeds, and whose stdin records what the relay forwarded. Covered: the argv
builders (fixed profile, pump intermediate, drawtext escaping and card
filter), the idle->live->idle swap with TS packet alignment across cutover,
presenter-loss auto-recovery, the standby timeout, encoder crash restart with
backoff, the circuit breaker, and stop().

Real audio/video through these argvs is bench work, not CI work — a separate
end-to-end pass against the shipping MediaMTX + ffmpeg binaries validated the
same builders on real streams.
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))

from integrations.present import output  # noqa: E402


# ──── Fakes ────


class _FakeStdin:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, b):
        if self.closed:
            raise ConnectionResetError("stdin closed")
        self.data += b

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, args):
        self.args = args
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode = None
        self.pid = 4242
        self._exit = asyncio.Event()

    async def wait(self):
        await self._exit.wait()
        return self.returncode

    def kill(self):
        if self.returncode is None:
            self.returncode = -9
        self.stdout.feed_eof()
        self.stdin.closed = True
        self._exit.set()

    terminate = kill

    def crash(self, code=1):
        """Simulate the process dying on its own."""
        self.returncode = code
        self.stdout.feed_eof()
        self.stdin.closed = True
        self._exit.set()


class _Spawner:
    def __init__(self):
        self.procs = []

    async def __call__(self, *args, stdin=None, stdout=None, stderr=None):
        proc = _FakeProc(args)
        self.procs.append(proc)
        return proc

    # Classification by argv shape — the same signals a human would read.
    def encoders(self):
        return [p for p in self.procs if "pipe:0" in p.args]

    def pumps(self):
        return [p for p in self.procs if p.args[-1] == "pipe:1"]

    def idle_pumps(self):
        return [p for p in self.pumps() if any("drawtext" in a for a in p.args)]

    def live_pumps(self):
        return [p for p in self.pumps() if any("/in/" in a for a in p.args)]


async def _until(predicate, timeout=3.0, message="condition not met in time"):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        await asyncio.sleep(0.01)


def _controller(tmp_path, spawner, tracks=None):
    card = output.IdleCard(tmp_path / "card")
    card.write_all("Bench Space", "192.0.2.10:8080/present", "1234")
    known = dict(tracks or {})

    async def tracks_for(presenter):
        return known.get(presenter)

    return output.OutputController(
        display_id="tv",
        ffmpeg_bin="ffmpeg.exe",
        encoder="libopenh264",
        publish_url="rtsp://127.0.0.1:8554/out/tv-k3y",
        ingest_url_for=lambda p: f"rtsp://openavc:pw@127.0.0.1:8554/in/{p}",
        tracks_for=tracks_for,
        card=card,
        font="C:/Windows/Fonts/segoeui.ttf",
        spawn=spawner,
    )


# ──── Argv builders ────


def test_lavfi_escape_two_level():
    # Level 1 escapes ':' for the option parser; level 2 (the graph parser)
    # consumes one backslash — so the argv carries a double backslash.
    assert output.lavfi_escape(r"C:\Fonts\arial.ttf") == "C\\\\:/Fonts/arial.ttf"
    assert output.lavfi_escape("/usr/share/f.ttf") == "/usr/share/f.ttf"


def test_encoder_args_fixed_profile():
    args = output.build_encoder_args("ffmpeg.exe", "libopenh264", "rtsp://x/out/tv-k")
    joined = " ".join(args)
    assert args[0] == "ffmpeg.exe"
    assert "-f mpegts -i pipe:0" in joined
    assert "-map 0:v:0 -map 0:a:0" in joined
    assert "-c:v libopenh264" in joined
    assert "-profile:v constrained_baseline" in joined
    assert "-pix_fmt yuv420p" in joined
    assert f"-g {output.OUTPUT_GOP}" in joined
    assert "-c:a aac" in joined and "-ar 48000 -ac 2" in joined
    assert args[-3:] == ["-f", "rtsp", "rtsp://x/out/tv-k"] or args[-1] == "rtsp://x/out/tv-k"
    assert "-rtsp_transport tcp" in joined


def test_idle_pump_args_card_and_offset(tmp_path):
    card = output.IdleCard(tmp_path / "card")
    args = output.build_idle_pump_args("ffmpeg.exe", card, "C:/W/f.ttf", 12.5)
    joined = " ".join(args)
    assert "-re" in args
    assert f"color=c={output.CARD_BG}:s=1920x1080:r=30" in joined
    assert "anullsrc=r=48000:cl=stereo" in joined
    vf = args[args.index("-vf") + 1]
    # All dynamic text rides in textfiles (no injection surface); the code and
    # join line reload every frame so rotation shows without a pump restart.
    assert vf.count("drawtext=") == 5
    assert vf.count("reload=1") == 3
    assert "space.txt" in vf and "join.txt" in vf and "code.txt" in vf
    assert "fontfile=C\\\\:/W/f.ttf" in vf
    assert "-c:v mpeg2video" in joined and "-g 15" in joined
    assert "-c:a s302m -strict experimental" in joined
    assert "-output_ts_offset 12.500" in joined
    assert args[-1] == "pipe:1"


def test_live_pump_args_audio_and_silence():
    with_audio = output.build_live_pump_args("f", "rtsp://u:p@h/in/alice", True, 3.0)
    joined = " ".join(with_audio)
    assert "-map 0:v:0 -map 0:a:0" in joined
    assert "anullsrc" not in joined
    assert "aresample=async=1" in joined
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in joined
    assert "fps=30,format=yuv420p" in joined
    # The over-tuned low-latency input flags were measured to drop WebRTC
    # packets; only the safe ones belong here.
    assert "-probesize" not in joined and "-analyzeduration" not in joined
    assert "-fflags nobuffer" in joined and "-flags low_delay" in joined

    no_audio = output.build_live_pump_args("f", "rtsp://u:p@h/in/bob", False, 3.0)
    joined = " ".join(no_audio)
    assert "anullsrc=r=48000:cl=stereo" in joined
    assert "-map 0:v:0 -map 1:a:0" in joined


def test_tracks_have_audio():
    assert output.tracks_have_audio(["Opus", "VP8"]) is True
    assert output.tracks_have_audio(["AAC"]) is True
    assert output.tracks_have_audio(["VP8"]) is False
    assert output.tracks_have_audio(["MPEG-1/2 Video"]) is False
    assert output.tracks_have_audio([]) is False
    assert output.tracks_have_audio(None) is False


def test_find_font_walks_candidates(tmp_path, monkeypatch):
    real = tmp_path / "present-test-font.ttf"
    real.write_bytes(b"\0")
    for attr in ("_FONT_CANDIDATES_WIN", "_FONT_CANDIDATES_LINUX"):
        monkeypatch.setattr(output, attr, (str(tmp_path / "missing.ttf"), str(real)))
    assert output.find_font() == str(real)
    for attr in ("_FONT_CANDIDATES_WIN", "_FONT_CANDIDATES_LINUX"):
        monkeypatch.setattr(output, attr, (str(tmp_path / "missing.ttf"),))
    assert output.find_font() is None


# ──── Controller state machine ────


@pytest.mark.asyncio
async def test_start_reaches_idle_and_relays_aligned(tmp_path):
    spawner = _Spawner()
    ctl = _controller(tmp_path, spawner)
    await ctl.start()
    assert ctl.state == "starting"
    await _until(lambda: len(spawner.encoders()) == 1 and len(spawner.idle_pumps()) == 1)
    encoder = spawner.encoders()[0]
    idle = spawner.idle_pumps()[0]

    # First packet arrives during standby -> discarded; cutover follows.
    idle.stdout.feed_data(b"A" * 188)
    await _until(lambda: ctl.state == "idle")
    idle.stdout.feed_data(b"B" * 188)
    await _until(lambda: len(encoder.stdin.data) >= 188)
    # Standby bytes were a whole packet, so relay starts exactly at the next.
    assert bytes(encoder.stdin.data) == b"B" * 188

    await ctl.stop()
    assert ctl.state == "stopped"
    assert encoder.returncode is not None and idle.returncode is not None


@pytest.mark.asyncio
async def test_swap_to_live_aligns_and_kills_old_pump(tmp_path):
    spawner = _Spawner()
    ctl = _controller(tmp_path, spawner)  # no tracks known -> silence mapped
    await ctl.start()
    await _until(lambda: len(spawner.idle_pumps()) == 1)
    encoder, idle = spawner.encoders()[0], spawner.idle_pumps()[0]
    idle.stdout.feed_data(b"A" * 188)
    await _until(lambda: ctl.state == "idle")
    idle.stdout.feed_data(b"B" * 188)
    await _until(lambda: len(encoder.stdin.data) == 188)

    ctl.show("alice")
    await _until(lambda: len(spawner.live_pumps()) == 1)
    live = spawner.live_pumps()[0]
    assert any("/in/alice" in a for a in live.args)
    assert any("anullsrc" in a for a in live.args)  # no audio track known
    offset = float(live.args[live.args.index("-output_ts_offset") + 1])
    assert offset >= output.TS_OFFSET_SLACK

    # 100 standby bytes -> cutover skips 88 more to realign to 188.
    live.stdout.feed_data(b"C" * 100)
    await _until(lambda: ctl.state == "live")
    assert idle.returncode is not None  # old pump killed at cutover
    live.stdout.feed_data(b"D" * 276)
    await _until(lambda: len(encoder.stdin.data) == 188 + 188)
    assert bytes(encoder.stdin.data[188:]) == b"D" * 188

    await ctl.stop()


@pytest.mark.asyncio
async def test_live_pump_with_audio_track(tmp_path):
    spawner = _Spawner()
    ctl = _controller(tmp_path, spawner, tracks={"bob": ["Opus", "VP8"]})
    await ctl.start()
    await _until(lambda: len(spawner.idle_pumps()) == 1)
    spawner.idle_pumps()[0].stdout.feed_data(b"A" * 188)
    await _until(lambda: ctl.state == "idle")

    ctl.show("bob")
    await _until(lambda: len(spawner.live_pumps()) == 1)
    live = spawner.live_pumps()[0]
    joined = " ".join(live.args)
    assert "anullsrc" not in joined
    assert "-map 0:v:0 -map 0:a:0" in joined
    await ctl.stop()


@pytest.mark.asyncio
async def test_presenter_loss_recovers_to_idle(tmp_path):
    spawner = _Spawner()
    ctl = _controller(tmp_path, spawner)
    await ctl.start()
    await _until(lambda: len(spawner.idle_pumps()) == 1)
    spawner.idle_pumps()[0].stdout.feed_data(b"A" * 188)
    await _until(lambda: ctl.state == "idle")

    ctl.show("alice")
    await _until(lambda: len(spawner.live_pumps()) == 1)
    live = spawner.live_pumps()[0]
    live.stdout.feed_data(b"C" * 188)
    await _until(lambda: ctl.state == "live")

    # The presenter stops sharing: the ingest read EOFs. The controller cuts
    # back to the card on its own — no routing poll involved.
    live.crash(0)
    await _until(lambda: len(spawner.idle_pumps()) == 2)
    idle2 = spawner.idle_pumps()[1]
    idle2.stdout.feed_data(b"E" * 188)
    await _until(lambda: ctl.state == "idle")
    await ctl.stop()


@pytest.mark.asyncio
async def test_standby_timeout_keeps_current_source(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "STANDBY_TIMEOUT", 0.15)
    monkeypatch.setattr(output, "RETRY_HOLDOFF", 60.0)  # one attempt only
    spawner = _Spawner()
    ctl = _controller(tmp_path, spawner)
    await ctl.start()
    await _until(lambda: len(spawner.idle_pumps()) == 1)
    idle = spawner.idle_pumps()[0]
    idle.stdout.feed_data(b"A" * 188)
    await _until(lambda: ctl.state == "idle")

    ctl.show("bob")
    await _until(lambda: len(spawner.live_pumps()) == 1)
    live = spawner.live_pumps()[0]
    # Never produces output -> killed at the timeout; the card stays on air.
    await _until(lambda: live.returncode is not None)
    assert ctl.state == "idle"
    assert idle.returncode is None  # still relaying
    ctl.show("")  # settle the target before teardown
    await ctl.stop()


@pytest.mark.asyncio
async def test_encoder_crash_restarts_with_new_pump(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "_BACKOFF_SCHEDULE", (0.05,))
    spawner = _Spawner()
    ctl = _controller(tmp_path, spawner)
    await ctl.start()
    await _until(lambda: len(spawner.idle_pumps()) == 1)
    encoder1, idle1 = spawner.encoders()[0], spawner.idle_pumps()[0]
    idle1.stdout.feed_data(b"A" * 188)
    await _until(lambda: ctl.state == "idle")

    encoder1.crash(1)
    # Old pump dies with the encoder; a fresh encoder + pump pair comes up.
    await _until(lambda: len(spawner.encoders()) == 2)
    await _until(lambda: len(spawner.idle_pumps()) == 2)
    assert idle1.returncode is not None
    encoder2, idle2 = spawner.encoders()[1], spawner.idle_pumps()[1]
    idle2.stdout.feed_data(b"F" * 188)
    await _until(lambda: ctl.state == "idle")
    idle2.stdout.feed_data(b"G" * 188)
    await _until(lambda: len(encoder2.stdin.data) >= 188)
    assert bytes(encoder2.stdin.data) == b"G" * 188
    await ctl.stop()


@pytest.mark.asyncio
async def test_encoder_circuit_breaker(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "_BACKOFF_SCHEDULE", (0.02,))
    monkeypatch.setattr(output, "_CIRCUIT_FAILURES", 2)
    spawner = _Spawner()
    ctl = _controller(tmp_path, spawner)
    await ctl.start()
    await _until(lambda: len(spawner.encoders()) == 1)

    spawner.encoders()[0].crash(1)
    await _until(lambda: len(spawner.encoders()) == 2)
    spawner.encoders()[1].crash(1)
    await _until(lambda: ctl.state == "error")
    # Circuit broken: no third spawn, even after the backoff would have fired.
    await asyncio.sleep(0.2)
    assert len(spawner.encoders()) == 2
    await ctl.stop()
