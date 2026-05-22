"""Tests for the Video Panel transcode planner (transcode.py).

Pure ffmpeg-orchestration logic, no third-party deps, so this file imports the
module directly (no fastapi/yaml guard). The detection helpers are async; the
tests drive them with asyncio.run and stub the subprocess layer (transcode._run)
so nothing actually spawns ffmpeg.

Run from the openavc-plugins root: pytest tests/test_video_panel_transcode.py -v
"""

import asyncio
import sys
from pathlib import Path

# Plugins root, so the plugin imports by its package path.
_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))

from integrations.video_panel import transcode as tc


# ──── Command string quoting (go-shellquote compatible) ────


def test_command_string_leaves_simple_args_bare():
    args = ["ffmpeg", "-i", "rtsp://127.0.0.1:8556/cam__src", "-c:v", "libopenh264"]
    assert tc.command_string(args) == (
        "ffmpeg -i rtsp://127.0.0.1:8556/cam__src -c:v libopenh264"
    )


def test_command_string_quotes_spaces_and_empty():
    assert tc.command_string(["C:/Program Files/ff.exe"]) == "'C:/Program Files/ff.exe'"
    assert tc.command_string([""]) == "''"


def test_command_string_escapes_embedded_single_quote():
    # The POSIX '\'' idiom: close quote, escaped quote, reopen.
    assert tc.command_string(["a'b"]) == "'a'\\''b'"


# ──── Argv construction ────


def test_software_args_shape_and_windows_path_is_forward_slashed():
    args = tc.build_transcode_args(
        r"C:\deps\ffmpeg.exe",
        "libopenh264",
        "rtsp://127.0.0.1:8556/cam__src",
        "rtsp://127.0.0.1:8556/cam",
    )
    # Backslashes would be eaten by MediaMTX's POSIX shellquote split.
    assert args[0] == "C:/deps/ffmpeg.exe"
    # Input is read over TCP, before -i.
    assert args.index("-rtsp_transport") < args.index("-i")
    assert args[args.index("-i") + 1] == "rtsp://127.0.0.1:8556/cam__src"
    # Constrained Baseline software encode, no audio.
    assert args[args.index("-c:v") + 1] == "libopenh264"
    assert "constrained_baseline" in args
    assert "-an" in args
    # Output is RTSP forced to TCP (the muxer otherwise defaults to UDP).
    assert args[-5:] == [
        "-f", "rtsp", "-rtsp_transport", "tcp", "rtsp://127.0.0.1:8556/cam",
    ]


def test_vaapi_args_carry_device_then_hwupload():
    args = tc.build_transcode_args(
        "/usr/bin/ffmpeg",
        "h264_vaapi",
        "rtsp://127.0.0.1:8556/cam__src",
        "rtsp://127.0.0.1:8556/cam",
    )
    # Device init must precede -i; the upload filter must follow it.
    assert args.index("-vaapi_device") < args.index("-i")
    assert args[args.index("-vaapi_device") + 1] == tc.VAAPI_DEVICE
    assert args.index("-vf") > args.index("-i")
    assert args[args.index("-vf") + 1] == "format=nv12,hwupload"
    assert args[args.index("-c:v") + 1] == "h264_vaapi"


def test_rtmp_publish_knob_swaps_only_the_output_muxer():
    args = tc.build_transcode_args(
        "/usr/bin/ffmpeg",
        "libopenh264",
        "rtsp://127.0.0.1:8556/cam__src",
        "rtmp://127.0.0.1:1936/cam",
        publish="rtmp",
    )
    # Output becomes FLV/RTMP...
    assert args[-3:] == ["-f", "flv", "rtmp://127.0.0.1:1936/cam"]
    # ...but the input is still read over RTSP/TCP.
    assert "-i" in args and args[args.index("-i") + 1] == "rtsp://127.0.0.1:8556/cam__src"
    assert "rtsp" not in args[-3:]


def test_build_transcode_command_is_a_splittable_string():
    cmd = tc.build_transcode_command(
        r"C:\deps\ffmpeg.exe",
        "libopenh264",
        "rtsp://127.0.0.1:8556/cam__src",
        "rtsp://127.0.0.1:8556/cam",
    )
    assert isinstance(cmd, str)
    assert cmd.startswith("C:/deps/ffmpeg.exe ")
    assert cmd.endswith(" -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8556/cam")


