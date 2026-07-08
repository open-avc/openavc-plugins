# Present

Wireless presentation for OpenAVC. A presenter shares a laptop screen from the
browser over WebRTC, and it appears on the space's displays. The plugin bundles
[MediaMTX](https://github.com/bluenviron/mediamtx) as a local helper that
ingests each presenter's screen and republishes it for playback. The helper's
signaling listens on localhost only; displays and presenters reach it through
the OpenAVC server.

The OpenAVC instance is the **space** — there is nothing to create beyond
naming it. You add **displays**, and every display shows a connect card
(space name, server address, and a rotating join code) when idle, cuts to a
presenter's screen when the routing sends one there, then returns to the
card. Displays come in two kinds:

- A **browser display** is a full-screen web page. Open its link in a
  browser on whatever drives that screen: a mini PC, a stick PC, a smart TV
  with a real browser, or a spare tablet. Sub-second latency and perfectly
  seamless switching.
- A **stream display** is for hardware that pulls a stream URL: it publishes
  a continuous RTSP and SRT stream that an IP video decoder (or anything
  else that plays a stream URL, like VLC) locks onto and turns into HDMI.
  Use it to land Present on gear that has no browser — at the cost of about
  a second of latency and a brief blip at each source switch.

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

- Each display needs either a browser (browser display — any modern one:
  Chrome, Edge, Firefox, Safari) or a generic stream decoder that accepts an
  RTSP or SRT URL (stream display).
- **HTTPS on the OpenAVC instance for presenters.** Browsers only allow
  screen capture on a secure page, so the share page needs the instance's
  HTTPS support enabled (Settings > Security in the Programmer). With the
  auto-generated certificate, guests click through a one-time browser
  warning; with a CA-issued certificate they don't. Without HTTPS the share
  page still loads but tells the guest sharing is unavailable. Displays are
  not affected.
- **Network:** WebRTC media travels over **UDP port 8190** directly between
  browsers and the server. Stream displays are pulled from the server over
  **TCP port 8554** (RTSP) or **UDP port 8899** (SRT). On a normal LAN this
  works as-is; if the server has a firewall, allow inbound traffic on the
  ports the space actually uses.
- Presenters need a **desktop** browser. No mobile browser can capture its
  screen — that is an iOS/Android platform restriction, not a browser setting.
- Stream displays render their connect card server-side, which needs a
  standard system font: any Windows install has one, as do desktop Linux
  distributions and the OpenAVC Pi image (DejaVu). On a minimal/container
  Linux install, add the DejaVu fonts package if the card reports a font
  error in the log.

The MediaMTX and FFmpeg helpers are downloaded automatically when the plugin
is installed. No manual setup is required on Windows or Linux.

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
| Kind | **Browser** (opens the Display page) or **Stream** (pulled by a decoder) |
| Display link | Browser displays: the URL to open on the device driving that screen |
| Stream URLs | Stream displays: the RTSP and SRT addresses a decoder pulls |
| Source | What it shows: `Auto` (follow the active presenter) or a pinned presenter |

Routing treats both kinds identically — a stream display pins, follows
`Auto`, and fires the same state keys and events as a browser display.

## Setting up a browser display

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

## Setting up a stream display (hardware decoders)

A stream display publishes a **continuous, never-changing stream** — H.264
(Constrained Baseline) 1080p30 with AAC stereo audio — that starts within
seconds of the display being added and keeps running whether or not anyone
is presenting. When idle it carries the connect card as video; when a source
is routed it cuts to that presenter inside the same stream, so a locked
decoder never loses sync. Point any decoder that accepts a URL at one of the
two addresses shown on the display's row in the plugin page:

- **RTSP:** `rtsp://<server>:8554/out/<display>-<key>` (TCP interleaved)
- **SRT:** `srt://<server>:8899?streamid=read:out/<display>-<key>`

Prefer SRT when the decoder supports a custom `streamid`; RTSP is the
universal fallback. VLC and ffplay open both, which makes a quick check easy
from any PC.

What to expect, honestly:

- **Latency is about one second** — the presenter's WebRTC feed is decoded,
  normalized, and re-encoded to keep the output stable for the decoder. A
  browser display is sub-second; put one wherever latency matters most.
- **Source switches show a brief blip** — up to half a second of held frame
  and a short audio hiccup as the decoder joins the new content. The stream
  itself never restarts, so the decoder stays locked (no black screen, no
  re-buffer on decent hardware).
- **Each stream display runs its own encoder** on the OpenAVC server (a
  hardware encoder is used automatically when the machine has one). A NUC or
  mini PC handles several; on a Raspberry Pi, count on one or two and watch
  CPU.

The URL is the credential: the long `<key>` segment is that display's stream
key, and only `out/…` streams can be pulled from the network — nothing else
on the media helper is readable without it. If a URL leaks or a decoder is
lost, **Regenerate stream key** on the display's row: the old URL goes dead
immediately and the row shows the new one. (Changing the display's ID does
the same, since the ID is part of the URL.)

A stream display also keeps a regular display link, which is handy for
checking what it is putting out from any browser.

## What to run a display on

For browser displays, any device with a modern browser works. In practice:

- **A mini PC, stick PC, or Raspberry Pi** running Chromium in kiosk mode is
  the simplest, most reliable choice, and what a permanent install should
  use.
- **Smart TVs and Android-based streaming sticks** (Android TV, Google TV,
  Fire TV) can work when a real browser is installed on them. Browser
  quality varies by model — test the exact device before committing a space
  to it.
- **Roku does not work.** It has no browser and no WebRTC support, so
  nothing on it can open the Display page.

For stream displays, use a **generic IP video decoder** — a box that accepts
an RTSP or SRT URL and outputs HDMI. They exist at every price point, from
budget H.264/H.265 decoder boxes to broadcast-grade converters, and the
budget tier is fine here (the stream is plain H.264 at a fixed profile
chosen for maximum compatibility). Two cautions:

- **Closed AV-over-IP receivers do not work.** An RX that only decodes its
  paired transmitter (selected by encoder ID) has no URL input to point at
  Present. To land Present on such a system, feed one of its
  encoders/transmitters instead: a decoder's HDMI output, or a small PC
  running the Display page full screen, into a TX — then every RX can show
  it like any other source.
- **Decoder reconnect behavior varies.** Present holds the stream
  parameters constant precisely so decoders stay locked across switches,
  but if a specific box still drops out, test the same URL in VLC to tell a
  network problem from a decoder quirk.

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
- **A decoder shows nothing on a stream display's URL:** Try the same URL in
  VLC from a PC on the decoder's network. If VLC fails too, check that TCP
  8554 (RTSP) or UDP 8899 (SRT) is open to the server and that the URL
  matches the plugin page exactly (the stream key changes when regenerated,
  and the display's encoder takes a few seconds to publish after the plugin
  starts). If VLC plays, the decoder needs its stream settings checked
  (RTSP over TCP; SRT caller mode with the full `streamid`).
- **A stream display shows Error in the plugin page:** Its output encoder
  could not run — see the System Log. On minimal Linux/container installs
  the usual cause is a missing system font for the connect card (install
  the DejaVu fonts package).
- **Sound is missing:** Look for the **Tap for sound** button (autoplay
  policy), and check that the presenter's browser is actually capturing audio
  — screen-share audio support varies by browser and OS.

## Bundled components

This plugin downloads and runs two third-party programs. They are fetched at
install time and are not redistributed in this repository.

| Component | License | Purpose |
|-----------|---------|---------|
| MediaMTX | MIT | WebRTC ingest and playback media server; RTSP/SRT output for stream displays |
| FFmpeg (LGPL build) | LGPL-2.1 | Stream-display output encoding (connect card + presenter transcode) |

## License

MIT
