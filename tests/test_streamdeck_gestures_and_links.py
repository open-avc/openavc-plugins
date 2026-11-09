"""
Stream Deck: what a dial or the touch strip runs, how a network deck's link
is kept, what a macro's run marks on its keys, and which paths draw only on a
deck that has a screen.

Characterisation pins for four paths: the dial gestures (push, release, turn,
push-and-turn) and the touch strip's zones and events; the watchdog's network
deck tick (one link per declared deck, its status, the serials it records and
the one discovery look-up per outage) and the two writers that change a
``network_decks`` entry; the macro run marks (set, flashed, cleared, and which
page shows them); and every path that draws, each of which does nothing on a
deck with no screen.

No hardware: decks are stand-ins that answer only what these paths ask.

Run from the openavc-plugins root: pytest tests/test_streamdeck_gestures_and_links.py -v
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
from control_surfaces.streamdeck import net_deck  # noqa: E402

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


class _Evt:
    """The library's event enums, matched by name."""

    def __init__(self, name):
        self.name = name


TURN, PUSH = _Evt("TURN"), _Evt("PUSH")
SHORT, LONG, DRAG = _Evt("SHORT"), _Evt("LONG"), _Evt("DRAG")


class _Deck:
    """Answers the questions the dial and strip paths ask of a deck."""

    def __init__(self, dials=2, width=800, visual=True, touch=True):
        self._dials = dials
        self._width = width
        self._visual = visual
        self._touch = touch

    def is_visual(self):
        return self._visual

    def is_touch(self):
        return self._touch

    def dial_count(self):
        return self._dials

    def touchscreen_image_format(self):
        return {"size": (self._width, 100)}


class _Asked:
    """A deck with no screen that records anything else it is asked."""

    def __init__(self, touch=True):
        self.asked = []
        self._touch = touch

    def is_visual(self):
        return False

    def is_touch(self):
        return self._touch

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def _ask(*args, **kwargs):
            self.asked.append(name)
            raise AssertionError(f"a deck with no screen was asked {name}")

        return _ask


def _plugin(config=None):
    state = StateStore()
    events = EventBus()
    state.set_event_bus(events)
    macros = MockMacroEngine()
    logs = []
    api = PluginAPI(
        plugin_id="streamdeck",
        capabilities=StreamDeckPlugin.PLUGIN_INFO["capabilities"],
        config=config or {},
        registry=PluginRegistry("streamdeck"),
        state_store=state,
        event_bus=events,
        macro_engine=macros,
        device_manager=MockDeviceManager(),
        platform_id="test",
        log_fn=lambda pid, msg, level="info": logs.append((level, msg)),
    )
    plugin = StreamDeckPlugin()
    plugin.api = api
    plugin._logs = logs
    return plugin, state, macros


def _session(plugin, deck=None, serial="SN1"):
    session = sd_module._DeckSession(deck)
    session.serial = serial
    session.device_id = f"dev-{serial}"
    plugin._sessions[session.device_id] = session
    return session


def _record(target, calls, name, result=None):
    async def _rec(*args, **kwargs):
        calls.append((name,) + args + ((kwargs,) if kwargs else ()))
        return result

    setattr(target, name, _rec)


def _dial_rig(dial, state_values=None):
    plugin, state, macros = _plugin({"dials": [dict(dial, index=0)]})
    for key, value in (state_values or {}).items():
        state.set(key, value, source="test")
    session = _session(plugin)
    events = []
    _record(plugin.api, events, "event_emit")
    return plugin, state, macros, session, events


def _hold(session, dial, seconds):
    """Back-date a dial's push so its release reads as ``seconds`` held."""
    loop = asyncio.get_running_loop()
    session.dial_press_times[dial] = loop.time() - seconds


def _m(name):
    return [{"action": "macro", "macro": name}]


# ──── Dial: push, release, turn ────


@pytest.mark.asyncio
async def test_a_dial_push_and_turn_announce_themselves_with_the_deck_serial():
    plugin, _, _, session, events = _dial_rig({})
    await plugin._on_dial_event(session, None, 0, PUSH, True)
    await plugin._on_dial_event(session, None, 0, PUSH, False)
    await plugin._on_dial_event(session, None, 0, TURN, -3)
    assert events == [
        ("event_emit", "dial.press", {"dial": 0, "serial": "SN1"}),
        ("event_emit", "dial.turn", {"dial": 0, "amount": -3, "serial": "SN1"}),
    ]


@pytest.mark.asyncio
async def test_a_zero_turn_a_turn_that_is_no_number_and_an_unknown_event_do_nothing():
    plugin, _, macros, session, events = _dial_rig({"cw": _m("up"), "ccw": _m("down")})
    echoes = []
    _record(plugin, echoes, "_publish_input_echo")
    await plugin._on_dial_event(session, None, 0, TURN, 0)
    await plugin._on_dial_event(session, None, 0, TURN, "fast")
    await plugin._on_dial_event(session, None, 0, _Evt("SPIN"), 1)
    assert events == [] and macros.executed == []
    # The echo goes out for any turn, before the value is read.
    assert [e[2:] for e in echoes] == [("dial", 0), ("dial", 0)]


