"""Case and suite results, including the status a known gap produces.

The benchmark has to be able to fail PatchAhead. That sounds obvious and is
easy to lose: a dataset written by the same person who wrote the implementation
drifts toward cases the implementation already handles, and then reports 100%
forever. The status model here is what keeps that from happening.

A case ends in one of four states:

``PASSED``
    It produced what the dataset says a correct tool produces.

``FAILED``
    It did not. The suite fails.

``KNOWN_GAP``
    It did not, *and the dataset says so in advance*, with a reason. The
    benchmark records an honest miss without reddening CI, so a limitation can
    be written down as a test case instead of being quietly left out.

``FIXED_GAP``
    A known gap that now passes. This **fails the suite**, on purpose: the gap
    is closed and the marker is stale, and stale markers are how a benchmark
    starts lying in the other direction.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class CaseStatus(str, enum.Enum):
    PASSED = "passed"
    FAILED = "failed"
    KNOWN_GAP = "known_gap"
    FIXED_GAP = "fixed_gap"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass
class CaseResult:
    """One evaluation case's verdict, with the detail needed to debug it."""

    case_id: str
    status: CaseStatus
    #: Why it failed, or what the gap is. Empty only for a clean pass.
    detail: str = ""
    #: The reason recorded in the dataset, for a gap case.
    gap_reason: str = ""
    metrics: dict[str, float] = field(default_factory=dict)

    @classmethod
    def judge(
        cls,
        case_id: str,
        problems: list[str],
        *,
        known_gap: str = "",
        fatal: list[str] | None = None,
        metrics: dict[str, float] | None = None,
    ) -> CaseResult:
        """Turn a list of problems into a status, honouring the gap marker.

        ``fatal`` problems are never excused by a gap marker. A known gap says
        "PatchAhead does not yet find this site" -- it may never be used to
        excuse *patching the wrong code*. Under-migrating is a limitation a user
        can work around; a wrong edit in unrelated code is the failure this
        project exists to prevent, and no marker in a dataset makes it
        acceptable.
        """
        fatal = list(fatal or [])
        problems = fatal + [p for p in problems if p not in fatal]
        detail = "; ".join(problems)
        if known_gap and fatal:
            status = CaseStatus.FAILED
            detail = (
                f"marked as a known gap ({known_gap}), but a known gap may not "
                f"patch the wrong code: " + "; ".join(fatal)
            )
        elif known_gap:
            status = CaseStatus.KNOWN_GAP if problems else CaseStatus.FIXED_GAP
            if status is CaseStatus.FIXED_GAP:
                detail = (
                    f"this case is marked as a known gap ({known_gap}) but now passes; "
                    f"remove the `known_gap` marker from the dataset"
                )
        else:
            status = CaseStatus.FAILED if problems else CaseStatus.PASSED
        return cls(
            case_id=case_id,
            status=status,
            detail=detail,
            gap_reason=known_gap,
            metrics=dict(metrics or {}),
        )

    @property
    def passed(self) -> bool:
        """Whether this case met expectations.

        A known gap is *not* a pass. It is a recorded miss that does not fail
        the build, which is a different thing, and conflating them is exactly
        how a benchmark inflates its own score.
        """
        return self.status is CaseStatus.PASSED

    @property
    def ok(self) -> bool:
        """Whether this case is allowed to leave the suite green."""
        return self.status in (CaseStatus.PASSED, CaseStatus.KNOWN_GAP)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "status": self.status.value,
            "passed": self.passed,
            "detail": self.detail,
            "gap_reason": self.gap_reason,
            "metrics": self.metrics,
        }


@dataclass
class SuiteResult:
    """Every case in one suite, plus the metrics computed across them."""

    name: str
    #: One line saying what this suite measures, rendered above its metrics.
    description: str = ""
    cases: list[CaseResult] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    #: Metric names whose meaning is not obvious from the name alone.
    metric_notes: dict[str, str] = field(default_factory=dict)
    duration_ms: int = 0

    def add(self, case: CaseResult) -> CaseResult:
        self.cases.append(case)
        return case

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def known_gaps(self) -> list[CaseResult]:
        return [c for c in self.cases if c.status is CaseStatus.KNOWN_GAP]

    @property
    def failures(self) -> list[CaseResult]:
        return [c for c in self.cases if not c.ok]

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "passed": self.passed,
            "total": self.total,
            "known_gaps": len(self.known_gaps),
            "ok": self.ok,
            "metrics": self.metrics,
            "metric_notes": self.metric_notes,
            "duration_ms": self.duration_ms,
            "cases": [c.to_dict() for c in self.cases],
        }


@dataclass
class BenchmarkResult:
    """The whole benchmark: every suite that ran, in the order it ran."""

    suites: list[SuiteResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.suites)

    @property
    def passed(self) -> int:
        return sum(s.passed for s in self.suites)

    @property
    def total(self) -> int:
        return sum(s.total for s in self.suites)

    @property
    def known_gaps(self) -> list[CaseResult]:
        return [case for suite in self.suites for case in suite.known_gaps]

    @property
    def duration_ms(self) -> int:
        return sum(s.duration_ms for s in self.suites)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "passed": self.passed,
            "total": self.total,
            "known_gaps": len(self.known_gaps),
            "duration_ms": self.duration_ms,
            "suites": {s.name: s.to_dict() for s in self.suites},
        }
