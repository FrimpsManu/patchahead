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
