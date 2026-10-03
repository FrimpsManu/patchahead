"""Migrating tests, without letting a rewritten test vouch for the rewrite."""

from __future__ import annotations

import pytest

from patchahead import engine
from patchahead.config import Config
from patchahead.domain.result import Outcome
from patchahead.domain.validation import GateName, GateStatus

METHOD_DOC = """
    ### Client method renamed: `fetch_orders` -> `list_orders`

    - **Before:** `client.fetch_orders(limit=10)`
"""

SDK = """
    class Client:
        def list_orders(self, limit=10):
            return [{"id": i} for i in range(limit)]
"""


def repo(make_repo, extra: dict[str, str]):
    return make_repo(
        {
            "conftest.py": "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n",
            "sdk.py": SDK,
            "app/__init__.py": "",
            "app/orders.py": """
                def recent(client):
                    return client.fetch_orders(limit=2)
            """,
            **extra,
        }
    )


def migrate(root, document, **config):
    return engine.migrate(
        root,
        document,
        engine.EngineOptions(write_artifacts=False),
        Config(test_command="python -m pytest", **config),
    ).results[0]


@pytest.mark.slow
class TestEvidence:
    def test_an_untouched_test_still_verifies_while_an_edited_one_is_migrated(
        self, make_repo, write_change
    ):
        """The app's test proves the fix; the mock test is migrated alongside it."""
        root = repo(
            make_repo,
            {
                "tests/test_orders.py": """
                    from app.orders import recent
                    from sdk import Client


                    def test_recent():
                        assert len(recent(Client())) == 2
                """,
                "tests/test_mocked.py": """
                    from unittest import mock

                    from app.orders import recent


                    def test_with_a_mock():
                        client = mock.Mock(spec=["list_orders", "fetch_orders"])
                        client.fetch_orders.return_value = []
                        assert recent(client) == []
                """,
            },
        )

        result = migrate(root, write_change(METHOD_DOC))

        assert result.outcome is Outcome.MIGRATED, result.message
        assert "client.list_orders.return_value = []" in result.diff
        assertion = result.validation.get(GateName.MIGRATION_ASSERTION)
        assert "tests/test_orders.py::test_recent" in assertion.detail

    def test_a_test_the_patch_rewrote_cannot_be_the_only_evidence(self, make_repo, write_change):
        """The only red test calls the SDK directly; renaming it makes it pass,
        which proves only that the rename agrees with itself."""
        root = repo(
            make_repo,
            {
                "tests/test_sdk.py": """
                    from sdk import Client


                    def test_orders_via_the_sdk():
                        client = Client()
                        assert len(client.fetch_orders(limit=3)) == 3
                """,
            },
        )

        result = migrate(root, write_change(METHOD_DOC))

        assert result.outcome is Outcome.PATCHED_UNVERIFIED, result.message
        assert "tests/test_sdk.py" in result.proposal.changed_files
        assertion = result.validation.get(GateName.MIGRATION_ASSERTION)
        assert assertion.status is GateStatus.SKIPPED
        assert "cannot vouch" in assertion.detail

    def test_migrate_tests_false_leaves_tests_alone(self, make_repo, write_change):
        root = repo(
            make_repo,
            {
                "tests/test_sdk.py": """
                    from sdk import Client


                    def test_orders_via_the_sdk():
                        assert Client().fetch_orders(limit=1)
                """,
            },
        )

        result = migrate(root, write_change(METHOD_DOC), migrate_tests=False)

        assert result.proposal.changed_files == ["app/orders.py"]
