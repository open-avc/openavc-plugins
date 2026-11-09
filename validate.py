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

A plugin folder is reported in this order: its *_plugin.py (PLUGIN_INFO, what
its text shows, then CONFIG_SCHEMA, CONFIG_SECRETS, EXTENSIONS and
validate_config), its plugin.json as a manifest and against PLUGIN_INFO, its
README, and its category against its folder. The module is read, never
imported (PluginSource), so a plugin's dependencies need not be installed.

The tables below copy OpenAVC's own plugin rules, because this runs without
OpenAVC installed; each names the OpenAVC module holding the original.
tests/test_validate_matches_platform.py compares them with an OpenAVC checkout
beside this repository. This repository's CI does not run pytest, so that
comparison runs wherever the tests are run by hand.
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

# openavc/core/plugin_manifest.py: REQUIRED_MANIFEST_FIELDS
REQUIRED_MANIFEST_FIELDS = {"id", "name", "version", "author", "description", "category", "license"}

# openavc/core/plugin_manifest.py: VALID_CAPABILITIES
VALID_CAPABILITIES = {
    "state_read", "state_write", "variable_write",
    "event_emit", "event_subscribe",
    "macro_execute", "device_command", "network_listen", "usb_access",
    "http_endpoints", "guest_endpoints",
}

# openavc/core/plugin_manifest.py: VALID_CATEGORIES
VALID_CATEGORIES = {"control_surface", "integration", "sensor", "utility"}

# openavc/core/plugin_manifest.py: MIT_COMPATIBLE_LICENSES
MIT_COMPATIBLE_LICENSES = {
    "mit", "bsd-2-clause", "bsd-3-clause", "apache-2.0", "isc",
    "psf", "unlicense", "0bsd", "cc0-1.0",
}

# Top-level path segments a guest_alias may not shadow, and the shape the whole
# alias must have. openavc/core/plugin_manifest.py: RESERVED_GUEST_ALIASES and
# GUEST_ALIAS_RE.
RESERVED_GUEST_ALIASES = {
    "api", "panel", "programmer", "pair", "setup", "ws", "isc",
    "docs", "redoc", "openapi.json", "assets", "themes", "health",
    "favicon.ico", "static", "simulator",
}
GUEST_ALIAS_PATTERN = r"[a-z][a-z0-9_-]{0,31}"

# The EXTENSIONS types the panel and the Programmer understand, and the field
# that identifies an entry within each type (panel_elements are keyed by
# `type`, every other type by `id`). openavc/core/plugin_extensions.py:
# EXTENSION_ID_FIELD.
EXTENSION_ID_FIELD = {
    "views": "id",
    "device_panels": "id",
    "status_cards": "id",
    "context_actions": "id",
    "panel_elements": "type",
}
VALID_EXTENSION_TYPES = set(EXTENSION_ID_FIELD)

# The keys a CONFIG_SECRETS entry may have. openavc/core/plugin_secrets.py:
# _LIST_DECLARATION_KEYS
_CONFIG_SECRETS_KEYS = {"id", "fields", "generated"}

# The two tables below have no OpenAVC table to compare with, so they are
# checked by eye. The settings field types the Programmer's plugin settings
# form takes:
VALID_CONFIG_TYPES = {
    "string", "text", "integer", "float", "boolean", "select", "color",
    "state_key", "macro_ref", "device_ref", "command_ref", "group", "mapping_list",
}

# "all", and the ids get_platform_id() in openavc/core/plugin_loader.py returns
# for a supported platform (it answers "unknown" for any other):
VALID_PLATFORMS = {"all", "win_x64", "linux_x64", "linux_arm64"}

# The list fields a manifest may carry: the field, what one value is called in
# a message, and the values it may hold. Checked in this order.
_LISTED_FIELDS = (
    ("capabilities", "capability", VALID_CAPABILITIES),
    ("platforms", "platform", VALID_PLATFORMS),
)

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


