"""Tests for the evaluation harness itself.

The benchmark measures PatchAhead. Nothing measures the benchmark, which is a
problem, because a harness bug is invisible in exactly the way that matters: a
scoring mistake makes the numbers *better*, and better numbers do not get
investigated. The old harness passed raw dicts from JSON into the scoring code,
so a mistyped ``expect_patched`` asserted nothing and the case passed.

So the property under test here is not "the harness runs". It is:

**the benchmark is capable of failing PatchAhead.**

Every test below is a way that could stop being true.
"""

from __future__ import annotations

import json

import pytest

from evals.harness import dataset, metrics
from evals.harness.dataset import DatasetError, SiteCase
from evals.harness.result import CaseResult, CaseStatus
from evals.suites import sites

FIELD_CHANGE = {
    "kind": "field_rename",
    "symbol": "total",
    "replacement": "amount",
    "owner": "order",
}
#: Two syntactic matches on separate lines with opposite correct verdicts:
#: line 2 is the renamed field, line 3 is a different object that happens to
#: share the name. Separate lines so a `path:line` site can tell them apart.
TWO_SITES = {
    "app/a.py": (
        "def a(order, customer):\n"
        '    x = order["total"]\n'
        '    y = customer["total"]\n'
        "    return x, y\n"
    ),
}


def write_dataset(tmp_path, suite: str, cases: list[dict], description: str = "test"):
    directory = tmp_path / suite
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cases.json").write_text(
        json.dumps({"description": description, "cases": cases}), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def datasets(tmp_path, monkeypatch):
    """Point the harness at a dataset this test wrote."""
    monkeypatch.setattr(dataset, "DATASETS", tmp_path)
    return tmp_path


class TestTheBenchmarkCanFail:
    """The property the whole harness exists to have."""

    def test_a_case_expecting_a_site_that_is_not_patched_fails(self, datasets):
        """`customer["total"]` must not be patched for an `order` rename.

        The dataset here claims it should be. PatchAhead is right and the
        dataset is wrong -- and the suite must go red, because a suite that
        cannot disagree with the implementation is not measuring it.
        """
        write_dataset(
            datasets,
            "impact",
            [
                {
                    "id": "deliberately_wrong",
                    "change": FIELD_CHANGE,
                    "files": TWO_SITES,
                    "expect_patched": ["app/a.py:2", "app/a.py:3"],
                }
            ],
        )

        suite = sites.run("impact")

        assert not suite.ok
        assert suite.failures[0].case_id == "deliberately_wrong"

    def test_a_case_expecting_no_patch_where_one_happens_fails(self, datasets):
        write_dataset(
            datasets,
            "impact",
            [
                {
                    "id": "expects_nothing",
                    "change": FIELD_CHANGE,
                    "files": TWO_SITES,
                    "expect_patched": [],
                }
            ],
        )

        suite = sites.run("impact")

        assert not suite.ok
        assert suite.metrics["patch_false_positives"] == 1

    def test_a_case_requiring_text_the_patch_destroys_fails(self, datasets):
        write_dataset(
            datasets,
            "impact",
            [
                {
                    "id": "requires_the_old_name",
                    "change": FIELD_CHANGE,
                    "files": TWO_SITES,
                    "expect_patched": ["app/a.py:2"],
                    "expect_source_contains": ['order["total"]'],
                }
            ],
        )

        suite = sites.run("impact")

        assert not suite.ok
        assert suite.metrics["mangled_sources"] == 1

    def test_a_correct_dataset_passes(self, datasets):
        """The control. Without this, every test above passes vacuously."""
        write_dataset(
            datasets,
            "impact",
            [
                {
                    "id": "correct",
                    "change": FIELD_CHANGE,
                    "files": TWO_SITES,
                    "expect_patched": ["app/a.py:2"],
                    "expect_reported_only": ["app/a.py:3"],
                    "expect_source_contains": ['order["amount"]', 'customer["total"]'],
                }
            ],
        )

        suite = sites.run("impact")

        assert suite.ok, [(c.case_id, c.detail) for c in suite.failures]
        assert suite.metrics["patch_precision"] == 1.0


