"""PatchAhead as a CI step: reading a dependency pull request, and reporting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from patchahead import apidiff, ci
from patchahead.ci import Upgrade, upgrades_in
from patchahead.domain.change import ChangeKind
from tests.conftest import EXAMPLE_CHANGES, EXAMPLE_REPO, dedent


class TestReadingTheUpgrade:
    @pytest.mark.parametrize(
        "title, body",
        [
            ("Bump storekit from 4.9.0 to 5.0.0", ""),
            ("build(deps): bump storekit from 4.9.0 to 5.0.0", ""),
            ("", "Bumps [storekit](https://example.invalid) from 4.9.0 to 5.0.0.\n"),
            ("", "Updates `storekit` from 4.9.0 to 5.0.0\n"),
            ("", "| [storekit](https://example.invalid) | major | `4.9.0` -> `5.0.0` |\n"),
        ],
    )
    def test_dependabot_and_renovate_spellings(self, title, body):
        assert upgrades_in(title, body) == [Upgrade("storekit", "4.9.0", "5.0.0")]

    def test_bumps_quoted_from_the_release_notes_are_not_this_pull_requests(self):
        """Found by a real Dependabot run: pydantic's notes mention its own `libc` bump."""
        body = (
            "Bumps [pydantic](https://example.invalid) from 1.10.13 to 2.13.5.\n"
            "<details>\n<summary>Release notes</summary>\n"
            "<li>Bump libc from 0.2.155 to 0.2.185 by someone</li>\n"
            "</details>\n"
            "> Bump serde from 1.0 to 2.0\n"
        )

        assert upgrades_in("Bump pydantic from 1.10.13 to 2.13.5", body) == [
            Upgrade("pydantic", "1.10.13", "2.13.5")
        ]

    def test_a_grouped_update_lists_every_package_once(self):
        body = "Updates `a` from 1.0 to 2.0\nUpdates `b` from 0.1.0 to 0.2.0\nUpdates `a` from 1.0 to 2.0\n"

        assert upgrades_in("Bump the pip group with 2 updates", body) == [
            Upgrade("a", "1.0", "2.0"),
            Upgrade("b", "0.1.0", "0.2.0"),
        ]


OLD = {"sdk/__init__.py": "class Client:\n    def fetch_all(self, limit=10):\n        return []\n"}
NEW = {
    "sdk/__init__.py": dedent(
        """
        import warnings


        class Client:
            def list_all(self, limit=10):
                return []

            def fetch_all(self, limit=10):
                warnings.warn("use list_all instead", DeprecationWarning)
                return self.list_all(limit)
        """
    )
}

PULL_REQUEST = {
    "title": "Bump sdk from 1.0 to 2.0",
    "body": dedent(
        """
        Bumps [sdk](https://example.invalid) from 1.0 to 2.0.
        <details><summary>Release notes</summary><blockquote>
        <h3>Breaking changes</h3>
        <ul>
        <li><code>Client.fetch_all()</code> was renamed to <code>Client.list_all()</code>.</li>
        <li><code>Client.get()</code> was renamed to <code>Client.retrieve()</code>.</li>
        </ul>
        </blockquote></details>
        """
    ),
}


@pytest.fixture
def libraries(tmp_path, monkeypatch):
    """Serve the two versions of `sdk` from disk instead of PyPI."""
    roots = {}
    for version, files in (("1.0", OLD), ("2.0", NEW)):
        root = tmp_path / f"sdk-{version}"
        for relative, source in files.items():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(source, encoding="utf-8")
        roots[version] = root

    def fetch(package, version, dest):
        if package != "sdk":
            raise apidiff.ApiDiffError(f"{package}=={version} was not found on the package index")
        return roots[version]

    monkeypatch.setattr(apidiff, "fetch", fetch)
    return roots


