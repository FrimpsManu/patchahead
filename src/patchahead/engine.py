"""The orchestrator: the one code path the CLI, the web UI, and the tests share.

Two entry points:

:func:`analyze`
    Parse the change document, index the repository, run the matching handler's
    analysis, and return an :class:`~patchahead.domain.result.AnalysisResult`.
    Read-only -- nothing is copied, nothing is executed, nothing is written.

:func:`migrate`
    Analysis, then plan, then patch **inside an isolated copy**, then validate,
    then report. The user's repository is never written to.

There is no demo-only branch anywhere in here. The bundled example repository in
``examples/`` goes through exactly this path, which is the point: if the example
works, the same code made it work for any other repository.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from patchahead import handlers
from patchahead.analysis import edits as edit_utils
from patchahead.analysis.index import RepoIndex
from patchahead.config import Config
from patchahead.domain.change import BreakingChange
from patchahead.domain.impact import ImpactGraph, ImpactReport
from patchahead.domain.patch import FileEdit, PatchProposal
from patchahead.domain.plan import MigrationPlan
from patchahead.domain.result import (
    AnalysisResult,
    MigrationResult,
    MigrationRun,
    Outcome,
)
from patchahead.domain.validation import TestRun
from patchahead.ingest import parse_file
from patchahead.observability import Timer
from patchahead.testing import discovery, runner
from patchahead.validation import ValidationEngine, ValidationOptions
from patchahead.workspace import Repository, Workspace

log = logging.getLogger(__name__)


@dataclass
class EngineOptions:
    """Everything the CLI can vary about a run."""

    #: Stop after planning; do not patch, do not run tests.
    dry_run: bool = False
    #: Let an LLM propose when a deterministic handler declines.
    use_llm: bool = False
    #: Run the gates that execute repository code.
    run_tests: bool = True
    #: Write artifacts (diff, plan, report) to the output directory.
    write_artifacts: bool = True
    #: Keep the workspace on disk so a user can inspect the patched tree.
    keep_workspace: bool = False
    #: Override the repository's configured test command.
    test_command: str = ""


def _index(repository: Repository, timer: Timer) -> RepoIndex:
    with timer.stage("index"):
        index = repository.index()
    log.info(
        "indexed %d Python file(s) under %s", index.file_count, repository.root
    )
    if index.skipped:
        log.warning(
            "%d file(s) could not be analyzed; run with -v to see why", len(index.skipped)
        )
    return index


def _load_changes(change_path: str | Path, timer: Timer) -> list[BreakingChange]:
    with timer.stage("parse_change"):
        changes = parse_file(change_path)
    log.info(
        "parsed %d breaking change(s) from %s: %s",
        len(changes),
        change_path,
        ", ".join(c.kind.value for c in changes),
    )
    return changes


def _analyze_one(
    change: BreakingChange, index: RepoIndex, config: Config, timer: Timer
) -> ImpactReport:
    """Run the matching handler's analysis, or return an unsupported report."""
    if not change.is_actionable:
        return ImpactReport(
            change=change,
            files_scanned=index.file_count,
            unsupported_reason=(
                change.classification_reason
                or f"`{change.kind.value}` changes cannot be migrated by PatchAhead v1"
            ),
        )

    handler = handlers.find_handler(change)
    if handler is None:
        return ImpactReport(
            change=change,
            files_scanned=index.file_count,
            unsupported_reason=(
                f"`{change.kind.value}` was recognized, but no handler accepted it. "
                + (
                    "The change document does not name both the old and the new "
                    "symbol, which a rename migration needs."
                    if not change.target.is_rename
                    else "This is a bug; please report it."
                )
            ),
        )

    with timer.stage(f"analyze:{change.kind.value}"):
        report = handler.analyze(change, index, config)

    report.analysis_ms = timer.durations.get(f"analyze:{change.kind.value}", 0)
    report.graph = ImpactGraph.build(
        change,
        report.findings,
        discovery.tests_by_file(index, report.affected_files),
    )
    log.info(
        "%s: %d finding(s) across %d file(s)",
        change.describe(),
        len(report.findings),
        len(report.affected_files),
    )
    return report


