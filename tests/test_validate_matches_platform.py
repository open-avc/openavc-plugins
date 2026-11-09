"""validate.py's copies of OpenAVC's plugin rules match the platform's.

validate.py runs in this repository's CI and on contributors' machines without
OpenAVC installed, so it keeps its own copy of each table the platform checks a
plugin against when it enables one. A table changed on one side and not the
other means a plugin that passes here and is refused when it is enabled, or the
reverse. These tests read both sides and fail on any difference.

They need an OpenAVC checkout (found the way ``_platform_root.py`` describes)
and skip without one. This repository's CI does not run pytest, so they run
wherever the tests are run by hand.

Two of validate.py's tables have no table on the platform to compare with:
``VALID_PLATFORMS`` is the set of ids ``get_platform_id()`` can return, and
``VALID_CONFIG_TYPES`` is the field types the Programmer's plugin settings form
draws. Those are compared by eye.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _platform_root import add_platform_to_path  # noqa: E402

add_platform_to_path()

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from openavc.core import plugin_extensions, plugin_manifest, plugin_secrets, plugin_wheels
except ModuleNotFoundError:
    pytest.skip("openavc repo not available as sibling directory", allow_module_level=True)

import validate  # noqa: E402


@pytest.mark.parametrize("name", [
    "REQUIRED_MANIFEST_FIELDS",
    "VALID_CAPABILITIES",
    "VALID_CATEGORIES",
    "MIT_COMPATIBLE_LICENSES",
    "RESERVED_GUEST_ALIASES",
])
def test_manifest_tables(name):
    assert getattr(validate, name) == getattr(plugin_manifest, name)


def test_guest_alias_pattern():
    assert validate.GUEST_ALIAS_PATTERN == plugin_manifest.GUEST_ALIAS_RE.pattern


def test_extension_types_and_their_id_field():
    assert validate.EXTENSION_ID_FIELD == plugin_extensions.EXTENSION_ID_FIELD
    assert validate.VALID_EXTENSION_TYPES == plugin_extensions.VALID_EXTENSION_TYPES


def test_config_secrets_keys():
    assert validate._CONFIG_SECRETS_KEYS == plugin_secrets._LIST_DECLARATION_KEYS


def test_vcs_prefixes():
    assert validate._VCS_PREFIXES == plugin_wheels._VCS_PREFIXES


@pytest.mark.parametrize("req", [
    "pkg", "Pkg", "pkg>=1.0", "pkg[extra]", 'pkg; sys_platform=="win32"', "  pkg  ",
    "", "   ", "-r requirements.txt", "--index-url=https://x", "./local", ".hidden",
    "/abs/path", "\\windows\\path", "http://example.com/pkg.tar.gz", "file:///tmp/pkg",
    "pkg @ https://example.com/pkg.whl", "pkg@1", "git+https://example.com/repo",
    "GIT+https://example.com/repo", "hg+x", "svn+x", "bzr+x", "_private", "~tilde",
])
def test_a_dependency_is_safe_on_both_sides(req):
    assert validate.is_safe_requirement(req) is plugin_wheels._is_safe_requirement(req)
