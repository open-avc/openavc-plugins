"""
Stream Deck: how many pages a deck has, what an action does, and how a
picture reaches a deck's touch strip and info screen.

Characterisation pins for three rules the plugin applies in several places:
which page a value names and how many pages a deck's config makes (a
navigate action, the page count, a page key's highlight, an auto_page rule
and the config check all read pages); what each action type a key, dial or
zone holds does, and what an action type the deck does not run does; and
how the touch strip, a strip region and the info screen are sent to the
deck, by a render and by a show_message overlay, including what a failed
send leaves in the log.

No hardware: decks are stand-ins that record what they are sent.

Run from the openavc-plugins root:
pytest tests/test_streamdeck_pages_actions_screens.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _platform_root import add_platform_to_path  # noqa: E402

add_platform_to_path()

_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))

from control_surfaces.streamdeck import streamdeck_plugin as sd_module  # noqa: E402
from control_surfaces.streamdeck.config_check import config_problems  # noqa: E402
from control_surfaces.streamdeck.pages import key_target_page  # noqa: E402
from control_surfaces.streamdeck.streamdeck_plugin import StreamDeckPlugin  # noqa: E402

try:
    from openavc.core.event_bus import EventBus
    from openavc.core.plugin_api import PluginAPI
    from openavc.core.plugin_registry import PluginRegistry
    from openavc.core.plugin_test_harness import MockDeviceManager, MockMacroEngine
    from openavc.core.state_store import StateStore
except ModuleNotFoundError:
    pytest.skip(
        "openavc repo not available as sibling directory",
        allow_module_level=True,
    )


# ──── Rig ────


def _plugin(config=None):
    state = StateStore()
    events = EventBus()
    state.set_event_bus(events)
    logs = []
    api = PluginAPI(
        plugin_id="streamdeck",
        capabilities=StreamDeckPlugin.PLUGIN_INFO["capabilities"],
        config=config or {},
        registry=PluginRegistry("streamdeck"),
        state_store=state,
        event_bus=events,
        macro_engine=MockMacroEngine(),
        device_manager=MockDeviceManager(),
        platform_id="test",
        log_fn=lambda pid, msg, level="info": logs.append((level, msg)),
    )
    plugin = StreamDeckPlugin()
    plugin.api = api
    return plugin, state, logs


def _session(plugin, deck=None, serial="SN1"):
    session = sd_module._DeckSession(deck)
    session.serial = serial
    session.device_id = f"dev-{serial}"
    plugin._sessions[session.device_id] = session
    return session


def _count(config, serial="SN1"):
    plugin, _state, _logs = _plugin(config)
    return plugin._effective_page_count(_session(plugin, serial=serial))


def _press(*actions):
    return {"bindings": {"press": list(actions)}}


def _nav(page):
    return {"action": "navigate", "page": page}


class _Calls:
    """Records the plugin methods an action reaches, in order."""

    def __init__(self, plugin, raise_on=None):
        self.calls = []
        self._raise_on = raise_on or {}
        for name in ("_change_page", "_apply_state_set"):
            self._wrap(plugin, name)

    def _wrap(self, plugin, name):
        async def _rec(*args):
            self.calls.append((name,) + args[1:] if name == "_change_page" else (name,) + args)
            error = self._raise_on.get(name)
            if error is not None:
                raise error

        setattr(plugin, name, _rec)


class _Api:
    """Records the platform calls an action makes; raises on request."""

    def __init__(self, plugin, raise_on=None):
        self.calls = []
        raise_on = raise_on or {}
        api = plugin.api

        async def macro_execute(macro):
            self.calls.append(("macro", macro))
            if "macro" in raise_on:
                raise raise_on["macro"]

        async def device_command(device, command, params=None):
            self.calls.append(("command", device, command, params))
            if "command" in raise_on:
                raise raise_on["command"]

        api.macro_execute = macro_execute
        api.device_command = device_command


# ──── How many pages a deck has ────


def test_a_deck_with_nothing_on_it_has_one_page():
    assert _count({}) == 1


def test_a_key_placement_makes_its_page_and_a_key_with_no_page_is_on_page_zero():
    assert _count({"buttons": [{"index": 0, "page": 3}]}) == 4
    assert _count({"buttons": [{"index": 0}]}) == 1


def test_key_entries_that_are_not_objects_and_a_buttons_value_that_is_not_a_list_count_nothing():
    assert _count({"buttons": ["x", 5, None, {"index": 0, "page": 2}]}) == 3
    assert _count({"buttons": {"index": 0, "page": 5}}) == 1


def test_a_navigate_in_a_key_press_makes_its_page_as_a_list_or_one_object():
    assert _count({"buttons": [dict(_press(_nav(4)), index=0)]}) == 5
    assert _count({"buttons": [{"index": 0, "bindings": {"press": _nav(6)}}]}) == 7


def test_a_navigate_hidden_in_an_off_or_hold_action_counts_even_under_another_action_type():
    off = {"action": "macro", "macro": "m", "off_action": _nav(5)}
    hold = {"action": "state.set", "key": "var.x", "hold_action": _nav(8)}
    assert _count({"buttons": [dict(_press(off), index=0)]}) == 6
    assert _count({"buttons": [dict(_press(hold), index=0)]}) == 9
    # Only an object there counts, and only a navigate.
    assert _count({"buttons": [dict(_press({"action": "macro", "off_action": [_nav(5)]}), index=0)]}) == 1
    assert _count({"buttons": [dict(_press({"action": "macro", "hold_action": {"action": "macro", "page": 5}}), index=0)]}) == 1


def test_a_key_whose_bindings_are_not_an_object_still_counts_its_placement():
    assert _count({"buttons": [{"index": 0, "page": 2, "bindings": "x"}]}) == 3


def test_a_locked_key_counts_its_navigates_but_not_a_page_of_its_own():
    assert _count({"global_buttons": [{"index": 0, "page": 7}]}) == 1
    assert _count({"global_buttons": [dict(_press(_nav(3)), index=0)]}) == 4
    assert _count({"global_buttons": [{"index": 0, "bindings": [_nav(3)]}, "x"]}) == 1


def test_auto_page_rules_count_their_page_and_skip_what_is_not_a_rule():
    assert _count({"auto_page": [{"when": {"key": "var.a"}, "page": 4}]}) == 5
    assert _count({"auto_page": ["x", {"page": 2}]}) == 3
    assert _count({"auto_page": {"page": 9}}) == 1


def test_page_names_count_their_numeric_keys():
    assert _count({"page_names": {"4": "Lights", "x": "No"}}) == 5
    assert _count({"page_names": [["4", "Lights"]]}) == 1


def test_a_dial_counts_its_turns_and_press_but_no_other_gesture():
    assert _count({"dials": [{"index": 0, "cw": [_nav(2)]}]}) == 3
    assert _count({"dials": [{"index": 0, "ccw": _nav(3)}]}) == 4
    assert _count({"dials": [{"index": 0, "press": [_nav(4)]}]}) == 5
    for field in ("long_press", "pressed_cw", "pressed_ccw", "touch", "long_touch"):
        assert _count({"dials": [{"index": 0, field: [_nav(9)]}]}) == 1, field
    assert _count({"dials": ["x", {"index": 0, "cw": [_nav(2)]}]}) == 3
    assert _count({"dials": {"index": 0, "cw": [_nav(2)]}}) == 1


def test_a_strip_zone_counts_its_touch_and_long_touch_only():
    zone = {"touch": [_nav(2)], "long_touch": _nav(5), "press": [_nav(9)]}
    assert _count({"touchscreen": {"zones": [zone, "x"]}}) == 6
    assert _count({"touchscreen": {"zones": {"touch": [_nav(2)]}}}) == 1
    assert _count({"touchscreen": [{"zones": [zone]}]}) == 1


def test_a_page_is_whatever_int_makes_of_the_value():
    def via_key(page):
        return _count({"buttons": [{"index": 0, "page": page}]})

    assert via_key(True) == 2
    assert via_key(2.7) == 3
    assert via_key("3") == 4
    assert via_key(" 3 ") == 4
    assert via_key("2.7") == 1
    assert via_key("x") == 1
    assert via_key(None) == 1
    assert via_key(-4) == 1
    assert via_key(float("nan")) == 1
    assert via_key([2]) == 1


def test_an_infinite_page_raises_out_of_the_count():
    with pytest.raises(OverflowError):
        _count({"buttons": [{"index": 0, "page": float("inf")}]})
    with pytest.raises(OverflowError):
        _count({"buttons": [dict(_press(_nav(float("-inf"))), index=0)]})


def test_the_relative_page_words_and_a_missing_page_count_nothing():
    assert _count({"buttons": [dict(_press(_nav("__next_page__"), {"action": "navigate"}), index=0)]}) == 1


def test_a_deck_with_its_own_layout_counts_that_layout_only():
    config = {
        "buttons": [{"index": 0, "page": 5}],
        "decks": {"SN2": {"buttons": [{"index": 0, "page": 1}]}},
    }
    assert _count(config, serial="SN1") == 6
    assert _count(config, serial="SN2") == 2


# ──── Navigate: where an action takes the deck ────


async def _navigate(page, current=1, raise_on=None, **extra):
    plugin, _state, logs = _plugin()
    session = _session(plugin)
    session.current_page = current
    calls = _Calls(plugin, raise_on)
    binding = {"action": "navigate", **extra}
    if page is not _MISSING:
        binding["page"] = page
    await plugin._execute_action(session, binding, "key 3")
    return calls.calls, logs


_MISSING = object()


@pytest.mark.asyncio
async def test_navigate_steps_to_the_next_or_previous_page():
    assert (await _navigate("__next_page__"))[0] == [("_change_page", 2)]
    assert (await _navigate("__prev_page__"))[0] == [("_change_page", 0)]


@pytest.mark.asyncio
async def test_navigate_goes_to_the_page_int_makes_of_the_value():
    for page, target in ((2, 2), ("2", 2), (2.7, 2), (True, 1), (" 4 ", 4), (-1, -1)):
        assert (await _navigate(page))[0] == [("_change_page", target)], page


@pytest.mark.asyncio
async def test_navigate_to_something_that_is_not_a_page_does_nothing():
    for page in ("x", None, "", [1], {"p": 1}, "2.7", _MISSING):
        calls, logs = await _navigate(page)
        assert calls == [] and logs == [], page


@pytest.mark.asyncio
async def test_a_numeric_navigate_swallows_a_type_or_value_error_from_the_page_change():
    calls, logs = await _navigate(2, raise_on={"_change_page": ValueError("bad")})
    assert calls == [("_change_page", 2)] and logs == []
    calls, logs = await _navigate("3", raise_on={"_change_page": TypeError("bad")})
    assert calls == [("_change_page", 3)] and logs == []


@pytest.mark.asyncio
async def test_a_relative_navigate_lets_the_same_error_through():
    with pytest.raises(ValueError):
        await _navigate("__next_page__", raise_on={"_change_page": ValueError("bad")})
    with pytest.raises(TypeError):
        await _navigate("__prev_page__", raise_on={"_change_page": TypeError("bad")})


@pytest.mark.asyncio
async def test_a_numeric_navigate_lets_any_other_error_through():
    with pytest.raises(RuntimeError):
        await _navigate(2, raise_on={"_change_page": RuntimeError("bad")})


@pytest.mark.asyncio
async def test_an_infinite_navigate_raises():
    with pytest.raises(OverflowError):
        await _navigate(float("inf"))


# ──── The other action types ────


async def _run(binding, raise_on=None, source="key 3"):
    plugin, _state, logs = _plugin()
    session = _session(plugin)
    calls = _Calls(plugin)
    api = _Api(plugin, raise_on)
    if raise_on and "state" in raise_on:
        calls._raise_on["_apply_state_set"] = raise_on["state"]
    await plugin._execute_action(session, binding, source)
    return api.calls + calls.calls, logs


@pytest.mark.asyncio
async def test_a_macro_action_runs_its_macro_and_says_so_at_debug():
    calls, logs = await _run({"action": "macro", "macro": "shutdown"}, source="dial 1")
    assert calls == [("macro", "shutdown")]
    assert logs == [("debug", "Executed macro 'shutdown' from dial 1")]


@pytest.mark.asyncio
async def test_a_macro_action_with_no_macro_does_nothing():
    for binding in ({"action": "macro"}, {"action": "macro", "macro": ""}, {"action": "macro", "macro": None}):
        assert await _run(binding) == ([], [])


@pytest.mark.asyncio
async def test_a_macro_that_fails_is_logged_as_an_error():
    calls, logs = await _run({"action": "macro", "macro": "m"}, raise_on={"macro": RuntimeError("boom")})
    assert calls == [("macro", "m")]
    assert logs == [("error", "Error executing macro 'm': boom")]


@pytest.mark.asyncio
async def test_a_device_command_sends_its_params_only_when_they_are_an_object():
    calls, logs = await _run({"action": "device.command", "device": "proj", "command": "on",
                              "params": {"x": 1}})
    assert calls == [("command", "proj", "on", {"x": 1})]
    assert logs == [("debug", "Sent on to proj from key 3")]
    calls, _logs = await _run({"action": "device.command", "device": "proj", "command": "on",
                               "params": [1]})
    assert calls == [("command", "proj", "on", None)]
    calls, _logs = await _run({"action": "device.command", "device": "proj", "command": "on"})
    assert calls == [("command", "proj", "on", None)]


@pytest.mark.asyncio
async def test_a_device_command_missing_its_device_or_command_does_nothing():
    assert await _run({"action": "device.command", "device": "proj"}) == ([], [])
    assert await _run({"action": "device.command", "command": "on"}) == ([], [])
    assert await _run({"action": "device.command", "device": "", "command": "on"}) == ([], [])


@pytest.mark.asyncio
async def test_a_device_command_that_fails_is_logged_as_an_error():
    calls, logs = await _run({"action": "device.command", "device": "p", "command": "on"},
                             raise_on={"command": RuntimeError("boom")})
    assert calls == [("command", "p", "on", None)]
    assert logs == [("error", "Error sending command: boom")]


@pytest.mark.asyncio
async def test_a_state_set_writes_its_value_through_the_write_rule():
    calls, logs = await _run({"action": "state.set", "key": "var.mode", "value": 3})
    assert calls == [("_apply_state_set", "var.mode", 3)] and logs == []
    calls, _logs = await _run({"action": "state.set", "key": "var.mode"})
    assert calls == [("_apply_state_set", "var.mode", None)]


@pytest.mark.asyncio
async def test_a_state_set_with_no_key_does_nothing_and_a_failed_one_is_logged():
    assert await _run({"action": "state.set", "value": 1}) == ([], [])
    assert await _run({"action": "state.set", "key": ""}) == ([], [])
    calls, logs = await _run({"action": "state.set", "key": "var.k", "value": 1},
                             raise_on={"state": RuntimeError("boom")})
    assert calls == [("_apply_state_set", "var.k", 1)]
    assert logs == [("error", "Error setting state 'var.k': boom")]


@pytest.mark.asyncio
async def test_an_action_type_the_deck_does_not_run_does_nothing():
    for kind in ("script.call", "", None, 5, ["macro"], {"a": 1}, "Macro"):
        assert await _run({"action": kind, "macro": "m", "page": 2, "key": "var.x",
                           "device": "d", "command": "c"}) == ([], []), kind
    assert await _run({"macro": "m"}) == ([], [])


@pytest.mark.asyncio
async def test_an_action_list_runs_its_objects_in_order_and_skips_the_rest():
    plugin, _state, _logs = _plugin()
    session = _session(plugin)
    api = _Api(plugin)
    await plugin._execute_actions(session, [
        {"action": "macro", "macro": "a"}, "x", None,
        {"action": "device.command", "device": "d", "command": "c"},
        {"action": "macro", "macro": "b"},
    ], "key 0")
    assert api.calls == [("macro", "a"), ("command", "d", "c", None), ("macro", "b")]


def test_the_config_check_names_the_action_types_a_deck_runs():
    problems = config_problems({"buttons": [dict(_press({"action": "script.call"}), index=0)]})
    assert problems == [
        "buttons[0].bindings.press[0]: action \"script.call\" is not one a deck runs "
        "(macro, device.command, state.set, navigate), so it does nothing."
    ]


# ──── A page key's highlight and the auto_page rules ────


def test_a_page_key_lights_for_the_page_int_makes_of_its_first_press_navigate():
    target = key_target_page
    assert target(_press(_nav(3))) == 3
    assert target({"bindings": {"press": _nav("2")}}) == 2
    assert target(_press(_nav(True))) == 1
    assert target(_press(_nav(2.9))) == 2
    assert target(_press(_nav("__next_page__"))) is None
    assert target(_press(_nav(None))) is None
    assert target(_press({"action": "macro", "page": 3})) is None
    assert target(_press("x", _nav(3))) is None
    assert target({"bindings": "x"}) is None
    assert target({}) is None


async def _auto_page(rules, values=None, pages=4, current=0):
    config = {"auto_page": rules, "buttons": [{"index": 0, "page": pages - 1}]}
    plugin, state, _logs = _plugin(config)
    for key, value in (values or {}).items():
        state.set(key, value, source="test")
    session = _session(plugin)
    session.current_page = current
    return await plugin._evaluate_auto_page(session)


_ON = {"key": "var.on", "operator": "eq", "value": True}


@pytest.mark.asyncio
async def test_the_first_matching_auto_page_rule_names_the_page():
    rules = [{"when": {"key": "var.off", "operator": "eq", "value": True}, "page": 1},
             {"when": _ON, "page": 2}, {"when": _ON, "page": 3}]
    assert await _auto_page(rules, {"var.on": True}) == 2


@pytest.mark.asyncio
async def test_an_auto_page_rule_page_is_read_by_int_and_kept_on_the_deck():
    on = {"var.on": True}
    assert await _auto_page([{"when": _ON, "page": "2"}], on) == 2
    assert await _auto_page([{"when": _ON, "page": 2.9}], on) == 2
    assert await _auto_page([{"when": _ON, "page": -3}], on) == 0
    # A rule's own page is one of the deck's pages, so it is never cut down.
    assert await _auto_page([{"when": _ON, "page": 99}], on, pages=4) == 99


@pytest.mark.asyncio
async def test_an_auto_page_rule_that_cannot_name_a_page_is_passed_over():
    on = {"var.on": True}
    rules = ["x", {"page": 1}, {"when": _ON}, {"when": _ON, "page": "x"}, {"when": _ON, "page": 2}]
    assert await _auto_page(rules, on) == 2
    assert await _auto_page({"when": _ON, "page": 1}, on) is None
    assert await _auto_page([{"when": _ON, "page": "x"}], on) is None


@pytest.mark.asyncio
async def test_a_page_change_is_kept_between_the_first_and_last_page():
    plugin, state, _logs = _plugin({"buttons": [{"index": 0, "page": 2}]})
    session = _session(plugin)
    session.current_page = 1
    await plugin._change_page(session, -5)
    assert session.current_page == 0
    await plugin._change_page(session, 50)
    assert session.current_page == 2
    assert state.get("plugin.streamdeck.SN1.current_page") == 2


def test_the_config_check_reads_a_navigate_page_the_way_the_deck_does():
    def nav_problems(page):
        return config_problems({"buttons": [dict(_press(_nav(page)), index=0)]})

    for page in (2, "2", 2.7, True, " 3 ", "__next_page__", "__prev_page__", "", None):
        assert nav_problems(page) == [], page
    for page, shown in (("x", '"x"'), ("2.7", '"2.7"'), ([2], "list")):
        assert nav_problems(page) == [
            f"buttons[0].bindings.press[0]: page {shown} is not a page number, "
            "\"__next_page__\" or \"__prev_page__\", so the key does not change page."
        ], page


def test_the_config_check_reads_an_auto_page_page_the_way_the_deck_does():
    def rule_problems(page):
        return config_problems({"auto_page": [{"when": _ON, "page": page}]})

    for page in (2, "2", 2.7, True):
        assert rule_problems(page) == [], page
    assert rule_problems("x") == ['auto_page[0].page "x" is not a page number, so it is skipped.']
    assert rule_problems("__next_page__") == [
        'auto_page[0].page "__next_page__" is not a page number, so it is skipped.'
    ]


# ──── How a picture reaches the touch strip and the info screen ────


class _Helper:
    """A PILHelper stand-in that records the size of what it converts."""

    converted = []

    @classmethod
    def to_native_key_format(cls, deck, img):
        cls.converted.append(("key", img.size))
        return b"native-key"

    @classmethod
    def to_native_touchscreen_format(cls, deck, img):
        cls.converted.append(("strip", img.size))
        return ("native-strip", img.copy())

    @classmethod
    def to_native_screen_format(cls, deck, img):
        cls.converted.append(("screen", img.size))
        return ("native-screen", img.copy())


@pytest.fixture
def pil(monkeypatch):
    img_mod = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw, ImageFont
    monkeypatch.setattr(sd_module, "Image", img_mod)
    monkeypatch.setattr(sd_module, "ImageDraw", ImageDraw)
    monkeypatch.setattr(sd_module, "ImageFont", ImageFont)
    monkeypatch.setattr(sd_module, "PILHelper", _Helper)
    _Helper.converted = []
    return img_mod


class _ScreenDeck:
    """A deck with keys, touch keys, a touch strip and an info screen, each
    size settable (or an exception), each send recorded (or refused)."""

    def __init__(self, *, keys=4, layout=(2, 2), key_size=(72, 72), touch_keys=2,
                 touch=True, strip=(400, 100), strip_flip=None, screen=(248, 58),
                 refuse=(), visual=True):
        self._keys = keys
        self._layout = layout
        self._key_size = key_size
        self._touch_keys = touch_keys
        self._touch = touch
        self._strip = strip
        self._strip_flip = strip_flip
        self._screen = screen
        self._refuse = refuse
        self._visual = visual
        self.sent = []
        self.locks = 0

    def is_visual(self):
        return self._visual

    def key_count(self):
        return self._keys

    def key_layout(self):
        return self._layout

    def touch_key_count(self):
        return self._touch_keys

    def is_touch(self):
        return self._touch

    def dial_count(self):
        return 0

    def key_image_format(self):
        return {"size": self._key_size, "format": "JPEG"}

    def touchscreen_image_format(self):
        if isinstance(self._strip, Exception):
            raise self._strip
        fmt = {"size": self._strip, "format": "JPEG"}
        if self._strip_flip is not None:
            fmt["flip"] = self._strip_flip
        return fmt

    def screen_image_format(self):
        if isinstance(self._screen, Exception):
            raise self._screen
        return {"size": self._screen, "format": "JPEG"}

    def __enter__(self):
        self.locks += 1
        return self

    def __exit__(self, *exc):
        return False

    def _send(self, what, *args):
        if what in self._refuse:
            raise RuntimeError(f"{what} refused")
        self.sent.append((what,) + args)

    def set_key_image(self, key, image):
        self._send("key", key)

    def set_key_color(self, key, r, g, b):
        self._send("color", key, (r, g, b))

    def set_touchscreen_image(self, image, x=0, y=0, w=0, h=0):
        self._send("strip", image[0], image[1].size, (x, y, w, h))
        self.last_strip = image[1]

    def set_screen_image(self, image):
        self._send("screen", image[0], image[1].size)


def _drawing(config=None, **deck_args):
    plugin, state, logs = _plugin(config)
    plugin._load_text_font()
    session = _session(plugin, _ScreenDeck(**deck_args))
    return plugin, session, logs


def _colors(image):
    return {color for _count_, color in image.getcolors(maxcolors=1 << 20)}


_ZONES = {"touchscreen": {"zones": [
    {"label": "A", "x": 0, "w": 200}, {"label": "B", "x": 200, "w": 200},
]}}


@pytest.mark.asyncio
async def test_a_strip_render_sends_the_whole_strip_and_keeps_it(pil):
    plugin, session, logs = _drawing(_ZONES)
    await plugin._render_touchscreen(session)
    assert [s[:2] + s[3:] for s in session.deck.sent] == [("strip", "native-strip", (0, 0, 400, 100))]
    assert session.deck.sent[0][2] == (400, 100)
    assert session.strip_image is not None and session.strip_image.size == (400, 100)
    assert session.deck.locks == 1 and logs == []


@pytest.mark.asyncio
async def test_a_strip_render_that_the_deck_refuses_is_logged_at_debug(pil):
    plugin, session, logs = _drawing(_ZONES, refuse=("strip",))
    await plugin._render_touchscreen(session)
    assert logs == [("debug", "Error setting touchscreen image: strip refused")]
    assert session.strip_image is not None


@pytest.mark.asyncio
async def test_a_strip_render_draws_nothing_without_a_strip_or_while_an_overlay_is_up(pil):
    for args in ({"touch": False}, {"visual": False}, {"strip": RuntimeError("no")}):
        plugin, session, logs = _drawing(_ZONES, **args)
        await plugin._render_touchscreen(session)
        assert session.deck.sent == [] and session.strip_image is None, args
    plugin, session, logs = _drawing(_ZONES)
    session.overlay_active = True
    await plugin._render_touchscreen(session)
    assert session.deck.sent == []


@pytest.mark.asyncio
async def test_an_idle_strip_shows_the_clock_or_stays_blank_and_a_touch_lightens_it(pil):
    plugin, session, _logs = _drawing({})
    await plugin._render_touchscreen(session)
    clock = _Helper.converted[-1]
    assert clock == ("strip", (400, 100))
    assert len(_colors(session.strip_image)) > 1
    plugin, session, _logs = _drawing({"touchscreen": {"idle": "blank"}})
    await plugin._render_touchscreen(session)
    assert _colors(session.strip_image) == {(26, 26, 46)}
    session.flash_zones = {0}
    await plugin._render_touchscreen(session)
    assert _colors(session.strip_image) == {(67, 67, 83)}


@pytest.mark.asyncio
async def test_a_strip_zone_render_sends_only_its_region(pil):
    plugin, session, logs = _drawing(_ZONES)
    await plugin._render_touchscreen(session)
    session.deck.sent.clear()
    await plugin._render_strip_zone(session, 1)
    assert session.deck.sent == [("strip", "native-strip", (200, 100), (200, 0, 200, 100))]
    region = session.strip_image.crop((200, 0, 400, 100))
    assert list(session.deck.last_strip.getdata()) == list(region.getdata())
    assert list(region.getdata()) != list(session.strip_image.crop((0, 0, 200, 100)).getdata())
    assert logs == []


@pytest.mark.asyncio
async def test_a_strip_zone_the_deck_refuses_is_logged_at_debug(pil):
    plugin, session, logs = _drawing(_ZONES)
    await plugin._render_touchscreen(session)
    session.deck._refuse = ("strip",)
    await plugin._render_strip_zone(session, 0)
    assert logs == [("debug", "Error setting touchscreen region: strip refused")]


@pytest.mark.asyncio
async def test_a_strip_zone_past_the_strip_is_mirrored_but_not_sent(pil):
    zones = {"touchscreen": {"zones": [{"label": "A", "x": 0, "w": 200},
                                       {"label": "B", "x": 500, "w": 100}]}}
    plugin, session, logs = _drawing(zones)
    session.is_virtual = True
    await plugin._render_touchscreen(session)
    session.deck.sent.clear()
    plugin._mirror_blobs.clear()
    await plugin._render_strip_zone(session, 1)
    assert session.deck.sent == []
    assert (session.serial, "touchscreen") in plugin._mirror_blobs


@pytest.mark.asyncio
async def test_a_strip_zone_redraws_the_whole_strip_when_a_region_cannot_be_sent(pil):
    cases = (
        ({"strip_flip": (True, False)}, 0),
        ({}, 7),
    )
    for args, index in cases:
        plugin, session, _logs = _drawing(_ZONES, **args)
        await plugin._render_touchscreen(session)
        session.deck.sent.clear()
        await plugin._render_strip_zone(session, index)
        assert [s[3] for s in session.deck.sent] == [(0, 0, 400, 100)], args
    plugin, session, _logs = _drawing({})
    await plugin._render_touchscreen(session)
    session.deck.sent.clear()
    await plugin._render_strip_zone(session, 0)
    assert [s[3] for s in session.deck.sent] == [(0, 0, 400, 100)]


@pytest.mark.asyncio
async def test_a_strip_zone_draws_nothing_before_the_strip_or_while_an_overlay_is_up(pil):
    plugin, session, _logs = _drawing(_ZONES)
    await plugin._render_strip_zone(session, 0)
    assert session.deck.sent == []
    await plugin._render_touchscreen(session)
    session.deck.sent.clear()
    session.overlay_active = True
    await plugin._render_strip_zone(session, 0)
    session.overlay_active = False
    session.deck._strip = RuntimeError("gone")
    await plugin._render_strip_zone(session, 0)
    session.deck._touch = False
    await plugin._render_strip_zone(session, 0)
    assert session.deck.sent == []


@pytest.mark.asyncio
async def test_an_info_screen_render_sends_the_screen(pil):
    plugin, session, logs = _drawing({"info_strip": {"source": "text", "text": "Hi"}})
    await plugin._render_info_strip(session)
    assert session.deck.sent == [("screen", "native-screen", (248, 58))]
    assert session.deck.locks == 1 and logs == []


@pytest.mark.asyncio
async def test_an_info_screen_the_deck_refuses_is_logged_at_debug(pil):
    plugin, session, logs = _drawing({"info_strip": {"source": "clock"}}, refuse=("screen",))
    await plugin._render_info_strip(session)
    assert logs == [("debug", "Error setting info strip image: screen refused")]


@pytest.mark.asyncio
async def test_a_blank_info_screen_is_its_background_only(pil):
    plugin, session, _logs = _drawing({"info_strip": {"source": "blank"}, "button_color": "#102030"})
    session.is_virtual = True
    await plugin._render_info_strip(session)
    assert session.deck.sent == [("screen", "native-screen", (248, 58))]
    blob, kind = plugin._mirror_blobs[(session.serial, "screen")]
    from io import BytesIO
    assert kind == "image/png"
    assert _colors(pil.open(BytesIO(blob)).convert("RGB")) == {(16, 32, 48)}


@pytest.mark.asyncio
async def test_an_info_screen_draws_nothing_without_a_screen_or_while_an_overlay_is_up(pil):
    for args in ({"screen": (0, 0)}, {"screen": (248, 0)}, {"screen": RuntimeError("no")},
                 {"visual": False}):
        plugin, session, _logs = _drawing({}, **args)
        await plugin._render_info_strip(session)
        assert session.deck.sent == [], args
    plugin, session, _logs = _drawing({})
    session.overlay_active = True
    await plugin._render_info_strip(session)
    assert session.deck.sent == []


# ──── The show_message overlay ────


@pytest.mark.asyncio
async def test_an_overlay_tiles_the_keys_lights_the_touch_keys_and_fills_both_screens(pil):
    plugin, session, logs = _drawing({})
    session.is_virtual = True
    await plugin._draw_overlay(session, "Mics are live")
    assert session.deck.sent == [
        ("key", 0), ("key", 1), ("key", 2), ("key", 3),
        ("color", 4, (255, 255, 255)), ("color", 5, (255, 255, 255)),
        ("strip", "native-strip", (400, 100), (0, 0, 400, 100)),
        ("screen", "native-screen", (248, 58)),
    ]
    assert _Helper.converted == [("key", (72, 72))] * 4 + [("strip", (400, 100)), ("screen", (248, 58))]
    assert {item for _serial, item in plugin._mirror_blobs} >= {
        "key_0", "key_3", "touchscreen", "screen"}
    assert logs == []


@pytest.mark.asyncio
async def test_an_overlay_tile_holds_its_part_of_one_canvas(pil):
    plugin, session, _logs = _drawing({}, keys=2, layout=(1, 2), touch_keys=0,
                                      touch=False, screen=(0, 0))
    session.is_virtual = True
    await plugin._draw_overlay(session, "W")
    from io import BytesIO
    tiles = [pil.open(BytesIO(plugin._mirror_blobs[(session.serial, f"key_{i}")][0]))
             for i in (0, 1)]
    assert all(t.size == (72, 72) for t in tiles)
    # The word spans both tiles, each cut from the one dark canvas.
    for tile in tiles:
        colors = _colors(tile.convert("RGB"))
        assert (16, 16, 24) in colors and len(colors) > 1
    assert session.deck.sent == [("key", 0), ("key", 1)]


@pytest.mark.asyncio
async def test_an_overlay_the_deck_refuses_is_not_logged(pil):
    plugin, session, logs = _drawing({}, refuse=("strip", "screen"))
    await plugin._draw_overlay(session, "Hi")
    assert [s[0] for s in session.deck.sent] == ["key"] * 4 + ["color"] * 2
    assert logs == []


@pytest.mark.asyncio
async def test_an_overlay_skips_a_strip_or_screen_it_cannot_size(pil):
    plugin, session, _logs = _drawing({}, strip=RuntimeError("no"), screen=RuntimeError("no"))
    await plugin._draw_overlay(session, "Hi")
    assert [s[0] for s in session.deck.sent] == ["key"] * 4 + ["color"] * 2
    plugin, session, _logs = _drawing({}, strip=(0, 100), screen=(248, 0))
    await plugin._draw_overlay(session, "Hi")
    assert [s[0] for s in session.deck.sent] == ["key"] * 4 + ["color"] * 2
    plugin, session, _logs = _drawing({}, touch=False, strip=(400, 100))
    await plugin._draw_overlay(session, "Hi")
    assert [s[0] for s in session.deck.sent] == ["key"] * 4 + ["color"] * 2 + ["screen"]


@pytest.mark.asyncio
async def test_an_overlay_draws_nothing_without_a_screen_pil_or_a_key_size(pil, monkeypatch):
    for args in ({"visual": False}, {"key_size": (0, 72)}, {"key_size": (72, 0)}):
        plugin, session, _logs = _drawing({}, **args)
        await plugin._draw_overlay(session, "Hi")
        assert session.deck.sent == [], args
    plugin, session, _logs = _drawing({})
    monkeypatch.setattr(sd_module, "Image", None)
    await plugin._draw_overlay(session, "Hi")
    assert session.deck.sent == []


def test_every_action_type_the_deck_runs_has_one_runner_and_no_other_does():
    from control_surfaces.streamdeck.key_entries import ACTION_KINDS
    assert tuple(StreamDeckPlugin._ACTION_RUNNERS) == ACTION_KINDS


@pytest.mark.asyncio
async def test_a_deck_without_a_strip_or_screen_reader_still_shows_the_overlay_on_its_keys(pil):
    class _KeysOnly(_ScreenDeck):
        touchscreen_image_format = property(lambda self: (_ for _ in ()).throw(AttributeError("no")))
        screen_image_format = property(lambda self: (_ for _ in ()).throw(AttributeError("no")))

    plugin, state, logs = _plugin({})
    plugin._load_text_font()
    session = _session(plugin, _KeysOnly())
    await plugin._draw_overlay(session, "Hi")
    assert [s[0] for s in session.deck.sent] == ["key"] * 4 + ["color"] * 2
    await plugin._render_info_strip(session)
    assert [s[0] for s in session.deck.sent] == ["key"] * 4 + ["color"] * 2
    assert logs == []


def test_the_config_check_reads_fields_only_for_action_types_the_deck_runs():
    from control_surfaces.streamdeck import config_check
    from control_surfaces.streamdeck.key_entries import ACTION_KINDS
    assert set(config_check._ACTION_FIELDS) <= set(ACTION_KINDS)
