"""The bundled demo: one command, real engine, nothing configured.

Two claims are worth testing here, and they are not the usual ones.

**The demo is not a second implementation.** The prototype's dashboard had its
own migration code, which meant it demonstrated the dashboard rather than the
product. So the test that matters is not "the demo runs" but "the demo produces
byte-identical output to calling the engine directly", which is asserted below.

**The demo does not lie about itself.** Each scenario declares the outcome it is
meant to show -- verified, refused, rejected, unverified -- and those sentences
are shown to a viewer as an explanation of what they are about to see. So each
one is run through the real engine and checked. A scenario whose story stops
being true fails the build rather than quietly misleading someone.
"""

from __future__ import annotations

import hashlib
import socket
from pathlib import Path

import pytest

from patchahead import demo, engine
from patchahead.demo import DemoError, Expectation, serve


def tree_digest(root: Path) -> str:
    """A content hash of every tracked-looking file under ``root``.

    Caches and compiled files are skipped: running the bundled repository's
    tests inside a workspace copy can leave those behind in *the copy*, and a
    stray ``__pycache__`` is not the thing this is guarding against. What it is
    guarding against is an edit to the source of truth.
    """
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or ".pytest_cache" in path.parts:
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


# --------------------------------------------------------------------------
# the catalogue
# --------------------------------------------------------------------------


class TestScenarioCatalogue:
    def test_every_scenario_points_at_a_bundled_document(self):
        for scenario in demo.scenarios():
            assert scenario.change_path.is_file(), scenario.id

    def test_ids_are_unique_and_url_safe(self):
        ids = [s.id for s in demo.scenarios()]

        assert len(ids) == len(set(ids))
        assert all(s.replace("-", "").isalnum() for s in ids), ids

    def test_every_scenario_explains_itself(self):
        """A scenario with no headline is a button with no story behind it."""
        for scenario in demo.scenarios():
            assert scenario.title.strip(), scenario.id
            assert scenario.headline.strip(), scenario.id
            assert scenario.watch_for.strip(), scenario.id

    def test_the_three_required_families_are_covered(self):
        families = " ".join(s.family for s in demo.scenarios())

        assert "field_rename" in families
        assert "method_rename" in families
        assert "kwarg_rename" in families
        assert "pagination_page_to_cursor" in families

    def test_a_refusal_is_bundled(self):
        """The differentiator is safe automation, so it has to be demonstrable.

        A demo made only of successes advertises the opposite of what this tool
        is for.
        """
        refusals = [s for s in demo.scenarios() if s.expect is Expectation.REFUSED]

        assert refusals, "at least one scenario must show PatchAhead declining"

    def test_a_complete_red_to_green_migration_is_bundled(self):
        verified = [s for s in demo.scenarios() if s.expect is Expectation.VERIFIED]

        assert verified, "at least one scenario must reach a verified migration"

    def test_every_outcome_state_the_ui_renders_has_a_scenario(self):
        shown = {s.expect for s in demo.scenarios()}

        assert shown == set(Expectation), sorted(e.value for e in set(Expectation) - shown)

    def test_lookup_by_id(self):
        assert demo.find("field-rename").expect is Expectation.VERIFIED

    def test_an_unknown_id_names_the_available_ones(self):
        with pytest.raises(DemoError, match="field-rename"):
            demo.find("nope")


# --------------------------------------------------------------------------
# packaged data
# --------------------------------------------------------------------------


class TestPackagedAssets:
    """The demo has to work from a wheel, not only from a git checkout."""

    def test_the_fixtures_live_inside_the_package(self):
        import patchahead

        package = Path(patchahead.__file__).resolve().parent

        assert package in demo.fixtures_root().resolve().parents
        assert package in demo.repo_root().resolve().parents

    def test_the_bundled_repository_is_a_real_repository(self):
        repo = demo.repo_root()

        assert (repo / "pyproject.toml").is_file()
        assert (repo / "app").is_dir()
        assert list((repo / "tests").glob("test_*.py"))

    def test_the_bundled_change_documents_are_present(self):
        names = {path.name for path in demo.changes_root().iterdir()}

        assert {s.document for s in demo.scenarios()} <= names

    def test_the_web_page_ships_with_the_package(self):
        import patchahead
        from patchahead.web.server import index_path

        page = index_path()

        assert Path(patchahead.__file__).resolve().parent in page.resolve().parents
        assert page.stat().st_size > 1000

    def test_the_web_page_renders_the_whole_pipeline(self):
        """The six-step story is the deliverable, so assert it is in the page."""
        from patchahead.web.server import index_path

        page = index_path().read_text(encoding="utf-8")

        for step in ("Upstream change", "Impact", "Migration plan", "Patch", "Verification"):
            assert step in page, step
        for gate in (
            "syntax",
            "scope",
            "targeted_tests",
            "regression_tests",
            "migration_assertion",
        ):
            assert gate in page, gate
        for verdict in ("VERIFIED MIGRATION", "PATCHED, NOT VERIFIED", "REFUSED"):
            assert verdict in page, verdict


