"""
Tests for the Elgato Stream Deck plugin's state-driven logic.

Covers the parts that don't need physical hardware or the StreamDeck/PIL
libraries:
- the vendored condition evaluator (operators + type coercion)
- condition key extraction (single / any / all)
- visible_when evaluation and the button-visibility helper
- auto_page rule evaluation (first match wins, clamping, no match)
- subscription key collection (feedback + toggle + visible_when + auto_page)

The plugin's start()/render paths need the StreamDeck + PIL libraries and a
physical deck, so they aren't exercised here. These tests bind a PluginAPI
directly (mirroring PluginTestHarness.start_plugin) without calling start(),
which is what pulls in those libraries.

Run from the openavc-plugins root: pytest tests/test_streamdeck_plugin.py -v
"""

import sys
from pathlib import Path

import pytest

# openavc server on the import path (for PluginAPI / harness)
_OPENAVC_ROOT = Path(__file__).resolve().parents[2] / "openavc"
if str(_OPENAVC_ROOT) not in sys.path:
    sys.path.insert(0, str(_OPENAVC_ROOT))

# plugins root, so the plugin imports by its package path
_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))

# The plugin module imports only the standard library at import time
# (StreamDeck/PIL are imported lazily inside start()), so it always imports.
from control_surfaces.streamdeck.streamdeck_plugin import (  # noqa: E402
    StreamDeckPlugin,
    _condition_state_keys,
    _eval_operator,
)
import control_surfaces.streamdeck.streamdeck_plugin as sd_module  # noqa: E402

try:
    from server.core.plugin_api import PluginAPI
    from server.core.plugin_registry import PluginRegistry
    from server.core.state_store import StateStore
    from server.core.event_bus import EventBus
    from server.core.plugin_test_harness import MockDeviceManager, MockMacroEngine
except ModuleNotFoundError:
    pytest.skip(
        "openavc repo not available as sibling directory",
        allow_module_level=True,
    )


# ──── Helpers ────


def _make_plugin_with_recorders(config=None):
    """Build a StreamDeckPlugin bound to a real StateStore via PluginAPI.

    Returns (plugin, state_store, macro_engine, device_manager) so a test can
    assert what the plugin executed. start() is intentionally not called (that
    is what pulls in the StreamDeck/PIL libraries and needs hardware).
    """
    state = StateStore()
    events = EventBus()
    state.set_event_bus(events)
    registry = PluginRegistry("streamdeck")
    macros = MockMacroEngine()
    devices = MockDeviceManager()
    logs: list[dict] = []
    api = PluginAPI(
        plugin_id="streamdeck",
        capabilities=StreamDeckPlugin.PLUGIN_INFO["capabilities"],
        config=config or {},
        registry=registry,
        state_store=state,
        event_bus=events,
        macro_engine=macros,
        device_manager=devices,
        platform_id="test",
        log_fn=lambda pid, msg, level="info": logs.append({"level": level, "message": msg}),
    )
    plugin = StreamDeckPlugin()
    plugin.api = api
    plugin._test_logs = logs  # captured log entries, for assertions
    return plugin, state, macros, devices


def _make_plugin(config=None):
    """Build a StreamDeckPlugin bound to a real StateStore via PluginAPI.

    Returns (plugin, state_store). start() is intentionally not called.
    """
    plugin, state, _macros, _devices = _make_plugin_with_recorders(config)
    return plugin, state


# ──── Vendored operator evaluator ────


def test_eval_operator_eq_ne_strings():
    assert _eval_operator("eq", "on", "on") is True
    assert _eval_operator("eq", "on", "off") is False
    assert _eval_operator("ne", "on", "off") is True
    assert _eval_operator("ne", "on", "on") is False


def test_eval_operator_numeric_string_coercion():
    # Strings that look like numbers compare numerically, not lexically.
    assert _eval_operator("gt", "10", "5") is True
    assert _eval_operator("lt", "10", "5") is False
    assert _eval_operator("gte", "5", "5") is True
    assert _eval_operator("lte", "5", "5") is True
    assert _eval_operator("eq", 5, "5") is True


def test_eval_operator_bool_coercion():
    assert _eval_operator("eq", True, "on") is True
    assert _eval_operator("eq", False, "off") is True
    assert _eval_operator("eq", True, "off") is False


def test_eval_operator_truthy_falsy():
    assert _eval_operator("truthy", 1, None) is True
    assert _eval_operator("truthy", 0, None) is False
    assert _eval_operator("truthy", "", None) is False
    assert _eval_operator("falsy", 0, None) is True
    assert _eval_operator("falsy", "x", None) is False


def test_eval_operator_none_guards():
    assert _eval_operator("gt", None, 5) is False
    assert _eval_operator("lt", 5, None) is False


def test_eval_operator_aliases():
    assert _eval_operator("equals", "a", "a") is True
    assert _eval_operator(">", "10", "5") is True


def test_eval_operator_unknown_raises():
    with pytest.raises(ValueError):
        _eval_operator("bogus", 1, 1)


# ──── Condition key extraction ────


def test_condition_state_keys_single():
    assert _condition_state_keys({"key": "device.a.power", "operator": "eq", "value": "on"}) == [
        "device.a.power"
    ]


def test_condition_state_keys_any_all_nested():
    cond = {"any": [{"key": "a"}, {"all": [{"key": "b"}, {"key": "c"}]}]}
    assert _condition_state_keys(cond) == ["a", "b", "c"]


def test_condition_state_keys_empty_and_invalid():
    assert _condition_state_keys(None) == []
    assert _condition_state_keys({}) == []
    assert _condition_state_keys({"any": []}) == []


# ──── _eval_condition (single / any / all) ────


@pytest.mark.asyncio
async def test_eval_condition_single():
    plugin, state = _make_plugin()
    state.set("device.proj.power", "on", source="test")
    assert await plugin._eval_condition({"key": "device.proj.power", "operator": "eq", "value": "on"}) is True
    assert await plugin._eval_condition({"key": "device.proj.power", "operator": "eq", "value": "off"}) is False


@pytest.mark.asyncio
async def test_eval_condition_truthy_on_unset_key():
    plugin, _ = _make_plugin()
    # Unset key reads as None -> truthy is False, falsy is True.
    assert await plugin._eval_condition({"key": "var.missing", "operator": "truthy"}) is False
    assert await plugin._eval_condition({"key": "var.missing", "operator": "falsy"}) is True


@pytest.mark.asyncio
async def test_eval_condition_any_or():
    plugin, state = _make_plugin()
    state.set("var.a", "no", source="test")
    state.set("var.b", "yes", source="test")
    cond = {"any": [
        {"key": "var.a", "operator": "eq", "value": "yes"},
        {"key": "var.b", "operator": "eq", "value": "yes"},
    ]}
    assert await plugin._eval_condition(cond) is True
    state.set("var.b", "no", source="test")
    assert await plugin._eval_condition(cond) is False


