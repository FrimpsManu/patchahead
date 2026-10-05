"""Opening a verified migration as a pull request, against a real git remote."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from patchahead import fixpr
from patchahead.fixpr import BOT_EMAIL, FixPrError, Target, target_from

DIFF = """\
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def total(order):
-    return order["total"]
+    return order["amount"]
"""


def git(cwd, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class FakeGitHub:
    """Records calls; answers like the pulls API."""

    repository = "acme/shop"

    def __init__(self, open_pulls=()):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.open_pulls = list(open_pulls)

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return self.open_pulls
        number = self.open_pulls[0]["number"] if self.open_pulls else 7
        return {"number": number, "html_url": f"https://github.com/acme/shop/pull/{number}"}


@pytest.fixture
def checkout(tmp_path):
    """A clone of a remote whose ``bump`` branch holds the service in ``svc/``."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    git(tmp_path, "clone", "--quiet", str(remote), str(work))
    git(work, "config", "user.name", "dev")
    git(work, "config", "user.email", "dev@example.invalid")
    (work / "svc").mkdir()
    (work / "svc" / "app.py").write_text('def total(order):\n    return order["total"]\n')
    git(work, "add", "-A")
    git(work, "commit", "--quiet", "-m", "service")
    git(work, "push", "--quiet", "origin", "HEAD:refs/heads/bump")
    return work, remote, git(work, "rev-parse", "HEAD")


def environment(tmp_path, work, sha, **overrides):
    diff = tmp_path / "patchahead.diff"
    diff.write_text(DIFF)
    summary = tmp_path / "summary.md"
    summary.write_text("<!-- patchahead -->\n## PatchAhead: verified migration\n")
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "pull_request": {
                    "number": 12,
                    "head": {"ref": "bump", "sha": sha, "repo": {"full_name": "acme/shop"}},
                }
            }
        )
    )
    env = {
        "PATCHAHEAD_REPO": str(work / "svc"),
        "PATCHAHEAD_OUTCOME": "migrated",
        "PATCHAHEAD_EXIT_CODE": "0",
        "PATCHAHEAD_DIFF": str(diff),
        "PATCHAHEAD_SUMMARY": str(summary),
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": "acme/shop",
        "RUNNER_TEMP": str(tmp_path),
    }
    env.update(overrides)
    return env


