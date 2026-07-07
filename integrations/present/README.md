# Present

Wireless presentation for OpenAVC. A presenter shares a laptop screen from the
browser over WebRTC, and it appears on the space's displays. The plugin bundles
[MediaMTX](https://github.com/bluenviron/mediamtx) as a local helper that
ingests each presenter's screen and republishes it for playback. The helper's
signaling listens on localhost only; displays and presenters reach it through
the OpenAVC server.

The OpenAVC instance is the **space** — there is nothing to create beyond
naming it. You add **displays**: each one is a full-screen web page that shows
a connect card (space name, server address, and a rotating join code) when
idle, and cuts to a presenter's screen when the routing sends one there — then
back to the card. Open a display's link in a browser on whatever drives that
screen: a mini PC, a stick PC, a smart TV with a real browser, or a spare
tablet.

Presenters join with nothing installed: the connect card on every display
shows a short address (for example `192.168.1.20:8080/present`) and a rotating
join code. A guest types the address into a laptop browser, enters the code
and their name, and picks a screen or window to share.

Between presenters and displays sits **routing**, and it works like a matrix
switcher with no frame: every display follows a source. `Auto` (the default)
follows the active presenter, so a one-display space needs no routing setup at
all. Pinning a presenter to a display sends that person's screen there — same
source to many displays, different sources to different displays — from the
plugin page, a macro step, a script, or a state key write.

## Requirements

- A browser at each display (any modern one: Chrome, Edge, Firefox, Safari).
  Devices that can only play a stream URL and devices without a browser are
  not supported by the Display page.
- **HTTPS on the OpenAVC instance for presenters.** Browsers only allow
  screen capture on a secure page, so the share page needs the instance's
  HTTPS support enabled (Settings > Security in the Programmer). With the
  auto-generated certificate, guests click through a one-time browser
  warning; with a CA-issued certificate they don't. Without HTTPS the share
  page still loads but tells the guest sharing is unavailable. Displays are
  not affected.
- **Network:** WebRTC media travels over **UDP port 8190** directly between
  browsers and the server. On a normal LAN this works as-is; if the server has
  a firewall, allow inbound UDP 8190.
- Presenters need a **desktop** browser. No mobile browser can capture its
  screen — that is an iOS/Android platform restriction, not a browser setting.

The MediaMTX helper is downloaded automatically when the plugin is installed.
No manual setup is required on Windows or Linux.

## Sharing your screen (presenters)

1. Open a browser on a laptop and go to the address on the display
   (for example `192.168.1.20:8080/present`).
2. Enter your name and the join code shown on the display.
3. Pick the screen, window, or tab to share in the browser's picker.

That's it — the routing decides which display shows it (with one display and
`Auto` routing, it just appears). Stop from the page's **Stop sharing**
button or the browser's own stop-sharing bar.

**Sound** is best-effort and browser-dependent: Chrome and Edge offer an
"Also share audio" option in the picker (tab audio anywhere; full system
audio on Windows when sharing the entire screen). Firefox and Safari share
video only.

By default the connect card shows the server's detected LAN address. If
guests reach the server at a different address (multiple networks, VLANs, a
DNS name), set **Join Address** in the plugin's configuration.

## Displays

A display is one routable output of the space. Displays are stored with the
project and managed on the plugin's page in the Programmer. Each display has:

| Field | Description |
|-------|-------------|
| Name | Friendly name, e.g. "Main Screen" or "Overflow TV" |
| Display ID | Short unique identifier (lowercase, no spaces) |
| Display link | The URL to open on the device driving that screen (see below) |
| Source | What it shows: `Auto` (follow the active presenter) or a pinned presenter |

## Setting up a display

1. Add a display on the **Present** plugin page in the Programmer.
2. Copy its **display link**. It includes a long display key that authorizes
   that one display — treat it like a password for the space's video.
3. On the device driving the screen, open the link in a browser, full screen
   (double-click the page toggles fullscreen; kiosk mode is better for
   permanent installs).

The display needs no OpenAVC login and survives server restarts — it keeps
retrying until the server is back. If a link ever needs to be revoked (a
device is lost, a link was shared too widely), use **Regenerate key** on that
display; every old copy of its link stops working immediately, and the other
displays are untouched.

Audio plays when the browser allows it. Browsers block sound before the page
has been interacted with, so a plain (non-kiosk) browser may show a **Tap for
sound** button on the first share. Kiosk launchers can disable that policy
(for Chromium: `--autoplay-policy=no-user-gesture-required`).

## What to run a display on

Any device with a modern browser works. In practice:

- **A mini PC, stick PC, or Raspberry Pi** running Chromium in kiosk mode is
  the simplest, most reliable choice, and what a permanent install should
  use.
- **Smart TVs and Android-based streaming sticks** (Android TV, Google TV,
  Fire TV) can work when a real browser is installed on them. Browser
  quality varies by model — test the exact device before committing a space
  to it.
- **Roku does not work.** It has no browser and no WebRTC support, so
  nothing on it can open the Display page.
- **Stream decoders and AV-over-IP receivers do not work as display
  devices.** Boxes that ingest a stream URL (RTSP/SRT decoders) or that
  decode only their paired transmitter have no browser to open the page
  with.

Present can still feed an existing video matrix or AV-over-IP system: run
the Display page full screen on a small PC and connect that PC's HDMI
output to a matrix input or an encoder/transmitter. Every screen on that
system can then show the space's presentation like any other source.

## Routing

Every display follows a source. `auto` means the active presenter — when one
person is sharing, every `auto` display shows them. Pinning a presenter to a
display holds that person's screen there; a pinned presenter who isn't
sharing shows the connect card (it does not fall through to someone else).
Routing resets to `Auto` when the plugin restarts.

