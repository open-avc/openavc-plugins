"""
Stream Deck: where each part of a key and a strip zone is drawn, how a key is
flashed, and which state a key, dial or zone may write.

Characterisation pins for the drawing a deck does from a look: where a key's
icon, label, live value and meter bar go (and which wins the text slot when an
icon shows), where a touch-strip zone or info item puts the same parts beside
its icon, the zone's touch flash and divider, the idle clock's two lines, the
guards on drawing a label, a flashed key and Identify. The last section pins the
write rule a state.set action, a dial adjust or a zone fader goes through, and
the config check's reading of the same rule.

Real Pillow draws; icons are stand-in squares of one color, and labels are
recorded rather than drawn so each part's place can be read off the pixels.
No hardware: decks are stand-ins that record what they are sent.

Run from the openavc-plugins root:
pytest tests/test_streamdeck_drawing_and_writes.py -v
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
from control_surfaces.streamdeck.looks import METER_DEFAULT_COLOR  # noqa: E402
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

RED = (255, 0, 0)
GREEN_HEX = "#00ff00"
GREEN = (0, 255, 0)
TRACK = (0x2A, 0x2A, 0x3E)
DIVIDER = (0x3A, 0x3A, 0x4E)
FG = "#e0e0e0"
BG = "#1a1a2e"


def _plugin(config=None, plugin_id="streamdeck"):
    state = StateStore()
    events = EventBus()
    state.set_event_bus(events)
    logs = []
    api = PluginAPI(
        plugin_id=plugin_id,
        capabilities=StreamDeckPlugin.PLUGIN_INFO["capabilities"],
        config=config or {},
        registry=PluginRegistry(plugin_id),
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


class _Helper:
    converted = []

    @classmethod
    def to_native_key_format(cls, deck, img):
        cls.converted.append(("key", img.size, img.copy()))
        return b"\x01\x02\x03"

    @classmethod
    def to_native_touchscreen_format(cls, deck, img):
        return img.copy()

    @classmethod
    def to_native_screen_format(cls, deck, img):
        return img.copy()


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


class _Deck:
    """A visual deck with LCD keys, touch keys and a strip, recording sends."""

    def __init__(self, *, keys=4, touch_keys=2, key_size=(72, 72), visual=True):
        self._keys = keys
        self._touch_keys = touch_keys
        self._key_size = key_size
        self._visual = visual
        self.sent = []

    def is_visual(self):
        return self._visual

    def key_count(self):
        return self._keys

    def touch_key_count(self):
        return self._touch_keys

    def key_layout(self):
        return (1, self._keys)

    def is_touch(self):
        return True

    def dial_count(self):
        return 0

    def key_image_format(self):
        return {"size": self._key_size, "format": "JPEG"}

    def touchscreen_image_format(self):
        return {"size": (400, 100), "format": "JPEG"}

    def __enter__(self):
        self.sent.append(("lock",))
        return self

    def __exit__(self, *exc):
        return False

    def set_key_image(self, key, image):
        self.sent.append(("key", key, bytes(image)))

    def set_key_color(self, key, r, g, b):
        self.sent.append(("color", key, (r, g, b)))


def _session(plugin, deck, serial="SN1"):
    session = sd_module._DeckSession(deck)
    session.serial = serial
    session.device_id = f"dev-{serial}"
    plugin._sessions[session.device_id] = session
    return session


def _label_call(args, kwargs):
    """One label paste as (text, color, box, max_font, max_lines)."""
    label, color, box = args
    return (label, color, tuple(box), kwargs["max_font"], kwargs["max_lines"])


def _record_labels(plugin):
    """Record every label paste instead of drawing it."""
    drawn = []

    def _paste(img, *args, **kwargs):
        drawn.append(_label_call(args, kwargs))

    plugin._paste_label = _paste
    return drawn


def _icon_stub(plugin, img_mod, renders=True):
    """Icons render as a red square (or not at all); each request recorded."""
    asked = []

    def _render(name, color, size):
        asked.append((name, color, size))
        if not renders:
            return None
        return img_mod.new("RGBA", (20, 20), RED + (255,))

    plugin._render_icon = _render
    return asked


def _pixels(img):
    """Every pixel, row by row (any Pillow version)."""
    access = img.load()
    width, height = img.size
    return [access[x, y] for y in range(height) for x in range(width)]


def _bbox(img, rgb):
    """(left, top, right, bottom) of the pixels exactly ``rgb``; right and
    bottom exclusive. None when there are none."""
    width = img.size[0]
    xs, ys = [], []
    for i, pixel in enumerate(_pixels(img)):
        if pixel[:3] == rgb:
            xs.append(i % width)
            ys.append(i // width)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs) + 1, max(ys) + 1)


def _key_image(plugin, session, label, icon=None, value="", meter=None,
               bg=BG, fg=FG):
    return plugin._create_button_image(
        session, label, bg, fg, icon, value_text=value, meter=meter,
    )


def _key_rig(pil, key_size=(72, 72), renders=True):
    plugin, state, logs = _plugin()
    session = _session(plugin, _Deck(key_size=key_size))
    labels = _record_labels(plugin)
    icons = _icon_stub(plugin, pil, renders)
    return plugin, session, labels, icons


# ──── A key's image: where each part goes ────


@pytest.mark.parametrize("size", [(72, 72), (96, 96), (80, 120), (120, 80)])
def test_a_label_alone_fills_the_key_padded_in_up_to_three_lines(pil, size):
    plugin, session, labels, icons = _key_rig(pil, size)
    img = _key_image(plugin, session, "Power")
    w, h = size
    pad = max(2, w // 12)
    assert labels == [("Power", FG, (pad, pad, w - 2 * pad, h - 2 * pad),
                       max(14, h // 4), 3)]
    assert img.size == size and img.mode == "RGB"
    assert _bbox(img, (0x1A, 0x1A, 0x2E)) == (0, 0, w, h)
    assert icons == []


def test_a_value_alone_takes_the_label_slot(pil):
    plugin, session, labels, _ = _key_rig(pil)
    _key_image(plugin, session, "", value="42 dB")
    assert labels == [("42 dB", FG, (6, 6, 60, 60), 18, 3)]


@pytest.mark.parametrize("size", [(72, 72), (96, 120), (64, 40)])
def test_a_label_sits_over_the_live_value(pil, size):
    plugin, session, labels, _ = _key_rig(pil, size)
    _key_image(plugin, session, "Mic", value="42 dB", fg="#123456")
    w, h = size
    assert labels == [
        ("Mic", "#123456", (2, 2, w - 4, h // 3), 14, 1),
        ("42 dB", "#123456", (2, h // 3 + 2, w - 4, max(0, h - h // 3 - 4)),
         max(16, h // 3), 1),
    ]


@pytest.mark.parametrize("size", [(72, 72), (96, 96), (80, 120)])
def test_an_icon_sits_over_a_wrapped_label(pil, size):
    plugin, session, labels, icons = _key_rig(pil, size)
    img = _key_image(plugin, session, "Power", icon="power", fg="#abcdef")
    w, h = size
    icon_size = min(w, h) // 2
    x = (w - icon_size) // 2
    y = max(4, (h // 2) - icon_size + 4)
    assert icons == [("power", "#abcdef", w)]
    assert _bbox(img, RED) == (x, y, x + icon_size, y + icon_size)
    top = y + icon_size + 2
    assert labels == [("Power", "#abcdef", (2, top, w - 4, max(0, h - top - 2)), 14, 2)]


def test_with_an_icon_the_live_value_wins_the_text_slot(pil):
    plugin, session, labels, _ = _key_rig(pil)
    _key_image(plugin, session, "Mic", icon="mic", value="42 dB")
    assert [call[0] for call in labels] == ["42 dB"]
    assert labels[0][3:] == (14, 2)


def test_an_icon_and_a_value_with_no_label_still_shows_the_value(pil):
    plugin, session, labels, _ = _key_rig(pil)
    _key_image(plugin, session, "", icon="mic", value="7")
    assert [call[0] for call in labels] == ["7"]


@pytest.mark.parametrize("size", [(72, 72), (96, 96), (80, 120)])
def test_an_icon_alone_is_centered_at_six_tenths(pil, size):
    plugin, session, labels, _ = _key_rig(pil, size)
    img = _key_image(plugin, session, "", icon="power")
    w, h = size
    icon_size = int(min(w, h) * 0.6)
    x, y = (w - icon_size) // 2, (h - icon_size) // 2
    assert _bbox(img, RED) == (x, y, x + icon_size, y + icon_size)
    assert labels == []


def test_an_icon_that_does_not_render_leaves_the_text_layout(pil):
    plugin, session, labels, icons = _key_rig(pil, renders=False)
    img = _key_image(plugin, session, "Mic", icon="nope", value="42 dB")
    assert icons == [("nope", FG, 72)]
    assert [call[0] for call in labels] == ["Mic", "42 dB"]
    assert _bbox(img, RED) is None


def test_a_key_with_nothing_on_it_is_its_background(pil):
    plugin, session, labels, icons = _key_rig(pil)
    img = _key_image(plugin, session, "", bg="#ffffff", fg="#ffffff")
    assert labels == [] and icons == []
    assert _bbox(img, (255, 255, 255)) == (0, 0, 72, 72)


@pytest.mark.parametrize("frac,fill_right", [(0.5, 36), (1.0, 69), (0.25, 20)])
def test_a_meter_draws_its_track_and_fill_along_the_bottom(pil, frac, fill_right):
    plugin, session, labels, _ = _key_rig(pil)
    img = _key_image(plugin, session, "Vol", meter=(frac, GREEN_HEX))
    assert _bbox(img, GREEN) == (3, 64, fill_right, 70)
    track = _bbox(img, TRACK)
    if frac < 1.0:
        assert track == (fill_right, 64, 69, 70)
    else:
        assert track is None
    # The meter takes 10 px from the text's height.
    assert labels == [("Vol", FG, (6, 6, 60, 50), 15, 3)]


def test_an_empty_meter_draws_only_its_track(pil):
    plugin, session, _, _ = _key_rig(pil)
    img = _key_image(plugin, session, "", meter=(0.0, GREEN_HEX))
    assert _bbox(img, GREEN) is None
    assert _bbox(img, TRACK) == (3, 64, 69, 70)


def test_a_meter_shrinks_the_icon_and_the_value_slots(pil):
    plugin, session, labels, _ = _key_rig(pil)
    img = _key_image(plugin, session, "", icon="power", meter=(0.0, GREEN_HEX))
    icon_size = int(62 * 0.6)
    x, y = (72 - icon_size) // 2, (62 - icon_size) // 2
    assert _bbox(img, RED) == (x, y, x + icon_size, y + icon_size)
    plugin, session, labels, _ = _key_rig(pil)
    _key_image(plugin, session, "Mic", value="5", meter=(0.0, GREEN_HEX))
    assert labels == [
        ("Mic", FG, (2, 2, 68, 20), 14, 1),
        ("5", FG, (2, 22, 68, 38), 20, 1),
    ]
    plugin, session, labels, _ = _key_rig(pil)
    img = _key_image(plugin, session, "Mic", icon="mic", meter=(0.0, GREEN_HEX))
    icon_size = 62 // 2
    x, y = (72 - icon_size) // 2, max(4, 31 - icon_size + 4)
    assert _bbox(img, RED) == (x, y, x + icon_size, y + icon_size)
    top = y + icon_size + 2
    assert labels == [("Mic", FG, (2, top, 68, max(0, 62 - top - 2)), 14, 2)]


def test_a_key_reads_its_size_from_the_deck(pil):
    plugin, session, labels, _ = _key_rig(pil, (100, 60))
    img = _key_image(plugin, session, "A")
    assert img.size == (100, 60)


# ──── A strip zone or info item: where each part goes ────


def _zone_rig(pil, config=None, renders=True):
    plugin, state, logs = _plugin(config)
    session = _session(plugin, _Deck())
    labels = _record_labels(plugin)
    icons = _icon_stub(plugin, pil, renders)
    img = pil.new("RGB", (400, 100), (0, 0, 0))
    return plugin, state, session, labels, icons, img


async def _zone(plugin, session, img, zone, height=100):
    await plugin._draw_strip_zone(session, img, zone, height)
    return img


@pytest.mark.asyncio
async def test_a_zone_fills_its_own_bounds_and_divides_from_the_one_before(pil):
    plugin, state, session, labels, _, img = _zone_rig(pil)
    await _zone(plugin, session, img, {"x": 100, "w": 150, "bg_color": "#ffffff"})
    assert _bbox(img, (255, 255, 255)) == (100, 0, 250, 100)
    assert _bbox(img, DIVIDER) == (100, 8, 101, 93)
    assert labels == []


@pytest.mark.asyncio
async def test_the_first_zone_has_no_divider_and_takes_the_deck_colors(pil):
    plugin, state, session, labels, _, img = _zone_rig(
        pil, {"button_color": "#ffffff", "text_color": "#123456"})
    await _zone(plugin, session, img, {"x": 0, "w": 200, "label": "A"})
    assert _bbox(img, DIVIDER) is None
    assert _bbox(img, (255, 255, 255)) == (0, 0, 200, 100)
    assert labels == [("A", "#123456", (4, 4, 192, 88), 24, 2)]


@pytest.mark.asyncio
async def test_a_zone_puts_its_label_over_its_value(pil):
    plugin, state, session, labels, _, img = _zone_rig(pil)
    state.set("var.level", 40, source="test")
    await _zone(plugin, session, img, {"x": 200, "w": 200, "label": "Mic",
                                       "value_source": "var.level", "unit": "dB"},
                height=90)
    assert labels == [
        ("Mic", FG, (204, 4, 192, 30), 16, 1),
        ("40 dB", FG, (204, 32, 192, 52), 30, 1),
    ]


@pytest.mark.asyncio
async def test_a_zone_with_only_a_value_shows_it_large(pil):
    plugin, state, session, labels, _, img = _zone_rig(pil)
    await _zone(plugin, session, img, {"x": 0, "w": 100, "value_static": 12})
    assert labels == [("12", FG, (4, 4, 92, 88), 24, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("zw,height", [(200, 100), (100, 100), (400, 58), (40, 30)])
async def test_a_zone_icon_sits_left_and_pushes_the_text_right(pil, zw, height):
    plugin, state, session, labels, icons, img = _zone_rig(pil)
    img = pil.new("RGB", (400, height), (0, 0, 0))
    await _zone(plugin, session, img, {"x": 0, "w": zw, "label": "Mic",
                                       "icon": "mic", "text_color": "#abcdef"},
                height=height)
    text_bottom = height - 4
    size = max(16, min(40, zw // 4, text_bottom - 8))
    y = max(2, (text_bottom - size) // 2)
    assert icons == [("mic", "#abcdef", 72)]
    assert _bbox(img, RED) == (8, y, 8 + size, y + size)
    text_x = 8 + size + 6
    assert labels == [("Mic", "#abcdef", (text_x, 4, zw - 4 - text_x, text_bottom - 8), 24, 2)]


@pytest.mark.asyncio
async def test_a_zone_icon_that_does_not_render_leaves_the_text_where_it_was(pil):
    plugin, state, session, labels, icons, img = _zone_rig(pil, renders=False)
    await _zone(plugin, session, img, {"x": 0, "w": 200, "label": "Mic", "icon": "x"})
    assert icons == [("x", FG, 72)]
    assert labels == [("Mic", FG, (4, 4, 192, 88), 24, 2)]


@pytest.mark.asyncio
async def test_a_zone_without_an_icon_asks_for_none(pil):
    plugin, state, session, labels, icons, img = _zone_rig(pil)
    await _zone(plugin, session, img, {"x": 0, "w": 200, "label": "Mic", "icon": ""})
    assert icons == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value,fill_right", [(50, 250), (100, 392), (10, 137)])
async def test_a_zone_meter_draws_along_its_bottom(pil, value, fill_right):
    plugin, state, session, labels, _, img = _zone_rig(pil)
    state.set("var.v", value, source="test")
    zone = {"x": 100, "w": 300, "label": "Vol", "value_source": "var.v",
            "meter": {"color": GREEN_HEX}}
    await _zone(plugin, session, img, zone)
    assert _bbox(img, GREEN) == (108, 86, fill_right, 94)
    track = _bbox(img, TRACK)
    assert track == (None if value == 100 else (fill_right, 86, 392, 94))
    # The meter lifts the text's bottom from 4 px to 18 px above the edge.
    assert labels == [
        ("Vol", FG, (104, 4, 292, 33), 16, 1),
        (str(value), FG, (104, 35, 292, 45), 30, 1),
    ]


@pytest.mark.asyncio
async def test_a_zone_meter_at_its_minimum_draws_only_its_track(pil):
    plugin, state, session, labels, _, img = _zone_rig(pil)
    state.set("var.v", 0, source="test")
    await _zone(plugin, session, img, {"x": 0, "w": 200, "value_source": "var.v",
                                       "meter": {"color": GREEN_HEX}})
    assert _bbox(img, GREEN) is None
    assert _bbox(img, TRACK) == (8, 86, 192, 94)


@pytest.mark.asyncio
async def test_a_zone_meter_reads_its_bounds_from_the_drag_adjust(pil):
    plugin, state, session, labels, _, img = _zone_rig(pil)
    state.set("var.v", 5, source="test")
    await _zone(plugin, session, img, {"x": 0, "w": 217, "value_source": "var.v",
                                       "drag_adjust": {"min": 0, "max": 10}})
    assert _bbox(img, tuple(int(METER_DEFAULT_COLOR[i:i + 2], 16) for i in (1, 3, 5))) == (
        8, 86, 109, 94)


@pytest.mark.asyncio
async def test_a_touched_zone_lightens_and_takes_a_white_border(pil):
    plugin, state, session, labels, _, img = _zone_rig(pil)
    session.flash_zones[1] = object()
    await _zone(plugin, session, img, {"x": 100, "w": 100, "index": 1,
                                       "bg_color": "#000000"})
    lightened = (56, 56, 56)
    for spot in ((150, 50), (103, 3), (196, 96), (100, 0), (199, 99)):
        assert img.getpixel(spot) == lightened, spot
    for spot in ((101, 1), (102, 2), (150, 1), (101, 50), (197, 97)):
        assert img.getpixel(spot) == (255, 255, 255), spot
    assert img.getpixel((198, 98)) == (255, 255, 255)
    assert img.getpixel((100, 50)) == DIVIDER
    assert img.getpixel((99, 50)) == (0, 0, 0)


@pytest.mark.asyncio
async def test_an_untouched_zone_is_not_lightened(pil):
    plugin, state, session, labels, _, img = _zone_rig(pil)
    session.flash_zones[2] = object()
    await _zone(plugin, session, img, {"x": 0, "w": 100, "index": 1,
                                       "bg_color": "#000000"})
    assert _bbox(img, (255, 255, 255)) is None


# ──── The idle clock and a label's guards ────


def test_the_idle_clock_puts_the_time_over_the_date(pil, monkeypatch):
    plugin, state, logs = _plugin()
    labels = _record_labels(plugin)
    monkeypatch.setattr(StreamDeckPlugin, "_clock_text", staticmethod(lambda: ("9:05 AM", "Mon Nov 09")))
    img = pil.new("RGB", (400, 100))
    plugin._draw_clock(img, 400, 100, "#abcdef")
    assert labels == [
        ("9:05 AM", "#abcdef", (0, 2, 400, 62), 50, 1),
        ("Mon Nov 09", "#8a8a9a", (0, 64, 400, 34), 20, 1),
    ]
    labels.clear()
    plugin._draw_clock(img, 120, 12, "#abcdef")
    assert labels == [
        ("9:05 AM", "#abcdef", (0, 2, 120, 10), 16, 1),
        ("Mon Nov 09", "#8a8a9a", (0, 12, 120, 8), 10, 1),
    ]


def test_a_label_with_no_text_or_no_room_draws_nothing(pil):
    plugin, state, logs = _plugin()
    plugin._load_text_font()
    img = pil.new("RGB", (100, 50), (0, 0, 0))
    for label, box in (("", (0, 0, 100, 50)), ("Hi", (0, 0, 0, 50)),
                       ("Hi", (0, 0, 100, 0)), ("Hi", (0, 0, -5, 50)),
                       (None, (0, 0, 100, 50))):
        plugin._paste_label(img, label, "#ffffff", box, max_font=14, max_lines=2)
    assert plugin._label_cache == {}
    assert _bbox(img, (0, 0, 0)) == (0, 0, 100, 50)


def test_a_label_draws_at_its_box_and_is_cached_by_its_fit(pil):
    plugin, state, logs = _plugin()
    plugin._load_text_font()
    img = pil.new("RGB", (100, 60), (0, 0, 0))
    plugin._paste_label(img, "Hi", "#ffffff", (30, 10, 40, 20), max_font=14, max_lines=2)
    assert list(plugin._label_cache) == [("Hi", 40, 20, "#ffffff", 14, 2)]
    layer = plugin._label_cache[("Hi", 40, 20, "#ffffff", 14, 2)]
    alone = pil.new("RGB", (40, 20), (0, 0, 0))
    alone.paste(layer, (0, 0), layer)
    assert alone.getbbox() is not None
    assert img.crop((30, 10, 70, 30)).tobytes() == alone.tobytes()
    assert img.getbbox() == (30 + alone.getbbox()[0], 10 + alone.getbbox()[1],
                             30 + alone.getbbox()[2], 10 + alone.getbbox()[3])
    assert list(plugin._label_cache) == [("Hi", 40, 20, "#ffffff", 14, 2)]


# ──── A strip zone's partial redraw at the strip's edges ────


def _strip_rig(pil, zones):
    plugin, state, logs = _plugin({"touchscreen": {"zones": zones}})
    deck = _Deck()
    sent = []
    deck.set_touchscreen_image = lambda image, x=0, y=0, w=0, h=0: sent.append((x, w, h))
    session = _session(plugin, deck)
    session.strip_image = pil.new("RGB", (400, 100), (0, 0, 0))
    full = []

    async def _full(session_):
        full.append(session_)

    plugin._render_touchscreen = _full
    _record_labels(plugin)
    return plugin, session, sent, full


@pytest.mark.asyncio
async def test_a_zone_that_starts_at_the_strips_edge_sends_nothing(pil):
    plugin, session, sent, full = _strip_rig(
        pil, [{"x": 0, "w": 400}, {"x": 400, "w": 50}, {"x": 350, "w": 100}])
    await plugin._render_strip_zone(session, 1)
    assert sent == [] and full == []
    await plugin._render_strip_zone(session, 2)
    assert sent == [(350, 50, 100)] and full == []


@pytest.mark.asyncio
async def test_a_zone_index_past_the_last_zone_redraws_the_whole_strip(pil):
    plugin, session, sent, full = _strip_rig(pil, [{"x": 0, "w": 200}, {"x": 200, "w": 200}])
    for index in (2, 3, -1):
        await plugin._render_strip_zone(session, index)
    assert full == [session] * 3 and sent == []
    await plugin._render_strip_zone(session, 1)
    assert sent == [(200, 200, 100)] and full == [session] * 3


# ──── A flashed key and Identify ────


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(sd_module.asyncio, "sleep", _sleep)
    return slept


def _flash_rig(deck):
    plugin, state, logs = _plugin()
    session = _session(plugin, deck)
    calls = []

    async def _render(session_, index):
        calls.append(("render", index))

    def _image(session_, index, image):
        calls.append(("image", index, image.size, set(_pixels(image))))

    def _color(session_, index, color):
        calls.append(("color", index, color))

    plugin._render_button = _render
    plugin._apply_key_image = _image
    plugin._apply_key_color = _color
    return plugin, session, calls


@pytest.mark.asyncio
async def test_a_flashed_lcd_key_shows_white_then_redraws_each_time(pil, no_sleep):
    plugin, session, calls = _flash_rig(_Deck(key_size=(80, 60)))
    await plugin._flash_key(session, 1, 2)
    white = ("image", 1, (80, 60), {(255, 255, 255)})
    assert calls == [white, ("render", 1), white, ("render", 1)]
    assert no_sleep == [0.18, 0.12, 0.18, 0.12]


@pytest.mark.asyncio
async def test_a_flashed_touch_key_lights_white(pil, no_sleep):
    plugin, session, calls = _flash_rig(_Deck(keys=4, touch_keys=2))
    await plugin._flash_key(session, 5, 1)
    assert calls == [("color", 5, "#ffffff"), ("render", 5)]


@pytest.mark.asyncio
async def test_a_flash_off_the_deck_or_on_no_screen_does_nothing(pil, no_sleep):
    for index in (-1, 6, 7):
        plugin, session, calls = _flash_rig(_Deck(keys=4, touch_keys=2))
        await plugin._flash_key(session, index, 2)
        assert calls == []
    plugin, session, calls = _flash_rig(_Deck(visual=False))
    await plugin._flash_key(session, 0, 2)
    assert calls == []
    plugin, session, calls = _flash_rig(_Deck())
    session.overlay_active = True
    await plugin._flash_key(session, 0, 2)
    assert calls == [] and no_sleep == []


@pytest.mark.asyncio
async def test_without_pillow_a_flashed_lcd_key_only_redraws(monkeypatch, no_sleep):
    monkeypatch.setattr(sd_module, "Image", None)
    plugin, session, calls = _flash_rig(_Deck())
    await plugin._flash_key(session, 0, 2)
    assert calls == [("render", 0), ("render", 0)]


@pytest.mark.asyncio
async def test_identify_flashes_every_key_three_times_then_redraws(pil, no_sleep):
    plugin, state, logs = _plugin()
    deck = _Deck(keys=2, touch_keys=1, key_size=(60, 40))
    session = _session(plugin, deck)
    colors, rendered = [], []

    def _color(session_, index, color):
        colors.append((index, color))

    async def _render_all(session_):
        rendered.append(session_)

    plugin._apply_key_color = _color
    plugin._render_all_buttons = _render_all
    await plugin._identify_deck(session)
    assert [(kind, size) for kind, size, _ in _Helper.converted] == [("key", (60, 40))]
    assert set(_pixels(_Helper.converted[0][2])) == {(255, 255, 255)}
    white, dark = b"\x01\x02\x03", b"\x00\x00\x00"
    one_flash = [("lock",), ("key", 0, white), ("key", 1, white),
                 ("lock",), ("key", 0, dark), ("key", 1, dark)]
    assert deck.sent == one_flash * 3
    assert colors == [(2, "#ffffff"), (2, "#000000")] * 3
    assert no_sleep == [0.3] * 6
    assert rendered == [session]
    assert logs == [("info", "Identifying Stream Deck SN1 (flashing all buttons)")]


@pytest.mark.asyncio
async def test_identify_on_a_deck_with_no_screen_does_nothing(pil, no_sleep):
    plugin, state, logs = _plugin()
    deck = _Deck(visual=False)
    session = _session(plugin, deck)
    await plugin._identify_deck(session)
    assert deck.sent == [] and logs == [] and no_sleep == []


def test_a_hidden_lcd_key_is_all_black(pil):
    plugin, state, logs = _plugin()
    session = _session(plugin, _Deck(key_size=(50, 30)))
    shown = []
    plugin._apply_key_image = lambda s, index, image: shown.append(
        (index, image.size, set(_pixels(image))))
    plugin._blank_key(session, 3, False)
    assert shown == [(3, (50, 30), {(0, 0, 0)})]


# ──── What a deck may write ────


def _write_rig(plugin_id="streamdeck"):
    plugin, state, logs = _plugin(plugin_id=plugin_id)
    calls = []

    async def _state_set(key, value):
        calls.append(("state", key, value))

    async def _variable_set(name, value):
        calls.append(("variable", name, value))

    plugin.api.state_set = _state_set
    plugin.api.variable_set = _variable_set
    return plugin, calls, logs


@pytest.mark.asyncio
async def test_a_deck_writes_its_own_state_and_user_variables(pil):
    plugin, calls, logs = _write_rig()
    await plugin._apply_state_set("plugin.streamdeck.mode", 2)
    await plugin._apply_state_set("var.room.mode", "on")
    await plugin._apply_state_set("var.", None)
    assert calls == [("state", "plugin.streamdeck.mode", 2),
                     ("variable", "room.mode", "on"), ("variable", "", None)]
    assert logs == []


@pytest.mark.asyncio
async def test_a_deck_refuses_any_other_write_with_a_warning(pil):
    plugin, calls, logs = _write_rig()
    for key in ("plugin.other.mode", "plugin.streamdeckx.a", "device.tv.power",
                "system.x", "variable.x", "Var.x", "plugin.streamdeck", ""):
        await plugin._apply_state_set(key, 1)
    assert calls == []
    assert logs[0] == (
        "warning",
        "Ignoring state.set to 'plugin.other.mode': a surface button may only "
        "write its own plugin.streamdeck.* state or a var.* user variable.",
    )
    assert len(logs) == 8 and {level for level, _ in logs} == {"warning"}


@pytest.mark.asyncio
async def test_the_write_rule_follows_the_plugin_id_it_runs_under(pil):
    plugin, calls, logs = _write_rig("deck2")
    await plugin._apply_state_set("plugin.deck2.a", 1)
    await plugin._apply_state_set("plugin.streamdeck.a", 1)
    assert calls == [("state", "plugin.deck2.a", 1)]
    assert "its own plugin.deck2.* state" in logs[0][1]


@pytest.mark.asyncio
async def test_a_write_to_a_key_that_is_not_text_raises(pil):
    plugin, calls, logs = _write_rig()
    with pytest.raises(AttributeError):
        await plugin._apply_state_set(5, 1)


def _write_problem_keys(keys):
    """Which of ``keys`` the config check reports as a write the deck refuses."""
    reported = []
    for key in keys:
        config = {"buttons": [{"index": 0, "bindings": {"press": [
            {"action": "state.set", "key": key, "value": 1}]}}]}
        if config_problems(config):
            reported.append(key)
    return reported


def test_the_config_check_reads_the_same_write_rule():
    keys = ["var.a", "var.", "plugin.streamdeck.a", "plugin.streamdeck.",
            "plugin.other.a", "plugin.streamdeckx.a", "device.tv.power",
            "Var.a", "plugin.streamdeck", "system.x"]
    assert _write_problem_keys(keys) == [
        "plugin.other.a", "plugin.streamdeckx.a", "device.tv.power", "Var.a",
        "plugin.streamdeck", "system.x"]


def test_the_config_check_leaves_an_unwritten_or_odd_key_to_other_rules():
    assert _write_problem_keys(["", None, 5, ["var.a"]]) == []


def test_a_dial_adjust_key_is_checked_by_the_same_rule():
    for key, reported in (("var.vol", False), ("plugin.streamdeck.v", False),
                          ("device.amp.vol", True)):
        config = {"dials": [{"index": 0, "adjust": {"key": key, "min": 0, "max": 10}}]}
        assert bool(config_problems(config)) is reported, key


def test_the_refusal_sentence_names_the_key_and_what_is_skipped():
    config = {"buttons": [{"index": 0, "bindings": {"press": [
        {"action": "state.set", "key": "device.tv.power", "value": 1}]}}]}
    (problem,) = config_problems(config)
    assert '"device.tv.power"' in problem and "the action does nothing" in problem

