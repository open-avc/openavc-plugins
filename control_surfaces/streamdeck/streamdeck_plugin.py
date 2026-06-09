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


# ──── Model Definitions ────

DECK_MODELS = {
    "Stream Deck Neo": {
        "layout_type": "grid",
        "rows": 2,
        "columns": 4,
        "key_size_px": 72,
        "key_spacing_px": 4,
    },
    "Stream Deck Mini": {
        "layout_type": "grid",
        "rows": 2,
        "columns": 3,
        "key_size_px": 80,
        "key_spacing_px": 4,
    },
    "Stream Deck Mini MK.2": {
        "layout_type": "grid",
        "rows": 2,
        "columns": 3,
        "key_size_px": 80,
        "key_spacing_px": 4,
    },
    "Stream Deck Original": {
        "layout_type": "grid",
        "rows": 3,
        "columns": 5,
        "key_size_px": 72,
        "key_spacing_px": 4,
    },
    "Stream Deck Original V2": {
        "layout_type": "grid",
        "rows": 3,
        "columns": 5,
        "key_size_px": 72,
        "key_spacing_px": 4,
    },
    "Stream Deck MK.2": {
        "layout_type": "grid",
        "rows": 3,
        "columns": 5,
        "key_size_px": 72,
        "key_spacing_px": 4,
    },
    "Stream Deck XL": {
        "layout_type": "grid",
        "rows": 4,
        "columns": 8,
        "key_size_px": 96,
        "key_spacing_px": 4,
    },
    "Stream Deck XL V2": {
        "layout_type": "grid",
        "rows": 4,
        "columns": 8,
        "key_size_px": 96,
        "key_spacing_px": 4,
    },
    "Stream Deck Pedal": {
        "layout_type": "grid",
        "rows": 1,
        "columns": 3,
        "key_size_px": 0,
        "key_spacing_px": 0,
    },
}


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
        "version": "1.5.0",
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
        "The default SURFACE_LAYOUT is for the Neo (2x4). The actual layout is "
        "detected from hardware at runtime. Button indices go left-to-right, "
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
        "order and the first match wins, so put more specific conditions first."
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
        self._loop = None
        self._model_info = None
        self._opening = False    # re-entrancy guard while a deck is being opened
        self._hold_tasks = {}    # key_index -> periodic task ID for hold-repeat
        self._press_times = {}   # key_index -> timestamp for tap/hold mode
        self._icon_font = None   # Loaded Lucide TTF font for icon rendering
        self._icon_map = {}      # icon-name -> unicode code point
        self._icon_cache = {}    # (icon_name, size, color_hex) -> PIL Image

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

        # Set initial state
        await self.api.state_set("connected", False)
        await self.api.state_set("model", "")
        await self.api.state_set("serial", "")
        await self.api.state_set("current_page", 0)
        await self.api.state_set("key_count", 0)

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

        self.deck = deck
        self._model_info = model

        # Apply brightness from config
        brightness = self.api.config.get("brightness", 70)
        deck.set_brightness(brightness)

        # Set up key callback (async variant — fires on our event loop)
        deck.set_key_callback_async(self._on_key_change, loop=self._loop)

        # Update state
        await self.api.state_set("connected", True)
        await self.api.state_set("model", model)
        await self.api.state_set("serial", serial)
        await self.api.state_set("key_count", key_count)
        await self.api.state_set("current_page", 0)
        self.current_page = 0

        self.api.log(f"Connected to {model} (S/N: {serial}, {key_count} keys)")
        await self.api.event_emit("connected", {"model": model, "serial": serial})

        # Subscribe to state changes for feedback, visibility, and auto-page keys
        await self._setup_feedback_subscriptions()

        # Apply the initial auto-page selection before the first render so we
        # don't briefly show page 0 and then immediately switch.
        initial_page = await self._evaluate_auto_page()
        if initial_page is not None:
            self.current_page = initial_page
            await self.api.state_set("current_page", initial_page)

        # Render all buttons for the current page
        await self._render_all_buttons()

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
        await self.api.state_set("connected", False)
        await self.api.event_emit("disconnected", {})

    # ──── Key Handling ────

    async def _on_key_change(self, deck, key_index, pressed):
        """Handle a physical button press/release with mode support."""
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
            return

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
                    lambda: self._execute_action(press, key_index),
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
                await self._execute_action(off_action, key_index)
            else:
                await self._execute_action(press, key_index)

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
                    await self._execute_action(hold_action, key_index)
                else:
                    await self._execute_action(press, key_index)
            return

        # Default: tap mode — fire every configured action in order, on press
        if pressed:
            await self._execute_actions(press_actions, key_index)

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

    async def _execute_actions(self, actions, key_index):
        """Execute a list of action bindings sequentially, in order."""
        for action in actions:
            if isinstance(action, dict):
                await self._execute_action(action, key_index)

    async def _execute_action(self, action_binding, key_index):
        """Execute a single surface action binding.

        Supports the documented surface action set: ``macro``,
        ``device.command``, ``state.set`` (scoped like the panel plugin
        bridge), and ``navigate`` (deck page: next/previous or a page index).
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
                    self.api.log(f"Executed macro '{macro}' from key {key_index}", level="debug")
                except Exception as e:
                    self.api.log(f"Error executing macro '{macro}': {e}", level="error")

        elif action == "device.command":
            device = action_binding.get("device", "")
            command = action_binding.get("command", "")
            params = action_binding.get("params")
            if device and command:
                try:
                    await self.api.device_command(device, command, params if isinstance(params, dict) else None)
                    self.api.log(f"Sent {command} to {device} from key {key_index}", level="debug")
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
        """Render all buttons on the deck for the current page."""
        if not self.deck or not self.deck.is_visual():
            return

        key_count = self.deck.key_count()
        for key_index in range(key_count):
            await self._render_button(key_index)

    async def _render_button(self, key_index):
        """Render a single button image based on its assignment and state."""
        if not self.deck or not self.deck.is_visual():
            return

        assignment = self._get_button_assignment(self.current_page, key_index)

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

        # Generate the button image and set it on the deck
        image = self._create_button_image(label, bg_color, text_color, icon)
        self._apply_key_image(key_index, image)

    def _apply_key_image(self, key_index, image):
        """Encode a PIL image and set it on a deck key (thread-safe)."""
        try:
            native_image = PILHelper.to_native_key_format(self.deck, image)
            with self.deck:
                self.deck.set_key_image(key_index, native_image)
        except Exception as e:
            self.api.log(f"Error setting key {key_index} image: {e}", level="debug")

    def _create_button_image(self, label, bg_color, text_color, icon_name=None):
        """Create a PIL image for a button with optional icon and label."""
        image_format = self.deck.key_image_format()
        width = image_format["size"][0]
        height = image_format["size"][1]

        img = Image.new("RGB", (width, height), bg_color)
        draw = ImageDraw.Draw(img)

        # Load icon image if specified
        icon_img = self._render_icon(icon_name, text_color, width) if icon_name else None

        if icon_img and label:
            # Icon above label
            icon_size = min(width, height) // 2
            icon_img = icon_img.resize((icon_size, icon_size), Image.LANCZOS)
            icon_x = (width - icon_size) // 2
            icon_y = max(4, (height // 2) - icon_size + 4)
            img.paste(icon_img, (icon_x, icon_y), icon_img)

            # Label below icon
            try:
                font = ImageFont.truetype("arial.ttf", 12)
            except (IOError, OSError):
                font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), label, font=font)
            text_w = bbox[2] - bbox[0]
            text_x = (width - text_w) // 2
            text_y = icon_y + icon_size + 2
            draw.text((text_x, text_y), label, fill=text_color, font=font)

        elif icon_img:
            # Icon only, centered
            icon_size = int(min(width, height) * 0.6)
            icon_img = icon_img.resize((icon_size, icon_size), Image.LANCZOS)
            icon_x = (width - icon_size) // 2
            icon_y = (height - icon_size) // 2
            img.paste(icon_img, (icon_x, icon_y), icon_img)

        elif label:
            # Label only, centered
            try:
                font = ImageFont.truetype("arial.ttf", 14)
            except (IOError, OSError):
                font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), label, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            x = (width - text_w) // 2
            y = (height - text_h) // 2
            draw.text((x, y), label, fill=text_color, font=font)

        return img

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

        # Flash white
        white_img = self._create_button_image("", "#ffffff", "#ffffff")
        native_white = PILHelper.to_native_key_format(self.deck, white_img)

        for flash in range(3):
            with self.deck:
                for k in range(key_count):
                    self.deck.set_key_image(k, native_white)
            await asyncio.sleep(0.3)

            with self.deck:
                for k in range(key_count):
                    self.deck.set_key_image(k, b"\x00" * len(native_white))
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
