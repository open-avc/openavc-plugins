"""Which config entry a deck key runs, and the actions a binding holds.

A key's entry comes from two lists in the deck's config view: ``buttons``
(each entry on one ``page``, 0 when absent) and ``global_buttons`` (a locked
key, the same on every page). A locked key reserves its index deck-wide: a
``buttons`` entry at that index never renders, fires or is watched while the
lock exists. Everything here is that one rule, so the key lookup, the state a
deck watches and the keys a state change redraws cannot disagree about it.

A binding (``press``, a dial's ``cw``, a zone's ``touch``...) is stored as a
list of actions, or as a single action object; ``action_list`` reads either.
Entries and actions that are not objects are skipped, never an error: the
config check (config_check.py) is what reports them. ``ACTION_KINDS`` are the
action types a deck runs: the plugin has one runner for each, and the config
check reports any other type. ``write_door`` is what a state.set action, a dial
adjust or a zone fader may write; the plugin writes through it and the config
check reports a key it refuses.
"""

# The action types a deck runs, in the order the config check lists them.
ACTION_KINDS = ("macro", "device.command", "state.set", "navigate")

# What a user variable's state key starts with; the variable's name follows.
_VARIABLE_PREFIX = "var."


def objects(value):
    """The entries of a config list that are objects; nothing for a value
    that is not a list."""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def action_list(value):
    """A binding's actions as a list: a single action object is wrapped,
    anything that is not an object is dropped."""
    if isinstance(value, dict):
        return [value]
    return objects(value)


def unwrap_binding(value):
    """The first action of a key's press, release or hold binding: the one
    that carries the key's mode and its settings. A single action object is
    read as itself; a list whose first entry is not an object reads as None.

    Not the same reading as ``press_actions(...)[0]``, which skips entries
    that are not objects: a press that starts with one runs its first object
    as the key's mode, but that action's toggle key is neither watched nor
    drawn as a toggle label.
    """
    if isinstance(value, list) and len(value) > 0:
        return value[0] if isinstance(value[0], dict) else None
    if isinstance(value, dict):
        return value
    return None


def press_actions(bindings):
    """A key's ``press`` binding as a list of actions. The first one carries
    the key's mode and its toggle and hold settings; a tap key fires them all
    in order."""
    if not isinstance(bindings, dict):
        return []
    return action_list(bindings.get("press"))


def global_buttons(cfg):
    """The locked keys: ``global_buttons`` entries that are objects."""
    return objects(cfg.get("global_buttons", [])) if isinstance(cfg, dict) else []


def locked_indexes(cfg):
    """Key indexes a locked key reserves on every page."""
    return {b.get("index") for b in global_buttons(cfg) if b.get("index") is not None}


def page_buttons(cfg):
    """``buttons`` entries on any page that a locked key does not shadow."""
    locked = locked_indexes(cfg)
    return [b for b in objects(cfg.get("buttons", [])) if b.get("index") not in locked]


def entries_on_page(cfg, page):
    """Every entry live on ``page``: the locked keys first, then that page's
    unshadowed ``buttons`` entries, in config order."""
    return global_buttons(cfg) + [
        b for b in page_buttons(cfg) if b.get("page", 0) == page
    ]


def assignment_for(cfg, page, key_index):
    """The entry a key runs on a page: its locked entry, else the ``buttons``
    entry placed on that page at that index, else None."""
    locked = next((b for b in global_buttons(cfg) if b.get("index") == key_index), None)
    if locked is not None:
        return locked
    return next(
        (
            b for b in objects(cfg.get("buttons", []))
            if b.get("index") == key_index and b.get("page", 0) == page
        ),
        None,
    )


def write_door(key, plugin_id):
    """How a deck writes ``key``: ``"state"`` for its own plugin's
    ``plugin.<plugin_id>.*`` state, ``"variable"`` for a ``var.*`` user
    variable, None for anything else, which the deck refuses (the scope rule
    the panel's plugin bridge keeps too). ``key`` must be text."""
    if key.startswith(f"plugin.{plugin_id}."):
        return "state"
    if key.startswith(_VARIABLE_PREFIX):
        return "variable"
    return None


def variable_name(key):
    """The user variable a ``"variable"`` key (``write_door``) names: the
    key after ``var.``."""
    return key[len(_VARIABLE_PREFIX):]
