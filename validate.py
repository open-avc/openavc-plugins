#!/usr/bin/env python3
"""
OpenAVC Plugin Validator

Validates plugin packages for correctness: what each plugin declares, its
structure, its license, and optionally index.json consistency.

Usage:
    python validate.py                               # Validate all plugins
    python validate.py integrations/mqtt              # Validate specific plugin(s)
    python validate.py --check-index                  # Also validate index.json
    python validate.py --verbose                      # Show passing checks too

What a plugin declares is judged by OpenAVC's own rules. scripts/_vendor/
holds copies of the platform's plugin_declarations, plugin_handler_rules and
plugin_source modules, made by scripts/vendor_platform_rules.py, whose --check
fails CI when they drift from the platform. The plugin is read the way OpenAVC
reads it before installing it: from the file OpenAVC would load its class
from, without running it, so a plugin's dependencies need not be installed.
Each refusal is the sentence OpenAVC shows when it refuses the plugin.

Two of OpenAVC's checks are left out. Whether this machine can run the plugin
(its platforms, its min_openavc_version) is not a question about the plugin:
a pull request is checked on one machine for plugins meant for others.
Whether a MACRO_ACTIONS or SCRIPT_API handler is an async method needs the
class imported.

Checked here and not by OpenAVC when it starts a plugin: the id's form, a
semver version, safe dependency strings, the platform ids listed, the
CONFIG_SCHEMA field types the Programmer's settings form draws, the
validate_config hook, what the module's text shows, and the repository's own
layout (plugin.json, README, category folder, index.json).

A plugin folder is reported in this order: each file OpenAVC passes over, the
file its class is read from (OpenAVC's rules, then the checks made only here,
then what its text shows), its plugin.json as a manifest and against
PLUGIN_INFO, its README, and its category against its folder.
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

_VENDOR_ROOT = str(Path(__file__).resolve().parent / "scripts")
if _VENDOR_ROOT not in sys.path:
    sys.path.insert(0, _VENDOR_ROOT)

from _vendor import plugin_declarations as rules  # noqa: E402
from _vendor.plugin_source import read_folder  # noqa: E402

# The settings field types the Programmer's plugin settings form takes. No
# OpenAVC table to compare with, so checked by eye.
VALID_CONFIG_TYPES = {
    "string", "text", "integer", "float", "boolean", "select", "color",
    "state_key", "macro_ref", "device_ref", "command_ref", "group", "mapping_list",
}

# The folder each category's plugins live in, in the order they are checked.
CATEGORY_DIRS = {
    "control_surfaces": "control_surface",
    "integrations": "integration",
    "sensors": "sensor",
    "utility": "utility",
}
PLUGIN_DIRS = list(CATEGORY_DIRS)

# The fields PLUGIN_INFO, plugin.json and index.json must agree on wherever two
# copies both have one, compared as text. min_openavc_version is here because
# PLUGIN_INFO is the copy the platform actually enforces -- a plugin.json that
# claims a higher floor than PLUGIN_INFO reads as gated in the catalog while
# installing onto a core that can't run it.
AGREED_FIELDS = ("id", "name", "version", "category", "license", "min_openavc_version")

# The fields every index.json entry must have, in the order a missing one is
# reported.
INDEX_REQUIRED_FIELDS = (
    "id", "name", "file", "format", "category", "version", "author", "license",
    "platforms", "capabilities", "has_native_dependencies", "verified", "description",
)

NO_PLUGIN_CLASS = (
    "No plugin class found: OpenAVC needs a class whose PLUGIN_INFO is a dict with "
    "an 'id', written out in the source so it can be read before the plugin is installed"
)

_VCS_PREFIXES = ("git+", "hg+", "svn+", "bzr+")


def is_safe_requirement(req):
    """A dependency must be a plain named pip requirement, not a pip option,
    URL, VCS spec, or PEP 508 direct reference.

    The installer rejects these shapes at install time -- a leading '-'
    ('--index-url=...'), a 'git+'/URL install, or a 'name @ url' reference
    could run install-time code from an attacker-chosen index/repo. Flag them
    here so contributors learn at PR time. Extras ('pkg[extra]'), version
    specifiers, and environment markers are allowed. A copy of
    openavc/core/plugin_wheels.py's _is_safe_requirement (and _VCS_PREFIXES),
    written the same way so the two can be compared; this one reads any value
    as text (``str(req)``), where the platform's takes text only.
    """
    s = str(req).strip()
    if not s or s[0] in "-./\\":
        return False
    low = s.lower()
    if "://" in low or "@" in s or any(p in low for p in _VCS_PREFIXES):
        return False
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", s))


class ValidationResult:
    """One plugin folder's (or index.json's) errors and warnings, in the order
    found. It passes when it has no errors."""

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


def _read(value):
    """Whether a declared value was read: not one the source builds when the
    plugin runs."""
    return value is not rules.UNREAD


def validate_plugin_info(plugin_info, result, source="PLUGIN_INFO"):
    """A PLUGIN_INFO dict, or plugin.json read as one: OpenAVC's rules for
    it, then the checks made only here. `source` begins every message."""
    judge(rules.Declared(plugin_info), source, result)
    _check_own_info(plugin_info, source, result)


def judge(declared, source, result):
    """Each refusal OpenAVC's rules give a plugin's declarations, in their
    order and OpenAVC's words (no host is asked), then the platform ids it
    lists. `source` begins every message."""
    for problem in rules.problems(declared):
        result.error(f"{source}: {problem}")
    if isinstance(declared.info, dict):
        problem = rules.platforms_problem(declared.info)
        if problem is not None:
            result.error(f"{source}: {problem}")


def _check_own_info(info, source, result):
    """The PLUGIN_INFO checks made only here, in order."""
    for check in _OWN_INFO_CHECKS:
        check(info, source, result)


def _check_id(info, source, result):
    plugin_id = info.get("id", "")
    if not plugin_id or not _read(plugin_id):
        return
    if not (isinstance(plugin_id, str) and validate_id_format(plugin_id)):
        result.error(f"{source}: invalid plugin ID '{plugin_id}': must be lowercase letters, numbers, and underscores, starting with a letter")
    if isinstance(plugin_id, str) and "." in plugin_id:
        result.error(f"{source}: plugin ID must not contain dots (breaks state key parsing)")


def _check_version(info, source, result):
    version = info.get("version", rules.UNREAD)
    if _read(version) and not _is_semver(version):
        result.warn(f"{source}: version '{version}' doesn't follow semver format (X.Y.Z)")


def _is_semver(version):
    return re.match(r'^\d+\.\d+\.\d+', str(version)) is not None


def _check_dependencies(info, source, result):
    """Each must be a plain named requirement (the installer rejects
    URLs/VCS/pip-options), plus a license reminder when there are any."""
    dependencies = info.get("dependencies")
    if "dependencies" not in info or not _read(dependencies):
        return
    if not isinstance(dependencies, list):
        result.error(f"{source}: dependencies must be a list")
        return
    if not dependencies:
        return
    for dep in dependencies:
        if not is_safe_requirement(dep):
            result.error(
                f"{source}: unsafe dependency {dep!r} -- must be a plain "
                "'package' or 'package>=x.y' requirement (no URLs, VCS, "
                "pip options, or '@' direct references)"
            )
    result.warn(f"{source}: has pip dependencies {dependencies} -- ensure all are MIT-compatible")


# The checks _check_own_info runs, in order.
_OWN_INFO_CHECKS = (
    _check_id,
    _check_version,
    _check_dependencies,
)


def validate_config_schema(config_schema, result):
    """The fields of a CONFIG_SCHEMA dict, a group's fields included: the
    types the Programmer's settings form draws. Whether it is a dict at all
    is OpenAVC's rule (``judge``)."""
    if not isinstance(config_schema, dict):
        return
    for field_id, field_def in config_schema.items():
        _check_schema_field(field_id, field_def, result)


