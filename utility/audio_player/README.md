# Audio Player Plugin

Play short sound effects through OpenAVC panels — chimes for meeting starts, bells for class changes, alerts, button-press feedback, paging notifications.

## How It Works

Audio plays in the panel browser, not on the OpenAVC server. Drop the **Audio Player** UI element on any page that should produce sound. When a macro fires `audio_player.play`, every panel running an Audio Player element receives a state update and plays the sound through the device's speakers.

**Targeting follows element placement.** Lobby chime only? Place the element on the lobby page. Building-wide announcement? Put it on a base page included on every panel.

The element can be visually hidden — its only purpose is audio output.

## Requirements

- Browser audio unlock: modern browsers block audio playback until the user has interacted with the page. The Audio Player element shows a one-time "Tap to enable sound" overlay on first load. Once dismissed, audio playback is silent.
- Speakers on the panel device. (Most tablets, mini PCs, and TV displays have audio out.)

## Configuration

The plugin itself has no configuration. The Audio Player UI element configures per-element behavior:

| Setting | Default | Notes |
|---------|---------|-------|
| **Visible** | Off | Hidden elements are 0×0 — no visual footprint. |
| **Volume** | 1.0 | Per-element multiplier applied on top of master volume. |
| **Allowed Sounds** | All | Restrict which sounds this element will play (useful for "lobby panel only plays announcements"). |

## Macro Actions

| Action | Parameters | Effect |
|--------|------------|--------|
| `Play Sound` | `sound`, `volume` | Play a sound on every panel with the element. |
| `Stop All Sounds` | — | Stop currently-playing sounds on every panel. |
| `Set Master Volume` | `volume` | Set the global volume multiplier. |
| `Mute` | — | Silence everything (overrides volume). |
| `Unmute` | — | Resume playback after Mute. |

`sound` is picked from a dropdown of built-in sounds plus any audio assets uploaded to the project. `volume` is `0.0`–`1.0`. Both fields support dynamic `$var.foo` values.

## State Keys

| Key | Type | Purpose |
|-----|------|---------|
| `plugin.audio_player.play_request` | string (JSON) | Latest play request — the panel element watches this. |
| `plugin.audio_player.last_played` | string | ID of last sound played. |
| `plugin.audio_player.last_played_at` | string (ISO 8601) | When the last sound was played. |
| `plugin.audio_player.master_volume` | float (0.0–1.0) | Global volume multiplier. |
| `plugin.audio_player.muted` | bool | When true, no sounds play regardless of master/element volume. |
| `plugin.audio_player.sounds` | string (JSON) | Available sounds list, used by the macro builder dropdown. |

## Sound Library

The plugin ships with a default library (chimes, bells, alerts, notifications, feedback tones). Custom audio uploaded to the project's Assets section also appears in the sound picker.

See [`sounds/SOURCING.md`](sounds/SOURCING.md) for the license audit policy and how the library is built.

## Troubleshooting

**No sound on a particular panel.** Check that the page being shown contains an Audio Player element. Targeting is by element placement; a page with no element produces no sound.

**Sound is silent / overlay won't dismiss.** Browser autoplay policy requires a user gesture. Tap the panel anywhere to unlock audio for that browser session.

**Custom sound doesn't appear in the dropdown.** Click the refresh button (↻) at the top of the Plugin Actions section in the macro Add Step menu. The sound list is fetched once when the macro builder loads.

## License

MIT.
