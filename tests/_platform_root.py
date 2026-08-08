"""Finding the OpenAVC platform checkout these tests bind against.

A few tests here need the real platform (PluginAPI, the state store, the test
harness) rather than a stand-in. Each of them used to work out where it lives
on its own, with the same hardcoded hop to the sibling ``openavc/`` folder.
That is wrong in the one case it matters most: when the platform work sits in
a task worktree, the sibling holds the *other* checkout, ``import openavc``
fails, and the test skips. A skip reads as green.

So the lookup lives here, once, and takes the same three answers openavc-drivers
takes -- an explicit ``OPENAVC_PLATFORM_ROOT``, then the worktree that shares
this one's suffix (``openavc-plugins-wt-foo`` -> ``openavc-wt-foo``), then the
plain sibling. Most explicit first.
"""

import os
import sys
from pathlib import Path

PLATFORM_ROOT_ENV = "OPENAVC_PLATFORM_ROOT"

REPO_ROOT = Path(__file__).resolve().parent.parent


def candidate_roots():
    """Where the openavc checkout might be, most explicit first."""
    roots = []
    configured = os.environ.get(PLATFORM_ROOT_ENV, "").strip()
    if configured:
        roots.append(Path(configured))
    workspace = REPO_ROOT.parent
    roots.append(workspace / REPO_ROOT.name.replace("openavc-plugins", "openavc", 1))
    roots.append(workspace / "openavc")
    return roots


def platform_root():
    """The first candidate that actually holds a platform, or None."""
    for root in candidate_roots():
        if (root / "openavc" / "core" / "plugin_api.py").exists():
            return root
    return None


def add_platform_to_path():
    """Put a located platform checkout on ``sys.path``. Returns it, or None.

    Callers import the platform right after and skip the module when that
    raises, so there is nothing to do here when no checkout turns up.
    """
    root = platform_root()
    if root is not None and str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root