@pytest.mark.asyncio
async def test_a_turn_with_no_dial_entry_still_announces_itself():
    plugin, _, _ = _plugin({})
    session = _session(plugin)
    events = []
    _record(plugin.api, events, "event_emit")
    await plugin._on_dial_event(session, None, 4, TURN, 2)
    assert events == [("event_emit", "dial.turn", {"dial": 4, "amount": 2, "serial": "SN1"})]


@pytest.mark.asyncio
async def test_a_keyless_pressed_adjust_defers_the_push_but_a_turn_while_held_acts_normally():
    # Any dict defers the click; only one naming a key routes the turn.
    plugin, state, macros, session, _ = _dial_rig(
        {"press": _m("click"), "pressed_adjust": {},
         "adjust": {"key": "var.v", "step": 2}, "cw": _m("cw")},
        {"var.v": 10},
    )
    await plugin._on_dial_event(session, None, 0, PUSH, True)
    assert macros.executed == []
    await plugin._on_dial_event(session, None, 0, TURN, 1)
    assert state.get("var.v") == 12 and macros.executed == ["cw"]
    await plugin._on_dial_event(session, None, 0, PUSH, False)
    assert macros.executed == ["cw"]  # the turn made it a grip
    await plugin._on_dial_event(session, None, 0, PUSH, True)
    await plugin._on_dial_event(session, None, 0, PUSH, False)
    assert macros.executed == ["cw", "click"]


@pytest.mark.asyncio
async def test_pressed_ccw_alone_routes_every_turn_while_held():
    # A clockwise turn while held runs pressed_cw (none), not cw.
    plugin, _, macros, session, _ = _dial_rig(
        {"cw": _m("cw"), "ccw": _m("ccw"), "pressed_ccw": _m("p_ccw")}
    )
    await plugin._on_dial_event(session, None, 0, PUSH, True)
    await plugin._on_dial_event(session, None, 0, TURN, 2)
    assert macros.executed == []
    await plugin._on_dial_event(session, None, 0, TURN, -1)
    assert macros.executed == ["p_ccw"]
    await plugin._on_dial_event(session, None, 0, PUSH, False)
    await plugin._on_dial_event(session, None, 0, TURN, 1)
    assert macros.executed == ["p_ccw", "cw"]


@pytest.mark.asyncio
async def test_pressed_cw_actions_alone_route_with_no_adjust():
    plugin, state, macros, session, _ = _dial_rig(
        {"adjust": {"key": "var.v", "step": 5}, "pressed_cw": _m("fine")},
        {"var.v": 0},
    )
    await plugin._on_dial_event(session, None, 0, PUSH, True)
    await plugin._on_dial_event(session, None, 0, TURN, 1)
    assert macros.executed == ["fine"] and state.get("var.v") == 0


@pytest.mark.asyncio
async def test_a_pressed_adjust_and_pressed_actions_both_run_on_a_held_turn():
    plugin, state, macros, session, _ = _dial_rig(
        {"pressed_adjust": {"key": "var.v", "step": 1},
         "pressed_cw": _m("p_cw"), "adjust": {"key": "var.v", "step": 10}},
        {"var.v": 0},
    )
    await plugin._on_dial_event(session, None, 0, PUSH, True)
    await plugin._on_dial_event(session, None, 0, TURN, 2)
    assert state.get("var.v") == 2 and macros.executed == ["p_cw", "p_cw"]


@pytest.mark.asyncio
async def test_the_hold_threshold_defaults_to_half_a_second():
    plugin, _, macros, session, _ = _dial_rig(
        {"press": _m("short"), "long_press": _m("long")}
    )
    await plugin._on_dial_event(session, None, 0, PUSH, True)
    _hold(session, 0, 0.4)
    await plugin._on_dial_event(session, None, 0, PUSH, False)
    await plugin._on_dial_event(session, None, 0, PUSH, True)
    _hold(session, 0, 0.6)
    await plugin._on_dial_event(session, None, 0, PUSH, False)
    assert macros.executed == ["short", "long"]


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [0, -100, "nope", None])
async def test_a_hold_threshold_that_is_not_a_positive_number_is_half_a_second(threshold):
    plugin, _, macros, session, _ = _dial_rig(
        {"press": _m("short"), "long_press": _m("long"),
         "hold_threshold_ms": threshold}
    )
    await plugin._on_dial_event(session, None, 0, PUSH, True)
    _hold(session, 0, 0.1)
    await plugin._on_dial_event(session, None, 0, PUSH, False)
    assert macros.executed == ["short"]


@pytest.mark.asyncio
async def test_a_hold_threshold_given_as_text_is_read_as_a_number():
    plugin, _, macros, session, _ = _dial_rig(
        {"press": _m("short"), "long_press": _m("long"), "hold_threshold_ms": "50"}
    )
    await plugin._on_dial_event(session, None, 0, PUSH, True)
    _hold(session, 0, 0.1)
    await plugin._on_dial_event(session, None, 0, PUSH, False)
    assert macros.executed == ["long"]


