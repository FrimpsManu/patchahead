"""The CLI: argument handling, exit codes, and output formats."""

from __future__ import annotations

import json

import pytest

from patchahead.cli import (
    EXIT_NOT_MIGRATED,
    EXIT_OK,
    EXIT_UNSUPPORTED,
    EXIT_USAGE,
    main,
)
from tests.conftest import EXAMPLE_CHANGES, EXAMPLE_REPO
from tests.test_engine_e2e import FIELD_RENAME_DOC, SERVICE


@pytest.fixture(autouse=True)
def _no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")


class TestParsing:
    def test_no_command_prints_help(self, capsys):
        assert main([]) == EXIT_USAGE
        assert "usage: patchahead" in capsys.readouterr().out

    def test_version(self, capsys):
        with pytest.raises(SystemExit) as exit_info:
            main(["--version"])

        assert exit_info.value.code == 0
        assert "patchahead" in capsys.readouterr().out

    def test_verbosity_is_accepted_after_the_subcommand(self, make_repo, write_change):
        code = main(
            ["analyze", "--repo", str(make_repo(SERVICE)), "--change",
             str(write_change(FIELD_RENAME_DOC)), "-v"]
        )

        assert code == EXIT_OK

    def test_missing_required_argument_exits_two(self):
        with pytest.raises(SystemExit) as exit_info:
            main(["analyze", "--repo", "."])

        assert exit_info.value.code == 2


class TestHandlersCommand:
    def test_lists_every_family_and_its_limitations(self, capsys):
        assert main(["handlers"]) == EXIT_OK
        out = capsys.readouterr().out

        for name in ("field_rename", "method_rename", "kwarg_rename",
                     "pagination_page_to_cursor"):
            assert name in out
        assert "does not:" in out
        assert "docs/migrations.md" in out


class TestAnalyzeCommand:
    def test_reports_findings_and_exits_zero(self, make_repo, write_change, capsys):
        code = main(
            ["analyze", "--repo", str(make_repo(SERVICE)),
             "--change", str(write_change(FIELD_RENAME_DOC))]
        )
        out = capsys.readouterr().out

        assert code == EXIT_OK
        assert "app/reports.py" in out
        assert "1 finding(s)" in out

    def test_json_output_is_machine_readable(self, make_repo, write_change, capsys):
        main(
            ["analyze", "--repo", str(make_repo(SERVICE)),
             "--change", str(write_change(FIELD_RENAME_DOC)), "--json"]
        )
        data = json.loads(capsys.readouterr().out)

        assert data["has_impact"] is True
        assert data["reports"][0]["affected_files"] == ["app/reports.py"]

    def test_logs_go_to_stderr_so_json_on_stdout_stays_parseable(
        self, make_repo, write_change, capsys
    ):
        main(
            ["analyze", "--repo", str(make_repo(SERVICE)),
             "--change", str(write_change(FIELD_RENAME_DOC)), "--json", "-vv"]
        )
        captured = capsys.readouterr()

        json.loads(captured.out)  # must not raise
        assert captured.err, "debug logging went somewhere"

    def test_an_unsupported_change_exits_three(self, make_repo, write_change):
        code = main(
            ["analyze", "--repo", str(make_repo(SERVICE)),
             "--change", str(write_change("### Moved\nThe endpoint moved to /v2.\n"))]
        )

        assert code == EXIT_UNSUPPORTED

    def test_a_missing_repository_exits_two_without_a_traceback(self, write_change, capsys):
        code = main(
            ["analyze", "--repo", "/nonexistent/path",
             "--change", str(write_change(FIELD_RENAME_DOC))]
        )

        assert code == EXIT_USAGE
        assert "Traceback" not in capsys.readouterr().err

    def test_a_missing_change_document_exits_two(self, make_repo, capsys):
        code = main(["analyze", "--repo", str(make_repo(SERVICE)), "--change", "/nope.md"])

        assert code == EXIT_USAGE
        assert "no such change document" in capsys.readouterr().err

    def test_min_confidence_is_accepted(self, make_repo, write_change):
        code = main(
            ["analyze", "--repo", str(make_repo(SERVICE)),
             "--change", str(write_change(FIELD_RENAME_DOC)), "--min-confidence", "high"]
        )

        assert code == EXIT_OK


