"""What the touch strip and the info screen show when the config fills them,
and when it does not.

The info screen draws the elements its ``info_strip`` config names
(``info_items``); the touch strip draws its zones (dials.py). A screen with
nothing to draw is idle: it shows the clock, or stays blank when the config
asks (``strip_idle_mode``, ``info_idle_mode``). The two idle words are
``IDLE_MODES``, which the config check reads too. Nothing here draws or reads
state; the plugin draws what these say.

Two rules worth knowing before changing this:

- The info screen is idle unless its config names something to draw: an
  ``items`` entry that is an object, or one of ``_INFO_CONTENT_FIELDS``. A
  ``source`` of "clock" or "blank" wins over both. The touch strip is idle
  unless it declares a zone or a dial is worth a zone (dials.shows_on_strip),
  and only ``touchscreen.idle: "blank"`` keeps an idle strip blank.
- ``info_items`` reads a config the same way whether or not the screen is idle,
  so a deck watches the keys an idle screen's config names (watch_keys.py).
"""

from .dials import declared_zones, shows_on_strip
from .key_entries import objects

CLOCK = "clock"
BLANK = "blank"
IDLE_MODES = (CLOCK, BLANK)

# An info_strip config with any of these set draws an element, not the clock.
_INFO_CONTENT_FIELDS = (
    "key", "text", "label", "label_source", "value_source", "icon", "meter",
)

# How many items the info screen fits side by side.
_INFO_ITEMS_FIT = 2


def strip_idle_mode(cfg):
    """CLOCK or BLANK when a deck's config view has nothing for the touch
    strip (no declared zone, no dial worth a zone), else None."""
    if declared_zones(cfg) or any(map(shows_on_strip, objects(cfg.get("dials", [])))):
        return None
    ts = cfg.get("touchscreen", {})
    ts = ts if isinstance(ts, dict) else {}
    return BLANK if ts.get("idle") == BLANK else CLOCK


def info_idle_mode(info):
    """CLOCK or BLANK when an ``info_strip`` config draws nothing, or its
    ``source`` names one of them; else None."""
    if not isinstance(info, dict):
        return CLOCK
    source = info.get("source")
    if source in IDLE_MODES:
        return source
    if objects(info.get("items")) or any(info.get(f) for f in _INFO_CONTENT_FIELDS):
        return None
    return CLOCK


def _as_element(entry):
    """One info element from a config object, as a copy: a ``text`` source
    shows its ``text`` as a fixed value; any other reads its
    ``value_source``, else its ``key``."""
    element = dict(entry)
    if element.get("source") == "text":
        element["value_static"] = str(element.get("text", ""))
        element.pop("value_source", None)
    elif not element.get("value_source"):
        element["value_source"] = element.get("key", "")
    return element


def info_items(info, width):
    """The info screen's elements with their pixel bounds across ``width``:
    the first ``_INFO_ITEMS_FIT`` objects in ``items`` side by side, else the
    config itself as one element the full width; none for a config that is
    not an object."""
    if not isinstance(info, dict):
        return []
    items = [_as_element(item) for item in objects(info.get("items"))]
    items = items[:_INFO_ITEMS_FIT]
    if not items:
        return [{**_as_element(info), "x": 0, "w": width}]
    slot = width // len(items)
    return [{**item, "x": i * slot, "w": slot} for i, item in enumerate(items)]
