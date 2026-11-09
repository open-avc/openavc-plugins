"""
What a context action asks the plugin to do: which action an event name
runs, the arguments each deck action reads from its payload, and the inputs
a simulated input (the IDE workbench's) stands for.

Context actions arrive as ``plugin.streamdeck.action.<name>`` events, from
the IDE or a macro's Emit Event step. The rules are tried in order and the
first that matches wins; most match the end of the name, but three match
anywhere in it (``identify`` also answers the IDE's ``identify_deck``).

Every reader returns None for a payload it refuses, and the plugin then does
nothing. Nothing here touches a deck; the plugin runs what these return.
"""

from .conditions import coerce_numeric

# (action, how its name appears in the event name, the text it matches), in
# the order they are tried.
_RULES = (
    ("simulate_input", "contains", "action.simulate_input"),
    ("set_live_mirror", "contains", "action.set_live_mirror"),
    ("set_page", "ends", ".set_page"),
    ("set_brightness", "ends", ".set_brightness"),
    ("flash_key", "ends", ".flash_key"),
    ("show_message", "ends", ".show_message"),
    ("identify", "contains", "action.identify"),
)


def action_for(event_name):
    """The context action an event name runs, or None."""
    for action, how, text in _RULES:
        if event_name.endswith(text) if how == "ends" else text in event_name:
            return action
    return None


def _whole(value):
    """``value`` as a whole number, or None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _page_args(data):
    page = _whole(data.get("page"))
    return None if page is None else (page,)


def _brightness_args(data):
    level = coerce_numeric(data.get("level"))
    return None if level is None else (max(0, min(100, int(level))),)


def _flash_args(data):
    index = _whole(data.get("index"))
    if index is None:
        return None
    times = _whole(data.get("times", 2))
    return index, 2 if times is None else max(1, min(10, times))


def _message_args(data):
    text = str(data.get("text", "")).strip()
    if not text:
        return None
    seconds = coerce_numeric(data.get("seconds"))
    return text, float(seconds) if seconds is not None and seconds > 0 else 5.0


_DECK_ARGS = {
    "set_page": _page_args,              # (page,)
    "set_brightness": _brightness_args,  # (level 0..100,)
    "flash_key": _flash_args,            # (index, times 1..10)
    "show_message": _message_args,       # (text, seconds)
    "identify": lambda data: (),
}


def deck_action_args(action, data):
    """The arguments a deck action reads from its payload, or None when it
    refuses the payload. Each one runs on the deck the payload's ``serial``
    names, or on every deck without one."""
    return _DECK_ARGS[action](data)


def simulated_inputs(payload, key_total):
    """The inputs a simulated input stands for, in the order they run.

    Each is ``("key", index, pressed)``, ``("dial", index, event, value)`` or
    ``("touch", event, value)``, ``event`` naming the library's event. A
    ``key`` or ``dial_push`` without ``pressed`` is a full tap (press then
    release); ``touch`` is a short tap unless ``touch_type`` says ``long`` or
    ``drag``, and a drag needs ``x_out``. ``key_total()`` is the deck's key
    count, asked only for a key. An unknown type, or a payload it refuses,
    stands for nothing.
    """
    kind = payload.get("type")
    reader = _SIMULATED.get(kind) if isinstance(kind, str) else None
    return reader(payload, key_total) if reader else []


def _edges(pressed):
    """A full tap without ``pressed``, else the one edge it names."""
    return [True, False] if pressed is None else [bool(pressed)]


def _simulated_key(payload, key_total):
    index = _whole(payload.get("index"))
    if index is None or not 0 <= index < key_total():
        return []
    return [("key", index, edge) for edge in _edges(payload.get("pressed"))]


def _simulated_turn(payload, key_total):
    index = _whole(payload.get("index"))
    if index is None:
        return []
    amount = _whole(payload.get("amount", 1))
    if amount is None:
        return []
    return [("dial", index, "TURN", amount)]


def _simulated_push(payload, key_total):
    index = _whole(payload.get("index"))
    if index is None:
        return []
    return [("dial", index, "PUSH", edge) for edge in _edges(payload.get("pressed"))]


def _simulated_touch(payload, key_total):
    x = _whole(payload.get("x"))
    if x is None:
        return []
    y = _whole(payload.get("y", 50))
    y = 50 if y is None else y
    touch_type = str(payload.get("touch_type", "short")).lower()
    if touch_type != "drag":
        name = "LONG" if touch_type == "long" else "SHORT"
        return [("touch", name, {"x": x, "y": y})]
    x_out = _whole(payload.get("x_out"))
    if x_out is None:
        return []
    return [("touch", "DRAG", {"x": x, "y": y, "x_out": x_out, "y_out": y})]


_SIMULATED = {
    "key": _simulated_key,
    "dial_turn": _simulated_turn,
    "dial_push": _simulated_push,
    "touch": _simulated_touch,
}
