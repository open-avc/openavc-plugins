"""
Dante DDM Plugin for OpenAVC

Audio network routing via Dante Domain Manager or Dante Director Professional.
Uses the official Audinate Managed API (GraphQL over HTTPS) to discover devices,
manage audio subscriptions, monitor routing status, and recall routing presets.

Requires: httpx (MIT license, async HTTP client)
"""

import asyncio
import logging

try:
    from . import channel_keys, routing
except ImportError:  # loaded by a loader that is not a package one
    import channel_keys
    import routing

logger = logging.getLogger(__name__)

PLUGIN_INFO = {
    "id": "dante",
    "name": "Dante DDM",
    "version": "1.2.2",
    "author": "OpenAVC",
    "description": "Audio routing via Dante Domain Manager or Dante Director Professional.",
    "category": "integration",
    "license": "MIT",
    "platforms": ["all"],
    "min_openavc_version": "0.3.0",
    "capabilities": [
        "state_read",
        "state_write",
        "event_emit",
        "event_subscribe",
    ],
    "dependencies": ["httpx>=0.27.0"],
}

# The groups CONFIG_SCHEMA puts its fields in. The settings form saves each
# field inside its group; see _settings.
_SETTING_GROUPS = ("connection", "polling")


def _settings(config: dict) -> dict:
    """The plugin's settings as one flat dict.

    The settings form saves each field inside its group, as CONFIG_SCHEMA
    declares them (``{"connection": {"api_key": ...}}``). A config written by
    hand or through the API may have them at the top level instead. A value
    inside its group wins over the same key at the top level.
    """
    flat = {k: v for k, v in config.items() if k not in _SETTING_GROUPS}
    for group in _SETTING_GROUPS:
        values = config.get(group)
        if isinstance(values, dict):
            flat.update(values)
    return flat


CONFIG_SCHEMA = {
    "connection": {
        "type": "group",
        "label": "Connection",
        "fields": {
            "ddm_url": {
                "type": "string",
                "label": "DDM / Director URL",
                "description": (
                    "GraphQL API endpoint. For Dante Director (cloud): "
                    "https://api.director.dante.cloud/graphql  "
                    "For on-premise DDM: https://<your-ddm-host>/graphql"
                ),
                "required": True,
                "placeholder": "https://api.director.dante.cloud/graphql",
            },
            "api_key": {
                "type": "string",
                "label": "API key",
                "description": "API key from Dante Director or DDM admin interface.",
                "required": True,
                "placeholder": "paste your API key here",
                "secret": True,
            },
            "verify_tls": {
                "type": "boolean",
                "label": "Verify TLS certificate",
                "description": (
                    "Disable for on-premise DDM with self-signed certificates."
                ),
                "default": True,
            },
        },
    },
    "polling": {
        "type": "group",
        "label": "Polling",
        "fields": {
            "domain_id": {
                "type": "string",
                "label": "Domain ID",
                "description": (
                    "Dante domain to manage. Leave blank to auto-select the first domain. "
                    "Use the 'Refresh Domains' action to discover available domains."
                ),
                "default": "",
                "placeholder": "auto-detect",
            },
            "poll_interval": {
                "type": "integer",
                "label": "Poll interval (seconds)",
                "description": (
                    "How often to query DDM for device and routing state. "
                    "The Dante Managed API rate-limits to 60 requests per minute."
                ),
                "default": 5,
                "min": 3,
                "max": 30,
            },
        },
    },
}

EXTENSIONS = {
    "status_cards": [
        {
            "id": "dante_status",
            "label": "Dante DDM",
            "icon": "radio",
            "metrics": [
                {
                    "key": "plugin.dante.connected",
                    "label": "Connected",
                    "format": "boolean",
                },
                {
                    "key": "plugin.dante.domain_name",
                    "label": "Domain",
                    "format": "string",
                },
                {
                    "key": "plugin.dante.device_count",
                    "label": "Devices",
                    "format": "number",
                },
                {
                    "key": "plugin.dante.subscription_count",
                    "label": "Subscriptions",
                    "format": "number",
                },
            ],
        }
    ],
    "context_actions": [
        {
            "id": "refresh_domains",
            "label": "Refresh Domains",
            "icon": "refresh-cw",
            "context": "global",
            "event": "action.refresh_domains",
        },
        {
            "id": "clear_all",
            "label": "Clear All Subscriptions",
            "icon": "x-circle",
            "context": "global",
            "event": "action.clear_all",
        },
    ],
}

