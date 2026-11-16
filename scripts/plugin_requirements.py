#!/usr/bin/env python3
"""The Python libraries OpenAVC installs for this repository's plugins.

    python scripts/plugin_requirements.py    # one requirement per line

CI installs these before it runs the plugins' tests. They are read the way
OpenAVC reads them when it installs a plugin: the text entries of the
``dependencies`` list in PLUGIN_INFO, from the file OpenAVC loads the plugin's
class from, without running it. A list the source builds when the plugin runs
installs nothing, in OpenAVC and here.

plugin.json is not read. OpenAVC never reads it, so a library listed only
there would be installed for the tests and missing from a real install.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from _vendor.plugin_declarations import info_value  # noqa: E402
from _vendor.plugin_source import read_folder  # noqa: E402
from validate import find_plugin_dirs  # noqa: E402


def requirements(repo_root):
    """Each plugin's requirements, plugin by plugin in the order validate.py
    checks them."""
    for plugin_dir in find_plugin_dirs(repo_root):
        declared = read_folder(plugin_dir).declared
        if declared is None:
            continue
        listed = info_value(declared.info, "dependencies")
        if isinstance(listed, (list, tuple)):
            yield from (entry for entry in listed if isinstance(entry, str))


def main():
    for requirement in requirements(REPO_ROOT):
        print(requirement)


if __name__ == "__main__":
    main()
