"""
MQTT Bridge's start() returns at once, whatever the broker does.

A settings save waits for the plugin to restart, and the restart waits for
start(). When start() waited for the first broker connection, a broker address
that drops packets held every save on "Saving..." until the platform's start
timeout ran out. These tests run start() against a broker that never answers
and one that refuses, with gmqtt replaced by a stand-in, and check that start()
returns at once and that the card and the health check say why the bridge is
not connected.

Run from the openavc-plugins root: pytest tests/test_mqtt_bridge_start.py -v
"""

import asyncio
import sys
import time
import types
from pathlib import Path

import pytest

_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))

from integrations.mqtt import mqtt_bridge_plugin as mqtt_mod  # noqa: E402


class _FakeApi:
    def __init__(self, config):
        self.config = dict(config)
        self.state = {}
        self.tasks = []

    async def state_set(self, key, value):
        self.state[key] = value

    async def state_subscribe(self, pattern, handler):
        pass

    async def event_emit(self, name, payload=None):
        pass

    def log(self, message, level="info"):
        pass

    def create_task(self, coro, name=None):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


def _install_fake_gmqtt(monkeypatch, connect):
    class Client:
        def __init__(self, client_id):
            self.is_connected = False

        def set_auth_credentials(self, username, password):
            pass

        async def connect(self, host, port, ssl=None):
            await connect(host, port)

        async def disconnect(self):
            pass

    module = types.ModuleType("gmqtt")
    module.Client = Client
    monkeypatch.setitem(sys.modules, "gmqtt", module)


async def _never_answers(host, port):
    await asyncio.sleep(3600)


async def _refused(host, port):
    raise ConnectionRefusedError(61, "Connection refused")


_CONFIG = {"broker_host": "192.0.2.50", "broker_port": 1883}


@pytest.mark.asyncio
async def test_start_returns_at_once_when_the_broker_never_answers(monkeypatch):
    _install_fake_gmqtt(monkeypatch, _never_answers)
    monkeypatch.setattr(mqtt_mod, "_CONNECT_TIMEOUT", 0.2)
    plugin = mqtt_mod.MQTTBridgePlugin()
    api = _FakeApi(_CONFIG)

    began = time.monotonic()
    await plugin.start(api)
    assert time.monotonic() - began < 0.1

    await asyncio.sleep(0.4)
    assert api.state["connected"] is False
    assert api.state["last_error"] == (
        "No answer from the broker at 192.0.2.50:1883. "
        "Check the address and that the broker is on."
    )
    health = await plugin.health_check()
    assert health == {"status": "error", "message": api.state["last_error"]}

    await plugin.stop()
    assert all(task.done() for task in api.tasks)


@pytest.mark.asyncio
async def test_a_refused_connection_says_so(monkeypatch):
    _install_fake_gmqtt(monkeypatch, _refused)
    plugin = mqtt_mod.MQTTBridgePlugin()
    api = _FakeApi(_CONFIG)

    await plugin.start(api)
    await asyncio.sleep(0.05)
    assert api.state["last_error"].startswith("Could not connect to 192.0.2.50:1883:")
    await plugin.stop()


@pytest.mark.asyncio
async def test_a_connection_clears_the_error(monkeypatch):
    async def answers(host, port):
        pass

    _install_fake_gmqtt(monkeypatch, answers)
    plugin = mqtt_mod.MQTTBridgePlugin()
    api = _FakeApi(_CONFIG)
    await plugin.start(api)
    await asyncio.sleep(0.05)
    await plugin._set_last_error("The broker refused the connection (code 5).")

    await plugin._handle_connect(0)
    assert api.state["connected"] is True
    assert api.state["last_error"] == ""
    await plugin.stop()


def test_the_card_shows_the_last_error():
    metrics = mqtt_mod.EXTENSIONS["status_cards"][0]["metrics"]
    assert {"key": "plugin.mqtt.last_error", "label": "Last error", "format": "string"} in metrics