@pytest.mark.asyncio
async def test_a_held_release_with_no_long_press_runs_the_press():
    plugin, _, macros, session, _ = _dial_rig(
        {"press": _m("short"), "pressed_cw": _m("p")}
    )
    await plugin._on_dial_event(session, None, 0, PUSH, True)
    _hold(session, 0, 2.0)
    await plugin._on_dial_event(session, None, 0, PUSH, False)
    assert macros.executed == ["short"]


@pytest.mark.asyncio
async def test_a_release_after_the_dial_stopped_deferring_runs_nothing():
    plugin, _, macros, session, _ = _dial_rig(
        {"press": _m("short"), "long_press": _m("long")}
    )
    await plugin._on_dial_event(session, None, 0, PUSH, True)
    plugin.api._update_config({"dials": [{"index": 0, "press": _m("short")}]})
    await plugin._on_dial_event(session, None, 0, PUSH, False)
    assert macros.executed == []
    assert session.dial_press_times == {} and session.dial_turned == set()


@pytest.mark.asyncio
async def test_a_second_push_with_no_release_between_starts_a_fresh_click():
    plugin, _, macros, session, _ = _dial_rig(
        {"press": _m("short"), "long_press": _m("long"), "cw": _m("cw")}
    )
    await plugin._on_dial_event(session, None, 0, PUSH, True)
    await plugin._on_dial_event(session, None, 0, TURN, 1)
    assert session.dial_turned == {0}
    await plugin._on_dial_event(session, None, 0, PUSH, True)
    assert session.dial_turned == set()
    await plugin._on_dial_event(session, None, 0, PUSH, False)
    assert macros.executed == ["cw", "short"]


@pytest.mark.asyncio
async def test_a_turn_with_no_push_recorded_is_an_ordinary_turn():
    plugin, _, macros, session, _ = _dial_rig(
        {"press": _m("short"), "cw": _m("cw"), "pressed_cw": _m("p_cw")}
    )
    await plugin._on_dial_event(session, None, 0, TURN, 1)
    assert macros.executed == ["cw"] and session.dial_turned == set()


# ──── Touch strip: zones and events ────


def _zones(dial, deck=None):
    plugin, _, _ = _plugin({"dials": [dict(dial, index=0)]})
    session = _session(plugin, deck or _Deck(dials=1, width=200))
    return plugin._touch_zones(session)


def test_a_dial_zone_reads_the_dial_fields_it_shows_and_runs():
    assert _zones({
        "label": "Vol", "icon": "volume", "unit": "%", "meter": {"style": "bar"},
        "adjust": {"key": "var.v", "step": 2}, "fader": 1,
        "press": _m("p"), "long_press": _m("lp"),
    }) == [{
        "label": "Vol", "icon": "volume", "unit": "%", "meter": {"style": "bar"},
        "value_source": "var.v", "touch": _m("p"), "long_touch": _m("lp"),
        "drag_adjust": {"key": "var.v", "step": 2, "fader": True},
        "x": 0, "w": 200, "index": 0,
    }]


def test_a_dial_zone_with_its_own_touch_entries_keeps_them_even_when_empty():
    zone = _zones({"press": _m("p"), "long_press": _m("lp"),
                   "touch": [], "long_touch": ""})[0]
    assert zone["touch"] == [] and zone["long_touch"] == ""


def test_a_dial_zone_with_no_live_adjust_has_no_drag():
    for adjust, source in (({"step": 5}, ""), ({"key": ""}, ""), ({"key": None}, None),
                           ("var.v", ""), (None, "")):
        zone = _zones({"adjust": adjust, "fader": True})[0]
        assert zone["drag_adjust"] is None
        assert zone["value_source"] == source
        assert zone["label"] == "" and zone["touch"] is None


def test_a_dial_zone_without_fader_drags_by_steps():
    zone = _zones({"adjust": {"key": "var.v"}, "fader": 0})[0]
    assert zone["drag_adjust"] == {"key": "var.v"}


def test_zones_with_no_deck_or_no_dials_are_none():
    plugin, _, _ = _plugin({"dials": [{"index": 0}]})
    assert plugin._touch_zones(_session(plugin, None)) == []
    assert plugin._touch_zones(_session(plugin, _Deck(dials=0), serial="SN2")) == []


def _touch_rig(zones, state_values=None):
    plugin, state, macros = _plugin({"touchscreen": {"zones": zones}})
    for key, value in (state_values or {}).items():
        state.set(key, value, source="test")
    session = _session(plugin, _Deck(dials=0, width=800))
    flashes, events = [], []
    _record(plugin, flashes, "_flash_touch_zone")
    _record(plugin.api, events, "event_emit")
    return plugin, state, macros, session, flashes, events


