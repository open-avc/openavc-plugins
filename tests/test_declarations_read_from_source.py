"""OpenAVC reads each plugin's declarations from its source as the loader
finds them on the imported class.

The platform's source reader (``openavc.core.plugin_source``) lets a check
judge a plugin by the platform's own rules without importing it. These tests
hold it to the loader on every plugin here: each plugin folder is imported the
way OpenAVC imports an installed plugin, and its main module is read as text,
and the two must give the same class and the same declarations, with nothing
left unread. Then the platform's rules must pass both.

They need an OpenAVC checkout (found the way ``_platform_root.py`` describes)
and skip without one. CI runs them on a fresh clone of the platform, where that
skip fails the run (tests/conftest.py).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _platform_root import add_platform_to_path  # noqa: E402

add_platform_to_path()

try:
    from openavc.core.plugin_declarations import Declared, first_problem
    from openavc.core.plugin_import import find_plugin_class
    from openavc.core.plugin_source import read_source
except ModuleNotFoundError:
    pytest.skip("openavc repo not available as sibling directory", allow_module_level=True)

REPO_ROOT = Path(__file__).resolve().parents[1]
CATEGORY_DIRS = ("control_surfaces", "integrations", "sensors", "utility")

PLUGIN_DIRS = sorted(
    folder for category in CATEGORY_DIRS if (REPO_ROOT / category).is_dir()
    for folder in (REPO_ROOT / category).iterdir()
    if folder.is_dir() and not folder.name.startswith((".", "_"))
    and any(folder.glob("*_plugin.py"))
)


def test_every_plugin_is_here():
    assert len(PLUGIN_DIRS) >= 6, [p.name for p in PLUGIN_DIRS]


@pytest.fixture
def imported(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = set(sys.modules)
    yield find_plugin_class
    for name in set(sys.modules) - before:
        if name.startswith("plugin_"):
            sys.modules.pop(name, None)


@pytest.mark.parametrize("folder", PLUGIN_DIRS, ids=lambda p: p.name)
def test_the_source_reads_as_the_imported_class(folder, imported):
    cls = imported(folder)
    assert cls is not None, f"{folder.name}: no plugin class imported"
    (main,) = folder.glob("*_plugin.py")
    read = read_source(main.read_text(encoding="utf-8"))
    assert read is not None, f"{main.name}: no plugin class read"
    held = Declared.of_class(cls)
    assert read.class_name == held.class_name
    assert read.unread == ()
    assert read == held
    assert first_problem(read) is None