class TestPlanning:
    def test_release_notes_and_the_library_agree_on_a_rename(self, libraries, tmp_path):
        work = ci.plan(pull_request=PULL_REQUEST, scratch=tmp_path)

        renames = [(c.target.symbol, c.target.replacement) for c in work.changes]
        assert renames.count(("fetch_all", "list_all")) == 1
        assert "a comparison of sdk 1.0 -> 2.0" in work.sources

    def test_a_release_note_reading_the_library_contradicts_is_dropped(self, libraries, tmp_path):
        """`Client.retrieve` does not exist in sdk 2.0: the note was misread, or is wrong."""
        work = ci.plan(pull_request=PULL_REQUEST, scratch=tmp_path)

        assert ("get", "retrieve") not in [
            (c.target.symbol, c.target.replacement) for c in work.changes
        ]
        assert any("`get` -> `retrieve`" in note for note in work.notes)

    def test_without_a_comparison_the_notes_are_taken_as_read(self, libraries, tmp_path):
        work = ci.plan(pull_request=PULL_REQUEST, compare_versions=False, scratch=tmp_path)

        assert ("get", "retrieve") in [
            (c.target.symbol, c.target.replacement) for c in work.changes
        ]

    def test_a_package_that_cannot_be_compared_is_noted_not_fatal(self, libraries, tmp_path):
        pull_request = {"title": "Bump other from 1 to 2", "body": PULL_REQUEST["body"]}

        work = ci.plan(pull_request=pull_request, scratch=tmp_path)

        assert any("could not compare other 1 -> 2" in note for note in work.notes)
        assert work.changes

    def test_the_same_keyword_on_two_functions_is_two_changes(self, tmp_path, monkeypatch):
        old = {
            "sdk/__init__.py": "class C:\n    def __init__(self, verify_ssl=True):\n        pass\n\n    def get(self, verify_ssl=True):\n        pass\n"
        }
        new = {
            "sdk/__init__.py": "class C:\n    def __init__(self, verify=True):\n        pass\n\n    def get(self, verify=True):\n        pass\n"
        }
        roots = {}
        for version, files in (("1", old), ("2", new)):
            root = tmp_path / version
            (root / "sdk").mkdir(parents=True)
            (root / "sdk/__init__.py").write_text(files["sdk/__init__.py"])
            roots[version] = root
        monkeypatch.setattr(apidiff, "fetch", lambda package, version, dest: roots[version])

        work = ci.plan(pull_request={"title": "Bump sdk from 1 to 2", "body": ""}, scratch=tmp_path)

        owners = sorted(c.target.owner for c in work.changes if c.kind is ChangeKind.KWARG_RENAME)
        assert owners == ["C", "get"]


def environment(tmp_path, **extra):
    env = {
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "step.md"),
    }
    env.update(extra)
    return env


def outputs_file(tmp_path):
    return dict(line.split("=", 1) for line in (tmp_path / "output.txt").read_text().splitlines())


@pytest.mark.slow
class TestRun:
    def test_a_verified_migration_writes_outputs_summary_and_diff(self, tmp_path):
        env = environment(
            tmp_path,
            PATCHAHEAD_REPO=str(EXAMPLE_REPO),
            PATCHAHEAD_CHANGE=str(EXAMPLE_CHANGES / "pagination-cursor.md"),
        )

        outputs = ci.run(env)

        assert outputs["outcome"] == "migrated"
        assert outputs_file(tmp_path)["succeeded"] == "true"
        assert "cursor = None" in Path(outputs["diff"]).read_text()
        summary = Path(outputs["summary"]).read_text()
        assert summary.startswith(ci.COMMENT_MARKER)
        assert "## PatchAhead: verified migration" in (tmp_path / "step.md").read_text()

    def test_a_pull_request_that_names_nothing_is_nothing_to_migrate(self, tmp_path):
        event = tmp_path / "event.json"
        event.write_text(json.dumps({"pull_request": {"title": "Fix typo", "body": "Typo."}}))
        env = environment(
            tmp_path,
            PATCHAHEAD_REPO=str(EXAMPLE_REPO),
            PATCHAHEAD_FROM_PULL_REQUEST="true",
            GITHUB_EVENT_PATH=str(event),
        )

        outputs = ci.run(env)

        assert outputs["outcome"] == "nothing_to_migrate"
        assert outputs["exit-code"] == "0"

    def test_a_change_document_that_cannot_be_read_is_an_error(self, tmp_path):
        env = environment(
            tmp_path,
            PATCHAHEAD_REPO=str(EXAMPLE_REPO),
            PATCHAHEAD_CHANGE=str(tmp_path / "missing.md"),
        )

        outputs = ci.run(env)

        assert (outputs["outcome"], outputs["exit-code"]) == ("error", "2")
        assert "could not run" in Path(outputs["summary"]).read_text()


