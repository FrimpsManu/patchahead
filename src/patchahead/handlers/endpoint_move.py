"""Endpoint move: ``.../pages/deployment`` -> ``.../pages/deployments``.

An API moves an endpoint, and code that builds the URL itself keeps calling the
old one::

    session.post(f"{BASE}/repos/{owner}/{repo}/pages/deployment", json=body)

This family handles the moves that change only **fixed words** in the path:
the placeholders keep their names and positions, and one run of fixed segments
is replaced by another (``deployment`` -> ``deployments``, ``v1/orders`` ->
``v2/orders``). A move that changes the placeholders --
``/repositories/{repository_id}`` -> ``/repos/{owner}/{repo}`` -- needs other
values in the URL, which is a decision rather than a rename, and is reported.

The change's owner is the old endpoint, ``POST [/v2]/repos/{owner}/{repo}/pages/deployment``,
written as for :mod:`patchahead.handlers.query_params`; ``symbol`` and
``replacement`` are the old and new paths. A call is rewritten when its verb and
URL address the old endpoint (:class:`patchahead.analysis.http.Endpoint`) and the
moved words appear exactly once in a string literal written in the call itself.
A URL whose moved part comes from a name bound elsewhere -- a constant other
calls may share -- is reported, not edited.
"""

from __future__ import annotations

import ast
import logging
import re

from patchahead.analysis.http import Endpoint, http_calls, segments
from patchahead.analysis.index import RepoIndex
from patchahead.analysis.python_ast import SourceRange
from patchahead.config import Config
from patchahead.domain.change import BreakingChange, ChangeKind, Confidence
from patchahead.domain.impact import AccessKind, CodeReference, ImpactFinding, ImpactReport
from patchahead.domain.plan import MigrationPlan, Risk, TextEdit, Transformation
from patchahead.handlers.base import MigrationHandler, analyzed_paths, register

log = logging.getLogger(__name__)


def moved_run(old_path: str, new_path: str) -> tuple[str, str] | None:
    """The fixed words that changed, as ``("/deployment", "/deployments")``.

    ``None`` unless the two paths differ in exactly one run of fixed segments,
    non-empty on both sides, with every placeholder unchanged.
    """
    old, new = segments(old_path), segments(new_path)
    if old == new:
        return None
    start = 0
    while start < min(len(old), len(new)) and old[start] == new[start]:
        start += 1
    end = 0
    while (
        end < min(len(old), len(new)) - start and old[len(old) - 1 - end] == new[len(new) - 1 - end]
    ):
        end += 1
    before, after = old[start : len(old) - end], new[start : len(new) - end]
    if not before or not after:
        return None
    if any(part.startswith("{") for part in (*before, *after)):
        return None
    return "/" + "/".join(before), "/" + "/".join(after)