class TestStrictDatasetLoading:
    """A typo in a dataset must be an error, not a silently unscored key."""

    def test_an_unknown_key_is_rejected(self, datasets):
        write_dataset(
            datasets,
            "impact",
            [
                {
                    "id": "typo",
                    "change": FIELD_CHANGE,
                    "files": TWO_SITES,
                    "expect_patched": [],
                    "expected_patched": ["app/a.py:2"],
                }
            ],
        )

        with pytest.raises(DatasetError, match="unknown key"):
            dataset.load("impact", SiteCase)

    def test_a_missing_required_key_is_rejected(self, datasets):
        write_dataset(datasets, "impact", [{"id": "incomplete", "change": FIELD_CHANGE}])

        with pytest.raises(DatasetError, match="missing required key"):
            dataset.load("impact", SiteCase)

    def test_a_duplicate_case_id_is_rejected(self, datasets):
        case = {
            "id": "same",
            "change": FIELD_CHANGE,
            "files": TWO_SITES,
            "expect_patched": [],
        }
        write_dataset(datasets, "impact", [case, dict(case)])

        with pytest.raises(DatasetError, match="duplicate case id"):
            dataset.load("impact", SiteCase)

    def test_a_value_of_the_wrong_shape_is_rejected(self, datasets):
        write_dataset(
            datasets,
            "impact",
            [
                {
                    "id": "wrong_type",
                    "change": FIELD_CHANGE,
                    "files": TWO_SITES,
                    "expect_patched": "app/a.py:2",
                }
            ],
        )

        with pytest.raises(DatasetError, match="must be list"):
            dataset.load("impact", SiteCase)

    def test_an_empty_dataset_is_rejected(self, datasets):
        write_dataset(datasets, "impact", [])

        with pytest.raises(DatasetError, match="non-empty"):
            dataset.load("impact", SiteCase)

    def test_the_runner_reports_a_dataset_error_rather_than_a_score(self, datasets, capsys):
        from evals import run as runner

        write_dataset(datasets, "impact", [{"id": "incomplete"}])

        assert runner.main(["impact"]) == 2
        assert "dataset error" in capsys.readouterr().err


class TestKnownGaps:
    """A recorded limitation, and the two ways the record can go stale."""

    def test_a_failing_known_gap_does_not_fail_the_suite(self, datasets):
        write_dataset(
            datasets,
            "impact",
            [
                {
                    "id": "recorded_miss",
                    "known_gap": "needs alias analysis",
                    "change": FIELD_CHANGE,
                    "files": {
                        "app/a.py": 'def a(orders):\n    return [o["total"] for o in orders]\n'
                    },
                    "expect_patched": ["app/a.py:2"],
                }
            ],
        )

        suite = sites.run("impact")

        assert suite.ok
        assert suite.passed == 0, "a known gap is a recorded miss, never a pass"
        assert [c.case_id for c in suite.known_gaps] == ["recorded_miss"]

    def test_a_known_gap_that_starts_passing_fails_the_suite(self, datasets):
        """A closed gap with a stale marker is the benchmark lying downward."""
        write_dataset(
            datasets,
            "impact",
            [
                {
                    "id": "already_fixed",
                    "known_gap": "this no longer describes reality",
                    "change": FIELD_CHANGE,
                    "files": TWO_SITES,
                    "expect_patched": ["app/a.py:2"],
                }
            ],
        )

        suite = sites.run("impact")

        assert not suite.ok
        assert suite.cases[0].status is CaseStatus.FIXED_GAP
        assert "remove the `known_gap` marker" in suite.cases[0].detail

    def test_a_known_gap_may_not_excuse_patching_the_wrong_code(self, datasets):
        """Under-migrating is a limitation. A wrong edit is never one.

        This is the invariant that keeps ``known_gap`` from becoming a way to
        silence the false-positive metric this project is built around.
        """
        write_dataset(
            datasets,
            "impact",
            [
                {
                    "id": "gap_that_mis_patches",
                    "known_gap": "an excuse that must not work",
                    "change": FIELD_CHANGE,
                    "files": TWO_SITES,
                    "expect_patched": [],
                }
            ],
        )

        suite = sites.run("impact")

        assert not suite.ok
        assert suite.cases[0].status is CaseStatus.FAILED
        assert "may not patch the wrong code" in suite.cases[0].detail

    def test_known_gaps_are_excluded_from_the_headline_metric_and_not_from_the_honest_one(
        self, datasets
    ):
        write_dataset(
            datasets,
            "impact",
            [
                {
                    "id": "clean",
                    "change": FIELD_CHANGE,
                    "files": TWO_SITES,
                    "expect_patched": ["app/a.py:2"],
                },
                {
                    "id": "gap",
                    "known_gap": "needs alias analysis",
                    "change": FIELD_CHANGE,
                    "files": {
                        "app/b.py": 'def b(orders):\n    return [o["total"] for o in orders]\n'
                    },
                    "expect_patched": ["app/b.py:2"],
                },
            ],
        )

        suite = sites.run("impact")

        assert suite.ok
        assert suite.metrics["patch_recall"] == 1.0
        assert suite.metrics["patch_recall_including_gaps"] == 0.5


