"""
What validate_config() reports: each part of a proposed config the deck
would skip or read as something else, as a sentence naming the part, what
is wrong with it and what the deck does instead.

Each rule mirrors the reader that uses the part, so the check never refuses
a shape the deck uses; the virtual and network deck lists are read through
the same helpers the watchdog uses (declared_decks.py). Nothing the Stream
Deck view can save is refused either, since it re-sends the whole config on
every edit: an unfinished entry (a rule with no state key yet, an action
with no type yet), and a value typed into one of its number boxes (a zone
position or width, a network port), are left to the deck to make of. A key
the deck would write but cannot (a panel control or a device state) is
refused: the view offers only variables there, and the deck would drop the
write at every press.

Every sentence is pinned word for word by the plugin's tests, since OpenAVC
shows them to the integrator and the AI assistant repeats them.
"""

import functools
import json

from .conditions import KNOWN_OPERATORS, coerce_numeric
from .declared_decks import endpoint_key, network_endpoint, virtual_serial
from .key_entries import ACTION_KINDS, unwrap_binding, write_door
from .pages import NEXT_PAGE, PREV_PAGE, page_number
from .screens import IDLE_MODES
from .virtual_deck import VIRTUAL_MODELS

_KNOWN_MODES = ("tap", "toggle", "hold_repeat", "tap_hold")
_LAYOUT_LISTS = ("buttons", "global_buttons", "auto_page", "dials", "auto_brightness")
_DIAL_ACTIONS = (
    "press", "long_press", "cw", "ccw", "pressed_cw", "pressed_ccw",
    "touch", "long_touch",
)
_PAGE_WORDS = (None, "", NEXT_PAGE, PREV_PAGE)
_SOURCE_FIELDS = ("label_source", "value_source", "key")

# The id the plugin runs under (PLUGIN_INFO["id"]): a key, dial or zone may
# write this plugin's own state or a user variable (key_entries.write_door).
_OWN_ID = "streamdeck"


# ──── Shared pieces ────


def _shown(value):
    return json.dumps(value) if isinstance(value, (str, int, float, bool)) else type(value).__name__


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_slot_number(value):
    """A key, dial or page number: a whole number, 0 or more."""
    return _is_number(value) and float(value).is_integer() and value >= 0


def _is_set_but_not(value, kind):
    """Present, and not of the type the deck reads it as."""
    return value is not None and not isinstance(value, kind)


def _join(prefix, name):
    return f"{prefix}.{name}" if prefix else name


def _each(items, path, check, *args):
    """``check`` on every entry of a list, each named ``path[i]``."""
    problems = []
    for i, item in enumerate(items):
        problems += check(item, f"{path}[{i}]", *args)
    return problems


def _each_serial(mapping, name, check):
    """``check`` on every value of an object keyed by deck serial, each
    named ``name.serial``; nothing when it is not an object."""
    if not isinstance(mapping, dict):
        return []
    problems = []
    for serial, value in mapping.items():
        problems += check(value, f"{name}.{serial}")
    return problems


def _number_problems(entry, path, fields, consequence):
    """Each of ``fields`` that is set and does not read as a number."""
    return [
        f"{_join(path, field)} {_shown(entry.get(field))} is not a number, so {consequence}."
        for field in fields
        if _is_set_but_number_less(entry.get(field))
    ]


def _is_set_but_number_less(value):
    return value is not None and coerce_numeric(value) is None


def _required_number_problems(entry, path, field, consequence):
    """One field that must be there and read as a number."""
    if entry.get(field) is None:
        return [f"{path} has no {field}, so {consequence}."]
    return _number_problems(entry, path, (field,), consequence)


def _source_problems(entry, path, consequence, fields=_SOURCE_FIELDS):
    """Each of ``fields`` that is set and is not a state key."""
    return [
        f"{path}.{field} is not a state key, so {consequence}."
        for field in fields
        if _is_set_but_not(entry.get(field), str)
    ]


