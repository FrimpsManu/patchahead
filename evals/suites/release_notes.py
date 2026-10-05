"""Suite: can PatchAhead read a whole release note the way vendors write them?

The ``classification`` suite asks whether one section is read correctly. This
one asks the question a user has: given the document a vendor actually
published, are *all* of its breaking changes found, and is each one read right?

Each document is scored change by change:

``found``
    An expected change matched by a reading with the same kind and every field
    the case asserts.

``missed``
    An expected change no reading matched -- including an unsupported change
    that was silently dropped instead of reported. Recoverable: the user is told
    less than they should be, but nothing is edited.

``misread``
    A reading of a *supported* kind that matches no expected change: the wrong
    names, the wrong kind, or a change the document does not describe. This is
    the reading that reaches a repository, so it fails the case even when the
    case is marked as a known gap.

Readings of ``unsupported`` or ``unknown`` that match nothing expected are not
scored: reporting a section PatchAhead cannot act on costs the user nothing.
"""

from __future__ import annotations

import time
from typing import Any

from evals.harness.dataset import ReleaseNoteCase, describe, load
from evals.harness.result import CaseResult, SuiteResult
from evals.suites.classification import _read_kind
from patchahead.ingest.base import ChangeDocument, parse_document

SUITE = "release_notes"

_ACTIONABLE = {
    "field_rename",
    "method_rename",
    "kwarg_rename",
    "pagination_page_to_cursor",
    "query_param_rename",
}


def _reading(change) -> dict[str, Any]:
    return {
        "kind": _read_kind(change),
        "symbol": change.target.symbol,
        "replacement": change.target.replacement,
        "owner": change.target.owner,
        "owner_explicit": change.target.owner_is_explicit,
        "pagination": change.pagination.to_dict() if change.pagination else {},
    }


def _matches(expected: dict[str, Any], reading: dict[str, Any]) -> bool:
    for key, value in expected.items():
        if key == "pagination":
            # Only the contract fields the case names are asserted.
            if any(reading["pagination"].get(name) != want for name, want in value.items()):
                return False
        elif reading[key] != value:
            return False
    return True


def _describe(change: dict[str, Any]) -> str:
    kind = change["kind"]
    if "symbol" not in change:
        return kind
    owner = f" on {change['owner']}" if change.get("owner") else ""
    return f"{kind} {change['symbol']}->{change.get('replacement', '?')}{owner}"


def run() -> SuiteResult:
    suite = SuiteResult(name=SUITE, description=describe(SUITE))
    start = time.perf_counter()

    counts = dict.fromkeys(
        ("expected", "found", "unsupported_expected", "unsupported_reported", "misread"), 0
    )
    counts_all = dict(counts)

    for case in load(SUITE, ReleaseNoteCase):
        changes = parse_document(ChangeDocument(text=case.text, path=case.id, suffix=case.suffix))
        readings = [_reading(change) for change in changes]
        unused = list(range(len(readings)))

        problems: list[str] = []
        tally = dict.fromkeys(counts, 0)
        for expected in case.expected_changes:
            supported = expected["kind"] in _ACTIONABLE
            tally["expected" if supported else "unsupported_expected"] += 1
            hit = next((i for i in unused if _matches(expected, readings[i])), None)
            if hit is None:
                problems.append(f"missed {_describe(expected)}")
                continue
            unused.remove(hit)
            tally["found" if supported else "unsupported_reported"] += 1

        fatal = [
            f"misread {_describe(readings[i])}"
            for i in unused
            if readings[i]["kind"] in _ACTIONABLE
        ]
        tally["misread"] = len(fatal)

        for key, value in tally.items():
            counts_all[key] += value
            if not case.known_gap:
                counts[key] += value

        suite.add(CaseResult.judge(case.id, problems, known_gap=case.known_gap, fatal=fatal))

    def ratio(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 3) if denominator else 0.0

    suite.metrics = {
        "documents": suite.total,
        "known_gap_cases": len(suite.known_gaps),
        "changes_expected": counts["expected"],
        "change_recall": ratio(counts["found"], counts["expected"]),
        "change_recall_including_gaps": ratio(counts_all["found"], counts_all["expected"]),
        "unsupported_reported_rate": ratio(
            counts["unsupported_reported"], counts["unsupported_expected"]
        ),
        "misread_changes": counts_all["misread"],
    }
    suite.metric_notes = {
        "change_recall": "share of supported changes found and read with the right "
        "kind and names, excluding known gaps",
        "change_recall_including_gaps": "the same with the known gaps counted -- the honest "
        "capability number, reported and never asserted as a floor",
        "unsupported_reported_rate": "share of changes PatchAhead cannot migrate that were "
        "reported rather than silently dropped",
        "misread_changes": "supported changes read with the wrong kind or names, gaps "
        "included -- the reading that reaches a repository; must stay 0",
    }
    suite.duration_ms = int((time.perf_counter() - start) * 1000)
    return suite
