"""Suite 4: does a whole engine run take a real repository from red to green?

Real directories, real ``pytest`` subprocesses, real validation gates. The
cheapest way to make this suite look good would be to fill it with cases
PatchAhead can migrate, so roughly half the dataset is cases it must *not*
migrate, and every run is sorted into a five-way taxonomy rather than a
pass rate:

=========================  ==================================================
``successful_migration``   Should migrate, did, and the tests proved it.
``missed_migration``       Should migrate, did not. A capability gap.
``safe_refusal``           Should not migrate, and declined with a reason.
``incorrect_migration``    Should not migrate, and reported success anyway.
``unnecessary_migration``  Nothing needed changing and it changed something.
=========================  ==================================================

The last two are the ones that matter. ``incorrect_migration`` means the gates
blessed something they should have rejected, and ``unnecessary_migration`` means
it edited already-correct code -- both are wrong edits reaching a user, and both
must stay at zero. ``missed_migration`` is a limitation, and limitations are
survivable.

Patch size is recorded too. There is no correct number of changed lines, but a
rename whose mean diff doubles has stopped being a rename, and nobody notices
that without a number.
"""

from __future__ import annotations

import time

from evals.harness.dataset import MigrationCase, describe, load
from evals.harness.metrics import Distribution, Tally
from evals.harness.repo import changed_line_count, temporary_repo, touched_text
from evals.harness.result import CaseResult, SuiteResult
from patchahead import engine

SUITE = "migrations"

OUTCOMES = (
    "successful_migration",
    "missed_migration",
    "safe_refusal",
    "incorrect_migration",
    "unnecessary_migration",
)


def run() -> SuiteResult:
    suite = SuiteResult(name=SUITE, description=describe(SUITE))
    start = time.perf_counter()
    tally = Tally(OUTCOMES)
    changed_lines = Distribution()
    changed_files = Distribution()
    attempted = 0
    succeeded = 0

    for case in load(SUITE, MigrationCase):
        with temporary_repo(case.files, case.change) as (repo, change_path):
            run_result = engine.migrate(
                repo, change_path, engine.EngineOptions(write_artifacts=False)
            )
            result = run_result.results[0]

            problems: list[str] = []
            fatal: list[str] = []
            diff = result.diff
            patched = bool(diff.strip())

            if case.expect_success:
                attempted += 1
                if result.succeeded:
                    succeeded += 1
                    tally.observe("successful_migration")
                else:
                    tally.observe("missed_migration")
                    problems.append(f"expected success, got {result.outcome.value}")
            elif result.succeeded:
                tally.observe("incorrect_migration")
                fatal.append(
                    f"expected NOT to succeed, but it did ({result.outcome.value}). "
                    f"A migration reported as verified that should have been refused is "
                    f"a wrong edit with the gates' blessing."
                )
            elif case.expect_no_patch and patched:
                tally.observe("unnecessary_migration")
                fatal.append(
                    "nothing needed migrating and a patch was produced anyway:\n"
                    + touched_text(diff)
                )
            else:
                tally.observe("safe_refusal")

            if case.expect_outcome and result.outcome.value != case.expect_outcome:
                problems.append(
                    f"outcome: expected {case.expect_outcome}, got {result.outcome.value}"
                )

            if case.expect_no_patch and patched and not fatal:
                fatal.append("expected no patch, but the run produced a diff")

            # Minimality. Only added/removed lines count -- a unified diff also
            # carries unchanged context, and matching that flags every correct
            # patch as having touched text it should not have.
            touched = touched_text(diff)
            for snippet in case.expect_unchanged:
                if snippet in touched:
                    fatal.append(f"diff changed text it should not have: {snippet!r}")

            touched_files = list(result.proposal.changed_files) if result.proposal else []
            for path in case.expect_untouched_files:
                if path in touched_files:
                    fatal.append(f"migrated a file that was already correct: {path}")

            lines = changed_line_count(diff)
            files = len(touched_files)
            if patched:
                changed_lines.observe(lines)
                changed_files.observe(files)
            if case.max_changed_lines is not None and lines > case.max_changed_lines:
                problems.append(
                    f"diff is {lines} changed line(s), above the case limit of "
                    f"{case.max_changed_lines}"
                )
            if case.max_changed_files is not None and files > case.max_changed_files:
                problems.append(
                    f"{files} file(s) changed, above the case limit of {case.max_changed_files}"
                )

            suite.add(
                CaseResult.judge(
                    case.id,
                    problems,
                    known_gap=case.known_gap,
                    fatal=fatal,
                    metrics={"changed_lines": lines, "changed_files": files},
                )
            )

    suite.metrics = {
        "cases": suite.total,
        "attempted": attempted,
        "migration_success_rate": round(succeeded / attempted, 3) if attempted else 0.0,
        "refusal_cases": suite.total - attempted,
    }
    suite.metrics.update(tally.to_dict())
    suite.metrics.update(changed_lines.to_dict("changed_lines"))
    suite.metrics.update(changed_files.to_dict("changed_files"))

    suite.metric_notes = {
        "migration_success_rate": "share of cases that should migrate and were verified "
        "red-to-green",
        "refusal_cases": "cases that must NOT migrate -- a success rate measured only on "
        "cases we can do is not a measurement",
        "incorrect_migration": "reported success on a case that should have been refused; "
        "must stay 0",
        "unnecessary_migration": "patched a repository that needed no migration; must stay 0",
        "missed_migration": "a capability gap: should have migrated and did not",
        "changed_lines_mean": "average added+removed lines per produced patch",
        "changed_files_mean": "average files touched per produced patch",
    }
    suite.duration_ms = int((time.perf_counter() - start) * 1000)
    return suite
