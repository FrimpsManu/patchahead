"""The evaluation suites run as part of CI.

Evals that are never executed rot into decoration. Running them here means a
change that degrades classification accuracy or introduces an impact false
positive fails the build, rather than being noticed at the next release.

The thresholds are the *current measured* values, asserted as floors. Raise them
when the numbers improve; never lower one to make a red build green.
"""

from __future__ import annotations

import pytest

from evals import run as evals


@pytest.mark.slow
class TestEvalSuites:
    def test_classification_accuracy(self):
        suite = evals.run_classification()

        assert suite.metrics["kind_accuracy"] >= 1.0, [
            (c.case_id, c.detail) for c in suite.cases if not c.passed
        ]
        assert suite.metrics["symbol_accuracy"] >= 1.0

    def test_impact_precision_and_recall(self):
        suite = evals.run_impact()

        assert suite.metrics["false_positives"] == 0, [
            (c.case_id, c.detail) for c in suite.cases if not c.passed
        ]
        assert suite.metrics["patch_precision"] >= 1.0
        assert suite.metrics["patch_recall"] >= 1.0

    def test_migration_success_rate(self):
        suite = evals.run_migrations()

        assert suite.metrics["migration_success_rate"] >= 1.0, [
            (c.case_id, c.detail) for c in suite.cases if not c.passed
        ]

    def test_refusal_cases_are_part_of_the_migration_dataset(self):
        """A success rate measured only on cases we can do is not a measurement."""
        suite = evals.run_migrations()

        assert suite.metrics["refusal_cases"] >= 3


@pytest.mark.slow
def test_the_eval_entry_point_exits_zero():
    assert evals.main([]) == 0
