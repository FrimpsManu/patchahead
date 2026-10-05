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

#: What `patchahead api-diff pydantic 1.10.13 2.13.5` reads for `.dict()`.
PYDANTIC_DICT = """
    {"changes": [{
      "title": "`pydantic.main.BaseModel.dict` renamed to `model_dump`",
      "kind": "method_rename",
      "target": {"symbol": "dict", "replacement": "model_dump",
                 "owner": "BaseModel", "owner_explicit": false}
    }]}
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

    def test_the_built_in_that_shares_a_renamed_methods_name_is_not_left_over(
        self, make_repo, write_change
    ):
        """pydantic's `.dict()` -> `.model_dump()`, in code that also uses the `dict` type.

        Found by a real Dependabot run: `-> dict` was reported as a leftover use
        of the old method, which would fail a `--require-complete` run.
        """
        files = {
            "app/__init__.py": "",
            "app/pricing.py": """
                def to_payload(item) -> dict:
                    data: dict = item.dict()
                    return dict(data)
            """,
            "tests/test_pricing.py": """
                def test_shape():
                    assert isinstance({}, dict)
            """,
        }
        run, found = residuals(make_repo(files), write_change(PYDANTIC_DICT, "changes.json"))

        assert all(not r.kind.unfinished for r in found.values()), found
        assert run.complete is True

    def test_the_old_method_name_on_an_object_is_still_left_over(self, make_repo, write_change):
        files = {
            "app/__init__.py": "",
            # The call is rewritten; the bare reference after it is not.
            "app/pricing.py": "def later(item) -> dict:\n    return item.dict(), item.dict\n",
        }

        run, found = residuals(make_repo(files), write_change(PYDANTIC_DICT, "changes.json"))

        assert found[("app/pricing.py", 2)].kind is ResidualKind.CODE
        assert ("app/pricing.py", 1) not in found
        assert run.complete is False


class TestWhoseDictionary:
    """A dictionary literal with the old key is unfinished -- unless it is another object's."""

    FILES = {
        "app/__init__.py": "",
        "app/report.py": 'def revenue(orders):\n    return sum(order["total"] for order in orders)\n',
        "tests/test_report.py": """
            ORDERS = [
                {"id": "1", "total": 10, "customer": {"name": "Ada", "total": 250}},
            ]
            customer = {"name": "Bo", "total": 20}
            order = {"id": "2", "total": 5}


            def make(**kwargs):
                return kwargs


            ARGS = make(customer={"total": 1}, order={"total": 2})
        """,
    }

    def found(self, make_repo, write_change):
        _, found = residuals(make_repo(self.FILES), write_change(FIELD_RENAME_DOC))
        return {line: r.kind for (path, line), r in found.items() if path == "tests/test_report.py"}

    def test_a_dictionary_named_for_another_object_is_left_on_purpose(
        self, make_repo, write_change
    ):
        kinds = self.found(make_repo, write_change)

        # Line 2 has both: the order's own key is reported once, as unfinished.
        assert kinds[2] is ResidualKind.TEST
        assert kinds[4] is ResidualKind.OTHER_OBJECT  # customer = {...}
        assert kinds[5] is ResidualKind.TEST  # order = {...}
        # customer={...} comes first on line 12, but the order's key beside it is
        # unfinished, and unfinished work is never hidden behind a mention.
        assert kinds[12] is ResidualKind.TEST

    def test_the_customer_value_of_a_key_is_the_customers(self, make_repo, write_change):
        files = dict(
            self.FILES,
            **{"tests/test_report.py": 'C = {"customer": {"name": "Ada", "total": 250}}\n'},
        )
        _, found = residuals(make_repo(files), write_change(FIELD_RENAME_DOC))

        assert found[("tests/test_report.py", 1)].kind is ResidualKind.OTHER_OBJECT
        assert "`customer`" in found[("tests/test_report.py", 1)].reason


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
