"""
Stream Deck: how a deck reacts to state, to a key press, and what a key draws.

Characterisation pins for three paths: which state keys a deck's config makes
it watch (and which watched change re-renders what), the key press modes
(tap, toggle, tap_hold, hold_repeat) with the bookkeeping around them, and the
decisions a key's render makes before any pixel is drawn (label, colors, the
value line, toggle labels, hidden keys, touch-key colors, the nav, press and
macro marks).

No hardware and no PIL: the render pins record what the plugin hands its image
and color writers, and the deck is a stand-in that only answers the questions
these paths ask.

Run from the openavc-plugins root: pytest tests/test_streamdeck_reactions.py -v
"""

import asyncio
import functools
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
from control_surfaces.streamdeck.watch_keys import (  # noqa: E402
    button_watches_key,
    zone_watches_key,
)

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
    """Answers what the watch, press and render paths ask of a deck."""

    def __init__(self, visual=True, keys=4, touch_keys=2, dials=2, width=800):
        self._visual = visual
        self._keys = keys
        self._touch_keys = touch_keys
        self._dials = dials
        self._width = width

    def is_visual(self):
        return self._visual

    def key_count(self):
        return self._keys

    def touch_key_count(self):
        return self._touch_keys

    def dial_count(self):
        return self._dials

    def touchscreen_image_format(self):
        return {"size": (self._width, 100)}


def _plugin(config=None):
    state = StateStore()
    events = EventBus()
    state.set_event_bus(events)
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
        log_fn=lambda pid, msg, level="info": None,
    )
    plugin = StreamDeckPlugin()
    plugin.api = api
    return plugin, state


def _session(plugin, deck=None, serial="SN1"):
    session = sd_module._DeckSession(deck)
    session.serial = serial
    session.device_id = f"dev-{serial}"
    plugin._sessions[session.device_id] = session
    return session


def _record(plugin, calls, name, result=None, raises=None):
    """Replace an async plugin method with a recorder appending (name, args)."""

    async def _rec(*args, **kwargs):
        calls.append((name,) + args + ((kwargs,) if kwargs else ()))
        if raises is not None:
            raise raises
        return result

    setattr(plugin, name, _rec)


def _record_sync(plugin, calls, name, result=None):
    def _rec(*args, **kwargs):
        calls.append((name,) + args + ((kwargs,) if kwargs else ()))
        return result

    setattr(plugin, name, _rec)


# ──── Which keys a deck watches ────


def _rich_config():
    return {
        "buttons": [
            {
                "index": 0, "page": 0,
                "label_source": "var.lbl0", "value_source": "var.val0",
                "bindings": {
                    "feedback": {"key": "var.fb0"},
                    "press": [{
                        "mode": "toggle", "toggle_key": "var.tog0",
                        "action": "macro", "macro": "m_on",
                        "off_action": {"action": "macro", "macro": "m_off"},
                    }],
                    "visible_when": {"key": "var.vis0", "operator": "truthy"},
                },
            },
            {
                "index": 1, "page": 1,
                "bindings": {"press": [
                    {"action": "macro", "macro": "m1",
                     "hold_action": {"action": "macro", "macro": "m_hold"}},
                    {"action": "macro", "macro": "m1b"},
                    "not an action",
                ]},
            },
            # Shadowed by the locked key at index 2: inert, watched by nothing.
            {
                "index": 2, "page": 0,
                "label_source": "var.shadowed_label",
                "bindings": {"feedback": {"key": "var.shadowed"},
                             "press": [{"action": "macro", "macro": "m_shadow"}]},
            },
            "not an entry",
            # No index: its keys are watched, its macro is not mapped.
            {"page": 0, "value_source": "var.noindex",
             "bindings": {"press": {"action": "macro", "macro": "m_noindex"}}},
            {"index": 3, "label_source": "var.lbl3", "bindings": "not a dict"},
            # A non-dict first press entry: unwrap reads no toggle key.
            {"index": 3, "page": 2,
             "bindings": {"press": ["x", {"toggle_key": "var.second_entry",
                                          "action": "macro", "macro": "m_p2"}]}},
            # Feedback with an empty key, a press action with an empty macro.
            {"index": 0, "page": 3,
             "bindings": {"feedback": {"key": ""},
                          "press": {"action": "macro", "macro": ""},
                          "visible_when": None}},
        ],
        "global_buttons": [
            {"index": 2, "bindings": {"press": {"action": "macro", "macro": "m_global"},
                                      "feedback": {"key": "var.gfb"}}},
            "not an entry",
        ],
        "auto_page": [{"when": {"key": "var.ap"}, "page": 1}, "x", {"page": 0}],
        "touchscreen": {"zones": [
            {"label_source": "var.zl", "value_source": "var.zv",
             "feedback": {"key": "var.zfb"}, "drag_adjust": {"key": "var.zdrag"}},
            "x",
            {"feedback": "not a dict"},
        ]},
        "dials": [{"index": 0, "adjust": {"key": "var.dial0"}}, {"index": 1},
                  {"index": 2, "adjust": "x"}, "x"],
        "info_strip": {"items": [
            {"label_source": "var.il", "value_source": "var.iv",
             "feedback": {"key": "var.ifb"}},
            {"key": "var.legacy"},
            {"key": "var.third_item_never_shows"},
        ]},
        "auto_brightness": [
            {"when": {"any": [{"key": "var.b1"}, {"key": "var.b2"}]}, "level": 10},
            "x",
        ],
    }


