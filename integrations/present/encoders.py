"""H.264 encoder selection for the Present output pipeline.

The bundled ffmpeg is BtbN's LGPL build: no libx264/libx265 (those are GPL), so
the software H.264 encoder is libopenh264 (Cisco, BSD). Hardware H.264 encoders
(NVENC / QSV / AMF / MediaFoundation / VAAPI / V4L2 M2M) are compiled in, but
each only runs when the matching GPU + driver is present. So rather than guess
from the OS, we ask the binary which encoders it has (``ffmpeg -encoders``) and
*test-encode* the platform's candidates to find one that actually works on this
machine. libopenh264 only emits Constrained Baseline — exactly the
maximum-decoder-compatibility profile the persistent stream-display output
wants, so the software fallback aligns with the fixed profile for free.

Adapted from the Video Panel plugin's transcode module (the detection half);
kept in sync by hand. No OpenAVC imports — pure ffmpeg orchestration,
unit-testable on its own.
"""

import asyncio
import platform
import re
import sys

# Always present in the LGPL build; works on every platform.
SOFTWARE_ENCODER = "libopenh264"

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


def norm_bin(path):
    # ffmpeg accepts forward slashes on every platform, and forward-slashed
    # paths survive logging/quoting contexts unmangled.
    return str(path).replace("\\", "/")


def init_flags(encoder):
    # Global options that must precede -i.
    if encoder == "h264_vaapi":
        return ["-vaapi_device", VAAPI_DEVICE]
    return []


def filter_flags(encoder):
    # Post-input filter: upload software-decoded frames to the GPU for VAAPI.
    if encoder == "h264_vaapi":
        return ["-vf", "format=nv12,hwupload"]
    return []


def encode_flags(encoder, bitrate, gop):
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
        [norm_bin(ffmpeg_bin), "-hide_banner", "-encoders"], _ENCODERS_TIMEOUT
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
    args = [norm_bin(ffmpeg_bin), "-hide_banner", "-loglevel", "error"]
    args += init_flags(encoder)
    args += ["-f", "lavfi", "-i", "color=c=black:s=320x180:r=10"]
    args += filter_flags(encoder)
    args += encode_flags(encoder, "200k", 10)
    args += ["-frames:v", "5", "-an", "-f", "null", "-"]
    rc, _, _ = await _run(args, _TEST_TIMEOUT)
    return rc == 0


async def select_encoder(ffmpeg_bin, hardware_accel="auto", log=None):
    """Choose the H.264 encoder for a ``hardware_accel`` setting.

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