def _module_level_assignments(tree):
    """Module-level ``NAME = value`` statements with one plain name, for
    resolving references: ``(consts, nodes)``, the values that are literals
    and every value node. A later statement replaces an earlier one's node,
    and its literal value only when it has one."""
    consts = {}
    nodes = {}
    for statement in tree.body:
        name = _single_name(statement)
        if name is None:
            continue
        nodes[name] = statement.value
        try:
            consts[name] = ast.literal_eval(statement.value)
        except Exception:
            pass
    return consts, nodes


def _single_name(statement):
    """The name a ``NAME = value`` statement with exactly one target assigns,
    or None. Unlike a class body (``_assigned_names``), a module-level chain
    ``A = B = value`` is not read, so an alias or constant defined that way is
    not found."""
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
        if isinstance(target, ast.Name):
            return target.id
    return None


def _assigned_names(statement):
    """``(name, value)`` for each plain name a ``NAME = value`` statement
    assigns (a chain ``A = B = value`` assigns two); nothing for any other
    statement."""
    if not isinstance(statement, ast.Assign):
        return []
    return [(t.id, statement.value) for t in statement.targets if isinstance(t, ast.Name)]


def _classes(tree):
    """Every class, nested ones included, in the order ``ast.walk`` meets them."""
    return (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef))


def _body_assignments(cls):
    """``(name, value)`` for every ``NAME = value`` in one class body, in order."""
    for statement in cls.body:
        yield from _assigned_names(statement)


def _class_assignments(tree):
    """``(name, value)`` for every ``NAME = value`` in every class body."""
    for cls in _classes(tree):
        yield from _body_assignments(cls)


class PluginSource:
    """A plugin module's source, parsed once, and the way this validator reads
    each declaration in it without running it.

    Three readings of a declared value, each kept for the declarations that
    use it, and ``config_hooks()`` for the validate_config methods:

    - ``plugin_info()``: PLUGIN_INFO a key at a time, a module constant
      resolved, so one value built at runtime leaves the rest checked.
    - ``literal(name)``: a whole literal, class-level first, following one
      ``NAME = NAME`` alias to the module-level value it names
      (CONFIG_SCHEMA, CONFIG_SECRETS).
    - ``class_literal(name)``: a whole literal written in a class body, no
      alias followed (EXTENSIONS).

    Source that does not parse declares nothing: every reading answers as if
    the name were absent.
    """

    def __init__(self, content):
        self.content = content
        try:
            self.tree = ast.parse(content)
        except SyntaxError:
            self.tree = None
            self._consts, self._module_nodes = {}, {}
        else:
            self._consts, self._module_nodes = _module_level_assignments(self.tree)

    def assignment(self, name):
        """The value node assigned to `name`, class-level first then module-level."""
        if self.tree is None:
            return None
        class_value = self._class_value(name)
        if class_value is not None:
            return class_value
        return self._module_nodes.get(name)

    def _class_value(self, name):
        """The first class-level value assigned to `name`, or None."""
        for assigned, value in _class_assignments(self.tree):
            if assigned == name:
                return value
        return None

    def _followed(self, name):
        """`name`'s value node, with one ``NAME = OTHER`` alias followed to the
        module-level value OTHER names."""
        node = self.assignment(name)
        if isinstance(node, ast.Name):
            node = self._module_nodes.get(node.id)
        return node

    def plugin_info(self):
        """The PLUGIN_INFO dict, read a key at a time.

        Returns ``(info, unresolved)`` -- the keys it could evaluate, and the
        names of the keys it could not. Both halves matter: a plugin whose
        PLUGIN_INFO can't be read gets NO validation at all, and reading it as a
        single ``literal_eval`` is too strict for two shapes real plugins use.
        One holds a module constant as a value (``"guest_alias": _GUEST_ALIAS``),
        the other aliases a module-level dict into the class (``PLUGIN_INFO =
        PLUGIN_INFO``). Either one used to make the whole extraction return
        None, silently, so three of the shipped plugins were validated on
        plugin.json alone -- which is how one of them shipped enforcing a lower
        min_openavc_version than its catalog entry advertised. So: resolve
        module constants, follow one alias, and evaluate the dict a key at a
        time rather than all-or-nothing. ``(None, [])`` when there is no dict to
        read.
        """
        node = self._followed("PLUGIN_INFO")
        if not isinstance(node, ast.Dict):
            return None, []
        info, unresolved = {}, []
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            found, resolved = self._key_value(value)
            if found:
                info[key.value] = resolved
            else:
                unresolved.append(key.value)
        return info, unresolved

    def _key_value(self, value):
        """``(True, value)`` for a literal or a module constant's name,
        ``(False, None)`` for anything computed."""
        try:
            return True, ast.literal_eval(value)
        except Exception:
            if isinstance(value, ast.Name) and value.id in self._consts:
                return True, self._consts[value.id]
            return False, None

    def literal(self, name):
        """`name`'s literal value, following one alias to the module; None
        when it is not there or not a literal."""
        return _literal_or_none(self._followed(name))

    def class_literal(self, name):
        """The first literal assigned to `name` in a class body; None when
        there is none, or when that first one is not a literal."""
        if self.tree is None:
            return None
        return _literal_or_none(self._class_value(name))

    def config_hooks(self):
        """Each ``validate_config`` defined in a class whose body assigns
        PLUGIN_INFO, in the order ``ast.walk`` meets the classes."""
        if self.tree is None:
            return
        for cls in _classes(self.tree):
            if _assigns(cls, "PLUGIN_INFO"):
                yield from (item for item in cls.body if _defines(item, "validate_config"))


