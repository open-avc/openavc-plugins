"""What a dial's entry runs, the value a turn or a fader touch writes, and the
touch strip zones a deck lays out.

A dial gesture is a push, its release, and the turns in between. The plugin
keeps the gesture's state (when the push began, whether the dial turned while
held) and runs what the readers here choose; nothing here touches a deck or
the state store.

The touch strip shows the zones its config declares, or, with none, one zone
under each dial that shows and runs what that dial does (``dial_zone``).

Two rules worth knowing before changing this:

- What makes a push wait for its release is not quite what routes a turn made
  while the dial is held. Any ``pressed_adjust`` object defers the push
  (``push_deferred``), but only one naming a key routes the held turn
  (``held_turn_routed``): a dial with ``pressed_adjust: {}`` waits for the
  release and still turns as it does unheld.
- A dial's ``adjust`` and ``pressed_adjust``, and a zone's ``drag_adjust``,
  change a value only when they name the key they write (``live_adjust``).
  A turn steps the value from what it reads now (``turned_value``); a touch
  on a fader zone (``zone_fader``) sets it from where the touch lands
  (``fader_value``), and only an adjust with a ``min`` below its ``max`` can
  do that. Either writes a whole
  number as an int.
"""

from .conditions import coerce_numeric
from .key_entries import action_list, objects

_DEFAULT_HOLD_MS = 500

# The fields that make a dial worth a zone under it on the strip.
_SHOWN_FIELDS = (
    "label", "icon", "adjust", "press", "long_press",
    "cw", "ccw", "touch", "long_touch",
    "pressed_adjust", "pressed_cw", "pressed_ccw",
)


def dial_entry(cfg, index):
    """The ``dials`` entry for a dial index, or None."""
    return next((d for d in objects(cfg.get("dials", [])) if d.get("index") == index), None)


def live_adjust(value):
    """An adjust object that names the key it writes, or None."""
    return value if isinstance(value, dict) and value.get("key") else None


def _has_pressed_actions(entry):
    return bool(action_list(entry.get("pressed_cw")) or action_list(entry.get("pressed_ccw")))


def push_deferred(entry):
    """True when a dial's push waits for the release, so a click can be told
    from a hold or a push-and-turn: it has a ``long_press``, a
    ``pressed_adjust`` object or pressed turn actions. A ``press``-only dial
    fires on the push."""
    if not entry:
        return False
    return bool(
        action_list(entry.get("long_press"))
        or isinstance(entry.get("pressed_adjust"), dict)
        or _has_pressed_actions(entry)
    )


def held_turn_routed(entry):
    """True when a turn made while the dial is held runs the ``pressed_*``
    entries in place of ``adjust`` and ``cw``/``ccw``: a live
    ``pressed_adjust``, or pressed turn actions."""
    return bool(live_adjust(entry.get("pressed_adjust")) or _has_pressed_actions(entry))


def hold_seconds(entry):
    """How long a push is held before its release runs ``long_press``:
    ``hold_threshold_ms`` when it is a positive number, else half a second."""
    threshold = coerce_numeric(entry.get("hold_threshold_ms"))
    return (threshold if threshold and threshold > 0 else _DEFAULT_HOLD_MS) / 1000.0


def release_actions(entry, held):
    """What a deferred push runs at its release, ``held`` seconds after the
    push: ``long_press`` once held long enough (when it has any), else
    ``press``."""
    long_actions = action_list(entry.get("long_press"))
    if held >= hold_seconds(entry) and long_actions:
        return long_actions
    return action_list(entry.get("press"))


def turn_route(entry, detents, held):
    """The adjust (None when it names no key) and the turn binding a turn of
    ``detents`` runs: the ``pressed_*`` entries while the dial is held and
    routes a held turn, else ``adjust`` and ``cw``/``ccw``."""
    if held and held_turn_routed(entry):
        adjust, cw, ccw = "pressed_adjust", "pressed_cw", "pressed_ccw"
    else:
        adjust, cw, ccw = "adjust", "cw", "ccw"
    return live_adjust(entry.get(adjust)), entry.get(cw if detents > 0 else ccw)


def whole(value):
    """A value to write, as an int when it is a whole number (5.0 -> 5)."""
    return int(value) if float(value).is_integer() else value


