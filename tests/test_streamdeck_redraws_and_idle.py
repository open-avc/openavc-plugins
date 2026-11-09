"""
Stream Deck: redrawing a whole deck, what an idle screen shows, what a dial
turn or a fader touch writes, and which keys a deck has.

Characterisation pins: the order a deck's keys, touch strip and info screen
are redrawn in when it opens, when its config changes, when the Live View
turns mirroring on and when a message overlay clears; when the touch strip and
the info screen are idle and whether they show the clock; the info screen's
elements and the state keys a deck watches for them; the value a dial turn or a
fader touch writes; the LCD and touch keys a deck draws, flashes and lights for
an overlay or Identify; the label font's fallbacks; the strip's debounced
redraw; and the state a deck publishes about itself.

No hardware: decks are stand-ins that record what they are asked.

Run from the openavc-plugins root:
pytest tests/test_streamdeck_redraws_and_idle.py -v
"""

import asyncio
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
from control_surfaces.streamdeck.screens import info_items  # noqa: E402
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
    """A deck stand-in: LCD keys, touch keys, a strip and a screen as asked."""

    def __init__(self, *, keys=4, touch_keys=2, visual=True, touch=True,
                 strip=(800, 100), screen=(248, 58)):
        self._keys = keys
        self._touch_keys = touch_keys
        self._visual = visual
        self._touch = touch
        self._strip = strip
        self._screen = screen
        self.calls = []

    def is_visual(self):
        return self._visual

    def is_touch(self):
        return self._touch

    def key_count(self):
        return self._keys

    def touch_key_count(self):
        return self._touch_keys

    def key_layout(self):
        return (1, self._keys)

    def key_image_format(self):
        return {"size": (72, 72)}

    def touchscreen_image_format(self):
        if self._strip is None:
            raise AttributeError("no strip")
        return {"size": self._strip, "flip": (False, False), "rotation": 0}

    def screen_image_format(self):
        if self._screen is None:
            raise AttributeError("no screen")
        return {"size": self._screen}

    def set_key_image(self, key, image):
        self.calls.append(("key_image", key))

    def set_key_color(self, key, r, g, b):
        self.calls.append(("key_color", key, (r, g, b)))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _session(plugin, deck=None, serial="SN1", virtual=False):
    session = sd_module._DeckSession(deck if deck is not None else _Deck())
    session.serial = serial
    session.device_id = f"dev-{serial}"
    session.is_virtual = virtual
    plugin._sessions[session.device_id] = session
    return session


def _record(plugin, names, log):
    """Replace plugin methods with recorders that note (name, session serial)."""
    for name in names:
        async def recorder(*args, _name=name, **kwargs):
            first = args[0] if args else None
            log.append((_name, getattr(first, "serial", first)))
        setattr(plugin, name, recorder)


_RENDERS = ("_render_all_buttons", "_render_touchscreen", "_render_info_strip")


# ──── Redrawing a whole deck ────


@pytest.mark.asyncio
async def test_a_config_change_redraws_each_deck_after_its_page_and_brightness():
    plugin, _state, _logs = _plugin()
    _session(plugin, serial="A")
    _session(plugin, serial="B")
    log = []
    _record(plugin, (
        "_drop_feedback_subscriptions", "_setup_feedback_subscriptions",
        "_settle_page", "_apply_active_brightness", *_RENDERS,
    ), log)

    async def publish():
        log.append(("_publish_deck_state", None))
    plugin._publish_deck_state = publish

    assert await plugin.on_config_changed({}) is True
    per_deck = [
        "_drop_feedback_subscriptions", "_setup_feedback_subscriptions",
        "_settle_page", "_apply_active_brightness", *_RENDERS,
    ]
    assert log == (
        [(n, "A") for n in per_deck] + [(n, "B") for n in per_deck]
        + [("_publish_deck_state", None)]
    )


@pytest.mark.asyncio
async def test_mirroring_turned_on_redraws_every_deck_in_turn():
    plugin, _state, _logs = _plugin()
    _session(plugin, serial="A")
    _session(plugin, serial="B", virtual=True)
    log = []
    _record(plugin, _RENDERS, log)
    await plugin._set_live_mirror({"on": True})
    assert plugin._mirror_physical is True
    assert log == [(n, "A") for n in _RENDERS] + [(n, "B") for n in _RENDERS]


@pytest.mark.asyncio
async def test_mirroring_asked_for_as_it_already_is_does_nothing():
    plugin, _state, _logs = _plugin()
    _session(plugin, serial="A")
    plugin._mirror_blobs[("A", "key_0")] = (b"x", "image/png")
    log = []
    _record(plugin, _RENDERS, log)
    await plugin._set_live_mirror({"on": False})
    assert log == []
    assert ("A", "key_0") in plugin._mirror_blobs
    plugin._mirror_physical = True
    await plugin._set_live_mirror({"on": 1})
    assert log == []