@pytest.mark.asyncio
async def test_eval_condition_all_and():
    plugin, state = _make_plugin()
    state.set("var.a", "yes", source="test")
    state.set("var.b", "yes", source="test")
    cond = {"all": [
        {"key": "var.a", "operator": "eq", "value": "yes"},
        {"key": "var.b", "operator": "eq", "value": "yes"},
    ]}
    assert await plugin._eval_condition(cond) is True
    state.set("var.b", "no", source="test")
    assert await plugin._eval_condition(cond) is False


@pytest.mark.asyncio
async def test_eval_condition_empty_groups_match_panel():
    plugin, _ = _make_plugin()
    # Mirrors panel.js: every([]) is True, some([]) is False.
    assert await plugin._eval_condition({"all": []}) is True
    assert await plugin._eval_condition({"any": []}) is False


@pytest.mark.asyncio
async def test_eval_condition_unknown_operator_is_false():
    plugin, state = _make_plugin()
    state.set("var.a", "x", source="test")
    assert await plugin._eval_condition({"key": "var.a", "operator": "bogus", "value": "x"}) is False


@pytest.mark.asyncio
async def test_eval_condition_missing_key_is_false():
    plugin, _ = _make_plugin()
    assert await plugin._eval_condition({"operator": "eq", "value": "on"}) is False
    assert await plugin._eval_condition("not a dict") is False


# ──── _is_button_visible ────


@pytest.mark.asyncio
async def test_is_button_visible_no_condition():
    plugin, _ = _make_plugin()
    assert await plugin._is_button_visible({"index": 0}) is True
    assert await plugin._is_button_visible({"index": 0, "bindings": {}}) is True


@pytest.mark.asyncio
async def test_is_button_visible_with_condition():
    plugin, state = _make_plugin()
    state.set("device.proj.power", "off", source="test")
    btn = {"index": 1, "bindings": {"visible_when": {"key": "device.proj.power", "operator": "eq", "value": "on"}}}
    assert await plugin._is_button_visible(btn) is False
    state.set("device.proj.power", "on", source="test")
    assert await plugin._is_button_visible(btn) is True


# ──── _evaluate_auto_page ────


@pytest.mark.asyncio
async def test_auto_page_first_match_wins():
    config = {
        "auto_page": [
            {"page": 1, "when": {"key": "device.proj.power", "operator": "eq", "value": "on"}},
            {"page": 0, "when": {"key": "device.proj.power", "operator": "ne", "value": "on"}},
        ]
    }
    plugin, state = _make_plugin(config)

    state.set("device.proj.power", "on", source="test")
    assert await plugin._evaluate_auto_page() == 1

    state.set("device.proj.power", "off", source="test")
    assert await plugin._evaluate_auto_page() == 0


@pytest.mark.asyncio
async def test_auto_page_order_matters():
    # Both rules match; the first one in the array wins.
    config = {
        "auto_page": [
            {"page": 2, "when": {"key": "var.x", "operator": "truthy"}},
            {"page": 3, "when": {"key": "var.x", "operator": "truthy"}},
        ]
    }
    plugin, state = _make_plugin(config)
    state.set("var.x", "1", source="test")
    assert await plugin._evaluate_auto_page() == 2


@pytest.mark.asyncio
async def test_auto_page_no_match_returns_none():
    config = {"auto_page": [{"page": 1, "when": {"key": "var.x", "operator": "eq", "value": "yes"}}]}
    plugin, state = _make_plugin(config)
    state.set("var.x", "no", source="test")
    assert await plugin._evaluate_auto_page() is None


@pytest.mark.asyncio
async def test_auto_page_clamps_out_of_range_page():
    # max_pages defaults to 10; a rule asking for page 99 clamps to 9.
    config = {"auto_page": [{"page": 99, "when": {"key": "var.x", "operator": "truthy"}}]}
    plugin, state = _make_plugin(config)
    state.set("var.x", "1", source="test")
    assert await plugin._evaluate_auto_page() == 9


@pytest.mark.asyncio
async def test_auto_page_absent_or_malformed():
    plugin, _ = _make_plugin({})
    assert await plugin._evaluate_auto_page() is None

    plugin2, state = _make_plugin({"auto_page": [{"page": 1}, {"when": {"key": "a"}}, "junk"]})
    state.set("a", "1", source="test")
    # Entries missing page or when are skipped; nothing valid matches.
    assert await plugin2._evaluate_auto_page() is None


# ──── _setup_feedback_subscriptions key collection ────


@pytest.mark.asyncio
async def test_subscriptions_collect_all_key_sources():
    config = {
        "buttons": [
            {
                "index": 0,
                "page": 0,
                "bindings": {
                    "feedback": {"key": "device.a.power"},
                    "press": [{"action": "macro", "macro": "m", "mode": "toggle", "toggle_key": "device.a.mute"}],
                    "visible_when": {"key": "device.a.online", "operator": "truthy"},
                },
            },
            {"index": 1, "page": 0, "bindings": {"feedback": {"key": "var.status"}}},
        ],
        "auto_page": [
            {"page": 1, "when": {"any": [{"key": "device.b.power"}, {"key": "device.c.power"}]}},
        ],
    }
    plugin, _ = _make_plugin(config)
    await plugin._setup_feedback_subscriptions()

    # Auto-page keys tracked separately
    assert plugin._auto_page_keys == {"device.b.power", "device.c.power"}

    # One subscription per unique watched key:
    # feedback, toggle_key, visible_when, second feedback, 2 auto_page keys = 6
    assert len(plugin._feedback_subs) == 6


@pytest.mark.asyncio
async def test_subscriptions_empty_config():
    plugin, _ = _make_plugin({})
    await plugin._setup_feedback_subscriptions()
    assert plugin._auto_page_keys == set()
    assert plugin._feedback_subs == []


# ──── _press_actions normalization ────


def test_press_actions_list():
    assert StreamDeckPlugin._press_actions(
        {"press": [{"action": "macro", "macro": "a"}, {"action": "macro", "macro": "b"}]}
    ) == [{"action": "macro", "macro": "a"}, {"action": "macro", "macro": "b"}]


def test_press_actions_single_dict_wrapped():
    assert StreamDeckPlugin._press_actions({"press": {"action": "macro", "macro": "a"}}) == [
        {"action": "macro", "macro": "a"}
    ]


def test_press_actions_absent_or_invalid():
    assert StreamDeckPlugin._press_actions({}) == []
    assert StreamDeckPlugin._press_actions({"press": None}) == []
    assert StreamDeckPlugin._press_actions("not a dict") == []
    # Non-dict entries inside the array are dropped.
    assert StreamDeckPlugin._press_actions({"press": ["x", {"action": "macro", "macro": "a"}]}) == [
        {"action": "macro", "macro": "a"}
    ]


