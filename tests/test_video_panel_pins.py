"""
Pins for the Video Panel plugin: what it does today, in the order it does it.

These hold behaviour the feature tests in test_video_panel_plugin.py leave
open: the order of every side effect a stream operation causes (the project
save, the sidecar path calls, the state writes, the log lines), the checks
each route makes and the order it makes them in, the exact rows the stream
picker is given for every kind of discovered source, the status poll's
reading of the sidecar's path list, a stream's login as it moves between the
address and the fields, and start()'s bring-up with its two refusals.

Some of them pin something that only looks deliberate, such as a rename that
leaves the old stream's status key behind. They record the behaviour; they do
not endorse it.

Run from the openavc-plugins root: pytest tests/test_video_panel_pins.py -v
"""

import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest

_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from starlette.responses import Response

    from integrations.video_panel import video_panel_plugin as vp_mod
    from integrations.video_panel.video_panel_plugin import SAVED_LOGIN, VideoPanelPlugin

    _PLUGIN_IMPORTABLE = True
except Exception:  # fastapi/httpx not installed in this environment
    _PLUGIN_IMPORTABLE = False

pytestmark = pytest.mark.skipif(
    not _PLUGIN_IMPORTABLE, reason="fastapi/httpx not available"
)

if _PLUGIN_IMPORTABLE:
    from test_video_panel_plugin import _FakeApi  # noqa: E402  (a helper, not a test)


# ──── A recorder of every side effect, in order ────


class _Log(list):
    """One ordered list of what happened, shared by the fakes below."""


class _RecordingApi(_FakeApi if _PLUGIN_IMPORTABLE else object):
    def __init__(self, entries, config=None):
        super().__init__(config)
        self.entries = entries

    async def save_config(self, cfg):
        self.entries.append(("save", copy.deepcopy(cfg.get("streams"))))
        await super().save_config(cfg)

    async def state_set(self, key, value):
        self.entries.append(("state", key, value))
        await super().state_set(key, value)

    async def event_emit(self, name, payload):
        self.entries.append(("event", name, payload))

    async def state_subscribe(self, pattern, callback):
        self.entries.append(("subscribe", pattern))
        return await super().state_subscribe(pattern, callback)

    def register_router(self, router, **kwargs):
        self.entries.append(("register_router", kwargs))
        super().register_router(router, **kwargs)

    def create_periodic_task(self, fn, interval_seconds=None, name=None):
        self.entries.append(("periodic", fn.__name__, interval_seconds, name))
        super().create_periodic_task(fn, interval_seconds=interval_seconds, name=name)

    def log(self, message, level="info"):
        self.entries.append(("log", level, message))
        super().log(message, level)


def _plugin(streams=(), *, items=None, ffmpeg=None):
    """A plugin with a recording api and a recording MediaMTX control API.

    ``plugin.sidecar`` is what the fake control API answers: ``answer`` is the
    paths/list reply (default ``{"items": items}``), ``post_ok`` what a path
    add returns.
    """
    entries = _Log()
    plugin = VideoPanelPlugin()
    plugin.api = _RecordingApi(entries, {"streams": [dict(s) for s in streams]})
    plugin._ffmpeg_bin = ffmpeg
    plugin._streams = [dict(s) for s in streams]
    plugin._auth_pass = "pw"
    plugin.sidecar = {"answer": {"items": list(items or [])}, "post_ok": True}

    async def post(path, body):
        entries.append(("post", path, body))
        return plugin.sidecar["post_ok"]

    async def delete(path):
        entries.append(("delete", path))
        return True

    async def get(path):
        entries.append(("get", path))
        return plugin.sidecar["answer"]

    plugin._api_post = post
    plugin._api_delete = delete
    plugin._api_get = get
    return plugin, entries


def _client(plugin):
    app = FastAPI()
    app.include_router(plugin._build_router())
    return TestClient(app)


def _stream(sid, **over):
    entry = {
        "stream_id": sid, "name": sid.title(), "rtsp_url": f"rtsp://cam/{sid}",
        "username": "", "password": "", "codec_hint": "h264",
        "transcode": "auto", "hardware_accel": "auto",
    }
    entry.update(over)
    return entry


def _ffmpeg_warning(sid):
    return (
        "log", "warning",
        f"stream '{sid}' needs transcoding but ffmpeg is unavailable; serving "
        "the source as-is (it may not play in browsers). Reinstall the Video "
        "Panel plugin to fetch ffmpeg.",
    )


def _add(sid, source):
    return ("post", f"/v3/config/paths/add/{sid}", {"source": source, "sourceOnDemand": True})


def _listing_write(entries):
    """The stream_ids value of every stream_ids write, decoded."""
    return [json.loads(e[2]) for e in entries if e[:2] == ("state", "stream_ids")]


# ──── What a stream record holds ────


class _Form:
    """A StreamIn-shaped object with every field settable to anything."""

    def __init__(self, **kw):
        self.name = kw.get("name", "Cam")
        self.stream_id = kw.get("stream_id")
        self.rtsp_url = kw.get("rtsp_url", "rtsp://h/x")
        self.username = kw.get("username", "")
        self.password = kw.get("password", "")
        self.codec_hint = kw.get("codec_hint", "auto")
        self.transcode = kw.get("transcode", "auto")
        self.hardware_accel = kw.get("hardware_accel", "auto")


def test_an_entry_fills_every_blank_and_keeps_its_key_order():
    form = _Form(
        name="   ", rtsp_url=None, username=None, password=None,
        codec_hint="", transcode=None, hardware_accel="",
    )
    entry = VideoPanelPlugin._entry_from_input(form, "cam")
    assert list(entry.items()) == [
        ("stream_id", "cam"), ("name", "cam"), ("rtsp_url", ""),
        ("username", ""), ("password", ""), ("codec_hint", "auto"),
        ("transcode", "auto"), ("hardware_accel", "auto"),
    ]


def test_an_entry_sent_the_marker_keeps_the_saved_password_or_none():
    form = _Form(password=SAVED_LOGIN, codec_hint="h265", transcode="never",
                 hardware_accel="qsv", name=" Lobby ", rtsp_url=" rtsp://a/b ")
    kept = VideoPanelPlugin._entry_from_input(form, "lobby", "secret")
    assert kept["password"] == "secret"
    assert (kept["name"], kept["rtsp_url"]) == ("Lobby", "rtsp://a/b")
    assert (kept["codec_hint"], kept["transcode"], kept["hardware_accel"]) == ("h265", "never", "qsv")
    assert VideoPanelPlugin._entry_from_input(form, "lobby")["password"] == ""
    assert VideoPanelPlugin._entry_from_input(form, "lobby", None)["password"] == ""
    typed = VideoPanelPlugin._entry_from_input(_Form(password="new"), "x", "secret")
    assert typed["password"] == "new"


@pytest.mark.parametrize("url, expected", [
    (None, ("", None, None)),
    ("", ("", None, None)),
    ("  rtsp://h/x ", ("rtsp://h/x", None, None)),
    ("noscheme@host", ("noscheme@host", None, None)),
    ("rtsp://u:p@h:554/s", ("rtsp://h:554/s", "u", "p")),
    ("rtsp://u@h/s", ("rtsp://h/s", "u", None)),
    ("rtsp://:p@h/s", ("rtsp://h/s", "", "p")),
    ("rtsp://a%40b:p%3Aq@h", ("rtsp://h", "a@b", "p:q")),
    ("rtsp://h/s?x=u@v", ("rtsp://h/s?x=u@v", None, None)),
    ("rtsp://h?x=u@v", ("rtsp://h?x=u@v", None, None)),
    ("rtsp://u:p@q@h/s", ("rtsp://h/s", "u", "p@q")),
    ("rtsp://u:p@h#f@x", ("rtsp://h#f@x", "u", "p")),
    ("srt://u:p:z@h:9000", ("srt://h:9000", "u", "p:z")),
])
def test_splitting_a_login_out_of_an_address(url, expected):
    assert VideoPanelPlugin._split_url_login(url) == expected


