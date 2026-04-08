# Contributing Plugins

Thank you for contributing to the OpenAVC plugin ecosystem! This guide covers everything you need to submit a plugin.

## Before You Start

1. Read the [Plugins guide](https://github.com/open-avc/openavc/blob/main/docs/creating-plugins.md) in the main repository to understand what plugins are and how users interact with them
2. Make sure your use case is a **plugin** (system-wide integration, control surface, sensor) and not a **driver** (single-device protocol translation)
3. Check existing plugins to avoid duplicating work
4. Start from the [plugin template](../template/) -- copy the directory and modify it

## Plugin Structure

Each plugin lives in its own directory under the appropriate category:

```
category/
└── your_plugin/
    ├── plugin.json           # Manifest (mirrors PLUGIN_INFO for the catalog)
    ├── your_plugin_plugin.py # Plugin code (single file or package)
    └── README.md             # Usage documentation
```

## plugin.json Manifest

Every plugin must include a `plugin.json` that mirrors the `PLUGIN_INFO` dict:

```json
{
    "id": "your_plugin",
    "name": "Your Plugin Name",
    "version": "1.0.0",
    "author": "Your Name",
    "description": "One-line description of what the plugin does.",
    "category": "utility",
    "license": "MIT",
    "platforms": ["all"],
    "capabilities": ["state_read", "state_write", "event_emit"],
    "dependencies": [],
    "min_openavc_version": "1.0.0"
}
```

## Plugin README

Every plugin must include a `README.md` that covers:

1. **What the plugin does** -- one-paragraph summary
2. **Requirements** -- any hardware, accounts, or services needed
3. **Configuration** -- explain each config field and what values are expected
4. **State keys** -- list the `plugin.<id>.*` keys the plugin sets, so users can bind to them
5. **Events** -- list the events the plugin emits, so users can create triggers
6. **Troubleshooting** -- common issues and solutions

## Submission Checklist

Before submitting a pull request:

- [ ] Plugin has a unique `id` (lowercase, underscores only)
- [ ] `plugin.json` is present, valid, and matches `PLUGIN_INFO` in the code
- [ ] License is MIT (or MIT-compatible: BSD-2/3, Apache-2.0, ISC, PSF, Unlicense, 0BSD, CC0-1.0)
- [ ] All pip dependencies are MIT-compatible
- [ ] Plugin works on all declared platforms
- [ ] `README.md` documents usage, configuration, state keys, and requirements
- [ ] Plugin has been tested with the plugin test harness
- [ ] Code follows Python best practices (async/await, proper error handling)
- [ ] No hardcoded paths, credentials, or environment-specific values
- [ ] `start()` and `stop()` lifecycle methods are implemented
- [ ] Plugin uses `api.create_task()` instead of `asyncio.create_task()`
- [ ] State values are flat primitives only (str, int, float, bool, None)
- [ ] External connections are closed in `stop()`
- [ ] Reconnection logic uses exponential backoff (not tight retry loops)
- [ ] `health_check()` is implemented for plugins with external connections

## index.json Entry

Add your plugin to `index.json` in the root of the repository:

```json
{
    "id": "your_plugin",
    "name": "Your Plugin Name",
    "file": "category/your_plugin/your_plugin_plugin.py",
    "format": "python",
    "category": "utility",
    "version": "1.0.0",
    "author": "Your Name",
    "license": "MIT",
    "platforms": ["all"],
    "min_openavc_version": "1.0.0",
    "capabilities": ["state_read", "state_write", "event_emit"],
    "has_native_dependencies": false,
    "verified": false,
    "description": "One-line description."
}
```

## Categories

Place your plugin directory under the correct category:

| Category | Directory | When to Use |
|----------|-----------|-------------|
| Control Surfaces | `control_surfaces/` | Physical button panels, fader banks, keypads |
| Integrations | `integrations/` | Protocol bridges, external platform connections |
| Sensors | `sensors/` | Environmental inputs (occupancy, temperature, light) |
| Utility | `utility/` | Analytics, logging, voice control bridges |

## Validation

Run the validator before submitting:

```bash
python validate.py                               # Validate all plugins
python validate.py integrations/mqtt              # Validate a specific plugin
python validate.py --check-index                  # Also check index.json consistency
```

## Using an AI Assistant

If you use an AI coding assistant, point it to [`AGENTS.md`](../AGENTS.md) in the root of this repository. It contains the complete Plugin API, manifest format, configuration schema, and examples in a format optimized for LLM agents. Have your assistant run `python validate.py` on its output to catch errors before you submit.

## Review Process

1. Submit a pull request with your plugin directory under the correct category
2. Maintainers review for code quality, security, and license compliance
3. Once approved, the plugin is merged and available for installation from the Programmer IDE
4. Verified plugins (reviewed and tested by OpenAVC maintainers) get a verified badge in the IDE
