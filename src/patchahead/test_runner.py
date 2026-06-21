"""Run the downstream test suite and capture structured results.

The upstream version under test is selected via ``ORDERS_API_VERSION`` so
the same suite can be run against v1 and v2.
"""

import os
import re
import subprocess
import sys
import time

from patchahead import paths
from patchahead.schemas import TestRun


def run_tests(api_version: str = "v2", test_path=None) -> TestRun:
    env = dict(os.environ)
    env["ORDERS_API_VERSION"] = api_version
    target = str(test_path) if test_path else str(paths.TEST_PATH)
    cmd = [sys.executable, "-m", "pytest", target, "-q", "--no-header",
           "-p", "no:cacheprovider"]

    start = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(paths.ROOT))
    duration_ms = int((time.time() - start) * 1000)

    passed = proc.returncode == 0
    out = proc.stdout + proc.stderr

    failing = re.findall(r"FAILED\s+(\S+)", out)

    # Pull a one-line human summary of the failure if present.
    summary = ""
    if not passed:
        m = re.search(r"^E\s+(\w*Error.*)$", out, re.MULTILINE)
        if m:
            summary = m.group(1).strip()
        else:
            m = re.search(r"(\d+ failed.*)", out)
            summary = m.group(1).strip() if m else "tests failed"
    else:
        m = re.search(r"(\d+ passed.*)", out)
        summary = m.group(1).strip() if m else "tests passed"

    return TestRun(
        passed=passed,
        command=" ".join(cmd) + f"   (ORDERS_API_VERSION={api_version})",
        stdout=proc.stdout,
        stderr=proc.stderr,
        failing_tests=failing,
        summary=summary,
        duration_ms=duration_ms,
    )
