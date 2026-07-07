# Present

Wireless presentation for OpenAVC. A presenter shares a laptop screen from the
browser over WebRTC, and it appears on the room display. The plugin bundles
[MediaMTX](https://github.com/bluenviron/mediamtx) as a local helper that
ingests each presenter's screen and republishes it for playback. The helper's
signaling listens on localhost only; displays and presenters reach it through
the OpenAVC server.

Each room gets a **Display page**: a full-screen web page that shows a connect
card (room name, server address, and a rotating room code) when idle, and cuts
to the presenter's screen when someone shares — then back to the card when they
stop. Open it in a browser on whatever drives the room display: a mini PC, a
stick PC, a smart TV with a real browser, or a spare tablet.

> **Early release.** This version ships the room model, the Display page, and
> the control-system state and events. The guest **share page** — where a
> visitor types the room code and picks a screen to share, with no software
> installed — is under active development. Until it ships, publishing to a room
> requires a WebRTC (WHIP) publisher running on the OpenAVC server host, which
> makes this release most useful for evaluating the display side and wiring up
> room automation.

## Requirements

- A browser at the display (any modern one: Chrome, Edge, Firefox, Safari).
  Devices that can only play a stream URL and devices without a browser are
  not supported by the Display page.
- **Network:** WebRTC media travels over **UDP port 8190** directly between
  browsers and the server. On a normal LAN this works as-is; if the server has
  a firewall, allow inbound UDP 8190.
- Presenters need a **desktop** browser. No mobile browser can capture its
  screen — that is an iOS/Android platform restriction, not a browser setting.

The MediaMTX helper is downloaded automatically when the plugin is installed.
No manual setup is required on Windows or Linux.

## Rooms

A room is one presentation space with its own display, join code, and state.
Rooms are stored with the project. Each room has:

| Field | Description |
|-------|-------------|
| Name | Friendly name shown on the display's connect card |
| Room ID | Short unique identifier (lowercase, no spaces) |
| Display link | The URL to open on the room display (see below) |

## Setting up a room display

1. Create a room in the **Present** section of the Programmer.
2. Copy the room's **display link**. It includes a long display key that
   authorizes that display — treat it like a password for the room's video.
3. On the device driving the display, open the link in a browser, full screen
   (double-click the page toggles fullscreen; kiosk mode is better for
   permanent installs).

The display needs no OpenAVC login and survives server restarts — it keeps
retrying until the server is back. If the link ever needs to be revoked (a
device is lost, a link was shared too widely), use **Regenerate key** on the
room; every old link stops working immediately.

Audio plays when the browser allows it. Browsers block sound before the page
has been interacted with, so a plain (non-kiosk) browser may show a **Tap for
sound** button on the first share. Kiosk launchers can disable that policy
(for Chromium: `--autoplay-policy=no-user-gesture-required`).

## Room automation

The point of wiring presentation into a control system: the room can react
when someone shares. A state-change trigger on
`plugin.present.<room>.active_presenters` rising above 0 can run a
"Presentation On" macro — power the display, switch inputs, set volume — and
another on it returning to 0 can run "Presentation Off."

### State keys

| Key | Type | Description |
|-----|------|-------------|
| `plugin.present.running` | boolean | Helper is up and responding |
| `plugin.present.sidecar` | string | Helper process state: `starting`, `running`, `restarting`, `failed` |
| `plugin.present.error` | string | Last fatal error message (empty when healthy) |
| `plugin.present.rooms` | string | JSON list of `{id, label}` for configured rooms |
| `plugin.present.<room>.code` | string | The join code currently shown on the room's connect card |
| `plugin.present.<room>.output_state` | string | `idle` or `live` |
| `plugin.present.<room>.active_presenters` | integer | How many presenters are currently sharing |
| `plugin.present.<room>.presenters` | string | JSON list of `{name, since}` for current presenters |

### Events

| Event | Payload | Description |
|-------|---------|-------------|
| `plugin.present.presenter_joined` | `{room, name}` | Someone started sharing |
| `plugin.present.presenter_left` | `{room, name}` | Someone stopped sharing |
| `plugin.present.error` | `{reason}` | The helper failed repeatedly and stopped restarting |

Event-triggered macros can read the payload with `$trigger.room` and
`$trigger.name`.

## The room code

The connect card shows a short room code that rotates when a presentation
ends and periodically while the room is idle, so a code seen during one
meeting can't be reused for the next. In this release the code is shown on
the display; the guest share flow that asks for it is part of the upcoming
share page.

## Troubleshooting

- **Plugin shows Error on start:** The MediaMTX helper could not start. Check
  the System Log. If the binary is missing, reinstall the plugin so its
  components download again.
- **Display shows "This display link isn't valid":** The key in the URL is
  wrong or was regenerated. Copy the room's display link again from the
  Programmer.
- **Display shows "Reconnecting to OpenAVC…":** The display can't reach the
  OpenAVC server — server down or network issue. It recovers on its own.
- **Video connects but never appears on another device:** Make sure UDP port
  8190 is open between the display's network and the server.
- **Sound is missing:** Look for the **Tap for sound** button (autoplay
  policy), and check that the presenter's browser is actually capturing audio
  — screen-share audio support varies by browser and OS.

## Bundled components

This plugin downloads and runs one third-party program. It is fetched at
install time and is not redistributed in this repository.

| Component | License | Purpose |
|-----------|---------|---------|
| MediaMTX | MIT | WebRTC ingest and playback media server |

## License

MIT
