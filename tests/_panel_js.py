"""Running the Video Panel tile's JavaScript harnesses from pytest.

The tile (integrations/video_panel/panel/video_stream.js) is vanilla browser JS
with no build step, so its tests run the real file in a jsdom window under node
and read back what the harness printed as JSON.

Node and jsdom are not dependencies of this repo -- jsdom lives in the platform
checkout's openavc/web/programmer/node_modules -- so a run without them skips
rather than failing a Python-only run. Set OPENAVC_REQUIRE_PANEL_JS=1 to make a
missing toolchain an error instead, for a run that has promised to cover this.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _platform_root import platform_root  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
PANEL = REPO_ROOT / "integrations" / "video_panel" / "panel"
TILE_JS = PANEL / "video_stream.js"
TILE_HTML = PANEL / "video_stream.html"
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def node_modules():
    root = platform_root()
    return None if root is None else root / "openavc" / "web" / "programmer" / "node_modules"


def toolchain_problem(harness):
    if shutil.which("node") is None:
        return "node not installed"
    modules = node_modules()
    if modules is None:
        return "no openavc platform checkout found (set OPENAVC_PLATFORM_ROOT)"
    if not (modules / "jsdom").is_dir():
        return f"jsdom not installed (run `npm ci` in {modules.parent})"
    if not harness.is_file():
        return f"{harness.name} missing"
    return None


def run_harness(harness):
    """Run one harness against the tile and return what it printed, parsed.

    Skips (or, under OPENAVC_REQUIRE_PANEL_JS, fails) when node or jsdom is
    missing.
    """
    problem = toolchain_problem(harness)
    if problem:
        if os.environ.get("OPENAVC_REQUIRE_PANEL_JS"):
            raise AssertionError(f"panel JS coverage was required but {problem}")
        pytest.skip(problem)
    proc = subprocess.run(
        ["node", str(harness), str(TILE_JS), str(TILE_HTML)],
        capture_output=True,
        text=True,
        # The harness reports what the viewer would read, and the overlay says
        # "Connecting…". Without this, Python decodes node's UTF-8 with the
        # platform's preferred encoding -- cp1252 on Windows -- and every
        # assertion on a sentence containing punctuation fails for a reason
        # that has nothing to do with the element.
        encoding="utf-8",
        env={**os.environ, "NODE_PATH": str(node_modules())},
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(f"harness crashed (rc={proc.returncode}):\n{proc.stderr}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise AssertionError(f"unparseable harness output:\n{proc.stdout}\n{proc.stderr}") from exc
