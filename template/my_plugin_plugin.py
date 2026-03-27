"""
OpenAVC Plugin Template

Copy this directory, rename the class, and fill in PLUGIN_INFO + CONFIG_SCHEMA.

Plugin lifecycle:
1. Plugin loader discovers the plugin in plugin_repo/ at startup
2. User enables the plugin in the Programmer IDE
3. start(api) is called -- begin operation, subscribe to events/state
4. Plugin runs until disabled or server shuts down
5. stop() is called -- close connections, release resources
6. Automatic cleanup removes all subscriptions, state keys, and tasks

The PluginAPI (passed to start()) is your only interface to the runtime.
All registrations are tracked and cleaned up automatically on stop.
"""


class MyPlugin:

    PLUGIN_INFO = {
        "id": "my_plugin",
        "name": "My Plugin",
        "version": "0.1.0",
        "author": "Your Name",
        "description": "What this plugin does.",
        "category": "utility",          # control_surface | integration | sensor | utility
        "license": "MIT",
        "platforms": ["all"],           # win_x64 | linux_x64 | linux_arm64 | all
        "dependencies": [],             # pip packages (must be MIT-compatible)
        "capabilities": [
            "state_read",               # Read any state key
            "state_write",              # Write to plugin.<id>.* namespace
            "event_emit",               # Emit events (auto-prefixed)
        ],
    }

    CONFIG_SCHEMA = {
        "example_setting": {
            "type": "string",
            "label": "Example Setting",
            "description": "An example configuration field.",
            "default": "hello",
        },
    }

    # Optional: UI extension points
    # EXTENSIONS = {
    #     "status_cards": [
    #         {
    #             "id": "my_status",
    #             "label": "My Plugin",
    #             "icon": "activity",
    #             "metrics": [
    #                 {"key": "plugin.my_plugin.status", "label": "Status", "format": "string"},
    #             ],
    #         },
    #     ],
    # }

    async def start(self, api):
        """Called when the plugin is enabled. Begin operation here."""
        self.api = api
        self.api.log("Plugin started!")

        # Read configuration
        setting = self.api.config.get("example_setting", "hello")
        self.api.log(f"Example setting: {setting}")

        # Subscribe to state changes
        await self.api.state_subscribe("device.*", self.on_device_change)

        # Set initial plugin state
        await self.api.state_set("status", "running")

    async def stop(self):
        """Called when the plugin is disabled or server shuts down.
        Close external connections and release hardware here.
        State keys, subscriptions, and tasks are cleaned up automatically.
        """
        self.api.log("Plugin stopped!")

    async def on_device_change(self, key, value, old_value):
        """Example state change callback."""
        self.api.log(f"Device state changed: {key} = {value} (was {old_value})")

    async def health_check(self):
        """Optional. Called periodically to check plugin health.
        Return a dict with 'status' (ok/degraded/error) and 'message'.
        """
        return {"status": "ok", "message": "Everything is fine"}
