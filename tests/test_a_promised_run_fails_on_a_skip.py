"""A run that set OPENAVC_REQUIRE_PLATFORM fails when anything skipped.

CI sets the variable on the job that installs the platform and each plugin's
libraries. Without this rule a test that skipped there, for a missing import or
a platform it could not find, would read as a pass. Each case runs pytest in a
fresh process on a folder holding this suite's conftest.py and one sample
module, so the rule is judged the way CI meets it.
"""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

CONFTEST = Path(__file__).resolve().parent / "conftest.py"

PASSES = """
def test_passes():
    assert True
"""

SKIPS_THE_MODULE = """
import pytest

pytest.skip("the platform is not here", allow_module_level=True)

def test_never_runs():
    assert True
"""

SKIPS_BY_MARK = """
import pytest

@pytest.mark.skipif(True, reason="no network address")
def test_marked():
    assert True
"""

SKIPS_INSIDE_A_TEST = """
import pytest

def test_inside():
    pytest.importorskip("a_library_nobody_installed")
"""

EXPECTS_A_FAILURE = """
import pytest

@pytest.mark.xfail(reason="recorded as wrong", strict=True)
def test_expected_failure():
    assert False
"""

FAILS = """
def test_fails():
    assert False
"""


def run(tmp_path, modules, required):
    """Run pytest on the sample modules and the real conftest; (exit code, output)."""
    shutil.copy(CONFTEST, tmp_path / "conftest.py")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    for name, source in modules.items():
        (tmp_path / f"test_{name}.py").write_text(textwrap.dedent(source), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "OPENAVC_REQUIRE_PLATFORM"}
    if required:
        env["OPENAVC_REQUIRE_PLATFORM"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rs", str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


def listed_skips(output):
    """The skips the rule listed, one per line, as it printed them."""
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if "skipped in a run that promised the platform" in line)
    listed = []
    for line in lines[start + 2:]:
        if not line.startswith("  "):
            break
        listed.append(line.strip())
    return listed


# Each way a test can skip: the module while pytest collects it, a mark, and a
# call inside the test. Each with the reason its skip gives.
SKIPS = {
    "module": (SKIPS_THE_MODULE, "the platform is not here"),
    "mark": (SKIPS_BY_MARK, "no network address"),
    "inside": (SKIPS_INSIDE_A_TEST, "a_library_nobody_installed"),
}


@pytest.mark.parametrize("name", list(SKIPS))
def test_a_skip_fails_a_promised_run_and_names_its_reason(tmp_path, name):
    source, reason = SKIPS[name]
    code, output = run(tmp_path, {"passes": PASSES, name: source}, required=True)
    assert code == pytest.ExitCode.TESTS_FAILED, output
    listed = listed_skips(output)
    assert len(listed) == 1 and reason in listed[0], output
    # Each line reads as pytest's own skip summary (-rs) words that skip.
    assert f"SKIPPED [1] {listed[0]}" in output, output


@pytest.mark.parametrize("name", list(SKIPS))
def test_the_same_skip_passes_a_run_that_promised_nothing(tmp_path, name):
    source, _ = SKIPS[name]
    code, output = run(tmp_path, {"passes": PASSES, name: source}, required=False)
    assert code == pytest.ExitCode.OK, output
    assert "promised the platform" not in output, output


def test_a_module_skip_still_lets_every_other_test_run(tmp_path):
    code, output = run(
        tmp_path, {"module": SKIPS_THE_MODULE, "passes": PASSES, "fails": FAILS}, required=True,
    )
    assert code == pytest.ExitCode.TESTS_FAILED, output
    assert "1 failed, 1 passed, 1 skipped" in output, output


def test_an_expected_failure_is_not_a_skip(tmp_path):
    code, output = run(tmp_path, {"passes": PASSES, "xfail": EXPECTS_A_FAILURE}, required=True)
    assert code == pytest.ExitCode.OK, output
    assert "promised the platform" not in output, output
