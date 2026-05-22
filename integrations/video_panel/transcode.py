"""HEVC -> H.264 transcode planning for the Video Panel sidecar.

The bundled ffmpeg is BtbN's LGPL build: no libx264/libx265 (those are GPL), so
the software H.264 encoder is libopenh264 (Cisco, BSD). Hardware H.264 encoders
(NVENC / QSV / AMF / MediaFoundation / VAAPI / V4L2 M2M) are compiled in, but
each only runs when the matching GPU + driver is present. So rather than guess
from the OS, we ask the binary which encoders it has (`ffmpeg -encoders`) and
*test-encode* the platform's candidates to find one that actually works on this
machine, then cache the choice.

v1 pipeline: software HEVC decode + hardware H.264 encode (the expensive leg,
encode, is accelerated; software HEVC decode is fine at 1080p on x86/Windows).
Output is H.264 Constrained Baseline (the only profile every browser reliably
plays), audio dropped (-an).

No OpenAVC imports — pure ffmpeg orchestration, unit-testable on its own. The
command this module emits is consumed by MediaMTX's runOnDemand, which splits it
with POSIX go-shellquote on every platform (including Windows) and then runs it
via exec (no shell). So: the binary path is forward-slashed (backslashes would
be eaten as escapes) and args are go-shellquote-quoted. The command references
only plugin-controlled localhost URLs and validated stream ids -- no user input,
hence no injection and nothing for MediaMTX's per-arg env-expansion to mangle.
"""

import asyncio
import platform
import re
import sys

# Always present in the LGPL build; works on every platform.
SOFTWARE_ENCODER = "libopenh264"

# Conservative defaults tuned for panel display; overridable per call.
DEFAULT_BITRATE = "3M"
DEFAULT_GOP = 60

# VAAPI render node (Linux). The only encoder that needs an explicit device.
VAAPI_DEVICE = "/dev/dri/renderD128"

# Bound the detection test-encodes so a wedged encoder can't hang startup.
_TEST_TIMEOUT = 20.0
_ENCODERS_TIMEOUT = 15.0

# User-facing hardware_accel value -> ffmpeg encoder name.
_EXPLICIT = {
    "qsv": "h264_qsv",
    "nvenc": "h264_nvenc",
    "vaapi": "h264_vaapi",
    "v4l2m2m": "h264_v4l2m2m",
    "amf": "h264_amf",
    "mf": "h264_mf",
    "videotoolbox": "h264_videotoolbox",
}

# Every encoder we know how to drive (for parsing `ffmpeg -encoders`).
_KNOWN_ENCODERS = {SOFTWARE_ENCODER, *_EXPLICIT.values()}


def platform_priority():
    """Hardware encoder candidates for this platform, best first."""
    if sys.platform == "win32":
        # h264_mf (MediaFoundation) is the vendor-agnostic Windows fallback,
        # tried after the vendor-specific encoders that tune latency better.
        return ["h264_qsv", "h264_nvenc", "h264_amf", "h264_mf"]
    if sys.platform == "darwin":
        return ["h264_videotoolbox"]
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64", "armv7l", "armv6l"):
        # Pi -> V4L2 M2M; Jetson -> NVENC.
        return ["h264_v4l2m2m", "h264_nvenc"]
    return ["h264_vaapi", "h264_qsv", "h264_nvenc", "h264_amf"]


# ──── Command construction ────


def _norm_bin(path):
    # MediaMTX splits the runOnDemand string with POSIX go-shellquote on every
    # platform (verified in internal/externalcmd/cmd_win.go, v1.18.2), so a
    # Windows backslash path would be mangled. ffmpeg and MediaMTX both accept
    # forward slashes on Windows.
    return str(path).replace("\\", "/")


def _init_flags(encoder):
    # Global options that must precede -i.
    if encoder == "h264_vaapi":
        return ["-vaapi_device", VAAPI_DEVICE]
    return []


def _filter_flags(encoder):
    # Post-input filter: upload software-decoded frames to the GPU for VAAPI.
    if encoder == "h264_vaapi":
        return ["-vf", "format=nv12,hwupload"]
    return []


def _encode_flags(encoder, bitrate, gop):
    rc = ["-b:v", str(bitrate), "-g", str(gop)]
    if encoder == "libopenh264":
        return ["-c:v", "libopenh264", "-profile:v", "constrained_baseline",
                "-pix_fmt", "yuv420p", *rc]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull",
                "-profile:v", "baseline", *rc]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast",
                "-profile:v", "baseline", *rc]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-usage", "lowlatency",
                "-profile:v", "constrained_baseline", *rc]
    if encoder == "h264_mf":
        # MediaFoundation exposes few knobs; CBR keeps latency predictable.
        return ["-c:v", "h264_mf", "-rate_control", "cbr", *rc]
    if encoder == "h264_vaapi":
        return ["-c:v", "h264_vaapi", "-profile:v", "constrained_baseline", *rc]
    if encoder == "h264_v4l2m2m":
        return ["-c:v", "h264_v4l2m2m", "-pix_fmt", "yuv420p", *rc]
    if encoder == "h264_videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-profile:v", "baseline",
                "-realtime", "1", *rc]
    # Unknown encoder: fall back to a plain software encode so we never emit a
    # bogus -c:v.
    return ["-c:v", SOFTWARE_ENCODER, "-profile:v", "constrained_baseline",
            "-pix_fmt", "yuv420p", *rc]


