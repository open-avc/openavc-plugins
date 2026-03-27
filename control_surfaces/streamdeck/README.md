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
| Active State Color | String | `#0f3460` | Background color when feedback key is active |
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

## State Keys

| Key | Type | Description |
|-----|------|-------------|
| `plugin.streamdeck.connected` | boolean | Whether a deck is connected |
| `plugin.streamdeck.model` | string | Connected model name |
| `plugin.streamdeck.serial` | string | Device serial number |
| `plugin.streamdeck.key_count` | integer | Number of keys on the connected deck |
| `plugin.streamdeck.current_page` | integer | Currently active page number |

## Events

| Event | Payload | Description |
|-------|---------|-------------|
| `plugin.streamdeck.connected` | `{model, serial}` | Deck connected |
| `plugin.streamdeck.button.press` | `{key, page}` | Button pressed |
| `plugin.streamdeck.button.release` | `{key, page}` | Button released |

## Context Actions

- **Identify Stream Deck** -- Flashes all buttons white three times so you can identify which physical deck is connected.

## Troubleshooting

- **No Stream Deck found:** Make sure the deck is connected via USB. On Linux, check that the udev rule is installed (see Requirements above).
- **Plugin shows Error:** Check the System Log for details. The most common issue is a missing HIDAPI library.
- **Buttons not updating:** Make sure the feedback key you chose actually changes value. Check the State view to verify.
- **Multiple decks:** The plugin currently connects to the first detected deck. Multi-deck support is planned.

## License

MIT