def _move(entry, api=True):
    plugin, entries = _plugin()
    if not api:
        plugin.api = None
    changed = plugin._move_url_login(entry)
    return changed, entry, list(entries)


_REPLACED = (
    "log", "warning",
    "Stream 'cam': the login written into its address replaced its Username and Password",
)


def test_moving_a_login_out_of_the_address():
    # No login: nothing changes.
    assert _move({"stream_id": "cam", "rtsp_url": "rtsp://h/x"}) == (
        False, {"stream_id": "cam", "rtsp_url": "rtsp://h/x"}, [])
    # A password with no username stays where it is.
    assert _move({"stream_id": "cam", "rtsp_url": "rtsp://:p@h/x"}) == (
        False, {"stream_id": "cam", "rtsp_url": "rtsp://:p@h/x"}, [])
    # A username alone moves, and the saved password is left alone.
    assert _move({"stream_id": "cam", "rtsp_url": "rtsp://u@h/x", "password": "keep"}) == (
        True, {"stream_id": "cam", "rtsp_url": "rtsp://h/x", "password": "keep", "username": "u"}, [])
    # The same login as the fields: moved, nothing said.
    assert _move({"stream_id": "cam", "rtsp_url": "rtsp://u:p@h/x", "username": "u", "password": "p"}) == (
        True, {"stream_id": "cam", "rtsp_url": "rtsp://h/x", "username": "u", "password": "p"}, [])


def test_an_address_login_that_replaces_the_fields_says_so():
    # A different username.
    changed, entry, said = _move(
        {"stream_id": "cam", "rtsp_url": "rtsp://new:pw@h/x", "username": "old"})
    assert (changed, said) == (True, [_REPLACED])
    assert entry == {"stream_id": "cam", "rtsp_url": "rtsp://h/x", "username": "new", "password": "pw"}
    # The same username, a different password.
    changed, entry, said = _move(
        {"stream_id": "cam", "rtsp_url": "rtsp://u:new@h/x", "username": "u", "password": "old"})
    assert (changed, said, entry["password"]) == (True, [_REPLACED], "new")
    # A username only in the address, a different username in the field.
    changed, entry, said = _move(
        {"stream_id": "cam", "rtsp_url": "rtsp://u@h/x", "username": "v", "password": "keep"})
    assert (changed, said, entry["password"]) == (True, [_REPLACED], "keep")
    # No stream id yet, and no api at all.
    changed, entry, said = _move({"rtsp_url": "rtsp://a:b@h", "username": "z"})
    assert said == [(
        "log", "warning",
        "Stream '': the login written into its address replaced its Username and Password",
    )]
    assert _move({"rtsp_url": "rtsp://a:b@h", "username": "z"}, api=False)[0] is True


class _Probe:
    def __init__(self, stream_id="", rtsp_url="", username="", password=""):
        self.stream_id = stream_id
        self.rtsp_url = rtsp_url
        self.username = username
        self.password = password


def test_which_saved_password_a_test_stands_for():
    plugin, _ = _plugin([
        _stream("a", rtsp_url=" rtsp://h/1", username="", password="pa"),
        _stream("b", rtsp_url="rtsp://h/1", username="", password="pb"),
        _stream("c", rtsp_url="rtsp://h/2", username="u", password=None),
        _stream("d", rtsp_url=None, username=None, password="pd"),
    ])
    assert plugin._saved_password_for(_Probe(stream_id="b")) == "pb"
    # A named stream that is gone falls back to the address.
    assert plugin._saved_password_for(_Probe(stream_id="zz", rtsp_url="rtsp://h/1")) == "pa"
    # The first match wins; both sides are stripped.
    assert plugin._saved_password_for(_Probe(rtsp_url=" rtsp://h/1 ")) == "pa"
    # A username that does not match finds nothing; None reads as empty.
    assert plugin._saved_password_for(_Probe(rtsp_url="rtsp://h/1", username="x")) == ""
    assert plugin._saved_password_for(_Probe(rtsp_url="", username=None)) == "pd"
    # A stream with no password answers empty.
    assert plugin._saved_password_for(_Probe(rtsp_url="rtsp://h/2", username="u")) == ""
    assert plugin._saved_password_for(_Probe(stream_id="c")) == ""


@pytest.mark.parametrize("url, user, password, expected", [
    ("", "u", "p", None),
    (None, "u", "p", None),
    ("host/x", "u", "p", None),
    ("rtsp://h/x", "", "p", "rtsp://h/x"),
    (" rtsp://h/x ", "", "", "rtsp://h/x"),
    ("rtsp://h/x", "u s", "p@:/", "rtsp://u%20s:p%40%3A%2F@h/x"),
    ("rtsp://a@h/x", "u", "p", "rtsp://a@h/x"),
    ("rtsp://h/x?y=a@b", "u", "p", "rtsp://u:p@h/x?y=a@b"),
    ("rtsp://h?y=a@b", "u", "p", "rtsp://h?y=a@b"),
    ("rtsp://h/x", "u", "", "rtsp://u:@h/x"),
])
def test_putting_a_login_back_into_a_source_address(url, user, password, expected):
    assert VideoPanelPlugin._source_url(url, user, password) == expected


def test_a_stream_source_reads_missing_fields_as_empty():
    assert VideoPanelPlugin._stream_source_url({"rtsp_url": "rtsp://h/x", "username": "u"}) == "rtsp://u:@h/x"
    assert VideoPanelPlugin._stream_source_url({}) is None


def test_ids_from_names():
    assert VideoPanelPlugin._slugify(None) == "stream"
    assert VideoPanelPlugin._slugify("Ünïcode Cam") == "n_code_cam"
    plugin, _ = _plugin([_stream("lobby"), _stream("lobby_2")])
    assert plugin._unique_stream_id("Lobby") == "lobby_3"
    assert plugin._unique_stream_id("!!!") == "stream"
    plugin, _ = _plugin([_stream("lobby_2")])
    assert plugin._unique_stream_id("Lobby") == "lobby"


def test_the_three_checks_and_their_sentences():
    for bad in (None, "", "rtsp:/x"):
        with pytest.raises(HTTPException) as exc:
            VideoPanelPlugin._validate_url(bad)
        assert (exc.value.status_code, exc.value.detail) == (
            422, "Source URL must be a full URL, e.g. rtsp://host:554/stream.")
    VideoPanelPlugin._validate_url("x://")
    for bad in (None, "", "a b", "a.b", "é"):
        with pytest.raises(HTTPException) as exc:
            VideoPanelPlugin._validate_stream_id(bad)
        assert (exc.value.status_code, exc.value.detail) == (
            422, "Stream ID may contain only letters, numbers, hyphens, and underscores.")
    VideoPanelPlugin._validate_stream_id("a-b_C9")
    for bad in (None, "", "a_b", "a/b"):
        with pytest.raises(HTTPException) as exc:
            VideoPanelPlugin._validate_secret(bad)
        assert (exc.value.status_code, exc.value.detail) == (422, "Invalid WHEP session id.")
    VideoPanelPlugin._validate_secret("abc-123")