def _check_schema_field(field_id, field_def, result):
    """One settings field: a dict, its secret flag, its type, then what its
    type needs."""
    if not isinstance(field_def, dict):
        result.error(f"CONFIG_SCHEMA field '{field_id}' must be a dict")
        return
    # A login. Only a real True marks one; "true" or 1 would read as
    # marked here and be treated as an ordinary setting by OpenAVC.
    if "secret" in field_def and not isinstance(field_def["secret"], bool):
        result.error(f"CONFIG_SCHEMA field '{field_id}' has 'secret' that is not true or false")
    if "type" not in field_def:
        result.error(f"CONFIG_SCHEMA field '{field_id}' missing 'type'")
        return
    field_type = field_def["type"]
    if not _is_one_of(field_type, VALID_CONFIG_TYPES):
        result.error(f"CONFIG_SCHEMA field '{field_id}' has invalid type '{field_type}': must be one of {sorted(VALID_CONFIG_TYPES)}")
        return
    type_check = _TYPE_CHECKS.get(field_type)
    if type_check is not None:
        type_check(field_id, field_def, result)


def _check_select(field_id, field_def, result):
    if "options" not in field_def:
        result.error(f"CONFIG_SCHEMA field '{field_id}' is type 'select' but missing 'options' list")


def _check_group(field_id, field_def, result):
    if "fields" not in field_def:
        result.error(f"CONFIG_SCHEMA field '{field_id}' is type 'group' but missing 'fields' dict")
    elif isinstance(field_def["fields"], dict):
        validate_config_schema(field_def["fields"], result)


