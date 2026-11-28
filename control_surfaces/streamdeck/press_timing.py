"""How long a held key or dial waits: a key's hold repeat and long-press
threshold, and a dial push's long-press threshold.

These are the touch panel's press timings, read by the panel's rule, so a
key held on a deck repeats, and turns into a long press, when the same key on
a panel does. A timing written as a finite number of milliseconds above 0, or
as text that reads as one, is kept. Anything else (none written, null, 0, a
negative one, words, a bool, a list, infinity) is the panel's default in
``PRESS_TIMING_DEFAULTS``. Text reads as the panel's JavaScript reads it:
a decimal number, or a whole number written 0x, 0o or 0b.

The plugin keeps its own copy because it runs on more than one OpenAVC
release. tests/test_validate_matches_platform.py holds the defaults to the
platform's schema and runs this rule beside the panel's own over the same
timings.
"""

import math
import re

#: A key's press timings, in milliseconds, when none usable is written.
PRESS_TIMING_DEFAULTS = {"hold_repeat_ms": 200, "hold_threshold_ms": 500}

# The text JavaScript's Number() reads as a number, once trimmed: a decimal
# (sign, digits, a point, an exponent), or a whole number in hex, octal or
# binary (no sign).
_DECIMAL_TEXT = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
_BASED_TEXT = re.compile(r"0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+")


def _text_number(text):
    """What ``text`` reads as, or None for text that is no number."""
    text = text.strip()
    if _DECIMAL_TEXT.fullmatch(text):
        return float(text)
    if _BASED_TEXT.fullmatch(text):
        return int(text, 0)
    return None


def written_ms(raw):
    """A timing as written, in milliseconds: a finite number above 0, else
    None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    number = _text_number(raw) if isinstance(raw, str) else raw
    if number is None:
        return None
    try:
        number = float(number)
    except OverflowError:  # a whole number too large to be a float
        return None
    return number if 0 < number < math.inf else None


def press_ms(raw, field):
    """A press timing (``hold_repeat_ms`` or ``hold_threshold_ms``) in
    milliseconds: the one written when usable, else the panel's default."""
    ms = written_ms(raw)
    return PRESS_TIMING_DEFAULTS[field] if ms is None else ms


def press_seconds(raw, field):
    """``press_ms`` in seconds, the unit a timer and a clock take."""
    return press_ms(raw, field) / 1000.0