def test_the_view_the_programmer_is_given():
    with_pw = VideoPanelPlugin._stream_view(_stream("a", password="x"), {"a": True})
    assert list(with_pw.items()) == [
        ("stream_id", "a"), ("name", "A"), ("rtsp_url", "rtsp://cam/a"), ("username", ""),
        ("password", SAVED_LOGIN), ("codec_hint", "h264"), ("transcode", "auto"),
        ("hardware_accel", "auto"), ("status", "streaming"),
    ]
    bare = VideoPanelPlugin._stream_view({"stream_id": "b"}, {"b": False})
    assert list(bare.items()) == [("stream_id", "b"), ("password", ""), ("status", "idle")]
    assert VideoPanelPlugin._stream_view({"stream_id": "c", "password": None}, {})["password"] == ""


# ──── The plugin page's operations, in order ────


def test_an_add_saves_registers_publishes_then_reads_the_sidecar():
    plugin, entries = _plugin([_stream("front_door")])
    client = _client(plugin)
    r = client.post("/streams", json={
        "name": "Front Door", "rtsp_url": " rtsp://admin:pw@cam/2 ", "password": SAVED_LOGIN,
    })
    assert r.status_code == 200, r.text
    new = {
        "stream_id": "front_door_2", "name": "Front Door", "rtsp_url": "rtsp://cam/2",
        "username": "admin", "password": "pw", "codec_hint": "auto",
        "transcode": "auto", "hardware_accel": "auto",
    }
    assert entries[0] == ("save", [_stream("front_door"), new])
    assert entries[1:] == [
        _ffmpeg_warning("front_door_2"),
        _add("front_door_2", "rtsp://admin:pw@cam/2"),
        ("state", "stream_ids", json.dumps([
            {"value": "front_door", "label": "Front_Door", "mode": "webrtc"},
            {"value": "front_door_2", "label": "Front Door", "mode": "webrtc"},
        ])),
        ("state", "streams.front_door", "idle"),
        ("state", "streams.front_door_2", "idle"),
        ("get", "/v3/paths/list"),
    ]
    assert r.json() == {**new, "password": SAVED_LOGIN, "status": "idle"}


def test_an_add_with_its_own_id_and_its_refusals():
    plugin, entries = _plugin([_stream("cam")])
    client = _client(plugin)
    # The URL is checked before the id, and the id before the clash.
    r = client.post("/streams", json={"name": "x", "rtsp_url": "nope", "stream_id": "bad id"})
    assert (r.status_code, r.json()["detail"]) == (
        422, "Source URL must be a full URL, e.g. rtsp://host:554/stream.")
    r = client.post("/streams", json={"name": "x", "rtsp_url": "rtsp://h", "stream_id": "bad id"})
    assert (r.status_code, r.json()["detail"]) == (
        422, "Stream ID may contain only letters, numbers, hyphens, and underscores.")
    r = client.post("/streams", json={"name": "x", "rtsp_url": "rtsp://h", "stream_id": " cam "})
    assert (r.status_code, r.json()["detail"]) == (409, "A stream with id 'cam' already exists.")
    assert entries == []
    # A name that slugs to nothing and no id: the fallback id.
    r = client.post("/streams", json={"name": "!!", "rtsp_url": "rtsp://h", "codec_hint": "h264"})
    assert r.json()["stream_id"] == "stream" and r.json()["name"] == "!!"


def test_an_add_whose_path_the_sidecar_refuses_is_kept_and_said():
    plugin, entries = _plugin()
    plugin.sidecar["post_ok"] = False
    r = _client(plugin).post("/streams", json={
        "name": "Cam", "rtsp_url": "rtsp://h", "codec_hint": "h264"})
    assert r.status_code == 200
    assert ("log", "warning", "sidecar rejected stream 'cam'; check the source URL") in entries
    assert [s["stream_id"] for s in plugin._streams] == ["cam"]


def test_an_edit_renames_in_place_keeps_the_password_and_leaves_the_old_status_key():
    plugin, entries = _plugin([
        _stream("a"), _stream("cam", password="secret", username="u"), _stream("z")])
    client = _client(plugin)
    r = client.put("/streams/cam", json={
        "name": "Cam", "stream_id": "cam2", "rtsp_url": "rtsp://h/n",
        "username": "u", "password": SAVED_LOGIN, "codec_hint": "h264",
    })
    assert r.status_code == 200, r.text
    edited = {
        "stream_id": "cam2", "name": "Cam", "rtsp_url": "rtsp://h/n", "username": "u",
        "password": "secret", "codec_hint": "h264", "transcode": "auto",
        "hardware_accel": "auto",
    }
    assert [s["stream_id"] for s in plugin._streams] == ["a", "cam2", "z"]
    assert entries == [
        ("save", [_stream("a"), edited, _stream("z")]),
        ("delete", "/v3/config/paths/delete/cam"),
        ("delete", "/v3/config/paths/delete/cam__src"),
        _add("cam2", "rtsp://u:secret@h/n"),
        ("state", "stream_ids", json.dumps([
            {"value": "a", "label": "A", "mode": "webrtc"},
            {"value": "cam2", "label": "Cam", "mode": "webrtc"},
            {"value": "z", "label": "Z", "mode": "webrtc"},
        ])),
        ("state", "streams.a", "idle"),
        ("state", "streams.cam2", "idle"),
        ("state", "streams.z", "idle"),
        ("get", "/v3/paths/list"),
    ]
    assert ("state", "streams.cam", None) not in entries
    assert r.json()["password"] == SAVED_LOGIN


def test_an_edit_checks_in_this_order():
    plugin, entries = _plugin([_stream("cam"), _stream("other")])
    client = _client(plugin)
    # An unknown stream is 404 whatever else is wrong with the request.
    r = client.put("/streams/nope", json={"name": "x", "rtsp_url": "bad", "stream_id": "a b"})
    assert (r.status_code, r.json()["detail"]) == (404, "No stream with id 'nope'.")
    r = client.put("/streams/cam", json={"name": "x", "rtsp_url": "bad", "stream_id": "a b"})
    assert (r.status_code, r.json()["detail"]) == (
        422, "Source URL must be a full URL, e.g. rtsp://host:554/stream.")
    r = client.put("/streams/cam", json={"name": "x", "rtsp_url": "rtsp://h", "stream_id": "a b"})
    assert r.status_code == 422 and r.json()["detail"].startswith("Stream ID may")
    r = client.put("/streams/cam", json={"name": "x", "rtsp_url": "rtsp://h", "stream_id": "other"})
    assert (r.status_code, r.json()["detail"]) == (409, "A stream with id 'other' already exists.")
    assert entries == []
    # Keeping its own id, or sending none, is no clash.
    r = client.put("/streams/cam", json={"name": "", "rtsp_url": "rtsp://h", "stream_id": "cam",
                                         "codec_hint": "h264"})
    assert r.status_code == 200 and r.json()["name"] == "cam"
    r = client.put("/streams/cam", json={"name": "N", "rtsp_url": "rtsp://h", "codec_hint": "h264"})
    assert r.status_code == 200 and r.json()["stream_id"] == "cam"


def test_an_edit_with_a_login_in_the_address_moves_it_over_the_kept_password():
    plugin, entries = _plugin([_stream("cam", username="u", password="old")])
    r = _client(plugin).put("/streams/cam", json={
        "name": "Cam", "rtsp_url": "rtsp://u:new@h", "username": "u",
        "password": SAVED_LOGIN, "codec_hint": "h264",
    })
    assert r.status_code == 200
    assert plugin._streams[0]["password"] == "new"
    # Said before the save: the move happens to the entry, then it is kept.
    assert [e[0] for e in entries[:2]] == ["log", "save"] and entries[0] == _REPLACED


