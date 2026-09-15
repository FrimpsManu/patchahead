"""The validation gates: what passes, what fails, and what is honestly skipped."""

from __future__ import annotations

import pytest

from patchahead.config import Config
from patchahead.domain.change import BreakingChange, ChangeKind
from patchahead.domain.patch import FileEdit, PatchProposal
from patchahead.domain.plan import MigrationPlan
from patchahead.domain.validation import GateName, GateStatus, TestRun, ValidationResult
from patchahead.testing import runner
from patchahead.validation import ValidationEngine, ValidationOptions
from patchahead.workspace import Repository, Workspace


@pytest.fixture
def workspace(make_repo):
    repository = Repository.open(
        make_repo(
            {
                "app/a.py": "def f():\n    return 1\n",
                "app/b.py": "def g():\n    return 2\n",
                "tests/test_a.py": "from app.a import f\n\n\ndef test_f():\n    assert f() == 1\n",
                "conftest.py": "import os, sys\nsys.path.insert(0, os.path.dirname(__file__))\n",
            }
        )
    )
    with Workspace.materialize(repository) as ws:
        yield ws


def proposal_for(workspace, path, new_source, expected_tests=None, planned=None):
    """Write a change and build the matching proposal, the way a handler does."""
    from patchahead.analysis import unified_diff
    from patchahead.domain.impact import CodeReference
    from patchahead.domain.plan import TextEdit, Transformation

    original = workspace.read(path)
    workspace.write(path, new_source)
    plan = MigrationPlan(
        change=BreakingChange(title="t", kind=ChangeKind.FIELD_RENAME),
        handler="test",
        expected_tests=list(expected_tests or []),
    )
    for target in planned if planned is not None else [path]:
        plan.transformations.append(
            Transformation(
                reference=CodeReference(path=target, line=1),
                old="a",
                new="b",
                edit=TextEdit(1, 0, 1, 0, ""),
            )
        )
    return PatchProposal(
        plan=plan,
        files=[FileEdit(path=path, old_source=original, new_source=new_source, edit_count=1)],
        diff=unified_diff(original, new_source, path),
    )


class TestSyntaxGate:
    def test_passes_for_valid_python(self, workspace):
        proposal = proposal_for(workspace, "app/a.py", "def f():\n    return 9\n")

        result = ValidationEngine(Config()).validate(
            proposal, workspace, ValidationOptions(run_tests=False)
        )

        assert result.get(GateName.SYNTAX).status is GateStatus.PASSED

    def test_fails_for_broken_python_and_skips_the_test_gates(self, workspace):
        proposal = proposal_for(workspace, "app/a.py", "def f(:\n")

        result = ValidationEngine(Config()).validate(proposal, workspace)

        assert result.get(GateName.SYNTAX).status is GateStatus.FAILED
        assert result.get(GateName.TARGETED_TESTS).status is GateStatus.SKIPPED
        assert result.passed is False

    def test_a_syntax_failure_stops_execution_of_repository_code(self, workspace):
        """A broken patch must never reach the stage that runs a test command."""
        proposal = proposal_for(workspace, "app/a.py", "def f(:\n")

        result = ValidationEngine(Config()).validate(proposal, workspace)

        regression = result.get(GateName.REGRESSION_TESTS)
        assert regression.test_run is None


