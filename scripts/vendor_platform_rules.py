#!/usr/bin/env python3
"""Vendor the OpenAVC platform's plugin-declaration modules into scripts/_vendor/.

The platform repo (github.com/open-avc/openavc) owns the rules for what a
plugin declares: what makes PLUGIN_INFO, CONFIG_SCHEMA, CONFIG_SECRETS,
MACRO_ACTIONS, SCRIPT_API and EXTENSIONS valid, how they are read from a
plugin's source without importing it, and which file in a plugin's folder
holds its class. validate.py runs the same rules from generated copies under
scripts/_vendor/, so a plugin that passes this repository's CI is one the
platform will start: the two cannot disagree.

Two modes:

  python scripts/vendor_platform_rules.py [--openavc PATH]
      Regenerate scripts/_vendor/ from a local platform checkout
      (default: the sibling ../openavc directory).

  python scripts/vendor_platform_rules.py --check
      Fetch the platform's current files from GitHub, apply the same
      transform, and fail if any committed copy differs. CI runs this on
      every push and pull request, so a rule change on the platform side
      fails this repository's CI until the copies are regenerated.

The transform is deterministic: line endings are normalized to LF, the
platform's absolute imports are rewritten to package-relative ones, and a
generated-file banner is prepended.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = REPO_ROOT / "scripts" / "_vendor"
UPSTREAM_BRANCH = "main"
RAW_BASE = "https://raw.githubusercontent.com/open-avc/openavc/"
API_BASE = "https://api.github.com/repos/open-avc/openavc"

# (platform path, target path relative to this repository's root, import
# rewrites applied exactly once each).
FILES: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "openavc/core/plugin_handler_rules.py",
        "scripts/_vendor/plugin_handler_rules.py",
        (),
    ),
    (
        "openavc/core/plugin_declarations.py",
        "scripts/_vendor/plugin_declarations.py",
        (
            (
                "from openavc.core.plugin_handler_rules import (",
                "from .plugin_handler_rules import (",
            ),
        ),
    ),
    (
        "openavc/core/plugin_source.py",
        "scripts/_vendor/plugin_source.py",
        (
            (
                "from openavc.core.plugin_declarations import",
                "from .plugin_declarations import",
            ),
        ),
    ),
)

INIT_CONTENT = '''\
"""Generated copies of the OpenAVC platform's plugin-declaration modules.

Do not edit these files here. The platform repo
(github.com/open-avc/openavc) is the source of truth; regenerate with
``python scripts/vendor_platform_rules.py``.
"""
'''


def _banner(source_path: str) -> str:
    return (
        "# GENERATED FILE - DO NOT EDIT.\n"
        "# Vendored from the OpenAVC platform repo "
        "(github.com/open-avc/openavc),\n"
        f"# source: {source_path}\n"
        "# The platform copy is the source of truth for the plugin rules; CI\n"
        "# fails when this copy drifts from it. To update, run:\n"
        "#     python scripts/vendor_platform_rules.py\n"
        "\n"
    )


def transform(source_path: str, raw: bytes) -> str:
    """The vendored file content for one platform source file."""
    text = raw.decode("utf-8").replace("\r\n", "\n")
    rewrites = next(rw for p, _, rw in FILES if p == source_path)
    for old, new in rewrites:
        count = text.count(old)
        if count != 1:
            raise SystemExit(
                f"ERROR: expected exactly one occurrence of {old!r} in "
                f"{source_path}, found {count}: the platform module's "
                f"imports changed; update FILES in {__file__}"
            )
        text = text.replace(old, new)
    return _banner(source_path) + text


def _read_local(openavc: Path, source_path: str) -> bytes:
    path = openavc / source_path
    if not path.is_file():
        raise SystemExit(
            f"ERROR: {path} not found: pass --openavc pointing at an "
            f"OpenAVC platform checkout"
        )
    return path.read_bytes()


@lru_cache(maxsize=1)
def _upstream_ref() -> str:
    """The platform's default branch as a commit SHA, resolved once.

    raw.githubusercontent.com caches a branch URL for minutes, so a run right
    after the platform half of a paired change lands would compare against
    the old bytes. A commit SHA's URL cannot go stale. Offline or rate
    limited, fall back to the branch name.
    """
    url = f"{API_BASE}/git/ref/heads/{UPSTREAM_BRANCH}"
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json"}
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return json.load(resp)["object"]["sha"]
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError):
        return UPSTREAM_BRANCH


def _fetch_upstream(source_path: str) -> bytes:
    url = f"{RAW_BASE}{_upstream_ref()}/{source_path}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()
    except urllib.error.URLError as exc:
        raise SystemExit(f"ERROR: could not fetch {url}: {exc}")


def expected_files(read) -> dict[str, str]:
    """Each vendored file's content, by its path from the repository root,
    from ``read(platform path) -> bytes``."""
    expected = {"scripts/_vendor/__init__.py": INIT_CONTENT}
    for source_path, target_rel, _ in FILES:
        expected[target_rel] = transform(source_path, read(source_path))
    return expected


def _committed(target_rel: str) -> str | None:
    target = REPO_ROOT / target_rel
    if not target.is_file():
        return None
    return target.read_bytes().decode("utf-8").replace("\r\n", "\n")


def sync(openavc: Path) -> int:
    expected = expected_files(lambda p: _read_local(openavc, p))
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    changed = 0
    for target_rel, content in expected.items():
        if _committed(target_rel) == content:
            print(f"  unchanged  {target_rel}")
            continue
        with open(REPO_ROOT / target_rel, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        print(f"  wrote      {target_rel}")
        changed += 1
    print(f"{changed} file(s) updated from {openavc}")
    return 0


def drift(expected: dict[str, str]) -> list[str]:
    """What differs between the committed copies and ``expected``."""
    problems: list[str] = []
    for target_rel, content in expected.items():
        committed = _committed(target_rel)
        if committed is None:
            problems.append(f"{target_rel}: missing")
        elif committed != content:
            diff = "".join(difflib.unified_diff(
                content.splitlines(keepends=True),
                committed.splitlines(keepends=True),
                fromfile=f"platform:{target_rel}",
                tofile=f"vendored:{target_rel}",
            ))
            problems.append(f"{target_rel}: differs from the platform copy\n{diff}")
    return problems


def check() -> int:
    expected = expected_files(_fetch_upstream)
    problems = drift(expected)
    if problems:
        print(
            "FAILED: the vendored plugin rules are out of sync with the OpenAVC "
            "platform repo.\nThe platform (github.com/open-avc/openavc) is the "
            "source of truth. Regenerate the\nvendored copies and commit the "
            "result:\n    python scripts/vendor_platform_rules.py\n",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"vendored plugin rules are in sync ({len(expected)} files).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else ""
    )
    parser.add_argument(
        "--openavc",
        type=Path,
        default=REPO_ROOT.parent / "openavc",
        help="Path to a local OpenAVC platform checkout (sync mode; "
        "default: the sibling ../openavc)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed copies match the platform's current "
        "files on GitHub (used in CI); writes nothing",
    )
    args = parser.parse_args(argv)
    if args.check:
        return check()
    return sync(args.openavc.resolve())


if __name__ == "__main__":
    sys.exit(main())