def analyze(
    repo_path: str | Path,
    change_path: str | Path,
    config: Config | None = None,
) -> AnalysisResult:
    """Find downstream code affected by a change document. Read-only."""
    repository = Repository.open(repo_path, config)
    timer = Timer()
    changes = _load_changes(change_path, timer)
    index = _index(repository, timer)

    result = AnalysisResult(
        repo=str(repository.root), change_document=str(change_path)
    )
    for change in changes:
        result.reports.append(_analyze_one(change, index, repository.config, timer))

    if index.skipped:
        result.warnings.append(
            f"{len(index.skipped)} file(s) were skipped and not analyzed: "
            + "; ".join(f"{path} ({why})" for path, why in list(index.skipped.items())[:3])
            + (" ..." if len(index.skipped) > 3 else "")
        )
    if not index.file_count:
        result.warnings.append(
            f"no Python files were found under {repository.root}. Check the path, "
            f"and any `source_dirs`/`exclude` settings in its PatchAhead config."
        )

    result.timings = timer.as_dict()
    return result


def migrate(
    repo_path: str | Path,
    change_path: str | Path,
    options: EngineOptions | None = None,
    config: Config | None = None,
) -> MigrationRun:
    """Analyze, plan, patch in an isolated workspace, validate, and report.

    Two phases, for a reason worth stating.

    **Phase 1 -- patch every change into one workspace.** A release note
    describing three breaking changes should produce one patched tree, not three
    that each fix a third of the problem. Each change is re-analyzed against the
    workspace as it stands, so line numbers reflect the patches already applied.

    **Phase 2 -- validate the result once.** Changes in one document are often
    interdependent: renaming ``fetch_orders`` to ``list_orders`` leaves the call
    broken until the ``timeout_seconds`` rename lands too. Validating after each
    individual patch would fail both of them for the absence of the other.
    Tests are run against the combined result, which is the state a reviewer
    would actually merge.
    """
    options = options or EngineOptions()
    repository = Repository.open(repo_path, config)
    timer = Timer()
    changes = _load_changes(change_path, timer)

    run = MigrationRun(repo=str(repository.root), change_document=str(change_path))

    if options.dry_run:
        index = _index(repository, timer)
        if index.skipped:
            run.warnings.append(f"{len(index.skipped)} file(s) were skipped and not analyzed")
        for change in changes:
            run.results.append(_plan_only(change, index, repository.config, timer))
        return run

    workspace = Workspace.materialize(repository)
    try:
        _run_migration(run, changes, workspace, repository, options, timer)
        return run
    finally:
        if options.keep_workspace:
            workspace.keep()
        else:
            workspace.cleanup()


def _run_migration(
    run: MigrationRun,
    changes: list[BreakingChange],
    workspace: Workspace,
    repository: Repository,
    options: EngineOptions,
    timer: Timer,
) -> None:
    config = repository.config
    command = options.test_command or config.test_command

    # Baselines, captured before any patch.
    full_baseline: TestRun | None = None
    if options.run_tests:
        with timer.stage("baseline_tests"):
            full_baseline = runner.run_tests(
                workspace, command, timeout=config.test_timeout_seconds
            )
        log.info("baseline full suite: %s", full_baseline.summary)

    # ---- phase 1: patch everything ------------------------------------
    patched: list[MigrationResult] = []
    for change in changes:
        index = workspace.index()
        if index.skipped and not run.warnings:
            run.warnings.append(
                f"{len(index.skipped)} file(s) were skipped and not analyzed"
            )
        result = _patch_one(change, workspace, index, repository, options, timer, full_baseline)
        run.results.append(result)
        if result.proposal is not None and result.proposal.ok:
            patched.append(result)

    if not patched:
        return

    # ---- phase 2: validate the combined result once --------------------
    combined = _combine(patched, workspace)
    targeted_baseline = full_baseline
    if options.run_tests and combined.plan.expected_tests:
        scoped = discovery.scoped_command(command, combined.plan.expected_tests)
        if scoped != command:
            # The baseline for the assertion gate must cover the same tests the
            # gate will run, measured before anything was patched. Recover it by
            # restoring the workspace, measuring, and re-applying.
            with timer.stage("baseline_tests"):
                targeted_baseline = _baseline_for(workspace, scoped, config, combined)

    with timer.stage("validate"):
        validation = ValidationEngine(config).validate(
            combined,
            workspace,
            ValidationOptions(
                run_tests=options.run_tests,
                baseline=targeted_baseline,
                full_baseline=full_baseline,
                test_command=command,
            ),
        )

    run.validation = validation
    for result in patched:
        result.validation = validation
        result.baseline_tests = targeted_baseline
        changed = len(result.proposal.changed_files) if result.proposal else 0

        if not validation.passed:
            result.outcome = Outcome.VALIDATION_FAILED
            result.message = f"migration not accepted: {validation.summary()}"
        elif not validation.tests_ran:
            # No gate objected, but nothing executed the tests either. Saying
            # "migrated" here would claim verification that did not happen.
            result.outcome = Outcome.PATCHED_UNVERIFIED
            result.message = (
                f"patched {changed} file(s), but no tests ran, so the migration is "
                f"unverified. Review the diff before applying it."
            )
        else:
            result.outcome = Outcome.MIGRATED
            result.message = f"migrated {changed} file(s); {validation.summary()}"

        result.timings = timer.as_dict()
        if options.write_artifacts:
            result.artifacts = write_artifacts(result, config)

    if len(patched) > 1:
        run.warnings.append(
            f"{len(patched)} changes were patched into one workspace and validated "
            f"together; the gate results above apply to the combined result"
        )


