# Elgato Stream Deck

Use any Elgato Stream Deck as a physical control surface for OpenAVC. Assign macros to buttons, get visual feedback from system state, and navigate between pages of controls.

## Supported Models

| Model | Keys | Layout | Display |
|-------|------|--------|---------|
| Neo | 8 | 4x2 | LCD keys + info strip |
| Mini / Mini MK.2 | 6 | 3x2 | LCD keys |
| Original / MK.2 | 15 | 5x3 | LCD keys |
| XL / XL V2 | 32 | 8x4 | LCD keys |
| Plus | 8 + 4 dials | 4x2 + dials | LCD keys + touchscreen |
| Pedal | 3 | 3x1 | No display (foot switches) |

## Requirements

- Elgato Stream Deck hardware connected via USB
- **Windows:** No additional setup needed. The HIDAPI library is installed automatically.
- **Linux:** Install HIDAPI and add a USB permission rule:
  ```bash
  sudo apt-get install -y libhidapi-libusb0
  echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", MODE="0666"' | sudo tee /etc/udev/rules.d/99-streamdeck.rules
  sudo udevadm control --reload-rules && sudo udevadm trigger
  ```

## The Stream Deck View

Everything lives in one place: the **Stream Deck** view in the Programmer IDE sidebar. It shows a live picture of your deck — what you see on screen is exactly what the hardware is showing, as it renders it. With no deck connected, the view offers to wait for USB hardware or add a virtual deck so you can build now.

- **Click** anything in the picture — a key, a dial, the touch strip, the info screen — to edit it in the inspector panel on the right.
- **Shift+click** a key (or use the **▶** badge / the inspector's **Press** button) to press it for real.
- With nothing selected, the inspector shows **the deck itself**: its name, status, brightness, and an **Identify** button that flashes the hardware.
- Switching page tabs flips the physical deck too, so you always watch your edits land. If a page rule moves the deck while you're editing, a small notice says so.

Configuration changes apply live: saving updates the running decks in place, so they never blank or reconnect while you edit.

For reference, the plugin's config keys (all editable in the view, and writable by automation/AI): `buttons`, `global_buttons`, `auto_page`, `page_names`, `dials`, `touchscreen`, `info_strip`, `brightness` (default 70), `auto_brightness`, `idle_dim`, `button_color` (default `#1a1a2e`), `text_color` (default `#e0e0e0`), `deck_names`, `deck_settings`, `decks`, `virtual_decks`.

## Configuring Buttons

1. Click a key in the deck picture.
2. In the inspector, set:
   - **What it does** -- run a macro, send a device command, set a variable, or switch pages. Add more actions to run several in order.
   - **What it shows** -- label, icon, and colors.
   - **Behavior** -- the button mode (see below) and state-driven Visual Feedback.
   - **More** -- visibility conditions and the Arrange tools (copy, paste, move, swap).
3. Use the **page tabs** above the picture to work across pages; **+** adds one.

### Locked Keys

Turn on **Same on every page** in a key's inspector to lock it: the key keeps that one assignment on every page, and anything page-specific at that position stays hidden until you unlock it. Locked keys carry a small pin badge in the editor. Use them for the controls that must never move — page switchers, mute-all, a help key.

A key whose action navigates to a specific page lights up automatically while that page is showing, so a locked row of page keys reads like tabs on the hardware itself.

## Pages

A new project has one page. Click **+** in the page tab row to add the next one — the first time you do, the plugin places locked **‹ ›** page keys in free bottom-row slots so the new page is immediately reachable from the hardware (move, edit, or delete them like any other locked key). Double-click a tab to name it ("Sources", "Audio"); names show up in the tabs, Navigate targets, and paging rules. The **&#8943;** menu on the active tab renames, duplicates, clears, or (for the last page) deletes it.

There is no page-count setting: pages exist by being used. Placing a button, page name, paging rule, or numeric navigate target on page N creates pages up to N.

### Button Modes

| Mode | Behavior | Use Case |
|------|----------|----------|
| Tap | Fires action on press (default) | Most buttons |
| Toggle | Fires On or Off action based on current state | Power on/off, mute/unmute |
| Hold Repeat | Fires action repeatedly while held (configurable interval) | Volume ramp, camera pan/tilt |
| Tap / Hold | Short press = tap action, long press = long press action | Quick vs advanced |

**Toggle** is state-aware. Pick a state key to watch, set the "on" value, and configure On Action and Off Action separately. The button reads the current state to decide which action to fire. Set **On Label** / **Off Label** to change the physical button text per state (e.g., "ON" when active, "OFF" when inactive).

### Visual Feedback

Separate from button modes, the Visual Feedback section lets you set state-driven colors and conditional labels. Use it for color changes on any button mode. Toggle has its own label fields built in, so you don't need Visual Feedback just for label changes on toggle buttons.

Keys that run a macro also show the macro's progress automatically: an amber border with a spinner glyph while it runs, a brief green flash when it completes, and a red flash if it fails -- no matter where the macro was started from (the deck, a panel, a trigger, or a schedule). Neo touch keys pulse the same colors.

### Hiding Buttons

Each button can be hidden based on system state. In the assignment panel, open **Visibility**, turn on "Show only when...", then pick a state key, operator, and value. When the condition is false the button shows as a blank black key and ignores presses; when true it renders and responds normally. Add more than one condition and combine them with AND / OR. Operators: equals, not equals, greater/less than (and or-equal), has a value, is empty or zero.

Example: hide the source-select and volume buttons unless the projector is on (`device.projector_1.power` equals `on`).

### Dials (Stream Deck +)

When a deck with dials is connected, they appear in the deck picture. Click a dial to configure it (the inspector also has turn/press test buttons):

- **Label** -- shown on the touchscreen under the dial
- **Turning Adjusts a Value** -- pick a variable; each detent adds or subtracts the step, clamped to min/max. Spin fast and the value moves proportionally faster. Have a macro or trigger watch the variable to drive a device (volume, mic gain, camera pan speed).
- **Clockwise / Counter-Clockwise Turn Actions** -- actions that run on each turn event in that direction
- **Press Actions** -- actions that run when the dial is pushed

Dials keep their assignment on every button page.

### Touchscreen (Stream Deck +)

By default the touch strip shows one zone per dial with the dial's label and the live value of its adjusted variable -- no setup needed. To take over the strip, click it in the deck picture and add custom zones in the inspector. Each zone can show a label (static or from a state key), a live state value, custom colors, and run actions when tapped. A zone can also run separate **long-press actions**, and **swiping** across a zone can step a variable up and down like turning a dial (the default per-dial zones do this automatically with the dial's own variable). Zones split the strip evenly, or set explicit pixel positions.