@pytest.mark.asyncio
async def test_mirroring_turned_off_drops_physical_decks_images_only():
    plugin, _state, _logs = _plugin()
    _session(plugin, serial="PHYS")
    _session(plugin, serial="VIRT", virtual=True)
    plugin._mirror_physical = True
    for serial in ("PHYS", "VIRT"):
        plugin._mirror_blobs[(serial, "key_0")] = (b"x", "image/png")
        plugin._mirror_blobs[(serial, "touchscreen")] = (b"y", "image/png")
    log = []
    _record(plugin, _RENDERS, log)
    await plugin._set_live_mirror({})
    assert plugin._mirror_physical is False
    assert log == []
    assert sorted(plugin._mirror_blobs) == [("VIRT", "key_0"), ("VIRT", "touchscreen")]


class _Handle:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


@pytest.mark.asyncio
async def test_clearing_an_overlay_redraws_the_deck():
    plugin, _state, _logs = _plugin()
    session = _session(plugin, serial="A")
    handle = _Handle()
    session.overlay_handle = handle
    session.overlay_active = True
    log = []

    async def first(s):
        log.append(("_render_all_buttons", s.overlay_active))
    plugin._render_all_buttons = first
    _record(plugin, _RENDERS[1:], log)
    await plugin._clear_overlay(session)
    assert handle.cancelled and session.overlay_handle is None
    assert log == [("_render_all_buttons", False)] + [(n, "A") for n in _RENDERS[1:]]


@pytest.mark.asyncio
async def test_clearing_no_overlay_only_ends_its_timer():
    plugin, _state, _logs = _plugin()
    session = _session(plugin, serial="A")
    log = []
    _record(plugin, _RENDERS, log)
    await plugin._clear_overlay(session)
    assert log == [] and session.overlay_handle is None
    handle = _Handle()
    session.overlay_handle = handle
    await plugin._clear_overlay(session)
    assert handle.cancelled and session.overlay_handle is None
    assert log == []


@pytest.mark.asyncio
async def test_a_deck_that_opens_is_drawn_after_its_subscriptions_and_page(monkeypatch):
    plugin, _state, _logs = _plugin()
    plugin._loop = asyncio.get_event_loop()
    log = []

    class _OpeningDeck(_Deck):
        def open(self):
            pass

        def reset(self):
            pass

        def set_brightness(self, level):
            pass

        def id(self):
            return "hid-1"

        def get_serial_number(self):
            return "OPEN1"

        def deck_type(self):
            return "Stream Deck +"

        def is_open(self):
            return True

        def dial_count(self):
            return 0

        def set_key_callback_async(self, *a, **k):
            pass

    monkeypatch.setattr(plugin, "_publish_deck_state", lambda: _noop())
    _record(plugin, (
        "_setup_feedback_subscriptions", "_show_opening_page", *_RENDERS,
    ), log)
    await plugin._open_deck(_OpeningDeck(touch=False))
    assert [name for name, _ in log] == [
        "_setup_feedback_subscriptions", "_show_opening_page", *_RENDERS,
    ]


async def _noop():
    return None


# ──── The touch strip's idle mode ────


def _strip_idle(config, serial="SN1"):
    plugin, _state, _logs = _plugin(config)
    return plugin._strip_idle_mode(_session(plugin, serial=serial))


@pytest.mark.parametrize("config, expect", [
    ({}, "clock"),
    ({"touchscreen": {"idle": "blank"}}, "blank"),
    ({"touchscreen": {"idle": "clock"}}, "clock"),
    ({"touchscreen": {"idle": "dark"}}, "clock"),
    ({"touchscreen": {"idle": "BLANK"}}, "clock"),
    ({"touchscreen": "blank"}, "clock"),
    ({"touchscreen": {"idle": "blank", "zones": [{"label": "A"}]}}, None),
    ({"touchscreen": {"zones": ["not a zone", 3]}}, "clock"),
    ({"touchscreen": {"zones": [{}]}}, None),
    ({"dials": [{"index": 0}]}, "clock"),
    ({"dials": [{"index": 0, "unit": "%"}]}, "clock"),
    ({"dials": [{"index": 0, "label": ""}]}, "clock"),
    ({"dials": [{"index": 0, "label": "Vol"}]}, None),
    ({"dials": [{"index": 0, "pressed_ccw": [{"action": "macro"}]}]}, None),
    ({"dials": "not a list"}, "clock"),
    ({"dials": [{"index": 0, "icon": "mic"}], "touchscreen": {"idle": "blank"}}, None),
])
def test_when_the_strip_is_idle_and_what_it_shows(config, expect):
    assert _strip_idle(config) == expect