def test_a_delete_saves_unregisters_clears_its_status_then_publishes():
    plugin, entries = _plugin([_stream("a"), _stream("cam")])
    client = _client(plugin)
    r = client.delete("/streams/nope")
    assert (r.status_code, r.json()["detail"]) == (404, "No stream with id 'nope'.")
    assert entries == []
    r = client.delete("/streams/cam")
    assert r.json() == {"status": "deleted", "stream_id": "cam"}
    assert entries == [
        ("save", [_stream("a")]),
        ("delete", "/v3/config/paths/delete/cam"),
        ("delete", "/v3/config/paths/delete/cam__src"),
        ("state", "streams.cam", None),
        ("state", "stream_ids", json.dumps([{"value": "a", "label": "A", "mode": "webrtc"}])),
        ("state", "streams.a", "idle"),
    ]


def test_the_list_reads_available_before_ready():
    plugin, entries = _plugin(
        [_stream("a", password="x"), _stream("b"), _stream("c"), _stream("d")],
        items=[
            {"name": "a", "available": False, "ready": True},
            {"name": "b", "ready": True},
            {"name": "", "available": True},
            {"available": True},
            {"name": "c"},
        ],
    )
    body = _client(plugin).get("/streams").json()
    assert [(s["stream_id"], s["status"], s["password"]) for s in body["streams"]] == [
        ("a", "idle", SAVED_LOGIN), ("b", "streaming", ""), ("c", "idle", ""), ("d", "idle", ""),
    ]
    assert entries == [("get", "/v3/paths/list")]


def test_the_list_with_no_sidecar_answer_reads_every_stream_idle():
    plugin, _ = _plugin([_stream("a")])
    plugin.sidecar["answer"] = None
    assert _client(plugin).get("/streams").json()["streams"][0]["status"] == "idle"


def test_the_list_route_does_not_survive_a_null_item_list():
    plugin, _ = _plugin([_stream("a")])
    plugin.sidecar["answer"] = {"items": None}
    with pytest.raises(TypeError):
        _client(plugin).get("/streams")


def test_the_status_route():
    plugin, _ = _plugin([_stream("a"), _stream("b")])
    assert _client(plugin).get("/status").json() == {
        "running": False, "mediamtx_version": "1.18.2", "stream_ids": ["a", "b"],
    }

    class _Sup:
        running = True

    plugin._supervisor = _Sup()
    assert _client(plugin).get("/status").json()["running"] is True


def test_a_probe_checks_the_url_then_ffmpeg_then_resolves_the_marker():
    plugin, _ = _plugin([_stream("cam", rtsp_url="rtsp://h/1", username="u", password="saved")])
    probed = []

    async def probe(url):
        probed.append(url)
        return {"success": True, "codec": "h264"}

    plugin._probe_rtsp = probe
    client = _client(plugin)
    r = client.post("/streams/probe", json={"rtsp_url": "bad"})
    assert (r.status_code, r.json()["detail"]) == (
        422, "Source URL must be a full URL, e.g. rtsp://host:554/stream.")
    r = client.post("/streams/probe", json={"rtsp_url": "rtsp://h/1"})
    assert (r.status_code, r.json()["detail"]) == (
        503, "ffmpeg is not available. Reinstall the Video Panel plugin to fetch its native dependencies.")
    assert probed == []
    plugin._ffmpeg_bin = "/ffmpeg"
    r = client.post("/streams/probe", json={
        "rtsp_url": "rtsp://h/1", "username": "u", "password": SAVED_LOGIN, "stream_id": "cam"})
    assert r.json() == {"success": True, "codec": "h264"}
    client.post("/streams/probe", json={
        "rtsp_url": "rtsp://h/1", "username": "u", "password": SAVED_LOGIN})
    client.post("/streams/probe", json={"rtsp_url": "rtsp://h/1", "username": "u", "password": "typed"})
    client.post("/streams/probe", json={"rtsp_url": " rtsp://h/9 ", "password": SAVED_LOGIN})
    assert probed == [
        "rtsp://u:saved@h/1", "rtsp://u:saved@h/1", "rtsp://u:typed@h/1", "rtsp://h/9",
    ]


def test_a_snapshot_checks_the_stream_then_ffmpeg_then_the_frame():
    plugin, _ = _plugin([_stream("cam", username="u", password="p")])
    shot = []
    frames = [None, b"\xff\xd8jpeg"]

    async def snap(url):
        shot.append(url)
        return frames.pop(0)

    plugin._snapshot_rtsp = snap
    client = _client(plugin)
    r = client.get("/streams/nope/snapshot.jpg")
    assert (r.status_code, r.json()["detail"]) == (404, "No stream with id 'nope'.")
    r = client.get("/streams/cam/snapshot.jpg")
    assert (r.status_code, r.json()["detail"]) == (503, "ffmpeg is not available.")
    plugin._ffmpeg_bin = "/ffmpeg"
    r = client.get("/streams/cam/snapshot.jpg")
    assert (r.status_code, r.json()["detail"]) == (502, "Could not capture a frame from the stream.")
    r = client.get("/streams/cam/snapshot.jpg")
    assert r.status_code == 200 and r.content == b"\xff\xd8jpeg"
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"
    assert shot == ["rtsp://u:p@cam/cam", "rtsp://u:p@cam/cam"]


def test_a_report_collapses_and_caps_its_reason_and_stamps_it(monkeypatch):
    plugin, entries = _plugin()
    monkeypatch.setattr(vp_mod.time, "strftime", lambda fmt, t: f"at[{fmt}]")
    client = _client(plugin)
    r = client.post("/report", json={"reason": "x", "stream_id": "a b"})
    assert (r.status_code, r.json()["detail"]) == (
        422, "Stream ID may contain only letters, numbers, hyphens, and underscores.")
    assert entries == []
    r = client.post("/report", json={"reason": "  ICE \n  failed\tnow ", "stream_id": "cam"})
    assert r.json() == {"ok": True}
    r = client.post("/report", json={"reason": "y" * 300})
    assert entries == [
        ("state", "playback_error", "ICE failed now"),
        ("state", "playback_error_stream", "cam"),
        ("state", "playback_error_at", "at[%Y-%m-%d %H:%M:%S]"),
        ("state", "playback_error", "y" * 200),
        ("state", "playback_error_stream", ""),
        ("state", "playback_error_at", "at[%Y-%m-%d %H:%M:%S]"),
    ]


# ──── The play routes ────


_MJPEG = {"label": "M", "url": "http://enc/mjpg", "format": "mjpeg", "device": "enc", "group": "Enc"}
_SRT = {"label": "S", "url": "srt://enc:9000", "format": "srt", "device": "enc", "group": "Enc"}


def _play_plugin(access=None):
    plugin, entries = _plugin([_stream("cam")])
    plugin._discovered = {"auto-m": dict(_MJPEG), "auto-s": dict(_SRT)}
    if access is not None:
        plugin.api.viewer_access = lambda request, capability: (
            entries.append(("viewer_access", capability)) or access)
    return plugin, entries


def test_delivery_checks_the_id_then_that_it_is_known():
    plugin, _ = _play_plugin()
    client = _client(plugin)
    r = client.get("/delivery/a%20b")
    assert r.status_code == 422 and r.json()["detail"].startswith("Stream ID may")
    r = client.get("/delivery/nope")
    assert (r.status_code, r.json()["detail"]) == (404, "No stream with id 'nope'.")