def build_transcode_args(ffmpeg_bin, encoder, input_url, output_url, *,
                         bitrate=DEFAULT_BITRATE, gop=DEFAULT_GOP, publish="rtsp"):
    """Full ffmpeg argv that reads the raw source, transcodes, and republishes.

    ``publish`` selects the output muxer: ``"rtsp"`` (default) uses
    ``-f rtsp -rtsp_transport tcp`` (the muxer otherwise defaults to UDP, which
    stalls on Windows); ``"rtmp"`` uses ``-f flv`` as the proven fallback.
    """
    args = [_norm_bin(ffmpeg_bin), "-hide_banner", "-loglevel", "warning"]
    args += _init_flags(encoder)
    args += ["-rtsp_transport", "tcp", "-fflags", "nobuffer", "-i", input_url]
    args += _filter_flags(encoder)
    args += _encode_flags(encoder, bitrate, gop)
    args += ["-an"]
    if publish == "rtmp":
        args += ["-f", "flv", output_url]
    else:
        args += ["-f", "rtsp", "-rtsp_transport", "tcp", output_url]
    return args


# Characters that need no quoting under POSIX shell rules (go-shellquote).
_SAFE_ARG = re.compile(r"\A[A-Za-z0-9_@%+=:,./-]+\Z")


def _shquote(arg):
    if arg and _SAFE_ARG.match(arg):
        return arg
    # Single-quote everything else; an embedded ' becomes the '\'' idiom.
    return "'" + arg.replace("'", "'\\''") + "'"


def command_string(args):
    """Join an argv into a go-shellquote-splittable command string for MediaMTX."""
    return " ".join(_shquote(a) for a in args)


def build_transcode_command(ffmpeg_bin, encoder, input_url, output_url, **kw):
    """Convenience: the runOnDemand command string for a transcode path."""
    return command_string(
        build_transcode_args(ffmpeg_bin, encoder, input_url, output_url, **kw)
    )


# ──── Encoder detection ────


async def _run(args, timeout):
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return None, b"", b""
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, b"", b""
    return proc.returncode, out, err


async def list_encoders(ffmpeg_bin):
    """Return the set of known H.264 encoders compiled into this ffmpeg."""
    rc, out, _ = await _run(
        [_norm_bin(ffmpeg_bin), "-hide_banner", "-encoders"], _ENCODERS_TIMEOUT
    )
    found = set()
    if rc == 0:
        for line in out.decode("utf-8", "replace").splitlines():
            # Encoder rows look like: " V....D h264_nvenc  NVIDIA NVENC ..."
            m = re.match(r"\s*[A-Z.]+\s+(\S+)", line)
            if m and m.group(1) in _KNOWN_ENCODERS:
                found.add(m.group(1))
    return found


async def test_encode(ffmpeg_bin, encoder):
    """True if a tiny encode with this encoder actually succeeds on this box."""
    args = [_norm_bin(ffmpeg_bin), "-hide_banner", "-loglevel", "error"]
    args += _init_flags(encoder)
    args += ["-f", "lavfi", "-i", "color=c=black:s=320x180:r=10"]
    args += _filter_flags(encoder)
    args += _encode_flags(encoder, "200k", 10)
    args += ["-frames:v", "5", "-an", "-f", "null", "-"]
    rc, _, _ = await _run(args, _TEST_TIMEOUT)
    return rc == 0


async def select_encoder(ffmpeg_bin, hardware_accel="auto", log=None):
    """Choose the H.264 encoder for a stream's ``hardware_accel`` setting.

    ``none`` -> software. An explicit encoder (``qsv``/``nvenc``/``vaapi``/
    ``v4l2m2m``) is validated with a test-encode and falls back to software with
    a warning if it can't run here. ``auto`` walks the platform candidates and
    returns the first that works, else software.
    """
    ha = (hardware_accel or "auto").strip().lower()
    if ha in ("none", ""):
        return SOFTWARE_ENCODER

    compiled = await list_encoders(ffmpeg_bin)

    if ha != "auto":
        enc = _EXPLICIT.get(ha)
        if enc and enc in compiled and await test_encode(ffmpeg_bin, enc):
            return enc
        _log(log, f"hardware encoder for '{hardware_accel}' is unavailable on this "
                  f"machine; using software ({SOFTWARE_ENCODER})", "warning")
        return SOFTWARE_ENCODER

    for enc in platform_priority():
        if enc in compiled and await test_encode(ffmpeg_bin, enc):
            _log(log, f"selected hardware H.264 encoder: {enc}", "info")
            return enc
    _log(log, f"no hardware H.264 encoder available; using software ({SOFTWARE_ENCODER})", "info")
    return SOFTWARE_ENCODER


def _log(log, msg, level):
    if log:
        try:
            log(msg, level)
        except Exception:
            pass