def _literal_or_none(node):
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _assigns(cls, name):
    """Whether a class body has a ``NAME = value`` statement for `name`."""
    return any(assigned == name for assigned, _ in _body_assignments(cls))


def _defines(statement, name):
    return isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name == name


def validate_plugin_info(plugin_info, result, source="PLUGIN_INFO", unresolved=()):
    """Validate a PLUGIN_INFO dict, or plugin.json read as one; `source` names
    which in every message.

    `unresolved` names keys that are present in the source but whose value is
    computed at runtime -- they are declared, so they are not missing. The
    required fields come first, then each of _FIELD_CHECKS in order, each
    reporting every problem it finds.
    """
    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in plugin_info and field not in unresolved:
            result.error(f"{source}: missing required field '{field}'")
    for check in _FIELD_CHECKS:
        check(plugin_info, source, result)


def _check_id(info, source, result):
    plugin_id = info.get("id", "")
    if not plugin_id:
        return
    if not validate_id_format(plugin_id):
        result.error(f"{source}: invalid plugin ID '{plugin_id}': must be lowercase letters, numbers, and underscores, starting with a letter")
    if "." in plugin_id:
        result.error(f"{source}: plugin ID must not contain dots (breaks state key parsing)")


def _check_category(info, source, result):
    if "category" in info and info["category"] not in VALID_CATEGORIES:
        result.error(f"{source}: invalid category '{info['category']}': must be one of {sorted(VALID_CATEGORIES)}")


def _check_license(info, source, result):
    if "license" in info and not is_mit_compatible(info["license"]):
        result.error(f"{source}: license '{info['license']}' is not MIT-compatible. Must be one of: {sorted(MIT_COMPATIBLE_LICENSES)}")


def _check_listed(info, source, result):
    """Each of _LISTED_FIELDS present must be a list of its allowed values."""
    for field, noun, allowed in _LISTED_FIELDS:
        if field not in info:
            continue
        if not isinstance(info[field], list):
            result.error(f"{source}: {field} must be a list")
            continue
        for value in _unknown(info[field], allowed):
            result.error(f"{source}: invalid {noun} '{value}': must be one of {sorted(allowed)}")


def _check_guest_alias(info, source, result):
    """The guest router's short top-level route: one lowercase URL segment that
    is not a platform path, and only with the guest_endpoints capability."""
    if "guest_alias" not in info:
        return
    alias = info["guest_alias"]
    if not _is_url_segment(alias):
        result.error(f"{source}: invalid guest_alias '{alias}': must be one lowercase URL segment (letters, digits, hyphens, underscores; max 32 chars)")
    elif alias in RESERVED_GUEST_ALIASES:
        result.error(f"{source}: guest_alias '{alias}' is a reserved platform path")
    if "guest_endpoints" not in info.get("capabilities", []):
        result.error(f"{source}: guest_alias requires the guest_endpoints capability")


