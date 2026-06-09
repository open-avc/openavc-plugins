"""
Elgato Stream Deck plugin for OpenAVC.

Turns any Elgato Stream Deck into a physical control surface for AV room control.
Button presses execute macros; button images update based on system state.

Supported models: Neo, Mini, MK.2/Original V2, XL, Plus, Pedal.
"""

import asyncio
import json
import os
import platform as platform_mod
from pathlib import Path

# StreamDeck and PIL are imported lazily in start() to ensure
# the .deps/ DLL search path is set up by the plugin loader first.
StreamDeck = None
PILHelper = None
Image = None
ImageDraw = None
ImageFont = None


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


def _unwrap_binding(value):
    """Unwrap a binding value that may be a single dict or an array of dicts.

    The Surface Configurator stores press/release/hold as arrays to support
    multiple sequential actions. The plugin handler expects a single dict
    (the first action, which carries the mode and config).
    """
    if isinstance(value, list) and len(value) > 0:
        return value[0] if isinstance(value[0], dict) else None
    if isinstance(value, dict):
        return value
    return None


# ──── Condition Evaluation ────
#
# Self-contained copy of the platform's condition evaluator
# (server/core/condition_eval.py). Vendored rather than imported so this
# community plugin stays portable and doesn't couple to server internals.
# Semantics are kept identical to the platform evaluator so a `visible_when`
# or `auto_page` condition behaves the same here as in a macro skip_if or a
# trigger guard.

_CONDITION_OPERATOR_ALIASES = {
    "equals": "eq", "not_equals": "ne", "==": "eq", "!=": "ne",
    ">": "gt", "<": "lt", ">=": "gte", "<=": "lte",
    "equal": "eq", "not_equal": "ne", "greater_than": "gt", "less_than": "lt",
    "greater_or_equal": "gte", "less_or_equal": "lte",
}


def _coerce_numeric(value):
    """Try to coerce a value to a number for comparison."""
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    return None