def turned_value(adjust, current, detents):
    """The value a turn of ``detents`` writes through ``adjust``: ``current``
    read as a number (else the ``min``, else 0), moved ``step`` per detent
    (1 when unset), and held to whichever of ``min`` and ``max`` are set."""
    step = coerce_numeric(adjust.get("step"))
    if step is None:
        step = 1
    minimum = coerce_numeric(adjust.get("min"))
    maximum = coerce_numeric(adjust.get("max"))
    start = coerce_numeric(current)
    if start is None:
        start = minimum if minimum is not None else 0
    value = start + step * detents
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return whole(value)


def zone_fader(zone):
    """A zone's live ``drag_adjust`` when it is a fader (``fader`` set: a
    touch sets its value from where it lands), else None."""
    drag = live_adjust(zone.get("drag_adjust"))
    if drag is not None and drag.get("fader"):
        return drag
    return None


def fader_bounds(drag):
    """A fader's ``(min, max)`` as numbers; None unless both are set and
    ``max`` is above ``min``."""
    minimum = coerce_numeric(drag.get("min"))
    maximum = coerce_numeric(drag.get("max"))
    if minimum is None or maximum is None:
        return None
    if maximum <= minimum:
        return None
    return minimum, maximum


def fader_value(drag, zone, x):
    """The value a touch at pixel ``x`` writes through a fader ``drag`` on
    ``zone``: where ``x`` falls across the zone's width (held to its edges),
    mapped onto ``min``..``max``, snapped to the ``step`` grid from ``min``
    (1 unless positive) and held to the bounds; None without usable bounds
    (``fader_bounds``)."""
    bounds = fader_bounds(drag)
    if bounds is None:
        return None
    minimum, maximum = bounds
    step = coerce_numeric(drag.get("step"))
    if step is None or step <= 0:
        step = 1
    zx = zone.get("x", 0)
    zw = max(1, zone.get("w", 1))
    frac = max(0.0, min(1.0, (x - zx) / zw))
    value = minimum + frac * (maximum - minimum)
    value = minimum + round((value - minimum) / step) * step
    return whole(max(minimum, min(maximum, value)))


def shows_on_strip(entry):
    """True when a dial sets anything a zone under it would show or run."""
    return any(entry.get(field) for field in _SHOWN_FIELDS)


def _own_or(entry, own, fallback):
    value = entry.get(own)
    return value if value is not None else entry.get(fallback)


def dial_zone(entry):
    """The zone under a dial when the strip declares none: the dial's label,
    icon, unit and meter, its adjust value, its press on a tap and its long
    press on a long touch (unless it sets ``touch`` / ``long_touch`` itself),
    and its adjust on a swipe, set from the touch position when ``fader``."""
    adjust = entry.get("adjust")
    live = live_adjust(adjust)
    drag = None
    if live is not None:
        drag = dict(live)
        if entry.get("fader"):
            drag["fader"] = True
    return {
        "label": entry.get("label", ""),
        "icon": entry.get("icon"),
        "unit": entry.get("unit"),
        "meter": entry.get("meter"),
        "value_source": adjust.get("key", "") if isinstance(adjust, dict) else "",
        "touch": _own_or(entry, "touch", "press"),
        "long_touch": _own_or(entry, "long_touch", "long_press"),
        "drag_adjust": drag,
    }


def declared_zones(cfg):
    """The zones the config's ``touchscreen`` declares, objects only."""
    touchscreen = cfg.get("touchscreen", {})
    return objects(touchscreen.get("zones")) if isinstance(touchscreen, dict) else []


def lay_out_zones(cfg, dial_count, width):
    """The strip's zones with their pixel bounds and index: the declared
    zones, else one under each dial (``dial_count()`` is asked only then). A
    zone with no ``x`` or ``w`` takes its share of an even split of
    ``width``."""
    zones = declared_zones(cfg) or [
        dial_zone(dial_entry(cfg, i) or {}) for i in range(dial_count())
    ]
    if not zones:
        return []
    slot = width // len(zones)
    resolved = []
    for i, zone in enumerate(zones):
        x, w = zone.get("x"), zone.get("w")
        x = int(x) if isinstance(x, (int, float)) else i * slot
        w = int(w) if isinstance(w, (int, float)) else slot
        resolved.append({**zone, "x": x, "w": w, "index": i})
    return resolved
