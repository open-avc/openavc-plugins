"""What ``validate.py`` reports, message for message.

``validate.py`` is the check every plugin pull request runs (``python validate.py
--check-index``) and the one contributors run before submitting. These tests
pin what it says today for each shape a plugin folder, its source, its
plugin.json and index.json can take: the exact error and warning sentences,
their order, which checks run and which are skipped, the printed report and the
exit code. A sentence that looks wrong is pinned as it is, so a cleanup of the
validator cannot change what a contributor is told without a test saying so.

What a plugin declares is judged by OpenAVC's own rules (``scripts/_vendor/``),
so those sentences are the platform's: a rule change there that moves one fails
here too, once the copies are regenerated.

Every plugin here is an invented one (``acme_widget``) in a temporary folder;
only the last test reads this repository's real plugins.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import validate as v  # noqa: E402


GOOD_INFO = {
    "id": "acme_widget",
    "name": "Acme Widget",
    "version": "1.0.0",
    "author": "Acme",
    "description": "Controls an Acme widget",
    "category": "integration",
    "license": "MIT",
}

GOOD_SOURCE = '''
class AcmeWidgetPlugin:
    PLUGIN_INFO = {
        "id": "acme_widget",
        "name": "Acme Widget",
        "version": "1.0.0",
        "author": "Acme",
        "description": "Controls an Acme widget",
        "category": "integration",
        "license": "MIT",
    }

    async def start(self, api):
        self.api = api

    async def stop(self):
        pass
'''


def _plugin(root, source=GOOD_SOURCE, manifest=GOOD_INFO, readme=True,
            category_dir="integrations", name="acme_widget", file_name=None):
    """A plugin folder under ``root/<category_dir>/<name>``. ``manifest`` is
    written as JSON when a dict or list, as raw text when a str, and left out
    when None."""
    folder = root / category_dir / name
    folder.mkdir(parents=True)
    if source is not None:
        (folder / (file_name or f"{name}_plugin.py")).write_text(source, encoding="utf-8")
    if isinstance(manifest, (dict, list)):
        (folder / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    elif isinstance(manifest, str):
        (folder / "plugin.json").write_text(manifest, encoding="utf-8")
    if readme:
        (folder / "README.md").write_text("# Acme Widget\n", encoding="utf-8")
    return folder


def _check_dir(folder):
    result = v.ValidationResult(folder)
    v.validate_plugin_dir(folder, result)
    return result.errors, result.warnings


def _check_info(info, **kwargs):
    result = v.ValidationResult(Path("x"))
    v.validate_plugin_info(info, result, **kwargs)
    return result.errors, result.warnings


def _info(**changes):
    info = dict(GOOD_INFO)
    for key, value in changes.items():
        if value is _DROP:
            info.pop(key)
        else:
            info[key] = value
    return info


_DROP = object()


def _source_with(info_lines, body="", extra_module=""):
    """Plugin source whose class declares ``info_lines`` as PLUGIN_INFO's
    body and ``body`` after it (indented by the caller)."""
    return (
        f"{extra_module}\n"
        "class AcmeWidgetPlugin:\n"
        f"    PLUGIN_INFO = {info_lines}\n"
        f"{body}\n"
        "    async def start(self, api):\n"
        "        pass\n\n"
        "    async def stop(self):\n"
        "        pass\n"
    )


# ---------------------------------------------------------------------------
# The small rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("req, safe", [
    ("pkg", True),
    ("Pkg", True),
    ("pkg>=1.0", True),
    ("pkg[extra]", True),
    ('pkg; sys_platform=="win32"', True),
    ("  pkg  ", True),
    ("zeroconf==0.1", True),
    (123, True),
    ("", False),
    ("   ", False),
    ("-r requirements.txt", False),
    ("--index-url=https://x", False),
    ("./local", False),
    (".hidden", False),
    ("/abs/path", False),
    ("\\windows\\path", False),
    ("http://example.com/pkg.tar.gz", False),
    ("file:///tmp/pkg", False),
    ("pkg @ https://example.com/pkg.whl", False),
    ("pkg@1", False),
    ("git+https://example.com/repo", False),
    ("GIT+https://example.com/repo", False),
    ("GIT+example", False),
    ("Hg+repo", False),
    ("hg+https://example.com/repo", False),
    ("svn+https://example.com/repo", False),
    ("bzr+https://example.com/repo", False),
    ("pkg-git+x", False),
    ("_private", False),
    ("~tilde", False),
])
def test_is_safe_requirement(req, safe):
    assert v.is_safe_requirement(req) is safe


@pytest.mark.parametrize("plugin_id, ok", [
    ("acme", True),
    ("acme_widget2", True),
    ("a", True),
    ("Acme", False),
    ("1acme", False),
    ("_acme", False),
    ("acme.widget", False),
    ("acme-widget", False),
    ("", False),
    ("acme widget", False),
])
def test_validate_id_format(plugin_id, ok):
    assert v.validate_id_format(plugin_id) is ok


def test_validation_result_collects_and_passes_only_without_errors():
    r = v.ValidationResult(Path("p"))
    assert r.plugin_path == Path("p")
    assert r.passed is True
    r.warn("w1")
    assert r.passed is True
    r.error("e1")
    r.error("e2")
    assert r.passed is False
    assert r.errors == ["e1", "e2"]
    assert r.warnings == ["w1"]


def test_the_tables():
    assert v.PLUGIN_DIRS == ["control_surfaces", "integrations", "sensors", "utility"]
    assert "mapping_list" in v.VALID_CONFIG_TYPES and "group" in v.VALID_CONFIG_TYPES
    assert len(v.VALID_CONFIG_TYPES) == 13


def test_the_rules_and_the_reader_are_openavcs_own():
    """No copy of OpenAVC's tables, rules or source reader is kept here: they
    are the vendored platform modules, held to the platform by
    scripts/vendor_platform_rules.py --check."""
    assert v.rules.__name__ == "_vendor.plugin_declarations"
    assert v.read_folder.__module__ == "_vendor.plugin_source"
    for copy in ("PluginSource", "REQUIRED_MANIFEST_FIELDS", "VALID_CAPABILITIES",
                 "VALID_PLATFORMS", "EXTENSION_ID_FIELD", "validate_config_secrets",
                 "validate_extensions_schema", "is_mit_compatible"):
        assert not hasattr(v, copy), copy


# ---------------------------------------------------------------------------
# The validate_config hook
# ---------------------------------------------------------------------------


def _hook(src, class_name="C"):
    import ast
    result = v.ValidationResult(Path("x"))
    v.validate_config_hook(ast.parse(src), class_name, "acme_plugin.py", result)
    return result.errors, result.warnings


_ASYNC_HOOK = ("acme_plugin.py: validate_config must not be async -- "
               "OpenAVC calls it synchronously before saving a config")
_INSTANCE_HOOK = ("acme_plugin.py: validate_config must be a @classmethod or "
                  "@staticmethod -- OpenAVC calls it on the class, which may not be running")


@pytest.mark.parametrize("decorator, body_prefix, expected", [
    ("@classmethod\n    ", "def", []),
    ("@staticmethod\n    ", "def", []),
    ("", "def", [_INSTANCE_HOOK]),
    ("@other\n    ", "def", [_INSTANCE_HOOK]),
    ("@builtins.classmethod\n    ", "def", [_INSTANCE_HOOK]),
    ("@classmethod\n    ", "async def", [_ASYNC_HOOK]),
    ("", "async def", [_ASYNC_HOOK]),
    ("@functools.wraps(f)\n    @staticmethod\n    ", "def", []),
])
def test_validate_config_hook(decorator, body_prefix, expected):
    src = (
        "class C:\n"
        "    PLUGIN_INFO = {'id': 'acme'}\n"
        f"    {decorator}{body_prefix} validate_config(cls, config):\n"
        "        return []\n"
    )
    assert _hook(src) == (expected, [])


def test_validate_config_hook_reads_only_the_plugin_class():
    src = (
        "class Helper:\n"
        "    def validate_config(self, config):\n"
        "        return []\n"
        "class C:\n"
        "    PLUGIN_INFO = {'id': 'acme'}\n"
        "    def validate_config(self, config):\n"
        "        return []\n"
        "    class Inner:\n"
        "        async def validate_config(cls, config):\n"
        "            return []\n"
    )
    assert _hook(src) == ([_INSTANCE_HOOK], [])


def test_validate_config_hook_reads_every_definition_in_the_class_the_module_holds():
    """A class defined twice is the later one, as the module holds it."""
    src = (
        "class C:\n"
        "    @classmethod\n"
        "    async def validate_config(cls, c):\n"
        "        pass\n"
        "class C:\n"
        "    def validate_config(self, c):\n"
        "        pass\n"
        "    async def validate_config(self, c):\n"
        "        pass\n"
    )
    assert _hook(src) == ([_INSTANCE_HOOK, _ASYNC_HOOK], [])
    assert _hook(src, "Missing") == ([], [])


# ---------------------------------------------------------------------------
# PLUGIN_INFO and plugin.json fields
# ---------------------------------------------------------------------------


def test_a_good_manifest_says_nothing():
    assert _check_info(dict(GOOD_INFO)) == ([], [])


def test_missing_required_fields_in_one_sorted_sentence():
    assert _check_info({}) == ([
        "PLUGIN_INFO: Missing required fields: ['author', 'category', 'description', 'id', "
        "'license', 'name', 'version']"], [])
    assert _check_info(_info(author=_DROP, license=_DROP))[0] == [
        "PLUGIN_INFO: Missing required fields: ['author', 'license']"]


def test_the_source_label():
    errors, _ = _check_info(_info(category="nope"), source="plugin.json")
    assert errors == [
        "plugin.json: Invalid category 'nope', must be one of "
        "['control_surface', 'integration', 'sensor', 'utility']"
    ]


@pytest.mark.parametrize("plugin_id, expected", [
    ("acme_widget", []),
    ("", []),
    ("Acme", ["PLUGIN_INFO: invalid plugin ID 'Acme': must be lowercase letters, numbers, "
              "and underscores, starting with a letter"]),
    ("acme.widget", [
        "PLUGIN_INFO: invalid plugin ID 'acme.widget': must be lowercase letters, numbers, "
        "and underscores, starting with a letter",
        "PLUGIN_INFO: plugin ID must not contain dots (breaks state key parsing)",
    ]),
    (5, ["PLUGIN_INFO: invalid plugin ID '5': must be lowercase letters, numbers, "
         "and underscores, starting with a letter"]),
])
def test_plugin_id(plugin_id, expected):
    assert _check_info(_info(id=plugin_id)) == (expected, [])


def test_category():
    assert _check_info(_info(category="sensor")) == ([], [])
    assert _check_info(_info(category="lighting"))[0] == [
        "PLUGIN_INFO: Invalid category 'lighting', must be one of "
        "['control_surface', 'integration', 'sensor', 'utility']"
    ]


@pytest.mark.parametrize("license_name, errors", [
    ("Apache-2.0", []),
    ("mit", []),
    (" MIT ", []),
    ("GPL-3.0", ["PLUGIN_INFO: License 'GPL-3.0' is not MIT-compatible"]),
    (1, ["PLUGIN_INFO: License '1' is not MIT-compatible"]),
])
def test_license(license_name, errors):
    assert _check_info(_info(license=license_name)) == (errors, [])


def test_capabilities():
    assert _check_info(_info(capabilities=["state_read", "usb_access"])) == ([], [])
    assert _check_info(_info(capabilities="state_read"))[0] == ["PLUGIN_INFO: capabilities must be a list"]
    assert _check_info(_info(capabilities=["state_read", "fly", "swim"]))[0] == [
        "PLUGIN_INFO: Unknown capabilities: ['fly', 'swim']"]


def test_platforms():
    assert _check_info(_info(platforms=["all", "linux_arm64"])) == ([], [])
    assert _check_info(_info(platforms="all"))[0] == ["PLUGIN_INFO: platforms must be a list"]
    assert _check_info(_info(platforms=["mac_arm64"]))[0] == [
        "PLUGIN_INFO: Unknown platforms: ['mac_arm64']; each must be one of "
        "['all', 'linux_arm64', 'linux_x64', 'win_x64']"
    ]


_BAD_ALIAS = ("must be one lowercase URL segment (letters, digits, hyphens, "
              "underscores; max 32 chars)")
_NEEDS_GUEST = "PLUGIN_INFO: guest_alias requires the guest_endpoints capability"


@pytest.mark.parametrize("alias, caps, expected", [
    ("present", ["guest_endpoints"], []),
    ("a-b_c9", ["guest_endpoints"], []),
    ("a" * 32, ["guest_endpoints"], []),
    ("a" * 33, ["guest_endpoints"], [f"PLUGIN_INFO: Invalid guest_alias '{'a' * 33}': {_BAD_ALIAS}"]),
    ("Present", ["guest_endpoints"], [f"PLUGIN_INFO: Invalid guest_alias 'Present': {_BAD_ALIAS}"]),
    (7, ["guest_endpoints"], [f"PLUGIN_INFO: Invalid guest_alias '7': {_BAD_ALIAS}"]),
    ("api", ["guest_endpoints"], ["PLUGIN_INFO: guest_alias 'api' is a reserved platform path"]),
    ("simulator", [], ["PLUGIN_INFO: guest_alias 'simulator' is a reserved platform path"]),
    ("present", [], [_NEEDS_GUEST]),
    ("present", None, [_NEEDS_GUEST]),
    ("present", "guest_endpoints", ["PLUGIN_INFO: capabilities must be a list"]),
])
def test_guest_alias(alias, caps, expected):
    """OpenAVC names the first thing wrong with an alias."""
    info = _info(guest_alias=alias)
    if caps is not None:
        info["capabilities"] = caps
    assert _check_info(info) == (expected, [])


@pytest.mark.parametrize("version, warns", [
    ("1.0.0", False),
    ("1.0.0-beta.1", False),
    ("10.20.30abc", False),
    ("1.0", True),
    ("v1.0.0", True),
    (1, True),
])
def test_version_is_a_warning(version, warns):
    errors, warnings = _check_info(_info(version=version))
    assert errors == []
    expected = [f"PLUGIN_INFO: version '{version}' doesn't follow semver format (X.Y.Z)"] if warns else []
    assert warnings == expected


def test_dependencies():
    assert _check_info(_info(dependencies=[])) == ([], [])
    assert _check_info(_info(dependencies="paho-mqtt")) == (["PLUGIN_INFO: dependencies must be a list"], [])
    errors, warnings = _check_info(_info(dependencies=["paho-mqtt>=2", "git+https://x/y", "-r reqs"]))
    assert errors == [
        "PLUGIN_INFO: unsafe dependency 'git+https://x/y' -- must be a plain 'package' or "
        "'package>=x.y' requirement (no URLs, VCS, pip options, or '@' direct references)",
        "PLUGIN_INFO: unsafe dependency '-r reqs' -- must be a plain 'package' or "
        "'package>=x.y' requirement (no URLs, VCS, pip options, or '@' direct references)",
    ]
    assert warnings == [
        "PLUGIN_INFO: has pip dependencies ['paho-mqtt>=2', 'git+https://x/y', '-r reqs'] "
        "-- ensure all are MIT-compatible"
    ]


def test_every_field_check_in_its_order():
    """OpenAVC's rules in the order it refuses a plugin, the platform ids,
    then the checks made only here."""
    info = {
        "id": "Bad.Id",
        "name": "n",
        "author": "a",
        "description": "d",
        "category": "nope",
        "license": "GPL",
        "capabilities": ["x"],
        "platforms": ["y"],
        "guest_alias": "api",
        "version": "1",
        "dependencies": ["-e ."],
    }
    errors, warnings = _check_info(info)
    assert errors == [
        "PLUGIN_INFO: License 'GPL' is not MIT-compatible",
        "PLUGIN_INFO: Invalid category 'nope', must be one of "
        "['control_surface', 'integration', 'sensor', 'utility']",
        "PLUGIN_INFO: Unknown capabilities: ['x']",
        "PLUGIN_INFO: guest_alias 'api' is a reserved platform path",
        "PLUGIN_INFO: Unknown platforms: ['y']; each must be one of "
        "['all', 'linux_arm64', 'linux_x64', 'win_x64']",
        "PLUGIN_INFO: invalid plugin ID 'Bad.Id': must be lowercase letters, numbers, "
        "and underscores, starting with a letter",
        "PLUGIN_INFO: plugin ID must not contain dots (breaks state key parsing)",
        "PLUGIN_INFO: unsafe dependency '-e .' -- must be a plain 'package' or "
        "'package>=x.y' requirement (no URLs, VCS, pip options, or '@' direct references)",
    ]
    assert warnings == [
        "PLUGIN_INFO: version '1' doesn't follow semver format (X.Y.Z)",
        "PLUGIN_INFO: has pip dependencies ['-e .'] -- ensure all are MIT-compatible",
    ]


# ---------------------------------------------------------------------------
# CONFIG_SCHEMA's fields
# ---------------------------------------------------------------------------


def _schema(schema):
    result = v.ValidationResult(Path("x"))
    v.validate_config_schema(schema, result)
    return result.errors, result.warnings


def test_a_config_schema_that_is_not_a_dict_is_openavcs_to_refuse():
    assert _schema([1]) == ([], [])


_LOGIN_COLUMN = (
    "CONFIG_SCHEMA field 'ml_ok' column '{}' holds a login, but a mapping_list row has no id "
    "to keep a login under, so OpenAVC keeps it in the project. Keep the list outside "
    "CONFIG_SCHEMA and declare it in CONFIG_SECRETS.")


def test_config_schema_fields():
    allowed = sorted(v.VALID_CONFIG_TYPES)
    errors, warnings = _schema({
        "ok": {"type": "string"},
        "notdict": "string",
        "secret_bad": {"type": "string", "secret": "true"},
        "secret_ok": {"type": "string", "secret": False},
        "notype": {"label": "x", "secret": 1},
        "badtype": {"type": "table"},
        "listtype": {"type": ["select"]},
        "sel": {"type": "select"},
        "sel_ok": {"type": "select", "options": []},
        "grp": {"type": "group"},
        "grp_nondict": {"type": "group", "fields": ["a"]},
        "grp_ok": {"type": "group", "fields": {
            "inner_bad": {"type": "nope"},
            "inner_notype": {},
            "nested": {"type": "group", "fields": {"deep": {"type": "select"}}},
        }},
        "ml": {"type": "mapping_list"},
        "ml_nondict": {"type": "mapping_list", "item_schema": ["a"]},
        "ml_ok": {"type": "mapping_list", "item_schema": {
            "host": {"type": "string"},
            "key": {"type": "string", "secret": True},
            "token": {"type": "string", "secret": False},
            "password": {"type": "string"},
            "pin": {"type": "password"},
            "raw": "string",
        }},
    })
    assert errors == [
        "CONFIG_SCHEMA field 'notdict' must be a dict",
        "CONFIG_SCHEMA field 'secret_bad' has 'secret' that is not true or false",
        "CONFIG_SCHEMA field 'notype' has 'secret' that is not true or false",
        "CONFIG_SCHEMA field 'notype' missing 'type'",
        f"CONFIG_SCHEMA field 'badtype' has invalid type 'table': must be one of {allowed}",
        f"CONFIG_SCHEMA field 'listtype' has invalid type '['select']': must be one of {allowed}",
        "CONFIG_SCHEMA field 'sel' is type 'select' but missing 'options' list",
        "CONFIG_SCHEMA field 'grp' is type 'group' but missing 'fields' dict",
        f"CONFIG_SCHEMA field 'inner_bad' has invalid type 'nope': must be one of {allowed}",
        "CONFIG_SCHEMA field 'inner_notype' missing 'type'",
        "CONFIG_SCHEMA field 'deep' is type 'select' but missing 'options' list",
        "CONFIG_SCHEMA field 'ml' is type 'mapping_list' but missing 'item_schema'",
    ]
    assert warnings == [_LOGIN_COLUMN.format(c) for c in ("key", "password", "pin")]


def test_a_group_whose_fields_are_not_a_dict_says_nothing_more():
    assert _schema({"g": {"type": "group", "fields": None}}) == ([], [])


# ---------------------------------------------------------------------------
# A plugin folder
# ---------------------------------------------------------------------------


def test_a_good_plugin_folder_says_nothing(tmp_path):
    assert _check_dir(_plugin(tmp_path)) == ([], [])


def test_a_plugin_module_by_another_name_is_found_as_openavc_finds_it(tmp_path):
    folder = _plugin(tmp_path, file_name="acme_widget.py")
    assert _check_dir(folder) == ([], [])


def test_a_folder_with_no_module(tmp_path):
    assert _check_dir(_plugin(tmp_path, source=None)) == ([NO_PLUGIN_CLASS], [])


def test_a_second_plugin_module_openavc_never_reaches_is_not_checked(tmp_path):
    folder = _plugin(tmp_path)
    (folder / "spare_plugin.py").write_text(
        _source_with(repr(dict(GOOD_INFO, license="GPL"))), encoding="utf-8")
    assert _check_dir(folder) == ([], [])


def test_an_unreadable_plugin_module(tmp_path):
    folder = _plugin(tmp_path)
    (folder / "acme_widget_plugin.py").write_bytes(b"\xff\xfe\x00bad")
    errors, warnings = _check_dir(folder)
    assert errors[0].startswith(
        "acme_widget_plugin.py: OpenAVC cannot read this file: "
        "UnicodeDecodeError: 'utf-8' codec can't decode")
    assert errors[1:] == [NO_PLUGIN_CLASS]
    assert warnings == []


@pytest.mark.parametrize("src", [
    "class AcmeWidgetPlugin:\n    async def start(self):\n        pass\n",
    _source_with("make_info()"),
])
def test_no_plugin_info_that_can_be_read(tmp_path, src):
    assert _check_dir(_plugin(tmp_path, source=src)) == ([NO_PLUGIN_CLASS], [])


def test_plugin_info_with_values_built_at_runtime(tmp_path):
    info = dict(GOOD_INFO)
    lines = repr({k: val for k, val in info.items() if k not in ("version", "author")})
    lines = lines[:-1] + ", 'version': VERSION, 'author': get_author()}"
    folder = _plugin(tmp_path, source=_source_with(lines, extra_module="VERSION = read()"))
    assert _check_dir(folder) == ([], [
        "acme_widget_plugin.py: not checked, because the source builds it when the plugin "
        "runs: PLUGIN_INFO['version'], PLUGIN_INFO['author']"])


def test_plugin_info_errors_and_the_folder_name(tmp_path):
    bad = repr(dict(GOOD_INFO, id="other_widget", license="GPL"))
    manifest = dict(GOOD_INFO, id="other_widget", license="GPL")
    folder = _plugin(tmp_path, source=_source_with(bad), manifest=manifest)
    assert _check_dir(folder) == (
        ["acme_widget_plugin.py: License 'GPL' is not MIT-compatible",
         "plugin.json: License 'GPL' is not MIT-compatible"],
        ["Plugin ID 'other_widget' doesn't match directory name 'acme_widget'"],
    )


def test_a_plugin_with_no_id_is_not_compared_with_its_folder(tmp_path):
    info = repr(dict(GOOD_INFO, id=""))
    folder = _plugin(tmp_path, source=_source_with(info), manifest=dict(GOOD_INFO, id=""))
    assert _check_dir(folder) == ([], [])


@pytest.mark.parametrize("body, errors, warnings", [
    ("    def start(self):\n        pass\n    def stop(self):\n        pass\n", [], []),
    ("    async def stop(self):\n        pass\n",
     ["acme_widget_plugin.py: missing start() method"], []),
    ("    async def start(self, api):\n        pass\n",
     ["acme_widget_plugin.py: missing stop() method"], []),
    ("    pass\n",
     ["acme_widget_plugin.py: missing start() method",
      "acme_widget_plugin.py: missing stop() method"], []),
    ("    # def start( def stop(\n", [], []),
    ("    def restart(self):\n        pass\n    def nonstop(self):\n        pass\n",
     ["acme_widget_plugin.py: missing start() method",
      "acme_widget_plugin.py: missing stop() method"], []),
    ("    x = 'undef start(' + 'undef stop('\n", [], []),
])
def test_start_and_stop_are_read_as_text(tmp_path, body, errors, warnings):
    src = "class AcmeWidgetPlugin:\n    PLUGIN_INFO = " + repr(GOOD_INFO) + "\n" + body
    assert _check_dir(_plugin(tmp_path, source=src)) == (errors, warnings)


def test_task_and_sleep_habits(tmp_path):
    src = GOOD_SOURCE + "\n# asyncio.create_task(x)\n# time.sleep(1)\n"
    assert _check_dir(_plugin(tmp_path, source=src)) == (
        ["acme_widget_plugin.py: uses time.sleep() which blocks the event loop. Use "
         "asyncio.sleep() instead."],
        ["acme_widget_plugin.py: uses asyncio.create_task() instead of api.create_task() -- "
         "tasks won't be auto-cancelled on stop"],
    )


def test_config_schema_is_checked_when_it_is_a_literal(tmp_path):
    src = _source_with(repr(GOOD_INFO), body="    CONFIG_SCHEMA = {'host': {'type': 'url'}}\n")
    errors, warnings = _check_dir(_plugin(tmp_path, source=src))
    assert errors == [
        f"CONFIG_SCHEMA field 'host' has invalid type 'url': must be one of {sorted(v.VALID_CONFIG_TYPES)}"]
    assert warnings == []


def test_config_schema_through_an_alias(tmp_path):
    src = _source_with(repr(GOOD_INFO), body="    CONFIG_SCHEMA = CONFIG_SCHEMA\n",
                       extra_module="CONFIG_SCHEMA = {'host': {}}")
    assert _check_dir(_plugin(tmp_path, source=src)) == (
        ["CONFIG_SCHEMA field 'host' missing 'type'"], [])


def test_a_config_schema_that_is_not_a_dict(tmp_path):
    src = _source_with(repr(GOOD_INFO), body="    CONFIG_SCHEMA = ['host']\n")
    assert _check_dir(_plugin(tmp_path, source=src)) == (
        ["acme_widget_plugin.py: CONFIG_SCHEMA must be a dict"], [])


_BUILT = "acme_widget_plugin.py: not checked, because the source builds it when the plugin runs: "


def test_config_schema_built_at_runtime_is_a_warning(tmp_path):
    src = _source_with(repr(GOOD_INFO), body="    CONFIG_SCHEMA = build_schema()\n")
    assert _check_dir(_plugin(tmp_path, source=src)) == ([], [_BUILT + "CONFIG_SCHEMA"])


def test_config_secrets_read_beside_the_schema(tmp_path):
    body = ("    CONFIG_SCHEMA = {'streams': {'type': 'string'}}\n"
            "    CONFIG_SECRETS = {'streams': {'id': 'sid', 'fields': ['password']}}\n")
    src = _source_with(repr(GOOD_INFO), body=body)
    assert _check_dir(_plugin(tmp_path, source=src)) == ([
        "acme_widget_plugin.py: CONFIG_SECRETS['streams'] is also a CONFIG_SCHEMA field; "
        "declare its logins in one place"], [])


def test_config_secrets_with_a_schema_built_at_runtime(tmp_path):
    body = ("    CONFIG_SCHEMA = build()\n"
            "    CONFIG_SECRETS = {'streams': {'id': 'sid', 'fields': ['password']}}\n")
    src = _source_with(repr(GOOD_INFO), body=body)
    assert _check_dir(_plugin(tmp_path, source=src)) == ([], [_BUILT + "CONFIG_SCHEMA"])


def test_config_secrets_built_at_runtime_is_a_warning(tmp_path):
    src = _source_with(repr(GOOD_INFO), body="    CONFIG_SECRETS = build()\n")
    assert _check_dir(_plugin(tmp_path, source=src)) == ([], [_BUILT + "CONFIG_SECRETS"])


def test_config_secrets_named_only_in_a_comment_says_nothing(tmp_path):
    src = GOOD_SOURCE + "\n# no CONFIG_SECRETS here\n"
    assert _check_dir(_plugin(tmp_path, source=src)) == ([], [])


def test_config_secrets_at_module_level_only_is_not_the_plugins(tmp_path):
    """OpenAVC reads the class's attribute, so a module-level dict the class
    does not bind is no declaration."""
    src = _source_with(repr(GOOD_INFO), extra_module="CONFIG_SECRETS = {'[b]': {}}")
    assert _check_dir(_plugin(tmp_path, source=src)) == ([], [])


def test_extensions_in_the_class_are_checked(tmp_path):
    src = _source_with(repr(GOOD_INFO), body="    EXTENSIONS = {'views': [{}]}\n")
    assert _check_dir(_plugin(tmp_path, source=src)) == (
        ["acme_widget_plugin.py: EXTENSIONS invalid: views[0] missing 'id' (string)"], [])


def test_the_validate_config_hook_is_checked(tmp_path):
    body = "    def validate_config(self, config):\n        return []\n"
    src = _source_with(repr(GOOD_INFO), body=body)
    assert _check_dir(_plugin(tmp_path, source=src)) == ([
        "acme_widget_plugin.py: validate_config must be a @classmethod or @staticmethod -- "
        "OpenAVC calls it on the class, which may not be running"], [])


def test_no_plugin_json(tmp_path):
    assert _check_dir(_plugin(tmp_path, manifest=None)) == (["Missing plugin.json manifest file"], [])


def test_plugin_json_that_is_not_json(tmp_path):
    errors, warnings = _check_dir(_plugin(tmp_path, manifest="{not json"))
    assert errors == ["plugin.json is invalid JSON: Expecting property name enclosed in "
                      "double quotes: line 1 column 2 (char 1)"]
    assert warnings == []


def test_plugin_json_that_is_not_an_object(tmp_path):
    assert _check_dir(_plugin(tmp_path, manifest=["id"])) == (["plugin.json must hold a JSON object"], [])


def test_plugin_json_is_checked_and_compared(tmp_path):
    info = dict(GOOD_INFO, min_openavc_version="0.30.0", version="1.0.0")
    manifest = dict(GOOD_INFO, name="Acme Gadget", version="1.0.1", category="utility",
                    license="mit", min_openavc_version="0.31.0", author="Someone else",
                    platforms="all")
    folder = _plugin(tmp_path, source=_source_with(repr(info)), manifest=manifest)
    errors, warnings = _check_dir(folder)
    assert errors == [
        "plugin.json: platforms must be a list",
        "Mismatch: PLUGIN_INFO.name='Acme Widget' vs plugin.json.name='Acme Gadget'",
        "Mismatch: PLUGIN_INFO.version='1.0.0' vs plugin.json.version='1.0.1'",
        "Mismatch: PLUGIN_INFO.category='integration' vs plugin.json.category='utility'",
        "Mismatch: PLUGIN_INFO.license='MIT' vs plugin.json.license='mit'",
        "Mismatch: PLUGIN_INFO.min_openavc_version='0.30.0' vs "
        "plugin.json.min_openavc_version='0.31.0'",
    ]
    assert warnings == []


def test_values_are_compared_as_text(tmp_path):
    info = dict(GOOD_INFO, min_openavc_version=1)
    manifest = dict(GOOD_INFO, min_openavc_version="1")
    folder = _plugin(tmp_path, source=_source_with(repr(info)), manifest=manifest)
    assert _check_dir(folder) == ([], [])


def test_a_field_in_only_one_copy_is_not_compared(tmp_path):
    info = dict(GOOD_INFO, min_openavc_version="0.30.0")
    folder = _plugin(tmp_path, source=_source_with(repr(info)), manifest=dict(GOOD_INFO))
    assert _check_dir(folder) == ([], [])


def test_a_folder_with_no_plugin_class_stops_before_its_plugin_json(tmp_path):
    manifest = dict(GOOD_INFO, license="GPL")
    folder = _plugin(tmp_path, source=_source_with("make()"), manifest=manifest, readme=False)
    assert _check_dir(folder) == ([NO_PLUGIN_CLASS], [])


def test_no_readme(tmp_path):
    assert _check_dir(_plugin(tmp_path, readme=False)) == (
        [], ["Missing README.md (recommended for community plugins)"])


@pytest.mark.parametrize("category_dir, category, warning", [
    ("integrations", "integration", None),
    ("control_surfaces", "control_surface", None),
    ("sensors", "sensor", None),
    ("utility", "utility", None),
    ("integrations", "utility", "Category 'utility' doesn't match directory 'integrations/' (expected 'integration')"),
    ("utility", "sensor", "Category 'sensor' doesn't match directory 'utility/' (expected 'utility')"),
    ("control_surfaces", "integration",
     "Category 'integration' doesn't match directory 'control_surfaces/' (expected 'control_surface')"),
    ("sensors", "utility", "Category 'utility' doesn't match directory 'sensors/' (expected 'sensor')"),
    ("misc", "utility", None),
])
def test_category_and_its_folder(tmp_path, category_dir, category, warning):
    info = dict(GOOD_INFO, category=category)
    folder = _plugin(tmp_path, source=_source_with(repr(info)), manifest=info,
                     category_dir=category_dir)
    assert _check_dir(folder) == ([], [warning] if warning else [])


def test_no_category_is_not_compared_with_its_folder(tmp_path):
    info = {k: val for k, val in GOOD_INFO.items() if k != "category"}
    folder = _plugin(tmp_path, source=_source_with(repr(info)), manifest=info, category_dir="utility")
    assert _check_dir(folder) == ([
        "acme_widget_plugin.py: Missing required fields: ['category']",
        "plugin.json: Missing required fields: ['category']",
    ], [])


def test_a_file_that_does_not_parse_stops_the_check(tmp_path):
    src = "PLUGIN_INFO = {\nclass (:\n  def start(\n  time.sleep(\n  asyncio.create_task(\nCONFIG_SECRETS validate_config"
    folder = _plugin(tmp_path, source=src, readme=False, category_dir="sensors")
    errors, warnings = _check_dir(folder)
    assert errors[0].startswith(
        "acme_widget_plugin.py: OpenAVC cannot read this file: SyntaxError: ")
    assert errors[1:] == [NO_PLUGIN_CLASS]
    assert warnings == []


def test_every_folder_check_in_its_order(tmp_path):
    info_src = (
        "{'id': 'other_id', 'name': 'Acme Widget', 'version': '1', 'author': 'Acme',"
        " 'description': 'd', 'category': 'utility', 'license': 'MIT',"
        " 'capabilities': ['nope'], 'dependencies': ['paho'], 'extra': COMPUTED}"
    )
    body = (
        "    CONFIG_SCHEMA = {'a': {'type': 'select'}}\n"
        "    CONFIG_SECRETS = {'b': {'id': 'k'}}\n"
        "    EXTENSIONS = {'views': 'x'}\n"
        "    def validate_config(self, config):\n"
        "        time.sleep(1)\n"
        "        asyncio.create_task(x)\n"
    )
    src = (
        "class AcmeWidgetPlugin:\n"
        f"    PLUGIN_INFO = {info_src}\n"
        f"{body}"
        "    def run(self):\n"
        "        pass\n"
    )
    manifest = {"id": "other_id", "name": "Other", "version": "1", "category": "utility",
                "license": "MIT", "author": "Acme", "description": "d", "dependencies": "x"}
    folder = _plugin(tmp_path, source=src, manifest=manifest, readme=False,
                     category_dir="integrations")
    errors, warnings = _check_dir(folder)
    assert errors == [
        "acme_widget_plugin.py: Unknown capabilities: ['nope']",
        "acme_widget_plugin.py: CONFIG_SECRETS['b']['fields'] must be a list of distinct field names",
        "acme_widget_plugin.py: EXTENSIONS invalid: extension type 'views' must be a list",
        "acme_widget_plugin.py: missing start() method",
        "acme_widget_plugin.py: missing stop() method",
        "acme_widget_plugin.py: uses time.sleep() which blocks the event loop. Use "
        "asyncio.sleep() instead.",
        "CONFIG_SCHEMA field 'a' is type 'select' but missing 'options' list",
        "acme_widget_plugin.py: validate_config must be a @classmethod or @staticmethod -- "
        "OpenAVC calls it on the class, which may not be running",
        "plugin.json: dependencies must be a list",
        "Mismatch: PLUGIN_INFO.name='Acme Widget' vs plugin.json.name='Other'",
    ]
    assert warnings == [
        _BUILT + "PLUGIN_INFO['extra']",
        "acme_widget_plugin.py: version '1' doesn't follow semver format (X.Y.Z)",
        "acme_widget_plugin.py: has pip dependencies ['paho'] -- ensure all are MIT-compatible",
        "Plugin ID 'other_id' doesn't match directory name 'acme_widget'",
        "acme_widget_plugin.py: uses asyncio.create_task() instead of api.create_task() -- "
        "tasks won't be auto-cancelled on stop",
        "plugin.json: version '1' doesn't follow semver format (X.Y.Z)",
        "Missing README.md (recommended for community plugins)",
        "Category 'utility' doesn't match directory 'integrations/' (expected 'integration')",
    ]


# ---------------------------------------------------------------------------
# index.json
# ---------------------------------------------------------------------------


INDEX_FIELDS = ("id", "name", "file", "format", "category", "version", "author", "license",
                "platforms", "capabilities", "has_native_dependencies", "verified", "description")


def _entry(**changes):
    entry = {
        "id": "acme_widget", "name": "Acme Widget", "file": "integrations/acme_widget",
        "format": "package", "category": "integration", "version": "1.0.0", "author": "Acme",
        "license": "MIT", "platforms": ["all"], "capabilities": ["state_read"],
        "has_native_dependencies": False, "verified": True, "description": "d",
    }
    for key, value in changes.items():
        if value is _DROP:
            entry.pop(key)
        else:
            entry[key] = value
    return entry


def _index(root, index):
    if isinstance(index, str):
        (root / "index.json").write_text(index, encoding="utf-8")
    elif index is not None:
        (root / "index.json").write_text(json.dumps(index), encoding="utf-8")
    results = ["earlier"]
    v.validate_index_json(root, results)
    assert len(results) == 2 and results[0] == "earlier"
    return results[1]


def test_no_index(tmp_path):
    r = _index(tmp_path, None)
    assert r.plugin_path == tmp_path / "index.json"
    assert (r.errors, r.warnings) == (["index.json not found"], [])


def test_an_index_that_is_not_json(tmp_path):
    r = _index(tmp_path, "{")
    assert r.plugin_path == tmp_path / "index.json"
    assert r.errors == ["Invalid JSON: Expecting property name enclosed in double quotes: "
                        "line 1 column 2 (char 1)"]
    assert r.warnings == []


def test_an_index_without_plugins(tmp_path):
    r = _index(tmp_path, {"version": 1})
    assert (r.errors, r.warnings) == (["Missing 'plugins' array"], [])


def test_a_good_index(tmp_path):
    _plugin(tmp_path)
    r = _index(tmp_path, {"plugins": [_entry()]})
    assert (r.errors, r.warnings) == ([], [])


def test_index_entry_problems_in_their_order(tmp_path):
    _plugin(tmp_path)
    _plugin(tmp_path, name="beta", manifest="{broken")
    entries = [
        {},
        _entry(),
        _entry(),
        _entry(id="beta", file="integrations/beta", category="lighting", license="GPL",
               capabilities=["state_read", "fly", "swim"]),
        _entry(id="gamma", file="integrations/gamma", capabilities="fly", license="Mit"),
        _entry(id="delta", file=_DROP, category=_DROP, license=_DROP, capabilities=_DROP),
    ]
    r = _index(tmp_path, {"plugins": entries})
    assert r.errors == (
        [f"Plugin '<missing>': missing required field '{f}'" for f in INDEX_FIELDS]
        + [
            "Duplicate plugin ID 'acme_widget' in index.json",
            "Plugin 'beta': invalid category 'lighting'",
            "Plugin 'beta': license 'GPL' is not MIT-compatible",
            "Plugin 'beta': invalid capability 'fly'",
            "Plugin 'beta': invalid capability 'swim'",
            "Plugin 'gamma': directory 'integrations/gamma' does not exist",
            "Plugin 'delta': missing required field 'file'",
            "Plugin 'delta': missing required field 'category'",
            "Plugin 'delta': missing required field 'license'",
            "Plugin 'delta': missing required field 'capabilities'",
        ]
    )
    assert r.warnings == []


def test_an_index_entry_with_a_license_that_is_not_text(tmp_path):
    assert _index(tmp_path, {"plugins": [_entry(license=None)]}).errors[1:] == [
        "Plugin 'acme_widget': license 'None' is not MIT-compatible"]


def test_an_index_entry_that_is_not_an_object_raises(tmp_path):
    (tmp_path / "index.json").write_text(json.dumps({"plugins": ["acme"]}))
    with pytest.raises(AttributeError):
        v.validate_index_json(tmp_path, [])


def test_index_entries_are_compared_with_plugin_json(tmp_path):
    manifest = dict(GOOD_INFO, min_openavc_version="0.30.0", version=2)
    _plugin(tmp_path, manifest=manifest)
    entry = _entry(name="Acme Gadget", version="2", category="utility", license="mit",
                   min_openavc_version="0.31.0", author="Someone")
    r = _index(tmp_path, {"plugins": [entry]})
    assert r.errors == [
        "Plugin 'acme_widget': index.json name='Acme Gadget' doesn't match plugin.json "
        "name='Acme Widget'",
        "Plugin 'acme_widget': index.json category='utility' doesn't match plugin.json "
        "category='integration'",
        "Plugin 'acme_widget': index.json license='mit' doesn't match plugin.json license='MIT'",
        "Plugin 'acme_widget': index.json min_openavc_version='0.31.0' doesn't match "
        "plugin.json min_openavc_version='0.30.0'",
    ]
    assert r.warnings == []


def test_an_index_entry_whose_folder_has_no_plugin_json(tmp_path):
    _plugin(tmp_path, manifest=None)
    r = _index(tmp_path, {"plugins": [_entry(name="Other")]})
    assert (r.errors, r.warnings) == ([], [])


def test_an_index_entry_whose_plugin_json_is_not_an_object_is_skipped(tmp_path):
    _plugin(tmp_path, manifest=["id"])
    r = _index(tmp_path, {"plugins": [_entry(name="Other")]})
    assert (r.errors, r.warnings) == ([], [])


@pytest.mark.parametrize("make, warned", [
    (lambda root: _plugin(root, name="extra"), True),
    (lambda root: _plugin(root, name="extra", category_dir="sensors"), True),
    (lambda root: _plugin(root, name="extra", category_dir="misc"), False),
    (lambda root: _plugin(root, name="_extra"), False),
    (lambda root: _plugin(root, name=".extra"), False),
    (lambda root: _plugin(root, name="extra", file_name="extra.py"), False),
    (lambda root: (root / "integrations" / "loose_plugin.py").write_text("x"), False),
])
def test_a_plugin_folder_missing_from_the_index(tmp_path, make, warned):
    _plugin(tmp_path)
    make(tmp_path)
    r = _index(tmp_path, {"plugins": [_entry()]})
    assert r.errors == []
    if warned:
        assert len(r.warnings) == 1
        rel = r.warnings[0].split("'")[1]
        assert r.warnings[0] == f"Plugin directory '{rel}' exists but has no index.json entry"
        assert rel.endswith("/extra")
    else:
        assert r.warnings == []


def test_index_category_folders_that_do_not_exist(tmp_path):
    (tmp_path / "integrations").mkdir()
    r = _index(tmp_path, {"plugins": []})
    assert (r.errors, r.warnings) == ([], [])


# ---------------------------------------------------------------------------
# Which folders get checked
# ---------------------------------------------------------------------------


def test_find_plugin_dirs_walks_the_category_folders_in_order(tmp_path):
    for category_dir, name in [("utility", "zeta"), ("utility", "alpha"),
                               ("control_surfaces", "mid"), ("sensors", "s1"),
                               ("integrations", "b"), ("integrations", "a")]:
        _plugin(tmp_path, category_dir=category_dir, name=name)
    _plugin(tmp_path, category_dir="integrations", name="json_only", source=None)
    _plugin(tmp_path, category_dir="integrations", name="neither", source=None, manifest=None)
    _plugin(tmp_path, category_dir="integrations", name="_private")
    _plugin(tmp_path, category_dir="integrations", name=".hidden")
    _plugin(tmp_path, category_dir="misc", name="elsewhere")
    (tmp_path / "integrations" / "loose_plugin.py").write_text("x")
    found = v.find_plugin_dirs(tmp_path)
    assert [str(p.relative_to(tmp_path)) for p in found] == [
        "control_surfaces/mid",
        "integrations/a", "integrations/b", "integrations/json_only",
        "sensors/s1",
        "utility/alpha", "utility/zeta",
    ]


def test_find_plugin_dirs_with_no_category_folders(tmp_path):
    assert v.find_plugin_dirs(tmp_path) == []
    assert v.find_plugin_dirs(tmp_path, []) == []


def test_find_plugin_dirs_takes_named_targets(tmp_path, capsys):
    a = _plugin(tmp_path, name="a")
    elsewhere = tmp_path / "outside"
    elsewhere.mkdir()
    (tmp_path / "file.txt").write_text("x")
    found = v.find_plugin_dirs(tmp_path, ["integrations/a", str(elsewhere), "file.txt", "missing"])
    assert found == [a, elsewhere]
    assert capsys.readouterr().out == (
        "WARNING: Not a directory: file.txt\nWARNING: Not a directory: missing\n")


# ---------------------------------------------------------------------------
# The command line: what CI and contributors see
# ---------------------------------------------------------------------------


def _main(monkeypatch, capsys, root, *argv):
    monkeypatch.setattr(v, "__file__", str(root / "validate.py"))
    monkeypatch.setattr(sys, "argv", ["validate.py", *argv])
    with pytest.raises(SystemExit) as exit_info:
        v.main()
    return exit_info.value.code, capsys.readouterr().out


def test_main_all_passing(monkeypatch, capsys, tmp_path):
    _plugin(tmp_path)
    assert _main(monkeypatch, capsys, tmp_path) == (
        0, "\nValidated 1 plugin(s): 1 passed, 0 failed\n")


def test_main_verbose(monkeypatch, capsys, tmp_path):
    _plugin(tmp_path)
    _plugin(tmp_path, name="beta", readme=False, manifest=dict(GOOD_INFO, id="beta"),
            source=_source_with(repr(dict(GOOD_INFO, id="beta"))))
    code, out = _main(monkeypatch, capsys, tmp_path, "--verbose")
    assert code == 0
    assert out == (
        "  PASS  integrations/acme_widget\n"
        "  PASS  integrations/beta\n"
        "        WARNING: Missing README.md (recommended for community plugins)\n"
        "\n"
        "Validated 2 plugin(s): 2 passed, 0 failed\n"
        "  1 warning(s)\n"
    )


def test_main_repeats_the_warn_line_for_each_warning(monkeypatch, capsys, tmp_path):
    info = dict(GOOD_INFO, id="beta", version="1")
    _plugin(tmp_path, name="beta", readme=False, manifest=info, source=_source_with(repr(info)))
    code, out = _main(monkeypatch, capsys, tmp_path, "-v")
    code2, out2 = _main(monkeypatch, capsys, tmp_path)
    assert code == code2 == 0
    assert out2 == (
        "  WARN  integrations/beta\n"
        "        WARNING: beta_plugin.py: version '1' doesn't follow semver format (X.Y.Z)\n"
        "  WARN  integrations/beta\n"
        "        WARNING: plugin.json: version '1' doesn't follow semver format (X.Y.Z)\n"
        "  WARN  integrations/beta\n"
        "        WARNING: Missing README.md (recommended for community plugins)\n"
        "\n"
        "Validated 1 plugin(s): 1 passed, 0 failed\n"
        "  3 warning(s)\n"
    )
    assert out.startswith("  PASS  integrations/beta\n        WARNING: beta_plugin.py: version")


def test_main_failing(monkeypatch, capsys, tmp_path):
    _plugin(tmp_path, manifest=None, readme=False)
    _plugin(tmp_path, name="good", manifest=dict(GOOD_INFO, id="good"),
            source=_source_with(repr(dict(GOOD_INFO, id="good"))))
    for flags in [(), ("--verbose",)]:
        code, out = _main(monkeypatch, capsys, tmp_path, *flags)
        assert code == 1
        passed = "  PASS  integrations/good\n" if flags else ""
        assert out == (
            "  FAIL  integrations/acme_widget\n"
            "        ERROR: Missing plugin.json manifest file\n"
            "        WARNING: Missing README.md (recommended for community plugins)\n"
            f"{passed}"
            "\n"
            "Validated 2 plugin(s): 1 passed, 1 failed\n"
            "  1 error(s)\n"
            "  1 warning(s)\n"
        )


def test_main_named_plugins_and_a_path_outside_the_repo(monkeypatch, capsys, tmp_path):
    root = tmp_path / "repo"
    _plugin(root)
    outside = tmp_path / "outside_plugin_dir"
    outside.mkdir()
    code, out = _main(monkeypatch, capsys, root, "integrations/acme_widget", str(outside), "nope")
    assert code == 1
    assert out == (
        "WARNING: Not a directory: nope\n"
        f"  FAIL  {outside}\n"
        f"        ERROR: {NO_PLUGIN_CLASS}\n"
        "\n"
        "Validated 2 plugin(s): 1 passed, 1 failed\n"
        "  1 error(s)\n"
    )


def test_main_check_index(monkeypatch, capsys, tmp_path):
    _plugin(tmp_path)
    code, out = _main(monkeypatch, capsys, tmp_path, "--check-index")
    assert code == 1
    assert out == (
        "  FAIL  index.json\n"
        "        ERROR: index.json not found\n"
        "\n"
        "Validated 2 plugin(s): 1 passed, 1 failed\n"
        "  1 error(s)\n"
    )
    (tmp_path / "index.json").write_text(json.dumps({"plugins": [_entry()]}))
    assert _main(monkeypatch, capsys, tmp_path, "--check-index", "-v") == (0, (
        "  PASS  integrations/acme_widget\n"
        "  PASS  index.json\n"
        "\n"
        "Validated 2 plugin(s): 2 passed, 0 failed\n"
    ))


def test_main_with_nothing_to_check(monkeypatch, capsys, tmp_path):
    assert _main(monkeypatch, capsys, tmp_path) == (0, "\nValidated 0 plugin(s): 0 passed, 0 failed\n")


def test_this_repository_validates():
    """What CI runs, on the real plugins: it must pass."""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "validate.py"), "--check-index"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert " 0 failed" in proc.stdout


# ---------------------------------------------------------------------------
# What is already recorded when a check raises
# ---------------------------------------------------------------------------


def test_index_json_adds_no_result_when_an_entry_raises(tmp_path):
    (tmp_path / "index.json").write_text(json.dumps({"plugins": [_entry(), "acme"]}))
    results = ["earlier"]
    with pytest.raises(AttributeError):
        v.validate_index_json(tmp_path, results)
    assert results == ["earlier"]


# ---------------------------------------------------------------------------
# What OpenAVC refuses at enable is refused here, in its words
# ---------------------------------------------------------------------------


def _with_attrs(attrs, info=None, module=""):
    """GOOD_SOURCE's class with ``info`` as PLUGIN_INFO and ``attrs`` (class
    body lines) after it."""
    body = "".join(f"    {line}\n" for line in attrs)
    return _source_with(repr(info or GOOD_INFO), body=body, extra_module=module)


@pytest.mark.parametrize("attrs, error", [
    (['CONFIG_SECRETS = {"[b]": {"id": "x", "fields": ["y"]}}'],
     "acme_widget_plugin.py: CONFIG_SECRETS key '[b]' must be a config key"),
    (['CONFIG_SECRETS = {"k": {"id": "a=b", "fields": ["y"]}}'],
     "acme_widget_plugin.py: CONFIG_SECRETS['k']['id'] must name the key holding each item's id"),
    (['MACRO_ACTIONS = {"other.ping": {"handler": "ping"}}'],
     "acme_widget_plugin.py: MACRO_ACTIONS invalid: action 'other.ping' must be prefixed "
     "with the plugin id ('acme_widget.')"),
    (['SCRIPT_API = {"register": {"handler": "ping"}}'],
     "acme_widget_plugin.py: SCRIPT_API invalid: method name 'register' is reserved"),
])
def test_a_declaration_openavc_refuses_fails_here(tmp_path, attrs, error):
    errors, _ = _check_dir(_plugin(tmp_path, source=_with_attrs(attrs)))
    assert errors == [error]


@pytest.mark.parametrize("over, error", [
    ({"network_ports": [{"port": 9000, "reason": "media"}]},
     "network_ports requires the network_listen capability"),
    ({"network_ports": [{"port": 22, "reason": "ssh"}], "capabilities": ["network_listen"]},
     "network_ports may not request port 22 -- it belongs to the host, not to a plugin"),
    ({"network_ports": "9000", "capabilities": ["network_listen"]}, "network_ports must be a list"),
])
def test_network_ports_openavc_refuses_fail_here(tmp_path, over, error):
    info = dict(GOOD_INFO, **over)
    errors, _ = _check_dir(_plugin(tmp_path, source=_with_attrs([], info), manifest=info))
    assert errors == [f"acme_widget_plugin.py: {error}", f"plugin.json: {error}"]


def test_a_license_with_spaces_around_it_passes_as_openavc_trims_it(tmp_path):
    info = dict(GOOD_INFO, license=" MIT ")
    assert _check_dir(_plugin(tmp_path, source=_with_attrs([], info), manifest=info)) == ([], [])


def test_a_login_column_in_a_mapping_list_is_warned_about_as_openavc_does(tmp_path):
    schema = ('CONFIG_SCHEMA = {"rows": {"type": "mapping_list", "item_schema": '
              '{"name": {"type": "string"}, "key": {"type": "string", "secret": True}}}}')
    errors, warnings = _check_dir(_plugin(tmp_path, source=_with_attrs([schema])))
    assert errors == []
    assert warnings == [
        "CONFIG_SCHEMA field 'rows' column 'key' holds a login, but a mapping_list row has "
        "no id to keep a login under, so OpenAVC keeps it in the project. Keep the list "
        "outside CONFIG_SCHEMA and declare it in CONFIG_SECRETS."]


def test_extensions_bound_through_a_module_name_are_checked(tmp_path):
    module = 'EXTENSIONS = {"views": [{"label": "no id"}]}'
    errors, _ = _check_dir(_plugin(tmp_path, source=_with_attrs(["EXTENSIONS = EXTENSIONS"], module=module)))
    assert errors == ["acme_widget_plugin.py: EXTENSIONS invalid: views[0] missing 'id' (string)"]


@pytest.mark.parametrize("info", ["{}", "{**BASE}", '{1: "x"}'])
def test_a_plugin_info_with_no_id_is_reported(tmp_path, info):
    folder = _plugin(tmp_path, source=_source_with(info, extra_module="BASE = make()"))
    errors, _ = _check_dir(folder)
    assert errors == [NO_PLUGIN_CLASS]


def test_the_required_fields_are_named_in_one_sorted_sentence(tmp_path):
    info = {"id": "acme_widget", "name": "Acme Widget", "category": "integration", "license": "MIT"}
    errors, _ = _check_dir(_plugin(tmp_path, source=_source_with(repr(info)), manifest=GOOD_INFO))
    assert errors == ["acme_widget_plugin.py: Missing required fields: ['author', 'description', 'version']"]


def test_the_module_checked_is_the_one_openavc_loads(tmp_path):
    """OpenAVC takes the class from __init__.py before <folder>_plugin.py."""
    folder = _plugin(tmp_path)
    (folder / "__init__.py").write_text(_source_with(repr(dict(GOOD_INFO, license="GPL"))), encoding="utf-8")
    errors, warnings = _check_dir(folder)
    assert errors[0] == "__init__.py: License 'GPL' is not MIT-compatible"
    assert warnings == []


def test_a_file_openavc_passes_over_is_reported(tmp_path):
    folder = _plugin(tmp_path)
    (folder / "__init__.py").write_text("def broken(\n", encoding="utf-8")
    errors, _ = _check_dir(folder)
    assert len(errors) == 1
    assert errors[0].startswith("__init__.py: OpenAVC cannot read this file: SyntaxError: ")


#: A module whose PLUGIN_INFO reads a value through 5000 names: Python runs it,
#: and following the names recurses past the interpreter's limit.
_NAME_CHAIN = (
    "A0 = 1\n" + "".join(f"A{i} = A{i - 1}\n" for i in range(1, 5000))
    + f"class AcmeWidgetPlugin:\n    PLUGIN_INFO = {{**{GOOD_INFO!r}, 'x': A4999}}\n"
)


@pytest.mark.parametrize("src, raised", [
    ("x = " + "-" * 100000 + "1\n", ("MemoryError", "RecursionError")),
    (_NAME_CHAIN, ("RecursionError",)),
])
def test_a_source_too_deep_to_read_is_that_plugins_error(tmp_path, src, raised):
    errors, _ = _check_dir(_plugin(tmp_path, source=src))
    prefix = "acme_widget_plugin.py: OpenAVC cannot read this file: "
    assert errors[0].startswith(prefix)
    assert errors[0][len(prefix):].split(":")[0] in raised
    assert errors[1:] == [NO_PLUGIN_CLASS]


@pytest.mark.parametrize("platforms, error", [
    (["linux_x64", "macos"],
     "Unknown platforms: ['macos']; each must be one of ['all', 'linux_arm64', 'linux_x64', 'win_x64']"),
    ([], "platforms lists no platform; list 'all' or each platform the plugin runs on"),
    ("all", "platforms must be a list"),
])
def test_the_platforms_a_plugin_lists(tmp_path, platforms, error):
    info = dict(GOOD_INFO, platforms=platforms)
    errors, _ = _check_dir(_plugin(tmp_path, source=_with_attrs([], info), manifest=info))
    assert errors == [f"acme_widget_plugin.py: {error}", f"plugin.json: {error}"]


def test_a_plugin_for_another_machine_passes_on_this_one(tmp_path):
    """Whether this machine runs the plugin is not asked: CI runs on one."""
    info = dict(GOOD_INFO, platforms=["win_x64"])
    assert _check_dir(_plugin(tmp_path, source=_with_attrs([], info), manifest=info)) == ([], [])


NO_PLUGIN_CLASS = (
    "No plugin class found: OpenAVC needs a class whose PLUGIN_INFO is a dict with an 'id', "
    "written out in the source so it can be read before the plugin is installed")
