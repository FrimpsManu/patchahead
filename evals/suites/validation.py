"""Suite 5: do the validation gates reach the right verdict?

The validation engine is the only thing in PatchAhead permitted to call a
migration successful, and until now it had unit tests but no evaluation suite --
so the subsystem that decides what "working" means was the one subsystem the
benchmark did not measure.

Each case declares the status every gate must reach, and why:

==============================  ============================================
red before, green after         ``migration_assertion`` PASSED -- verified
green before, green after       ``migration_assertion`` SKIPPED -- no evidence
red before, still red           ``migration_assertion`` SKIPPED, not FAILED
a test the patch broke          ``regression_tests`` FAILED
a test that was already red     ``regression_tests`` PASSED -- not a regression
no tests in the repository      test gates SKIPPED, never PASSED
``--no-tests``                  test gates SKIPPED, for a stated reason
a patch that does not parse     ``syntax`` FAILED, later gates never run
a file the plan did not name    ``scope`` FAILED, later gates never run
a test command that is missing  both test gates SKIPPED, not FAILED
a test command that won't run   both test gates SKIPPED, not FAILED
==============================  ============================================

The last three rows are the ones this suite was written to hold. "No evidence"
and "evidence of breakage" are different verdicts, and the gates used to collapse
them: a patch with nothing to prove it, and a test runner that was never
installed, both came out as failures that pointed at the patch.

Two of those cannot be produced by running the engine: a deterministic handler
does not emit invalid Python, and it does not touch files its plan omits. So
cases come in two modes. ``engine`` runs the real pipeline. ``direct`` writes a
crafted patch into a real workspace and hands it to the real
:class:`~patchahead.validation.engine.ValidationEngine`. The gates being
measured are the same objects in both; only the source of the patch differs.
Without ``direct``, the two gates that exist specifically to catch a
misbehaving patch generator -- the ones that will matter most when the LLM path
is used in anger -- would have no benchmark coverage at all.

``expect_gate_detail`` guards against the subtler failure: a gate reaching the
right status for the wrong reason. ``migration_assertion`` SKIPPED because the
suite was already green and SKIPPED because nothing ever ran are the same status
and very different facts.
"""

from __future__ import annotations

import time

from evals.harness.dataset import ValidationCase, describe, load
from evals.harness.metrics import Tally
from evals.harness.repo import temporary_repo
from evals.harness.result import CaseResult, SuiteResult
from patchahead import engine
from patchahead.config import Config
from patchahead.domain.change import BreakingChange, ChangeKind
from patchahead.domain.impact import CodeReference
from patchahead.domain.patch import FileEdit, PatchProposal
from patchahead.domain.plan import MigrationPlan, TextEdit, Transformation
from patchahead.domain.validation import GateName, ValidationResult
from patchahead.validation.engine import ValidationEngine, ValidationOptions
from patchahead.workspace import Repository, Workspace

SUITE = "validation"

GATE_STATUSES = ("passed", "failed", "skipped")


def _crafted_proposal(case: ValidationCase, workspace: Workspace) -> PatchProposal:
    """Build a proposal from the case's literal ``patch`` and ``plan_files``.

    The plan is synthetic but its shape is the real one: ``target_files`` is
    derived from transformations, so a case forces a scope failure by naming
    fewer files in ``plan_files`` than its ``patch`` actually writes -- exactly
    what a patch generator that edited a neighbouring file would produce.
    """
    planned = case.plan_files if case.plan_files is not None else sorted(case.patch)
    plan = MigrationPlan(
        change=BreakingChange(title=case.id, kind=ChangeKind.FIELD_RENAME),
        handler="eval",
        rationale="synthetic plan built by the validation eval suite",
        expected_tests=list(case.expected_tests),
    )
    for path in planned:
        plan.transformations.append(
            Transformation(
                reference=CodeReference(path=path, line=1),
                old="<eval>",
                new="<eval>",
                edit=TextEdit(line=1, col=0, end_line=1, end_col=0, new_text=""),
            )
        )

    files: list[FileEdit] = []
    for path, new_source in case.patch.items():
        old_source = workspace.read(path)
        workspace.write(path, new_source)
        files.append(
            FileEdit(path=path, old_source=old_source, new_source=new_source, edit_count=1)
        )
    return PatchProposal(plan=plan, files=files, diff=workspace.diff())


