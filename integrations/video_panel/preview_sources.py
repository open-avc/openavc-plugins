"""Sources a device offers for preview, read from the platform's state, and
the rows the stream picker is given.

The convention any driver can follow: ``preview_url`` (and optionally
``preview_format``) on a device (``device.<id>.``) or a child entity
(``device.<id>.<type>.<local id>.``) offers that scope as a stream with no
manual setup; ``preview_status`` (``ready``, ``needs_setup``,
``unavailable``), ``preview_status_detail`` and ``preview_setup_field`` say
why a scope has no stream yet and what would give it one. A driver that
publishes none of the optional keys is unaffected: absent means ready.

A scan sorts every scope into one of two maps. A playable source (a URL this
plugin can draw, its status absent or ``ready``) goes into ``discovered``,
which is what the play routes check; it stays there while its device is
unreachable, marked offline, because a page is built against the camera
whatever its state today. A scope with a status and nothing playable goes
into ``unusable``: listed with what is missing, never offered to a panel.
Whether a scope is reachable is the platform's to say, not the driver's:
the device's ``connected``, and a child's own ``online``.

Each new key of the convention is a pattern in :data:`WATCHED` and a read
in the scan; the picker's rows are built here too. Registering a playable
source with the sidecar, and what the sidecar learns about it, stays with
the plugin.
"""

import re

# Discovered sources' stream ids carry this prefix so they can never collide
# with a user-authored stream.
PREFIX = "auto-"

# The preview formats this plugin can actually render. A driver that declares
# something outside this set is SKIPPED rather than guessed at: a source we
# cannot draw is better absent from the picker than present and permanently
# broken. (Before SRT landed an unknown format fell back to mjpeg, which meant
# an <img> pointed at an srt:// URL -- a dead tile with no explanation.)
RENDERABLE_FORMATS = ("mjpeg", "rtsp", "srt")
# Of those, the ones MediaMTX ingests and re-serves as WebRTC. MJPEG is the odd
# one out: the plugin proxies it itself and the sidecar never sees it.
SIDECAR_FORMATS = ("rtsp", "srt")
# How a URL scheme maps to a format, for a driver that publishes a preview_url
# and leaves preview_format off -- the convention says the format is optional.
SCHEME_FORMATS = {
    "rtsp": "rtsp", "rtsps": "rtsp",
    "srt": "srt",
    "http": "mjpeg", "https": "mjpeg",
}

# What a source on a device the platform cannot reach says for itself. The
# driver's own sentence is about the source; this one is about the device, and
# it outranks the driver's EXCEPT where the driver named a setting to fill in
# -- typing a port number works perfectly well with the device switched off.
OFFLINE_DETAIL = (
    "This device is not connected right now. The source will play when it "
    "comes back."
)

# Every key whose change can change the picker. fnmatch ``*`` crosses dots, so
# one pattern per key covers both device-level (``device.<id>.preview_url``)
# and child-level (``device.<id>.<type>.<padded>.preview_url``) sources.
# ``label`` and ``name`` are watched too so a rename refreshes the dropdown
# text: the label falls back from ``label`` to ``name``, so both must trigger
# a rebuild (a device-reported ``name`` change is the common case when no
# user label is set).
WATCHED = (
    "device.*.preview_url",
    "device.*.preview_format",
    # The optional half of the convention: a source that has no stream
    # and something that could be done about it. A driver that never
    # publishes these is unaffected -- absent means ready.
    "device.*.preview_status",
    "device.*.preview_status_detail",
    "device.*.preview_setup_field",
    "device.*.label",
    "device.*.name",
    # Whether the device is reachable is nobody's key but the
    # platform's, and no driver reports it. preview_url is never
    # cleared on a disconnect, so without this an unplugged encoder
    # sits in the picker looking exactly like a working one.
    "device.*.connected",
    # And the same question one level down. A frame stays connected
    # while one of its encoders goes dark, so the device key never
    # moves and this is the only thing that says anything happened.
    "device.*.online",
    "device.*.offline_detail",  # the child half of it; see _unreachable_detail
)