@pytest.mark.parametrize("access, sid, expected", [
    (None, "cam", {"stream_id": "cam", "mode": "webrtc"}),
    (None, "auto-m", {"stream_id": "auto-m", "mode": "mjpeg"}),
    (None, "auto-s", {"stream_id": "auto-s", "mode": "webrtc"}),
    ("local", "auto-m", {"stream_id": "auto-m", "mode": "mjpeg"}),
    ("remote", "cam", {"stream_id": "cam", "mode": "hls"}),
    ("remote", "auto-m", {"stream_id": "auto-m", "mode": "mjpeg"}),
    ("remote", "auto-s", {"stream_id": "auto-s", "mode": "hls"}),
    ("something_new", "cam", {"stream_id": "cam", "mode": "hls"}),
    ("remote_blocked", "auto-m", {
        "stream_id": "auto-m", "mode": "blocked",
        "detail": "Remote video is not included in this plan. This stream still plays on panels in the space.",
    }),
])
def test_delivery_answers(access, sid, expected):
    plugin, entries = _play_plugin(access)
    assert _client(plugin).get(f"/delivery/{sid}").json() == expected
    if access is not None:
        assert entries == [("viewer_access", "tunnel_video")]


def test_hls_checks_the_id_known_file_then_plan_in_that_order():
    plugin, _ = _play_plugin("remote_blocked")
    sent = []

    async def respond(url, token=""):
        sent.append((url, token))
        return Response(content=b"#EXTM3U", media_type="application/vnd.apple.mpegurl")

    plugin._hls_stream_response = respond
    client = _client(plugin)
    r = client.get("/hls/a%20b/x.txt")
    assert r.status_code == 422 and r.json()["detail"].startswith("Stream ID may")
    r = client.get("/hls/nope/x.txt")
    assert (r.status_code, r.json()["detail"]) == (404, "No stream with id 'nope'.")
    r = client.get("/hls/cam/x.txt")
    assert (r.status_code, r.json()["detail"]) == (422, "Invalid HLS file name.")
    r = client.get("/hls/cam/index.m3u8")
    assert (r.status_code, r.json()["detail"]) == (
        402, "Remote video is not included in this plan. This stream still plays on panels in the space.")
    assert sent == []
    plugin.api.viewer_access = lambda request, capability: "remote"
    r = client.get("/hls/auto-s/index.m3u8?_HLS_msn=3&_plugin_token=t%20k&_HLS_part=1")
    assert r.status_code == 200 and r.content == b"#EXTM3U"
    r = client.get("/hls/cam/seg1.mp4")
    assert sent == [
        ("http://openavc:pw@127.0.0.1:8890/auto-s/index.m3u8?_HLS_msn=3&_HLS_part=1", "t k"),
        ("http://openavc:pw@127.0.0.1:8890/cam/seg1.mp4", ""),
    ]


def test_mjpeg_checks_the_id_then_the_source_then_the_plan():
    plugin, _ = _play_plugin("remote_blocked")
    pulled = []

    async def respond(url):
        pulled.append(url)
        return Response(content=b"--frame", media_type="multipart/x-mixed-replace")

    plugin._mjpeg_stream_response = respond
    client = _client(plugin)
    r = client.get("/mjpeg/a%20b")
    assert r.status_code == 422
    for sid in ("nope", "cam", "auto-s"):
        r = client.get(f"/mjpeg/{sid}")
        assert (r.status_code, r.json()["detail"]) == (404, f"No MJPEG preview for '{sid}'.")
    r = client.get("/mjpeg/auto-m")
    assert r.status_code == 402
    plugin.api.viewer_access = lambda request, capability: "local"
    assert _client(plugin).get("/mjpeg/auto-m").content == b"--frame"
    assert pulled == ["http://enc/mjpg"]


def test_a_whep_offer_and_its_location():
    plugin, _ = _play_plugin()
    client = _client(plugin)
    r = client.post("/whep/a%20b", content=b"v=0")
    assert r.status_code == 422
    r = client.post("/whep/nope", content=b"v=0")
    assert (r.status_code, r.json()["detail"]) == (404, "No stream with id 'nope'.")
    assert plugin.api.proxy_calls == []
    plugin.api.proxy_location = "/auto-s/whep/abc-123/?x=1"
    r = client.post("/whep/auto-s/", content=b"v=0")
    assert r.headers["location"] == "/whep/auto-s/abc-123"
    plugin.api.proxy_location = None
    r = client.post("/whep/cam", content=b"v=0")
    assert r.status_code == 201 and "location" not in r.headers
    assert plugin.api.proxy_calls == [
        {"url": "http://openavc:pw@127.0.0.1:8889/auto-s/whep", "method": "POST", "allow_internal": True},
        {"url": "http://openavc:pw@127.0.0.1:8889/cam/whep", "method": "POST", "allow_internal": True},
    ]


def test_a_whep_session_route_checks_the_id_then_the_secret_and_not_the_stream():
    plugin, _ = _play_plugin()
    plugin._auth_pass = ""
    client = _client(plugin)
    r = client.patch("/whep/a%20b/bad_secret", content=b"")
    assert r.status_code == 422 and r.json()["detail"].startswith("Stream ID may")
    r = client.delete("/whep/cam/bad_secret")
    assert (r.status_code, r.json()["detail"]) == (422, "Invalid WHEP session id.")
    r = client.patch("/whep/cam/bad_secret", content=b"")
    assert (r.status_code, r.json()["detail"]) == (422, "Invalid WHEP session id.")
    assert plugin.api.proxy_calls == []
    assert client.patch("/whep/nope/abc", content=b"").status_code == 204
    assert client.delete("/whep/cam/abc").status_code == 200
    assert plugin.api.proxy_calls == [
        {"url": "http://127.0.0.1:8889/nope/whep/abc", "method": "PATCH", "allow_internal": True},
        {"url": "http://127.0.0.1:8889/cam/whep/abc", "method": "DELETE", "allow_internal": True},
    ]


# ──── Discovered sources: what the picker is given ────


_SCENE = {
    "device.enc.name": "Encoder Frame",
    "device.enc.connected": True,
    "device.enc.in.01.preview_url": " rtsp://10.0.0.9/1 ",
    "device.enc.in.01.online": True,
    "device.enc.in.01.label": " Podium ",
    "device.enc.in.02.preview_url": "http://10.0.0.9/mjpg",
    "device.enc.in.02.preview_format": "MJPEG ",
    "device.enc.in.02.online": False,
    "device.enc.in.02.offline_detail": " Not answering. ",
    "device.enc.in.03.preview_url": "ftp://10.0.0.9/x",
    "device.enc.in.04.preview_status": "needs_setup",
    "device.enc.in.04.preview_setup_field": " port ",
    "device.enc.in.04.preview_status_detail": "Enter the port.",
    "device.enc.in.04.online": False,
    "device.enc.in.05.preview_status": "unavailable",
    "device.enc.in.05.preview_status_detail": "Output off.",
    "device.enc.in.05.online": False,
    "device.cam.preview_url": "srt://1.2.3.4:9000",
    "device.cam.connected": False,
    "device.cam.offline_detail": "Install it on the PATH",
    "device.box.preview_url": 42,
    "device.box.preview_status": "ready",
    "device.box.name": "Box",
    "device.box2.preview_url": "rtsp://h/x",
    "device.box2.preview_status": " needs_setup ",
    "device.box2.preview_setup_field": "addr",
}

_OFFLINE = "This device is not connected right now. The source will play when it comes back."
_UNDRAWABLE = (
    "log", "warning",
    "device.enc.in.03 offers a preview this plugin cannot draw (format None, "
    "URL 'ftp://10.0.0.9/x'); it is not being listed as a stream. Supported: "
    "mjpeg, rtsp, srt.",
)