def _validate_direct(case: ValidationCase) -> tuple[ValidationResult, str]:
    config = Config(test_command=case.test_command or Config().test_command)
    with temporary_repo(case.files) as (repo_path, _):
        repository = Repository.open(repo_path, config)
        with Workspace.materialize(repository) as workspace:
            proposal = _crafted_proposal(case, workspace)
            result = ValidationEngine(config).validate(
                proposal,
                workspace,
                ValidationOptions(run_tests=case.run_tests, test_command=config.test_command),
            )
    return result, ""


def _validate_through_engine(case: ValidationCase) -> tuple[ValidationResult, str]:
    with temporary_repo(case.files, case.change) as (repo_path, change_path):
        options = engine.EngineOptions(write_artifacts=False, run_tests=case.run_tests)
        run_result = engine.migrate(repo_path, change_path, options)
        result = run_result.results[0]
        return result.validation or ValidationResult(), result.outcome.value


def run() -> SuiteResult:
    suite = SuiteResult(name=SUITE, description=describe(SUITE))
    start = time.perf_counter()
    gate_tally = {gate.value: Tally(GATE_STATUSES) for gate in GateName}
    verified_cases = 0
    unverified_cases = 0

    for case in load(SUITE, ValidationCase):
        if case.mode not in ValidationCase.VALID_MODES:
            suite.add(CaseResult.judge(case.id, [f"unknown mode {case.mode!r}"]))
            continue

        if case.mode == "direct":
            validation, outcome = _validate_direct(case)
        else:
            validation, outcome = _validate_through_engine(case)

        problems: list[str] = []
        for gate in validation.gates:
            gate_tally[gate.name.value].observe(gate.status.value)

        for name, expected in case.expect_gates.items():
            try:
                gate_name = GateName(name)
            except ValueError:
                problems.append(f"unknown gate {name!r} in expect_gates")
                continue
            if expected not in GATE_STATUSES:
                problems.append(f"{name}: unknown expected status {expected!r}")
                continue
            gate = validation.get(gate_name)
            if gate is None:
                problems.append(f"{name}: gate did not run at all, expected {expected}")
            elif gate.status.value != expected:
                problems.append(
                    f"{name}: expected {expected}, got {gate.status.value} ({gate.detail})"
                )

        for name, fragment in case.expect_gate_detail.items():
            try:
                gate = validation.get(GateName(name))
            except ValueError:
                problems.append(f"unknown gate {name!r} in expect_gate_detail")
                continue
            if gate is None:
                problems.append(f"{name}: no such gate to check the detail of")
            elif fragment.lower() not in gate.detail.lower():
                problems.append(f"{name}: detail {gate.detail!r} does not mention {fragment!r}")

        if case.expect_verified is not None and validation.verified != case.expect_verified:
            problems.append(f"verified: expected {case.expect_verified}, got {validation.verified}")
        if case.expect_passed is not None and validation.passed != case.expect_passed:
            problems.append(f"passed: expected {case.expect_passed}, got {validation.passed}")
        if case.expect_outcome and outcome != case.expect_outcome:
            problems.append(f"outcome: expected {case.expect_outcome}, got {outcome or 'n/a'}")

        if validation.verified:
            verified_cases += 1
        else:
            unverified_cases += 1

        suite.add(
            CaseResult.judge(
                case.id,
                problems,
                known_gap=case.known_gap,
                metrics={"gates": len(validation.gates)},
            )
        )

    suite.metrics = {
        "cases": suite.total,
        "verified": verified_cases,
        "unverified": unverified_cases,
    }
    for gate_value, tally in gate_tally.items():
        suite.metrics.update(tally.to_dict(gate_value))

    suite.metric_notes = {
        "verified": "cases where the gates produced red-to-green evidence",
        "unverified": "cases where they did not -- most of this dataset, by design",
        "migration_assertion_passed": "the only gate result that makes a run a migration",
        "syntax_failed": "crafted invalid patches the syntax gate caught",
        "scope_failed": "crafted out-of-plan writes the scope gate caught",
        "regression_tests_failed": "patches that broke a previously-passing test",
    }
    suite.duration_ms = int((time.perf_counter() - start) * 1000)
    return suite