SURFACE_LAYOUT = {
    "type": "matrix",
    "rows_label": "Receivers (Rx)",
    "columns_label": "Transmitters (Tx)",
    "rows_state_pattern": "plugin.dante.rx.*",
    "columns_state_pattern": "plugin.dante.tx.*",
    "cell_type": "route",
    "cell_state_pattern": "plugin.dante.route.{row}.{col}",
    "presets": True,
}

AI_GUIDE = """
The Dante DDM plugin connects OpenAVC to a Dante Domain Manager or Dante Director
Professional instance to provide audio network routing control.

## Setup

1. Enter the GraphQL API endpoint URL (cloud or on-premise DDM).
2. Enter the API key from your Dante Director or DDM admin dashboard.
3. Optionally set a specific domain ID, or leave blank to auto-detect.
4. Once connected, a routing matrix appears showing all Tx and Rx channels.

## Routing Matrix

The routing matrix shows transmitters (Tx) as columns and receivers (Rx) as rows.
Click a crosspoint to route audio between them. Click an active crosspoint to
unroute it. The matrix updates in real time as DDM state changes.

## Routing Presets

Save the current routing state as a named preset using the toolbar above the matrix.
Recall a preset to restore that exact routing snapshot (clears all routes first).
After recalling a preset and making changes, click "Update" to save the changes
or "Save as New" to create a new preset.

Presets are stored in the project file and can be recalled from macros/triggers
by emitting plugin.dante.action.recall_preset with payload:
{"preset_name": "Meeting"}.

## State Keys

- plugin.dante.connected — Connected to DDM (boolean)
- plugin.dante.domain_name — Active Dante domain name (string)
- plugin.dante.device_count — Dante devices discovered (integer)
- plugin.dante.subscription_count — Active audio subscriptions (integer)
- plugin.dante.active_preset — Currently recalled preset name (string)
- plugin.dante.preset_dirty — Routes changed since last recall (boolean)
- plugin.dante.tx.<device_id>.<channel_index> — Tx channel label (string)
- plugin.dante.rx.<device_id>.<channel_index> — Rx channel label (string)
- plugin.dante.route.<rx_device_id>.<rx_channel_index> — Route status (string)
- plugin.dante.route_info.<rx_device_id>.<rx_channel_index> — Source info (string)

## Events

- plugin.dante.connected — Connection established
- plugin.dante.disconnected — Connection lost
- plugin.dante.devices.updated — Device/channel list refreshed
- plugin.dante.route.changed — A subscription was created or removed
- plugin.dante.route.failed — A subscription mutation failed
- plugin.dante.preset.recalled — A routing preset was applied

## Macros / Triggers

To route audio from a macro or script, emit plugin.dante.action.route with
payload: {"rx_device": "...", "rx_channel_index": 1,
"tx_device_name": "...", "tx_channel_name": "..."}.

To unsubscribe, emit plugin.dante.action.unroute with payload:
{"rx_device": "...", "rx_channel_index": 1}.

To recall a preset, emit plugin.dante.action.recall_preset with payload:
{"preset_name": "Meeting"}.
"""


# ──── GraphQL Queries ────

_QUERY_DOMAINS = """
query Domains {
    domains {
        id
        name
    }
}
"""

_QUERY_DOMAIN = """
query Domain($domainIDInput: ID!) {
    domain(id: $domainIDInput) {
        id
        name
        devices {
            id
            name
            rxChannels {
                id
                index
                name
                subscribedDevice
                subscribedChannel
                status
                summary
            }
            txChannels {
                id
                index
                name
            }
        }
    }
}
"""

_MUTATION_SET_SUBSCRIPTIONS = """
mutation DeviceRxChannelsSubscriptionSet($input: DeviceRxChannelsSubscriptionSetInput!) {
    DeviceRxChannelsSubscriptionSet(input: $input) {
        ok
    }
}
"""


