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
    )
    plugin = StreamDeckPlugin()
    plugin.api = api
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