def _leaves_alone(key):
    """A key the write rule has nothing to say about: not text or empty
    (another rule's, or not filled in yet), or one a deck may write."""
    return not isinstance(key, str) or not key or write_door(key, _OWN_ID) is not None


def _write_problems(key, path, consequence):
    if _leaves_alone(key):
        return []
    return [
        f"{path}: {_shown(key)} is not a variable (var.*) or the deck's own "
        f"plugin.{_OWN_ID}.* state, the only keys a deck writes, so {consequence}."
    ]


def _slot_problems(index, path, noun):
    """A key's or dial's ``index``."""
    if index is None:
        return [f"{path} has no index, so it is on no {noun}."]
    if _is_slot_number(index):
        return []
    return [
        f"{path}: index {_shown(index)} is not a {noun} number (0 or more), so it is on no {noun}."
    ]


# ──── Actions ────


def _kind_problems(kind, path):
    if kind in (None, "") or kind in ACTION_KINDS:
        return []
    return [
        f"{path}: action {_shown(kind)} is not one a deck runs "
        f"({', '.join(ACTION_KINDS)}), so it does nothing."
    ]


def _navigate_problems(action, path):
    page = action.get("page")
    if page in _PAGE_WORDS or page_number(page) is not None:
        return []
    return [
        f"{path}: page {_shown(page)} is not a page number, "
        f"\"__next_page__\" or \"__prev_page__\", so the key does not change page."
    ]


def _params_problems(action, path):
    if _is_set_but_not(action.get("params"), dict):
        return [f"{path}: params is not an object, so the command is sent without them."]
    return []


def _state_set_problems(action, path):
    return _write_problems(action.get("key"), path, "the action does nothing")


# What each action type reads beyond its type.
_ACTION_FIELDS = {
    "navigate": _navigate_problems,
    "device.command": _params_problems,
    "state.set": _state_set_problems,
}


def _action_problems(action, path):
    if not isinstance(action, dict):
        return [f"{path} is not an action, so it does nothing."]
    kind = action.get("action")
    problems = _kind_problems(kind, path)
    fields = _ACTION_FIELDS.get(kind) if isinstance(kind, str) else None
    if fields is not None:
        problems += fields(action, path)
    return problems


def _action_list_problems(value, path):
    """A binding that is one action or a list of them."""
    if value is None:
        return []
    if isinstance(value, dict):
        return _action_problems(value, path)
    if isinstance(value, list):
        return _each(value, path, _action_problems)
    return [f"{path} is not an action or a list of actions, so it does nothing."]


# ──── Conditions ────


def _unknown_operator(operator):
    if operator in (None, ""):
        return False
    return not isinstance(operator, str) or operator not in KNOWN_OPERATORS


def _group_problems(cond, group, path, consequence):
    """An ``all`` or ``any`` list inside a condition."""
    children = cond.get(group)
    if children is None:
        return []
    if not isinstance(children, list):
        return [f"{path}.{group} is not a list of conditions, so {consequence}."]
    return _each(children, f"{path}.{group}", _condition_problems, consequence)


def _condition_problems(cond, path, consequence):
    if not isinstance(cond, dict):
        return [f"{path} is not a condition object, so {consequence}."]
    problems = _group_problems(cond, "all", path, consequence)
    problems += _group_problems(cond, "any", path, consequence)
    if _is_set_but_not(cond.get("key"), str):
        problems.append(f"{path}.key is not a state key, so {consequence}.")
    operator = cond.get("operator")
    if _unknown_operator(operator):
        problems.append(
            f"{path}: operator {_shown(operator)} is not one the deck knows "
            f"(eq, ne, gt, lt, gte, lte, truthy, falsy), so {consequence}."
        )
    return problems


# ──── Keys ────


def _mode_problems(mode, path):
    if mode is None or mode in _KNOWN_MODES:
        return []
    return [
        f"{path}: mode {_shown(mode)} is not one a key has "
        f"({', '.join(_KNOWN_MODES)}), so the key acts as a tap."
    ]


