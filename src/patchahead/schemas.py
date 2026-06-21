"""Structured objects passed between PatchAhead stages.

Everything the agent produces is a typed, inspectable artifact -- not
opaque "AI magic". These objects are what get serialized into the run
log and rendered in the PR summary.
"""

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class BreakingChange:
    title: str
    change_type: str  # pagination | field_rename | endpoint_change | method_rename | response_shape_change
    old_behavior: str
    new_behavior: str
    migration_hint: str
    risk_level: str  # LOW | MEDIUM | HIGH
    evidence: list = field(default_factory=list)
    source: str = "deterministic"  # deterministic | llm
    old_symbol: str = ""  # for rename changes, e.g. "total"
    new_symbol: str = ""  # for rename changes, e.g. "amount"

    def to_dict(self):
        return asdict(self)


@dataclass
class ImpactReport:
    affected_files: list = field(default_factory=list)
    affected_functions: list = field(default_factory=list)
    old_patterns_found: list = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0
    source: str = "deterministic"

    def to_dict(self):
        return asdict(self)


@dataclass
class PatchResult:
    diff: str
    files_changed: list
    patch_explanation: str
    risk_level: str
    new_contents: dict = field(default_factory=dict)  # path -> new source
    source: str = "deterministic"
    fallback_used: bool = False

    def to_dict(self):
        d = asdict(self)
        d.pop("new_contents", None)  # keep the log readable
        return d


@dataclass
class TestRun:
    passed: bool
    command: str
    stdout: str
    stderr: str
    failing_tests: list = field(default_factory=list)
    summary: str = ""
    duration_ms: int = 0

    def to_dict(self):
        d = asdict(self)
        # trim long blobs out of the structured log
        d["stdout"] = self.stdout[-1500:]
        d["stderr"] = self.stderr[-1500:]
        return d


@dataclass
class MigrationReport:
    breaking_change: BreakingChange
    impact: ImpactReport
    patch: PatchResult
    tests_before: TestRun
    tests_after: TestRun
    pr_summary_path: str
    patch_path: str
    succeeded: bool
    similar_migrations: list = field(default_factory=list)

    def to_dict(self):
        return {
            "succeeded": self.succeeded,
            "breaking_change": self.breaking_change.to_dict(),
            "impact": self.impact.to_dict(),
            "patch": self.patch.to_dict(),
            "tests_before": self.tests_before.to_dict(),
            "tests_after": self.tests_after.to_dict(),
            "pr_summary_path": self.pr_summary_path,
            "patch_path": self.patch_path,
            "similar_migrations": self.similar_migrations,
        }
