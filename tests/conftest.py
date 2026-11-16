"""A run that promised everything these tests need, where a skip is a failure.

Most of this suite runs on the real OpenAVC platform, and parts of it on a
plugin's own libraries (Pillow, the Stream Deck library) or on node with jsdom.
Each test that needs one of those skips without it, so a contributor with only
Python installed can still run the rest. A skip reads as green, though: a CI
job that meant to install the platform and did not would pass while checking
nothing.

Set ``OPENAVC_REQUIRE_PLATFORM=1`` to promise the platform and everything the
tests run on (the same variable openavc-drivers' platform job sets). The run
then ends failed when anything skipped, and lists each skip with its reason.
Every test still runs first: most of these skips happen while pytest collects a
module, and failing a module there would stop the run before any test ran. An
expected failure (``xfail``) is not a skip and is left alone.

This is one rule for every reason a test can skip, rather than a check at each
place that skips, so a skip added later is covered too.
"""

import os

import pytest

REQUIRE_PLATFORM_ENV = "OPENAVC_REQUIRE_PLATFORM"

_TRUE = {"1", "true", "yes", "on"}

_skipped = []


def platform_required():
    """True when the run promised the platform, so a skip must fail."""
    return os.environ.get(REQUIRE_PLATFORM_ENV, "").strip().lower() in _TRUE


def _relative(path, start):
    try:
        return os.path.relpath(path, start)
    except ValueError:  # another drive, on Windows
        return path


def _reason(report, start):
    """Where a skip happened and why, worded as pytest's own skip summary."""
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        path, lineno, message = longrepr
        message = message.removeprefix("Skipped: ")
        if lineno is None:
            return f"{_relative(path, start)}: {message}"
        return f"{_relative(path, start)}:{lineno}: {message}"
    return f"{report.nodeid}: {longrepr}"


def pytest_collectreport(report):
    if report.skipped:
        _skipped.append(report)


def pytest_runtest_logreport(report):
    if report.skipped and not hasattr(report, "wasxfail"):
        _skipped.append(report)


def pytest_terminal_summary(terminalreporter):
    if not (platform_required() and _skipped):
        return
    terminalreporter.section("skipped in a run that promised the platform", sep="=", red=True)
    terminalreporter.line(
        f"{REQUIRE_PLATFORM_ENV}=1 promises the OpenAVC platform and everything "
        f"these tests run on, so each skip below fails the run:"
    )
    for report in _skipped:
        terminalreporter.line(f"  {_reason(report, terminalreporter.startpath)}")


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session):
    if platform_required() and _skipped and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