@pytest.mark.asyncio
async def test_a_long_touch_on_a_fader_zone_runs_its_actions_and_leaves_the_value():
    fader = {"key": "var.v", "min": 0, "max": 100, "fader": True}
    plugin, state, macros, session, _, _ = _touch_rig(
        [{"touch": _m("t"), "long_touch": _m("lt"), "drag_adjust": fader}],
        {"var.v": 5},
    )
    await plugin._on_touchscreen_event(session, None, LONG, {"x": 400})
    assert macros.executed == ["lt"] and state.get("var.v") == 5
    await plugin._on_touchscreen_event(session, None, SHORT, {"x": 400})
    assert macros.executed == ["lt"] and state.get("var.v") == 50


@pytest.mark.asyncio
async def test_a_drag_steps_one_detent_per_eight_pixels_toward_zero():
    plugin, state, _, session, flashes, _ = _touch_rig(
        [{"drag_adjust": {"key": "var.v", "step": 1}}], {"var.v": 0}
    )
    await plugin._on_touchscreen_event(session, None, DRAG, {"x": 100, "x_out": 123})
    assert state.get("var.v") == 2
    await plugin._on_touchscreen_event(session, None, DRAG, {"x": 100, "x_out": 93})
    assert state.get("var.v") == 2
    await plugin._on_touchscreen_event(session, None, DRAG, {"x": 100, "x_out": 84})
    assert state.get("var.v") == 0
    assert [f[2] for f in flashes] == [0, 0, 0]


@pytest.mark.asyncio
async def test_a_drag_on_a_zone_with_no_live_adjust_only_flashes():
    plugin, state, macros, session, flashes, events = _touch_rig(
        [{"touch": _m("t"), "drag_adjust": {"step": 1}}]
    )
    await plugin._on_touchscreen_event(session, None, DRAG, {"x": 10, "x_out": 300})
    assert [f[2] for f in flashes] == [0]
    assert macros.executed == [] and events == []


@pytest.mark.asyncio
async def test_a_drag_outside_every_zone_changes_nothing():
    plugin, state, _, session, flashes, _ = _touch_rig(
        [{"drag_adjust": {"key": "var.v", "step": 1}, "x": 0, "w": 100}], {"var.v": 0}
    )
    await plugin._on_touchscreen_event(session, None, DRAG, {"x": 300, "x_out": 20})
    assert flashes == [] and state.get("var.v") == 0


def test_a_strip_that_cannot_say_its_size_is_laid_out_as_800_pixels():
    class _NoFormat(_Deck):
        def touchscreen_image_format(self):
            raise OSError("closed")

    assert [(z["x"], z["w"]) for z in _zones({}, _NoFormat(dials=2))] == [(0, 400), (400, 400)]


@pytest.mark.asyncio
async def test_touch_events_the_strip_cannot_place_do_nothing():
    plugin, _, macros, session, flashes, events = _touch_rig([{"touch": _m("t")}])
    echoes = []
    _record(plugin, echoes, "_publish_input_echo")
    for kind, value in (
        (SHORT, "tap"), (SHORT, {"x": "left"}), (DRAG, "drag"),
        (DRAG, {"x": 10}), (DRAG, {"x": 10, "x_out": None}), (_Evt("PINCH"), {"x": 5}),
    ):
        await plugin._on_touchscreen_event(session, None, kind, value)
    assert macros.executed == [] and flashes == [] and events == []
    assert [e[2:] for e in echoes] == [("touch", 10), ("touch", 10)]


@pytest.mark.asyncio
async def test_a_tap_outside_every_zone_is_announced_and_runs_nothing():
    plugin, _, macros, session, flashes, events = _touch_rig(
        [{"touch": _m("t"), "x": 0, "w": 100}]
    )
    await plugin._on_touchscreen_event(session, None, SHORT, {"x": 500.7})
    assert events == [("event_emit", "touchscreen.touch", {"x": 500, "serial": "SN1"})]
    assert macros.executed == [] and flashes == []


@pytest.mark.asyncio
async def test_a_touch_while_an_overlay_shows_only_clears_it():
    plugin, _, macros, session, flashes, events = _touch_rig([{"touch": _m("t")}])
    session.overlay_active = True
    cleared = []
    _record(plugin, cleared, "_clear_overlay")
    await plugin._on_touchscreen_event(session, None, SHORT, {"x": 5})
    assert cleared == [("_clear_overlay", session)]
    assert macros.executed == [] and flashes == [] and events == []


# ──── Network decks: the watchdog's tick ────


class _Ensure:
    """Stands in for DockLink.ensure: the transports it reports and the
    status and failure count it leaves."""

    def __init__(self, status="connected", fails=0, transports=()):
        self.status = status
        self.fails = fails
        self.transports = list(transports)
        self.calls = []

    async def __call__(self, link, _loop, _log):
        self.calls.append(link.key)
        link.status = self.status
        link.fail_count = self.fails
        return self.transports


def _net_rig(monkeypatch, decks, ensure=None):
    plugin, state, _ = _plugin({"network_decks": decks})
    plugin._loop = asyncio.get_running_loop()
    ensure = ensure or _Ensure()
    monkeypatch.setattr(
        net_deck.DockLink, "ensure", lambda link, loop, log: ensure(link, loop, log)
    )
    saves, tasks = [], []

    async def save(config):
        saves.append(config["network_decks"])
        plugin.api._update_config(config)

    def create_task(coro, name=None):
        coro.close()
        tasks.append(name)

    plugin.api.save_config = save
    plugin.api.create_task = create_task

    async def browse(service_types, duration=5.0):
        return []

    plugin.api.mdns_browse = browse
    return plugin, state, ensure, saves, tasks


