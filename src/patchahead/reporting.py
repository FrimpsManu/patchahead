"""Rendering results for humans: terminal text and a review-ready Markdown PR.

Every number here comes from a result object. Nothing is computed a second time,
so what the CLI prints and what the JSON output says cannot drift apart.
"""

from __future__ import annotations

import os
from pathlib import Path

from patchahead.domain.change import BreakingChange, Confidence
from patchahead.domain.impact import ImpactReport
from patchahead.domain.result import AnalysisResult, MigrationResult, MigrationRun, Outcome
from patchahead.domain.validation import GateStatus, ValidationResult

_COLORS = {
    "bold": "1",
    "dim": "90",
    "red": "91",
    "green": "92",
    "yellow": "93",
    "cyan": "96",
}


def _use_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


class Style:
    """Colors, switched off when output is redirected or ``NO_COLOR`` is set."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, color: str) -> str:
        if not self.enabled or color not in _COLORS:
            return text
        return f"\033[{_COLORS[color]}m{text}\033[0m"

    @classmethod
    def for_stream(cls, stream) -> Style:
        return cls(_use_color(stream))


_CONFIDENCE_COLOR = {
    Confidence.HIGH: "green",
    Confidence.MEDIUM: "yellow",
    Confidence.LOW: "dim",
}

_OUTCOME_COLOR = {
    Outcome.MIGRATED: "green",
    Outcome.VALIDATION_FAILED: "red",
    Outcome.PATCHED_UNVERIFIED: "yellow",
    Outcome.PATCH_FAILED: "red",
    Outcome.NO_IMPACT: "dim",
    Outcome.UNSUPPORTED_CHANGE: "yellow",
    Outcome.NOT_PLANNABLE: "yellow",
    Outcome.DRY_RUN: "cyan",
}


def relative(path: str) -> str:
    """Shorten an absolute path against the working directory, when shorter."""
    try:
        candidate = os.path.relpath(path, Path.cwd())
    except (ValueError, OSError):
        return path
    return candidate if len(candidate) < len(path) else path


# --------------------------------------------------------------------------
# terminal rendering
# --------------------------------------------------------------------------


def render_change(change: BreakingChange, style: Style) -> list[str]:
    lines = [
        style(change.title, "bold"),
        f"  kind        {style(change.kind.value, 'cyan')}",
        f"  severity    {change.severity.value}"
        f"    confidence {change.confidence.value}",
    ]
    if change.target.is_rename:
        owner = f" on `{change.target.owner}`" if change.target.owner else ""
        lines.append(
            f"  rename      `{change.target.symbol}` -> "
            f"`{change.target.replacement}`{owner}"
        )
    if change.old_behavior:
        lines.append(f"  before      {change.old_behavior}")
    if change.new_behavior:
        lines.append(f"  after       {change.new_behavior}")
    if change.classification_reason:
        lines.append(style(f"  why         {change.classification_reason}", "dim"))
    return lines


def render_impact(report: ImpactReport, style: Style, verbose: bool = False) -> list[str]:
    if report.unsupported_reason:
        return [style(f"  unsupported: {report.unsupported_reason}", "yellow")]
    if not report.findings:
        return [style("  no affected code found", "dim")]

    lines = [
        f"  {len(report.findings)} finding(s) in "
        f"{len(report.affected_files)} file(s), "
        f"scanned {report.files_scanned} file(s) in {report.analysis_ms}ms",
    ]
    for finding in report.findings:
        marker = "+" if finding.patchable else "-"
        color = _CONFIDENCE_COLOR[finding.confidence]
        lines.append(
            f"  {style(marker, color)} {finding.reference}"
            f"  {style(finding.confidence.value, color)}"
            f"  {finding.symbol}  {finding.matched_contract}"
        )
        if verbose or not finding.patchable:
            lines.append(style(f"      {finding.reason}", "dim"))
        if finding.reference.snippet:
            lines.append(style(f"      | {finding.reference.snippet}", "dim"))

    if report.related_tests:
        lines.append(f"  relevant tests: {', '.join(report.related_tests)}")
    if report.graph and verbose:
        lines.append("")
        lines.extend(f"  {line}" for line in report.graph.render().splitlines())
    return lines


def render_validation(validation: ValidationResult, style: Style) -> list[str]:
    marks = {
        GateStatus.PASSED: (style("pass", "green"), "green"),
        GateStatus.FAILED: (style("FAIL", "red"), "red"),
        GateStatus.SKIPPED: (style("skip", "dim"), "dim"),
    }
    lines = []
    for gate in validation.gates:
        mark, _ = marks[gate.status]
        timing = f" ({gate.duration_ms}ms)" if gate.duration_ms else ""
        lines.append(f"  [{mark}] {gate.name.value:<20}{timing} {gate.detail}")
    return lines


def render_analysis(result: AnalysisResult, verbose: bool = False, stream=None) -> str:
    style = Style.for_stream(stream)
    lines = [
        style(f"repository     {relative(result.repo)}", "dim"),
        style(f"change document {relative(result.change_document)}", "dim"),
        "",
    ]
    for report in result.reports:
        lines.extend(render_change(report.change, style))
        lines.extend(render_impact(report, style, verbose))
        lines.append("")

    for warning in result.warnings:
        lines.append(style(f"warning: {warning}", "yellow"))

    if result.total_findings:
        lines.append(
            style(
                f"{result.total_findings} finding(s). Run `patchahead migrate` with "
                f"the same arguments to propose a migration.",
                "bold",
            )
        )
    else:
        lines.append(style("No affected code found.", "dim"))
    if verbose and result.timings:
        lines.append(
            style(
                "timings: "
                + ", ".join(f"{k}={v}ms" for k, v in sorted(result.timings.items())),
                "dim",
            )
        )
    return "\n".join(lines)


def render_migration(
    result: MigrationResult, verbose: bool = False, show_diff: bool = True, stream=None
) -> str:
    style = Style.for_stream(stream)
    color = _OUTCOME_COLOR.get(result.outcome, "bold")
    lines = list(render_change(result.impact.change, style))
    lines.append("")
    lines.extend(render_impact(result.impact, style, verbose))
    lines.append("")

    if result.plan and not result.plan.blocked_reason:
        lines.append(style("plan", "bold"))
        lines.extend(f"  {line}" for line in result.plan.render().splitlines()[1:])
        lines.append("")
    elif result.plan and result.plan.blocked_reason:
        lines.append(style(f"plan blocked: {result.plan.blocked_reason}", "yellow"))
        for skipped in result.plan.skipped:
            lines.append(style(f"  skipped {skipped}", "dim"))
        lines.append("")

    if show_diff and result.diff:
        lines.append(style("proposed diff", "bold"))
        for line in result.diff.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                lines.append(style(f"  {line}", "dim"))
            elif line.startswith("+"):
                lines.append(style(f"  {line}", "green"))
            elif line.startswith("-"):
                lines.append(style(f"  {line}", "red"))
            elif line.startswith("@@"):
                lines.append(style(f"  {line}", "cyan"))
            else:
                lines.append(f"  {line}")
        lines.append("")

    if result.validation:
        lines.append(style("validation", "bold"))
        lines.extend(render_validation(result.validation, style))
        lines.append("")

    lines.append(style(f"{result.outcome.value}: {result.message}", color))
    if result.artifacts:
        for key, path in sorted(result.artifacts.items()):
            lines.append(style(f"  {key:<8} {relative(path)}", "dim"))
    if result.workspace_path:
        lines.append(style(f"  workspace {result.workspace_path}", "dim"))
    if verbose and result.timings:
        lines.append(
            style(
                "  timings: "
                + ", ".join(f"{k}={v}ms" for k, v in sorted(result.timings.items())),
                "dim",
            )
        )
    return "\n".join(lines)


def render_run(
    run: MigrationRun, verbose: bool = False, show_diff: bool = True, stream=None
) -> str:
    style = Style.for_stream(stream)
    blocks = [
        style(f"repository      {relative(run.repo)}", "dim"),
        style(f"change document {relative(run.change_document)}", "dim"),
        "",
    ]
    for index, result in enumerate(run.results):
        if index:
            blocks.append(style("-" * 60, "dim"))
        blocks.append(render_migration(result, verbose, show_diff, stream))
    for warning in run.warnings:
        blocks.append(style(f"warning: {warning}", "yellow"))
    if not run.results:
        blocks.append(style("No breaking changes were parsed from the document.", "yellow"))
    return "\n".join(blocks)


# --------------------------------------------------------------------------
# Markdown, for a pull request body
# --------------------------------------------------------------------------


def render_pr_markdown(result: MigrationResult) -> str:
    """A review-ready Markdown summary of one migration.

    Ordered by what a reviewer needs to decide: what upstream changed, where it
    hit us, exactly what we propose to do about it, and what evidence says it
    worked. Then the diff. The checklist at the end is what the human is being
    asked to confirm -- PatchAhead does not merge anything.
    """
    change = result.impact.change
    report = result.impact
    lines: list[str] = []

    title = (
        f"Migrate `{change.target.symbol}` -> `{change.target.replacement}`"
        if change.target.is_rename
        else f"Migrate for: {change.title}"
    )
    lines += [
        f"# {title}",
        "",
        "> Proposed by **PatchAhead**. Not merged, not applied. Review before approving.",
        "",
        "## 1. Upstream change",
        "",
        f"- **Kind:** `{change.kind.value}`",
        f"- **Severity:** {change.severity.value}",
        f"- **Reading confidence:** {change.confidence.value} "
        f"({change.classification_reason})",
    ]
    if change.old_behavior:
        lines.append(f"- **Before:** {change.old_behavior}")
    if change.new_behavior:
        lines.append(f"- **After:** {change.new_behavior}")
    if change.migration_hint:
        lines.append(f"- **Migration guidance:** {change.migration_hint}")
    if change.evidence:
        lines += ["", "<details><summary>Evidence from the change document</summary>", ""]
        for evidence in change.evidence:
            location = f" (line {evidence.line})" if evidence.line else ""
            lines.append(f"- {evidence.quote}{location}")
        lines += ["", "</details>"]

    lines += ["", "## 2. Downstream impact", ""]
    if report.findings:
        lines += [
            "| Location | Symbol | Matched | Confidence | Patched |",
            "|---|---|---|---|---|",
        ]
        for finding in report.findings:
            lines.append(
                f"| `{finding.reference}` | `{finding.symbol}` | "
                f"`{finding.matched_contract}` | {finding.confidence.value} | "
                f"{'yes' if finding.patchable else 'no'} |"
            )
        unpatched = [f for f in report.findings if not f.patchable]
        if unpatched:
            lines += ["", "**Reported but not changed:**", ""]
            lines += [
                f"- `{f.reference}` -- {f.unpatchable_reason or f.reason}" for f in unpatched
            ]
    else:
        lines.append("_No affected code was found._")

    lines += ["", "## 3. Proposed change", ""]
    if result.plan and not result.plan.blocked_reason:
        lines += [
            f"- **Handler:** `{result.plan.handler}`",
            f"- **Risk:** {result.plan.risk.value}",
            f"- **Rationale:** {result.plan.rationale}",
            "",
            "```",
            result.plan.render(),
            "```",
        ]
    elif result.plan:
        lines.append(f"_No migration could be planned:_ {result.plan.blocked_reason}")

    lines += ["", "## 4. Evidence", ""]
    if result.validation:
        lines += ["| Gate | Status | Detail |", "|---|---|---|"]
        for gate in result.validation.gates:
            lines.append(
                f"| `{gate.name.value}` | {gate.status.value} | {gate.detail} |"
            )
    else:
        lines.append("_Validation did not run._")

    if result.baseline_tests:
        lines += [
            "",
            f"Tests before the patch: `{result.baseline_tests.summary}`",
        ]

    if result.diff:
        lines += ["", "## 5. Diff", "", "```diff", result.diff.rstrip(), "```"]

    lines += [
        "",
        "## 6. Human review",
        "",
        f"- **Outcome:** `{result.outcome.value}` -- {result.message}",
        "- **Auto-merge:** disabled. PatchAhead proposes; a human approves.",
        "",
        "### Checklist",
        "",
        "- [ ] The upstream change is characterized correctly",
        "- [ ] The diff is minimal and changes no unrelated code",
        "- [ ] The tests genuinely exercise the migrated behavior",
        "- [ ] Findings reported but not patched have been looked at",
        "",
    ]
    return "\n".join(lines)