def _check_mapping_list(field_id, field_def, result):
    """A mapping_list needs its item_schema. A column that holds a login is
    warned about, as OpenAVC warns when it loads the plugin: a row has no id
    to keep a login under, so the login stays in the project."""
    if "item_schema" not in field_def:
        result.error(f"CONFIG_SCHEMA field '{field_id}' is type 'mapping_list' but missing 'item_schema'")
        return
    item_schema = field_def["item_schema"]
    if not isinstance(item_schema, dict):
        return
    for column_id, column in item_schema.items():
        if isinstance(column, dict) and is_login(str(column_id), column):
            result.warn(
                f"CONFIG_SCHEMA field '{field_id}' column '{column_id}' holds a login, but a "
                "mapping_list row has no id to keep a login under, so OpenAVC keeps it in "
                "the project. Keep the list outside CONFIG_SCHEMA and declare it in CONFIG_SECRETS."
            )


def is_login(name, definition):
    """Whether OpenAVC treats a settings field as a login: one named
    'password', or one whose definition says secret: true or type: password.
    A copy of openavc/drivers/spec.py's is_secret_setting, compared with it
    in tests/test_validate_matches_platform.py."""
    if name == "password":
        return True
    if not isinstance(definition, dict):
        return False
    return definition.get("secret") is True or definition.get("type") == "password"


# What a settings field of each of these types needs besides its type.
_TYPE_CHECKS = {
    "select": _check_select,
    "group": _check_group,
    "mapping_list": _check_mapping_list,
}


def validate_config_hook(tree, class_name, file_name, result):
    """The optional ``validate_config`` on the plugin's class is called on
    the class (which may not be running) before its config is saved,
    synchronously. An instance method or an async one is never asked, which
    reads as a check that passes everything."""
    for hook in _config_hooks(tree, class_name):
        decorators = {d.id for d in hook.decorator_list if isinstance(d, ast.Name)}
        if isinstance(hook, ast.AsyncFunctionDef):
            result.error(f"{file_name}: validate_config must not be async -- "
                         "OpenAVC calls it synchronously before saving a config")
        elif not decorators & {"classmethod", "staticmethod"}:
            result.error(f"{file_name}: validate_config must be a @classmethod or "
                         "@staticmethod -- OpenAVC calls it on the class, which may not be running")


def _config_hooks(tree, class_name):
    """Each ``validate_config`` defined in the body of the top-level class
    named `class_name` (the last of that name, as the module holds it)."""
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
    if not classes:
        return []
    return [item for item in classes[-1].body if _defines(item, "validate_config")]


def _defines(statement, name):
    return isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name == name