@pytest.mark.asyncio
async def test_setup_collects_each_watch_set_exactly():
    plugin, _ = _plugin(_rich_config())
    session = _session(plugin)
    subscribed = []

    async def _subscribe(key, callback):
        subscribed.append((key, callback))
        return f"sub-{len(subscribed)}"

    plugin.api.state_subscribe = _subscribe
    await plugin._setup_feedback_subscriptions(session)

    assert session.watch.auto_page == {"var.ap"}
    # Explicit zones' sources and feedback, plus every dial's adjust key; an
    # explicit zone's drag_adjust key is not subscribed.
    assert session.watch.touch_strip == {"var.zl", "var.zv", "var.zfb", "var.dial0"}
    # Two info items at most; a legacy ``key`` becomes the value source.
    assert session.watch.info_strip == {"var.il", "var.iv", "var.ifb", "var.legacy"}
    assert session.watch.brightness == {"var.b1", "var.b2"}
    assert session.watch.macro_keys == {
        "m_on": {(0, 0)}, "m_off": {(0, 0)},
        "m1": {(1, 1)}, "m_hold": {(1, 1)}, "m1b": {(1, 1)},
        "m_global": {(None, 2)},
        "m_p2": {(2, 3)},
    }
    expected = {
        "var.lbl0", "var.val0", "var.fb0", "var.tog0", "var.vis0",
        "var.noindex", "var.lbl3", "var.gfb",
        "var.ap", "var.zl", "var.zv", "var.zfb", "var.dial0",
        "var.il", "var.iv", "var.ifb", "var.legacy", "var.b1", "var.b2",
    }
    assert {k for k, _ in subscribed} == expected
    assert len(subscribed) == len(expected)
    assert session.feedback_subs == [f"sub-{i}" for i in range(1, len(expected) + 1)]
    callback = subscribed[0][1]
    assert isinstance(callback, functools.partial)
    assert callback.func == plugin._on_state_change
    assert callback.args == (session,)


@pytest.mark.asyncio
async def test_setup_replaces_the_sets_and_keeps_appending_subscription_ids():
    plugin, _ = _plugin(_rich_config())
    session = _session(plugin)
    count = 0

    async def _subscribe(key, callback):
        nonlocal count
        count += 1
        return f"sub-{count}"

    plugin.api.state_subscribe = _subscribe
    await plugin._setup_feedback_subscriptions(session)
    first = len(session.feedback_subs)
    plugin.api._update_config({"buttons": [{"index": 0, "label_source": "var.only"}]})
    await plugin._setup_feedback_subscriptions(session)
    assert session.watch.auto_page == set()
    assert session.watch.touch_strip == set()
    assert session.watch.info_strip == set()
    assert session.watch.brightness == set()
    assert session.watch.macro_keys == {}
    assert len(session.feedback_subs) == first + 1


@pytest.mark.asyncio
async def test_setup_tolerates_every_section_in_the_wrong_shape():
    config = {
        "buttons": {"index": 0}, "global_buttons": "x", "auto_page": {"when": 1},
        "touchscreen": ["x"], "dials": {"index": 0}, "info_strip": "x",
        "auto_brightness": "x",
    }
    plugin, _ = _plugin(config)
    session = _session(plugin)
    keys = []

    async def _subscribe(key, callback):
        keys.append(key)
        return "s"

    plugin.api.state_subscribe = _subscribe
    await plugin._setup_feedback_subscriptions(session)
    assert keys == []
    assert session.watch.macro_keys == {}


@pytest.mark.asyncio
async def test_setup_reads_the_decks_own_config_view():
    config = {
        "buttons": [{"index": 0, "label_source": "var.flat"}],
        "decks": {"SN2": {"buttons": [{"index": 0, "label_source": "var.own"}],
                          "touchscreen": {"zones": "x"}}},
    }
    plugin, _ = _plugin(config)
    flat = _session(plugin, serial="SN1")
    own = _session(plugin, serial="SN2")
    seen = {}

    async def _subscribe(key, callback):
        seen.setdefault(callback.args[0].serial, []).append(key)
        return "s"

    plugin.api.state_subscribe = _subscribe
    await plugin._setup_feedback_subscriptions(flat)
    await plugin._setup_feedback_subscriptions(own)
    assert seen == {"SN1": ["var.flat"], "SN2": ["var.own"]}


def test_zone_watches_key_reads_sources_feedback_and_drag():
    watches = zone_watches_key
    zone = {"label_source": "a", "value_source": "b", "feedback": {"key": "c"},
            "drag_adjust": {"key": "d"}}
    assert [watches(zone, k) for k in "abcde"] == [True, True, True, True, False]
    assert watches({"feedback": "c", "drag_adjust": "d"}, "c") is False
    assert watches({"feedback": "c", "drag_adjust": "d"}, "d") is False
    assert watches({}, None) is True  # both sources absent read as None


def test_button_watches_key_reads_sources_feedback_toggle_and_visibility():
    watches = button_watches_key
    btn = {
        "label_source": "a", "value_source": "b",
        "bindings": {
            "feedback": {"key": "c"},
            "press": [{"toggle_key": "d"}],
            "visible_when": {"all": [{"key": "e"}]},
        },
    }
    assert [watches(btn, k) for k in "abcdef"] == [True, True, True, True, True, False]
    assert watches({"bindings": "x"}, "a") is False
    assert watches({"bindings": {"feedback": "c"}}, "c") is False
    assert watches({"bindings": {"press": ["d", {"toggle_key": "d"}]}}, "d") is False
    assert watches({"bindings": {"press": {"toggle_key": "d"}}}, "d") is True
    # An absent field is never a watched key: only non-empty keys are
    # subscribed, so a state change never asks about None.
    assert watches({}, None) is False


# ──── What a watched change does ────