def _baseline_for(
    workspace: Workspace, scoped_command: str, config: Config, combined: PatchProposal
) -> TestRun:
    """Measure the targeted tests against the *pre-patch* tree, then restore.

    Needed because the targeted test set is only known once every change has
    been planned, by which point the workspace is already patched.
    """
    current = {path: workspace.read(path) for path in combined.changed_files}
    for path in combined.changed_files:
        workspace.restore(path)
    try:
        return runner.run_tests(
            workspace, scoped_command, timeout=config.test_timeout_seconds
        )
    finally:
        for path, contents in current.items():
            workspace.write(path, contents)


def _combine(results: list[MigrationResult], workspace: Workspace) -> PatchProposal:
    """Merge several accepted proposals into one, for validation and reporting."""
    first = results[0].proposal
    assert first is not None
    plan = MigrationPlan(
        change=first.plan.change,
        handler="+".join(
            dict.fromkeys(r.proposal.plan.handler for r in results if r.proposal)
        ),
        risk=max(
            (r.proposal.plan.risk for r in results if r.proposal),
            key=lambda risk: ["low", "medium", "high"].index(risk.value),
        ),
        rationale="; ".join(
            r.proposal.plan.rationale for r in results if r.proposal and r.proposal.plan.rationale
        ),
    )
    for result in results:
        proposal = result.proposal
        assert proposal is not None
        plan.transformations.extend(proposal.plan.transformations)
        for test in proposal.plan.expected_tests:
            if test not in plan.expected_tests:
                plan.expected_tests.append(test)
        plan.skipped.extend(proposal.plan.skipped)

    # One FileEdit per file, spanning every change that touched it.
    originals: dict[str, str] = {}
    for result in results:
        for file_edit in result.proposal.files:  # type: ignore[union-attr]
            originals.setdefault(file_edit.path, file_edit.old_source)

    files = [
        FileEdit(
            path=path,
            old_source=original,
            new_source=workspace.read(path),
            edit_count=sum(
                len(r.proposal.plan.edits_for(path))  # type: ignore[union-attr]
                for r in results
            ),
        )
        for path, original in sorted(originals.items())
    ]
    return PatchProposal(
        plan=plan,
        files=files,
        diff=edit_utils.combined_diff(
            [(f.path, f.old_source, f.new_source) for f in files]
        ),
        engine="+".join(dict.fromkeys(r.proposal.engine for r in results if r.proposal)),
        explanation=plan.rationale,
    )


def _plan_only(
    change: BreakingChange, index: RepoIndex, config: Config, timer: Timer
) -> MigrationResult:
    """The ``--dry-run`` path: analyze and plan, touch nothing."""
    report = _analyze_one(change, index, config, timer)
    if report.unsupported_reason:
        return MigrationResult(
            outcome=Outcome.UNSUPPORTED_CHANGE,
            impact=report,
            message=report.unsupported_reason,
            timings=timer.as_dict(),
        )
    if not report.has_impact:
        return MigrationResult(
            outcome=Outcome.NO_IMPACT,
            impact=report,
            message=f"no code uses the old contract for {change.describe()}",
            timings=timer.as_dict(),
        )

    handler = handlers.find_handler(change)
    assert handler is not None
    with timer.stage("plan"):
        plan = handler.plan(change, report, index, config)
    return MigrationResult(
        outcome=Outcome.DRY_RUN,
        impact=report,
        plan=plan,
        message=(
            f"dry run: planned {len(plan.transformations)} transformation(s) across "
            f"{len(plan.target_files)} file(s); nothing was changed"
            if not plan.blocked_reason
            else f"dry run: no migration could be planned -- {plan.blocked_reason}"
        ),
        timings=timer.as_dict(),
    )