class TestWhereTheFixGoes:
    def test_a_pull_request_is_fixed_on_its_own_branch(self, tmp_path):
        event = tmp_path / "e.json"
        event.write_text(
            json.dumps(
                {
                    "pull_request": {
                        "number": 3,
                        "head": {"ref": "dependabot/pip/sdk-2.0", "sha": "abc"},
                    }
                }
            )
        )

        target, _ = target_from({"GITHUB_EVENT_PATH": str(event)})

        assert target == Target("dependabot/pip/sdk-2.0", "abc", 3)
        assert target.fix_branch == "patchahead/dependabot/pip/sdk-2.0"

    def test_a_fork_is_skipped(self, tmp_path):
        event = tmp_path / "e.json"
        head = {"ref": "main", "sha": "abc", "repo": {"full_name": "someone/shop"}}
        event.write_text(json.dumps({"pull_request": {"number": 3, "head": head}}))

        target, why = target_from(
            {"GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "acme/shop"}
        )

        assert target is None and "fork" in why

    def test_a_push_is_fixed_on_the_branch_it_ran_on(self):
        target, _ = target_from({"GITHUB_REF_NAME": "main", "GITHUB_SHA": "abc"})

        assert target == Target("main", "abc")

    def test_patchaheads_own_branch_is_never_a_target(self):
        target, why = target_from({"GITHUB_REF_NAME": "patchahead/main", "GITHUB_SHA": "abc"})

        assert target is None and "own branch" in why


class TestOpeningThePullRequest:
    def test_a_verified_migration_is_pushed_and_opened(self, tmp_path, checkout):
        work, remote, sha = checkout
        api = FakeGitHub()

        outputs = fixpr.run(environment(tmp_path, work, sha), api=api)

        assert outputs["pull-request-url"] == "https://github.com/acme/shop/pull/7"
        fixed = git(remote, "show", "patchahead/bump:svc/app.py")
        assert 'order["amount"]' in fixed
        assert git(remote, "rev-parse", "patchahead/bump~1") == sha
        assert git(remote, "log", "-1", "--format=%ae", "patchahead/bump") == BOT_EMAIL
        method, path, body = api.calls[-1]
        assert (method, path) == ("POST", "/pulls")
        assert body["base"] == "bump" and body["head"] == "patchahead/bump"
        assert "#12" in body["title"] and "verified migration" in body["body"]

    def test_the_description_does_not_say_nothing_was_committed(self):
        from patchahead.ci import COMMENT_MARKER, NOT_COMMITTED
        from patchahead.reporting import PROPOSED_NOTE

        summary = f"{COMMENT_MARKER}\n## PatchAhead: verified migration\n\n{NOT_COMMITTED}\n"
        summary += f"Read from the release notes.\n\n# Migrate\n\n{PROPOSED_NOTE}\n"

        _, body = fixpr.describe(Target("bump", "abc", 12), summary)

        assert "nothing was committed" not in body and "not applied" not in body
        assert COMMENT_MARKER not in body
        assert "Committed by **PatchAhead**" in body and "Read from the release notes." in body

    def test_the_summary_then_says_where_the_fix_went(self, tmp_path, checkout):
        """The comment posted after this step links to the fix's pull request."""
        from patchahead.ci import NOT_COMMITTED

        work, _, sha = checkout
        env = environment(tmp_path, work, sha)
        summary = Path(env["PATCHAHEAD_SUMMARY"])
        summary.write_text(f"## PatchAhead: verified migration\n\n{NOT_COMMITTED}\n")

        fixpr.run(env, api=FakeGitHub())

        text = summary.read_text()
        assert "nothing was committed" not in text
        assert "opened it as #7" in text and "`bump`" in text

    def test_the_checkout_is_left_alone(self, tmp_path, checkout):
        work, _, sha = checkout

        fixpr.run(environment(tmp_path, work, sha), api=FakeGitHub())

        assert git(work, "status", "--porcelain") == ""
        assert git(work, "worktree", "list").count("\n") == 0

    def test_a_rerun_updates_the_open_pull_request(self, tmp_path, checkout):
        work, remote, sha = checkout
        fixpr.run(environment(tmp_path, work, sha), api=FakeGitHub())
        first = git(remote, "rev-parse", "patchahead/bump")
        api = FakeGitHub(open_pulls=[{"number": 7}])

        fixpr.run(environment(tmp_path, work, sha), api=api)

        assert git(remote, "rev-parse", "patchahead/bump") == first  # same fix, no push
        assert [c[0] for c in api.calls] == ["GET", "PATCH"]

    def test_someone_elses_commits_on_the_branch_are_not_overwritten(self, tmp_path, checkout):
        work, remote, sha = checkout
        git(work, "commit", "--quiet", "--allow-empty", "-m", "a human fix")
        git(work, "push", "--quiet", "origin", "HEAD:refs/heads/patchahead/bump")
        before = git(remote, "rev-parse", "patchahead/bump")
        api = FakeGitHub()

        outputs = fixpr.run(environment(tmp_path, work, sha), api=api)

        assert "does not overwrite" in outputs["pull-request-skipped"]
        assert git(remote, "rev-parse", "patchahead/bump") == before
        assert api.calls == []

    def test_a_patch_that_does_not_apply_is_reported(self, tmp_path, checkout):
        work, _, sha = checkout
        env = environment(tmp_path, work, sha)
        Path(env["PATCHAHEAD_DIFF"]).write_text(DIFF.replace('order["total"]', 'x["total"]'))

        outputs = fixpr.run(env, api=FakeGitHub())

        assert "does not apply" in outputs["pull-request-skipped"]

    @pytest.mark.parametrize(
        "overrides",
        [{"PATCHAHEAD_OUTCOME": "patched_unverified"}, {"PATCHAHEAD_EXIT_CODE": "1"}],
    )
    def test_only_a_verified_complete_migration_is_opened(self, tmp_path, checkout, overrides):
        work, remote, sha = checkout
        api = FakeGitHub()

        outputs = fixpr.run(environment(tmp_path, work, sha, **overrides), api=api)

        assert "verified" in outputs["pull-request-skipped"]
        assert api.calls == []
        assert "patchahead/bump" not in git(remote, "branch", "--list")


class TestGitHubErrors:
    def test_a_missing_permission_says_how_to_grant_it(self, monkeypatch):
        import io
        import urllib.error

        def refuse(*args, **kwargs):
            raise urllib.error.HTTPError(
                "u", 403, "Forbidden", {}, io.BytesIO(b"GitHub Actions is not permitted to create")
            )

        monkeypatch.setattr(fixpr.urllib.request, "urlopen", refuse)

        with pytest.raises(FixPrError, match="pull-requests: write"):
            fixpr.GitHub("t", "acme/shop").request("POST", "/pulls", {})
