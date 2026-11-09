"""
Stream Deck: a deck's life on the plugin, from start() to the deck going away.

Characterisation pins for the paths that open, keep and lose a deck: what
start() publishes before any deck is attached and the order it wires itself
up in; what an opened deck reports (its identity, its transport and the
geometry read from the hardware, each with its fallback) and what opening it
does, in order; what one watchdog tick checks, finds and opens; and what a
lost deck and a config change end (the hold repeats and the state
subscriptions).

No hardware: decks are stand-ins that answer only what these paths ask, and
the plugin's own drawing and brightness methods are replaced by recorders
where a pin is about the order of the steps, not their result.

Run from the openavc-plugins root: pytest tests/test_streamdeck_deck_lifecycle.py -v
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
from control_surfaces.streamdeck.streamdeck_plugin import StreamDeckPlugin  # noqa: E402
from control_surfaces.streamdeck.virtual_deck import VIRTUAL_MODELS, VirtualDeck  # noqa: E402

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


# ──── Helpers ────


def _plugin(config=None):
    """A plugin on a real PluginAPI and StateStore, start() not called."""
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
    """A deck that answers what opening it asks, recording each call."""

    def __init__(self, serial="SN1", *, key_count=6, layout=(2, 3), dials=0,
                 touch_keys=0, touch=False, info_size=None, visual=False,
                 model="Stream Deck Mini", hid=None):
        self.calls = []
        self.serial = serial
        self._key_count = key_count
        self._layout = layout
        self._dials = dials
        self._touch_keys = touch_keys
        self._touch = touch
        self._info_size = info_size
        self._visual = visual
        self._model = model
        self._hid = hid if hid is not None else f"hid-{serial}"
        self._open = False
        self._connected = True
        self.closed = False
        self.brightness = None
        self.callbacks = {}

    def _note(self, name):
        self.calls.append(name)

    def id(self):
        self._note("id")
        if isinstance(self._hid, Exception):
            raise self._hid
        return self._hid

    def open(self):
        self._note("open")
        self._open = True

    def reset(self):
        self._note("reset")

    def close(self):
        self._note("close")
        self._open = False
        self.closed = True

    def is_open(self):
        return self._open

    def connected(self):
        return self._connected

    def get_serial_number(self):
        self._note("get_serial_number")
        return self.serial

    def deck_type(self):
        self._note("deck_type")
        return self._model

    def key_layout(self):
        self._note("key_layout")
        if isinstance(self._layout, Exception):
            raise self._layout
        return self._layout

    def screen_image_format(self):
        self._note("screen_image_format")
        if self._info_size is None:
            raise AttributeError("no info screen")
        return {"size": self._info_size}

    def is_visual(self):
        self._note("is_visual")
        if isinstance(self._visual, Exception):
            raise self._visual
        return self._visual

    def key_count(self):
        self._note("key_count")
        return self._key_count

    def dial_count(self):
        self._note("dial_count")
        return self._dials

    def touch_key_count(self):
        self._note("touch_key_count")
        return self._touch_keys

    def is_touch(self):
        self._note("is_touch")
        return self._touch

    def set_brightness(self, level):
        self._note("set_brightness")
        self.brightness = level

    def set_key_callback_async(self, cb, loop=None):
        self.callbacks["key"] = cb

    def set_dial_callback_async(self, cb, loop=None):
        self.callbacks["dial"] = cb

    def set_touchscreen_callback_async(self, cb, loop=None):
        self.callbacks["touch"] = cb


class _Manager:
    def __init__(self, decks, error=None):
        self.decks = decks
        self.error = error

    def enumerate(self):
        if self.error is not None:
            raise self.error
        return list(self.decks)


class _Library:
    """Stands in for the StreamDeck module: DeviceManager().enumerate()."""

    def __init__(self, decks=(), error=None):
        self.manager = _Manager(list(decks), error)

    def DeviceManager(self):
        return self.manager


def _record_steps(plugin, steps, names):
    """Replace each named async plugin method with one that only records
    ``(name, session serial)`` in ``steps``."""
    for name in names:
        async def recorder(session, *args, _name=name):
            steps.append((_name, getattr(session, "serial", session)))
        setattr(plugin, name, recorder)


# ──── start(): what is published before any deck, and the wiring order ────


class _StartAPI:
    """Records what start() asks of the platform, in order."""

    plugin_id = "streamdeck"

    def __init__(self):
        self.calls = []
        self.config = {}

    async def state_set(self, key, value):
        self.calls.append(("state_set", key, value))

    async def event_subscribe(self, pattern, handler):
        self.calls.append(("event_subscribe", pattern, handler.__name__))

    def register_router(self, router):
        self.calls.append(("register_router",))

    def create_periodic_task(self, fn, interval_seconds, name):
        self.calls.append(("create_periodic_task", fn.__name__, interval_seconds, name))

    def log(self, message, level="info"):
        self.calls.append(("log", level, message))


def _start_rig(monkeypatch, *, import_error=None, enumerate_error=None):
    api = _StartAPI()
    plugin = StreamDeckPlugin()

    def fake_import():
        if import_error is not None:
            raise import_error

    monkeypatch.setattr(sd_module, "_lazy_import", fake_import)
    monkeypatch.setattr(sd_module, "StreamDeck", _Library(error=enumerate_error))
    monkeypatch.setattr(plugin, "_build_ext_router", lambda: object())
    return plugin, api


@pytest.mark.asyncio
async def test_start_publishes_the_unconnected_deck_keys_in_order(monkeypatch):
    plugin, api = _start_rig(monkeypatch)
    await plugin.start(api)
    published = [c[1:] for c in api.calls if c[0] == "state_set"]
    assert published == [
        ("connected", False),
        ("model", ""),
        ("serial", ""),
        ("current_page", 0),
        ("key_count", 0),
        ("rows", 0),
        ("columns", 0),
        ("dial_count", 0),
        ("touch_key_count", 0),
        ("has_touchscreen", False),
        ("has_info_screen", False),
        ("deck_count", 0),
        ("deck_serials", ""),
    ]


@pytest.mark.asyncio
async def test_start_never_publishes_visual_virtual_or_transport(monkeypatch):
    """Those three are published only with a deck (its geometry), never as
    an unconnected value."""
    plugin, api = _start_rig(monkeypatch)
    await plugin.start(api)
    keys = {c[1] for c in api.calls if c[0] == "state_set"}
    assert not keys & {"visual", "virtual", "transport"}


@pytest.mark.asyncio
async def test_start_wires_up_in_order(monkeypatch):
    plugin, api = _start_rig(monkeypatch)
    await plugin.start(api)
    wiring = [c for c in api.calls if c[0] not in ("state_set", "log")]
    assert wiring == [
        ("event_subscribe", "plugin.streamdeck.action.*", "_on_context_action"),
        ("register_router",),
        ("event_subscribe", "macro.*", "_on_macro_event"),
        ("create_periodic_task", "_watchdog", 3, "deck_watchdog"),
    ]
    # The unconnected keys go out before anything is wired.
    first_wire = api.calls.index(wiring[0])
    last_state = max(i for i, c in enumerate(api.calls) if c[0] == "state_set")
    assert last_state < first_wire


@pytest.mark.asyncio
async def test_start_loads_both_fonts_before_publishing(monkeypatch):
    plugin, api = _start_rig(monkeypatch)
    order = []
    monkeypatch.setattr(plugin, "_load_icon_font", lambda: order.append("icons"))
    monkeypatch.setattr(plugin, "_load_text_font", lambda: order.append("text"))
    await plugin.start(api)
    assert order == ["icons", "text"]
    assert plugin._loop is not None


@pytest.mark.asyncio
async def test_start_refuses_a_missing_hid_library_at_import(monkeypatch):
    plugin, api = _start_rig(monkeypatch, import_error=ImportError("No module named hid"))
    with pytest.raises(sd_module.PluginRefusal) as caught:
        await plugin.start(api)
    assert str(caught.value) == plugin._hidapi_error_message()
    assert api.calls == []


@pytest.mark.asyncio
async def test_start_refuses_a_hidapi_import_error_by_its_word(monkeypatch):
    plugin, api = _start_rig(monkeypatch, import_error=ImportError("HIDAPI missing"))
    with pytest.raises(sd_module.PluginRefusal) as caught:
        await plugin.start(api)
    assert str(caught.value) == plugin._hidapi_error_message()


@pytest.mark.asyncio
async def test_start_refuses_any_other_import_error_with_the_package_names(monkeypatch):
    plugin, api = _start_rig(monkeypatch, import_error=ImportError("No module named PIL"))
    with pytest.raises(sd_module.PluginRefusal) as caught:
        await plugin.start(api)
    assert str(caught.value) == (
        "Failed to import Stream Deck library. "
        "Make sure the 'streamdeck' and 'pillow' packages are installed."
    )
    assert isinstance(caught.value.__cause__, ImportError)


@pytest.mark.asyncio
async def test_start_refuses_when_enumerate_cannot_load_hidapi(monkeypatch):
    plugin, api = _start_rig(monkeypatch, enumerate_error=OSError("Unable to load hidapi"))
    with pytest.raises(sd_module.PluginRefusal) as caught:
        await plugin.start(api)
    assert str(caught.value) == plugin._hidapi_error_message()
    # The unconnected keys were published first; nothing was wired.
    assert [c[0] for c in api.calls if c[0] != "log"] == ["state_set"] * 13


@pytest.mark.asyncio
async def test_start_lets_any_other_enumerate_error_through(monkeypatch):
    error = RuntimeError("bus busy")
    plugin, api = _start_rig(monkeypatch, enumerate_error=error)
    with pytest.raises(RuntimeError) as caught:
        await plugin.start(api)
    assert caught.value is error
    assert not any(c[0] == "event_subscribe" for c in api.calls)


# ──── Opening a deck: what it reports ────


@pytest.mark.asyncio
async def test_open_reports_a_usb_deck_and_its_geometry():
    plugin, state, _logs = _plugin()
    deck = _Deck("USB1", key_count=6, layout=(2, 3), info_size=(0, 0))
    await plugin._open_deck(deck)
    session = plugin._sessions["hid-USB1"]
    assert session.deck is deck
    assert (session.serial, session.model) == ("USB1", "Stream Deck Mini")
    assert (session.is_virtual, session.is_network) == (False, False)
    assert (session.transport, session.address) == ("usb", "")
    assert session.geometry == {
        "key_count": 6,
        "rows": 2,
        "columns": 3,
        "dial_count": 0,
        "touch_key_count": 0,
        "has_touchscreen": False,
        "has_info_screen": False,
        "visual": False,
        "virtual": False,
        "transport": "usb",
    }
    for key, value in session.geometry.items():
        assert state.get(f"plugin.streamdeck.{key}") == value
        assert state.get(f"plugin.streamdeck.USB1.{key}") == value


@pytest.mark.asyncio
async def test_open_reads_the_deck_in_order():
    plugin, _state, _logs = _plugin()
    deck = _Deck("ORD", info_size=(248, 58))
    await plugin._open_deck(deck)
    first = deck.calls[:13]
    assert first == [
        "open", "reset", "id", "get_serial_number", "deck_type",
        "key_layout", "screen_image_format", "is_visual",
        "key_count", "dial_count", "touch_key_count", "is_touch",
        "set_brightness",
    ]


@pytest.mark.asyncio
async def test_open_reads_an_info_screen_from_its_width():
    plugin, _state, _logs = _plugin()
    await plugin._open_deck(_Deck("NEO", info_size=(248, 58)))
    assert plugin._sessions["hid-NEO"].geometry["has_info_screen"] is True
    await plugin._open_deck(_Deck("ZERO", info_size=(0, 58)))
    assert plugin._sessions["hid-ZERO"].geometry["has_info_screen"] is False


@pytest.mark.asyncio
async def test_open_treats_an_unreadable_screen_flag_as_visual():
    plugin, _state, _logs = _plugin()
    deck = _Deck("VIS", visual=RuntimeError("no answer"))
    # Opening a "visual" deck draws; keep the drawing out of this pin.
    steps = []
    _record_steps(plugin, steps, [
        "_render_all_buttons", "_render_touchscreen", "_render_info_strip",
    ])
    await plugin._open_deck(deck)
    assert plugin._sessions["hid-VIS"].geometry["visual"] is True


@pytest.mark.asyncio
async def test_open_names_a_deck_without_an_id_by_its_object():
    plugin, _state, _logs = _plugin()
    deck = _Deck("NOID", hid=RuntimeError("no path"))
    await plugin._open_deck(deck)
    assert list(plugin._sessions) == [f"deck-{id(deck)}"]


@pytest.mark.asyncio
async def test_open_calls_a_deck_with_no_serial_unknown():
    plugin, state, _logs = _plugin()
    deck = _Deck("")
    deck._hid = "hid-blank"
    await plugin._open_deck(deck)
    assert plugin._sessions["hid-blank"].serial == "unknown"
    assert state.get("plugin.streamdeck.unknown.connected") is True


@pytest.mark.asyncio
async def test_open_reports_a_virtual_deck():
    plugin, _state, _logs = _plugin()
    deck = VirtualDeck("Stream Deck Mini", "VIRT1", VIRTUAL_MODELS["Stream Deck Mini"])
    steps = []
    _record_steps(plugin, steps, [
        "_render_all_buttons", "_render_touchscreen", "_render_info_strip",
    ])
    await plugin._open_deck(deck)
    session = plugin._sessions["virtual:VIRT1"]
    assert (session.is_virtual, session.is_network) == (True, False)
    assert (session.transport, session.address) == ("virtual", "")
    assert session.geometry["virtual"] is True
    assert session.geometry["transport"] == "virtual"


def _network_deck(serial, link, address="10.0.0.9:5343"):
    deck = _Deck(serial)
    deck.device = SimpleNamespace(is_network=True, address=address, link=link)
    return deck


@pytest.mark.asyncio
async def test_open_reports_a_network_deck_and_tells_its_link_the_serial():
    plugin, state, _logs = _plugin()
    link = SimpleNamespace(observed_serial=None)
    deck = _network_deck("NET1", link)
    await plugin._open_deck(deck)
    session = plugin._sessions["hid-NET1"]
    assert (session.is_virtual, session.is_network) == (False, True)
    assert (session.transport, session.address) == ("network", "10.0.0.9:5343")
    assert session.geometry["transport"] == "network"
    assert link.observed_serial == "NET1"
    assert state.get("plugin.streamdeck.NET1.address") == "10.0.0.9:5343"


@pytest.mark.asyncio
async def test_open_tells_the_link_nothing_for_an_unknown_serial():
    plugin, _state, _logs = _plugin()
    link = SimpleNamespace(observed_serial="KEEP")
    deck = _network_deck("", link)
    await plugin._open_deck(deck)
    assert link.observed_serial == "KEEP"


@pytest.mark.asyncio
async def test_open_tells_the_link_nothing_for_the_word_unknown():
    plugin, _state, _logs = _plugin()
    link = SimpleNamespace(observed_serial="KEEP")
    deck = _network_deck("unknown", link)
    await plugin._open_deck(deck)
    assert link.observed_serial == "KEEP"


@pytest.mark.asyncio
async def test_open_of_a_network_deck_without_a_link_still_opens():
    plugin, _state, _logs = _plugin()
    deck = _network_deck("NOLINK", None)
    await plugin._open_deck(deck)
    assert plugin._sessions["hid-NOLINK"].is_network is True


@pytest.mark.asyncio
async def test_open_reads_a_device_that_is_not_networked_as_usb():
    plugin, _state, _logs = _plugin()
    deck = _Deck("LOCAL")
    deck.device = SimpleNamespace(is_network=False, address="ignored")
    await plugin._open_deck(deck)
    session = plugin._sessions["hid-LOCAL"]
    assert (session.is_network, session.transport, session.address) == (False, "usb", "")


@pytest.mark.asyncio
async def test_an_open_that_fails_on_the_layout_registers_nothing_but_has_told_the_link():
    plugin, state, _logs = _plugin()
    link = SimpleNamespace(observed_serial=None)
    deck = _network_deck("BROKE", link)
    deck._layout = RuntimeError("layout unreadable")
    with pytest.raises(RuntimeError, match="layout unreadable"):
        await plugin._open_deck(deck)
    assert plugin._sessions == {}
    assert link.observed_serial == "BROKE"
    assert state.get("plugin.streamdeck.BROKE.connected") is None


# ──── Opening a deck: what it does, in order ────


@pytest.mark.asyncio
async def test_open_wires_the_dial_and_touch_callbacks_only_where_the_deck_has_them():
    plugin, _state, _logs = _plugin()
    keys_only = _Deck("K")
    await plugin._open_deck(keys_only)
    assert sorted(keys_only.callbacks) == ["key"]

    plus = _Deck("P", key_count=8, layout=(2, 4), dials=4, touch=True)
    await plugin._open_deck(plus)
    assert sorted(plus.callbacks) == ["dial", "key", "touch"]
    session = plugin._sessions["hid-P"]
    for name in ("key", "dial", "touch"):
        assert plus.callbacks[name].args == (session,)
    assert plus.callbacks["key"].func == plugin._on_key_change
    assert plus.callbacks["dial"].func == plugin._on_dial_event
    assert plus.callbacks["touch"].func == plugin._on_touchscreen_event

    touch_only = _Deck("T", touch=True)
    await plugin._open_deck(touch_only)
    assert sorted(touch_only.callbacks) == ["key", "touch"]


@pytest.mark.asyncio
async def test_open_sets_the_brightness_and_starts_the_idle_clock():
    plugin, _state, _logs = _plugin({"brightness": 42})
    deck = _Deck("B")
    await plugin._open_deck(deck)
    assert deck.brightness == 42
    assert plugin._sessions["hid-B"].last_input > 0


@pytest.mark.asyncio
async def test_open_logs_the_deck_in_one_line():
    plugin, _state, logs = _plugin()
    await plugin._open_deck(_Deck("L1", key_count=6, layout=(2, 3)))
    await plugin._open_deck(
        _Deck("L2", key_count=8, layout=(2, 4), dials=4, touch=True, touch_keys=2,
              model="Stream Deck +")
    )
    await plugin._open_deck(_Deck("L3", touch_keys=2))
    lines = [m for level, m in logs if m.startswith("Connected to")]
    assert lines == [
        "Connected to Stream Deck Mini (S/N: L1, 6 keys, 2x3, 0 dials)",
        "Connected to Stream Deck + (S/N: L2, 8 keys, 2x4, 4 dials, "
        "touchscreen, 2 touch keys)",
        "Connected to Stream Deck Mini (S/N: L3, 6 keys, 2x3, 0 dials, 2 touch keys)",
    ]


@pytest.mark.asyncio
async def test_open_runs_its_steps_in_order():
    plugin, _state, _logs = _plugin()
    steps = []
    names = [
        "_setup_feedback_subscriptions", "_evaluate_auto_page",
        "_render_all_buttons", "_render_touchscreen", "_render_info_strip",
    ]
    _record_steps(plugin, steps, names)

    async def publish():
        steps.append(("_publish_deck_state", sorted(plugin._sessions)))
    plugin._publish_deck_state = publish

    async def brightness(session):
        steps.append(("_current_brightness_level", sorted(plugin._sessions)))
        return 50
    plugin._current_brightness_level = brightness

    emitted = []

    async def emit(event, payload=None):
        emitted.append((event, payload))
        steps.append(("event_emit", event))
    plugin.api.event_emit = emit

    await plugin._open_deck(_Deck("STEP"))
    assert steps == [
        ("_current_brightness_level", ["hid-STEP"]),
        ("_publish_deck_state", ["hid-STEP"]),
        ("event_emit", "connected"),
        ("_setup_feedback_subscriptions", "STEP"),
        ("_evaluate_auto_page", "STEP"),
        ("_render_all_buttons", "STEP"),
        ("_render_touchscreen", "STEP"),
        ("_render_info_strip", "STEP"),
    ]
    assert emitted == [("connected", {"model": "Stream Deck Mini", "serial": "STEP"})]


@pytest.mark.asyncio
async def test_open_shows_the_auto_page_before_the_first_render():
    config = {
        "buttons": [{"page": 0, "key": 0, "label": "a"}, {"page": 2, "key": 0, "label": "c"}],
        "page_names": ["A", "B", "C"],
        "auto_page": [{"when": {"key": "var.mode", "operator": "eq", "value": "c"}, "page": 2}],
    }
    plugin, state, _logs = _plugin(config)
    state.set("var.mode", "c")
    seen = []

    async def render(session):
        seen.append(session.current_page)
    plugin._render_all_buttons = render
    await plugin._open_deck(_Deck("AUTO"))
    assert seen == [2]
    assert state.get("plugin.streamdeck.AUTO.current_page") == 2
    assert state.get("plugin.streamdeck.current_page") == 2


@pytest.mark.asyncio
async def test_open_of_a_second_deck_leaves_the_primary_page_key_alone():
    config = {
        "buttons": [{"page": 0, "key": 0, "label": "a"}, {"page": 1, "key": 0, "label": "b"}],
        "page_names": ["A", "B"],
        "auto_page": [{"when": {"key": "var.mode", "operator": "eq", "value": "b"}, "page": 1}],
    }
    plugin, state, _logs = _plugin(config)
    await plugin._open_deck(_Deck("FIRST"))
    assert state.get("plugin.streamdeck.current_page") == 0
    state.set("var.mode", "b")

    async def publish():
        return None
    plugin._publish_deck_state = publish
    await plugin._open_deck(_Deck("SECOND"))
    assert plugin._sessions["hid-SECOND"].current_page == 1
    assert state.get("plugin.streamdeck.SECOND.current_page") == 1
    assert state.get("plugin.streamdeck.current_page") == 0


@pytest.mark.asyncio
async def test_open_with_no_auto_page_keeps_page_zero():
    plugin, _state, _logs = _plugin()
    page_writes = []
    original = plugin.api.state_set

    async def spy(key, value):
        if key.endswith("current_page"):
            page_writes.append(key)
        await original(key, value)
    plugin.api.state_set = spy

    async def publish():
        return None
    plugin._publish_deck_state = publish
    await plugin._open_deck(_Deck("NOAUTO"))
    assert plugin._sessions["hid-NOAUTO"].current_page == 0
    assert page_writes == []


# ──── One watchdog tick ────


def _watch_rig(monkeypatch, config=None, decks=(), enumerate_error=None):
    plugin, state, logs = _plugin(config)
    library = _Library(decks, enumerate_error)
    monkeypatch.setattr(sd_module, "StreamDeck", library)
    return plugin, state, logs, library


@pytest.mark.asyncio
async def test_a_tick_while_opening_does_nothing(monkeypatch):
    plugin, _state, _logs, library = _watch_rig(monkeypatch, decks=[_Deck("W")])
    plugin._opening = True
    await plugin._watchdog()
    assert plugin._sessions == {}


@pytest.mark.asyncio
async def test_a_healthy_deck_gets_its_idle_dim_then_its_clock(monkeypatch):
    plugin, _state, _logs, library = _watch_rig(monkeypatch, decks=[_Deck("H")])
    await plugin._watchdog()
    steps = []
    _record_steps(plugin, steps, ["_check_idle_dim", "_tick_clock", "_handle_deck_lost"])
    await plugin._watchdog()
    assert steps == [("_check_idle_dim", "H"), ("_tick_clock", "H")]


@pytest.mark.parametrize("breakage", ["closed", "disconnected", "raises"])
@pytest.mark.asyncio
async def test_an_unhealthy_deck_is_lost(monkeypatch, breakage):
    deck = _Deck("U")
    plugin, _state, _logs, library = _watch_rig(monkeypatch, decks=[deck])
    await plugin._watchdog()
    library.manager.decks = []
    if breakage == "closed":
        deck._open = False
    elif breakage == "disconnected":
        deck._connected = False
    else:
        def broken():
            raise OSError("gone")
        deck.connected = broken
    steps = []
    _record_steps(plugin, steps, ["_check_idle_dim", "_tick_clock", "_handle_deck_lost"])
    await plugin._watchdog()
    assert steps == [("_handle_deck_lost", "U")]


@pytest.mark.asyncio
async def test_a_virtual_deck_no_longer_declared_is_lost(monkeypatch):
    config = {"virtual_decks": [{"model": "Stream Deck Mini", "serial": "V1"}]}
    plugin, _state, _logs, library = _watch_rig(monkeypatch, config)
    steps = []
    _record_steps(plugin, [], [
        "_render_all_buttons", "_render_touchscreen", "_render_info_strip",
    ])
    await plugin._watchdog()
    assert list(plugin._sessions) == ["virtual:V1"]
    plugin.api._update_config({})
    _record_steps(plugin, steps, ["_check_idle_dim", "_tick_clock", "_handle_deck_lost"])
    await plugin._watchdog()
    assert steps == [("_handle_deck_lost", "V1")]


@pytest.mark.asyncio
async def test_a_declared_virtual_deck_stays_while_declared(monkeypatch):
    config = {"virtual_decks": [{"model": "Stream Deck Mini", "serial": "V2"}]}
    plugin, _state, _logs, library = _watch_rig(monkeypatch, config)
    _record_steps(plugin, [], [
        "_render_all_buttons", "_render_touchscreen", "_render_info_strip",
    ])
    await plugin._watchdog()
    steps = []
    _record_steps(plugin, steps, ["_check_idle_dim", "_tick_clock", "_handle_deck_lost"])
    await plugin._watchdog()
    assert steps == [("_check_idle_dim", "V2"), ("_tick_clock", "V2")]


@pytest.mark.asyncio
async def test_a_tick_opens_usb_then_virtual_then_network_decks(monkeypatch):
    config = {"virtual_decks": [
        {"model": "Stream Deck Mini", "serial": "VA"},
        {"model": "Stream Deck XL", "serial": "VB"},
    ]}
    usb = [_Deck("UA"), _Deck("UB")]
    plugin, _state, _logs, library = _watch_rig(monkeypatch, config, decks=usb)
    net = _Deck("NA")

    async def reconcile(new_decks):
        assert [getattr(d, "serial", None) or d.get_serial_number() for d in new_decks] == [
            "UA", "UB", "VA", "VB",
        ]
        new_decks.append(net)
    plugin._reconcile_network_decks = reconcile
    opened = []

    async def open_deck(deck):
        opened.append(deck)
    plugin._open_deck = open_deck
    await plugin._watchdog()
    assert opened[:2] == usb
    assert [type(d) for d in opened[2:4]] == [VirtualDeck, VirtualDeck]
    assert [d.get_serial_number() for d in opened[2:4]] == ["VA", "VB"]
    assert [d.deck_type() for d in opened[2:4]] == ["Stream Deck Mini", "Stream Deck XL"]
    assert [d._preset for d in opened[2:4]] == [
        VIRTUAL_MODELS["Stream Deck Mini"], VIRTUAL_MODELS["Stream Deck XL"],
    ]
    assert opened[4] is net
    assert plugin._opening is False


@pytest.mark.asyncio
async def test_a_tick_skips_decks_it_already_holds(monkeypatch):
    held, fresh = _Deck("HELD"), _Deck("FRESH")
    plugin, _state, _logs, library = _watch_rig(monkeypatch, decks=[held])
    await plugin._watchdog()
    library.manager.decks = [held, fresh]
    opened = []

    async def open_deck(deck):
        opened.append(deck)
    plugin._open_deck = open_deck
    await plugin._watchdog()
    assert opened == [fresh]


@pytest.mark.asyncio
async def test_a_running_virtual_deck_is_not_made_again(monkeypatch):
    config = {"virtual_decks": [{"model": "Stream Deck Mini", "serial": "VR"}]}
    plugin, _state, _logs, library = _watch_rig(monkeypatch, config)
    _record_steps(plugin, [], [
        "_render_all_buttons", "_render_touchscreen", "_render_info_strip",
    ])
    await plugin._watchdog()
    opened = []

    async def open_deck(deck):
        opened.append(deck)
    plugin._open_deck = open_deck
    await plugin._watchdog()
    assert opened == []


@pytest.mark.asyncio
async def test_a_deck_whose_id_cannot_be_read_is_opened_every_tick(monkeypatch):
    deck = _Deck("NOID", hid=RuntimeError("no path"))
    plugin, _state, _logs, library = _watch_rig(monkeypatch, decks=[deck])
    opened = []

    async def open_deck(d):
        opened.append(d)
    plugin._open_deck = open_deck
    await plugin._watchdog()
    await plugin._watchdog()
    assert opened == [deck, deck]


@pytest.mark.asyncio
async def test_a_deck_whose_id_is_none_is_opened(monkeypatch):
    deck = _Deck("NONE")
    deck._hid = None
    deck.id = lambda: None
    plugin, _state, _logs, library = _watch_rig(monkeypatch, decks=[deck])
    plugin._sessions[None] = SimpleNamespace(
        deck=SimpleNamespace(is_open=lambda: True, connected=lambda: True),
        is_virtual=False, serial="X",
    )

    async def nothing(session):
        return None
    plugin._check_idle_dim = nothing
    plugin._tick_clock = nothing
    opened = []

    async def open_deck(d):
        opened.append(d)
    plugin._open_deck = open_deck
    await plugin._watchdog()
    assert opened == [deck]


@pytest.mark.asyncio
async def test_a_tick_whose_enumerate_fails_still_makes_virtual_decks(monkeypatch):
    config = {"virtual_decks": [{"model": "Stream Deck Mini", "serial": "VE"}]}
    plugin, _state, _logs, library = _watch_rig(
        monkeypatch, config, enumerate_error=OSError("hid gone")
    )
    opened = []

    async def open_deck(d):
        opened.append(d)
    plugin._open_deck = open_deck
    await plugin._watchdog()
    assert [d.get_serial_number() for d in opened] == ["VE"]


@pytest.mark.asyncio
async def test_a_failed_network_reconcile_is_logged_and_the_tick_goes_on(monkeypatch):
    usb = _Deck("GO")
    plugin, _state, logs, library = _watch_rig(monkeypatch, decks=[usb])

    async def reconcile(new_decks):
        new_decks.append("half")
        raise RuntimeError("dock refused")
    plugin._reconcile_network_decks = reconcile
    opened = []

    async def open_deck(d):
        opened.append(d)
    plugin._open_deck = open_deck
    await plugin._watchdog()
    assert ("warning", "Network deck reconcile error: dock refused") in logs
    assert opened == [usb, "half"]


@pytest.mark.asyncio
async def test_a_tick_with_nothing_new_never_sets_opening(monkeypatch):
    plugin, _state, _logs, library = _watch_rig(monkeypatch)
    seen = []

    async def reconcile(new_decks):
        seen.append(plugin._opening)
    plugin._reconcile_network_decks = reconcile

    async def open_deck(d):
        raise AssertionError("nothing to open")
    plugin._open_deck = open_deck
    await plugin._watchdog()
    assert seen == [False]
    assert plugin._opening is False


@pytest.mark.asyncio
async def test_a_deck_that_fails_to_open_is_logged_and_the_rest_still_open(monkeypatch):
    bad, good = _Deck("BAD"), _Deck("GOOD")
    plugin, _state, logs, library = _watch_rig(monkeypatch, decks=[bad, good])
    opening_seen = []

    async def open_deck(d):
        opening_seen.append(plugin._opening)
        if d is bad:
            raise RuntimeError("would not open")
        plugin._sessions[d.id()] = SimpleNamespace(deck=d)
    plugin._open_deck = open_deck
    await plugin._watchdog()
    assert ("warning", "Failed to open Stream Deck: would not open") in logs
    assert opening_seen == [True, True]
    assert list(plugin._sessions) == ["hid-GOOD"]
    assert plugin._opening is False


@pytest.mark.asyncio
async def test_opening_is_cleared_even_when_an_open_escapes(monkeypatch):
    plugin, _state, _logs, library = _watch_rig(monkeypatch, decks=[_Deck("ESC")])

    async def open_deck(d):
        raise KeyboardInterrupt
    plugin._open_deck = open_deck
    with pytest.raises(KeyboardInterrupt):
        await plugin._watchdog()
    assert plugin._opening is False


@pytest.mark.asyncio
async def test_a_tick_tends_held_decks_before_it_looks_for_new_ones(monkeypatch):
    held = _Deck("FIRST")
    plugin, _state, _logs, library = _watch_rig(monkeypatch, decks=[held])
    await plugin._watchdog()
    order = []

    async def dim(session):
        order.append("dim")
    plugin._check_idle_dim = dim

    async def clock(session):
        order.append("clock")
    plugin._tick_clock = clock

    async def reconcile(new_decks):
        order.append("reconcile")
    plugin._reconcile_network_decks = reconcile
    original_enumerate = library.manager.enumerate

    def enumerate_spy():
        order.append("enumerate")
        return original_enumerate()
    library.manager.enumerate = enumerate_spy
    await plugin._watchdog()
    assert order == ["dim", "clock", "enumerate", "reconcile"]


@pytest.mark.asyncio
async def test_a_lost_deck_frees_its_path_for_the_same_tick(monkeypatch):
    """A deck lost early in a tick is no longer held when the tick looks
    for new decks, so a deck re-enumerated at its path re-opens at once."""
    deck = _Deck("BACK")
    plugin, _state, _logs, library = _watch_rig(monkeypatch, decks=[deck])
    await plugin._watchdog()
    deck._connected = False
    again = _Deck("BACK")
    library.manager.decks = [again]
    await plugin._watchdog()
    assert plugin._sessions["hid-BACK"].deck is again


# ──── A lost deck and a config change: what each ends ────


class _Recorder:
    def __init__(self):
        self.cancelled = []
        self.unsubscribed = []


def _ending_rig(config=None, *, failing_sub=None):
    plugin, state, logs = _plugin(config)
    rec = _Recorder()
    plugin.api.cancel_task = lambda task_id: rec.cancelled.append(task_id)

    async def unsubscribe(sub_id):
        rec.unsubscribed.append(sub_id)
        if sub_id == failing_sub:
            raise RuntimeError("already gone")
    plugin.api.state_unsubscribe = unsubscribe
    return plugin, state, logs, rec


@pytest.mark.asyncio
async def test_a_lost_deck_ends_its_holds_presses_and_subscriptions():
    plugin, state, logs, rec = _ending_rig(failing_sub="s1")
    deck = _Deck("LOST")
    deck._open = True
    session = sd_module._DeckSession(deck)
    session.serial, session.model, session.device_id = "LOST", "Mini", "hid-LOST"
    plugin._sessions["hid-LOST"] = session
    session.hold_tasks = {1: "t1", 4: "t4"}
    session.press_times = {1: 1.0}
    session.dial_press_times = {0: 2.0}
    session.dial_turned = {0}
    session.feedback_subs = ["s1", "s2"]
    session.strip_image = object()
    session.strip_dirty = "all"
    session.overlay_active = True
    plugin._mirror_blobs[("LOST", "key_0")] = (b"", "image/png")
    events = []

    async def emit(event, payload=None):
        events.append((event, payload))
    plugin.api.event_emit = emit
    await plugin._handle_deck_lost(session)
    assert rec.cancelled == ["t1", "t4"]
    assert rec.unsubscribed == ["s1", "s2"]
    assert session.hold_tasks == {} and session.press_times == {}
    assert session.dial_press_times == {} and session.dial_turned == set()
    assert session.feedback_subs == []
    assert plugin._sessions == {}
    assert deck.closed is True
    assert (session.strip_image, session.strip_dirty, session.overlay_active) == (
        None, None, False,
    )
    assert plugin._mirror_blobs == {}
    assert state.get("plugin.streamdeck.LOST.connected") is False
    assert state.get("plugin.streamdeck.connected") is False
    assert events == [("disconnected", {"serial": "LOST"})]
    assert logs[0] == ("warning", "Stream Deck disconnected (Mini S/N: LOST)")


@pytest.mark.asyncio
async def test_a_lost_deck_whose_close_raises_still_goes():
    plugin, state, _logs, _rec = _ending_rig()
    deck = _Deck("CLOSE")

    def broken_close():
        raise OSError("already closed")
    deck.close = broken_close
    session = sd_module._DeckSession(deck)
    session.serial, session.device_id = "CLOSE", "hid-CLOSE"
    plugin._sessions["hid-CLOSE"] = session
    await plugin._handle_deck_lost(session)
    assert plugin._sessions == {}
    assert state.get("plugin.streamdeck.CLOSE.connected") is False


@pytest.mark.asyncio
async def test_a_config_change_ends_holds_and_resubscribes_each_deck():
    plugin, _state, _logs, rec = _ending_rig(failing_sub="a1")
    sessions = []
    for serial in ("CA", "CB"):
        session = sd_module._DeckSession(None)
        session.serial, session.device_id = serial, f"hid-{serial}"
        plugin._sessions[session.device_id] = session
        sessions.append(session)
    sessions[0].hold_tasks = {2: "h2"}
    sessions[0].feedback_subs = ["a1", "a2"]
    sessions[1].hold_tasks = {0: "h0", 5: "h5"}
    sessions[1].feedback_subs = ["b1"]
    sessions[0].press_times = {2: 9.0}
    steps = []

    async def setup(session):
        steps.append(("subscribe", session.serial, list(session.feedback_subs),
                      dict(session.hold_tasks)))
    plugin._setup_feedback_subscriptions = setup
    assert await plugin.on_config_changed({}) is True
    assert rec.cancelled == ["h2", "h0", "h5"]
    assert rec.unsubscribed == ["a1", "a2", "b1"]
    assert steps == [("subscribe", "CA", [], {}), ("subscribe", "CB", [], {})]
    # A press that was timing keeps its time; only the repeats end.
    assert sessions[0].press_times == {2: 9.0}


@pytest.mark.asyncio
async def test_a_config_change_runs_its_steps_per_deck_in_order():
    plugin, _state, _logs, _rec = _ending_rig()
    session = sd_module._DeckSession(None)
    session.serial, session.device_id = "ORDER", "hid-ORDER"
    session.current_page = 4
    plugin._sessions[session.device_id] = session
    steps = []
    _record_steps(plugin, steps, [
        "_setup_feedback_subscriptions", "_apply_active_brightness",
        "_render_all_buttons", "_render_touchscreen", "_render_info_strip",
    ])
    plugin._effective_page_count = lambda s: (steps.append(("count", s.serial)), 2)[1]

    async def change(s, page):
        steps.append(("change_page", page))
        s.current_page = page
    plugin._change_page = change

    async def auto(s):
        steps.append(("auto", s.current_page))
        return 0
    plugin._evaluate_auto_page = auto

    async def publish():
        steps.append(("publish",))
    plugin._publish_deck_state = publish
    await plugin.on_config_changed({})
    assert steps == [
        ("_setup_feedback_subscriptions", "ORDER"),
        ("count", "ORDER"),
        ("change_page", 1),
        ("auto", 1),
        ("change_page", 0),
        ("_apply_active_brightness", "ORDER"),
        ("_render_all_buttons", "ORDER"),
        ("_render_touchscreen", "ORDER"),
        ("_render_info_strip", "ORDER"),
        ("publish",),
    ]


@pytest.mark.asyncio
async def test_a_config_change_leaves_a_page_that_still_fits_and_matches():
    plugin, _state, _logs, _rec = _ending_rig()
    session = sd_module._DeckSession(None)
    session.serial, session.device_id = "STAY", "hid-STAY"
    session.current_page = 1
    plugin._sessions[session.device_id] = session
    changes = []

    async def change(s, page):
        changes.append(page)
    plugin._change_page = change
    plugin._effective_page_count = lambda s: 3

    async def auto(s):
        return 1
    plugin._evaluate_auto_page = auto
    await plugin.on_config_changed({})
    assert changes == []
