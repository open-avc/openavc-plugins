"""
MQTT Bridge Plugin for OpenAVC

Bridges OpenAVC state to/from an MQTT broker. Supports bidirectional state mapping:
inbound (MQTT -> OpenAVC state), outbound (OpenAVC state -> MQTT publish), or both.

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
    "version": "1.0.0",
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
        "description": "Prefix prepended to all MQTT topics. Example: 'openavc/room1' becomes 'openavc/room1/device.projector.power'.",
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
        "description": "Published messages are retained by the broker so new subscribers get the last value immediately.",
        "default": True,
    },
    "mappings": {
        "type": "string",
        "label": "State mappings",
        "description": (
            "One mapping per line: state_key:direction\n"
            "Directions: in (MQTT->OpenAVC), out (OpenAVC->MQTT), both.\n"
            "Example:\n"
            "  device.projector.power:both\n"
            "  var.room_mode:out\n"
            "  device.switcher.input:in\n"
            "Leave blank to bridge nothing (you can add mappings later)."
        ),
        "default": "",
        "placeholder": "device.projector.power:both\nvar.room_mode:out",
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
The MQTT Bridge plugin connects OpenAVC state to an MQTT broker.

## Configuration

Connection settings (broker_host, broker_port, username, password, use_tls) are set
in the plugin config. The topic_prefix is prepended to all MQTT topics.

## State Mappings

Mappings are configured in the "mappings" config field, one per line:
  state_key:direction

Directions:
- "in"   — Subscribe to MQTT topic, write value to OpenAVC state
- "out"  — Watch OpenAVC state, publish changes to MQTT
- "both" — Bidirectional sync

The MQTT topic is derived from the state key by replacing dots with slashes
and prepending the topic prefix. For example, with prefix "openavc":
  device.projector.power → openavc/device/projector/power

## Examples

To bridge projector power and room mode:
  device.projector.power:both
  var.room_mode:out

To subscribe to external sensor data:
  plugin.mqtt.temperature:in

## MQTT Topics

Inbound messages are parsed as follows:
- "true"/"false"/"on"/"off" → boolean
- Numeric strings → int or float
- JSON strings → parsed, but only the top-level value is used (must be a primitive)
- Everything else → string

Outbound messages publish the state value as a string.
"""


def _parse_mappings(raw: str) -> list[dict]:
    """Parse the mappings config string into a list of mapping dicts."""
    mappings = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) < 2:
            continue
        # Rejoin in case state key has colons (unlikely but safe)
        direction = parts[-1].strip().lower()
        state_key = ":".join(parts[:-1]).strip()
        if direction not in ("in", "out", "both"):
            continue
        if not state_key:
            continue
        mappings.append({"state_key": state_key, "direction": direction})
    return mappings


def _state_key_to_topic(prefix: str, state_key: str) -> str:
    """Convert a state key to an MQTT topic. Dots become slashes."""
    topic = state_key.replace(".", "/")
    if prefix:
        return f"{prefix}/{topic}"
    return topic


def _topic_to_state_key(prefix: str, topic: str) -> str | None:
    """Convert an MQTT topic back to a state key. Slashes become dots."""
    if prefix:
        expected = prefix + "/"
        if not topic.startswith(expected):
            return None
        topic = topic[len(expected):]
    return topic.replace("/", ".")


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
    def __init__(self):
        self.api = None
        self._client = None
        self._mappings: list[dict] = []
        self._topic_to_key: dict[str, str] = {}
        self._key_to_topic: dict[str, str] = {}
        self._inbound_keys: set[str] = set()
        self._outbound_keys: set[str] = set()
        self._suppress_echo: set[str] = set()  # Prevents echo loops on bidirectional mappings
        self._reconnect_task = None

    async def start(self, api):
        self.api = api
        cfg = api.config

        # Parse mappings
        raw_mappings = cfg.get("mappings", "")
        self._mappings = _parse_mappings(raw_mappings)
        prefix = cfg.get("topic_prefix", "openavc")

        # Build lookup tables
        for m in self._mappings:
            key = m["state_key"]
            topic = _state_key_to_topic(prefix, key)
            direction = m["direction"]

            self._topic_to_key[topic] = key
            self._key_to_topic[key] = topic

            if direction in ("in", "both"):
                self._inbound_keys.add(key)
            if direction in ("out", "both"):
                self._outbound_keys.add(key)

        # Set initial state
        await api.state_set("connected", False)
        await api.state_set("broker", f"{cfg.get('broker_host', 'localhost')}:{cfg.get('broker_port', 1883)}")
        await api.state_set("mapping_count", len(self._mappings))

        # Connect to broker
        await self._connect(cfg)

        # Subscribe to outbound state changes
        if self._outbound_keys:
            await api.state_subscribe("*", self._on_state_change)

        api.log(f"MQTT Bridge started with {len(self._mappings)} mapping(s)")

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

        self._mappings.clear()
        self._topic_to_key.clear()
        self._key_to_topic.clear()
        self._inbound_keys.clear()
        self._outbound_keys.clear()
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
            # Schedule reconnect
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
        for topic, key in self._topic_to_key.items():
            if key in self._inbound_keys:
                self._client.subscribe(topic, qos=qos)
                self.api.log(f"Subscribed to {topic} -> {key}", level="debug")

        # Publish current state for all outbound mappings
        for key in self._outbound_keys:
            value = await self.api.state_get(key)
            if value is not None:
                topic = self._key_to_topic[key]
                self._publish(topic, value)

    def _on_message(self, client, topic, payload, qos, properties):
        """Called when a message is received from the broker."""
        asyncio.ensure_future(self._handle_message(topic, payload))
        return 0  # gmqtt requires returning 0

    async def _handle_message(self, topic: str, payload: bytes):
        text = payload.decode("utf-8", errors="replace") if payload else ""
        state_key = self._topic_to_key.get(topic)
        if not state_key:
            return

        value = _parse_mqtt_value(text)

        # Suppress echo for bidirectional mappings
        if state_key in self._outbound_keys:
            self._suppress_echo.add(state_key)

        try:
            await self.api.state_set(state_key, value)
        except Exception as e:
            self.api.log(f"Failed to set state {state_key}: {e}", level="error")
        finally:
            self._suppress_echo.discard(state_key)

        await self.api.event_emit("message.received", {
            "topic": topic,
            "state_key": state_key,
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
        # gmqtt handles reconnection internally, but if it fails we schedule our own
        self._schedule_reconnect(self.api.config)

    async def _on_state_change(self, key: str, value, old_value):
        """Called when any OpenAVC state changes. Publish outbound mappings."""
        if key not in self._outbound_keys:
            return
        if key in self._suppress_echo:
            return  # This change came from an inbound MQTT message, don't echo it back
        if not self._client or not self._client.is_connected:
            return

        topic = self._key_to_topic.get(key)
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
            return  # Already scheduled

        async def _reconnect():
            delay = 5
            max_delay = 60
            while True:
                await asyncio.sleep(delay)
                if self._client and self._client.is_connected:
                    return
                self.api.log(f"Reconnecting to broker in {delay}s...", level="debug")
                try:
                    await self._connect(cfg)
                    if self._client and self._client.is_connected:
                        return
                except Exception as e:
                    self.api.log(f"Reconnect failed: {e}", level="warning")
                delay = min(delay * 2, max_delay)

        self._reconnect_task = self.api.create_task(_reconnect(), name="mqtt_reconnect")
