"""The mark a key shows while a macro its press runs is running, and after it
ends.

A macro's events set the mark (``mark_for_event``): ``macro.started.<id>``
marks its keys RUNNING, ``macro.completed.<id>`` DONE, ``macro.error.<id>``
ERROR, and ``macro.cancelled.<id>`` clears the mark. DONE and ERROR show for
their ``SHOW_SECONDS`` and then the keys draw as usual; RUNNING stays until the
macro's next event reaches the key. Each mark has one color (``MARK_COLORS``):
an LCD key draws it as a border, with a loader while RUNNING, and a touch key
lights in it (looks.touch_key_color). Which keys show a macro's mark is
watch_keys.macro_keys; the plugin holds each deck's marks on its session.

A new mark is a name here, its color, the event that sets it and, for a mark
that passes, how long it shows.
"""

RUNNING = "running"
DONE = "done"
ERROR = "error"

# Which event sets which mark, in the order they are matched; None clears it.
_EVENT_MARKS = (
    ("macro.started.", RUNNING),
    ("macro.completed.", DONE),
    ("macro.cancelled.", None),
    ("macro.error.", ERROR),
)

MARK_COLORS = {
    RUNNING: "#f59e0b",
    DONE: "#22c55e",
    ERROR: "#ef4444",
}

# How long a result shows on its keys before they draw as usual.
SHOW_SECONDS = {DONE: 0.8, ERROR: 1.2}


def mark_for_event(event_name):
    """``(macro_id, mark)`` for a macro's event, the mark None when the event
    clears it; None for any other event."""
    for prefix, mark in _EVENT_MARKS:
        if event_name.startswith(prefix):
            return event_name[len(prefix):], mark
    return None
