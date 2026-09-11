"""Suites 2 and 3: which sites does impact analysis find, and which does it patch?

Two datasets, one measurement:

``impact``
    Ordinary repositories. Does it find what is there?

``adversarial``
    Repositories written to fool it -- unrelated objects sharing a field name,
    strings that merely contain it, Unicode before an edit site, nested scopes,
    comprehensions, aliases, imports, partially-migrated trees. Same scoring,
    dataset chosen to break things rather than to demonstrate them.

A *site* is ``path:line``. The dataset labels three kinds:

``expect_patched``
    Must be rewritten. Missing one is a false negative.

``expect_reported_only``
    Must be found and explained but **not** rewritten -- the sites where the
    name matches and the evidence does not. Rewriting one of these is the false
    positive that matters, and it is counted separately from simply patching a
    line nobody labelled.

``expect_source_contains``
    Text that must survive in the patched file. A site list can be right while
    the edit is still wrong: a byte-versus-character column error lands on the
    correct line and mangles it, and only reading the patched source catches it.
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

from evals.harness.dataset import SiteCase, describe, load
from evals.harness.metrics import ConfusionMatrix
from evals.harness.result import CaseResult, SuiteResult
from patchahead import handlers
from patchahead.analysis import analyze_source, apply_edits
from patchahead.analysis.edits import is_parseable
from patchahead.analysis.index import RepoIndex
from patchahead.config import Config
from patchahead.domain.change import BreakingChange, ChangeKind, PaginationContract, SymbolTarget


def _change_from(spec: dict) -> BreakingChange:
    kind = ChangeKind(spec["kind"])
    return BreakingChange(
        title=f"eval {kind.value}",
        kind=kind,
        target=SymbolTarget(
            symbol=spec.get("symbol", ""),
            replacement=spec.get("replacement", ""),
            owner=spec.get("owner", ""),
            # Default True: a dataset that writes an `owner` is asserting it,
            # the same way a structured change document does. A case that wants
            # the looser inferred-owner grading sets it to false explicitly.
            owner_is_explicit=spec.get("owner_explicit", True),
        ),
        pagination=PaginationContract(**spec.get("pagination", {})),
    )


def run(suite_name: str) -> SuiteResult:
    suite = SuiteResult(name=suite_name, description=describe(suite_name))
    start = time.perf_counter()
    config = Config()
    # Two matrices on purpose. `sites` covers the cases PatchAhead is expected
    # to get right, and is what CI asserts a floor on. `sites_all` adds the
    # known gaps, and is the honest capability number -- lower, reported, never
    # asserted. Keeping them apart means a recorded limitation does not quietly
    # erode the regression floor, and the floor does not quietly hide the
    # limitation.
    sites = ConfusionMatrix()
    sites_all = ConfusionMatrix()
    #: Counted separately from `sites`: these are the labelled near-misses, the
    #: ones a name-matching tool rewrites and a receiver-checking one does not.
    wrongly_patched = 0
    missed_reports = 0
    mangled_sources = 0
    confidence_counts: Counter[str] = Counter()
    gap_cases = 0

    for case in load(suite_name, SiteCase):
        change = _change_from(case.change)
        index = RepoIndex(root=Path("/eval"), config=config)
        for path, source in case.files.items():
            index.modules[path] = analyze_source(source, path)

        handler = handlers.find_handler(change)
        if handler is None:
            suite.add(CaseResult.judge(case.id, ["no handler accepted the change"]))
            continue

        report = handler.analyze(change, index, config)
        plan = handler.plan(change, report, index, config)

        patched = sorted(f"{t.path}:{t.reference.line}" for t in plan.transformations)
        reported = sorted(f"{f.path}:{f.reference.line}" for f in report.findings)
        expected = sorted(case.expect_patched)
        reported_only = set(case.expect_reported_only)

        for finding in report.findings:
            confidence_counts[finding.confidence.value] += 1

        case_tp = sum(1 for site in patched if site in expected)
        case_fp = len(patched) - case_tp
        case_fn = max(0, len(expected) - case_tp)
        sites_all.observe(tp=case_tp, fp=case_fp, fn=case_fn)
        if not case.known_gap:
            sites.observe(tp=case_tp, fp=case_fp, fn=case_fn)

        problems: list[str] = []
        # A wrong edit is never excused by a `known_gap` marker. See
        # `CaseResult.judge`.
        fatal: list[str] = []

        if patched != expected:
            message = f"patched {patched}, expected {expected}"
            if case_fp:
                fatal.append(message)
                wrongly_patched += case_fp
                if set(patched) & reported_only:
                    problems.append(
                        "rewrote a site labelled report-only: "
                        f"{sorted(set(patched) & reported_only)}"
                    )
            else:
                problems.append(message)

        for site in case.expect_reported_only:
            if site not in reported:
                problems.append(f"did not report {site}")
                missed_reports += 1

        for site, level in case.expect_confidence.items():
            # Every finding on the line must match, not just the first. A line
            # can carry two findings with different verdicts, and checking one
            # of them at random would make the assertion depend on iteration
            # order rather than on the grading.
            at_site = [f for f in report.findings if f"{f.path}:{f.reference.line}" == site]
            if not at_site:
                problems.append(f"no finding at {site} to check the confidence of")
            for finding in at_site:
                if finding.confidence.value != level:
                    problems.append(
                        f"{site} ({finding.matched_contract}): confidence "
                        f"{finding.confidence.value}, expected {level}"
                    )

        if case.expect_source_contains:
            # Apply the plan to every file in the case, not only the ones it
            # touched: `edits_for` is empty for the rest, so they pass through
            # unchanged, and the snippets are then checked against the whole
            # patched repository. Checking them file-by-file would require each
            # snippet to survive in every file, which is not what preservation
            # means when a case spans several modules.
            patched_sources = {
                path: apply_edits(source, plan.edits_for(path))
                for path, source in case.files.items()
            }
            for path, result in sorted(patched_sources.items()):
                if result == case.files[path]:
                    continue
                ok, error = is_parseable(result, path)
                if not ok:
                    fatal.append(f"patched {path} does not parse: {error}")
                    mangled_sources += 1
            # Two uses, one check: text that must *survive* the patch (a
            # neighbouring string the rename must not touch) and text that must
            # *appear* because of it (the migrated call). Both are "after
            # patching, this repository contains this", so both are expressed
            # the same way.
            after = "\n".join(patched_sources[path] for path in sorted(patched_sources))
            for snippet in case.expect_source_contains:
                if snippet not in after:
                    fatal.append(f"patched source does not contain {snippet!r}")
                    mangled_sources += 1

        if case.expect_symbols is not None:
            actual = sorted(
                {t.symbol for t in plan.transformations} or {f.symbol for f in report.findings}
            )
            if actual != sorted(case.expect_symbols):
                problems.append(f"symbols {actual}, expected {sorted(case.expect_symbols)}")

        if case.known_gap:
            gap_cases += 1

        suite.add(
            CaseResult.judge(
                case.id,
                problems,
                known_gap=case.known_gap,
                fatal=fatal,
                metrics={"patched": len(patched), "reported": len(reported)},
            )
        )

    suite.metrics = {"cases": suite.total, "known_gap_cases": gap_cases}
    suite.metrics.update(sites.to_dict("patch"))
    suite.metrics["patch_recall_including_gaps"] = round(sites_all.recall, 3)
    suite.metrics["patch_f1_including_gaps"] = round(sites_all.f1, 3)
    suite.metrics["patch_false_positives_including_gaps"] = sites_all.false_positives
    suite.metrics["wrongly_patched_sites"] = wrongly_patched
    suite.metrics["missed_reports"] = missed_reports
    suite.metrics["mangled_sources"] = mangled_sources
    for level in ("high", "medium", "low"):
        suite.metrics[f"findings_{level}"] = confidence_counts.get(level, 0)

    suite.metric_notes = {
        "patch_precision": "of sites rewritten, the share that should have been",
        "patch_recall": "of sites that should have been rewritten, the share that were",
        "patch_f1": "harmonic mean of the two, so neither can be traded away silently",
        "patch_false_positives": "wrong edits -- the metric this project is built around",
        "wrongly_patched_sites": "same count, restated: must stay 0",
        "missed_reports": "sites correctly left alone but not explained to the user",
        "mangled_sources": "edits that hit the right line and corrupted it",
        "patch_recall_including_gaps": "recall with the known gaps counted -- the honest "
        "capability number, reported and never asserted as a floor",
        "patch_false_positives_including_gaps": "must also stay 0: a gap may under-patch, "
        "never mis-patch",
        "known_gap_cases": "cases a correct tool passes and this one does not, by design",
        "findings_high": "confidence distribution across all findings, patched or not",
    }
    suite.duration_ms = int((time.perf_counter() - start) * 1000)
    return suite


def run_impact() -> SuiteResult:
    return run("impact")


def run_adversarial() -> SuiteResult:
    return run("adversarial")
