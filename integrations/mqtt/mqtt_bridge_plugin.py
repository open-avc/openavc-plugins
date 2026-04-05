"""
MQTT Bridge Plugin for OpenAVC

Bridges OpenAVC to an MQTT broker. Outbound: publish state changes to MQTT.
Inbound: set variables, send device commands, or execute macros from MQTT messages.

Requires: gmqtt (MIT license, pure Python, native asyncio)
"""

import asyncio
import json
import ssl
import logging

logger = logging.getLogger(__name__)

PLUGIN_INFO = {
    "id": "mqtt",
    "name": "MQTT Bridge",
    "version": "1.3.0",
    "author": "OpenAVC",
    "description": "Bridge OpenAVC state to and from an MQTT broker for BMS, IoT, and Home Assistant integration.",
    "category": "integration",
    "license": "MIT",
    "platforms": ["all"],
    "min_openavc_version": "0.3.0",
    "capabilities": [
        "state_read",
        "state_write",
        "event_emit",
        "event_subscribe",
        "device_command",
        "macro_execute",
    ],
    "dependencies": ["gmqtt>=0.6.0"],
}

CONFIG_SCHEMA = {
    "broker_host": {
        "type": "string",
        "label": "Broker host",
        "description": "MQTT broker hostname or IP address.",
        "default": "localhost",
        "required": True,
        "placeholder": "mqtt.example.com",
    },
    "broker_port": {
        "type": "integer",
        "label": "Broker port",
        "description": "Default: 1883 (plain) or 8883 (TLS).",
        "default": 1883,
        "min": 1,
        "max": 65535,
    },
    "username": {
        "type": "string",
        "label": "Username",
        "description": "Leave blank for anonymous connections.",
        "default": "",
        "placeholder": "optional",
    },
    "password": {
        "type": "string",
        "label": "Password",
        "description": "Broker password.",
        "default": "",
        "placeholder": "optional",
    },
    "use_tls": {
        "type": "boolean",
        "label": "Use TLS",
        "description": "Connect to the broker over TLS/SSL.",
        "default": False,
    },
    "client_id": {
        "type": "string",
        "label": "Client ID",
        "description": "MQTT client identifier. Leave blank for auto-generated.",
        "default": "",
        "placeholder": "openavc",
    },
    "topic_prefix": {
        "type": "string",
        "label": "Topic prefix",
        "description": "Prefix prepended to auto-generated MQTT topics.",
        "default": "openavc",
        "placeholder": "openavc",
    },
    "default_qos": {
        "type": "select",
        "label": "Default QoS",
        "description": "Quality of Service level for published messages.",
        "default": "0",
        "options": [
            {"value": "0", "label": "0 - At most once"},
            {"value": "1", "label": "1 - At least once"},
            {"value": "2", "label": "2 - Exactly once"},
        ],
    },
    "default_retain": {
        "type": "boolean",
        "label": "Retain messages",
        "description": "Published messages are retained by the broker.",
        "default": True,
    },
    "outbound_mappings": {
        "type": "mapping_list",
        "label": "Outbound (OpenAVC \u2192 MQTT)",
        "description": "Publish OpenAVC state changes to MQTT topics.",
        "item_schema": {
            "state_key": {
                "type": "state_key",
                "label": "State Key",
                "required": True,
                "placeholder": "device.projector.power",
            },
            "topic": {
                "type": "string",
                "label": "Topic Override",
                "placeholder": "auto from state key",
            },
        },
    },
    "inbound_mappings": {
        "type": "mapping_list",
        "label": "Inbound (MQTT \u2192 OpenAVC)",
        "description": "Control OpenAVC from incoming MQTT messages.",
        "item_schema": {
            "topic": {
                "type": "string",
                "label": "MQTT Topic",
                "required": True,
                "placeholder": "sensors/room/temp",
            },
            "action": {
                "type": "select",
                "label": "Action",
                "options": [
                    {"value": "set_variable", "label": "Set Variable"},
                    {"value": "device_command", "label": "Device Command"},
                    {"value": "run_macro", "label": "Run Macro"},
                ],
                "default": "set_variable",
            },
            "variable_id": {
                "type": "state_key",
                "label": "Variable",
                "placeholder": "Select variable...",
                "visible_when": {"action": "set_variable"},
            },
            "device_id": {
                "type": "device_ref",
                "label": "Device",
                "visible_when": {"action": "device_command"},
            },
            "command": {
                "type": "command_ref",
                "label": "Command",
                "device_field": "device_id",
                "visible_when": {"action": "device_command"},
            },
            "param_name": {
                "type": "string",
                "label": "Parameter",
                "placeholder": "optional",
                "visible_when": {"action": "device_command"},
            },
            "macro_id": {
                "type": "macro_ref",
                "label": "Macro",
                "visible_when": {"action": "run_macro"},
            },
        },
    },
}