def _reaction_rig(config, deck=None):
    plugin, state = _plugin(config)
    session = _session(plugin, deck if deck is not None else _Deck())
    calls = []
    _record(plugin, calls, "_render_button")
    _record(plugin, calls, "_render_info_strip")
    _record(plugin, calls, "_apply_active_brightness")
    _record(plugin, calls, "_change_page")
    _record_sync(plugin, calls, "_schedule_strip_render")
    return plugin, state, session, calls


@pytest.mark.asyncio
async def test_state_change_does_nothing_without_a_visual_deck():
    for deck in (None, _Deck(visual=False)):
        plugin, _, session, calls = _reaction_rig({"buttons": [
            {"index": 0, "label_source": "var.k"}]})
        session.deck = deck
        session.watch.auto_page = {"var.k"}
        await plugin._on_state_change(session, "var.k", 1, 0)
        assert calls == []


@pytest.mark.asyncio
async def test_auto_page_switch_returns_before_any_render():
    config = {
        "auto_page": [{"when": {"key": "var.mode", "operator": "eq", "value": "b"},
                       "page": 1}],
        "buttons": [{"index": 0, "page": 0, "label_source": "var.mode"},
                    {"index": 0, "page": 1}],
    }
    plugin, state, session, calls = _reaction_rig(config)
    session.watch.auto_page = {"var.mode"}
    session.watch.info_strip = {"var.mode"}
    state.set("var.mode", "b", source="test")
    await plugin._on_state_change(session, "var.mode", "b", None)
    assert calls == [("_change_page", session, 1)]


@pytest.mark.asyncio
async def test_auto_page_on_the_showing_page_or_no_match_falls_through():
    config = {
        "auto_page": [{"when": {"key": "var.mode", "operator": "eq", "value": "b"},
                       "page": 1}],
        "buttons": [{"index": 0, "page": 0, "label_source": "var.mode"},
                    {"index": 2, "page": 1, "label_source": "var.mode"}],
    }
    plugin, state, session, calls = _reaction_rig(config)
    session.watch.auto_page = {"var.mode"}
    # No rule matches: the current page's watcher re-renders.
    await plugin._on_state_change(session, "var.mode", "a", None)
    assert calls == [("_render_button", session, 0)]
    # The rule names the page already showing.
    calls.clear()
    session.current_page = 1
    state.set("var.mode", "b", source="test")
    await plugin._on_state_change(session, "var.mode", "b", None)
    assert calls == [("_render_button", session, 2)]


@pytest.mark.asyncio
async def test_strip_change_redraws_the_watching_zones_or_the_whole_strip():
    config = {"touchscreen": {"zones": [
        {"value_source": "var.a"}, {"drag_adjust": {"key": "var.b"}},
        {"feedback": {"key": "var.a"}},
    ]}}
    plugin, _, session, calls = _reaction_rig(config)
    session.watch.touch_strip = {"var.a", "var.b", "var.none"}
    await plugin._on_state_change(session, "var.a", 1, 0)
    await plugin._on_state_change(session, "var.b", 1, 0)
    await plugin._on_state_change(session, "var.none", 1, 0)
    await plugin._on_state_change(session, "var.unwatched", 1, 0)
    assert calls == [
        ("_schedule_strip_render", session, [0, 2]),
        ("_schedule_strip_render", session, [1]),
        ("_schedule_strip_render", session, None),
    ]


@pytest.mark.asyncio
async def test_default_zones_follow_their_dials_adjust_key():
    config = {"dials": [{"index": 1, "adjust": {"key": "var.vol"}}]}
    plugin, _, session, calls = _reaction_rig(config)
    session.watch.touch_strip = {"var.vol"}
    await plugin._on_state_change(session, "var.vol", 3, 2)
    assert calls == [("_schedule_strip_render", session, [1])]


@pytest.mark.asyncio
async def test_info_and_brightness_react_and_dim_holds_brightness():
    plugin, _, session, calls = _reaction_rig({})
    session.watch.info_strip = {"var.k"}
    session.watch.brightness = {"var.k"}
    await plugin._on_state_change(session, "var.k", 1, 0)
    assert calls == [("_render_info_strip", session),
                     ("_apply_active_brightness", session)]
    calls.clear()
    session.idle_dimmed = True
    await plugin._on_state_change(session, "var.k", 2, 1)
    assert calls == [("_render_info_strip", session)]


@pytest.mark.asyncio
async def test_reactions_run_in_order_strip_info_brightness_keys():
    config = {
        "touchscreen": {"zones": [{"value_source": "var.k"}]},
        "buttons": [{"index": 1, "label_source": "var.k"}],
    }
    plugin, _, session, calls = _reaction_rig(config)
    session.watch.auto_page = {"var.k"}
    session.watch.touch_strip = {"var.k"}
    session.watch.info_strip = {"var.k"}
    session.watch.brightness = {"var.k"}
    await plugin._on_state_change(session, "var.k", 1, 0)
    assert [c[0] for c in calls] == [
        "_schedule_strip_render", "_render_info_strip",
        "_apply_active_brightness", "_render_button",
    ]


