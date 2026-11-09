"""How many pages a deck has, and which page a value names.

Pages exist by being used: a deck has 1 + the highest page its config view
refers to, and always at least one. A page is referred to by a key's
placement (``page``, 0 when absent), an ``auto_page`` rule, a
``page_names`` key, and the page a navigate action names: in a key's press,
that press's ``off_action`` or ``hold_action``, a locked key's press, a
dial's ``cw``, ``ccw`` or ``press``, or a strip zone's ``touch`` or
``long_touch``. A dial's other gestures are not read, so a navigate there to
a page nothing else uses lands on the last page.

A page is whatever Python's ``int()`` makes of a value, so ``2.7`` is page
2 and ``true`` page 1; a value ``int()`` refuses names no page, and an
infinite number raises. The deck's navigate, a page key's highlight, the
auto_page rules and the config check all read a page here, so the check
never reports a page the deck goes to. OpenAVC's Stream Deck view counts
pages with its own copy of this rule to preview them, reading only whole
numbers, so the two can differ on a page such as 2.7.
"""

from .key_entries import action_list, global_buttons, objects, press_actions, unwrap_binding

NEXT_PAGE = "__next_page__"
PREV_PAGE = "__prev_page__"

# Where a navigate can sit: a dial's gestures and a zone's touches that are
# read, and the actions nested inside one action.
_DIAL_FIELDS = ("cw", "ccw", "press")
_ZONE_FIELDS = ("touch", "long_touch")
_NESTED_FIELDS = ("off_action", "hold_action")


def page_number(value):
    """The page ``value`` names: ``int()`` of it, None when ``int()``
    refuses it."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def page_step(page):
    """+1 for the next-page word, -1 for the previous-page word, else None."""
    if page == NEXT_PAGE:
        return 1
    if page == PREV_PAGE:
        return -1
    return None


def key_target_page(entry):
    """The page a key's first press action navigates to, which the key
    lights up while it shows. None for a key that does not navigate, a
    next or previous key, and a press that starts with a non-object."""
    bindings = entry.get("bindings", {})
    if not isinstance(bindings, dict):
        return None
    press = unwrap_binding(bindings.get("press"))
    if not isinstance(press, dict) or press.get("action") != "navigate":
        return None
    return page_number(press.get("page"))


def _with_nested(action):
    """An action, then the actions in its off_action and
    hold_action that are objects."""
    nested = [action.get(field) for field in _NESTED_FIELDS]
    return [action] + [each for each in nested if isinstance(each, dict)]


def _zones(cfg):
    touchscreen = cfg.get("touchscreen", {})
    return objects(touchscreen.get("zones")) if isinstance(touchscreen, dict) else []


def _bindings(cfg):
    """Every action list in a deck's config view a navigate is read from:
    the keys' and locked keys' presses, then the dials', then the zones'."""
    entries = objects(cfg.get("buttons", [])) + global_buttons(cfg)
    lists = [press_actions(entry.get("bindings", {})) for entry in entries]
    dials = objects(cfg.get("dials", []))
    lists += [action_list(dial.get(field)) for dial in dials for field in _DIAL_FIELDS]
    lists += [action_list(zone.get(field)) for zone in _zones(cfg) for field in _ZONE_FIELDS]
    return lists


def navigate_targets(cfg):
    """Every page a navigate action in a deck's config view names, as
    written (a next or previous word included)."""
    return [
        each.get("page")
        for actions in _bindings(cfg)
        for action in actions
        for each in _with_nested(action)
        if each.get("action") == "navigate"
    ]


def pages_referred_to(cfg):
    """Every page a deck's config view refers to, as written."""
    for entry in objects(cfg.get("buttons", [])):
        yield entry.get("page", 0)
    for rule in objects(cfg.get("auto_page", [])):
        yield rule.get("page")
    names = cfg.get("page_names", {})
    if isinstance(names, dict):
        yield from names
    yield from navigate_targets(cfg)


def page_count(cfg):
    """How many pages a deck's config view makes: 1 + the highest page it
    refers to, at least 1."""
    numbers = (page_number(value) for value in pages_referred_to(cfg))
    return 1 + max([0] + [n for n in numbers if n is not None])
