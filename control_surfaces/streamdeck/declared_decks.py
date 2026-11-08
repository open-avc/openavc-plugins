"""
The decks the config declares beyond the ones plugged in over USB: the
``virtual_decks`` list (software decks, virtual_deck.py) and the
``network_decks`` list (decks reached over TCP, net_deck.py).

The watchdog reads these lists to know which decks to run, and the config
check (config_check.py) reads each entry the same way, through the helpers
here, to say which ones the watchdog would skip.
"""

import re

from .net_deck import NET_DEFAULT_PORT
from .virtual_deck import VIRTUAL_MODELS


def virtual_serial(item):
    """The serial a ``virtual_decks`` entry gives, cut to the characters a
    state key takes (letters, digits, - and _)."""
    return re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("serial", "")))


def _virtual_deck(item):
    """The ``{model, serial}`` one entry declares, or None when it names no
    model VIRTUAL_MODELS has or no serial."""
    if not isinstance(item, dict):
        return None
    model = item.get("model")
    serial = virtual_serial(item)
    if model not in VIRTUAL_MODELS or not serial:
        return None
    return {"model": model, "serial": serial}


def _first_of_each(raw, read, field):
    """``read`` on each item of a list, in order, dropping the items it
    answers None for and every entry whose ``field`` an earlier one took."""
    if not isinstance(raw, list):
        return []
    entries = []
    seen = set()
    for item in raw:
        entry = read(item)
        if entry is None or entry[field] in seen:
            continue
        seen.add(entry[field])
        entries.append(entry)
    return entries


def virtual_deck_entries(raw):
    """The decks a ``virtual_decks`` list declares, as ``{model, serial}``;
    a serial an earlier deck took is skipped."""
    return _first_of_each(raw, _virtual_deck, "serial")


def network_endpoint(item):
    """The ``(host, port)`` a ``network_decks`` entry names: the host with the
    spaces around it trimmed, and the port as a whole number (NET_DEFAULT_PORT
    when it is left out), or None when it is not one."""
    host = str(item.get("host", "")).strip()
    try:
        port = int(item.get("port", NET_DEFAULT_PORT))
    except (TypeError, ValueError):
        port = None
    return host, port


def _usable_port(port):
    return port is not None and 0 < port < 65536


def endpoint_key(host, port):
    """``"host:port"``, the address a deck is known by, for an endpoint the
    plugin connects to; None for one it skips (no host, or no port in 1-65535)."""
    if host and _usable_port(port):
        return f"{host}:{port}"
    return None


def _network_deck(item):
    """The deck one ``network_decks`` entry declares, or None when it names
    no address the plugin connects to."""
    if not isinstance(item, dict):
        return None
    host, port = network_endpoint(item)
    key = endpoint_key(host, port)
    if key is None:
        return None
    return {
        "host": host,
        "port": port,
        "key": key,
        # The deck's own serial (recorded after the first connect)
        "serial": str(item.get("serial", "") or ""),
        # The serial the unit advertises via discovery (recorded at add
        # time): for a dock this is the dock's serial, not the docked
        # deck's, so re-resolve accepts either.
        "mdns_sn": str(item.get("mdns_sn", "") or ""),
    }


def network_deck_entries(raw):
    """The decks a ``network_decks`` list declares, each with its ``key``; an
    address an earlier deck took is skipped."""
    return _first_of_each(raw, _network_deck, "key")
