#!/usr/bin/env python3
"""Build manifest.json — a SHA-256 for every file in every published plugin.

OpenAVC installs a plugin by walking its directory through the GitHub Contents
API and downloading each file it is told about. That listing arrives over the
network, so the installer needs its own idea of which files a plugin has and
what they should contain. This manifest is that idea: the installer refuses a
file whose hash doesn't match, a file the manifest doesn't list, and a manifest
entry that never arrived. All three matter — per-file hashes alone would still
let an injected extra file through.

The file set comes from ``git ls-files``, so it is exactly what GitHub serves.
Walking the working tree instead would sweep in untracked local junk
(``__pycache__``, editor droppings) and produce a manifest that no real install
could ever satisfy.

Kept separate from index.json on purpose: that file is hand-maintained, and
rewriting it here would reformat every hand-authored line into whatever
``json.dumps`` felt like.

Usage:
    python scripts/build_manifest.py            # write manifest.json
    python scripts/build_manifest.py --check    # verify it is in sync (CI)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = "1"
GENERATOR_VERSION = "1.0.0"


def _tracked_files(repo_root: Path, subdir: str) -> list[Path]:
    """Repo-relative paths of every committed file under ``subdir``."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", "--", subdir],
            capture_output=True,
            check=True,
        ).stdout
    except FileNotFoundError:
        sys.exit("error: git is required to enumerate plugin files")
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: git ls-files failed for {subdir!r}: {e}")
    return [Path(p) for p in out.decode("utf-8").split("\0") if p]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(repo_root: Path) -> dict:
    index_path = repo_root / "index.json"
    if not index_path.exists():
        sys.exit(f"error: {index_path} not found")
    index = json.loads(index_path.read_text(encoding="utf-8"))

    plugins: dict[str, dict] = {}
    problems: list[str] = []
    for entry in index.get("plugins", []):
        plugin_id = entry.get("id")
        rel = entry.get("file")
        if not plugin_id or not rel:
            problems.append(f"index.json entry missing id or file: {entry!r}")
            continue
        tracked = _tracked_files(repo_root, rel)
        if not tracked:
            problems.append(
                f"plugin {plugin_id!r}: no committed files under {rel!r} — "
                "the manifest would let any file through"
            )
            continue
        files = {}
        for p in sorted(tracked, key=lambda x: x.as_posix()):
            full = repo_root / p
            if not full.is_file():
                problems.append(f"plugin {plugin_id!r}: {p.as_posix()} is tracked but missing")
                continue
            files[p.as_posix()] = _sha256(full)
        plugins[plugin_id] = {"files": files}

    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        sys.exit(1)

    return {
        "_meta": {
            "generator_version": GENERATOR_VERSION,
            "schema_version": SCHEMA_VERSION,
            "total_plugins": len(plugins),
        },
        "plugins": dict(sorted(plugins.items())),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify manifest.json matches the committed files; write nothing",
    )
    ap.add_argument("--root", default=None, help="repository root (default: script's parent)")
    args = ap.parse_args(argv)

    repo_root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    manifest = build(repo_root)
    text = json.dumps(manifest, indent=2) + "\n"
    out = repo_root / "manifest.json"

    if args.check:
        if not out.exists():
            print("manifest.json is missing — run: python scripts/build_manifest.py", file=sys.stderr)
            return 1
        if out.read_text(encoding="utf-8") != text:
            print(
                "manifest.json is out of date — a plugin's files changed without it "
                "being rebuilt. Run: python scripts/build_manifest.py",
                file=sys.stderr,
            )
            return 1
        print(f"manifest.json is in sync ({manifest['_meta']['total_plugins']} plugins)")
        return 0

    out.write_text(text, encoding="utf-8")
    total = sum(len(p["files"]) for p in manifest["plugins"].values())
    print(f"Wrote manifest.json: {manifest['_meta']['total_plugins']} plugins, {total} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