def test_the_strip_reads_its_own_decks_config_view():
    config = {
        "dials": [{"index": 0, "label": "Vol"}],
        "decks": {"OWN": {"touchscreen": {"idle": "blank"}}},
    }
    assert _strip_idle(config, serial="OWN") == "blank"
    assert _strip_idle(config, serial="OTHER") is None


# ──── The info screen's idle mode ────


def _info_idle(info_strip, serial="SN1", decks=None):
    config = {} if info_strip is _MISSING else {"info_strip": info_strip}
    if decks is not None:
        config["decks"] = decks
    plugin, _state, _logs = _plugin(config)
    return plugin._info_idle_mode(_session(plugin, serial=serial))


_MISSING = object()


@pytest.mark.parametrize("info_strip, expect", [
    (_MISSING, "clock"),
    (None, "clock"),
    ("Room A", "clock"),
    ({}, "clock"),
    ({"source": "clock"}, "clock"),
    ({"source": "clock", "key": "var.t"}, "clock"),
    ({"source": "blank"}, "blank"),
    ({"source": "blank", "key": "var.t"}, "blank"),
    ({"source": "Blank"}, "clock"),
    ({"source": "state"}, "clock"),
    ({"source": "text"}, "clock"),
    ({"source": "text", "text": "Room A"}, None),
    ({"source": "text", "text": ""}, "clock"),
    ({"source": "state", "key": "var.t"}, None),
    ({"key": "var.t"}, None),
    ({"label": "Temp"}, None),
    ({"label": ""}, "clock"),
    ({"label_source": "var.l"}, None),
    ({"value_source": "var.v"}, None),
    ({"icon": "sun"}, None),
    ({"meter": {"min": 0}}, None),
    ({"meter": False}, "clock"),
    ({"unit": "F"}, "clock"),
    ({"feedback": {"key": "var.f"}}, "clock"),
    ({"value_static": "x"}, "clock"),
    ({"items": [{"label": "A"}]}, None),
    ({"items": [{}]}, None),
    ({"items": []}, "clock"),
    ({"items": ["a", 3]}, "clock"),
    ({"items": ["a"], "label": "A"}, None),
    ({"items": "not a list"}, "clock"),
    ({"items": "not a list", "key": "var.t"}, None),
    ({"items": [{"label": "A"}], "source": "blank"}, "blank"),
])
def test_when_the_info_screen_is_idle_and_what_it_shows(info_strip, expect):
    assert _info_idle(info_strip) == expect


def test_the_info_screen_reads_its_own_decks_config_view():
    decks = {"OWN": {"info_strip": {"source": "blank"}}}
    assert _info_idle({"key": "var.t"}, serial="OWN", decks=decks) == "blank"
    assert _info_idle({"key": "var.t"}, serial="OTHER", decks=decks) is None
    assert _info_idle({"key": "var.t"}, serial="OWN", decks={"OWN": {}}) == "clock"


# ──── The info screen's elements ────


def test_info_elements_from_a_non_object_are_none():
    for cfg in (None, "x", 3, ["a"]):
        assert info_items(cfg, 248) == []


def test_an_items_list_with_no_objects_reads_the_config_itself():
    cfg = {"items": ["a", 3], "key": "var.t", "label": "T"}
    elements = info_items(cfg, 248)
    assert elements == [{
        "items": ["a", 3], "key": "var.t", "label": "T",
        "value_source": "var.t", "x": 0, "w": 248,
    }]
    assert "value_source" not in cfg and "x" not in cfg


def test_info_items_split_the_width_and_keep_the_first_two():
    items = [{"label": "A"}, "skip", {"key": "var.b"}, {"label": "C"}]
    elements = info_items({"items": items}, 249)
    assert elements == [
        {"label": "A", "value_source": "", "x": 0, "w": 124},
        {"key": "var.b", "value_source": "var.b", "x": 124, "w": 124},
    ]
    assert items[0] == {"label": "A"}
    one = info_items({"items": [{"label": "A"}]}, 0)
    assert one == [{"label": "A", "value_source": "", "x": 0, "w": 0}]


def test_an_info_elements_source_words():
    read = info_items
    text = read({"source": "text", "text": 42, "value_source": "var.v"}, 10)
    assert text == [{"source": "text", "text": 42, "value_static": "42", "x": 0, "w": 10}]
    no_text = read({"source": "text"}, 10)
    assert no_text[0]["value_static"] == ""
    own = read({"key": "var.k", "value_source": "var.v"}, 10)
    assert own[0]["value_source"] == "var.v"
    empty_own = read({"key": "var.k", "value_source": ""}, 10)
    assert empty_own[0]["value_source"] == "var.k"
    neither = read({"source": "state"}, 10)
    assert neither[0]["value_source"] == ""


