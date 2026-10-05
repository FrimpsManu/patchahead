"""PatchAhead as a CI step: ``python -m patchahead.ci``.

Built for the pull requests Dependabot and Renovate open. Such a pull request
already says which packages moved between which versions, and usually quotes
their release notes. This module reads both:

1. **The release notes** in the pull request body (Dependabot writes them as
   HTML; the Markdown parser reads that).
2. **The library itself.** For each package the pull request bumps, the two
   versions are compared with :mod:`patchahead.apidiff`. That finds renames the
   notes never mention -- and it checks the notes: a rename to a name the new
   version does not have is a misreading, and is dropped with a note.
3. **An OpenAPI spec kept in the repository.** For each spec file the workflow
   names that this pull request changed, its version on the base branch is
   compared with this branch's using :mod:`patchahead.openapi`.

The changes are combined into one change document, migrated by the ordinary
engine in a temporary copy, and reported the way GitHub Actions expects: step
outputs in ``$GITHUB_OUTPUT``, a summary in ``$GITHUB_STEP_SUMMARY``, and the
combined diff and pull-request summary as files. It never writes to the
repository and never fails the step for a verdict -- ``action.yml`` decides
that from the outputs, according to the workflow's ``fail-on``.

Configuration comes from ``PATCHAHEAD_*`` environment variables, set by
``action.yml`` from the action's inputs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from patchahead import apidiff, engine, openapi, reporting
from patchahead.config import ConfigError
from patchahead.domain.change import BreakingChange, ChangeKind
from patchahead.ingest import IngestError, parse_file
from patchahead.ingest.base import ChangeDocument, parse_document
from patchahead.ingest.structured import change_to_mapping
from patchahead.workspace import RepositoryError, WorkspaceError

log = logging.getLogger("patchahead.ci")

#: Marks PatchAhead's pull-request comment, so a re-run updates it in place.
COMMENT_MARKER = "<!-- patchahead -->"
#: True of the comment, and not of the pull request ``patchahead.fixpr`` opens.
NOT_COMMITTED = (
    "PatchAhead proposed a patch in a temporary copy of this branch; nothing was "
    "committed. The diff and the evidence are below."
)

_VERSION = r"v?([0-9][\w.+!-]*?)\.?"
_UPGRADE_PATTERNS = (
    # Dependabot: "Bumps [storekit](https://...) from 4.9.0 to 5.0.0."
    re.compile(rf"Bumps \[([^\]]+)\]\([^)]*\) from {_VERSION} to {_VERSION}(?=\s|$)"),
    # Dependabot, grouped: "Updates `storekit` from 4.9.0 to 5.0.0"
    re.compile(rf"Updates `([^`]+)` from {_VERSION} to {_VERSION}(?=\s|$)"),
    # Dependabot title: "Bump storekit from 4.9.0 to 5.0.0"
    re.compile(rf"\b[Bb]ump ([\w.\-\[\]]+) from {_VERSION} to {_VERSION}(?=\s|$)"),
    # Renovate's table, whatever columns sit between the package and the change:
    # "| [storekit](https://...) | major | `4.9.0` -> `5.0.0` |"
    re.compile(rf"\|\s*\[([\w.\-]+)\]\([^)]*\)\s*\|[^\n]*?`{_VERSION}`\s*(?:->|→)\s*`{_VERSION}`"),
)


@dataclass(frozen=True)
class Upgrade:
    package: str
    old: str
    new: str

    def __str__(self) -> str:
        return f"{self.package} {self.old} -> {self.new}"


@dataclass
class Plan:
    """What a CI run will migrate, and what it learned getting there."""

    changes: list[BreakingChange] = field(default_factory=list)
    upgrades: list[Upgrade] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


#: Quoted material in a bot's pull request: the release notes, changelog and
#: commits it folds into ``<details>``, and ``>`` quotes. A library's notes
#: mention its *own* dependency bumps ("Bump libc from 0.2.155 to 0.2.185"),
#: which are not upgrades this pull request makes.
_QUOTED = re.compile(r"<details>.*?</details>|^\s*>.*$", re.DOTALL | re.MULTILINE | re.IGNORECASE)


def upgrades_in(*texts: str) -> list[Upgrade]:
    """The package upgrades a Dependabot or Renovate pull request describes.

    Read from the bot's own words only, never from the notes it quotes.
    """
    found: dict[str, Upgrade] = {}
    for text in texts:
        own_words = _QUOTED.sub("", text or "")
        for pattern in _UPGRADE_PATTERNS:
            for match in pattern.finditer(own_words):
                package, old, new = match.group(1), match.group(2), match.group(3)
                found.setdefault(package.lower(), Upgrade(package, old, new))
    return list(found.values())


def plan(
    *,
    change_path: str = "",
    pull_request: dict | None = None,
    compare_versions: bool = True,
    specs: list[str] | None = None,
    base: str = "",
    repo: str | Path = ".",
    scratch: Path,
) -> Plan:
    """Collect the changes to migrate from every source the run was given."""
    result = Plan()
    noted: list[BreakingChange] = []

    if change_path:
        noted.extend(parse_file(change_path))
        result.sources.append(f"the change document `{change_path}`")

    if pull_request:
        body = pull_request.get("body") or ""
        title = pull_request.get("title") or ""
        result.upgrades = upgrades_in(title, body)
        if body.strip():
            document = ChangeDocument(text=body, path="pull request body", suffix=".md")
            read = [c for c in parse_document(document) if c.kind is not ChangeKind.UNKNOWN]
            if read:
                noted.extend(read)
                result.sources.append("the release notes in this pull request")

    surfaces: list[apidiff.Surface] = []
    compared: list[BreakingChange] = []
    if compare_versions:
        for upgrade in result.upgrades:
            try:
                old = apidiff.read(apidiff.fetch(upgrade.package, upgrade.old, scratch / "old"))
                new = apidiff.read(apidiff.fetch(upgrade.package, upgrade.new, scratch / "new"))
            except apidiff.ApiDiffError as exc:
                result.notes.append(f"could not compare {upgrade}: {exc}")
                continue
            surfaces.append(new)
            diff = apidiff.compare(old, new, upgrade.package, (upgrade.old, upgrade.new))
            compared.extend(diff.changes)
            result.sources.append(f"a comparison of {upgrade}")

    if specs:
        if not base:
            result.notes.append(
                "openapi-spec is set, but this run has no pull request whose base "
                "branch it could compare the spec with"
            )
        for path in specs if base else []:
            changes, note = _compare_spec(Path(repo), path, base)
            if note:
                result.notes.append(note)
            if changes is not None:
                compared.extend(changes)
                result.sources.append(f"a comparison of `{path}` with the base branch's version")

    # A release note that renames something to a name the new version does not
    # have was misread -- or describes a different package. Either way it is
    # not something to rewrite code toward.
    for change in noted:
        missing = _absent_from(change, surfaces) if surfaces else ""
        if missing:
            result.notes.append(
                f"dropped the reading `{change.target.symbol}` -> `{missing}` from the "
                f"release notes: no `{missing}` exists in the new version's public API"
            )
            continue
        result.changes.append(change)

    known = {_identity(c) for c in result.changes}
    for change in compared:
        if _identity(change) not in known:
            known.add(_identity(change))
            result.changes.append(change)
    return result


def specs_in(value: str) -> list[str]:
    """Spec paths from the action input: one per line, or separated by commas."""
    return [part.strip() for part in re.split(r"[\n,]", value or "") if part.strip()]


def _compare_spec(repo: Path, path: str, base: str) -> tuple[list[BreakingChange] | None, str]:
    """The changes between a spec file's base-branch version and this one.

    Returns ``(None, note)`` when there is nothing to compare, and ``(None, "")``
    when the pull request did not change the file.
    """
    current = repo / path
    if not current.is_file():
        return None, f"could not compare `{path}`: no such file in the repository"
    top = _git(repo, "rev-parse", "--show-toplevel")
    if top is None:
        return None, f"could not compare `{path}`: `{repo}` is not a git checkout"
    relative = Path(os.path.relpath(current.resolve(), Path(top).resolve())).as_posix()
    if _git(repo, "cat-file", "-e", f"{base}^{{commit}}") is None:
        # A pull request checkout is shallow; the base commit may not be in it.
        _git(repo, "fetch", "--no-tags", "--depth=1", "origin", base)
    before = _git(repo, "show", f"{base}:{relative}", strip=False)
    if before is None:
        return None, f"`{path}` is new in this pull request; there is no earlier version to compare"
    after = current.read_text(encoding="utf-8")
    if before == after:
        return None, ""
    try:
        old = openapi.read(openapi.parse(before, f"{path} (base)"), f"{path} (base)")
        new = openapi.read(openapi.parse(after, path), path)
    except openapi.SpecError as exc:
        return None, f"could not compare `{path}`: {exc}"
    return openapi.compare(old, new).changes, ""


def _git(repo: Path, *args: str, strip: bool = True) -> str | None:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip() if strip else result.stdout


def _identity(change: BreakingChange) -> tuple[str, str, str, str]:
    """What makes two readings the same change. The owner is part of it: the same
    keyword renamed on two functions is two changes."""
    target = change.target
    return change.kind.value, target.symbol, target.replacement, target.owner.lower()


def _absent_from(change: BreakingChange, surfaces: list[apidiff.Surface]) -> str:
    """The new name of a rename that none of the new versions define, else ""."""
    new = change.target.replacement
    if change.kind is ChangeKind.METHOD_RENAME:
        names = {member.name for s in surfaces for member in s.public.values()}
    elif change.kind is ChangeKind.KWARG_RENAME:
        names = {p.name for s in surfaces for member in s.public.values() for p in member.params}
    else:
        # A field is data the API returns, not a name in the library's code.
        return ""
    return "" if new in names else new


def run(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Do one CI run. Returns the step outputs it also writes to ``$GITHUB_OUTPUT``."""
    env = dict(os.environ if environ is None else environ)
    out_dir = Path(env.get("RUNNER_TEMP") or tempfile.gettempdir()) / "patchahead"
    out_dir.mkdir(parents=True, exist_ok=True)

    event = {}
    if env.get("GITHUB_EVENT_PATH"):
        event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    # The spec comparison needs the base commit whether or not the pull
    # request's text is read.
    base = ((event.get("pull_request") or {}).get("base") or {}).get("sha", "")
    pull_request = None
    if _flag(env, "PATCHAHEAD_FROM_PULL_REQUEST") and env.get("GITHUB_EVENT_PATH"):
        pull_request = event.get("pull_request")
        if pull_request is None:
            log.warning("from-pull-request is set, but this run was not triggered by one")

    outputs = {"outcome": "", "succeeded": "false", "complete": "true", "exit-code": "0"}
    try:
        with tempfile.TemporaryDirectory(prefix="patchahead-ci-") as scratch:
            work = plan(
                change_path=env.get("PATCHAHEAD_CHANGE", ""),
                pull_request=pull_request,
                compare_versions=_flag(env, "PATCHAHEAD_COMPARE_VERSIONS", default=True),
                specs=specs_in(env.get("PATCHAHEAD_OPENAPI", "")),
                base=base,
                repo=env.get("PATCHAHEAD_REPO") or ".",
                scratch=Path(scratch),
            )
        if not work.changes:
            outputs["outcome"] = "nothing_to_migrate"
            summary = _summary_header(outputs["outcome"], work, None)
        else:
            changes_path = out_dir / "changes.json"
            changes_path.write_text(
                json.dumps({"changes": [change_to_mapping(c) for c in work.changes]}, indent=2),
                encoding="utf-8",
            )
            run_result = engine.migrate(
                env.get("PATCHAHEAD_REPO") or ".",
                changes_path,
                engine.EngineOptions(
                    write_artifacts=False, test_command=env.get("PATCHAHEAD_TEST_COMMAND", "")
                ),
            )
            from patchahead.cli import _migration_exit_code

            code = _migration_exit_code(
                run_result,
                require_complete=_flag(env, "PATCHAHEAD_REQUIRE_COMPLETE"),
            )
            outcome = run_result.outcome
            outputs.update(
                {
                    "outcome": outcome.value if outcome else "nothing_to_migrate",
                    "succeeded": str(run_result.succeeded).lower(),
                    "complete": str(run_result.complete).lower(),
                    "exit-code": str(code),
                    "changes": str(changes_path),
                }
            )
            if run_result.diff:
                diff_path = out_dir / "patchahead.diff"
                diff_path.write_text(run_result.diff, encoding="utf-8")
                outputs["diff"] = str(diff_path)
            summary = _summary_header(outputs["outcome"], work, run_result) + "\n\n".join(
                reporting.render_pr_markdown(result) for result in run_result.results
            )
    except (IngestError, RepositoryError, ConfigError, WorkspaceError) as exc:
        outputs.update({"outcome": "error", "exit-code": "2"})
        summary = f"## PatchAhead could not run\n\n{exc}\n"
        log.error("%s", exc)

    summary_path = out_dir / "summary.md"
    summary_path.write_text(f"{COMMENT_MARKER}\n{summary}", encoding="utf-8")
    outputs["summary"] = str(summary_path)
    _append(env.get("GITHUB_STEP_SUMMARY"), summary)
    _append(env.get("GITHUB_OUTPUT"), "".join(f"{k}={v}\n" for k, v in outputs.items()))
    return outputs