class TestCaseJudging:
    def test_problems_produce_a_failure(self):
        assert CaseResult.judge("x", ["broken"]).status is CaseStatus.FAILED

    def test_no_problems_produce_a_pass(self):
        assert CaseResult.judge("x", []).status is CaseStatus.PASSED

    def test_a_known_gap_is_not_counted_as_a_pass(self):
        result = CaseResult.judge("x", ["still broken"], known_gap="why")

        assert result.status is CaseStatus.KNOWN_GAP
        assert result.passed is False
        assert result.ok is True

    def test_a_fatal_problem_overrides_the_gap_marker(self):
        result = CaseResult.judge("x", [], known_gap="why", fatal=["patched the wrong thing"])

        assert result.status is CaseStatus.FAILED


class TestMetrics:
    def test_precision_and_recall(self):
        matrix = metrics.ConfusionMatrix(true_positives=8, false_positives=2, false_negatives=2)

        assert matrix.precision == 0.8
        assert matrix.recall == 0.8
        assert matrix.f1 == pytest.approx(0.8)

    def test_an_empty_matrix_reads_as_perfect_precision(self):
        """Patching nothing makes no wrong edits. Recall is what suffers."""
        matrix = metrics.ConfusionMatrix(false_negatives=3)

        assert matrix.precision == 1.0
        assert matrix.recall == 0.0

    def test_f1_punishes_trading_recall_for_precision(self):
        cautious = metrics.ConfusionMatrix(true_positives=1, false_negatives=9)
        balanced = metrics.ConfusionMatrix(true_positives=8, false_positives=1, false_negatives=2)

        assert cautious.precision == 1.0
        assert balanced.f1 > cautious.f1

    def test_calibration_notices_confidence_that_carries_no_information(self):
        informative = metrics.Calibration()
        for _ in range(4):
            informative.observe("high", True)
        informative.observe("low", False)
        informative.observe("low", True)

        inverted = metrics.Calibration()
        inverted.observe("high", False)
        inverted.observe("low", True)

        assert informative.monotonic()
        assert not inverted.monotonic()

    def test_calibration_ignores_levels_with_no_observations(self):
        calibration = metrics.Calibration()
        calibration.observe("high", True)

        assert calibration.monotonic()
        assert calibration.accuracy("medium") is None

    def test_a_tally_rejects_an_unknown_label(self):
        tally = metrics.Tally(("a", "b"))

        with pytest.raises(ValueError, match="unknown tally label"):
            tally.observe("c")

    def test_a_tally_reports_empty_categories(self):
        """'never seen' and 'not counted' must not look the same."""
        tally = metrics.Tally(("safe_refusal", "incorrect_migration"))
        tally.observe("safe_refusal")

        assert tally.to_dict() == {"safe_refusal": 1, "incorrect_migration": 0}

    def test_distribution_summarizes(self):
        distribution = metrics.Distribution()
        for value in (2, 4, 6, 100):
            distribution.observe(value)

        assert distribution.median == 5
        assert distribution.mean == 28