@pytest.mark.asyncio
async def test_a_deck_watches_its_info_elements_keys():
    config = {
        "info_strip": {"items": [
            {"key": "var.legacy", "feedback": {"key": "var.fb"}},
            {"source": "text", "text": "Room", "value_source": "var.gone"},
            {"label_source": "var.lab"},
        ]},
    }
    plugin, _state, _logs = _plugin(config)
    session = _session(plugin)
    await plugin._setup_feedback_subscriptions(session)
    # The screen fits two items: the third is neither drawn nor watched.
    assert session.info_strip_keys == {"var.legacy", "var.fb"}


@pytest.mark.asyncio
async def test_an_idle_info_screen_still_watches_the_keys_it_names():
    plugin, _state, _logs = _plugin({"info_strip": {"source": "clock", "key": "var.t"}})
    session = _session(plugin)
    await plugin._setup_feedback_subscriptions(session)
    assert session.info_strip_keys == {"var.t"}


@pytest.mark.asyncio
async def test_no_info_strip_watches_nothing_for_the_screen():
    plugin, _state, _logs = _plugin({})
    session = _session(plugin)
    await plugin._setup_feedback_subscriptions(session)
    assert session.info_strip_keys == set()


# ──── The idle clock's tick ────


def _tick_rig(config, deck=None):
    plugin, _state, _logs = _plugin(config)
    session = _session(plugin, deck if deck is not None else _Deck())
    log = []
    _record(plugin, ("_render_touchscreen", "_render_info_strip"), log)
    return plugin, session, log


@pytest.mark.asyncio
async def test_the_clock_tick_redraws_each_idle_clock_once_a_minute():
    plugin, session, log = _tick_rig({})
    await plugin._tick_clock(session)
    assert [n for n, _ in log] == ["_render_touchscreen", "_render_info_strip"]
    log.clear()
    await plugin._tick_clock(session)
    assert log == []


@pytest.mark.asyncio
@pytest.mark.parametrize("config, deck_touch, expect", [
    ({"touchscreen": {"idle": "blank"}}, True, ["_render_info_strip"]),
    ({"info_strip": {"source": "blank"}}, True, ["_render_touchscreen"]),
    ({"info_strip": {"key": "var.t"}}, True, ["_render_touchscreen"]),
    ({"dials": [{"index": 0, "label": "V"}]}, True, ["_render_info_strip"]),
    ({}, False, ["_render_info_strip"]),
    ({"touchscreen": {"idle": "blank"}, "info_strip": {"source": "blank"}}, True, []),
])
async def test_the_clock_tick_redraws_only_the_screens_showing_the_clock(
    config, deck_touch, expect
):
    plugin, session, log = _tick_rig(config, _Deck(touch=deck_touch))
    await plugin._tick_clock(session)
    assert [n for n, _ in log] == expect
    assert session.clock_minute != -1


@pytest.mark.asyncio
async def test_the_clock_tick_notes_the_minute_on_a_deck_it_cannot_draw():
    plugin, session, log = _tick_rig({}, _Deck(visual=False))
    await plugin._tick_clock(session)
    assert log == [] and session.clock_minute != -1


@pytest.mark.asyncio
async def test_an_idle_screen_draws_the_clock_in_the_text_color_or_nothing():
    plugin, _state, _logs = _plugin({"text_color": "#123456"})
    session = _session(plugin)
    drawn = []
    plugin._draw_clock = lambda img, w, h, fg: drawn.append((w, h, fg))

    class _Img:
        size = (200, 50)
    plugin._draw_idle(session, _Img(), "clock")
    plugin._draw_idle(session, _Img(), "blank")
    plugin._draw_idle(session, _Img(), None)
    assert drawn == [(200, 50, "#123456")]


def test_the_check_names_an_idle_word_the_strip_does_not_know():
    problems = config_problems({"touchscreen": {"idle": "dark"}})
    assert problems == [
        'touchscreen.idle "dark" is not "clock" or "blank", so the strip shows the clock.'
    ]
    for word in ("clock", "blank", None):
        assert config_problems({"touchscreen": {"idle": word}}) == []


# ──── What a dial turn writes ────


def _adjust_rig(state_values=None):
    plugin, _state, logs = _plugin()
    writes = []

    async def state_get(key):
        return (state_values or {}).get(key)

    async def apply(key, value):
        writes.append((key, value, type(value).__name__))
    plugin.api.state_get = state_get
    plugin._apply_state_set = apply
    return plugin, writes, logs


