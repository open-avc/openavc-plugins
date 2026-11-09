"""Routes, the presets saved from them, and the subscription requests that
set them.

DDM routes audio by subscription: a receive channel names the device and
channel it takes its audio from, set through one mutation per receiving device
whose items are ``subscription(index, device, channel)``; blanks unsubscribe.

A preset is a named list of routes, each
``{tx_device, tx_channel, rx_device, rx_channel_index}`` (the shape
``snapshot`` takes from the domain), kept in the plugin's config under
``_presets`` and edited by name from the Programmer's matrix toolbar. Recalling
one clears every routed channel first (``clear_requests``), then sends each
receiving device its routes in one request (``by_receiver``, then
``subscriptions`` once the device is found). The plugin sends the requests,
saves the config and says which preset is showing; this module holds what
they contain and the sentences refusing an edit.
"""

try:
    from . import channel_keys
except ImportError:  # loaded by a loader that is not a package one
    import channel_keys

PRESETS_KEY = "_presets"


def subscription(index, device: str = "", channel: str = "") -> dict:
    """One receive channel's item in a subscription request: the source it
    takes, blank for none."""
    return {"rxChannelIndex": index, "subscribedDevice": device, "subscribedChannel": channel}


def stored_presets(config: dict) -> dict:
    """The presets the config holds by name; none when they are not a mapping."""
    presets = config.get(PRESETS_KEY, {})
    if not isinstance(presets, dict):
        return {}
    return presets


def with_presets(config: dict, presets: dict) -> dict:
    """A copy of the config holding these presets, ready to save."""
    saved = dict(config)
    saved[PRESETS_KEY] = presets
    return saved


def checked_name(payload: dict, presets: dict, verb: str, must_exist: bool):
    """The preset a save, update or delete names, and the sentence refusing
    it, or None: a blank name, a save naming one that exists, an update or
    delete naming one that does not."""
    name = (payload.get("name") or "").strip()
    if not name:
        return name, f"{verb} preset: name is required"
    if must_exist and name not in presets:
        return name, f"Preset '{name}' does not exist"
    if not must_exist and name in presets:
        return name, f"Preset '{name}' already exists"
    return name, None


def snapshot(devices) -> list[dict]:
    """Every routed receive channel as a preset route, in device order."""
    routes = []
    for device in devices:
        for channel in device.get("rxChannels") or []:
            source = channel_keys.source_of(channel)
            if source:
                routes.append({
                    "tx_device": source[0],
                    "tx_channel": source[1],
                    "rx_device": device["name"],
                    "rx_channel_index": channel["index"],
                })
    return routes


def clear_requests(devices):
    """Each device with a routed receive channel and the request that
    unroutes them, as it reads the devices."""
    for device in devices:
        unroute = [
            subscription(channel["index"])
            for channel in device.get("rxChannels") or []
            if channel_keys.source_of(channel)
        ]
        if unroute:
            yield device["id"], unroute


def by_receiver(routes: list[dict]) -> dict[str, list[dict]]:
    """A preset's routes grouped by the receiving device's name, in the order
    each first appears."""
    grouped: dict[str, list[dict]] = {}
    for route in routes:
        grouped.setdefault(route.get("rx_device", ""), []).append(route)
    return grouped


def subscriptions(routes: list[dict]) -> list[dict]:
    """One receiving device's preset routes as subscription items. A missing
    or empty channel index is 0, and a source not given is blank."""
    items = []
    for route in routes:
        index = route.get("rx_channel_index", 0)
        items.append(subscription(
            int(index) if index else 0,
            route.get("tx_device", ""),
            route.get("tx_channel", ""),
        ))
    return items