def _timing_problems(press, path):
    return [
        f"{path}.{field} {_shown(press.get(field))} is not a number, so the key fails when pressed."
        for field in ("hold_repeat_ms", "hold_threshold_ms")
        if press.get(field) is not None and not _is_number(press.get(field))
    ]


def _press_setting_problems(press, path):
    """What a key's first press action carries for the key itself."""
    problems = _mode_problems(press.get("mode"), path)
    problems += _timing_problems(press, path)
    for field in ("off_action", "hold_action"):
        if press.get(field) is not None:
            problems += _action_problems(press[field], f"{path}.{field}")
    return problems


def _press_problems(value, path):
    problems = _action_list_problems(value, path)
    press = unwrap_binding(value)
    if isinstance(press, dict):
        problems += _press_setting_problems(press, f"{path}[0]")
    return problems


def _feedback_problems(feedback, path):
    if feedback is None:
        return []
    if not isinstance(feedback, dict):
        return [f"{path} is not an object, so the key never changes look."]
    return _source_problems(feedback, path, "the deck stops updating its keys", ("key",))


def _bindings_problems(bindings, path):
    if bindings is None:
        return []
    if not isinstance(bindings, dict):
        return [f"{path} is not an object, so the key does nothing."]
    problems = _press_problems(bindings.get("press"), f"{path}.press")
    problems += _feedback_problems(bindings.get("feedback"), f"{path}.feedback")
    if bindings.get("visible_when") is not None:
        problems += _condition_problems(
            bindings["visible_when"], f"{path}.visible_when", "the key stays hidden",
        )
    return problems


def _page_problems(page, path, paged):
    """A key's ``page``, read only on the paged list (not global_buttons)."""
    if not paged or page is None:
        return []
    if _is_slot_number(page):
        return []
    return [f"{path}: page {_shown(page)} is not a page number (0 or more), so the key never shows."]


def _button_problems(btn, path, paged):
    if not isinstance(btn, dict):
        return [f"{path} is not an object, so it is on no key."]
    problems = _slot_problems(btn.get("index"), path, "key")
    problems += _page_problems(btn.get("page"), path, paged)
    problems += _source_problems(
        btn, path, "the deck stops updating its keys", ("label_source", "value_source"),
    )
    return problems + _bindings_problems(btn.get("bindings"), f"{path}.bindings")


# ──── Dials, the touch strip and the info screen ────


def _adjust_problems(adjust, path):
    """A dial's or zone's adjust: the state key it changes and its bounds."""
    if adjust is None:
        return []
    if not isinstance(adjust, dict):
        return [f"{path} is not an object, so turning does not change anything."]
    problems = []
    key = adjust.get("key")
    if _is_set_but_not(key, str):
        problems.append(f"{path}.key is not a state key, so turning does not change anything.")
    problems += _write_problems(key, path, "the deck does not change it")
    return problems + _number_problems(adjust, path, ("step", "min", "max"), "it is ignored")


def _dial_problems(dial, path):
    if not isinstance(dial, dict):
        return [f"{path} is not an object, so it is on no dial."]
    problems = _slot_problems(dial.get("index"), path, "dial")
    for field in _DIAL_ACTIONS:
        problems += _action_list_problems(dial.get(field), f"{path}.{field}")
    for field in ("adjust", "pressed_adjust"):
        problems += _adjust_problems(dial.get(field), f"{path}.{field}")
    return problems + _number_problems(dial, path, ("hold_threshold_ms",), "it is ignored")


def _zone_position_problems(zone, path):
    return [
        f"{path}.{field} {_shown(zone.get(field))} is not a number, "
        f"so the zone is placed by an even split instead."
        for field in ("x", "w")
        if _is_set_but_not(zone.get(field), (int, float))
    ]