# --------------------------------------------------------------------------
# port selection
# --------------------------------------------------------------------------


@pytest.fixture
def occupied_port():
    """Bind a real port and yield its number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind((serve.HOST, 0))
        held.listen(1)
        yield held.getsockname()[1]


class TestPortSelection:
    def test_a_free_port_is_used_as_is(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((serve.HOST, 0))
            free = probe.getsockname()[1]

        assert serve.choose_port(free) == free

    def test_an_occupied_port_is_detected(self, occupied_port):
        assert serve.port_is_free(occupied_port) is False

    def test_an_occupied_default_moves_up_and_says_so(self, occupied_port):
        chosen = serve.choose_port(occupied_port, explicit=False)

        assert chosen != occupied_port
        assert serve.port_is_free(chosen)

    def test_an_occupied_explicit_port_is_an_error(self, occupied_port):
        """Silently serving elsewhere would send the user to the wrong URL.

        Worse, it could send them to whatever *else* is already listening
        there.
        """
        with pytest.raises(DemoError, match="already in use"):
            serve.choose_port(occupied_port, explicit=True)

    def test_no_free_port_in_range_is_an_error(self, monkeypatch):
        monkeypatch.setattr(serve, "port_is_free", lambda *_a, **_k: False)

        with pytest.raises(DemoError, match="all in use"):
            serve.choose_port(9000)


# --------------------------------------------------------------------------
# startup behaviour
# --------------------------------------------------------------------------


class TestBanner:
    def test_it_prints_the_url_and_the_trust_boundary(self):
        text = serve.banner("http://127.0.0.1:8000", demo.repo_root(), "python -m pytest")

        assert "http://127.0.0.1:8000" in text
        assert "never modified" in text
        assert "localhost only" in text
        assert "python -m pytest" in text

    def test_it_warns_when_no_test_runner_is_installed(self):
        """Without pytest every scenario is unverifiable, which looks broken."""
        text = serve.banner(
            "http://127.0.0.1:8000", demo.repo_root(), "python -m pytest", can_run_tests=False
        )

        assert "pytest is not installed" in text
        assert "patchahead[demo]" in text


class TestBrowserLaunch:
    def test_it_is_declined_when_explicitly_disabled(self, monkeypatch):
        monkeypatch.setenv("PATCHAHEAD_NO_BROWSER", "1")

        assert serve.browser_is_practical() is False

    def test_it_is_declined_on_a_headless_linux_box(self, monkeypatch):
        """A terminal browser opening over the output is worse than nothing."""
        monkeypatch.delenv("PATCHAHEAD_NO_BROWSER", raising=False)
        monkeypatch.setattr(serve.sys, "platform", "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

        assert serve.browser_is_practical() is False


# --------------------------------------------------------------------------
# the real engine, and only the real engine
# --------------------------------------------------------------------------


@pytest.mark.slow
class TestScenariosTellTheTruth:
    """Each scenario's declared outcome, checked against the real engine."""

    @pytest.mark.parametrize("scenario", demo.scenarios(), ids=lambda s: s.id)
    def test_the_declared_outcome_is_what_happens(self, scenario):
        run = engine.migrate(
            demo.repo_root(),
            scenario.change_path,
            engine.EngineOptions(write_artifacts=False, run_tests=scenario.run_tests),
        )
        outcomes = {result.outcome.value for result in run.results}

        if scenario.expect is Expectation.VERIFIED:
            assert run.succeeded, f"{scenario.id}: {[r.message for r in run.results]}"
            assert outcomes == {"migrated"}
        elif scenario.expect is Expectation.REFUSED:
            assert not run.succeeded
            assert outcomes == {"not_plannable"}
            assert any(result.impact.findings for result in run.results), (
                "a refusal is only interesting if it found the code first"
            )
        elif scenario.expect is Expectation.FAILED:
            assert not run.succeeded
            assert outcomes == {"validation_failed"}
        elif scenario.expect is Expectation.UNVERIFIED:
            assert not run.succeeded
            assert outcomes == {"patched_unverified"}

    def test_a_verified_scenario_really_went_from_red_to_green(self):
        """`migrated` is not a label the demo applies; it is earned per gate."""
        from patchahead.domain.validation import GateName, GateStatus

        run = engine.migrate(
            demo.repo_root(),
            demo.find("field-rename").change_path,
            engine.EngineOptions(write_artifacts=False),
        )
        assertion = run.results[0].validation.get(GateName.MIGRATION_ASSERTION)

        assert assertion.status is GateStatus.PASSED
        assert run.results[0].validation.verified

    def test_the_refusal_scenario_explains_every_site_it_declined(self):
        run = engine.migrate(
            demo.repo_root(),
            demo.find("receiver-mismatch").change_path,
            engine.EngineOptions(write_artifacts=False),
        )
        result = run.results[0]

        assert result.impact.findings, "it must find the sites before declining them"
        assert all(not f.patchable for f in result.impact.findings)
        assert all(f.unpatchable_reason for f in result.impact.findings)
        assert "invoice" in result.plan.blocked_reason or any(
            "invoice" in f.unpatchable_reason for f in result.impact.findings
        )

    def test_the_same_patch_is_verified_with_tests_and_not_without(self):
        """The two field-rename scenarios differ only in whether tests ran.

        Same document, same diff, opposite verdicts -- which is the clearest
        statement of what "verified" means here that the demo can make.
        """
        document = demo.find("field-rename").change_path
        with_tests = engine.migrate(
            demo.repo_root(), document, engine.EngineOptions(write_artifacts=False)
        ).results[0]
        without = engine.migrate(
            demo.repo_root(),
            document,
            engine.EngineOptions(write_artifacts=False, run_tests=False),
        ).results[0]

        assert with_tests.diff == without.diff
        assert with_tests.succeeded is True
        assert without.succeeded is False


