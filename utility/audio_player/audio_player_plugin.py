"""
OpenAVC Audio Player Plugin

Plays sound effects through panel browsers. Sounds are uploaded to the
project's Assets section (Programmer IDE → Project → Assets) and then
referenced from macros, scripts, or button bindings.

Architecture: server-side plugin only — audio playback lives in the panel
runtime. Plugin writes a state key when triggered; every connected panel
watches that key and plays the sound through its speakers.
"""

import json
import time
import uuid


class AudioPlayerPlugin:

    PLUGIN_INFO = {
        "id": "audio_player",
        "name": "Audio Player",
        "version": "0.4.0",
        "author": "OpenAVC",
        "description": "Play sound effects through panels. Upload audio in Project → Assets, then trigger from macros or scripts.",
        "category": "utility",
        "license": "MIT",
        "platforms": ["all"],
        "min_openavc_version": "0.10.3",
        "capabilities": ["state_read", "state_write"],
        "usage": (
            "**1. Upload your sounds.** Open **Project → Assets**, then drop in "
            "the audio files you want to play (`.mp3`, `.wav`, `.ogg`, `.m4a`). "
            "They appear in the **Audio** filter and become available to this "
            "plugin automatically.\n\n"
            "**2. Trigger them from a macro.** In the **Macros** view, "
            "**+ Add Step** → **Plugin Actions** → **Audio Player** → "
            "**Play Sound**. Pick the file from the dropdown and (optionally) "
            "set a volume.\n\n"
            "**3. Or from a panel button.** In **UI Builder**, set the button's "
            "**Press Action** to **Run Macro** and pick the macro that contains "
            "the Play Sound step.\n\n"
            "**4. Or from a script:**\n\n"
            "```python\n"
            "from openavc import plugins, on_event\n\n"
            "@on_event(\"ui.press.lobby_chime\")\n"
            "async def chime(event):\n"
            "    await plugins.audio_player.play(\"assets://lobby_chime.mp3\", volume=0.6)\n"
            "```\n\n"
            "Audio plays on **every connected panel**. The first tap on a panel "
            "after page load unlocks audio (browser autoplay policy) — sounds "
            "before that first tap are dropped.\n\n"
            "**Other actions:** Stop All Sounds, Set Master Volume, Mute, Unmute "
            "(also available as `plugins.audio_player.stop()`, `set_volume(...)`, "
            "`mute()`, `unmute()` from scripts)."
        ),
    }

    MACRO_ACTIONS = {
        "audio_player.play": {
            "label": "Play Sound",
            "description": "Play a sound on every connected panel.",
            "icon": "volume-2",
            "handler": "action_play",
            "params": [
                {
                    "key": "sound",
                    "type": "select",
                    "label": "Sound",
                    "required": True,
                    "options_source": "plugin.audio_player.sounds",
                    "description": "Audio file uploaded to Project → Assets.",
                },
                {
                    "key": "volume",
                    "type": "float",
                    "label": "Volume",
                    "default": 1.0,
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.1,
                    "description": "0.0 (silent) to 1.0 (full volume).",
                },
            ],
        },
        "audio_player.stop": {
            "label": "Stop All Sounds",
            "description": "Stop any sounds currently playing on all panels.",
            "icon": "square",
            "handler": "action_stop",
            "params": [],
        },
        "audio_player.set_volume": {
            "label": "Set Master Volume",
            "description": "Set the master volume multiplier applied to every sound.",
            "icon": "sliders-horizontal",
            "handler": "action_set_volume",
            "params": [
                {
                    "key": "volume",
                    "type": "float",
                    "label": "Volume",
                    "required": True,
                    "default": 1.0,
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.1,
                },
            ],
        },
        "audio_player.mute": {
            "label": "Mute",
            "description": "Mute all sounds (overrides volume).",
            "icon": "volume-x",
            "handler": "action_mute",
            "params": [],
        },
        "audio_player.unmute": {
            "label": "Unmute",
            "description": "Resume sound playback after a Mute action.",
            "icon": "volume-2",
            "handler": "action_unmute",
            "params": [],
        },
    }

    SCRIPT_API = {
        "play": {
            "handler": "script_play",
            "doc": "Play a sound on every connected panel. Pass an `assets://filename.mp3` reference to a project audio asset.",
        },
        "stop": {
            "handler": "script_stop",
            "doc": "Stop sounds currently playing on every panel.",
        },
        "set_volume": {
            "handler": "script_set_volume",
            "doc": "Set the global master volume (0.0 to 1.0).",
        },
        "mute": {
            "handler": "script_mute",
            "doc": "Mute all sound playback (overrides volume).",
        },
        "unmute": {
            "handler": "script_unmute",
            "doc": "Resume playback after mute().",
        },
        "list_sounds": {
            "handler": "script_list_sounds",
            "doc": "Return the list of audio assets currently available in the project.",
            "sync": True,
        },
    }

    def __init__(self):
        self.api = None
        self._audio_assets: list[dict] = []

    # ──── Lifecycle ────

    async def start(self, api):
        self.api = api

        # Initial state values. Panel runtime watches play_request to know
        # when to play; last_played and last_played_at are observability
        # helpers; master_volume and muted apply globally.
        await self.api.state_set("play_request", "")
        await self.api.state_set("last_played", "")
        await self.api.state_set("last_played_at", "")
        await self.api.state_set("master_volume", 1.0)
        await self.api.state_set("muted", False)

        # Seed catalog from current project assets, then subscribe so it
        # refreshes whenever an asset is uploaded or deleted.
        initial = await self.api.state_get("project.assets")
        self._refresh_audio_assets(initial)
        await self._publish_sound_catalog()
        await self.api.state_subscribe("project.assets", self._on_assets_changed)

        self.api.log(f"Audio Player started — {len(self._audio_assets)} audio asset(s)")

    async def stop(self):
        # Auto-cleanup handles state keys, subscriptions, and tasks.
        pass

    async def health_check(self):
        return {
            "status": "ok",
            "message": f"{len(self._audio_assets)} audio asset(s) available",
        }

    # ──── Macro action handlers ────

    async def action_play(self, params: dict, _context: dict) -> None:
        """Write a play request — every connected panel will play."""
        sound = params.get("sound")
        if not sound:
            raise ValueError(
                "audio_player.play requires a 'sound' parameter "
                "(e.g. 'assets://chime.mp3')"
            )
        volume = float(params.get("volume", 1.0))
        if not 0.0 <= volume <= 1.0:
            raise ValueError(f"volume must be between 0.0 and 1.0 (got {volume})")

        request = {
            "id": uuid.uuid4().hex,
            "sound": sound,
            "volume": volume,
            "ts": time.time(),
        }
        await self.api.state_set("play_request", json.dumps(request))
        await self.api.state_set("last_played", sound)
        await self.api.state_set("last_played_at", _iso_now())

    async def action_stop(self, _params: dict, _context: dict) -> None:
        """Tell every panel to stop currently-playing sounds."""
        request = {"id": uuid.uuid4().hex, "stop": True, "ts": time.time()}
        await self.api.state_set("play_request", json.dumps(request))

    async def action_set_volume(self, params: dict, _context: dict) -> None:
        volume = float(params.get("volume", 1.0))
        if not 0.0 <= volume <= 1.0:
            raise ValueError(f"volume must be between 0.0 and 1.0 (got {volume})")
        await self.api.state_set("master_volume", volume)

    async def action_mute(self, _params: dict, _context: dict) -> None:
        await self.api.state_set("muted", True)

    async def action_unmute(self, _params: dict, _context: dict) -> None:
        await self.api.state_set("muted", False)

    # ──── Script API handlers ────
    #
    # These delegate to the macro action handlers but expose Pythonic
    # signatures so user scripts can call them naturally.

    async def script_play(self, sound: str, volume: float = 1.0) -> None:
        await self.action_play({"sound": sound, "volume": volume}, {})

    async def script_stop(self) -> None:
        await self.action_stop({}, {})

    async def script_set_volume(self, volume: float) -> None:
        await self.action_set_volume({"volume": volume}, {})

    async def script_mute(self) -> None:
        await self.action_mute({}, {})

    async def script_unmute(self) -> None:
        await self.action_unmute({}, {})

    def script_list_sounds(self) -> list[dict]:
        return list(self._audio_assets)

    # ──── Internal helpers ────

    async def _on_assets_changed(self, _key: str, value, _old) -> None:
        self._refresh_audio_assets(value)
        await self._publish_sound_catalog()

    def _refresh_audio_assets(self, raw_value) -> None:
        """Parse the project.assets state value and keep just the audio entries."""
        if not raw_value or not isinstance(raw_value, str):
            self._audio_assets = []
            return
        try:
            assets = json.loads(raw_value)
        except json.JSONDecodeError:
            self._audio_assets = []
            return
        if not isinstance(assets, list):
            self._audio_assets = []
            return
        self._audio_assets = [
            a for a in assets
            if isinstance(a, dict) and a.get("type") == "audio" and a.get("name")
        ]

    async def _publish_sound_catalog(self) -> None:
        """Write the available-sound list as a JSON string for dropdown population.

        Format matches the plugin macro action ``options_source`` contract:
        a JSON-serialized list of ``{value, label}`` objects. The macro
        builder reads this state key when rendering the sound picker.

        Each entry's value is an ``assets://filename`` reference — exactly
        what the action handler wants and what panel.js knows how to play.
        """
        catalog = [
            {
                "value": f"assets://{asset['name']}",
                "label": asset["name"],
            }
            for asset in self._audio_assets
        ]
        await self.api.state_set("sounds", json.dumps(catalog))


def _iso_now() -> str:
    """Current UTC time as ISO-8601, second precision."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