@pytest.mark.asyncio
async def test_every_kind_of_source_in_one_rebuild():
    plugin, entries = _plugin([
        _stream("auto-cam", name="Cam Override"), _stream("auto-box2", name="Box Two"),
    ])
    plugin.api.state.update(_SCENE)
    await plugin._rebuild_discovered()

    assert list(plugin._discovered.items()) == [
        ("auto-cam", {
            "label": "auto-cam", "url": "srt://1.2.3.4:9000", "format": "srt",
            "device": "cam", "group": "cam", "status": "offline", "detail": _OFFLINE,
        }),
        ("auto-enc-in-01", {
            "label": "Podium", "url": "rtsp://10.0.0.9/1", "format": "rtsp",
            "device": "enc", "group": "Encoder Frame", "status": "", "detail": "",
        }),
        ("auto-enc-in-02", {
            "label": "auto-enc-in-02", "url": "http://10.0.0.9/mjpg", "format": "mjpeg",
            "device": "enc", "group": "Encoder Frame", "status": "offline",
            "detail": "Not answering.",
        }),
    ]
    assert list(plugin._unusable.items()) == [
        ("auto-box", {"label": "Box", "device": "box", "group": "Box", "status": "ready",
                      "detail": "", "setup_field": ""}),
        ("auto-box2", {"label": "auto-box2", "device": "box2", "group": "box2",
                       "status": "needs_setup", "detail": "", "setup_field": "addr"}),
        ("auto-enc-in-04", {"label": "auto-enc-in-04", "device": "enc", "group": "Encoder Frame",
                            "status": "needs_setup", "detail": "Enter the port.",
                            "setup_field": "port"}),
        ("auto-enc-in-05", {"label": "auto-enc-in-05", "device": "enc", "group": "Encoder Frame",
                            "status": "offline", "detail": _OFFLINE, "setup_field": ""}),
    ]
    listing = [
        {"value": "auto-cam", "label": "Cam Override", "mode": "webrtc"},
        {"value": "auto-box2", "label": "Box Two", "mode": "webrtc"},
        {"value": "auto-enc-in-01", "label": "Podium", "mode": "webrtc",
         "group": "Encoder Frame", "device": "enc"},
        {"value": "auto-enc-in-02", "label": "auto-enc-in-02", "mode": "mjpeg",
         "group": "Encoder Frame", "device": "enc", "status": "offline",
         "detail": "Not answering."},
        {"id": "auto-box", "label": "Box", "group": "Box", "device": "box",
         "status": "ready", "detail": ""},
        {"id": "auto-enc-in-04", "label": "auto-enc-in-04", "group": "Encoder Frame",
         "device": "enc", "status": "needs_setup", "detail": "Enter the port.",
         "setup": {"device": "enc", "field": "port"}},
        {"id": "auto-enc-in-05", "label": "auto-enc-in-05", "group": "Encoder Frame",
         "device": "enc", "status": "offline", "detail": _OFFLINE},
    ]
    assert entries == [
        _UNDRAWABLE,
        _add("auto-cam", "srt://1.2.3.4:9000"),
        _ffmpeg_warning("auto-enc-in-01"),
        _add("auto-enc-in-01", "rtsp://10.0.0.9/1"),
        ("state", "stream_ids", json.dumps(listing)),
    ]
    assert plugin._discovered_sidecar == {
        "auto-cam": "srt://1.2.3.4:9000", "auto-enc-in-01": "rtsp://10.0.0.9/1",
    }

    # Nothing changed: nothing re-registered, the undrawable one not said again.
    entries.clear()
    await plugin._rebuild_discovered()
    assert entries == [("state", "stream_ids", json.dumps(listing))]

    # It goes away, then comes back: NOT said again. Only becoming drawable
    # forgets it (the next test).
    del plugin.api.state["device.enc.in.03.preview_url"]
    await plugin._rebuild_discovered()
    plugin.api.state["device.enc.in.03.preview_url"] = "ftp://10.0.0.9/x"
    entries.clear()
    await plugin._rebuild_discovered()
    assert entries == [("state", "stream_ids", json.dumps(listing))]

    # A drawable one going away unregisters it.
    entries.clear()
    plugin._learned_codec["auto-cam"] = "other"
    plugin._warned_unreachable.add("auto-cam")
    del plugin.api.state["device.cam.preview_url"]
    await plugin._rebuild_discovered()
    assert entries[:2] == [
        ("delete", "/v3/config/paths/delete/auto-cam"),
        ("delete", "/v3/config/paths/delete/auto-cam__src"),
    ]
    assert "auto-cam" not in plugin._learned_codec
    assert "auto-cam" not in plugin._warned_unreachable


@pytest.mark.asyncio
async def test_a_source_that_becomes_drawable_is_forgotten_as_undrawable():
    plugin, entries = _plugin()
    plugin.api.state["device.x.preview_url"] = "ftp://h"
    await plugin._rebuild_discovered()
    plugin.api.state["device.x.preview_format"] = "rtsp"
    await plugin._rebuild_discovered()
    del plugin.api.state["device.x.preview_format"]
    entries.clear()
    await plugin._rebuild_discovered()
    assert entries[0][:2] == ("log", "warning")


@pytest.mark.asyncio
async def test_odd_scopes_and_odd_reachability_keys():
    plugin, entries = _plugin()
    plugin.api.state.update({
        "device..preview_url": "rtsp://x/y",
        "device.a.preview_url": "rtsp://a/1",
        "device.a.connected": 0,
        "device.a.b.connected": False,  # four parts: not a device's key
        "device.c.d.01.preview_url": "rtsp://c/1",
        "device.c.online": False,  # three parts: not a child's key
        "device.c.d.01.e.online": False,  # six parts: not a child's key either
        "device.c.d.01.offline_detail": "child says",
        "device.e.f.preview_url": "rtsp://e/1",
        "device.e.f.online": False,  # four parts: a scope, but not a child's key
        "device.g.preview_url": "rtsp://g/1",
        "device.g.connected": True,
        "device.g.x.connected": False,  # four parts, read after the device's own
    })
    await plugin._rebuild_discovered()
    assert {sid: (d["device"], d["group"], d["status"], d["detail"])
            for sid, d in plugin._discovered.items()} == {
        "auto-source": ("", "", "", ""),
        "auto-a": ("a", "a", "offline", _OFFLINE),
        "auto-c-d-01": ("c", "c", "", ""),
        "auto-e-f": ("e", "e", "", ""),
        "auto-g": ("g", "g", "", ""),
    }


@pytest.mark.asyncio
async def test_labels_fall_back_from_label_to_name_to_the_id():
    plugin, _ = _plugin()
    plugin.api.state.update({
        "device.a.preview_url": "rtsp://a", "device.a.label": "  ", "device.a.name": " Named ",
        "device.b.preview_url": "rtsp://b", "device.b.label": 7, "device.b.name": "",
        "device.c.preview_url": "rtsp://c", "device.c.label": "L", "device.c.name": "N",
    })
    await plugin._rebuild_discovered()
    assert {s: (d["label"], d["group"]) for s, d in plugin._discovered.items()} == {
        "auto-a": ("Named", "Named"), "auto-b": ("auto-b", "b"), "auto-c": ("L", "N"),
    }


@pytest.mark.asyncio
async def test_a_ready_status_with_a_url_plays_and_any_other_status_does_not():
    plugin, _ = _plugin()
    plugin.api.state.update({
        "device.a.preview_url": "rtsp://a", "device.a.preview_status": " ready ",
        "device.b.preview_url": "rtsp://b", "device.b.preview_status": "Ready",
        "device.c.preview_url": "  ", "device.c.preview_status": 3,
        "device.d.preview_status": "unavailable", "device.d.preview_setup_field": "f",
    })
    await plugin._rebuild_discovered()
    assert list(plugin._discovered) == ["auto-a"]
    assert {s: (u["status"], u["setup_field"]) for s, u in plugin._unusable.items()} == {
        "auto-b": ("Ready", ""), "auto-d": ("unavailable", "f"),
    }


