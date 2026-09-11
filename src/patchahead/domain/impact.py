"""The downstream side of the domain model: what the change touches.

The chain the prototype could not express, and which every report now walks:

.. code-block:: text

    BreakingChange
       -> affected API symbol          (BreakingChange.target)
       -> call sites / data accesses   (CodeReference)
       -> affected functions           (ImpactFinding.symbol)
       -> affected files               (ImpactFinding.reference.path)
       -> relevant tests               (ImpactReport.related_tests)

:class:`ImpactGraph` materializes that chain so migration planning can be
explained rather than asserted.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from patchahead.domain.change import BreakingChange, Confidence


class AccessKind(str, enum.Enum):
    """The syntactic shape of an old-contract usage, as seen by the AST."""

    #: ``obj["name"]``
    SUBSCRIPT = "subscript"
    #: ``obj.get("name")``
    DICT_GET = "dict_get"
    #: ``obj.name``
    ATTRIBUTE = "attribute"
    #: ``name(...)`` or ``obj.name(...)``
    CALL = "call"
    #: ``f(name=...)``
    KEYWORD_ARG = "keyword_arg"
    #: A ``while``-loop paginating over an integer page counter.
    PAGE_LOOP = "page_loop"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class CodeReference:
    """A precise location in a source file.

    Columns are 0-indexed byte offsets into the line, matching ``ast``. ``line``
    is 1-indexed, matching every editor and traceback. Ranges are half-open on
    the end column, again matching ``ast``.
    """

    #: Path relative to the repository root, always POSIX-style.
    path: str
    line: int
    col: int = 0
    end_line: int | None = None
    end_col: int | None = None
    #: The source line, stripped -- so a report is readable without the file.
    snippet: str = ""

    def __str__(self) -> str:
        return f"{self.path}:{self.line}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "col": self.col,
            "end_line": self.end_line,
            "end_col": self.end_col,
            "snippet": self.snippet,
        }


@dataclass
class ImpactFinding:
    """One place in the downstream repository affected by the change.

    Every field here exists because a reviewer needs it: *where* (reference),
    *in what* (symbol), *what matched* (matched_contract, access), *why we think
    so* (reason), and *how sure* (confidence).
    """

    reference: CodeReference
    #: Dotted name of the enclosing function/method/class, or "<module>".
    symbol: str
    #: The old contract this matched, e.g. ``order["total"]``.
    matched_contract: str
    access: AccessKind
    reason: str
    confidence: Confidence
    #: The exact source text the reference covers, e.g. ``"total"`` including
    #: its quotes. Recorded at analysis time so planning can build a
    #: replacement that preserves quote style without re-reading the file.
    source_text: str = ""
    #: True when a handler can mechanically rewrite this exact site.
    patchable: bool = True
    #: Set when ``patchable`` is False: why not.
    unpatchable_reason: str = ""

    @property
    def path(self) -> str:
        return self.reference.path

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference.to_dict(),
            "symbol": self.symbol,
            "matched_contract": self.matched_contract,
            "access": self.access.value,
            "reason": self.reason,
            "confidence": self.confidence.value,
            "source_text": self.source_text,
            "patchable": self.patchable,
            "unpatchable_reason": self.unpatchable_reason,
        }


@dataclass
class ImpactGraph:
    """The change -> symbol -> site -> function -> file -> test chain.

    Not a graph database; a set of adjacency views over the findings, computed
    once so reports and plans do not each re-derive them.
    """

    change_title: str
    api_symbol: str
    #: file path -> enclosing symbols affected in it
    functions_by_file: dict[str, list[str]] = field(default_factory=dict)
    #: file path -> test files believed relevant to it
    tests_by_file: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        change: BreakingChange,
        findings: list[ImpactFinding],
        tests_by_file: dict[str, list[str]] | None = None,
    ) -> ImpactGraph:
        functions_by_file: dict[str, list[str]] = {}
        for finding in findings:
            bucket = functions_by_file.setdefault(finding.path, [])
            if finding.symbol not in bucket:
                bucket.append(finding.symbol)
        return cls(
            change_title=change.title,
            api_symbol=change.target.symbol or change.title,
            functions_by_file=functions_by_file,
            tests_by_file=dict(tests_by_file or {}),
        )

    def render(self) -> str:
        """An indented text rendering, used by ``patchahead analyze``."""
        lines = [f"BreakingChange: {self.change_title}", f"  symbol: {self.api_symbol}"]
        for path in sorted(self.functions_by_file):
            lines.append(f"  file: {path}")
            for symbol in self.functions_by_file[path]:
                lines.append(f"    function: {symbol}")
            for test in self.tests_by_file.get(path, []):
                lines.append(f"    test: {test}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_title": self.change_title,
            "api_symbol": self.api_symbol,
            "functions_by_file": self.functions_by_file,
            "tests_by_file": self.tests_by_file,
        }


@dataclass
class ImpactReport:
    """Everything analysis learned about one change against one repository."""

    change: BreakingChange
    findings: list[ImpactFinding] = field(default_factory=list)
    graph: ImpactGraph | None = None
    #: Test files judged relevant to the affected modules.
    related_tests: list[str] = field(default_factory=list)
    #: Number of Python files actually parsed (for timing/debug reporting).
    files_scanned: int = 0
    #: Files that could not be parsed, as ``path: reason``.
    skipped_files: dict[str, str] = field(default_factory=dict)
    analysis_ms: int = 0
    #: Set when the handler declined to analyze at all.
    unsupported_reason: str = ""

    @property
    def has_impact(self) -> bool:
        return bool(self.findings)

    @property
    def affected_files(self) -> list[str]:
        seen: list[str] = []
        for finding in self.findings:
            if finding.path not in seen:
                seen.append(finding.path)
        return seen

    @property
    def affected_symbols(self) -> list[str]:
        seen: list[str] = []
        for finding in self.findings:
            if finding.symbol not in seen:
                seen.append(finding.symbol)
        return seen

    def at_least(self, minimum: Confidence) -> list[ImpactFinding]:
        """Findings meeting a confidence floor -- the patching threshold."""
        return [f for f in self.findings if f.confidence >= minimum]

    @property
    def highest_confidence(self) -> Confidence | None:
        if not self.findings:
            return None
        return max((f.confidence for f in self.findings), key=lambda c: c.rank)

    def to_dict(self) -> dict[str, Any]:
        return {
            "change": self.change.to_dict(),
            "findings": [f.to_dict() for f in self.findings],
            "graph": self.graph.to_dict() if self.graph else None,
            "related_tests": self.related_tests,
            "affected_files": self.affected_files,
            "affected_symbols": self.affected_symbols,
            "files_scanned": self.files_scanned,
            "skipped_files": self.skipped_files,
            "analysis_ms": self.analysis_ms,
            "unsupported_reason": self.unsupported_reason,
        }
