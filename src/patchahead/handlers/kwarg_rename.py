"""Keyword argument rename: ``fetch(timeout_seconds=30)`` -> ``fetch(timeout=30)``.

The most mechanically reliable of the four families, because the construct is
unambiguous: a keyword argument is a keyword argument, and ``ast`` hands back the
exact range of the name token. The edit replaces that token and leaves the value
expression, the other arguments, and the formatting exactly as written.

The only real judgement is *which calls*. When the change document names the
function (``timeout_seconds`` on ``fetch_orders``), only calls to that function
are rewritten; a ``timeout_seconds=`` passed to some unrelated function keeps its
name and is reported for review instead.
"""

from __future__ import annotations

import logging

from patchahead.analysis.index import RepoIndex
from patchahead.config import Config
from patchahead.domain.change import BreakingChange, ChangeKind, Confidence
from patchahead.domain.impact import AccessKind, CodeReference, ImpactFinding, ImpactReport
from patchahead.domain.plan import MigrationPlan, Risk, TextEdit, Transformation
from patchahead.handlers.base import MigrationHandler, register

log = logging.getLogger(__name__)


class KwargRenameHandler(MigrationHandler):
    """Renames a keyword argument at call sites."""

    name = "kwarg_rename"
    kinds = (ChangeKind.KWARG_RENAME,)
    summary = "Rename a keyword argument: f(old_name=x) -> f(new_name=x)"
    limitations = (
        "Only explicit keyword arguments. A value passed positionally, or "
        "expanded from `**kwargs`, is invisible to this handler.",
        "Does not rename the parameter in a function definition; this family is "
        "for calls into an upstream SDK.",
        "When the change document names no function, every call using the keyword "
        "is rewritten -- run `analyze` first to see the list.",
    )

    def supports(self, change: BreakingChange) -> bool:
        return change.kind in self.kinds and change.target.is_rename

    def analyze(
        self, change: BreakingChange, index: RepoIndex, config: Config
    ) -> ImpactReport:
        old = change.target.symbol
        # For this family the owner is the *called function*, not a receiver.
        target_function = change.target.owner.rsplit(".", 1)[-1]
        findings: list[ImpactFinding] = []

        for path in index.non_test_paths():
            module = index.modules[path]
            for call in module.calls:
                keyword_range = call.keywords.get(old)
                if keyword_range is None:
                    continue

                confidence, reason, patchable, blocked = self._grade(
                    call.name, target_function
                )
                receiver = f"{call.receiver}." if call.receiver else ""
                findings.append(
                    ImpactFinding(
                        reference=CodeReference(
                            path=path,
                            line=keyword_range.line,
                            col=keyword_range.col,
                            end_line=keyword_range.end_line,
                            end_col=keyword_range.end_col,
                            snippet=module.line_text(keyword_range.line),
                        ),
                        symbol=call.symbol,
                        matched_contract=f"{receiver}{call.name}({old}=...)",
                        access=AccessKind.KEYWORD_ARG,
                        reason=reason,
                        confidence=confidence,
                        source_text=old,
                        patchable=patchable,
                        unpatchable_reason=blocked,
                    )
                )

        findings.sort(key=lambda f: (f.reference.path, f.reference.line, f.reference.col))
        return ImpactReport(
            change=change,
            findings=findings,
            related_tests=_related_tests(index, findings),
            files_scanned=index.file_count,
            skipped_files=dict(index.skipped),
        )

    def _grade(
        self, called: str, target_function: str
    ) -> tuple[Confidence, str, bool, str]:
        if target_function and called == target_function:
            return (
                Confidence.HIGH,
                f"keyword argument on a call to `{target_function}`, the function "
                f"the change document names",
                True,
                "",
            )
        if target_function:
            return (
                Confidence.LOW,
                f"keyword argument with the renamed name, but on a call to "
                f"`{called}` rather than `{target_function}`; probably a different "
                f"function that happens to share the parameter name",
                False,
                f"call is to `{called}`, not the renamed function `{target_function}`",
            )
        return (
            Confidence.MEDIUM,
            f"keyword argument with the renamed name on a call to `{called}`; the "
            f"change document names no function, so this could not be narrowed",
            True,
            "",
        )

    def plan(
        self,
        change: BreakingChange,
        report: ImpactReport,
        index: RepoIndex,
        config: Config,
    ) -> MigrationPlan:
        old, new = change.target.symbol, change.target.replacement
        plan = MigrationPlan(
            change=change,
            handler=self.name,
            expected_tests=list(report.related_tests),
            risk=Risk.LOW,
            rationale=(
                f"Rename the `{old}=` keyword argument to `{new}=` at each matching "
                f"call site. Only the argument name token is replaced; the value "
                f"expression is untouched, so this migration cannot change what is "
                f"passed, only what it is called."
            ),
        )

        for finding in report.findings:
            if not finding.patchable:
                plan.skipped.append(
                    f"{finding.reference} ({finding.matched_contract}): "
                    f"{finding.unpatchable_reason}"
                )
                continue
            if finding.confidence < config.min_confidence:
                plan.skipped.append(
                    f"{finding.reference} ({finding.matched_contract}): confidence "
                    f"{finding.confidence.value} is below the "
                    f"`{config.min_confidence.value}` threshold"
                )
                continue
            reference = finding.reference
            plan.transformations.append(
                Transformation(
                    reference=reference,
                    old=f"{old}=",
                    new=f"{new}=",
                    symbol=finding.symbol,
                    confidence=finding.confidence,
                    edit=TextEdit(
                        line=reference.line,
                        col=reference.col,
                        end_line=reference.end_line or reference.line,
                        end_col=reference.end_col or reference.col,
                        new_text=new,
                        description=f"rename keyword argument `{old}` to `{new}`",
                    ),
                )
            )

        if not plan.transformations:
            plan.blocked_reason = (
                f"found {len(report.findings)} use(s) of `{old}=` but none were on a "
                f"call this migration should rewrite"
                if report.findings
                else f"no calls passing `{old}=` were found"
            )
        return plan


def _related_tests(index: RepoIndex, findings: list[ImpactFinding]) -> list[str]:
    from patchahead.testing import discovery

    return discovery.tests_for_paths(index, [f.path for f in findings])


register(KwargRenameHandler())
