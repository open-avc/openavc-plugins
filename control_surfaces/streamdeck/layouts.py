"""Where each part of a key, a touch-strip zone or info item, the idle clock
and a show_message overlay is drawn: geometry only, from a size and what the
look shows, with no Pillow. The plugin paints a layout (the icon, then each
text slot, then the meter bar) and owns the pixels.

A key with an icon has room for one line of text under it, and the live value
wins that slot over the label (an icon and two lines of text are mush on a
key). A zone puts its icon at the left and both texts beside it. A meter is a
bar along the bottom that takes its height from the text's room: 10 px on a
key, 14 px more on a zone. A text slot's text is drawn centered in its box,
wrapped and shrunk from ``max_font`` until it fits in ``max_lines`` lines.
"""

from typing import NamedTuple


class TextSlot(NamedTuple):
    """Text to draw in the box (x, y, w, h), fitted within ``max_font`` and
    ``max_lines``."""

    text: str
    x: int
    y: int
    w: int
    h: int
    max_font: int
    max_lines: int

    @property
    def draws(self):
        """False for a slot with no text or an empty box: it draws nothing."""
        return bool(self.text) and self.w > 0 and self.h > 0


class IconSpot(NamedTuple):
    """Where an icon goes: its top-left corner and its side."""

    x: int
    y: int
    size: int


class Layout(NamedTuple):
    """Where a look's parts go: the icon (None without one), the text slots
    in drawing order, and the meter as ``(bar, (fraction, color))`` with the
    bar (left, top, right, bottom), both edges inclusive, or None without a
    meter."""

    icon: IconSpot | None
    texts: tuple
    meter: tuple | None


def key_layout(size, look, has_icon):
    """A key's layout at ``size`` (width, height) for ``look`` (looks.Look):
    its label, value line and whether it has a meter. ``has_icon`` is whether
    its icon rendered, not whether it names one."""
    width, height = size
    usable_h = height - 10 if look.meter else height
    meter = ((3, height - 8, width - 4, height - 3), look.meter) if look.meter else None
    if has_icon:
        icon, texts = _key_icon_and_text(width, usable_h, look.value_text or look.label)
    else:
        icon, texts = None, _key_texts(look, width, usable_h)
    return Layout(icon, texts, meter)


def _key_icon_and_text(width, usable_h, text):
    """An icon over one line of text, or an icon alone, larger and centered."""
    if not text:
        size = int(min(width, usable_h) * 0.6)
        return IconSpot((width - size) // 2, (usable_h - size) // 2, size), ()
    size = min(width, usable_h) // 2
    y = max(4, (usable_h // 2) - size + 4)
    top = y + size + 2
    slot = TextSlot(text, 2, top, width - 4, max(0, usable_h - top - 2), 14, 2)
    return IconSpot((width - size) // 2, y, size), (slot,)


def _key_texts(look, width, usable_h):
    """The name over the live value, or the one text there is, padded and
    filling the key."""
    label, value_text = look.label, look.value_text
    if label and value_text:
        third = usable_h // 3
        return (
            TextSlot(label, 2, 2, width - 4, third, 14, 1),
            TextSlot(value_text, 2, third + 2, width - 4,
                     max(0, usable_h - third - 4), max(16, third), 1),
        )
    if label or value_text:
        pad = max(2, width // 12)
        return (TextSlot(label or value_text, pad, pad, width - 2 * pad,
                         usable_h - 2 * pad, max(14, usable_h // 4), 3),)
    return ()


def zone_layout(span, height, look, has_icon):
    """A strip zone's or info item's layout in the columns ``span`` (x, w) of
    an image ``height`` tall, for ``look`` (looks.Look). ``has_icon`` is
    whether its icon rendered."""
    zx, zw = span
    text_bottom = height - 18 if look.meter else height - 4
    icon = None
    text_x = zx + 4
    if has_icon:
        size = max(16, min(40, zw // 4, text_bottom - 8))
        icon = IconSpot(zx + 8, max(2, (text_bottom - size) // 2), size)
        text_x = zx + 8 + size + 6
    texts = _zone_texts(look, (text_x, zx + zw - 4 - text_x, text_bottom), height)
    bar = (zx + 8, height - 14, zx + zw - 9, height - 7)
    return Layout(icon, texts, (bar, look.meter) if look.meter else None)


def _zone_texts(look, area, height):
    """The name in the top third over a large live value, or the one text
    there is in two lines, in the text ``area`` (x, w, bottom)."""
    x, w, text_bottom = area
    label, value_text = look.label, look.value_text
    if label and value_text:
        return (
            TextSlot(label, x, 4, w, height // 3, 16, 1),
            TextSlot(value_text, x, height // 3 + 2, w,
                     text_bottom - height // 3 - 4, 30, 1),
        )
    if label or value_text:
        return (TextSlot(label or value_text, x, 4, w, text_bottom - 8, 24, 2),)
    return ()


def clock_slots(width, height, time_text, date_text):
    """The idle clock: the time over the date, each one line."""
    split = max(10, int(height * 0.62))
    return (
        TextSlot(time_text, 0, 2, width, split, max(16, int(height * 0.5)), 1),
        TextSlot(date_text, 0, split + 2, width, max(8, height - split - 4),
                 max(10, int(height * 0.2)), 1),
    )


def overlay_keys_slot(text, grid, key_size):
    """A show_message overlay across the key grid (rows, cols) of keys
    ``key_size`` (width, height), drawn on one canvas the size of the grid and
    inset by a quarter key on each side."""
    rows, cols = grid
    key_w, key_h = key_size
    return TextSlot(text, key_w // 4, key_h // 4, cols * key_w - key_w // 2,
                    rows * key_h - key_h // 2, key_h, max(2, rows))


def overlay_panel_slot(text, size, inset, min_font):
    """A show_message overlay on a strip or screen of ``size`` (width,
    height), inset by (x, y) on each side, in up to two lines."""
    width, height = size
    inset_x, inset_y = inset
    return TextSlot(text, inset_x, inset_y, width - 2 * inset_x,
                    height - 2 * inset_y, max(min_font, height // 2), 2)