_HEADLINES = {
    "migrated": "verified migration",
    "patched_unverified": "patched, not verified",
    "validation_failed": "rejected by the tests",
    "not_plannable": "found affected code it would not rewrite",
    "patch_failed": "could not apply the patch",
    "unsupported_change": "nothing it can migrate",
    "no_impact": "no affected code",
    "nothing_to_migrate": "nothing to migrate",
}


def _summary_header(outcome: str, work: Plan, run_result) -> str:
    lines = [f"## PatchAhead: {_HEADLINES.get(outcome, outcome)}", ""]
    if run_result is not None and run_result.diff:
        lines.append(NOT_COMMITTED)
    if work.sources:
        lines.append(f"Read from {', '.join(work.sources)}.")
    elif not work.changes:
        lines.append(
            "No change document was given, and this pull request names no breaking change "
            "PatchAhead could read."
        )
    if run_result is not None and not run_result.complete:
        lines.append("Code or tests still use an old name; see each change's last section.")
    if work.notes:
        lines += ["", "**Notes**", ""] + [f"- {note}" for note in work.notes]
    return "\n".join(lines) + "\n\n"


def _flag(env: dict[str, str], name: str, default: bool = False) -> bool:
    value = env.get(name, "").strip().lower()
    return default if not value else value in ("1", "true", "yes", "on")


def _append(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    outputs = run()
    log.info("outcome: %s (exit code %s)", outputs["outcome"], outputs["exit-code"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