class DanteDDMPlugin:
    """Dante Domain Manager / Director plugin."""

    PLUGIN_INFO = PLUGIN_INFO
    CONFIG_SCHEMA = CONFIG_SCHEMA
    EXTENSIONS = EXTENSIONS
    SURFACE_LAYOUT = SURFACE_LAYOUT
    AI_GUIDE = AI_GUIDE

    def __init__(self):
        self.api = None
        self._client = None  # httpx.AsyncClient
        self._connected = False
        self._domain_id: str | None = None
        self._domain_name: str | None = None
        self._devices: dict[str, dict] = {}  # id -> device data from GraphQL
        self._state_keys: set[str] = set()  # Track keys for stale cleanup
        self._poll_task = None
        self._reconnect_task = None
        self._shutting_down = False

    # ──── Lifecycle ────

    async def start(self, api):
        self.api = api
        self._shutting_down = False
        cfg = _settings(api.config)

        # Set initial state
        await api.state_set("connected", False)
        await api.state_set("domain_name", "")
        await api.state_set("device_count", 0)
        await api.state_set("subscription_count", 0)
        await self._show_preset("")

        # Subscribe to context actions and programmatic route events
        await api.event_subscribe("plugin.dante.action.*", self._on_action)

        # Connect
        connected = await self._connect(cfg)
        if not connected:
            self._schedule_reconnect(cfg)

        api.log("Dante DDM plugin started")

    async def stop(self):
        self._shutting_down = True

        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

        self._connected = False
        self._devices.clear()
        self._state_keys.clear()
        self._domain_id = None
        self._domain_name = None

        if self.api:
            self.api.log("Dante DDM plugin stopped")

    async def health_check(self):
        if self._connected:
            device_count = len(self._devices)
            return {
                "status": "ok",
                "message": f"Connected to {self._domain_name or 'DDM'} ({device_count} devices)",
            }
        return {"status": "error", "message": "Disconnected from DDM"}

    # ──── Connection ────

    async def _connect(self, cfg: dict) -> bool:
        """Establish connection to DDM GraphQL API. Returns True on success."""
        try:
            import httpx
        except ImportError:
            self.api.log(
                "httpx not installed. Install with: pip install httpx",
                level="error",
            )
            return False

        url = cfg.get("ddm_url", "").strip()
        api_key = cfg.get("api_key", "").strip()

        if not url or not api_key:
            self.api.log(
                "DDM URL and API key are required. Configure in plugin settings.",
                level="error",
            )
            return False

        verify = cfg.get("verify_tls", True)

        try:
            self._client = httpx.AsyncClient(
                base_url=url.rstrip("/").rsplit("/graphql", 1)[0],
                headers={
                    "Authorization": api_key,
                    "Content-Type": "application/json",
                },
                verify=verify,
                timeout=10.0,
            )

            # Test connection by fetching domains
            domains = await self._fetch_domains()
            if domains is None:
                raise ConnectionError("Failed to fetch domains")

            # Select domain
            domain_id = cfg.get("domain_id", "").strip()
            if domain_id:
                self._domain_id = domain_id
                # Find name from list
                for d in domains:
                    if d["id"] == domain_id:
                        self._domain_name = d["name"]
                        break
                else:
                    self._domain_name = domain_id
            elif domains:
                self._domain_id = domains[0]["id"]
                self._domain_name = domains[0]["name"]
                self.api.log(
                    f"Auto-selected domain: {self._domain_name} ({self._domain_id})"
                )
            else:
                self.api.log("No Dante domains found", level="error")
                if self._client:
                    await self._client.aclose()
                    self._client = None
                return False

            self._connected = True
            await self.api.state_set("connected", True)
            await self.api.state_set("domain_name", self._domain_name or "")
            await self.api.event_emit("connected")

            # Start polling
            interval = cfg.get("poll_interval", 5)
            self._poll_task = self.api.create_task(
                self._poll_loop(interval), name="dante_poll"
            )

            self.api.log(
                f"Connected to DDM, domain: {self._domain_name}"
            )
            return True

        except Exception as e:
            self.api.log(f"Failed to connect to DDM: {e}", level="error")
            await self.api.state_set("connected", False)
            if self._client:
                try:
                    await self._client.aclose()
                except Exception:
                    pass
                self._client = None
            return False

    def _schedule_reconnect(self, cfg: dict):
        """Schedule reconnection with exponential backoff."""
        if self._shutting_down:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return  # Already running

        async def _reconnect():
            delay = 5
            max_delay = 60
            try:
                while not self._shutting_down:
                    await asyncio.sleep(delay)
                    if self._connected or self._shutting_down:
                        return
                    self.api.log("Reconnecting to DDM...", level="debug")
                    try:
                        if await self._connect(cfg):
                            return
                    except Exception as e:
                        self.api.log(f"Reconnect failed: {e}", level="warning")
                    delay = min(delay * 2, max_delay)
            finally:
                self._reconnect_task = None

        self._reconnect_task = self.api.create_task(
            _reconnect(), name="dante_reconnect"
        )

    # ──── GraphQL Client ────

    async def _graphql(
        self, query: str, variables: dict | None = None
    ) -> dict | None:
        """Execute a GraphQL query/mutation.

        Returns data dict on success, None on GraphQL-level errors.
        Raises on network/HTTP errors (caller decides how to handle).
        """
        if not self._client:
            return None

        body = {"query": query}
        if variables:
            body["variables"] = variables

        resp = await self._client.post("/graphql", json=body)
        resp.raise_for_status()
        result = resp.json()

        if "errors" in result:
            errors = result["errors"]
            msg = errors[0].get("message", str(errors[0])) if errors else "Unknown"
            self.api.log(f"GraphQL error: {msg}", level="error")
            return None

        return result.get("data")

    async def _fetch_domains(self) -> list[dict] | None:
        """Fetch available Dante domains."""
        data = await self._graphql(_QUERY_DOMAINS)
        if data is None:
            return None
        return data.get("domains", [])

    async def _fetch_domain(self) -> dict | None:
        """Fetch full domain data (devices, channels, subscriptions)."""
        if not self._domain_id:
            return None
        data = await self._graphql(
            _QUERY_DOMAIN, {"domainIDInput": self._domain_id}
        )
        if data is None:
            return None
        return data.get("domain")

    # ──── Polling ────

    async def _poll_loop(self, interval: int):
        """Periodically poll DDM for device and routing state."""
        consecutive_failures = 0
        max_failures = 3

        while not self._shutting_down:
            try:
                await self._refresh_state()
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                level = "error" if consecutive_failures >= max_failures else "warning"
                self.api.log(
                    f"Poll error ({consecutive_failures}/{max_failures}): {e}",
                    level=level,
                )
                if consecutive_failures >= max_failures:
                    if self._connected:
                        self._connected = False
                        await self.api.state_set("connected", False)
                        await self.api.event_emit("disconnected")
                        self._schedule_reconnect(self.api.config)
                        return

            await asyncio.sleep(interval)

    async def _refresh_state(self):
        """Read the domain, publish every channel's state, clear the keys of
        channels gone, and announce a change in the device list.

        Raises on network errors (handled by _poll_loop). A GraphQL error,
        already logged, is not a lost connection: nothing changes.
        """
        domain = await self._fetch_domain()
        if domain is None:
            return

        devices = domain.get("devices") or []
        old_ids = set(self._devices)
        self._devices = {d["id"]: d for d in devices}

        published: set[str] = set()
        for key, value in channel_keys.channel_states(devices):
            await self.api.state_set(key, value)
            published.add(key)
        for key in self._state_keys - published:
            await self.api.state_set(key, None)
        self._state_keys = published

        await self.api.state_set("device_count", len(devices))
        await self.api.state_set(
            "subscription_count", channel_keys.routed_count(devices)
        )

        if old_ids != set(self._devices):
            await self.api.event_emit(
                "devices.updated",
                {"device_count": len(devices)},
            )

    # ──── Routing Mutations ────

    async def _set_subscription(
        self,
        rx_device_id: str,
        rx_channel_index: int,
        tx_device_name: str,
        tx_channel_name: str,
    ) -> bool:
        """Create or update an audio subscription on an Rx channel.

        To unsubscribe, pass empty strings for tx_device_name and tx_channel_name.
        """
        try:
            data = await self._graphql(
                _MUTATION_SET_SUBSCRIPTIONS,
                {
                    "input": {
                        "deviceId": rx_device_id,
                        "subscriptions": [
                            {
                                "rxChannelIndex": rx_channel_index,
                                "subscribedDevice": tx_device_name,
                                "subscribedChannel": tx_channel_name,
                            }
                        ],
                        "allowSubscriptionToNonExistentDevice": False,
                        "allowSubscriptionToNonExistentChannel": False,
                    }
                },
            )
        except Exception as e:
            self.api.log(f"Subscription request failed: {e}", level="error")
            return False

        if data is None:
            return False

        result = data.get("DeviceRxChannelsSubscriptionSet", {})
        return result.get("ok", False)

    async def _set_subscriptions_batch(
        self, rx_device_id: str, subscriptions: list[dict]
    ) -> bool:
        """Set multiple subscriptions on a single Rx device in one mutation.

        Each subscription dict: {rxChannelIndex, subscribedDevice, subscribedChannel}
        """
        try:
            data = await self._graphql(
                _MUTATION_SET_SUBSCRIPTIONS,
                {
                    "input": {
                        "deviceId": rx_device_id,
                        "subscriptions": subscriptions,
                        "allowSubscriptionToNonExistentDevice": True,
                        "allowSubscriptionToNonExistentChannel": True,
                    }
                },
            )
        except Exception as e:
            self.api.log(f"Batch subscription request failed: {e}", level="error")
            return False

        if data is None:
            return False

        result = data.get("DeviceRxChannelsSubscriptionSet", {})
        return result.get("ok", False)

    async def route(
        self,
        rx_device_name: str,
        rx_channel_index: int,
        tx_device_name: str,
        tx_channel_name: str,
    ) -> bool:
        """Route a Tx channel to an Rx channel by name."""
        rx_device = self._find_device_by_name(rx_device_name)
        if not rx_device:
            self.api.log(
                f"Rx device not found: {rx_device_name}", level="error"
            )
            await self.api.event_emit(
                "route.failed",
                {
                    "rx_device": rx_device_name,
                    "rx_channel_index": rx_channel_index,
                    "reason": "rx_device_not_found",
                },
            )
            return False

        success = await self._set_subscription(
            rx_device["id"], rx_channel_index, tx_device_name, tx_channel_name
        )

        if success:
            await self.api.event_emit(
                "route.changed",
                {
                    "rx_device": rx_device_name,
                    "rx_channel_index": rx_channel_index,
                    "tx_device": tx_device_name,
                    "tx_channel": tx_channel_name,
                    "active": True,
                },
            )
            self.api.log(
                f"Routed {tx_device_name}/{tx_channel_name} -> "
                f"{rx_device_name}/{rx_channel_index}"
            )
            await self._set_preset_dirty(True)
        else:
            await self.api.event_emit(
                "route.failed",
                {
                    "rx_device": rx_device_name,
                    "rx_channel_index": rx_channel_index,
                    "tx_device": tx_device_name,
                    "tx_channel": tx_channel_name,
                    "reason": "mutation_failed",
                },
            )
            self.api.log(
                f"Failed to route {tx_device_name}/{tx_channel_name} -> "
                f"{rx_device_name}/{rx_channel_index}",
                level="error",
            )

        return success

    async def unroute(
        self, rx_device_name: str, rx_channel_index: int
    ) -> bool:
        """Remove a subscription from an Rx channel."""
        rx_device = self._find_device_by_name(rx_device_name)
        if not rx_device:
            self.api.log(
                f"Rx device not found: {rx_device_name}", level="error"
            )
            return False

        success = await self._set_subscription(
            rx_device["id"], rx_channel_index, "", ""
        )

        if success:
            await self.api.event_emit(
                "route.changed",
                {
                    "rx_device": rx_device_name,
                    "rx_channel_index": rx_channel_index,
                    "tx_device": "",
                    "tx_channel": "",
                    "active": False,
                },
            )
            self.api.log(
                f"Unrouted {rx_device_name}/{rx_channel_index}"
            )
            await self._set_preset_dirty(True)

        return success

    # ──── Presets ────
    #
    # Which preset is showing: active_preset names the preset the routing was
    # last recalled from or saved as ("" for none), and preset_dirty turns true
    # when a route changes after that. The matrix toolbar shows both.

    async def _show_preset(self, name: str):
        """Show this preset ("" for none) as the routing in force, unchanged."""
        await self.api.state_set("active_preset", name)
        await self._set_preset_dirty(False)

    async def _set_preset_dirty(self, dirty: bool):
        await self.api.state_set("preset_dirty", dirty)

    def _snapshot_current_routes(self) -> list[dict]:
        """The domain's routes now, as a preset holds them."""
        return routing.snapshot(self._devices.values())

    def _parse_presets(self) -> dict[str, list[dict]]:
        """The presets saved in the config, by name."""
        return routing.stored_presets(self.api.config)

    async def recall_preset(self, preset_name: str) -> bool:
        """Apply a named routing preset.

        Clears all existing subscriptions first, then applies the preset's
        routes, so the routing matches the preset exactly. A preset with no
        routes only clears, and is not shown as the preset in force.
        """
        presets = self._parse_presets()
        if preset_name not in presets:
            self.api.log(f"Preset not found: {preset_name}", level="error")
            return False

        routes = presets[preset_name]

        clear_ok = await self._clear_all_subscriptions()
        if not clear_ok:
            self.api.log(
                "Warning: failed to clear some subscriptions before preset recall",
                level="warning",
            )

        if not routes:
            await self.api.event_emit(
                "preset.recalled", {"preset_name": preset_name}
            )
            self.api.log(f"Recalled preset: {preset_name} (cleared all)")
            return clear_ok

        if await self._send_preset_routes(routes):
            await self.api.event_emit(
                "preset.recalled", {"preset_name": preset_name}
            )
            await self._show_preset(preset_name)
            self.api.log(f"Recalled preset: {preset_name}")
            return True

        await self.api.event_emit(
            "route.failed",
            {"reason": "preset_partial_failure", "preset_name": preset_name},
        )
        return False

    async def _send_preset_routes(self, routes: list[dict]) -> bool:
        """Send each receiving device its preset routes in one request. False
        when a device is not in the domain or refuses; the rest are still sent."""
        all_ok = True
        for rx_name, device_routes in routing.by_receiver(routes).items():
            rx_device = self._find_device_by_name(rx_name)
            if not rx_device:
                self.api.log(
                    f"Preset: Rx device not found: {rx_name}", level="error"
                )
                all_ok = False
                continue

            subs = routing.subscriptions(device_routes)
            if not await self._set_subscriptions_batch(rx_device["id"], subs):
                self.api.log(
                    f"Preset: Failed to set routes on {rx_name}",
                    level="error",
                )
                all_ok = False
        return all_ok

    async def _clear_all_subscriptions(self) -> bool:
        """Unroute every routed receive channel in the domain, one request per
        device. When every request succeeds, no preset is shown."""
        all_ok = True
        for device_id, subs in routing.clear_requests(self._devices.values()):
            if not await self._set_subscriptions_batch(device_id, subs):
                all_ok = False

        if all_ok:
            await self._show_preset("")
            self.api.log("Cleared all subscriptions")
        return all_ok

    async def _handle_save_preset(self, payload: dict):
        """Save the routing now as a new preset, and show it."""
        name, presets = self._preset_to_edit(payload, "Save", must_exist=False)
        if presets is None:
            return
        routes = self._snapshot_current_routes()
        presets[name] = routes
        await self._save_presets(presets)
        await self._show_preset(name)
        self.api.log(f"Saved preset: {name} ({len(routes)} routes)")

    async def _handle_update_preset(self, payload: dict):
        """Replace a preset's routes with the routing now."""
        name, presets = self._preset_to_edit(payload, "Update", must_exist=True)
        if presets is None:
            return
        routes = self._snapshot_current_routes()
        presets[name] = routes
        await self._save_presets(presets)
        await self._set_preset_dirty(False)
        self.api.log(f"Updated preset: {name} ({len(routes)} routes)")

    async def _handle_delete_preset(self, payload: dict):
        """Delete a preset, and show none."""
        name, presets = self._preset_to_edit(payload, "Delete", must_exist=True)
        if presets is None:
            return
        del presets[name]
        await self._save_presets(presets)
        await self._show_preset("")
        self.api.log(f"Deleted preset: {name}")

    def _preset_to_edit(
        self, payload: dict, verb: str, must_exist: bool
    ) -> tuple[str, dict | None]:
        """The preset a save, update or delete names and a copy of the presets
        to change; (name, None), the refusal logged, when it cannot be done."""
        presets = dict(self._parse_presets())
        name, refusal = routing.checked_name(payload, presets, verb, must_exist)
        if refusal:
            self.api.log(refusal, level="error")
            return name, None
        return name, presets

    async def _save_presets(self, presets: dict):
        await self.api.save_config(routing.with_presets(self.api.config, presets))

    # ──── Event Handlers ────

    async def _on_action(self, event_name: str, payload: dict | None):
        """Handle context action events from the IDE and programmatic events."""
        action = event_name.rsplit(".", 1)[-1] if "." in event_name else event_name

        if action == "refresh_domains":
            await self._handle_refresh_domains()
        elif action == "recall_preset":
            preset_name = (payload or {}).get("preset_name", "")
            if not preset_name:
                # List available presets in log
                presets = self._parse_presets()
                if presets:
                    names = ", ".join(presets.keys())
                    self.api.log(f"Available presets: {names}")
                else:
                    self.api.log("No presets configured", level="warning")
                return
            await self.recall_preset(preset_name)
        elif action == "save_preset":
            await self._handle_save_preset(payload or {})
        elif action == "update_preset":
            await self._handle_update_preset(payload or {})
        elif action == "delete_preset":
            await self._handle_delete_preset(payload or {})
        elif action == "clear_all":
            await self._clear_all_subscriptions()
        elif action == "route":
            await self._handle_route_action(payload or {})
        elif action == "unroute":
            await self._handle_unroute_action(payload or {})

    async def _handle_refresh_domains(self):
        """Refresh the domain list and log results."""
        try:
            domains = await self._fetch_domains()
        except Exception as e:
            self.api.log(f"Failed to fetch domains: {e}", level="error")
            return
        if domains is None:
            self.api.log("Failed to fetch domains", level="error")
            return
        if not domains:
            self.api.log("No domains found")
            return
        for d in domains:
            self.api.log(f"Domain: {d['name']} (ID: {d['id']})")

    async def _handle_route_action(self, payload: dict):
        """Handle a route request from either matrix click or programmatic event.

        Matrix clicks send {row, col} with sanitized state key segments.
        Programmatic events send {rx_device, rx_channel_index, tx_device_name,
        tx_channel_name}.
        """
        if "row" in payload and "col" in payload:
            # Matrix cell click
            row = payload["row"]
            col = payload["col"]
            rx_device_name, rx_channel_index = self._resolve_matrix_ref(row, "rx")
            tx_device_name, tx_channel_name = self._resolve_matrix_ref(col, "tx")
            if not rx_device_name or not tx_device_name:
                self.api.log(
                    f"Could not resolve matrix reference: row={row} col={col}",
                    level="error",
                )
                return
            await self.route(
                rx_device_name, rx_channel_index, tx_device_name, tx_channel_name
            )
        else:
            # Programmatic event
            rx_device = payload.get("rx_device", "")
            rx_channel_index = payload.get("rx_channel_index", 0)
            tx_device_name = payload.get("tx_device_name", "")
            tx_channel_name = payload.get("tx_channel_name", "")

            if not all([rx_device, rx_channel_index, tx_device_name, tx_channel_name]):
                self.api.log("Route action missing required fields", level="error")
                return

            await self.route(
                rx_device, rx_channel_index, tx_device_name, tx_channel_name
            )

    async def _handle_unroute_action(self, payload: dict):
        """Handle an unroute request from either matrix click or programmatic event."""
        if "row" in payload:
            # Matrix cell click
            row = payload["row"]
            rx_device_name, rx_channel_index = self._resolve_matrix_ref(row, "rx")
            if not rx_device_name:
                self.api.log(
                    f"Could not resolve matrix reference: row={row}",
                    level="error",
                )
                return
            await self.unroute(rx_device_name, rx_channel_index)
        else:
            # Programmatic event
            rx_device = payload.get("rx_device", "")
            rx_channel_index = payload.get("rx_channel_index", 0)

            if not all([rx_device, rx_channel_index]):
                self.api.log("Unroute action missing required fields", level="error")
                return

            await self.unroute(rx_device, rx_channel_index)

    # ──── Helpers ────

    def _find_device_by_name(self, name: str) -> dict | None:
        """Find a device by its Dante name."""
        for device in self._devices.values():
            if device["name"] == name:
                return device
        return None

    def _resolve_matrix_ref(
        self, ref: str, direction: str
    ) -> tuple[str | None, int | str | None]:
        """The device name and channel a matrix row ("rx": the index) or
        column ("tx": the channel's name) names; channel_keys.resolve_ref."""
        return channel_keys.resolve_ref(self._devices.values(), ref, direction)
