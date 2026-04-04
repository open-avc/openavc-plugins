"""
Tests for the Dante DDM plugin.

Covers: connection lifecycle, device discovery / state mapping, routing
mutations, preset recall, reconnection scheduling, event handling,
health check, matrix cell click resolution, stale key cleanup,
and poll failure tolerance.

Requires the openavc repo as a sibling directory (for PluginTestHarness).
Run from the openavc-plugins root: pytest tests/ -v
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
except ModuleNotFoundError:
    pytest.skip(
        "openavc repo not available as sibling directory",
        allow_module_level=True,
    )

from integrations.dante.dante_plugin import DanteDDMPlugin, _sanitize_id


# ──── Fixtures ────


SAMPLE_DOMAIN_RESPONSE = {
    "data": {
        "domain": {
            "id": "domain-1",
            "name": "Main AV",
            "devices": [
                {
                    "id": "dev-001",
                    "name": "MIC-01",
                    "txChannels": [
                        {"id": "tx-1", "index": 1, "name": "01"},
                        {"id": "tx-2", "index": 2, "name": "02"},
                    ],
                    "rxChannels": [],
                },
                {
                    "id": "dev-002",
                    "name": "AMP-01",
                    "txChannels": [],
                    "rxChannels": [
                        {
                            "id": "rx-1",
                            "index": 1,
                            "name": "01",
                            "subscribedDevice": "MIC-01",
                            "subscribedChannel": "01",
                            "status": "DYNAMIC",
                            "summary": "CONNECTED",
                        },
                        {
                            "id": "rx-2",
                            "index": 2,
                            "name": "02",
                            "subscribedDevice": "",
                            "subscribedChannel": "",
                            "status": "NONE",
                            "summary": "NONE",
                        },
                    ],
                },
            ],
        }
    }
}

SAMPLE_DOMAINS_RESPONSE = {
    "data": {
        "domains": [
            {"id": "domain-1", "name": "Main AV"},
            {"id": "domain-2", "name": "Overflow"},
        ]
    }
}

SAMPLE_CONFIG = {
    "ddm_url": "https://ddm.local/graphql",
    "api_key": "test-api-key",
    "verify_tls": False,
    "domain_id": "domain-1",
    "poll_interval": 60,  # Long interval so polling doesn't interfere with tests
    "_presets": {
        "Meeting": [
            {
                "tx_device": "MIC-01",
                "tx_channel": "01",
                "rx_device": "AMP-01",
                "rx_channel_index": 1,
            },
            {
                "tx_device": "MIC-01",
                "tx_channel": "02",
                "rx_device": "AMP-01",
                "rx_channel_index": 2,
            },
        ],
    },
}


def _mock_httpx_client(responses: list[dict] | None = None):
    """Create a mock httpx.AsyncClient that returns canned GraphQL responses."""
    client = AsyncMock()
    client.aclose = AsyncMock()

    if responses is None:
        responses = [SAMPLE_DOMAINS_RESPONSE, SAMPLE_DOMAIN_RESPONSE]

    call_count = 0

    async def mock_post(url, json=None, **kwargs):
        nonlocal call_count
        resp = MagicMock()
        if call_count < len(responses):
            resp.json.return_value = responses[call_count]
        else:
            # Repeat last response
            resp.json.return_value = responses[-1]
        resp.raise_for_status = MagicMock()
        call_count += 1
        return resp

    client.post = mock_post
    return client


# ──── Unit Tests ────


class TestSanitizeId:
    def test_lowercase(self):
        assert _sanitize_id("MIC-01") == "mic_01"

    def test_spaces(self):
        assert _sanitize_id("My Device") == "my_device"

    def test_dots(self):
        assert _sanitize_id("audio.mixer.1") == "audio_mixer_1"

    def test_combined(self):
        assert _sanitize_id("AMP-Main 02.v2") == "amp_main_02_v2"


# ──── Integration Tests ────


@pytest.mark.asyncio
async def test_start_sets_initial_state():
    """Plugin sets connected=False and zeroed counts on start."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()

    # Patch httpx import to avoid real network calls
    mock_client = _mock_httpx_client()
    with patch("integrations.dante.dante_plugin.httpx", create=True) as mock_httpx:
        mock_httpx.AsyncClient.return_value = mock_client
        # Prevent the import inside _connect from failing
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            await harness.start_plugin(plugin, config=SAMPLE_CONFIG)

    # Initial state should be set
    assert await harness.state_get("plugin.dante.connected") is not None
    assert await harness.state_get("plugin.dante.device_count") is not None
    assert await harness.state_get("plugin.dante.subscription_count") is not None

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_health_check_disconnected():
    """Health check reports error when disconnected."""
    plugin = DanteDDMPlugin()
    result = await plugin.health_check()
    assert result["status"] == "error"
    assert "Disconnected" in result["message"]


