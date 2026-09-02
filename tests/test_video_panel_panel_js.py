"""What the Video Panel tile does when the plugin's state goes away.

The panel element (integrations/video_panel/panel/video_stream.js) is vanilla
browser JS with no build step, so these run the real file in a jsdom window via
tests/fixtures/video_stream_harness.cjs and assert on what the viewer would see.

Node and jsdom are not dependencies of this repo -- jsdom lives in the platform
checkout's openavc/web/programmer/node_modules -- so these skip when the
toolchain is not there rather than failing a Python-only run. Set
OPENAVC_REQUIRE_PANEL_JS=1 to make a missing toolchain an error instead, for a
run that has promised to cover this.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Matches the sibling suites: this directory on the path, then the shared
# lookup for the openavc checkout (jsdom lives in its node_modules).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _platform_root import platform_root  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HARNESS = _REPO_ROOT / "tests" / "fixtures" / "video_stream_harness.cjs"
_PANEL = _REPO_ROOT / "integrations" / "video_panel" / "panel"
_JS = _PANEL / "video_stream.js"
_HTML = _PANEL / "video_stream.html"

# What the element says when it cannot play and nothing better was supplied.
# Mirrors NO_STREAM_TEXT / SOURCE_GONE_TEXT in video_stream.js.
NO_STREAM = "This source has no stream right now."
SOURCE_GONE = "This source is no longer available."


def _node_modules():
    root = platform_root()
    return None if root is None else root / "openavc" / "web" / "programmer" / "node_modules"


def _toolchain_problem():
    if shutil.which("node") is None:
        return "node not installed"
    modules = _node_modules()
    if modules is None:
        return "no openavc platform checkout found (set OPENAVC_PLATFORM_ROOT)"
    if not (modules / "jsdom").is_dir():
        return f"jsdom not installed (run `npm ci` in {modules.parent})"
    if not _HARNESS.is_file():
        return "video_stream harness missing"
    return None


@pytest.fixture(scope="module")
def tile():
    problem = _toolchain_problem()
    if problem:
        if os.environ.get("OPENAVC_REQUIRE_PANEL_JS"):
            raise AssertionError(f"panel JS coverage was required but {problem}")
        pytest.skip(problem)
    proc = subprocess.run(
        ["node", str(_HARNESS), str(_JS), str(_HTML)],
        capture_output=True,
        text=True,
        # The harness reports what the viewer would read, and the overlay says
        # "Connecting…". Without this, Python decodes node's UTF-8 with the
        # platform's preferred encoding -- cp1252 on Windows -- and every
        # assertion on a sentence containing punctuation fails for a reason
        # that has nothing to do with the element.
        encoding="utf-8",
        env={**os.environ, "NODE_PATH": str(_node_modules())},
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(f"harness crashed (rc={proc.returncode}):\n{proc.stderr}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise AssertionError(f"unparseable harness output:\n{proc.stdout}\n{proc.stderr}") from exc


# The teardown deletes the stream list and the per-stream status key in one
# batch, and the order is whatever a Python set iterates, so both are pinned.
@pytest.mark.parametrize("order", ["status_key_first", "list_key_first"])
def test_a_plugin_restart_does_not_permanently_kill_the_tile(tile, order):
    """Stopping the plugin deletes every key it set, and the panel forwards each
    deletion to the element as value=null. That is not a stream being deleted --
    a plugin update, a settings save, or a sidecar crash all do it -- and the
    element must come back on its own when the plugin republishes, without
    anyone reloading the panel."""
    result = tile["plugin_restart_" + order]

    assert result["before"]["deliveries"] == 1  # it was playing

    stopped = result["stopped"]["overlay"]
    assert stopped["text"] == NO_STREAM
    assert stopped["spinner"] is False  # a block, not a connection failure
    assert stopped["retry"] is True  # and never a dead end
    assert result["stopped"]["deliveries"] == 1  # no retry storm against a stopped plugin

    back = result["back"]["overlay"]
    assert result["back"]["deliveries"] == 2, "the republished list did not revive the tile"
    assert back["spinner"] is True
    assert back["text"] == "Connecting…"


def test_a_stream_that_really_was_deleted_stays_gone(tile):
    """The other half: the same null, followed by a list that no longer names
    the stream, must NOT come back to life -- and must say the thing that is
    actually true rather than the generic sentence."""
    result = tile["deleted_stream_stays_gone"]
    overlay = result["after"]["overlay"]
    assert overlay["text"] == SOURCE_GONE
    assert overlay["spinner"] is False
    assert result["after"]["deliveries"] == result["before"]["deliveries"]


def test_retry_while_the_plugin_is_down_says_so_instead_of_spinning(tile):
    """Retry is offered on the block, and pressing it re-reads what is known
    before doing anything. With the plugin still down that is the same sentence,
    not a spinner -- a button that pretends to be trying is worse than none."""
    result = tile["retry_after_the_key_goes"]
    assert result["blocked"]["retry"] is True
    pressed = result["pressed"]["overlay"]
    assert pressed["text"] == NO_STREAM
    assert pressed["spinner"] is False
    assert result["pressed"]["deliveries"] == 1


def test_a_republished_row_that_cannot_play_keeps_its_own_sentence(tile):
    """The generic sentence is a floor, not an override: when the plugin comes
    back and still says the source is off, the viewer reads why."""
    result = tile["a_source_that_is_still_offline_keeps_its_own_sentence"]
    assert result["overlay"]["text"] == "Front Door is switched off."
    assert result["deliveries"] == 1  # blocked, so nothing was started


# ── The ext token has a TTL; a wall panel does not ──────────────────────────
#
# On a claimed instance the panel mints a plugin-scoped token for the iframe and
# it expires after hours. A tile in a meeting room outlives that by design, so
# every /ext/ call starts refusing partway through the day and the tile
# reconnects forever against a credential that will never work again -- video
# gone until somebody reloads the panel, which on a wall plate means somebody
# standing at it. The host has always been able to mint a new one on demand
# ('openavc:request-init' in panel.js); nothing here ever asked.


def test_an_expired_token_is_replaced_without_anyone_reloading(tile):
    """The whole point: the tile notices the 401, asks for a new token, and the
    next delivery call carries it. No reload, nobody standing at the panel."""
    result = tile["an_expired_token_is_replaced_without_a_reload"]

    assert result["before"]["token"] == "first-token"  # it was playing on the old one
    assert result["asked"] == 1, "the 401 did not produce an openavc:request-init"
    assert result["after"]["token"] == "second-token", (
        "playback resumed on the dead token"
    )
    assert result["after"]["deliveries"] > result["before"]["deliveries"]
    # And it is trying, not sitting on an error.
    assert result["after"]["overlay"]["spinner"] is True


def test_the_same_dead_token_is_only_asked_about_once(tile):
    """The backoff re-fires the same 401 every few seconds. One request per
    failure would be a loop against the host, minting a token each time."""
    assert tile["a_token_is_only_ever_asked_about_once"]["asks"] == 1


def test_an_instance_with_no_token_never_asks(tile):
    """An open instance issues no token, so a 401 there is about something
    else. Asking would hide the real fault behind a refresh that cannot help."""
    assert tile["an_open_instance_never_asks"]["asks"] == 0


def test_a_dead_mjpeg_connection_asks_once_and_then_stops(tile):
    """An <img> reports no status, so an expired token and an unplugged encoder
    arrive as the same event. Asking once per token settles which it was
    without turning an unplugged camera into a refresh loop."""
    assert tile["a_dead_mjpeg_connection_asks_once"]["asks"] == 1
