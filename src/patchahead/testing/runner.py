"""Running a test command inside a workspace and structuring the result.

Parsing test output is inherently runner-specific. This module understands
pytest's summary lines well and degrades to "the command's exit code" for
anything else, which is the part that actually decides a gate.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time

from patchahead.domain.validation import TestRun
from patchahead.workspace import Workspace

log = logging.getLogger(__name__)

_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)
_SHORT_TALLY = re.compile(r"(\d+ (?:passed|failed|error|skipped)[^\n=]*)")
_ASSERTION = re.compile(r"^E\s+(\w*(?:Error|Exception|AssertionError).*)$", re.MULTILINE)
_NO_TESTS = re.compile(r"no tests ran|collected 0 items", re.IGNORECASE)
_MISSING_RUNNER = re.compile(
    r"No module named (\S+)|command not found|is not recognized as an internal"
)


def _summarize(output: str, returncode: int) -> str:
    """One line describing what happened, for reports and logs."""
    missing = _MISSING_RUNNER.search(output)
    if missing and returncode != 0:
        return (
            f"the test command could not start ({missing.group(0).strip()}). "
            f"Check `test_command` in the repository's PatchAhead configuration, "
            f"and that the test runner is installed in the environment PatchAhead "
            f"is running in."
        )
    if _NO_TESTS.search(output):
        return "no tests ran"
    tally = _SHORT_TALLY.search(output)
    if returncode == 0:
        return tally.group(1).strip() if tally else "tests passed"
    assertion = _ASSERTION.search(output)
    if assertion:
        detail = assertion.group(1).strip()
        return f"{tally.group(1).strip()} ({detail})" if tally else detail
    if tally:
        return tally.group(1).strip()
    return f"test command exited {returncode}"


def run_tests(
    workspace: Workspace,
    command: str,
    timeout: int = 300,
) -> TestRun:
    """Run ``command`` in ``workspace`` and return a structured result.

    A command that cannot be executed at all -- not found, or killed by the
    timeout -- is returned with ``errored=True`` rather than raising, so a gate
    can report "could not verify" distinctly from "verified and failed". These
    are different facts and the prototype's boolean could not tell them apart.
    """
    start = time.perf_counter()
    try:
        completed = workspace.run(command, timeout=timeout)
    except subprocess.TimeoutExpired:
        duration = int((time.perf_counter() - start) * 1000)
        log.warning("test command timed out after %ds: %s", timeout, command)
        return TestRun(
            command=command,
            returncode=-1,
            summary=f"test command timed out after {timeout}s",
            duration_ms=duration,
            errored=True,
        )
    except OSError as exc:
        duration = int((time.perf_counter() - start) * 1000)
        log.warning("test command could not be run: %s", exc)
        return TestRun(
            command=command,
            returncode=-1,
            stderr=str(exc),
            summary=f"could not run the test command: {exc}",
            duration_ms=duration,
            errored=True,
        )

    duration = int((time.perf_counter() - start) * 1000)
    output = completed.stdout + completed.stderr
    failing = sorted(set(_FAILED_LINE.findall(output)))

    # pytest exit code 5 is "no tests collected", which is not a test failure.
    # Reporting it as one would make an empty repository look broken. A runner
    # that could not start at all is also "could not verify", not "verified and
    # failed" -- the gates treat those differently on purpose.
    errored = (completed.returncode == 5 and bool(_NO_TESTS.search(output))) or bool(
        completed.returncode != 0 and _MISSING_RUNNER.search(output)
    )

    run = TestRun(
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        failing_tests=failing,
        summary=_summarize(output, completed.returncode),
        duration_ms=duration,
        errored=errored,
    )
    log.debug("test run finished in %dms: rc=%d, %s", duration, completed.returncode, run.summary)
    return run
