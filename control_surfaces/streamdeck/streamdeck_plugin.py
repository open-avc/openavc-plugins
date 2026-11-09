"""
Elgato Stream Deck plugin for OpenAVC.

Turns any Elgato Stream Deck into a physical control surface for AV room control.
Button presses execute macros; button images update based on system state.

Supported models: Neo, Mini, MK.2/Original V2, XL, Plus, Pedal, and over the
network the Network Dock and the Stream Deck Studio (net_deck.py).

Beside this module: conditions.py (the rule visible_when, auto_page and
auto_brightness are judged by), config_check.py (what validate_config
reports), declared_decks.py (the virtual and network decks the config
declares, and the writes to a network deck's entry), dials.py (what a dial
gesture runs, and the touch strip's zones), key_entries.py (which config entry
a key runs, and the actions a binding holds), looks.py (what a key, a strip
zone or an info item shows: the value line, the meter, the feedback style),
context_actions.py (what a context action or a simulated input asks for),
deck_answers.py (what a network deck's discovery answer says),
deck_profile.py (what a deck says about itself when it opens: its transport,
serial, model and geometry), pages.py (how many pages a deck has, and which
page a value names), virtual_deck.py (a deck that exists only in software)
and watch_keys.py (which state keys a deck reads, and what each one
redraws).
"""

import asyncio
import functools
import io
import json
import os
import platform as platform_mod
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .conditions import coerce_numeric, eval_condition, reads_as_on
from .config_check import config_problems
from .context_actions import action_for, deck_action_args, simulated_inputs
from .declared_decks import (
    edit_network_decks,
    network_deck_entries,
    record_serials,
    unrecorded_serials,
    virtual_deck_entries,
)
from .key_entries import (
    action_list,
    assignment_for,
    objects,
    press_actions,
    unwrap_binding,
)
from .deck_answers import answer_address, answer_serial, scan_listing
from .deck_classes import net_deck_class
from .deck_profile import (
    UNCONNECTED_GEOMETRY,
    UNKNOWN_SERIAL,
    connected_line,
    deck_geometry,
    deck_identity,
    tell_link_serial,
)
from .dials import (
    declared_zones,
    dial_entry,
    lay_out_zones,
    live_adjust,
    push_deferred,
    release_actions,
    shows_on_strip,
    turn_route,
)
from .looks import meter_fill, styled, value_line
from .net_deck import NET_DEFAULT_PORT, NET_MDNS_SERVICE, DockLink, classify_net_error
from .pages import key_target_page, page_count, page_number, page_step
from .virtual_deck import VIRTUAL_MODELS, VirtualDeck
from .watch_keys import deck_watch, keys_watching, zone_watches_key

# A sentence raised as PluginRefusal from start() is the one the AI assistant
# repeats to the integrator; on an older OpenAVC the card shows it either way.
try:
    from openavc.core.plugin_api import PluginRefusal
except ImportError:  # OpenAVC before 0.38.0
    PluginRefusal = RuntimeError

# StreamDeck and PIL are imported lazily in start() to ensure
# the .deps/ DLL search path is set up by the plugin loader first.
StreamDeck = None
PILHelper = None
Image = None
ImageDraw = None
ImageFont = None

# What a show_message overlay writes on, on the keys, the strip and the screen.
_OVERLAY_BACKGROUND = "#101018"


def _lazy_import():
    """Import streamdeck and PIL after DLL paths are configured."""
    global StreamDeck, PILHelper, Image, ImageDraw, ImageFont
    if StreamDeck is not None:
        return

    from StreamDeck import DeviceManager as _DM
    from StreamDeck.ImageHelpers import PILHelper as _PH
    from PIL import Image as _Img, ImageDraw as _ID, ImageFont as _IF

    StreamDeck = _DM
    PILHelper = _PH
    Image = _Img
    ImageDraw = _ID
    ImageFont = _IF

    # On Linux, LD_LIBRARY_PATH set after process start is not picked up by
    # dlopen (glibc caches it at startup).  Extend the StreamDeck library's
    # HIDAPI search to also try full paths into .deps/, which makes dlopen
    # treat them as path lookups instead of name-only lookups.
    if platform_mod.system() == "Linux":
        _patch_hidapi_search()


def _names_hid_library(error):
    """True for an error that names the native HID library (hidapi): the
    plugin cannot reach a USB deck until it is installed."""
    return "hid" in str(error).lower()


def _patch_hidapi_search():
    """Extend HIDAPI library search to find .so files bundled in .deps/."""
    try:
        from StreamDeck.Transport.LibUSBHIDAPI import LibUSBHIDAPI
    except ImportError:
        return

    deps_dir = str(Path(__file__).parent.parent / ".deps")
    original_load = LibUSBHIDAPI.Library._load_hidapi_library

    def _extended_load(self, search_list):
        # Try the standard system search first
        result = original_load(self, search_list)
        if result is not None:
            return result
        # Fall back to full paths in .deps/
        deps_paths = [os.path.join(deps_dir, os.path.basename(n)) for n in search_list]
        return original_load(self, deps_paths)

    LibUSBHIDAPI.Library._load_hidapi_library = _extended_load


class _DeckSession:
    """Per-deck runtime state for one connected Stream Deck.

    The plugin holds one session per attached deck; every handler and render
    method takes the session it acts on, so multiple decks run independently
    (own page, own pressed keys, own subscriptions, own idle timer).
    """

    def __init__(self, deck):
        self.deck = deck
        self.device_id = None      # HID path — stable while attached
        self.serial = UNKNOWN_SERIAL
        self.model = ""
        self.geometry = {}         # key_count/rows/columns/dials/touch/screens
        self.transport = ""        # virtual / network / usb, once opened
        self.is_network = False
        self.address = ""          # a network deck's host:port
        self.current_page = 0
        self.pressed_keys = set()  # keys currently held (momentary highlight)
        self.hold_tasks = {}       # key_index -> periodic task ID (hold-repeat)
        self.press_times = {}      # key_index -> timestamp (tap/hold mode)
        self.dial_press_times = {} # dial index -> push timestamp (deferred click)
        self.dial_turned = set()   # dials that turned while held (chord, no click)
        self.feedback_subs = []    # state subscription IDs for this deck
        self.auto_page_keys = set()
        self.touch_strip_keys = set()
        self.info_strip_keys = set()
        self.brightness_keys = set()
        self.last_input = 0.0      # loop time of the last key/dial/touch input
        self.idle_dimmed = False   # True while idle_dim has lowered brightness
        self.is_virtual = False
        self.render_version = 0    # bumped when mirrored images change
        self.touch_key_colors = {}  # key index -> current hex color (mirror)
        self.mirror_bump_handle = None  # debounce timer for render_version
        self.overlay_active = False     # show_message overlay suppresses renders
        self.overlay_handle = None      # auto-restore timer for the overlay
        self.strip_image = None         # cached full-strip PIL render (partial updates)
        self.strip_render_task = None   # debounced strip-redraw task
        self.strip_dirty = None         # zone indexes pending redraw, or "all"
        self.flash_zones = {}           # zone index -> clear timer (touch flash)
        self.clock_minute = -1          # last minute an idle clock rendered
        self.macro_keys = {}            # macro_id -> {(page, key_index), ...}
        self.macro_marks = {}           # (page, key_index) -> running|done|error
        self.macro_clear_handles = {}   # (page, key_index) -> flash-clear timer
        self.input_seq = 0              # monotonic counter for the input echo

    def take_profile(self, identity, geometry):
        """Hold what the deck said about itself when it opened
        (deck_profile.py)."""
        self.device_id = identity.device_id
        self.serial = identity.serial
        self.model = identity.model
        self.is_virtual = identity.is_virtual
        self.is_network = identity.is_network
        self.address = identity.address
        self.transport = identity.transport
        self.geometry = geometry

    @property
    def drawable(self):
        """True for an attached deck with a screen to draw on (a Pedal has
        none). Every path that draws asks this first."""
        return bool(self.deck) and bool(self.deck.is_visual())

    def shows(self, page):
        """True when a key stored under ``page`` is on the page showing; page
        None is a locked key, on every page."""
        return page is None or page == self.current_page

    def macro_mark_showing(self, key_index):
        """The macro mark a key shows on the page showing. A locked key's
        mark is stored under page None (lit on every page) and wins."""
        return (
            self.macro_marks.get((None, key_index))
            or self.macro_marks.get((self.current_page, key_index))
        )

    def mark_macro_run(self, spot, mark):
        """Set the macro run mark of the key at ``spot`` (``(page, index)``),
        or drop it for None; either way the end of a flash it showed is
        cancelled."""
        handle = self.macro_clear_handles.pop(spot, None)
        if handle is not None:
            handle.cancel()
        if mark is None:
            self.macro_marks.pop(spot, None)
        else:
            self.macro_marks[spot] = mark

    def flash_macro_mark(self, spot, seconds, flash_ended):
        """Show the mark at ``spot`` for ``seconds``, then call
        ``flash_ended(page, index)``."""
        self.macro_clear_handles[spot] = asyncio.get_event_loop().call_later(
            seconds, flash_ended, *spot
        )

    def end_macro_flash(self, spot, drop_mark):
        """A flash is over: forget its timer and, when ``drop_mark``, its mark.
        True when a mark was dropped."""
        self.macro_clear_handles.pop(spot, None)
        return drop_mark and self.macro_marks.pop(spot, None) is not None

    def cancel_timers(self):
        """Cancel every timer and debounced task the deck has running: the
        mirror bump, the overlay restore, the strip redraw, the touch flashes
        and the macro marks' clears."""
        if self.mirror_bump_handle is not None:
            self.mirror_bump_handle.cancel()
            self.mirror_bump_handle = None
        if self.overlay_handle is not None:
            self.overlay_handle.cancel()
            self.overlay_handle = None
        if self.strip_render_task is not None:
            self.strip_render_task.cancel()
            self.strip_render_task = None
        for handle in self.flash_zones.values():
            handle.cancel()
        self.flash_zones.clear()
        for handle in self.macro_clear_handles.values():
            handle.cancel()
        self.macro_clear_handles.clear()


@dataclass
class _KeyLook:
    """What a key shows before its marks (``_key_look``)."""

    label: str
    icon: object
    bg_color: str
    text_color: str
    value_text: str
    meter: object