# ──── Platform candidate lists ────


def test_platform_priority_windows(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    assert tc.platform_priority() == ["h264_qsv", "h264_nvenc", "h264_amf", "h264_mf"]


def test_platform_priority_linux_arm_prefers_v4l2m2m(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "linux")
    monkeypatch.setattr(tc.platform, "machine", lambda: "aarch64")
    assert tc.platform_priority()[0] == "h264_v4l2m2m"


def test_platform_priority_linux_x86_prefers_vaapi(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "linux")
    monkeypatch.setattr(tc.platform, "machine", lambda: "x86_64")
    assert tc.platform_priority()[0] == "h264_vaapi"


# ──── Encoder detection (subprocess stubbed) ────

_SAMPLE_ENCODERS = """Encoders:
 V..... = Video
 ------
 V....D libopenh264          OpenH264 H.264 (codec h264)
 V....D h264_nvenc           NVIDIA NVENC H.264 (codec h264)
 V....D hevc_nvenc           NVIDIA NVENC hevc (codec hevc)
 V....D h264_qsv             Intel QSV H.264 (codec h264)
 A....D aac                  AAC (Advanced Audio Coding)
"""


def _stub_run(monkeypatch, *, returncode=0, stdout=b""):
    async def fake_run(args, timeout):
        return returncode, stdout, b""
    monkeypatch.setattr(tc, "_run", fake_run)


def test_list_encoders_extracts_only_known_h264(monkeypatch):
    _stub_run(monkeypatch, returncode=0, stdout=_SAMPLE_ENCODERS.encode())
    found = asyncio.run(tc.list_encoders("ffmpeg"))
    assert found == {"libopenh264", "h264_nvenc", "h264_qsv"}


def test_select_encoder_none_is_software_without_probing(monkeypatch):
    # Should not even look at the binary.
    def boom(*a, **k):
        raise AssertionError("must not probe when hardware_accel=none")
    monkeypatch.setattr(tc, "list_encoders", boom)
    assert asyncio.run(tc.select_encoder("ffmpeg", "none")) == tc.SOFTWARE_ENCODER


def _stub_detection(monkeypatch, compiled, working):
    async def fake_list(_bin):
        return set(compiled)
    async def fake_test(_bin, enc):
        return enc in working
    monkeypatch.setattr(tc, "list_encoders", fake_list)
    monkeypatch.setattr(tc, "test_encode", fake_test)


def test_select_encoder_explicit_works(monkeypatch):
    _stub_detection(monkeypatch, compiled={"h264_nvenc"}, working={"h264_nvenc"})
    assert asyncio.run(tc.select_encoder("ffmpeg", "nvenc")) == "h264_nvenc"


def test_select_encoder_explicit_unavailable_falls_back_to_software(monkeypatch):
    # Compiled in, but the test-encode fails (no NVIDIA GPU here).
    _stub_detection(monkeypatch, compiled={"h264_nvenc"}, working=set())
    assert asyncio.run(tc.select_encoder("ffmpeg", "nvenc")) == tc.SOFTWARE_ENCODER


def test_select_encoder_auto_picks_first_working_candidate(monkeypatch):
    monkeypatch.setattr(tc, "platform_priority",
                        lambda: ["h264_qsv", "h264_nvenc", "h264_amf"])
    # qsv compiled but doesn't run; nvenc runs -> nvenc wins.
    _stub_detection(monkeypatch,
                    compiled={"h264_qsv", "h264_nvenc", "h264_amf"},
                    working={"h264_nvenc", "h264_amf"})
    assert asyncio.run(tc.select_encoder("ffmpeg", "auto")) == "h264_nvenc"


def test_select_encoder_auto_falls_back_to_software(monkeypatch):
    monkeypatch.setattr(tc, "platform_priority", lambda: ["h264_qsv", "h264_nvenc"])
    _stub_detection(monkeypatch, compiled={"h264_qsv", "h264_nvenc"}, working=set())
    assert asyncio.run(tc.select_encoder("ffmpeg", "auto")) == tc.SOFTWARE_ENCODER