class TestScopeGate:
    def test_fails_when_a_file_outside_the_plan_changed(self, workspace):
        proposal = proposal_for(
            workspace, "app/a.py", "def f():\n    return 9\n", planned=["app/a.py"]
        )
        workspace.write("app/b.py", "def g():\n    return 99\n")

        result = ValidationEngine(Config()).validate(
            proposal, workspace, ValidationOptions(run_tests=False)
        )

        gate = result.get(GateName.SCOPE)
        assert gate.status is GateStatus.FAILED
        assert "app/b.py" in gate.detail

    def test_fails_when_too_many_files_changed(self, workspace):
        proposal = proposal_for(
            workspace, "app/a.py", "def f():\n    return 9\n", planned=["app/a.py", "app/b.py"]
        )
        workspace.write("app/b.py", "def g():\n    return 99\n")

        result = ValidationEngine(Config(max_changed_files=1)).validate(
            proposal, workspace, ValidationOptions(run_tests=False)
        )

        assert "max_changed_files" in result.get(GateName.SCOPE).detail

    def test_fails_an_oversized_diff(self, workspace):
        proposal = proposal_for(
            workspace, "app/a.py", "def f():\n" + "    x = 1\n" * 50 + "    return 1\n"
        )

        result = ValidationEngine(Config(max_diff_lines=5)).validate(
            proposal, workspace, ValidationOptions(run_tests=False)
        )

        assert "max_diff_lines" in result.get(GateName.SCOPE).detail

    def test_preexisting_changes_are_not_blamed_on_this_proposal(self, workspace):
        """Multi-change runs share one workspace; each patch is judged alone."""
        workspace.write("app/b.py", "def g():\n    return 99\n")
        preexisting = set(workspace.changed_files())
        proposal = proposal_for(
            workspace, "app/a.py", "def f():\n    return 9\n", planned=["app/a.py"]
        )

        result = ValidationEngine(Config()).validate(
            proposal,
            workspace,
            ValidationOptions(run_tests=False, preexisting_changes=preexisting),
        )

        assert result.get(GateName.SCOPE).status is GateStatus.PASSED


class TestRegressionGate:
    @pytest.mark.slow
    def test_a_newly_broken_test_is_a_regression(self, workspace):
        baseline = TestRun(command="c", returncode=0, failing_tests=[])
        proposal = proposal_for(workspace, "app/a.py", "def f():\n    return 999\n")

        result = ValidationEngine(Config()).validate(
            proposal, workspace, ValidationOptions(full_baseline=baseline)
        )

        gate = result.get(GateName.REGRESSION_TESTS)
        assert gate.status is GateStatus.FAILED
        assert "passed before" in gate.detail

    @pytest.mark.slow
    def test_a_test_that_was_already_failing_is_not_a_regression(self, workspace):
        baseline = TestRun(
            command="c",
            returncode=1,
            failing_tests=["tests/test_a.py::test_f"],
        )
        proposal = proposal_for(workspace, "app/a.py", "def f():\n    return 999\n")

        result = ValidationEngine(Config()).validate(
            proposal, workspace, ValidationOptions(full_baseline=baseline)
        )

        gate = result.get(GateName.REGRESSION_TESTS)
        assert gate.status is GateStatus.PASSED
        assert "already failing" in gate.detail

    @pytest.mark.slow
    def test_without_a_baseline_any_failure_is_treated_as_a_regression(self, workspace):
        proposal = proposal_for(workspace, "app/a.py", "def f():\n    return 999\n")

        result = ValidationEngine(Config()).validate(proposal, workspace)

        gate = result.get(GateName.REGRESSION_TESTS)
        assert gate.status is GateStatus.FAILED
        assert "no pre-patch baseline" in gate.detail


class TestMigrationAssertionGate:
    def test_skipped_without_a_baseline(self, workspace):
        proposal = proposal_for(workspace, "app/a.py", "def f():\n    return 9\n")

        result = ValidationEngine(Config()).validate(
            proposal, workspace, ValidationOptions(run_tests=False)
        )

        assert result.get(GateName.MIGRATION_ASSERTION).status is GateStatus.SKIPPED

    @pytest.mark.slow
    def test_a_green_to_green_run_is_skipped_not_claimed_as_a_success(self, workspace):
        """Passing tests before and after is not evidence of a migration."""
        baseline = TestRun(command="c", returncode=0)
        proposal = proposal_for(
            workspace,
            "app/a.py",
            "def f():\n    return 1\n",
            expected_tests=["tests/test_a.py"],
        )

        result = ValidationEngine(Config()).validate(
            proposal,
            workspace,
            ValidationOptions(baseline=baseline, full_baseline=baseline),
        )

        gate = result.get(GateName.MIGRATION_ASSERTION)
        assert gate.status is GateStatus.SKIPPED
        assert "already passed" in gate.detail

    @pytest.mark.slow
    def test_red_to_green_passes_the_assertion(self, workspace):
        workspace.write("app/a.py", "def f():\n    return 999\n")
        baseline = TestRun(command="c", returncode=1, failing_tests=["tests/test_a.py::test_f"])
        proposal = proposal_for(
            workspace,
            "app/a.py",
            "def f():\n    return 1\n",
            expected_tests=["tests/test_a.py"],
        )

        result = ValidationEngine(Config()).validate(
            proposal,
            workspace,
            ValidationOptions(baseline=baseline, full_baseline=baseline),
        )

        assert result.get(GateName.MIGRATION_ASSERTION).status is GateStatus.PASSED
        assert result.passed is True


