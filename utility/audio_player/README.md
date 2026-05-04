# Audio Player Plugin

Play short sound effects through OpenAVC panels — chimes for meeting starts, bells for class changes, alerts, button-press feedback, paging notifications.

## How to Use It

The plugin doesn't ship any sounds. You upload your own to the project's Assets section and reference them from macros or scripts.

### 1. Upload your sounds

Programmer IDE → **Project → Assets** → drop in `.mp3`, `.wav`, `.ogg`, or `.m4a` files. They appear in the **Audio** filter and are automatically picked up by this plugin.

### 2. Trigger from a macro

1. Open or create a macro in the **Macros** view.
2. **+ Add Step** → scroll to **Plugin Actions** → **Audio Player** → **Play Sound**.
3. Pick the sound from the dropdown and (optionally) set a volume.
4. Run the macro — every connected panel plays the sound.

### 3. Trigger from a panel button

In **UI Builder**, set a button's **Press Action** to **Run Macro** and pick a macro that contains a Play Sound step.

### 4. Trigger from a script

```python
from openavc import plugins, on_event

@on_event("ui.press.lobby_chime")
async def chime(event):
    await plugins.audio_player.play("assets://lobby_chime.mp3", volume=0.6)
```

Available script methods: `play(sound, volume=1.0)`, `stop()`, `set_volume(volume)`, `mute()`, `unmute()`, `list_sounds()`.

## How It Works

Audio plays in the panel browser, not on the OpenAVC server. When you fire `audio_player.play`, the plugin writes a state key that every connected panel watches; each panel plays the sound through its speakers.

**Audio plays globally** — every connected panel plays. Per-panel targeting (e.g. "lobby only") is a future feature waiting on platform-level panel identity.

## Requirements

- **Browser audio unlock.** Modern browsers block audio until the user has tapped the screen at least once. Tap any panel once after it loads; audio works silently from there. Sounds that arrive before that first tap are dropped (logged in the browser console).
- **Speakers on the panel device.** Tablets, mini PCs, and TVs almost always have audio out.
- **At least one audio asset uploaded** to the project, otherwise the Play Sound dropdown will be empty.

## State Keys

| Key | Type | Purpose |
|-----|------|---------|
| `plugin.audio_player.play_request` | string (JSON) | Latest play request — panel runtime watches this. |
| `plugin.audio_player.last_played` | string | ID of last sound played. |
| `plugin.audio_player.last_played_at` | string (ISO 8601) | When the last sound was played. |
| `plugin.audio_player.master_volume` | float (0.0–1.0) | Global volume multiplier. |
| `plugin.audio_player.muted` | bool | When true, no sounds play regardless of volume. |
| `plugin.audio_player.sounds` | string (JSON) | Available sounds list (built from project audio assets), used by the macro builder dropdown. |

You can bind UI elements to any of these. For example, a "Mute" toggle on a panel can write `plugin.audio_player.muted` directly.

## Troubleshooting

**Empty sound dropdown in the macro builder.** No audio assets are uploaded yet. Open **Project → Assets** and add some audio files.

**Newly uploaded sound doesn't appear.** The macro builder fetches the sound list once when it loads. Click the ↻ button next to "Plugin Actions" in the Add Step menu to refresh.

**No sound on any panel.** First tap of the panel after page load unlocks audio. Tap anywhere on the panel and try again. Check the browser console for `[panel-audio]` messages.

**Sound plays on some panels but not others.** A panel that hasn't been tapped yet has audio still locked. Each panel needs one user gesture to unlock.

## License

MIT.
