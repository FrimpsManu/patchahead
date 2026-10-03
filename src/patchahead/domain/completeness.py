"""What is left of the old API after a migration.

Tests that pass after a patch show that the code they exercise works. They do
not show that the migration is *finished*: a wrapper no test calls, a
``getattr(order, "total")``, an import of the old name, or a test that still
mocks the old method can all survive a green run. A completeness report lists
every place the old name still appears in the patched copy, sorted by how much
it needs a person's attention.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class ResidualKind(str, enum.Enum):
    """Where an old name survived, from most to least in need of attention."""

    #: Code that still uses the old name and was not rewritten.
    CODE = "code"
    #: The old name as a string handed to ``getattr``/``hasattr``/``setattr``:
    #: an access no static rewrite can see, which fails only at runtime.
    DYNAMIC = "dynamic"
    #: A test that still uses the old name.
    TEST = "test"
    #: The same name on an object the change document says is not affected.
    OTHER_OBJECT = "other_object"
    #: A string, comment, configuration file, or document mentioning the name.
    STRING = "string"
    COMMENT = "comment"
    CONFIG = "config"
    DOCS = "docs"

    @property
    def unfinished(self) -> bool:
        """Whether a residual of this kind means the migration is not done."""
        return self in (ResidualKind.CODE, ResidualKind.DYNAMIC, ResidualKind.TEST)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class Residual:
    """One place the old name still appears after patching."""

    path: str
    line: int
    kind: ResidualKind
    snippet: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "kind": self.kind.value,
            "snippet": self.snippet,
            "reason": self.reason,
        }


@dataclass
class CompletenessReport:
    """Every surviving use of one change's old name, in the patched copy."""

    old: str
    new: str
    residuals: list[Residual] = field(default_factory=list)

    @property
    def unfinished(self) -> list[Residual]:
        return [r for r in self.residuals if r.kind.unfinished]

    @property
    def complete(self) -> bool:
        """No code, dynamic access, or test still uses the old name."""
        return not self.unfinished

    def count(self, kind: ResidualKind) -> int:
        return sum(1 for residual in self.residuals if residual.kind is kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "old": self.old,
            "new": self.new,
            "complete": self.complete,
            "residuals": [r.to_dict() for r in self.residuals],
        }
