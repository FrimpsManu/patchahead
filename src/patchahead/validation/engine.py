"""The validation engine: five gates, in order, producing a structured verdict.

This is the subsystem that decides whether a migration worked. Nothing else is
permitted to: ``MigrationResult.succeeded`` is defined as "validation passed".

Gate order is deliberate. Syntax and scope are near-instant and decisive, and
they run *before* anything executes repository code -- so a proposal that
produced invalid Python or touched 40 unrelated files never gets as far as
running a test command.

===  ====================  ======================================================
#    Gate                  Fails when
===  ====================  ======================================================
1    ``syntax``            A modified file no longer parses as Python.
2    ``scope``             Files outside the plan changed, or the change is
                           larger than the configured limits.
3    ``targeted_tests``    The tests mapped to the changed modules fail.
4    ``regression_tests``  The patch broke a test that passed before it.
5    ``migration_assertion``  Never. It reports evidence, not breakage.
===  ====================  ======================================================

Gate 5 is the one that distinguishes a migration from a no-op. A patch can leave
a green suite green without having fixed anything; this gate asserts that the
specific breakage the change describes was real before the patch and gone after.

It is the one gate that cannot fail. Its question is "is there red-to-green
evidence", so the only answers are PASSED and SKIPPED-because-there-is-none: a
suite that was already green, a runner that never started, a repository whose
remaining failures were failing before the patch too. Evidence of *breakage* is
gate 4's to report, and a patch that broke something must be reported once, by
the gate that measured it, rather than twice under two explanations that do not
agree with each other.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from patchahead.analysis import edits as edit_utils
from patchahead.config import Config
from patchahead.domain.patch import PatchProposal
from patchahead.domain.validation import (
    GateName,
    GateResult,
    GateStatus,
    TestRun,
    ValidationResult,
)
from patchahead.testing import discovery, runner
from patchahead.workspace import Workspace

log = logging.getLogger(__name__)


@dataclass
class ValidationOptions:
    """What the caller wants validated."""

    #: Run gates that execute repository code. ``--no-tests`` sets this False.
    run_tests: bool = True
    #: Targeted-test result from before patching, for the migration assertion.
    baseline: TestRun | None = None
    #: Full-suite result from before patching, so the regression gate can tell
    #: a test this patch broke from one that was already failing.
    full_baseline: TestRun | None = None
    #: Test command override; defaults to the repository's configured command.
    test_command: str = ""
    #: Files already modified in the workspace by an earlier change in the same
    #: run. The scope gate judges a proposal by what *it* changed, not by what
    #: the workspace has accumulated.
    preexisting_changes: set[str] = field(default_factory=set)


class ValidationEngine:
    """Runs the gates against a patched workspace."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def validate(
        self,
        proposal: PatchProposal,
        workspace: Workspace,
        options: ValidationOptions | None = None,
    ) -> ValidationResult:
        options = options or ValidationOptions()
        result = ValidationResult()

        result.gates.append(self._syntax_gate(proposal, workspace))
        result.gates.append(self._scope_gate(proposal, workspace, options.preexisting_changes))

        # Do not execute repository code if the patch is already known bad.
        if any(gate.failed for gate in result.gates):
            reason = "skipped because an earlier gate failed"
            for name in (
                GateName.TARGETED_TESTS,
                GateName.REGRESSION_TESTS,
                GateName.MIGRATION_ASSERTION,
            ):
                result.gates.append(GateResult(name=name, status=GateStatus.SKIPPED, detail=reason))
            return result

        if not options.run_tests:
            reason = "skipped: test execution was disabled (--no-tests)"
            for name in (
                GateName.TARGETED_TESTS,
                GateName.REGRESSION_TESTS,
                GateName.MIGRATION_ASSERTION,
            ):
                result.gates.append(GateResult(name=name, status=GateStatus.SKIPPED, detail=reason))
            return result

        command = options.test_command or self.config.test_command
        targeted = self._targeted_gate(proposal, workspace, command)
        result.gates.append(targeted)
        regression = self._regression_gate(workspace, command, options.full_baseline)
        result.gates.append(regression)
        result.gates.append(
            self._assertion_gate(targeted, regression, options.baseline, options.full_baseline)
        )
        return result

    # -- gate 1: syntax ----------------------------------------------------

    def _syntax_gate(self, proposal: PatchProposal, workspace: Workspace) -> GateResult:
        start = time.perf_counter()
        broken: list[str] = []
        for file_edit in proposal.files:
            if not file_edit.changed:
                continue
            ok, error = edit_utils.is_parseable(file_edit.new_source, file_edit.path)
            if not ok:
                broken.append(error)

        duration = int((time.perf_counter() - start) * 1000)
        if broken:
            return GateResult(
                name=GateName.SYNTAX,
                status=GateStatus.FAILED,
                detail="patched file(s) no longer parse: " + "; ".join(broken),
                duration_ms=duration,
            )
        changed = len(proposal.changed_files)
        if changed == 0:
            return GateResult(
                name=GateName.SYNTAX,
                status=GateStatus.SKIPPED,
                detail="no files were modified",
                duration_ms=duration,
            )
        return GateResult(
            name=GateName.SYNTAX,
            status=GateStatus.PASSED,
            detail=f"{changed} modified file(s) parse as valid Python",
            duration_ms=duration,
        )

    # -- gate 2: scope -----------------------------------------------------

    def _scope_gate(
        self,
        proposal: PatchProposal,
        workspace: Workspace,
        preexisting: set[str] | None = None,
    ) -> GateResult:
        """Nothing outside the plan changed, and the change is not oversized.

        This is the gate that contains an LLM. A model asked to fix one function
        can reformat a file, "improve" a neighbour, or rewrite an import block;
        the plan names exactly which files were supposed to change, and anything
        else is a failure regardless of how good the diff looks.
        """
        start = time.perf_counter()
        planned = set(proposal.plan.target_files)
        actual = set(workspace.changed_files()) - set(preexisting or ())
        unexpected = sorted(actual - planned)
        duration = int((time.perf_counter() - start) * 1000)

        if not actual:
            # Nothing changed, so there is nothing to judge the scope of. This
            # must not read as a pass: a proposal that modified no files has not
            # migrated anything, and with every other gate skipped a PASSED here
            # would make an empty patch look validated.
            return GateResult(
                name=GateName.SCOPE,
                status=GateStatus.SKIPPED,
                detail="no files were modified",
                duration_ms=duration,
            )

        if unexpected:
            return GateResult(
                name=GateName.SCOPE,
                status=GateStatus.FAILED,
                detail=(
                    f"{len(unexpected)} file(s) changed that the plan did not name: "
                    + ", ".join(unexpected[:5])
                    + (" ..." if len(unexpected) > 5 else "")
                ),
                duration_ms=duration,
            )

        limit = self.config.max_changed_files
        if limit and len(actual) > limit:
            return GateResult(
                name=GateName.SCOPE,
                status=GateStatus.FAILED,
                detail=(
                    f"{len(actual)} files changed, above the `max_changed_files` limit of {limit}"
                ),
                duration_ms=duration,
            )

        diff_lines = proposal.diff_line_count
        diff_limit = self.config.max_diff_lines
        if diff_limit and diff_lines > diff_limit:
            return GateResult(
                name=GateName.SCOPE,
                status=GateStatus.FAILED,
                detail=(
                    f"the diff changes {diff_lines} lines, above the "
                    f"`max_diff_lines` limit of {diff_limit}"
                ),
                duration_ms=duration,
            )

        return GateResult(
            name=GateName.SCOPE,
            status=GateStatus.PASSED,
            detail=(
                f"{len(actual)} file(s) changed, all named by the plan; {diff_lines} diff line(s)"
            ),
            duration_ms=duration,
        )

    # -- gate 3: targeted tests -------------------------------------------

    def _targeted_gate(
        self, proposal: PatchProposal, workspace: Workspace, command: str
    ) -> GateResult:
        expected = proposal.plan.expected_tests
        if not expected:
            return GateResult(
                name=GateName.TARGETED_TESTS,
                status=GateStatus.SKIPPED,
                detail=(
                    "no tests could be mapped to the changed modules; the "
                    "regression gate runs the full suite instead"
                ),
            )

        scoped = discovery.scoped_command(command, expected)
        if scoped == command:
            return GateResult(
                name=GateName.TARGETED_TESTS,
                status=GateStatus.SKIPPED,
                detail=(
                    f"the configured test command (`{command}`) cannot be narrowed "
                    f"to specific files; the regression gate covers these tests"
                ),
            )

        run = runner.run_tests(workspace, scoped, timeout=self.config.test_timeout_seconds)
        if run.errored:
            # The command could not start, or collected nothing. That is "could
            # not verify", not "verified and failed", and both test gates have to
            # say so in the same words: a runner that is missing is one fact, and
            # a gate that called it a failure while the other called it a skip
            # was how the same fact ended up reported as a code regression. The
            # run ends as `patched_unverified` rather than `migrated`, because no
            # test gate actually ran.
            return GateResult(
                name=GateName.TARGETED_TESTS,
                status=GateStatus.SKIPPED,
                detail=f"the targeted tests did not run: {run.summary}",
                duration_ms=run.duration_ms,
                test_run=run,
            )
        return GateResult(
            name=GateName.TARGETED_TESTS,
            status=GateStatus.PASSED if run.passed else GateStatus.FAILED,
            detail=f"{', '.join(expected)}: {run.summary}",
            duration_ms=run.duration_ms,
            test_run=run,
        )

    # -- gate 4: regression -----------------------------------------------

    def _regression_gate(
        self, workspace: Workspace, command: str, baseline: TestRun | None
    ) -> GateResult:
        """Did this patch break anything that was working?

        "Regression" means *newly* failing, so the gate compares the failing set
        against a full-suite run from before the patch. A test that was already
        red stays red without failing this gate -- a repository broken by three
        upstream changes must still be able to migrate the first one. Only tests
        this patch turned from passing to failing count.

        Without a baseline the gate falls back to requiring a fully green suite,
        which is the only safe reading when there is nothing to compare to.
        """
        run = runner.run_tests(workspace, command, timeout=self.config.test_timeout_seconds)
        if run.errored:
            return GateResult(
                name=GateName.REGRESSION_TESTS,
                status=GateStatus.SKIPPED,
                detail=f"the full suite did not run: {run.summary}",
                duration_ms=run.duration_ms,
                test_run=run,
            )
        if run.passed:
            return GateResult(
                name=GateName.REGRESSION_TESTS,
                status=GateStatus.PASSED,
                detail=f"`{command}`: {run.summary}",
                duration_ms=run.duration_ms,
                test_run=run,
            )

        if baseline is None or baseline.errored:
            return GateResult(
                name=GateName.REGRESSION_TESTS,
                status=GateStatus.FAILED,
                detail=(
                    f"`{command}`: {run.summary} (no pre-patch baseline was available, "
                    f"so any failure is treated as a regression)"
                ),
                duration_ms=run.duration_ms,
                test_run=run,
            )

        already_failing = set(baseline.failing_tests)
        now_failing = set(run.failing_tests)
        new_failures = sorted(now_failing - already_failing)

        if new_failures:
            return GateResult(
                name=GateName.REGRESSION_TESTS,
                status=GateStatus.FAILED,
                detail=(
                    f"the patch broke {len(new_failures)} test(s) that passed before: "
                    + ", ".join(new_failures[:5])
                    + (" ..." if len(new_failures) > 5 else "")
                ),
                duration_ms=run.duration_ms,
                test_run=run,
            )

        if not baseline.failing_tests and not run.failing_tests:
            # Both runs failed without naming tests -- a collection or import
            # error, say. Not something to wave through as "pre-existing".
            return GateResult(
                name=GateName.REGRESSION_TESTS,
                status=GateStatus.FAILED,
                detail=(
                    f"`{command}`: {run.summary}; the failure names no specific test, "
                    f"so it cannot be attributed to pre-existing breakage"
                ),
                duration_ms=run.duration_ms,
                test_run=run,
            )

        fixed = sorted(already_failing - now_failing)
        return GateResult(
            name=GateName.REGRESSION_TESTS,
            status=GateStatus.PASSED,
            detail=(
                f"no new failures. {len(now_failing)} test(s) were already failing "
                f"before this patch (unrelated upstream breakage)"
                + (f"; this patch fixed {len(fixed)}" if fixed else "")
            ),
            duration_ms=run.duration_ms,
            test_run=run,
        )

    # -- gate 5: migration assertion --------------------------------------

    def _assertion_gate(
        self,
        targeted: GateResult,
        regression: GateResult,
        baseline: TestRun | None,
        full_baseline: TestRun | None,
    ) -> GateResult:
        """Did the specific breakage actually get fixed?

        Requires evidence on both sides: a test failed before the patch, and
        passes after it. This is the gate that separates "we changed some code"
        from "we migrated something", and
        :attr:`~patchahead.domain.validation.ValidationResult.verified` is
        defined in terms of it.

        It prefers the targeted gate's evidence and falls back to the full
        suite, because a repository whose test command cannot be narrowed to
        specific files still produces perfectly good red-to-green evidence --
        it is just spread across the whole run. A scope is only usable when both
        of its runs completed; one that could not run is skipped over, and if no
        scope is usable the gate says which ones were missing and why.
        """
        reasons: list[str] = []
        for scope, before, gate in (
            ("targeted", baseline, targeted),
            ("full suite", full_baseline, regression),
        ):
            after = gate.test_run
            if after is None and gate.status is GateStatus.SKIPPED:
                # The gate declined to run at all (no mapped tests, a command
                # that cannot be narrowed). Not a problem worth reporting here;
                # the other scope may still carry the evidence.
                continue
            if after is None or after.errored:
                reasons.append(
                    f"the post-patch {scope} run did not complete"
                    + (f": {after.summary}" if after is not None else "")
                )
                continue
            if before is None:
                reasons.append(f"no pre-patch {scope} run was recorded")
                continue
            if before.errored:
                reasons.append(f"the pre-patch {scope} run did not complete: {before.summary}")
                continue
            return self._compare_runs(scope, before, after)

        detail = "; ".join(dict.fromkeys(reasons)) or "no before/after test evidence is available"
        return GateResult(
            name=GateName.MIGRATION_ASSERTION,
            status=GateStatus.SKIPPED,
            detail=f"{detail}, so this patch is unverified",
        )

    @staticmethod
    def _compare_runs(scope: str, before: TestRun, after: TestRun) -> GateResult:
        """Decide what one usable pair of runs evidences.

        Exactly one shape is a pass: something that was failing before the patch
        passes after it. Everything else is SKIPPED, never FAILED, because this
        gate asks "is there migration evidence" and the answer "no" is an
        absence of proof rather than proof of breakage. A patch that genuinely
        broke something is the regression gate's verdict to give; giving it
        again here, under a different explanation, is how two gates end up
        contradicting each other about the same test run.
        """

        def verdict(status: GateStatus, detail: str) -> GateResult:
            return GateResult(name=GateName.MIGRATION_ASSERTION, status=status, detail=detail)

        if before.passed:
            return verdict(
                GateStatus.SKIPPED,
                f"the {scope} tests already passed before the patch, so this run "
                f"cannot evidence that the migration fixed anything. The patch "
                f"may still be correct; these tests do not cover the change.",
            )

        fixed = sorted(set(before.failing_tests) - set(after.failing_tests))
        broken = sorted(set(after.failing_tests) - set(before.failing_tests))
        named = ", ".join(fixed[:3]) + (" ..." if len(fixed) > 3 else "")

        if after.passed:
            # Red before, green after. Whatever was failing passes now, named or
            # not -- which is what makes this branch the one that works for a
            # runner whose output PatchAhead cannot parse test names out of.
            return verdict(
                GateStatus.PASSED,
                f"the {scope} tests failed before the patch and pass after it "
                f"({named or before.summary})",
            )

        if not fixed:
            # Still red, and nothing that was red is green. The patch may be
            # incomplete or simply irrelevant to this failure; either way there
            # is nothing here to verify a migration with.
            return verdict(
                GateStatus.SKIPPED,
                f"the {scope} tests still fail after the patch and none of the "
                f"tests that were failing before it pass now, so this run does "
                f"not evidence a migration",
            )

        if broken:
            # Some red went green and some green went red. The repair is real
            # but so is the damage, and calling this verified would let the
            # headline read "migrated" over a regression.
            return verdict(
                GateStatus.SKIPPED,
                f"the patch repaired {len(fixed)} previously failing {scope} "
                f"test(s) ({named}) but {len(broken)} test(s) that passed before "
                f"it now fail; the regression gate reports those",
            )

        return verdict(
            GateStatus.PASSED,
            f"{len(fixed)} {scope} test(s) that failed before the patch now pass "
            f"({named}); the {len(after.failing_tests)} still failing were "
            f"already failing before it",
        )