# ──── Press execution: tap fires every action, in order ────


@pytest.mark.asyncio
async def test_tap_fires_all_actions_in_order():
    config = {"buttons": [{"index": 0, "page": 0, "bindings": {"press": [
        {"action": "macro", "macro": "first"},
        {"action": "macro", "macro": "second"},
    ]}}]}
    plugin, _state, macros, _devices = _make_plugin_with_recorders(config)
    await plugin._on_key_change(None, 0, True)
    assert macros.executed == ["first", "second"]


@pytest.mark.asyncio
async def test_tap_fires_only_on_press_not_release():
    config = {"buttons": [{"index": 0, "page": 0, "bindings": {"press": [
        {"action": "macro", "macro": "m"},
    ]}}]}
    plugin, _state, macros, _devices = _make_plugin_with_recorders(config)
    await plugin._on_key_change(None, 0, False)  # release
    assert macros.executed == []
    await plugin._on_key_change(None, 0, True)   # press
    assert macros.executed == ["m"]


@pytest.mark.asyncio
async def test_hidden_button_fires_nothing():
    config = {"buttons": [{"index": 0, "page": 0, "bindings": {
        "press": [{"action": "macro", "macro": "m"}],
        "visible_when": {"key": "device.p.power", "operator": "eq", "value": "on"},
    }}]}
    plugin, state, macros, _devices = _make_plugin_with_recorders(config)
    state.set("device.p.power", "off", source="test")
    await plugin._on_key_change(None, 0, True)
    assert macros.executed == []
    # Visible again -> fires normally.
    state.set("device.p.power", "on", source="test")
    await plugin._on_key_change(None, 0, True)
    assert macros.executed == ["m"]


@pytest.mark.asyncio
async def test_toggle_fires_off_action_when_active_else_primary():
    config = {"buttons": [{"index": 0, "page": 0, "bindings": {"press": [
        {"action": "macro", "macro": "turn_on", "mode": "toggle",
         "toggle_key": "device.p.power", "toggle_value": "on",
         "off_action": {"action": "macro", "macro": "turn_off"}},
        # An extra action is ignored in toggle mode (toggle uses the first entry).
        {"action": "macro", "macro": "extra"},
    ]}}]}
    plugin, state, macros, _devices = _make_plugin_with_recorders(config)
    state.set("device.p.power", "on", source="test")
    await plugin._on_key_change(None, 0, True)
    assert macros.executed == ["turn_off"]
    state.set("device.p.power", "off", source="test")
    await plugin._on_key_change(None, 0, True)
    assert macros.executed == ["turn_off", "turn_on"]


@pytest.mark.asyncio
async def test_toggle_ignores_release():
    config = {"buttons": [{"index": 0, "page": 0, "bindings": {"press": [
        {"action": "macro", "macro": "on", "mode": "toggle",
         "toggle_key": "var.x", "toggle_value": "1",
         "off_action": {"action": "macro", "macro": "off"}},
    ]}}]}
    plugin, _state, macros, _devices = _make_plugin_with_recorders(config)
    await plugin._on_key_change(None, 0, False)
    assert macros.executed == []


# ──── state.set scope (mirrors the panel plugin bridge) ────


@pytest.mark.asyncio
async def test_state_set_writes_own_plugin_namespace():
    plugin, state, _m, _d = _make_plugin_with_recorders()
    await plugin._execute_action(
        {"action": "state.set", "key": "plugin.streamdeck.mode", "value": "show"}, 0)
    assert state.get("plugin.streamdeck.mode") == "show"


@pytest.mark.asyncio
async def test_state_set_writes_var_via_variable_set():
    plugin, state, _m, _d = _make_plugin_with_recorders()
    await plugin._execute_action(
        {"action": "state.set", "key": "var.volume", "value": 42}, 0)
    assert state.get("var.volume") == 42


@pytest.mark.asyncio
async def test_state_set_drops_foreign_device_key_with_warning():
    plugin, state, _m, _d = _make_plugin_with_recorders()
    await plugin._execute_action(
        {"action": "state.set", "key": "device.proj.power", "value": "on"}, 0)
    # The confused-deputy write is dropped, not applied.
    assert state.get("device.proj.power") is None
    assert any(e["level"] == "warning" for e in plugin._test_logs)


@pytest.mark.asyncio
async def test_state_set_drops_other_plugin_namespace():
    plugin, state, _m, _d = _make_plugin_with_recorders()
    await plugin._execute_action(
        {"action": "state.set", "key": "plugin.other.x", "value": "y"}, 0)
    assert state.get("plugin.other.x") is None


@pytest.mark.asyncio
async def test_tap_runs_state_set_alongside_macro():
    config = {"buttons": [{"index": 0, "page": 0, "bindings": {"press": [
        {"action": "state.set", "key": "var.scene", "value": "movie"},
        {"action": "macro", "macro": "dim_lights"},
    ]}}]}
    plugin, state, macros, _d = _make_plugin_with_recorders(config)
    await plugin._on_key_change(None, 0, True)
    assert state.get("var.scene") == "movie"
    assert macros.executed == ["dim_lights"]


# ──── navigate (deck pages) ────


@pytest.mark.asyncio
async def test_navigate_next_and_prev_page():
    plugin, state, _m, _d = _make_plugin_with_recorders({})
    await plugin._execute_action({"action": "navigate", "page": "__next_page__"}, 0)
    assert plugin.current_page == 1
    assert state.get("plugin.streamdeck.current_page") == 1
    await plugin._execute_action({"action": "navigate", "page": "__prev_page__"}, 0)
    assert plugin.current_page == 0


@pytest.mark.asyncio
async def test_navigate_to_page_index():
    plugin, state, _m, _d = _make_plugin_with_recorders({})
    await plugin._execute_action({"action": "navigate", "page": "2"}, 0)
    assert plugin.current_page == 2
    assert state.get("plugin.streamdeck.current_page") == 2
    # A non-numeric, non-special target is ignored (no crash, no move).
    await plugin._execute_action({"action": "navigate", "page": "garbage"}, 0)
    assert plugin.current_page == 2


# ──── Watchdog: connect / unplug recovery / late connect ────
#
# A non-visual fake deck (is_visual() False, like a Pedal) lets us exercise
# the open/teardown path without PIL or the StreamDeck library: rendering is
# skipped for non-visual decks, so no image code runs.


class _FakeDeck:
    def __init__(self, serial="ABC123"):
        self._open = False
        self._connected = True
        self.serial = serial
        self.closed = False
        self.brightness = None
        self.key_cb = None

    def open(self):
        self._open = True

    def reset(self):
        pass

    def close(self):
        self._open = False
        self.closed = True

    def is_open(self):
        return self._open

    def connected(self):
        return self._connected

    def is_visual(self):
        return False  # skip rendering -> no PIL needed

    def deck_type(self):
        return "Stream Deck Pedal"

    def get_serial_number(self):
        return self.serial

    def key_count(self):
        return 3

    def key_layout(self):
        return (1, 3)

    def dial_count(self):
        return 0

    def touch_key_count(self):
        return 0

    def is_touch(self):
        return False

    def set_brightness(self, b):
        self.brightness = b

    def set_key_callback_async(self, cb, loop=None):
        self.key_cb = cb