class TestValidationResultSemantics:
    def test_an_empty_result_is_not_a_pass(self):
        assert ValidationResult().passed is False

    def test_an_all_skipped_result_is_not_a_pass(self, workspace):
        """Refusing to check something is not evidence that it is correct."""
        proposal = PatchProposal(
            plan=MigrationPlan(
                change=BreakingChange(title="t", kind=ChangeKind.FIELD_RENAME), handler="t"
            ),
            files=[],
        )

        result = ValidationEngine(Config()).validate(
            proposal, workspace, ValidationOptions(run_tests=False)
        )

        assert all(g.status is GateStatus.SKIPPED for g in result.gates)
        assert result.passed is False

    def test_no_tests_flag_skips_execution_gates_with_a_stated_reason(self, workspace):
        proposal = proposal_for(workspace, "app/a.py", "def f():\n    return 9\n")

        result = ValidationEngine(Config()).validate(
            proposal, workspace, ValidationOptions(run_tests=False)
        )

        gate = result.get(GateName.REGRESSION_TESTS)
        assert gate.status is GateStatus.SKIPPED
        assert "--no-tests" in gate.detail


class TestUnrunnableTestCommand:
    """A runner that cannot start is "could not verify", not "verified and failed"."""

    def test_a_missing_runner_skips_rather_than_fails(self, workspace):
        proposal = proposal_for(
            workspace,
            "app/a.py",
            "def f():\n    return 9\n",
            expected_tests=["tests/test_a.py"],
        )

        result = ValidationEngine(Config()).validate(
            proposal,
            workspace,
            ValidationOptions(test_command="python -m pytest_absent_xyz"),
        )

        for name in (GateName.TARGETED_TESTS, GateName.REGRESSION_TESTS):
            assert result.get(name).status is GateStatus.SKIPPED
            assert "could not start" in result.get(name).detail

    def test_and_the_result_is_unverified_not_passed(self, workspace):
        proposal = proposal_for(
            workspace,
            "app/a.py",
            "def f():\n    return 9\n",
            expected_tests=["tests/test_a.py"],
        )

        result = ValidationEngine(Config()).validate(
            proposal,
            workspace,
            ValidationOptions(test_command="python -m pytest_absent_xyz"),
        )

        assert result.verified is False, "nothing ran, so nothing was verified"
        assert result.tests_ran is False


