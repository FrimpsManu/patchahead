"""The PatchAhead domain model.

Everything that crosses a stage boundary is one of these objects. No stage
passes a loosely-structured dict to another stage.
"""

from patchahead.domain.change import (
    BreakingChange,
    ChangeKind,
    Confidence,
    Evidence,
    PaginationContract,
    Severity,
    SymbolTarget,
)
from patchahead.domain.impact import (
    AccessKind,
    CodeReference,
    ImpactFinding,
    ImpactGraph,
    ImpactReport,
)
from patchahead.domain.patch import FileEdit, PatchProposal
from patchahead.domain.plan import MigrationPlan, Risk, TextEdit, Transformation
from patchahead.domain.result import (
    AnalysisResult,
    MigrationResult,
    MigrationRun,
    Outcome,
)
from patchahead.domain.validation import (
    GateName,
    GateResult,
    GateStatus,
    TestRun,
    ValidationResult,
)

__all__ = [
    "AccessKind",
    "AnalysisResult",
    "BreakingChange",
    "ChangeKind",
    "CodeReference",
    "Confidence",
    "Evidence",
    "FileEdit",
    "GateName",
    "GateResult",
    "GateStatus",
    "ImpactFinding",
    "ImpactGraph",
    "ImpactReport",
    "MigrationPlan",
    "MigrationResult",
    "MigrationRun",
    "Outcome",
    "PaginationContract",
    "PatchProposal",
    "Risk",
    "Severity",
    "SymbolTarget",
    "TestRun",
    "TextEdit",
    "Transformation",
    "ValidationResult",
]