@pytest.mark.asyncio
async def test_a_discovered_row_with_a_status_and_no_detail_reads_the_offline_sentence():
    plugin, entries = _plugin()
    plugin._discovered = {"auto-x": {"label": "X", "format": "rtsp", "status": "offline"}}
    plugin._unusable = {"auto-y": {"label": "Y", "status": "needs_setup", "setup_field": "f"}}
    await plugin._publish_stream_list()
    assert _listing_write(entries) == [[
        {"value": "auto-x", "label": "X", "mode": "webrtc", "group": "", "device": "",
         "status": "offline", "detail": _OFFLINE},
        {"id": "auto-y", "label": "Y", "group": "", "device": "", "status": "needs_setup",
         "detail": ""},
    ]]


@pytest.mark.asyncio
async def test_discovery_watches_these_keys_in_this_order_then_rebuilds():
    plugin, entries = _plugin()
    await plugin._setup_discovery()
    assert entries == [("subscribe", p) for p in (
        "device.*.preview_url",
        "device.*.preview_format",
        "device.*.preview_status",
        "device.*.preview_status_detail",
        "device.*.preview_setup_field",
        "device.*.label",
        "device.*.name",
        "device.*.connected",
        "device.*.online",
        "device.*.offline_detail",
    )] + [("state", "stream_ids", "[]")]
    assert {cb for _, cb in plugin.api.subscriptions} == {plugin._on_discovery_change}


@pytest.mark.asyncio
async def test_a_burst_of_changes_rebuilds_once(monkeypatch):
    monkeypatch.setattr(vp_mod, "_DISCOVER_DEBOUNCE_SECONDS", 0.05)
    plugin, entries = _plugin()
    plugin._on_discovery_change("device.a.name", "x", None)
    first = plugin._rebuild_task
    await asyncio.sleep(0.01)  # the first one is now waiting out its debounce
    plugin._on_discovery_change("device.a.name", "y", "x")
    await asyncio.sleep(0.01)
    # Superseded while it waited: it ends quietly rather than as cancelled.
    assert first.done() and not first.cancelled()
    await asyncio.wait_for(plugin._rebuild_task, 1.0)
    assert entries == [("state", "stream_ids", "[]")]


# ──── Loading the configured streams ────


@pytest.mark.asyncio
async def test_a_load_skips_what_is_not_a_stream_and_saves_only_a_moved_login():
    plugin, entries = _plugin()
    plugin.api._config = {"streams": [
        "not a dict", {"stream_id": "  "}, {"name": "no id"},
        _stream("a"), _stream("b", rtsp_url="rtsp://u:p@h/b"),
    ]}
    await plugin._load_streams()
    moved = _stream("b", rtsp_url="rtsp://h/b", username="u", password="p")
    assert plugin._streams == [_stream("a"), moved]
    assert entries == [
        _add("a", "rtsp://cam/a"),
        _add("b", "rtsp://u:p@h/b"),
        ("save", [_stream("a"), moved]),
    ]
    entries.clear()
    plugin.api._config = {"streams": [_stream("a")]}
    await plugin._load_streams()
    assert entries == [_add("a", "rtsp://cam/a")]
    plugin.api._config = {"streams": {"a": 1}}
    await plugin._load_streams()
    assert plugin._streams == [] and entries == [_add("a", "rtsp://cam/a")]


# ──── The status poll ────


class _Sup:
    running = True
    pid = 7


def _polled(streams, items, *, answer=...):
    plugin, entries = _plugin(streams, items=items)
    if answer is not ...:
        plugin.sidecar["answer"] = answer
    plugin._supervisor = _Sup()
    plugin._detect_local_ip = lambda: "192.0.2.10"
    plugin._rendered_host = "192.0.2.10"
    return plugin, entries


@pytest.mark.asyncio
async def test_the_poll_reads_available_before_ready_and_writes_every_stream():
    plugin, entries = _polled([_stream("a"), _stream("b"), _stream("c")], [
        {"name": "a", "available": False, "ready": True},
        {"name": "b", "ready": True},
        {"name": "", "available": True},
        {"available": True},
    ])
    await plugin._poll_statuses()
    assert entries == [
        ("get", "/v3/paths/list"),
        ("state", "running", True),
        ("state", "streams.a", "idle"),
        ("state", "streams.b", "streaming"),
        ("state", "streams.c", "idle"),
    ]


@pytest.mark.asyncio
async def test_the_poll_reads_a_null_item_list_as_empty():
    plugin, entries = _polled([_stream("a")], [], answer={"items": None})
    await plugin._poll_statuses()
    assert entries[1:] == [("state", "running", True), ("state", "streams.a", "idle")]


@pytest.mark.asyncio
async def test_the_poll_with_no_answer_or_not_our_sidecar_reports_not_running():
    plugin, entries = _polled([_stream("a")], [], answer=None)
    await plugin._poll_statuses()
    assert entries == [("get", "/v3/paths/list"), ("state", "running", False)]
    plugin, entries = _polled([_stream("a")], [{"name": "a", "available": True}])
    plugin._supervisor.running = False
    await plugin._poll_statuses()
    assert entries == [("get", "/v3/paths/list"), ("state", "running", False)]


@pytest.mark.asyncio
async def test_the_poll_stops_after_an_address_change_restart():
    plugin, entries = _polled([_stream("a")], [])
    plugin._rendered_host = "192.0.2.99"

    async def restart():
        entries.append(("restart",))

    plugin._restart_sidecar = restart
    await plugin._poll_statuses()
    assert entries == [
        ("log", "warning",
         "LAN address changed (192.0.2.99 -> 192.0.2.10); restarting MediaMTX so "
         "WebRTC advertises the new address"),
        ("restart",),
    ]


@pytest.mark.asyncio
async def test_a_source_nobody_can_reach_is_said_once_per_outage():
    plugin, entries = _polled([], [])
    plugin._discovered_sidecar = {"auto-s": "srt://enc:9000"}
    down = {"name": "auto-s", "available": False, "readers": [{"id": 1}]}
    said = (
        "log", "warning",
        "'auto-s' has a viewer but its source is not answering: srt://enc:9000. The "
        "OpenAVC server has to be able to reach that address itself -- the panel never "
        "connects to it directly. Check the device is streaming and that nothing "
        "between the two is blocking it.",
    )
    plugin._warn_unwatchable([down, {"name": "other", "readers": [1]}])
    plugin._warn_unwatchable([down])
    assert entries == [said]
    plugin._warn_unwatchable([{"name": "auto-s", "ready": True, "readers": [1]}])
    plugin._warn_unwatchable([down])
    assert entries == [said, said]
    plugin._warn_unwatchable([{"name": "auto-s", "readers": None}])
    plugin._warn_unwatchable([down])
    assert entries == [said, said, said]


# ──── start() ────


