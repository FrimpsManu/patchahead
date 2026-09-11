"""End-to-end: the real engine against real repositories on disk.

Nothing is mocked here. These run the actual analyze/plan/patch/validate path,
including a real pytest subprocess, which is why most are marked ``slow``.
"""

from __future__ import annotations

import pytest

from patchahead import engine
from patchahead.domain.result import Outcome
from patchahead.workspace import Repository
from tests.conftest import EXAMPLE_CHANGES, EXAMPLE_REPO

SERVICE = {
    "pyproject.toml": """
        [tool.patchahead]
        test_command = "python -m pytest"
    """,
    "conftest.py": """
        import os
        import sys

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    """,
    "sdk.py": """
        ORDERS = [{"id": 1, "amount": 10.0}, {"id": 2, "amount": 20.0}]


        class Client:
            def get_orders(self, cursor=None, page=None):
                start = {None: 0, "c1": 1}.get(cursor, 0)
                chunk = ORDERS[start:start + 1]
                has_more = start + 1 < len(ORDERS)
                return {
                    "orders": chunk,
                    "next_cursor": "c1" if has_more else None,
                    "has_more": has_more,
                }
    """,
    "app/__init__.py": "",
    "app/reports.py": """
        LABEL = "total"


        def revenue(orders):
            return round(sum(order["total"] for order in orders), 2)
    """,
    "tests/test_reports.py": """
        from app.reports import revenue
        from sdk import Client


        def test_revenue():
            orders = Client().get_orders()["orders"]
            assert revenue(orders) == 10.0
    """,
}

FIELD_RENAME_DOC = """
    ### Order field renamed: `total` -> `amount`

    The monetary field on each `order` object was renamed.

    - **Before:** each order object had a `total` field.
    - **After:** the field is now named `amount`.
    - **Migration:** read `amount` instead of `total`.

    > Risk: HIGH
"""


class TestAnalyze:
    def test_finds_impact_without_touching_the_repository(self, make_repo, write_change):
        root = make_repo(SERVICE)
        before = {p: p.read_text() for p in root.rglob("*.py")}

        result = engine.analyze(root, write_change(FIELD_RENAME_DOC))

        assert result.has_impact
        assert result.total_findings == 1
        assert {p: p.read_text() for p in root.rglob("*.py")} == before

    def test_reports_the_impact_chain(self, make_repo, write_change):
        result = engine.analyze(make_repo(SERVICE), write_change(FIELD_RENAME_DOC))
        report = result.reports[0]

        assert report.affected_files == ["app/reports.py"]
        assert report.affected_symbols == ["revenue"]
        assert report.related_tests == ["tests/test_reports.py"]
        assert report.graph is not None
        assert "revenue" in report.graph.render()

    def test_no_impact_is_reported_as_such_not_as_an_error(self, make_repo, write_change):
        root = make_repo({"a.py": "x = 1\n"})

        result = engine.analyze(root, write_change(FIELD_RENAME_DOC))

        assert result.has_impact is False
        assert result.reports[0].unsupported_reason == ""

    def test_an_unsupported_change_says_why(self, make_repo, write_change):
        doc = "### Endpoint moved\nThe endpoint moved to /v2/orders.\n"

        result = engine.analyze(make_repo(SERVICE), write_change(doc))

        assert "cannot migrate" in result.reports[0].unsupported_reason

    def test_an_empty_repository_warns_rather_than_failing(self, make_repo, write_change):
        root = make_repo({"README.md": "# nothing here\n"})

        result = engine.analyze(root, write_change(FIELD_RENAME_DOC))

        assert result.has_impact is False
        assert any("no Python files" in w for w in result.warnings)

    def test_a_file_with_a_syntax_error_is_skipped_and_reported(self, make_repo, write_change):
        files = dict(SERVICE)
        files["app/broken.py"] = "def f(:\n"
        root = make_repo(files)

        result = engine.analyze(root, write_change(FIELD_RENAME_DOC))

        assert any("broken.py" in w for w in result.warnings)
        assert result.has_impact, "one bad file must not stop the rest of the analysis"


class TestDryRun:
    def test_produces_a_plan_and_changes_nothing(self, make_repo, write_change):
        root = make_repo(SERVICE)
        before = {p: p.read_text() for p in root.rglob("*.py")}

        run = engine.migrate(
            root, write_change(FIELD_RENAME_DOC), engine.EngineOptions(dry_run=True)
        )

        result = run.results[0]
        assert result.outcome is Outcome.DRY_RUN
        assert result.plan is not None
        assert len(result.plan.transformations) == 1
        assert result.proposal is None
        assert {p: p.read_text() for p in root.rglob("*.py")} == before

    def test_the_plan_serializes(self, make_repo, write_change):
        run = engine.migrate(
            make_repo(SERVICE), write_change(FIELD_RENAME_DOC), engine.EngineOptions(dry_run=True)
        )
        data = run.results[0].plan.to_dict()

        assert data["handler"] == "field_rename"
        assert data["transformations"][0]["new"] == '"amount"'