def _coerce_bool(value):
    """Normalize boolean-like values for comparison."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    return None


def _eval_operator(op, actual, target):
    """Evaluate a comparison operator with alias normalization and type coercion.

    Raises ValueError on an unknown operator (callers treat that as no match).
    """
    op = _CONDITION_OPERATOR_ALIASES.get(op, op)

    if op in ("eq", "ne"):
        if isinstance(actual, bool) or isinstance(target, bool):
            a_bool = _coerce_bool(actual)
            t_bool = _coerce_bool(target)
            if a_bool is not None and t_bool is not None:
                return (a_bool == t_bool) if op == "eq" else (a_bool != t_bool)
        if type(actual) is not type(target):
            a_num = _coerce_numeric(actual)
            t_num = _coerce_numeric(target)
            if a_num is not None and t_num is not None:
                return (a_num == t_num) if op == "eq" else (a_num != t_num)
        return (actual == target) if op == "eq" else (actual != target)

    if op in ("gt", "lt", "gte", "lte"):
        if actual is None or target is None:
            return False
        a_num = _coerce_numeric(actual)
        t_num = _coerce_numeric(target)
        if a_num is not None and t_num is not None:
            if op == "gt":
                return a_num > t_num
            if op == "lt":
                return a_num < t_num
            if op == "gte":
                return a_num >= t_num
            return a_num <= t_num
        try:
            if op == "gt":
                return actual > target
            if op == "lt":
                return actual < target
            if op == "gte":
                return actual >= target
            return actual <= target
        except TypeError:
            return False

    if op == "truthy":
        return bool(actual)
    if op == "falsy":
        return not bool(actual)
    raise ValueError(f"Unknown condition operator: {op!r}")


def _condition_state_keys(cond):
    """Collect every state key referenced by a condition.

    Handles a single ``{key, operator, value}`` condition plus the compound
    ``{all: [...]}`` and ``{any: [...]}`` forms (recursively).
    """
    keys = []
    if not isinstance(cond, dict):
        return keys
    for group in ("all", "any"):
        sub = cond.get(group)
        if isinstance(sub, list):
            for child in sub:
                keys.extend(_condition_state_keys(child))
    key = cond.get("key")
    if key:
        keys.append(key)
    return keys


class StreamDeckPlugin:

    PLUGIN_INFO = {
        "id": "streamdeck",
        "name": "Elgato Stream Deck",
        "version": "1.11.0",
        "author": "OpenAVC",
        "description": "Use Elgato Stream Deck hardware as a physical control surface.",
        "category": "control_surface",
        "license": "MIT",
        "platforms": ["win_x64", "linux_x64", "linux_arm64"],
        "min_openavc_version": "0.16.0",
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
        ],
    }

    CONFIG_SCHEMA = {
        "brightness": {
            "type": "integer",
            "label": "Button Brightness",
            "description": "Screen brightness percentage (0-100).",
            "default": 70,
            "min": 0,
            "max": 100,
        },
        "button_color": {
            "type": "string",
            "label": "Default Button Color",
            "description": "Background color for buttons without a custom icon (hex).",
            "default": "#1a1a2e",
        },
        "text_color": {
            "type": "string",
            "label": "Text Color",
            "description": "Label text color (hex).",
            "default": "#e0e0e0",
        },
        "pages": {
            "type": "group",
            "label": "Button Assignments",
            "description": "Configured by the Surface Configurator. Do not edit manually.",
            "fields": {},
        },
    }

    SURFACE_LAYOUT = {
        "type": "grid",
        "rows": 2,
        "columns": 4,
        "key_size_px": 72,
        "key_spacing_px": 4,
        "supports_pages": True,
        "max_pages": 10,
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
        "shows static text. "
        "Brightness can follow state: a top-level 'auto_brightness' array of "
        "{\"level\": 0-100, \"when\": {condition}} rules (same operator "
        "schema; first match wins, no match falls back to the base "
        "'brightness' config). A top-level 'idle_dim' object "
        "{\"after_seconds\": N, \"level\": 0-100} dims the deck after N "
        "seconds without any key/dial/touch input; any input wakes it. "
        "Example: dim to 10 when device.projector_1.power is off, and "
        "idle-dim to 5 after 600 seconds. "
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
        "When dial_count > 0, a top-level 'dials' array configures the rotary "
        "encoders (not paged — dials keep their assignment on every page): "
        "{\"index\": 0, \"label\": \"Volume\", \"adjust\": {\"key\": "
        "\"var.volume\", \"step\": 2, \"min\": 0, \"max\": 100}, \"cw\": "
        "[actions], \"ccw\": [actions], \"press\": [actions]}. 'adjust' "
        "increments the key by step per detent turned (clamped to min/max) — "
        "ideal for volume, mic gain, or camera pan/tilt speed; the key must be "
        "a var.* variable or this plugin's own state, and a macro/trigger can "
        "watch it to drive the device. 'cw'/'ccw' actions run on each "
        "clockwise/counter-clockwise turn event, 'press' on dial push; all use "
        "the same action format as button press arrays. "
        "When has_touchscreen is true, the touch strip shows one zone per dial "
        "by default (the dial's label and live adjust value — no config "
        "needed). To customize, set a top-level 'touchscreen' object: "
        "{\"zones\": [{\"label\": \"Mics\", \"value_source\": \"var.mic_gain\", "
        "\"label_source\": \"(optional state key)\", \"touch\": [actions], "
        "\"bg_color\": \"#1a1a2e\", \"text_color\": \"#e0e0e0\"}]}. Zones "
        "split the strip evenly (or set explicit 'x'/'w' pixel bounds, strip "
        "is 800x100); 'touch' actions run when the zone is tapped."
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

    def __init__(self):
        self.api = None
        self.deck = None
        self.current_page = 0
        self._feedback_subs = []
        self._auto_page_keys = set()   # state keys watched by auto_page rules
        self._touch_strip_keys = set()  # state keys shown on the touchscreen strip
        self._info_strip_keys = set()   # state keys shown on the info strip
        self._brightness_keys = set()   # state keys watched by auto_brightness rules
        self._last_input = 0.0          # loop time of the last key/dial/touch input
        self._idle_dimmed = False       # True while idle_dim has lowered brightness
        self._loop = None
        self._model_info = None
        self._opening = False    # re-entrancy guard while a deck is being opened
        self._hold_tasks = {}    # key_index -> periodic task ID for hold-repeat
        self._press_times = {}   # key_index -> timestamp for tap/hold mode
        self._pressed_keys = set()  # keys currently held (momentary highlight)
        self._icon_font = None   # Loaded Lucide TTF font for icon rendering
        self._icon_map = {}      # icon-name -> unicode code point
        self._icon_cache = {}    # (icon_name, size, color_hex) -> PIL Image
        self._text_font_path = None  # Bundled label font (legible on Linux/Pi)
        self._font_cache = {}    # size -> ImageFont
        self._label_cache = {}   # (label, w, h, color, max_font, max_lines) -> RGBA

    async def start(self, api):
        """Initialize and connect to the Stream Deck."""
        self.api = api
        self._loop = asyncio.get_event_loop()

        # Lazy-import after DLL paths are set up by plugin loader
        try:
            _lazy_import()
        except ImportError as e:
            if "hidapi" in str(e).lower() or "hid" in str(e).lower():
                raise RuntimeError(self._hidapi_error_message()) from e
            raise RuntimeError(
                "Failed to import Stream Deck library. "
                "Make sure the 'streamdeck' and 'pillow' packages are installed."
            ) from e

        # Load Lucide icon font for button icon rendering
        self._load_icon_font()
        # Load the bundled text font for legible labels on every platform
        self._load_text_font()

        # Set initial state (geometry keys are filled in by _open_deck once a
        # deck is detected; the Surface Configurator falls back to the static
        # SURFACE_LAYOUT while connected is false)
        await self.api.state_set("connected", False)
        await self.api.state_set("model", "")
        await self.api.state_set("serial", "")
        await self.api.state_set("current_page", 0)
        await self.api.state_set("key_count", 0)
        await self.api.state_set("rows", 0)
        await self.api.state_set("columns", 0)
        await self.api.state_set("dial_count", 0)
        await self.api.state_set("touch_key_count", 0)
        await self.api.state_set("has_touchscreen", False)
        await self.api.state_set("has_info_screen", False)

        # Validate HIDAPI is loadable now, so a missing native library fails
        # the plugin with a clear message instead of silently looping in the
        # watchdog below.
        try:
            StreamDeck.DeviceManager().enumerate()
        except Exception as e:
            if "hidapi" in str(e).lower() or "hid" in str(e).lower():
                raise RuntimeError(self._hidapi_error_message()) from e
            raise

        # Subscribe to context actions (independent of any connected deck, and
        # kept across reconnects).
        await self.api.event_subscribe(
            "plugin.streamdeck.action.*", self._on_context_action
        )

        # A single watchdog opens the deck if one is present now, recovers it
        # after a mid-session unplug, and connects one that appears later. Its
        # first iteration runs almost immediately.
        self.api.create_periodic_task(
            self._watchdog, interval_seconds=3, name="deck_watchdog"
        )

    async def stop(self):
        """Release the Stream Deck."""
        if self.deck:
            try:
                self.deck.reset()
                self.deck.close()
            except Exception as e:
                self.api.log(f"Error closing deck: {e}", level="warning")
            self.deck = None
        self.api.log("Stream Deck plugin stopped")

    async def health_check(self):
        """Report deck connection health."""
        if self.deck and self.deck.is_open():
            return {"status": "ok", "message": f"Connected: {self._model_info or 'Unknown'}"}
        return {"status": "degraded", "message": "No Stream Deck connected"}

    # ──── Device Management ────

    async def _open_deck(self, deck):
        """Open a deck, configure it, and set up callbacks."""
        deck.open()
        deck.reset()

        model = deck.deck_type()
        serial = deck.get_serial_number() or "unknown"
        key_count = deck.key_count()

        # Geometry comes from the live hardware, not a static model table, so
        # any deck the library enumerates renders correctly — including models
        # added after this plugin was written.
        rows, columns = deck.key_layout()
        dial_count = deck.dial_count()
        touch_key_count = deck.touch_key_count()
        has_touchscreen = deck.is_touch()
        try:
            # A secondary info screen reports a non-zero size (e.g. Neo 248x58)
            has_info_screen = deck.screen_image_format()["size"][0] > 0
        except Exception:
            has_info_screen = False

        self.deck = deck
        self._model_info = model

        # Apply brightness (base config level or the first matching
        # auto_brightness rule), and start the idle timer fresh.
        self._last_input = asyncio.get_event_loop().time()
        self._idle_dimmed = False
        deck.set_brightness(await self._current_brightness_level())

        # Set up key callback (async variant — fires on our event loop)
        deck.set_key_callback_async(self._on_key_change, loop=self._loop)

        # Wire dials and the touchscreen when the hardware has them
        if dial_count > 0:
            deck.set_dial_callback_async(self._on_dial_event, loop=self._loop)
        if has_touchscreen:
            deck.set_touchscreen_callback_async(
                self._on_touchscreen_event, loop=self._loop
            )

        # Publish the detected hardware to state. The Surface Configurator
        # prefers these keys over the static SURFACE_LAYOUT while connected,
        # so the editor always draws the surface that's actually plugged in.
        await self.api.state_set("connected", True)
        await self.api.state_set("model", model)
        await self.api.state_set("serial", serial)
        await self.api.state_set("key_count", key_count)
        await self.api.state_set("rows", rows)
        await self.api.state_set("columns", columns)
        await self.api.state_set("dial_count", dial_count)
        await self.api.state_set("touch_key_count", touch_key_count)
        await self.api.state_set("has_touchscreen", has_touchscreen)
        await self.api.state_set("has_info_screen", has_info_screen)
        await self.api.state_set("current_page", 0)
        self.current_page = 0

        self.api.log(
            f"Connected to {model} (S/N: {serial}, {key_count} keys, "
            f"{rows}x{columns}, {dial_count} dials"
            f"{', touchscreen' if has_touchscreen else ''}"
            f"{f', {touch_key_count} touch keys' if touch_key_count else ''})"
        )
        await self.api.event_emit("connected", {"model": model, "serial": serial})

        # Subscribe to state changes for feedback, visibility, and auto-page keys
        await self._setup_feedback_subscriptions()

        # Apply the initial auto-page selection before the first render so we
        # don't briefly show page 0 and then immediately switch.
        initial_page = await self._evaluate_auto_page()
        if initial_page is not None:
            self.current_page = initial_page
            await self.api.state_set("current_page", initial_page)

        # Render all buttons for the current page, then the secondary displays
        await self._render_all_buttons()
        await self._render_touchscreen()
        await self._render_info_strip()

    async def _watchdog(self):
        """Keep a deck connected: open one if present, recover after unplug.

        Runs on a single periodic task for the plugin's whole lifetime, so a
        deck that is unplugged mid-session is detected and re-opened on the next
        tick (the bundled library closes the deck object on a transport error
        but never re-opens it). Periodic ticks never overlap, but an
        ``_opening`` guard is kept so a slow open can't be re-entered.
        """
        if self._opening:
            return

        # If we hold a deck, confirm it's still healthy; otherwise tear it down.
        if self.deck is not None:
            try:
                healthy = self.deck.is_open() and self.deck.connected()
            except Exception:
                healthy = False
            if healthy:
                # The watchdog tick doubles as the idle-dim clock.
                await self._check_idle_dim()
                return
            await self._handle_deck_lost()

        # No (healthy) deck — try to (re)connect to the first one present.
        try:
            decks = StreamDeck.DeviceManager().enumerate()
        except Exception:
            decks = []
        if not decks:
            return

        self._opening = True
        try:
            await self._open_deck(decks[0])
        except Exception as e:
            self.api.log(f"Failed to open Stream Deck: {e}", level="warning")
            self.deck = None
        finally:
            self._opening = False

    async def _handle_deck_lost(self):
        """Tear down a deck that has gone away so the watchdog can re-open it.

        Cancels in-flight hold-repeat tasks, drops the feedback subscriptions
        (re-created on re-open), closes the stale deck object, and publishes the
        disconnect. The context-action subscription is left intact — it lives
        for the plugin's whole lifetime, not per-deck.
        """
        self.api.log("Stream Deck disconnected", level="warning")

        for task_id in list(self._hold_tasks.values()):
            self.api.cancel_task(task_id)
        self._hold_tasks.clear()
        self._press_times.clear()

        for sub_id in self._feedback_subs:
            try:
                await self.api.state_unsubscribe(sub_id)
            except Exception:
                pass
        self._feedback_subs = []

        old = self.deck
        self.deck = None
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

        self._model_info = None
        self._idle_dimmed = False
        await self.api.state_set("connected", False)
        await self.api.event_emit("disconnected", {})

    # ──── Key Handling ────

    async def _on_key_change(self, deck, key_index, pressed):
        """Handle a physical button press/release with mode support."""
        await self._note_input()
        page = self.current_page

        event_type = "press" if pressed else "release"
        await self.api.event_emit(
            f"button.{event_type}",
            {"key": key_index, "page": page},
        )

        assignment = self._get_button_assignment(page, key_index)
        if not assignment:
            return

        # A hidden button (visible_when false) is inert: fire no action. Also
        # clear any in-flight hold/tap-hold state so a button that became
        # hidden mid-press can't leak a periodic task or fire a stale action.
        if not await self._is_button_visible(assignment):
            task_id = self._hold_tasks.pop(key_index, None)
            if task_id:
                self.api.cancel_task(task_id)
            self._press_times.pop(key_index, None)
            self._pressed_keys.discard(key_index)  # don't leak a press highlight
            return

        # Momentary press highlight: mark the key as held and redraw. The mark
        # lives in the render path so a feedback/toggle re-render keeps the
        # highlight rather than fighting it; the redraw is a no-op without a
        # visual deck.
        if pressed:
            self._pressed_keys.add(key_index)
        else:
            self._pressed_keys.discard(key_index)
        if self.deck and self.deck.is_visual():
            await self._render_button(key_index)

        # Get press binding. The UI stores press as an array of actions; mode
        # and toggle/hold config live on the first entry, while a default tap
        # button fires every entry in order.
        bindings = assignment.get("bindings", {})
        press_actions = self._press_actions(bindings)
        press = press_actions[0] if press_actions else None

        if not press or not isinstance(press, dict):
            return

        mode = press.get("mode", "tap")

        if mode == "hold_repeat":
            if pressed:
                # Cancel any existing hold task for this key first
                old_task = self._hold_tasks.pop(key_index, None)
                if old_task:
                    self.api.cancel_task(old_task)
                # Store task ID synchronously BEFORE any await to prevent
                # race condition where release fires during _execute_action
                # and can't find the task to cancel.  The periodic task's
                # first iteration fires the action almost immediately.
                interval = press.get("hold_repeat_ms", 200) / 1000.0
                self._hold_tasks[key_index] = self.api.create_periodic_task(
                    lambda: self._execute_action(press, f"key {key_index}"),
                    interval_seconds=interval,
                    name=f"hold_repeat_{key_index}",
                )
            else:
                task_id = self._hold_tasks.pop(key_index, None)
                if task_id:
                    self.api.cancel_task(task_id)
            return

        if mode == "toggle":
            if not pressed:
                return
            # Toggle: read toggle_key state to determine on/off
            off_action = press.get("off_action")
            toggle_key = press.get("toggle_key", "")
            toggle_value = press.get("toggle_value")
            is_active = False
            if toggle_key:
                value = await self.api.state_get(toggle_key)
                if toggle_value is not None:
                    is_active = str(value).lower() == str(toggle_value).lower()
                else:
                    is_active = bool(value)

            if is_active and off_action and isinstance(off_action, dict):
                await self._execute_action(off_action, f"key {key_index}")
            else:
                await self._execute_action(press, f"key {key_index}")

            # Update button label if on_label/off_label configured
            on_label = press.get("on_label", "")
            off_label = press.get("off_label", "")
            if on_label or off_label:
                # Re-render after action (state may have changed)
                await asyncio.sleep(0.1)
                await self._render_button(key_index)
            return

        if mode == "tap_hold":
            threshold = press.get("hold_threshold_ms", 500) / 1000.0
            hold_action = press.get("hold_action")
            if pressed:
                self._press_times[key_index] = asyncio.get_event_loop().time()
            else:
                if key_index not in self._press_times:
                    # Release with no recorded press (e.g. the press was
                    # suppressed while the button was hidden) — fire nothing.
                    return
                press_time = self._press_times.pop(key_index)
                held = asyncio.get_event_loop().time() - press_time
                if held >= threshold and hold_action and isinstance(hold_action, dict):
                    await self._execute_action(hold_action, f"key {key_index}")
                else:
                    await self._execute_action(press, f"key {key_index}")
            return

        # Default: tap mode — fire every configured action in order, on press
        if pressed:
            await self._execute_actions(press_actions, f"key {key_index}")

    @staticmethod
    def _press_actions(bindings):
        """Return the press binding as a list of action dicts.

        The Surface Configurator stores ``press`` as an array of action
        objects (mode/toggle/hold config lives on the first entry). A single
        dict is wrapped; anything else yields an empty list.
        """
        if not isinstance(bindings, dict):
            return []
        press = bindings.get("press")
        if isinstance(press, list):
            return [a for a in press if isinstance(a, dict)]
        if isinstance(press, dict):
            return [press]
        return []

    async def _execute_actions(self, actions, source):
        """Execute a list of action bindings sequentially, in order."""
        for action in actions:
            if isinstance(action, dict):
                await self._execute_action(action, source)

    async def _execute_action(self, action_binding, source):
        """Execute a single surface action binding.

        Supports the documented surface action set: ``macro``,
        ``device.command``, ``state.set`` (scoped like the panel plugin
        bridge), and ``navigate`` (deck page: next/previous or a page index).
        ``source`` only labels log lines (e.g. ``key 3``, ``dial 0``).
        """
        action = action_binding.get("action", "")

        if action == "navigate":
            page_id = action_binding.get("page", "")
            if page_id == "__next_page__":
                await self._change_page(self.current_page + 1)
            elif page_id == "__prev_page__":
                await self._change_page(self.current_page - 1)
            else:
                # A specific page index (int or numeric string).
                try:
                    await self._change_page(int(page_id))
                except (TypeError, ValueError):
                    pass

        elif action == "macro":
            macro = action_binding.get("macro", "")
            if macro:
                try:
                    await self.api.macro_execute(macro)
                    self.api.log(f"Executed macro '{macro}' from {source}", level="debug")
                except Exception as e:
                    self.api.log(f"Error executing macro '{macro}': {e}", level="error")

        elif action == "device.command":
            device = action_binding.get("device", "")
            command = action_binding.get("command", "")
            params = action_binding.get("params")
            if device and command:
                try:
                    await self.api.device_command(device, command, params if isinstance(params, dict) else None)
                    self.api.log(f"Sent {command} to {device} from {source}", level="debug")
                except Exception as e:
                    self.api.log(f"Error sending command: {e}", level="error")

        elif action == "state.set":
            key = action_binding.get("key", "")
            if key:
                try:
                    await self._apply_state_set(key, action_binding.get("value"))
                except Exception as e:
                    self.api.log(f"Error setting state '{key}': {e}", level="error")

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

    @staticmethod
    def _action_list(value):
        """Normalize an action binding (single dict or list of dicts) to a list."""
        if isinstance(value, list):
            return [a for a in value if isinstance(a, dict)]
        if isinstance(value, dict):
            return [value]
        return []

    def _get_dial_config(self, index):
        """Look up the dial config entry for a dial index."""
        dials = self.api.config.get("dials", [])
        if not isinstance(dials, list):
            return None
        for dial in dials:
            if isinstance(dial, dict) and dial.get("index") == index:
                return dial
        return None

    async def _on_dial_event(self, deck, dial, event, value):
        """Handle a dial turn or push.

        Event kinds are matched by enum name (``TURN``/``PUSH``) so this
        module never needs the StreamDeck enums at import time (they only
        exist after the lazy import, and tests run without the library).
        """
        await self._note_input()
        kind = getattr(event, "name", None)
        cfg = self._get_dial_config(dial)

        if kind == "PUSH":
            if not value:
                return  # release
            await self.api.event_emit("dial.press", {"dial": dial})
            if cfg:
                await self._execute_actions(
                    self._action_list(cfg.get("press")), f"dial {dial}"
                )
            return

        if kind == "TURN":
            try:
                detents = int(value)
            except (TypeError, ValueError):
                return
            if detents == 0:
                return
            await self.api.event_emit("dial.turn", {"dial": dial, "amount": detents})
            if not cfg:
                return
            adjust = cfg.get("adjust")
            if isinstance(adjust, dict) and adjust.get("key"):
                await self._apply_dial_adjust(adjust, detents)
            actions = cfg.get("cw") if detents > 0 else cfg.get("ccw")
            await self._execute_actions(self._action_list(actions), f"dial {dial}")

    async def _apply_dial_adjust(self, adjust, detents):
        """Increment a numeric state value by ``step * detents``, clamped.

        The turn magnitude (detents moved since the last event) scales the
        step, so spinning a dial fast moves the value proportionally faster.
        Writes go through the same scope rule as state.set actions (own
        ``plugin.<id>.*`` state or ``var.*`` user variables only).
        """
        key = adjust.get("key", "")
        step = _coerce_numeric(adjust.get("step"))
        if step is None:
            step = 1
        minimum = _coerce_numeric(adjust.get("min"))
        maximum = _coerce_numeric(adjust.get("max"))

        current = _coerce_numeric(await self.api.state_get(key))
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

    def _touch_zones(self):
        """Return the effective touchscreen zones with resolved pixel bounds.

        Explicit ``touchscreen.zones`` config wins; zones missing ``x``/``w``
        are laid out by splitting the strip evenly. With no zones configured,
        one zone is created per dial (aligned under it) showing the dial's
        label and its adjust value — so a configured dial gets a live readout
        with zero extra config.
        """
        if not self.deck:
            return []
        try:
            width = self.deck.touchscreen_image_format()["size"][0]
        except Exception:
            width = 800

        ts = self.api.config.get("touchscreen", {})
        zones = ts.get("zones") if isinstance(ts, dict) else None
        zones = [z for z in zones if isinstance(z, dict)] if isinstance(zones, list) else []

        if not zones:
            for i in range(self.deck.dial_count()):
                cfg = self._get_dial_config(i) or {}
                adjust = cfg.get("adjust")
                adjust = adjust if isinstance(adjust, dict) else {}
                zones.append({
                    "label": cfg.get("label", ""),
                    "value_source": adjust.get("key", ""),
                })

        if not zones:
            return []

        slot = width // len(zones)
        resolved = []
        for i, zone in enumerate(zones):
            x = zone.get("x")
            w = zone.get("w")
            x = int(x) if isinstance(x, (int, float)) else i * slot
            w = int(w) if isinstance(w, (int, float)) else slot
            resolved.append({**zone, "x": x, "w": w})
        return resolved

    def _zone_at(self, x):
        """Return the touchscreen zone containing pixel ``x``, or None."""
        for zone in self._touch_zones():
            if zone["x"] <= x < zone["x"] + zone["w"]:
                return zone
        return None

    async def _on_touchscreen_event(self, deck, event, value):
        """Handle a touchscreen tap: map the x position to a zone's actions."""
        await self._note_input()
        kind = getattr(event, "name", None)
        if kind not in ("SHORT", "LONG"):
            return  # DRAG is unused
        x = value.get("x") if isinstance(value, dict) else None
        if not isinstance(x, (int, float)):
            return
        await self.api.event_emit("touchscreen.touch", {"x": int(x)})
        zone = self._zone_at(int(x))
        if zone:
            await self._execute_actions(
                self._action_list(zone.get("touch")), "touchscreen"
            )

    async def _render_touchscreen(self):
        """Render the touchscreen strip: one cell per zone (label + value)."""
        if not self.deck or not self.deck.is_visual() or not self.deck.is_touch():
            return
        try:
            width, height = self.deck.touchscreen_image_format()["size"]
        except Exception:
            return
        zones = self._touch_zones()

        bg = self.api.config.get("button_color", "#1a1a2e")
        fg = self.api.config.get("text_color", "#e0e0e0")
        img = Image.new("RGB", (width, height), bg)
        draw = ImageDraw.Draw(img)

        for zone in zones:
            zx, zw = zone["x"], zone["w"]

            label = zone.get("label", "")
            label_source = zone.get("label_source", "")
            if label_source:
                live_label = await self.api.state_get(label_source)
                if live_label is not None:
                    label = str(live_label)

            value_text = ""
            value_source = zone.get("value_source", "")
            if value_source:
                live_value = await self.api.state_get(value_source)
                if live_value is not None:
                    value_text = str(live_value)

            zone_fg = zone.get("text_color") or fg
            if zone.get("bg_color"):
                draw.rectangle(
                    [zx, 0, zx + zw - 1, height - 1], fill=zone["bg_color"]
                )

            if label and value_text:
                self._paste_label(
                    img, label, zone_fg,
                    (zx + 4, 4, zw - 8, height // 3),
                    max_font=16, max_lines=1,
                )
                self._paste_label(
                    img, value_text, zone_fg,
                    (zx + 4, height // 3 + 4, zw - 8, height - height // 3 - 8),
                    max_font=30, max_lines=1,
                )
            elif label or value_text:
                self._paste_label(
                    img, label or value_text, zone_fg,
                    (zx + 4, 4, zw - 8, height - 8),
                    max_font=24, max_lines=2,
                )

            if zx > 0:
                draw.line([(zx, 8), (zx, height - 8)], fill="#3a3a4e", width=1)

        try:
            native = PILHelper.to_native_touchscreen_format(self.deck, img)
            with self.deck:
                self.deck.set_touchscreen_image(native, 0, 0, width, height)
        except Exception as e:
            self.api.log(f"Error setting touchscreen image: {e}", level="debug")

    # ──── Info strip (decks with a secondary info screen) ────

    async def _render_info_strip(self):
        """Render the secondary info screen from the ``info_strip`` config.

        Config shape: ``{"source": "state"|"text", "key": "<state key>",
        "text": "<static text>", "label": "<small heading>"}``. A state
        source shows the key's live value; re-rendered on change.
        """
        if not self.deck or not self.deck.is_visual():
            return
        try:
            width, height = self.deck.screen_image_format()["size"]
        except Exception:
            return
        if not width or not height:
            return  # this deck has no info screen

        bg = self.api.config.get("button_color", "#1a1a2e")
        fg = self.api.config.get("text_color", "#e0e0e0")
        img = Image.new("RGB", (width, height), bg)

        cfg = self.api.config.get("info_strip")
        if isinstance(cfg, dict):
            label = cfg.get("label", "")
            if cfg.get("source") == "text":
                value = str(cfg.get("text", ""))
            else:
                key = cfg.get("key", "")
                live = await self.api.state_get(key) if key else None
                value = str(live) if live is not None else ""

            if label and value:
                self._paste_label(
                    img, label, fg, (4, 2, width - 8, height // 3),
                    max_font=14, max_lines=1,
                )
                self._paste_label(
                    img, value, fg,
                    (4, height // 3 + 2, width - 8, height - height // 3 - 4),
                    max_font=24, max_lines=1,
                )
            elif label or value:
                self._paste_label(
                    img, label or value, fg, (4, 2, width - 8, height - 4),
                    max_font=22, max_lines=2,
                )

        try:
            native = PILHelper.to_native_screen_format(self.deck, img)
            with self.deck:
                self.deck.set_screen_image(native)
        except Exception as e:
            self.api.log(f"Error setting info strip image: {e}", level="debug")

    # ──── Brightness (auto rules + idle dim) ────

    async def _current_brightness_level(self):
        """Return the active brightness: the first matching ``auto_brightness``
        rule's level, else the base ``brightness`` config value. Clamped 0-100."""
        rules = self.api.config.get("auto_brightness", [])
        if isinstance(rules, list):
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                when = rule.get("when")
                level = _coerce_numeric(rule.get("level"))
                if when is None or level is None:
                    continue
                if await self._eval_condition(when):
                    return max(0, min(100, int(level)))
        base = _coerce_numeric(self.api.config.get("brightness", 70))
        return max(0, min(100, int(base if base is not None else 70)))

    def _set_deck_brightness(self, level):
        """Apply a brightness level to the deck (best-effort)."""
        if not self.deck:
            return
        try:
            self.deck.set_brightness(level)
        except Exception as e:
            self.api.log(f"Error setting brightness: {e}", level="debug")

    async def _apply_active_brightness(self):
        """Re-apply the rule-or-base brightness (used on wake and rule change)."""
        if not self.deck:
            return
        self._set_deck_brightness(await self._current_brightness_level())

    async def _check_idle_dim(self):
        """Dim the deck when no input has arrived for ``idle_dim.after_seconds``.

        Runs on the watchdog tick. Any key/dial/touch input wakes the deck via
        ``_note_input``.
        """
        cfg = self.api.config.get("idle_dim")
        if not isinstance(cfg, dict) or self._idle_dimmed:
            return
        after = _coerce_numeric(cfg.get("after_seconds"))
        level = _coerce_numeric(cfg.get("level"))
        if after is None or after <= 0 or level is None:
            return
        now = asyncio.get_event_loop().time()
        if now - self._last_input >= after:
            self._idle_dimmed = True
            self._set_deck_brightness(max(0, min(100, int(level))))

    async def _note_input(self):
        """Record user input: resets the idle timer and wakes a dimmed deck."""
        self._last_input = asyncio.get_event_loop().time()
        if self._idle_dimmed:
            self._idle_dimmed = False
            await self._apply_active_brightness()

    # ──── Page Navigation ────

    async def _change_page(self, new_page):
        """Switch to a different button page."""
        max_pages = self.api.config.get("max_pages", 10)
        if new_page < 0:
            new_page = 0
        elif new_page >= max_pages:
            new_page = max_pages - 1

        if new_page == self.current_page:
            return

        self.current_page = new_page
        await self.api.state_set("current_page", new_page)
        await self._render_all_buttons()
        self.api.log(f"Switched to page {new_page}", level="debug")

    # ──── Button Rendering ────

    async def _render_all_buttons(self):
        """Render every key for the current page (LCD keys + touch keys)."""
        if not self.deck or not self.deck.is_visual():
            return

        key_count = self.deck.key_count() + self.deck.touch_key_count()
        for key_index in range(key_count):
            await self._render_button(key_index)

    def _is_touch_key(self, key_index):
        """True for the color-only touch keys indexed after the LCD keys."""
        if not self.deck:
            return False
        key_count = self.deck.key_count()
        return key_count <= key_index < key_count + self.deck.touch_key_count()

    async def _render_button(self, key_index):
        """Render a single button image based on its assignment and state."""
        if not self.deck or not self.deck.is_visual():
            return

        is_touch_key = self._is_touch_key(key_index)
        assignment = self._get_button_assignment(self.current_page, key_index)

        # Touch keys have no LCD — an unassigned one just goes dark.
        if is_touch_key and not assignment:
            self._apply_key_color(key_index, "#000000")
            return

        global_bg = self.api.config.get("button_color", "#1a1a2e")
        global_text = self.api.config.get("text_color", "#e0e0e0")
        label = ""
        icon = None
        bg_color = global_bg
        text_color = global_text

        if assignment:
            # Hidden by visible_when → blank black key; render nothing else.
            bindings = assignment.get("bindings", {})
            visible_when = bindings.get("visible_when") if isinstance(bindings, dict) else None
            if visible_when is not None and not await self._eval_condition(visible_when):
                if is_touch_key:
                    self._apply_key_color(key_index, "#000000")
                else:
                    self._apply_key_image(
                        key_index, self._create_button_image("", "#000000", "#000000")
                    )
                return

            label = assignment.get("label", "")
            icon = assignment.get("icon") or None

            # Per-button default colors (override global defaults)
            bg_color = assignment.get("bg_color") or global_bg
            text_color = assignment.get("text_color") or global_text

            # Toggle mode: on_label/off_label override static label
            press_binding = _unwrap_binding(bindings.get("press")) if isinstance(bindings, dict) else None
            if press_binding and isinstance(press_binding, dict) and press_binding.get("mode") == "toggle":
                tk = press_binding.get("toggle_key", "")
                tv = press_binding.get("toggle_value")
                if tk:
                    tval = await self.api.state_get(tk)
                    t_active = (str(tval).lower() == str(tv).lower()) if tv is not None else bool(tval)
                    on_lbl = press_binding.get("on_label", "")
                    off_lbl = press_binding.get("off_label", "")
                    if t_active and on_lbl:
                        label = on_lbl
                    elif not t_active and off_lbl:
                        label = off_lbl

            # Read feedback config from bindings
            feedback = bindings.get("feedback") if isinstance(bindings, dict) else None

            if feedback and isinstance(feedback, dict):
                fk = feedback.get("key", "")
                condition = feedback.get("condition", {})
                style_active = feedback.get("style_active", {})
                style_inactive = feedback.get("style_inactive", {})

                if fk:
                    value = await self.api.state_get(fk)

                    # Multi-state feedback
                    states = feedback.get("states")
                    if states and isinstance(states, dict):
                        state_str = str(value) if value is not None else ""
                        appearance = states.get(state_str) or states.get(feedback.get("default_state", ""))
                        if appearance and isinstance(appearance, dict):
                            bg_color = appearance.get("bg_color", bg_color) or bg_color
                            text_color = appearance.get("text_color", text_color) or text_color
                            if appearance.get("label"):
                                label = appearance["label"]
                            if appearance.get("icon"):
                                icon = appearance["icon"]
                    else:
                        # Simple active/inactive feedback
                        expected = condition.get("equals") if isinstance(condition, dict) else None
                        is_active = (str(value).lower() == str(expected).lower()) if expected is not None else bool(value)

                        if is_active and isinstance(style_active, dict):
                            bg_color = style_active.get("bg_color", bg_color) or bg_color
                            text_color = style_active.get("text_color", text_color) or text_color
                            if feedback.get("label_active"):
                                label = feedback["label_active"]
                            if style_active.get("icon"):
                                icon = style_active["icon"]
                        elif not is_active and isinstance(style_inactive, dict):
                            bg_color = style_inactive.get("bg_color", bg_color) or bg_color
                            text_color = style_inactive.get("text_color", text_color) or text_color
                            if feedback.get("label_inactive"):
                                label = feedback["label_inactive"]
                            if style_inactive.get("icon"):
                                icon = style_inactive["icon"]

        # Touch keys show color only (no LCD): the effective background color
        # after feedback/toggle evaluation, brightened while held.
        if is_touch_key:
            color = bg_color
            if key_index in self._pressed_keys:
                color = self._lighten_hex(color, 0.25)
            self._apply_key_color(key_index, color)
            return

        # Generate the button image and set it on the deck. While the key is
        # physically held, draw the momentary-press highlight on top.
        image = self._create_button_image(label, bg_color, text_color, icon)
        if key_index in self._pressed_keys:
            image = self._apply_press_highlight(image)
        self._apply_key_image(key_index, image)

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

    def _apply_key_color(self, key_index, color):
        """Set a touch key's RGB backlight from a hex color (thread-safe)."""
        r, g, b = self._hex_to_rgb(color)
        try:
            with self.deck:
                self.deck.set_key_color(key_index, r, g, b)
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

    def _apply_key_image(self, key_index, image):
        """Encode a PIL image and set it on a deck key (thread-safe)."""
        try:
            native_image = PILHelper.to_native_key_format(self.deck, image)
            with self.deck:
                self.deck.set_key_image(key_index, native_image)
        except Exception as e:
            self.api.log(f"Error setting key {key_index} image: {e}", level="debug")

    def _create_button_image(self, label, bg_color, text_color, icon_name=None):
        """Create a PIL image for a button with optional icon and wrapped label."""
        image_format = self.deck.key_image_format()
        width = image_format["size"][0]
        height = image_format["size"][1]

        img = Image.new("RGB", (width, height), bg_color)

        # Load icon image if specified
        icon_img = self._render_icon(icon_name, text_color, width) if icon_name else None

        if icon_img and label:
            # Icon in the upper area, wrapped label below it.
            icon_size = min(width, height) // 2
            icon_img = icon_img.resize((icon_size, icon_size), Image.LANCZOS)
            icon_x = (width - icon_size) // 2
            icon_y = max(4, (height // 2) - icon_size + 4)
            img.paste(icon_img, (icon_x, icon_y), icon_img)

            text_top = icon_y + icon_size + 2
            self._paste_label(
                img, label, text_color,
                (2, text_top, width - 4, max(0, height - text_top - 2)),
                max_font=14, max_lines=2,
            )

        elif icon_img:
            # Icon only, centered
            icon_size = int(min(width, height) * 0.6)
            icon_img = icon_img.resize((icon_size, icon_size), Image.LANCZOS)
            icon_x = (width - icon_size) // 2
            icon_y = (height - icon_size) // 2
            img.paste(icon_img, (icon_x, icon_y), icon_img)

        elif label:
            # Label only — wrapped and shrunk to fill the key.
            pad = max(2, width // 12)
            self._paste_label(
                img, label, text_color,
                (pad, pad, width - 2 * pad, height - 2 * pad),
                max_font=max(14, height // 4), max_lines=3,
            )

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
        """Evaluate a visible_when / auto_page condition against current state.

        Supports a single ``{key, operator, value}`` condition, a compound
        ``{all: [...]}`` (every sub-condition must be true) and ``{any: [...]}``
        (at least one true) — matching the panel UI visible_when schema. An
        empty ``all`` is vacuously true and an empty ``any`` is false. Operator
        semantics mirror the platform condition evaluator.
        """
        if not isinstance(cond, dict):
            return False

        all_list = cond.get("all")
        if isinstance(all_list, list):
            for child in all_list:
                if not await self._eval_condition(child):
                    return False
            return True

        any_list = cond.get("any")
        if isinstance(any_list, list):
            for child in any_list:
                if await self._eval_condition(child):
                    return True
            return False

        key = cond.get("key")
        if not key:
            return False
        actual = await self.api.state_get(key)
        try:
            return _eval_operator(cond.get("operator", "eq"), actual, cond.get("value"))
        except ValueError:
            self.api.log(
                f"Ignoring unknown condition operator {cond.get('operator')!r}",
                level="debug",
            )
            return False

    async def _is_button_visible(self, assignment) -> bool:
        """True unless the button's visible_when condition evaluates false."""
        bindings = assignment.get("bindings", {})
        if not isinstance(bindings, dict):
            return True
        visible_when = bindings.get("visible_when")
        if visible_when is None:
            return True
        return await self._eval_condition(visible_when)

    async def _evaluate_auto_page(self):
        """Return the page of the first matching auto_page rule, or None.

        Rules are evaluated in array order; the first whose ``when`` condition
        is true wins. The page index is clamped to the valid range.
        """
        rules = self.api.config.get("auto_page", [])
        if not isinstance(rules, list):
            return None
        max_pages = self.api.config.get("max_pages", 10)
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            when = rule.get("when")
            page = rule.get("page")
            if when is None or page is None:
                continue
            if await self._eval_condition(when):
                try:
                    target = int(page)
                except (TypeError, ValueError):
                    continue
                return max(0, min(target, max_pages - 1))
        return None

    # ──── State Subscriptions ────

    async def _setup_feedback_subscriptions(self):
        """Subscribe to every state key that can change a button or the page.

        Covers feedback keys, toggle keys, visible_when condition keys, and
        auto_page rule keys. Auto_page keys are also tracked separately so that
        only an auto_page-watched change can drive automatic paging.
        """
        buttons = self.api.config.get("buttons", [])
        watch_keys = set()
        self._auto_page_keys = set()

        for btn in buttons:
            if not isinstance(btn, dict):
                continue
            bindings = btn.get("bindings", {})
            if isinstance(bindings, dict):
                # Feedback key
                feedback = bindings.get("feedback", {})
                if isinstance(feedback, dict) and feedback.get("key"):
                    watch_keys.add(feedback["key"])
                # Toggle key (for label/icon updates on toggle state change)
                press = _unwrap_binding(bindings.get("press"))
                if isinstance(press, dict) and press.get("toggle_key"):
                    watch_keys.add(press["toggle_key"])
                # Visibility condition keys
                watch_keys.update(_condition_state_keys(bindings.get("visible_when")))

        # Auto-page rule keys (tracked separately — only these drive paging)
        auto_page = self.api.config.get("auto_page", [])
        if isinstance(auto_page, list):
            for rule in auto_page:
                if isinstance(rule, dict):
                    self._auto_page_keys.update(_condition_state_keys(rule.get("when")))
        watch_keys |= self._auto_page_keys

        # Touchscreen strip keys (tracked separately — a change re-renders the
        # strip): explicit zone label/value sources, plus every dial adjust key
        # since the default zones display those values.
        self._touch_strip_keys = set()
        touchscreen = self.api.config.get("touchscreen", {})
        zones = touchscreen.get("zones") if isinstance(touchscreen, dict) else None
        if isinstance(zones, list):
            for zone in zones:
                if isinstance(zone, dict):
                    for field in ("label_source", "value_source"):
                        if zone.get(field):
                            self._touch_strip_keys.add(zone[field])
        dials = self.api.config.get("dials", [])
        if isinstance(dials, list):
            for dial in dials:
                if isinstance(dial, dict):
                    adjust = dial.get("adjust")
                    if isinstance(adjust, dict) and adjust.get("key"):
                        self._touch_strip_keys.add(adjust["key"])
        watch_keys |= self._touch_strip_keys

        # Info-strip key (tracked separately — a change re-renders the strip)
        self._info_strip_keys = set()
        info_strip = self.api.config.get("info_strip")
        if (
            isinstance(info_strip, dict)
            and info_strip.get("source", "state") == "state"
            and info_strip.get("key")
        ):
            self._info_strip_keys.add(info_strip["key"])
        watch_keys |= self._info_strip_keys

        # Auto-brightness rule keys (tracked separately — a change re-applies
        # the brightness level)
        self._brightness_keys = set()
        auto_brightness = self.api.config.get("auto_brightness", [])
        if isinstance(auto_brightness, list):
            for rule in auto_brightness:
                if isinstance(rule, dict):
                    self._brightness_keys.update(_condition_state_keys(rule.get("when")))
        watch_keys |= self._brightness_keys

        for key in watch_keys:
            sub_id = await self.api.state_subscribe(key, self._on_state_change)
            self._feedback_subs.append(sub_id)

    async def _on_state_change(self, key, value, old_value):
        """React to a watched state change: auto-page switch and/or re-render."""
        if not self.deck or not self.deck.is_visual():
            return

        # Auto-page: re-evaluate only when an auto_page-watched key changes, so
        # an ordinary feedback/visibility change never overrides manual paging.
        if key in self._auto_page_keys:
            target = await self._evaluate_auto_page()
            if target is not None and target != self.current_page:
                await self._change_page(target)
                return  # _change_page re-rendered the whole new page

        # Touchscreen strip shows live values — re-render it on change
        if key in self._touch_strip_keys:
            await self._render_touchscreen()

        # Info strip shows a live value — re-render it on change
        if key in self._info_strip_keys:
            await self._render_info_strip()

        # Auto-brightness rules — re-apply unless the deck is idle-dimmed
        # (waking on input restores the rule level anyway)
        if key in self._brightness_keys and not self._idle_dimmed:
            await self._apply_active_brightness()

        # Re-render buttons on the current page that depend on this key
        buttons = self.api.config.get("buttons", [])
        for btn in buttons:
            if not isinstance(btn, dict):
                continue
            if btn.get("page", 0) != self.current_page:
                continue

            bindings = btn.get("bindings", {})
            matched = False
            if isinstance(bindings, dict):
                feedback = bindings.get("feedback", {})
                if isinstance(feedback, dict) and feedback.get("key") == key:
                    matched = True
                press = _unwrap_binding(bindings.get("press"))
                if isinstance(press, dict) and press.get("toggle_key") == key:
                    matched = True
                if not matched and key in _condition_state_keys(bindings.get("visible_when")):
                    matched = True

            if matched:
                key_index = btn.get("index")
                if key_index is not None:
                    await self._render_button(key_index)

    # ──── Context Actions ────

    async def _on_context_action(self, event_name, payload):
        """Handle context action events."""
        if "action.identify" in event_name:
            await self._identify_deck()

    async def _identify_deck(self):
        """Flash all buttons to identify which physical deck this is."""
        if not self.deck or not self.deck.is_visual():
            return

        self.api.log("Identifying Stream Deck (flashing all buttons)")
        key_count = self.deck.key_count()
        touch_keys = range(key_count, key_count + self.deck.touch_key_count())

        # Flash white
        white_img = self._create_button_image("", "#ffffff", "#ffffff")
        native_white = PILHelper.to_native_key_format(self.deck, white_img)

        for flash in range(3):
            with self.deck:
                for k in range(key_count):
                    self.deck.set_key_image(k, native_white)
            for k in touch_keys:
                self._apply_key_color(k, "#ffffff")
            await asyncio.sleep(0.3)

            with self.deck:
                for k in range(key_count):
                    self.deck.set_key_image(k, b"\x00" * len(native_white))
            for k in touch_keys:
                self._apply_key_color(k, "#000000")
            await asyncio.sleep(0.3)

        # Restore current page
        await self._render_all_buttons()

    # ──── Helpers ────

    def _get_columns(self):
        """Get the number of columns for the connected deck."""
        if self.deck:
            layout = self.deck.key_layout()
            if layout:
                return layout[1]  # (rows, cols)
        return 4  # default for Neo

    def _get_button_assignment(self, page, key_index):
        """Look up the button assignment for a specific page/key index."""
        buttons = self.api.config.get("buttons", [])
        for btn in buttons:
            if not isinstance(btn, dict):
                continue
            if btn.get("index") == key_index and btn.get("page", 0) == page:
                return btn
        return None

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
