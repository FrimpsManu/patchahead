"""After a patch: where does the old name survive, and does it matter?"""

from __future__ import annotations

import pytest

from patchahead import engine, reporting
from patchahead.cli import EXIT_NOT_MIGRATED, EXIT_OK, main
from patchahead.domain.completeness import ResidualKind
from tests.test_engine_e2e import FIELD_RENAME_DOC, SERVICE

METHOD_DOC = """
    ### Client method renamed: `fetch_orders` -> `list_orders`

    - **Before:** `client.fetch_orders(limit=10)`
"""

INCOMPLETE = {
    "app/__init__.py": "",
    "app/orders.py": """
        from sdk import fetch_orders


        def recent(client):
            return client.fetch_orders(limit=2)


        def first(client):
            return getattr(client, "fetch_orders")(limit=1)


        LABEL = "fetch_orders is gone"  # fetch_orders was renamed


        def later(client):
            return client.fetch_orders
    """,
    "tests/test_orders.py": """
        from unittest import mock


        def test_mocked():
            client = mock.Mock()
            client.fetch_orders.return_value = []
    """,
    "tests/fakes.py": """
        class FakeClient:
            def fetch_orders(self, limit):
                return []
    """,
    "settings.yaml": "orders:\n  method: fetch_orders\n",
    "NOTES.md": "Call `fetch_orders` to sync.\n",
}


def residuals(root, document):
    """Run without tests -- completeness does not need them -- and index by line."""
    run = engine.migrate(
        root, document, engine.EngineOptions(run_tests=False, write_artifacts=False)
    )
    report = run.results[0].completeness
    assert report is not None, run.results[0].message
    return run, {(r.path, r.line): r for r in report.residuals}


class TestWhatIsLeft:
    def test_every_kind_of_survivor_is_found_and_sorted_by_kind(self, make_repo, write_change):
        run, found = residuals(make_repo(INCOMPLETE), write_change(METHOD_DOC))

        assert found[("app/orders.py", 16)].kind is ResidualKind.CODE
        assert "bare method reference" in found[("app/orders.py", 16)].reason
        assert found[("app/orders.py", 9)].kind is ResidualKind.DYNAMIC
        # The mock's setup was migrated with the calls; the test's own fake,
        # which defines the old name, was left -- and is reported.
        assert ("tests/test_orders.py", 6) not in found
        assert found[("tests/fakes.py", 2)].kind is ResidualKind.TEST
        assert found[("app/orders.py", 12)].kind is ResidualKind.STRING
        assert found[("settings.yaml", 2)].kind is ResidualKind.CONFIG
        assert found[("NOTES.md", 1)].kind is ResidualKind.DOCS
        # The call site and its import were rewritten, so neither is a residual.
        assert ("app/orders.py", 1) not in found
        assert ("app/orders.py", 5) not in found
        assert run.complete is False

    def test_a_label_that_shares_the_field_name_is_only_a_mention(self, make_repo, write_change):
        """`LABEL = "total"` is not a use of the `total` field."""
        run, found = residuals(make_repo(SERVICE), write_change(FIELD_RENAME_DOC))

        assert all(not r.kind.unfinished for r in found.values()), found
        assert run.complete is True

    def test_a_payload_built_with_the_old_key_is_unfinished(self, make_repo, write_change):
        """Sending `{"total": ...}` to an API that renamed the field is a real residual."""
        files = dict(
            SERVICE,
            **{"app/payload.py": 'def build(order):\n    return {"total": order["total"]}\n'},
        )
        _, found = residuals(make_repo(files), write_change(FIELD_RENAME_DOC))

        residual = found[("app/payload.py", 2)]
        assert residual.kind is ResidualKind.CODE
        assert "dictionary" in residual.reason

    def test_another_object_with_the_same_name_is_left_on_purpose(self, make_repo, write_change):
        files = dict(
            SERVICE,
            **{"app/customers.py": 'def owed(customer):\n    return customer["total"]\n'},
        )
        run, found = residuals(make_repo(files), write_change(FIELD_RENAME_DOC))

        assert found[("app/customers.py", 2)].kind is ResidualKind.OTHER_OBJECT
        assert run.complete is True


class TestReporting:
    def test_the_terminal_lists_unfinished_work_and_counts_mentions(self, make_repo, write_change):
        run, _ = residuals(make_repo(INCOMPLETE), write_change(METHOD_DOC))

        text = "\n".join(
            reporting.render_completeness(
                run.results[0].completeness, reporting.Style(enabled=False)
            )
        )

        assert "3 place(s) still use `fetch_orders`" in text
        assert "[dynamic]" in text
        assert "mentions to review: 1 string, 1 config, 1 docs" in text

    def test_the_pull_request_summary_has_a_section_for_it(self, make_repo, write_change):
        run, _ = residuals(make_repo(INCOMPLETE), write_change(METHOD_DOC))

        markdown = reporting.render_pr_markdown(run.results[0])

        assert "## 6. What is left of the old API" in markdown
        assert "`app/orders.py:9` | dynamic" in markdown

    def test_json_carries_the_report(self, make_repo, write_change):
        run, _ = residuals(make_repo(INCOMPLETE), write_change(METHOD_DOC))

        data = run.to_dict()

        assert data["complete"] is False
        assert data["results"][0]["completeness"]["residuals"]


@pytest.mark.slow
class TestRequireComplete:
    def args(self, root, document, *extra):
        return ["migrate", "--repo", str(root), "--change", str(document), "--no-artifacts", *extra]

    def test_unfinished_work_fails_the_run_when_asked(self, make_repo, write_change):
        root, document = make_repo(INCOMPLETE), write_change(METHOD_DOC)

        assert main(self.args(root, document, "--no-tests")) == EXIT_OK
        assert main(self.args(root, document, "--no-tests", "--require-complete")) == (
            EXIT_NOT_MIGRATED
        )

    def test_a_complete_verified_migration_still_exits_zero(self, make_repo, write_change):
        code = main(
            self.args(make_repo(SERVICE), write_change(FIELD_RENAME_DOC), "--require-complete")
        )

        assert code == EXIT_OK
