"""Suite: can PatchAhead read breaking changes out of two versions of an OpenAPI spec?

The scoring of ``api_diff`` -- found, missed, misread -- applied to
``patchahead.openapi``: each case is a spec before and after, compared. A
rename read where the spec did not rename anything is a misread, and fails the
case even when it is a known gap.
"""

from __future__ import annotations

import time

from evals.harness.dataset import OpenApiDiffCase, describe, load
from evals.harness.result import CaseResult, SuiteResult
from evals.suites.release_notes import _ACTIONABLE, _describe, _matches, _reading
from patchahead import openapi

SUITE = "openapi_diff"


def run() -> SuiteResult:
    suite = SuiteResult(name=SUITE, description=describe(SUITE))
    start = time.perf_counter()
    counts = dict.fromkeys(("expected", "found", "misread"), 0)

    for case in load(SUITE, OpenApiDiffCase):
        old, new = openapi.read(case.old_spec), openapi.read(case.new_spec)
        readings = [_reading(change) for change in openapi.compare(old, new).changes]
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
        # A spec diff has a definite answer for every property and operation,
        # so an unexpected *report* is a problem too -- just not a dangerous one.
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
        "specs": suite.total,
        "renames_expected": counts["expected"],
        "rename_recall": round(counts["found"] / counts["expected"], 3)
        if counts["expected"]
        else 0.0,
        "misread_changes": counts["misread"],
    }
    suite.metric_notes = {
        "rename_recall": "share of expected renames read with the right kind and names",
        "misread_changes": "renames read where the spec made none, or with the wrong "
        "names -- the reading that reaches a repository; must stay 0",
    }
    suite.duration_ms = int((time.perf_counter() - start) * 1000)
    return suite
