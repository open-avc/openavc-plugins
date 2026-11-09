"""What ``validate.py`` reports, message for message.

``validate.py`` is the check every plugin pull request runs (``python validate.py
--check-index``) and the one contributors run before submitting. These tests
pin what it says today for each shape a plugin folder, its source, its
plugin.json and index.json can take: the exact error and warning sentences,
their order, which checks run and which are skipped, the printed report and the
exit code. A sentence that looks wrong is pinned as it is, so a cleanup of the
validator cannot change what a contributor is told without a test saying so.

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
    assert v.VALID_CATEGORIES == {"control_surface", "integration", "sensor", "utility"}
    assert v.VALID_PLATFORMS == {"all", "win_x64", "linux_x64", "linux_arm64"}
    assert v.EXTENSION_ID_FIELD == {
        "views": "id", "device_panels": "id", "status_cards": "id",
        "context_actions": "id", "panel_elements": "type",
    }
    assert v.VALID_EXTENSION_TYPES == set(v.EXTENSION_ID_FIELD)
    assert v._CONFIG_SECRETS_KEYS == {"id", "fields", "generated"}
    assert "mapping_list" in v.VALID_CONFIG_TYPES and "group" in v.VALID_CONFIG_TYPES
    assert len(v.VALID_CONFIG_TYPES) == 13


# ---------------------------------------------------------------------------
# Reading the plugin's source
# ---------------------------------------------------------------------------


def _tree(src):
    import ast
    return ast.parse(src)


def test_module_level_assignments_keeps_single_name_targets_only():
    src = (
        "A = 1\n"
        "B = make()\n"
        "C = D = 2\n"
        "E: int = 3\n"
        "obj.attr = 4\n"
        "F, G = 5, 6\n"
        "if True:\n"
        "    H = 7\n"
        "A = 8\n"
    )
    consts, nodes = v._module_level_assignments(_tree(src))
    assert consts == {"A": 8}
    assert sorted(nodes) == ["A", "B"]
    import ast
    assert isinstance(nodes["B"], ast.Call)


def test_find_assignment_prefers_the_class_then_the_module():
    import ast
    src = (
        "X = {'from': 'module'}\n"
        "class First:\n"
        "    other = 1\n"
        "    X = {'from': 'first'}\n"
        "class Second:\n"
        "    X = {'from': 'second'}\n"
    )
    node = v._find_assignment(_tree(src), "X")
    assert ast.literal_eval(node) == {"from": "first"}
    only_module = v._find_assignment(_tree("X = 3\nclass C:\n    y = 1\n"), "X")
    assert ast.literal_eval(only_module) == 3
    assert v._find_assignment(_tree("class C:\n    y = 1\n"), "X") is None


def test_find_assignment_matches_any_target_of_a_chained_class_assignment():
    import ast
    node = v._find_assignment(_tree("class C:\n    A = X = {'k': 1}\n"), "X")
    assert ast.literal_eval(node) == {"k": 1}


def test_find_assignment_ignores_an_annotated_class_assignment():
    import ast
    src = "X = 'module'\nclass C:\n    X: dict = {'k': 1}\n"
    assert ast.literal_eval(v._find_assignment(_tree(src), "X")) == "module"


def test_find_assignment_reaches_a_nested_class():
    import ast
    src = "class Outer:\n    class Inner:\n        X = 5\n"
    assert ast.literal_eval(v._find_assignment(_tree(src), "X")) == 5


def test_extract_plugin_info_reads_a_literal():
    info, unresolved = v.extract_plugin_info(GOOD_SOURCE)
    assert info == GOOD_INFO
    assert unresolved == []


def test_extract_plugin_info_on_a_syntax_error():
    assert v.extract_plugin_info("class (:\n") == (None, [])


def test_extract_plugin_info_without_plugin_info():
    assert v.extract_plugin_info("class C:\n    x = 1\n") == (None, [])


def test_extract_plugin_info_when_it_is_not_a_dict():
    assert v.extract_plugin_info("class C:\n    PLUGIN_INFO = make()\n") == (None, [])
    assert v.extract_plugin_info("class C:\n    PLUGIN_INFO = [1]\n") == (None, [])


def test_extract_plugin_info_follows_one_alias_to_the_module():
    src = "PLUGIN_INFO = {'id': 'acme'}\nclass C:\n    PLUGIN_INFO = PLUGIN_INFO\n"
    assert v.extract_plugin_info(src) == ({"id": "acme"}, [])


def test_extract_plugin_info_follows_an_alias_to_another_module_name():
    src = "_INFO = {'id': 'acme'}\nclass C:\n    PLUGIN_INFO = _INFO\n"
    assert v.extract_plugin_info(src) == ({"id": "acme"}, [])


def test_extract_plugin_info_alias_to_a_missing_name():
    assert v.extract_plugin_info("class C:\n    PLUGIN_INFO = NOWHERE\n") == (None, [])


def test_extract_plugin_info_follows_only_one_alias():
    src = "A = {'id': 'acme'}\nB = A\nclass C:\n    PLUGIN_INFO = B\n"
    assert v.extract_plugin_info(src) == (None, [])


def test_extract_plugin_info_at_module_level_only():
    assert v.extract_plugin_info("PLUGIN_INFO = {'id': 'acme'}\n") == ({"id": "acme"}, [])


def test_extract_plugin_info_resolves_constants_and_names_the_rest():
    src = (
        "_ALIAS = 'acme'\n"
        "_COMPUTED = make()\n"
        "class C:\n"
        "    PLUGIN_INFO = {\n"
        "        'id': 'acme',\n"
        "        'guest_alias': _ALIAS,\n"
        "        'version': _COMPUTED,\n"
        "        'name': build_name(),\n"
        "        'author': UNKNOWN,\n"
        "        1: 'numeric key',\n"
        "        **EXTRA,\n"
        "        'license': 'MIT',\n"
        "    }\n"
    )
    info, unresolved = v.extract_plugin_info(src)
    assert info == {"id": "acme", "guest_alias": "acme", "license": "MIT"}
    assert unresolved == ["version", "name", "author"]


def test_extract_plugin_info_reads_an_empty_dict():
    assert v.extract_plugin_info("class C:\n    PLUGIN_INFO = {}\n") == ({}, [])


def test_extract_declared():
    assert v._extract_declared("class (:\n", "CONFIG_SCHEMA") is None
    assert v._extract_declared("class C:\n    x = 1\n", "CONFIG_SCHEMA") is None
    assert v._extract_declared("class C:\n    CONFIG_SCHEMA = {'a': 1}\n", "CONFIG_SCHEMA") == {"a": 1}
    alias = "CONFIG_SCHEMA = {'a': 2}\nclass C:\n    CONFIG_SCHEMA = CONFIG_SCHEMA\n"
    assert v._extract_declared(alias, "CONFIG_SCHEMA") == {"a": 2}
    assert v._extract_declared("class C:\n    CONFIG_SCHEMA = NOWHERE\n", "CONFIG_SCHEMA") is None
    assert v._extract_declared("class C:\n    CONFIG_SCHEMA = make()\n", "CONFIG_SCHEMA") is None
    assert v._extract_declared("CONFIG_SCHEMA = [1, 2]\n", "CONFIG_SCHEMA") == [1, 2]
    two_hops = "A = {'a': 3}\nB = A\nclass C:\n    CONFIG_SCHEMA = B\n"
    assert v._extract_declared(two_hops, "CONFIG_SCHEMA") is None


def test_extract_class_attr_reads_a_class_literal_only():
    assert v._extract_class_attr("class C:\n    EXTENSIONS = {'views': []}\n", "EXTENSIONS") == {"views": []}
    assert v._extract_class_attr("EXTENSIONS = {'views': []}\n", "EXTENSIONS") is None
    alias = "EXTENSIONS = {'views': []}\nclass C:\n    EXTENSIONS = EXTENSIONS\n"
    assert v._extract_class_attr(alias, "EXTENSIONS") is None
    assert v._extract_class_attr("class (:\n", "EXTENSIONS") is None
    assert v._extract_class_attr("class C:\n    EXTENSIONS = make()\n", "EXTENSIONS") is None
    assert v._extract_class_attr("class C:\n    other = 1\n", "EXTENSIONS") is None
    after_a_method = "class C:\n    def f(self):\n        pass\n    EXTENSIONS: dict = {}\n    EXTENSIONS = {'views': []}\n"
    assert v._extract_class_attr(after_a_method, "EXTENSIONS") == {"views": []}


def test_extract_class_attr_stops_at_the_first_match_even_when_it_is_not_a_literal():
    src = "class A:\n    EXTENSIONS = make()\nclass B:\n    EXTENSIONS = {'views': []}\n"
    assert v._extract_class_attr(src, "EXTENSIONS") is None
    src2 = "class A:\n    EXTENSIONS = {'views': [1]}\nclass B:\n    EXTENSIONS = {'views': []}\n"
    assert v._extract_class_attr(src2, "EXTENSIONS") == {"views": [1]}


def _hook(src):
    result = v.ValidationResult(Path("x"))
    v.validate_config_hook(src, "acme_plugin.py", result)
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


def test_validate_config_hook_reads_only_classes_that_declare_plugin_info():
    src = (
        "class Helper:\n"
        "    def validate_config(self, config):\n"
        "        return []\n"
        "class C:\n"
        "    PLUGIN_INFO = {'id': 'acme'}\n"
        "    def validate_config(self, config):\n"
        "        return []\n"
        "class Annotated:\n"
        "    PLUGIN_INFO: dict = {'id': 'x'}\n"
        "    def validate_config(self, config):\n"
        "        return []\n"
    )
    assert _hook(src) == ([_INSTANCE_HOOK], [])


def test_validate_config_hook_reads_every_declaring_class_and_every_definition():
    src = (
        "class A:\n"
        "    PLUGIN_INFO = {}\n"
        "    def validate_config(self, c):\n"
        "        pass\n"
        "    async def validate_config(self, c):\n"
        "        pass\n"
        "    def other(self):\n"
        "        pass\n"
        "    x = 1\n"
        "class B:\n"
        "    X = PLUGIN_INFO = {}\n"
        "    def validate_config(self, c):\n"
        "        pass\n"
    )
    assert _hook(src) == ([_INSTANCE_HOOK, _ASYNC_HOOK, _INSTANCE_HOOK], [])


def test_validate_config_hook_on_a_syntax_error():
    assert _hook("class (:\n") == ([], [])


# ---------------------------------------------------------------------------
# PLUGIN_INFO and plugin.json fields
# ---------------------------------------------------------------------------


def test_a_good_manifest_says_nothing():
    assert _check_info(dict(GOOD_INFO)) == ([], [])


def test_missing_required_fields_one_error_each():
    errors, warnings = _check_info({})
    assert sorted(errors) == sorted(
        f"PLUGIN_INFO: missing required field '{f}'" for f in v.REQUIRED_MANIFEST_FIELDS
    )
    assert len(errors) == 7
    assert warnings == []


def test_missing_required_fields_follow_the_set_order():
    errors, _ = _check_info({})
    assert errors == [f"PLUGIN_INFO: missing required field '{f}'" for f in v.REQUIRED_MANIFEST_FIELDS]


def test_an_unresolved_field_is_not_missing():
    errors, _ = _check_info(_info(author=_DROP, license=_DROP), unresolved=["author"])
    assert errors == ["PLUGIN_INFO: missing required field 'license'"]


def test_the_source_label():
    errors, _ = _check_info(_info(category="nope"), source="plugin.json")
    assert errors == [
        "plugin.json: invalid category 'nope': must be one of "
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
])
def test_plugin_id(plugin_id, expected):
    assert _check_info(_info(id=plugin_id)) == (expected, [])


def test_category():
    assert _check_info(_info(category="sensor")) == ([], [])
    assert _check_info(_info(category="lighting"))[0] == [
        "PLUGIN_INFO: invalid category 'lighting': must be one of "
        "['control_surface', 'integration', 'sensor', 'utility']"
    ]


def test_license():
    assert _check_info(_info(license="Apache-2.0")) == ([], [])
    assert _check_info(_info(license="mit")) == ([], [])
    errors, _ = _check_info(_info(license="GPL-3.0"))
    assert errors == [
        "PLUGIN_INFO: license 'GPL-3.0' is not MIT-compatible. Must be one of: "
        "['0bsd', 'apache-2.0', 'bsd-2-clause', 'bsd-3-clause', 'cc0-1.0', 'isc', "
        "'mit', 'psf', 'unlicense']"
    ]
    assert _check_info(_info(license=" MIT"))[0][0].startswith("PLUGIN_INFO: license ' MIT'")


def test_a_license_that_is_not_text_raises():
    with pytest.raises(AttributeError):
        _check_info(_info(license=1))


def test_capabilities():
    assert _check_info(_info(capabilities=["state_read", "usb_access"])) == ([], [])
    assert _check_info(_info(capabilities="state_read"))[0] == ["PLUGIN_INFO: capabilities must be a list"]
    errors, _ = _check_info(_info(capabilities=["state_read", "fly", "swim"]))
    allowed = sorted(v.VALID_CAPABILITIES)
    assert errors == [
        f"PLUGIN_INFO: invalid capability 'fly': must be one of {allowed}",
        f"PLUGIN_INFO: invalid capability 'swim': must be one of {allowed}",
    ]


def test_platforms():
    assert _check_info(_info(platforms=["all", "linux_arm64"])) == ([], [])
    assert _check_info(_info(platforms="all"))[0] == ["PLUGIN_INFO: platforms must be a list"]
    assert _check_info(_info(platforms=["mac_arm64"]))[0] == [
        "PLUGIN_INFO: invalid platform 'mac_arm64': must be one of "
        "['all', 'linux_arm64', 'linux_x64', 'win_x64']"
    ]


_BAD_ALIAS = ("must be one lowercase URL segment (letters, digits, hyphens, "
              "underscores; max 32 chars)")
_NEEDS_GUEST = "PLUGIN_INFO: guest_alias requires the guest_endpoints capability"


@pytest.mark.parametrize("alias, caps, expected", [
    ("present", ["guest_endpoints"], []),
    ("a-b_c9", ["guest_endpoints"], []),
    ("a" * 32, ["guest_endpoints"], []),
    ("a" * 33, ["guest_endpoints"], [f"PLUGIN_INFO: invalid guest_alias '{'a' * 33}': {_BAD_ALIAS}"]),
    ("Present", ["guest_endpoints"], [f"PLUGIN_INFO: invalid guest_alias 'Present': {_BAD_ALIAS}"]),
    ("9lives", ["guest_endpoints"], [f"PLUGIN_INFO: invalid guest_alias '9lives': {_BAD_ALIAS}"]),
    (7, ["guest_endpoints"], [f"PLUGIN_INFO: invalid guest_alias '7': {_BAD_ALIAS}"]),
    ("api", ["guest_endpoints"], ["PLUGIN_INFO: guest_alias 'api' is a reserved platform path"]),
    ("openapi.json", ["guest_endpoints"], [f"PLUGIN_INFO: invalid guest_alias 'openapi.json': {_BAD_ALIAS}"]),
    ("simulator", [], ["PLUGIN_INFO: guest_alias 'simulator' is a reserved platform path", _NEEDS_GUEST]),
    ("present", [], [_NEEDS_GUEST]),
    ("BAD", ["state_read"], [f"PLUGIN_INFO: invalid guest_alias 'BAD': {_BAD_ALIAS}", _NEEDS_GUEST]),
    ("present", None, [_NEEDS_GUEST]),
    ("present", "guest_endpoints", ["PLUGIN_INFO: capabilities must be a list"]),
])
def test_guest_alias(alias, caps, expected):
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
        "PLUGIN_INFO: invalid plugin ID 'Bad.Id': must be lowercase letters, numbers, "
        "and underscores, starting with a letter",
        "PLUGIN_INFO: plugin ID must not contain dots (breaks state key parsing)",
        "PLUGIN_INFO: invalid category 'nope': must be one of "
        "['control_surface', 'integration', 'sensor', 'utility']",
        "PLUGIN_INFO: license 'GPL' is not MIT-compatible. Must be one of: "
        "['0bsd', 'apache-2.0', 'bsd-2-clause', 'bsd-3-clause', 'cc0-1.0', 'isc', "
        "'mit', 'psf', 'unlicense']",
        f"PLUGIN_INFO: invalid capability 'x': must be one of {sorted(v.VALID_CAPABILITIES)}",
        "PLUGIN_INFO: invalid platform 'y': must be one of "
        "['all', 'linux_arm64', 'linux_x64', 'win_x64']",
        "PLUGIN_INFO: guest_alias 'api' is a reserved platform path",
        _NEEDS_GUEST,
        "PLUGIN_INFO: unsafe dependency '-e .' -- must be a plain 'package' or "
        "'package>=x.y' requirement (no URLs, VCS, pip options, or '@' direct references)",
    ]
    assert warnings == [
        "PLUGIN_INFO: version '1' doesn't follow semver format (X.Y.Z)",
        "PLUGIN_INFO: has pip dependencies ['-e .'] -- ensure all are MIT-compatible",
    ]


# ---------------------------------------------------------------------------
# CONFIG_SCHEMA, CONFIG_SECRETS, EXTENSIONS
# ---------------------------------------------------------------------------


def _schema(schema):
    result = v.ValidationResult(Path("x"))
    v.validate_config_schema(schema, result)
    return result.errors, result.warnings


def test_config_schema_not_a_dict():
    assert _schema([1]) == (["CONFIG_SCHEMA must be a dict"], [])


def test_config_schema_fields():
    allowed = sorted(v.VALID_CONFIG_TYPES)
    errors, warnings = _schema({
        "ok": {"type": "string"},
        "notdict": "string",
        "secret_bad": {"type": "string", "secret": "true"},
        "secret_ok": {"type": "string", "secret": False},
        "notype": {"label": "x", "secret": 1},
        "badtype": {"type": "table"},
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
            "password": {"type": "string", "secret": True},
            "token": {"type": "string", "secret": False},
            "raw": "string",
        }},
    })
    assert errors == [
        "CONFIG_SCHEMA field 'notdict' must be a dict",
        "CONFIG_SCHEMA field 'secret_bad' has 'secret' that is not true or false",
        "CONFIG_SCHEMA field 'notype' has 'secret' that is not true or false",
        "CONFIG_SCHEMA field 'notype' missing 'type'",
        f"CONFIG_SCHEMA field 'badtype' has invalid type 'table': must be one of {allowed}",
        "CONFIG_SCHEMA field 'sel' is type 'select' but missing 'options' list",
        "CONFIG_SCHEMA field 'grp' is type 'group' but missing 'fields' dict",
        f"CONFIG_SCHEMA field 'inner_bad' has invalid type 'nope': must be one of {allowed}",
        "CONFIG_SCHEMA field 'inner_notype' missing 'type'",
        "CONFIG_SCHEMA field 'deep' is type 'select' but missing 'options' list",
        "CONFIG_SCHEMA field 'ml' is type 'mapping_list' but missing 'item_schema'",
        "CONFIG_SCHEMA field 'ml_ok' column 'password' has 'secret': a mapping_list row has "
        "no id to keep a login under. Keep the list outside CONFIG_SCHEMA and declare it in "
        "CONFIG_SECRETS.",
        "CONFIG_SCHEMA field 'ml_ok' column 'token' has 'secret': a mapping_list row has "
        "no id to keep a login under. Keep the list outside CONFIG_SCHEMA and declare it in "
        "CONFIG_SECRETS.",
    ]
    assert warnings == []


def test_a_group_whose_fields_are_not_a_dict_says_nothing_more():
    assert _schema({"g": {"type": "group", "fields": None}}) == ([], [])


def _secrets(secrets, schema=None):
    result = v.ValidationResult(Path("x"))
    v.validate_config_secrets(secrets, schema, result)
    return result.errors, result.warnings


def test_config_secrets_not_a_dict():
    assert _secrets([1]) == (["CONFIG_SECRETS must be a dict"], [])


def test_config_secrets_good():
    assert _secrets({"streams": {"id": "stream_id", "fields": ["password"], "generated": True}},
                    {"other": {}}) == ([], [])


def test_config_secrets_every_problem_in_its_order():
    errors, warnings = _secrets({
        "": {"id": "x", "fields": ["y"]},
        5: {"id": "x", "fields": ["y"]},
        "schema_key": {"id": "x", "fields": ["y"]},
        "notdict": ["x"],
        "unknown": {"id": "x", "fields": ["y"], "extra": 1, "also": 2},
        "noid": {"fields": ["y"]},
        "emptyid": {"id": "", "fields": ["y"]},
        "intid": {"id": 3, "fields": ["y"]},
        "nofields": {"id": "x"},
        "emptyfields": {"id": "x", "fields": []},
        "strfields": {"id": "x", "fields": "y"},
        "badnames": {"id": "x", "fields": ["y", "", 3]},
        "dupes": {"id": "x", "fields": ["y", "y"]},
        "idlogin": {"id": "y", "fields": ["y", "z"]},
        "intid_named": {"id": 3, "fields": [3]},
        "gen": {"id": "x", "fields": ["y"], "generated": "yes"},
        "gen_ok": {"id": "x", "fields": ["y"], "generated": False},
        "[bracket]": {"id": "a=b", "fields": ["y"]},
    }, {"schema_key": {"type": "string"}})
    assert errors == [
        "CONFIG_SECRETS has a key that is not a non-empty string: ''",
        "CONFIG_SECRETS has a key that is not a non-empty string: 5",
        "CONFIG_SECRETS['schema_key']: 'schema_key' is also a CONFIG_SCHEMA field. Mark a "
        "schema field with 'secret': True instead.",
        "CONFIG_SECRETS['notdict'] must be a dict with 'id' and 'fields'",
        "CONFIG_SECRETS['unknown'] has unknown key(s) ['also', 'extra']: only 'id', 'fields' "
        "and 'generated'",
        "CONFIG_SECRETS['noid'] needs 'id': the key in each item that holds its unique id",
        "CONFIG_SECRETS['emptyid'] needs 'id': the key in each item that holds its unique id",
        "CONFIG_SECRETS['intid'] needs 'id': the key in each item that holds its unique id",
        "CONFIG_SECRETS['nofields'] needs 'fields': a list of the keys in each item that hold "
        "a login",
        "CONFIG_SECRETS['emptyfields'] needs 'fields': a list of the keys in each item that "
        "hold a login",
        "CONFIG_SECRETS['strfields'] needs 'fields': a list of the keys in each item that "
        "hold a login",
        "CONFIG_SECRETS['badnames']['fields'] must hold non-empty strings only",
        "CONFIG_SECRETS['dupes']['fields'] names a key twice",
        "CONFIG_SECRETS['idlogin']: the id key 'y' cannot also be a login",
        "CONFIG_SECRETS['intid_named'] needs 'id': the key in each item that holds its unique id",
        "CONFIG_SECRETS['intid_named']['fields'] must hold non-empty strings only",
        "CONFIG_SECRETS['gen']['generated'] must be true or false",
    ]
    assert warnings == []


def test_config_secrets_against_a_schema_that_is_not_a_dict():
    assert _secrets({"k": {"id": "x", "fields": ["y"]}}, ["k"]) == ([], [])


def _extensions(ext):
    result = v.ValidationResult(Path("x"))
    v.validate_extensions_schema(ext, result)
    return result.errors, result.warnings


def test_extensions_not_a_dict():
    assert _extensions([]) == (["EXTENSIONS must be a dict"], [])


def test_extensions_every_problem_in_its_order():
    errors, warnings = _extensions({
        "views": [{"id": "a"}, {"id": "b"}, {"id": "a"}, "x", {"name": "n"}, {"id": ""}, {"id": 3}],
        "toolbar": [{"id": "a"}],
        "status_cards": {"id": "a"},
        "panel_elements": [{"type": "gauge"}, {"id": "gauge"}, {"type": "gauge"}],
        "device_panels": [],
        "context_actions": [{"id": "a"}],
    })
    assert errors == [
        "EXTENSIONS['views'] has duplicate id 'a'",
        "EXTENSIONS['views'][3] must be a dict",
        "EXTENSIONS['views'][4] missing 'id' (string)",
        "EXTENSIONS['views'][5] missing 'id' (string)",
        "EXTENSIONS['views'][6] missing 'id' (string)",
        "EXTENSIONS has unknown type 'toolbar': must be one of ['context_actions', "
        "'device_panels', 'panel_elements', 'status_cards', 'views']",
        "EXTENSIONS['status_cards'] must be a list",
        "EXTENSIONS['panel_elements'][1] missing 'type' (string)",
        "EXTENSIONS['panel_elements'] has duplicate type 'gauge'",
    ]
    assert warnings == []


# ---------------------------------------------------------------------------
# A plugin folder
# ---------------------------------------------------------------------------


def test_a_good_plugin_folder_says_nothing(tmp_path):
    assert _check_dir(_plugin(tmp_path)) == ([], [])


def test_no_plugin_module(tmp_path):
    folder = _plugin(tmp_path, file_name="acme_widget.py")
    assert _check_dir(folder) == (
        ["No *_plugin.py file found (plugin loader looks for this pattern)"], [])


def test_two_plugin_modules(tmp_path):
    folder = _plugin(tmp_path)
    (folder / "spare_plugin.py").write_text(GOOD_SOURCE, encoding="utf-8")
    errors, warnings = _check_dir(folder)
    assert errors == []
    assert len(warnings) == 1
    assert warnings[0].startswith("Multiple *_plugin.py files found: [")
    assert "'acme_widget_plugin.py'" in warnings[0] and "'spare_plugin.py'" in warnings[0]


def test_an_unreadable_plugin_module(tmp_path):
    folder = _plugin(tmp_path)
    (folder / "acme_widget_plugin.py").write_bytes(b"\xff\xfe\x00bad")
    errors, warnings = _check_dir(folder)
    assert len(errors) == 1
    assert errors[0].startswith("Cannot read acme_widget_plugin.py: ")
    assert "codec can't decode" in errors[0]
    assert warnings == []


def test_no_plugin_info_at_all(tmp_path):
    src = "class AcmeWidgetPlugin:\n    async def start(self):\n        pass\n    async def stop(self):\n        pass\n"
    folder = _plugin(tmp_path, source=src)
    assert _check_dir(folder) == (
        ["acme_widget_plugin.py: missing PLUGIN_INFO class attribute"],
        ["acme_widget_plugin.py: could not read PLUGIN_INFO statically -- it is not being "
         "validated. Declare it as a dict literal."],
    )


def test_plugin_info_built_at_runtime(tmp_path):
    folder = _plugin(tmp_path, source=_source_with("make_info()"))
    assert _check_dir(folder) == ([], [
        "acme_widget_plugin.py: could not read PLUGIN_INFO statically -- it is not being "
        "validated. Declare it as a dict literal."])


def test_plugin_info_with_values_built_at_runtime(tmp_path):
    info = dict(GOOD_INFO)
    lines = repr({k: val for k, val in info.items() if k not in ("version", "author")})
    lines = lines[:-1] + ", 'version': VERSION, 'author': get_author()}"
    folder = _plugin(tmp_path, source=_source_with(lines, extra_module="VERSION = read()"))
    assert _check_dir(folder) == ([], [
        "acme_widget_plugin.py: PLUGIN_INFO values built at runtime are not validated: "
        "['author', 'version']"])


def test_an_empty_plugin_info_is_not_validated(tmp_path):
    folder = _plugin(tmp_path, source=_source_with("{}"))
    assert _check_dir(folder) == ([], [])


def test_plugin_info_errors_and_the_folder_name(tmp_path):
    bad = repr(dict(GOOD_INFO, id="other_widget", license="GPL"))
    manifest = dict(GOOD_INFO, id="other_widget", license="GPL")
    folder = _plugin(tmp_path, source=_source_with(bad), manifest=manifest)
    license_error = ("license 'GPL' is not MIT-compatible. Must be one of: "
                     "['0bsd', 'apache-2.0', 'bsd-2-clause', 'bsd-3-clause', 'cc0-1.0', "
                     "'isc', 'mit', 'psf', 'unlicense']")
    assert _check_dir(folder) == (
        [f"PLUGIN_INFO: {license_error}", f"plugin.json: {license_error}"],
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


def test_config_schema_built_at_runtime_is_skipped_quietly(tmp_path):
    src = _source_with(repr(GOOD_INFO), body="    CONFIG_SCHEMA = build_schema()\n")
    assert _check_dir(_plugin(tmp_path, source=src)) == ([], [])


def test_config_secrets_read_beside_the_schema(tmp_path):
    body = ("    CONFIG_SCHEMA = {'streams': {'type': 'string'}}\n"
            "    CONFIG_SECRETS = {'streams': {'id': 'sid', 'fields': ['password']}}\n")
    src = _source_with(repr(GOOD_INFO), body=body)
    assert _check_dir(_plugin(tmp_path, source=src)) == ([
        "CONFIG_SECRETS['streams']: 'streams' is also a CONFIG_SCHEMA field. Mark a schema "
        "field with 'secret': True instead."], [])


def test_config_secrets_with_a_schema_built_at_runtime(tmp_path):
    body = ("    CONFIG_SCHEMA = build()\n"
            "    CONFIG_SECRETS = {'streams': {'id': 'sid', 'fields': ['password']}}\n")
    src = _source_with(repr(GOOD_INFO), body=body)
    assert _check_dir(_plugin(tmp_path, source=src)) == ([], [])


def test_config_secrets_built_at_runtime_is_a_warning(tmp_path):
    src = _source_with(repr(GOOD_INFO), body="    CONFIG_SECRETS = build()\n")
    assert _check_dir(_plugin(tmp_path, source=src)) == ([], [
        "acme_widget_plugin.py: could not read CONFIG_SECRETS statically -- it is not being "
        "validated. Declare it as a dict literal."])


def test_config_secrets_named_only_in_a_comment_is_a_warning(tmp_path):
    src = GOOD_SOURCE + "\n# no CONFIG_SECRETS here\n"
    assert _check_dir(_plugin(tmp_path, source=src)) == ([], [
        "acme_widget_plugin.py: could not read CONFIG_SECRETS statically -- it is not being "
        "validated. Declare it as a dict literal."])


def test_extensions_in_the_class_are_checked(tmp_path):
    src = _source_with(repr(GOOD_INFO), body="    EXTENSIONS = {'views': [{}]}\n")
    assert _check_dir(_plugin(tmp_path, source=src)) == (
        ["EXTENSIONS['views'][0] missing 'id' (string)"], [])


def test_extensions_through_an_alias_are_not_checked(tmp_path):
    src = _source_with(repr(GOOD_INFO), body="    EXTENSIONS = EXTENSIONS\n",
                       extra_module="EXTENSIONS = {'views': [{}]}")
    assert _check_dir(_plugin(tmp_path, source=src)) == ([], [])


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


def test_plugin_json_that_is_not_an_object_raises(tmp_path):
    with pytest.raises(AttributeError):
        _check_dir(_plugin(tmp_path, manifest=["id"]))


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


def test_plugin_json_alone_is_not_compared_when_plugin_info_is_unreadable(tmp_path):
    manifest = dict(GOOD_INFO, version="9.9.9")
    folder = _plugin(tmp_path, source=_source_with("make()"), manifest=manifest)
    assert _check_dir(folder) == ([], [
        "acme_widget_plugin.py: could not read PLUGIN_INFO statically -- it is not being "
        "validated. Declare it as a dict literal."])


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
        "PLUGIN_INFO: missing required field 'category'",
        "plugin.json: missing required field 'category'",
    ], [])


def test_a_syntax_error_still_gets_the_text_checks(tmp_path):
    src = "PLUGIN_INFO = {\nclass (:\n  def start(\n  time.sleep(\n  asyncio.create_task(\nCONFIG_SECRETS validate_config"
    folder = _plugin(tmp_path, source=src, readme=False, category_dir="sensors")
    assert _check_dir(folder) == (
        ["acme_widget_plugin.py: missing stop() method",
         "acme_widget_plugin.py: uses time.sleep() which blocks the event loop. Use "
         "asyncio.sleep() instead."],
        ["acme_widget_plugin.py: could not read PLUGIN_INFO statically -- it is not being "
         "validated. Declare it as a dict literal.",
         "acme_widget_plugin.py: uses asyncio.create_task() instead of api.create_task() -- "
         "tasks won't be auto-cancelled on stop",
         "acme_widget_plugin.py: could not read CONFIG_SECRETS statically -- it is not being "
         "validated. Declare it as a dict literal.",
         "Missing README.md (recommended for community plugins)"],
    )


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
        f"PLUGIN_INFO: invalid capability 'nope': must be one of {sorted(v.VALID_CAPABILITIES)}",
        "acme_widget_plugin.py: missing start() method",
        "acme_widget_plugin.py: missing stop() method",
        "acme_widget_plugin.py: uses time.sleep() which blocks the event loop. Use "
        "asyncio.sleep() instead.",
        "CONFIG_SCHEMA field 'a' is type 'select' but missing 'options' list",
        "CONFIG_SECRETS['b'] needs 'fields': a list of the keys in each item that hold a login",
        "EXTENSIONS['views'] must be a list",
        "acme_widget_plugin.py: validate_config must be a @classmethod or @staticmethod -- "
        "OpenAVC calls it on the class, which may not be running",
        "plugin.json: dependencies must be a list",
        "Mismatch: PLUGIN_INFO.name='Acme Widget' vs plugin.json.name='Other'",
    ]
    assert warnings == [
        "acme_widget_plugin.py: PLUGIN_INFO values built at runtime are not validated: ['extra']",
        "PLUGIN_INFO: version '1' doesn't follow semver format (X.Y.Z)",
        "PLUGIN_INFO: has pip dependencies ['paho'] -- ensure all are MIT-compatible",
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


def test_an_index_entry_with_a_license_that_is_not_text_raises(tmp_path):
    (tmp_path / "index.json").write_text(json.dumps({"plugins": [_entry(license=None)]}))
    with pytest.raises(AttributeError):
        v.validate_index_json(tmp_path, [])


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
        "        WARNING: PLUGIN_INFO: version '1' doesn't follow semver format (X.Y.Z)\n"
        "  WARN  integrations/beta\n"
        "        WARNING: plugin.json: version '1' doesn't follow semver format (X.Y.Z)\n"
        "  WARN  integrations/beta\n"
        "        WARNING: Missing README.md (recommended for community plugins)\n"
        "\n"
        "Validated 1 plugin(s): 1 passed, 0 failed\n"
        "  3 warning(s)\n"
    )
    assert out.startswith("  PASS  integrations/beta\n        WARNING: PLUGIN_INFO: version")


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
        "        ERROR: No *_plugin.py file found (plugin loader looks for this pattern)\n"
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