async def scan(api, warned):
    """Every scope's source, as ``(discovered, unusable)``, keyed by stream id.

    A scope earns a row if it has a URL we can draw, or a status saying
    why it has not. The second kind is the point of the whole exercise:
    an SRT output one port number away from working used to publish
    nothing and be indistinguishable from a device that has no video at
    all. ``warned`` is the set of scopes already logged as undrawable, kept
    by the caller across scans so one is said once.
    """
    urls = await api.state_get_pattern("device.*.preview_url")
    statuses = await api.state_get_pattern("device.*.preview_status")
    reach = await _Reach.read(api)
    discovered, unusable = {}, {}
    for prefix in sorted(_scopes(urls, statuses)):
        url = _stripped(urls.get(f"{prefix}.preview_url"))
        status = _stripped(statuses.get(f"{prefix}.preview_status"))
        sid = discovered_stream_id(prefix)
        reachable = reach.reachable(prefix)
        if url and status in ("", "ready"):
            source = await _playable(api, prefix, url, reachable, warned)
            if source is not None:
                discovered[sid] = source
        elif status:
            unusable[sid] = await _unusable(api, prefix, status, reachable)
        # else: no stream and nothing to say -- not a source at all
    return discovered, unusable


def _scopes(urls, statuses):
    """``device.<id>[.<type>.<pad>]`` for every preview_url or preview_status key."""
    scopes = {key[: -len(".preview_url")] for key in urls}
    scopes.update(key[: -len(".preview_status")] for key in statuses)
    return scopes


def _stripped(value):
    return value.strip() if isinstance(value, str) else ""


async def _playable(api, prefix, url, reachable, warned):
    """A drawable scope's source, or None (said once) when nothing can draw it."""
    declared = await api.state_get(f"{prefix}.preview_format")
    fmt = preview_format(declared, url)
    if fmt is None:
        if prefix not in warned:
            warned.add(prefix)
            api.log(
                f"{prefix} offers a preview this plugin cannot draw "
                f"(format {declared!r}, URL {url!r}); it is not being "
                f"listed as a stream. Supported: "
                f"{', '.join(RENDERABLE_FORMATS)}.",
                "warning",
            )
        return None
    warned.discard(prefix)
    device_id = device_of(prefix)
    return {
        "label": await _label(api, prefix),
        "url": url,
        "format": fmt,
        "device": device_id,
        "group": await _device_label(api, device_id),
        # Offline is decided here, not by the driver: it is the one
        # thing about a source that the platform already knows and
        # no driver reports. "Here" means the sub-unit as well as
        # the box -- one encoder on a sixteen-encoder frame going
        # dark is the common case, and the frame stays connected
        # right through it.
        "status": "" if reachable else "offline",
        "detail": "" if reachable else await _unreachable_detail(api, prefix),
    }


async def _unusable(api, prefix, status, reachable):
    """A scope with a status and no stream: what is missing, and where to
    fill it in. A missing setting outranks an unreachable device: the number
    can be typed either way, and "connect the device" is not the next step
    when the next step is "enter the port"."""
    offline_ranks_below = reachable or status == "needs_setup"
    device_id = device_of(prefix)
    return {
        "label": await _label(api, prefix),
        "device": device_id,
        "group": await _device_label(api, device_id),
        "status": status if offline_ranks_below else "offline",
        "detail": (
            await _text(api, f"{prefix}.preview_status_detail")
            if offline_ranks_below
            else await _unreachable_detail(api, prefix)
        ),
        "setup_field": await _text(api, f"{prefix}.preview_setup_field"),
    }


class _Reach:
    """Whether each device and each child the platform knows is reachable.

    A frame with sixteen encoders is ONE connected device. Reading only the
    device's `connected` made every encoder on it look fine, including the
    one somebody had just unplugged: its row stayed in the picker with
    nothing to say, and a tile pointed at it reconnected forever to a source
    that had gone. The child's own `online` is the only key that knows, and
    it is the same key the device page draws its red dot from.
    """

    def __init__(self, devices, children):
        self.devices = devices  # device id -> connected
        self.children = children  # child scope -> online

    @classmethod
    async def read(cls, api):
        devices = {}
        for key, value in (await api.state_get_pattern("device.*.connected")).items():
            parts = key.split(".")
            # Only the DEVICE's own key. A child entity has an `online` of its
            # own -- the two answer different questions and a frame full of
            # encoders answers the device one for all of them at once.
            if len(parts) == 3:
                devices[parts[1]] = bool(value)
        children = {}
        for key, value in (await api.state_get_pattern("device.*.online")).items():
            # device.<id>.<type>.<local id>.online -- five parts. Nothing else
            # is a child, and the device's own reachability is `connected`.
            if key.count(".") == 4:
                children[key[: -len(".online")]] = bool(value)
        return cls(devices, children)

    def reachable(self, prefix):
        """Whether the thing at this scope can be reached at all right now.

        A child is unreachable when it says so OR when the device carrying it
        is down -- an encoder on a frame we have lost contact with is not
        online just because the last poll before the link dropped said it was.
        """
        if not self.devices.get(device_of(prefix), True):
            return False
        return self.children.get(prefix, True)


