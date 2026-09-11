"""The optional web UI consumes the same engine as the CLI.

The point of these tests is the architectural claim from ``docs/architecture.md``:
there is no demo-only business logic in the web layer. Each endpoint is checked
to return the same result objects the engine produces.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CHANGES, EXAMPLE_REPO, REPO_ROOT

pytest.importorskip("fastapi", reason="the web UI is an optional extra")
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "web"))
import server as web_server  # noqa: E402


@pytest.fixture
def client():
    app = web_server.create_app(Path(EXAMPLE_REPO), Path(EXAMPLE_CHANGES))
    return TestClient(app)


class TestContext:
    def test_reports_what_it_is_pointed_at(self, client):
        data = client.get("/api/context").json()

        assert data["repo"].endswith("orders-service")
        assert "pagination-cursor.md" in data["documents"]
        assert data["config"]["test_command"]

    def test_lists_the_same_handlers_as_the_cli(self, client):
        from patchahead import handlers

        names = [h["name"] for h in client.get("/api/context").json()["handlers"]]

        assert names == [h.name for h in handlers.registered()]

    def test_every_handler_reports_its_limitations(self, client):
        for handler in client.get("/api/context").json()["handlers"]:
            assert handler["limitations"]


class TestIndex:
    def test_serves_the_page(self, client):
        response = client.get("/")

        assert response.status_code == 200
        assert "PatchAhead" in response.text


class TestAnalyze:
    def test_returns_the_engine_result_shape(self, client):
        data = client.post("/api/analyze", params={"document": "pagination-cursor.md"}).json()

        assert data["has_impact"] is True
        assert data["reports"][0]["affected_files"] == ["app/order_sync.py"]

    def test_matches_the_engine_called_directly(self, client):
        from patchahead import engine

        via_http = client.post(
            "/api/analyze", params={"document": "field-rename.md"}
        ).json()
        direct = engine.analyze(EXAMPLE_REPO, EXAMPLE_CHANGES / "field-rename.md").to_dict()

        assert via_http["reports"][0]["findings"] == direct["reports"][0]["findings"]


class TestMigrate:
    def test_dry_run_returns_a_plan(self, client):
        data = client.post(
            "/api/migrate", params={"document": "field-rename.md", "dry_run": True}
        ).json()

        assert data["results"][0]["outcome"] == "dry_run"
        assert data["results"][0]["plan"]["transformations"]

    @pytest.mark.slow
    def test_a_full_migration_returns_a_diff_and_gates(self, client):
        data = client.post("/api/migrate", params={"document": "field-rename.md"}).json()
        result = data["results"][0]

        assert data["succeeded"] is True
        assert result["proposal"]["diff"].startswith("---")
        assert len(result["validation"]["gates"]) == 5

    @pytest.mark.slow
    def test_includes_the_same_pr_summary_the_cli_writes(self, client):
        data = client.post("/api/migrate", params={"document": "field-rename.md"}).json()

        assert "## 1. Upstream change" in data["pr_summaries"][0]
        assert "a human approves" in data["pr_summaries"][0]

    @pytest.mark.slow
    def test_the_example_repository_is_not_modified(self, client):
        before = {p: p.read_text() for p in Path(EXAMPLE_REPO).rglob("*.py")}

        client.post("/api/migrate", params={"document": "field-rename.md"})

        assert {p: p.read_text() for p in Path(EXAMPLE_REPO).rglob("*.py")} == before


class TestSafety:
    def test_an_unknown_document_is_a_404(self, client):
        assert client.post("/api/analyze", params={"document": "nope.md"}).status_code == 404

    @pytest.mark.parametrize(
        "name", ["../../etc/passwd", "../../pyproject.toml", "/etc/passwd"]
    )
    def test_path_traversal_is_refused(self, client, name):
        response = client.post("/api/analyze", params={"document": name})

        assert response.status_code == 404