def _is_url_segment(alias):
    return isinstance(alias, str) and bool(re.fullmatch(GUEST_ALIAS_PATTERN, alias))


def _check_version(info, source, result):
    if "version" in info and not re.match(r'^\d+\.\d+\.\d+', str(info["version"])):
        result.warn(f"{source}: version '{info['version']}' doesn't follow semver format (X.Y.Z)")


def _check_dependencies(info, source, result):
    """Each must be a plain named requirement (the installer rejects
    URLs/VCS/pip-options), plus a license reminder when there are any."""
    if "dependencies" not in info:
        return
    dependencies = info["dependencies"]
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


# The checks validate_plugin_info runs after the required fields, in order.
_FIELD_CHECKS = (
    _check_id,
    _check_category,
    _check_license,
    _check_listed,
    _check_guest_alias,
    _check_version,
    _check_dependencies,
)


def is_mit_compatible(name):
    """Whether a license name is one of MIT_COMPATIBLE_LICENSES, in any case."""
    return name.lower() in MIT_COMPATIBLE_LICENSES


def _unknown(values, allowed):
    """Each value not in `allowed`, in order."""
    return (value for value in values if value not in allowed)


def validate_config_schema(config_schema, result):
    """Validate a CONFIG_SCHEMA dict, a group's fields included."""
    if not isinstance(config_schema, dict):
        result.error("CONFIG_SCHEMA must be a dict")
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
    if field_type not in VALID_CONFIG_TYPES:
        result.error(f"CONFIG_SCHEMA field '{field_id}' has invalid type '{field_type}': must be one of {sorted(VALID_CONFIG_TYPES)}")
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
    """A mapping_list needs its item_schema, and no column of it may be a
    login: a mapping_list row has no id, so OpenAVC has nothing to keep a
    row's login under. A list of logins is declared in CONFIG_SECRETS."""
    if "item_schema" not in field_def:
        result.error(f"CONFIG_SCHEMA field '{field_id}' is type 'mapping_list' but missing 'item_schema'")
        return
    item_schema = field_def["item_schema"]
    if not isinstance(item_schema, dict):
        return
    for column_id, column in item_schema.items():
        if isinstance(column, dict) and "secret" in column:
            result.error(
                f"CONFIG_SCHEMA field '{field_id}' column '{column_id}' has 'secret': "
                "a mapping_list row has no id to keep a login under. Keep the list "
                "outside CONFIG_SCHEMA and declare it in CONFIG_SECRETS."
            )


# What a settings field of each of these types needs besides its type.
_TYPE_CHECKS = {
    "select": _check_select,
    "group": _check_group,
    "mapping_list": _check_mapping_list,
}


def validate_config_secrets(config_secrets, config_schema, result):
    """Validate a CONFIG_SECRETS dict: the config lists whose items hold a
    login, keyed by the list's config key."""
    if not isinstance(config_secrets, dict):
        result.error("CONFIG_SECRETS must be a dict")
        return
    schema_keys = set(config_schema) if isinstance(config_schema, dict) else set()
    for list_key, entry in config_secrets.items():
        _check_secret_list(list_key, entry, schema_keys, result)


def _check_secret_list(list_key, entry, schema_keys, result):
    """One declared list: its config key, then its entry."""
    where = f"CONFIG_SECRETS['{list_key}']"
    if not isinstance(list_key, str) or not list_key:
        result.error(f"CONFIG_SECRETS has a key that is not a non-empty string: {list_key!r}")
        return
    if list_key in schema_keys:
        result.error(
            f"{where}: '{list_key}' is also a CONFIG_SCHEMA field. Mark a schema field "
            "with 'secret': True instead."
        )
    _check_secret_entry(entry, where, result)


def _check_secret_entry(entry, where, result):
    """A list's declaration: a dict of known keys, with the id key, the login
    fields and, optionally, whether the plugin makes the logins itself."""
    if not isinstance(entry, dict):
        result.error(f"{where} must be a dict with 'id' and 'fields'")
        return
    unknown = sorted(set(entry) - _CONFIG_SECRETS_KEYS)
    if unknown:
        result.error(f"{where} has unknown key(s) {unknown}: only 'id', 'fields' and 'generated'")
    id_key = entry.get("id")
    if not _is_name(id_key):
        result.error(f"{where} needs 'id': the key in each item that holds its unique id")
    _check_secret_fields(entry.get("fields"), id_key, where, result)
    if "generated" in entry and not isinstance(entry["generated"], bool):
        result.error(f"{where}['generated'] must be true or false")


