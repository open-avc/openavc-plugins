"""
What a key, a touch-strip zone or an info item shows, from its config and the
values it reads: the value line, the meter's fill and color, and the feedback
style.

Keys and display elements (zones, info items) share these rules; the plugin
reads the state they need and draws what they return. A look is the tuple
``(label, icon, bg_color, text_color)``.

Two feedback forms exist. A ``states`` map styles by the watched value as
text (a missing value reads as ``""``), falling back to ``default_state``.
Without a usable map, the ``condition`` pair applies ``style_active`` or
``style_inactive``; their label sits beside the style (``label_active`` /
``label_inactive``), not inside it, and applies only when the style is an
object. In every form an empty field keeps what the look already has.
"""

from .conditions import coerce_numeric, reads_as_on

METER_DEFAULT_COLOR = "#8ab493"


def value_line(raw_value, unit):
    """The text a live value shows: the unit after a space, but a percent
    sign right after the number."""
    unit = str(unit or "")
    if not unit:
        return str(raw_value)
    return str(raw_value) + (unit if unit == "%" else f" {unit}")


def meter_bounds(meter, bounds_source):
    """A meter's min/max: its own, else the surrounding adjust's, else
    0..100; a pair that is not rising reads as 0..100."""
    meter = meter if isinstance(meter, dict) else {}
    bounds = bounds_source if isinstance(bounds_source, dict) else {}
    minimum = _first_number(meter.get("min"), bounds.get("min"), 0)
    maximum = _first_number(meter.get("max"), bounds.get("max"), 100)
    return (minimum, maximum) if maximum > minimum else (0, 100)


def meter_fill(meter, raw_value, bounds_source=None):
    """A meter as ``(fill_fraction, color)``, or None when it draws nothing.

    A meter draws when enabled (``"meter": {}`` or ``true``), or on its own
    when the surrounding adjust or drag_adjust declares both bounds, so a
    volume dial gets a level bar with no extra config. ``"meter": false``
    never draws, and neither does a value that is no number. Color: the
    highest ``thresholds`` entry the value reaches, else ``color``, else the
    accent default.
    """
    if not _draws(meter, bounds_source):
        return None
    value = coerce_numeric(raw_value)
    if value is None:
        return None
    minimum, maximum = meter_bounds(meter, bounds_source)
    frac = max(0.0, min(1.0, (value - minimum) / (maximum - minimum)))
    meter = meter if isinstance(meter, dict) else {}
    color = meter.get("color") or METER_DEFAULT_COLOR
    return frac, _threshold_color(meter.get("thresholds"), value) or color


def styled(feedback, value, look):
    """``look`` restyled by a feedback config for the watched ``value``."""
    states = feedback.get("states")
    if states and isinstance(states, dict):
        state_str = str(value) if value is not None else ""
        appearance = states.get(state_str) or states.get(
            feedback.get("default_state", "")
        )
        if isinstance(appearance, dict):
            return _restyle(look, appearance, appearance.get("label"))
        return look
    condition = feedback.get("condition", {})
    expected = condition.get("equals") if isinstance(condition, dict) else None
    side = "active" if reads_as_on(value, expected) else "inactive"
    style = feedback.get(f"style_{side}", {})
    if not isinstance(style, dict):
        return look
    return _restyle(look, style, feedback.get(f"label_{side}"))


def _restyle(look, style, label):
    """``look`` with the style's colors and icon and ``label`` over it, each
    only where it has one."""
    old_label, icon, bg_color, text_color = look
    return (
        label or old_label,
        style.get("icon") or icon,
        style.get("bg_color") or bg_color,
        style.get("text_color") or text_color,
    )


def _first_number(*values):
    """The first value that reads as a number (the last is the default)."""
    for value in values[:-1]:
        number = coerce_numeric(value)
        if number is not None:
            return number
    return values[-1]


def _draws(meter, bounds_source):
    """False for ``"meter": false``; True for an enabled meter, or one the
    surrounding adjust turns on by declaring both bounds."""
    if meter is False:
        return False
    if meter is True or isinstance(meter, dict):
        return True
    return _both_bounds(bounds_source)


def _both_bounds(bounds_source):
    """True when the surrounding adjust declares a numeric min and max."""
    bounds = bounds_source if isinstance(bounds_source, dict) else {}
    return (
        coerce_numeric(bounds.get("min")) is not None
        and coerce_numeric(bounds.get("max")) is not None
    )


def _threshold_color(thresholds, value):
    """The color of the highest threshold ``value`` reaches (the first of
    equal ones), or None."""
    if not isinstance(thresholds, list):
        return None
    best = None
    for above, color in _thresholds_reached(thresholds, value):
        if best is None or above > best[0]:
            best = (above, color)
    return best[1] if best else None


def _thresholds_reached(thresholds, value):
    """``(above, color)`` for each threshold entry ``value`` reaches, in
    order. Entries with no color or no numeric ``above`` count for nothing."""
    for rule in thresholds:
        if not isinstance(rule, dict) or not rule.get("color"):
            continue
        above = coerce_numeric(rule.get("above"))
        if above is None or value < above:
            continue
        yield above, rule["color"]
