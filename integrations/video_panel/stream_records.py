"""What a configured video stream holds, and its login.

A stream is a dict in the plugin's ``streams`` config: ``stream_id`` (a
MediaMTX path name and a panel binding value), ``name``, ``rtsp_url`` (the
source address, whatever its scheme), ``username`` and ``password`` (the
source's login), and three playback settings (``codec_hint``, ``transcode``,
``hardware_accel``).

The login is kept in the two fields, never in the address: a login written
into the address moves into the fields on every door that writes a record
(the load at start, an add, an edit), and is put back into the address only
when the source is dialled. The password itself never leaves the plugin: the
Programmer is shown :data:`SAVED_LOGIN` in its place and sends that string
back to mean "keep the saved one", on a save and on a Test.

The add and edit routes refuse with the HTTP status and the sentence the
plugin page shows (the statuses are part of the rule, hence HTTPException
here). What a record does to the sidecar, the project file and the state
keys stays with the plugin.
"""

import re
from urllib.parse import quote, unquote

from fastapi import HTTPException

# What a stream's password reads as anywhere outside this plugin: the stream
# list the Programmer draws, and what it sends back for "keep the saved one".
# The same string OpenAVC puts in a project field whose login is saved on the
# system, so the Programmer shows it as "Saved on this system"; an older
# Programmer shows it as a filled password box and sends it back unchanged,
# which keeps the password too.
try:
    from openavc.core.device_logins import SAVED_LOGIN
except ImportError:  # OpenAVC before the login file
    SAVED_LOGIN = "<saved on this system>"

# Stream ids become MediaMTX path names and panel-element binding values, so
# keep them to a portable, URL-safe character set.
ID_RE = re.compile(r"[A-Za-z0-9_-]+")

# The three playback settings. Each reads as "auto" when the form leaves it
# empty.
SETTINGS = ("codec_hint", "transcode", "hardware_accel")


def _trimmed(value):
    return (value or "").strip()


def find(streams, stream_id):
    for s in streams:
        if s.get("stream_id") == stream_id:
            return s
    return None


def validate_url(url):
    if not url or "://" not in url:
        raise HTTPException(422, "Source URL must be a full URL, e.g. rtsp://host:554/stream.")


def validate_stream_id(stream_id):
    if not ID_RE.fullmatch(stream_id or ""):
        raise HTTPException(
            422,
            "Stream ID may contain only letters, numbers, hyphens, and underscores.",
        )


def _id_taken(stream_id):
    return HTTPException(409, f"A stream with id '{stream_id}' already exists.")


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return slug or "stream"


def unique_stream_id(streams, name):
    base = slugify(name)
    sid = base
    n = 2
    while find(streams, sid):
        sid = f"{base}_{n}"
        n += 1
    return sid


def new_record(streams, data, log=None):
    """The record an add makes from the form, or the refusal: the address,
    then the id (the form's, else one made from the name), then a clash."""
    validate_url(data.rtsp_url)
    stream_id = (data.stream_id or "").strip() or unique_stream_id(streams, data.name)
    validate_stream_id(stream_id)
    if find(streams, stream_id):
        raise _id_taken(stream_id)
    entry = entry_from_input(data, stream_id)
    move_url_login(entry, log)
    return entry


def edited_record(streams, existing, data, log=None):
    """The record an edit of ``existing`` makes, or the refusal: the address,
    then the id (the form's, else the stream's own), then a clash with another
    stream. :data:`SAVED_LOGIN` keeps the stream's own password, whatever its
    id becomes."""
    validate_url(data.rtsp_url)
    stream_id = existing["stream_id"]
    new_id = (data.stream_id or "").strip() or stream_id
    validate_stream_id(new_id)
    if new_id != stream_id and find(streams, new_id):
        raise _id_taken(new_id)
    entry = entry_from_input(data, new_id, existing.get("password") or "")
    move_url_login(entry, log)
    return entry


