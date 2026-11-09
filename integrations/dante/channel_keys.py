"""The state keys a Dante domain's channels are published under, and a
routing-matrix row or column read back to the channel it names.

Each channel's keys are its device's name made into a key segment
(``sanitize_id``) and the channel's index: ``tx.<device>.<index>`` for a
transmit channel; ``rx.<device>.<index>``, ``route.<device>.<index>`` and
``route_info.<device>.<index>`` for a receive channel. A ``route`` value is
DDM's summary of the subscription, lowercased ("connected", "none", ...); a
``route_info`` value names its source ("MIC-01 / 01"), blank when unrouted.

The Programmer's routing matrix (``SURFACE_LAYOUT`` in dante_plugin.py) lists
its rows from the rx keys and its columns from the tx keys, and a click sends
back the text after ``rx.`` or ``tx.``; ``resolve_ref`` reads that text by
making each device's name into a segment again, so writing a key and reading a
click back are here side by side. The layout's cell pattern,
``route.<row>.<col>``, names one key per crosspoint, which is not published:
route status is published per receive channel only.

A receive channel is routed when it names a source device (``source_of``):
the published route text, the subscription count, a preset's snapshot and
clearing every route all read it that way.
"""


def sanitize_id(name: str) -> str:
    """A Dante device or channel name as a state key segment."""
    return name.lower().replace(" ", "_").replace("-", "_").replace(".", "_")


def source_of(channel: dict) -> tuple[str, str] | None:
    """The device and channel a receive channel takes its audio from, or None
    when it is not routed. A routed channel's source channel may be blank."""
    device = channel.get("subscribedDevice") or ""
    if not device:
        return None
    return device, channel.get("subscribedChannel") or ""


def _label(device: dict, channel: dict) -> str:
    return f"{device['name']} / {channel['name']}"


def channel_states(devices: list[dict]):
    """Each channel's state key and value, in the order they are published:
    a device's transmit channels, then each receive channel's name, route
    status and source. Yields as it reads, so a malformed device fails at the
    same key it would have been written at."""
    for device in devices:
        segment = sanitize_id(device["name"])
        for channel in device.get("txChannels") or []:
            yield f"tx.{segment}.{channel['index']}", _label(device, channel)
        for channel in device.get("rxChannels") or []:
            place = f"{segment}.{channel['index']}"
            yield f"rx.{place}", _label(device, channel)
            yield f"route.{place}", (channel.get("summary") or "NONE").lower()
            source = source_of(channel)
            yield f"route_info.{place}", f"{source[0]} / {source[1]}" if source else ""


def routed_count(devices: list[dict]) -> int:
    """How many receive channels across the devices are routed."""
    return sum(
        source_of(channel) is not None
        for device in devices
        for channel in device.get("rxChannels") or []
    )


def resolve_ref(devices, ref: str, direction: str) -> tuple[str | None, int | str | None]:
    """The channel a matrix row or column names: ``<device segment>.<index>``.

    A row (any direction but "tx") answers the device's name and the index,
    whether or not the device has that receive channel; a column ("tx")
    answers the device's name and the transmit channel's name, or nothing when
    the device has no transmit channel at that index. The first device whose
    name makes the segment is the one meant. (None, None) when nothing matches.
    """
    parts = ref.rsplit(".", 1)
    if len(parts) != 2:
        return None, None
    segment, index_text = parts
    device = _device_at(devices, segment)
    index = _index(index_text) if device else None
    if index is None:
        return None, None
    if direction != "tx":
        return device["name"], index
    return _transmit_channel(device, index)


def _device_at(devices, segment: str) -> dict | None:
    return next((d for d in devices if sanitize_id(d["name"]) == segment), None)


def _index(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def _transmit_channel(device: dict, index: int) -> tuple[str | None, str | None]:
    for channel in device.get("txChannels") or []:
        if channel["index"] == index:
            return device["name"], channel["name"]
    return None, None
