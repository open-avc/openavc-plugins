"""
Stream Deck: what brightness a deck shows and when it dims, how big its touch
strip and info screen are, and what color a touch key shows.

Characterisation pins: the level a deck's brightness rules and its own base
level give, read edge by edge (a rule that cannot say its level, a level read
from text, the per-deck base, the clamp); when idle dim lowers a deck, which
delays and levels it never acts on, and what an input does to a dimmed deck;
the three readings of a touch strip's or an info screen's size (the zones'
width, the renders' format, the overlay's and the info screen's size) and
whether a deck counts as having an info screen; a touch key's color with its
marks; and the hex color readers.

No hardware: decks are stand-ins that record what they are asked.

Run from the openavc-plugins root:
pytest tests/test_streamdeck_brightness_and_sizes.py -v
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _platform_root import add_platform_to_path  # noqa: E402

add_platform_to_path()

_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))

from control_surfaces.streamdeck import streamdeck_plugin as sd_module  # noqa: E402
from control_surfaces.streamdeck.deck_profile import (  # noqa: E402
    deck_geometry,
    panel_size,
    strip_format,
)
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


class _Deck:
    """A deck stand-in: brightness, a strip and a screen as asked."""

    def __init__(self, *, strip=(800, 100), screen=(248, 58), dials=4,
                 brightness_error=None, strip_error=None, screen_error=None,
                 strip_format=None):
        self._strip = strip
        self._screen = screen
        self._dials = dials
        self._brightness_error = brightness_error
        self._strip_error = strip_error
        self._screen_error = screen_error
        self._strip_format = strip_format
        self.levels = []
        self.asked = []

    def is_visual(self):
        return True

    def is_touch(self):
        return True

    def key_count(self):
        return 8

    def touch_key_count(self):
        return 0

    def key_layout(self):
        return (2, 4)

    def dial_count(self):
        self.asked.append("dial_count")
        return self._dials

    def deck_type(self):
        return "Stand-in"

    def touchscreen_image_format(self):
        self.asked.append("strip")
        if self._strip_error is not None:
            raise self._strip_error
        if self._strip_format is not None:
            return self._strip_format
        return {"size": self._strip, "flip": (False, False), "rotation": 0}

    def screen_image_format(self):
        self.asked.append("screen")
        if self._screen_error is not None:
            raise self._screen_error
        return {"size": self._screen}

    def set_brightness(self, level):
        if self._brightness_error is not None:
            raise self._brightness_error
        self.levels.append(level)


def _session(plugin, deck=None, serial="SN1"):
    session = sd_module._DeckSession(deck if deck is not None else _Deck())
    session.serial = serial
    session.device_id = f"dev-{serial}"
    plugin._sessions[session.device_id] = session
    return session


def _rule(level, key="var.on"):
    return {"level": level, "when": {"key": key, "operator": "truthy"}}


class _Clock:
    """A loop whose time() is what the test says."""

    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    loop = _Clock(1000.0)
    monkeypatch.setattr(sd_module.asyncio, "get_event_loop", lambda: loop)
    return loop


# ──── The level a deck shows ────


async def _level(config, serial="SN1", on=True):
    plugin, state, _logs = _plugin(config)
    if on:
        state.set("var.on", "1", source="test")
    return await plugin._current_brightness_level(_session(plugin, serial=serial))


@pytest.mark.asyncio
@pytest.mark.parametrize("level, expect", [
    (35, 35), ("35", 35), ("55.9", 55), (55.9, 55), (-4, 0), ("-4", 0),
    (101, 100), (True, 1), (0, 0),
])
async def test_a_matching_rule_level_is_read_as_a_whole_number_from_0_to_100(level, expect):
    assert await _level({"brightness": 70, "auto_brightness": [_rule(level)]}) == expect


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", [
    "x", 5, None, {"level": 20}, {"when": {"key": "var.on"}},
    {"level": "abc", "when": {"key": "var.on", "operator": "truthy"}},
    {"level": None, "when": {"key": "var.on", "operator": "truthy"}},
    {"level": [1], "when": {"key": "var.on", "operator": "truthy"}},
])
async def test_a_rule_that_cannot_say_its_level_or_when_is_skipped(rule):
    config = {"brightness": 60, "auto_brightness": [rule, _rule(15)]}
    assert await _level(config) == 15
    config = {"brightness": 60, "auto_brightness": [rule]}
    assert await _level(config) == 60


@pytest.mark.asyncio
async def test_a_rule_without_a_level_is_skipped_before_its_when_is_judged():
    plugin, _state, _logs = _plugin({"auto_brightness": [
        {"level": "abc", "when": {"key": "var.a"}},
        {"when": {"key": "var.b"}},
        {"level": 5, "when": {"key": "var.c"}},
        {"level": 9, "when": {"key": "var.d"}},
    ]})
    judged = []

    async def _judge(cond):
        judged.append(cond["key"])
        return cond["key"] == "var.c"
    plugin._eval_condition = _judge
    assert await plugin._current_brightness_level(_session(plugin)) == 5
    assert judged == ["var.c"]


@pytest.mark.asyncio
@pytest.mark.parametrize("rules", ["x", {"level": 5}, 7, None])
async def test_rules_that_are_not_a_list_leave_the_base_level(rules):
    assert await _level({"brightness": 44, "auto_brightness": rules}) == 44


@pytest.mark.asyncio
async def test_the_rules_come_from_the_decks_own_layout_and_the_base_from_the_unit():
    config = {
        "brightness": 50,
        "auto_brightness": [_rule(10)],
        "decks": {"SN2": {"auto_brightness": [_rule(30)], "brightness": 99}},
        "deck_settings": {"SN2": {"brightness": 80}},
    }
    assert await _level(config, "SN1") == 10
    assert await _level(config, "SN2") == 30
    assert await _level(config, "SN2", on=False) == 80
    assert await _level(config, "SN1", on=False) == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("config, expect", [
    ({}, 70),
    ({"brightness": "33.3"}, 33),
    ({"brightness": "abc"}, 70),
    ({"brightness": None}, 70),
    ({"brightness": 250}, 100),
    ({"brightness": -1}, 0),
    ({"brightness": False}, 0),
    ({"brightness": 40, "deck_settings": {"SN1": {"brightness": "45"}}}, 45),
    ({"brightness": 40, "deck_settings": {"SN1": {"brightness": "abc"}}}, 40),
    ({"brightness": 40, "deck_settings": {"SN1": {"brightness": None}}}, 40),
    ({"brightness": 40, "deck_settings": {"SN1": {}}}, 40),
    ({"brightness": 40, "deck_settings": {"SN1": 12}}, 40),
    ({"brightness": 40, "deck_settings": ["SN1"]}, 40),
    ({"brightness": 40, "deck_settings": {"SN9": {"brightness": 5}}}, 40),
    ({"brightness": "abc", "deck_settings": {"SN1": {"brightness": 0}}}, 0),
    ({"deck_settings": {"SN1": {"brightness": 300}}}, 100),
])
async def test_the_base_level_is_the_units_then_the_plugins_then_70(config, expect):
    assert await _level(config, on=False) == expect


# ──── Applying a level ────


@pytest.mark.asyncio
async def test_a_level_reaches_the_deck_and_a_refusal_is_logged_at_debug():
    plugin, _state, logs = _plugin()
    deck = _Deck()
    session = _session(plugin, deck)
    plugin._set_deck_brightness(session, 33)
    assert deck.levels == [33]
    session.deck = _Deck(brightness_error=RuntimeError("busy"))
    plugin._set_deck_brightness(session, 33)
    assert ("debug", "Error setting brightness: busy") in logs
    session.deck = None
    plugin._set_deck_brightness(session, 33)


@pytest.mark.asyncio
async def test_the_active_level_is_not_read_for_a_deck_that_is_gone():
    plugin, _state, _logs = _plugin()
    session = _session(plugin)
    read = []

    async def _current(s):
        read.append(s)
        return 12
    plugin._current_brightness_level = _current
    session.deck = None
    await plugin._apply_active_brightness(session)
    assert read == []
    deck = _Deck()
    session.deck = deck
    await plugin._apply_active_brightness(session)
    assert read == [session] and deck.levels == [12]


# ──── Idle dim ────


async def _dim(idle_dim, idle_for, *, dimmed=False, serial="SN1", config=None):
    plugin, _state, _logs = _plugin({**(config or {}), "idle_dim": idle_dim})
    deck = _Deck()
    session = _session(plugin, deck, serial=serial)
    session.idle_dimmed = dimmed
    session.last_input = sd_module.asyncio.get_event_loop().time() - idle_for
    await plugin._check_idle_dim(session)
    return session, deck


@pytest.mark.asyncio
@pytest.mark.parametrize("after, level, idle_for, expect", [
    (10, 5, 10, [5]),           # exactly the delay dims
    (10, 5, 9.99, []),
    ("10", "5", 11, [5.0]),
    (10, "12.7", 11, [12]),
    (10, 150, 11, [100]),
    (10, -3, 11, [0]),
    (0.5, 1, 1, [1]),
    (10, True, 11, [1]),
])
async def test_idle_dim_lowers_a_deck_once_it_has_waited_the_delay(clock, after, level, idle_for, expect):
    session, deck = await _dim({"after_seconds": after, "level": level}, idle_for)
    assert deck.levels == [int(v) for v in expect]
    assert session.idle_dimmed is bool(expect)


@pytest.mark.asyncio
@pytest.mark.parametrize("idle_dim", [
    None, "x", [10, 5], 7,
    {"level": 5},
    {"after_seconds": 10},
    {"after_seconds": "abc", "level": 5},
    {"after_seconds": 10, "level": "abc"},
    {"after_seconds": 10, "level": None},
    {"after_seconds": [10], "level": 5},
    # A delay that is not above 0 never dims (the config check does not
    # say so yet; pinned as it is).
    {"after_seconds": 0, "level": 5},
    {"after_seconds": -5, "level": 5},
    {"after_seconds": "-5", "level": 5},
    {"after_seconds": "nan", "level": 5},
    {"after_seconds": False, "level": 5},
])
async def test_idle_dim_that_cannot_say_a_delay_and_a_level_never_dims(clock, idle_dim):
    session, deck = await _dim(idle_dim, 10 ** 6)
    assert deck.levels == [] and session.idle_dimmed is False


@pytest.mark.asyncio
async def test_a_dimmed_deck_is_not_dimmed_again(clock):
    session, deck = await _dim({"after_seconds": 10, "level": 5}, 100, dimmed=True)
    assert deck.levels == [] and session.idle_dimmed is True


@pytest.mark.asyncio
async def test_a_level_that_reads_as_nan_raises_once_the_deck_is_marked_dimmed(clock):
    plugin, _state, _logs = _plugin({"idle_dim": {"after_seconds": 10, "level": "nan"}})
    deck = _Deck()
    session = _session(plugin, deck)
    session.last_input = clock.now - 9
    await plugin._check_idle_dim(session)
    assert session.idle_dimmed is False
    session.last_input = clock.now - 11
    with pytest.raises(ValueError):
        await plugin._check_idle_dim(session)
    assert session.idle_dimmed is True and deck.levels == []


@pytest.mark.asyncio
async def test_idle_dim_is_read_from_the_decks_own_layout(clock):
    config = {"decks": {"SN2": {"idle_dim": {"after_seconds": 10, "level": 3}}}}
    plugin, _state, _logs = _plugin({**config, "idle_dim": {"after_seconds": 10, "level": 8}})
    one, two = _Deck(), _Deck()
    s1, s2 = _session(plugin, one, "SN1"), _session(plugin, two, "SN2")
    s1.last_input = s2.last_input = clock.now - 20
    await plugin._check_idle_dim(s1)
    await plugin._check_idle_dim(s2)
    assert one.levels == [8] and two.levels == [3]


@pytest.mark.asyncio
async def test_input_wakes_a_dimmed_deck_and_resets_its_idle_time(clock):
    plugin, _state, _logs = _plugin({"brightness": 66})
    deck = _Deck()
    session = _session(plugin, deck)
    session.idle_dimmed = True
    session.last_input = 3.0
    await plugin._note_input(session)
    assert session.last_input == 1000.0
    assert session.idle_dimmed is False and deck.levels == [66]
    clock.now = 1005.0
    await plugin._note_input(session)
    assert session.last_input == 1005.0 and deck.levels == [66]


# ──── A touch strip's and an info screen's size ────


def _zones(deck, config=None):
    plugin, _state, _logs = _plugin(config or {"touchscreen": {"zones": [{}, {}]}})
    session = _session(plugin, deck)
    return plugin._touch_zones(session)


def test_the_zones_are_laid_out_on_the_strips_reported_width():
    zones = _zones(_Deck(strip=(600, 100)))
    assert [(z["x"], z["w"]) for z in zones] == [(0, 300), (300, 300)]


@pytest.mark.parametrize("error", [AttributeError("none"), KeyError("size"), TypeError("x")])
def test_a_strip_that_cannot_say_its_width_is_taken_as_800(error):
    zones = _zones(_Deck(strip_error=error))
    assert [(z["x"], z["w"]) for z in zones] == [(0, 400), (400, 400)]


def test_a_strip_format_without_a_size_is_taken_as_800():
    zones = _zones(_Deck(strip_format={"flip": (False, False)}))
    assert [(z["x"], z["w"]) for z in zones] == [(0, 400), (400, 400)]


def test_a_strip_of_width_0_lays_its_zones_out_on_0():
    zones = _zones(_Deck(strip=(0, 100)))
    assert [(z["x"], z["w"]) for z in zones] == [(0, 0), (0, 0)]


def test_the_strip_width_is_read_before_the_dials_are_counted():
    deck = _Deck(strip=(400, 100), dials=2)
    zones = _zones(deck, config={"brightness": 70})
    assert deck.asked == ["strip", "dial_count"]
    assert [(z["x"], z["w"]) for z in zones] == [(0, 200), (200, 200)]


def test_a_session_with_no_deck_has_no_zones():
    plugin, _state, _logs = _plugin({"touchscreen": {"zones": [{}]}})
    session = _session(plugin)
    session.deck = None
    assert plugin._touch_zones(session) == []


def test_the_renders_read_the_strips_format_and_both_sides_as_reported():
    fmt = {"size": (800, 0), "flip": (True, False), "rotation": 90}
    assert strip_format(_Deck(strip_format=fmt)) == (fmt, 800, 0)
    plain = strip_format(_Deck(strip=(0, 0)))
    assert plain[1:] == (0, 0) and plain[0]["size"] == (0, 0)


@pytest.mark.parametrize("deck", [
    _Deck(strip_error=AttributeError("none")),
    _Deck(strip_format={"flip": (False, False)}),
    _Deck(strip_format={"size": (800,)}),
    _Deck(strip_format={"size": None}),
])
def test_the_renders_get_no_format_from_a_strip_that_cannot_say_it(deck):
    assert strip_format(deck) is None


@pytest.mark.parametrize("reader", ["touchscreen_image_format", "screen_image_format"])
def test_the_overlay_and_info_screen_size_needs_both_sides(reader):
    size = panel_size
    assert size(_Deck(strip=(640, 80), screen=(640, 80)), reader) == (640, 80)
    for side in ((0, 80), (640, 0), (0, 0)):
        assert size(_Deck(strip=side, screen=side), reader) is None
    broken = _Deck(strip_error=KeyError("x"), screen_error=KeyError("x"))
    assert size(broken, reader) is None
    assert size(SimpleNamespace(), reader) is None


def test_a_deck_has_an_info_screen_when_its_screen_reports_a_width():
    identity = SimpleNamespace(is_virtual=False, transport="usb")
    assert deck_geometry(_Deck(screen=(248, 58)), identity)["has_info_screen"] is True
    assert deck_geometry(_Deck(screen=(248, 0)), identity)["has_info_screen"] is True
    assert deck_geometry(_Deck(screen=(0, 58)), identity)["has_info_screen"] is False
    broken = _Deck(screen_error=AttributeError("none"))
    assert deck_geometry(broken, identity)["has_info_screen"] is False


# ──── A touch key's color ────


@pytest.mark.parametrize("bg, mark, nav, held, expect", [
    ("#204060", None, False, False, "#204060"),
    ("#204060", None, True, False, "#4c667f"),
    ("#204060", None, False, True, "#576f87"),
    ("#204060", None, True, True, "#788c9f"),
    ("#204060", "running", False, False, "#f59e0b"),
    ("#204060", "running", True, False, "#f59e0b"),
    ("#204060", "done", False, True, "#59d386"),
    ("#204060", "error", True, True, "#f37272"),
    # A mark with no color of its own leaves the key's, with no "you are
    # here" lift.
    ("#204060", "paused", True, False, "#204060"),
    ("junk", None, False, False, "junk"),
    ("junk", None, True, False, "#333333"),
])
def test_a_touch_keys_color_with_its_marks(bg, mark, nav, held, expect):
    plugin, _state, _logs = _plugin()
    assert plugin._touch_key_color(bg, mark, nav, held) == expect


@pytest.mark.parametrize("color, rgb", [
    ("#abc", (170, 187, 204)), ("ABCDEF", (171, 205, 239)), ("#12345", (0, 0, 0)),
    ("#gggggg", (0, 0, 0)), ("#ggg", (0, 0, 0)), ("", (0, 0, 0)), (0, (0, 0, 0)),
    ("##123456", (18, 52, 86)),
])
def test_a_hex_color_reads_as_rgb_or_black(color, rgb):
    assert StreamDeckPlugin._hex_to_rgb(color) == rgb


@pytest.mark.parametrize("color, amount, expect", [
    ("#000000", 1.0, "#ffffff"), ("#102030", 0, "#102030"),
    ("#808080", 0.5, "#bfbfbf"), ("xyz", 0.2, "#333333"), ("bad", 0.2, "#c8bbe3"),
])
def test_lightening_blends_toward_white(color, amount, expect):
    assert StreamDeckPlugin._lighten_hex(color, amount) == expect
