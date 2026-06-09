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

## Configuration

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| Button Brightness | Integer | 70 | Screen brightness (0-100) |
| Default Button Color | String | `#1a1a2e` | Background color for unassigned buttons |
| Text Color | String | `#e0e0e0` | Button label text color |

## Configuring Buttons

Use the **Surface Configurator** in the Programmer IDE:

1. Open the **Stream Deck** view in the Plugins sidebar section
2. Click a button on the visual grid
3. In the assignment panel, set:
   - **Label** -- text displayed on the button
   - **Button Mode** -- how the button behaves (see below)
   - **Press Action** -- what happens when pressed: run a macro, send a device command, set a variable, or navigate pages
   - **Visual Feedback** -- pick a state key, set a condition, choose active/inactive colors and labels
   - **Visibility** -- optionally hide the button unless a state condition is met
4. Use **page tabs** to set up multiple pages of buttons

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

### Hiding Buttons

Each button can be hidden based on system state. In the assignment panel, open **Visibility**, turn on "Show only when...", then pick a state key, operator, and value. When the condition is false the button shows as a blank black key and ignores presses; when true it renders and responds normally. Add more than one condition and combine them with AND / OR. Operators: equals, not equals, greater/less than (and or-equal), has a value, is empty or zero.

Example: hide the source-select and volume buttons unless the projector is on (`device.projector_1.power` equals `on`).

### Dials (Stream Deck +)

When a deck with dials is connected, a dial row appears under the button grid in the Surface Configurator. Click a dial to configure it:

- **Label** -- shown on the touchscreen under the dial
- **Turning Adjusts a Value** -- pick a variable; each detent adds or subtracts the step, clamped to min/max. Spin fast and the value moves proportionally faster. Have a macro or trigger watch the variable to drive a device (volume, mic gain, camera pan speed).
- **Clockwise / Counter-Clockwise Turn Actions** -- actions that run on each turn event in that direction
- **Press Actions** -- actions that run when the dial is pushed

Dials keep their assignment on every button page.

### Touchscreen (Stream Deck +)

By default the touch strip shows one zone per dial with the dial's label and the live value of its adjusted variable -- no setup needed. To take over the strip, add custom zones in the **Touchscreen** section below the grid. Each zone can show a label (static or from a state key), a live state value, custom colors, and run actions when tapped. Zones split the strip evenly, or set explicit pixel positions.

### Touch Keys and Info Screen (Neo)

The Neo's two side touch keys appear below the button grid in the Surface Configurator. They work like regular buttons (press actions, modes, feedback, visibility, per-page assignment) but have no display -- each key glows with its background color, and feedback colors override it when active.

The small info screen between the touch keys is configured in the **Info Screen** section: show a live state value (with an optional heading) or static text. State-driven values refresh automatically.

### Automatic Paging

Below the button grid, the **Automatic Paging** section switches the deck to a page automatically when state changes. Add a rule, choose the target page, and set the condition (same operators as visibility, with AND / OR). Rules are checked top to bottom and the first match wins, so list the most specific conditions first. Reorder rules with the up/down arrows.

Manual navigation (a button's Navigate action) still works immediately; the next state change that matches a rule takes over again.

Example: switch to the full controls page when the projector turns on, and back to a simple "power on" page when it turns off.

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

The hardware layout is detected when a deck connects, so the Surface Configurator always shows the deck that's actually plugged in. While no deck is connected it shows the default layout.

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

## Troubleshooting

- **No Stream Deck found:** Make sure the deck is connected via USB. On Linux, check that the udev rule is installed (see Requirements above).
- **Plugin shows Error:** Check the System Log for details. The most common issue is a missing HIDAPI library.
- **Buttons not updating:** Make sure the feedback key you chose actually changes value. Check the State view to verify.
- **Multiple decks:** The plugin currently connects to the first detected deck. Multi-deck support is planned.

## License

MIT