class _FakeManager:
    def __init__(self, decks):
        self._decks = decks

    def enumerate(self):
        return list(self._decks)


class _FakeStreamDeck:
    """Stand-in for the StreamDeck module's DeviceManager factory."""

    def __init__(self, decks):
        self._decks = decks

    def DeviceManager(self):
        return _FakeManager(self._decks)


@pytest.mark.asyncio
async def test_watchdog_opens_deck_when_present(monkeypatch):
    plugin, state, _m, _d = _make_plugin_with_recorders({})
    deck = _FakeDeck()
    monkeypatch.setattr(sd_module, "StreamDeck", _FakeStreamDeck([deck]))
    await plugin._watchdog()
    assert plugin.deck is deck
    assert deck.is_open()
    assert state.get("plugin.streamdeck.connected") is True
    assert state.get("plugin.streamdeck.model") == "Stream Deck Pedal"


@pytest.mark.asyncio
async def test_watchdog_recovers_after_unplug(monkeypatch):
    plugin, state, _m, _d = _make_plugin_with_recorders({})
    deck = _FakeDeck()
    fake_sd = _FakeStreamDeck([deck])
    monkeypatch.setattr(sd_module, "StreamDeck", fake_sd)

    await plugin._watchdog()
    assert plugin.deck is deck

    # Unplug: the library closes the deck object and it stops enumerating.
    deck._open = False
    deck._connected = False
    fake_sd._decks = []
    await plugin._watchdog()
    assert plugin.deck is None
    assert state.get("plugin.streamdeck.connected") is False
    assert deck.closed is True

    # A deck reappears -> the same watchdog re-opens it.
    deck2 = _FakeDeck(serial="XYZ789")
    fake_sd._decks = [deck2]
    await plugin._watchdog()
    assert plugin.deck is deck2
    assert state.get("plugin.streamdeck.connected") is True
    assert state.get("plugin.streamdeck.serial") == "XYZ789"


# ──── Hardware detection: geometry published to state ────


@pytest.mark.asyncio
async def test_open_deck_publishes_detected_geometry(monkeypatch):
    plugin, state, _m, _d = _make_plugin_with_recorders({})
    monkeypatch.setattr(sd_module, "StreamDeck", _FakeStreamDeck([_FakeDeck()]))
    await plugin._watchdog()
    assert state.get("plugin.streamdeck.rows") == 1
    assert state.get("plugin.streamdeck.columns") == 3
    assert state.get("plugin.streamdeck.key_count") == 3
    assert state.get("plugin.streamdeck.dial_count") == 0
    assert state.get("plugin.streamdeck.touch_key_count") == 0
    assert state.get("plugin.streamdeck.has_touchscreen") is False


class _FakePlusDeck(_FakeDeck):
    """Plus-shaped fake: 4x2 LCD keys, 4 dials, a touchscreen.

    is_visual() stays False (from _FakeDeck) so render paths skip PIL;
    the strip-render test uses _VisualPlusDeck below.
    """

    def __init__(self, serial="PLUS01"):
        super().__init__(serial)
        self.dial_cb = None
        self.touch_cb = None

    def deck_type(self):
        return "Stream Deck +"

    def key_count(self):
        return 8

    def key_layout(self):
        return (2, 4)

    def dial_count(self):
        return 4

    def is_touch(self):
        return True

    def touchscreen_image_format(self):
        return {"size": (800, 100), "format": "JPEG"}

    def set_dial_callback_async(self, cb, loop=None):
        self.dial_cb = cb

    def set_touchscreen_callback_async(self, cb, loop=None):
        self.touch_cb = cb


@pytest.mark.asyncio
async def test_open_deck_publishes_dials_and_touchscreen(monkeypatch):
    plugin, state, _m, _d = _make_plugin_with_recorders({})
    monkeypatch.setattr(sd_module, "StreamDeck", _FakeStreamDeck([_FakePlusDeck()]))
    await plugin._watchdog()
    assert state.get("plugin.streamdeck.model") == "Stream Deck +"
    assert state.get("plugin.streamdeck.rows") == 2
    assert state.get("plugin.streamdeck.columns") == 4
    assert state.get("plugin.streamdeck.dial_count") == 4
    assert state.get("plugin.streamdeck.has_touchscreen") is True


@pytest.mark.asyncio
async def test_open_deck_wires_dial_and_touch_callbacks(monkeypatch):
    plugin, _state, _m, _d = _make_plugin_with_recorders({})
    plus = _FakePlusDeck()
    basic = _FakeDeck()
    monkeypatch.setattr(sd_module, "StreamDeck", _FakeStreamDeck([plus]))
    await plugin._watchdog()
    assert plus.dial_cb is not None
    assert plus.touch_cb is not None
    # A deck without dials/touch never gets those callbacks registered
    # (the basic fake has no setter methods, so wiring it would raise).
    plugin2, _s2, _m2, _d2 = _make_plugin_with_recorders({})
    monkeypatch.setattr(sd_module, "StreamDeck", _FakeStreamDeck([basic]))
    await plugin2._watchdog()
    assert plugin2.deck is basic


# ──── Dials: turn / push routing, adjust clamping ────


class _Evt:
    """Stand-in for the library's DialEventType/TouchscreenEventType members
    (the plugin matches events by their .name, never by enum identity)."""

    def __init__(self, name):
        self.name = name


TURN = _Evt("TURN")
PUSH = _Evt("PUSH")
TOUCH_SHORT = _Evt("SHORT")
TOUCH_DRAG = _Evt("DRAG")


@pytest.mark.asyncio
async def test_dial_turn_routes_cw_and_ccw():
    config = {"dials": [{
        "index": 0,
        "cw": [{"action": "macro", "macro": "vol_up"}],
        "ccw": [{"action": "macro", "macro": "vol_down"}],
    }]}
    plugin, _state, macros, _d = _make_plugin_with_recorders(config)
    await plugin._on_dial_event(None, 0, TURN, 1)
    assert macros.executed == ["vol_up"]
    await plugin._on_dial_event(None, 0, TURN, -2)
    assert macros.executed == ["vol_up", "vol_down"]


@pytest.mark.asyncio
async def test_dial_push_fires_press_only_on_press():
    config = {"dials": [{
        "index": 1,
        "press": [{"action": "macro", "macro": "mute"}],
    }]}
    plugin, _state, macros, _d = _make_plugin_with_recorders(config)
    await plugin._on_dial_event(None, 1, PUSH, False)  # release
    assert macros.executed == []
    await plugin._on_dial_event(None, 1, PUSH, True)
    assert macros.executed == ["mute"]


