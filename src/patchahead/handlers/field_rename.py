"""Field rename: ``order["total"]`` -> ``order["amount"]``.

The hardest part of this family is not rewriting -- it is deciding *what not to
rewrite*. A field named ``total``, ``name``, ``id``, or ``status`` is a word that
appears all over a codebase for unrelated reasons. The prototype matched the
word as text and corrupted three out of four sites in a four-line test
(``docs/assessment.md`` §2.2).

Two mechanisms keep this one honest.

**Syntactic filtering.** Only three constructs can be a field access:
``obj["total"]``, ``obj.get("total")``, and ``obj.total``. A bare string literal
(``LABEL = "total"``) is not one of them and is invisible to the AST walk, so it
cannot be a false positive at all -- not "filtered out later", but never a
candidate.

**Confidence grading by receiver.** Among real accesses, the receiver name
decides:

===================================  ==========  =======================
Site                                 Confidence  Patched by default?
===================================  ==========  =======================
``order["total"]``, owner ``order``  HIGH        yes
``o["total"]``, owner ``order``      MEDIUM      yes
``order.total``, owner ``order``     HIGH        yes
``df.total``, owner ``order``        LOW         no -- reported for review
===================================  ==========  =======================

Attribute access on an unrelated receiver is the dangerous case (``df.total`` on
a DataFrame has nothing to do with the API), so it is graded LOW and left alone,
while still being *reported* so a human can look.
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


def quoted_replacement(original_quote: str, new_value: str) -> str:
    """Re-quote a string literal, preserving the author's quote style.

    ``'total'`` becomes ``'amount'`` and ``"total"`` becomes ``"amount"``. A
    rename should not also restyle the file.
    """
    quote = "'" if original_quote.lstrip("rbuRBU").startswith("'") else '"'
    prefix = original_quote[: len(original_quote) - len(original_quote.lstrip("rbuRBU"))]
    return f"{prefix}{quote}{new_value}{quote}"


class FieldRenameHandler(MigrationHandler):
    """Renames a data field accessed by subscript, ``.get()``, or attribute."""

    name = "field_rename"
    kinds = (ChangeKind.FIELD_RENAME,)
    summary = "Rename a response/data field: obj[\"old\"], obj.get(\"old\"), obj.old"
    limitations = (
        "Only constant string keys. `order[key]` with a variable key is reported, "
        "never rewritten.",
        "Attribute access on a receiver that does not match the declared owner is "
        "graded LOW and left for a human.",
        "Does not follow the field through assignment: `t = order[\"total\"]` is "
        "renamed, but a later use of `t` is not traced.",
    )

    def supports(self, change: BreakingChange) -> bool:
        return change.kind in self.kinds and change.target.is_rename

    # -- analysis ----------------------------------------------------------

    def analyze(
        self, change: BreakingChange, index: RepoIndex, config: Config
    ) -> ImpactReport:
        old = change.target.symbol
        owner = base_name(change.target.owner)
        findings: list[ImpactFinding] = []

        for path in index.non_test_paths():
            module = index.modules[path]

            for access in module.subscripts:
                if access.key != old:
                    continue
                receiver = base_name(access.receiver)
                confidence, reason = self._grade_subscript(receiver, owner)
                findings.append(
                    ImpactFinding(
                        reference=CodeReference(
                            path=path,
                            line=access.key_range.line,
                            col=access.key_range.col,
                            end_line=access.key_range.end_line,
                            end_col=access.key_range.end_col,
                            snippet=module.line_text(access.range.line),
                        ),
                        symbol=access.symbol,
                        matched_contract=f'{access.receiver or "<expr>"}["{old}"]',
                        access=AccessKind.SUBSCRIPT,
                        reason=reason,
                        confidence=confidence,
                        source_text=_span(module.source, access.key_range),
                    )
                )

            for access in module.get_calls:
                if access.key != old:
                    continue
                receiver = base_name(access.receiver)
                confidence, reason = self._grade_subscript(receiver, owner)
                findings.append(
                    ImpactFinding(
                        reference=CodeReference(
                            path=path,
                            line=access.key_range.line,
                            col=access.key_range.col,
                            end_line=access.key_range.end_line,
                            end_col=access.key_range.end_col,
                            snippet=module.line_text(access.range.line),
                        ),
                        symbol=access.symbol,
                        matched_contract=f'{access.receiver or "<expr>"}.get("{old}")',
                        access=AccessKind.DICT_GET,
                        reason=reason,
                        confidence=confidence,
                        source_text=_span(module.source, access.key_range),
                    )
                )

            for access in module.attributes:
                if access.attr != old:
                    continue
                receiver = base_name(access.receiver)
                confidence, reason = self._grade_attribute(receiver, owner)
                findings.append(
                    ImpactFinding(
                        reference=CodeReference(
                            path=path,
                            line=access.attr_range.line,
                            col=access.attr_range.col,
                            end_line=access.attr_range.end_line,
                            end_col=access.attr_range.end_col,
                            snippet=module.line_text(access.range.line),
                        ),
                        symbol=access.symbol,
                        matched_contract=f"{access.receiver or '<expr>'}.{old}",
                        access=AccessKind.ATTRIBUTE,
                        reason=reason,
                        confidence=confidence,
                        source_text=_span(module.source, access.attr_range),
                        patchable=confidence >= Confidence.MEDIUM,
                        unpatchable_reason=(
                            ""
                            if confidence >= Confidence.MEDIUM
                            else "attribute access on an unrelated receiver"
                        ),
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

    def _grade_subscript(self, receiver: str, owner: str) -> tuple[Confidence, str]:
        """Grade a dict-style access.

        A constant string key exactly matching a renamed field is strong
        evidence on its own -- dicts are how API responses arrive in Python --
        so the floor here is MEDIUM rather than LOW.
        """
        if owner and receiver == owner:
            return (
                Confidence.HIGH,
                f"dict access with the renamed key on `{owner}`, the object the "
                f"change document names",
            )
        if owner:
            return (
                Confidence.MEDIUM,
                f"dict access with the renamed key on `{receiver or '<expr>'}` "
                f"(the change names `{owner}`, but response objects are commonly "
                f"bound to other names)",
            )
        return (
            Confidence.MEDIUM,
            "dict access with the renamed key; the change document does not name "
            "an owning object, so the receiver could not be checked",
        )

    def _grade_attribute(self, receiver: str, owner: str) -> tuple[Confidence, str]:
        """Grade an attribute access.

        Inverted relative to subscripts: ``.total`` on an arbitrary object is
        weak evidence, because attribute names collide across unrelated
        libraries. Only a receiver match rescues it.
        """
        if owner and receiver == owner:
            return (
                Confidence.HIGH,
                f"attribute access on `{owner}`, the object the change document names",
            )
        if owner:
            return (
                Confidence.LOW,
                f"attribute named `{receiver}.<field>` but the change names owner "
                f"`{owner}`; likely an unrelated object",
            )
        return (
            Confidence.LOW,
            "attribute access with a matching name, but the change document names "
            "no owning object, so this cannot be distinguished from an unrelated "
            "attribute",
        )

    # -- planning ----------------------------------------------------------

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
                f"Rename the `{old}` field to `{new}` at every data access that "
                f"reads it. Only subscript, `.get()`, and attribute accesses are "
                f"rewritten; string literals that merely contain the word `{old}` "
                f"are not field accesses and are left alone."
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
                    f"{finding.confidence.value} is below the `{config.min_confidence.value}` "
                    f"threshold"
                )
                continue

            reference = finding.reference
            if finding.access is AccessKind.ATTRIBUTE:
                old_text, new_text = old, new
            else:
                # The recorded span is the quoted literal; preserve quote style.
                old_text = finding.source_text or f'"{old}"'
                new_text = quoted_replacement(old_text, new)

            plan.transformations.append(
                Transformation(
                    reference=reference,
                    old=old_text,
                    new=new_text,
                    symbol=finding.symbol,
                    confidence=finding.confidence,
                    edit=TextEdit(
                        line=reference.line,
                        col=reference.col,
                        end_line=reference.end_line or reference.line,
                        end_col=reference.end_col or reference.col,
                        new_text=new_text,
                        description=f"rename `{old}` to `{new}` ({finding.access.value})",
                    ),
                )
            )

        if not plan.transformations:
            plan.blocked_reason = (
                f"found {len(report.findings)} reference(s) to `{old}` but none met "
                f"the `{config.min_confidence.value}` confidence threshold for "
                f"automatic rewriting"
                if report.findings
                else f"no field accesses to `{old}` were found"
            )
        return plan


def _span(source: str, source_range) -> str:
    """The exact source text a single-line range covers."""
    lines = source.splitlines()
    if not (1 <= source_range.line <= len(lines)):
        return ""
    if source_range.end_line != source_range.line:
        return ""
    return lines[source_range.line - 1][source_range.col : source_range.end_col]


def _related_tests(index: RepoIndex, findings: list[ImpactFinding]) -> list[str]:
    from patchahead.testing import discovery

    return discovery.tests_for_paths(index, [f.path for f in findings])


register(FieldRenameHandler())
