"""Migration planning: deciding *how* to fix what analysis found.

Finding the problem and deciding how to fix it are separate stages with separate
outputs. A :class:`MigrationPlan` is produced, serialized, and inspectable
*before* any source file is touched -- ``patchahead migrate --dry-run`` stops
exactly here.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from patchahead.domain.change import BreakingChange, Confidence
from patchahead.domain.impact import CodeReference


class Risk(str, enum.Enum):
    """How risky applying this plan is, independent of the change's severity.

    Severity says "how bad if we do nothing". Risk says "how bad if we do this".
    A HIGH-severity change can have a LOW-risk migration (a scoped rename), and
    that distinction is what a reviewer actually needs.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class TextEdit:
    """A replacement of one source range with new text.

    The unit of patching. Editing ranges rather than regenerating files is what
    keeps diffs minimal and leaves comments, blank lines, and formatting outside
    the edited span exactly as the author wrote them.

    Coordinates follow ``ast``: ``line`` is 1-indexed, columns are 0-indexed
    into the line, and ``end_col`` is exclusive.
    """

    line: int
    col: int
    end_line: int
    end_col: int
    new_text: str
    #: What this edit accomplishes, shown in the plan.
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "col": self.col,
            "end_line": self.end_line,
            "end_col": self.end_col,
            "new_text": self.new_text,
            "description": self.description,
        }


@dataclass
class Transformation:
    """One concrete edit the plan intends to make, with its provenance.

    Carries both the human-readable ``old``/``new`` (for the plan a person
    reads) and the machine-applicable ``edit`` (for the patcher).
    """

    reference: CodeReference
    old: str
    new: str
    edit: TextEdit
    #: The enclosing function/method this edit lands in.
    symbol: str = ""
    confidence: Confidence = Confidence.MEDIUM

    @property
    def path(self) -> str:
        return self.reference.path

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference.to_dict(),
            "old": self.old,
            "new": self.new,
            "symbol": self.symbol,
            "confidence": self.confidence.value,
            "edit": self.edit.to_dict(),
        }


@dataclass
class MigrationPlan:
    """What will be changed, where, why, and what should verify it.

    Serializable and inspectable by design: ``patchahead migrate --dry-run
    --json`` emits exactly this, and a reviewer can read it without trusting
    that the patcher did what the plan says.
    """

    change: BreakingChange
    #: The handler that produced this plan, e.g. "field_rename".
    handler: str
    transformations: list[Transformation] = field(default_factory=list)
    #: Tests that should exercise the migrated behavior.
    expected_tests: list[str] = field(default_factory=list)
    risk: Risk = Risk.MEDIUM
    #: One-paragraph explanation of the strategy, shown in reports.
    rationale: str = ""
    #: Sites analysis found but the plan deliberately leaves alone.
    skipped: list[str] = field(default_factory=list)
    #: Set when the handler could not produce a plan at all.
    blocked_reason: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.transformations

    @property
    def target_files(self) -> list[str]:
        seen: list[str] = []
        for transformation in self.transformations:
            if transformation.path not in seen:
                seen.append(transformation.path)
        return seen

    def edits_for(self, path: str) -> list[TextEdit]:
        return [t.edit for t in self.transformations if t.path == path]

    def render(self) -> str:
        """The compact text rendering used by the CLI."""
        lines = [
            "MigrationPlan",
            f"- change_kind: {self.change.kind.value}",
            f"- handler: {self.handler}",
            f"- risk: {self.risk.value.upper()}",
        ]
        for path in self.target_files:
            lines.append(f"- target_file: {path}")
            for transformation in self.transformations:
                if transformation.path != path:
                    continue
                where = f"{transformation.symbol}:{transformation.reference.line}"
                lines.append(f"    - {where}")
                lines.append(f"        old: {transformation.old}")
                lines.append(f"        new: {transformation.new}")
        if self.expected_tests:
            lines.append("- expected_tests:")
            lines.extend(f"    {test}" for test in self.expected_tests)
        if self.skipped:
            lines.append("- skipped (not patched):")
            lines.extend(f"    {item}" for item in self.skipped)
        if self.blocked_reason:
            lines.append(f"- blocked: {self.blocked_reason}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "change": self.change.to_dict(),
            "handler": self.handler,
            "risk": self.risk.value,
            "rationale": self.rationale,
            "transformations": [t.to_dict() for t in self.transformations],
            "target_files": self.target_files,
            "expected_tests": self.expected_tests,
            "skipped": self.skipped,
            "blocked_reason": self.blocked_reason,
        }