def _check_secret_fields(fields, id_key, where, result):
    """A list's 'fields': a non-empty list of distinct non-empty names, none of
    them the id key."""
    if not isinstance(fields, list) or not fields:
        result.error(f"{where} needs 'fields': a list of the keys in each item that hold a login")
        return
    names = [f for f in fields if _is_name(f)]
    if len(names) != len(fields):
        result.error(f"{where}['fields'] must hold non-empty strings only")
    if len(set(names)) != len(names):
        result.error(f"{where}['fields'] names a key twice")
    if isinstance(id_key, str) and id_key in names:
        result.error(f"{where}: the id key '{id_key}' cannot also be a login")


def _is_name(value):
    """A non-empty string."""
    return isinstance(value, str) and bool(value)


def validate_extensions_schema(extensions, result):
    """Validate an EXTENSIONS dict (the rules of openavc/core/plugin_extensions.py).

    Each type must be a known key holding a list of dicts, and every entry needs
    its identifier field (``id``, or ``type`` for panel_elements) unique within
    the type. The loader rejects a malformed EXTENSIONS at enable time, so a
    contributor should catch it here first.
    """
    if not isinstance(extensions, dict):
        result.error("EXTENSIONS must be a dict")
        return
    for ext_type, ext_list in extensions.items():
        _check_extension_list(ext_type, ext_list, result)


def _check_extension_list(ext_type, ext_list, result):
    if ext_type not in VALID_EXTENSION_TYPES:
        result.error(
            f"EXTENSIONS has unknown type '{ext_type}': must be one of "
            f"{sorted(VALID_EXTENSION_TYPES)}"
        )
        return
    if not isinstance(ext_list, list):
        result.error(f"EXTENSIONS['{ext_type}'] must be a list")
        return
    id_field = EXTENSION_ID_FIELD[ext_type]
    seen = set()
    for i, ext in enumerate(ext_list):
        if not isinstance(ext, dict):
            result.error(f"EXTENSIONS['{ext_type}'][{i}] must be a dict")
            continue
        ext_id = ext.get(id_field)
        if not _is_name(ext_id):
            result.error(f"EXTENSIONS['{ext_type}'][{i}] missing '{id_field}' (string)")
            continue
        if ext_id in seen:
            result.error(f"EXTENSIONS['{ext_type}'] has duplicate {id_field} '{ext_id}'")
        seen.add(ext_id)


def validate_config_hook(source, file_name, result):
    """The optional ``validate_config`` is called on the plugin class (which may
    not be running) before its config is saved, synchronously. An instance
    method or an async one is never asked, which reads as a check that passes
    everything."""
    for hook in source.config_hooks():
        decorators = {d.id for d in hook.decorator_list if isinstance(d, ast.Name)}
        if isinstance(hook, ast.AsyncFunctionDef):
            result.error(f"{file_name}: validate_config must not be async -- "
                         "OpenAVC calls it synchronously before saving a config")
        elif not decorators & {"classmethod", "staticmethod"}:
            result.error(f"{file_name}: validate_config must be a @classmethod or "
                         "@staticmethod -- OpenAVC calls it on the class, which may not be running")


def validate_plugin_dir(plugin_path, result):
    """Validate a plugin directory, reported in this order: its *_plugin.py
    (PLUGIN_INFO, what its text shows, its declarations), its plugin.json
    against PLUGIN_INFO, its README, and its category against its folder.
    Without a module it can read, it stops there."""
    main_file = _main_module(plugin_path, result)
    if main_file is None:
        return
    content = _read_module(main_file, result)
    if content is None:
        return
    if "PLUGIN_INFO" not in content:
        result.error(f"{main_file.name}: missing PLUGIN_INFO class attribute")
    source = PluginSource(content)
    plugin_info = _check_plugin_info(source, main_file.name, plugin_path, result)
    _check_habits(content, main_file.name, result)
    _check_declarations(source, main_file.name, result)
    _check_plugin_json(plugin_path, plugin_info, result)
    _check_readme(plugin_path, result)
    _check_category_folder(plugin_path, plugin_info, result)