@pytest.mark.asyncio
async def test_dial_unconfigured_or_zero_turn_is_ignored():
    plugin, _state, macros, _d = _make_plugin_with_recorders({})
    await plugin._on_dial_event(None, 0, TURN, 3)
    await plugin._on_dial_event(None, 0, PUSH, True)
    await plugin._on_dial_event(None, 0, TURN, 0)
    assert macros.executed == []


@pytest.mark.asyncio
async def test_dial_adjust_steps_and_clamps():
    config = {"dials": [{
        "index": 0,
        "adjust": {"key": "var.volume", "step": 2, "min": 0, "max": 10},
    }]}
    plugin, state, _m, _d = _make_plugin_with_recorders(config)
    state.set("var.volume", 8, source="test")

    await plugin._on_dial_event(None, 0, TURN, 1)
    assert state.get("var.volume") == 10

    # Already at max — clamped
    await plugin._on_dial_event(None, 0, TURN, 3)
    assert state.get("var.volume") == 10

    await plugin._on_dial_event(None, 0, TURN, -1)
    assert state.get("var.volume") == 8

    # Big CCW spin clamps at min
    await plugin._on_dial_event(None, 0, TURN, -50)
    assert state.get("var.volume") == 0


@pytest.mark.asyncio
async def test_dial_adjust_turn_magnitude_scales_step():
    config = {"dials": [{
        "index": 0,
        "adjust": {"key": "var.level", "step": 1},
    }]}
    plugin, state, _m, _d = _make_plugin_with_recorders(config)
    state.set("var.level", 0, source="test")
    # 5 detents in one event (fast spin) moves 5 steps.
    await plugin._on_dial_event(None, 0, TURN, 5)
    assert state.get("var.level") == 5


@pytest.mark.asyncio
async def test_dial_adjust_unset_value_starts_at_min():
    config = {"dials": [{
        "index": 0,
        "adjust": {"key": "var.fresh", "step": 1, "min": 20, "max": 30},
    }]}
    plugin, state, _m, _d = _make_plugin_with_recorders(config)
    await plugin._on_dial_event(None, 0, TURN, 1)
    assert state.get("var.fresh") == 21


@pytest.mark.asyncio
async def test_dial_adjust_respects_state_scope_rule():
    config = {"dials": [{
        "index": 0,
        "adjust": {"key": "device.proj.volume", "step": 1},
    }]}
    plugin, state, _m, _d = _make_plugin_with_recorders(config)
    await plugin._on_dial_event(None, 0, TURN, 1)
    # Foreign namespace write is dropped with a warning, like state.set.
    assert state.get("device.proj.volume") is None
    assert any(e["level"] == "warning" for e in plugin._test_logs)


@pytest.mark.asyncio
async def test_dial_adjust_float_step():
    config = {"dials": [{
        "index": 0,
        "adjust": {"key": "var.gain", "step": 0.5},
    }]}
    plugin, state, _m, _d = _make_plugin_with_recorders(config)
    state.set("var.gain", 0, source="test")
    await plugin._on_dial_event(None, 0, TURN, 1)
    assert state.get("var.gain") == 0.5
    await plugin._on_dial_event(None, 0, TURN, 1)
    # Whole numbers come back as ints so state stays clean.
    assert state.get("var.gain") == 1


# ──── Touchscreen: zones, hit-testing, touch routing ────


@pytest.mark.asyncio
async def test_default_zones_follow_dials():
    config = {"dials": [
        {"index": 0, "label": "Volume", "adjust": {"key": "var.volume"}},
        {"index": 2, "label": "Mics", "adjust": {"key": "var.mic_gain"}},
    ]}
    plugin, _state, _m, _d = _make_plugin_with_recorders(config)
    plugin.deck = _FakePlusDeck()
    zones = plugin._touch_zones()
    # One zone per dial (the Plus has 4), evenly split across 800px.
    assert len(zones) == 4
    assert [z["x"] for z in zones] == [0, 200, 400, 600]
    assert all(z["w"] == 200 for z in zones)
    assert zones[0]["label"] == "Volume"
    assert zones[0]["value_source"] == "var.volume"
    assert zones[2]["label"] == "Mics"
    assert zones[1]["label"] == ""  # unconfigured dial -> empty zone


@pytest.mark.asyncio
async def test_explicit_zones_override_and_even_split():
    config = {"touchscreen": {"zones": [
        {"label": "A"}, {"label": "B"}, {"label": "C"},
    ]}}
    plugin, _state, _m, _d = _make_plugin_with_recorders(config)
    plugin.deck = _FakePlusDeck()
    zones = plugin._touch_zones()
    assert len(zones) == 3
    assert [z["x"] for z in zones] == [0, 266, 532]
    # Explicit pixel bounds win over the even split.
    config2 = {"touchscreen": {"zones": [
        {"label": "Wide", "x": 0, "w": 600}, {"label": "Narrow", "x": 600, "w": 200},
    ]}}
    plugin2, _s2, _m2, _d2 = _make_plugin_with_recorders(config2)
    plugin2.deck = _FakePlusDeck()
    zones2 = plugin2._touch_zones()
    assert zones2[0]["w"] == 600
    assert zones2[1]["x"] == 600


@pytest.mark.asyncio
async def test_zone_hit_testing():
    config = {"touchscreen": {"zones": [
        {"label": "A"}, {"label": "B"}, {"label": "C"}, {"label": "D"},
    ]}}
    plugin, _state, _m, _d = _make_plugin_with_recorders(config)
    plugin.deck = _FakePlusDeck()
    assert plugin._zone_at(0)["label"] == "A"
    assert plugin._zone_at(199)["label"] == "A"
    assert plugin._zone_at(200)["label"] == "B"
    assert plugin._zone_at(750)["label"] == "D"
    assert plugin._zone_at(9999) is None


@pytest.mark.asyncio
async def test_touch_event_runs_zone_actions():
    config = {"touchscreen": {"zones": [
        {"label": "A", "touch": [{"action": "macro", "macro": "zone_a"}]},
        {"label": "B", "touch": [{"action": "macro", "macro": "zone_b"}]},
    ]}}
    plugin, _state, macros, _d = _make_plugin_with_recorders(config)
    plugin.deck = _FakePlusDeck()
    await plugin._on_touchscreen_event(None, TOUCH_SHORT, {"x": 500, "y": 50})
    assert macros.executed == ["zone_b"]
    # Drag events and malformed payloads are ignored.
    await plugin._on_touchscreen_event(None, TOUCH_DRAG, {"x": 100, "y": 50})
    await plugin._on_touchscreen_event(None, TOUCH_SHORT, "junk")
    assert macros.executed == ["zone_b"]


