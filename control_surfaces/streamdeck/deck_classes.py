"""
The bundled library's device classes for network decks: which class drives
a deck with a given product id, and the Stream Deck Studio's own class (the
bundled library predates the Studio).

A network deck's transport (net_deck.py) presents the same device interface
as the library's HID transport, so these are the library's ordinary device
classes, built on that transport. Nothing here imports the library until a
class is asked for: the plugin loads it after the .deps/ search path is set.
"""

STUDIO_PID = 0x00AA


def _signed_step(value):
    """A dial turn byte as a signed step count."""
    return value if value < 0x80 else -(0x100 - value)


class _StudioProtocol:
    """The Stream Deck Studio (32 keys, 2 dials) on the library's gen2 image
    protocol. The bundled library predates the Studio; ``studio_deck_class``
    puts this in front of its V2 device class so the rack unit works over
    its built-in Ethernet."""

    KEY_COUNT = 32
    KEY_COLS = 16
    KEY_ROWS = 2
    DIAL_COUNT = 2
    TOUCH_KEY_COUNT = 0
    KEY_PIXEL_WIDTH = 80
    KEY_PIXEL_HEIGHT = 120
    KEY_IMAGE_FORMAT = "JPEG"
    KEY_FLIP = (False, False)
    KEY_ROTATION = 0
    DECK_TYPE = "Stream Deck Studio"
    DECK_VISUAL = True

    def _read_control_states(self):
        data = self.device.read(43)
        if data is None:
            return None
        states = data[1:]
        if states[0] == 0x00:  # key event
            return {
                self._CONTROL_TYPE.KEY: [bool(s) for s in states[3:3 + self.KEY_COUNT]]
            }
        if states[0] == 0x03:  # dial event
            return self._dial_states(states)
        return None

    def _dial_states(self, states):
        event_type = self._DIAL_EVENTS.get(states[3])
        if event_type is None:
            return None
        transform = self._DIAL_EVENT_TRANSFORM[event_type]
        return {
            self._CONTROL_TYPE.DIAL: {
                event_type: [transform(s) for s in states[4:4 + self.DIAL_COUNT]],
            }
        }

    def get_serial_number(self):
        serial = self.device.read_feature(0x06, 32)
        return self._extract_string(serial[5:])

    def get_firmware_version(self):
        version = self.device.read_feature(0x05, 32)
        return self._extract_string(version[5:])


_studio_class = None


def studio_deck_class():
    """The Stream Deck Studio's device class, built once the library is loaded."""
    global _studio_class
    if _studio_class is None:
        from StreamDeck.Devices.StreamDeck import ControlType, DialEventType
        from StreamDeck.Devices.StreamDeckOriginalV2 import StreamDeckOriginalV2

        class StreamDeckStudio(_StudioProtocol, StreamDeckOriginalV2):
            _CONTROL_TYPE = ControlType
            _DIAL_EVENTS = {0x01: DialEventType.TURN, 0x00: DialEventType.PUSH}
            _DIAL_EVENT_TRANSFORM = {
                DialEventType.TURN: _signed_step,
                DialEventType.PUSH: bool,
            }

        _studio_class = StreamDeckStudio
    return _studio_class


def net_deck_class(pid, library):
    """The library device class that drives a network deck with this product
    id. ``library`` is the bundled library's DeviceManager module, or None
    when it is not loaded (then there is no class)."""
    if library is None:
        return None
    if pid == STUDIO_PID:
        return studio_deck_class()
    return _library_classes(library).get(pid)


def _library_classes(library):
    ids = library.USBProductIDs
    return {
        ids.USB_PID_STREAMDECK_ORIGINAL: library.StreamDeckOriginal,
        ids.USB_PID_STREAMDECK_ORIGINAL_V2: library.StreamDeckOriginalV2,
        ids.USB_PID_STREAMDECK_MK2_SCISSOR: library.StreamDeckOriginalV2,
        ids.USB_PID_STREAMDECK_MK2_MODULE: library.StreamDeckOriginalV2,
        ids.USB_PID_STREAMDECK_MINI: library.StreamDeckMini,
        ids.USB_PID_STREAMDECK_NEO: library.StreamDeckNeo,
        ids.USB_PID_STREAMDECK_XL: library.StreamDeckXL,
        ids.USB_PID_STREAMDECK_MK2: library.StreamDeckOriginalV2,
        ids.USB_PID_STREAMDECK_MK2_V2: library.StreamDeckOriginalV2,
        ids.USB_PID_STREAMDECK_PEDAL: library.StreamDeckPedal,
        ids.USB_PID_STREAMDECK_MINI_MK2: library.StreamDeckMini,
        ids.USB_PID_STREAMDECK_MINI_MK2_MODULE: library.StreamDeckMini,
        ids.USB_PID_STREAMDECK_XL_V2: library.StreamDeckXL,
        ids.USB_PID_STREAMDECK_XL_V2_MODULE: library.StreamDeckXL,
        ids.USB_PID_STREAMDECK_PLUS: library.StreamDeckPlus,
    }