def _main_module(plugin_path, result):
    """The first *_plugin.py the folder lists, or None; with several, a
    warning naming them."""
    plugin_files = list(plugin_path.glob("*_plugin.py"))
    if not plugin_files:
        result.error("No *_plugin.py file found (plugin loader looks for this pattern)")
        return None
    if len(plugin_files) > 1:
        result.warn(f"Multiple *_plugin.py files found: {[f.name for f in plugin_files]}")
    return plugin_files[0]


def _read_module(main_file, result):
    try:
        return main_file.read_text(encoding="utf-8")
    except Exception as e:
        result.error(f"Cannot read {main_file.name}: {e}")
        return None


def _check_plugin_info(source, file_name, plugin_path, result):
    """PLUGIN_INFO: readable, valid, and named for its folder. Returns what
    could be read of it (None when nothing could; an empty dict is not
    validated)."""
    plugin_info, unresolved = source.plugin_info()
    if plugin_info is None:
        result.warn(f"{file_name}: could not read PLUGIN_INFO statically -- "
                    "it is not being validated. Declare it as a dict literal.")
    if unresolved:
        result.warn(f"{file_name}: PLUGIN_INFO values built at runtime are not "
                    f"validated: {sorted(unresolved)}")
    if plugin_info:
        validate_plugin_info(plugin_info, result, unresolved=unresolved)
        _check_folder_name(plugin_info, plugin_path, result)
    return plugin_info


def _check_folder_name(plugin_info, plugin_path, result):
    plugin_id = plugin_info.get("id", "")
    if plugin_id and plugin_id != plugin_path.name:
        result.warn(f"Plugin ID '{plugin_id}' doesn't match directory name '{plugin_path.name}'")


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


def _check_declarations(source, file_name, result):
    """CONFIG_SCHEMA, CONFIG_SECRETS, EXTENSIONS and validate_config, each
    read only when its name appears in the text. Only a literal can be checked;
    CONFIG_SECRETS alone says when it could not be read."""
    config_schema = _checked_config_schema(source, result)
    if "CONFIG_SECRETS" in source.content:
        _check_config_secrets(source, config_schema, file_name, result)
    if "EXTENSIONS" in source.content:
        extensions = source.class_literal("EXTENSIONS")
        if extensions is not None:
            validate_extensions_schema(extensions, result)
    if "validate_config" in source.content:
        validate_config_hook(source, file_name, result)


def _checked_config_schema(source, result):
    """CONFIG_SCHEMA, validated, or None when it is absent or not a literal."""
    if "CONFIG_SCHEMA" not in source.content:
        return None
    config_schema = source.literal("CONFIG_SCHEMA")
    if config_schema is not None:
        validate_config_schema(config_schema, result)
    return config_schema


def _check_config_secrets(source, config_schema, file_name, result):
    config_secrets = source.literal("CONFIG_SECRETS")
    if config_secrets is None:
        result.warn(f"{file_name}: could not read CONFIG_SECRETS statically -- "
                    "it is not being validated. Declare it as a dict literal.")
    else:
        validate_config_secrets(config_secrets, config_schema, result)


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
    if not plugin_info or "category" not in plugin_info:
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
    """The entry's category, license and capabilities, by the same rules as a
    manifest's, in shorter messages. A capabilities value that is not a list
    is not reported here."""
    if "category" in entry and entry["category"] not in VALID_CATEGORIES:
        result.error(f"Plugin '{plugin_id}': invalid category '{entry['category']}'")
    if "license" in entry and not is_mit_compatible(entry["license"]):
        result.error(f"Plugin '{plugin_id}': license '{entry['license']}' is not MIT-compatible")
    if "capabilities" in entry and isinstance(entry["capabilities"], list):
        for cap in _unknown(entry["capabilities"], VALID_CAPABILITIES):
            result.error(f"Plugin '{plugin_id}': invalid capability '{cap}'")


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
    return field in first and field in second


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
