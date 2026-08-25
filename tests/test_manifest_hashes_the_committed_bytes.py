"""manifest.json must record the bytes a download receives, not the bytes on disk.

An install fetches each file from GitHub and refuses it when the SHA-256 does
not match this manifest. GitHub serves the committed blob, which always has LF
line endings. A Windows checkout with `core.autocrlf=true` has CRLF in the
working copy of every text file, so a manifest built by hashing files on disk
records hashes no download can match -- and nothing complains: the build
succeeds, `--check` passes against its own output, and every plugin in the
catalog quietly becomes uninstallable. It happened on 2026-08-25.

So the property under test is not "the manifest is in sync" (that check compares
the generator with itself). It is: **every recorded hash equals the hash of the
committed blob.** That is true on every platform or the generator is wrong.
"""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(_REPO_ROOT), *args], capture_output=True, check=True
    ).stdout


def _staged(path: str) -> bytes | None:
    """The committed/staged bytes for a repo-relative path, or None."""
    try:
        return _git("show", f":{path}")
    except subprocess.CalledProcessError:
        return None


@pytest.fixture(scope="module")
def manifest() -> dict:
    p = _REPO_ROOT / "manifest.json"
    if not p.exists():
        pytest.skip("manifest.json not present")
    return json.loads(p.read_text(encoding="utf-8"))


def test_every_recorded_hash_is_the_hash_of_the_committed_blob(manifest):
    wrong = []
    checked = 0
    for plugin_id, entry in manifest["plugins"].items():
        for path, recorded in entry["files"].items():
            blob = _staged(path)
            if blob is None:
                wrong.append(f"{path}: tracked in the manifest but not in git")
                continue
            checked += 1
            actual = hashlib.sha256(blob).hexdigest()
            if actual != recorded:
                on_disk = (_REPO_ROOT / path).read_bytes()
                hint = (
                    " (matches the file on disk, so the manifest was built from "
                    "the working tree rather than from git)"
                    if hashlib.sha256(on_disk).hexdigest() == recorded
                    else ""
                )
                wrong.append(f"{plugin_id}: {path}{hint}")
    assert checked, "no manifest entries were checked"
    assert not wrong, "manifest hashes do not match the committed bytes:\n  " + "\n  ".join(wrong)


def test_the_vendored_bundle_is_never_line_ending_converted():
    """hls.js ships byte-for-byte and its hash is published. A checkout that
    rewrote its line endings would produce a file the installer refuses."""
    bundle = "integrations/video_panel/panel/hls.light.min.js"
    if _staged(bundle) is None:
        pytest.skip("hls.js bundle not present")
    attrs = _git("check-attr", "text", "--", bundle).decode("utf-8")
    assert attrs.strip().endswith(": text: unset"), (
        "the vendored bundle must be marked -text in .gitattributes so no "
        f"checkout can rewrite it; got: {attrs.strip()}"
    )