def entry_from_input(data, stream_id, saved_password=""):
    """The stream entry for an add or edit. A password sent as
    SAVED_LOGIN keeps ``saved_password`` (the edited stream's own; an add
    has none)."""
    password = data.password or ""
    if password == SAVED_LOGIN:
        password = saved_password or ""
    return {
        "stream_id": stream_id,
        "name": _trimmed(data.name) or stream_id,
        "rtsp_url": _trimmed(data.rtsp_url),
        "username": data.username or "",
        "password": password,
        **{key: getattr(data, key) or "auto" for key in SETTINGS},
    }


def split_url_login(url):
    """``url`` written ``scheme://user:password@host/...`` as the address
    without its login, the username and the password, both unescaped.
    The username and password are None when the address carries no login,
    and the password alone is None when it carries a username only."""
    url = _trimmed(url)
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url, None, None
    ends = [i for i in (rest.find(c) for c in "/?#") if i != -1]
    end = min(ends) if ends else len(rest)
    authority, tail = rest[:end], rest[end:]
    userinfo, at, host = authority.rpartition("@")
    if not at:
        return url, None, None
    username, colon, password = userinfo.partition(":")
    return (
        f"{scheme}://{host}{tail}",
        unquote(username),
        unquote(password) if colon else None,
    )


def move_url_login(entry, log=None):
    """Move a login written into ``entry``'s address into its Username and
    Password. The address's login wins over the fields, since it is the
    one the stream was played with. Returns True when ``entry`` changed;
    says so through ``log`` when the move replaced what the fields held.

    An address with a password and no username is left as it is: the
    fields cannot carry a password without a username."""
    url, username, password = split_url_login(entry.get("rtsp_url"))
    if not username:
        return False
    replaced = _overwrites(entry, username, password)
    entry["rtsp_url"] = url
    entry["username"] = username
    if password is not None:
        entry["password"] = password
    if replaced and log is not None:
        log(
            f"Stream '{entry.get('stream_id', '')}': the login written into its "
            "address replaced its Username and Password",
            level="warning",
        )
    return True


def _overwrites(entry, username, password):
    """Whether moving this login into ``entry``'s fields overwrites a
    different one they already held (a password of None moves nothing)."""
    if entry.get("username") and entry["username"] != username:
        return True
    return bool(
        password is not None and entry.get("password") and entry["password"] != password
    )


def source_url(url, username, password):
    """The address MediaMTX or ffmpeg dials: ``url`` with the login put
    back into it, or None when ``url`` is not a full URL. An address that
    already carries a login is left as authored."""
    url = _trimmed(url)
    if not url or "://" not in url:
        return None
    if not username:
        return url
    scheme, _, rest = url.partition("://")
    authority = rest.split("/", 1)[0]
    if "@" in authority:
        return url
    cred = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return f"{scheme}://{cred}{rest}"


def stream_source_url(stream):
    return source_url(
        stream.get("rtsp_url") or "", stream.get("username") or "", stream.get("password") or ""
    )


def password_for_test(streams, body):
    """The password a Test of ``body`` uses: the one it sent, or, for
    SAVED_LOGIN, the saved one it stands for: the stream named in ``body``,
    else the stream at the same address with the same username, else none."""
    if body.password != SAVED_LOGIN:
        return body.password
    stream = _stream_tested(streams, body)
    return (stream or {}).get("password") or ""


def _stream_tested(streams, body):
    """The saved stream a Test means: the one it names, else the first at
    the same address with the same username (a Programmer that names none)."""
    named = find(streams, body.stream_id) if body.stream_id else None
    if named is not None:
        return named
    address = _trimmed(body.rtsp_url)
    username = body.username or ""
    return next(
        (
            s for s in streams
            if _trimmed(s.get("rtsp_url")) == address
            and (s.get("username") or "") == username
        ),
        None,
    )


def programmer_view(stream, live):
    """A stream as the Programmer sees it: its password, when it has one,
    is SAVED_LOGIN, never the password itself."""
    return {
        **stream,
        "password": SAVED_LOGIN if stream.get("password") else "",
        "status": "streaming" if live.get(stream["stream_id"]) else "idle",
    }