class EndpointMoveHandler(MigrationHandler):
    """Rewrites the fixed words of a moved endpoint's path in the URLs that call it."""

    name = "endpoint_move"
    kinds = (ChangeKind.ENDPOINT_MOVE,)
    summary = "Move an endpoint: get(f'{BASE}/pages/deployment') -> '.../deployments'"
    limitations = (
        "Only moves that change fixed words in the path; a move that changes the "
        "placeholders, or only adds or removes segments, is reported.",
        "The moved words must be written in the call's own URL string; a URL built "
        "from a constant defined elsewhere is reported.",
    )

    def supports(self, change: BreakingChange) -> bool:
        target = change.target
        return (
            change.kind in self.kinds
            and Endpoint.parse(target.owner) is not None
            and moved_run(target.symbol, target.replacement) is not None
        )

    def analyze(self, change: BreakingChange, index: RepoIndex, config: Config) -> ImpactReport:
        endpoint = Endpoint.parse(change.target.owner)
        run = moved_run(change.target.symbol, change.target.replacement)
        assert endpoint is not None and run is not None  # supports() checked
        old_words, new_words = run
        findings: list[ImpactFinding] = []
        for path in analyzed_paths(index, config):
            module = index.modules[path]
            lines = module.source.splitlines()
            for site in http_calls(module.tree, module.columns):
                if site.method != endpoint.method or not endpoint.matches(site.url):
                    continue
                spans = [
                    span
                    for literal in site.literals
                    for span in _occurrences(literal, lines, module.columns, old_words)
                ]
                contract = f"{site.callee}({site.shown_url})"
                if len(spans) == 1:
                    findings.append(
                        _finding(
                            path,
                            module,
                            spans[0],
                            site.symbol,
                            contract,
                            Confidence.HIGH,
                            f"a `{endpoint.method}` call to `{site.shown_url}`, which addresses "
                            f"`{endpoint}`; `{old_words}` moved to `{new_words}`",
                            True,
                            "",
                            old_words,
                        )
                    )
                    continue
                why = (
                    f"`{old_words}` appears {len(spans)} times in the URL"
                    if spans
                    else f"`{old_words}` is not written in this call's URL; it comes "
                    "from a name bound elsewhere, which other calls may share"
                )
                findings.append(
                    _finding(
                        path,
                        module,
                        site.call_range,
                        site.symbol,
                        contract,
                        Confidence.LOW,
                        f"a call to `{endpoint}` whose URL cannot be rewritten in place: {why}",
                        False,
                        why,
                        old_words,
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

    def plan(
        self, change: BreakingChange, report: ImpactReport, index: RepoIndex, config: Config
    ) -> MigrationPlan:
        run = moved_run(change.target.symbol, change.target.replacement)
        assert run is not None
        old_words, new_words = run
        plan = MigrationPlan(
            change=change,
            handler=self.name,
            expected_tests=list(report.related_tests),
            risk=Risk.LOW,
            rationale=(
                f"Replace `{old_words}` with `{new_words}` in the URLs of calls to "
                f"`{change.target.owner}`. Only those words change; the base URL, the "
                f"interpolated values, and the rest of the call are untouched."
            ),
        )
        for finding in report.findings:
            if not finding.patchable:
                plan.skipped.append(f"{finding.reference}: {finding.unpatchable_reason}")
                continue
            if finding.confidence < config.min_confidence:
                plan.skipped.append(
                    f"{finding.reference}: confidence {finding.confidence.value} is below "
                    f"the `{config.min_confidence.value}` threshold"
                )
                continue
            reference = finding.reference
            plan.transformations.append(
                Transformation(
                    reference=reference,
                    old=old_words,
                    new=new_words,
                    symbol=finding.symbol,
                    confidence=finding.confidence,
                    edit=TextEdit(
                        line=reference.line,
                        col=reference.col,
                        end_line=reference.end_line or reference.line,
                        end_col=reference.end_col or reference.col,
                        new_text=new_words,
                        description=f"move `{old_words}` to `{new_words}` in the URL",
                    ),
                )
            )
        if not plan.transformations:
            plan.blocked_reason = (
                f"found {len(report.findings)} call(s) to `{change.target.owner}`, but none "
                "had a URL that could be rewritten in place"
                if report.findings
                else f"no calls to `{change.target.owner}` were found"
            )
        return plan


def _occurrences(
    literal: ast.Constant | ast.JoinedStr, lines: list[str], columns, words: str
) -> list[SourceRange]:
    """Where ``words`` is written in a single-line literal, as whole path segments.

    In an f-string, only the literal text counts, never the inside of ``{...}``.
    A match must end the segment: be followed by ``/``, ``?``, ``#``, or the
    string's end, so ``/deployment`` does not match inside ``/deployments``.
    """
    where = SourceRange.of(literal, columns)
    if where.line != where.end_line or not (1 <= where.line <= len(lines)):
        return []
    text = lines[where.line - 1][where.col : where.end_col]
    literal_text = _outside_braces(text) if isinstance(literal, ast.JoinedStr) else None
    found = []
    for match in re.finditer(re.escape(words), text):
        start, end = match.span()
        if literal_text is not None and not all(literal_text[start:end]):
            continue
        follower = text[end : end + 1]
        if follower and follower not in "/?#'\"":
            continue
        found.append(
            SourceRange(
                line=where.line,
                col=where.col + start,
                end_line=where.line,
                end_col=where.col + end,
            )
        )
    return found


def _outside_braces(text: str) -> list[bool]:
    """For each character of an f-string's source, whether it is literal text."""
    flags = []
    depth = 0
    index = 0
    while index < len(text):
        pair = text[index : index + 2]
        if depth == 0 and pair in ("{{", "}}"):
            flags += [True, True]
            index += 2
            continue
        char = text[index]
        if char == "{":
            depth += 1
        flags.append(depth == 0)
        if char == "}" and depth:
            depth -= 1
        index += 1
    return flags


def _finding(
    path, module, source_range, symbol, contract, confidence, reason, patchable, blocked, text
) -> ImpactFinding:
    return ImpactFinding(
        reference=CodeReference(
            path=path,
            line=source_range.line,
            col=source_range.col,
            end_line=source_range.end_line,
            end_col=source_range.end_col,
            snippet=module.line_text(source_range.line),
        ),
        symbol=symbol,
        matched_contract=contract,
        access=AccessKind.URL,
        reason=reason,
        confidence=confidence,
        source_text=text,
        patchable=patchable,
        unpatchable_reason=blocked,
    )


def _related_tests(index: RepoIndex, findings: list[ImpactFinding]) -> list[str]:
    from patchahead.testing import discovery

    return discovery.tests_for_paths(index, [f.path for f in findings])


register(EndpointMoveHandler())
