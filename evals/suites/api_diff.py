"""Suite: can PatchAhead read breaking changes out of two versions of a library?

The same scoring as ``release_notes`` -- found, missed, misread -- applied to
``patchahead.apidiff``: each case writes a small library twice, reads both
without importing them, and compares. A rename read where the library did not
rename anything is a misread, and fails the case even when it is a known gap.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from evals.harness.dataset import ApiDiffCase, describe, load
from evals.harness.result import CaseResult, SuiteResult
from evals.suites.release_notes import _ACTIONABLE, _describe, _matches, _reading
from patchahead import apidiff

SUITE = "api_diff"


def _write(root: Path, files: dict[str, str]) -> Path:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


def run() -> SuiteResult:
    suite = SuiteResult(name=SUITE, description=describe(SUITE))
    start = time.perf_counter()
    counts = dict.fromkeys(("expected", "found", "misread"), 0)

    for case in load(SUITE, ApiDiffCase):
        with tempfile.TemporaryDirectory(prefix="patchahead-eval-") as scratch:
            old = apidiff.read(_write(Path(scratch) / "old", case.old_files))
            new = apidiff.read(_write(Path(scratch) / "new", case.new_files))
        readings = [_reading(change) for change in apidiff.compare(old, new).changes]
        unused = list(range(len(readings)))

        problems: list[str] = []
        for expected in case.expected_changes:
            if expected["kind"] in _ACTIONABLE:
                counts["expected"] += 1
            hit = next((i for i in unused if _matches(expected, readings[i])), None)
            if hit is None:
                problems.append(f"missed {_describe(expected)}")
                continue
            unused.remove(hit)
            if expected["kind"] in _ACTIONABLE:
                counts["found"] += 1
        # Unlike a release note, a library diff has a definite answer for every
        # member, so an unexpected *report* is a problem too -- just not a
        # dangerous one.
        fatal = [
            f"misread {_describe(readings[i])}"
            for i in unused
            if readings[i]["kind"] in _ACTIONABLE
        ]
        problems += [
            f"unexpected {_describe(readings[i])}"
            for i in unused
            if readings[i]["kind"] not in _ACTIONABLE
        ]
        counts["misread"] += len(fatal)
        suite.add(CaseResult.judge(case.id, problems, known_gap=case.known_gap, fatal=fatal))

    suite.metrics = {
        "libraries": suite.total,
        "renames_expected": counts["expected"],
        "rename_recall": round(counts["found"] / counts["expected"], 3)
        if counts["expected"]
        else 0.0,
        "misread_changes": counts["misread"],
    }
    suite.metric_notes = {
        "rename_recall": "share of expected renames read with the right kind and names",
        "misread_changes": "renames read where the library made none, or with the wrong "
        "names -- the reading that reaches a repository; must stay 0",
    }
    suite.duration_ms = int((time.perf_counter() - start) * 1000)
    return suite