@pytest.mark.asyncio
async def test_key_rerender_takes_locked_keys_then_this_pages_unshadowed_watchers():
    config = {
        "global_buttons": [
            {"index": 5, "bindings": {"feedback": {"key": "var.k"}}},
            {"index": 4, "label_source": "var.other"},
            {"label_source": "var.k"},  # no index: never rendered
        ],
        "buttons": [
            {"index": 3, "page": 0, "value_source": "var.k"},
            {"index": 5, "page": 0, "label_source": "var.k"},  # shadowed
            {"index": 1, "page": 1, "label_source": "var.k"},  # other page
            {"index": 0, "label_source": "var.k"},  # page defaults to 0
            {"index": 2, "page": 0,
             "bindings": {"press": [{"toggle_key": "var.k"}]}},
            {"index": 6, "page": 0,
             "bindings": {"visible_when": {"key": "var.k"}}},
            "x",
        ],
    }
    plugin, _, session, calls = _reaction_rig(config)
    await plugin._on_state_change(session, "var.k", 1, 0)
    assert calls == [
        ("_render_button", session, 5),
        ("_render_button", session, 3),
        ("_render_button", session, 0),
        ("_render_button", session, 2),
        ("_render_button", session, 6),
    ]
    calls.clear()
    session.current_page = 1
    await plugin._on_state_change(session, "var.k", 2, 1)
    assert calls == [("_render_button", session, 5), ("_render_button", session, 1)]


@pytest.mark.asyncio
async def test_key_rerender_tolerates_a_buttons_section_that_is_not_a_list():
    config = {"buttons": {"index": 0}, "global_buttons": [
        {"index": 1, "label_source": "var.k"}]}
    plugin, _, session, calls = _reaction_rig(config)
    await plugin._on_state_change(session, "var.k", 1, 0)
    assert calls == [("_render_button", session, 1)]


# ──── A key press ────


def _press_rig(config, deck=None):
    plugin, state = _plugin(config)
    session = _session(plugin, deck if deck is not None else _Deck())
    calls = []
    _record(plugin, calls, "_render_button")
    _record(plugin, calls, "_execute_action")
    _record(plugin, calls, "_execute_actions")
    _record(plugin, calls, "_clear_overlay")

    async def _emit(name, payload=None):
        calls.append(("emit", name, payload))

    plugin.api.event_emit = _emit
    tasks = []

    def _create(coro_fn, interval_seconds, name=None):
        tasks.append({"fn": coro_fn, "interval": interval_seconds, "name": name})
        return f"task-{len(tasks)}"

    def _cancel(task_id):
        calls.append(("cancel_task", task_id))

    plugin.api.create_periodic_task = _create
    plugin.api.cancel_task = _cancel
    return plugin, state, session, calls, tasks


def _key(index=0, page=0, **press):
    return {"index": index, "page": page, "bindings": {"press": [press]}}


@pytest.mark.asyncio
async def test_release_always_ends_the_press_even_under_an_overlay():
    plugin, _, session, calls, _ = _press_rig({"buttons": [_key(action="macro", macro="m")]})
    session.hold_tasks[0] = "task-9"
    session.pressed_keys.add(0)
    session.overlay_active = True
    await plugin._on_key_change(session, session.deck, 0, False)
    assert calls == [("cancel_task", "task-9"), ("_render_button", session, 0)]
    assert session.pressed_keys == set() and session.hold_tasks == {}


@pytest.mark.asyncio
async def test_release_renders_only_a_key_that_was_held_on_a_visual_deck():
    plugin, _, session, calls, _ = _press_rig({})
    await plugin._on_key_change(session, session.deck, 0, False)
    assert ("_render_button", session, 0) not in calls
    plugin, _, session, calls, _ = _press_rig({}, deck=_Deck(visual=False))
    session.pressed_keys.add(0)
    await plugin._on_key_change(session, session.deck, 0, False)
    assert ("_render_button", session, 0) not in calls
    assert session.pressed_keys == set()


@pytest.mark.asyncio
async def test_a_failed_release_render_is_swallowed():
    plugin, _, session, calls, _ = _press_rig({})
    _record(plugin, calls, "_render_button", raises=RuntimeError("boom"))
    session.pressed_keys.add(1)
    await plugin._on_key_change(session, session.deck, 1, False)
    assert ("emit", "button.release", {"key": 1, "page": 0, "serial": "SN1"}) in calls


@pytest.mark.asyncio
async def test_an_overlay_swallows_the_press_that_dismisses_it():
    plugin, state, session, calls, _ = _press_rig({"buttons": [_key(action="macro", macro="m")]})
    session.overlay_active = True
    await plugin._on_key_change(session, session.deck, 0, True)
    assert calls == [("_clear_overlay", session)]
    assert state.get("plugin.streamdeck.SN1.last_input") is None
    assert session.pressed_keys == set()


@pytest.mark.asyncio
async def test_press_and_release_emit_with_page_and_serial_and_echo_on_press():
    plugin, state, session, calls, _ = _press_rig({})
    session.current_page = 2
    await plugin._on_key_change(session, session.deck, 3, True)
    await plugin._on_key_change(session, session.deck, 3, False)
    assert calls == [
        ("emit", "button.press", {"key": 3, "page": 2, "serial": "SN1"}),
        ("emit", "button.release", {"key": 3, "page": 2, "serial": "SN1"}),
    ]
    assert state.get("plugin.streamdeck.SN1.last_input") == "key:3:1"
    assert session.last_input > 0


@pytest.mark.asyncio
async def test_a_hidden_key_drops_its_press_state_and_fires_nothing():
    key = _key(action="macro", macro="m")
    key["bindings"]["visible_when"] = {"key": "var.show", "operator": "truthy"}
    plugin, _, session, calls, _ = _press_rig({"buttons": [key]})
    session.hold_tasks[0] = "task-3"
    session.press_times[0] = 1.0
    session.pressed_keys.add(0)  # a second press with no release between
    await plugin._on_key_change(session, session.deck, 0, True)
    assert calls == [
        ("emit", "button.press", {"key": 0, "page": 0, "serial": "SN1"}),
        ("cancel_task", "task-3"),
    ]
    assert session.hold_tasks == {} and session.press_times == {}
    assert session.pressed_keys == set()


