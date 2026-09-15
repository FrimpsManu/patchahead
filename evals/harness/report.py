"""Rendering benchmark results as text, JSON, or Markdown.

Three audiences, three renderings, one set of numbers:

* **text** -- what a contributor sees after ``python evals/run.py``.
* **JSON** -- what CI archives, and what a future PR can diff run-over-run.
* **Markdown** -- what goes in a PR comment or a report artifact.

Known gaps are rendered prominently in all three. A benchmark that prints its
successes loudly and its recorded limitations quietly is a marketing document.
"""

from __future__ import annotations

import json

from evals.harness.result import BenchmarkResult, CaseStatus, SuiteResult

_STATUS_MARK = {
    CaseStatus.PASSED: "ok",
    CaseStatus.FAILED: "FAILED",
    CaseStatus.KNOWN_GAP: "gap",
    CaseStatus.FIXED_GAP: "STALE GAP",
}


def _format(value: float) -> str:
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(int(value))


def _suite_headline(suite: SuiteResult) -> str:
    status = "PASS" if suite.ok else "FAIL"
    gaps = f", {len(suite.known_gaps)} known gap(s)" if suite.known_gaps else ""
    return (
        f"[{status}] {suite.name}: {suite.passed}/{suite.total} cases{gaps} ({suite.duration_ms}ms)"
    )


def render_text(result: BenchmarkResult) -> str:
    lines: list[str] = []
    for suite in result.suites:
        lines.append(_suite_headline(suite))
        for key, value in suite.metrics.items():
            note = suite.metric_notes.get(key, "")
            suffix = f"   -- {note}" if note else ""
            lines.append(f"         {key}: {_format(value)}{suffix}")
        for case in suite.cases:
            if case.status is CaseStatus.PASSED:
                continue
            lines.append(f"         {_STATUS_MARK[case.status]} {case.case_id}: {case.detail}")
        lines.append("")

    lines.append(f"{result.passed}/{result.total} eval cases passed")
    gaps = result.known_gaps
    if gaps:
        lines.append(
            f"{len(gaps)} known gap(s) -- cases a correct tool passes and this one does not:"
        )
        for case in gaps:
            lines.append(f"  - {case.case_id}: {case.gap_reason}")
    if not result.ok:
        lines.append("BENCHMARK FAILED")
    return "\n".join(lines)


def render_json(result: BenchmarkResult) -> str:
    return json.dumps(result.to_dict(), indent=2, sort_keys=False)


def render_markdown(result: BenchmarkResult) -> str:
    """A self-contained report, suitable for a PR comment or an artifact."""
    lines = ["# PatchAhead evaluation report", ""]
    status = "passing" if result.ok else "**FAILING**"
    lines.append(
        f"{result.passed}/{result.total} cases passed, "
        f"{len(result.known_gaps)} known gap(s), {result.duration_ms}ms -- {status}."
    )
    lines.append("")
    lines.append("| Suite | Cases | Known gaps | Status |")
    lines.append("|---|---|---|---|")
    for suite in result.suites:
        lines.append(
            f"| `{suite.name}` | {suite.passed}/{suite.total} | "
            f"{len(suite.known_gaps)} | {'pass' if suite.ok else 'FAIL'} |"
        )
    lines.append("")

    for suite in result.suites:
        lines.append(f"## `{suite.name}`")
        if suite.description:
            lines.append("")
            lines.append(suite.description)
        lines.append("")
        lines.append("| Metric | Value | Meaning |")
        lines.append("|---|---|---|")
        for key, value in suite.metrics.items():
            lines.append(f"| `{key}` | {_format(value)} | {suite.metric_notes.get(key, '')} |")
        lines.append("")
        problems = [c for c in suite.cases if c.status is not CaseStatus.PASSED]
        if problems:
            lines.append("| Case | Status | Detail |")
            lines.append("|---|---|---|")
            for case in problems:
                detail = case.detail.replace("|", "\\|")
                lines.append(f"| `{case.case_id}` | {case.status.value} | {detail} |")
            lines.append("")

    gaps = result.known_gaps
    lines.append("## Known gaps")
    lines.append("")
    if not gaps:
        lines.append("None recorded.")
    else:
        lines.append(
            "Cases a correct tool passes and PatchAhead does not. They are in the "
            "dataset on purpose: a limitation that is written down as an executable "
            "case gets fixed, and one that is left out of the dataset does not."
        )
        lines.append("")
        lines.append("| Case | Why it fails today |")
        lines.append("|---|---|")
        for case in gaps:
            lines.append(f"| `{case.case_id}` | {case.gap_reason} |")
    lines.append("")
    return "\n".join(lines)


RENDERERS = {"text": render_text, "json": render_json, "markdown": render_markdown}
