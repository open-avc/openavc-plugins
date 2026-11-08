"""
Virtual decks: a software stand-in for a Stream Deck, with the device
interface the plugin's deck sessions drive, so layouts can be built and
tested with no deck attached. It opens, renders and takes input like
attached hardware, except that its "display" is the live mirror the plugin
serves over its HTTP routes, and its input arrives through the plugin's
simulate_input action. The config declares them in ``virtual_decks``
(declared_decks.py reads that list).
"""

NO_DISPLAY = {"size": (0, 0), "format": "", "flip": (False, False), "rotation": 0}

# Geometry/format presets per virtual model. These mirror the bundled
# library's per-device constants (Devices/StreamDeck*.py) so a virtual deck
# renders byte-identically to the real hardware path.
VIRTUAL_MODELS = {
    "Stream Deck Neo": {
        "key_count": 8, "rows": 2, "cols": 4, "dials": 0, "touch_keys": 2,
        "touch": False, "visual": True,
        "key_format": {"size": (96, 96), "format": "JPEG", "flip": (True, True), "rotation": 0},
        "screen_format": {"size": (248, 58), "format": "JPEG", "flip": (True, True), "rotation": 0},
        "touchscreen_format": NO_DISPLAY,
    },
    "Stream Deck Mini": {
        "key_count": 6, "rows": 2, "cols": 3, "dials": 0, "touch_keys": 0,
        "touch": False, "visual": True,
        "key_format": {"size": (80, 80), "format": "BMP", "flip": (False, True), "rotation": 90},
        "screen_format": NO_DISPLAY,
        "touchscreen_format": NO_DISPLAY,
    },
    "Stream Deck MK.2": {
        "key_count": 15, "rows": 3, "cols": 5, "dials": 0, "touch_keys": 0,
        "touch": False, "visual": True,
        "key_format": {"size": (72, 72), "format": "JPEG", "flip": (True, True), "rotation": 0},
        "screen_format": NO_DISPLAY,
        "touchscreen_format": NO_DISPLAY,
    },
    "Stream Deck XL": {
        "key_count": 32, "rows": 4, "cols": 8, "dials": 0, "touch_keys": 0,
        "touch": False, "visual": True,
        "key_format": {"size": (96, 96), "format": "JPEG", "flip": (True, True), "rotation": 0},
        "screen_format": NO_DISPLAY,
        "touchscreen_format": NO_DISPLAY,
    },
    "Stream Deck +": {
        "key_count": 8, "rows": 2, "cols": 4, "dials": 4, "touch_keys": 0,
        "touch": True, "visual": True,
        "key_format": {"size": (120, 120), "format": "JPEG", "flip": (False, False), "rotation": 0},
        "screen_format": NO_DISPLAY,
        "touchscreen_format": {"size": (800, 100), "format": "JPEG", "flip": (False, False), "rotation": 0},
    },
    "Stream Deck Pedal": {
        "key_count": 3, "rows": 1, "cols": 3, "dials": 0, "touch_keys": 0,
        "touch": False, "visual": False,
        "key_format": NO_DISPLAY,
        "screen_format": NO_DISPLAY,
        "touchscreen_format": NO_DISPLAY,
    },
}


class VirtualDeck:
    """Software deck implementing the device interface the sessions consume."""

    def __init__(self, model, serial, preset):
        self._model = model
        self._serial = serial
        self._preset = preset
        self._open = False
        self.brightness = None

    def id(self):
        return f"virtual:{self._serial}"

    def open(self):
        self._open = True

    def close(self):
        self._open = False

    def reset(self):
        pass

    def is_open(self):
        return self._open

    def connected(self):
        return True

    def is_visual(self):
        return self._preset["visual"]

    def is_touch(self):
        return self._preset["touch"]

    def deck_type(self):
        return self._model

    def get_serial_number(self):
        return self._serial

    def key_count(self):
        return self._preset["key_count"]

    def key_layout(self):
        return (self._preset["rows"], self._preset["cols"])

    def dial_count(self):
        return self._preset["dials"]

    def touch_key_count(self):
        return self._preset["touch_keys"]

    def key_image_format(self):
        return dict(self._preset["key_format"])

    def screen_image_format(self):
        return dict(self._preset["screen_format"])

    def touchscreen_image_format(self):
        return dict(self._preset["touchscreen_format"])

    def set_brightness(self, level):
        self.brightness = level

    # Display writes are swallowed — the live mirror taps the pre-encoded
    # images upstream, so nothing needs storing here.
    def set_key_image(self, key, image):
        pass

    def set_key_color(self, key, r, g, b):
        pass

    def set_screen_image(self, image):
        pass

    def set_touchscreen_image(self, image, x_pos=0, y_pos=0, width=0, height=0):
        pass

    # Input callbacks are unused — simulated input dispatches straight to the
    # plugin's session handlers.
    def set_key_callback_async(self, cb, loop=None):
        pass

    def set_dial_callback_async(self, cb, loop=None):
        pass

    def set_touchscreen_callback_async(self, cb, loop=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