@pytest.mark.asyncio
async def test_a_press_highlights_the_key_then_fires_every_tap_action():
    key = {"index": 1, "bindings": {"press": [
        {"action": "macro", "macro": "a"}, "x", {"action": "macro", "macro": "b"}]}}
    plugin, _, session, calls, _ = _press_rig({"buttons": [key]})
    await plugin._on_key_change(session, session.deck, 1, True)
    assert calls[1:] == [
        ("_render_button", session, 1),
        ("_execute_actions", session,
         [{"action": "macro", "macro": "a"}, {"action": "macro", "macro": "b"}],
         "key 1"),
    ]
    assert session.pressed_keys == {1}
    calls.clear()
    await plugin._on_key_change(session, session.deck, 1, False)
    assert [c[0] for c in calls] == ["_render_button", "emit"]


@pytest.mark.asyncio
async def test_a_press_on_a_non_visual_deck_marks_without_rendering():
    plugin, _, session, calls, _ = _press_rig(
        {"buttons": [_key(action="macro", macro="m")]}, deck=_Deck(visual=False))
    await plugin._on_key_change(session, session.deck, 0, True)
    assert session.pressed_keys == {0}
    assert [c[0] for c in calls] == ["emit", "_execute_actions"]


@pytest.mark.asyncio
async def test_no_press_binding_fires_nothing():
    for bindings in ({}, {"press": "x"}, {"press": []}, {"press": ["x"]}, "x"):
        plugin, _, session, calls, _ = _press_rig(
            {"buttons": [{"index": 0, "bindings": bindings}]})
        await plugin._on_key_change(session, session.deck, 0, True)
        assert [c[0] for c in calls] == ["emit", "_render_button"], bindings


@pytest.mark.asyncio
async def test_hold_repeat_starts_one_periodic_task_and_release_ends_it():
    press = {"mode": "hold_repeat", "hold_repeat_ms": 350,
             "action": "macro", "macro": "vol_up"}
    plugin, _, session, calls, tasks = _press_rig({"buttons": [_key(**press)]})
    session.hold_tasks[0] = "task-old"
    await plugin._on_key_change(session, session.deck, 0, True)
    assert ("cancel_task", "task-old") in calls
    assert len(tasks) == 1
    assert tasks[0]["interval"] == pytest.approx(0.35)
    assert tasks[0]["name"] == "hold_repeat_SN1_0"
    assert session.hold_tasks == {0: "task-1"}
    assert not any(c[0] == "_execute_action" for c in calls)
    await tasks[0]["fn"]()
    assert calls[-1] == ("_execute_action", session, press, "key 0")
    calls.clear()
    await plugin._on_key_change(session, session.deck, 0, False)
    assert calls[0] == ("cancel_task", "task-1")
    assert not any(c[0].startswith("_execute") for c in calls)
    assert session.hold_tasks == {}