def _zone_problems(zone, path):
    if not isinstance(zone, dict):
        return [f"{path} is not an object, so it is skipped."]
    problems = _zone_position_problems(zone, path)
    for field in ("touch", "long_touch"):
        problems += _action_list_problems(zone.get(field), f"{path}.{field}")
    problems += _adjust_problems(zone.get("drag_adjust"), f"{path}.drag_adjust")
    problems += _source_problems(zone, path, "the deck stops updating the strip")
    if _is_set_but_not(zone.get("feedback"), dict):
        problems.append(f"{path}.feedback is not an object, so the zone never changes look.")
    return problems


def _touchscreen_problems(ts, path):
    if not isinstance(ts, dict):
        return [f"{path} is not an object, so the touch strip shows the clock."]
    problems = []
    idle = ts.get("idle")
    if idle is not None and idle not in IDLE_MODES:
        words = " or ".join(f'"{word}"' for word in IDLE_MODES)
        problems.append(f"{path}.idle {_shown(idle)} is not {words}, so the strip shows the clock.")
    zones = ts.get("zones")
    if zones is None:
        return problems
    if not isinstance(zones, list):
        return problems + [f"{path}.zones is not a list, so the strip lays out its own zones."]
    return problems + _each(zones, f"{path}.zones", _zone_problems)


def _info_item_problems(item, path):
    if not isinstance(item, dict):
        return [f"{path} is not an object, so it is skipped."]
    return _source_problems(item, path, "the deck stops updating the screen")


def _info_strip_problems(cfg, path):
    if not isinstance(cfg, dict):
        return [f"{path} is not an object, so the screen shows the clock."]
    problems = _source_problems(cfg, path, "the deck stops updating the screen")
    items = cfg.get("items")
    if items is None:
        return problems
    if not isinstance(items, list):
        return problems + [f"{path}.items is not a list, so it is ignored."]
    problems += _each(items, f"{path}.items", _info_item_problems)
    drawn = sum(1 for item in items if isinstance(item, dict))
    if drawn > 2:
        problems.append(
            f"{path}.items holds {drawn} items, and the screen fits two, "
            f"so the ones after the second are not drawn."
        )
    return problems


def _idle_dim_problems(idle_dim, path):
    if not isinstance(idle_dim, dict):
        return [f"{path} is not an object, so the deck never dims."]
    problems = []
    for field in ("after_seconds", "level"):
        problems += _required_number_problems(idle_dim, path, field, "the deck never dims")
    return problems


# ──── Rules and the per-deck layout ────


def _rule_value_problems(rule, path, value_field):
    if value_field != "page":
        return _required_number_problems(rule, path, value_field, "it is skipped")
    page = rule.get("page")
    if page is None:
        return [f"{path} has no page, so it is skipped."]
    if page_number(page) is not None:
        return []
    return [f"{path}.page {_shown(page)} is not a page number, so it is skipped."]


def _rule_problems(rule, path, value_field):
    """An auto_page or auto_brightness rule: a ``when`` plus a page or level."""
    if not isinstance(rule, dict):
        return [f"{path} is not an object, so it is skipped."]
    when = rule.get("when")
    if when is None:
        problems = [f"{path} has no when, so it is skipped."]
    else:
        problems = _condition_problems(when, f"{path}.when", "it never matches")
    return problems + _rule_value_problems(rule, path, value_field)


# Each layout list and what reads its entries, in the order they are reported.
_LIST_ENTRIES = (
    ("buttons", functools.partial(_button_problems, paged=True)),
    ("global_buttons", functools.partial(_button_problems, paged=False)),
    ("auto_page", functools.partial(_rule_problems, value_field="page")),
    ("auto_brightness", functools.partial(_rule_problems, value_field="level")),
    ("dials", _dial_problems),
)

# Each layout section that is one object, in the order they are reported.
_SECTIONS = (
    ("touchscreen", _touchscreen_problems),
    ("info_strip", _info_strip_problems),
    ("idle_dim", _idle_dim_problems),
)


def _list_entry_problems(cfg, prefix):
    problems = []
    for name, check in _LIST_ENTRIES:
        if isinstance(cfg.get(name), list):
            problems += _each(cfg[name], _join(prefix, name), check)
    return problems