EXTENSIONS = {
    "status_cards": [
        {
            "id": "mqtt_status",
            "label": "MQTT Bridge",
            "icon": "radio",
            "metrics": [
                {
                    "key": "plugin.mqtt.connected",
                    "label": "Connected",
                    "format": "boolean",
                },
                {
                    "key": "plugin.mqtt.broker",
                    "label": "Broker",
                    "format": "string",
                },
                {
                    "key": "plugin.mqtt.mapping_count",
                    "label": "Mappings",
                    "format": "number",
                },
            ],
        }
    ],
}

AI_GUIDE = """
The MQTT Bridge plugin connects OpenAVC to an MQTT broker.

## Configuration

Connection settings (broker_host, broker_port, username, password, use_tls) are set
in the plugin config. The topic_prefix is prepended to auto-generated outbound topics.

## Outbound Mappings (OpenAVC -> MQTT)

Publishes OpenAVC state changes to MQTT topics. Each row has:
- State Key: pick the state key to publish (e.g. device.projector.power)
- Topic Override: optional custom MQTT topic (leave blank to auto-generate from state key)

Auto-generated topics replace dots with slashes and prepend the prefix:
  device.projector.power -> openavc/device/projector/power

## Inbound Mappings (MQTT -> OpenAVC)

Controls OpenAVC from incoming MQTT messages. Each row has an MQTT topic and an
action type:

- **Set Variable**: writes the MQTT payload value to a user variable (var.<id>)
- **Device Command**: sends a command to a device, optionally passing the MQTT
  payload as a parameter
- **Run Macro**: executes a macro when any message arrives on the topic

## MQTT Value Parsing

Inbound message payloads are parsed automatically:
- "true"/"false"/"on"/"off" -> boolean
- Numeric strings -> int or float
- JSON primitives -> parsed value
- Everything else -> string

## State Keys

- plugin.mqtt.connected — Connected to broker (boolean)
- plugin.mqtt.broker — Broker address (string)
- plugin.mqtt.mapping_count — Total mapping count (integer)

## Events

- plugin.mqtt.connected — Broker connection established
- plugin.mqtt.disconnected — Broker connection lost
- plugin.mqtt.message.received — Inbound message processed
"""


def _state_key_to_topic(prefix: str, state_key: str) -> str:
    """Convert a state key to an MQTT topic. Dots become slashes."""
    topic = state_key.replace(".", "/")
    if prefix:
        return f"{prefix}/{topic}"
    return topic


def _parse_mqtt_value(payload: str):
    """Parse an MQTT payload string into a Python primitive."""
    if not payload:
        return None
    low = payload.lower()
    if low in ("true", "on", "1"):
        return True
    if low in ("false", "off", "0"):
        return False
    # Try numeric
    try:
        if "." in payload:
            return float(payload)
        return int(payload)
    except ValueError:
        pass
    # Try JSON
    try:
        val = json.loads(payload)
        if isinstance(val, (str, int, float, bool, type(None))):
            return val
    except (json.JSONDecodeError, ValueError):
        pass
    return payload