@pytest.mark.asyncio
async def test_subscriptions_include_touch_strip_keys():
    config = {
        "dials": [{"index": 0, "adjust": {"key": "var.volume"}}],
        "touchscreen": {"zones": [
            {"label_source": "var.zone_label", "value_source": "var.mic_gain"},
        ]},
    }
    plugin, _state = _make_plugin(config)
    await plugin._setup_feedback_subscriptions()
    assert plugin._touch_strip_keys == {"var.volume", "var.zone_label", "var.mic_gain"}
    assert len(plugin._feedback_subs) == 3


@pytest.mark.asyncio
async def test_render_touchscreen_draws_zones(monkeypatch):
    img_mod = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw, ImageFont
    monkeypatch.setattr(sd_module, "Image", img_mod)
    monkeypatch.setattr(sd_module, "ImageDraw", ImageDraw)
    monkeypatch.setattr(sd_module, "ImageFont", ImageFont)

    sent = {}

    class _FakePILHelper:
        @staticmethod
        def to_native_touchscreen_format(deck, img):
            sent["size"] = img.size
            return b"native-strip"

    monkeypatch.setattr(sd_module, "PILHelper", _FakePILHelper)

    class _VisualPlusDeck(_FakePlusDeck):
        def is_visual(self):
            return True

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def set_touchscreen_image(self, image, x=0, y=0, w=0, h=0):
            sent["image"] = image
            sent["rect"] = (x, y, w, h)

    config = {"dials": [{"index": 0, "label": "Volume", "adjust": {"key": "var.volume"}}]}
    plugin, state, _m, _d = _make_plugin_with_recorders(config)
    plugin._load_text_font()
    state.set("var.volume", 42, source="test")
    plugin.deck = _VisualPlusDeck()

    await plugin._render_touchscreen()
    assert sent["image"] == b"native-strip"
    assert sent["size"] == (800, 100)
    assert sent["rect"] == (0, 0, 800, 100)


@pytest.mark.asyncio
async def test_watchdog_no_deck_stays_disconnected(monkeypatch):
    plugin, state, _m, _d = _make_plugin_with_recorders({})
    monkeypatch.setattr(sd_module, "StreamDeck", _FakeStreamDeck([]))
    await plugin._watchdog()
    assert plugin.deck is None
    assert state.get("plugin.streamdeck.connected") in (None, False)


@pytest.mark.asyncio
async def test_watchdog_skips_while_opening(monkeypatch):
    # The re-entrancy guard prevents a second open while one is in progress.
    plugin, _state, _m, _d = _make_plugin_with_recorders({})
    monkeypatch.setattr(sd_module, "StreamDeck", _FakeStreamDeck([_FakeDeck()]))
    plugin._opening = True
    await plugin._watchdog()
    assert plugin.deck is None  # guard short-circuited the open


# ──── Neo: touch keys (color-only) + info strip ────


