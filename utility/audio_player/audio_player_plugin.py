"""
OpenAVC Audio Player Plugin

Plays sound effects through panel browsers. Triggered from macros, scripts,
and UI bindings. Use cases include meeting-start chimes, "class dismissed"
bells, doorbell tones, paging notifications, button-press feedback.

Architecture: audio playback happens in a panel-side UI element that
subscribes to a state key. Macro actions write a play request to that key;
every panel running an Audio Player element receives the state update and
plays the sound. Targeting falls out of element placement — put the element
on the lobby page only and only the lobby chimes; put it on a shared base
page and the whole building chimes.
"""

import json
import time
import uuid
from pathlib import Path


class AudioPlayerPlugin:

    PLUGIN_INFO = {
        "id": "audio_player",
        "name": "Audio Player",
        "version": "0.2.0",
        "author": "OpenAVC",
        "description": "Play sound effects through panels — chimes, bells, alerts, notifications.",
        "category": "utility",
        "license": "MIT",
        "platforms": ["all"],
        "min_openavc_version": "0.6.0",
        "capabilities": ["state_write"],
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
                    "description": "A built-in sound or a custom audio asset uploaded to the project.",
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
            "doc": "Play a sound on every connected panel. Pass the sound id (built-in sound) or 'assets://filename.mp3' for a project-uploaded audio asset.",
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
            "doc": "Return the list of built-in sounds with their metadata.",
            "sync": True,
        },
    }

    def __init__(self):
        self.api = None
        self._builtin_sounds: list[dict] = []

    # ──── Lifecycle ────

    async def start(self, api):
        self.api = api
        self._builtin_sounds = self._load_builtin_manifest()

        # Initial state values. The panel element subscribes to play_request
        # to learn when to play; last_played and last_played_at are read-only
        # observability helpers; master_volume and muted apply globally.
        await self.api.state_set("play_request", "")
        await self.api.state_set("last_played", "")
        await self.api.state_set("last_played_at", "")
        await self.api.state_set("master_volume", 1.0)
        await self.api.state_set("muted", False)

        # Publish the sound catalog as a JSON-serialized list so the macro
        # builder can populate the sound dropdown via options_source.
        await self._publish_sound_catalog()

        self.api.log(f"Audio Player started — {len(self._builtin_sounds)} built-in sound(s)")

    async def stop(self):
        # Auto-cleanup handles state keys, subscriptions, and tasks.
        pass

    async def health_check(self):
        return {"status": "ok", "message": f"{len(self._builtin_sounds)} built-in sounds available"}

    # ──── Macro action handlers ────

    async def action_play(self, params: dict, _context: dict) -> None:
        """Write a play request — every panel with the element will play."""
        sound = params.get("sound")
        if not sound:
            raise ValueError("audio_player.play requires a 'sound' parameter")
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
    # signatures so user scripts can call them naturally. Same underlying
    # behavior — just a friendlier surface for code than the (params, context)
    # macro envelope.

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
        return list(self._builtin_sounds)

    # ──── Internal helpers ────

    def _load_builtin_manifest(self) -> list[dict]:
        """Read the built-in sound library manifest from the plugin directory."""
        manifest_path = Path(__file__).parent / "sounds" / "manifest.json"
        if not manifest_path.is_file():
            return []
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            if self.api:
                self.api.log(f"Failed to read sound manifest: {e}", level="warning")
            return []
        sounds = data.get("sounds", [])
        return [s for s in sounds if isinstance(s, dict) and s.get("id") and s.get("file")]

    async def _publish_sound_catalog(self) -> None:
        """Write the available-sound list as a JSON string for dropdown population.

        Format matches the plugin macro action `options_source` contract:
        a JSON-serialized list of ``{value, label}`` objects. The macro
        builder reads this state key when rendering the sound picker.
        """
        catalog = [
            {"value": s["id"], "label": s.get("name") or s["id"]}
            for s in self._builtin_sounds
        ]
        await self.api.state_set("sounds", json.dumps(catalog))


def _iso_now() -> str:
    """Current UTC time as ISO-8601, second precision."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
