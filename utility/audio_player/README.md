# Audio Player Plugin

Play short sound effects through OpenAVC panels — chimes for meeting starts, bells for class changes, alerts, button-press feedback, paging notifications.

## How to Use It

The plugin has no settings panel. You play sounds from macros, scripts, or button bindings.

### From a macro

1. Open or create a macro in the **Macros** view.
2. Click **+ Add Step** → scroll to the **Plugin Actions** section → **Audio Player** → **Play Sound**.
3. Pick a sound from the dropdown and (optionally) set a volume.
4. Run the macro. Every connected panel plays the sound.

### From a button on a panel

1. In **UI Builder**, select a button.
2. In **Press Action**, choose **Run macro** and pick a macro that contains a Play Sound step.

### From a script

```python
from openavc import plugins, on_event

@on_event("ui.press.lobby_chime")
async def chime(event):
    await plugins.audio_player.play("chime_soft", volume=0.6)
```

Available script methods: `play(sound, volume=1.0)`, `stop()`, `set_volume(volume)`, `mute()`, `unmute()`, `list_sounds()`.

## How It Works

Audio plays in the panel browser, not on the OpenAVC server. When you fire `audio_player.play`, the plugin writes a state key that every connected panel watches; each panel plays the sound through its speakers.

**Audio plays globally** — every connected panel chimes. Per-panel targeting (e.g. "lobby only") is a future feature waiting on platform-level panel identity.

## Requirements

- **Browser audio unlock.** Modern browsers block audio until the user has tapped the screen at least once. Tap any panel once after it loads; audio works silently from there. Sounds that arrive before that first tap are dropped (logged in the browser console).
- **Speakers on the panel device.** Tablets, mini PCs, and TVs almost always have audio out.

## Built-in Sounds

| ID | Description |
|----|-------------|
| `chime_soft` | Gentle bong — meeting starts |
| `chime_doorbell` | Two-tone confirmation — arrivals |
| `bell_school` | Glass-bell ring — class change / period transition |
| `alert_attention` | Three-tone questioning chime — get attention |
| `notification_pop` | Short pluck — button feedback |
| `countdown_beep` | Single tick — timer / countdown |
| `success` | Rising tone — action confirmed |
| `error` | Descending tone — action failed |
| `applause_short` | Crowd applause — award / celebration |

All sounds are CC0 (Kenney's interface sounds pack and BigSoundBank).

## Custom Sounds

Upload audio files to **Project → Assets** (`.mp3`, `.wav`, `.ogg`, `.m4a` accepted) and reference them by `assets://filename.mp3`:

```python
await plugins.audio_player.play("assets://my_custom_chime.mp3")
```

In a macro Play Sound step, type the `assets://` reference into the sound field (or pick from the autocomplete once project audio assets are wired into the dropdown — coming soon).

## State Keys

| Key | Type | Purpose |
|-----|------|---------|
| `plugin.audio_player.play_request` | string (JSON) | Latest play request — panel runtime watches this. |
| `plugin.audio_player.last_played` | string | ID of last sound played. |
| `plugin.audio_player.last_played_at` | string (ISO 8601) | When the last sound was played. |
| `plugin.audio_player.master_volume` | float (0.0–1.0) | Global volume multiplier. |
| `plugin.audio_player.muted` | bool | When true, no sounds play regardless of volume. |
| `plugin.audio_player.sounds` | string (JSON) | Available sounds list, used by the macro builder dropdown. |

You can bind UI elements to any of these state keys. For example, a "Mute" toggle on a panel can write `plugin.audio_player.muted` directly.

## Troubleshooting

**No sound on any panel.** First tap of the panel after page load unlocks audio. Tap anywhere on the panel and try again. Check the browser console for `[panel-audio]` messages.

**Custom sound doesn't play.** Confirm the file is in **Project → Assets**, the filename matches exactly (case-sensitive), and the extension is one of the supported types (`.mp3`, `.wav`, `.ogg`, `.m4a`).

**Refreshing the sound dropdown.** Sounds are fetched once when the macro builder loads. After uploading a new asset or installing a plugin update, click the ↻ button next to "Plugin Actions" in the macro Add Step menu.

## License

MIT. Built-in sounds are CC0 (see [`sounds/manifest.json`](sounds/manifest.json) for source attribution).