@pytest.mark.asyncio
async def test_a_new_link_starts_from_the_serials_its_entry_recorded(monkeypatch):
    plugin, state, ensure, saves, _ = _net_rig(monkeypatch, [
        {"host": " 192.0.2.1 ", "port": 6000, "serial": "OWN", "mdns_sn": "ALIAS"},
        {"host": "192.0.2.2"},
    ])
    await plugin._reconcile_network_decks([])
    first = plugin._dock_links["192.0.2.1:6000"]
    second = plugin._dock_links["192.0.2.2:5343"]
    assert (first.host, first.port) == ("192.0.2.1", 6000)
    assert (first.observed_serial, first.advertised_serial) == ("OWN", "ALIAS")
    assert (second.observed_serial, second.advertised_serial) == ("", "")
    assert ensure.calls == ["192.0.2.1:6000", "192.0.2.2:5343"]
    assert state.get("plugin.streamdeck.net.192_0_2_1_6000.address") == "192.0.2.1:6000"
    assert state.get("plugin.streamdeck.net.192_0_2_1_6000.status") == "connected"
    assert state.get("plugin.streamdeck.net.192_0_2_2_5343.status") == "connected"
    assert saves == []  # nothing new to record


@pytest.mark.asyncio
async def test_a_link_is_kept_across_ticks_and_dropped_when_its_entry_goes(monkeypatch):
    plugin, state, _, _, _ = _net_rig(monkeypatch, [{"host": "192.0.2.1"}])
    await plugin._reconcile_network_decks([])
    link = plugin._dock_links["192.0.2.1:5343"]
    await plugin._reconcile_network_decks([])
    assert plugin._dock_links["192.0.2.1:5343"] is link
    closed = []
    link.close = lambda: closed.append(link.key)
    plugin.api._update_config({"network_decks": [{"host": "192.0.2.9"}]})
    await plugin._reconcile_network_decks([])
    assert closed == ["192.0.2.1:5343"]
    assert list(plugin._dock_links) == ["192.0.2.9:5343"]
    assert state.get("plugin.streamdeck.net.192_0_2_1_5343.status") == "removed"


@pytest.mark.asyncio
@pytest.mark.parametrize("field, attr", [("serial", "observed_serial"),
                                         ("mdns_sn", "advertised_serial")])
async def test_a_serial_the_link_learned_is_recorded_on_its_entry(monkeypatch, field, attr):
    plugin, _, _, saves, _ = _net_rig(monkeypatch, [
        {"host": "192.0.2.1", "port": 6000, "label": "Lectern"},
        "not an entry",
        {"host": "192.0.2.1", "port": 6000},
        {"host": "192.0.2.3"},
    ])
    await plugin._reconcile_network_decks([])
    setattr(plugin._dock_links["192.0.2.1:6000"], attr, "LEARNED")
    await plugin._reconcile_network_decks([])
    assert saves == [[
        {"host": "192.0.2.1", "port": 6000, "label": "Lectern", field: "LEARNED"},
        "not an entry",
        {"host": "192.0.2.1", "port": 6000, field: "LEARNED"},
        {"host": "192.0.2.3"},
    ]]
    # Once recorded, the next tick has nothing to save.
    await plugin._reconcile_network_decks([])
    assert len(saves) == 1


@pytest.mark.asyncio
async def test_the_entry_address_the_writers_read_takes_the_default_port_for_a_bad_one(monkeypatch):
    # The watchdog skips an entry whose port is no number; the serial writer
    # reads it as the default port and records the serial there too.
    plugin, _, _, saves, _ = _net_rig(monkeypatch, [
        {"host": "192.0.2.1"}, {"host": "192.0.2.1 ", "port": "abc"},
        {"host": "192.0.2.1", "port": None}, {"host": "192.0.2.1", "port": 7000},
    ])
    await plugin._reconcile_network_decks([])
    assert list(plugin._dock_links) == ["192.0.2.1:5343", "192.0.2.1:7000"]
    plugin._dock_links["192.0.2.1:5343"].observed_serial = "S"
    await plugin._reconcile_network_decks([])
    assert [e.get("serial") for e in saves[0]] == ["S", "S", "S", None]


@pytest.mark.asyncio
async def test_an_entry_with_no_link_at_its_address_is_left_as_it_is(monkeypatch):
    plugin, _, _, saves, _ = _net_rig(monkeypatch, [
        {"host": "192.0.2.1"}, {"host": "192.0.2.1", "port": 0}, {"host": ""},
    ])
    await plugin._reconcile_network_decks([])
    plugin._dock_links["192.0.2.1:5343"].observed_serial = "S"
    await plugin._persist_network_serials()
    assert saves == [[{"host": "192.0.2.1", "serial": "S"},
                      {"host": "192.0.2.1", "port": 0}, {"host": ""}]]