class _FakeNeoDeck(_FakeDeck):
    """Neo-shaped fake: 8 LCD keys (2x4), 2 color-only touch keys, an info
    screen. is_visual() is True; image/color setters are recorders. Touch-key
    rendering goes through set_key_color, which needs no PIL."""

    def __init__(self, serial="NEO01"):
        super().__init__(serial)
        self.key_images = {}
        self.key_colors = {}
        self.screen_image = None

    def deck_type(self):
        return "Stream Deck Neo"

    def key_count(self):
        return 8

    def key_layout(self):
        return (2, 4)

    def touch_key_count(self):
        return 2

    def is_visual(self):
        return True

    def screen_image_format(self):
        return {"size": (248, 58), "format": "JPEG"}

    def key_image_format(self):
        return {"size": (96, 96), "format": "JPEG"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_key_image(self, key, image):
        self.key_images[key] = image

    def set_key_color(self, key, r, g, b):
        self.key_colors[key] = (r, g, b)

    def set_screen_image(self, image):
        self.screen_image = image


def test_hex_color_parsing():
    assert StreamDeckPlugin._hex_to_rgb("#ff0000") == (255, 0, 0)
    assert StreamDeckPlugin._hex_to_rgb("00ff00") == (0, 255, 0)
    assert StreamDeckPlugin._hex_to_rgb("#0f0") == (0, 255, 0)
    assert StreamDeckPlugin._hex_to_rgb("junk") == (0, 0, 0)
    assert StreamDeckPlugin._hex_to_rgb(None) == (0, 0, 0)
    assert StreamDeckPlugin._lighten_hex("#000000", 0.25) == "#3f3f3f"


@pytest.mark.asyncio
async def test_touch_key_renders_color_not_image():
    config = {"buttons": [{"index": 8, "page": 0, "bg_color": "#ff0000",
                           "bindings": {"press": [{"action": "macro", "macro": "m"}]}}]}
    plugin, _state, _m, _d = _make_plugin_with_recorders(config)
    plugin.deck = _FakeNeoDeck()
    await plugin._render_button(8)
    assert plugin.deck.key_colors[8] == (255, 0, 0)
    assert 8 not in plugin.deck.key_images


@pytest.mark.asyncio
async def test_touch_key_unassigned_goes_dark():
    plugin, _state, _m, _d = _make_plugin_with_recorders({})
    plugin.deck = _FakeNeoDeck()
    await plugin._render_button(9)
    assert plugin.deck.key_colors[9] == (0, 0, 0)


@pytest.mark.asyncio
async def test_touch_key_hidden_goes_dark():
    config = {"buttons": [{"index": 8, "page": 0, "bg_color": "#ff0000", "bindings": {
        "visible_when": {"key": "var.show", "operator": "truthy"},
    }}]}
    plugin, state, _m, _d = _make_plugin_with_recorders(config)
    plugin.deck = _FakeNeoDeck()
    state.set("var.show", "", source="test")
    await plugin._render_button(8)
    assert plugin.deck.key_colors[8] == (0, 0, 0)


@pytest.mark.asyncio
async def test_touch_key_feedback_drives_color():
    config = {"buttons": [{"index": 9, "page": 0, "bg_color": "#111111", "bindings": {
        "feedback": {
            "key": "var.mute",
            "condition": {"equals": "1"},
            "style_active": {"bg_color": "#00ff00"},
        },
    }}]}
    plugin, state, _m, _d = _make_plugin_with_recorders(config)
    plugin.deck = _FakeNeoDeck()
    state.set("var.mute", "1", source="test")
    await plugin._render_button(9)
    assert plugin.deck.key_colors[9] == (0, 255, 0)
    state.set("var.mute", "0", source="test")
    await plugin._render_button(9)
    assert plugin.deck.key_colors[9] == (17, 17, 17)


@pytest.mark.asyncio
async def test_touch_key_press_fires_actions_and_highlights():
    config = {"buttons": [{"index": 8, "page": 0, "bg_color": "#000000",
                           "bindings": {"press": [{"action": "macro", "macro": "m"}]}}]}
    plugin, _state, macros, _d = _make_plugin_with_recorders(config)
    plugin.deck = _FakeNeoDeck()
    await plugin._on_key_change(None, 8, True)
    assert macros.executed == ["m"]
    # While held, the key color is brightened toward white.
    assert plugin.deck.key_colors[8] == (63, 63, 63)
    await plugin._on_key_change(None, 8, False)
    assert plugin.deck.key_colors[8] == (0, 0, 0)


@pytest.mark.asyncio
async def test_open_deck_publishes_info_screen_and_touch_keys(monkeypatch):
    class _NonVisualNeo(_FakeNeoDeck):
        def is_visual(self):
            return False  # skip rendering -> no PIL needed

    plugin, state, _m, _d = _make_plugin_with_recorders({})
    monkeypatch.setattr(sd_module, "StreamDeck", _FakeStreamDeck([_NonVisualNeo()]))
    await plugin._watchdog()
    assert state.get("plugin.streamdeck.touch_key_count") == 2
    assert state.get("plugin.streamdeck.has_info_screen") is True


@pytest.mark.asyncio
async def test_render_all_buttons_covers_touch_keys(monkeypatch):
    img_mod = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw, ImageFont
    monkeypatch.setattr(sd_module, "Image", img_mod)
    monkeypatch.setattr(sd_module, "ImageDraw", ImageDraw)
    monkeypatch.setattr(sd_module, "ImageFont", ImageFont)

    class _FakePILHelper:
        @staticmethod
        def to_native_key_format(deck, img):
            return b"native-key"

    monkeypatch.setattr(sd_module, "PILHelper", _FakePILHelper)

    plugin, _state, _m, _d = _make_plugin_with_recorders({})
    plugin._load_text_font()
    plugin.deck = _FakeNeoDeck()
    await plugin._render_all_buttons()
    # 8 LCD keys get images, 2 touch keys get colors.
    assert sorted(plugin.deck.key_images) == list(range(8))
    assert sorted(plugin.deck.key_colors) == [8, 9]


@pytest.mark.asyncio
async def test_info_strip_renders_state_value(monkeypatch):
    img_mod = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw, ImageFont
    monkeypatch.setattr(sd_module, "Image", img_mod)
    monkeypatch.setattr(sd_module, "ImageDraw", ImageDraw)
    monkeypatch.setattr(sd_module, "ImageFont", ImageFont)

    sent = {}

    class _FakePILHelper:
        @staticmethod
        def to_native_screen_format(deck, img):
            sent["size"] = img.size
            return b"native-screen"

    monkeypatch.setattr(sd_module, "PILHelper", _FakePILHelper)

    config = {"info_strip": {"source": "state", "key": "var.temp", "label": "Temp"}}
    plugin, state, _m, _d = _make_plugin_with_recorders(config)
    plugin._load_text_font()
    state.set("var.temp", 72, source="test")
    plugin.deck = _FakeNeoDeck()

    await plugin._render_info_strip()
    assert plugin.deck.screen_image == b"native-screen"
    assert sent["size"] == (248, 58)


@pytest.mark.asyncio
async def test_info_strip_skipped_on_deck_without_screen(monkeypatch):
    img_mod = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(sd_module, "Image", img_mod)

    class _NoScreenDeck(_FakeNeoDeck):
        def screen_image_format(self):
            return {"size": (0, 0), "format": ""}

    plugin, _state, _m, _d = _make_plugin_with_recorders(
        {"info_strip": {"source": "text", "text": "hi"}}
    )
    plugin.deck = _NoScreenDeck()
    await plugin._render_info_strip()
    assert plugin.deck.screen_image is None


@pytest.mark.asyncio
async def test_info_strip_state_key_is_watched():
    config = {"info_strip": {"source": "state", "key": "var.temp"}}
    plugin, _state = _make_plugin(config)
    await plugin._setup_feedback_subscriptions()
    assert plugin._info_strip_keys == {"var.temp"}
    assert len(plugin._feedback_subs) == 1

    # A static-text strip watches nothing.
    plugin2, _s2 = _make_plugin({"info_strip": {"source": "text", "text": "Room A"}})
    await plugin2._setup_feedback_subscriptions()
    assert plugin2._info_strip_keys == set()
    assert plugin2._feedback_subs == []


# ──── Brightness: auto rules + idle dim ────


@pytest.mark.asyncio
async def test_brightness_rule_first_match_wins():
    config = {
        "brightness": 70,
        "auto_brightness": [
            {"level": 20, "when": {"key": "var.night", "operator": "truthy"}},
            {"level": 90, "when": {"key": "var.night", "operator": "falsy"}},
        ],
    }
    plugin, state = _make_plugin(config)
    state.set("var.night", "1", source="test")
    assert await plugin._current_brightness_level() == 20
    state.set("var.night", "", source="test")
    assert await plugin._current_brightness_level() == 90


@pytest.mark.asyncio
async def test_brightness_no_match_uses_base_and_clamps():
    config = {"brightness": 70, "auto_brightness": [
        {"level": 150, "when": {"key": "var.x", "operator": "truthy"}}]}
    plugin, state = _make_plugin(config)
    # No rule matches -> base brightness.
    assert await plugin._current_brightness_level() == 70
    # Match -> level clamped to 100.
    state.set("var.x", "1", source="test")
    assert await plugin._current_brightness_level() == 100
    # Malformed rules are skipped (no when / not a dict).
    plugin2, _ = _make_plugin({"auto_brightness": ["junk", {"level": 5}], "brightness": 40})
    assert await plugin2._current_brightness_level() == 40


@pytest.mark.asyncio
async def test_brightness_rule_keys_watched_and_applied():
    config = {"brightness": 70, "auto_brightness": [
        {"level": 25, "when": {"key": "var.movie", "operator": "truthy"}}]}
    plugin, state, _m, _d = _make_plugin_with_recorders(config)
    plugin.deck = _FakeNeoDeck()
    await plugin._setup_feedback_subscriptions()
    assert plugin._brightness_keys == {"var.movie"}

    state.set("var.movie", "1", source="test")
    await plugin._on_state_change("var.movie", "1", None)
    assert plugin.deck.brightness == 25

    state.set("var.movie", "", source="test")
    await plugin._on_state_change("var.movie", "", "1")
    assert plugin.deck.brightness == 70


@pytest.mark.asyncio
async def test_idle_dim_after_timeout_and_wake_on_input():
    import asyncio as _a
    config = {"brightness": 70, "idle_dim": {"after_seconds": 10, "level": 5}}
    plugin, _state, _m, _d = _make_plugin_with_recorders(config)
    deck = _FakeNeoDeck()
    plugin.deck = deck
    now = _a.get_event_loop().time()
    plugin._last_input = now - 11

    await plugin._check_idle_dim()
    assert plugin._idle_dimmed is True
    assert deck.brightness == 5

    # Any input wakes the deck, restores brightness, resets the timer.
    await plugin._on_key_change(None, 0, True)
    assert plugin._idle_dimmed is False
    assert deck.brightness == 70
    assert plugin._last_input >= now


@pytest.mark.asyncio
async def test_idle_dim_waits_for_timeout():
    import asyncio as _a
    config = {"idle_dim": {"after_seconds": 1000, "level": 5}}
    plugin, _state, _m, _d = _make_plugin_with_recorders(config)
    deck = _FakeNeoDeck()
    plugin.deck = deck
    plugin._last_input = _a.get_event_loop().time()
    await plugin._check_idle_dim()
    assert plugin._idle_dimmed is False
    assert deck.brightness is None  # untouched


@pytest.mark.asyncio
async def test_watchdog_tick_drives_idle_dim(monkeypatch):
    config = {"idle_dim": {"after_seconds": 10, "level": 5}}
    plugin, _state, _m, _d = _make_plugin_with_recorders(config)
    deck = _FakeDeck()
    monkeypatch.setattr(sd_module, "StreamDeck", _FakeStreamDeck([deck]))
    await plugin._watchdog()  # opens the deck, applies base brightness
    assert deck.brightness == 70
    plugin._last_input -= 11
    await plugin._watchdog()  # healthy tick doubles as the idle clock
    assert plugin._idle_dimmed is True
    assert deck.brightness == 5


@pytest.mark.asyncio
async def test_dial_input_wakes_idle_dimmed_deck():
    config = {"brightness": 60, "idle_dim": {"after_seconds": 10, "level": 0}}
    plugin, _state, _m, _d = _make_plugin_with_recorders(config)
    deck = _FakeNeoDeck()
    plugin.deck = deck
    plugin._idle_dimmed = True
    await plugin._on_dial_event(None, 0, TURN, 1)
    assert plugin._idle_dimmed is False
    assert deck.brightness == 60


# ──── Bundled text font + button image rendering ────


def test_text_font_path_resolves():
    plugin, _state, _m, _d = _make_plugin_with_recorders({})
    plugin._load_text_font()
    assert plugin._text_font_path is not None
    assert plugin._text_font_path.endswith("DejaVuSans.ttf")
    assert Path(plugin._text_font_path).exists()


def test_create_button_image_sizes_and_wraps(monkeypatch):
    # Rendering needs PIL; wire it the way _lazy_import() would. Skips cleanly
    # if Pillow isn't installed so the suite still runs without it.
    img_mod = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw, ImageFont
    monkeypatch.setattr(sd_module, "Image", img_mod)
    monkeypatch.setattr(sd_module, "ImageDraw", ImageDraw)
    monkeypatch.setattr(sd_module, "ImageFont", ImageFont)

    plugin, _state, _m, _d = _make_plugin_with_recorders({})
    plugin._load_text_font()

    class _VisualDeck:
        def key_image_format(self):
            return {"size": (72, 72)}

    plugin.deck = _VisualDeck()

    # Short and long labels both yield a correctly-sized image without raising.
    for label in ["OK", "Presentation Mode",
                  "A really long multi word button label that must wrap"]:
        img = plugin._create_button_image(label, "#1a1a2e", "#e0e0e0")
        assert img.size == (72, 72)
        assert img.mode == "RGB"

    # Rendered labels are cached (text caching, like icons).
    assert len(plugin._label_cache) >= 1

    # Icon + label path also renders to the right size.
    plugin._load_icon_font()
    img = plugin._create_button_image("Power", "#1a1a2e", "#e0e0e0", icon_name="power")
    assert img.size == (72, 72)


def test_wrap_greedy_packs_and_breaks(monkeypatch):
    img_mod = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw, ImageFont
    monkeypatch.setattr(sd_module, "Image", img_mod)
    monkeypatch.setattr(sd_module, "ImageDraw", ImageDraw)
    monkeypatch.setattr(sd_module, "ImageFont", ImageFont)

    plugin, _state, _m, _d = _make_plugin_with_recorders({})
    plugin._load_text_font()
    layer = img_mod.new("RGBA", (60, 60), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = plugin._text_font(12)
    # Empty string -> no lines; a multi-word phrase wraps onto >1 line at a
    # narrow width.
    assert plugin._wrap_greedy(draw, "", font, 60) == []
    wrapped = plugin._wrap_greedy(draw, "one two three four five six", font, 40)
    assert len(wrapped) >= 2


# ──── Momentary press highlight ────


@pytest.mark.asyncio
async def test_press_highlight_marker_set_and_cleared():
    config = {"buttons": [{"index": 0, "page": 0, "bindings": {"press": [
        {"action": "macro", "macro": "m"}]}}]}
    plugin, _state, _m, _d = _make_plugin_with_recorders(config)
    await plugin._on_key_change(None, 0, True)
    assert 0 in plugin._pressed_keys
    await plugin._on_key_change(None, 0, False)
    assert 0 not in plugin._pressed_keys


@pytest.mark.asyncio
async def test_press_highlight_not_set_for_hidden_button():
    config = {"buttons": [{"index": 0, "page": 0, "bindings": {
        "press": [{"action": "macro", "macro": "m"}],
        "visible_when": {"key": "var.show", "operator": "truthy"},
    }}]}
    plugin, state, _m, _d = _make_plugin_with_recorders(config)
    state.set("var.show", "", source="test")  # hidden
    await plugin._on_key_change(None, 0, True)
    assert 0 not in plugin._pressed_keys


@pytest.mark.asyncio
async def test_press_highlight_cleared_when_hidden_mid_press():
    config = {"buttons": [{"index": 0, "page": 0, "bindings": {
        "press": [{"action": "macro", "macro": "m"}],
        "visible_when": {"key": "var.show", "operator": "truthy"},
    }}]}
    plugin, state, _m, _d = _make_plugin_with_recorders(config)
    state.set("var.show", "1", source="test")
    await plugin._on_key_change(None, 0, True)
    assert 0 in plugin._pressed_keys
    state.set("var.show", "", source="test")  # hidden before release
    await plugin._on_key_change(None, 0, False)
    assert 0 not in plugin._pressed_keys


def test_press_highlight_image_same_size(monkeypatch):
    img_mod = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw, ImageFont
    monkeypatch.setattr(sd_module, "Image", img_mod)
    monkeypatch.setattr(sd_module, "ImageDraw", ImageDraw)
    monkeypatch.setattr(sd_module, "ImageFont", ImageFont)
    plugin, _state, _m, _d = _make_plugin_with_recorders({})
    base = img_mod.new("RGB", (72, 72), "#1a1a2e")
    out = plugin._apply_press_highlight(base)
    assert out.size == (72, 72)
    assert out.mode == "RGB"
