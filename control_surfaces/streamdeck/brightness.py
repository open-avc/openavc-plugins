"""What brightness a Stream Deck shows, and when it dims for want of input.

A deck shows the level of its first ``auto_brightness`` rule whose ``when``
holds, else its own base level. ``rule_levels`` reads the rules that can say
both a ``when`` and a level, in order; the plugin judges each ``when``
(conditions.py) and stops at the first that holds, so a rule that cannot say
its level is skipped before its condition is read. The rules come from the
deck's layout, which a ``decks[serial]`` entry may replace; the base level
(``base_level``) is a property of the unit, never read from a layout: the
unit's ``deck_settings[serial].brightness``, then the plugin-wide
``brightness``, then 70. Every level a deck is set to is a whole number from
0 to 100 (``clamp_level``).

``idle_dim`` lowers a deck to its own level once no input has arrived for
``after_seconds``. ``idle_dim_setting`` reads the two numbers: a delay that is
missing, not a number, or 0 or below is no setting at all, and one that reads
as "nan" is a delay no deck ever reaches. The level is clamped only when the
deck dims, so a level that reads as "nan" raises then and not before.
"""

from .conditions import coerce_numeric

# A deck's base level when neither its unit nor the plugin sets one.
DEFAULT_LEVEL = 70


def clamp_level(value):
    """A level as a whole number from 0 to 100 (``int()``, then the bounds)."""
    return max(0, min(100, int(value)))


def rule_levels(rules):
    """Each ``auto_brightness`` rule that has a ``when`` and a level that
    reads as a number, as ``(when, level)``, in order; nothing when the rules
    are not a list."""
    if not isinstance(rules, list):
        return
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        when = rule.get("when")
        level = coerce_numeric(rule.get("level"))
        if when is not None and level is not None:
            yield when, level


def _unit_setting(config, serial):
    """The unit's own ``deck_settings[serial].brightness`` when it reads as a
    number, else the plugin-wide ``brightness`` as written (70 when unset)."""
    settings = config.get("deck_settings")
    if isinstance(settings, dict):
        entry = settings.get(serial)
        if isinstance(entry, dict):
            level = coerce_numeric(entry.get("brightness"))
            if level is not None:
                return level
    return config.get("brightness", DEFAULT_LEVEL)


def base_level(config, serial):
    """The level of the deck with ``serial`` when no rule holds, from the
    plugin's whole config (not a deck's layout); 70 when the plugin-wide
    setting does not read as a number."""
    base = coerce_numeric(_unit_setting(config, serial))
    return clamp_level(base if base is not None else DEFAULT_LEVEL)


def idle_dim_setting(idle_dim):
    """``(after_seconds, level)`` of an ``idle_dim`` object, each read as a
    number and the level not yet clamped; None when either does not read as a
    number or the delay is 0 or below."""
    if not isinstance(idle_dim, dict):
        return None
    after = coerce_numeric(idle_dim.get("after_seconds"))
    level = coerce_numeric(idle_dim.get("level"))
    if level is None or after is None:
        return None
    if after <= 0:
        return None
    return after, level
