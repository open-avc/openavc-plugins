"""
Tests for the Audio Player plugin.

Covers:
- Plugin lifecycle and initial state
- Sound catalog publishing (built-in manifest -> options_source state)
- Each macro action handler writes the right state
- Volume / mute validation
- MACRO_ACTIONS schema is valid (prefix, handler binding, param types)

Requires the openavc repo as a sibling directory (for PluginTestHarness).
Run from the openavc-plugins root: pytest tests/test_audio_player_plugin.py -v
"""

import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Add openavc server to the import path for the test harness
_OPENAVC_ROOT = Path(__file__).resolve().parents[2] / "openavc"
if str(_OPENAVC_ROOT) not in sys.path:
    sys.path.insert(0, str(_OPENAVC_ROOT))

# Add plugins root so we can import the plugin
_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))

try:
    from server.core.plugin_test_harness import PluginTestHarness
    from server.core.plugin_loader import validate_macro_actions
except ModuleNotFoundError:
    pytest.skip(
        "openavc repo not available as sibling directory",
        allow_module_level=True,
    )

from utility.audio_player.audio_player_plugin import AudioPlayerPlugin


# ──── Fixtures ────


@pytest_asyncio.fixture
async def harness_and_plugin():
    harness = PluginTestHarness()
    plugin = AudioPlayerPlugin()
    await harness.start_plugin(plugin)
    yield harness, plugin
    await harness.stop_plugin(plugin)


# ──── Manifest validation ────


def test_macro_actions_schema_valid():
    valid, error = validate_macro_actions(
        AudioPlayerPlugin.MACRO_ACTIONS, "audio_player", AudioPlayerPlugin
    )
    assert valid is True, error


def test_all_actions_prefixed_with_plugin_id():
    for action_type in AudioPlayerPlugin.MACRO_ACTIONS:
        assert action_type.startswith("audio_player.")


def test_all_handlers_are_async_methods():
    import inspect
    for action_type, spec in AudioPlayerPlugin.MACRO_ACTIONS.items():
        handler_name = spec["handler"]
        handler = getattr(AudioPlayerPlugin, handler_name, None)
        assert handler is not None, f"{action_type} handler '{handler_name}' missing"
        assert inspect.iscoroutinefunction(handler), f"{action_type} handler '{handler_name}' is not async"


# ──── Lifecycle and state ────


@pytest.mark.asyncio
async def test_initial_state_keys_set(harness_and_plugin):
    harness, _ = harness_and_plugin
    assert harness.state.get("plugin.audio_player.play_request") == ""
    assert harness.state.get("plugin.audio_player.last_played") == ""
    assert harness.state.get("plugin.audio_player.last_played_at") == ""
    assert harness.state.get("plugin.audio_player.master_volume") == 1.0
    assert harness.state.get("plugin.audio_player.muted") is False


@pytest.mark.asyncio
async def test_sound_catalog_published(harness_and_plugin):
    harness, _ = harness_and_plugin
    catalog_json = harness.state.get("plugin.audio_player.sounds")
    assert isinstance(catalog_json, str)
    catalog = json.loads(catalog_json)
    # We ship 10 placeholder built-in sounds in manifest.json
    assert len(catalog) >= 1
    for entry in catalog:
        assert "value" in entry and "label" in entry


@pytest.mark.asyncio
async def test_health_check(harness_and_plugin):
    _, plugin = harness_and_plugin
    health = await plugin.health_check()
    assert health["status"] == "ok"


# ──── action_play ────


@pytest.mark.asyncio
async def test_play_writes_request(harness_and_plugin):
    harness, plugin = harness_and_plugin
    await plugin.action_play({"sound": "chime_soft", "volume": 0.8}, {})

    request_json = harness.state.get("plugin.audio_player.play_request")
    request = json.loads(request_json)
    assert request["sound"] == "chime_soft"
    assert request["volume"] == 0.8
    assert "id" in request
    assert "ts" in request

    assert harness.state.get("plugin.audio_player.last_played") == "chime_soft"
    last_at = harness.state.get("plugin.audio_player.last_played_at")
    assert last_at and last_at.endswith("Z")


@pytest.mark.asyncio
async def test_play_default_volume(harness_and_plugin):
    harness, plugin = harness_and_plugin
    await plugin.action_play({"sound": "chime_soft"}, {})
    request = json.loads(harness.state.get("plugin.audio_player.play_request"))
    assert request["volume"] == 1.0


@pytest.mark.asyncio
async def test_play_unique_request_id(harness_and_plugin):
    harness, plugin = harness_and_plugin
    await plugin.action_play({"sound": "chime_soft"}, {})
    first = json.loads(harness.state.get("plugin.audio_player.play_request"))
    await plugin.action_play({"sound": "chime_soft"}, {})
    second = json.loads(harness.state.get("plugin.audio_player.play_request"))
    # Two consecutive plays must produce different request IDs so the panel
    # element fires twice (state changes only fire when value changes).
    assert first["id"] != second["id"]


@pytest.mark.asyncio
async def test_play_missing_sound_raises(harness_and_plugin):
    _, plugin = harness_and_plugin
    with pytest.raises(ValueError, match="sound"):
        await plugin.action_play({}, {})


@pytest.mark.asyncio
async def test_play_volume_out_of_range_raises(harness_and_plugin):
    _, plugin = harness_and_plugin
    with pytest.raises(ValueError, match="volume"):
        await plugin.action_play({"sound": "x", "volume": 1.5}, {})
    with pytest.raises(ValueError, match="volume"):
        await plugin.action_play({"sound": "x", "volume": -0.1}, {})


# ──── action_stop ────


@pytest.mark.asyncio
async def test_stop_writes_stop_request(harness_and_plugin):
    harness, plugin = harness_and_plugin
    await plugin.action_stop({}, {})
    request = json.loads(harness.state.get("plugin.audio_player.play_request"))
    assert request.get("stop") is True


# ──── action_set_volume ────


@pytest.mark.asyncio
async def test_set_volume(harness_and_plugin):
    harness, plugin = harness_and_plugin
    await plugin.action_set_volume({"volume": 0.5}, {})
    assert harness.state.get("plugin.audio_player.master_volume") == 0.5


@pytest.mark.asyncio
async def test_set_volume_out_of_range_raises(harness_and_plugin):
    _, plugin = harness_and_plugin
    with pytest.raises(ValueError, match="volume"):
        await plugin.action_set_volume({"volume": 2.0}, {})


# ──── action_mute / action_unmute ────


@pytest.mark.asyncio
async def test_mute_unmute(harness_and_plugin):
    harness, plugin = harness_and_plugin
    await plugin.action_mute({}, {})
    assert harness.state.get("plugin.audio_player.muted") is True
    await plugin.action_unmute({}, {})
    assert harness.state.get("plugin.audio_player.muted") is False
