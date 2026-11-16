"""CI installs the libraries OpenAVC installs for each plugin, and no others.

OpenAVC installs a plugin's libraries from the ``dependencies`` in its
PLUGIN_INFO and never reads plugin.json. If CI installed from plugin.json, a
library listed only there would be present for the tests and missing from a
real install: the tests would pass on a plugin that fails to start.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "plugin_requirements.py"


def _plugin(root, name, info_dependencies, manifest_dependencies, category_dir="integrations"):
    folder = root / category_dir / name
    folder.mkdir(parents=True)
    info = {"id": name, "name": name, "version": "1.0.0", "author": "Acme",
            "description": "d", "category": "integration", "license": "MIT"}
    if info_dependencies is not None:
        info["dependencies"] = info_dependencies
    (folder / f"{name}_plugin.py").write_text(
        f"class Plugin:\n    PLUGIN_INFO = {info!r}\n", encoding="utf-8")
    (folder / "plugin.json").write_text(
        json.dumps(dict(info, dependencies=manifest_dependencies)), encoding="utf-8")


def _requirements(root):
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        import plugin_requirements
    finally:
        sys.path.remove(str(SCRIPT.parent))
    return list(plugin_requirements.requirements(root))


def test_the_libraries_come_from_plugin_info_not_plugin_json(tmp_path):
    _plugin(tmp_path, "acme_widget", ["acme-lib>=1.0", "other-lib"], ["only-in-plugin-json"])
    _plugin(tmp_path, "acme_gadget", None, ["listed-but-never-installed"])
    _plugin(tmp_path, "acme_panel", ["panel-lib", 7], [], category_dir="control_surfaces")
    assert _requirements(tmp_path) == ["panel-lib", "acme-lib>=1.0", "other-lib"]


def test_a_list_the_source_builds_installs_nothing(tmp_path):
    folder = tmp_path / "integrations" / "acme_widget"
    folder.mkdir(parents=True)
    (folder / "acme_widget_plugin.py").write_text(
        "class Plugin:\n    PLUGIN_INFO = {'id': 'acme_widget', 'dependencies': libs()}\n",
        encoding="utf-8")
    assert _requirements(tmp_path) == []


def test_this_repository_prints_one_requirement_a_line():
    proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, check=True)
    printed = proc.stdout.splitlines()
    assert printed == _requirements(REPO_ROOT)
    assert printed and all(line.strip() == line and line for line in printed)