CONFTEST = (
    "import os\nimport sys\n\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
)
FIELD_DOCUMENT = (
    "### Order field renamed: `total` -> `amount`\n\n"
    "The monetary field on each `order` object was renamed.\n"
)
ONE_SITE_REPO = {
    "conftest.py": CONFTEST,
    "app/__init__.py": "",
    "app/report.py": 'def revenue(order):\n    return order["total"]\n',
}


@pytest.mark.slow
class TestAssertionsNoCurrentCaseTriggers:
    """Scoring paths that stay quiet while PatchAhead behaves.

    ``unnecessary_migration``, ``expect_untouched_files``, ``max_changed_lines``
    and ``expect_gate_detail`` all read zero on the real datasets, because
    PatchAhead does not currently do the things they catch. That is the desired
    state and also an untested one: a scoring path that has never fired is
    indistinguishable from a scoring path that cannot. These tests hand each one
    the failure it exists for.
    """

    def test_patching_a_repository_that_needed_nothing_is_caught(self, datasets):
        from evals.suites import migrations

        write_dataset(
            datasets,
            "migrations",
            [
                {
                    "id": "claims_nothing_to_do",
                    "expect_success": False,
                    "expect_no_patch": True,
                    "change": FIELD_DOCUMENT,
                    "files": ONE_SITE_REPO,
                }
            ],
        )

        suite = migrations.run()

        assert not suite.ok
        assert suite.metrics["unnecessary_migration"] == 1

    def test_editing_a_file_declared_untouched_is_caught(self, datasets):
        from evals.suites import migrations

        write_dataset(
            datasets,
            "migrations",
            [
                {
                    "id": "touches_a_file_it_should_not",
                    "expect_success": False,
                    "change": FIELD_DOCUMENT,
                    "expect_untouched_files": ["app/report.py"],
                    "files": ONE_SITE_REPO,
                }
            ],
        )

        suite = migrations.run()

        assert not suite.ok
        assert "already correct" in suite.failures[0].detail

    def test_a_patch_larger_than_the_case_allows_is_caught(self, datasets):
        from evals.suites import migrations

        write_dataset(
            datasets,
            "migrations",
            [
                {
                    "id": "over_budget",
                    "expect_success": False,
                    "change": FIELD_DOCUMENT,
                    "max_changed_lines": 0,
                    "files": ONE_SITE_REPO,
                }
            ],
        )

        suite = migrations.run()

        assert not suite.ok
        assert "above the case limit" in suite.failures[0].detail

    def test_a_gate_with_the_right_status_for_the_wrong_reason_is_caught(self, datasets):
        """`migration_assertion` SKIPPED has several distinct causes.

        "the suite was already green" and "nothing ever ran" are the same status
        and very different facts, and only the detail tells them apart.
        """
        from evals.suites import validation

        write_dataset(
            datasets,
            "validation",
            [
                {
                    "id": "right_status_wrong_reason",
                    "mode": "engine",
                    "change": FIELD_DOCUMENT,
                    "files": ONE_SITE_REPO,
                    "expect_gates": {"migration_assertion": "skipped"},
                    "expect_gate_detail": {"migration_assertion": "already passed"},
                }
            ],
        )

        suite = validation.run()

        assert not suite.ok
        assert "does not mention" in suite.failures[0].detail