@pytest.mark.asyncio
async def test_the_serial_writer_saves_nothing_without_a_list_or_a_change(monkeypatch):
    plugin, _, _, saves, _ = _net_rig(monkeypatch, [{"host": "192.0.2.1"}])
    link = net_deck.DockLink("192.0.2.1", 5343)
    plugin._dock_links[link.key] = link
    await plugin._persist_network_serials()
    link.observed_serial = "S"
    plugin.api._update_config({"network_decks": {"host": "192.0.2.1"}})
    await plugin._persist_network_serials()
    plugin.api._update_config({"network_decks": [{"host": "192.0.2.1", "serial": "S"}]})
    await plugin._persist_network_serials()
    assert saves == []


@pytest.mark.asyncio
async def test_a_deck_is_looked_up_once_from_its_third_failed_connect(monkeypatch):
    ensure = _Ensure(status="unreachable", fails=2)
    plugin, _, _, _, tasks = _net_rig(
        monkeypatch, [{"host": "192.0.2.1", "serial": "OWN"}], ensure
    )
    await plugin._reconcile_network_decks([])
    assert tasks == []
    ensure.fails = 3
    await plugin._reconcile_network_decks([])
    ensure.fails = 4
    await plugin._reconcile_network_decks([])
    assert tasks == ["net_reresolve_192_0_2_1_5343"]
    assert plugin._dock_links["192.0.2.1:5343"]._resolve_attempted is True


@pytest.mark.asyncio
async def test_the_look_up_asks_for_every_serial_the_deck_answers_to(monkeypatch):
    ensure = _Ensure(status="unreachable", fails=3)
    plugin, _, _, _, _ = _net_rig(
        monkeypatch, [{"host": "192.0.2.1", "serial": "OWN", "mdns_sn": "ALIAS"}], ensure
    )
    asked = []

    async def reresolve(key, serials):
        asked.append((key, serials))

    plugin._net_reresolve = reresolve
    plugin.api.create_task = lambda coro, name=None: asyncio.ensure_future(coro)
    await plugin._reconcile_network_decks([])
    link = plugin._dock_links["192.0.2.1:5343"]
    await asyncio.sleep(0)
    link._resolve_attempted = False
    link.observed_serial = "NEW"
    link.advertised_serial = ""
    await plugin._reconcile_network_decks([])
    await asyncio.sleep(0)
    assert asked == [
        ("192.0.2.1:5343", {"OWN", "ALIAS"}),
        ("192.0.2.1:5343", {"NEW", "OWN", "ALIAS"}),
    ]


@pytest.mark.asyncio
async def test_no_look_up_without_a_serial_or_without_discovery(monkeypatch):
    ensure = _Ensure(status="unreachable", fails=5)
    plugin, _, _, _, tasks = _net_rig(monkeypatch, [{"host": "192.0.2.1"}], ensure)
    await plugin._reconcile_network_decks([])
    assert tasks == []
    plugin.api._update_config({"network_decks": [{"host": "192.0.2.1", "serial": "S"}]})
    plugin.api.mdns_browse = None
    await plugin._reconcile_network_decks([])
    assert tasks == []
    assert plugin._dock_links["192.0.2.1:5343"]._resolve_attempted is False


# ──── Network decks: following a deck to its new address ────


def _browse(plugin, results=None, raises=None):
    async def browse(service_types, duration=5.0):
        assert service_types == [net_deck.NET_MDNS_SERVICE] and duration == 5.0
        if raises:
            raise raises
        return results

    plugin.api.mdns_browse = browse


def _found(ip, sn, port=5343):
    return {"ip": ip, "port": port, "txt": {"sn": sn}}


@pytest.mark.asyncio
async def test_a_deck_found_under_a_known_serial_moves_every_entry_at_its_address(monkeypatch):
    plugin, _, _, saves, _ = _net_rig(monkeypatch, [
        {"host": "192.0.2.1", "label": "A"}, {"host": "192.0.2.2"},
        {"host": " 192.0.2.1", "port": "x"}, 7,
    ])
    _browse(plugin, [{"ip": "192.0.2.50", "txt": {"sn": "S"}}])
    await plugin._net_reresolve("192.0.2.1:5343", {"S"})
    assert saves == [[
        {"host": "192.0.2.50", "port": 5343, "label": "A"}, {"host": "192.0.2.2"},
        {"host": "192.0.2.50", "port": 5343}, 7,
    ]]
    assert plugin._logs[-1] == (
        "info", "Network deck S answered at 192.0.2.50:5343 (was 192.0.2.1:5343); following it"
    )


@pytest.mark.asyncio
async def test_the_first_answer_under_a_known_serial_decides_even_at_the_same_address(monkeypatch):
    plugin, _, _, saves, _ = _net_rig(monkeypatch, [{"host": "192.0.2.1"}])
    _browse(plugin, [_found("192.0.2.1", "S"), _found("192.0.2.9", "S")])
    await plugin._net_reresolve("192.0.2.1:5343", {"S"})
    _browse(plugin, [{"ip": "", "txt": {"sn": "S"}}, _found("192.0.2.9", "S")])
    await plugin._net_reresolve("192.0.2.1:5343", {"S"})
    assert saves == []


