#!/usr/bin/env python3
"""
OpenAVC Plugin Validator

Validates plugin packages for correctness: manifest, structure, capabilities,
license, and optionally index.json consistency.

Usage:
    python validate.py                               # Validate all plugins
    python validate.py integrations/mqtt              # Validate specific plugin(s)
    python validate.py --check-index                  # Also validate index.json
    python validate.py --verbose                      # Show passing checks too
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

REQUIRED_MANIFEST_FIELDS = {"id", "name", "version", "author", "description", "category", "license"}

VALID_CAPABILITIES = {
    "state_read", "state_write", "variable_write",
    "event_emit", "event_subscribe",
    "macro_execute", "device_command", "network_listen", "usb_access",
    "http_endpoints",
}

VALID_CATEGORIES = {"control_surface", "integration", "sensor", "utility"}

MIT_COMPATIBLE_LICENSES = {
    "mit", "bsd-2-clause", "bsd-3-clause", "apache-2.0", "isc",
    "psf", "unlicense", "0bsd", "cc0-1.0",
}

VALID_CONFIG_TYPES = {
    "string", "integer", "float", "boolean", "select",
    "state_key", "macro_ref", "device_ref", "command_ref", "group", "mapping_list",
}

VALID_PLATFORMS = {"all", "win_x64", "linux_x64", "linux_arm64"}

# EXTENSIONS types the panel/IDE understand, and the field that uniquely
# identifies an entry within each type (panel_elements are keyed by `type`,
# every other type by `id`). Mirrors server/core/plugin_loader.py.
VALID_EXTENSION_TYPES = {
    "views", "device_panels", "status_cards", "context_actions", "panel_elements",
}
EXTENSION_ID_FIELD = {
    "views": "id",
    "device_panels": "id",
    "status_cards": "id",
    "context_actions": "id",
    "panel_elements": "type",
}

PLUGIN_DIRS = ["control_surfaces", "integrations", "sensors", "utility"]


class ValidationResult:
    def __init__(self, plugin_path):
        self.plugin_path = plugin_path
        self.errors = []
        self.warnings = []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    @property
    def passed(self):
        return len(self.errors) == 0


def validate_id_format(plugin_id):
    """Check that ID is lowercase with underscores only, no dots."""
    return bool(re.match(r'^[a-z][a-z0-9_]*$', plugin_id))


def extract_plugin_info(content):
    """Try to extract PLUGIN_INFO dict from Python source using AST.
    Falls back to regex if AST extraction fails.
    """
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.Assign):
                        for target in item.targets:
                            if isinstance(target, ast.Name) and target.id == "PLUGIN_INFO":
                                return ast.literal_eval(item.value)
    except Exception:
        pass
    return None


def validate_plugin_info(plugin_info, result, source="PLUGIN_INFO"):
    """Validate a PLUGIN_INFO dict."""

    # Required fields
    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in plugin_info:
            result.error(f"{source}: missing required field '{field}'")

    # ID format
    plugin_id = plugin_info.get("id", "")
    if plugin_id:
        if not validate_id_format(plugin_id):
            result.error(f"{source}: invalid plugin ID '{plugin_id}': must be lowercase letters, numbers, and underscores, starting with a letter")
        if "." in plugin_id:
            result.error(f"{source}: plugin ID must not contain dots (breaks state key parsing)")

    # Category
    if "category" in plugin_info:
        if plugin_info["category"] not in VALID_CATEGORIES:
            result.error(f"{source}: invalid category '{plugin_info['category']}': must be one of {sorted(VALID_CATEGORIES)}")

    # License
    if "license" in plugin_info:
        if plugin_info["license"].lower() not in MIT_COMPATIBLE_LICENSES:
            result.error(f"{source}: license '{plugin_info['license']}' is not MIT-compatible. Must be one of: {sorted(MIT_COMPATIBLE_LICENSES)}")

    # Capabilities
    if "capabilities" in plugin_info:
        if not isinstance(plugin_info["capabilities"], list):
            result.error(f"{source}: capabilities must be a list")
        else:
            for cap in plugin_info["capabilities"]:
                if cap not in VALID_CAPABILITIES:
                    result.error(f"{source}: invalid capability '{cap}': must be one of {sorted(VALID_CAPABILITIES)}")

    # Platforms
    if "platforms" in plugin_info:
        if not isinstance(plugin_info["platforms"], list):
            result.error(f"{source}: platforms must be a list")
        else:
            for plat in plugin_info["platforms"]:
                if plat not in VALID_PLATFORMS:
                    result.error(f"{source}: invalid platform '{plat}': must be one of {sorted(VALID_PLATFORMS)}")

    # Version format
    if "version" in plugin_info:
        if not re.match(r'^\d+\.\d+\.\d+', str(plugin_info["version"])):
            result.warn(f"{source}: version '{plugin_info['version']}' doesn't follow semver format (X.Y.Z)")

    # Dependencies license check (just warn -- can't verify without installing)
    if "dependencies" in plugin_info:
        if not isinstance(plugin_info["dependencies"], list):
            result.error(f"{source}: dependencies must be a list")
        elif plugin_info["dependencies"]:
            result.warn(f"{source}: has pip dependencies {plugin_info['dependencies']} -- ensure all are MIT-compatible")


def validate_config_schema(config_schema, result):
    """Validate a CONFIG_SCHEMA dict."""
    if not isinstance(config_schema, dict):
        result.error("CONFIG_SCHEMA must be a dict")
        return

    for field_id, field_def in config_schema.items():
        if not isinstance(field_def, dict):
            result.error(f"CONFIG_SCHEMA field '{field_id}' must be a dict")
            continue

        if "type" not in field_def:
            result.error(f"CONFIG_SCHEMA field '{field_id}' missing 'type'")
            continue

        field_type = field_def["type"]
        if field_type not in VALID_CONFIG_TYPES:
            result.error(f"CONFIG_SCHEMA field '{field_id}' has invalid type '{field_type}': must be one of {sorted(VALID_CONFIG_TYPES)}")

        if field_type == "select" and "options" not in field_def:
            result.error(f"CONFIG_SCHEMA field '{field_id}' is type 'select' but missing 'options' list")

        if field_type == "group":
            if "fields" not in field_def:
                result.error(f"CONFIG_SCHEMA field '{field_id}' is type 'group' but missing 'fields' dict")
            elif isinstance(field_def["fields"], dict):
                # Recursively validate group fields
                validate_config_schema(field_def["fields"], result)

        if field_type == "mapping_list" and "item_schema" not in field_def:
            result.error(f"CONFIG_SCHEMA field '{field_id}' is type 'mapping_list' but missing 'item_schema'")


def validate_extensions_schema(extensions, result):
    """Validate an EXTENSIONS dict (mirrors the runtime plugin loader).

    Each type must be a known key holding a list of dicts, and every entry needs
    its identifier field (``id``, or ``type`` for panel_elements) unique within
    the type. The loader rejects a malformed EXTENSIONS at enable time, so a
    contributor should catch it here first.
    """
    if not isinstance(extensions, dict):
        result.error("EXTENSIONS must be a dict")
        return

    for ext_type, ext_list in extensions.items():
        if ext_type not in VALID_EXTENSION_TYPES:
            result.error(
                f"EXTENSIONS has unknown type '{ext_type}': must be one of "
                f"{sorted(VALID_EXTENSION_TYPES)}"
            )
            continue
        if not isinstance(ext_list, list):
            result.error(f"EXTENSIONS['{ext_type}'] must be a list")
            continue

        id_field = EXTENSION_ID_FIELD[ext_type]
        seen = set()
        for i, ext in enumerate(ext_list):
            if not isinstance(ext, dict):
                result.error(f"EXTENSIONS['{ext_type}'][{i}] must be a dict")
                continue
            ext_id = ext.get(id_field)
            if not ext_id or not isinstance(ext_id, str):
                result.error(
                    f"EXTENSIONS['{ext_type}'][{i}] missing '{id_field}' (string)"
                )
                continue
            if ext_id in seen:
                result.error(
                    f"EXTENSIONS['{ext_type}'] has duplicate {id_field} '{ext_id}'"
                )
            seen.add(ext_id)


def _extract_class_attr(content, attr_name):
    """Best-effort literal extraction of a class-level attribute via AST."""
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.Assign):
                        for target in item.targets:
                            if isinstance(target, ast.Name) and target.id == attr_name:
                                return ast.literal_eval(item.value)
    except Exception:
        pass
    return None


def validate_plugin_dir(plugin_path, result):
    """Validate a plugin directory."""

    # Check for main plugin file
    plugin_files = list(plugin_path.glob("*_plugin.py"))
    if not plugin_files:
        result.error("No *_plugin.py file found (plugin loader looks for this pattern)")
        return

    if len(plugin_files) > 1:
        result.warn(f"Multiple *_plugin.py files found: {[f.name for f in plugin_files]}")

    main_file = plugin_files[0]

    # Read and parse the plugin file
    try:
        content = main_file.read_text(encoding="utf-8")
    except Exception as e:
        result.error(f"Cannot read {main_file.name}: {e}")
        return

    # Check for PLUGIN_INFO
    if "PLUGIN_INFO" not in content:
        result.error(f"{main_file.name}: missing PLUGIN_INFO class attribute")

    # Try to extract and validate PLUGIN_INFO
    plugin_info = extract_plugin_info(content)
    if plugin_info:
        validate_plugin_info(plugin_info, result)

        # Check ID matches directory name
        plugin_id = plugin_info.get("id", "")
        dir_name = plugin_path.name
        if plugin_id and plugin_id != dir_name:
            result.warn(f"Plugin ID '{plugin_id}' doesn't match directory name '{dir_name}'")

    # Check for start/stop methods
    if "async def start(" not in content and "def start(" not in content:
        result.error(f"{main_file.name}: missing start() method")
    if "async def stop(" not in content and "def stop(" not in content:
        result.error(f"{main_file.name}: missing stop() method")

    # Check for common mistakes
    if "asyncio.create_task(" in content:
        result.warn(f"{main_file.name}: uses asyncio.create_task() instead of api.create_task() -- tasks won't be auto-cancelled on stop")

    if "time.sleep(" in content:
        result.error(f"{main_file.name}: uses time.sleep() which blocks the event loop. Use asyncio.sleep() instead.")

    # Check for CONFIG_SCHEMA
    has_config_schema = "CONFIG_SCHEMA" in content
    if has_config_schema:
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, ast.Assign):
                            for target in item.targets:
                                if isinstance(target, ast.Name) and target.id == "CONFIG_SCHEMA":
                                    try:
                                        schema = ast.literal_eval(item.value)
                                        validate_config_schema(schema, result)
                                    except Exception:
                                        pass
        except Exception:
            pass

    # Check for EXTENSIONS (UI extensions). Best-effort: only literal dicts can
    # be checked; ones built at runtime are skipped.
    if "EXTENSIONS" in content:
        extensions = _extract_class_attr(content, "EXTENSIONS")
        if extensions is not None:
            validate_extensions_schema(extensions, result)

    # Check for plugin.json
    plugin_json_path = plugin_path / "plugin.json"
    if not plugin_json_path.exists():
        result.error("Missing plugin.json manifest file")
    else:
        try:
            with open(plugin_json_path, encoding="utf-8") as f:
                manifest = json.load(f)
            validate_plugin_info(manifest, result, source="plugin.json")

            # Cross-reference with PLUGIN_INFO
            if plugin_info:
                for field in ("id", "name", "version", "category", "license"):
                    if field in plugin_info and field in manifest:
                        if str(plugin_info[field]) != str(manifest[field]):
                            result.error(
                                f"Mismatch: PLUGIN_INFO.{field}='{plugin_info[field]}' "
                                f"vs plugin.json.{field}='{manifest[field]}'"
                            )
        except json.JSONDecodeError as e:
            result.error(f"plugin.json is invalid JSON: {e}")

    # Check for README.md
    readme_path = plugin_path / "README.md"
    if not readme_path.exists():
        result.warn("Missing README.md (recommended for community plugins)")

    # Check category matches directory
    if plugin_info and "category" in plugin_info:
        parent_dir = plugin_path.parent.name
        expected_categories = {
            "control_surfaces": "control_surface",
            "integrations": "integration",
            "sensors": "sensor",
            "utility": "utility",
        }
        expected = expected_categories.get(parent_dir)
        if expected and plugin_info["category"] != expected:
            result.warn(f"Category '{plugin_info['category']}' doesn't match directory '{parent_dir}/' (expected '{expected}')")


def validate_index_json(repo_root, results):
    """Validate index.json and cross-reference with plugin files."""
    index_path = repo_root / "index.json"
    if not index_path.exists():
        r = ValidationResult(index_path)
        r.error("index.json not found")
        results.append(r)
        return

    result = ValidationResult(index_path)

    try:
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
    except json.JSONDecodeError as e:
        result.error(f"Invalid JSON: {e}")
        results.append(result)
        return

    if "plugins" not in index:
        result.error("Missing 'plugins' array")
        results.append(result)
        return

    seen_ids = set()
    for entry in index["plugins"]:
        plugin_id = entry.get("id", "<missing>")

        # Required fields
        for field in ("id", "name", "file", "format", "category",
                      "version", "author", "license", "platforms",
                      "capabilities", "has_native_dependencies",
                      "verified", "description"):
            if field not in entry:
                result.error(f"Plugin '{plugin_id}': missing required field '{field}'")

        # Duplicate IDs
        if plugin_id in seen_ids:
            result.error(f"Duplicate plugin ID '{plugin_id}' in index.json")
        seen_ids.add(plugin_id)

        # Directory exists
        if "file" in entry:
            plugin_dir = repo_root / entry["file"]
            if not plugin_dir.exists():
                result.error(f"Plugin '{plugin_id}': directory '{entry['file']}' does not exist")

        # Valid category
        if "category" in entry and entry["category"] not in VALID_CATEGORIES:
            result.error(f"Plugin '{plugin_id}': invalid category '{entry['category']}'")

        # Valid license
        if "license" in entry and entry["license"].lower() not in MIT_COMPATIBLE_LICENSES:
            result.error(f"Plugin '{plugin_id}': license '{entry['license']}' is not MIT-compatible")

        # Valid capabilities
        if "capabilities" in entry and isinstance(entry["capabilities"], list):
            for cap in entry["capabilities"]:
                if cap not in VALID_CAPABILITIES:
                    result.error(f"Plugin '{plugin_id}': invalid capability '{cap}'")

        # Cross-reference with plugin.json
        if "file" in entry:
            plugin_json = repo_root / entry["file"] / "plugin.json"
            if plugin_json.exists():
                try:
                    with open(plugin_json, encoding="utf-8") as f:
                        manifest = json.load(f)
                    for field in ("id", "name", "version", "category", "license"):
                        if field in manifest and field in entry:
                            if str(manifest[field]) != str(entry[field]):
                                result.error(
                                    f"Plugin '{plugin_id}': index.json {field}='{entry[field]}' "
                                    f"doesn't match plugin.json {field}='{manifest[field]}'"
                                )
                except Exception:
                    pass

    # Check for plugin dirs not in index
    for dir_name in PLUGIN_DIRS:
        dir_path = repo_root / dir_name
        if not dir_path.exists():
            continue
        for d in dir_path.iterdir():
            if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("_"):
                file_rel = f"{dir_name}/{d.name}"
                if not any(e.get("file") == file_rel for e in index["plugins"]):
                    # Check it's actually a plugin (has *_plugin.py)
                    if list(d.glob("*_plugin.py")):
                        result.warn(f"Plugin directory '{file_rel}' exists but has no index.json entry")

    results.append(result)


def find_plugin_dirs(repo_root, targets=None):
    """Find plugin directories to validate."""
    dirs = []
    if targets:
        for target in targets:
            path = Path(target)
            if not path.is_absolute():
                path = repo_root / path
            if path.is_dir():
                dirs.append(path)
            else:
                print(f"WARNING: Not a directory: {target}")
    else:
        for dir_name in PLUGIN_DIRS:
            dir_path = repo_root / dir_name
            if not dir_path.exists():
                continue
            for d in sorted(dir_path.iterdir()):
                if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("_"):
                    # Only validate directories that look like plugins
                    if list(d.glob("*_plugin.py")) or (d / "plugin.json").exists():
                        dirs.append(d)
    return dirs


def main():
    parser = argparse.ArgumentParser(description="Validate OpenAVC plugin packages")
    parser.add_argument("plugins", nargs="*", help="Specific plugin directories to validate (default: all)")
    parser.add_argument("--check-index", action="store_true", help="Also validate index.json consistency")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show passing checks")
    args = parser.parse_args()

    repo_root = Path(__file__).parent

    plugin_dirs = find_plugin_dirs(repo_root, args.plugins if args.plugins else None)
    results = []

    for plugin_path in plugin_dirs:
        result = ValidationResult(plugin_path)
        validate_plugin_dir(plugin_path, result)
        results.append(result)

    if args.check_index:
        validate_index_json(repo_root, results)

    # Print results
    total_errors = 0
    total_warnings = 0
    total_plugins = len(results)
    passed_plugins = 0

    for result in results:
        rel_path = result.plugin_path
        try:
            rel_path = result.plugin_path.relative_to(repo_root)
        except ValueError:
            pass

        if result.passed:
            passed_plugins += 1
            if args.verbose:
                print(f"  PASS  {rel_path}")
        else:
            print(f"  FAIL  {rel_path}")
            for err in result.errors:
                print(f"        ERROR: {err}")
                total_errors += 1

        for warn in result.warnings:
            if result.passed and not args.verbose:
                print(f"  WARN  {rel_path}")
            print(f"        WARNING: {warn}")
            total_warnings += 1

    # Summary
    print()
    print(f"Validated {total_plugins} plugin(s): {passed_plugins} passed, {total_plugins - passed_plugins} failed")
    if total_errors:
        print(f"  {total_errors} error(s)")
    if total_warnings:
        print(f"  {total_warnings} warning(s)")

    sys.exit(1 if total_errors > 0 else 0)


if __name__ == "__main__":
    main()
