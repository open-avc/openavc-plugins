# OpenAVC Community Plugins

Community plugin repository for [OpenAVC](https://github.com/open-avc/openavc) — the open-source AV room control platform.

Plugins extend OpenAVC with system-wide integrations, control surfaces, sensors, and services that go beyond single-device protocol translation (which is what [drivers](https://github.com/open-avc/openavc-drivers) are for).

## Plugin Categories

| Category | Directory | Examples |
|----------|-----------|----------|
| **Control Surfaces** | `control_surfaces/` | Elgato Stream Deck, X-Keys, MIDI controllers |
| **Integrations** | `integrations/` | MQTT bridge, Dante DDM, Home Assistant, webhooks |
| **Sensors** | `sensors/` | Occupancy sensors, ambient light, temperature |
| **Utility** | `utility/` | Analytics export, voice control bridges |

## Installing Plugins

Plugins can be installed directly from the OpenAVC Programmer IDE:

1. Open the **Plugins** view in the sidebar
2. Click the **Browse** tab
3. Find the plugin you want and click **Install**
4. Enable and configure the plugin in the **Installed** tab

## Creating Plugins

See the [Creating Plugins](https://github.com/open-avc/openavc/blob/main/docs/creating-plugins.md) guide in the main repository.

For contribution guidelines, see [Contributing Plugins](docs/contributing-plugins.md).

## License

All plugins in this repository are MIT licensed. See [LICENSE](LICENSE) for details.
