"""Which state keys a deck's config reads, and which part of the deck each one
redraws.

A deck subscribes to every key here (``DeckWatch.keys``) and, when one
changes, asks the same readers which parts read it: a key entry through
``button_watches_key``, a strip zone through ``zone_watches_key``, and the
four named sets for the parts that redraw as a whole (automatic paging, the
touch strip, the info strip, automatic brightness). A new field a key draws
from is added to ``button_keys`` alone, and the subscription and the redraw
follow it.

Two rules worth knowing before changing this:

- A zone is redrawn when its ``drag_adjust`` key changes, but only the dials'
  ``adjust`` keys are subscribed for the strip, not an explicit zone's
  ``drag_adjust``. The default zones (one per dial) take their value from that
  same adjust key, and a zone draws its value from ``value_source``; the drag
  key only sets a meter's bounds.
- A macro's run lights only keys: ``macro_keys`` maps each macro a key's press
  runs (any action in the list, and each one's ``off_action`` and
  ``hold_action``) to the ``(page, index)`` keys that show it, page None for a
  locked key (lit on whatever page is showing).
"""

from dataclasses import dataclass

from .conditions import condition_state_keys
from .config_check import unwrap_binding
from .key_entries import entries_on_page, global_buttons, objects, page_buttons, press_actions

_SOURCE_FIELDS = ("label_source", "value_source")


@dataclass
class DeckWatch:
    """What one deck's config view reads. ``keys`` is everything to subscribe;
    the named sets are the keys that redraw that part of the deck."""

    keys: set
    auto_page: set
    touch_strip: set
    info_strip: set
    brightness: set
    macro_keys: dict


def _feedback_key(element):
    feedback = element.get("feedback", {})
    if isinstance(feedback, dict) and feedback.get("key"):
        return [feedback["key"]]
    return []


def _display_keys(element):
    """A display element's live sources and its feedback key."""
    sources = [element[field] for field in _SOURCE_FIELDS if element.get(field)]
    return sources + _feedback_key(element)


def button_keys(btn):
    """The state keys a key entry draws from, in the order they are read:
    its live sources, its feedback key, its press toggle key and the keys its
    ``visible_when`` reads."""
    keys = [btn[field] for field in _SOURCE_FIELDS if btn.get(field)]
    bindings = btn.get("bindings", {})
    if not isinstance(bindings, dict):
        return keys
    keys += _feedback_key(bindings)
    press = unwrap_binding(bindings.get("press"))
    if isinstance(press, dict) and press.get("toggle_key"):
        keys.append(press["toggle_key"])
    return keys + condition_state_keys(bindings.get("visible_when"))


def button_watches_key(btn, key):
    """True when a key entry's drawing depends on a state key."""
    return key in button_keys(btn)


def zone_watches_key(zone, key):
    """True when a strip zone's drawing depends on a state key (its sources,
    its feedback key or its ``drag_adjust`` key)."""
    if key in (zone.get("label_source"), zone.get("value_source")):
        return True
    feedback = zone.get("feedback")
    if isinstance(feedback, dict) and feedback.get("key") == key:
        return True
    drag = zone.get("drag_adjust")
    return isinstance(drag, dict) and drag.get("key") == key


def keys_watching(cfg, page, key):
    """The key indexes live on ``page`` whose drawing reads ``key``, locked
    keys first. Yields one at a time, so each entry is judged just before the
    caller redraws the one ahead of it."""
    for btn in entries_on_page(cfg, page):
        if button_watches_key(btn, key):
            key_index = btn.get("index")
            if key_index is not None:
                yield key_index


def _rule_keys(rules):
    """Every key the ``when`` of a list of rules reads (auto_page,
    auto_brightness)."""
    keys = set()
    for rule in objects(rules):
        keys.update(condition_state_keys(rule.get("when")))
    return keys


def _dial_adjust_key(dial):
    adjust = dial.get("adjust")
    if isinstance(adjust, dict) and adjust.get("key"):
        return [adjust["key"]]
    return []


def _strip_keys(cfg):
    """The touch strip's keys: each explicit zone's display keys, and every
    dial's adjust key (the default zones show those values)."""
    touchscreen = cfg.get("touchscreen", {})
    zones = touchscreen.get("zones") if isinstance(touchscreen, dict) else None
    keys = set()
    for zone in objects(zones):
        keys.update(_display_keys(zone))
    for dial in objects(cfg.get("dials", [])):
        keys.update(_dial_adjust_key(dial))
    return keys


def _info_keys(info_items):
    keys = set()
    for element in info_items:
        keys.update(_display_keys(element))
    return keys


def _macros_run(action):
    """The macros one press action runs: itself, its ``off_action`` and its
    ``hold_action``, each when it is a macro action naming one."""
    runs = (action, action.get("off_action"), action.get("hold_action"))
    return [
        str(run["macro"]) for run in runs
        if isinstance(run, dict) and run.get("action") == "macro" and run.get("macro")
    ]


def _entry_macros(btn):
    """The macros a key entry's press runs, in order; none for an entry with
    no index (it has no key to light)."""
    if btn.get("index") is None:
        return []
    actions = press_actions(btn.get("bindings", {}))
    return [macro for action in actions for macro in _macros_run(action)]


def macro_keys(cfg):
    """Each macro a key's press runs, mapped to the ``(page, index)`` keys
    that show its run."""
    entries = [(btn, btn.get("page", 0)) for btn in page_buttons(cfg)]
    entries += [(btn, None) for btn in global_buttons(cfg)]
    marks = {}
    for btn, page in entries:
        for macro in _entry_macros(btn):
            marks.setdefault(macro, set()).add((page, btn["index"]))
    return marks


def deck_watch(cfg, info_items):
    """Everything a deck's config view reads. ``info_items`` is the info
    strip's elements as the deck lays them out."""
    keys = set()
    for btn in page_buttons(cfg) + global_buttons(cfg):
        keys.update(button_keys(btn))
    auto_page = _rule_keys(cfg.get("auto_page", []))
    touch_strip = _strip_keys(cfg)
    info_strip = _info_keys(info_items)
    brightness = _rule_keys(cfg.get("auto_brightness", []))
    for part in (auto_page, touch_strip, info_strip, brightness):
        keys |= part
    return DeckWatch(
        keys=keys, auto_page=auto_page, touch_strip=touch_strip,
        info_strip=info_strip, brightness=brightness, macro_keys=macro_keys(cfg),
    )
