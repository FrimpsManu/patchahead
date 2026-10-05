"""Query parameter rename: ``params={"page": 2}`` -> ``params={"cursor": 2}``.

Code that calls an HTTP API directly passes query parameters as a dictionary::

    requests.get(f"{BASE_URL}/orders", params={"page": page, "limit": 50})

A parameter name is a common word, and the same ``"page"`` key is sent to every
paginated endpoint an application calls. So the change names one endpoint --
its owner is ``GET /orders``, or ``GET [/v2]/orders`` with the server's base
path in brackets -- and a key is renamed only in a call that provably targets it:

- **The verb matches:** ``.get(...)`` for ``GET``, or ``.request("GET", ...)``.
- **The URL matches the path**, read from the call's URL argument: a string, an
  f-string, a ``+`` concatenation, or a name bound once to one of those. A
  ``{placeholder}`` in the spec's path matches any segment; an interpolated
  value in the URL matches only a placeholder. One interpolated value may lead
  the URL (``f"{BASE_URL}/orders"``) and stand for the scheme, the host and the
  base path. Otherwise the path must match exactly, with or without the base
  path, so ``f"{BASE}/customers/{id}/orders"`` never matches ``GET /orders``.
- **The parameters can be seen:** a dictionary literal, ``dict(page=...)``, or a
  name bound once in the function to one of those and used nowhere else. A
  dictionary built any other way is reported, not rewritten.

Only the key is replaced, in its original quote style; values are untouched.
"""

from __future__ import annotations

import logging

from patchahead.analysis.http import Endpoint, http_calls
from patchahead.analysis.index import RepoIndex
from patchahead.config import Config
from patchahead.domain.change import BreakingChange, ChangeKind, Confidence
from patchahead.domain.impact import AccessKind, CodeReference, ImpactFinding, ImpactReport
from patchahead.domain.plan import MigrationPlan, Risk, TextEdit, Transformation
from patchahead.handlers.base import MigrationHandler, analyzed_paths, register
from patchahead.handlers.field_rename import quoted_replacement

log = logging.getLogger(__name__)


class QueryParamRenameHandler(MigrationHandler):
    """Renames a query parameter in the calls that address one endpoint."""

    name = "query_param_rename"
    kinds = (ChangeKind.QUERY_PARAM_RENAME,)
    summary = 'Rename an HTTP query parameter: get(url, params={"old": x}) -> {"new": x}'
    limitations = (
        "Only `params=` passed as a dictionary literal, `dict(...)`, or a name bound "
        "once to one of those; a dictionary built up elsewhere is reported.",
        "The URL must be readable from the call: a string, an f-string, a `+` "
        "concatenation, or a name bound once to one of those.",
        "Query parameters only; a renamed header, cookie, or body field is reported.",
    )

    def supports(self, change: BreakingChange) -> bool:
        return (
            change.kind in self.kinds
            and change.target.is_rename
            and Endpoint.parse(change.target.owner) is not None
        )

    def analyze(self, change: BreakingChange, index: RepoIndex, config: Config) -> ImpactReport:
        endpoint = Endpoint.parse(change.target.owner)
        assert endpoint is not None  # supports() checked
        old, new = change.target.symbol, change.target.replacement
        findings: list[ImpactFinding] = []
        for path in analyzed_paths(index, config):
            module = index.modules[path]
            for site in http_calls(module.tree, module.columns):
                if not site.has_params:
                    continue
                if site.method != endpoint.method or not endpoint.matches(site.url):
                    continue
                findings.extend(self._findings(path, module, site, endpoint, old, new))

        findings.sort(key=lambda f: (f.reference.path, f.reference.line, f.reference.col))
        return ImpactReport(
            change=change,
            findings=findings,
            related_tests=_related_tests(index, findings),
            files_scanned=index.file_count,
            skipped_files=dict(index.skipped),
        )

    def _findings(self, path, module, site, endpoint, old, new) -> list[ImpactFinding]:
        call = f"{site.callee}({site.shown_url}, params=...)"
        if site.blocked:
            return [
                _finding(
                    path,
                    module,
                    site.call_range,
                    site.symbol,
                    call,
                    Confidence.LOW,
                    f"a call to `{endpoint}` whose query parameters cannot be read: {site.blocked}",
                    False,
                    site.blocked,
                    old,
                )
            ]
        keys = [key for key in site.keys if key.name == old]
        if not keys:
            return []
        if any(key.name == new for key in site.keys):
            return [
                _finding(
                    path,
                    module,
                    keys[0].range,
                    site.symbol,
                    call,
                    Confidence.LOW,
                    f"the call to `{endpoint}` already passes `{new}` as well as `{old}`",
                    False,
                    f"`{new}` is already passed; renaming `{old}` would duplicate it",
                    keys[0].text,
                )
            ]
        return [
            _finding(
                path,
                module,
                key.range,
                site.symbol,
                call,
                Confidence.HIGH,
                f"query parameter `{old}` on a `{endpoint.method}` call whose URL "
                f"`{site.shown_url}` addresses `{endpoint}`, the endpoint the change names",
                True,
                "",
                key.text,
            )
            for key in keys
        ]

    def plan(
        self, change: BreakingChange, report: ImpactReport, index: RepoIndex, config: Config
    ) -> MigrationPlan:
        old, new = change.target.symbol, change.target.replacement
        plan = MigrationPlan(
            change=change,
            handler=self.name,
            expected_tests=list(report.related_tests),
            risk=Risk.LOW,
            rationale=(
                f"Rename the `{old}` query parameter to `{new}` in calls to "
                f"`{change.target.owner}`. Only the key is replaced; the value, the "
                f"other parameters, and the URL are untouched."
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
            old_text = finding.source_text
            # A dictionary key is a quoted string; a `dict(...)` keyword is a name.
            quoted = old_text[-1:] in ("'", '"')
            new_text = quoted_replacement(old_text, new) if quoted else new
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
                        description=f"rename query parameter `{old}` to `{new}`",
                    ),
                )
            )
        if not plan.transformations:
            plan.blocked_reason = (
                f"found {len(report.findings)} call(s) to `{change.target.owner}` passing "
                f"`{old}`, but none could be rewritten safely"
                if report.findings
                else f"no calls to `{change.target.owner}` pass `{old}`"
            )
        return plan


def _finding(
    path, module, source_range, symbol, contract, confidence, reason, patchable, blocked, text
) -> ImpactFinding:
    if not text:
        line = module.source.splitlines()[source_range.line - 1]
        text = line[source_range.col : source_range.end_col]
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
        access=AccessKind.QUERY_PARAM,
        reason=reason,
        confidence=confidence,
        source_text=text,
        patchable=patchable,
        unpatchable_reason=blocked,
    )


def _related_tests(index: RepoIndex, findings: list[ImpactFinding]) -> list[str]:
    from patchahead.testing import discovery

    return discovery.tests_for_paths(index, [f.path for f in findings])


register(QueryParamRenameHandler())