class MQTTBridgePlugin:
    PLUGIN_INFO = PLUGIN_INFO
    CONFIG_SCHEMA = CONFIG_SCHEMA
    EXTENSIONS = EXTENSIONS
    AI_GUIDE = AI_GUIDE

    def __init__(self):
        self.api = None
        self._client = None
        # Outbound: state_key -> topic
        self._outbound_keys: dict[str, str] = {}
        # Inbound: topic -> mapping dict (action, variable_id, device_id, etc.)
        self._inbound_mappings: dict[str, dict] = {}
        self._suppress_echo: set[str] = set()
        self._reconnect_task = None

    async def start(self, api):
        self.api = api
        cfg = api.config
        prefix = cfg.get("topic_prefix", "openavc")

        # Parse outbound mappings
        for m in cfg.get("outbound_mappings", []):
            key = m.get("state_key", "").strip()
            if not key:
                continue
            override = (m.get("topic") or "").strip()
            topic = override if override else _state_key_to_topic(prefix, key)
            self._outbound_keys[key] = topic

        # Parse inbound mappings
        for m in cfg.get("inbound_mappings", []):
            topic = m.get("topic", "").strip()
            action = m.get("action", "")
            if not topic or not action:
                continue
            self._inbound_mappings[topic] = m

        total = len(self._outbound_keys) + len(self._inbound_mappings)

        # Set initial state
        await api.state_set("connected", False)
        await api.state_set("broker", f"{cfg.get('broker_host', 'localhost')}:{cfg.get('broker_port', 1883)}")
        await api.state_set("mapping_count", total)

        # Connect to broker
        await self._connect(cfg)

        # Subscribe to outbound state changes
        if self._outbound_keys:
            await api.state_subscribe("*", self._on_state_change)

        api.log(f"MQTT Bridge started ({len(self._outbound_keys)} outbound, {len(self._inbound_mappings)} inbound)")

    async def stop(self):
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

        self._outbound_keys.clear()
        self._inbound_mappings.clear()
        self._suppress_echo.clear()

        if self.api:
            self.api.log("MQTT Bridge stopped")

    async def health_check(self):
        if self._client and self._client.is_connected:
            return {"status": "ok", "message": "Connected to broker"}
        return {"status": "error", "message": "Disconnected from broker"}

    async def _connect(self, cfg: dict):
        """Connect to the MQTT broker."""
        try:
            from gmqtt import Client as MQTTClient
        except ImportError:
            self.api.log("gmqtt not installed. Install it with: pip install gmqtt", level="error")
            return

        client_id = cfg.get("client_id", "") or f"openavc-{PLUGIN_INFO['id']}"
        self._client = MQTTClient(client_id)

        # Auth
        username = cfg.get("username", "")
        password = cfg.get("password", "")
        if username:
            self._client.set_auth_credentials(username, password or None)

        # Callbacks
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        # TLS
        ssl_ctx = None
        if cfg.get("use_tls", False):
            ssl_ctx = ssl.create_default_context()

        host = cfg.get("broker_host", "localhost")
        port = cfg.get("broker_port", 1883)

        try:
            await self._client.connect(host, port, ssl=ssl_ctx)
        except Exception as e:
            self.api.log(f"Failed to connect to {host}:{port}: {e}", level="error")
            await self.api.state_set("connected", False)
            self._schedule_reconnect(cfg)

    def _on_connect(self, client, flags, rc, properties):
        """Called when connected to the broker."""
        asyncio.ensure_future(self._handle_connect(rc))

    async def _handle_connect(self, rc):
        if rc != 0:
            self.api.log(f"Broker rejected connection (code {rc})", level="error")
            await self.api.state_set("connected", False)
            return

        await self.api.state_set("connected", True)
        await self.api.event_emit("connected")
        self.api.log("Connected to MQTT broker")

        # Subscribe to inbound topics
        qos = int(self.api.config.get("default_qos", "0"))
        for topic in self._inbound_mappings:
            self._client.subscribe(topic, qos=qos)
            self.api.log(f"Subscribed to {topic}", level="debug")

        # Publish current state for all outbound mappings
        for key, topic in self._outbound_keys.items():
            value = await self.api.state_get(key)
            if value is not None:
                self._publish(topic, value)

    def _on_message(self, client, topic, payload, qos, properties):
        """Called when a message is received from the broker."""
        asyncio.ensure_future(self._handle_message(topic, payload))
        return 0  # gmqtt requires returning 0

    async def _handle_message(self, topic: str, payload: bytes):
        text = payload.decode("utf-8", errors="replace") if payload else ""
        mapping = self._inbound_mappings.get(topic)
        if not mapping:
            return

        value = _parse_mqtt_value(text)
        action = mapping.get("action", "")

        try:
            if action == "set_variable":
                var_id = mapping.get("variable_id", "").strip()
                if var_id:
                    await self.api.variable_set(var_id, value)
                    self.api.log(f"Set var.{var_id} = {value}", level="debug")

            elif action == "device_command":
                device_id = mapping.get("device_id", "").strip()
                command = mapping.get("command", "").strip()
                if device_id and command:
                    param_name = mapping.get("param_name", "").strip()
                    params = {param_name: value} if param_name and value is not None else None
                    await self.api.device_command(device_id, command, params)
                    self.api.log(
                        f"Sent {device_id}.{command}({params or ''})",
                        level="debug",
                    )

            elif action == "run_macro":
                macro_id = mapping.get("macro_id", "").strip()
                if macro_id:
                    await self.api.macro_execute(macro_id)
                    self.api.log(f"Executed macro {macro_id}", level="debug")

        except Exception as e:
            self.api.log(
                f"Inbound action failed (topic={topic}, action={action}): {e}",
                level="error",
            )

        await self.api.event_emit("message.received", {
            "topic": topic,
            "action": action,
            "value": value,
        })

    def _on_disconnect(self, client, packet, exc=None):
        """Called when disconnected from the broker."""
        asyncio.ensure_future(self._handle_disconnect(exc))

    async def _handle_disconnect(self, exc):
        await self.api.state_set("connected", False)
        await self.api.event_emit("disconnected")
        if exc:
            self.api.log(f"Disconnected from broker: {exc}", level="warning")
        else:
            self.api.log("Disconnected from broker")
        self._schedule_reconnect(self.api.config)

    async def _on_state_change(self, key: str, value, old_value):
        """Called when any OpenAVC state changes. Publish outbound mappings."""
        if key not in self._outbound_keys:
            return
        if key in self._suppress_echo:
            return
        if not self._client or not self._client.is_connected:
            return

        topic = self._outbound_keys.get(key)
        if topic:
            self._publish(topic, value)

    def _publish(self, topic: str, value):
        """Publish a value to an MQTT topic."""
        if not self._client or not self._client.is_connected:
            return
        cfg = self.api.config
        qos = int(cfg.get("default_qos", "0"))
        retain = cfg.get("default_retain", True)
        payload = str(value) if value is not None else ""
        self._client.publish(topic, payload, qos=qos, retain=retain)

    def _schedule_reconnect(self, cfg: dict):
        """Schedule a reconnection attempt after a delay."""
        if self._reconnect_task and not self._reconnect_task.done():
            return

        async def _reconnect():
            delay = 5
            max_delay = 60
            while True:
                await asyncio.sleep(delay)
                if self._client and self._client.is_connected:
                    return
                self.api.log(f"Reconnecting to broker...", level="debug")
                try:
                    await self._connect(cfg)
                    if self._client and self._client.is_connected:
                        return
                except Exception as e:
                    self.api.log(f"Reconnect failed: {e}", level="warning")
                delay = min(delay * 2, max_delay)

        self._reconnect_task = self.api.create_task(_reconnect(), name="mqtt_reconnect")