def _section_problems(cfg, prefix):
    problems = []
    for name, check in _SECTIONS:
        if cfg.get(name) is not None:
            problems += check(cfg[name], _join(prefix, name))
    return problems


def _layout_problems(cfg, prefix):
    """The per-deck sections, in the flat config or one ``decks`` override."""
    problems = [
        f"{_join(prefix, name)} is not a list, so it is ignored."
        for name in _LAYOUT_LISTS
        if _is_set_but_not(cfg.get(name), list)
    ]
    problems += _list_entry_problems(cfg, prefix)
    problems += _section_problems(cfg, prefix)
    if _is_set_but_not(cfg.get("page_names"), dict):
        problems.append(f"{_join(prefix, 'page_names')} is not an object, so it is ignored.")
    return problems


def _override_problems(override, path):
    """One deck's entry in ``decks``: its own layout."""
    if not isinstance(override, dict):
        return [f"{path} is not an object, so that deck uses the main layout."]
    return _layout_problems(override, path)


def _deck_setting_problems(entry, path):
    if not isinstance(entry, dict):
        return [f"{path} is not an object, so it is ignored."]
    return _number_problems(entry, path, ("brightness",), "it is ignored")


# ──── The deck lists ────


def _virtual_serial_problems(serial, path, seen):
    if not serial:
        problems = [
            f"{path} has no serial made of letters, digits, - or _, so no deck is created for it."
        ]
    elif serial in seen:
        problems = [f"{path} repeats serial {serial}, so no second deck is created."]
    else:
        problems = []
    seen.add(serial)
    return problems


def _virtual_deck_problems(item, path, seen):
    if not isinstance(item, dict):
        return [f"{path} is not an object, so no deck is created for it."]
    problems = []
    model = item.get("model")
    if model not in VIRTUAL_MODELS:
        problems.append(
            f"{path}: model {_shown(model)} is not a virtual deck model "
            f"({', '.join(VIRTUAL_MODELS)}), so no deck is created for it."
        )
    return problems + _virtual_serial_problems(virtual_serial(item), path, seen)


def _network_deck_problems(item, path, seen):
    if not isinstance(item, dict):
        return [f"{path} is not an object, so no deck is added for it."]
    host, port = network_endpoint(item)
    problems = []
    if not host:
        problems.append(f"{path} has no host, so no deck is added for it.")
    if port is None:
        problems.append(
            f"{path}: port {_shown(item.get('port'))} is not a number, so no deck is added for it."
        )
    key = endpoint_key(host, port)
    if key is None:
        return problems
    if key in seen:
        problems.append(f"{path} repeats {key}, so no second deck is added.")
    seen.add(key)
    return problems


def _deck_list_problems(value, name, check, not_a_list):
    """A ``virtual_decks`` or ``network_decks`` list, checked in order."""
    if _is_set_but_not(value, list):
        return [f"{name} is not a list, so {not_a_list}."]
    if value is None:
        return []
    return _each(value, name, check, set())


def config_problems(config):
    """Every part of a proposed config the plugin would skip or misread."""
    if not isinstance(config, dict):
        return []
    problems = _layout_problems(config, "")
    problems += _number_problems(config, "", ("brightness",), "70 is used")
    problems += [
        f"{name} is not an object keyed by deck serial, so it is ignored."
        for name in ("decks", "deck_names", "deck_settings")
        if _is_set_but_not(config.get(name), dict)
    ]
    problems += _each_serial(config.get("decks"), "decks", _override_problems)
    problems += _each_serial(config.get("deck_settings"), "deck_settings", _deck_setting_problems)
    problems += _deck_list_problems(
        config.get("virtual_decks"), "virtual_decks", _virtual_deck_problems,
        "no virtual deck is created",
    )
    problems += _deck_list_problems(
        config.get("network_decks"), "network_decks", _network_deck_problems,
        "no network deck is added",
    )
    return problems