@pytest.mark.slow
class TestMigrate:
    def test_red_to_green_with_every_gate_passing(self, make_repo, write_change):
        run = engine.migrate(
            make_repo(SERVICE),
            write_change(FIELD_RENAME_DOC),
            engine.EngineOptions(write_artifacts=False),
        )
        result = run.results[0]

        assert result.succeeded, result.message
        assert result.outcome is Outcome.MIGRATED
        assert [g.status.value for g in result.validation.gates] == ["passed"] * 5

    def test_the_repository_is_untouched_afterwards(self, make_repo, write_change):
        root = make_repo(SERVICE)
        before = {p: p.read_text() for p in root.rglob("*.py")}

        engine.migrate(
            root, write_change(FIELD_RENAME_DOC), engine.EngineOptions(write_artifacts=False)
        )

        assert {p: p.read_text() for p in root.rglob("*.py")} == before

    def test_the_diff_is_minimal_and_leaves_unrelated_text_alone(self, make_repo, write_change):
        run = engine.migrate(
            make_repo(SERVICE),
            write_change(FIELD_RENAME_DOC),
            engine.EngineOptions(write_artifacts=False),
        )
        diff = run.results[0].diff

        assert '+    return round(sum(order["amount"] for order in orders), 2)' in diff
        assert 'LABEL = "total"' not in diff, "the unrelated constant is not in the diff"
        assert run.results[0].proposal.diff_line_count == 2

    def test_repeated_runs_are_identical(self, make_repo, write_change):
        root = make_repo(SERVICE)
        change = write_change(FIELD_RENAME_DOC)
        options = engine.EngineOptions(write_artifacts=False)

        first = engine.migrate(root, change, options)
        second = engine.migrate(root, change, options)

        assert first.results[0].diff == second.results[0].diff
        assert first.succeeded == second.succeeded is True

    def test_a_migration_that_does_not_fix_the_tests_is_not_a_success(
        self, make_repo, write_change
    ):
        """The test asserts a value the rename cannot produce, so it stays red."""
        files = dict(SERVICE)
        files["tests/test_reports.py"] = """
            from app.reports import revenue
            from sdk import Client


            def test_revenue():
                orders = Client().get_orders()["orders"]
                assert revenue(orders) == 999.0
        """
        run = engine.migrate(
            make_repo(files),
            write_change(FIELD_RENAME_DOC),
            engine.EngineOptions(write_artifacts=False),
        )
        result = run.results[0]

        assert result.succeeded is False
        assert result.outcome is Outcome.VALIDATION_FAILED
        assert result.diff, "the diff is still available for a human to look at"

    def test_no_tests_cannot_produce_a_validated_migration(self, make_repo, write_change):
        run = engine.migrate(
            make_repo(SERVICE),
            write_change(FIELD_RENAME_DOC),
            engine.EngineOptions(run_tests=False, write_artifacts=False),
        )
        result = run.results[0]

        assert result.proposal.ok, "a patch is still produced"
        assert result.outcome is Outcome.PATCHED_UNVERIFIED
        assert result.succeeded is False, "but it cannot be called verified"
        assert "unverified" in result.message

    def test_artifacts_are_written_when_asked(self, make_repo, write_change, tmp_path):
        from patchahead.config import Config

        output = tmp_path / "artifacts"
        run = engine.migrate(
            make_repo(SERVICE),
            write_change(FIELD_RENAME_DOC),
            engine.EngineOptions(),
            Config(output_dir=str(output)),
        )
        artifacts = run.results[0].artifacts

        assert set(artifacts) == {"diff", "plan", "result"}
        assert (output / "field_rename-total.diff").read_text().startswith("---")

    def test_an_unplannable_change_suggests_the_llm_without_running_it(
        self, make_repo, write_change
    ):
        files = dict(SERVICE)
        files["app/sync.py"] = """
            def sync(api, log):
                page = 1
                out = []
                while True:
                    r = api.get_orders(page=page)
                    out.extend(r["orders"])
                    log.info("page %d", page)
                    if page >= r["total_pages"]:
                        break
                    page += 1
                return out
        """
        run = engine.migrate(
            make_repo(files),
            EXAMPLE_CHANGES / "pagination-cursor.md",
            engine.EngineOptions(write_artifacts=False, run_tests=False),
        )
        result = run.results[0]

        assert result.outcome is Outcome.NOT_PLANNABLE
        assert "--use-llm" in result.message


@pytest.mark.slow
class TestMultipleChanges:
    def test_changes_in_one_document_compose_in_one_workspace(self, write_change):
        """Two interdependent changes; neither alone leaves the tests green."""
        run = engine.migrate(
            EXAMPLE_REPO,
            EXAMPLE_CHANGES / "sdk-v2.md",
            engine.EngineOptions(write_artifacts=False),
        )

        assert len(run.results) == 2
        assert run.succeeded, run.results[0].message
        assert all(r.outcome is Outcome.MIGRATED for r in run.results)

    def test_one_change_alone_correctly_fails(self):
        run = engine.migrate(
            EXAMPLE_REPO,
            EXAMPLE_CHANGES / "method-rename.md",
            engine.EngineOptions(write_artifacts=False),
        )

        assert run.succeeded is False, (
            "renaming the method without the keyword argument leaves the call broken, "
            "and validation must not paper over that"
        )


@pytest.mark.slow
class TestBundledExample:
    """The shipped example must work through the ordinary engine path."""

    @pytest.mark.parametrize(
        "document", ["pagination-cursor.md", "field-rename.md", "pagination-cursor.json"]
    )
    def test_each_single_change_example_migrates_and_validates(self, document):
        run = engine.migrate(
            EXAMPLE_REPO,
            EXAMPLE_CHANGES / document,
            engine.EngineOptions(write_artifacts=False),
        )

        assert run.succeeded, run.results[0].message

    def test_the_example_repository_is_never_modified(self):
        root = Repository.open(EXAMPLE_REPO).root
        before = {p: p.read_text() for p in root.rglob("*.py")}

        for document in ("pagination-cursor.md", "field-rename.md", "sdk-v2.md"):
            engine.migrate(
                EXAMPLE_REPO,
                EXAMPLE_CHANGES / document,
                engine.EngineOptions(write_artifacts=False),
            )

        assert {p: p.read_text() for p in root.rglob("*.py")} == before