@pytest.mark.asyncio
async def test_health_check_connected():
    """Health check reports ok when connected."""
    plugin = DanteDDMPlugin()
    plugin._connected = True
    plugin._domain_name = "Test Domain"
    plugin._devices = {"dev-1": {"name": "Device 1"}}
    result = await plugin.health_check()
    assert result["status"] == "ok"
    assert "1 devices" in result["message"]


@pytest.mark.asyncio
async def test_refresh_state_updates_keys():
    """_refresh_state populates Tx/Rx state keys and subscription counts."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "",
        "api_key": "",
        "poll_interval": 9999,
    }))
    plugin._domain_id = "domain-1"

    # Mock the GraphQL call
    domain_data = SAMPLE_DOMAIN_RESPONSE["data"]["domain"]
    plugin._graphql = AsyncMock(return_value={"domain": domain_data})

    await plugin._refresh_state()

    # Check device count
    assert await harness.state_get("plugin.dante.device_count") == 2

    # Check subscription count (MIC-01 -> AMP-01 ch1 is the one active sub)
    assert await harness.state_get("plugin.dante.subscription_count") == 1

    # Check Tx channel state
    tx_key = "plugin.dante.tx.mic_01.1"
    assert await harness.state_get(tx_key) == "MIC-01 / 01"

    # Check Rx channel state
    rx_key = "plugin.dante.rx.amp_01.1"
    assert await harness.state_get(rx_key) == "AMP-01 / 01"

    # Check route status
    route_key = "plugin.dante.route.amp_01.1"
    assert await harness.state_get(route_key) == "connected"

    # Check route info
    info_key = "plugin.dante.route_info.amp_01.1"
    assert await harness.state_get(info_key) == "MIC-01 / 01"

    # Channel 2 on AMP-01 has no subscription
    route2 = "plugin.dante.route.amp_01.2"
    assert await harness.state_get(route2) == "none"

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_route_success():
    """route() calls GraphQL mutation and emits route.changed event."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "",
        "api_key": "",
        "poll_interval": 9999,
    }))

    # Populate devices
    plugin._devices = {
        "dev-002": {
            "id": "dev-002",
            "name": "AMP-01",
            "rxChannels": [{"id": "rx-1", "index": 1, "name": "01"}],
        }
    }

    # Mock mutation
    plugin._set_subscription = AsyncMock(return_value=True)

    events = []
    await plugin.api.event_subscribe("plugin.dante.route.*", lambda e, p: events.append((e, p)))

    result = await plugin.route("AMP-01", 1, "MIC-01", "01")
    assert result is True
    plugin._set_subscription.assert_called_once_with("dev-002", 1, "MIC-01", "01")

    # Give event bus time to dispatch
    await asyncio.sleep(0.05)
    assert any("route.changed" in e for e, _ in events)

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_route_device_not_found():
    """route() returns False and emits route.failed for unknown device."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "",
        "api_key": "",
        "poll_interval": 9999,
    }))
    plugin._devices = {}

    events = []
    await plugin.api.event_subscribe("plugin.dante.route.*", lambda e, p: events.append((e, p)))

    result = await plugin.route("NONEXISTENT", 1, "MIC-01", "01")
    assert result is False

    await asyncio.sleep(0.05)
    assert any("route.failed" in e for e, _ in events)

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_unroute_success():
    """unroute() sends empty strings and emits route.changed."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "",
        "api_key": "",
        "poll_interval": 9999,
    }))

    plugin._devices = {
        "dev-002": {
            "id": "dev-002",
            "name": "AMP-01",
            "rxChannels": [{"id": "rx-1", "index": 1, "name": "01"}],
        }
    }

    plugin._set_subscription = AsyncMock(return_value=True)

    result = await plugin.unroute("AMP-01", 1)
    assert result is True
    plugin._set_subscription.assert_called_once_with("dev-002", 1, "", "")

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_preset_recall():
    """recall_preset clears existing routes then applies preset routes."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config=SAMPLE_CONFIG))

    plugin._devices = {
        "dev-002": {
            "id": "dev-002",
            "name": "AMP-01",
            "rxChannels": [
                {
                    "id": "rx-1",
                    "index": 1,
                    "name": "01",
                    "subscribedDevice": "OLD-SRC",
                    "subscribedChannel": "01",
                },
                {"id": "rx-2", "index": 2, "name": "02",
                 "subscribedDevice": "", "subscribedChannel": ""},
            ],
        }
    }

    batch_calls = []
    async def mock_batch(device_id, subs):
        batch_calls.append((device_id, subs))
        return True

    plugin._set_subscriptions_batch = mock_batch

    events = []
    await plugin.api.event_subscribe("plugin.dante.preset.*", lambda e, p: events.append((e, p)))

    result = await plugin.recall_preset("Meeting")
    assert result is True

    # First call should be the clear (OLD-SRC on ch1)
    assert len(batch_calls) >= 2
    clear_call = batch_calls[0]
    assert clear_call[0] == "dev-002"
    assert clear_call[1][0]["subscribedDevice"] == ""

    # Second call should be the preset routes
    preset_call = batch_calls[1]
    assert preset_call[0] == "dev-002"
    assert len(preset_call[1]) == 2

    await asyncio.sleep(0.05)
    assert any("preset.recalled" in e for e, _ in events)

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_preset_not_found():
    """recall_preset returns False for unknown preset."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config=SAMPLE_CONFIG))

    result = await plugin.recall_preset("NonExistent")
    assert result is False

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_clear_all_subscriptions():
    """clear_all action clears all active subscriptions."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config=SAMPLE_CONFIG))

    plugin._devices = {
        "dev-002": {
            "id": "dev-002",
            "name": "AMP-01",
            "rxChannels": [
                {
                    "id": "rx-1",
                    "index": 1,
                    "name": "01",
                    "subscribedDevice": "MIC-01",
                    "subscribedChannel": "01",
                },
                {
                    "id": "rx-2",
                    "index": 2,
                    "name": "02",
                    "subscribedDevice": "",
                    "subscribedChannel": "",
                },
            ],
        }
    }

    plugin._set_subscriptions_batch = AsyncMock(return_value=True)

    result = await plugin._clear_all_subscriptions()
    assert result is True

    # Only channel 1 had an active subscription, so batch should have 1 unsub
    plugin._set_subscriptions_batch.assert_called_once()
    subs = plugin._set_subscriptions_batch.call_args[0][1]
    assert len(subs) == 1
    assert subs[0]["subscribedDevice"] == ""
    assert subs[0]["subscribedChannel"] == ""

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_graphql_error_handling():
    """_graphql returns None on GraphQL errors."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "",
        "api_key": "",
        "poll_interval": 9999,
    }))

    # Mock client that returns GraphQL errors
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "errors": [{"message": "Unauthorized"}]
    }
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    plugin._client = mock_client

    result = await plugin._graphql("query { domains { id } }")
    assert result is None

    assert harness.log_contains("Unauthorized")

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_graphql_network_error():
    """_graphql raises on network exceptions (not swallowed)."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "",
        "api_key": "",
        "poll_interval": 9999,
    }))

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=ConnectionError("timeout"))
    plugin._client = mock_client

    with pytest.raises(ConnectionError):
        await plugin._graphql("query { domains { id } }")

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_find_device_by_name():
    """_find_device_by_name returns device dict or None."""
    plugin = DanteDDMPlugin()
    plugin._devices = {
        "dev-001": {"id": "dev-001", "name": "MIC-01"},
        "dev-002": {"id": "dev-002", "name": "AMP-01"},
    }

    assert plugin._find_device_by_name("MIC-01")["id"] == "dev-001"
    assert plugin._find_device_by_name("AMP-01")["id"] == "dev-002"
    assert plugin._find_device_by_name("NONEXISTENT") is None


@pytest.mark.asyncio
async def test_resolve_matrix_ref_rx():
    """_resolve_matrix_ref resolves Rx references correctly."""
    plugin = DanteDDMPlugin()
    plugin._devices = {
        "dev-002": {
            "id": "dev-002",
            "name": "AMP-01",
            "rxChannels": [{"id": "rx-1", "index": 1, "name": "01"}],
        }
    }

    name, idx = plugin._resolve_matrix_ref("amp_01.1", "rx")
    assert name == "AMP-01"
    assert idx == 1


@pytest.mark.asyncio
async def test_resolve_matrix_ref_tx():
    """_resolve_matrix_ref resolves Tx references to channel name."""
    plugin = DanteDDMPlugin()
    plugin._devices = {
        "dev-001": {
            "id": "dev-001",
            "name": "MIC-01",
            "txChannels": [
                {"id": "tx-1", "index": 1, "name": "Audio L"},
                {"id": "tx-2", "index": 2, "name": "Audio R"},
            ],
        }
    }

    name, ch_name = plugin._resolve_matrix_ref("mic_01.1", "tx")
    assert name == "MIC-01"
    assert ch_name == "Audio L"


@pytest.mark.asyncio
async def test_resolve_matrix_ref_invalid():
    """_resolve_matrix_ref returns None for invalid refs."""
    plugin = DanteDDMPlugin()
    plugin._devices = {}

    name, idx = plugin._resolve_matrix_ref("bad_ref", "rx")
    assert name is None

    name, idx = plugin._resolve_matrix_ref("unknown.1", "rx")
    assert name is None


@pytest.mark.asyncio
async def test_stop_cleanup():
    """stop() closes client and clears internal state."""
    plugin = DanteDDMPlugin()
    plugin._connected = True
    plugin._domain_id = "domain-1"
    plugin._domain_name = "Test"
    plugin._devices = {"dev-1": {"name": "X"}}
    plugin._state_keys = {"tx.x.1", "rx.y.1"}
    plugin._client = AsyncMock()
    plugin.api = MagicMock()
    plugin.api.log = MagicMock()

    await plugin.stop()

    assert plugin._connected is False
    assert plugin._domain_id is None
    assert plugin._devices == {}
    assert plugin._state_keys == set()
    assert plugin._client is None


@pytest.mark.asyncio
async def test_parse_presets_valid():
    """_parse_presets reads _presets dict from config."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config=SAMPLE_CONFIG))

    presets = plugin._parse_presets()
    assert "Meeting" in presets
    assert len(presets["Meeting"]) == 2
    assert presets["Meeting"][0]["tx_device"] == "MIC-01"

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_parse_presets_empty():
    """_parse_presets returns empty dict when no presets configured."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "",
        "api_key": "",
        "poll_interval": 9999,
    }))

    presets = plugin._parse_presets()
    assert presets == {}

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_snapshot_current_routes():
    """_snapshot_current_routes captures active subscriptions."""
    plugin = DanteDDMPlugin()
    plugin._devices = {
        "dev-001": {
            "id": "dev-001",
            "name": "MIC-01",
            "txChannels": [{"id": "tx-1", "index": 1, "name": "01"}],
            "rxChannels": [],
        },
        "dev-002": {
            "id": "dev-002",
            "name": "AMP-01",
            "txChannels": [],
            "rxChannels": [
                {
                    "id": "rx-1", "index": 1, "name": "01",
                    "subscribedDevice": "MIC-01", "subscribedChannel": "01",
                },
                {
                    "id": "rx-2", "index": 2, "name": "02",
                    "subscribedDevice": "", "subscribedChannel": "",
                },
            ],
        },
    }
    routes = plugin._snapshot_current_routes()
    assert len(routes) == 1
    assert routes[0]["tx_device"] == "MIC-01"
    assert routes[0]["rx_device"] == "AMP-01"
    assert routes[0]["rx_channel_index"] == 1


@pytest.mark.asyncio
async def test_save_preset():
    """save_preset captures current routes and saves to config."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "", "api_key": "", "poll_interval": 9999,
    }))
    plugin._devices = {
        "dev-002": {
            "id": "dev-002",
            "name": "AMP-01",
            "rxChannels": [
                {"id": "rx-1", "index": 1, "name": "01",
                 "subscribedDevice": "MIC-01", "subscribedChannel": "01"},
            ],
        },
    }

    await plugin._handle_save_preset({"name": "Test Preset"})

    # Config should now have the preset
    presets = plugin.api.config.get("_presets", {})
    assert "Test Preset" in presets
    assert len(presets["Test Preset"]) == 1
    assert presets["Test Preset"][0]["tx_device"] == "MIC-01"

    # Active preset should be set
    assert await harness.state_get("plugin.dante.active_preset") == "Test Preset"
    assert await harness.state_get("plugin.dante.preset_dirty") is False

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_update_preset():
    """update_preset overwrites an existing preset."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "", "api_key": "", "poll_interval": 9999,
        "_presets": {"Old": [{"tx_device": "X", "tx_channel": "1",
                              "rx_device": "Y", "rx_channel_index": 1}]},
    }))
    plugin._devices = {}  # No active routes

    await plugin._handle_update_preset({"name": "Old"})

    presets = plugin.api.config.get("_presets", {})
    assert "Old" in presets
    assert presets["Old"] == []  # No devices = no routes

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_delete_preset():
    """delete_preset removes preset and clears active state."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "", "api_key": "", "poll_interval": 9999,
        "_presets": {"ToDelete": []},
    }))

    await plugin._handle_delete_preset({"name": "ToDelete"})

    presets = plugin.api.config.get("_presets", {})
    assert "ToDelete" not in presets
    assert await harness.state_get("plugin.dante.active_preset") == ""

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_dirty_tracking():
    """route() sets preset_dirty to True."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "", "api_key": "", "poll_interval": 9999,
    }))
    plugin._devices = {
        "dev-002": {
            "id": "dev-002", "name": "AMP-01",
            "rxChannels": [{"id": "rx-1", "index": 1, "name": "01"}],
        },
    }
    plugin._set_subscription = AsyncMock(return_value=True)

    # Initially not dirty
    assert await harness.state_get("plugin.dante.preset_dirty") is False

    await plugin.route("AMP-01", 1, "MIC-01", "01")

    assert await harness.state_get("plugin.dante.preset_dirty") is True

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_devices_updated_event():
    """devices.updated event fires when device list changes."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "",
        "api_key": "",
        "poll_interval": 9999,
    }))
    plugin._domain_id = "domain-1"

    events = []
    await plugin.api.event_subscribe(
        "plugin.dante.devices.*",
        lambda e, p: events.append((e, p)),
    )

    domain_data = SAMPLE_DOMAIN_RESPONSE["data"]["domain"]
    plugin._graphql = AsyncMock(return_value={"domain": domain_data})

    # First refresh — devices go from empty to 2
    await plugin._refresh_state()
    await asyncio.sleep(0.05)

    assert any("devices.updated" in e for e, _ in events)

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_graphql_error_no_disconnect():
    """GraphQL errors during refresh don't count as connection failures."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "",
        "api_key": "",
        "poll_interval": 9999,
    }))
    plugin._domain_id = "domain-1"
    plugin._connected = True

    # _graphql returns None (GraphQL error, not network error)
    plugin._graphql = AsyncMock(return_value=None)

    # Should return silently, not raise
    await plugin._refresh_state()

    # Connection should still be intact
    assert plugin._connected is True

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_stale_keys_cleaned_up():
    """State keys from disappeared devices are set to None."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "",
        "api_key": "",
        "poll_interval": 9999,
    }))
    plugin._domain_id = "domain-1"

    # First refresh with 2 devices
    domain_data = SAMPLE_DOMAIN_RESPONSE["data"]["domain"]
    plugin._graphql = AsyncMock(return_value={"domain": domain_data})
    await plugin._refresh_state()

    # Verify MIC-01 tx keys exist
    assert await harness.state_get("plugin.dante.tx.mic_01.1") == "MIC-01 / 01"

    # Second refresh with MIC-01 removed
    reduced_domain = {
        "id": "domain-1",
        "name": "Main AV",
        "devices": [
            {
                "id": "dev-002",
                "name": "AMP-01",
                "txChannels": [],
                "rxChannels": [
                    {
                        "id": "rx-1",
                        "index": 1,
                        "name": "01",
                        "subscribedDevice": "",
                        "subscribedChannel": "",
                        "status": "NONE",
                        "summary": "NONE",
                    },
                    {
                        "id": "rx-2",
                        "index": 2,
                        "name": "02",
                        "subscribedDevice": "",
                        "subscribedChannel": "",
                        "status": "NONE",
                        "summary": "NONE",
                    },
                ],
            },
        ],
    }
    plugin._graphql = AsyncMock(return_value={"domain": reduced_domain})
    await plugin._refresh_state()

    # MIC-01 tx keys should now be None (stale)
    assert await harness.state_get("plugin.dante.tx.mic_01.1") is None
    assert await harness.state_get("plugin.dante.tx.mic_01.2") is None

    # AMP-01 keys should still be alive
    assert await harness.state_get("plugin.dante.rx.amp_01.1") == "AMP-01 / 01"

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_poll_failure_tolerance():
    """Poll loop tolerates transient failures before disconnecting."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "",
        "api_key": "",
        "poll_interval": 9999,
    }))
    plugin._connected = True
    plugin._domain_id = "domain-1"

    call_count = 0

    async def mock_graphql(query, variables=None):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise ConnectionError("transient error")
        # Third call succeeds
        return {"domain": SAMPLE_DOMAIN_RESPONSE["data"]["domain"]}

    plugin._graphql = mock_graphql

    # Run 3 poll cycles manually
    for _ in range(3):
        try:
            await plugin._refresh_state()
        except Exception:
            pass  # Poll loop would catch this

    # After 2 failures + 1 success, should still be connected
    assert plugin._connected is True

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_matrix_click_route():
    """Matrix cell click routes via _handle_route_action with row/col payload."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "",
        "api_key": "",
        "poll_interval": 9999,
    }))

    plugin._devices = {
        "dev-001": {
            "id": "dev-001",
            "name": "MIC-01",
            "txChannels": [{"id": "tx-1", "index": 1, "name": "01"}],
            "rxChannels": [],
        },
        "dev-002": {
            "id": "dev-002",
            "name": "AMP-01",
            "txChannels": [],
            "rxChannels": [{"id": "rx-1", "index": 1, "name": "01"}],
        },
    }

    plugin._set_subscription = AsyncMock(return_value=True)

    # Simulate matrix cell click: row=rx device.channel, col=tx device.channel
    await plugin._handle_route_action({"row": "amp_01.1", "col": "mic_01.1"})

    plugin._set_subscription.assert_called_once_with("dev-002", 1, "MIC-01", "01")

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_matrix_click_unroute():
    """Matrix cell click unroutes via _handle_unroute_action with row payload."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "",
        "api_key": "",
        "poll_interval": 9999,
    }))

    plugin._devices = {
        "dev-002": {
            "id": "dev-002",
            "name": "AMP-01",
            "rxChannels": [{"id": "rx-1", "index": 1, "name": "01"}],
        },
    }

    plugin._set_subscription = AsyncMock(return_value=True)

    await plugin._handle_unroute_action({"row": "amp_01.1"})

    plugin._set_subscription.assert_called_once_with("dev-002", 1, "", "")

    await harness.stop_plugin(plugin)


@pytest.mark.asyncio
async def test_reconnect_task_cleared_after_success():
    """_reconnect_task is set to None after reconnect completes."""
    plugin = DanteDDMPlugin()
    plugin.api = MagicMock()
    plugin.api.log = MagicMock()
    plugin.api.config = SAMPLE_CONFIG

    # Make create_task run the coroutine as a real asyncio task
    plugin.api.create_task = lambda coro, name=None: asyncio.create_task(coro)

    # Mock _connect to succeed
    plugin._connect = AsyncMock(return_value=True)
    plugin._connected = False

    plugin._schedule_reconnect(SAMPLE_CONFIG)
    assert plugin._reconnect_task is not None

    # Wait for the reconnect task to complete
    await asyncio.sleep(6)  # 5s delay + margin

    # Task should have cleared itself
    assert plugin._reconnect_task is None

    # Verify we can schedule a new reconnect
    plugin._connected = False
    plugin._connect = AsyncMock(return_value=True)
    plugin._schedule_reconnect(SAMPLE_CONFIG)
    assert plugin._reconnect_task is not None

    # Clean up
    plugin._shutting_down = True
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_connect_returns_bool():
    """_connect returns True on success, False on failure."""
    harness = PluginTestHarness()
    plugin = DanteDDMPlugin()
    plugin.api = (await harness.start_plugin(plugin, config={
        "ddm_url": "",
        "api_key": "",
        "poll_interval": 9999,
    }))

    # Missing URL/key should return False
    result = await plugin._connect({"ddm_url": "", "api_key": ""})
    assert result is False

    await harness.stop_plugin(plugin)
