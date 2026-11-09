"""
Stream Deck: what a key, a strip zone or an info item shows, what a context
action or a simulated input asks the plugin to do, and what a network-deck
discovery answer says.

Characterisation pins for three paths: the display-element look (the live
label, the value line and its unit, the meter's fill and color, and the
feedback style in both its forms, the ``states`` map and the active/inactive
pair); the context actions (which event name runs which action, how each
payload is read, which decks it reaches) and the simulated input the IDE's
workbench sends (each input type, the full tap, and every payload refused);
and the network-deck scan and connection test routes and the discovery
look-up that follows a moved deck.

No hardware: decks are stand-ins that answer only what these paths ask.

Run from the openavc-plugins root: pytest tests/test_streamdeck_looks_and_requests.py -v
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

from control_surfaces.streamdeck.streamdeck_plugin import StreamDeckPlugin  # noqa: E402
import control_surfaces.streamdeck.streamdeck_plugin as sd_module  # noqa: E402

try:
    from openavc.core.plugin_api import PluginAPI
    from openavc.core.plugin_registry import PluginRegistry
    from openavc.core.state_store import StateStore
    from openavc.core.event_bus import EventBus
    from openavc.core.plugin_test_harness import MockDeviceManager, MockMacroEngine
except ModuleNotFoundError:
    pytest.skip(
        "openavc repo not available as sibling directory",
        allow_module_level=True,
    )


# ──── Rig ────


class _Deck:
    """Answers what the request paths ask of a deck."""

    def __init__(self, keys=6, touch_keys=2, visual=True):
        self._keys = keys
        self._touch_keys = touch_keys
        self._visual = visual

    def key_count(self):
        return self._keys

    def touch_key_count(self):
        return self._touch_keys

    def is_visual(self):
        return self._visual


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
    plugin._logs = logs
    return plugin, state


def _session(plugin, deck=None, serial="SN1", device_id=None):
    session = sd_module._DeckSession(deck)
    session.serial = serial
    session.device_id = device_id or f"dev-{serial}"
    plugin._sessions[session.device_id] = session
    return session


def _record(target, calls, name, result=None):
    async def _rec(*args, **kwargs):
        calls.append((name,) + args + ((kwargs,) if kwargs else ()))
        return result

    setattr(target, name, _rec)


def _record_sync(target, calls, name, result=None):
    def _rec(*args, **kwargs):
        calls.append((name,) + args + ((kwargs,) if kwargs else ()))
        return result

    setattr(target, name, _rec)


def _set(state, values):
    for key, value in values.items():
        state.set(key, value, source="test")


BASE = ("Label", "bolt", "#111111", "#eeeeee")


# ──── Feedback style: the states map ────


@pytest.mark.asyncio
async def test_a_states_map_styles_by_the_value_as_text():
    plugin, state = _plugin()
    _set(state, {"var.src": 2})
    feedback = {"key": "var.src", "states": {
        "2": {"bg_color": "#222222", "text_color": "#333333",
              "label": "Two", "icon": "tv"},
    }}
    assert await plugin._apply_feedback_styles(feedback, *BASE) == (
        "Two", "tv", "#222222", "#333333")


@pytest.mark.asyncio
async def test_a_states_map_falls_back_to_its_default_state_then_to_nothing():
    plugin, state = _plugin()
    _set(state, {"var.src": "hdmi9"})
    feedback = {"key": "var.src", "default_state": "off", "states": {
        "off": {"bg_color": "#000001"},
    }}
    assert await plugin._apply_feedback_styles(feedback, *BASE) == (
        "Label", "bolt", "#000001", "#eeeeee")
    # No default_state: the "" entry is the fallback a missing value also reads.
    feedback = {"key": "var.src", "states": {"x": {"bg_color": "#000002"}}}
    assert await plugin._apply_feedback_styles(feedback, *BASE) == BASE
    feedback = {"key": "var.none", "states": {"": {"bg_color": "#000003"}}}
    assert await plugin._apply_feedback_styles(feedback, *BASE) == (
        "Label", "bolt", "#000003", "#eeeeee")


@pytest.mark.asyncio
async def test_a_states_entry_keeps_what_it_leaves_empty_and_is_ignored_when_no_object():
    plugin, state = _plugin()
    _set(state, {"var.src": "a"})
    feedback = {"key": "var.src", "states": {
        "a": {"bg_color": "", "text_color": None, "label": "", "icon": ""},
    }}
    assert await plugin._apply_feedback_styles(feedback, *BASE) == BASE
    feedback = {"key": "var.src", "states": {"a": "red"}}
    assert await plugin._apply_feedback_styles(feedback, *BASE) == BASE


@pytest.mark.asyncio
async def test_a_states_map_wins_over_the_condition_pair_and_an_empty_one_does_not():
    plugin, state = _plugin()
    _set(state, {"var.on": True})
    pair = {"style_active": {"bg_color": "#00ff00"}, "label_active": "ON"}
    # A states map with no entry for the value styles nothing, pair or not.
    feedback = dict(pair, key="var.on", states={"x": {"bg_color": "#ff0000"}})
    assert await plugin._apply_feedback_styles(feedback, *BASE) == BASE
    # An empty map, or one that is not an object, reads the pair.
    for states in ({}, ["x"], None):
        feedback = dict(pair, key="var.on", states=states)
        assert await plugin._apply_feedback_styles(feedback, *BASE) == (
            "ON", "bolt", "#00ff00", "#eeeeee")


# ──── Feedback style: the active/inactive pair ────


@pytest.mark.asyncio
async def test_the_inactive_style_sets_its_colors_label_and_icon():
    plugin, state = _plugin()
    _set(state, {"var.on": False})
    feedback = {"key": "var.on", "label_inactive": "OFF",
                "style_inactive": {"bg_color": "#440000", "text_color": "#cccccc",
                                   "icon": "power-off"}}
    assert await plugin._apply_feedback_styles(feedback, *BASE) == (
        "OFF", "power-off", "#440000", "#cccccc")


@pytest.mark.asyncio
async def test_the_active_style_reads_the_condition_equals_and_the_label_sits_outside_it():
    plugin, state = _plugin()
    _set(state, {"var.src": "hdmi2"})
    feedback = {"key": "var.src", "condition": {"equals": "hdmi2"},
                "style_active": {"icon": "tv", "label": "ignored"},
                "label_active": "HDMI 2"}
    assert await plugin._apply_feedback_styles(feedback, *BASE) == (
        "HDMI 2", "tv", "#111111", "#eeeeee")
    _set(state, {"var.src": "hdmi1"})
    assert await plugin._apply_feedback_styles(feedback, *BASE) == BASE


@pytest.mark.asyncio
async def test_a_style_that_is_no_object_or_empty_fields_keep_the_look():
    plugin, state = _plugin()
    _set(state, {"var.on": True})
    feedback = {"key": "var.on", "style_active": "green", "label_active": "ON"}
    assert await plugin._apply_feedback_styles(feedback, *BASE) == BASE
    _set(state, {"var.on": False})
    feedback = {"key": "var.on", "style_inactive": ["red"], "label_inactive": "OFF"}
    assert await plugin._apply_feedback_styles(feedback, *BASE) == BASE
    feedback = {"key": "var.on", "style_inactive": {"bg_color": "", "text_color": ""},
                "label_inactive": ""}
    assert await plugin._apply_feedback_styles(feedback, *BASE) == BASE
    # A condition that is no object compares against nothing.
    _set(state, {"var.on": 1})
    feedback = {"key": "var.on", "condition": "1",
                "style_active": {"bg_color": "#00ff00"}}
    assert await plugin._apply_feedback_styles(feedback, *BASE) == (
        "Label", "bolt", "#00ff00", "#eeeeee")


@pytest.mark.asyncio
async def test_a_feedback_with_no_key_or_no_object_reads_no_state():
    plugin, state = _plugin()
    reads = []
    _record(plugin.api, reads, "state_get")
    for feedback in (None, "var.on", {}, {"key": ""}, {"states": {"": {}}}):
        assert await plugin._apply_feedback_styles(feedback, *BASE) == BASE
    assert reads == []


# ──── Meter ────


def test_a_meter_takes_the_highest_threshold_reached_in_any_order():
    plugin, _ = _plugin()
    meter = {"color": "#0000ff", "thresholds": [
        {"above": 90, "color": "#ff0000"},
        "loud",
        {"above": 50},
        {"above": "high", "color": "#999999"},
        {"above": 50, "color": "#ffff00"},
        {"above": 50, "color": "#00ffff"},
        {"above": 70, "color": ""},
    ]}
    assert plugin._resolve_meter(meter, 60) == (0.6, "#ffff00")
    assert plugin._resolve_meter(meter, 95) == (0.95, "#ff0000")
    assert plugin._resolve_meter(meter, 49) == (0.49, "#0000ff")
    assert plugin._resolve_meter(dict(meter, thresholds="x"), 95) == (0.95, "#0000ff")


def test_a_meter_bound_that_is_no_number_falls_back_and_a_reversed_pair_resets():
    plugin, _ = _plugin()
    assert plugin._meter_bounds({"min": "a", "max": 50}, {"min": 10}) == (10, 50)
    assert plugin._meter_bounds({"min": 60, "max": 50}, None) == (0, 100)
    assert plugin._meter_bounds("x", {"min": 0, "max": 10}) == (0, 10)
    assert plugin._resolve_meter({"color": ""}, 30, {"min": 0, "max": 60}) == (
        0.5, plugin._METER_DEFAULT_COLOR)
    # Only one bound around it: no automatic meter.
    assert plugin._resolve_meter(None, 30, {"min": 0}) is None
    assert plugin._resolve_meter(None, 30, {"min": 0, "max": 60}) == (
        0.5, plugin._METER_DEFAULT_COLOR)


# ──── A display element (a zone or an info item) ────


@pytest.mark.asyncio
async def test_a_display_element_reads_its_live_label_and_keeps_its_own_when_unset():
    plugin, state = _plugin()
    element = {"label": "Mic", "label_source": "var.name", "icon": "",
               "bg_color": "", "text_color": "#123456"}
    resolved = await plugin._resolve_display(element, "#000", "#fff")
    assert resolved == {"label": "Mic", "icon": None, "value_text": "",
                        "bg": "#000", "fg": "#123456", "meter": None}
    _set(state, {"var.name": 7})
    resolved = await plugin._resolve_display(dict(element, icon="mic"), "#000", "#fff")
    assert resolved["label"] == "7" and resolved["icon"] == "mic"


@pytest.mark.asyncio
async def test_a_fixed_value_wins_over_a_live_one_and_takes_no_unit_or_meter():
    plugin, state = _plugin()
    _set(state, {"var.level": 40})
    element = {"value_static": 12, "value_source": "var.level", "unit": "dB",
               "meter": {}}
    resolved = await plugin._resolve_display(element, "#000", "#fff")
    assert resolved["value_text"] == "12" and resolved["meter"] is None
    resolved = await plugin._resolve_display(
        dict(element, value_static=None), "#000", "#fff")
    assert resolved["value_text"] == "40 dB" and resolved["meter"] == (
        0.4, plugin._METER_DEFAULT_COLOR)
    resolved = await plugin._resolve_display(
        {"value_source": "var.level", "unit": "%"}, "#000", "#fff")
    assert resolved["value_text"] == "40%"
    resolved = await plugin._resolve_display(
        {"value_source": "var.missing", "unit": "%", "meter": True}, "#000", "#fff")
    assert resolved["value_text"] == "" and resolved["meter"] is None


@pytest.mark.asyncio
async def test_a_display_element_styles_over_its_own_colors_and_live_label():
    plugin, state = _plugin()
    _set(state, {"var.name": "Lectern", "var.on": True})
    element = {"label_source": "var.name", "bg_color": "#101010",
               "feedback": {"key": "var.on", "style_active": {"text_color": "#00ff00"}}}
    resolved = await plugin._resolve_display(element, "#000", "#fff")
    assert (resolved["label"], resolved["bg"], resolved["fg"]) == (
        "Lectern", "#101010", "#00ff00")
    element["feedback"]["label_active"] = "LIVE"
    resolved = await plugin._resolve_display(element, "#000", "#fff")
    assert resolved["label"] == "LIVE"


@pytest.mark.asyncio
async def test_a_display_element_reads_label_value_then_feedback():
    plugin, state = _plugin()
    reads = []
    real = plugin.api.state_get

    async def _get(key):
        reads.append(key)
        return await real(key)

    plugin.api.state_get = _get
    await plugin._resolve_display(
        {"label_source": "var.a", "value_source": "var.b",
         "feedback": {"key": "var.c"}}, "#000", "#fff")
    assert reads == ["var.a", "var.b", "var.c"]


@pytest.mark.asyncio
async def test_a_key_reads_label_value_toggle_then_feedback():
    plugin, state = _plugin()
    reads = []
    real = plugin.api.state_get

    async def _get(key):
        reads.append(key)
        return await real(key)

    plugin.api.state_get = _get
    session = _session(plugin, _Deck())
    await plugin._key_look(session, {
        "label_source": "var.a", "value_source": "var.b",
        "bindings": {
            "press": {"mode": "toggle", "toggle_key": "var.t",
                      "on_label": "On", "off_label": "Off"},
            "feedback": {"key": "var.c"},
        },
    })
    assert reads == ["var.a", "var.b", "var.t", "var.c"]


@pytest.mark.asyncio
async def test_a_key_and_a_zone_write_the_same_value_line():
    plugin, state = _plugin()
    session = _session(plugin, _Deck())
    for value, unit, text in ((5, "", "5"), (5, "%", "5%"), (5, "ms", "5 ms"),
                              (0, "dB", "0 dB"), ("on", None, "on")):
        _set(state, {"var.v": value})
        assignment = {"value_source": "var.v", "unit": unit}
        look = await plugin._key_look(session, assignment)
        zone = await plugin._resolve_display(assignment, "#000", "#fff")
        assert look.value_text == zone["value_text"] == text


# ──── Context actions: which name runs which action ────


def _action_rig(sessions=1, visual=True):
    plugin, state = _plugin()
    calls = []
    decks = [
        _session(plugin, _Deck(visual=visual), serial=f"SN{i + 1}")
        for i in range(sessions)
    ]
    for name in ("_simulate_input", "_set_live_mirror", "_change_page",
                 "_flash_key", "_show_message", "_identify_deck"):
        _record(plugin, calls, name)
    _record_sync(plugin, calls, "_set_deck_brightness")
    return plugin, calls, decks


@pytest.mark.asyncio
@pytest.mark.parametrize("event,payload,expect", [
    ("plugin.streamdeck.action.simulate_input", {"type": "key"}, "_simulate_input"),
    ("x.action.simulate_input_extra", {}, "_simulate_input"),
    ("plugin.streamdeck.action.set_live_mirror", {"on": True}, "_set_live_mirror"),
    ("plugin.streamdeck.action.set_page", {"page": "2"}, "_change_page"),
    ("anything.set_page", {"page": 1}, "_change_page"),
    ("plugin.streamdeck.action.set_brightness", {"level": 40}, "_set_deck_brightness"),
    ("plugin.streamdeck.action.flash_key", {"index": 1}, "_flash_key"),
    ("plugin.streamdeck.action.show_message", {"text": "Hi"}, "_show_message"),
    ("plugin.streamdeck.action.identify", {}, "_identify_deck"),
    ("plugin.streamdeck.action.identify_deck", {}, "_identify_deck"),
    # The first rule that matches wins.
    ("x.action.simulate_input.set_page", {"page": 1}, "_simulate_input"),
    ("x.action.set_live_mirror.show_message", {"text": "a"}, "_set_live_mirror"),
    ("x.action.identify.set_page", {"page": 1}, "_change_page"),
])
async def test_each_event_name_runs_its_action(event, payload, expect):
    plugin, calls, _ = _action_rig()
    await plugin._on_context_action(event, payload)
    assert [c[0] for c in calls] == [expect]


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    "plugin.streamdeck.action.set_pages", "plugin.streamdeck.action.unknown",
    "plugin.streamdeck.set_page.later", "identify", "set_page",
])
async def test_an_event_name_no_rule_matches_runs_nothing(event):
    plugin, calls, _ = _action_rig()
    await plugin._on_context_action(event, {"page": 1, "text": "x"})
    assert calls == []


@pytest.mark.asyncio
async def test_a_payload_that_is_no_object_reads_as_empty():
    plugin, calls, decks = _action_rig()
    await plugin._on_context_action("a.action.simulate_input", ["key"])
    await plugin._on_context_action("a.action.set_live_mirror", None)
    await plugin._on_context_action("a.action.identify", "SN1")
    assert calls == [
        ("_simulate_input", {}),
        ("_set_live_mirror", {}),
        ("_identify_deck", decks[0]),
    ]


# ──── Context actions: each payload ────


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,page", [
    ({"page": 3}, 3), ({"page": "2"}, 2), ({"page": 2.9}, 2), ({"page": True}, 1),
    ({"page": -1}, -1), ({"page": "two"}, None), ({"page": None}, None), ({}, None),
])
async def test_set_page_reads_a_whole_page_number(payload, page):
    plugin, calls, decks = _action_rig()
    await plugin._on_context_action("a.set_page", payload)
    assert calls == ([] if page is None else [("_change_page", decks[0], page)])


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,level", [
    ({"level": 40}, 40), ({"level": "55"}, 55), ({"level": 12.7}, 12),
    ({"level": 150}, 100), ({"level": -5}, 0), ({"level": "bright"}, None),
    ({"level": None}, None), ({}, None), ({"level": True}, 1),
])
async def test_set_brightness_reads_a_level_and_clamps_it(payload, level):
    plugin, calls, decks = _action_rig()
    await plugin._on_context_action("a.set_brightness", payload)
    assert calls == ([] if level is None else [("_set_deck_brightness", decks[0], level)])


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,expect", [
    ({"index": 2}, (2, 2)), ({"index": "1", "times": 4}, (1, 4)),
    ({"index": 0, "times": 0}, (0, 1)), ({"index": 0, "times": 99}, (0, 10)),
    ({"index": 0, "times": "many"}, (0, 2)), ({"index": 0, "times": None}, (0, 2)),
    ({"index": 0, "times": 3.6}, (0, 3)), ({"index": "x"}, None), ({}, None),
])
async def test_flash_key_reads_an_index_and_a_bounded_count(payload, expect):
    plugin, calls, decks = _action_rig()
    await plugin._on_context_action("a.flash_key", payload)
    assert calls == ([] if expect is None else [("_flash_key", decks[0], *expect)])


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,expect", [
    ({"text": " Mics live "}, ("Mics live", 5.0)),
    ({"text": "Hi", "seconds": 2}, ("Hi", 2.0)),
    ({"text": "Hi", "seconds": "1.5"}, ("Hi", 1.5)),
    ({"text": "Hi", "seconds": 0}, ("Hi", 5.0)),
    ({"text": "Hi", "seconds": -3}, ("Hi", 5.0)),
    ({"text": "Hi", "seconds": "soon"}, ("Hi", 5.0)),
    ({"text": 42}, ("42", 5.0)),
    ({"text": "   "}, None), ({"text": ""}, None), ({}, None),
])
async def test_show_message_reads_its_text_and_how_long(payload, expect):
    plugin, calls, decks = _action_rig()
    await plugin._on_context_action("a.show_message", payload)
    assert calls == ([] if expect is None else [("_show_message", decks[0], *expect)])


@pytest.mark.asyncio
async def test_show_message_skips_a_deck_with_no_screen():
    plugin, calls, _ = _action_rig(visual=False)
    await plugin._on_context_action("a.show_message", {"text": "Hi"})
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("event,payload", [
    ("a.set_page", {"page": 1}),
    ("a.set_brightness", {"level": 10}),
    ("a.flash_key", {"index": 0}),
    ("a.show_message", {"text": "Hi"}),
    ("a.action.identify", {}),
])
async def test_each_deck_action_reaches_one_serial_or_every_deck(event, payload):
    plugin, calls, decks = _action_rig(sessions=3)
    await plugin._on_context_action(event, dict(payload, serial="SN2"))
    assert [c[1] for c in calls] == [decks[1]]
    calls.clear()
    await plugin._on_context_action(event, dict(payload, serial="SN9"))
    assert calls == []
    for serial in (None, ""):
        calls.clear()
        await plugin._on_context_action(event, dict(payload, serial=serial))
        assert [c[1] for c in calls] == decks


# ──── Simulated input ────


def _input_rig(keys=6, touch_keys=2):
    plugin, state = _plugin()
    session = _session(plugin, _Deck(keys=keys, touch_keys=touch_keys), serial="VSN")
    calls = []

    async def _key(s, deck, index, pressed):
        calls.append(("key", s is session, deck is session.deck, index, pressed))

    async def _dial(s, deck, index, event, value):
        calls.append(("dial", s is session, deck is session.deck, index, event.name, value))

    async def _touch(s, deck, event, value):
        calls.append(("touch", s is session, deck is session.deck, event.name, value))

    plugin._on_key_change = _key
    plugin._on_dial_event = _dial
    plugin._on_touchscreen_event = _touch
    return plugin, session, calls


@pytest.mark.asyncio
async def test_simulated_input_for_a_serial_no_deck_has_warns_and_does_nothing():
    plugin, _, calls = _input_rig()
    await plugin._simulate_input({"serial": "NOPE", "type": "key", "index": 0})
    await plugin._simulate_input({"type": "key", "index": 0})
    assert calls == []
    assert plugin._logs == [
        ("warning", "simulate_input: no connected deck with serial 'NOPE'"),
        ("warning", "simulate_input: no connected deck with serial ''"),
    ]


@pytest.mark.asyncio
async def test_simulated_input_reads_the_serial_as_text_and_takes_the_first_match():
    plugin, session, calls = _input_rig()
    session.serial = "123"
    second = _session(plugin, _Deck(), serial="123", device_id="dev-other")
    await plugin._simulate_input({"serial": 123, "type": "key", "index": 1})
    assert calls == [("key", True, True, 1, True), ("key", True, True, 1, False)]
    assert second is not session


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,expect", [
    ({"index": 0}, [(0, True), (0, False)]),
    ({"index": "7"}, [(7, True), (7, False)]),
    ({"index": 2, "pressed": True}, [(2, True)]),
    ({"index": 2, "pressed": 0}, [(2, False)]),
    ({"index": 2, "pressed": "yes"}, [(2, True)]),
    ({"index": 8}, []), ({"index": -1}, []), ({"index": "a"}, []), ({}, []),
])
async def test_a_simulated_key_is_a_full_tap_or_one_edge_inside_the_deck(payload, expect):
    plugin, _, calls = _input_rig(keys=6, touch_keys=2)
    await plugin._simulate_input(dict(payload, serial="VSN", type="key"))
    assert calls == [("key", True, True, i, p) for i, p in expect]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,expect", [
    ({"index": 1}, [(1, "TURN", 1)]),
    ({"index": "0", "amount": "-3"}, [(0, "TURN", -3)]),
    ({"index": 9, "amount": 2}, [(9, "TURN", 2)]),
    ({"index": 0, "amount": "fast"}, []), ({"index": None}, []), ({}, []),
])
async def test_a_simulated_dial_turn_reads_an_index_and_an_amount(payload, expect):
    plugin, _, calls = _input_rig()
    await plugin._simulate_input(dict(payload, serial="VSN", type="dial_turn"))
    assert calls == [("dial", True, True, *e) for e in expect]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,expect", [
    ({"index": 1}, [(1, "PUSH", True), (1, "PUSH", False)]),
    ({"index": 0, "pressed": 1}, [(0, "PUSH", True)]),
    ({"index": 0, "pressed": False}, [(0, "PUSH", False)]),
    ({"index": "x"}, []), ({}, []),
])
async def test_a_simulated_dial_push_is_a_full_tap_or_one_edge(payload, expect):
    plugin, _, calls = _input_rig()
    await plugin._simulate_input(dict(payload, serial="VSN", type="dial_push"))
    assert calls == [("dial", True, True, *e) for e in expect]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,expect", [
    ({"x": 100}, [("SHORT", {"x": 100, "y": 50})]),
    ({"x": "100", "y": "20"}, [("SHORT", {"x": 100, "y": 20})]),
    ({"x": 100, "y": "top"}, [("SHORT", {"x": 100, "y": 50})]),
    ({"x": 100, "touch_type": "LONG"}, [("LONG", {"x": 100, "y": 50})]),
    ({"x": 100, "touch_type": "swipe"}, [("SHORT", {"x": 100, "y": 50})]),
    ({"x": 100, "touch_type": None}, [("SHORT", {"x": 100, "y": 50})]),
    ({"x": 100, "y": 30, "touch_type": "Drag", "x_out": "300"},
     [("DRAG", {"x": 100, "y": 30, "x_out": 300, "y_out": 30})]),
    ({"x": 100, "touch_type": "drag"}, []),
    ({"x": 100, "touch_type": "drag", "x_out": "far"}, []),
    ({"x": "left"}, []), ({}, []),
])
async def test_a_simulated_touch_is_a_tap_a_long_press_or_a_drag(payload, expect):
    plugin, _, calls = _input_rig()
    await plugin._simulate_input(dict(payload, serial="VSN", type="touch"))
    assert calls == [("touch", True, True, *e) for e in expect]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["swipe", None, "KEY", 3])
async def test_a_simulated_input_of_no_known_type_does_nothing(kind):
    plugin, _, calls = _input_rig()
    await plugin._simulate_input({"serial": "VSN", "type": kind, "index": 0, "x": 5})
    assert calls == []


# ──── Network decks: the scan, the connection test, the look-up ────


def _route(plugin, path):
    router = plugin._build_ext_router()
    return next(r.endpoint for r in router.routes if r.path == path)


def test_the_router_serves_its_three_routes_in_order():
    plugin, _ = _plugin()
    router = plugin._build_ext_router()
    assert [(r.path, sorted(r.methods)) for r in router.routes] == [
        ("/live/{serial}/{item}", ["GET"]),
        ("/network/scan", ["POST"]),
        ("/network/test", ["POST"]),
    ]


@pytest.mark.asyncio
async def test_a_scan_with_no_browse_or_a_failed_one_says_browse_is_unavailable():
    plugin, _ = _plugin()
    plugin.api.mdns_browse = None
    assert await _route(plugin, "/network/scan")() == {
        "browse_available": False, "found": []}

    async def _fails(services, duration):
        raise OSError("no multicast")

    plugin.api.mdns_browse = _fails
    assert await _route(plugin, "/network/scan")() == {
        "browse_available": False, "found": []}
    assert plugin._logs == [("warning", "Network deck scan failed: no multicast")]


@pytest.mark.asyncio
async def test_a_scan_lists_each_elgato_answer_with_an_address():
    plugin, _ = _plugin({"network_decks": [
        {"host": "10.0.0.5", "port": 5343}, {"host": "10.0.0.9", "port": 9000},
    ]})
    asked = []

    async def _browse(services, duration):
        asked.append((services, duration))
        return [
            {"service_type": "_elg._tcp.local.", "ip": "10.0.0.5", "port": 5343,
             "instance_name": "Dock A", "txt": {"sn": 12345, "dt": 215}},
            {"service_type": "_elg._tcp.local.", "ip": "10.0.0.9",
             "hostname": "studio.local", "txt": {"sn": "S1", "dt": "1"}},
            {"service_type": "_elg._tcp.local.", "ip": "10.0.0.7", "port": 7000,
             "txt": None},
            {"service_type": "_http._tcp.local.", "ip": "10.0.0.8"},
            {"service_type": None, "ip": "10.0.0.8"},
            {"service_type": "_elg._tcp.local.", "ip": "", "txt": {"sn": "X"}},
            {"service_type": "_elg._tcp.local."},
        ]

    plugin.api.mdns_browse = _browse
    result = await _route(plugin, "/network/scan")()
    assert asked == [(["_elg._tcp.local."], 4.0)]
    assert result == {"browse_available": True, "found": [
        {"host": "10.0.0.5", "port": 5343, "name": "Dock A", "serial": "12345",
         "kind": "Network Dock", "already_added": True},
        {"host": "10.0.0.9", "port": 5343, "name": "studio.local", "serial": "S1",
         "kind": "Stream Deck", "already_added": True},
        {"host": "10.0.0.7", "port": 7000, "name": "10.0.0.7", "serial": "",
         "kind": "Stream Deck", "already_added": False},
    ]}


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,error", [
    ({"host": "10.0.0.5", "port": "abc"}, "invalid port"),
    ({"host": "10.0.0.5", "port": None}, "invalid port"),
    ({"host": "  ", "port": 5343}, "enter a host or IP address"),
    ({"host": "10.0.0.5", "port": 0}, "enter a host or IP address"),
    ({"host": "10.0.0.5", "port": 65536}, "enter a host or IP address"),
])
async def test_a_connection_test_refuses_a_bad_address_before_dialling(payload, error):
    plugin, _ = _plugin()
    dialled = []
    plugin.api.local_address = lambda host: dialled.append(host)
    assert await _route(plugin, "/network/test")(payload) == {
        "success": False, "error": error}
    assert dialled == []


@pytest.mark.asyncio
async def test_a_connection_test_dials_from_the_chosen_adapter_and_closes(monkeypatch):
    plugin, _ = _plugin()
    plugin.api.local_address = lambda host: "192.168.1.10"
    opened = []

    class _Writer:
        closed = False

        def close(self):
            self.closed = True

    writer = _Writer()

    async def _open(host, port, local_addr=None):
        opened.append((host, port, local_addr))
        return None, writer

    monkeypatch.setattr(sd_module.asyncio, "open_connection", _open)
    assert await _route(plugin, "/network/test")({"host": " 10.0.0.5 "}) == {
        "success": True}
    assert opened == [("10.0.0.5", 5343, ("192.168.1.10", 0))]
    assert writer.closed
    plugin.api.local_address = lambda host: None
    await _route(plugin, "/network/test")({"host": "10.0.0.5", "port": "6000"})
    assert opened[-1] == ("10.0.0.5", 6000, None)


@pytest.mark.asyncio
async def test_the_look_up_follows_the_first_answer_for_a_known_serial():
    plugin, _ = _plugin({"network_decks": [{"host": "10.0.0.5", "port": 5343}]})
    saved = []

    async def _save(config):
        saved.append(config)

    plugin.api.save_config = _save
    asked = []

    async def _browse(services, duration):
        asked.append((services, duration))
        return [
            {"ip": "10.0.0.20", "txt": {"sn": "OTHER"}},
            {"ip": "", "port": 1, "txt": {"sn": "A1"}},
            {"ip": "10.0.0.30", "txt": {"sn": "A1"}},
        ]

    plugin.api.mdns_browse = _browse
    await plugin._net_reresolve("10.0.0.5:5343", {"A1"})
    # The first answer naming the serial has no address: the look-up ends.
    assert asked == [(["_elg._tcp.local."], 5.0)] and saved == []

    async def _browse2(services, duration):
        return [{"ip": "10.0.0.30", "txt": {"sn": 77}}]

    plugin.api.mdns_browse = _browse2
    await plugin._net_reresolve("10.0.0.5:5343", {"77"})
    assert saved[-1]["network_decks"] == [{"host": "10.0.0.30", "port": 5343}]
    assert plugin._logs[-1] == (
        "info", "Network deck 77 answered at 10.0.0.30:5343 (was 10.0.0.5:5343); "
        "following it")


@pytest.mark.asyncio
async def test_the_look_up_logs_a_failed_browse_and_ignores_an_answer_where_it_is():
    plugin, _ = _plugin({"network_decks": [{"host": "10.0.0.5", "port": 5343}]})
    saved = []

    async def _save(config):
        saved.append(config)

    plugin.api.save_config = _save

    async def _fails(services, duration):
        raise OSError("down")

    plugin.api.mdns_browse = _fails
    await plugin._net_reresolve("10.0.0.5:5343", {"A1"})
    assert plugin._logs == [("warning", "Network deck discovery failed: down")]

    async def _same(services, duration):
        return [{"ip": "10.0.0.5", "port": 5343, "txt": {"sn": "A1"}}]

    plugin.api.mdns_browse = _same
    await plugin._net_reresolve("10.0.0.5:5343", {"A1"})
    assert saved == []


@pytest.mark.asyncio
async def test_an_active_style_with_empty_colors_keeps_the_look():
    plugin, state = _plugin()
    _set(state, {"var.on": True})
    feedback = {"key": "var.on",
                "style_active": {"bg_color": "", "text_color": None, "icon": ""}}
    assert await plugin._apply_feedback_styles(feedback, *BASE) == BASE


@pytest.mark.asyncio
async def test_a_simulated_edge_is_passed_on_as_true_or_false():
    plugin, _, calls = _input_rig()
    await plugin._simulate_input({"serial": "VSN", "type": "dial_push", "index": 0,
                                  "pressed": 1})
    await plugin._simulate_input({"serial": "VSN", "type": "key", "index": 0,
                                  "pressed": 0})
    assert [type(c[-1]) for c in calls] == [bool, bool]
