"""The upstream side of the domain model: what broke, and how sure we are.

A :class:`BreakingChange` is the output of change ingestion and the input to
everything else. It carries two things that the prototype conflated:

* what the change *is* (:class:`ChangeKind` plus a :class:`SymbolTarget`), and
* how confident we are that we read the document correctly
  (:class:`Confidence` plus the :class:`Evidence` we based it on).

Uncertain extraction is represented as uncertain. A document PatchAhead cannot
classify yields ``ChangeKind.UNKNOWN``, not a plausible-looking guess.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class ChangeKind(str, enum.Enum):
    """The migration families PatchAhead knows about.

    Every member except :attr:`UNKNOWN` and :attr:`UNSUPPORTED` has a handler in
    :mod:`patchahead.handlers` with analysis, planning, patching, validation,
    tests, and documentation. Adding a member without a handler is a bug --
    :func:`patchahead.handlers.selftest_registry` asserts this.
    """

    FIELD_RENAME = "field_rename"
    METHOD_RENAME = "method_rename"
    KWARG_RENAME = "kwarg_rename"
    PAGINATION_PAGE_TO_CURSOR = "pagination_page_to_cursor"

    #: Recognized as a breaking change, but no v1 handler can migrate it.
    UNSUPPORTED = "unsupported"
    #: The document could not be classified at all.
    UNKNOWN = "unknown"

    @property
    def is_actionable(self) -> bool:
        return self not in (ChangeKind.UNSUPPORTED, ChangeKind.UNKNOWN)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Confidence(str, enum.Enum):
    """Graded confidence, ordered.

    Deliberately coarse. A float implies a calibration we do not have; three
    buckets can be explained to a reviewer and tested against fixtures.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.rank >= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.rank > other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.rank <= other.rank

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.rank < other.rank

    @classmethod
    def parse(cls, value: object, default: Confidence = None) -> Confidence:
        """Coerce user/LLM-supplied text to a member, falling back to ``default``."""
        if isinstance(value, Confidence):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        if default is None:
            raise ValueError(f"not a confidence level: {value!r}")
        return default


class Severity(str, enum.Enum):
    """How bad the break is if left unmigrated. Reported, never acted upon."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @classmethod
    def parse(cls, value: object, default: Severity = None) -> Severity:
        if isinstance(value, Severity):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        if default is None:
            raise ValueError(f"not a severity: {value!r}")
        return default


@dataclass(frozen=True)
class Evidence:
    """A quotation from the change document that justifies a conclusion.

    Evidence always points back at the source text. If PatchAhead asserts
    something about a change, a reviewer can find the line it came from.
    """

    quote: str
    #: 1-indexed line in the source document, when known.
    line: int | None = None
    #: Free-text note on why this quote mattered, e.g. "matched 'renamed to'".
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"quote": self.quote, "line": self.line, "note": self.note}


@dataclass
class SymbolTarget:
    """The concrete syntactic thing a migration renames or rewrites.

    ``symbol`` and ``replacement`` are the old and new names. ``owner`` scopes
    them: for a field rename it is the object the field hangs off
    (``order["total"]`` -> owner ``order``), and for a kwarg rename it is the
    function being called (``fetch_orders(timeout_seconds=...)`` -> owner
    ``fetch_orders``). ``owner`` is the main tool for suppressing false
    positives, and it is optional because release notes often omit it.
    """

    symbol: str = ""
    replacement: str = ""
    owner: str = ""

    @property
    def is_rename(self) -> bool:
        return bool(self.symbol and self.replacement and self.symbol != self.replacement)

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "replacement": self.replacement, "owner": self.owner}


@dataclass
class PaginationContract:
    """Field and parameter names for a page-based -> cursor-based migration.

    Defaults are the overwhelmingly common names. They are overridable so the
    handler is not tied to one vendor's vocabulary.
    """

    page_param: str = "page"
    total_pages_key: str = "total_pages"
    cursor_param: str = "cursor"
    next_cursor_key: str = "next_cursor"
    has_more_key: str = "has_more"

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_param": self.page_param,
            "total_pages_key": self.total_pages_key,
            "cursor_param": self.cursor_param,
            "next_cursor_key": self.next_cursor_key,
            "has_more_key": self.has_more_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PaginationContract:
        data = data or {}
        base = cls()
        return cls(
            page_param=str(data.get("page_param") or base.page_param),
            total_pages_key=str(data.get("total_pages_key") or base.total_pages_key),
            cursor_param=str(data.get("cursor_param") or base.cursor_param),
            next_cursor_key=str(data.get("next_cursor_key") or base.next_cursor_key),
            has_more_key=str(data.get("has_more_key") or base.has_more_key),
        )


@dataclass
class BreakingChange:
    """One breaking change, extracted from one change document.

    A document may describe several; ingestion returns a list of these.
    """

    title: str
    kind: ChangeKind
    target: SymbolTarget = field(default_factory=SymbolTarget)
    old_behavior: str = ""
    new_behavior: str = ""
    migration_hint: str = ""
    severity: Severity = Severity.MEDIUM
    #: How confident ingestion is that this reading of the document is correct.
    confidence: Confidence = Confidence.MEDIUM
    evidence: list[Evidence] = field(default_factory=list)
    pagination: PaginationContract = field(default_factory=PaginationContract)
    #: Which parser produced this: "markdown", "structured", or "llm".
    source: str = "markdown"
    #: Why classification landed where it did -- shown to the user verbatim.
    classification_reason: str = ""

    @property
    def is_actionable(self) -> bool:
        return self.kind.is_actionable

    def describe(self) -> str:
        """A one-line human summary, used in logs and reports."""
        if self.target.is_rename:
            return f"{self.kind.value}: `{self.target.symbol}` -> `{self.target.replacement}`"
        return f"{self.kind.value}: {self.title}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "kind": self.kind.value,
            "target": self.target.to_dict(),
            "old_behavior": self.old_behavior,
            "new_behavior": self.new_behavior,
            "migration_hint": self.migration_hint,
            "severity": self.severity.value,
            "confidence": self.confidence.value,
            "evidence": [e.to_dict() for e in self.evidence],
            "pagination": self.pagination.to_dict(),
            "source": self.source,
            "classification_reason": self.classification_reason,
        }