@pytest.mark.asyncio
@pytest.mark.parametrize("adjust, current, detents, expect", [
    ({"key": "var.v"}, 5, 2, 7),
    ({"key": "var.v", "step": None}, 5, -1, 4),
    ({"key": "var.v", "step": "2"}, 5, 3, 11),
    ({"key": "var.v", "step": 0}, 5, 3, 5),
    ({"key": "var.v", "step": -2}, 5, 1, 3),
    ({"key": "var.v", "step": "x"}, 5, 1, 6),
    ({"key": "var.v", "step": 0.5}, 5, 1, 5.5),
    ({"key": "var.v", "step": 0.5}, 5, 2, 6),
    ({"key": "var.v", "min": 0, "max": 10}, 9, 5, 10),
    ({"key": "var.v", "min": 0, "max": 10}, 1, -5, 0),
    ({"key": "var.v", "min": 3}, None, 1, 4),
    ({"key": "var.v"}, None, 1, 1),
    ({"key": "var.v", "max": 2}, None, 5, 2),
    ({"key": "var.v"}, "4.5", 1, 5.5),
    ({"key": "var.v"}, "loud", 1, 1),
    ({"key": "var.v", "min": 10, "max": 0}, 5, 1, 0),
    ({"key": "var.v", "step": True}, 5, 1, 6),
])
async def test_a_dial_turn_writes_the_stepped_clamped_value(adjust, current, detents, expect):
    plugin, writes, _logs = _adjust_rig({"var.v": current})
    await plugin._apply_dial_adjust(adjust, detents)
    assert writes == [("var.v", expect, type(expect).__name__)]


@pytest.mark.asyncio
async def test_a_dial_turn_with_no_key_writes_to_the_empty_key():
    plugin, writes, _logs = _adjust_rig({})
    await plugin._apply_dial_adjust({"step": 1}, 1)
    assert writes == [("", 1, "int")]


# ──── What a fader touch writes ────


@pytest.mark.asyncio
@pytest.mark.parametrize("drag, zone, x, expect", [
    ({"key": "var.f", "min": 0, "max": 100}, {"x": 0, "w": 200}, 100, 50),
    ({"key": "var.f", "min": 0, "max": 100}, {"x": 0, "w": 200}, -50, 0),
    ({"key": "var.f", "min": 0, "max": 100}, {"x": 0, "w": 200}, 900, 100),
    ({"key": "var.f", "min": 0, "max": 100, "step": 10}, {"x": 200, "w": 200}, 233, 20),
    ({"key": "var.f", "min": 0, "max": 100, "step": 0}, {"x": 0, "w": 200}, 101, 50),
    ({"key": "var.f", "min": 0, "max": 100, "step": -5}, {"x": 0, "w": 200}, 101, 50),
    ({"key": "var.f", "min": 0, "max": 100, "step": "x"}, {"x": 0, "w": 200}, 101, 50),
    ({"key": "var.f", "min": 0, "max": 1, "step": 0.25}, {"x": 0, "w": 100}, 40, 0.5),
    ({"key": "var.f", "min": 0, "max": 1, "step": 0.25}, {"x": 0, "w": 100}, 30, 0.25),
    ({"key": "var.f", "min": 0, "max": 10, "step": 3}, {"x": 0, "w": 100}, 100, 9),
    # A touch past the zone counts as its edge, then snaps: 9 here, not max.
    ({"key": "var.f", "min": 0, "max": 10, "step": 3}, {"x": 0, "w": 100}, 500, 9),
    # The step grid can overshoot max (12 here); the value stops at max.
    ({"key": "var.f", "min": 0, "max": 10, "step": 6}, {"x": 0, "w": 100}, 100, 10),
    ({"key": "var.f", "min": "0", "max": "10"}, {}, 0, 0),
    ({"key": "var.f", "min": 0, "max": 10}, {"x": 0, "w": 0}, 1, 10),
    ({"key": "var.f", "min": 0, "max": 10}, {"x": 5, "w": -3}, 6, 10),
    ({"min": 0, "max": 10}, {"x": 0, "w": 100}, 50, 5),
])
async def test_a_fader_touch_writes_the_touched_place_on_the_step_grid(drag, zone, x, expect):
    plugin, writes, logs = _adjust_rig()
    assert await plugin._apply_touch_fader(drag, zone, x) is True
    assert writes == [(drag.get("key", ""), expect, type(expect).__name__)]
    assert logs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("drag", [
    {"key": "var.f"},
    {"key": "var.f", "min": 0},
    {"key": "var.f", "max": 10},
    {"key": "var.f", "min": 10, "max": 10},
    {"key": "var.f", "min": 10, "max": 0},
    {"key": "var.f", "min": "a", "max": 10},
])
async def test_a_fader_with_no_usable_bounds_writes_nothing(drag):
    plugin, writes, logs = _adjust_rig()
    assert await plugin._apply_touch_fader(drag, {"x": 0, "w": 100}, 50) is False
    assert writes == []
    assert logs == [("debug", "Touch fader needs min and max on its adjust; ignoring touch")]