class TestMigrateCommand:
    def test_dry_run_exits_zero_and_changes_nothing(self, make_repo, write_change, capsys):
        root = make_repo(SERVICE)
        before = {p: p.read_text() for p in root.rglob("*.py")}

        code = main(
            ["migrate", "--repo", str(root), "--change", str(write_change(FIELD_RENAME_DOC)),
             "--dry-run"]
        )

        assert code == EXIT_OK
        assert "dry run" in capsys.readouterr().out
        assert {p: p.read_text() for p in root.rglob("*.py")} == before

    @pytest.mark.slow
    def test_a_validated_migration_exits_zero_and_prints_a_diff(
        self, make_repo, write_change, capsys, tmp_path
    ):
        code = main(
            ["migrate", "--repo", str(make_repo(SERVICE)),
             "--change", str(write_change(FIELD_RENAME_DOC)),
             "--output-dir", str(tmp_path / "out")]
        )
        out = capsys.readouterr().out

        assert code == EXIT_OK
        assert '+    return round(sum(order["amount"] for order in orders), 2)' in out
        assert "migrated" in out

    @pytest.mark.slow
    def test_a_failing_migration_exits_one(self, capsys):
        code = main(
            ["migrate", "--repo", str(EXAMPLE_REPO),
             "--change", str(EXAMPLE_CHANGES / "method-rename.md"), "--no-artifacts"]
        )

        assert code == EXIT_NOT_MIGRATED
        assert "validation_failed" in capsys.readouterr().out

    @pytest.mark.slow
    def test_no_tests_exits_zero_but_says_unverified(self, make_repo, write_change, capsys):
        code = main(
            ["migrate", "--repo", str(make_repo(SERVICE)),
             "--change", str(write_change(FIELD_RENAME_DOC)),
             "--no-tests", "--no-artifacts"]
        )

        assert code == EXIT_OK
        assert "unverified" in capsys.readouterr().out

    @pytest.mark.slow
    def test_json_output_carries_the_whole_run(self, make_repo, write_change, capsys):
        main(
            ["migrate", "--repo", str(make_repo(SERVICE)),
             "--change", str(write_change(FIELD_RENAME_DOC)), "--json", "--no-artifacts"]
        )
        data = json.loads(capsys.readouterr().out)

        assert data["succeeded"] is True
        assert data["results"][0]["proposal"]["diff"]
        assert len(data["validation"]["gates"]) == 5

    @pytest.mark.slow
    def test_pr_summary_is_written(self, make_repo, write_change, tmp_path):
        summary = tmp_path / "PR.md"
        main(
            ["migrate", "--repo", str(make_repo(SERVICE)),
             "--change", str(write_change(FIELD_RENAME_DOC)),
             "--pr-summary", str(summary), "--no-artifacts"]
        )

        text = summary.read_text()
        assert "## 1. Upstream change" in text
        assert "```diff" in text
        assert "a human approves" in text

    @pytest.mark.slow
    def test_use_llm_without_credentials_reports_the_reason(
        self, make_repo, write_change, monkeypatch, capsys
    ):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
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
        code = main(
            ["migrate", "--repo", str(make_repo(files)),
             "--change", str(EXAMPLE_CHANGES / "pagination-cursor.md"),
             "--use-llm", "--no-tests", "--no-artifacts"]
        )
        out = capsys.readouterr().out

        assert code == EXIT_NOT_MIGRATED
        assert "ANTHROPIC_API_KEY" in out or "anthropic" in out

    @pytest.mark.slow
    def test_allow_llm_false_blocks_the_llm_path(self, make_repo, capsys):
        files = dict(SERVICE)
        files["pyproject.toml"] = '[tool.patchahead]\nallow_llm = false\n'
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
        main(
            ["migrate", "--repo", str(make_repo(files)),
             "--change", str(EXAMPLE_CHANGES / "pagination-cursor.md"),
             "--use-llm", "--no-tests", "--no-artifacts"]
        )

        assert "allow_llm = false" in capsys.readouterr().out