class TestMigrationSemantics:
    """The four required outcomes, stated as a table.

    ==========================  =====================  ==========
    Before -> after             Outcome                succeeded
    ==========================  =====================  ==========
    red -> green                MIGRATED               True
    green -> green              PATCHED_UNVERIFIED     False
    no runnable tests           PATCHED_UNVERIFIED     False
    a test this patch broke     VALIDATION_FAILED      False
    ==========================  =====================  ==========
    """

    def _validate(self, workspace, *, baseline, full_baseline, expected_tests, new_source):
        proposal = proposal_for(workspace, "app/a.py", new_source, expected_tests=expected_tests)
        return ValidationEngine(Config()).validate(
            proposal,
            workspace,
            ValidationOptions(baseline=baseline, full_baseline=full_baseline),
        )

    @pytest.mark.slow
    def test_red_before_green_after_is_verified(self, workspace):
        workspace.write("app/a.py", "def f():\n    return 999\n")
        baseline = TestRun(command="c", returncode=1, failing_tests=["tests/test_a.py::test_f"])

        result = self._validate(
            workspace,
            baseline=baseline,
            full_baseline=baseline,
            expected_tests=["tests/test_a.py"],
            new_source="def f():\n    return 1\n",
        )

        assert result.get(GateName.MIGRATION_ASSERTION).status is GateStatus.PASSED
        assert result.verified is True

    @pytest.mark.slow
    def test_green_before_green_after_is_not_verified(self, workspace):
        """No gate objects, but nothing proved the migration did anything."""
        baseline = TestRun(command="c", returncode=0)

        result = self._validate(
            workspace,
            baseline=baseline,
            full_baseline=baseline,
            expected_tests=["tests/test_a.py"],
            new_source="def f():\n    return 1\n",
        )

        assert result.passed is True, "no gate failed"
        assert result.verified is False, "but nothing verified it"
        assert "already passed" in result.get(GateName.MIGRATION_ASSERTION).detail

    def test_no_runnable_tests_is_not_verified(self, workspace):
        result = ValidationEngine(Config()).validate(
            proposal_for(workspace, "app/a.py", "def f():\n    return 9\n"),
            workspace,
            ValidationOptions(run_tests=False),
        )

        assert result.verified is False
        assert result.get(GateName.MIGRATION_ASSERTION).status is GateStatus.SKIPPED

    @pytest.mark.slow
    def test_a_regression_fails_validation_outright(self, workspace):
        baseline = TestRun(command="c", returncode=0, failing_tests=[])

        result = self._validate(
            workspace,
            baseline=baseline,
            full_baseline=baseline,
            expected_tests=["tests/test_a.py"],
            new_source="def f():\n    return 999\n",
        )

        assert result.passed is False
        assert result.verified is False
        assert result.get(GateName.REGRESSION_TESTS).status is GateStatus.FAILED

    @pytest.mark.slow
    def test_the_full_suite_can_supply_the_evidence_when_targeting_cannot(self, workspace):
        """A test command that cannot be narrowed still produces red-to-green proof."""
        workspace.write("app/a.py", "def f():\n    return 999\n")
        baseline = TestRun(command="c", returncode=1, failing_tests=["tests/test_a.py::test_f"])

        proposal = proposal_for(workspace, "app/a.py", "def f():\n    return 1\n")
        result = ValidationEngine(Config()).validate(
            proposal,
            workspace,
            ValidationOptions(baseline=None, full_baseline=baseline),
        )

        assert result.get(GateName.TARGETED_TESTS).status is GateStatus.SKIPPED
        assert result.get(GateName.MIGRATION_ASSERTION).status is GateStatus.PASSED
        assert "full suite" in result.get(GateName.MIGRATION_ASSERTION).detail
        assert result.verified is True