def validate_plugin_dir(plugin_path, result):
    """Validate a plugin directory, reported in this order: each file OpenAVC
    passes over, the file its class is read from (OpenAVC's rules, the checks
    made only here, what its text shows), its plugin.json against
    PLUGIN_INFO, its README, and its category against its folder. Without a
    plugin class it stops there."""
    folder = read_folder(plugin_path)
    for path, why in folder.passed_over:
        result.error(f"{path.name}: OpenAVC cannot read this file: {why}")
    if folder.declared is None:
        result.error(NO_PLUGIN_CLASS)
        return
    declared = folder.declared
    file_name = folder.path.name
    content = folder.path.read_text(encoding="utf-8")
    judge(declared, file_name, result)
    if declared.unread:
        result.warn(f"{file_name}: not checked, because the source builds it when the "
                    f"plugin runs: {', '.join(declared.unread)}")
    _check_own_info(declared.info, file_name, result)
    _check_folder_name(declared.info, plugin_path, result)
    _check_habits(content, file_name, result)
    validate_config_schema(declared.config_schema, result)
    validate_config_hook(ast.parse(content), declared.class_name, file_name, result)
    _check_plugin_json(plugin_path, declared.info, result)
    _check_readme(plugin_path, result)
    _check_category_folder(plugin_path, declared.info, result)


def _check_folder_name(plugin_info, plugin_path, result):
    plugin_id = plugin_info.get("id", "")
    if _is_text(plugin_id) and plugin_id != plugin_path.name:
        result.warn(f"Plugin ID '{plugin_id}' doesn't match directory name '{plugin_path.name}'")


def _is_text(value):
    """A non-empty string."""
    return isinstance(value, str) and value != ""


def _check_habits(content, file_name, result):
    """What the module's text shows, found as text anywhere in it (a comment
    counts): a start and a stop method (``async def start(`` contains
    ``def start(``), and the two calls that misbehave in a plugin."""
    for method in ("start", "stop"):
        if f"def {method}(" not in content:
            result.error(f"{file_name}: missing {method}() method")
    if "asyncio.create_task(" in content:
        result.warn(f"{file_name}: uses asyncio.create_task() instead of api.create_task() -- tasks won't be auto-cancelled on stop")
    if "time.sleep(" in content:
        result.error(f"{file_name}: uses time.sleep() which blocks the event loop. Use asyncio.sleep() instead.")


