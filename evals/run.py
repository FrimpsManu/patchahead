#!/usr/bin/env python3
"""The PatchAhead evaluation harness.

    python evals/run.py                 # everything
    python evals/run.py classification  # one suite
    python evals/run.py --json          # machine-readable

Three suites, each measuring something different:

``classification``
    Given a release note, does PatchAhead read it correctly? Scored on both the
    change kind and the extracted symbols, and it includes notes PatchAhead
    *should* refuse -- a classifier scored only on what it can do is not a
    classifier.

``impact``
    Given a change and a repository, does it find the right sites? Reported as
    precision and recall against a hand-labelled ground truth, counting false
    positives explicitly, because that is the metric the prototype failed.

``migrations``
    Does a full run take the tests from red to green? Real repositories, real
    ``pytest`` subprocesses, real validation gates. Includes cases that must
    *not* succeed.

Every number this prints is computed from a run that just happened. Nothing is
recorded, cached, or asserted from a previous version.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from patchahead import engine, handlers  # noqa: E402
from patchahead.analysis import analyze_source  # noqa: E402
from patchahead.analysis.index import RepoIndex  # noqa: E402
from patchahead.config import Config  # noqa: E402
from patchahead.domain.change import (  # noqa: E402
    BreakingChange,
    ChangeKind,
    PaginationContract,
    SymbolTarget,
)
from patchahead.ingest.base import ChangeDocument, parse_document  # noqa: E402

DATASETS = Path(__file__).parent / "datasets"


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    detail: str = ""
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass
class SuiteResult:
    name: str
    cases: list[CaseResult] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    duration_ms: int = 0

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def ok(self) -> bool:
        return self.passed == self.total

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "total": self.total,
            "metrics": self.metrics,
            "duration_ms": self.duration_ms,
            "cases": [
                {"id": c.case_id, "passed": c.passed, "detail": c.detail, "metrics": c.metrics}
                for c in self.cases
            ],
        }


def load(suite: str) -> list[dict]:
    path = DATASETS / suite / "cases.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


# --------------------------------------------------------------------------
# suite 1: classification
# --------------------------------------------------------------------------


def run_classification() -> SuiteResult:
    suite = SuiteResult(name="classification")
    start = time.perf_counter()
    kind_correct = 0
    symbol_cases = 0
    symbol_correct = 0

    for case in load("classification"):
        text = case["text"]
        expected_kind = case["expected_kind"]

        changes = parse_document(ChangeDocument(text=text, path=case["id"], suffix=".md"))
        change = changes[0]

        # An actionable rename with no extractable symbols is, in practice,
        # unknown: nothing downstream can act on it. Score it that way.
        actual_kind = change.kind.value
        if (
            change.kind.is_actionable
            and change.kind is not ChangeKind.PAGINATION_PAGE_TO_CURSOR
            and not change.target.is_rename
        ):
            actual_kind = "unknown"

        kind_ok = actual_kind == expected_kind
        kind_correct += kind_ok

        problems = []
        if not kind_ok:
            problems.append(f"kind: expected {expected_kind}, got {actual_kind}")

        for label, key in (
            ("symbol", "expected_symbol"),
            ("replacement", "expected_replacement"),
            ("owner", "expected_owner"),
        ):
            if key not in case:
                continue
            symbol_cases += 1
            actual = getattr(change.target, label)
            if actual == case[key]:
                symbol_correct += 1
            else:
                problems.append(f"{label}: expected {case[key]!r}, got {actual!r}")

        suite.cases.append(
            CaseResult(
                case_id=case["id"],
                passed=not problems,
                detail="; ".join(problems),
            )
        )

    total = len(suite.cases)
    suite.metrics = {
        "kind_accuracy": round(kind_correct / total, 3) if total else 0.0,
        "symbol_accuracy": round(symbol_correct / symbol_cases, 3) if symbol_cases else 0.0,
        "cases": total,
        "symbol_fields": symbol_cases,
    }
    suite.duration_ms = int((time.perf_counter() - start) * 1000)
    return suite


# --------------------------------------------------------------------------
# suite 2: impact detection
# --------------------------------------------------------------------------


def _change_from(spec: dict) -> BreakingChange:
    kind = ChangeKind(spec["kind"])
    return BreakingChange(
        title=f"eval {kind.value}",
        kind=kind,
        target=SymbolTarget(
            symbol=spec.get("symbol", ""),
            replacement=spec.get("replacement", ""),
            owner=spec.get("owner", ""),
        ),
        pagination=PaginationContract(**spec.get("pagination", {})),
    )


def run_impact() -> SuiteResult:
    """Precision and recall against hand-labelled sites.

    A *site* is ``path:line``. Recall is over the sites that should have been
    patched; precision counts anything patched that should not have been. Sites
    labelled ``expect_reported_only`` must be found but not patched -- finding
    them is correct, patching them is the false positive.
    """
    suite = SuiteResult(name="impact")
    start = time.perf_counter()
    config = Config()
    true_positives = false_positives = false_negatives = 0
    missed_reports = 0
    wrongly_patched = 0

    for case in load("impact"):
        change = _change_from(case["change"])
        index = RepoIndex(root=Path("/eval"), config=config)
        for path, source in case["files"].items():
            index.modules[path] = analyze_source(source, path)

        handler = handlers.find_handler(change)
        if handler is None:
            suite.cases.append(CaseResult(case["id"], False, "no handler accepted the change"))
            continue

        report = handler.analyze(change, index, config)
        plan = handler.plan(change, report, index, config)

        patched = sorted(f"{t.path}:{t.reference.line}" for t in plan.transformations)
        reported = sorted(f"{f.path}:{f.reference.line}" for f in report.findings)
        expected_patched = sorted(case["expect_patched"])

        case_tp = sum(1 for site in patched if site in expected_patched)
        case_fp = len(patched) - case_tp
        case_fn = max(0, len(expected_patched) - case_tp)
        true_positives += case_tp
        false_positives += case_fp
        false_negatives += case_fn

        problems = []
        if sorted(patched) != expected_patched:
            problems.append(f"patched {patched}, expected {expected_patched}")
            wrongly_patched += case_fp
        for site in case.get("expect_reported_only", []):
            if site not in reported:
                problems.append(f"did not report {site}")
                missed_reports += 1

        suite.cases.append(
            CaseResult(
                case_id=case["id"],
                passed=not problems,
                detail="; ".join(problems),
                metrics={"patched": len(patched), "reported": len(reported)},
            )
        )

    precision = (
        true_positives / (true_positives + false_positives)
        if (true_positives + false_positives)
        else 1.0
    )
    recall = (
        true_positives / (true_positives + false_negatives)
        if (true_positives + false_negatives)
        else 1.0
    )
    suite.metrics = {
        "patch_precision": round(precision, 3),
        "patch_recall": round(recall, 3),
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "missed_reports": missed_reports,
        "wrongly_patched": wrongly_patched,
    }
    suite.duration_ms = int((time.perf_counter() - start) * 1000)
    return suite


# --------------------------------------------------------------------------
# suite 3: end-to-end migrations
# --------------------------------------------------------------------------


def run_migrations() -> SuiteResult:
    suite = SuiteResult(name="migrations")
    start = time.perf_counter()
    attempted = 0
    succeeded = 0

    for case in load("migrations"):
        temp = Path(tempfile.mkdtemp(prefix="patchahead-eval-"))
        try:
            repo = temp / "repo"
            for path, source in case["files"].items():
                target = repo / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source, encoding="utf-8")
            change_path = temp / "change.md"
            change_path.write_text(case["change"], encoding="utf-8")

            run = engine.migrate(
                repo,
                change_path,
                engine.EngineOptions(write_artifacts=False),
            )
            result = run.results[0]

            problems = []
            expected_success = case["expect_success"]
            if expected_success:
                attempted += 1
                if result.succeeded:
                    succeeded += 1
                else:
                    problems.append(f"expected success, got {result.outcome.value}")
            elif result.succeeded:
                problems.append(f"expected NOT to succeed, but it did ({result.outcome.value})")

            expected_outcome = case.get("expect_outcome")
            if expected_outcome and result.outcome.value != expected_outcome:
                problems.append(f"outcome: expected {expected_outcome}, got {result.outcome.value}")

            # Minimality: text the migration must not have changed. Only
            # added/removed lines count -- a unified diff also carries unchanged
            # context lines, and matching those would flag every correct patch.
            touched = "\n".join(
                line
                for line in result.diff.splitlines()
                if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
            )
            for snippet in case.get("expect_unchanged", []):
                if snippet in touched:
                    problems.append(f"diff changed text it should not have: {snippet!r}")

            suite.cases.append(
                CaseResult(
                    case_id=case["id"],
                    passed=not problems,
                    detail="; ".join(problems) or result.message,
                    metrics={
                        "diff_lines": result.proposal.diff_line_count if result.proposal else 0
                    },
                )
            )
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    suite.metrics = {
        "migration_success_rate": round(succeeded / attempted, 3) if attempted else 0.0,
        "attempted": attempted,
        "succeeded": succeeded,
        "refusal_cases": len(suite.cases) - attempted,
    }
    suite.duration_ms = int((time.perf_counter() - start) * 1000)
    return suite


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

SUITES = {
    "classification": run_classification,
    "impact": run_impact,
    "migrations": run_migrations,
}


def render(suites: list[SuiteResult]) -> str:
    lines = []
    for suite in suites:
        status = "PASS" if suite.ok else "FAIL"
        lines.append(
            f"[{status}] {suite.name}: {suite.passed}/{suite.total} cases ({suite.duration_ms}ms)"
        )
        for key, value in suite.metrics.items():
            lines.append(f"         {key}: {value}")
        for case in suite.cases:
            if not case.passed:
                lines.append(f"         FAILED {case.case_id}: {case.detail}")
        lines.append("")
    total_passed = sum(s.passed for s in suites)
    total_cases = sum(s.total for s in suites)
    lines.append(f"{total_passed}/{total_cases} eval cases passed")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals/run.py", description="Run the PatchAhead evaluation suites."
    )
    # `choices` is deliberately not used here: with `nargs="*"`, argparse
    # validates the empty default against it and rejects "run everything".
    parser.add_argument(
        "suites",
        nargs="*",
        metavar="SUITE",
        help=f"suites to run, any of: {', '.join(SUITES)} (default: all)",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    unknown = [name for name in args.suites if name not in SUITES]
    if unknown:
        parser.error(f"unknown suite(s): {', '.join(unknown)}. Choose from: {', '.join(SUITES)}")

    selected = args.suites or list(SUITES)
    results = [SUITES[name]() for name in selected]

    if args.as_json:
        print(json.dumps({s.name: s.to_dict() for s in results}, indent=2))
    else:
        print(render(results))

    return 0 if all(s.ok for s in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