class StreamDeckPlugin:

    PLUGIN_INFO = {
        "id": "streamdeck",
        "name": "Elgato Stream Deck",
        "version": "1.38.8",
        "author": "OpenAVC",
        "description": "Use Elgato Stream Deck hardware as a physical control surface.",
        "usage": (
            "Plug in a Stream Deck over USB, add one on the network (Elgato "
            "Network Dock or a Stream Deck Studio's Ethernet port), or add a "
            "virtual deck to design without hardware. Open the **Stream "
            "Deck** view in the sidebar: the picture of your deck is live. "
            "Click any key to set what it does (run a macro, send a device "
            "command, set a variable, switch pages); Shift+click presses it "
            "for real. Add pages with the + tab, and lock keys you want on "
            "every page — like page switchers. Dials, the touch strip, and "
            "the info screen are set up by clicking them in the picture."
        ),
        "category": "control_surface",
        "license": "MIT",
        "platforms": ["win_x64", "linux_x64", "linux_arm64"],
        # 0.37.0: a network deck's connections and the Test button bind to
        # api.local_address (Settings > Network > Reach devices through),
        # which earlier platforms do not have. (The routes' response shapes
        # need 0.24.0, covered by the same floor.)
        "min_openavc_version": "0.37.0",
        "dependencies": ["streamdeck", "pillow>=10.0"],
        "native_dependencies": [
            {
                "id": "hidapi",
                "name": "HIDAPI",
                "version": "0.15.0",
                "license": "BSD-3-Clause",
                "required": True,
                "check": {
                    "type": "library_load",
                    "names": {
                        "Windows": "hidapi.dll",
                        "Linux": "libhidapi-libusb.so",
                    },
                },
                "platforms": {
                    "win_x64": {
                        "url": "https://github.com/libusb/hidapi/releases/download/hidapi-0.15.0/hidapi-win.zip",
                        "type": "zip",
                        "extract": "x64/hidapi.dll",
                    },
                    "linux_x64": {
                        "url": "https://github.com/open-avc/openavc-plugins/releases/download/hidapi-0.15.0/hidapi-linux-x86_64.zip",
                        "type": "zip",
                        "extract": "libhidapi-libusb.so",
                    },
                    "linux_arm64": {
                        "url": "https://github.com/open-avc/openavc-plugins/releases/download/hidapi-0.15.0/hidapi-linux-aarch64.zip",
                        "type": "zip",
                        "extract": "libhidapi-libusb.so",
                    },
                },
            },
        ],
        "capabilities": [
            "state_read",
            "state_write",
            "variable_write",
            "event_emit",
            "event_subscribe",
            "macro_execute",
            "device_command",
            "usb_access",
            # Network-attached decks: outbound TCP to the deck plus mDNS
            # browse (api.mdns_browse, feature-detected — older cores fall
            # back to manual add-by-address).
            "network_listen",
            "http_endpoints",
        ],
    }

    # No CONFIG_SCHEMA: every setting is edited in context inside the
    # Stream Deck view (brightness on the deck, colors under Appearance,
    # pages as tabs), so the generic settings form has nothing to show.
    # The config keys themselves (brightness, button_color, text_color,
    # deck_settings, ...) are documented in AI_GUIDE and the README.

    SURFACE_LAYOUT = {
        "type": "grid",
        "rows": 2,
        "columns": 4,
        "key_size_px": 72,
        "key_spacing_px": 4,
        "supports_pages": True,
        # Device-backed surface: the IDE editor renders only real units
        # (USB, network, or virtual). With none connected it shows a connect /
        # add-deck state instead of this fallback grid.
        "requires_device": True,
        "device_label": "Stream Deck",
        # The editor offers "Add network deck" (ext/network/scan + /test
        # routes, network_decks config array).
        "network": True,
        # The models a virtual deck can be (virtual_deck.py).
        "virtual_models": list(VIRTUAL_MODELS),
    }

    AI_GUIDE = (
        "The default SURFACE_LAYOUT is for the Neo (2x4). The actual hardware "
        "is detected at runtime and published to state: plugin.streamdeck.model, "
        "rows, columns, key_count, dial_count, touch_key_count, "
        "has_touchscreen, and has_info_screen. Read these keys to learn what "
        "the connected deck offers before configuring it — dials and a "
        "touchscreen exist only on the Stream Deck + (dial_count 4, "
        "has_touchscreen true); side touch keys and the info screen only on "
        "the Neo (touch_key_count 2, has_info_screen true). When connected is "
        "false, no deck is attached and the geometry keys are stale or zero. "
        "Touch keys are configured as ordinary 'buttons' entries at the "
        "indices after the LCD keys (Neo: 8 and 9). They have no display — "
        "only their bg_color (and feedback bg colors) show, as an RGB glow; "
        "label and icon are ignored. "
        "When has_info_screen is true, a top-level 'info_strip' object renders "
        "the small info screen: {\"source\": \"state\", \"key\": "
        "\"var.room_temp\", \"label\": \"Temp\"} shows the key's live value "
        "under the label, or {\"source\": \"text\", \"text\": \"Room A\"} "
        "shows static text. Info elements also accept 'icon', 'unit', "
        "'meter', and 'feedback' (see the display fields below), and an "
        "'items' array of up to two such elements renders them side by side "
        "({\"items\": [{\"label\": \"Temp\", \"key\": \"var.room_temp\", "
        "\"unit\": \"F\"}, {\"source\": \"text\", \"text\": \"Room A\"}]}). "
        "With no info_strip configured the screen shows a clock; "
        "{\"source\": \"clock\"} asks for it explicitly and "
        "{\"source\": \"blank\"} turns the screen off. "
        "Brightness can follow state: a top-level 'auto_brightness' array of "
        "{\"level\": 0-100, \"when\": {condition}} rules (same operator "
        "schema; first match wins, no match falls back to the base "
        "'brightness' config). A top-level 'idle_dim' object "
        "{\"after_seconds\": N, \"level\": 0-100} dims the deck after N "
        "seconds without any key/dial/touch input; any input wakes it. "
        "Example: dim to 10 when device.projector_1.power is off, and "
        "idle-dim to 5 after 600 seconds. "
        "Base brightness is the top-level 'brightness' (0-100, default 70). "
        "Per-deck base levels live in a top-level 'deck_settings' map: "
        "{\"deck_settings\": {\"ABC123\": {\"brightness\": 40}}} — a unit "
        "property that never creates a decks override. Default key colors "
        "are the top-level 'button_color' and 'text_color' hex values. "
        "Multiple decks: every connected deck is listed in "
        "plugin.streamdeck.deck_serials (comma-separated) with per-deck state "
        "at plugin.streamdeck.<serial>.* (connected, model, rows, columns, "
        "key_count, dial_count, touch_key_count, has_touchscreen, "
        "has_info_screen, current_page). The un-prefixed singleton keys track "
        "the primary (earliest-connected) deck. By default every deck mirrors "
        "the main config; to give a specific deck its own assignments, add a "
        "top-level 'decks' map keyed by serial — the entry fully replaces the "
        "per-deck sections (buttons, global_buttons, auto_page, dials, "
        "touchscreen, info_strip, auto_brightness, idle_dim, page_names) for "
        "that deck: {\"decks\": {\"ABC123\": {\"buttons\": [...]}}}. "
        "Macros can drive the deck with an event.emit step targeting "
        "plugin.streamdeck.action.<name> — actions: set_page {page}, "
        "set_brightness {level} (holds until the next brightness rule, idle "
        "dim, or wake), flash_key {index, times?}, show_message {text, "
        "seconds?} (splashes the text across the whole deck; the first press "
        "dismisses it without firing that key), identify_deck {}. All accept "
        "an optional serial to target one deck; omitted means every deck. "
        "Example macro step: {\"action\": \"event.emit\", \"event\": "
        "\"plugin.streamdeck.action.show_message\", \"payload\": {\"text\": "
        "\"Mics are LIVE\", \"seconds\": 10}}. "
        "To work with no hardware attached, add a top-level 'virtual_decks' "
        "array: [{\"model\": \"Stream Deck +\", \"serial\": \"VIRT-1\"}] — "
        "models: Stream Deck Neo, Stream Deck Mini, Stream Deck MK.2, Stream "
        "Deck XL, Stream Deck +, Stream Deck Pedal. A virtual deck behaves "
        "exactly like attached hardware (sessions, pages, dials, state, decks "
        "overrides); the user sees and clicks it in the IDE's Live View, and "
        "per-serial state marks it with <serial>.virtual = true. "
        "Decks reached over the network (Elgato Network Dock, or a Stream "
        "Deck Studio's built-in Ethernet) are added with a top-level "
        "'network_decks' array: [{\"host\": \"192.168.1.40\", \"port\": 5343}] "
        "— port is optional (default 5343), and the plugin records the deck's "
        "serial on the entry after the first connect so it can follow the "
        "deck to a new DHCP address. Network decks never attach on their own; "
        "an entry here is the explicit opt-in. Once connected they behave "
        "exactly like USB decks (same buttons/pages/decks config, same state "
        "keys) with <serial>.transport = \"network\" and <serial>.address "
        "set. Connection progress per entry is published at "
        "plugin.streamdeck.net.<host_port>.status (host_port has non-"
        "alphanumerics replaced with _, e.g. 192_168_1_40_5343). "
        "Button indices go left-to-right, "
        "top-to-bottom (e.g. Neo: 0-3 top row, 4-7 bottom row; MK.2: 0-4 top, "
        "5-9 middle, 10-14 bottom). Use page 0 unless multi-page is requested. "
        "A button's 'press' is an array of one or more actions, run in order. "
        "Supported press actions are exactly: macro, device.command, state.set, "
        "and navigate (deck page: page \"__next_page__\", \"__prev_page__\", or a "
        "page index). state.set may write only this plugin's own "
        "plugin.streamdeck.* state or a var.* user variable; writes to device.*, "
        "ui.*, or system.* are ignored. script.call and value_map are panel-only "
        "and do not run on surface buttons — to call a script from a button, run "
        "a one-line macro instead. "
        "Keys that must stay the same on every page (page switchers, "
        "mute-all, help) go in a top-level 'global_buttons' array: same entry "
        "shape as 'buttons' but with NO 'page' field. A global_buttons entry "
        "at an index wins over any per-page button at that index, on every "
        "page. Example page-switcher pair: {\"global_buttons\": [{\"index\": "
        "6, \"icon\": \"chevron-left\", \"bindings\": {\"press\": "
        "[{\"action\": \"navigate\", \"page\": \"__prev_page__\"}]}}, "
        "{\"index\": 7, \"icon\": \"chevron-right\", \"bindings\": "
        "{\"press\": [{\"action\": \"navigate\", \"page\": "
        "\"__next_page__\"}]}}]}. A key that navigates to a specific page "
        "index is automatically highlighted while that page is showing. "
        "Common AV icons: power, volume-2, volume-x, play, pause, square (stop), "
        "skip-back, skip-forward, mic, mic-off, monitor, tv, sun, moon, "
        "thermometer, fan, camera, video, airplay, cast. "
        "Always set a label OR icon (or both) so the button isn't blank. "
        "To hide a button based on state, add a 'visible_when' object to its "
        "bindings (same shape as panel UI visible_when): "
        "{\"key\": \"device.projector_1.power\", \"operator\": \"eq\", "
        "\"value\": \"on\"} — operators eq/ne/gt/lt/gte/lte/truthy/falsy, plus "
        "an 'any':[...] array for OR logic. A hidden button shows as a blank "
        "black key and ignores presses. "
        "To switch pages automatically, add a top-level 'auto_page' array to "
        "the config (alongside 'buttons'): each entry is {\"page\": N, \"when\": "
        "{condition}} using the same operator schema. Rules are evaluated in "
        "order and the first match wins, so put more specific conditions first. "
        "Pages exist by being used — there is no page-count setting: placing "
        "a button, auto_page rule, page name, or numeric navigate target on "
        "page N creates pages 0 through N (every deck always has page 0). "
        "Pages can be labeled with a per-deck 'page_names' section "
        "({\"0\": \"Sources\"}), and decks with a top-level 'deck_names' map "
        "({\"<serial>\": \"Lectern\"}) — names are display-only. "
        "Display fields (shared by touchscreen zones, info elements, and "
        "buttons): 'label_source' (state key whose live value replaces the "
        "label), 'value_source' (state key shown as a live value) with "
        "optional 'unit' (\"dB\", \"%\"), 'icon', and 'meter' — a live level "
        "bar driven by value_source. A meter draws automatically when the "
        "element's adjust/drag_adjust declares min and max; set \"meter\": "
        "{\"min\": 0, \"max\": 100, \"color\": \"#8ab493\", \"thresholds\": "
        "[{\"above\": 90, \"color\": \"#e05341\"}]} to control it, "
        "\"meter\": false to suppress it (button meters are never automatic "
        "and default to 0..100 when bounds are omitted). Zones and info "
        "elements also accept 'feedback' with the same schema as button "
        "feedback (condition or states map) for state-driven colors — e.g. "
        "a zone that turns red while device.amp.clip is true. "
        "When dial_count > 0, a top-level 'dials' array configures the rotary "
        "encoders (not paged — dials keep their assignment on every page): "
        "{\"index\": 0, \"label\": \"Volume\", \"icon\": \"volume-2\", "
        "\"unit\": \"%\", \"adjust\": {\"key\": "
        "\"var.volume\", \"step\": 2, \"min\": 0, \"max\": 100}, \"cw\": "
        "[actions], \"ccw\": [actions], \"press\": [actions], \"long_press\": "
        "[actions], \"hold_threshold_ms\": 500, \"pressed_adjust\": {\"key\": "
        "\"var.volume\", \"step\": 1, \"min\": 0, \"max\": 100}}. 'adjust' "
        "increments the key by step per detent turned (clamped to min/max) — "
        "ideal for volume, mic gain, or camera pan/tilt speed; the key must be "
        "a var.* variable or this plugin's own state, and a macro/trigger can "
        "watch it to drive the device. 'cw'/'ccw' actions run once per detent "
        "turned (capped at 8 per event, so command-per-click mappings track "
        "fast spins). 'press' runs on dial push; with 'long_press' or any "
        "pressed_* field configured, a quick release fires 'press', a release "
        "past hold_threshold_ms fires 'long_press', and turning while held "
        "routes to 'pressed_adjust'/'pressed_cw'/'pressed_ccw' (fine trim) "
        "and fires no click. All actions use the same format as button press "
        "arrays. "
        "When has_touchscreen is true, the touch strip shows one zone per dial "
        "by default: the dial's label, icon, live adjust value, and an "
        "automatic meter when the adjust has bounds — no config needed. "
        "Tapping a dial's zone presses the dial (its 'press'/'long_press'), "
        "or set dial-level 'touch'/'long_touch' overrides; \"fader\": true on "
        "the dial makes taps and swipes on its zone jump the adjust value to "
        "the touched position. With nothing configured at all the strip shows "
        "a clock ({\"touchscreen\": {\"idle\": \"blank\"}} opts out). "
        "To take over the strip, set a top-level 'touchscreen' object: "
        "{\"zones\": [{\"label\": \"Mics\", \"value_source\": \"var.mic_gain\", "
        "\"unit\": \"dB\", \"icon\": \"mic\", \"meter\": {...}, \"feedback\": "
        "{...}, \"touch\": [actions], "
        "\"long_touch\": [actions], \"drag_adjust\": {\"key\": \"var.x\", "
        "\"step\": 1, \"min\": 0, \"max\": 100, \"fader\": true}, "
        "\"bg_color\": \"#1a1a2e\", \"text_color\": \"#e0e0e0\"}]}. Zones "
        "split the strip evenly (or set explicit 'x'/'w' pixel bounds, strip "
        "is 800x100); 'touch' runs on tap, 'long_touch' on long-press "
        "(falls back to 'touch' when absent), and a horizontal swipe steps "
        "'drag_adjust' like turning a dial — or jumps straight to the touched "
        "position when its 'fader' is true (needs min and max). Every touch "
        "briefly flashes the touched zone on the glass. Custom zones replace "
        "ALL default per-dial zones, so carry over any readouts you still "
        "want. "
        "Buttons can show live data too: 'value_source' (+ 'unit') renders "
        "the value under the label, 'label_source' makes the label itself "
        "live, and 'meter' adds a level bar along the key's bottom edge — "
        "e.g. a mic-level key on a Stream Deck XL dashboard."
    )

    EXTENSIONS = {
        "status_cards": [
            {
                "id": "deck_status",
                "label": "Stream Deck",
                "icon": "gamepad",
                "metrics": [
                    {
                        "key": "plugin.streamdeck.connected",
                        "label": "Connected",
                        "format": "boolean",
                    },
                    {
                        "key": "plugin.streamdeck.model",
                        "label": "Model",
                        "format": "string",
                    },
                    {
                        "key": "plugin.streamdeck.serial",
                        "label": "Serial",
                        "format": "string",
                    },
                    {
                        "key": "plugin.streamdeck.current_page",
                        "label": "Page",
                        "format": "number",
                    },
                ],
            },
        ],
        "context_actions": [
            {
                "id": "identify_deck",
                "label": "Identify Stream Deck",
                "icon": "eye",
                "context": "plugin",
                "event": "action.identify",
            },
        ],
        "views": [
            {
                "id": "surface",
                "label": "Stream Deck",
                "icon": "gamepad",
                "renderer": "surface",
            },
        ],
    }

    @classmethod
    def validate_config(cls, config):
        """Sentences naming each part of a proposed config the deck would
        skip or misread: a key with no index, an action type it does not
        run, a virtual deck model it does not have. OpenAVC asks before
        storing a config and refuses one that adds a problem; an empty list
        is a config the deck can use as written."""
        return config_problems(config)

    def __init__(self):
        self.api = None
        # device_id -> _DeckSession, in connect order (first = primary deck)
        self._sessions = {}
        self._loop = None
        self._opening = False    # re-entrancy guard while decks are being opened
        # Live mirror: (serial, item) -> (png bytes, media type). Virtual decks
        # always mirror; physical decks mirror while the Live View is open.
        self._mirror_blobs = {}
        self._mirror_physical = False
        self._icon_map = {}      # icon-name -> unicode code point
        self._icon_cache = {}    # (icon_name, size, color_hex) -> PIL Image
        self._text_font_path = None  # Bundled label font (legible on Linux/Pi)
        self._font_cache = {}    # size -> ImageFont
        self._label_cache = {}   # (label, w, h, color, max_font, max_lines) -> RGBA
        # "host:port" -> DockLink for each configured network deck
        self._dock_links = {}

    async def start(self, api):
        """Initialize and connect to the Stream Deck."""
        self.api = api
        self._loop = asyncio.get_event_loop()
        self._import_libraries()

        # Load Lucide icon font for button icon rendering
        self._load_icon_font()
        # Load the bundled text font for legible labels on every platform
        self._load_text_font()

        await self._publish_unconnected()
        self._check_hid_library()
        await self._wire_up()

    def _import_libraries(self):
        """Import the StreamDeck library and Pillow, after the plugin loader
        has set up the .deps/ DLL search path; refused with what to install
        when either is missing."""
        try:
            _lazy_import()
        except ImportError as e:
            if _names_hid_library(e):
                raise PluginRefusal(self._hidapi_error_message()) from e
            raise PluginRefusal(
                "Failed to import Stream Deck library. "
                "Make sure the 'streamdeck' and 'pillow' packages are installed."
            ) from e

    async def _publish_unconnected(self):
        """What the plugin publishes before any deck is attached. The geometry
        keys are filled in by _open_deck once a deck is detected; the Surface
        Configurator falls back to the static SURFACE_LAYOUT while connected
        is false."""
        await self.api.state_set("connected", False)
        await self.api.state_set("model", "")
        await self.api.state_set("serial", "")
        await self.api.state_set("current_page", 0)
        for key, value in UNCONNECTED_GEOMETRY:
            await self.api.state_set(key, value)
        await self.api.state_set("deck_count", 0)
        await self.api.state_set("deck_serials", "")

    def _check_hid_library(self):
        """Enumerate once now, so a missing native HID library fails the
        plugin with a clear message instead of silently looping in the
        watchdog. Any other enumerate error is raised as it is."""
        try:
            StreamDeck.DeviceManager().enumerate()
        except Exception as e:
            if _names_hid_library(e):
                raise PluginRefusal(self._hidapi_error_message()) from e
            raise

    async def _wire_up(self):
        """Subscribe to context actions and macro events, serve the routes,
        and start the watchdog. The subscriptions are the plugin's, not a
        deck's: they outlive every reconnect."""
        await self.api.event_subscribe(
            "plugin.streamdeck.action.*", self._on_context_action
        )

        # HTTP routes serving the live-mirror images (rendered key/strip
        # images for the IDE's Live View and for virtual decks).
        self.api.register_router(self._build_ext_router())

        # Macro lifecycle events drive run/done/error marks on any key whose
        # bindings reference the macro — no matter who started it.
        await self.api.event_subscribe("macro.*", self._on_macro_event)

        # A single watchdog opens every deck present now, recovers decks after
        # a mid-session unplug, and connects decks that appear later. Its
        # first iteration runs almost immediately.
        self.api.create_periodic_task(
            self._watchdog, interval_seconds=3, name="deck_watchdog"
        )

    async def stop(self):
        """Release every connected Stream Deck."""
        for session in list(self._sessions.values()):
            session.cancel_timers()
            try:
                session.deck.reset()
                session.deck.close()
            except Exception as e:
                self.api.log(f"Error closing deck: {e}", level="warning")
        self._sessions.clear()
        self._mirror_blobs.clear()
        for link in self._dock_links.values():
            link.close()
        self._dock_links.clear()
        self.api.log("Stream Deck plugin stopped")

    async def health_check(self):
        """Report deck connection health."""
        connected = [
            s for s in self._sessions.values()
            if s.deck and s.deck.is_open()
        ]
        if connected:
            models = ", ".join(f"{s.model} ({s.serial})" for s in connected)
            return {"status": "ok", "message": f"Connected: {models}"}
        return {"status": "degraded", "message": "No Stream Deck connected"}

    async def on_config_changed(self, new_config):
        """Hot-apply a config change without restarting (no USB churn).

        The platform has already swapped ``self.api.config`` when this runs.
        Each session rebuilds its subscriptions against the new config view,
        clamps/re-evaluates its page, re-applies brightness, and re-renders —
        physical deck handles are never closed, so the deck doesn't blank.
        Virtual deck additions/removals reconcile on the next watchdog tick
        (within seconds). Returning True tells the platform to skip the
        stop/start restart.
        """
        for session in list(self._sessions.values()):
            # The config under any in-flight press just changed — end hold
            # repeats now rather than trusting the (possibly re-bound)
            # release to find them. A still-held key simply re-presses.
            self._end_hold_repeats(session)
            await self._drop_feedback_subscriptions(session)
            await self._setup_feedback_subscriptions(session)
            await self._settle_page(session)

            await self._apply_active_brightness(session)
            await self._render_all_buttons(session)
            await self._render_touchscreen(session)
            await self._render_info_strip(session)

        await self._publish_deck_state()
        return True

    async def _settle_page(self, session):
        """After a config change: a page past the new last page moves to the
        last one, then the first matching auto_page rule's page, if any,
        is shown."""
        max_pages = self._effective_page_count(session)
        if session.current_page >= max_pages:
            await self._change_page(session, max_pages - 1)
        target = await self._evaluate_auto_page(session)
        if target is not None and target != session.current_page:
            await self._change_page(session, target)

    def _end_hold_repeats(self, session):
        """Cancel every hold-repeat a deck has running."""
        for task_id in list(session.hold_tasks.values()):
            self.api.cancel_task(task_id)
        session.hold_tasks.clear()

    async def _drop_feedback_subscriptions(self, session):
        """End a deck's state subscriptions; one already gone is skipped."""
        for sub_id in session.feedback_subs:
            try:
                await self.api.state_unsubscribe(sub_id)
            except Exception:
                pass
        session.feedback_subs = []

    # ──── Device Management ────

    def _primary_session(self):
        """The earliest-connected deck still attached (drives singleton keys)."""
        return next(iter(self._sessions.values()), None)

    def _deck_config(self, session):
        """Per-deck config view.

        A ``decks[serial]`` override fully replaces the per-deck sections
        (buttons, global_buttons, auto_page, dials, touchscreen, info_strip,
        auto_brightness, idle_dim, page_names) for that deck. Decks without
        an override mirror the flat top-level config — so a single deck never
        deals with serials, and a second deck shows the same controls until
        it's given its own layout. Unit properties (``deck_settings``,
        ``deck_names``) live outside this view on purpose: setting them never
        forks a layout.
        """
        decks = self.api.config.get("decks")
        if isinstance(decks, dict):
            override = decks.get(session.serial)
            if isinstance(override, dict):
                return override
        return self.api.config

    def _deck_setting(self, session, key, default):
        """A scalar setting from the deck's config view, falling back to the
        flat config (so an override can omit e.g. colors and inherit them)."""
        cfg = self._deck_config(session)
        if isinstance(cfg, dict) and key in cfg:
            return cfg[key]
        return self.api.config.get(key, default)

    async def _publish_deck_state(self):
        """Publish per-deck state keys and the primary-deck singleton keys.

        Every deck gets ``plugin.streamdeck.<serial>.*`` keys; the un-prefixed
        singleton keys mirror the primary deck so the status card and simple
        automations keep working unchanged with one deck.
        """
        sessions = list(self._sessions.values())
        await self.api.state_set("deck_count", len(sessions))
        await self.api.state_set("deck_serials", ",".join(s.serial for s in sessions))

        primary = self._primary_session()
        if primary is None:
            await self.api.state_set("connected", False)
        else:
            await self.api.state_set("connected", True)
            await self.api.state_set("model", primary.model)
            await self.api.state_set("serial", primary.serial)
            for key, value in primary.geometry.items():
                await self.api.state_set(key, value)
            await self.api.state_set("current_page", primary.current_page)

        deck_names = self.api.config.get("deck_names")
        deck_names = deck_names if isinstance(deck_names, dict) else {}
        for session in sessions:
            prefix = session.serial
            await self.api.state_set(f"{prefix}.connected", True)
            await self.api.state_set(f"{prefix}.model", session.model)
            await self.api.state_set(
                f"{prefix}.name", str(deck_names.get(session.serial, ""))
            )
            await self.api.state_set(
                f"{prefix}.address", getattr(session, "address", "")
            )
            for key, value in session.geometry.items():
                await self.api.state_set(f"{prefix}.{key}", value)
            await self.api.state_set(f"{prefix}.current_page", session.current_page)

    async def _open_deck(self, deck):
        """Open a deck, configure it, and set up its callbacks."""
        deck.open()
        deck.reset()

        session = _DeckSession(deck)
        identity = deck_identity(deck)
        tell_link_serial(deck, identity)
        session.take_profile(identity, deck_geometry(deck, identity))

        self._sessions[session.device_id] = session

        # Apply brightness (base config level or the first matching
        # auto_brightness rule), and start the idle timer fresh.
        session.last_input = asyncio.get_event_loop().time()
        deck.set_brightness(await self._current_brightness_level(session))
        self._wire_callbacks(session)

        # Publish the detected hardware to state. The Surface Configurator
        # prefers these keys over the static SURFACE_LAYOUT while connected,
        # so the editor always draws the surface that's actually plugged in.
        await self._publish_deck_state()

        self.api.log(connected_line(identity, session.geometry))
        await self.api.event_emit(
            "connected", {"model": session.model, "serial": session.serial}
        )

        # Subscribe to state changes for feedback, visibility, and auto-page keys
        await self._setup_feedback_subscriptions(session)
        await self._show_opening_page(session)

        # Render all buttons for the current page, then the secondary displays
        await self._render_all_buttons(session)
        await self._render_touchscreen(session)
        await self._render_info_strip(session)

    def _wire_callbacks(self, session):
        """Bind a deck's key, dial and touch callbacks to its session, the
        dial and touch ones only where the deck has them (async variants:
        they fire on the plugin's loop)."""
        deck = session.deck
        deck.set_key_callback_async(
            functools.partial(self._on_key_change, session), loop=self._loop
        )
        if session.geometry["dial_count"] > 0:
            deck.set_dial_callback_async(
                functools.partial(self._on_dial_event, session), loop=self._loop
            )
        if session.geometry["has_touchscreen"]:
            deck.set_touchscreen_callback_async(
                functools.partial(self._on_touchscreen_event, session),
                loop=self._loop,
            )

    async def _show_opening_page(self, session):
        """Open on the first matching auto_page rule's page, set before the
        first render so the deck never shows page 0 and then switches. The
        un-prefixed page key follows only the primary deck."""
        initial_page = await self._evaluate_auto_page(session)
        if initial_page is None:
            return
        session.current_page = initial_page
        await self.api.state_set(f"{session.serial}.current_page", initial_page)
        if self._primary_session() is session:
            await self.api.state_set("current_page", initial_page)

    async def _watchdog(self):
        """Keep every deck connected: open new ones, recover after unplug.

        Runs on a single periodic task for the plugin's whole lifetime. A deck
        unplugged mid-session is detected by its dead session and torn down
        (the bundled library closes the deck object on a transport error but
        never re-opens it); newly attached decks are recognized by their HID
        path and opened. Periodic ticks never overlap, but an ``_opening``
        guard is kept so a slow open can't be re-entered.

        Each tick tends the decks it holds before it looks for new ones, so a
        deck lost this tick frees its path for a deck found at it.
        """
        if self._opening:
            return

        declared_virtual = self._virtual_deck_entries()
        await self._tend_held_decks({entry["serial"] for entry in declared_virtual})

        new_decks = self._unheld_usb_decks() + self._unmade_virtual_decks(declared_virtual)
        # Network decks: reconnect configured units (with backoff) and add
        # any decks whose connection came up since the last tick. It appends
        # as it goes, so a deck it added before failing is still opened.
        try:
            await self._reconcile_network_decks(new_decks)
        except Exception as e:
            self.api.log(f"Network deck reconcile error: {e}", level="warning")

        if new_decks:
            await self._open_decks(new_decks)

    async def _tend_held_decks(self, declared_serials):
        """Run each held deck's tick, or tear it down when it went away."""
        for session in list(self._sessions.values()):
            if self._still_held(session, declared_serials):
                await self._tick_held_deck(session)
            else:
                await self._handle_deck_lost(session)

    @staticmethod
    def _still_held(session, declared_serials):
        """True while a deck is open and connected, and, for a virtual deck,
        still declared in the config."""
        try:
            healthy = session.deck.is_open() and session.deck.connected()
        except Exception:
            healthy = False
        if session.is_virtual and session.serial not in declared_serials:
            healthy = False
        return healthy

    async def _tick_held_deck(self, session):
        """What a held deck does on each watchdog tick: the idle-dim clock,
        then the idle clock's minute."""
        await self._check_idle_dim(session)
        await self._tick_clock(session)

    def _unheld_usb_decks(self):
        """The attached decks the plugin does not hold yet, matched by HID
        path; a deck whose path cannot be read is never held, so it is
        offered again each tick. Empty when the library cannot enumerate."""
        try:
            decks = StreamDeck.DeviceManager().enumerate()
        except Exception:
            decks = []
        new_decks = []
        for deck in decks:
            try:
                device_id = deck.id()
            except Exception:
                device_id = None
            if device_id is None or device_id not in self._sessions:
                new_decks.append(deck)
        return new_decks

    def _unmade_virtual_decks(self, declared_virtual):
        """A deck for each declared virtual deck that is not running yet."""
        return [
            VirtualDeck(entry["model"], entry["serial"], VIRTUAL_MODELS[entry["model"]])
            for entry in declared_virtual
            if f"virtual:{entry['serial']}" not in self._sessions
        ]

    async def _open_decks(self, new_decks):
        """Open each deck in turn under the ``_opening`` guard; one that fails
        is logged and the rest still open."""
        self._opening = True
        try:
            for deck in new_decks:
                try:
                    await self._open_deck(deck)
                except Exception as e:
                    self.api.log(f"Failed to open Stream Deck: {e}", level="warning")
        finally:
            self._opening = False

    async def _handle_deck_lost(self, session):
        """Tear down one deck that has gone away (others keep running).

        Cancels in-flight hold-repeat tasks, drops the deck's feedback
        subscriptions (re-created on re-open), closes the stale deck object,
        and publishes the disconnect. The context-action subscription is left
        intact — it lives for the plugin's whole lifetime, not per-deck.
        """
        self.api.log(
            f"Stream Deck disconnected ({session.model} S/N: {session.serial})",
            level="warning",
        )

        self._end_hold_repeats(session)
        session.press_times.clear()
        session.dial_press_times.clear()
        session.dial_turned.clear()
        await self._drop_feedback_subscriptions(session)

        self._sessions.pop(session.device_id, None)
        try:
            session.deck.close()
        except Exception:
            pass

        await self.api.state_set(f"{session.serial}.connected", False)
        session.cancel_timers()
        session.strip_image = None
        session.strip_dirty = None
        session.overlay_active = False
        self._drop_mirror_blobs(session.serial)
        await self._publish_deck_state()
        await self.api.event_emit("disconnected", {"serial": session.serial})

    # ──── Virtual decks + live mirror ────

    def _virtual_deck_entries(self):
        """The virtual decks the config declares (declared_decks.py)."""
        return virtual_deck_entries(self.api.config.get("virtual_decks", []))

    # ──── Network decks ────

    def _network_deck_entries(self):
        """The network decks the config declares (declared_decks.py).

        ``serial`` is recorded automatically after the first connect so a
        deck that moves to a new DHCP address can be found again through
        discovery. Entries are explicit opt-in: the plugin never attaches to
        a deck on the network that has not been added here.
        """
        return network_deck_entries(self.api.config.get("network_decks", []))

    async def _reconcile_network_decks(self, new_decks):
        """Watchdog half of network decks.

        Keeps one DockLink per configured entry, publishes each link's
        status for the editor (``plugin.streamdeck.net.<key>.status``),
        appends deck objects for transports that came up, records the
        serials a deck reported, and looks a known deck up through discovery
        when it stops answering.
        """
        declared = {e["key"]: e for e in self._network_deck_entries()}
        await self._close_undeclared_links(declared)
        unrecorded = False
        for key, entry in declared.items():
            link = self._link_for(key, entry)
            self._add_net_decks(new_decks, await link.ensure(self._loop, self.api.log))
            await self.api.state_set(f"net.{link.state_key}.address", key)
            await self.api.state_set(f"net.{link.state_key}.status", link.status)
            if unrecorded_serials(entry, link.reported_serials()):
                unrecorded = True
            self._look_up_if_lost(key, link, link.answers_to(entry))
        if unrecorded:
            await self._persist_network_serials()

    async def _close_undeclared_links(self, declared):
        """Close the link of every deck the config no longer declares, and say
        so in its status."""
        for key, link in list(self._dock_links.items()):
            if key not in declared:
                link.close()
                del self._dock_links[key]
                await self.api.state_set(f"net.{link.state_key}.status", "removed")

    def _link_for(self, key, entry):
        """A declared deck's link, made on its first tick from the entry's
        address and the serials it recorded."""
        link = self._dock_links.get(key)
        if link is None:
            link = DockLink(entry["host"], entry["port"], self.api.local_address)
            link.remember_serials(entry)
            self._dock_links[key] = link
        return link

    def _add_net_decks(self, new_decks, transports):
        """Add a deck object for each transport that came up and is not open
        yet, of the class the library names for its product id."""
        for transport in transports:
            if transport.path() not in self._sessions:
                cls = net_deck_class(transport.product_id(), StreamDeck)
                if cls is not None:
                    new_decks.append(cls(transport))

    def _look_up_if_lost(self, key, link, serials):
        """Look a deck up by every serial it answers to, once per outage
        (DockLink.claim_look_up, asked last: asking takes the claim), when
        discovery is available."""
        if (
            serials
            and callable(getattr(self.api, "mdns_browse", None))
            and link.claim_look_up()
        ):
            self.api.create_task(
                self._net_reresolve(key, serials),
                name=f"net_reresolve_{link.state_key}",
            )

    async def _persist_network_serials(self):
        """Record each deck's serials on its ``network_decks`` entry
        (declared_decks.record_serials) so discovery can follow the deck if
        its address changes later."""

        def record(entry, address):
            link = self._dock_links.get(address)
            return link is not None and record_serials(entry, link.reported_serials())

        updated, changed = edit_network_decks(self.api.config.get("network_decks"), record)
        if changed:
            await self._save_network_decks(updated)

    async def _net_reresolve(self, key, serials):
        """A configured deck stopped answering: browse discovery for any of
        its known serials (deck or advertised unit) and follow the first
        answer to its new address. An answer at the address it already has
        ends the look-up."""
        try:
            results = await self.api.mdns_browse([NET_MDNS_SERVICE], duration=5.0)
        except Exception as e:
            self.api.log(f"Network deck discovery failed: {e}", level="warning")
            return
        found = next((r for r in results if answer_serial(r) in serials), None)
        if found is None:
            return
        new_host, new_port = answer_address(found)
        if not new_host or f"{new_host}:{new_port}" == key:
            return

        def move(entry, address):
            if address != key:
                return False
            entry["host"] = new_host
            entry["port"] = new_port
            return True

        updated, changed = edit_network_decks(self.api.config.get("network_decks"), move)
        if changed:
            self.api.log(
                f"Network deck {answer_serial(found)} answered at "
                f"{new_host}:{new_port} (was {key}); following it"
            )
            await self._save_network_decks(updated)

    async def _save_network_decks(self, decks):
        """Save the config with ``decks`` as its ``network_decks`` list."""
        config = dict(self.api.config)
        config["network_decks"] = decks
        await self.api.save_config(config)

    def _build_ext_router(self):
        """FastAPI router for the IDE: the live-mirror images, and the Add
        network deck dialog's scan and connection test.

        ``GET /api/plugins/streamdeck/ext/live/{serial}/{item}`` returns the
        most recent rendered image for a key (``key_<n>``), the touchscreen
        strip (``touchscreen``), or the info screen (``screen``).
        """
        from fastapi import APIRouter
        from fastapi.responses import Response

        router = APIRouter()

        @router.get("/live/{serial}/{item}")
        async def live_image(serial: str, item: str):
            blob = self._mirror_blobs.get((serial, item))
            if blob is None:
                return Response(status_code=404)
            data, media_type = blob
            return Response(
                content=data,
                media_type=media_type,
                headers={"Cache-Control": "no-store"},
            )

        @router.post("/network/scan")
        async def network_scan():
            """Find network decks on the LAN. Returns browse_available=False
            where multicast discovery can't run (older cores, Docker bridge
            networks) — the manual add-by-address path always works."""
            return await self._scan_network()

        @router.post("/network/test")
        async def network_test(payload: dict):
            """Try a TCP connection to host:port (the manual-add Test button)."""
            return await self._test_network_address(payload)

        return router

    async def _scan_network(self):
        """The scan route's answer: the decks discovery finds
        (deck_answers.scan_listing), or ``browse_available`` False when it
        cannot browse."""
        entries = self._network_deck_entries()
        browse = getattr(self.api, "mdns_browse", None)
        if not callable(browse):
            return {"browse_available": False, "found": []}
        try:
            results = await browse([NET_MDNS_SERVICE], duration=4.0)
        except Exception as e:
            self.api.log(f"Network deck scan failed: {e}", level="warning")
            return {"browse_available": False, "found": []}
        return {"browse_available": True, "found": scan_listing(results, entries)}

    async def _test_network_address(self, payload):
        """Try a TCP connection to host:port (the manual-add Test button),
        from the adapter Reach devices through names."""
        host = str(payload.get("host", "")).strip()
        try:
            port = int(payload.get("port", NET_DEFAULT_PORT))
        except (TypeError, ValueError):
            return {"success": False, "error": "invalid port"}
        if not host or not (0 < port < 65536):
            return {"success": False, "error": "enter a host or IP address"}
        try:
            ip = self.api.local_address(host)
        except ConnectionError as e:
            # The adapter chosen in Reach devices through has no address;
            # the sentence names it and says where to change it.
            return {"success": False, "error": str(e)}
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host, port, local_addr=(ip, 0) if ip else None
                ),
                timeout=3.0,
            )
        except Exception as e:
            return {"success": False, "error": classify_net_error(e)}
        writer.close()
        return {"success": True}

    def _mirror_enabled(self, session):
        """Virtual decks always mirror; physical decks only while a Live View
        has turned mirroring on (set_live_mirror context action)."""
        return session.is_virtual or self._mirror_physical

    def _mirror(self, session, item, image):
        """Store a rendered PIL image for the Live View and schedule a bump.

        Captures the pre-native image (the native bytes are model-flipped /
        rotated). Failures never break rendering.
        """
        if not self._mirror_enabled(session) or Image is None:
            return
        try:
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
        except Exception:
            return
        # Soft cap so a pathological config can't grow memory unbounded; a
        # full XL with strips is ~35 entries per deck.
        if len(self._mirror_blobs) > 512:
            self._mirror_blobs.pop(next(iter(self._mirror_blobs)), None)
        self._mirror_blobs[(session.serial, item)] = (buffer.getvalue(), "image/png")
        self._schedule_mirror_bump(session)

    def _drop_mirror_blobs(self, serial):
        for key in [k for k in self._mirror_blobs if k[0] == serial]:
            self._mirror_blobs.pop(key, None)

    def _schedule_mirror_bump(self, session):
        """Debounce render_version so a full-page render bumps state once."""
        if session.mirror_bump_handle is not None:
            session.mirror_bump_handle.cancel()
        loop = asyncio.get_event_loop()
        session.mirror_bump_handle = loop.call_later(
            0.05, lambda: asyncio.ensure_future(self._publish_mirror_version(session))
        )

    async def _publish_mirror_version(self, session):
        session.mirror_bump_handle = None
        if session.device_id not in self._sessions:
            return
        session.render_version += 1
        await self.api.state_set(
            f"{session.serial}.render_version", session.render_version
        )
        for index, color in list(session.touch_key_colors.items()):
            await self.api.state_set(f"{session.serial}.touch_key.{index}", color)

    async def _simulate_input(self, payload):
        """Run a simulated key/dial/touch input on a deck.

        Used by the IDE workbench (clicking a virtual or physical deck's
        image). Runs the exact same handler paths as hardware input. The
        payload (``{serial, type: key|dial_turn|dial_push|touch, ...}``) is
        read by context_actions.simulated_inputs.
        """
        serial = str(payload.get("serial", ""))
        session = next(
            (s for s in self._sessions.values() if s.serial == serial), None
        )
        if session is None:
            self.api.log(
                f"simulate_input: no connected deck with serial '{serial}'",
                level="warning",
            )
            return
        deck = session.deck
        inputs = simulated_inputs(
            payload, lambda: deck.key_count() + deck.touch_key_count()
        )
        for kind, *args in inputs:
            if kind == "key":
                await self._on_key_change(session, deck, *args)
            elif kind == "dial":
                index, event, value = args
                await self._on_dial_event(
                    session, deck, index, SimpleNamespace(name=event), value
                )
            else:
                event, value = args
                await self._on_touchscreen_event(
                    session, deck, SimpleNamespace(name=event), value
                )

    async def _set_live_mirror(self, payload):
        """Toggle mirroring of physical decks (virtual decks always mirror)."""
        turn_on = bool(payload.get("on"))
        if turn_on == self._mirror_physical:
            return
        self._mirror_physical = turn_on
        if turn_on:
            # Populate the mirror with the current rendering of every deck.
            for session in list(self._sessions.values()):
                await self._render_all_buttons(session)
                await self._render_touchscreen(session)
                await self._render_info_strip(session)
        else:
            for session in list(self._sessions.values()):
                if not session.is_virtual:
                    self._drop_mirror_blobs(session.serial)

    # ──── Automation actions (driven by macro event.emit steps) ────

    def _sessions_for(self, serial):
        """Sessions an automation action targets: one serial, or every deck."""
        sessions = list(self._sessions.values())
        if serial:
            return [s for s in sessions if s.serial == serial]
        return sessions

    async def _flash_key(self, session, index, times):
        """Flash one key white to draw attention to it."""
        deck = session.deck
        if not session.drawable or session.overlay_active:
            return
        if not 0 <= index < deck.key_count() + deck.touch_key_count():
            return
        is_touch = self._is_touch_key(session, index)
        white = None
        if not is_touch and Image is not None:
            white = self._create_button_image(session, "", "#ffffff", "#ffffff")
        for _ in range(times):
            if is_touch:
                self._apply_key_color(session, index, "#ffffff")
            elif white is not None:
                self._apply_key_image(session, index, white)
            await asyncio.sleep(0.18)
            await self._render_button(session, index)
            await asyncio.sleep(0.12)

    async def _show_message(self, session, text, seconds):
        """Splash a message across the whole deck, restoring it afterwards.

        While the overlay is up, normal renders are suppressed and the first
        key/dial/touch input dismisses it without firing any action.
        """
        if session.overlay_handle is not None:
            session.overlay_handle.cancel()
            session.overlay_handle = None
        session.overlay_active = True
        await self._draw_overlay(session, text)
        loop = asyncio.get_event_loop()
        session.overlay_handle = loop.call_later(
            seconds, lambda: asyncio.ensure_future(self._clear_overlay(session))
        )

    async def _clear_overlay(self, session):
        """Drop a show_message overlay and restore the normal rendering."""
        if session.overlay_handle is not None:
            session.overlay_handle.cancel()
            session.overlay_handle = None
        if not session.overlay_active:
            return
        session.overlay_active = False
        await self._render_all_buttons(session)
        await self._render_touchscreen(session)
        await self._render_info_strip(session)

    async def _draw_overlay(self, session, text):
        """Show ``text`` everywhere a deck can: across the keys (one canvas
        cut into key tiles, the touch keys lit white), then on the touch strip
        and the info screen. A strip or screen send the deck refuses is
        dropped silently; a refused key tile is logged as any key is."""
        deck = session.deck
        if not session.drawable or Image is None:
            return
        key_w, key_h = deck.key_image_format()["size"]
        if not key_w or not key_h:
            return
        self._overlay_keys(session, text, key_w, key_h)
        if deck.is_touch():
            strip = self._overlay_panel(text, deck, self._OVERLAY_ON_STRIP)
            if strip is not None:
                self._mirror(session, "touchscreen", strip)
                self._send_strip_image(deck, strip)
        screen = self._overlay_panel(text, deck, self._OVERLAY_ON_SCREEN)
        if screen is not None:
            self._mirror(session, "screen", screen)
            self._send_screen_image(deck, screen)

    def _overlay_keys(self, session, text, key_w, key_h):
        """The overlay on the keys: ``text`` on one canvas the size of the
        key grid, each LCD key showing its tile, the touch keys white."""
        deck = session.deck
        rows, cols = deck.key_layout()
        canvas = Image.new("RGB", (cols * key_w, rows * key_h), _OVERLAY_BACKGROUND)
        self._paste_label(
            canvas, text, "#ffffff",
            (key_w // 4, key_h // 4, cols * key_w - key_w // 2, rows * key_h - key_h // 2),
            max_font=key_h, max_lines=max(2, rows),
        )
        for index in range(deck.key_count()):
            row, col = divmod(index, cols)
            tile = canvas.crop(
                (col * key_w, row * key_h, (col + 1) * key_w, (row + 1) * key_h)
            )
            self._apply_key_image(session, index, tile)
        for index in range(
            deck.key_count(), deck.key_count() + deck.touch_key_count()
        ):
            self._apply_key_color(session, index, "#ffffff")

    # Where the overlay writes on the strip and the screen: the deck's format
    # reader for its size, the text's inset (x, y) on each side, and the
    # smallest font it may shrink to.
    _OVERLAY_ON_STRIP = ("touchscreen_image_format", (8, 4), 16)
    _OVERLAY_ON_SCREEN = ("screen_image_format", (4, 2), 14)

    def _overlay_panel(self, text, deck, where):
        """The overlay's ``text`` on the strip or screen ``where`` names
        (``_OVERLAY_ON_STRIP`` or ``_OVERLAY_ON_SCREEN``), sized as the deck
        reports; None when the deck has none or cannot say its size."""
        format_reader, (inset_x, inset_y), min_font = where
        size = self._panel_size(deck, format_reader)
        if size is None:
            return None
        width, height = size
        panel = Image.new("RGB", size, _OVERLAY_BACKGROUND)
        self._paste_label(
            panel, text, "#ffffff",
            (inset_x, inset_y, width - 2 * inset_x, height - 2 * inset_y),
            max_font=max(min_font, height // 2), max_lines=2,
        )
        return panel

    @staticmethod
    def _panel_size(deck, format_reader):
        """A touch strip's or info screen's (width, height), from the deck's
        ``touchscreen_image_format`` or ``screen_image_format``; None when
        the deck lacks the reader, it raises, or either side is 0."""
        try:
            width, height = getattr(deck, format_reader)()["size"]
        except Exception:
            return None
        return (width, height) if width and height else None

    # ──── Key Handling ────

    async def _on_key_change(self, session, deck, key_index, pressed):
        """Handle a physical key press or release.

        In order: the release ends the press, an overlay swallows the input,
        the press is echoed and announced, then the key's entry runs in its
        press mode (``_KEY_MODES``; tap when the mode is anything else).
        """
        await self._note_input(session)
        if not pressed:
            await self._end_press(session, key_index)

        if await self._overlay_swallows_key(session, pressed):
            return
        if pressed:
            await self._publish_input_echo(session, "key", key_index)
        page = session.current_page
        event_type = "press" if pressed else "release"
        await self.api.event_emit(
            f"button.{event_type}",
            {"key": key_index, "page": page, "serial": session.serial},
        )

        assignment = self._get_button_assignment(session, page, key_index)
        if not assignment:
            return
        # A hidden button (visible_when false) is inert, and one that became
        # hidden mid-press must not leak a hold task or fire a stale action.
        if not await self._is_button_visible(assignment):
            self._drop_press(session, key_index)
            return
        if pressed:
            await self._hold_highlight(session, key_index)

        actions = press_actions(assignment.get("bindings", {}))
        if not actions or not actions[0]:
            return
        handler = self._key_mode_handler(actions[0].get("mode", "tap"))
        await getattr(self, handler)(session, key_index, pressed, actions)

    # What a key's press does, by the first action's mode.
    _KEY_MODES = (
        ("hold_repeat", "_press_hold_repeat"),
        ("toggle", "_press_toggle"),
        ("tap_hold", "_press_tap_hold"),
    )

    @classmethod
    def _key_mode_handler(cls, mode):
        """The method that runs a key in ``mode``; a tap for any other."""
        return next((name for each, name in cls._KEY_MODES if each == mode), "_press_tap")

    async def _overlay_swallows_key(self, session, pressed):
        """True while a show_message overlay is up. The first press dismisses
        it and is swallowed (it must not fire the key's own action), and so is
        the matching release (its press already ended)."""
        if not session.overlay_active:
            return False
        if pressed:
            await self._clear_overlay(session)
        return True

    async def _end_press(self, session, key_index):
        """End a key's press at its release, whatever changed in between (page
        flipped, buttons edited or deleted, overlay appeared, visibility
        changed). Skipping this is how keys get stuck drawn pressed and how a
        hold-repeat task leaks and fires its action forever."""
        self._cancel_hold(session, key_index)
        was_pressed = key_index in session.pressed_keys
        session.pressed_keys.discard(key_index)
        if was_pressed and session.drawable:
            try:
                await self._render_button(session, key_index)
            except Exception:
                pass

    def _cancel_hold(self, session, key_index):
        """Stop a key's hold-repeat task, if it has one."""
        task_id = session.hold_tasks.pop(key_index, None)
        if task_id:
            self.api.cancel_task(task_id)

    def _drop_press(self, session, key_index):
        """Forget everything a key's press has under way: its hold task, its
        tap_hold start time and its press highlight."""
        self._cancel_hold(session, key_index)
        session.press_times.pop(key_index, None)
        session.pressed_keys.discard(key_index)

    async def _hold_highlight(self, session, key_index):
        """Mark a key held and redraw it. The mark lives in the render path,
        so a feedback or toggle redraw keeps the highlight rather than
        fighting it."""
        session.pressed_keys.add(key_index)
        if session.drawable:
            await self._render_button(session, key_index)

    async def _press_tap(self, session, key_index, pressed, actions):
        """Tap: every action in order, on the press."""
        if pressed:
            await self._execute_actions(session, actions, f"key {key_index}")

    async def _press_hold_repeat(self, session, key_index, pressed, actions):
        """Hold-repeat: the first action every ``hold_repeat_ms`` (200) while
        held. The release's ``_end_press`` stops it."""
        if not pressed:
            return
        press = actions[0]
        self._cancel_hold(session, key_index)
        # The task id is stored before any await, so a release that arrives
        # while the first action runs finds the task to cancel. The periodic
        # task fires the action almost at once.
        interval = press.get("hold_repeat_ms", 200) / 1000.0
        session.hold_tasks[key_index] = self.api.create_periodic_task(
            lambda: self._execute_action(session, press, f"key {key_index}"),
            interval_seconds=interval,
            name=f"hold_repeat_{session.serial}_{key_index}",
        )

    async def _press_toggle(self, session, key_index, pressed, actions):
        """Toggle: on the press, the ``off_action`` while the toggle key reads
        on, else the first action; a key with on/off labels redraws a tenth of
        a second later, once the state has had time to change."""
        if not pressed:
            return
        press = actions[0]
        off_action = press.get("off_action")
        is_on = await self._toggle_is_on(press)
        if is_on and off_action and isinstance(off_action, dict):
            await self._execute_action(session, off_action, f"key {key_index}")
        else:
            await self._execute_action(session, press, f"key {key_index}")
        if press.get("on_label", "") or press.get("off_label", ""):
            await asyncio.sleep(0.1)
            await self._render_button(session, key_index)

    async def _press_tap_hold(self, session, key_index, pressed, actions):
        """Tap or hold: at the release, the ``hold_action`` when the key was
        held ``hold_threshold_ms`` (500) or longer, else the first action."""
        press = actions[0]
        threshold = press.get("hold_threshold_ms", 500) / 1000.0
        if pressed:
            session.press_times[key_index] = asyncio.get_event_loop().time()
            return
        if key_index not in session.press_times:
            # A release with no recorded press (the press was swallowed
            # while the button was hidden) fires nothing.
            return
        held = asyncio.get_event_loop().time() - session.press_times.pop(key_index)
        hold_action = press.get("hold_action")
        if held >= threshold and hold_action and isinstance(hold_action, dict):
            await self._execute_action(session, hold_action, f"key {key_index}")
        else:
            await self._execute_action(session, press, f"key {key_index}")

    async def _toggle_is_on(self, press):
        """Whether a toggle key's ``toggle_key`` reads as on (never, without
        one)."""
        toggle_key = press.get("toggle_key", "")
        if not toggle_key:
            return False
        value = await self.api.state_get(toggle_key)
        return reads_as_on(value, press.get("toggle_value"))

    async def _execute_actions(self, session, actions, source):
        """Execute a list of action bindings sequentially, in order."""
        for action in actions:
            if isinstance(action, dict):
                await self._execute_action(session, action, source)

    async def _execute_action(self, session, action_binding, source):
        """Run one action binding by its type: one runner for each of
        ``ACTION_KINDS`` (``_ACTION_RUNNERS``); any other type does nothing.
        ``source`` only labels log lines (e.g. ``key 3``, ``dial 0``).
        """
        kind = action_binding.get("action", "")
        runner = self._ACTION_RUNNERS.get(kind) if isinstance(kind, str) else None
        if runner is not None:
            await runner(self, session, action_binding, source)

    async def _run_navigate(self, session, action_binding, source):
        """Change the deck's page: one step for the next- or previous-page
        word, else to the page the value names (pages.py); a value that
        names no page does nothing. A TypeError or ValueError raised while
        changing to a named page is dropped here; a step lets it through."""
        page = action_binding.get("page", "")
        step = page_step(page)
        if step is not None:
            await self._change_page(session, session.current_page + step)
            return
        number = page_number(page)
        if number is None:
            return
        try:
            await self._change_page(session, number)
        except (TypeError, ValueError):
            pass

    async def _run_macro(self, session, action_binding, source):
        """Run the binding's macro; a failure is logged, never raised."""
        macro = action_binding.get("macro", "")
        if not macro:
            return
        try:
            await self.api.macro_execute(macro)
            self.api.log(f"Executed macro '{macro}' from {source}", level="debug")
        except Exception as e:
            self.api.log(f"Error executing macro '{macro}': {e}", level="error")

    async def _run_device_command(self, session, action_binding, source):
        """Send a device command, with its ``params`` when they are an object;
        a failure is logged, never raised."""
        device = action_binding.get("device", "")
        command = action_binding.get("command", "")
        if not (device and command):
            return
        params = action_binding.get("params")
        try:
            await self.api.device_command(device, command, params if isinstance(params, dict) else None)
            self.api.log(f"Sent {command} to {device} from {source}", level="debug")
        except Exception as e:
            self.api.log(f"Error sending command: {e}", level="error")

    async def _run_state_set(self, session, action_binding, source):
        """Write the binding's value through the deck's write rule
        (``_apply_state_set``); a failure is logged, never raised."""
        key = action_binding.get("key", "")
        if not key:
            return
        try:
            await self._apply_state_set(key, action_binding.get("value"))
        except Exception as e:
            self.api.log(f"Error setting state '{key}': {e}", level="error")

    # What each action type runs, one runner per ACTION_KINDS entry.
    _ACTION_RUNNERS = {
        "macro": _run_macro,
        "device.command": _run_device_command,
        "state.set": _run_state_set,
        "navigate": _run_navigate,
    }

    async def _apply_state_set(self, key, value):
        """Write a state value, mirroring the panel plugin-bridge scope rule.

        A surface button may write only its own ``plugin.<id>.*`` namespace
        (via ``state_set``) or a ``var.*`` user variable (via ``variable_set``).
        Anything else is a confused-deputy write and is dropped with a warning —
        exactly the scope rule the panel plugin bridge enforces in panel.js.
        """
        prefix = f"plugin.{self.api.plugin_id}."
        if key.startswith(prefix):
            await self.api.state_set(key, value)
        elif key.startswith("var."):
            await self.api.variable_set(key[len("var."):], value)
        else:
            self.api.log(
                f"Ignoring state.set to '{key}': a surface button may only write "
                f"its own {prefix}* state or a var.* user variable.",
                level="warning",
            )

    # ──── Dials (decks with rotary encoders) ────

    async def _execute_per_detent(self, session, actions, detents, source):
        """Run a turn's action list once per detent moved (capped at 8 per
        event) so a command-per-click mapping tracks fast spins."""
        turn = action_list(actions)
        if not turn:
            return
        for _ in range(min(abs(int(detents)), 8)):
            await self._execute_actions(session, turn, source)

    async def _on_dial_event(self, session, deck, dial, event, value):
        """Handle a dial turn or push (dials.py says what each one runs).

        A ``press``-only dial fires on the push. One with a ``long_press`` or
        a push-and-turn binding waits for the release: a quick release runs
        ``press``, a held one ``long_press``, and a push during which the dial
        turned runs neither (it was a grip, not a click).

        Event kinds are matched by enum name (``TURN``/``PUSH``) so this
        module never needs the StreamDeck enums at import time (they only
        exist after the lazy import, and tests run without the library).
        """
        await self._note_input(session)
        if session.overlay_active:
            await self._clear_overlay(session)
            return  # the input that dismisses an overlay never fires actions
        kind = getattr(event, "name", None)
        entry = dial_entry(self._deck_config(session), dial)
        if kind == "PUSH":
            handler = self._dial_pushed if value else self._dial_released
            await handler(session, dial, entry)
        elif kind == "TURN":
            await self._dial_turned(session, dial, entry, value)

    async def _dial_pushed(self, session, dial, entry):
        """The push: echoed and announced, then ``press`` at once, or the
        push's start noted for its release."""
        await self._publish_input_echo(session, "dial", dial)
        await self.api.event_emit("dial.press", {"dial": dial, "serial": session.serial})
        if push_deferred(entry):
            session.dial_press_times[dial] = asyncio.get_running_loop().time()
            session.dial_turned.discard(dial)
        elif entry:
            await self._execute_actions(
                session, action_list(entry.get("press")), f"dial {dial}"
            )

    async def _dial_released(self, session, dial, entry):
        """The release of a deferred push that did not turn: its click."""
        press_time = session.dial_press_times.pop(dial, None)
        turned = dial in session.dial_turned
        session.dial_turned.discard(dial)
        if press_time is None or not push_deferred(entry) or turned:
            return
        held = asyncio.get_running_loop().time() - press_time
        await self._execute_actions(session, release_actions(entry, held), f"dial {dial}")

    async def _dial_turned(self, session, dial, entry, value):
        """A turn of ``value`` detents: echoed whatever the value, announced,
        then its adjust and its turn binding, once per detent. A turn while
        held marks the push a grip."""
        await self._publish_input_echo(session, "dial", dial)
        try:
            detents = int(value)
        except (TypeError, ValueError):
            return
        if detents == 0:
            return
        await self.api.event_emit(
            "dial.turn", {"dial": dial, "amount": detents, "serial": session.serial}
        )
        if not entry:
            return
        held = dial in session.dial_press_times
        if held:
            session.dial_turned.add(dial)
        adjust, actions = turn_route(entry, detents, held)
        if adjust:
            await self._apply_dial_adjust(adjust, detents)
        await self._execute_per_detent(session, actions, detents, f"dial {dial}")

    async def _apply_dial_adjust(self, adjust, detents):
        """Increment a numeric state value by ``step * detents``, clamped.

        The turn magnitude (detents moved since the last event) scales the
        step, so spinning a dial fast moves the value proportionally faster.
        Writes go through the same scope rule as state.set actions (own
        ``plugin.<id>.*`` state or ``var.*`` user variables only).
        """
        key = adjust.get("key", "")
        step = coerce_numeric(adjust.get("step"))
        if step is None:
            step = 1
        minimum = coerce_numeric(adjust.get("min"))
        maximum = coerce_numeric(adjust.get("max"))

        current = coerce_numeric(await self.api.state_get(key))
        if current is None:
            current = minimum if minimum is not None else 0

        new_value = current + step * detents
        if minimum is not None:
            new_value = max(minimum, new_value)
        if maximum is not None:
            new_value = min(maximum, new_value)
        if float(new_value).is_integer():
            new_value = int(new_value)
        await self._apply_state_set(key, new_value)

    # ──── Touchscreen strip (decks with a touch strip) ────

    def _touch_zones(self, session):
        """The strip's zones with their pixel bounds (dials.lay_out_zones):
        the declared ``touchscreen.zones``, else one under each dial that
        shows its label and adjust value, so a configured dial gets a live
        readout with no more config."""
        if not session.deck:
            return []
        try:
            width = session.deck.touchscreen_image_format()["size"][0]
        except Exception:
            width = 800
        return lay_out_zones(self._deck_config(session), session.deck.dial_count, width)

    def _zone_at(self, session, x):
        """Return the touchscreen zone containing pixel ``x``, or None."""
        for zone in self._touch_zones(session):
            if zone["x"] <= x < zone["x"] + zone["w"]:
                return zone
        return None

    async def _apply_touch_fader(self, drag, zone, x_px):
        """Set a drag_adjust value absolutely from a touch position.

        A fader zone maps the touched fraction of its width linearly onto
        min..max (snapped to the step grid). Returns True when applied;
        False (with a debug log) when the bounds are missing, so the caller
        can fall back to relative stepping.
        """
        minimum = coerce_numeric(drag.get("min"))
        maximum = coerce_numeric(drag.get("max"))
        if minimum is None or maximum is None or maximum <= minimum:
            self.api.log(
                "Touch fader needs min and max on its adjust; ignoring touch",
                level="debug",
            )
            return False
        step = coerce_numeric(drag.get("step"))
        if step is None or step <= 0:
            step = 1
        zx = zone.get("x", 0)
        zw = max(1, zone.get("w", 1))
        frac = max(0.0, min(1.0, (x_px - zx) / zw))
        value = minimum + frac * (maximum - minimum)
        value = minimum + round((value - minimum) / step) * step
        value = max(minimum, min(maximum, value))
        if float(value).is_integer():
            value = int(value)
        await self._apply_state_set(drag.get("key", ""), value)
        return True

    async def _flash_touch_zone(self, session, zone_index):
        """Brief lighten + border flash on a touched zone.

        The glass always acknowledges a touch, configured or not — the same
        role the momentary press highlight plays on keys.
        """
        if not session.drawable:
            return
        old = session.flash_zones.pop(zone_index, None)
        if old is not None:
            old.cancel()
        session.flash_zones[zone_index] = asyncio.get_running_loop().call_later(
            0.15, self._end_zone_flash, session, zone_index
        )
        await self._render_strip_zone(session, zone_index)

    def _end_zone_flash(self, session, zone_index):
        session.flash_zones.pop(zone_index, None)
        self._schedule_strip_render(session, [zone_index])

    async def _on_touchscreen_event(self, session, deck, event, value):
        """Handle a touchscreen tap, long-press, or drag.

        SHORT runs the zone's ``touch`` actions; LONG runs ``long_touch``
        (falling back to ``touch`` when not configured). DRAG arrives as one
        event carrying start and end x — a horizontal swipe adjusts the
        zone's ``drag_adjust`` value like turning a dial (one detent per
        8 px of travel).
        """
        await self._note_input(session)
        if session.overlay_active:
            await self._clear_overlay(session)
            return  # the input that dismisses an overlay never fires actions
        kind = getattr(event, "name", None)
        if kind not in ("SHORT", "LONG", "DRAG") or not isinstance(value, dict):
            return
        touch_x = value.get("x")
        if isinstance(touch_x, (int, float)):
            await self._publish_input_echo(session, "touch", int(touch_x))
        if kind == "DRAG":
            await self._touch_dragged(session, value)
        elif isinstance(touch_x, (int, float)):
            await self._touch_tapped(session, kind, int(touch_x))

    async def _touch_dragged(self, session, value):
        """A swipe from ``x`` to ``x_out``: the zone it starts in flashes and
        its live ``drag_adjust`` moves, set from where the swipe ends on a
        fader zone, else one step per 8 px of travel."""
        x = value.get("x")
        x_out = value.get("x_out")
        if not isinstance(x, (int, float)) or not isinstance(x_out, (int, float)):
            return
        zone = self._zone_at(session, int(x))
        if zone is None:
            return
        await self._flash_touch_zone(session, zone["index"])
        drag = live_adjust(zone.get("drag_adjust"))
        if drag is None:
            return
        if drag.get("fader") and await self._apply_touch_fader(drag, zone, int(x_out)):
            return
        detents = int((x_out - x) / 8)  # truncate toward zero
        if detents:
            await self._apply_dial_adjust(drag, detents)

    async def _touch_tapped(self, session, kind, x):
        """A tap (SHORT) or long touch (LONG) at ``x``: announced, then the
        zone there flashes and runs ``long_touch`` (LONG, when it has one) or
        ``touch``. A tap on a fader zone sets its value from the touch
        position instead."""
        await self.api.event_emit("touchscreen.touch", {"x": x, "serial": session.serial})
        zone = self._zone_at(session, x)
        if zone is None:
            return
        await self._flash_touch_zone(session, zone["index"])
        drag = live_adjust(zone.get("drag_adjust"))
        if kind == "SHORT" and drag and drag.get("fader"):
            if await self._apply_touch_fader(drag, zone, x):
                return
        actions = zone.get("long_touch") if kind == "LONG" else None
        if not actions:
            actions = zone.get("touch")
        await self._execute_actions(session, action_list(actions), "touchscreen")

    # ──── Display elements (shared by zones, info items, and keys) ────

    async def _live_label(self, element):
        """An element's label: the value its ``label_source`` reads when that
        is set, else its own ``label``."""
        label = element.get("label", "")
        if element.get("label_source"):
            live = await self.api.state_get(element["label_source"])
            if live is not None:
                return str(live)
        return label

    async def _live_value(self, element):
        """An element's live value and its value line (looks.value_line), or
        ``(None, "")`` without one."""
        if not element.get("value_source"):
            return None, ""
        raw_value = await self.api.state_get(element["value_source"])
        if raw_value is None:
            return None, ""
        return raw_value, value_line(raw_value, element.get("unit"))

    async def _styled_look(self, feedback, look):
        """``look`` restyled by a feedback config (looks.styled), reading the
        key it watches; without a key it reads nothing and keeps the look."""
        if not (isinstance(feedback, dict) and feedback.get("key")):
            return look
        return styled(feedback, await self.api.state_get(feedback["key"]), look)

    async def _resolve_display(self, element, default_bg, default_fg):
        """Resolve a display element (zone / info item) to drawables.

        Shared schema: ``label`` / ``label_source`` (live override), ``icon``
        (Lucide or asset://), ``value_source`` + ``unit`` (live value text;
        ``value_static`` for fixed text), ``meter`` (bounds defaulting from
        the element's drag_adjust), and ``feedback`` (conditional styling).
        """
        label = await self._live_label(element)
        icon = element.get("icon") or None
        bg = element.get("bg_color") or default_bg
        fg = element.get("text_color") or default_fg
        if element.get("value_static") is not None:
            raw_value, value_text = None, str(element["value_static"])
        else:
            raw_value, value_text = await self._live_value(element)
        label, icon, bg, fg = await self._styled_look(
            element.get("feedback"), (label, icon, bg, fg)
        )
        meter = meter_fill(element.get("meter"), raw_value, element.get("drag_adjust"))
        return {
            "label": label, "icon": icon, "value_text": value_text,
            "bg": bg, "fg": fg, "meter": meter,
        }

    async def _draw_strip_zone(self, session, img, zone, height):
        """Draw one display element (background, icon, label, value, meter)
        into a strip image at the element's pixel bounds."""
        draw = ImageDraw.Draw(img)
        zx, zw = zone["x"], zone["w"]
        bg = self._deck_setting(session, "button_color", "#1a1a2e")
        fg = self._deck_setting(session, "text_color", "#e0e0e0")
        resolved = await self._resolve_display(zone, bg, fg)

        draw.rectangle([zx, 0, zx + zw - 1, height - 1], fill=resolved["bg"])

        meter = resolved["meter"]
        text_bottom = height - 18 if meter else height - 4

        text_x = zx + 4
        if resolved["icon"]:
            icon_img = self._render_icon(resolved["icon"], resolved["fg"], 72)
            if icon_img:
                icon_size = max(16, min(40, zw // 4, text_bottom - 8))
                icon_img = icon_img.resize((icon_size, icon_size), Image.LANCZOS)
                icon_y = max(2, (text_bottom - icon_size) // 2)
                img.paste(icon_img, (zx + 8, icon_y), icon_img)
                text_x = zx + 8 + icon_size + 6
        text_w = zx + zw - 4 - text_x

        label = resolved["label"]
        value_text = resolved["value_text"]
        if label and value_text:
            self._paste_label(
                img, label, resolved["fg"],
                (text_x, 4, text_w, height // 3),
                max_font=16, max_lines=1,
            )
            self._paste_label(
                img, value_text, resolved["fg"],
                (text_x, height // 3 + 2, text_w, text_bottom - height // 3 - 4),
                max_font=30, max_lines=1,
            )
        elif label or value_text:
            self._paste_label(
                img, label or value_text, resolved["fg"],
                (text_x, 4, text_w, text_bottom - 8),
                max_font=24, max_lines=2,
            )

        if meter:
            frac, color = meter
            draw.rectangle(
                [zx + 8, height - 14, zx + zw - 9, height - 7], fill="#2a2a3e"
            )
            fill_w = int((zw - 17) * frac)
            if fill_w > 0:
                draw.rectangle(
                    [zx + 8, height - 14, zx + 8 + fill_w, height - 7], fill=color
                )

        # Momentary touch flash (the strip's press highlight).
        if zone.get("index") in session.flash_zones:
            region = img.crop((zx, 0, zx + zw, height))
            overlay = Image.new("RGB", region.size, (255, 255, 255))
            img.paste(Image.blend(region, overlay, 0.22), (zx, 0))
            draw.rectangle(
                [zx + 1, 1, zx + zw - 2, height - 2],
                outline=(255, 255, 255), width=2,
            )

        if zx > 0:
            draw.line([(zx, 8), (zx, height - 8)], fill="#3a3a4e", width=1)

    # ──── Idle clock (unconfigured displays are never dead black) ────

    @staticmethod
    def _clock_text():
        """Current time + date strings (12-hour where the locale has an
        AM/PM notion, 24-hour elsewhere)."""
        now = time.localtime()
        ampm = time.strftime("%p", now)
        if ampm:
            hour = now.tm_hour % 12 or 12
            time_str = f"{hour}:{now.tm_min:02d} {ampm}"
        else:
            time_str = time.strftime("%H:%M", now)
        return time_str, time.strftime("%a %b %d", now)

    def _draw_clock(self, img, width, height, fg):
        """Draw the idle clock — proof the glass works, useful in a rack."""
        time_str, date_str = self._clock_text()
        split = max(10, int(height * 0.62))
        self._paste_label(
            img, time_str, fg,
            (0, 2, width, split),
            max_font=max(16, int(height * 0.5)), max_lines=1,
        )
        self._paste_label(
            img, date_str, "#8a8a9a",
            (0, split + 2, width, max(8, height - split - 4)),
            max_font=max(10, int(height * 0.2)), max_lines=1,
        )

    def _strip_idle_mode(self, session):
        """``"clock"``/``"blank"`` when nothing is configured for the touch
        strip (no declared zones, no dial worth a zone), else None."""
        cfg = self._deck_config(session)
        if declared_zones(cfg) or any(map(shows_on_strip, objects(cfg.get("dials", [])))):
            return None
        ts = cfg.get("touchscreen", {})
        ts = ts if isinstance(ts, dict) else {}
        return "blank" if ts.get("idle") == "blank" else "clock"

    def _info_idle_mode(self, session):
        """``"clock"``/``"blank"`` when the info screen has nothing
        configured (or explicitly asks for the clock), else None."""
        cfg = self._deck_config(session).get("info_strip")
        if not isinstance(cfg, dict):
            return "clock"
        source = cfg.get("source")
        if source == "clock":
            return "clock"
        if source == "blank":
            return "blank"
        items = cfg.get("items")
        if isinstance(items, list) and any(isinstance(i, dict) for i in items):
            return None
        if any(
            cfg.get(field) for field in (
                "key", "text", "label", "label_source", "value_source",
                "icon", "meter",
            )
        ):
            return None
        return "clock"

    async def _tick_clock(self, session):
        """Re-render idle clocks when the wall-clock minute changes
        (rides the watchdog tick)."""
        minute = time.localtime().tm_min
        if minute == session.clock_minute:
            return
        session.clock_minute = minute
        if not session.drawable:
            return
        if session.deck.is_touch() and self._strip_idle_mode(session) == "clock":
            await self._render_touchscreen(session)
        if self._info_idle_mode(session) == "clock":
            await self._render_info_strip(session)

    async def _render_touchscreen(self, session):
        """Render the touchscreen strip: one display element per zone.

        Zones carry the full display schema (live label, icon, value + unit,
        meter bar, conditional feedback styling). With nothing configured
        the strip shows the idle clock (or stays blank when asked). The
        rendered image is cached on the session so a single-zone change can
        redraw partially.
        """
        deck = session.deck
        if not session.drawable or not deck.is_touch():
            return
        if session.overlay_active:
            return  # a show_message overlay owns the display right now
        try:
            width, height = deck.touchscreen_image_format()["size"]
        except Exception:
            return

        bg = self._deck_setting(session, "button_color", "#1a1a2e")
        img = Image.new("RGB", (width, height), bg)

        idle = self._strip_idle_mode(session)
        if idle:
            if idle == "clock":
                fg = self._deck_setting(session, "text_color", "#e0e0e0")
                self._draw_clock(img, width, height, fg)
            if session.flash_zones:
                # Touch acknowledgment while idle: lighten the whole strip.
                overlay = Image.new("RGB", img.size, (255, 255, 255))
                img = Image.blend(img, overlay, 0.18)
        else:
            for zone in self._touch_zones(session):
                await self._draw_strip_zone(session, img, zone, height)

        session.strip_image = img
        self._mirror(session, "touchscreen", img)
        self._send_strip_image(deck, img, failure="Error setting touchscreen image")

    def _schedule_strip_render(self, session, zones=None):
        """Coalesce strip redraws (~40 ms debounce): collect dirty zones,
        then redraw only those regions when possible, the whole strip when
        not (``zones=None`` means everything)."""
        if zones is None or session.strip_dirty == "all":
            session.strip_dirty = "all"
        else:
            if not isinstance(session.strip_dirty, set):
                session.strip_dirty = set()
            session.strip_dirty.update(zones)
        if session.strip_render_task and not session.strip_render_task.done():
            return
        session.strip_render_task = asyncio.create_task(
            self._flush_strip_render(session)
        )

    async def _flush_strip_render(self, session):
        try:
            while session.strip_dirty:
                await asyncio.sleep(0.04)
                dirty = session.strip_dirty
                session.strip_dirty = None
                if dirty == "all" or session.strip_image is None:
                    await self._render_touchscreen(session)
                else:
                    for index in sorted(dirty):
                        await self._render_strip_zone(session, index)
        finally:
            session.strip_render_task = None

    async def _render_strip_zone(self, session, zone_index):
        """Redraw one zone into the cached strip image and ship only that
        region to the deck (the hardware accepts partial-region updates)."""
        deck = session.deck
        if not session.drawable or not deck.is_touch():
            return
        if session.overlay_active or session.strip_image is None:
            return
        if self._strip_idle_mode(session):
            # Idle clock/blank has no zone regions — repaint the whole strip
            # (this is also how the idle touch flash draws and clears).
            await self._render_touchscreen(session)
            return
        try:
            fmt = deck.touchscreen_image_format()
            width, height = fmt["size"]
        except Exception:
            return
        # A flipped/rotated native format transforms whole-image coordinates;
        # partial regions would land in the wrong place, so redraw fully.
        if fmt.get("flip", (False, False)) != (False, False) or fmt.get("rotation", 0):
            await self._render_touchscreen(session)
            return
        zones = self._touch_zones(session)
        if not 0 <= zone_index < len(zones):
            await self._render_touchscreen(session)
            return
        zone = zones[zone_index]
        img = session.strip_image
        await self._draw_strip_zone(session, img, zone, height)
        self._mirror(session, "touchscreen", img)
        zx = max(0, int(zone["x"]))
        zw = min(int(zone["w"]), width - zx)
        if zw <= 0:
            return
        region = img.crop((zx, 0, zx + zw, height))
        self._send_strip_image(deck, region, x=zx, failure="Error setting touchscreen region")

    # ──── Info strip (decks with a secondary info screen) ────

    @staticmethod
    def _info_strip_items(cfg, width):
        """Normalize ``info_strip`` config to display elements with pixel
        bounds across the screen.

        Accepts the display-element shape directly ({label, label_source,
        icon, value_source, unit, meter, feedback, ...}); the legacy
        ``{source: "state"|"text", key, text}`` shape maps onto it (a text
        source becomes a static value).
        """
        if not isinstance(cfg, dict):
            return []

        def _normalize(element):
            if element.get("source") == "text":
                element["value_static"] = str(element.get("text", ""))
                element.pop("value_source", None)
            elif not element.get("value_source"):
                element["value_source"] = element.get("key", "")
            return element

        items = cfg.get("items")
        if isinstance(items, list):
            items = [_normalize(dict(i)) for i in items if isinstance(i, dict)]
            # The screen physically fits two items side by side.
            items = items[:2]
            if items:
                slot = width // len(items)
                for idx, item in enumerate(items):
                    item["x"] = idx * slot
                    item["w"] = slot
                return items

        element = _normalize(dict(cfg))
        element["x"] = 0
        element["w"] = width
        return [element]

    async def _render_info_strip(self, session):
        """Render the secondary info screen from the ``info_strip`` config.

        The config is a display element (live label, icon, value + unit,
        meter, feedback styling); the legacy ``{"source": "state"|"text",
        "key"/"text", "label"}`` shape still works. Re-rendered when a
        watched key changes.
        """
        deck = session.deck
        if not session.drawable:
            return
        if session.overlay_active:
            return  # a show_message overlay owns the display right now
        size = self._panel_size(deck, "screen_image_format")
        if size is None:
            return  # this deck has no info screen
        width, height = size

        bg = self._deck_setting(session, "button_color", "#1a1a2e")
        img = Image.new("RGB", (width, height), bg)

        idle = self._info_idle_mode(session)
        if idle == "clock":
            fg = self._deck_setting(session, "text_color", "#e0e0e0")
            self._draw_clock(img, width, height, fg)
        elif idle is None:
            cfg = self._deck_config(session).get("info_strip")
            for element in self._info_strip_items(cfg, width):
                await self._draw_strip_zone(session, img, element, height)
        # idle == "blank": leave the plain background

        self._mirror(session, "screen", img)
        self._send_screen_image(deck, img, failure="Error setting info strip image")

    # ──── Brightness (auto rules + idle dim) ────

    async def _current_brightness_level(self, session):
        """Return the active brightness: the first matching ``auto_brightness``
        rule's level, else the base ``brightness`` config value. Clamped 0-100."""
        rules = self._deck_config(session).get("auto_brightness", [])
        if isinstance(rules, list):
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                when = rule.get("when")
                level = coerce_numeric(rule.get("level"))
                if when is None or level is None:
                    continue
                if await self._eval_condition(when):
                    return max(0, min(100, int(level)))
        base = coerce_numeric(self._unit_brightness(session))
        return max(0, min(100, int(base if base is not None else 70)))

    def _unit_brightness(self, session):
        """Base brightness for one deck.

        A unit property, not a layout property: ``deck_settings[serial]``
        wins, then the flat plugin-wide ``brightness``, then 70. Never read
        from a ``decks[serial]`` layout override — a deck shouldn't need its
        own layout just to sit at a different brightness.
        """
        settings = self.api.config.get("deck_settings")
        if isinstance(settings, dict):
            entry = settings.get(session.serial)
            if isinstance(entry, dict):
                level = coerce_numeric(entry.get("brightness"))
                if level is not None:
                    return level
        return self.api.config.get("brightness", 70)

    def _set_deck_brightness(self, session, level):
        """Apply a brightness level to a deck (best-effort)."""
        if not session.deck:
            return
        try:
            session.deck.set_brightness(level)
        except Exception as e:
            self.api.log(f"Error setting brightness: {e}", level="debug")

    async def _apply_active_brightness(self, session):
        """Re-apply the rule-or-base brightness (used on wake and rule change)."""
        if not session.deck:
            return
        self._set_deck_brightness(session, await self._current_brightness_level(session))

    async def _check_idle_dim(self, session):
        """Dim a deck when no input has arrived for ``idle_dim.after_seconds``.

        Runs on the watchdog tick. Any key/dial/touch input wakes the deck via
        ``_note_input``.
        """
        cfg = self._deck_config(session).get("idle_dim")
        if not isinstance(cfg, dict) or session.idle_dimmed:
            return
        after = coerce_numeric(cfg.get("after_seconds"))
        level = coerce_numeric(cfg.get("level"))
        if after is None or after <= 0 or level is None:
            return
        now = asyncio.get_event_loop().time()
        if now - session.last_input >= after:
            session.idle_dimmed = True
            self._set_deck_brightness(session, max(0, min(100, int(level))))

    async def _note_input(self, session):
        """Record user input: resets the idle timer and wakes a dimmed deck."""
        session.last_input = asyncio.get_event_loop().time()
        if session.idle_dimmed:
            session.idle_dimmed = False
            await self._apply_active_brightness(session)

    async def _publish_input_echo(self, session, kind, index):
        """Publish ``<serial>.last_input`` so the IDE can flash the control.

        Format ``"<kind>:<index>:<seq>"`` — the monotonic seq makes repeated
        presses of the same control still produce a state change (the state
        store dedupes writes of an unchanged value).
        """
        session.input_seq += 1
        await self.api.state_set(
            f"{session.serial}.last_input", f"{kind}:{int(index)}:{session.input_seq}"
        )

    # ──── Page Navigation ────

    def _effective_page_count(self, session):
        """How many pages a deck's config view makes: pages exist by being
        used, so there is no page-count setting (pages.py says what counts)."""
        return page_count(self._deck_config(session))

    async def _change_page(self, session, new_page):
        """Switch a deck to a different button page."""
        max_pages = self._effective_page_count(session)
        if new_page < 0:
            new_page = 0
        elif new_page >= max_pages:
            new_page = max_pages - 1

        if new_page == session.current_page:
            return

        session.current_page = new_page
        await self.api.state_set(f"{session.serial}.current_page", new_page)
        if self._primary_session() is session:
            await self.api.state_set("current_page", new_page)
        await self._render_all_buttons(session)
        self.api.log(f"Switched to page {new_page}", level="debug")

    # ──── Button Rendering ────

    async def _render_all_buttons(self, session):
        """Render every key for a deck's current page (LCD keys + touch keys)."""
        if not session.drawable:
            return

        key_count = session.deck.key_count() + session.deck.touch_key_count()
        for key_index in range(key_count):
            await self._render_button(session, key_index)

    @staticmethod
    def _is_touch_key(session, key_index):
        """True for the color-only touch keys indexed after the LCD keys."""
        if not session.deck:
            return False
        key_count = session.deck.key_count()
        return key_count <= key_index < key_count + session.deck.touch_key_count()

    async def _render_button(self, session, key_index):
        """Draw one key from its entry and the state it reads: an LCD key's
        image, or a touch key's color."""
        if not session.drawable:
            return
        if session.overlay_active:
            return  # a show_message overlay owns the display right now

        is_touch_key = self._is_touch_key(session, key_index)
        assignment = self._get_button_assignment(
            session, session.current_page, key_index
        )
        # Touch keys have no LCD: an unassigned one just goes dark.
        if is_touch_key and not assignment:
            self._apply_key_color(session, key_index, "#000000")
            return
        # Hidden by visible_when: a blank black key, nothing else.
        if assignment and not await self._is_button_visible(assignment):
            self._blank_key(session, key_index, is_touch_key)
            return

        look = await self._key_look(session, assignment)
        macro_mark = session.macro_mark_showing(key_index)
        nav_active = self._navigates_here(session, assignment)
        held = key_index in session.pressed_keys

        if is_touch_key:
            color = self._touch_key_color(look.bg_color, macro_mark, nav_active, held)
            self._apply_key_color(session, key_index, color)
            return
        image = self._create_button_image(
            session, look.label, look.bg_color, look.text_color, look.icon,
            value_text=look.value_text, meter=look.meter,
        )
        self._apply_key_image(
            session, key_index, self._marked_image(image, nav_active, held, macro_mark)
        )

    def _navigates_here(self, session, assignment):
        """True for a key whose press navigates to the page showing: its
        "you are here" treatment makes page-switcher rows read as tabs."""
        target = key_target_page(assignment) if assignment else None
        return target is not None and target == session.current_page

    def _blank_key(self, session, key_index, is_touch_key):
        """A hidden key: a dark touch key, or a black LCD key."""
        if is_touch_key:
            self._apply_key_color(session, key_index, "#000000")
        else:
            self._apply_key_image(
                session, key_index,
                self._create_button_image(session, "", "#000000", "#000000"),
            )

    async def _key_look(self, session, assignment):
        """What a key shows before any mark: its label, icon and colors, then
        the live label and value, the toggle label and the feedback style, read
        in that order. An unassigned key shows the deck's colors."""
        bg_color = self._deck_setting(session, "button_color", "#1a1a2e")
        text_color = self._deck_setting(session, "text_color", "#e0e0e0")
        if not assignment:
            return _KeyLook("", None, bg_color, text_color, "", None)
        label = await self._live_label(assignment)
        icon = assignment.get("icon") or None
        raw_value, value_text = await self._live_value(assignment)
        # Keys have no surrounding adjust, so a key meter draws only when
        # enabled (bounds default to 0..100).
        meter = meter_fill(assignment.get("meter"), raw_value)
        bg_color = assignment.get("bg_color") or bg_color
        text_color = assignment.get("text_color") or text_color
        bindings = assignment.get("bindings", {})
        if not isinstance(bindings, dict):
            return _KeyLook(label, icon, bg_color, text_color, value_text, meter)
        label = await self._toggle_label(bindings, label)
        label, icon, bg_color, text_color = await self._styled_look(
            bindings.get("feedback"), (label, icon, bg_color, text_color)
        )
        return _KeyLook(label, icon, bg_color, text_color, value_text, meter)

    async def _toggle_label(self, bindings, label):
        """A toggle key's ``on_label`` while its toggle key reads on and its
        ``off_label`` while it does not; ``label`` when the one for that state
        is empty, or the key is no toggle with a toggle key."""
        press = unwrap_binding(bindings.get("press"))
        if not isinstance(press, dict) or press.get("mode") != "toggle":
            return label
        if not press.get("toggle_key", ""):
            return label
        if await self._toggle_is_on(press):
            return press.get("on_label", "") or label
        return press.get("off_label", "") or label

    def _touch_key_color(self, bg_color, macro_mark, nav_active, held):
        """A touch key's color: a macro mark's color outright, else the key's
        own, lifted for "you are here" and again while held."""
        color = self._MACRO_MARK_COLORS.get(macro_mark, bg_color)
        if nav_active and macro_mark is None:
            color = self._lighten_hex(color, 0.2)
        if held:
            color = self._lighten_hex(color, 0.25)
        return color

    def _marked_image(self, image, nav_active, held, macro_mark):
        """An LCD key's image with its marks drawn over it in order: "you are
        here", the press highlight, then a macro's run border."""
        if nav_active:
            image = self._apply_nav_active(image)
        if held:
            image = self._apply_press_highlight(image)
        if macro_mark:
            image = self._apply_macro_mark(image, macro_mark)
        return image

    def _apply_nav_active(self, image):
        """Subtle highlight for a page key whose target page is showing."""
        try:
            overlay = Image.new("RGB", image.size, (255, 255, 255))
            out = Image.blend(image, overlay, 0.12)
            draw = ImageDraw.Draw(out)
            w, h = out.size
            draw.rectangle([0, 0, w - 1, h - 1], outline=(138, 180, 147), width=2)
            return out
        except Exception:
            return image

    @staticmethod
    def _hex_to_rgb(color):
        """Parse a #rrggbb (or #rgb) hex color to an (r, g, b) tuple."""
        value = str(color or "").lstrip("#")
        try:
            if len(value) == 3:
                return tuple(int(c * 2, 16) for c in value)
            if len(value) == 6:
                return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            pass
        return (0, 0, 0)

    @staticmethod
    def _lighten_hex(color, amount):
        """Blend a hex color toward white by ``amount`` (0..1), as hex."""
        r, g, b = StreamDeckPlugin._hex_to_rgb(color)
        r = int(r + (255 - r) * amount)
        g = int(g + (255 - g) * amount)
        b = int(b + (255 - b) * amount)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _apply_key_color(self, session, key_index, color):
        """Set a touch key's RGB backlight from a hex color (thread-safe)."""
        if self._mirror_enabled(session):
            session.touch_key_colors[key_index] = color
            self._schedule_mirror_bump(session)
        r, g, b = self._hex_to_rgb(color)
        try:
            with session.deck:
                session.deck.set_key_color(key_index, r, g, b)
        except Exception as e:
            self.api.log(f"Error setting key {key_index} color: {e}", level="debug")

    def _apply_press_highlight(self, image):
        """Return a lightened, inset-bordered variant of a button image.

        Used as brief tactile feedback while a key is physically held. Any
        failure falls back to the original image so a press never blanks a key.
        """
        try:
            overlay = Image.new("RGB", image.size, (255, 255, 255))
            highlighted = Image.blend(image, overlay, 0.25)
            draw = ImageDraw.Draw(highlighted)
            w, h = image.size
            draw.rectangle([1, 1, w - 2, h - 2], outline=(255, 255, 255), width=2)
            return highlighted
        except Exception:
            return image

    def _apply_key_image(self, session, key_index, image):
        """Encode a PIL image and set it on a deck key (thread-safe)."""
        self._mirror(session, f"key_{key_index}", image)
        try:
            native_image = PILHelper.to_native_key_format(session.deck, image)
            with session.deck:
                session.deck.set_key_image(key_index, native_image)
        except Exception as e:
            self.api.log(f"Error setting key {key_index} image: {e}", level="debug")

    def _send_strip_image(self, deck, image, x=0, failure=None):
        """Set ``image`` on the touch strip at ``x``: the whole strip, or one
        zone's region (the strip takes partial updates). Converted to the
        deck's native format and set under its lock; a send the deck refuses
        is logged at debug after ``failure``'s words, or dropped silently
        without them. Unlike ``_apply_key_image`` it takes the deck the
        caller drew for and leaves the Live View mirror to the caller, which
        mirrors the whole strip when it sends one region."""
        width, height = image.size
        try:
            native = PILHelper.to_native_touchscreen_format(deck, image)
            with deck:
                deck.set_touchscreen_image(native, x, 0, width, height)
        except Exception as e:
            if failure:
                self.api.log(f"{failure}: {e}", level="debug")

    def _send_screen_image(self, deck, image, failure=None):
        """Set ``image`` on the info screen, the way ``_send_strip_image``
        sets the strip."""
        try:
            native = PILHelper.to_native_screen_format(deck, image)
            with deck:
                deck.set_screen_image(native)
        except Exception as e:
            if failure:
                self.api.log(f"{failure}: {e}", level="debug")

    def _create_button_image(self, session, label, bg_color, text_color,
                             icon_name=None, value_text="", meter=None):
        """Create a PIL image for a button: icon, wrapped label, optional
        live value line, optional meter bar along the bottom."""
        image_format = session.deck.key_image_format()
        width = image_format["size"][0]
        height = image_format["size"][1]

        img = Image.new("RGB", (width, height), bg_color)

        # A meter reserves a strip along the bottom edge.
        usable_h = height - 10 if meter else height

        # Load icon image if specified
        icon_img = self._render_icon(icon_name, text_color, width) if icon_name else None

        # An icon carries the identity, so with an icon plus both texts the
        # live value wins the text slot (two text lines + icon turn to mush
        # on a small key).
        if icon_img and value_text:
            label = value_text
            value_text = ""

        if icon_img and label:
            # Icon in the upper area, wrapped label below it.
            icon_size = min(width, usable_h) // 2
            icon_img = icon_img.resize((icon_size, icon_size), Image.LANCZOS)
            icon_x = (width - icon_size) // 2
            icon_y = max(4, (usable_h // 2) - icon_size + 4)
            img.paste(icon_img, (icon_x, icon_y), icon_img)

            text_top = icon_y + icon_size + 2
            self._paste_label(
                img, label, text_color,
                (2, text_top, width - 4, max(0, usable_h - text_top - 2)),
                max_font=14, max_lines=2,
            )

        elif icon_img:
            # Icon only, centered
            icon_size = int(min(width, usable_h) * 0.6)
            icon_img = icon_img.resize((icon_size, icon_size), Image.LANCZOS)
            icon_x = (width - icon_size) // 2
            icon_y = (usable_h - icon_size) // 2
            img.paste(icon_img, (icon_x, icon_y), icon_img)

        elif label and value_text:
            # Name on top, live value as the main element below it.
            self._paste_label(
                img, label, text_color,
                (2, 2, width - 4, usable_h // 3),
                max_font=14, max_lines=1,
            )
            self._paste_label(
                img, value_text, text_color,
                (2, usable_h // 3 + 2, width - 4,
                 max(0, usable_h - usable_h // 3 - 4)),
                max_font=max(16, usable_h // 3), max_lines=1,
            )

        elif label or value_text:
            # Single text — wrapped and shrunk to fill the key.
            pad = max(2, width // 12)
            self._paste_label(
                img, label or value_text, text_color,
                (pad, pad, width - 2 * pad, usable_h - 2 * pad),
                max_font=max(14, usable_h // 4), max_lines=3,
            )

        if meter:
            frac, color = meter
            draw = ImageDraw.Draw(img)
            draw.rectangle([3, height - 8, width - 4, height - 3], fill="#2a2a3e")
            fill_w = int((width - 7) * frac)
            if fill_w > 0:
                draw.rectangle([3, height - 8, 3 + fill_w, height - 3], fill=color)

        return img

    # ──── Text Rendering (bundled font, word-wrap + shrink-to-fit) ────

    def _load_text_font(self):
        """Resolve the bundled text-font path used for button labels.

        ``arial.ttf`` exists only on Windows, so a bundled font keeps labels
        legible on Linux and the Pi. ``_text_font`` falls back to arial.ttf
        then PIL's bitmap default if this file is somehow missing.
        """
        text_ttf = Path(__file__).parent / "fonts" / "DejaVuSans.ttf"
        self._text_font_path = str(text_ttf) if text_ttf.exists() else None
        if self._text_font_path is None:
            self.api.log(
                "Bundled text font not found; labels will use a small default font",
                level="warning",
            )

    def _text_font(self, size):
        """Return a cached text font at ``size`` (bundled font, then fallbacks)."""
        font = self._font_cache.get(size)
        if font is not None:
            return font
        font = None
        if self._text_font_path:
            try:
                font = ImageFont.truetype(self._text_font_path, size)
            except (IOError, OSError):
                font = None
        if font is None:
            try:
                font = ImageFont.truetype("arial.ttf", size)
            except (IOError, OSError):
                font = ImageFont.load_default()
        self._font_cache[size] = font
        return font

    @staticmethod
    def _wrap_greedy(draw, text, font, max_width):
        """Greedy word-wrap: pack words onto lines no wider than ``max_width``.

        A single word longer than ``max_width`` keeps its own (overflowing)
        line — there's nothing to break it on; shrink-to-fit handles the rest.
        """
        words = text.split()
        if not words:
            return []
        lines = []
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}"
            if draw.textlength(trial, font=font) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    def _paste_label(self, img, label, color, box, max_font, max_lines):
        """Draw ``label`` centered in ``box``=(x, y, w, h), wrapped + shrunk."""
        bx, by, bw, bh = box
        if not label or bw <= 0 or bh <= 0:
            return
        layer = self._render_label_layer(label, color, bw, bh, max_font, max_lines)
        img.paste(layer, (bx, by), layer)

    def _render_label_layer(self, label, color, w, h, max_font, max_lines):
        """Render ``label`` to an RGBA layer of size (w, h), wrapped and shrunk
        to fit in at most ``max_lines`` lines. Cached like rendered icons."""
        cache_key = (label, w, h, color, max_font, max_lines)
        cached = self._label_cache.get(cache_key)
        if cached is not None:
            return cached.copy()

        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)

        # Shrink the font from max_font down to an 8px floor until the wrapped
        # text fits the box in both dimensions and the line count.
        min_font = 8
        font = self._text_font(min_font)
        lines = self._wrap_greedy(draw, label, font, w)
        for size in range(max_font, min_font - 1, -1):
            candidate = self._text_font(size)
            wrapped = self._wrap_greedy(draw, label, candidate, w)
            if len(wrapped) > max_lines:
                continue
            ascent, descent = candidate.getmetrics()
            line_h = ascent + descent
            widest = max((draw.textlength(ln, font=candidate) for ln in wrapped), default=0)
            if line_h * len(wrapped) <= h and widest <= w:
                font, lines = candidate, wrapped
                break

        lines = lines[:max_lines]
        ascent, descent = font.getmetrics()
        line_h = ascent + descent
        total_h = line_h * len(lines)
        y = max(0, (h - total_h) // 2)
        for line in lines:
            line_w = draw.textlength(line, font=font)
            x = max(0, int((w - line_w) // 2))
            draw.text((x, y), line, fill=color, font=font)
            y += line_h

        self._label_cache[cache_key] = layer.copy()
        return layer

    # ──── Icon Rendering ────

    def _load_icon_font(self):
        """Load the bundled Lucide icon font and code point map."""
        fonts_dir = Path(__file__).parent / "fonts"
        ttf_path = fonts_dir / "lucide.ttf"
        info_path = fonts_dir / "lucide-info.json"

        if not ttf_path.exists():
            self.api.log("Lucide icon font not found, icons will not render", level="warning")
            return

        try:
            with open(info_path, "r", encoding="utf-8") as f:
                info = json.load(f)
            # Build name -> unicode char map
            # encodedCode values are like "\e589" — a backslash followed by
            # the full hex code point. Skip only the leading backslash.
            for name, data in info.items():
                encoded = data.get("encodedCode", "")
                if encoded.startswith("\\"):
                    code_point = int(encoded[1:], 16)
                    self._icon_map[name] = chr(code_point)
            self.api.log(f"Loaded {len(self._icon_map)} icon glyphs", level="debug")
        except Exception as e:
            self.api.log(f"Failed to load icon map: {e}", level="warning")

        self._icon_font_path = str(ttf_path)

    def _render_icon(self, icon_name, color, button_size):
        """Render an icon as an RGBA PIL Image.

        Supports:
          - Lucide icon names (rendered from the bundled TTF font)
          - asset:// references (loaded from project assets directory)
          - File paths to PNG/JPG images
        """
        if not icon_name:
            return None

        # Asset reference — load image file
        if icon_name.startswith("asset://"):
            return self._load_asset_icon(icon_name[8:], button_size)

        # Lucide icon — render from font
        return self._render_lucide_icon(icon_name, color, button_size)

    def _render_lucide_icon(self, icon_name, color, button_size):
        """Render a Lucide icon glyph to an RGBA image."""
        if not self._icon_map or not hasattr(self, "_icon_font_path"):
            return None

        char = self._icon_map.get(icon_name)
        if not char:
            return None

        # Check cache
        icon_size = int(button_size * 0.6)
        cache_key = (icon_name, icon_size, color)
        cached = self._icon_cache.get(cache_key)
        if cached is not None:
            return cached.copy()

        try:
            font = ImageFont.truetype(self._icon_font_path, icon_size)
            # Render glyph onto transparent image
            img = Image.new("RGBA", (icon_size, icon_size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)

            # Measure and center the glyph
            bbox = draw.textbbox((0, 0), char, font=font)
            glyph_w = bbox[2] - bbox[0]
            glyph_h = bbox[3] - bbox[1]
            x = (icon_size - glyph_w) // 2 - bbox[0]
            y = (icon_size - glyph_h) // 2 - bbox[1]
            draw.text((x, y), char, fill=color, font=font)

            self._icon_cache[cache_key] = img.copy()
            return img
        except Exception as e:
            self.api.log(f"Failed to render icon '{icon_name}': {e}", level="debug")
            return None

    def _load_asset_icon(self, filename, button_size):
        """Load a custom icon from the project assets directory."""
        try:
            # Assets are stored relative to the project directory
            # The plugin API provides the project path via config
            assets_dir = Path(self.api.config.get("_project_dir", "")) / "assets"
            icon_path = assets_dir / filename
            if not icon_path.exists():
                return None
            icon = Image.open(icon_path).convert("RGBA")
            return icon
        except Exception as e:
            self.api.log(f"Failed to load asset icon '{filename}': {e}", level="debug")
            return None

    # ──── Conditions, Visibility & Auto-Page ────

    async def _eval_condition(self, cond) -> bool:
        """Whether a visible_when / auto_page / auto_brightness condition
        holds against current state (the rule is conditions.py)."""
        return await eval_condition(cond, self.api.state_get, self.api.log)

    async def _is_button_visible(self, assignment) -> bool:
        """True unless the button's visible_when condition evaluates false."""
        bindings = assignment.get("bindings", {})
        if not isinstance(bindings, dict):
            return True
        visible_when = bindings.get("visible_when")
        if visible_when is None:
            return True
        return await self._eval_condition(visible_when)

    async def _evaluate_auto_page(self, session):
        """Return the page of the first matching auto_page rule, or None.

        Rules are evaluated in array order; the first whose ``when`` condition
        is true wins. The page index is clamped to the valid range.
        """
        rules = self._deck_config(session).get("auto_page", [])
        if not isinstance(rules, list):
            return None
        max_pages = self._effective_page_count(session)
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            when = rule.get("when")
            page = rule.get("page")
            if when is None or page is None:
                continue
            if await self._eval_condition(when):
                target = page_number(page)
                if target is None:
                    continue
                return max(0, min(target, max_pages - 1))
        return None

    # ──── State Subscriptions ────

    async def _setup_feedback_subscriptions(self, session):
        """Subscribe to every state key that can change a deck's keys, strips,
        brightness or page, and note which keys show which macro's run.

        Each deck subscribes against its own config view; what the view reads
        is watch_keys.py.
        """
        cfg = self._deck_config(session)
        watch = deck_watch(cfg, self._info_strip_items(cfg.get("info_strip"), 0))
        session.auto_page_keys = watch.auto_page
        session.touch_strip_keys = watch.touch_strip
        session.info_strip_keys = watch.info_strip
        session.brightness_keys = watch.brightness
        session.macro_keys = watch.macro_keys
        callback = functools.partial(self._on_state_change, session)
        for key in watch.keys:
            sub_id = await self.api.state_subscribe(key, callback)
            session.feedback_subs.append(sub_id)

    async def _on_state_change(self, session, key, value, old_value):
        """Redraw what a watched state change touches, in order: an automatic
        page switch (which redraws the whole page and ends here), the strip
        zones, the info strip, the brightness, then the keys."""
        if not session.drawable:
            return
        # Only an auto_page key can page, so an ordinary feedback or
        # visibility change never overrides paging by hand.
        if key in session.auto_page_keys and await self._follow_auto_page(session):
            return
        if key in session.touch_strip_keys:
            # No zone reads the key (a dial's adjust under explicit zones):
            # the whole strip redraws.
            self._schedule_strip_render(session, self._zones_watching(session, key) or None)
        if key in session.info_strip_keys:
            await self._render_info_strip(session)
        # An idle-dimmed deck keeps its dim level; waking on input applies
        # the rule level anyway.
        if key in session.brightness_keys and not session.idle_dimmed:
            await self._apply_active_brightness(session)
        cfg = self._deck_config(session)
        for key_index in keys_watching(cfg, session.current_page, key):
            await self._render_button(session, key_index)

    def _zones_watching(self, session, key):
        """Indexes of the strip zones whose drawing reads a state key."""
        return [
            i for i, zone in enumerate(self._touch_zones(session))
            if zone_watches_key(zone, key)
        ]

    async def _follow_auto_page(self, session):
        """Switch to the page the auto_page rules choose; True when it did."""
        target = await self._evaluate_auto_page(session)
        if target is None or target == session.current_page:
            return False
        await self._change_page(session, target)
        return True

    # ──── Macro run feedback ────

    _MACRO_MARK_COLORS = {
        "running": "#f59e0b",
        "done": "#22c55e",
        "error": "#ef4444",
    }
    # How long a result shows on its keys before they draw as usual.
    _MACRO_FLASH_SECONDS = {"done": 0.8, "error": 1.2}

    async def _on_macro_event(self, event_name, payload):
        """Mark keys whose bindings reference a macro that changed state."""
        for prefix, mark in (
            ("macro.started.", "running"),
            ("macro.completed.", "done"),
            ("macro.cancelled.", None),
            ("macro.error.", "error"),
        ):
            if event_name.startswith(prefix):
                await self._apply_macro_mark_event(event_name[len(prefix):], mark)
                return

    async def _apply_macro_mark_event(self, macro_id, mark):
        """Mark every key whose press runs ``macro_id``, on every deck, and
        redraw the ones showing."""
        seconds = self._MACRO_FLASH_SECONDS.get(mark)
        for session in list(self._sessions.values()):
            flash_ended = functools.partial(self._end_macro_flash, session)
            for page, key_index in session.macro_keys.get(macro_id) or ():
                session.mark_macro_run((page, key_index), mark)
                if seconds is not None:
                    session.flash_macro_mark((page, key_index), seconds, flash_ended)
                if session.shows(page):
                    await self._render_button(session, key_index)

    def _end_macro_flash(self, session, page, key_index):
        asyncio.ensure_future(self._clear_macro_mark(session, page, key_index))

    async def _clear_macro_mark(self, session, page, key_index):
        """A result flash is over: its key draws as usual again, unless the
        deck has gone."""
        attached = session.device_id in self._sessions
        if session.end_macro_flash((page, key_index), attached) and session.shows(page):
            await self._render_button(session, key_index)

    def _apply_macro_mark(self, image, mark):
        """Border (+ loader glyph while running) showing a macro's run state."""
        color = self._MACRO_MARK_COLORS.get(mark)
        if color is None:
            return image
        try:
            out = image.copy()
            draw = ImageDraw.Draw(out)
            w, h = out.size
            draw.rectangle([0, 0, w - 1, h - 1], outline=color, width=3)
            if mark == "running":
                icon = self._render_icon("loader", color, max(16, w // 3))
                if icon is not None:
                    size = max(10, w // 4)
                    icon = icon.resize((size, size), Image.LANCZOS)
                    out.paste(icon, (w - size - 3, 3), icon)
            return out
        except Exception:
            return image

    # ──── Context Actions ────

    async def _on_context_action(self, event_name, payload):
        """Run the context action an event names (context_actions.py): a
        simulated input, the live mirror switch, or a deck action on the
        deck its payload names (every deck without a serial)."""
        data = payload if isinstance(payload, dict) else {}
        action = action_for(event_name)
        if action == "simulate_input":
            await self._simulate_input(data)
        elif action == "set_live_mirror":
            await self._set_live_mirror(data)
        elif action is not None:
            args = deck_action_args(action, data)
            if args is None:
                return
            for session in self._sessions_for(data.get("serial")):
                await self._run_deck_action(action, session, args)

    async def _run_deck_action(self, action, session, args):
        """One deck action on one deck; a message shows only on a deck with
        a screen."""
        if action == "set_page":
            await self._change_page(session, *args)
        elif action == "set_brightness":
            self._set_deck_brightness(session, *args)
        elif action == "flash_key":
            await self._flash_key(session, *args)
        elif action == "show_message":
            if session.drawable:
                await self._show_message(session, *args)
        elif action == "identify":
            await self._identify_deck(session)

    async def _identify_deck(self, session):
        """Flash a deck's buttons to identify which physical deck it is."""
        deck = session.deck
        if not session.drawable:
            return

        self.api.log(
            f"Identifying Stream Deck {session.serial} (flashing all buttons)"
        )
        key_count = deck.key_count()
        touch_keys = range(key_count, key_count + deck.touch_key_count())

        # Flash white
        white_img = self._create_button_image(session, "", "#ffffff", "#ffffff")
        native_white = PILHelper.to_native_key_format(deck, white_img)

        for flash in range(3):
            with deck:
                for k in range(key_count):
                    deck.set_key_image(k, native_white)
            for k in touch_keys:
                self._apply_key_color(session, k, "#ffffff")
            await asyncio.sleep(0.3)

            with deck:
                for k in range(key_count):
                    deck.set_key_image(k, b"\x00" * len(native_white))
            for k in touch_keys:
                self._apply_key_color(session, k, "#000000")
            await asyncio.sleep(0.3)

        # Restore current page
        await self._render_all_buttons(session)

    # ──── Helpers ────

    def _get_button_assignment(self, session, page, key_index):
        """The entry a key runs on a page of this deck (key_entries.py)."""
        return assignment_for(self._deck_config(session), page, key_index)

    @staticmethod
    def _hidapi_error_message() -> str:
        """Return a user-friendly error message for missing HIDAPI library."""
        system = platform_mod.system()
        if system == "Windows":
            return (
                "HIDAPI library not found. It should have been installed automatically. "
                "Check that plugin_repo/.deps/hidapi.dll exists. If not, reinstall "
                "the Stream Deck plugin from the community repository."
            )
        elif system == "Linux":
            return (
                "HIDAPI library not found. Run these commands on the server, "
                "then restart the plugin:\n"
                "  sudo apt-get install -y libhidapi-libusb0\n"
                "  echo 'SUBSYSTEM==\"usb\", ATTRS{idVendor}==\"0fd9\", MODE=\"0666\"' "
                "| sudo tee /etc/udev/rules.d/99-streamdeck.rules\n"
                "  sudo udevadm control --reload-rules && sudo udevadm trigger"
            )
        return "HIDAPI library not found. See the Stream Deck plugin README for install instructions."