def _patch_one(
    change: BreakingChange,
    workspace: Workspace,
    index: RepoIndex,
    repository: Repository,
    options: EngineOptions,
    timer: Timer,
    baseline: TestRun | None,
) -> MigrationResult:
    """Phase 1 for one change: analyze, plan, patch. No validation here."""
    config = repository.config
    report = _analyze_one(change, index, config, timer)

    if report.unsupported_reason:
        return MigrationResult(
            outcome=Outcome.UNSUPPORTED_CHANGE,
            impact=report,
            message=report.unsupported_reason,
            timings=timer.as_dict(),
        )
    if not report.has_impact:
        return MigrationResult(
            outcome=Outcome.NO_IMPACT,
            impact=report,
            message=(
                f"no code in {repository.root.name} uses the old contract for "
                f"{change.describe()}. Nothing to migrate."
            ),
            timings=timer.as_dict(),
        )

    handler = handlers.find_handler(change)
    assert handler is not None  # _analyze_one guarantees this
    with timer.stage("plan"):
        plan = handler.plan(change, report, index, config)

    proposal: PatchProposal
    if plan.blocked_reason:
        proposal = _llm_or_blocked(
            change, report, plan, workspace, repository, options, baseline, timer
        )
    else:
        with timer.stage("generate_patch"):
            proposal = handler.generate(plan, workspace)

    if not proposal.ok:
        outcome = Outcome.NOT_PLANNABLE if plan.blocked_reason else Outcome.PATCH_FAILED
        return MigrationResult(
            outcome=outcome,
            impact=report,
            plan=plan,
            proposal=proposal,
            message=proposal.error or "no patch could be generated",
            workspace_path=str(workspace.root) if options.keep_workspace else "",
            timings=timer.as_dict(),
        )

    return MigrationResult(
        # Provisional. Phase 2 replaces this once validation has run.
        outcome=Outcome.VALIDATION_FAILED,
        impact=report,
        plan=plan,
        proposal=proposal,
        workspace_path=str(workspace.root) if options.keep_workspace else "",
        message="patched; awaiting validation",
        timings=timer.as_dict(),
    )


def _llm_or_blocked(
    change: BreakingChange,
    report: ImpactReport,
    plan: MigrationPlan,
    workspace: Workspace,
    repository: Repository,
    options: EngineOptions,
    baseline: TestRun | None,
    timer: Timer,
) -> PatchProposal:
    """Try the LLM when a deterministic handler declined, if that is permitted."""
    config = repository.config
    permitted, refusal = config.llm_permitted(options.use_llm)

    if refusal:
        log.warning("%s", refusal)
        return PatchProposal(plan=plan, engine="deterministic", error=refusal)
    if not permitted:
        hint = (
            " Re-run with `--use-llm` to let a model propose a migration for this "
            "shape; the same validation gates still apply."
        )
        return PatchProposal(
            plan=plan, engine="deterministic", error=plan.blocked_reason + hint
        )

    from patchahead.llm import LLMProposer, available

    usable, why_not = available()
    if not usable:
        # Check before assembling a prompt, so the user gets the actionable
        # message ("no API key") rather than a failure after the work is done.
        return PatchProposal(
            plan=plan,
            engine="llm",
            error=(
                f"{plan.blocked_reason} `--use-llm` cannot run: {why_not}."
            ),
        )

    index = workspace.index()
    with timer.stage("llm_propose"):
        proposal = LLMProposer(config).propose(
            change, report, index, workspace, plan, baseline
        )
    return proposal


def write_artifacts(result: MigrationResult, config: Config) -> dict[str, str]:
    """Write the diff, the plan, and the full result to the output directory."""
    output_dir = Path(config.output_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("could not create the output directory %s: %s", output_dir, exc)
        return {}

    slug = _slug(result.impact.change)
    artifacts: dict[str, str] = {}
    files: list[tuple[str, str, str]] = []

    if result.proposal and result.proposal.diff:
        files.append(("diff", f"{slug}.diff", result.proposal.diff))
    if result.plan:
        files.append(("plan", f"{slug}.plan.json", json.dumps(result.plan.to_dict(), indent=2)))
    files.append(("result", f"{slug}.result.json", json.dumps(result.to_dict(), indent=2)))

    for key, name, content in files:
        path = output_dir / name
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            log.warning("could not write %s: %s", path, exc)
            continue
        artifacts[key] = str(path)

    log.debug("wrote %d artifact(s) to %s", len(artifacts), output_dir)
    return artifacts


def _slug(change: BreakingChange) -> str:
    """A filesystem-safe name for one change's artifacts."""
    parts = [change.kind.value]
    if change.target.symbol:
        parts.append(change.target.symbol)
    slug = "-".join(parts)
    return "".join(c if (c.isalnum() or c in "-_") else "-" for c in slug)[:80]