class TestTheAssertionGateDistinguishesAbsenceOfProofFromBreakage:
    """The gate asks "is there red-to-green evidence", so "no" is SKIPPED.

    It used to answer FAILED to several shapes that are merely unevidenced, and
    -- worse -- it read the *regression gate's* verdict as "the suite is green".
    The regression gate is baseline-relative on purpose: it passes when nothing
    got worse, which on a repository with pre-existing breakage means it passes
    over a red suite. Reading that as green produced the self-contradictory
    "the run is green but none of the tests that failed before the patch were
    among them", attached to a FAILED status, on a patch that had done nothing
    wrong.

    Each test below pins one row of the required table.
    """

    TEST_B = "from app.b import g\n\n\ndef test_g():\n    assert g() == 2\n"

    @pytest.mark.slow
    def test_still_red_with_nothing_repaired_is_skipped_not_failed(self, workspace):
        """Row 3: red before, still red, nothing fixed -> SKIPPED."""
        workspace.write("app/a.py", "def f():\n    return 999\n")
        baseline = TestRun(command="c", returncode=1, failing_tests=["tests/test_a.py::test_f"])
        proposal = proposal_for(
            workspace,
            "app/b.py",
            "def g():\n    return 3\n",
            expected_tests=["tests/test_a.py"],
        )

        result = ValidationEngine(Config()).validate(
            proposal,
            workspace,
            ValidationOptions(
                baseline=baseline,
                full_baseline=baseline,
                preexisting_changes={"app/a.py"},
            ),
        )

        gate = result.get(GateName.MIGRATION_ASSERTION)
        assert gate.status is GateStatus.SKIPPED
        assert "none of the tests that were failing before it pass now" in gate.detail
        assert result.verified is False

    @pytest.mark.slow
    def test_a_red_suite_the_regression_gate_passed_is_not_called_green(self, workspace):
        """The exact contradiction: regression PASSED over a suite that is red.

        No targeted tests, so the assertion falls back to the full suite -- whose
        gate passed, because the only failure was already failing. The gate must
        read the *run*, not the gate's verdict.
        """
        workspace.write("app/a.py", "def f():\n    return 999\n")
        baseline = TestRun(command="c", returncode=1, failing_tests=["tests/test_a.py::test_f"])
        proposal = proposal_for(workspace, "app/b.py", "def g():\n    return 3\n")

        result = ValidationEngine(Config()).validate(
            proposal,
            workspace,
            ValidationOptions(
                baseline=None,
                full_baseline=baseline,
                preexisting_changes={"app/a.py"},
            ),
        )

        assert result.get(GateName.REGRESSION_TESTS).status is GateStatus.PASSED
        gate = result.get(GateName.MIGRATION_ASSERTION)
        assert gate.status is GateStatus.SKIPPED
        assert "green" not in gate.detail, f"the suite is red: {gate.detail}"
        assert result.verified is False

    @pytest.mark.slow
    def test_repairing_one_of_several_failures_is_evidence_enough(self, workspace):
        """Row 1, the partial case: a repository broken twice, migrated once.

        Requiring a fully green suite would make PatchAhead unusable on exactly
        the repositories it exists for -- ones already behind on their upstreams.
        """
        workspace.write("tests/test_b.py", self.TEST_B)
        workspace.write("app/a.py", "def f():\n    return 999\n")
        workspace.write("app/b.py", "def g():\n    return 999\n")
        baseline = TestRun(
            command="c",
            returncode=1,
            failing_tests=["tests/test_a.py::test_f", "tests/test_b.py::test_g"],
        )
        proposal = proposal_for(workspace, "app/b.py", "def g():\n    return 2\n")

        result = ValidationEngine(Config()).validate(
            proposal,
            workspace,
            ValidationOptions(
                baseline=None,
                full_baseline=baseline,
                preexisting_changes={"app/a.py", "tests/test_b.py"},
            ),
        )

        gate = result.get(GateName.MIGRATION_ASSERTION)
        assert gate.status is GateStatus.PASSED, gate.detail
        assert "tests/test_b.py::test_g" in gate.detail
        assert "already failing before it" in gate.detail
        assert result.verified is True

    @pytest.mark.slow
    def test_a_repair_alongside_a_new_failure_is_the_regression_gates_verdict(self, workspace):
        """Row 4: real evidence and real damage. The damage wins, once.

        Passing here would put "migrated" over a regression; failing here would
        report the same broken test twice under two different explanations.
        """
        workspace.write("tests/test_b.py", self.TEST_B)
        workspace.write("app/b.py", "def g():\n    return 999\n")
        baseline = TestRun(command="c", returncode=1, failing_tests=["tests/test_b.py::test_g"])
        workspace.write("app/a.py", "def f():\n    return 999\n")
        proposal = proposal_for(
            workspace,
            "app/b.py",
            "def g():\n    return 2\n",
            planned=["app/a.py", "app/b.py"],
        )

        result = ValidationEngine(Config()).validate(
            proposal,
            workspace,
            ValidationOptions(
                baseline=None,
                full_baseline=baseline,
                preexisting_changes={"tests/test_b.py"},
            ),
        )

        regression = result.get(GateName.REGRESSION_TESTS)
        assert regression.status is GateStatus.FAILED
        assert "tests/test_a.py::test_f" in regression.detail

        gate = result.get(GateName.MIGRATION_ASSERTION)
        assert gate.status is GateStatus.SKIPPED
        assert "the regression gate reports those" in gate.detail
        assert result.passed is False
        assert result.verified is False


