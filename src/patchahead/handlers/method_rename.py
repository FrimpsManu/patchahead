"""Method/function rename: ``client.fetch_orders()`` -> ``client.list_orders()``.

Simpler than a field rename, because a call is a much more specific construct
than a word. ``fetch_orders`` appearing as a method call on some object is
strong evidence on its own; the receiver only raises confidence further.

The edit replaces the callee name token and nothing else, so arguments,
formatting, and any chained call are preserved exactly.
"""

from __future__ import annotations

import logging

from patchahead.analysis import base_name
from patchahead.analysis.index import RepoIndex
from patchahead.config import Config
from patchahead.domain.change import BreakingChange, ChangeKind, Confidence
from patchahead.domain.impact import AccessKind, CodeReference, ImpactFinding, ImpactReport
from patchahead.domain.plan import MigrationPlan, Risk, TextEdit, Transformation
from patchahead.handlers.base import MigrationHandler, register

log = logging.getLogger(__name__)


class MethodRenameHandler(MigrationHandler):
    """Renames a called method or function at its call sites."""

    name = "method_rename"
    kinds = (ChangeKind.METHOD_RENAME,)
    summary = "Rename a called method or function: obj.old() -> obj.new()"
    limitations = (
        "Only call sites. A bare reference used as a value "
        "(`callback = client.fetch_orders`) is reported but not rewritten.",
        "Does not rename the definition. This family is for calls into an "
        "upstream SDK, not for renaming a function the repository owns.",
        "A local function that merely shares the upstream name is graded MEDIUM "
        "when the change document names no owner.",
    )

    def supports(self, change: BreakingChange) -> bool:
        return change.kind in self.kinds and change.target.is_rename

    def analyze(
        self, change: BreakingChange, index: RepoIndex, config: Config
    ) -> ImpactReport:
        old = change.target.symbol
        owner = base_name(change.target.owner)
        findings: list[ImpactFinding] = []

        for path in index.non_test_paths():
            module = index.modules[path]

            # A repository that *defines* this name owns it; renaming calls to
            # its own function would break the code rather than migrate it.
            defines_locally = _defines(module, old)

            for call in module.calls:
                if call.name != old:
                    continue
                receiver = base_name(call.receiver)
                confidence, reason, patchable, blocked = self._grade(
                    receiver, owner, defines_locally
                )
                findings.append(
                    ImpactFinding(
                        reference=CodeReference(
                            path=path,
                            line=call.name_range.line,
                            col=call.name_range.col,
                            end_line=call.name_range.end_line,
                            end_col=call.name_range.end_col,
                            snippet=module.line_text(call.range.line),
                        ),
                        symbol=call.symbol,
                        matched_contract=f"{call.receiver + '.' if call.receiver else ''}{old}()",
                        access=AccessKind.CALL,
                        reason=reason,
                        confidence=confidence,
                        source_text=old,
                        patchable=patchable,
                        unpatchable_reason=blocked,
                    )
                )

            # Bare references: `cb = client.fetch_orders` (no call parentheses).
            for attribute in module.attributes:
                if attribute.attr != old:
                    continue
                findings.append(
                    ImpactFinding(
                        reference=CodeReference(
                            path=path,
                            line=attribute.attr_range.line,
                            col=attribute.attr_range.col,
                            end_line=attribute.attr_range.end_line,
                            end_col=attribute.attr_range.end_col,
                            snippet=module.line_text(attribute.range.line),
                        ),
                        symbol=attribute.symbol,
                        matched_contract=f"{attribute.receiver or '<expr>'}.{old}",
                        access=AccessKind.ATTRIBUTE,
                        reason=(
                            "the renamed method is referenced without being called; "
                            "rewriting a bare reference can change behavior if it is "
                            "passed somewhere that expects the old name"
                        ),
                        confidence=Confidence.MEDIUM,
                        source_text=old,
                        patchable=False,
                        unpatchable_reason="bare method reference, not a call site",
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
        self, receiver: str, owner: str, defines_locally: bool
    ) -> tuple[Confidence, str, bool, str]:
        if defines_locally:
            return (
                Confidence.LOW,
                f"this module also defines `{receiver or 'a function'}` with the "
                f"renamed name, so these calls are probably to local code, not to "
                f"the upstream SDK",
                False,
                "the module defines a function with this name locally",
            )
        if owner and receiver == owner:
            return (
                Confidence.HIGH,
                f"method call on `{owner}`, the receiver the change document names",
                True,
                "",
            )
        if receiver:
            return (
                Confidence.HIGH if not owner else Confidence.MEDIUM,
                f"method call with the renamed name on `{receiver}`"
                + (f" (the change names `{owner}`)" if owner else ""),
                True,
                "",
            )
        return (
            Confidence.MEDIUM,
            "bare function call with the renamed name and no receiver to check",
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
                f"Rename calls to `{old}` to `{new}`. Only the callee name token is "
                f"replaced, so arguments and formatting are untouched. The signature "
                f"is unchanged by this migration."
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
                    old=f"{old}(",
                    new=f"{new}(",
                    symbol=finding.symbol,
                    confidence=finding.confidence,
                    edit=TextEdit(
                        line=reference.line,
                        col=reference.col,
                        end_line=reference.end_line or reference.line,
                        end_col=reference.end_col or reference.col,
                        new_text=new,
                        description=f"rename call `{old}` to `{new}`",
                    ),
                )
            )

        if not plan.transformations:
            plan.blocked_reason = (
                f"found {len(report.findings)} reference(s) to `{old}` but none were "
                f"rewritable call sites above the `{config.min_confidence.value}` "
                f"confidence threshold"
                if report.findings
                else f"no calls to `{old}` were found"
            )
        return plan


def _defines(module, name: str) -> bool:
    """Whether a module defines a function or method with this name."""
    import ast

    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in ast.walk(module.tree)
    )


def _related_tests(index: RepoIndex, findings: list[ImpactFinding]) -> list[str]:
    from patchahead.testing import discovery

    return discovery.tests_for_paths(index, [f.path for f in findings])


register(MethodRenameHandler())
