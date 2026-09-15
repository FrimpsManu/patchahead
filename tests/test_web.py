"""The optional web UI consumes the same engine as the CLI.

The point of these tests is the architectural claim from ``docs/architecture.md``:
there is no demo-only business logic in the web layer. Each endpoint is checked
to return the same result objects the engine produces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CHANGES, EXAMPLE_REPO

pytest.importorskip("fastapi", reason="the web UI is an optional extra")
try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError) as exc:
    # starlette's TestClient raises RuntimeError -- not ImportError -- when its
    # HTTP client is missing, so `importorskip` cannot catch it and collection
    # fails outright. `httpx2` is in the `dev` extra; a partial install skips.
    pytest.skip(f"the web test client is unavailable: {exc}", allow_module_level=True)

from patchahead import demo, engine  # noqa: E402
from patchahead.web import server as web_server  # noqa: E402


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
        via_http = client.post("/api/analyze", params={"document": "field-rename.md"}).json()
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

    @pytest.mark.parametrize("name", ["../../etc/passwd", "../../pyproject.toml", "/etc/passwd"])
    def test_path_traversal_is_refused(self, client, name):
        response = client.post("/api/analyze", params={"document": name})

        assert response.status_code == 404


@pytest.fixture
def demo_client():
    """The app as ``patchahead demo`` builds it: fixtures plus scenarios."""
    app = web_server.create_app(demo.repo_root(), demo.changes_root(), demo.scenarios())
    return TestClient(app)


class TestDemoMode:
    """Scenarios are presentation only. They must not become a second engine."""

    def test_context_advertises_the_bundled_scenarios(self, demo_client):
        data = demo_client.get("/api/context").json()

        assert data["demo"] is True
        assert [s["id"] for s in data["scenarios"]] == [s.id for s in demo.scenarios()]
        assert all(s["headline"] and s["watch_for"] for s in data["scenarios"])

    def test_a_plain_instance_advertises_no_scenarios(self, client):
        data = client.get("/api/context").json()

        assert data["demo"] is False
        assert data["scenarios"] == []

    def test_the_raw_release_note_is_readable(self, demo_client):
        data = demo_client.get("/api/document", params={"document": "field-rename.md"}).json()

        assert "total" in data["text"]
        assert data["name"] == "field-rename.md"

    def test_the_document_endpoint_refuses_traversal(self, demo_client):
        response = demo_client.get("/api/document", params={"document": "../../pyproject.toml"})

        assert response.status_code == 404

    def test_an_unknown_scenario_is_a_404(self, demo_client):
        response = demo_client.post(
            "/api/migrate", params={"document": "field-rename.md", "scenario": "nope"}
        )

        assert response.status_code == 404

    @pytest.mark.slow
    def test_a_scenario_only_chooses_a_document_and_whether_tests_run(self, demo_client):
        """The claim that the demo has no code path of its own, asserted.

        Running through the scenario parameter and running the engine directly
        with the same two inputs must produce the same diff and the same
        verdict -- otherwise something in the demo layer is deciding outcomes.
        """
        via_scenario = demo_client.post(
            "/api/migrate", params={"document": "ignored.md", "scenario": "field-rename"}
        ).json()
        direct = engine.migrate(
            demo.repo_root(),
            demo.find("field-rename").change_path,
            engine.EngineOptions(write_artifacts=False),
        ).to_dict()

        assert (
            via_scenario["results"][0]["proposal"]["diff"]
            == direct["results"][0]["proposal"]["diff"]
        )
        assert via_scenario["results"][0]["outcome"] == direct["results"][0]["outcome"]
        assert via_scenario["succeeded"] == direct["succeeded"]

    @pytest.mark.slow
    def test_the_tests_off_scenario_reaches_patched_unverified(self, demo_client):
        data = demo_client.post(
            "/api/migrate", params={"document": "ignored.md", "scenario": "no-evidence"}
        ).json()

        assert data["run_tests"] is False
        assert data["results"][0]["outcome"] == "patched_unverified"
        assert data["succeeded"] is False

    @pytest.mark.slow
    def test_the_refusal_scenario_reports_sites_and_rewrites_none(self, demo_client):
        data = demo_client.post(
            "/api/migrate", params={"document": "ignored.md", "scenario": "receiver-mismatch"}
        ).json()
        result = data["results"][0]

        assert result["outcome"] == "not_plannable"
        assert result["impact"]["findings"]
        assert all(not f["patchable"] for f in result["impact"]["findings"])

    def test_the_page_is_served_from_the_package(self, demo_client):
        response = demo_client.get("/")

        assert response.status_code == 200
        assert "PatchAhead" in response.text
        assert "Upstream change" in response.text
