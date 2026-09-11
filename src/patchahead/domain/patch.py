"""Patch proposals: the new file contents a plan produces, plus a diff."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from patchahead.domain.plan import MigrationPlan


@dataclass
class FileEdit:
    """The before and after contents of one file."""

    path: str
    old_source: str
    new_source: str
    #: Number of :class:`~patchahead.domain.plan.TextEdit` s applied.
    edit_count: int = 0

    @property
    def changed(self) -> bool:
        return self.old_source != self.new_source

    def to_dict(self) -> dict[str, Any]:
        # Source is deliberately excluded: run logs are for reading, and the
        # diff already carries the content a reviewer needs.
        return {"path": self.path, "edit_count": self.edit_count, "changed": self.changed}


@dataclass
class PatchProposal:
    """A proposed, not-yet-validated set of file edits.

    "Proposal" is the operative word: a :class:`PatchProposal` has passed no
    gates. Only a :class:`~patchahead.domain.validation.ValidationResult` can
    say whether it is any good.
    """

    plan: MigrationPlan
    files: list[FileEdit] = field(default_factory=list)
    #: Unified diff across every changed file.
    diff: str = ""
    #: "deterministic" or "llm".
    engine: str = "deterministic"
    explanation: str = ""
    #: Set when generation failed; ``files`` is then empty.
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and any(f.changed for f in self.files)

    @property
    def changed_files(self) -> list[str]:
        return [f.path for f in self.files if f.changed]

    @property
    def edit_count(self) -> int:
        return sum(f.edit_count for f in self.files)

    @property
    def diff_line_count(self) -> int:
        """Added + removed lines, used by the diff-size validation gate."""
        return sum(
            1
            for line in self.diff.splitlines()
            if (line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "explanation": self.explanation,
            "error": self.error,
            "files": [f.to_dict() for f in self.files],
            "changed_files": self.changed_files,
            "edit_count": self.edit_count,
            "diff_line_count": self.diff_line_count,
            "diff": self.diff,
        }
