"""Top-level results returned by the engine to the CLI, the web UI, and tests."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from patchahead.domain.impact import ImpactReport
from patchahead.domain.patch import PatchProposal
from patchahead.domain.plan import MigrationPlan
from patchahead.domain.validation import TestRun, ValidationResult


class Outcome(str, enum.Enum):
    """Why a migration run ended the way it did.

    Distinguishing these is the whole point: "we could not find anything to
    change", "we refused to try", and "we tried and the tests failed" are three
    very different messages to a user, and the prototype reported all of them as
    the same boolean.
    """

    #: Patch generated and every gate passed.
    MIGRATED = "migrated"
    #: Patch generated, at least one gate failed. Diff is still available.
    VALIDATION_FAILED = "validation_failed"
    #: Patch generated and no gate objected, but no test gate ran -- so nothing
    #: verified it. Produced by `--no-tests`, or a repository with no tests.
    PATCHED_UNVERIFIED = "patched_unverified"
    #: Analysis found nothing to change. Not an error.
    NO_IMPACT = "no_impact"
    #: The change kind has no handler in this version.
    UNSUPPORTED_CHANGE = "unsupported_change"
    #: A handler matched but declined to patch (e.g. unrecognized code shape).
    NOT_PLANNABLE = "not_plannable"
    #: Planning succeeded but patch generation failed.
    PATCH_FAILED = "patch_failed"
    #: ``--dry-run``: stopped after planning by request.
    DRY_RUN = "dry_run"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass
class AnalysisResult:
    """What ``patchahead analyze`` returns."""

    repo: str
    change_document: str
    reports: list[ImpactReport] = field(default_factory=list)
    #: Wall-clock timings per stage, in milliseconds.
    timings: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_impact(self) -> bool:
        return any(r.has_impact for r in self.reports)

    @property
    def total_findings(self) -> int:
        return sum(len(r.findings) for r in self.reports)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "change_document": self.change_document,
            "has_impact": self.has_impact,
            "total_findings": self.total_findings,
            "reports": [r.to_dict() for r in self.reports],
            "timings": self.timings,
            "warnings": self.warnings,
        }


@dataclass
class MigrationResult:
    """What ``patchahead migrate`` returns, for one breaking change."""

    outcome: Outcome
    impact: ImpactReport
    plan: MigrationPlan | None = None
    proposal: PatchProposal | None = None
    validation: ValidationResult | None = None
    #: Test state before patching, used by the migration-assertion gate.
    baseline_tests: TestRun | None = None
    #: Absolute path of the isolated workspace, when it was kept.
    workspace_path: str = ""
    #: Paths of artifacts written to the output directory.
    artifacts: dict[str, str] = field(default_factory=dict)
    #: Human-readable explanation of the outcome. Always populated.
    message: str = ""
    timings: dict[str, int] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        """A migration succeeded only if the tests verified it. No other path."""
        return self.outcome is Outcome.MIGRATED and bool(
            self.validation and self.validation.verified
        )

    @property
    def diff(self) -> str:
        return self.proposal.diff if self.proposal else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "succeeded": self.succeeded,
            "message": self.message,
            "impact": self.impact.to_dict(),
            "plan": self.plan.to_dict() if self.plan else None,
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "validation": self.validation.to_dict() if self.validation else None,
            "baseline_tests": self.baseline_tests.to_dict() if self.baseline_tests else None,
            "workspace_path": self.workspace_path,
            "artifacts": self.artifacts,
            "timings": self.timings,
        }


@dataclass
class MigrationRun:
    """All migration results for one change document against one repository."""

    repo: str
    change_document: str
    results: list[MigrationResult] = field(default_factory=list)
    #: The verdict on the combined result of every change in the document. All
    #: changes are patched into one workspace and validated together, because
    #: changes in one release note are frequently interdependent.
    validation: ValidationResult | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def actionable_results(self) -> list[MigrationResult]:
        """Results for changes that had work to do.

        A change with no downstream impact, or one this version cannot migrate,
        is not a failure -- but it is also not a migration, so it does not count
        toward (or against) the run's success.
        """
        return [
            result
            for result in self.results
            if result.outcome not in (Outcome.NO_IMPACT, Outcome.UNSUPPORTED_CHANGE)
        ]

    @property
    def succeeded(self) -> bool:
        """True when there was work to do and all of it passed validation."""
        actionable = self.actionable_results
        return bool(actionable) and all(result.succeeded for result in actionable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "change_document": self.change_document,
            "succeeded": self.succeeded,
            "validation": self.validation.to_dict() if self.validation else None,
            "results": [r.to_dict() for r in self.results],
            "warnings": self.warnings,
        }