# ──── Which keys a deck has ────


def test_touch_keys_are_the_ones_after_the_lcd_keys():
    plugin, _state, _logs = _plugin()
    session = _session(plugin, _Deck(keys=8, touch_keys=2))
    touch = [i for i in range(-1, 12) if StreamDeckPlugin._is_touch_key(session, i)]
    assert touch == [8, 9]
    assert StreamDeckPlugin._is_touch_key(session, 8.0) is True
    assert StreamDeckPlugin._is_touch_key(session, 8.5) is True
    session.deck = None
    assert StreamDeckPlugin._is_touch_key(session, 8) is False


@pytest.mark.asyncio
async def test_a_page_draws_every_lcd_and_touch_key():
    plugin, _state, _logs = _plugin()
    session = _session(plugin, _Deck(keys=3, touch_keys=2))
    drawn = []

    async def render(s, index):
        drawn.append(index)
    plugin._render_button = render
    await plugin._render_all_buttons(session)
    assert drawn == [0, 1, 2, 3, 4]
    drawn.clear()
    session.deck = _Deck(visual=False)
    await plugin._render_all_buttons(session)
    assert drawn == []


@pytest.mark.asyncio
@pytest.mark.parametrize("index, painted", [
    (-1, None), (0, "image"), (2, "image"), (3, "color"), (4, "color"), (5, None),
])
async def test_a_flash_reaches_each_key_the_deck_has(monkeypatch, index, painted):
    plugin, _state, _logs = _plugin()
    session = _session(plugin, _Deck(keys=3, touch_keys=2))
    calls = []
    monkeypatch.setattr(sd_module, "Image", object())
    monkeypatch.setattr(sd_module.asyncio, "sleep", _noop_sleep)
    plugin._create_button_image = lambda s, look: ("white", look.bg_color)
    plugin._apply_key_image = lambda s, i, img: calls.append(("image", i, img))
    plugin._apply_key_color = lambda s, i, c: calls.append(("color", i, c))

    async def render(s, i):
        calls.append(("render", i))
    plugin._render_button = render
    await plugin._flash_key(session, index, 2)
    if painted is None:
        assert calls == []
    elif painted == "image":
        paint = ("image", index, ("white", "#ffffff"))
        assert calls == [paint, ("render", index)] * 2
    else:
        assert calls == [("color", index, "#ffffff"), ("render", index)] * 2


async def _noop_sleep(_seconds):
    return None


@pytest.mark.asyncio
async def test_a_flash_without_pillow_still_redraws_an_lcd_key(monkeypatch):
    plugin, _state, _logs = _plugin()
    session = _session(plugin, _Deck(keys=3, touch_keys=2))
    calls = []
    monkeypatch.setattr(sd_module, "Image", None)
    monkeypatch.setattr(sd_module.asyncio, "sleep", _noop_sleep)
    plugin._create_button_image = lambda s, look: calls.append("made")
    plugin._apply_key_color = lambda s, i, c: calls.append(("color", i, c))

    async def render(s, i):
        calls.append(("render", i))
    plugin._render_button = render
    await plugin._flash_key(session, 1, 2)
    await plugin._flash_key(session, 4, 1)
    assert calls == [("render", 1), ("render", 1), ("color", 4, "#ffffff"), ("render", 4)]


@pytest.mark.asyncio
async def test_a_flash_waits_out_an_overlay_and_an_undrawable_deck(monkeypatch):
    plugin, _state, _logs = _plugin()
    session = _session(plugin, _Deck(keys=3, touch_keys=2))
    calls = []
    monkeypatch.setattr(sd_module.asyncio, "sleep", _noop_sleep)

    async def render(s, i):
        calls.append(i)
    plugin._render_button = render
    session.overlay_active = True
    await plugin._flash_key(session, 1, 1)
    session.overlay_active = False
    session.deck = _Deck(visual=False)
    await plugin._flash_key(session, 1, 1)
    assert calls == []


def test_an_overlay_lights_the_touch_keys_white(monkeypatch):
    img_mod = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(sd_module, "Image", img_mod)
    plugin, _state, _logs = _plugin()
    session = _session(plugin, _Deck(keys=3, touch_keys=2))
    calls = []
    plugin._paste_label = lambda *a: None
    plugin._apply_key_image = lambda s, i, img: calls.append(("image", i, img.size))
    plugin._apply_key_color = lambda s, i, c: calls.append(("color", i, c))
    plugin._overlay_keys(session, "Hi", 72, 72)
    assert calls == [
        ("image", 0, (72, 72)), ("image", 1, (72, 72)), ("image", 2, (72, 72)),
        ("color", 3, "#ffffff"), ("color", 4, "#ffffff"),
    ]