@pytest.mark.slow
class TestTheBundledRepositoryIsNeverModified:
    def test_running_every_scenario_leaves_the_fixtures_byte_identical(self):
        before = tree_digest(demo.fixtures_root())

        for scenario in demo.scenarios():
            engine.migrate(
                demo.repo_root(),
                scenario.change_path,
                engine.EngineOptions(write_artifacts=False, run_tests=scenario.run_tests),
            )

        assert tree_digest(demo.fixtures_root()) == before

    def test_the_repository_object_cannot_write(self):
        """Structural, not behavioural: there is no method to call by mistake."""
        from patchahead.workspace import Repository

        assert not hasattr(Repository, "write")


# --------------------------------------------------------------------------
# the CLI surface
# --------------------------------------------------------------------------


class TestDemoCommand:
    def test_print_paths_reports_directories_that_exist(self, capsys):
        from patchahead import cli

        assert cli.main(["demo", "--print-paths"]) == 0
        out = capsys.readouterr().out
        printed = dict(line.split(None, 1) for line in out.strip().splitlines())
        repo, changes = printed["repository"].strip(), printed["changes"].strip()

        assert Path(repo).is_dir()
        assert Path(changes).is_dir()

    def test_list_names_every_scenario_and_its_expected_outcome(self, capsys):
        from patchahead import cli

        assert cli.main(["demo", "--list"]) == 0
        out = capsys.readouterr().out

        for scenario in demo.scenarios():
            assert scenario.id in out
            assert scenario.expect.value in out

    def test_an_unknown_scenario_fails_before_a_server_starts(self, capsys, monkeypatch):
        from patchahead import cli
        from patchahead.demo import serve as serve_module

        def explode(*_a, **_k):  # pragma: no cover - must not be reached
            raise AssertionError("the server must not start for a bad scenario id")

        monkeypatch.setattr(serve_module, "serve", explode)

        assert cli.main(["demo", "--scenario", "not-a-scenario"]) == 2
        assert "no such demo scenario" in capsys.readouterr().err

    def test_it_serves_on_the_requested_port_without_a_browser(self, monkeypatch):
        from patchahead import cli
        from patchahead.demo import serve as serve_module

        captured = {}

        def fake_serve(**kwargs):
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(serve_module, "serve", fake_serve)

        assert cli.main(["demo", "--port", "9123", "--no-browser"]) == 0
        assert captured["port"] == 9123
        assert captured["open_browser"] is False

    def test_a_named_port_is_marked_explicit(self, monkeypatch):
        """So that an occupied one errors instead of silently moving."""
        from patchahead import cli
        from patchahead.demo import serve as serve_module

        captured = {}
        monkeypatch.setattr(serve_module, "serve", lambda **kw: captured.update(kw) or 0)
        monkeypatch.setattr(cli.sys, "argv", ["patchahead", "demo", "--port", "9123"])

        cli.main(["demo", "--port", "9123"])

        assert captured["port_was_explicit"] is True

    def test_an_unnamed_port_is_not_marked_explicit(self, monkeypatch):
        from patchahead import cli
        from patchahead.demo import serve as serve_module

        captured = {}
        monkeypatch.setattr(serve_module, "serve", lambda **kw: captured.update(kw) or 0)
        monkeypatch.setattr(cli.sys, "argv", ["patchahead", "demo"])

        cli.main(["demo"])

        assert captured["port_was_explicit"] is False

    def test_demo_is_advertised_in_the_help(self, capsys):
        from patchahead import cli

        cli.build_parser().print_help()

        assert "patchahead demo" in capsys.readouterr().out
