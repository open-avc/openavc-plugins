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

The Stream Deck keeps a copy of the platform's condition rule too
(``control_surfaces/streamdeck/conditions.py``), and its operator spellings
are the platform's: every operator and every other spelling of one, so a
spelling the platform takes works on a deck as well.

It keeps a copy of the touch panel's press timings as well
(``control_surfaces/streamdeck/press_timing.py``): the defaults are the
schema's, and every timing reads on a deck as the panel's own ``pressTiming``
reads it, run under node, so a key held on a deck repeats and turns into a
long press when the same key on a panel does.

They need an OpenAVC checkout (found the way ``_platform_root.py`` describes)
and skip without one. CI runs them on a fresh clone of the platform, where that
skip fails the run (tests/conftest.py).
"""

import json
import shutil
import subprocess
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
    from openavc.core import condition_eval, plugin_wheels
    from openavc.core.project_loader import UIElement
    from openavc.drivers.spec import is_secret_setting
except ModuleNotFoundError:
    pytest.skip("openavc repo not available as sibling directory", allow_module_level=True)

import validate  # noqa: E402
import vendor_platform_rules  # noqa: E402
from control_surfaces.streamdeck import conditions as deck_conditions  # noqa: E402
from control_surfaces.streamdeck import press_timing as deck_press_timing  # noqa: E402


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


def test_a_deck_takes_every_operator_spelling_the_platform_takes():
    assert deck_conditions.OPERATOR_ALIASES == condition_eval.OPERATOR_ALIASES
    assert deck_conditions.KNOWN_OPERATORS == (
        set(condition_eval.OPERATORS) | set(condition_eval.OPERATOR_ALIASES)
    )


_TIMINGS = ("hold_repeat_ms", "hold_threshold_ms")
_ABSENT = "<absent>"
#: Timings as a config may hold them: numbers, the blanks, text in every
#: form JavaScript's Number() reads (and some it does not), and values that
#: are no number at all.
_TIMING_CASES = [
    _ABSENT, None, 0, -0.0, 150, 150.5, 2500, -5, True, False, [300], [], {"ms": 300},
    "", " ", "300", " 300 ", "+5", "-5", ".5", "5.", "1e3", "1E+3", ".5e1", "00010",
    "0x10", "0X1f", "0o17", "0b11", "-0x10", "0x1g", "1_000", "abc", "nan",
    "Infinity", "-Infinity", "1e999", "0x" + "f" * 300,
]

_PANEL_TIMINGS_SCRIPT = """
const { pressTiming } = await import(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
const out = {};
for (const field of JSON.parse(process.argv[3])) {
    out[field] = cases.map((raw) => pressTiming(raw === '<absent>' ? undefined : raw, field));
}
console.log(JSON.stringify(out));
"""


def _panel_timings():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    module = PLATFORM_ROOT / "openavc" / "web" / "panel" / "modules" / "project_values.js"
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", _PANEL_TIMINGS_SCRIPT, module.as_uri(),
         json.dumps(_TIMING_CASES), json.dumps(_TIMINGS)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_deck_key_falls_back_to_the_panels_press_timings():
    declared = UIElement.KEY_DEFAULTS["bindings"]
    assert deck_press_timing.PRESS_TIMING_DEFAULTS == {
        field: declared[f"do.press.{field}"] for field in _TIMINGS
    }


def test_a_deck_key_reads_each_timing_the_way_the_panel_does():
    panel = _panel_timings()
    for field in _TIMINGS:
        deck = [
            deck_press_timing.press_ms(None if raw == _ABSENT else raw, field)
            for raw in _TIMING_CASES
        ]
        assert deck == panel[field], f"{field} over {_TIMING_CASES}"