class _NativeHelper:
    @staticmethod
    def to_native_key_format(deck, img):
        return b"\x01\x02"


@pytest.mark.asyncio
async def test_identify_flashes_every_lcd_and_touch_key(monkeypatch):
    plugin, _state, _logs = _plugin()
    deck = _Deck(keys=2, touch_keys=1)
    session = _session(plugin, deck)
    calls = []
    monkeypatch.setattr(sd_module, "PILHelper", _NativeHelper)
    monkeypatch.setattr(sd_module.asyncio, "sleep", _noop_sleep)
    plugin._create_button_image = lambda s, look: "white"
    plugin._apply_key_color = lambda s, i, c: calls.append(("color", i, c))

    async def render_all(s):
        calls.append(("render_all", s.serial))
    plugin._render_all_buttons = render_all
    await plugin._identify_deck(session)
    assert deck.calls == [("key_image", 0), ("key_image", 1)] * 6
    assert calls == [
        ("color", 2, "#ffffff"), ("color", 2, "#000000"),
    ] * 3 + [("render_all", "SN1")]


@pytest.mark.asyncio
async def test_a_simulated_key_press_reaches_every_lcd_and_touch_key():
    plugin, _state, _logs = _plugin()
    session = _session(plugin, _Deck(keys=3, touch_keys=2), serial="SIM")
    pressed = []

    async def key_change(s, deck, index, down):
        pressed.append((index, down))
    plugin._on_key_change = key_change
    for index in (0, 4, 5, -1):
        await plugin._simulate_input({"serial": "SIM", "type": "key", "index": index})
    assert [p[0] for p in pressed if p[1]] == [0, 4]
    assert session.serial == "SIM"


# ──── The label font ────


class _Font:
    def __init__(self, name, size):
        self.name = name
        self.size = size


class _FontModule:
    def __init__(self, failing=()):
        self.failing = set(failing)
        self.calls = []

    def truetype(self, path, size):
        self.calls.append((path, size))
        if path in self.failing:
            raise OSError("cannot open resource")
        return _Font(path, size)

    def load_default(self):
        self.calls.append(("default", None))
        return _Font("default", None)


@pytest.mark.parametrize("bundled, failing, expect, calls", [
    ("/fonts/DejaVu.ttf", (), "/fonts/DejaVu.ttf", [("/fonts/DejaVu.ttf", 20)]),
    (None, (), "arial.ttf", [("arial.ttf", 20)]),
    ("", (), "arial.ttf", [("arial.ttf", 20)]),
    ("/fonts/DejaVu.ttf", ("/fonts/DejaVu.ttf",), "arial.ttf",
     [("/fonts/DejaVu.ttf", 20), ("arial.ttf", 20)]),
    ("/fonts/DejaVu.ttf", ("/fonts/DejaVu.ttf", "arial.ttf"), "default",
     [("/fonts/DejaVu.ttf", 20), ("arial.ttf", 20), ("default", None)]),
    (None, ("arial.ttf",), "default", [("arial.ttf", 20), ("default", None)]),
])
def test_a_label_font_falls_back_bundled_then_arial_then_the_default(
    monkeypatch, bundled, failing, expect, calls
):
    fonts = _FontModule(failing)
    monkeypatch.setattr(sd_module, "ImageFont", fonts)
    plugin, _state, _logs = _plugin()
    plugin._text_font_path = bundled
    font = plugin._text_font(20)
    assert font.name == expect
    assert fonts.calls == calls
    assert plugin._text_font(20) is font
    assert fonts.calls == calls


def test_a_font_that_is_not_an_os_error_is_raised(monkeypatch):
    class _Broken(_FontModule):
        def truetype(self, path, size):
            raise ValueError("bad size")
    monkeypatch.setattr(sd_module, "ImageFont", _Broken())
    plugin, _state, _logs = _plugin()
    plugin._text_font_path = "/fonts/DejaVu.ttf"
    with pytest.raises(ValueError):
        plugin._text_font(20)
    assert plugin._font_cache == {}


def test_each_font_size_is_opened_once(monkeypatch):
    fonts = _FontModule()
    monkeypatch.setattr(sd_module, "ImageFont", fonts)
    plugin, _state, _logs = _plugin()
    plugin._text_font_path = "/f.ttf"
    plugin._text_font(10)
    plugin._text_font(12)
    plugin._text_font(10)
    assert fonts.calls == [("/f.ttf", 10), ("/f.ttf", 12)]