def _check_plugin_json(plugin_path, plugin_info, result):
    """plugin.json: there, JSON, valid as a manifest, and agreeing with what
    could be read of PLUGIN_INFO."""
    plugin_json_path = plugin_path / "plugin.json"
    if not plugin_json_path.exists():
        result.error("Missing plugin.json manifest file")
        return
    try:
        with open(plugin_json_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except json.JSONDecodeError as e:
        result.error(f"plugin.json is invalid JSON: {e}")
        return
    if not isinstance(manifest, dict):
        result.error("plugin.json must hold a JSON object")
        return
    validate_plugin_info(manifest, result, source="plugin.json")
    if plugin_info:
        for field in _disagreements(plugin_info, manifest):
            result.error(
                f"Mismatch: PLUGIN_INFO.{field}='{plugin_info[field]}' "
                f"vs plugin.json.{field}='{manifest[field]}'"
            )


def _check_readme(plugin_path, result):
    if not (plugin_path / "README.md").exists():
        result.warn("Missing README.md (recommended for community plugins)")


def _check_category_folder(plugin_path, plugin_info, result):
    """A plugin in a category folder declares that folder's category."""
    if not plugin_info or not _read(plugin_info.get("category", rules.UNREAD)):
        return
    parent_dir = plugin_path.parent.name
    expected = CATEGORY_DIRS.get(parent_dir)
    if expected and plugin_info["category"] != expected:
        result.warn(f"Category '{plugin_info['category']}' doesn't match directory '{parent_dir}/' (expected '{expected}')")


def validate_index_json(repo_root, results):
    """Validate index.json: each entry, its folder and its plugin.json, then
    the plugin folders no entry names. One result, added to `results`."""
    index_path = repo_root / "index.json"
    result = ValidationResult(index_path)
    index = _read_index(index_path, result)
    if index is not None:
        _check_entries(repo_root, index["plugins"], result)
        _check_unindexed(repo_root, index["plugins"], result)
    results.append(result)


def _read_index(index_path, result):
    """index.json's contents, or None after reporting why there are none."""
    if not index_path.exists():
        result.error("index.json not found")
        return None
    try:
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
    except json.JSONDecodeError as e:
        result.error(f"Invalid JSON: {e}")
        return None
    if "plugins" not in index:
        result.error("Missing 'plugins' array")
        return None
    return index


def _check_entries(repo_root, entries, result):
    seen_ids = set()
    for entry in entries:
        _check_entry(repo_root, entry, seen_ids, result)


def _check_entry(repo_root, entry, seen_ids, result):
    """One entry: its required fields, its id against the entries before it,
    its folder, its values, then its plugin.json."""
    plugin_id = entry.get("id", "<missing>")
    for field in INDEX_REQUIRED_FIELDS:
        if field not in entry:
            result.error(f"Plugin '{plugin_id}': missing required field '{field}'")
    if plugin_id in seen_ids:
        result.error(f"Duplicate plugin ID '{plugin_id}' in index.json")
    seen_ids.add(plugin_id)
    if "file" in entry and not (repo_root / entry["file"]).exists():
        result.error(f"Plugin '{plugin_id}': directory '{entry['file']}' does not exist")
    _check_entry_values(entry, plugin_id, result)
    if "file" in entry:
        _compare_with_plugin_json(repo_root / entry["file"] / "plugin.json", entry, plugin_id, result)


def _check_entry_values(entry, plugin_id, result):
    """The entry's category, license and capabilities, against OpenAVC's
    tables and license rule, in shorter messages."""
    for check in (_check_entry_category, _check_entry_license, _check_entry_capabilities):
        check(entry, plugin_id, result)


def _check_entry_category(entry, plugin_id, result):
    if "category" in entry and not _is_one_of(entry["category"], rules.VALID_CATEGORIES):
        result.error(f"Plugin '{plugin_id}': invalid category '{entry['category']}'")


def _check_entry_license(entry, plugin_id, result):
    if "license" in entry and not rules.is_mit_compatible(entry["license"]):
        result.error(f"Plugin '{plugin_id}': license '{entry['license']}' is not MIT-compatible")


def _check_entry_capabilities(entry, plugin_id, result):
    """Each capability OpenAVC does not know. A value that is not a list is
    not reported here."""
    capabilities = entry.get("capabilities")
    if not isinstance(capabilities, list):
        return
    for cap in capabilities:
        if not _is_one_of(cap, rules.VALID_CAPABILITIES):
            result.error(f"Plugin '{plugin_id}': invalid capability '{cap}'")


def _is_one_of(value, allowed):
    return isinstance(value, str) and value in allowed


def _compare_with_plugin_json(plugin_json, entry, plugin_id, result):
    """An error for each of AGREED_FIELDS the entry and the plugin's
    plugin.json disagree on. A plugin.json that is missing, or that cannot be
    read or compared, is passed over here without a word."""
    if not plugin_json.exists():
        return
    try:
        with open(plugin_json, encoding="utf-8") as f:
            manifest = json.load(f)
        for field in _disagreements(manifest, entry):
            result.error(
                f"Plugin '{plugin_id}': index.json {field}='{entry[field]}' "
                f"doesn't match plugin.json {field}='{manifest[field]}'"
            )
    except Exception:
        pass


def _check_unindexed(repo_root, entries, result):
    """A warning for each plugin folder (one with a *_plugin.py) that no
    entry's ``file`` names. A folder with only a plugin.json is not one here,
    though find_plugin_dirs validates it."""
    for dir_name, dir_path in _category_folders(repo_root):
        for folder in dir_path.iterdir():
            if not _may_hold_a_plugin(folder):
                continue
            file_rel = f"{dir_name}/{folder.name}"
            if not any(e.get("file") == file_rel for e in entries) and _has_plugin_module(folder):
                result.warn(f"Plugin directory '{file_rel}' exists but has no index.json entry")


def _disagreements(first, second):
    """Each of AGREED_FIELDS that both copies have and that reads differently
    as text, in AGREED_FIELDS order."""
    for field in AGREED_FIELDS:
        if _both_have(field, first, second) and str(first[field]) != str(second[field]):
            yield field


def _both_have(field, first, second):
    """Whether both copies hold a value for `field` that was read."""
    return field in first and field in second and _read(first[field]) and _read(second[field])


def _category_folders(repo_root):
    """``(name, path)`` for each category folder the repository has, in
    PLUGIN_DIRS order."""
    for dir_name in PLUGIN_DIRS:
        dir_path = repo_root / dir_name
        if dir_path.exists():
            yield dir_name, dir_path


def _may_hold_a_plugin(path):
    """A folder in a category folder that may hold a plugin: a directory
    whose name starts with neither '.' nor '_'."""
    return path.is_dir() and not path.name.startswith((".", "_"))


def _has_plugin_module(path):
    return any(path.glob("*_plugin.py"))


def find_plugin_dirs(repo_root, targets=None):
    """The plugin folders to validate: each named target that is a directory
    (a relative one taken from the repository root), else every folder in the
    category folders with a *_plugin.py or a plugin.json, sorted within each."""
    if targets:
        return _named_dirs(repo_root, targets)
    return [
        folder
        for _, dir_path in _category_folders(repo_root)
        for folder in sorted(dir_path.iterdir())
        if _may_hold_a_plugin(folder) and (_has_plugin_module(folder) or (folder / "plugin.json").exists())
    ]


def _named_dirs(repo_root, targets):
    """The targets that are directories; a line printed for each that is not."""
    dirs = []
    for target in targets:
        path = Path(target)
        if not path.is_absolute():
            path = repo_root / path
        if path.is_dir():
            dirs.append(path)
        else:
            print(f"WARNING: Not a directory: {target}")
    return dirs


def main():
    args = _parse_args()
    repo_root = Path(__file__).parent
    results = [_validated(plugin_path)
               for plugin_path in find_plugin_dirs(repo_root, args.plugins if args.plugins else None)]
    if args.check_index:
        validate_index_json(repo_root, results)
    for result in results:
        rel_path = _relative(result.plugin_path, repo_root)
        _print_outcome(result, rel_path, args.verbose)
        _print_warnings(result, rel_path, args.verbose)
    total_errors = _print_summary(results)
    sys.exit(1 if total_errors > 0 else 0)


def _parse_args():
    parser = argparse.ArgumentParser(description="Validate OpenAVC plugin packages")
    parser.add_argument("plugins", nargs="*", help="Specific plugin directories to validate (default: all)")
    parser.add_argument("--check-index", action="store_true", help="Also validate index.json consistency")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show passing checks")
    return parser.parse_args()


def _validated(plugin_path):
    result = ValidationResult(plugin_path)
    validate_plugin_dir(plugin_path, result)
    return result


def _relative(path, repo_root):
    """`path` from the repository root, or as it is when it lies outside."""
    try:
        return path.relative_to(repo_root)
    except ValueError:
        return path


def _print_outcome(result, rel_path, verbose):
    """A failed result's FAIL line and its errors; a passed one's PASS line,
    only when verbose."""
    if not result.passed:
        print(f"  FAIL  {rel_path}")
        for err in result.errors:
            print(f"        ERROR: {err}")
    elif verbose:
        print(f"  PASS  {rel_path}")


def _print_warnings(result, rel_path, verbose):
    """Each warning. A passed result printed no line of its own when not
    verbose, so each of its warnings gets a WARN line naming the plugin."""
    headed = result.passed and not verbose
    for warn in result.warnings:
        if headed:
            print(f"  WARN  {rel_path}")
        print(f"        WARNING: {warn}")


def _print_summary(results):
    """The closing lines; returns the number of errors."""
    total = len(results)
    passed = sum(1 for result in results if result.passed)
    errors = sum(len(result.errors) for result in results)
    warnings = sum(len(result.warnings) for result in results)
    print()
    print(f"Validated {total} plugin(s): {passed} passed, {total - passed} failed")
    if errors:
        print(f"  {errors} error(s)")
    if warnings:
        print(f"  {warnings} warning(s)")
    return errors


if __name__ == "__main__":
    main()