def _startable(monkeypatch, tmp_path, *, deps=("mediamtx", "ffmpeg"), busy=None,
               ready=True, streams=(), register=None):
    plugin, entries = _plugin(streams)
    plugin.api.data_dir = tmp_path
    if register is not None:
        plugin.api.register_router = register.__get__(plugin.api)
    found = {name: Path(f"/deps/{name}") for name in deps}

    def resolve(filename):
        entries.append(("resolve", filename))
        return found.get(filename)

    async def port_busy():
        entries.append(("port_check",))
        return busy

    async def wait_ready():
        entries.append(("ready_check",))
        return ready

    class _Supervisor:
        def __init__(self, cmd, *, name, log, on_status, on_circuit_break, task_factory):
            entries.append((
                "supervisor", cmd, name, log == plugin.api.log, on_status.__name__,
                on_circuit_break.__name__, task_factory == plugin.api.create_task,
            ))
            self.running = False

        async def start(self):
            entries.append(("supervisor_start", plugin._encoder_deadline is not None))
            self.running = True

        async def stop(self):
            entries.append(("supervisor_stop",))
            self.running = False

        def kill_now(self):
            entries.append(("kill",))

    monkeypatch.setattr(vp_mod, "SidecarSupervisor", _Supervisor)
    monkeypatch.setattr(vp_mod.sys, "platform", "linux")
    plugin._resolve_dep = resolve
    plugin._ensure_executable = lambda path: entries.append(("ensure_executable", str(path)))
    plugin._load_or_create_auth = lambda: "pw"
    plugin._detect_local_ip = lambda: "192.0.2.10"
    plugin._port_already_in_use = port_busy
    plugin._wait_until_ready = wait_ready
    return plugin, entries


_PANEL_PATHS = ["/whep/*", "GET /mjpeg/*", "GET /delivery/*", "GET /hls/*", "POST /report"]
_MEDIA_PATHS = ["/whep/*", "GET /mjpeg/*", "GET /hls/*"]


@pytest.mark.asyncio
async def test_a_start_brings_everything_up_in_this_order(monkeypatch, tmp_path):
    plugin, entries = _startable(monkeypatch, tmp_path, streams=[
        _stream("cam", rtsp_url="rtsp://u:p@h/x", name="Cam")])
    await plugin.start(plugin.api)
    config = tmp_path / "mediamtx.yml"
    assert entries[:13] == [
        ("resolve", "mediamtx"),
        ("resolve", "ffmpeg"),
        ("ensure_executable", "/deps/mediamtx"),
        ("ensure_executable", "/deps/ffmpeg"),
        ("state", "running", False),
        ("state", "error", ""),
        ("state", "sidecar", "starting"),
        ("port_check",),
        ("supervisor", ["/deps/mediamtx", str(config)], "mediamtx", True,
         "_on_sidecar_status", "_on_sidecar_circuit_break", True),
        ("supervisor_start", True),
        ("ready_check",),
        ("state", "running", True),
        ("register_router", {"panel_paths": _PANEL_PATHS}),
    ]
    moved = _stream("cam", rtsp_url="rtsp://h/x", name="Cam", username="u", password="p")
    rest = entries[13:]
    assert rest[:5] == [
        _add("cam", "rtsp://u:p@h/x"),
        ("save", [moved]),
        ("state", "stream_ids", json.dumps([{"value": "cam", "label": "Cam", "mode": "webrtc"}])),
        ("state", "streams.cam", "idle"),
        ("subscribe", "device.*.preview_url"),
    ]
    assert rest[-3:] == [
        ("state", "stream_ids", json.dumps([{"value": "cam", "label": "Cam", "mode": "webrtc"}])),
        ("periodic", "_poll_statuses", 5.0, "status_poll"),
        ("log", "info", "Video Panel started: MediaMTX 1.18.2 on 127.0.0.1:9997, 1 stream(s)"),
    ]
    assert len(rest) == 5 + 9 + 3
    assert plugin._encoder_deadline is None
    assert plugin._auth_pass == "pw"
    assert plugin._ffmpeg_bin == Path("/deps/ffmpeg")
    assert config.read_text(encoding="utf-8") == plugin._render_config()
    assert plugin._rendered_host == "192.0.2.10"


@pytest.mark.asyncio
async def test_a_start_asks_for_the_media_budget_where_the_platform_offers_it(monkeypatch, tmp_path):
    def register(self, router, panel_paths=None, media_paths=None):
        self.entries.append(("register_router", {"panel_paths": panel_paths, "media_paths": media_paths}))

    plugin, entries = _startable(monkeypatch, tmp_path, register=register)
    await plugin.start(plugin.api)
    assert ("register_router", {"panel_paths": _PANEL_PATHS, "media_paths": _MEDIA_PATHS}) in entries


@pytest.mark.asyncio
async def test_a_start_without_mediamtx_refuses_before_anything_else(monkeypatch, tmp_path):
    plugin, entries = _startable(monkeypatch, tmp_path, deps=("ffmpeg",))
    msg = (
        "MediaMTX binary not found in plugin_repo/.deps/. Native dependencies "
        "install when the plugin is installed from the community repository; "
        "reinstall the Video Panel plugin."
    )
    with pytest.raises(vp_mod.PluginRefusal) as exc:
        await plugin.start(plugin.api)
    assert type(exc.value) is vp_mod.PluginRefusal and str(exc.value) == msg
    assert entries == [
        ("resolve", "mediamtx"),
        ("state", "running", False),
        ("state", "error", msg),
    ]
    assert plugin._ffmpeg_bin is None and plugin._supervisor is None
    assert not (tmp_path / "mediamtx.yml").exists()


@pytest.mark.asyncio
async def test_a_start_without_ffmpeg_carries_on(monkeypatch, tmp_path):
    plugin, entries = _startable(monkeypatch, tmp_path, deps=("mediamtx",))
    await plugin.start(plugin.api)
    assert entries[:4] == [
        ("resolve", "mediamtx"),
        ("resolve", "ffmpeg"),
        ("ensure_executable", "/deps/mediamtx"),
        ("state", "running", False),
    ]
    assert plugin._ffmpeg_bin is None and plugin._supervisor.running


@pytest.mark.asyncio
async def test_a_start_with_a_port_taken_refuses_after_writing_its_config(monkeypatch, tmp_path):
    plugin, entries = _startable(monkeypatch, tmp_path, busy=8889)
    msg = (
        "Port 8889 on this machine is already in use, so the video server cannot "
        "start. Another copy of it is most likely still running. Stop it, then "
        "start the Video Panel plugin again."
    )
    with pytest.raises(vp_mod.PluginRefusal) as exc:
        await plugin.start(plugin.api)
    # The type tells only where the platform is importable (PluginRefusal is
    # RuntimeError without it); run with the openavc checkout on PYTHONPATH.
    assert type(exc.value) is vp_mod.PluginRefusal and str(exc.value) == msg
    assert entries[4:] == [
        ("state", "running", False),
        ("state", "error", ""),
        ("state", "sidecar", "starting"),
        ("port_check",),
        ("state", "error", msg),
        ("state", "sidecar", "failed"),
    ]
    assert (tmp_path / "mediamtx.yml").exists() and plugin._supervisor is None
    assert plugin._encoder_deadline is None


@pytest.mark.asyncio
async def test_a_start_whose_sidecar_never_answers_stops_it_and_raises(monkeypatch, tmp_path):
    plugin, entries = _startable(monkeypatch, tmp_path, ready=False)
    with pytest.raises(RuntimeError) as exc:
        await plugin.start(plugin.api)
    assert type(exc.value) is RuntimeError
    assert str(exc.value) == "MediaMTX did not respond on 127.0.0.1:9997 within 10s"
    assert entries[-3:] == [("supervisor_start", True), ("ready_check",), ("supervisor_stop",)]
    assert plugin._supervisor is None and plugin._encoder_deadline is None


@pytest.mark.asyncio
async def test_a_start_that_fails_after_registering_still_stops_the_sidecar(monkeypatch, tmp_path):
    plugin, entries = _startable(monkeypatch, tmp_path)

    async def boom(pattern, callback):
        raise ValueError("no subscriptions today")

    plugin.api.state_subscribe = boom
    with pytest.raises(ValueError):
        await plugin.start(plugin.api)
    assert entries[-1] == ("supervisor_stop",)
    assert ("register_router", {"panel_paths": _PANEL_PATHS}) in entries
    assert plugin._supervisor is None and plugin._encoder_deadline is None