# ──── The strip's debounced redraw ────


@pytest.mark.asyncio
async def test_a_flush_redraws_the_whole_strip_for_all_or_no_cached_image():
    plugin, _state, _logs = _plugin()
    session = _session(plugin)
    log = []

    async def full(s):
        log.append("full")

    async def zone(s, index):
        log.append(index)
    plugin._render_touchscreen = full
    plugin._render_strip_zone = zone
    session.strip_dirty = "all"
    session.strip_image = object()
    await plugin._flush_strip_render(session)
    session.strip_dirty = {2, 0}
    session.strip_image = None
    await plugin._flush_strip_render(session)
    session.strip_dirty = {2, 0, 1}
    session.strip_image = object()
    await plugin._flush_strip_render(session)
    assert log == ["full", "full", 0, 1, 2]
    assert session.strip_dirty is None and session.strip_render_task is None


@pytest.mark.asyncio
async def test_a_flush_keeps_going_while_new_zones_turn_dirty():
    plugin, _state, _logs = _plugin()
    session = _session(plugin)
    session.strip_image = object()
    log = []

    async def zone(s, index):
        log.append(index)
        if index == 1:
            s.strip_dirty = "all"

    async def full(s):
        log.append("full")
    plugin._render_strip_zone = zone
    plugin._render_touchscreen = full
    session.strip_dirty = {1, 3}
    session.strip_render_task = "running"
    await plugin._flush_strip_render(session)
    assert log == [1, 3, "full"]
    assert session.strip_render_task is None


@pytest.mark.asyncio
async def test_a_flush_that_fails_still_frees_the_next_redraw():
    plugin, _state, _logs = _plugin()
    session = _session(plugin)
    session.strip_image = object()

    async def boom(s, index):
        raise RuntimeError("deck gone")
    plugin._render_strip_zone = boom
    session.strip_dirty = {0}
    session.strip_render_task = "running"
    with pytest.raises(RuntimeError):
        await plugin._flush_strip_render(session)
    assert session.strip_render_task is None and session.strip_dirty is None


@pytest.mark.asyncio
async def test_a_flush_with_nothing_dirty_redraws_nothing():
    plugin, _state, _logs = _plugin()
    session = _session(plugin)
    log = []
    plugin._render_touchscreen = lambda s: log.append("full")
    for dirty in (None, set()):
        session.strip_dirty = dirty
        session.strip_render_task = "running"
        await plugin._flush_strip_render(session)
        assert session.strip_render_task is None
    assert log == []


# ──── The state a deck publishes ────


def _publish_rig(config=None):
    plugin, _state, _logs = _plugin(config)
    writes = []

    async def state_set(key, value):
        writes.append((key, value))
    plugin.api.state_set = state_set
    return plugin, writes


@pytest.mark.asyncio
async def test_with_no_deck_only_the_counts_and_connected_are_published():
    plugin, writes = _publish_rig()
    await plugin._publish_deck_state()
    assert writes == [("deck_count", 0), ("deck_serials", ""), ("connected", False)]


@pytest.mark.asyncio
async def test_each_deck_publishes_under_its_serial_and_the_first_unprefixed():
    plugin, writes = _publish_rig({"deck_names": {"B": "Lectern", "A": 7}})
    first = _session(plugin, serial="A")
    first.model = "Stream Deck +"
    first.geometry = {"rows": 2, "columns": 4}
    first.current_page = 1
    first.address = ""
    second = _session(plugin, serial="B")
    second.model = "Stream Deck Neo"
    second.geometry = {"rows": 2}
    second.address = "10.0.0.5:5343"
    await plugin._publish_deck_state()
    assert writes == [
        ("deck_count", 2), ("deck_serials", "A,B"),
        ("connected", True), ("model", "Stream Deck +"), ("serial", "A"),
        ("rows", 2), ("columns", 4), ("current_page", 1),
        ("A.connected", True), ("A.model", "Stream Deck +"), ("A.name", "7"),
        ("A.address", ""), ("A.rows", 2), ("A.columns", 4), ("A.current_page", 1),
        ("B.connected", True), ("B.model", "Stream Deck Neo"), ("B.name", "Lectern"),
        ("B.address", "10.0.0.5:5343"), ("B.rows", 2), ("B.current_page", 0),
    ]


@pytest.mark.asyncio
async def test_deck_names_that_are_not_a_map_name_no_deck():
    plugin, writes = _publish_rig({"deck_names": ["A"]})
    session = _session(plugin, serial="A")
    del session.address
    await plugin._publish_deck_state()
    assert ("A.name", "") in writes and ("A.address", "") in writes