### Touch Keys and Info Screen (Neo)

The Neo's two side touch keys appear in the deck picture beside the info screen. They work like regular buttons (press actions, modes, feedback, visibility, per-page assignment, locking) but have no display -- each key glows with its background color, and feedback colors override it when active.

The small info screen between the touch keys is configured by clicking it in the picture: show a live state value (with an optional heading) or static text. State-driven values refresh automatically.

### Page Automation

Below the deck picture, the **Page automation** section switches the deck to a page automatically when state changes. Add a rule, choose the target page, and set the condition (same operators as visibility, with AND / OR). Rules are checked top to bottom and the first match wins, so list the most specific conditions first. Reorder rules with the up/down arrows.

Manual navigation (a button's Navigate action) still works immediately; the next state change that matches a rule takes over again.

Example: switch to the full controls page when the projector turns on, and back to a simple "power on" page when it turns off.

### Multiple Decks

Connect as many decks as you like -- each runs independently with its own pages, dials, and displays. With two or more known decks, a card strip appears above the page tabs: every connected deck (plus any remembered, disconnected one) is a card showing its name, model, whether it uses the shared layout or its own, and which page it's on right now. Click a card to edit that deck; **Identify** in its inspector flashes the hardware so you can tell twins apart. Name each deck ("Lectern", "Tech Booth") in its inspector -- the name is published to `plugin.streamdeck.<serial>.name`.

Every deck shows the **shared layout** by default, so identical panels on both sides of a space need zero extra setup, and a brand-new deck works the moment it's plugged in. A line by the page tabs always says which layout you're editing. To make one deck different, open its inspector and choose **Give this deck its own layout** (it starts as a copy of the shared one); **Use the shared layout instead** deletes its own layout again, and **Move this layout to another deck...** re-keys it -- including onto a replacement after hardware dies. A disconnected deck with a saved layout stays visible as a dimmed card, so swapping a dead deck never means rebuilding its layout.

### Virtual Decks

No hardware yet, or building a project away from the space? With nothing connected, the Stream Deck view offers **Add virtual Stream Deck** directly; with decks already present, use **+ Virtual deck** at the end of the card strip. Pick a model and a software deck connects a moment later. It behaves exactly like a plugged-in deck: live picture, pages, dials, touchscreen, per-deck state, its own card (marked *virtual*). Click its keys to press them, turn its dials from the inspector, tap its strip -- a whole layout can be exercised end to end without touching hardware. **Remove** in its inspector retires it; because layouts live in the shared configuration, a real deck picks up everything you built on a virtual one the moment it's plugged in.

### Brightness

Each deck's base brightness is a slider in its inspector (select the deck -- click its card, or click empty space so nothing else is selected). Different decks can sit at different levels; the value is stored per deck in `deck_settings`, with the plugin-wide `brightness` as the fallback, so changing it never forks a layout.

The **Brightness automation** section below the picture adds:

- **Dim when idle** -- lower the brightness after a period with no key, dial, or touch input. Any press, turn, or tap wakes the deck and restores the normal level.
- **Brightness rules** -- set a level while a state condition holds (same condition editor as visibility and paging). Rules are checked in order, the first match wins, and with no match each deck returns to its own base level. Example: drop to 20% whenever the projector is running so the deck doesn't glow in a dark room.

## State Keys

| Key | Type | Description |
|-----|------|-------------|
| `plugin.streamdeck.connected` | boolean | Whether a deck is connected |
| `plugin.streamdeck.model` | string | Connected model name |
| `plugin.streamdeck.serial` | string | Device serial number |
| `plugin.streamdeck.key_count` | integer | Number of keys on the connected deck |
| `plugin.streamdeck.rows` | integer | Key rows on the connected deck |
| `plugin.streamdeck.columns` | integer | Key columns on the connected deck |
| `plugin.streamdeck.dial_count` | integer | Number of dials (Stream Deck +) |
| `plugin.streamdeck.touch_key_count` | integer | Number of side touch keys (Neo) |
| `plugin.streamdeck.has_touchscreen` | boolean | Whether the deck has a touchscreen strip (Stream Deck +) |
| `plugin.streamdeck.has_info_screen` | boolean | Whether the deck has a secondary info screen (Neo) |
| `plugin.streamdeck.current_page` | integer | Currently active page number |
| `plugin.streamdeck.deck_count` | integer | Number of connected decks |
| `plugin.streamdeck.deck_serials` | string | Comma-separated serials of connected decks |
| `plugin.streamdeck.<serial>.*` | mixed | Per-deck keys (connected, model, geometry, current_page, virtual, render_version) for every connected deck |

The hardware layout is detected when a deck connects, so the Surface Configurator always shows the deck that's actually plugged in. While no deck (real or virtual) is connected, the view explains how to connect one instead of showing an editor. With several decks attached, the un-prefixed keys above track the first-connected deck; use the per-serial keys to automate against a specific deck.

## Events

| Event | Payload | Description |
|-------|---------|-------------|
| `plugin.streamdeck.connected` | `{model, serial}` | Deck connected |
| `plugin.streamdeck.button.press` | `{key, page}` | Button pressed |
| `plugin.streamdeck.button.release` | `{key, page}` | Button released |
| `plugin.streamdeck.dial.turn` | `{dial, amount}` | Dial turned (amount is signed detents) |
| `plugin.streamdeck.dial.press` | `{dial}` | Dial pushed |
| `plugin.streamdeck.touchscreen.touch` | `{x}` | Touchscreen tapped |

## Context Actions

- **Identify Stream Deck** -- Flashes all buttons white three times so you can identify which physical deck is connected.

## Controlling the Deck from Macros

Macros (and therefore triggers, schedules, and scripts that run macros) can drive the deck with an **Emit Event** step targeting `plugin.streamdeck.action.<name>`:

| Event | Payload | What it does |
|-------|---------|--------------|
| `...action.set_page` | `{"page": 2}` | Switch to a page (0-based) |
| `...action.set_brightness` | `{"level": 30}` | Set brightness; holds until the next brightness rule, idle dim, or wake |
| `...action.flash_key` | `{"index": 3, "times": 2}` | Flash one key white to draw attention |
| `...action.show_message` | `{"text": "Mics are LIVE", "seconds": 10}` | Splash a message across the whole deck (and strips); the first press dismisses it without firing that key |
| `...action.identify_deck` | `{}` | Flash every key |

Every payload accepts an optional `"serial"` to target one specific deck; leave it out to address all of them. Example: a trigger on `device.mic_1.mute` becoming `false` runs a macro whose only step emits `plugin.streamdeck.action.show_message` with `{"text": "Mics are LIVE"}` -- every deck in the space lights up with the warning.

## Troubleshooting

- **No Stream Deck found:** Make sure the deck is connected via USB. On Linux, check that the udev rule is installed (see Requirements above).
- **Plugin shows Error:** Check the System Log for details. The most common issue is a missing HIDAPI library.
- **Buttons not updating:** Make sure the feedback key you chose actually changes value. Check the State view to verify.
- **Multiple decks:** All connected decks are used. Two decks showing the same buttons is the default — they share one layout. To make one different, click its card and choose **Give this deck its own layout** in its inspector.

## License

MIT