@pytest.mark.asyncio
async def test_a_look_up_that_finds_nothing_usable_changes_nothing(monkeypatch):
    plugin, _, _, saves, _ = _net_rig(monkeypatch, [{"host": "192.0.2.1"}])
    _browse(plugin, [_found("192.0.2.5", "OTHER"), {"ip": "192.0.2.6"},
                     {"ip": "192.0.2.7", "txt": None}])
    await plugin._net_reresolve("192.0.2.1:5343", {"S"})
    _browse(plugin, [_found("192.0.2.5", "S")])
    await plugin._net_reresolve("192.0.2.3:5343", {"S"})  # no entry there
    plugin.api._update_config({"network_decks": "not a list"})
    await plugin._net_reresolve("192.0.2.1:5343", {"S"})
    assert saves == []


@pytest.mark.asyncio
async def test_a_look_up_that_fails_is_logged_and_changes_nothing(monkeypatch):
    plugin, _, _, saves, _ = _net_rig(monkeypatch, [{"host": "192.0.2.1"}])
    _browse(plugin, raises=OSError("no network"))
    await plugin._net_reresolve("192.0.2.1:5343", {"S"})
    assert saves == []
    assert plugin._logs[-1] == ("warning", "Network deck discovery failed: no network")


@pytest.mark.asyncio
async def test_a_serial_matches_as_text_and_a_new_port_is_taken_as_given(monkeypatch):
    plugin, _, _, saves, _ = _net_rig(monkeypatch, [{"host": "192.0.2.1"}])
    _browse(plugin, [{"ip": "192.0.2.8", "port": 6001, "txt": {"sn": 42}}])
    await plugin._net_reresolve("192.0.2.1:5343", {"42"})
    assert saves == [[{"host": "192.0.2.8", "port": 6001}]]


# ──── Macro run marks ────


def _mark_rig(config, page=0):
    plugin, _, _ = _plugin(config)
    session = _session(plugin, _Deck())
    session.current_page = page
    session.macro_keys = {"m": {(0, 1), (1, 2), (None, 3)}}
    renders = []
    _record(plugin, renders, "_render_button")
    return plugin, session, renders


def _rendered(renders):
    return sorted(r[2] for r in renders)


@pytest.mark.asyncio
async def test_a_mark_is_kept_for_every_key_and_drawn_on_those_showing():
    plugin, session, renders = _mark_rig({})
    await plugin._apply_macro_mark_event("m", "running")
    assert session.macro_marks == {(0, 1): "running", (1, 2): "running", (None, 3): "running"}
    assert _rendered(renders) == [1, 3]
    assert session.macro_clear_handles == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mark, delay", [("done", 0.8), ("error", 1.2)])
async def test_a_result_mark_flashes_and_clears_itself(mark, delay):
    plugin, session, renders = _mark_rig({})
    loop = asyncio.get_running_loop()
    await plugin._apply_macro_mark_event("m", mark)
    handles = session.macro_clear_handles
    assert set(handles) == {(0, 1), (1, 2), (None, 3)}
    for handle in handles.values():
        assert handle.when() - loop.time() == pytest.approx(delay, abs=0.05)


@pytest.mark.asyncio
async def test_a_new_mark_cancels_the_pending_clear():
    plugin, session, _ = _mark_rig({})
    await plugin._apply_macro_mark_event("m", "done")
    pending = dict(session.macro_clear_handles)
    await plugin._apply_macro_mark_event("m", "running")
    assert all(h.cancelled() for h in pending.values())
    assert session.macro_clear_handles == {}
    await plugin._apply_macro_mark_event("m", "error")
    errors = dict(session.macro_clear_handles)
    await plugin._apply_macro_mark_event("m", None)
    assert all(h.cancelled() for h in errors.values())
    assert session.macro_marks == {} and session.macro_clear_handles == {}


@pytest.mark.asyncio
async def test_a_mark_reaches_every_deck_that_shows_the_macro():
    plugin, session, renders = _mark_rig({})
    other = _session(plugin, _Deck(), serial="SN2")
    other.macro_keys = {}
    await plugin._apply_macro_mark_event("m", "running")
    await plugin._apply_macro_mark_event("unknown", "running")
    assert other.macro_marks == {}
    assert all(r[1] is session for r in renders)


@pytest.mark.asyncio
async def test_a_clear_redraws_a_key_showing_and_skips_one_on_another_page():
    plugin, session, renders = _mark_rig({}, page=1)
    session.macro_marks = {(0, 1): "done", (1, 2): "done", (None, 3): "error"}
    for page, key_index in ((0, 1), (1, 2), (None, 3)):
        await plugin._clear_macro_mark(session, page, key_index)
    assert _rendered(renders) == [2, 3] and session.macro_marks == {}
    renders.clear()
    await plugin._clear_macro_mark(session, 1, 2)  # nothing left to clear
    assert renders == []


