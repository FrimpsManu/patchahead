"""Replay real migrations: what PatchAhead does to the code a person migrated by hand.

For each case -- a public repository and the commit in which its maintainers
moved from pydantic 1 to 2 by hand -- this fetches that commit and its parent,
runs PatchAhead on the parent with a change document produced by
``patchahead api-diff pydantic 1.10.13 2.0`` (no release note, no hand-written
input), and compares the result with what the people did, call site by call
site:

``agree``         both renamed the call
``patchahead``    only PatchAhead renamed it. The repository's current default
                  branch is checked: if the maintainers later made the same edit,
                  PatchAhead was early, not wrong; otherwise read the site.
``person``        only the person renamed it. PatchAhead's own finding for the
                  site, if it had one, says why it declined.
``not renamed``   a call to a renamed method nobody changed

It measures editing, not verification: the repositories' tests are not run,
because installing each project's dependencies is out of scope. Needs network
access and ``git``; it is not part of the CI benchmark.

    python evals/realworld/replay.py --changes changes.json [--json]
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from patchahead import engine  # noqa: E402
from patchahead.ingest import parse_file  # noqa: E402

CASES = Path(__file__).with_name("cases.json")
#: Renames a person might also make that PatchAhead deliberately reports instead
#: of applying, because the replacement does not accept every call.
REPORTED_ONLY = {
    "json": "model_dump_json",
    "copy": "model_copy",
    "parse_raw": "model_validate_json",
}


@dataclass
class Site:
    path: str
    line: int
    name: str
    text: str
    person: bool = False
    patchahead: bool = False
    #: For a site only the person renamed: why PatchAhead declined, or that it
    #: never found the call.
    declined: str = ""
    #: For a site only PatchAhead renamed: whether today's default branch has
    #: the same edit ("made later"), still has the old call ("still old"), or
    #: no longer has the line at all ("gone").
    later: str = ""

    @property
    def verdict(self) -> str:
        if self.person and self.patchahead:
            return "agree"
        if self.patchahead:
            return "patchahead"
        if self.person:
            return "person"
        return "not renamed"


@dataclass
class Replay:
    repo: str
    commit: str
    sites: list[Site] = field(default_factory=list)
    reported_only: Counter = field(default_factory=Counter)
    error: str = ""

    def counts(self) -> Counter:
        return Counter(site.verdict for site in self.sites)


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def export(repo: Path, rev: str, dest: Path) -> None:
    dest.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "archive", rev], cwd=repo, check=True, capture_output=True
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)


def calls(line: str, name: str) -> int:
    return len(re.findall(rf"\.{re.escape(name)}\s*\(", line))


def renamed(before: str, after: str, old: str, new: str) -> bool:
    return calls(after, old) < calls(before, old) and calls(after, new) > calls(before, new)


def line_map(before: list[str], after: list[str]) -> dict[int, str]:
    """Each line of ``before`` (0-based) -> the line of ``after`` it became, if any."""
    mapped: dict[int, str] = {}
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for offset in range(i2 - i1):
                mapped[i1 + offset] = after[j1 + offset]
        elif tag == "replace":
            # Uneven blocks: pair each old line with its closest new line.
            candidates = after[j1:j2]
            for index in range(i1, i2):
                best = difflib.get_close_matches(before[index], candidates, n=1, cutoff=0.5)
                if best:
                    mapped[index] = best[0]
    return mapped


def replay(repo: str, commit: str, changes: Path, renames: dict[str, str], scratch: Path) -> Replay:
    result = Replay(repo=repo, commit=commit)
    clone = scratch / "clone"
    clone.mkdir()
    git("init", "-q", cwd=clone)
    try:
        git("fetch", "-q", "--depth", "2", f"https://github.com/{repo}", commit, cwd=clone)
    except subprocess.CalledProcessError as exc:
        result.error = f"could not fetch: {exc.stderr.strip()[-200:]}"
        return result
    export(clone, f"{commit}^", scratch / "before")
    export(clone, commit, scratch / "after")
    git("fetch", "-q", "--depth", "1", f"https://github.com/{repo}", "HEAD", cwd=clone)
    export(clone, "FETCH_HEAD", scratch / "today")

    run = engine.migrate(
        scratch / "before",
        changes,
        engine.EngineOptions(run_tests=False, write_artifacts=False),
    )
    patched: dict[str, str] = {}
    findings: dict[tuple[str, int], str] = {}
    for migration in run.results:
        for file_edit in migration.proposal.files if migration.proposal else []:
            patched[file_edit.path] = file_edit.new_source
        for finding in migration.impact.findings:
            if not finding.patchable:
                key = (finding.path, finding.reference.line)
                findings.setdefault(key, finding.unpatchable_reason or finding.reason)

    for path in sorted((scratch / "before").rglob("*.py")):
        relative = path.relative_to(scratch / "before").as_posix()
        person_path = scratch / "after" / relative
        if not person_path.exists():
            continue
        before = path.read_text(encoding="utf-8", errors="replace").splitlines()
        person = line_map(
            before, person_path.read_text(encoding="utf-8", errors="replace").splitlines()
        )
        ours = patched.get(relative)
        ours_lines = ours.splitlines() if ours is not None else before
        for index, text in enumerate(before):
            for old, new in renames.items():
                if not calls(text, old):
                    continue
                site = Site(relative, index + 1, old, text.strip())
                site.person = renamed(text, person.get(index, text), old, new)
                site.patchahead = index < len(ours_lines) and renamed(
                    text, ours_lines[index], old, new
                )
                if site.verdict == "person":
                    site.declined = findings.get((relative, index + 1), "not found")
                elif site.verdict == "patchahead":
                    site.later = _later(scratch / "today" / relative, text, ours_lines[index])
                result.sites.append(site)
            for old, new in REPORTED_ONLY.items():
                if calls(text, old) and renamed(text, person.get(index, text), old, new):
                    result.reported_only[f"{old}->{new}"] += 1
    return result


def _later(today: Path, before: str, ours: str) -> str:
    """Whether today's default branch made PatchAhead's edit too."""
    if not today.exists():
        return "gone"
    lines = {
        line.strip() for line in today.read_text(encoding="utf-8", errors="replace").splitlines()
    }
    if ours.strip() in lines:
        return "made later"
    return "still old" if before.strip() in lines else "gone"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--changes", required=True, help="change document from `patchahead api-diff`"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    changes = Path(args.changes).resolve()
    renames = {
        c.target.symbol: c.target.replacement
        for c in parse_file(changes)
        if c.kind.value == "method_rename"
    }
    replays = []
    for case in json.loads(CASES.read_text())["cases"]:
        with tempfile.TemporaryDirectory(prefix="patchahead-replay-") as scratch:
            replays.append(replay(case["repo"], case["commit"], changes, renames, Path(scratch)))

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "repo": r.repo,
                        "commit": r.commit,
                        "error": r.error,
                        "counts": dict(r.counts()),
                        "reported_only": dict(r.reported_only),
                        "disagreements": [
                            vars(s) | {"verdict": s.verdict}
                            for s in r.sites
                            if s.verdict in ("patchahead", "person")
                        ],
                    }
                    for r in replays
                ],
                indent=2,
            )
        )
        return 0

    total: Counter = Counter()
    for r in replays:
        counts = r.counts()
        total.update(counts)
        status = r.error or ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        print(f"{r.repo}@{r.commit[:10]}: {status}")
        for site in r.sites:
            if site.verdict in ("patchahead", "person"):
                why = site.later or site.declined
                print(f"    [{site.verdict:<10}] {site.path}:{site.line}  {site.text[:80]}")
                print(f"                 {why[:110]}")
        if r.reported_only:
            print(f"    person also renamed (PatchAhead reports these): {dict(r.reported_only)}")
    print(f"\ntotal: {dict(total)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
