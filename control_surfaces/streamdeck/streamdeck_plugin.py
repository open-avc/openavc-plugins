"""
Elgato Stream Deck plugin for OpenAVC.

Turns any Elgato Stream Deck into a physical control surface for AV room control.
Button presses execute macros; button images update based on system state.

Supported models: Neo, Mini, MK.2/Original V2, XL, Plus, Pedal.
"""

import asyncio
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


class StreamDeckPlugin:

    PLUGIN_INFO = {
        "id": "streamdeck",
        "name": "Elgato Stream Deck",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": "Use Elgato Stream Deck hardware as a physical control surface.",
        "category": "control_surface",
        "license": "MIT",
        "platforms": ["win_x64", "linux_x64", "linux_arm64"],
        "min_openavc_version": "1.0.0",
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
                        "Linux": "libhidapi-libusb.so.0",
                    },
                },
                "platforms": {
                    "win_x64": {
                        "url": "https://github.com/libusb/hidapi/releases/download/hidapi-0.15.0/hidapi-win.zip",
                        "type": "zip",
                        "extract": "x64/hidapi.dll",
                    },
                    "linux_x64": {
                        "package": "libhidapi-libusb0",
                        "install_cmd": "sudo apt-get install -y libhidapi-libusb0",
                    },
                    "linux_arm64": {
                        "package": "libhidapi-libusb0",
                        "install_cmd": "sudo apt-get install -y libhidapi-libusb0",
                    },
                },
            },
        ],
        "capabilities": [
            "state_read",
            "state_write",
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
        "active_color": {
            "type": "string",
            "label": "Active State Color",
            "description": "Background color when a button's feedback key is active (hex).",
            "default": "#0f3460",
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
        self._loop = None
        self._model_info = None
        self._hold_tasks = {}    # key_index -> periodic task ID for hold-repeat
        self._press_times = {}   # key_index -> timestamp for tap/hold mode

    async def start(self, api):
        """Initialize and connect to the Stream Deck."""
        self.api = api
        self._loop = asyncio.get_event_loop()

        # Lazy-import after DLL paths are set up by plugin loader
        try:
            _lazy_import()
        except ImportError as e:
            self.api.log(
                f"Failed to import Stream Deck library: {e}. "
                f"Make sure the 'streamdeck' and 'pillow' packages are installed.",
                level="error",
            )
            if "hidapi" in str(e).lower() or "hid" in str(e).lower():
                self._log_hidapi_help()
            raise

        # Set initial state
        await self.api.state_set("connected", False)
        await self.api.state_set("model", "")
        await self.api.state_set("serial", "")
        await self.api.state_set("current_page", 0)
        await self.api.state_set("key_count", 0)

        # Try to find and open a deck
        try:
            decks = StreamDeck.DeviceManager().enumerate()
        except Exception as e:
            self.api.log(f"Could not enumerate Stream Deck devices: {e}", level="error")
            if "hidapi" in str(e).lower() or "hid" in str(e).lower():
                self._log_hidapi_help()
            raise

        if not decks:
            self.api.log(
                "No Stream Deck devices found. Connect a device and restart the plugin.",
                level="warning",
            )
            # Start polling for device connection
            self.api.create_periodic_task(
                self._poll_for_device, interval_seconds=5, name="deck_poll"
            )
            return

        await self._open_deck(decks[0])

        # Subscribe to context actions
        await self.api.event_subscribe(
            "plugin.streamdeck.action.*", self._on_context_action
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

        # Subscribe to state changes for feedback keys
        await self._setup_feedback_subscriptions()

        # Render all buttons for the current page
        await self._render_all_buttons()

    async def _poll_for_device(self):
        """Periodically check if a deck has been connected."""
        if self.deck and self.deck.is_open():
            return
        try:
            decks = StreamDeck.DeviceManager().enumerate()
            if decks:
                self.api.log("Stream Deck detected, connecting...")
                await self._open_deck(decks[0])
        except Exception:
            pass

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

        # Get press binding
        bindings = assignment.get("bindings", {})
        press = bindings.get("press") if isinstance(bindings, dict) else None
        feedback = bindings.get("feedback") if isinstance(bindings, dict) else None

        # Backward compat
        if not press and assignment.get("macro_id"):
            press = {"action": "macro", "macro": assignment["macro_id"]}

        if not press or not isinstance(press, dict):
            return

        mode = press.get("mode", "tap")

        if mode == "hold_repeat":
            if pressed:
                await self._execute_action(press, key_index)
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
                press_time = self._press_times.pop(key_index, 0)
                held = asyncio.get_event_loop().time() - press_time
                if held >= threshold and hold_action and isinstance(hold_action, dict):
                    await self._execute_action(hold_action, key_index)
                else:
                    await self._execute_action(press, key_index)
            return

        # Default: tap mode — fire on press only
        if pressed:
            await self._execute_action(press, key_index)

    async def _execute_action(self, action_binding, key_index):
        """Execute a single action binding (macro, device command, navigate)."""
        action = action_binding.get("action", "")

        if action == "navigate":
            page_id = action_binding.get("page", "")
            if page_id == "__next_page__":
                await self._change_page(self.current_page + 1)
            elif page_id == "__prev_page__":
                await self._change_page(self.current_page - 1)

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

        default_bg = self.api.config.get("button_color", "#1a1a2e")
        default_text = self.api.config.get("text_color", "#e0e0e0")
        label = ""
        bg_color = default_bg
        text_color = default_text

        if assignment:
            label = assignment.get("label", "")

            # Toggle mode: on_label/off_label override static label
            bindings = assignment.get("bindings", {})
            press_binding = bindings.get("press") if isinstance(bindings, dict) else None
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

            # Read feedback from bindings (new) or legacy feedback_key
            feedback = bindings.get("feedback") if isinstance(bindings, dict) else None

            # Backward compat
            if not feedback and assignment.get("feedback_key"):
                feedback = {
                    "source": "state", "key": assignment["feedback_key"],
                    "condition": {"equals": True},
                    "style_active": {"bg_color": self.api.config.get("active_color", "#0f3460")},
                    "style_inactive": {},
                }

            if feedback and isinstance(feedback, dict):
                fk = feedback.get("key", "")
                condition = feedback.get("condition", {})
                style_active = feedback.get("style_active", {})
                style_inactive = feedback.get("style_inactive", {})

                if fk:
                    value = await self.api.state_get(fk)
                    expected = condition.get("equals") if isinstance(condition, dict) else None
                    # Loose comparison (same as web UI panel)
                    is_active = (str(value).lower() == str(expected).lower()) if expected is not None else bool(value)

                    if is_active and isinstance(style_active, dict):
                        bg_color = style_active.get("bg_color", bg_color)
                        text_color = style_active.get("text_color", text_color)
                        # Conditional label (overrides static label)
                        active_label = feedback.get("label_active", "")
                        if active_label:
                            label = active_label
                    elif not is_active and isinstance(style_inactive, dict):
                        bg_color = style_inactive.get("bg_color", bg_color) or default_bg
                        text_color = style_inactive.get("text_color", text_color) or default_text
                        inactive_label = feedback.get("label_inactive", "")
                        if inactive_label:
                            label = inactive_label

        # Generate the button image
        image = self._create_button_image(label, bg_color, text_color)

        # Set it on the deck (thread-safe via context manager)
        try:
            native_image = PILHelper.to_native_key_format(self.deck, image)
            with self.deck:
                self.deck.set_key_image(key_index, native_image)
        except Exception as e:
            self.api.log(f"Error setting key {key_index} image: {e}", level="debug")

    def _create_button_image(self, label, bg_color, text_color):
        """Create a PIL image for a button."""
        # Get the image size the deck expects
        image_format = self.deck.key_image_format()
        width = image_format["size"][0]
        height = image_format["size"][1]

        # Create image with background color
        img = Image.new("RGB", (width, height), bg_color)

        if label:
            draw = ImageDraw.Draw(img)
            # Use a simple built-in font, scaled to fit
            try:
                font = ImageFont.truetype("arial.ttf", 14)
            except (IOError, OSError):
                font = ImageFont.load_default()

            # Center the text
            bbox = draw.textbbox((0, 0), label, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            x = (width - text_w) // 2
            y = (height - text_h) // 2
            draw.text((x, y), label, fill=text_color, font=font)

        return img

    # ──── State Feedback ────

    async def _setup_feedback_subscriptions(self):
        """Subscribe to state keys referenced by button feedback bindings."""
        buttons = self.api.config.get("buttons", [])
        feedback_keys = set()

        for btn in buttons:
            if not isinstance(btn, dict):
                continue
            # New format: bindings.feedback.key
            bindings = btn.get("bindings", {})
            if isinstance(bindings, dict):
                feedback = bindings.get("feedback", {})
                if isinstance(feedback, dict) and feedback.get("key"):
                    feedback_keys.add(feedback["key"])
            # Legacy format
            fk = btn.get("feedback_key")
            if fk:
                feedback_keys.add(fk)

        for key in feedback_keys:
            sub_id = await self.api.state_subscribe(key, self._on_feedback_state_change)
            self._feedback_subs.append(sub_id)

    async def _on_feedback_state_change(self, key, value, old_value):
        """Re-render buttons that reference this state key as a feedback key."""
        if not self.deck or not self.deck.is_visual():
            return

        buttons = self.api.config.get("buttons", [])
        for btn in buttons:
            if not isinstance(btn, dict):
                continue
            if btn.get("page", 0) != self.current_page:
                continue

            # Check new bindings format
            bindings = btn.get("bindings", {})
            fk = None
            if isinstance(bindings, dict):
                feedback = bindings.get("feedback", {})
                if isinstance(feedback, dict):
                    fk = feedback.get("key")
            # Legacy
            if not fk:
                fk = btn.get("feedback_key")

            if fk == key:
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

    def _log_hidapi_help(self):
        """Log platform-specific help for installing hidapi."""
        system = platform_mod.system()
        if system == "Windows":
            self.api.log(
                "HIDAPI library not found. It should have been installed automatically. "
                "Check that plugin_repo/.deps/hidapi.dll exists. If not, reinstall "
                "the Stream Deck plugin from the community repository.",
                level="error",
            )
        elif system == "Linux":
            self.api.log(
                "HIDAPI library not found. Install it with: "
                "sudo apt-get install -y libhidapi-libusb0",
                level="error",
            )
            self.api.log(
                "For non-root USB access, add a udev rule: "
                'echo \'SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", MODE="0666"\' | '
                "sudo tee /etc/udev/rules.d/99-streamdeck.rules && "
                "sudo udevadm control --reload-rules && sudo udevadm trigger",
                level="error",
            )