class TestARunnerThatCannotStartIsNeverAFailure:
    """Exit 127 is the shell saying it found nothing to run.

    It used to reach the gates as a test failure -- and, because the regression
    gate compares against a baseline that also could not run, as a *code
    regression*: PatchAhead blaming its own patch for the absence of pytest. The
    distinguishing signal is the exit status, not the message: `bash` says
    "command not found" and `dash` says "not found", and the matcher only knew
    the first spelling.
    """

    #: A file that exists but carries no execute bit, so the shell finds it and
    #: still refuses: the 126 half of the pair.
    NOT_EXECUTABLE = "pytest-not-executable"

    def _validate(self, workspace, command):
        workspace.write(self.NOT_EXECUTABLE, "#!/bin/sh\nexit 0\n")
        proposal = proposal_for(
            workspace,
            "app/a.py",
            "def f():\n    return 9\n",
            expected_tests=["tests/test_a.py"],
        )
        return ValidationEngine(Config()).validate(
            proposal,
            workspace,
            ValidationOptions(
                test_command=command,
                # The stand-in runner is scaffolding, not part of the patch.
                preexisting_changes={self.NOT_EXECUTABLE},
            ),
        )

    @pytest.mark.parametrize(
        "command, why",
        [
            ("pytest-no-such-runner-xyz", "exit 127: the shell found no such command"),
            ("./pytest-not-executable", "exit 126: found, but not executable"),
            ("python -m pytest_absent_xyz", "the runner reports a missing module itself"),
        ],
    )
    def test_both_test_gates_skip_with_an_actionable_reason(self, workspace, command, why):
        result = self._validate(workspace, command)

        for name in (GateName.TARGETED_TESTS, GateName.REGRESSION_TESTS):
            gate = result.get(name)
            assert gate.status is GateStatus.SKIPPED, f"{name} ({why}): {gate.detail}"
            assert "could not start" in gate.detail
            assert runner.RUNNER_ADVICE in gate.detail, "a dead end with no next step"

    @pytest.mark.parametrize("command", ["pytest-no-such-runner-xyz", "./pytest-not-executable"])
    def test_it_is_never_reported_as_a_regression(self, workspace, command):
        """The specific misclassification: "the patch broke N test(s)"."""
        result = self._validate(workspace, command)

        assert result.failed_gates == []
        assert "broke" not in result.get(GateName.REGRESSION_TESTS).detail

    @pytest.mark.parametrize("command", ["pytest-no-such-runner-xyz", "./pytest-not-executable"])
    def test_the_migration_stays_unverified_and_says_why(self, workspace, command):
        result = self._validate(workspace, command)

        assert result.verified is False
        assert result.tests_ran is False
        gate = result.get(GateName.MIGRATION_ASSERTION)
        assert gate.status is GateStatus.SKIPPED
        assert "did not complete" in gate.detail
        assert runner.RUNNER_ADVICE in gate.detail


class TestTheRunnerReadsExitStatusNotProse:
    """Unit-level cover for the signal the gates depend on."""

    @pytest.mark.parametrize("code", [126, 127])
    def test_the_shell_cannot_start_codes_are_recognized_whatever_the_wording(self, code):
        for wording in ("sh: 1: thing: not found", "bash: thing: command not found", ""):
            assert runner.could_not_start(wording, code) is True

    def test_a_real_test_failure_is_not_mistaken_for_a_missing_runner(self):
        assert runner.could_not_start("1 failed, 2 passed", 1) is False

    def test_a_green_run_is_never_read_as_unstartable(self):
        assert runner.could_not_start("", 0) is False
        assert runner.could_not_start("No module named foo", 0) is False

    def test_a_runner_that_reports_its_own_missing_module_still_counts(self):
        assert runner.could_not_start("No module named pytest", 1) is True

    def test_the_summary_tells_the_reader_what_to_do_about_it(self):
        summary = runner._summarize("sh: 1: nope: not found", 127)

        assert "could not start" in summary
        assert runner.RUNNER_ADVICE in summary