@pytest.mark.asyncio
async def test_a_clear_for_a_deck_that_is_gone_leaves_its_marks():
    plugin, session, renders = _mark_rig({})
    session.macro_marks = {(0, 1): "done"}
    session.macro_clear_handles = {(0, 1): object()}
    del plugin._sessions[session.device_id]
    await plugin._clear_macro_mark(session, 0, 1)
    assert renders == [] and session.macro_marks == {(0, 1): "done"}
    assert session.macro_clear_handles == {}


def test_the_mark_a_key_shows_is_its_locked_mark_first():
    session = sd_module._DeckSession(None)
    session.current_page = 2
    session.macro_marks = {(None, 1): "error", (2, 1): "running", (2, 4): "done",
                           (0, 4): "running"}
    showing = StreamDeckPlugin._macro_mark_showing
    assert [showing(session, k) for k in (1, 4, 5)] == ["error", "done", None]


# ──── Only a deck with a screen is drawn on ────


def _blind_rig(config=None, touch=True):
    plugin, _, _ = _plugin(config or {})
    deck = _Asked(touch=touch)
    session = _session(plugin, deck)
    calls = []
    for name in ("_render_button", "_render_touchscreen", "_render_strip_zone",
                 "_render_info_strip", "_show_message", "_render_all_buttons"):
        _record(plugin, calls, name)
    for name in ("_apply_key_image", "_apply_key_color", "_mirror"):
        setattr(plugin, name, lambda *a, _n=name, **k: calls.append((_n,) + a))
    return plugin, session, deck, calls


@pytest.mark.asyncio
async def test_flashing_a_key_on_a_deck_with_no_screen_does_nothing():
    plugin, session, deck, calls = _blind_rig()
    await plugin._flash_key(session, 0, 2)
    assert deck.asked == [] and calls == []


@pytest.mark.asyncio
async def test_an_overlay_is_not_drawn_on_a_deck_with_no_screen(monkeypatch):
    pil = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(sd_module, "Image", pil)
    plugin, session, deck, calls = _blind_rig()
    await plugin._draw_overlay(session, "Hello")
    assert deck.asked == [] and calls == []


@pytest.mark.asyncio
async def test_the_clock_tick_draws_nothing_on_a_deck_with_no_screen():
    plugin, session, deck, calls = _blind_rig()
    await plugin._tick_clock(session)
    assert deck.asked == [] and calls == []
    assert session.clock_minute != -1


@pytest.mark.asyncio
async def test_the_strip_is_not_drawn_on_a_deck_with_no_screen():
    plugin, session, deck, calls = _blind_rig()
    plugin._render_touchscreen = StreamDeckPlugin._render_touchscreen.__get__(plugin)
    await plugin._render_touchscreen(session)
    assert deck.asked == [] and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [{}, {"touchscreen": {"zones": [{"label": "A"}]}}])
async def test_a_strip_zone_is_not_redrawn_on_a_deck_with_no_screen(config):
    plugin, session, deck, calls = _blind_rig(config)
    plugin._render_strip_zone = StreamDeckPlugin._render_strip_zone.__get__(plugin)
    session.strip_image = object()
    await plugin._render_strip_zone(session, 0)
    assert deck.asked == [] and calls == []


@pytest.mark.asyncio
async def test_the_info_screen_is_not_drawn_on_a_deck_with_no_screen():
    plugin, session, deck, calls = _blind_rig({"info_strip": {"label": "Hi"}})
    plugin._render_info_strip = StreamDeckPlugin._render_info_strip.__get__(plugin)
    await plugin._render_info_strip(session)
    assert deck.asked == [] and calls == []


@pytest.mark.asyncio
async def test_all_keys_are_not_drawn_on_a_deck_with_no_screen():
    plugin, session, deck, calls = _blind_rig()
    plugin._render_all_buttons = StreamDeckPlugin._render_all_buttons.__get__(plugin)
    await plugin._render_all_buttons(session)
    assert deck.asked == [] and calls == []


@pytest.mark.asyncio
async def test_a_touch_zone_flash_is_not_drawn_on_a_deck_with_no_screen():
    plugin, session, deck, calls = _blind_rig()
    await plugin._flash_touch_zone(session, 0)
    assert deck.asked == [] and calls == [] and session.flash_zones == {}


@pytest.mark.asyncio
async def test_a_message_goes_only_to_the_decks_with_a_screen():
    plugin, session, _, calls = _blind_rig()
    seen = _session(plugin, _Deck(), serial="SN2")
    await plugin._on_context_action("streamdeck.action.show_message", {"text": "Hi"})
    assert calls == [("_show_message", seen, "Hi", 5.0)]
    assert session.overlay_active is False


@pytest.mark.asyncio
async def test_identifying_a_deck_with_no_screen_does_nothing():
    plugin, session, deck, calls = _blind_rig()
    logs_before = list(plugin._logs)
    await plugin._identify_deck(session)
    assert deck.asked == [] and calls == [] and plugin._logs == logs_before
