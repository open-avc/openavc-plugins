"""
The condition rule a deck's ``visible_when``, ``auto_page`` and
``auto_brightness`` entries are judged by. A condition is one
``{key, operator, value}`` test, or ``{all: [...]}`` (every one true; an
empty list is true) or ``{any: [...]}`` (at least one true) of them.

A copy of the platform's rule (openavc/core/condition_eval.py), kept here so
the plugin does not reach into platform internals. The two differ: the
platform answers false for ``eq`` and ``ne`` when either side is None and
compares two strings ignoring case, where this copy compares None like any
other value and strings exactly. Changing either side changes what a deck
shows.
"""

import functools
import operator

OPERATOR_ALIASES = {
    "equals": "eq", "not_equals": "ne", "==": "eq", "!=": "ne",
    ">": "gt", "<": "lt", ">=": "gte", "<=": "lte",
    "equal": "eq", "not_equal": "ne", "greater_than": "gt", "less_than": "lt",
    "greater_or_equal": "gte", "less_or_equal": "lte",
}

# Every operator name a condition may use, the aliases included.
KNOWN_OPERATORS = frozenset(
    {"eq", "ne", "gt", "lt", "gte", "lte", "truthy", "falsy"} | set(OPERATOR_ALIASES)
)

_BOOL_WORDS = {
    "true": True, "1": True, "yes": True, "on": True,
    "false": False, "0": False, "no": False, "off": False,
}


def coerce_numeric(value):
    """A number, or text that reads as one, as a number; anything else None."""
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    return None


def coerce_bool(value):
    """A bool, or a yes/no word in any case, as a bool; anything else None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _BOOL_WORDS.get(value.lower())
    return None


def _both(a, b):
    """The pair, when neither side is None; else None."""
    if a is None or b is None:
        return None
    return a, b


def _as_bools(actual, target):
    """Both sides as bools, when one is a bool and the other reads as one."""
    if isinstance(actual, bool) or isinstance(target, bool):
        return _both(coerce_bool(actual), coerce_bool(target))
    return None


def _as_numbers(actual, target):
    """Both sides as numbers, when their types differ and both read as one."""
    if type(actual) is not type(target):
        return _both(coerce_numeric(actual), coerce_numeric(target))
    return None


def _equality(compare, actual, target):
    """eq / ne: as bools, else as numbers, else as written."""
    pair = _as_bools(actual, target) or _as_numbers(actual, target) or (actual, target)
    return compare(*pair)


def _ordering(compare, actual, target):
    """gt / lt / gte / lte: never a match against None; numbers (or text that
    reads as one) as numbers; anything else as Python orders it, and two
    values it cannot order are no match."""
    if actual is None or target is None:
        return False
    numbers = _both(coerce_numeric(actual), coerce_numeric(target))
    if numbers is not None:
        return compare(*numbers)
    try:
        return compare(actual, target)
    except TypeError:
        return False


_COMPARISONS = {
    "eq": functools.partial(_equality, operator.eq),
    "ne": functools.partial(_equality, operator.ne),
    "gt": functools.partial(_ordering, operator.gt),
    "lt": functools.partial(_ordering, operator.lt),
    "gte": functools.partial(_ordering, operator.ge),
    "lte": functools.partial(_ordering, operator.le),
    "truthy": lambda actual, _target: bool(actual),
    "falsy": lambda actual, _target: not bool(actual),
}


def eval_operator(op, actual, target):
    """Compare a state value with a condition's value.

    Raises ValueError on an unknown operator (callers treat that as no match).
    """
    op = OPERATOR_ALIASES.get(op, op)
    compare = _COMPARISONS.get(op)
    if compare is None:
        raise ValueError(f"Unknown condition operator: {op!r}")
    return compare(actual, target)


def condition_state_keys(cond):
    """Every state key a condition reads, through ``all`` and ``any`` at any
    depth, in the order they appear."""
    keys = []
    if not isinstance(cond, dict):
        return keys
    for group in ("all", "any"):
        sub = cond.get(group)
        if isinstance(sub, list):
            for child in sub:
                keys.extend(condition_state_keys(child))
    key = cond.get("key")
    if key:
        keys.append(key)
    return keys


async def _all_true(children, state_get, log):
    for child in children:
        if not await eval_condition(child, state_get, log):
            return False
    return True


async def _any_true(children, state_get, log):
    for child in children:
        if await eval_condition(child, state_get, log):
            return True
    return False


async def eval_condition(cond, state_get, log):
    """Whether a condition holds, reading state through ``state_get``.

    ``all`` is read first and decides alone when it is a list, then ``any``,
    then the condition's own key. No key, or not a condition at all, is
    false; an unknown operator is false and goes to ``log`` at debug.
    """
    if not isinstance(cond, dict):
        return False
    all_list = cond.get("all")
    if isinstance(all_list, list):
        return await _all_true(all_list, state_get, log)
    any_list = cond.get("any")
    if isinstance(any_list, list):
        return await _any_true(any_list, state_get, log)
    key = cond.get("key")
    if not key:
        return False
    actual = await state_get(key)
    try:
        return eval_operator(cond.get("operator", "eq"), actual, cond.get("value"))
    except ValueError:
        log(f"Ignoring unknown condition operator {cond.get('operator')!r}", level="debug")
        return False
