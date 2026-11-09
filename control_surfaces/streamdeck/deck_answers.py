"""
What a discovery (mDNS) answer from a network deck says: the serial it
advertises, the address it answers at, and the line the IDE's Add network
deck dialog lists for it.

Two readers ask: the scan route (every Elgato answer with an address, each
saying whether the config already has it) and the look-up that follows a
configured deck to a new address (net_deck.py's DockLink claims it; the
plugin runs it). Both read the serial and the address here, so an answer
the scan lists under one serial is the one the look-up matches by it.
"""

from .net_deck import NET_DEFAULT_PORT

# The device type a Network Dock advertises (``dt`` in its TXT record).
_DOCK_DEVICE_TYPE = "215"


def answer_serial(result):
    """The serial an answer advertises, as text ("" for none)."""
    return str((result.get("txt") or {}).get("sn", ""))


def answer_address(result):
    """``(host, port)`` an answer gives: "" for no host, NET_DEFAULT_PORT
    for no port."""
    return result.get("ip") or "", result.get("port") or NET_DEFAULT_PORT


def scan_listing(results, entries):
    """The decks a scan lists, in the order they answered: each answer from
    an Elgato service (``_elg.``) that gives a host. ``entries`` are the
    configured network decks; a listed deck is ``already_added`` when one
    has its address, or its host on any port."""
    keys = {entry["key"] for entry in entries}
    hosts = {entry["host"] for entry in entries}
    return [_listing(result, keys, hosts) for result in results if _listed(result)]


def _listed(result):
    """True for an answer from an Elgato service that gives a host."""
    service = str(result.get("service_type") or "")
    return service.startswith("_elg.") and bool(answer_address(result)[0])


def _listing(result, keys, hosts):
    """The line a scan lists for one answer; ``keys`` and ``hosts`` are the
    configured decks' addresses and hosts."""
    host, port = answer_address(result)
    return {
        "host": host,
        "port": port,
        "name": result.get("instance_name") or result.get("hostname") or host,
        "serial": answer_serial(result),
        "kind": _kind(result),
        "already_added": f"{host}:{port}" in keys or host in hosts,
    }


def _kind(result):
    """``"Network Dock"`` for a dock's answer, else ``"Stream Deck"``."""
    txt = result.get("txt") or {}
    return "Network Dock" if str(txt.get("dt", "")) == _DOCK_DEVICE_TYPE else "Stream Deck"