def _spec(**properties):
    return json.dumps(
        {
            "openapi": "3.0.3",
            "info": {"title": "Shop", "version": "1"},
            "paths": {},
            "components": {"schemas": {"Order": {"properties": properties}}},
        },
        indent=2,
    )


def _git(cwd, *args):
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def spec_repo(tmp_path):
    """A repository whose base commit has v1 of the spec, and whose branch has v2."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "report.py").write_text(
        'def revenue(orders):\n    return sum(order["total"] for order in orders)\n'
    )
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "tests" / "test_report.py").write_text(
        "from app.report import revenue\n\n\n"
        "def test_revenue():\n"
        '    assert revenue([{"amount": 2}, {"amount": 3}]) == 5\n'
    )
    (repo / "openapi.json").write_text(_spec(total={"type": "number"}))
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "v1")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "openapi.json").write_text(_spec(amount={"type": "number"}))
    return repo, base


class TestOpenApiSpecs:
    def test_a_changed_spec_is_compared_with_the_base_branch(self, spec_repo, tmp_path):
        repo, base = spec_repo

        work = ci.plan(specs=["openapi.json"], base=base, repo=repo, scratch=tmp_path)

        [change] = work.changes
        assert change.kind is ChangeKind.FIELD_RENAME
        assert (change.target.symbol, change.target.replacement) == ("total", "amount")
        assert work.sources == ["a comparison of `openapi.json` with the base branch's version"]

    def test_an_unchanged_spec_adds_nothing(self, spec_repo, tmp_path):
        repo, base = spec_repo
        _git(repo, "checkout", "--quiet", "--", "openapi.json")

        work = ci.plan(specs=["openapi.json"], base=base, repo=repo, scratch=tmp_path)

        assert work.changes == [] and work.sources == [] and work.notes == []

    def test_a_spec_new_in_the_pull_request_has_nothing_to_compare(self, spec_repo, tmp_path):
        repo, base = spec_repo
        (repo / "billing.json").write_text(_spec(total={"type": "number"}))

        work = ci.plan(specs=["billing.json"], base=base, repo=repo, scratch=tmp_path)

        assert work.changes == []
        assert "new in this pull request" in work.notes[0]

    def test_without_a_pull_request_there_is_no_base_to_compare_with(self, spec_repo, tmp_path):
        repo, _ = spec_repo

        work = ci.plan(specs=["openapi.json"], repo=repo, scratch=tmp_path)

        assert work.changes == []
        assert "no pull request" in work.notes[0]

    def test_spec_paths_are_read_from_lines_or_commas(self):
        assert ci.specs_in("api/shop.yaml\n api/billing.json, \n") == [
            "api/shop.yaml",
            "api/billing.json",
        ]

    @pytest.mark.slow
    def test_a_spec_change_in_a_pull_request_is_migrated_and_verified(self, spec_repo, tmp_path):
        repo, base = spec_repo
        event = tmp_path / "event.json"
        event.write_text(
            json.dumps({"pull_request": {"title": "Update the spec", "base": {"sha": base}}})
        )
        env = environment(
            tmp_path,
            PATCHAHEAD_REPO=str(repo),
            PATCHAHEAD_OPENAPI="openapi.json",
            GITHUB_EVENT_PATH=str(event),
        )

        outputs = ci.run(env)

        assert outputs["outcome"] == "migrated", Path(outputs["summary"]).read_text()
        assert 'order["amount"]' in Path(outputs["diff"]).read_text()
