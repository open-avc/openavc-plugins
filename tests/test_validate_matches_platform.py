"""validate.py judges a plugin by OpenAVC's own rules, and its two small
copies of platform rules agree with the platform.

The rules for what a plugin declares are the platform's modules, vendored
into scripts/_vendor/. CI's ``scripts/vendor_platform_rules.py --check``
compares them with the platform on GitHub; the first test here compares them
with the platform checkout these tests run beside, so a rule changed in a
paired worktree fails here before either side lands.

validate.py still keeps two copies of a platform rule that lives outside the
vendored modules: which dependency strings the installer takes
(``is_safe_requirement``) and which settings field holds a login
(``is_login``). Each is compared with the platform's on the same cases.

They need an OpenAVC checkout (found the way ``_platform_root.py`` describes)
and skip without one. CI runs them on a fresh clone of the platform, where that
skip fails the run (tests/conftest.py).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _platform_root import add_platform_to_path  # noqa: E402

PLATFORM_ROOT = add_platform_to_path()

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    from openavc.core import plugin_wheels
    from openavc.drivers.spec import is_secret_setting
except ModuleNotFoundError:
    pytest.skip("openavc repo not available as sibling directory", allow_module_level=True)

import validate  # noqa: E402
import vendor_platform_rules  # noqa: E402


def test_the_vendored_rules_are_the_platforms():
    expected = vendor_platform_rules.expected_files(
        lambda source_path: (PLATFORM_ROOT / source_path).read_bytes())
    assert vendor_platform_rules.drift(expected) == [], (
        "regenerate with: python scripts/vendor_platform_rules.py --openavc <platform checkout>")


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


@pytest.mark.parametrize("name, definition", [
    ("password", {"type": "string"}),
    ("password", None),
    ("key", {"type": "string", "secret": True}),
    ("key", {"type": "string", "secret": "true"}),
    ("key", {"type": "string", "secret": 1}),
    ("key", {"type": "string", "secret": False}),
    ("pin", {"type": "password"}),
    ("host", {"type": "string"}),
    ("host", "string"),
    ("Password", {"type": "string"}),
])
def test_a_login_field_is_one_on_both_sides(name, definition):
    assert validate.is_login(name, definition) is is_secret_setting(name, definition)