Drive it like any matrix switcher:

- **Plugin page:** each display row has a Source dropdown.
- **Macro:** the **Route Display** step (under Plugin Actions) picks a display
  and a source from dropdowns.
- **Script:** `openavc.plugins.present.route("main_screen", "alice")` — use
  `"auto"` to clear a pin.
- **State key:** set `plugin.present.display.<id>.source` to a presenter name
  or `auto` from a `state.set` macro step, a script, or the API. Writing an
  empty value also clears the pin.

## Space automation

The point of wiring presentation into a control system: the space can react
when someone shares. The recipes below are built entirely in the Programmer —
no scripting.

### Presentation on and off

Power the space up when the first screen appears; shut it down when the last
one leaves.

1. Build a **Presentation On** macro with the space's power-up steps — for
   example, Device Command steps that power on the display and switch it to
   the input showing Present, and one that sets the program volume.
2. On the macro's **Triggers** tab, add a **State Change** trigger watching
   `plugin.present.active_presenters` with operator **greater than** and
   value `0`.
3. Build a **Presentation Off** macro that reverses it, with a **State
   Change** trigger on the same key, operator **equals**, value `0`. Give
   this trigger a delay (with re-check) so a presenter's brief disconnect
   doesn't shut the space down.

The Off trigger fires exactly once, when the last presenter stops. The On
trigger also re-fires when a second presenter joins while one is already
sharing — the count changed and still matches. Normal power-up steps are
harmless to run again; if the macro must run only for the very first
presenter, wrap its steps in a **Conditional** on key `trigger.old_value`,
operator **equals**, value `0` (a state-change trigger hands the macro the
key's previous value), or set a cooldown on the trigger.

### A panel button that routes the overflow display

Pin one presenter to a secondary display while the main screen keeps
following whoever presents — a matrix take with no matrix frame.

1. Create a macro with a single **Route Display** step (in the Add Step
   menu under **Plugin Actions**): Display = the overflow display,
   Source = the presenter to pin. The Source dropdown lists the people
   sharing right now, so pick from it while that person is live. To author
   it ahead of time instead, use a **Set Variable** step targeting the
   state key `plugin.present.display.<id>.source` with the presenter's
   routable name (the `name` field in `plugin.present.presenters` — for
   example `alice` for a guest who typed "Alice").
2. In the UI Builder, add a **Button** to the panel page whose action runs
   the macro.
3. Add a second button whose Route Display step sets the same display back
   to **Auto** — the "un-pin" take.

### State keys

| Key | Type | Description |
|-----|------|-------------|
| `plugin.present.running` | boolean | Helper is up and responding |
| `plugin.present.sidecar` | string | Helper process state: `starting`, `running`, `restarting`, `failed` |
| `plugin.present.error` | string | Last fatal error message (empty when healthy) |
| `plugin.present.code` | string | The join code currently shown on every connect card |
| `plugin.present.active_presenters` | integer | How many presenters are currently sharing |
| `plugin.present.presenters` | string | JSON list of `{name, label, since}` for current presenters (`name` is the routable ingest name, `label` the name the guest typed) |
| `plugin.present.displays` | string | JSON list of `{id, value, label}` for configured displays (feeds the Route Display dropdown) |
| `plugin.present.sources` | string | JSON list of `{value, label}` routable sources: `auto` plus live presenters (feeds the Route Display dropdown) |
| `plugin.present.display.<id>.source` | string | The routing assignment: `auto` or a presenter name. **Writable** — set it to route the display |
| `plugin.present.display.<id>.showing` | string | Who the display is actually showing (empty = the connect card). Read-only; trigger on it |
| `plugin.present.display.<id>.output_state` | string | `idle` or `live` |

### Events

| Event | Payload | Description |
|-------|---------|-------------|
| `plugin.present.presenter_joined` | `{name, label}` | Someone started sharing |
| `plugin.present.presenter_left` | `{name, label}` | Someone stopped sharing |
| `plugin.present.route_changed` | `{display, source}` | A display's routing assignment changed |
| `plugin.present.error` | `{reason}` | The helper failed repeatedly and stopped restarting |

Event-triggered macros can read the payload with `$trigger.name`,
`$trigger.display`, and `$trigger.source`.

## The join code

Every connect card shows the space's join code, and the share page requires
it before anything else happens. It rotates when a presentation ends and
periodically while the space is idle, so a code seen during one meeting can't
be reused for the next. Entering the code mints a short-lived session for
that presenter only; wrong guesses are rate-limited by the server.

## Troubleshooting

- **Plugin shows Error on start:** The MediaMTX helper could not start. Check
  the System Log. If the binary is missing, reinstall the plugin so its
  components download again.
- **Display shows "This display link isn't valid":** The key in the URL is
  wrong or was regenerated. Copy the display's link again from the plugin
  page.
- **Display shows "Reconnecting to OpenAVC…":** The display can't reach the
  OpenAVC server — server down or network issue. It recovers on its own.
- **The share page says screen sharing is unavailable:** The instance doesn't
  have HTTPS enabled (or the guest opened a plain `http://` address directly).
  Enable HTTPS under Settings > Security; the card's address then upgrades
  automatically.
- **"Someone is already presenting as …":** Two guests picked the same name.
  The second one just needs a different name.
- **Video connects but never appears on another device:** Make sure UDP port
  8190 is open between the display's network and the server.
- **A display stays on the connect card while someone is sharing:** Check its
  Source — a pinned presenter who isn't sharing shows the card. Set it back
  to `Auto` to follow whoever presents.
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
