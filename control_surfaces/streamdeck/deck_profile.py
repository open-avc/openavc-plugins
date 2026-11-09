"""
What a Stream Deck says about itself when it opens.

The plugin opens every deck the same way, whatever carries it: a USB deck the
library enumerates, a virtual deck (virtual_deck.py) or a network deck
(net_deck.py). What differs between them is read here: which transport
carries the deck and at what address, the name the plugin holds it under,
its serial and model, and its geometry. The geometry comes from the deck
itself, not a model table, so a model the library adds later still draws.

Every geometry field is published as a state key per deck, and the
un-prefixed keys mirror the primary deck. UNCONNECTED_GEOMETRY is what the
plugin publishes for those keys before any deck is attached; ``visual``,
``virtual`` and ``transport`` have no unconnected value and appear only with
a deck. A new geometry field is one entry in ``deck_geometry`` and, when the
status card needs it before a deck attaches, one in UNCONNECTED_GEOMETRY.

A deck's keys are numbered LCD keys first, then its touch keys (a Neo's eight
LCD keys are 0-7 and its two touch keys 8 and 9); ``key_total``,
``touch_key_indexes`` and ``is_touch_key`` read that numbering from the deck.

The identity is read before the geometry, and a deck that fails mid-read
raises out of the open: a network deck's link is told the serial it reported
between the two reads (``tell_link_serial``), so a deck whose layout cannot
be read has still been recorded on its link.
"""

from dataclasses import dataclass

from .virtual_deck import VirtualDeck

# The serial of a deck that reports none.
UNKNOWN_SERIAL = "unknown"

# The geometry keys a status card reads, as published before any deck.
UNCONNECTED_GEOMETRY = (
    ("key_count", 0),
    ("rows", 0),
    ("columns", 0),
    ("dial_count", 0),
    ("touch_key_count", 0),
    ("has_touchscreen", False),
    ("has_info_screen", False),
)


@dataclass(frozen=True)
class DeckIdentity:
    """Who a deck is and what carries it."""

    device_id: object
    serial: str
    model: str
    is_virtual: bool
    is_network: bool
    address: str
    transport: str


def deck_identity(deck):
    """What an open deck reports about itself: its transport (``virtual``,
    ``network`` or ``usb``) and a network deck's address, the name the plugin
    holds it under (its HID path, or one made from the object when the path
    cannot be read), its serial (``unknown`` when it reports none) and its
    model."""
    is_virtual = isinstance(deck, VirtualDeck)
    net_device = getattr(deck, "device", None)
    is_network = bool(getattr(net_device, "is_network", False))
    address = net_device.address if is_network else ""
    transport = "virtual" if is_virtual else "network" if is_network else "usb"
    try:
        device_id = deck.id()
    except Exception:
        device_id = f"deck-{id(deck)}"
    return DeckIdentity(
        device_id=device_id,
        serial=deck.get_serial_number() or UNKNOWN_SERIAL,
        model=deck.deck_type(),
        is_virtual=is_virtual,
        is_network=is_network,
        address=address,
        transport=transport,
    )


def tell_link_serial(deck, identity):
    """Record the serial a network deck reported on the link it came up on;
    the watchdog persists it to the deck's config entry so discovery can
    follow the deck if its address changes later (DHCP renumbering). Nothing
    for any other deck, a deck with no link, or one that reported no serial."""
    if not identity.is_network or identity.serial == UNKNOWN_SERIAL:
        return
    link = getattr(getattr(deck, "device", None), "link", None)
    if link is not None:
        link.observed_serial = identity.serial


def deck_geometry(deck, identity):
    """The deck's layout as the plugin publishes it. An info screen counts
    when it reports a width; a deck that cannot say whether it has a screen
    is taken to have one."""
    rows, columns = deck.key_layout()
    try:
        has_info_screen = deck.screen_image_format()["size"][0] > 0
    except Exception:
        has_info_screen = False
    try:
        is_visual = bool(deck.is_visual())
    except Exception:
        is_visual = True
    return {
        "key_count": deck.key_count(),
        "rows": rows,
        "columns": columns,
        "dial_count": deck.dial_count(),
        "touch_key_count": deck.touch_key_count(),
        "has_touchscreen": deck.is_touch(),
        "has_info_screen": has_info_screen,
        # False for display-less decks (foot pedals): keys fire actions but
        # nothing renders, so the editor skips display-only flows.
        "visual": is_visual,
        "virtual": identity.is_virtual,
        "transport": identity.transport,
    }


def key_total(deck):
    """How many keys a deck has: its LCD keys and its touch keys."""
    return deck.key_count() + deck.touch_key_count()


def touch_key_indexes(deck):
    """The indexes of a deck's touch keys, numbered after its LCD keys."""
    first = deck.key_count()
    return range(first, first + deck.touch_key_count())


def is_touch_key(deck, index):
    """True for an index past a deck's LCD keys and within its touch keys."""
    first = deck.key_count()
    return first <= index < first + deck.touch_key_count()


def connected_line(identity, geometry):
    """The log line that says a deck connected and what it has."""
    extras = ", touchscreen" if geometry["has_touchscreen"] else ""
    if geometry["touch_key_count"]:
        extras += f", {geometry['touch_key_count']} touch keys"
    return (
        f"Connected to {identity.model} (S/N: {identity.serial}, "
        f"{geometry['key_count']} keys, {geometry['rows']}x{geometry['columns']}, "
        f"{geometry['dial_count']} dials{extras})"
    )
