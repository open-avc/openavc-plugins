# OpenAVC Plugin Development Guide for AI Agents

This file is a self-contained reference for LLM-based coding agents helping users create plugins for OpenAVC. It contains the complete Plugin API, manifest format, configuration schema, extension types, lifecycle rules, and examples needed to produce working plugins without reading the full platform source code.

**What is OpenAVC?** An open-source (MIT) AV room control platform that replaces Crestron, Extron, and AMX. Plugins extend the platform with system-wide integrations, control surfaces, sensors, and utility services.

**Repository:** `github.com/open-avc/openavc-plugins`
**Platform source:** `github.com/open-avc/openavc`

---

## Table of Contents

1. [Plugins vs Drivers](#1-plugins-vs-drivers)
2. [Plugin Structure](#2-plugin-structure)
3. [PLUGIN_INFO Manifest](#3-plugin_info-manifest)
4. [CONFIG_SCHEMA](#4-config_schema)
5. [Plugin API Reference](#5-plugin-api-reference)
6. [Plugin Lifecycle](#6-plugin-lifecycle)
7. [Extensions (UI Integration)](#7-extensions-ui-integration)
8. [Macro Actions](#8-macro-actions)
9. [Script API](#9-script-api)
10. [Panel Elements (Custom UI)](#10-panel-elements-custom-ui)
11. [Surface Layout (Control Surfaces)](#11-surface-layout-control-surfaces)
12. [Native Dependencies](#12-native-dependencies)
13. [Testing](#13-testing)
14. [Repository Structure and Naming](#14-repository-structure-and-naming)
15. [index.json Catalog Entry](#15-indexjson-catalog-entry)
16. [Validation](#16-validation)
17. [Complete Examples](#17-complete-examples)
18. [Common Mistakes](#18-common-mistakes)

---

## 1. Plugins vs Drivers

| | Drivers | Plugins |
|---|---------|---------|
| **Scope** | One device, one protocol | System-wide |
| **Purpose** | Translate commands/state for a specific piece of hardware | Add capabilities that span devices, connect external systems, or add physical interfaces |
| **Examples** | PJLink projector, Extron switcher, Samsung display | MQTT bridge, Elgato Stream Deck, Dante DDM, occupancy analytics |

**Rule of thumb:** If it talks to one device over TCP/serial/HTTP, it's a driver. If it bridges systems, adds a control surface, or provides a service, it's a plugin.

For driver creation, see `AGENTS.md` in the `openavc-drivers` repository.

---

## 2. Plugin Structure

Each plugin is a directory containing at minimum a Python file with a class that has `PLUGIN_INFO`:

```
category/
└── my_plugin/
    ├── my_plugin_plugin.py    # Plugin class (naming: <id>_plugin.py)
    ├── plugin.json            # Manifest (mirrors PLUGIN_INFO for the catalog)
    ├── README.md              # Usage documentation
    └── panel/                 # Optional: custom UI files for panel elements
        ├── index.html
        ├── style.css
        └── app.js
```

**File naming:** The main Python file must end in `_plugin.py` (e.g., `mqtt_bridge_plugin.py`). The plugin loader scans for this pattern.

---

## 3. PLUGIN_INFO Manifest

Every plugin class must define `PLUGIN_INFO` as a class-level dict.

### Required Fields

```python
PLUGIN_INFO = {
    "id": "my_plugin",            # Lowercase, underscores only. Immutable after publication.
    "name": "My Plugin",          # Human-readable name
    "version": "0.1.0",           # SemVer format
    "author": "Your Name",        # Author name
    "description": "What it does.", # One-line description
    "category": "utility",        # control_surface | integration | sensor | utility
    "license": "MIT",             # Must be MIT-compatible (see below)
}
```

### Optional Fields

```python
PLUGIN_INFO = {
    # ... required fields ...
    "platforms": ["all"],           # win_x64 | linux_x64 | linux_arm64 | all
    "min_openavc_version": "0.3.0", # Minimum compatible OpenAVC version
    "dependencies": [],             # pip packages (must be MIT-compatible)
    "native_dependencies": [],      # Platform-level SDKs (see section 10)
    "capabilities": [],             # API permissions needed (see below)
}
```

### Capabilities

Declare only the capabilities your plugin actually uses. Each unlocks specific API methods.

| Capability | What It Unlocks |
|------------|----------------|
| `state_read` | `state_get()`, `state_get_pattern()`, `state_subscribe()` |
| `state_write` | `state_set()` (own `plugin.<id>.*` namespace only) |
| `variable_write` | `variable_set()` (writes to `var.*` user variables) |
| `event_emit` | `event_emit()` |
| `event_subscribe` | `event_subscribe()` |
| `macro_execute` | `macro_execute()` |
| `device_command` | `device_command()` |
| `network_listen` | Plugin may open network ports |
| `usb_access` | Plugin may access USB devices |

`state_write` and `variable_write` are independent. `state_write` lets a plugin write its own namespaced state (e.g., `plugin.my_plugin.connected`). `variable_write` lets a plugin write user variables (`var.*`) — shared room-logic state. Most plugins need only `state_write`. Declare `variable_write` only when the plugin explicitly contributes to user-variable state, e.g., a sensor reporting occupancy into `var.room_occupied` or a bridge mirroring an external system.

Calling a method without the required capability raises `PluginPermissionError`.

### MIT-Compatible Licenses

All plugins and their dependencies must use one of these licenses:

`MIT`, `BSD-2-Clause`, `BSD-3-Clause`, `Apache-2.0`, `ISC`, `PSF`, `Unlicense`, `0BSD`, `CC0-1.0`

**No GPL, LGPL, or AGPL.**

### Categories

| Category | Directory | When to Use |
|----------|-----------|-------------|
| `control_surface` | `control_surfaces/` | Physical button panels, fader banks, keypads (Stream Deck, X-Keys, MIDI) |
| `integration` | `integrations/` | Protocol bridges, external platform connections (MQTT, Dante, Home Assistant) |
| `sensor` | `sensors/` | Environmental inputs (occupancy, temperature, ambient light) |
| `utility` | `utility/` | Analytics, logging, voice control bridges |

---

## 4. CONFIG_SCHEMA

Define `CONFIG_SCHEMA` as a class-level dict to declare configurable settings. The Programmer IDE auto-renders a configuration form from this schema.

### Field Types

```python
CONFIG_SCHEMA = {
    # Text input
    "broker_host": {
        "type": "string",
        "label": "Broker Host",
        "description": "MQTT broker hostname or IP",
        "default": "localhost",
        "required": True,
        "pattern": "^[a-zA-Z0-9._-]+$",  # Optional regex validation
    },

    # Integer input
    "broker_port": {
        "type": "integer",
        "label": "Broker Port",
        "default": 1883,
        "min": 1,
        "max": 65535,
        "step": 1,
    },

    # Decimal input
    "threshold": {
        "type": "float",
        "label": "Threshold",
        "default": 0.5,
        "min": 0.0,
        "max": 1.0,
        "step": 0.1,
    },

    # Toggle switch
    "auto_reconnect": {
        "type": "boolean",
        "label": "Auto Reconnect",
        "default": True,
    },

    # Dropdown
    "qos": {
        "type": "select",
        "label": "QoS Level",
        "options": [
            {"value": 0, "label": "0 - At most once"},
            {"value": 1, "label": "1 - At least once"},
            {"value": 2, "label": "2 - Exactly once"},
        ],
        "default": 1,
    },

    # State key picker (autocomplete from live state)
    "watch_key": {
        "type": "state_key",
        "label": "State Key to Watch",
        "default": "",
    },

    # Macro picker (dropdown of project macros)
    "on_trigger_macro": {
        "type": "macro_ref",
        "label": "Trigger Macro",
        "default": "",
    },

    # Device picker (dropdown of project devices)
    "target_device": {
        "type": "device_ref",
        "label": "Target Device",
        "default": "",
    },

    # Collapsible group
    "connection": {
        "type": "group",
        "label": "Connection Settings",
        "fields": {
            "host": {"type": "string", "required": True, "label": "Host"},
            "port": {"type": "integer", "default": 1883, "label": "Port"},
        },
    },

    # Repeatable row list
    "topic_mappings": {
        "type": "mapping_list",
        "label": "Topic Mappings",
        "item_schema": {
            "topic": {"type": "string", "required": True, "label": "MQTT Topic"},
            "state_key": {"type": "state_key", "required": True, "label": "State Key"},
            "direction": {
                "type": "select",
                "options": [
                    {"value": "inbound", "label": "MQTT to OpenAVC"},
                    {"value": "outbound", "label": "OpenAVC to MQTT"},
                    {"value": "bidirectional", "label": "Both"},
                ],
                "default": "inbound",
                "label": "Direction",
            },
        },
    },
}
```

### Setup Dialog

If a field has `required: True` and no `default` value, the IDE shows a **Setup Dialog** when the user first enables the plugin. The user must fill in required fields before the plugin can start.

---

## 5. Plugin API Reference

The `PluginAPI` object is passed to your `start()` method. It is your only interface to the OpenAVC runtime.

**Source reference:** [`server/core/plugin_api.py`](https://github.com/open-avc/openavc/blob/main/server/core/plugin_api.py)

### 5.1 State Methods

```python
# Read any state key (requires: state_read)
value = await api.state_get("device.projector.power")

# Read all keys matching glob pattern (requires: state_read)
devices = await api.state_get_pattern("device.*.power")
# Returns: {"device.proj1.power": "on", "device.proj2.power": "off"}

# Write state in plugin namespace (requires: state_write)
# Auto-prefixed with plugin.<plugin_id>. -- do NOT include the prefix yourself
await api.state_set("status", "connected")
# Actual key: plugin.my_plugin.status

# Write a user variable (requires: variable_write)
# Writes to var.<variable_id> — shared room-logic state
await api.variable_set("room_occupied", True)

# Subscribe to state changes matching glob (requires: state_read)
# Callback signature: (key: str, value: Any, old_value: Any) -> None  (sync or async)
sub_id = await api.state_subscribe("device.*.power", on_power_change)

# Unsubscribe
await api.state_unsubscribe(sub_id)
```

**State value constraints:** Values must be flat primitives only: `str`, `int`, `float`, `bool`, `None`. No lists, dicts, or nested objects.

**State key namespaces:**
```
device.<device_id>.<property>    # Device state (read-only for plugins)
var.<variable_id>                # User variables (writable via variable_set; requires variable_write)
ui.<element_id>.<property>       # UI element state
system.<property>                # System state (uptime, version, etc.)
plugin.<plugin_id>.*             # Plugin namespace (writable via state_set)
```

### 5.2 Event Methods

```python
# Emit event (requires: event_emit)
# Auto-prefixed with plugin.<plugin_id>. -- do NOT include the prefix
await api.event_emit("connected", {"broker": "mqtt.example.com"})
# Actual event: plugin.my_plugin.connected

# Subscribe to events matching glob (requires: event_subscribe)
# Callback signature: (event_name: str, payload: dict) -> None  (sync or async)
handler_id = await api.event_subscribe("device.connected.*", on_device_connected)

# Unsubscribe
await api.event_unsubscribe(handler_id)
```

**Standard events you can subscribe to:**
```
state.changed                    # Any state change
state.changed.<key>              # Specific key changed
device.connected.<device_id>     # Device came online
device.disconnected.<device_id>  # Device went offline
device.error.<device_id>         # Device error
ui.press.<element_id>            # Button pressed
ui.release.<element_id>          # Button released
ui.change.<element_id>           # Slider/select changed
system.started                   # Engine started
system.stopping                  # Engine shutting down
macro.started.<macro_id>         # Macro execution began
macro.completed.<macro_id>       # Macro finished
```

### 5.3 Action Methods

```python
# Execute a macro by ID (requires: macro_execute)
await api.macro_execute("all_off")

# Send a command to a device (requires: device_command)
result = await api.device_command("projector1", "power_on")
result = await api.device_command("switcher1", "set_input", {"input": 3})
```

### 5.4 Background Task Methods

```python
# Create a managed async task (auto-cancelled on stop)
task = api.create_task(my_coroutine(), name="my_worker")

# Create a repeating task (auto-cancelled on stop)
# Calls the function (sync or async) every interval_seconds
task_id = api.create_periodic_task(check_status, interval_seconds=30.0, name="status_check")

# Cancel a periodic task by ID
api.cancel_task(task_id)
```

**Always use `api.create_task()` instead of `asyncio.create_task()`.** The API version tracks tasks for automatic cleanup when the plugin stops.

### 5.5 Configuration

```python
# Read configuration (read-only dict)
config = api.config
broker = api.config.get("broker_host", "localhost")

# Save updated configuration to project file
# Triggers plugin restart if the plugin is running
await api.save_config({"broker_host": "new-broker.example.com", "broker_port": 1883})
```

### 5.6 Identity and Logging

```python
# Plugin ID
plugin_id = api.plugin_id  # "my_plugin"

# Current platform
platform = api.platform  # "win_x64", "linux_x64", or "linux_arm64"

# Log a message (appears in Programmer IDE System Log)
api.log("Connected to broker", level="info")  # level: "info", "warning", "error", "debug"
```

---

## 6. Plugin Lifecycle

### 6.1 Required Methods

```python
class MyPlugin:
    PLUGIN_INFO = { ... }

    async def start(self, api):
        """Called when the plugin is enabled.
        Store the api reference. Begin operation: subscribe to
        events/state, start background tasks, open connections.
        """
        self.api = api
        # ... setup code ...

    async def stop(self):
        """Called when the plugin is disabled or server shuts down.
        Close external connections and release hardware here.
        State keys, subscriptions, and tasks are cleaned up automatically.
        """
        # ... teardown code ...
```

### 6.2 Optional Methods

```python
    async def health_check(self):
        """Called periodically to check plugin health.
        Return a dict with 'status' and 'message'.
        Status: 'ok', 'degraded', or 'error'.
        """
        if self.connected:
            return {"status": "ok", "message": "Connected to broker"}
        return {"status": "error", "message": "Broker connection lost"}
```

### 6.3 Lifecycle Flow

1. **Discovery:** Plugin loader scans `plugin_repo/` at startup, finds `*_plugin.py` files with `PLUGIN_INFO`.
2. **Validation:** `PLUGIN_INFO` checked for required fields, valid capabilities, compatible license, platform support.
3. **Enable:** User enables plugin in IDE. If required config fields lack defaults, a Setup Dialog appears.
4. **Start:** `PluginAPI` created with declared capabilities. `start(api)` called.
5. **Running:** Plugin responds to state changes, events, etc. Config changes trigger restart.
6. **Stop:** `stop()` called. All subscriptions, state keys, and background tasks automatically cleaned up.

### 6.4 Automatic Cleanup

When a plugin stops, the system automatically:
- Removes all state subscriptions
- Removes all event subscriptions
- Deletes all state keys under `plugin.<id>.*`
- Cancels all managed tasks and periodic tasks

You do not need to manually unsubscribe or delete state keys in `stop()`. You only need to close external connections and release hardware.

### 6.5 Callback Failure Protection

If a plugin's callbacks fail 10 consecutive times, the plugin is automatically disabled. The counter resets on any successful callback. Implement proper error handling in callbacks to prevent this.

### 6.6 Hot Reload

Two distinct things get called "reload":

- **Config reload** (user toggles enable, edits config in the IDE, project file changes). The platform calls `stop()`, then instantiates a new plugin from the cached class and calls `start()` with the new config. The plugin file is **not** re-read; code edits do not take effect.
- **Code reload** (server restart, fresh plugin scan). The platform re-executes the plugin file via `importlib`. A new module object and a new class object replace the cached ones. Module-level state (globals, top-level caches) is reset.

Two things to avoid:

- **Don't cache `type(self)` or the plugin class outside the platform's registry.** After a code reload, the registry holds the new class; any external cache still holds the old one. `isinstance(new_instance, OldClass)` returns False, and method lookup against the stale class runs old code.
- **Don't rely on module-level state surviving a code reload.** Counters or caches defined at the top of your plugin file are reset when the file is re-executed. Put long-lived state on the instance (`self.foo`) so each `start()` rebuilds it.

---

## 7. Extensions (UI Integration)

Plugins can extend the Programmer IDE with UI elements by defining an `EXTENSIONS` class attribute.

### 7.1 Status Cards

Compact metric cards shown on the Dashboard.

```python
EXTENSIONS = {
    "status_cards": [
        {
            "id": "mqtt_status",            # Unique within plugin
            "label": "MQTT Bridge",         # Card title
            "icon": "activity",             # Lucide icon name (https://lucide.dev)
            "metrics": [
                {"key": "plugin.mqtt.connected", "label": "Connected", "format": "boolean"},
                {"key": "plugin.mqtt.messages_in", "label": "Messages In", "format": "number"},
                {"key": "plugin.mqtt.messages_out", "label": "Messages Out", "format": "number"},
            ],
        },
    ],
}
```

### 7.2 Views

Top-level sidebar views.

```python
EXTENSIONS = {
    "views": [
        {
            "id": "mqtt_monitor",
            "label": "MQTT Monitor",       # Sidebar label
            "icon": "activity",             # Lucide icon name
            "renderer": "state_table",      # surface | state_table | log
            "state_pattern": "plugin.mqtt.*",  # For state_table renderer
        },
    ],
}
```

### 7.3 Device Panels

Panels shown on device detail pages, scoped to matching devices.

```python
EXTENSIONS = {
    "device_panels": [
        {
            "id": "dante_channels",
            "label": "Dante Audio Channels",
            "icon": "music",
            "match": {
                "driver_id": "dante_*",     # Glob match
                "category": "audio",        # Optional filter
            },
            "renderer": "state_table",
            "state_pattern": "plugin.dante.device.{device_id}.*",
        },
    ],
}
```

### 7.4 Context Actions

Action buttons in toolbars. Clicking emits an event your plugin subscribes to.

```python
EXTENSIONS = {
    "context_actions": [
        {
            "id": "scan_network",
            "label": "Scan Dante Network",
            "icon": "search",
            "context": "global",            # global | device | plugin
            "event": "action.scan_network", # Emits: plugin.<id>.action.scan_network
        },
        {
            "id": "identify",
            "label": "Identify on Dante",
            "icon": "eye",
            "context": "device",
            "match": {"driver_id": "dante_*"},
            "event": "action.identify",     # Payload includes {device_id: "..."}
        },
    ],
}
```

Subscribe to context action events in `start()`:

```python
async def start(self, api):
    self.api = api
    await self.api.event_subscribe("plugin.my_plugin.action.*", self.on_action)

async def on_action(self, event, payload):
    if event.endswith("scan_network"):
        # Handle scan
        pass
```

---

## 8. Macro Actions

Plugins can register new macro action types that show up in the macro builder and run inside the macro engine alongside built-in actions like `device.command` and `state.set`.

Use this for actions that aren't tied to a single device — playing a sound on every panel, sending a notification, posting to Slack, triggering a scene on an external system.

### 8.1 Declaration

Define `MACRO_ACTIONS` as a class-level dict. Each entry maps an action type string to a handler method name plus a parameter schema for the macro builder.

```python
class AudioPlayerPlugin:

    PLUGIN_INFO = {
        "id": "audio_player",
        "name": "Audio Player",
        ...
    }

    MACRO_ACTIONS = {
        "audio_player.play": {
            "label": "Play Sound",
            "description": "Play a sound on every panel with the Audio Player element",
            "icon": "volume-2",                 # Lucide icon name (optional)
            "handler": "action_play",            # method name on this class
            "params": [
                {
                    "key": "sound",
                    "type": "select",
                    "label": "Sound",
                    "required": True,
                    "options_source": "plugin.audio_player.sounds",
                },
                {
                    "key": "volume",
                    "type": "float",
                    "label": "Volume",
                    "default": 1.0,
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.1,
                },
            ],
        },
        "audio_player.stop": {
            "label": "Stop All Sounds",
            "handler": "action_stop",
            "params": [],
        },
    }

    async def action_play(self, params, context):
        sound = params["sound"]
        volume = params.get("volume", 1.0)
        # Plugin already has self.api from start() — write play_request, etc.
        ...

    async def action_stop(self, params, context):
        ...
```

### 8.2 Action Naming

Every action type must be **prefixed with the plugin's id** followed by a dot. The suffix must be lowercase letters, digits, or underscores (`audio_player.play`, `mqtt_bridge.publish`, `slack.send_message`). The validator rejects anything else at load time, which prevents two plugins from claiming the same action name.

### 8.3 Param Schema

Each param entry supports the following fields:

| Field | Required | Notes |
|-------|----------|-------|
| `key` | Yes | The key under `step.params`. Must be unique within the action. |
| `type` | Yes | One of: `text`, `integer`, `float`, `boolean`, `select`, `state_key`, `device_ref`, `macro_ref`. |
| `label` | No | Human-readable label in the macro builder form. |
| `description` | No | Inline help text under the field. |
| `required` | No | Marks the field with a red asterisk. |
| `default` | No | Initial value when a new step is added. |
| `min`, `max`, `step` | No | Numeric input constraints. |
| `options` | For `select` | Array of `{value, label}` for static dropdowns. |
| `options_source` | For `select` | State key that holds a JSON list, populated dynamically by the plugin. Either `options` or `options_source` is required for `select`. |

Field types `text`, `integer`, `float`, and `select` also support **dynamic values**: a user can switch the field into "$var.foo" mode and the macro engine will resolve it from state at runtime before invoking the handler.

### 8.4 Handler Signature

```python
async def handler(self, params: dict, context: dict) -> None:
    ...
```

- `params` is the resolved parameter dict (with `$var.foo` already looked up).
- `context` is the macro execution context (currently used for trigger payloads).
- Handlers must be `async`. The validator rejects plain `def` methods.
- Raise to fail the step. The error message is shown in the macro builder and emitted as a `macro.step_error.<macro_id>` event with the plugin id in the context.

### 8.5 Lifecycle

Actions register automatically when the plugin starts and unregister when it stops. There is no API call to make — the platform reads `MACRO_ACTIONS` from the class. If a project file references an action whose plugin isn't installed or enabled, the macro builder shows a "Missing plugin" warning on the affected steps and the macro fails at that step if run.

### 8.6 No Capability Required

`MACRO_ACTIONS` is purely declarative, like `EXTENSIONS`. The handler runs in the plugin's own code with whatever `self.api` permissions you declared in `capabilities`.

---

## 9. Script API

Plugins can expose methods that user scripts call as `openavc.plugins.<plugin_id>.<method>(...)`. Same shape as the macro action declaration but with a different runtime — scripts call methods like normal Python functions (positional + keyword arguments) instead of dispatching through the macro engine with a uniform params dict.

Use this to give script authors a clean Pythonic interface to plugin behavior. A user writing a script shouldn't have to construct JSON state writes to play a sound; they should be able to write `await plugins.audio_player.play("chime_soft", volume=0.6)`.

### 9.1 Declaration

```python
class AudioPlayerPlugin:

    PLUGIN_INFO = {
        "id": "audio_player",
        "name": "Audio Player",
        ...
    }

    SCRIPT_API = {
        "play": {
            "handler": "script_play",
            "doc": "Play a sound on every panel with the Audio Player element.",
        },
        "stop": {
            "handler": "script_stop",
            "doc": "Stop currently-playing sounds on every panel.",
        },
        "list_sounds": {
            "handler": "script_list_sounds",
            "doc": "Return the list of available sounds.",
            "sync": True,    # handler is `def`, not `async def`
        },
    }

    async def script_play(self, sound: str, volume: float = 1.0) -> None:
        # Reuse the macro action handler; signature differs
        await self.action_play({"sound": sound, "volume": volume}, {})

    async def script_stop(self) -> None:
        await self.action_stop({}, {})

    def script_list_sounds(self) -> list[dict]:
        return list(self._builtin_sounds)
```

### 9.2 Method and Plugin Naming

The plugin id and every method name must be valid Python identifiers (lowercase start, letters/digits/underscores only). Names beginning with underscore are rejected so plugins can't shadow proxy machinery. The validator catches all of this at load time.

If your plugin id has dashes or other illegal-in-Python characters, declaring `SCRIPT_API` will fail validation. Pick an identifier-friendly id — `audio_player`, not `audio-player`.

### 9.3 Async vs Sync Handlers

Default is async. Set `"sync": True` when the handler is a regular `def`. The validator enforces that the flag matches the actual method type — mismatches fail to load with a clear error.

Mixed plugins are normal: most methods async (because they touch state, await macros, etc.), but quick read-only accessors like `list_sounds` can stay sync.

### 9.4 Method Signatures

Pure Python — positional args, keyword args, defaults, type hints all work. No `(params, context)` envelope (that's macro actions). Scripts call them like any other function:

```python
from openavc import plugins, on_event

@on_event("ui.press.lobby_chime")
async def on_press(event):
    await plugins.audio_player.play("chime_soft", volume=0.6)
    await plugins.audio_player.list_sounds()  # sync, no await
```

Raised exceptions propagate to the script and surface in the System Log.

### 9.5 Lifecycle and Capabilities

`SCRIPT_API` is purely declarative. No capability declaration is required to declare methods, and no API call is needed to register them. Methods are picked up at plugin start and removed at plugin stop. They run as the plugin instance, with whatever capabilities you declared in `PLUGIN_INFO`.

When a plugin stops, scripts that still hold a reference to `plugins.<plugin_id>` get an `AttributeError` on subsequent attribute access ("not currently running"). When a plugin isn't installed at all, `plugins.<plugin_id>` raises `AttributeError` ("not installed or not currently running"). Both error messages are explicit so script authors can fix typos and missing-plugin issues quickly.

### 9.6 Sharing Code with Macro Actions

A plugin can expose the same behavior as both a macro action and a script method. The cleanest pattern is to keep the underlying logic in a private helper (or in the macro action handler) and have the script-API method delegate:

```python
async def action_play(self, params: dict, _context: dict) -> None:
    await self._do_play(params["sound"], params.get("volume", 1.0))

async def script_play(self, sound: str, volume: float = 1.0) -> None:
    await self._do_play(sound, volume)

async def _do_play(self, sound: str, volume: float) -> None:
    # actual work here
    ...
```

This keeps the two surfaces in sync without forcing one to call into the other.

---

## 10. Panel Elements (Custom UI)

Plugins can provide custom HTML/CSS/JS UI elements for touch panels via iframes.

### 9.1 Declaration

```python
EXTENSIONS = {
    "panel_elements": [
        {
            "type": "status_display",           # Unique element type name
            "label": "Status Display",          # Shown in Element Palette
            "renderer": "iframe",               # Only "iframe" supported
            "renderer_url": "/plugins/{plugin_id}/panel/index.html",
            "default_size": {"col_span": 3, "row_span": 2},
            "config_schema": [
                {"key": "title", "type": "text", "label": "Title", "default": "Status"},
                {"key": "state_key", "type": "state_key", "label": "State Key", "default": ""},
            ],
        },
    ],
}
```

### 9.2 File Placement

Place panel files in a `panel/` subdirectory within your plugin directory:

```
my_plugin/
├── my_plugin_plugin.py
└── panel/
    ├── index.html
    ├── style.css
    └── app.js
```

### 9.3 Communication API

The iframe communicates with the panel via `postMessage`.

**Messages received by iframe:**

| Type | When | Payload |
|------|------|---------|
| `openavc:init` | Once, on iframe load | `{config, theme, state}` |
| `openavc:state` | On state change | `{key, value}` |
| `openavc:theme` | On theme change | `{variables}` |

**Messages sent by iframe:**

| Type | Purpose | Payload |
|------|---------|---------|
| `openavc:command` | Send device command | `{device, command, params}` |
| `openavc:set_state` | Write state value | `{key, value}` |
| `openavc:navigate` | Navigate to page | `{page}` |

**Example iframe JavaScript:**

```javascript
window.addEventListener("message", (event) => {
    if (event.data.type === "openavc:init") {
        const { config, theme, state } = event.data;
        // Initialize with current config, theme, and state
    }
    if (event.data.type === "openavc:state") {
        const { key, value } = event.data;
        // Update display based on state change
    }
});

// Send a device command
window.parent.postMessage({
    type: "openavc:command",
    device: "projector1",
    command: "power_on",
    params: {}
}, "*");
```

**Security:** Iframes are sandboxed with `allow-scripts allow-same-origin`. They cannot access the parent DOM or navigate the parent page.

---

## 11. Surface Layout (Control Surfaces)

Control surface plugins (Stream Deck, MIDI controllers) declare their physical layout so the IDE can show a visual configurator.

### Grid Layout (Button Panels)

```python
SURFACE_LAYOUT = {
    "type": "grid",
    "rows": 2,                   # Number of button rows
    "columns": 4,                # Number of button columns
    "key_size_px": 72,           # Button size in pixels
    "key_spacing_px": 4,         # Gap between buttons
    "supports_pages": True,      # Multi-page button assignment
    "max_pages": 10,             # Maximum number of pages
}
```

The actual layout may be detected from hardware at runtime. The static definition provides the default.

**Source reference for control surface implementation:** [`control_surfaces/streamdeck/streamdeck_plugin.py`](https://github.com/open-avc/openavc-plugins/blob/main/control_surfaces/streamdeck/streamdeck_plugin.py)

---

## 12. Native Dependencies

For plugins that require platform-level libraries (C shared libraries, USB drivers, etc.).

```python
PLUGIN_INFO = {
    # ...
    "native_dependencies": [
        {
            "id": "hidapi",
            "name": "HIDAPI",
            "version": "0.15.0",
            "license": "BSD-3-Clause",       # Must be MIT-compatible
            "required": True,                 # False = plugin works without it
            "check": {
                "type": "library_load",       # library_load | env_var | file_exists
                "names": {
                    "Windows": "hidapi.dll",
                    "Linux": "libhidapi-libusb.so.0",
                },
            },
            "platforms": {
                "win_x64": {
                    "type": "zip",
                    "url": "https://github.com/.../hidapi-win.zip",
                    "extract": "x64/hidapi.dll",
                },
                "linux_x64": {
                    "package": "libhidapi-libusb0",
                    "install_cmd": "sudo apt-get install -y libhidapi-libusb0",
                },
                "linux_arm64": {
                    "package": "libhidapi-libusb0",
                    "install_cmd": "sudo apt-get install -y libhidapi-libusb0",
                },
            },
        },
    ],
}
```

**Check types:**

| Type | Description | Fields |
|------|-------------|--------|
| `library_load` | Check if library can be loaded via ctypes | `names: {System: "lib_name"}` |
| `env_var` | Check if environment variable exists | `key: "VAR_NAME"` |
| `file_exists` | Check if file exists | `path: "/path/to/file"` |

**Pip dependencies** are listed in `"dependencies"` and installed automatically to `plugin_repo/.deps/`.

---

## 13. Testing

OpenAVC provides a test harness for plugin development.

**Source reference:** [`server/core/plugin_test_harness.py`](https://github.com/open-avc/openavc/blob/main/server/core/plugin_test_harness.py)

```python
from server.core.plugin_test_harness import PluginTestHarness

async def test_my_plugin():
    harness = PluginTestHarness()
    plugin = MyPlugin()

    # Start plugin with optional config and capability overrides
    api = await harness.start_plugin(plugin, config={"broker_host": "localhost"})

    # Simulate state changes
    await harness.state_set("device.projector.power", "on")

    # Check plugin set its own state
    status = await harness.state_get("plugin.my_plugin.status")
    assert status == "running"

    # Simulate events
    await harness.emit_event("device.connected.projector")

    # Check logs
    assert harness.log_contains("Connected")

    # Check what macros were executed
    macros = harness.get_executed_macros()

    # Check what device commands were sent
    commands = harness.get_device_commands()
    # Returns: [(device_id, command, params), ...]

    # Stop plugin
    await harness.stop_plugin(plugin)
```

---

## 14. Repository Structure and Naming

```
openavc-plugins/
├── control_surfaces/    # Stream Deck, X-Keys, MIDI controllers
├── integrations/        # MQTT, Dante, Home Assistant
├── sensors/             # Occupancy, temperature, light
├── utility/             # Analytics, logging, voice bridges
├── template/            # Plugin template (copy to start)
├── tests/               # Test files
├── docs/                # Contributing guide
├── index.json           # Plugin catalog
├── validate.py          # Validation script
└── AGENTS.md            # This file
```

### Naming Conventions

- **Plugin ID:** Lowercase with underscores. (e.g., `mqtt_bridge`, `streamdeck`)
- **Directory name:** Same as plugin ID. (e.g., `integrations/mqtt/`)
- **Main file:** `<id>_plugin.py`. (e.g., `mqtt_bridge_plugin.py`)
- **No dots** in plugin IDs (breaks state key parsing).

### plugin.json

Every plugin directory must include a `plugin.json` that mirrors `PLUGIN_INFO`:

```json
{
    "id": "my_plugin",
    "name": "My Plugin",
    "version": "0.1.0",
    "author": "Your Name",
    "description": "What it does.",
    "category": "utility",
    "license": "MIT",
    "platforms": ["all"],
    "capabilities": ["state_read", "state_write", "event_emit"],
    "dependencies": [],
    "min_openavc_version": "0.3.0"
}
```

### README.md

Every plugin must include a `README.md` that covers:

1. **What the plugin does** -- one-paragraph summary
2. **Requirements** -- any hardware, accounts, or services needed
3. **Configuration** -- explain each config field and expected values
4. **State keys** -- list the `plugin.<id>.*` keys the plugin sets, so users can bind to them
5. **Events** -- list the events the plugin emits, so users can create triggers
6. **Troubleshooting** -- common issues and solutions

---

## 15. index.json Catalog Entry

Every plugin must have an entry in `index.json`. The catalog is used by the Programmer IDE's "Browse Plugins" feature.

```json
{
    "id": "my_plugin",
    "name": "My Plugin",
    "file": "category/my_plugin",
    "format": "directory",
    "category": "utility",
    "version": "0.1.0",
    "author": "Your Name",
    "license": "MIT",
    "platforms": ["all"],
    "min_openavc_version": "0.3.0",
    "capabilities": ["state_read", "state_write", "event_emit"],
    "has_native_dependencies": false,
    "verified": false,
    "description": "One-line description."
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `id` | Yes | Must match plugin's `id` field exactly. |
| `name` | Yes | Must match plugin's `name`. |
| `file` | Yes | Path to plugin directory, relative to repo root. |
| `format` | Yes | Always `"directory"` for plugin packages. |
| `category` | Yes | Must match plugin's `category`. |
| `version` | Yes | Must match plugin's `version`. |
| `author` | Yes | Must match plugin's `author`. |
| `license` | Yes | Must match plugin's `license`. |
| `platforms` | Yes | Must match plugin's `platforms`. |
| `capabilities` | Yes | Must match plugin's `capabilities`. |
| `has_native_dependencies` | Yes | `true` if plugin has native dependencies. |
| `verified` | Yes | Always `false` for community contributions. |
| `description` | Yes | Must match plugin's `description`. |
| `min_openavc_version` | No | Minimum compatible OpenAVC version. |
| `manufacturer` | No | Hardware manufacturer (for control surface plugins). |

---

## 16. Validation

Run the validation script before submitting:

```bash
python validate.py                                   # Validate all plugins
python validate.py integrations/mqtt                  # Validate a specific plugin
python validate.py --check-index                      # Also validate index.json consistency
```

The validator checks:
- Required `PLUGIN_INFO` fields present
- Plugin ID format (lowercase, underscores, no dots)
- Category is valid
- License is MIT-compatible
- Capabilities are valid
- `plugin.json` exists and matches `PLUGIN_INFO`
- Main plugin file naming convention (`*_plugin.py`)
- `README.md` exists
- `start()` and `stop()` methods exist
- index.json entry matches plugin fields

---

## 17. Complete Examples

### 17.1 Minimal Plugin (Utility)

```python
"""
Room Activity Logger

Logs device state changes to plugin state for dashboard display.
"""


class RoomActivityPlugin:

    PLUGIN_INFO = {
        "id": "room_activity",
        "name": "Room Activity Logger",
        "version": "1.0.0",
        "author": "Your Name",
        "description": "Tracks device activity and shows room status on the dashboard.",
        "category": "utility",
        "license": "MIT",
        "platforms": ["all"],
        "capabilities": ["state_read", "state_write", "event_subscribe"],
    }

    CONFIG_SCHEMA = {
        "track_pattern": {
            "type": "string",
            "label": "State Pattern",
            "description": "Glob pattern for state keys to track (e.g., device.*.power)",
            "default": "device.*.power",
        },
    }

    EXTENSIONS = {
        "status_cards": [
            {
                "id": "activity",
                "label": "Room Activity",
                "icon": "activity",
                "metrics": [
                    {"key": "plugin.room_activity.devices_on", "label": "Devices On", "format": "number"},
                    {"key": "plugin.room_activity.last_event", "label": "Last Event", "format": "string"},
                ],
            },
        ],
    }

    async def start(self, api):
        self.api = api
        pattern = self.api.config.get("track_pattern", "device.*.power")

        await self.api.state_set("devices_on", 0)
        await self.api.state_set("last_event", "none")

        await self.api.state_subscribe(pattern, self.on_state_change)
        self.api.log(f"Tracking: {pattern}")

    async def stop(self):
        pass  # Cleanup is automatic

    async def on_state_change(self, key, value, old_value):
        await self.api.state_set("last_event", f"{key} = {value}")

        # Count devices that are "on" or True
        all_power = await self.api.state_get_pattern("device.*.power")
        on_count = sum(1 for v in all_power.values() if v in ("on", True, "1", 1))
        await self.api.state_set("devices_on", on_count)

    async def health_check(self):
        return {"status": "ok", "message": "Tracking active"}
```

### 17.2 Integration Plugin (MQTT Bridge)

```python
"""
MQTT Bridge Plugin

Bridges OpenAVC state to and from an MQTT broker.
Supports inbound (MQTT -> OpenAVC) and outbound (OpenAVC -> MQTT) mapping.
"""

import asyncio
import json


class MQTTBridgePlugin:

    PLUGIN_INFO = {
        "id": "mqtt_bridge",
        "name": "MQTT Bridge",
        "version": "1.0.0",
        "author": "Your Name",
        "description": "Bridge OpenAVC state to and from an MQTT broker.",
        "category": "integration",
        "license": "MIT",
        "platforms": ["all"],
        "dependencies": ["paho-mqtt"],
        "capabilities": [
            "state_read", "state_write",
            "event_emit", "event_subscribe",
        ],
    }

    CONFIG_SCHEMA = {
        "broker_host": {
            "type": "string",
            "label": "Broker Host",
            "required": True,
        },
        "broker_port": {
            "type": "integer",
            "label": "Broker Port",
            "default": 1883,
            "min": 1,
            "max": 65535,
        },
        "topic_prefix": {
            "type": "string",
            "label": "Topic Prefix",
            "default": "openavc",
            "description": "Prefix for all MQTT topics (e.g., openavc/device/proj/power)",
        },
    }

    EXTENSIONS = {
        "status_cards": [
            {
                "id": "mqtt_status",
                "label": "MQTT Bridge",
                "icon": "radio",
                "metrics": [
                    {"key": "plugin.mqtt_bridge.connected", "label": "Connected", "format": "boolean"},
                    {"key": "plugin.mqtt_bridge.messages_in", "label": "Messages In", "format": "number"},
                ],
            },
        ],
    }

    async def start(self, api):
        self.api = api
        self.msg_count = 0

        await self.api.state_set("connected", False)
        await self.api.state_set("messages_in", 0)

        # Subscribe to all device state changes for outbound publishing
        await self.api.state_subscribe("device.*", self.on_device_state)

        # Start MQTT connection in background task
        self.api.create_task(self._connect(), name="mqtt_connect")

    async def stop(self):
        if hasattr(self, "client"):
            self.client.loop_stop()
            self.client.disconnect()

    async def _connect(self):
        import paho.mqtt.client as mqtt

        broker = self.api.config.get("broker_host", "localhost")
        port = self.api.config.get("broker_port", 1883)

        self.client = mqtt.Client()
        self.client.on_connect = self._on_mqtt_connect
        self.client.on_message = self._on_mqtt_message

        try:
            self.client.connect(broker, port)
            self.client.loop_start()
            await self.api.state_set("connected", True)
            self.api.log(f"Connected to {broker}:{port}")
        except Exception as e:
            self.api.log(f"Connection failed: {e}", level="error")
            await self.api.state_set("connected", False)

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        prefix = self.api.config.get("topic_prefix", "openavc")
        client.subscribe(f"{prefix}/set/#")

    def _on_mqtt_message(self, client, userdata, msg):
        self.msg_count += 1
        # Handle inbound MQTT messages (runs in MQTT thread)
        asyncio.get_event_loop().call_soon_threadsafe(
            asyncio.create_task,
            self._handle_inbound(msg.topic, msg.payload.decode())
        )

    async def _handle_inbound(self, topic, payload):
        await self.api.state_set("messages_in", self.msg_count)
        self.api.log(f"Received: {topic} = {payload}")

    async def on_device_state(self, key, value, old_value):
        if not hasattr(self, "client"):
            return
        prefix = self.api.config.get("topic_prefix", "openavc")
        topic = f"{prefix}/{key.replace('.', '/')}"
        try:
            self.client.publish(topic, json.dumps(value))
        except Exception:
            pass

    async def health_check(self):
        connected = hasattr(self, "client") and self.client.is_connected()
        if connected:
            return {"status": "ok", "message": "Connected to broker"}
        return {"status": "error", "message": "Disconnected from broker"}
```

### 17.3 Sensor Plugin (Occupancy)

```python
"""
Occupancy Sensor Plugin

Reads occupancy state from a network sensor and sets a user variable.
"""


class OccupancySensorPlugin:

    PLUGIN_INFO = {
        "id": "occupancy_sensor",
        "name": "Occupancy Sensor",
        "version": "1.0.0",
        "author": "Your Name",
        "description": "Reads occupancy from a network sensor and sets a user variable.",
        "category": "sensor",
        "license": "MIT",
        "platforms": ["all"],
        "capabilities": ["state_read", "state_write", "variable_write", "event_emit"],
    }

    CONFIG_SCHEMA = {
        "sensor_url": {
            "type": "string",
            "label": "Sensor URL",
            "required": True,
            "description": "HTTP endpoint that returns occupancy JSON",
        },
        "poll_seconds": {
            "type": "integer",
            "label": "Poll Interval",
            "default": 10,
            "min": 1,
            "max": 300,
        },
        "variable_id": {
            "type": "string",
            "label": "Variable ID",
            "default": "room_occupied",
            "description": "User variable to set (true/false)",
        },
    }

    async def start(self, api):
        self.api = api
        interval = self.api.config.get("poll_seconds", 10)
        await self.api.state_set("status", "polling")
        self.api.create_periodic_task(self.check_sensor, interval, name="poll_sensor")

    async def stop(self):
        pass

    async def check_sensor(self):
        import aiohttp

        url = self.api.config.get("sensor_url", "")
        variable = self.api.config.get("variable_id", "room_occupied")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                    occupied = bool(data.get("occupied", False))
                    await self.api.variable_set(variable, occupied)
                    await self.api.state_set("status", "polling")
        except Exception as e:
            self.api.log(f"Sensor poll failed: {e}", level="warning")
            await self.api.state_set("status", "error")

    async def health_check(self):
        status = await self.api.state_get(f"plugin.{self.api.plugin_id}.status")
        if status == "polling":
            return {"status": "ok", "message": "Sensor responding"}
        return {"status": "error", "message": "Sensor not responding"}
```

---

## 18. Common Mistakes

### Plugin Structure

| Mistake | Fix |
|---------|-----|
| File not named `*_plugin.py` | The loader looks for `*_plugin.py` files specifically. |
| Plugin ID contains dots | Dots break state key parsing. Use underscores only. |
| Missing `plugin.json` | Required for the catalog. Must mirror `PLUGIN_INFO`. |
| Missing `README.md` | Required for community plugins. Document config, state keys, events. |
| `PLUGIN_INFO` and `plugin.json` don't match | Keep them identical. The validator checks this. |

### API Usage

| Mistake | Fix |
|---------|-----|
| Using `asyncio.create_task()` | Use `api.create_task()`. Framework tasks are auto-cancelled on stop. |
| Including `plugin.<id>.` prefix in `state_set()` | The prefix is added automatically. Just pass the key name. |
| Including `plugin.<id>.` prefix in `event_emit()` | Same -- auto-prefixed. |
| Writing to state keys outside plugin namespace | `state_set()` only allows `plugin.<id>.*`. Use `variable_set()` for user vars (requires `variable_write`). |
| Calling `variable_set()` with only `state_write` declared | `variable_set()` needs the separate `variable_write` capability. Add it to your manifest. |
| Storing nested objects in state | State values must be flat primitives: str, int, float, bool, None. |
| Not declaring capabilities | API calls without matching capability raise `PluginPermissionError`. |
| Blocking the event loop | Use `await` for I/O. Never use `time.sleep()` -- use `asyncio.sleep()`. |
| Not handling errors in callbacks | 10 consecutive callback failures auto-disable the plugin. |
| Manually cleaning up subscriptions in `stop()` | Cleanup is automatic. Only close external connections in `stop()`. |
| Subscribing to `*` (everything) | Use narrow patterns. Broad subscriptions cause unnecessary callback overhead. |

### Configuration

| Mistake | Fix |
|---------|-----|
| Required field with no `default` but no `required: True` | Mark it `required: True` so the Setup Dialog appears. |
| Using `config_schema` instead of `CONFIG_SCHEMA` | It's a class attribute named `CONFIG_SCHEMA` (uppercase). |
| `select` field without `options` list | `select` type requires an `options` array with `value` and `label`. |

### index.json

| Mistake | Fix |
|---------|-----|
| `file` path wrong | Path is relative to repo root, points to directory (e.g., `integrations/mqtt`). |
| `format` set to `"python"` | For directory-based plugins, use `"directory"`. |
| `verified` set to `true` | Only maintainers verify. Always submit with `false`. |
| Fields don't match `PLUGIN_INFO` / `plugin.json` | All three must be consistent. |

### Macro Actions

| Mistake | Fix |
|---------|-----|
| Action key not prefixed with plugin id | Action types must look like `<plugin_id>.<name>`. The validator rejects mismatches. |
| Handler is `def`, not `async def` | All macro action handlers must be coroutines. |
| `handler` field references a missing method | The method must exist on the plugin class with the exact name. |
| `select` param has no `options` or `options_source` | One or the other is required for `select` type. |
| Manually resolving `$var.foo` inside the handler | The macro engine resolves dynamic params before calling your handler. Just use `params[key]`. |

### Script API

| Mistake | Fix |
|---------|-----|
| Plugin id contains dashes or starts with a digit | Plugin id must be a valid Python identifier — `audio_player`, not `audio-player`. |
| Method name starts with `_` | Underscore-prefixed names are reserved for proxy machinery; pick a public name. |
| Sync `def` handler without `"sync": True` | Default expects async. Add `"sync": True` to opt out, or make the handler `async def`. |
| `"sync": True` on an async handler | The flag must match the actual method type. |
| Calling another plugin's script methods from inside your plugin | Don't reach across plugin boundaries. Use state keys, events, or macros to coordinate. |

---

## License

All plugins in this repository must be MIT licensed. All pip dependencies must use MIT-compatible licenses. See the list in section 3.
