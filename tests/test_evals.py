"""The evaluation benchmark, run as part of CI.

Evals that are never executed rot into decoration. Running them here means a
change that degrades classification accuracy, introduces an impact false
positive, or loosens a validation gate fails the build rather than being
noticed at the next release.

The thresholds are the *current measured* values, asserted as floors. Raise one
when the number improves; never lower one to make a red build green. Where a
threshold is a safety property -- no false positives, no incorrect migrations --
it is not negotiable in either direction.

Known gaps are deliberately absent from these floors. They are recorded in the
datasets, reported by the benchmark, and asserted on separately below: a gap
must have a stated reason, and a gap may never be a wrong edit.
"""

from __future__ import annotations

import pytest

from evals import run as evals
from evals.harness.result import CaseStatus


@pytest.mark.slow
class TestClassification:
    def test_kind_and_symbol_accuracy(self):
        suite = evals.run_classification()

        assert suite.metrics["kind_accuracy"] >= 1.0, [
            (c.case_id, c.detail) for c in suite.failures
        ]
        assert suite.metrics["symbol_accuracy_overall"] >= 1.0

    def test_asserted_and_illustrated_ownership_are_told_apart(self):
        """The distinction that decides whether a receiver mismatch refuses.

        Reading an illustration as an assertion refuses migrations that should
        happen; reading an assertion as an illustration patches objects the
        change document never mentioned.
        """
        suite = evals.run_classification()

        assert suite.metrics["owner_explicit_accuracy"] >= 1.0
        assert suite.metrics["owner_explicit_n"] >= 5

    def test_no_document_that_should_be_refused_is_acted_on(self):
        """The expensive classification error, asserted at zero."""
        suite = evals.run_classification()

        assert suite.metrics["refusal_false_negatives"] == 0
        assert suite.metrics["refusal_recall"] >= 1.0

    def test_reported_confidence_tracks_accuracy(self):
        """Confidence that does not predict correctness must not be shown."""
        suite = evals.run_classification()

        assert suite.metrics["calibration_monotonic"] == 1.0


@pytest.mark.slow
class TestImpactDetection:
    def test_impact_precision_and_recall(self):
        suite = evals.run_impact()

        assert suite.metrics["patch_false_positives"] == 0, [
            (c.case_id, c.detail) for c in suite.failures
        ]
        assert suite.metrics["patch_precision"] >= 1.0
        assert suite.metrics["patch_recall"] >= 1.0
        assert suite.metrics["patch_f1"] >= 1.0

    def test_the_adversarial_suite_finds_no_false_positives(self):
        """The suite designed to break things must not break anything.

        A false positive here is a wrong edit to unrelated code, which is the
        failure mode PatchAhead exists to avoid. Not negotiable downward.
        """
        suite = evals.run_adversarial()

        assert suite.metrics["patch_false_positives"] == 0, [
            (c.case_id, c.detail) for c in suite.failures
        ]
        assert suite.metrics["patch_precision"] >= 1.0
        assert suite.metrics["patch_recall"] >= 1.0

    def test_not_even_a_known_gap_patches_the_wrong_code(self):
        """A recorded limitation may under-migrate. It may never mis-migrate."""
        for suite in (evals.run_impact(), evals.run_adversarial()):
            assert suite.metrics["patch_false_positives_including_gaps"] == 0, suite.name

    def test_edits_land_where_they_are_aimed(self):
        """A site list can be right while the edit still mangles the line."""
        for suite in (evals.run_impact(), evals.run_adversarial()):
            assert suite.metrics["mangled_sources"] == 0, suite.name

    def test_the_adversarial_dataset_is_substantial(self):
        suite = evals.run_adversarial()

        assert suite.total >= 28, "the adversarial dataset must not shrink"


@pytest.mark.slow
class TestMigrations:
    def test_migration_success_rate(self):
        suite = evals.run_migrations()

        assert suite.metrics["migration_success_rate"] >= 1.0, [
            (c.case_id, c.detail) for c in suite.failures
        ]

    def test_refusal_cases_are_part_of_the_dataset(self):
        """A success rate measured only on cases we can do is not a measurement."""
        suite = evals.run_migrations()

        assert suite.metrics["refusal_cases"] >= 5

    def test_nothing_is_migrated_that_should_have_been_refused(self):
        suite = evals.run_migrations()

        assert suite.metrics["incorrect_migration"] == 0

    def test_an_already_migrated_repository_is_not_edited(self):
        """Editing code that needs no migration is a wrong edit."""
        suite = evals.run_migrations()

        assert suite.metrics["unnecessary_migration"] == 0

    def test_patches_stay_small(self):
        """A rename whose mean diff grows has stopped being a rename."""
        suite = evals.run_migrations()

        assert suite.metrics["changed_lines_mean"] <= 4
        assert suite.metrics["changed_files_max"] <= 2


@pytest.mark.slow
class TestValidationGates:
    def test_the_assertion_gate_is_the_only_route_to_verified(self):
        suite = evals.run_validation()

        assert suite.metrics["verified"] == suite.metrics["migration_assertion_passed"]
        assert suite.metrics["verified"] >= 2

    def test_most_of_the_dataset_is_deliberately_unverified(self):
        """Green-to-green, no tests, broken patches, out-of-scope writes."""
        suite = evals.run_validation()

        assert suite.metrics["unverified"] > suite.metrics["verified"]

    def test_the_gates_that_only_a_bad_patch_generator_triggers_are_covered(self):
        """Syntax and scope exist for the day a model writes the patch.

        No deterministic handler can reach them, so without crafted cases they
        would have no benchmark coverage at all.
        """
        suite = evals.run_validation()

        assert suite.metrics["syntax_failed"] >= 1
        assert suite.metrics["scope_failed"] >= 1

    def test_a_patch_that_breaks_a_passing_test_is_caught(self):
        suite = evals.run_validation()

        assert suite.metrics["regression_tests_failed"] >= 1


@pytest.mark.slow
class TestKnownGaps:
    """The benchmark's honesty mechanism, asserted rather than trusted."""

    def test_every_known_gap_states_why(self):
        """A gap with no reason is a suppressed failure."""
        result = evals.run_benchmark()

        for case in result.known_gaps:
            assert case.gap_reason.strip(), case.case_id

    def test_no_gap_marker_is_stale(self):
        """A gap that has started passing fails its suite; assert none has."""
        result = evals.run_benchmark()

        stale = [
            case.case_id
            for suite in result.suites
            for case in suite.cases
            if case.status is CaseStatus.FIXED_GAP
        ]
        assert stale == []

    def test_known_gaps_do_not_dominate_the_benchmark(self):
        result = evals.run_benchmark()

        assert len(result.known_gaps) <= result.total // 5


@pytest.mark.slow
def test_the_benchmark_entry_point_exits_zero():
    assert evals.main([]) == 0


@pytest.mark.slow
def test_every_registered_suite_runs():
    """A suite that is written but not registered measures nothing."""
    from evals.suites import SUITES

    result = evals.run_benchmark()

    assert sorted(s.name for s in result.suites) == sorted(SUITES)
    assert all(suite.total for suite in result.suites)
