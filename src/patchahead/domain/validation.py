"""Validation results: the gates a proposal must pass to be called a migration.

Two properties, and the difference between them is the point.
:attr:`ValidationResult.passed` is true when no gate failed and at least one
actually ran -- necessary, and not sufficient.
:attr:`ValidationResult.verified` additionally requires the
``migration_assertion`` gate to have *passed*, meaning a test that failed before
the patch passes after it.

:attr:`~patchahead.domain.result.MigrationResult.succeeded` is defined in terms
of ``verified``, not ``passed``: a run where every gate was happy but nothing
demonstrated the break was fixed reports ``patched_unverified``, which is not a
success. Nothing else in the codebase is permitted to decide that a migration
worked.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class GateName(str, enum.Enum):
    """The five gates, in the order they run.

    Cheap and decisive gates run first so an unsafe proposal never reaches the
    stage that executes repository code.
    """

    SYNTAX = "syntax"
    SCOPE = "scope"
    TARGETED_TESTS = "targeted_tests"
    REGRESSION_TESTS = "regression_tests"
    MIGRATION_ASSERTION = "migration_assertion"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class GateStatus(str, enum.Enum):
    PASSED = "passed"
    FAILED = "failed"
    #: Not run, for a stated reason (e.g. no tests found, ``--no-tests``).
    SKIPPED = "skipped"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass
class TestRun:
    """The structured result of executing a test command."""

    #: Tells pytest not to try to collect this as a test class. It is a domain
    #: object whose name happens to start with "Test".
    __test__ = False

    command: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    failing_tests: list[str] = field(default_factory=list)
    summary: str = ""
    duration_ms: int = 0
    #: True when the command could not be run at all (not found, timed out).
    errored: bool = False

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.errored

    def to_dict(self, *, tail: int = 2000) -> dict[str, Any]:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "passed": self.passed,
            "errored": self.errored,
            "failing_tests": self.failing_tests,
            "summary": self.summary,
            "duration_ms": self.duration_ms,
            "stdout_tail": self.stdout[-tail:],
            "stderr_tail": self.stderr[-tail:],
        }


@dataclass
class GateResult:
    """One gate's verdict, with the detail needed to debug a failure."""

    name: GateName
    status: GateStatus
    #: One line explaining the verdict. Always populated.
    detail: str
    duration_ms: int = 0
    test_run: TestRun | None = None

    @property
    def passed(self) -> bool:
        return self.status is GateStatus.PASSED

    @property
    def failed(self) -> bool:
        return self.status is GateStatus.FAILED

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name.value,
            "status": self.status.value,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            "test_run": self.test_run.to_dict() if self.test_run else None,
        }


@dataclass
class ValidationResult:
    """The verdict on a patch proposal: every gate, in order, with reasons."""

    gates: list[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only if no gate failed and at least one gate actually ran.

        An all-skipped result is not a pass. Refusing to check something is not
        evidence that it is correct.
        """
        if not self.gates:
            return False
        if any(g.failed for g in self.gates):
            return False
        return any(g.passed for g in self.gates)

    @property
    def verified(self) -> bool:
        """Passed *and* the tests proved the break was fixed.

        Distinct from :attr:`passed` because "no gate objected" and "the tests
        confirmed it" are different claims. Verification requires the
        migration-assertion gate to have *passed*, which means a test that
        failed before the patch passes after it.

        Everything short of that is a patch without evidence:

        =========================  ===========================================
        Situation                  Assertion gate
        =========================  ===========================================
        red before, green after    PASSED  -> verified
        green before, green after  SKIPPED -> the tests do not cover the change
        red before, still red      SKIPPED -> nothing was repaired to point at
        no runnable tests          SKIPPED -> nothing ran
        a test this patch broke    the regression gate FAILS first
        =========================  ===========================================

        Verification requires affirmative evidence, never merely the absence of
        a regression -- which is why every row but the first lands short of it.
        """
        assertion = self.get(GateName.MIGRATION_ASSERTION)
        return self.passed and assertion is not None and assertion.passed

    @property
    def tests_ran(self) -> bool:
        return any(
            gate.name in (GateName.TARGETED_TESTS, GateName.REGRESSION_TESTS)
            and gate.status is not GateStatus.SKIPPED
            for gate in self.gates
        )

    @property
    def failed_gates(self) -> list[GateResult]:
        return [g for g in self.gates if g.failed]

    @property
    def skipped_gates(self) -> list[GateResult]:
        return [g for g in self.gates if g.status is GateStatus.SKIPPED]

    def get(self, name: GateName) -> GateResult | None:
        for gate in self.gates:
            if gate.name is name:
                return gate
        return None

    def summary(self) -> str:
        if not self.gates:
            return "no gates ran"
        if self.passed:
            skipped = len(self.skipped_gates)
            tail = f" ({skipped} skipped)" if skipped else ""
            return f"{len(self.gates) - skipped}/{len(self.gates)} gates passed{tail}"
        first = self.failed_gates[0]
        return f"gate `{first.name.value}` failed: {first.detail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "verified": self.verified,
            "summary": self.summary(),
            "gates": [g.to_dict() for g in self.gates],
        }
