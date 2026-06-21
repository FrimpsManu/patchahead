"""PatchAhead agent — coordinates the full migration run.

Pipeline (each stage is a traced span inside one transaction):
  parse changelog -> tests before -> analyze impact -> generate patch
  -> apply patch -> tests after -> PR summary -> persist artifacts.
"""

import json

from patchahead import (
    changelog_parser,
    impact_analyzer,
    memory,
    observability as obs,
    patch_generator,
    paths,
    pr_summary,
    test_runner,
)
from patchahead.schemas import MigrationReport


def reset_demo():
    """Restore the downstream file to its original (broken-on-v2) state."""
    paths.ORDER_SYNC_FILE.write_text(paths.BASELINE_FILE.read_text())


def run() -> MigrationReport:
    paths.OUTPUTS.mkdir(parents=True, exist_ok=True)

    with obs.transaction("patchahead.migration_run") as txn:
        with obs.span("parse_changelog"):
            bc = changelog_parser.parse(paths.CHANGELOG_FILE.read_text())

        with obs.span("run_tests_before", api="v2"):
            before = test_runner.run_tests(api_version="v2")

        with obs.span("analyze_impact"):
            impact = impact_analyzer.analyze(bc, paths.DOWNSTREAM_APP_DIR)

        with obs.span("recall_similar"):
            similar = memory.similar(bc)

        with obs.span("generate_patch"):
            patch = patch_generator.generate(bc, impact)

        with obs.span("apply_patch"):
            patch_generator.apply(patch)
            paths.PATCH_FILE.write_text(patch.diff)

        with obs.span("run_tests_after", api="v2"):
            after = test_runner.run_tests(api_version="v2")

        with obs.span("generate_pr_summary"):
            summary_md = pr_summary.render(bc, impact, patch, before, after)
            paths.PR_SUMMARY_FILE.write_text(summary_md, encoding="utf-8")

        with obs.span("persist_memory"):
            memory.remember(bc)

        succeeded = (not before.passed) and after.passed

        report = MigrationReport(
            breaking_change=bc,
            impact=impact,
            patch=patch,
            tests_before=before,
            tests_after=after,
            pr_summary_path=str(paths.PR_SUMMARY_FILE),
            patch_path=str(paths.PATCH_FILE),
            succeeded=succeeded,
            similar_migrations=similar,
        )

        obs.set_tags(
            txn,
            change_type=bc.change_type,
            risk_level=bc.risk_level,
            tests_before="pass" if before.passed else "fail",
            tests_after="pass" if after.passed else "fail",
            patch_applied=True,
            fallback_used=patch.fallback_used,
        )

        paths.RUN_LOG_FILE.write_text(json.dumps(report.to_dict(), indent=2))

    return report
