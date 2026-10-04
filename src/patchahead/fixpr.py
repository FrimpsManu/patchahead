"""Open a verified migration as a pull request: ``python -m patchahead.fixpr``.

Runs after :mod:`patchahead.ci`, and only when that run ended in a verified,
complete migration. It commits the diff to a branch PatchAhead owns and opens a
pull request for it, or updates the one it opened before.

Where the pull request points:

- **On a pull request** (a Dependabot or Renovate bump), it targets that pull
  request's branch. Merging it adds the fix to the bump, and the bump's own
  branch is never pushed to -- the bot that owns it would rebase the commit away.
- **Anywhere else** (a push, a manual run), it targets the branch the run was on.

The branch is ``patchahead/<target>``. It is rewritten on each run, so a re-run
updates the same pull request rather than opening another, and it is never
rewritten once someone else has pushed to it.

The commit is made in a separate git worktree, so the checkout the workflow
uses is left exactly as it was. Pushing uses the credentials ``actions/checkout``
left in the repository; the GitHub API uses ``PATCHAHEAD_GITHUB_TOKEN``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from patchahead.ci import COMMENT_MARKER, NOT_COMMITTED
from patchahead.reporting import PROPOSED_NOTE

log = logging.getLogger("patchahead.fixpr")

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
BRANCH_PREFIX = "patchahead/"
#: GitHub rejects a pull-request body longer than this.
MAX_BODY = 65_000

PERMISSION_HELP = (
    "The token cannot open pull requests. Give the job `contents: write` and "
    "`pull-requests: write`, and turn on 'Allow GitHub Actions to create and approve "
    "pull requests' in the repository's Settings -> Actions -> General."
)


class FixPrError(Exception):
    """The pull request was asked for and could not be opened."""


@dataclass(frozen=True)
class Target:
    """The branch the fix is proposed against, and the commit it starts from."""

    branch: str
    sha: str
    #: The pull request that triggered the run, when there was one.
    number: int | None = None

    @property
    def fix_branch(self) -> str:
        return BRANCH_PREFIX + self.branch


def target_from(env: dict[str, str]) -> tuple[Target | None, str]:
    """Where the fix goes, or ``(None, why not)``."""
    event = {}
    if env.get("GITHUB_EVENT_PATH"):
        event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    pull_request = event.get("pull_request")
    if pull_request:
        head = pull_request.get("head") or {}
        head_repo = (head.get("repo") or {}).get("full_name", "")
        if head_repo and head_repo != env.get("GITHUB_REPOSITORY"):
            return None, (
                f"the pull request comes from the fork {head_repo}, and a fix cannot be "
                "pushed to another repository"
            )
        return Target(head["ref"], head["sha"], pull_request.get("number")), ""
    branch = env.get("GITHUB_REF_NAME", "")
    if not branch or env.get("GITHUB_REF_TYPE", "branch") != "branch":
        return None, "the run is not on a branch"
    if branch.startswith(BRANCH_PREFIX):
        return None, f"the run is on PatchAhead's own branch `{branch}`"
    return Target(branch, env.get("GITHUB_SHA", "")), ""


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------


def _git(cwd: Path | str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise FixPrError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _remote_tip(top: Path, branch: str) -> str:
    listed = _git(top, "ls-remote", "origin", f"refs/heads/{branch}").stdout.split()
    return listed[0] if listed else ""


def _have(top: Path, sha: str) -> bool:
    return _git(top, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def _fetch(top: Path, sha: str) -> None:
    if not _have(top, sha):
        _git(top, "fetch", "--no-tags", "--depth=1", "origin", sha)


def commit_fix(repo: Path, target: Target, diff: Path, message: str, scratch: Path) -> str:
    """Commit ``diff`` on top of ``target.sha`` and push it to the fix branch.

    Returns "" when the branch was pushed (or already held this fix), else why not.
    """
    top = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip())
    # The diff's paths are relative to the repository PatchAhead migrated, which
    # can be a directory inside the checkout.
    prefix = _git(repo, "rev-parse", "--show-prefix").stdout.strip()

    existing = _remote_tip(top, target.fix_branch)
    if existing:
        _fetch(top, existing)
        author = _git(top, "log", "-1", "--format=%ae", existing).stdout.strip()
        if author != BOT_EMAIL:
            return (
                f"`{target.fix_branch}` has commits from someone else ({author}); "
                "PatchAhead does not overwrite them"
            )

    _fetch(top, target.sha)
    tree = scratch / "worktree"
    _git(top, "worktree", "add", "--detach", str(tree), target.sha)
    try:
        apply = ["apply", "--index"] + ([f"--directory={prefix}"] if prefix else []) + [str(diff)]
        applied = _git(tree, *apply, check=False)
        if applied.returncode != 0:
            return (
                f"the verified patch does not apply to `{target.branch}`: {applied.stderr.strip()}"
            )
        identity = ["-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}"]
        _git(tree, *identity, "commit", "--quiet", "-m", message)
        if existing and _tree(tree, "HEAD") == _tree(tree, existing):
            log.info("%s already holds this fix", target.fix_branch)
            return ""
        lease = f"--force-with-lease=refs/heads/{target.fix_branch}:{existing}"
        _git(tree, "push", "--quiet", lease, "origin", f"HEAD:refs/heads/{target.fix_branch}")
        return ""
    finally:
        _git(top, "worktree", "remove", "--force", str(tree), check=False)


def _tree(cwd: Path, commit: str) -> str:
    return _git(cwd, "rev-parse", f"{commit}^{{tree}}").stdout.strip()


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------


class GitHub:
    """The few REST calls this needs."""

    def __init__(self, token: str, repository: str, api_url: str = "https://api.github.com"):
        self.token = token
        self.repository = repository
        self.api_url = api_url.rstrip("/")

    def request(self, method: str, path: str, body: dict | None = None):
        request = urllib.request.Request(
            f"{self.api_url}/repos/{self.repository}{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if exc.code in (401, 403, 404) or "not permitted to create" in detail:
                raise FixPrError(f"{PERMISSION_HELP} (GitHub said {exc.code}: {detail})") from exc
            raise FixPrError(f"GitHub said {exc.code}: {detail}") from exc


def open_or_update(api: GitHub, target: Target, title: str, body: str) -> dict:
    """Open the fix's pull request, or update the body of the one already open."""
    owner = api.repository.split("/")[0]
    open_ones = api.request(
        "GET", f"/pulls?state=open&head={owner}:{target.fix_branch}&base={target.branch}"
    )
    if open_ones:
        number = open_ones[0]["number"]
        return api.request("PATCH", f"/pulls/{number}", {"body": body})
    return api.request(
        "POST",
        "/pulls",
        {"title": title, "head": target.fix_branch, "base": target.branch, "body": body},
    )


