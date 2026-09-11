"""The validation gates: what passes, what fails, and what is honestly skipped."""

from __future__ import annotations

import pytest

from patchahead.config import Config
from patchahead.domain.change import BreakingChange, ChangeKind
from patchahead.domain.patch import FileEdit, PatchProposal
from patchahead.domain.plan import MigrationPlan
from patchahead.domain.validation import GateName, GateStatus, TestRun, ValidationResult
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
        baseline = TestRun(
            command="c", returncode=1, failing_tests=["tests/test_a.py::test_f"]
        )
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