async def _unreachable_detail(api, prefix):
    """The sentence for a source that cannot be reached.

    A CHILD's own `offline_detail` is used when it has one: that vocabulary
    is written for whoever is standing in front of the gear ("Not
    answering. Check that it has power and a network connection."), and it
    names the sub-unit rather than the frame it is plugged into.

    A DEVICE's `offline_detail` is deliberately NOT used, even though it is
    sitting right there. It is written for the person configuring the
    system -- "Install it and make sure it's on the system PATH", "Wrong
    transport or protocol for this device?" -- and a wall panel is the last
    place that belongs. Same key name, different reader.
    """
    if prefix.count(".") == 3:  # device.<id>.<type>.<local id>
        detail = await _text(api, f"{prefix}.offline_detail")
        if detail:
            return detail
    return OFFLINE_DETAIL


def device_of(prefix):
    """The device id out of a `device.<id>[.<type>.<pad>]` scope."""
    parts = prefix.split(".")
    return parts[1] if len(parts) > 1 else ""


async def _device_label(api, device_id):
    """What to call the device a source belongs to, for grouping."""
    if not device_id:
        return ""
    return await _text(api, f"device.{device_id}.name") or device_id


async def _text(api, key):
    return _stripped(await api.state_get(key))


async def _label(api, prefix):
    """Friendly name for a discovered source: user label, else device name."""
    for key in (f"{prefix}.label", f"{prefix}.name"):
        value = await api.state_get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return discovered_stream_id(prefix)


def preview_format(declared, url):
    """How to render a discovered preview, or None if we cannot.

    The convention makes ``preview_format`` optional, so a URL on its own
    has to resolve — and a declared format we do not recognise is more
    likely a newer driver than a typo, in which case the scheme is the
    better evidence anyway. Returns None when nothing can draw it, which
    keeps a source we would only draw wrong out of the picker entirely.
    """
    fmt = (declared or "").strip().lower() if isinstance(declared, str) else ""
    if fmt in RENDERABLE_FORMATS:
        return fmt
    scheme = (url or "").split("://", 1)[0].strip().lower()
    return SCHEME_FORMATS.get(scheme)


def discovered_stream_id(prefix):
    """Stable, URL-safe stream id derived from a ``device.<id>...`` prefix."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", prefix[len("device."):]).strip("-")
    return f"{PREFIX}{slug or 'source'}"


def picker_rows(streams, discovered, unusable):
    """What the stream picker lists, in order: the configured streams, the
    discovered sources, then the scopes that cannot play. A configured stream
    takes precedence over a discovered one with the same id.

    Configured streams play over WebRTC; discovered sources carry a `mode` so
    the panel element knows whether to use the WHEP path or the MJPEG <img>
    path, and what the picker needs to be legible rather than a flat list of
    names: which device each belongs to, and whether it can be played. The
    dropdown's option parser ignores unknown fields, so the extra ones are
    backward-compatible.
    """
    configured = {s["stream_id"] for s in streams}
    return (
        [
            {"value": s["stream_id"], "label": s.get("name") or s["stream_id"], "mode": "webrtc"}
            for s in streams
        ]
        + [_source_row(sid, d) for sid, d in discovered.items() if sid not in configured]
        + [_unusable_row(sid, u) for sid, u in unusable.items() if sid not in configured]
    )


def _source_row(sid, d):
    row = {
        "value": sid,
        "label": d["label"],
        "mode": "mjpeg" if d["format"] == "mjpeg" else "webrtc",
        "group": d.get("group", ""),
        "device": d.get("device", ""),
    }
    if d.get("status"):
        # Offline, and only offline, reaches here -- it stays pickable.
        # A page is built long before the room is powered up, and a
        # camera that is off today is still the camera the page is for.
        row["status"] = d["status"]
        row["detail"] = d.get("detail") or OFFLINE_DETAIL
    return row


def _unusable_row(sid, u):
    # NO `value` KEY, and that is the whole design of this row.
    #
    # There is no stream behind it, so there is nothing to pick: it is
    # on screen to say what is missing. The shared option parser drops
    # any entry without a `value`, so on a platform whose picker
    # predates this the row simply is not there -- the behaviour before
    # any of this work -- instead of offering a page a tile that could
    # never draw. No version gate needed for that to be true.
    row = {
        "id": sid,
        "label": u["label"],
        "group": u.get("group", ""),
        "device": u.get("device", ""),
        "status": u["status"],
        "detail": u.get("detail", ""),
    }
    if u.get("setup_field") and u.get("device"):
        row["setup"] = {"device": u["device"], "field": u["setup_field"]}
    return row