def describe(target: Target, summary: str) -> tuple[str, str]:
    """The pull request's title and body."""
    if target.number:
        title = f"PatchAhead: verified migration for #{target.number}"
        lead = (
            f"PatchAhead migrated the code #{target.number} breaks, and a test that failed "
            f"before the patch passes after it. Merging this adds the fix to "
            f"`{target.branch}`."
        )
    else:
        title = f"PatchAhead: verified migration on {target.branch}"
        lead = (
            "PatchAhead migrated the code a dependency change breaks, and a test that failed "
            "before the patch passes after it."
        )
    # The summary was written for a comment on a patch nobody committed. This
    # pull request is that commit, so the lines saying otherwise go.
    summary = summary.replace(f"{COMMENT_MARKER}\n", "").replace(f"{NOT_COMMITTED}\n", "")
    summary = summary.replace(
        PROPOSED_NOTE,
        "> Committed by **PatchAhead** after the tests verified it. Review before merging.",
    )
    body = f"{lead}\n\n{summary}"
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n\n*(Summary cut short. The full report is in the run.)*"
    return title, body


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def run(environ: dict[str, str] | None = None, api: GitHub | None = None) -> dict[str, str]:
    """Open or update the fix's pull request. Returns the step outputs.

    A run that should not open one (not verified, a fork) is skipped with a
    reason. One that should and cannot raises :class:`FixPrError`.
    """
    env = dict(os.environ if environ is None else environ)
    outputs = {"pull-request-url": "", "pull-request-number": ""}

    def skip(why: str) -> dict[str, str]:
        log.info("no pull request: %s", why)
        outputs["pull-request-skipped"] = why
        return outputs

    if env.get("PATCHAHEAD_OUTCOME") != "migrated" or env.get("PATCHAHEAD_EXIT_CODE") != "0":
        return skip("the run did not end in a verified, complete migration")
    diff = env.get("PATCHAHEAD_DIFF", "")
    if not diff or not Path(diff).is_file():
        return skip("there is no patch")
    target, why = target_from(env)
    if target is None:
        return skip(why)

    summary_path = env.get("PATCHAHEAD_SUMMARY", "")
    summary = Path(summary_path).read_text(encoding="utf-8") if summary_path else ""
    title, body = describe(target, summary)

    scratch = Path(tempfile.mkdtemp(prefix="patchahead-pr-", dir=env.get("RUNNER_TEMP") or None))
    try:
        repo = Path(env.get("PATCHAHEAD_REPO") or ".")
        why = commit_fix(repo, target, Path(diff), title, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if why:
        log.warning("no pull request: %s", why)
        outputs["pull-request-skipped"] = why
        return outputs

    if api is None:
        api = GitHub(
            env.get("PATCHAHEAD_GITHUB_TOKEN", ""),
            env["GITHUB_REPOSITORY"],
            env.get("GITHUB_API_URL") or "https://api.github.com",
        )
    pull = open_or_update(api, target, title, body)
    outputs["pull-request-url"] = pull["html_url"]
    outputs["pull-request-number"] = str(pull["number"])
    log.info("pull request: %s", pull["html_url"])
    return outputs


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        outputs = run()
    except FixPrError as exc:
        log.error("%s", exc)
        return 1
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            # One line per output: git's messages can span several.
            handle.write("".join(f"{k}={' '.join(v.split())}\n" for k, v in outputs.items()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