@pytest.mark.asyncio
async def test_hold_repeat_defaults_to_200_ms():
    plugin, _, session, _, tasks = _press_rig(
        {"buttons": [_key(mode="hold_repeat", action="macro", macro="m")]})
    await plugin._on_key_change(session, session.deck, 0, True)
    assert tasks[0]["interval"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_toggle_fires_off_action_when_the_key_reads_on():
    off = {"action": "macro", "macro": "mute_off"}
    press = {"mode": "toggle", "toggle_key": "var.mute", "toggle_value": "ON",
             "action": "macro", "macro": "mute_on", "off_action": off}
    plugin, state, session, calls, _ = _press_rig({"buttons": [_key(**press)]})
    state.set("var.mute", "on", source="test")
    await plugin._on_key_change(session, session.deck, 0, True)
    assert calls[-1] == ("_execute_action", session, off, "key 0")
    state.set("var.mute", "off", source="test")
    await plugin._on_key_change(session, session.deck, 0, True)
    assert calls[-1] == ("_execute_action", session, press, "key 0")


@pytest.mark.asyncio
async def test_toggle_without_a_value_reads_truthiness():
    off = {"action": "macro", "macro": "off"}
    press = {"mode": "toggle", "toggle_key": "var.p", "action": "macro",
             "macro": "on", "off_action": off}
    plugin, state, session, calls, _ = _press_rig({"buttons": [_key(**press)]})
    for value, expected in ((1, off), (0, press), ("", press), ("x", off)):
        state.set("var.p", value, source="test")
        await plugin._on_key_change(session, session.deck, 0, True)
        assert calls[-1] == ("_execute_action", session, expected, "key 0"), value


@pytest.mark.asyncio
async def test_toggle_fires_press_without_a_usable_off_action_or_key():
    cases = [
        {"toggle_key": "var.p"},                              # no off_action
        {"toggle_key": "var.p", "off_action": "x"},           # not a dict
        {"toggle_key": "var.p", "off_action": {}},            # empty dict
        {"off_action": {"action": "macro", "macro": "off"}},  # no toggle key
    ]
    for extra in cases:
        press = {"mode": "toggle", "action": "macro", "macro": "on", **extra}
        plugin, state, session, calls, _ = _press_rig({"buttons": [_key(**press)]})
        state.set("var.p", True, source="test")
        await plugin._on_key_change(session, session.deck, 0, True)
        assert calls[-1] == ("_execute_action", session, press, "key 0"), extra


@pytest.mark.asyncio
async def test_toggle_release_does_nothing():
    press = {"mode": "toggle", "toggle_key": "var.p", "action": "macro", "macro": "on"}
    plugin, _, session, calls, _ = _press_rig({"buttons": [_key(**press)]})
    await plugin._on_key_change(session, session.deck, 0, False)
    assert [c[0] for c in calls] == ["emit"]


@pytest.mark.asyncio
async def test_toggle_with_labels_redraws_a_tenth_of_a_second_after_the_action(monkeypatch):
    sleeps = []

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(sd_module.asyncio, "sleep", _sleep)
    for labels in ({"on_label": "Muted"}, {"off_label": "Live"}):
        press = {"mode": "toggle", "toggle_key": "var.p", "action": "macro",
                 "macro": "on", **labels}
        plugin, _, session, calls, _ = _press_rig({"buttons": [_key(**press)]})
        sleeps.clear()
        await plugin._on_key_change(session, session.deck, 0, True)
        assert [c[0] for c in calls[-2:]] == ["_execute_action", "_render_button"]
        assert sleeps == [0.1]
    press = {"mode": "toggle", "toggle_key": "var.p", "action": "macro", "macro": "on",
             "on_label": "", "off_label": ""}
    plugin, _, session, calls, _ = _press_rig({"buttons": [_key(**press)]})
    sleeps.clear()
    await plugin._on_key_change(session, session.deck, 0, True)
    assert calls[-1][0] == "_execute_action"
    assert sleeps == []


@pytest.mark.asyncio
async def test_tap_hold_chooses_by_how_long_the_key_was_held():
    hold = {"action": "macro", "macro": "long"}
    press = {"mode": "tap_hold", "hold_threshold_ms": 400, "action": "macro",
             "macro": "short", "hold_action": hold}
    plugin, _, session, calls, _ = _press_rig({"buttons": [_key(**press)]})
    loop = asyncio.get_event_loop()
    await plugin._on_key_change(session, session.deck, 0, True)
    assert abs(session.press_times[0] - loop.time()) < 1.0
    assert not any(c[0].startswith("_execute") for c in calls)
    session.press_times[0] = loop.time() - 0.5
    await plugin._on_key_change(session, session.deck, 0, False)
    assert calls[-1] == ("_execute_action", session, hold, "key 0")
    assert session.press_times == {}
    session.press_times[0] = loop.time() - 0.2
    await plugin._on_key_change(session, session.deck, 0, False)
    assert calls[-1] == ("_execute_action", session, press, "key 0")


@pytest.mark.asyncio
async def test_tap_hold_defaults_and_fallbacks():
    loop = asyncio.get_event_loop()
    # Default threshold 500 ms.
    hold = {"action": "macro", "macro": "long"}
    press = {"mode": "tap_hold", "action": "macro", "macro": "short", "hold_action": hold}
    plugin, _, session, calls, _ = _press_rig({"buttons": [_key(**press)]})
    session.press_times[0] = loop.time() - 0.45
    await plugin._on_key_change(session, session.deck, 0, False)
    assert calls[-1] == ("_execute_action", session, press, "key 0")
    session.press_times[0] = loop.time() - 0.6
    await plugin._on_key_change(session, session.deck, 0, False)
    assert calls[-1] == ("_execute_action", session, hold, "key 0")
    # A long hold with no usable hold action fires the press.
    for hold_action in (None, "x", {}):
        press = {"mode": "tap_hold", "action": "macro", "macro": "short",
                 "hold_action": hold_action}
        plugin, _, session, calls, _ = _press_rig({"buttons": [_key(**press)]})
        session.press_times[0] = loop.time() - 5
        await plugin._on_key_change(session, session.deck, 0, False)
        assert calls[-1] == ("_execute_action", session, press, "key 0"), hold_action


@pytest.mark.asyncio
async def test_tap_hold_release_without_a_recorded_press_fires_nothing():
    press = {"mode": "tap_hold", "action": "macro", "macro": "short"}
    plugin, _, session, calls, _ = _press_rig({"buttons": [_key(**press)]})
    await plugin._on_key_change(session, session.deck, 0, False)
    assert not any(c[0].startswith("_execute") for c in calls)


@pytest.mark.asyncio
async def test_an_unknown_mode_fires_like_a_tap():
    press = {"mode": "bogus", "action": "macro", "macro": "m"}
    plugin, _, session, calls, _ = _press_rig({"buttons": [_key(**press)]})
    await plugin._on_key_change(session, session.deck, 0, True)
    assert calls[-1] == ("_execute_actions", session, [press], "key 0")


@pytest.mark.asyncio
async def test_a_locked_key_wins_over_the_page_entry_at_its_index():
    page_key = _key(index=0, action="macro", macro="page")
    config = {"buttons": [page_key],
              "global_buttons": [{"index": 0, "bindings": {"press": [
                  {"action": "macro", "macro": "locked"}]}}]}
    plugin, _, session, calls, _ = _press_rig(config)
    await plugin._on_key_change(session, session.deck, 0, True)
    assert calls[-1] == ("_execute_actions", session,
                         [{"action": "macro", "macro": "locked"}], "key 0")


# ──── What a key draws ────


def _render_rig(config, deck=None):
    plugin, state = _plugin(config)
    session = _session(plugin, deck if deck is not None else _Deck(keys=4, touch_keys=2))
    calls = []

    def _image(session_, look):
        image = {"label": look.label, "bg": look.bg_color, "fg": look.text_color,
                 "icon": look.icon, "value": look.value_text, "meter": look.meter}
        calls.append(("image", image))
        return image

    plugin._create_button_image = _image
    _record_sync(plugin, calls, "_apply_key_image")
    _record_sync(plugin, calls, "_apply_key_color")
    plugin._apply_nav_active = lambda image: ("nav", image)
    plugin._apply_press_highlight = lambda image: ("pressed", image)
    plugin._apply_macro_mark = lambda image, mark: ("mark", mark, image)
    return plugin, state, session, calls


def _drawn(calls):
    """The image the render set on the key, and the key index."""
    applied = [c for c in calls if c[0] == "_apply_key_image"]
    assert len(applied) == 1, calls
    return applied[0][3], applied[0][2]


@pytest.mark.asyncio
async def test_render_does_nothing_without_a_visual_deck_or_under_an_overlay():
    for deck in (None, _Deck(visual=False)):
        plugin, _, session, calls = _render_rig({})
        session.deck = deck
        await plugin._render_button(session, 0)
        assert calls == []
    plugin, _, session, calls = _render_rig({})
    session.overlay_active = True
    await plugin._render_button(session, 0)
    assert calls == []


@pytest.mark.asyncio
async def test_an_unassigned_key_draws_the_deck_colors_and_a_touch_key_goes_dark():
    plugin, _, session, calls = _render_rig({})
    await plugin._render_button(session, 0)
    image = {"label": "", "bg": "#1a1a2e", "fg": "#e0e0e0", "icon": None,
             "value": "", "meter": None}
    assert calls == [("image", image), ("_apply_key_image", session, 0, image)]
    calls.clear()
    await plugin._render_button(session, 4)
    assert calls == [("_apply_key_color", session, 4, "#000000")]


@pytest.mark.asyncio
async def test_deck_colors_come_from_the_deck_view_then_the_flat_config():
    config = {"button_color": "#111111", "text_color": "#222222",
              "decks": {"SN1": {"text_color": "#333333"}}}
    plugin, _, session, calls = _render_rig(config)
    await plugin._render_button(session, 0)
    image, index = _drawn(calls)
    assert (image["bg"], image["fg"], index) == ("#111111", "#333333", 0)


@pytest.mark.asyncio
async def test_a_hidden_key_draws_black_and_a_hidden_touch_key_goes_dark():
    hidden = {"visible_when": {"key": "var.show", "operator": "truthy"}}
    config = {"buttons": [{"index": 0, "label": "A", "bindings": hidden},
                          {"index": 4, "label": "T", "bindings": hidden}]}
    plugin, _, session, calls = _render_rig(config)
    await plugin._render_button(session, 0)
    image = {"label": "", "bg": "#000000", "fg": "#000000", "icon": None,
             "value": "", "meter": None}
    assert calls == [("image", image), ("_apply_key_image", session, 0, image)]
    calls.clear()
    await plugin._render_button(session, 4)
    assert calls == [("_apply_key_color", session, 4, "#000000")]


@pytest.mark.asyncio
async def test_a_visible_key_draws_its_label_icon_value_and_colors():
    config = {"buttons": [{
        "index": 1, "label": "Static", "icon": "mic", "bg_color": "#abcdef",
        "text_color": "", "label_source": "var.name", "value_source": "var.level",
        "unit": "dB", "bindings": {"visible_when": {"key": "var.show", "operator": "truthy"}},
    }]}
    plugin, state, session, calls = _render_rig(config)
    state.set("var.show", True, source="test")
    state.set("var.name", 42, source="test")
    state.set("var.level", -6, source="test")
    await plugin._render_button(session, 1)
    image, _ = _drawn(calls)
    assert image == {"label": "42", "bg": "#abcdef", "fg": "#e0e0e0", "icon": "mic",
                     "value": "-6 dB", "meter": None}


@pytest.mark.asyncio
async def test_the_value_line_unit_rules_and_an_unset_source():
    for unit, expected in (("%", "50%"), ("", "50"), (None, "50"), ("ms", "50 ms")):
        config = {"buttons": [{"index": 0, "label": "L", "icon": "",
                               "value_source": "var.v", "unit": unit,
                               "label_source": "var.unset"}]}
        plugin, state, session, calls = _render_rig(config)
        state.set("var.v", 50, source="test")
        await plugin._render_button(session, 0)
        image, _ = _drawn(calls)
        assert (image["value"], image["label"], image["icon"]) == (expected, "L", None)


@pytest.mark.asyncio
async def test_bindings_that_are_not_an_object_draw_the_entry_as_it_is():
    config = {"buttons": [{"index": 0, "label": "Plain", "icon": "mic",
                           "bg_color": "#123456", "value_source": "var.v",
                           "unit": "%", "bindings": ["not", "an", "object"]}]}
    plugin, state, session, calls = _render_rig(config)
    state.set("var.v", 7, source="test")
    await plugin._render_button(session, 0)
    image, _ = _drawn(calls)
    assert image == {"label": "Plain", "bg": "#123456", "fg": "#e0e0e0", "icon": "mic",
                     "value": "7%", "meter": None}


@pytest.mark.asyncio
async def test_an_unset_value_source_draws_no_value_line():
    config = {"buttons": [{"index": 0, "value_source": "var.unset", "unit": "dB",
                           "meter": {}}]}
    plugin, _, session, calls = _render_rig(config)
    await plugin._render_button(session, 0)
    image, _ = _drawn(calls)
    assert (image["value"], image["meter"]) == ("", None)


@pytest.mark.asyncio
async def test_a_key_meter_draws_only_when_enabled():
    config = {"buttons": [{"index": 0, "value_source": "var.v", "meter": {}},
                          {"index": 1, "value_source": "var.v"}]}
    plugin, state, session, calls = _render_rig(config)
    state.set("var.v", 25, source="test")
    await plugin._render_button(session, 0)
    await plugin._render_button(session, 1)
    images = [c[1] for c in calls if c[0] == "image"]
    assert images[0]["meter"] == (0.25, "#8ab493")
    assert images[1]["meter"] is None


@pytest.mark.asyncio
async def test_toggle_labels_follow_the_toggle_key():
    press = {"mode": "toggle", "toggle_key": "var.p", "toggle_value": "YES",
             "on_label": "On", "off_label": "Off"}
    config = {"buttons": [{"index": 0, "label": "Base", "bindings": {"press": [press]}}]}
    plugin, state, session, calls = _render_rig(config)
    labels = []
    for value in ("yes", "no", None):
        state.set("var.p", value, source="test")
        calls.clear()
        await plugin._render_button(session, 0)
        labels.append(_drawn(calls)[0]["label"])
    assert labels == ["On", "Off", "Off"]


@pytest.mark.asyncio
async def test_toggle_labels_need_the_mode_the_key_and_the_matching_label():
    cases = [
        ({"mode": "tap", "toggle_key": "var.p", "on_label": "On"}, True, "Base"),
        ({"mode": "toggle", "on_label": "On"}, True, "Base"),
        ({"mode": "toggle", "toggle_key": "var.p", "off_label": "Off"}, True, "Base"),
        ({"mode": "toggle", "toggle_key": "var.p", "on_label": "On"}, False, "Base"),
        ({"mode": "toggle", "toggle_key": "var.p", "on_label": "On"}, 1, "On"),
    ]
    for press, value, expected in cases:
        config = {"buttons": [{"index": 0, "label": "Base",
                               "bindings": {"press": press}}]}
        plugin, state, session, calls = _render_rig(config)
        state.set("var.p", value, source="test")
        await plugin._render_button(session, 0)
        assert _drawn(calls)[0]["label"] == expected, (press, value)
    # A non-dict first entry hides the toggle from the render.
    config = {"buttons": [{"index": 0, "label": "Base", "bindings": {"press": [
        "x", {"mode": "toggle", "toggle_key": "var.p", "on_label": "On"}]}}]}
    plugin, state, session, calls = _render_rig(config)
    state.set("var.p", True, source="test")
    await plugin._render_button(session, 0)
    assert _drawn(calls)[0]["label"] == "Base"


@pytest.mark.asyncio
async def test_feedback_styles_apply_after_the_toggle_label():
    press = {"mode": "toggle", "toggle_key": "var.p", "on_label": "On"}
    feedback = {"key": "var.p", "style_active": {"bg_color": "#ff0000", "icon": "x"},
                "label_active": "Feedback"}
    config = {"buttons": [{"index": 0, "label": "Base",
                           "bindings": {"press": [press], "feedback": feedback}}]}
    plugin, state, session, calls = _render_rig(config)
    state.set("var.p", True, source="test")
    await plugin._render_button(session, 0)
    image, _ = _drawn(calls)
    assert (image["label"], image["bg"], image["icon"]) == ("Feedback", "#ff0000", "x")


@pytest.mark.asyncio
async def test_nav_press_and_macro_marks_wrap_the_image_in_that_order():
    config = {"buttons": [
        {"index": 0, "page": 0, "bindings": {"press": [{"action": "navigate", "page": 0}]}},
        {"index": 1, "page": 0, "bindings": {"press": [{"action": "navigate", "page": "1"}]}},
        {"index": 2, "page": 0, "bindings": {"press": [{"action": "navigate",
                                                        "page": "__next_page__"}]}},
    ]}
    plugin, _, session, calls = _render_rig(config)
    session.pressed_keys = {0, 1}
    session.macro_marks = {(0, 0): "running", (None, 0): "error", (0, 2): "done"}
    for index in (0, 1, 2):
        await plugin._render_button(session, index)
    drawn = [c[3] for c in calls if c[0] == "_apply_key_image"]
    base = {"label": "", "bg": "#1a1a2e", "fg": "#e0e0e0", "icon": None,
            "value": "", "meter": None}
    assert drawn == [
        ("mark", "error", ("pressed", ("nav", base))),
        ("pressed", base),
        ("mark", "done", base),
    ]


@pytest.mark.asyncio
async def test_a_touch_key_shows_its_color_lightened_by_nav_and_by_press():
    config = {"buttons": [
        {"index": 4, "bg_color": "#000000",
         "bindings": {"press": [{"action": "navigate", "page": 0}]}},
        {"index": 5, "bg_color": "#000000"},
    ]}
    plugin, _, session, calls = _render_rig(config)
    await plugin._render_button(session, 4)
    await plugin._render_button(session, 5)
    session.pressed_keys = {4, 5}
    await plugin._render_button(session, 4)
    await plugin._render_button(session, 5)
    colors = [c[3] for c in calls if c[0] == "_apply_key_color"]
    assert colors == ["#333333", "#000000", "#666666", "#3f3f3f"]
    assert not any(c[0] == "image" for c in calls)


@pytest.mark.asyncio
async def test_a_macro_mark_replaces_a_touch_keys_color_and_skips_the_nav_lift():
    config = {"global_buttons": [
        {"index": 4, "bg_color": "#000000",
         "bindings": {"press": [{"action": "navigate", "page": 0}]}}]}
    plugin, _, session, calls = _render_rig(config)
    session.macro_marks = {(None, 4): "running"}
    await plugin._render_button(session, 4)
    session.macro_marks = {(None, 4): "bogus"}
    await plugin._render_button(session, 4)
    session.pressed_keys = {4}
    session.macro_marks = {(0, 4): "error"}
    await plugin._render_button(session, 4)
    colors = [c[3] for c in calls if c[0] == "_apply_key_color"]
    assert colors == ["#f59e0b", "#000000", "#f37272"]
