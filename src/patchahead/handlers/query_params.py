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

import ast
import logging
import re
from dataclasses import dataclass

from patchahead.analysis.index import RepoIndex
from patchahead.analysis.python_ast import (
    ColumnMap,
    SourceRange,
    _keyword_name_range,
    iter_own_scope,
    receiver_name,
)
from patchahead.config import Config
from patchahead.domain.change import BreakingChange, ChangeKind, Confidence
from patchahead.domain.impact import AccessKind, CodeReference, ImpactFinding, ImpactReport
from patchahead.domain.plan import MigrationPlan, Risk, TextEdit, Transformation
from patchahead.handlers.base import MigrationHandler, analyzed_paths, register
from patchahead.handlers.field_rename import quoted_replacement

log = logging.getLogger(__name__)

_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}
_WILD = "\0"


@dataclass(frozen=True)
class Endpoint:
    """``GET [/v2]/orders``: a verb, the server's base path, and the path."""

    method: str
    base: tuple[str, ...]
    path: tuple[str, ...]

    @classmethod
    def parse(cls, owner: str) -> Endpoint | None:
        match = re.fullmatch(r"\s*([A-Za-z]+)\s+(?:\[([^\]]*)\])?(\S+)\s*", owner or "")
        if not match:
            return None
        return cls(
            match.group(1).upper(),
            _segments(match.group(2) or ""),
            _segments(match.group(3)),
        )

    def __str__(self) -> str:
        base = f"[/{'/'.join(self.base)}]" if self.base else ""
        return f"{self.method} {base}/{'/'.join(self.path)}"

    def matches(self, url: str) -> bool:
        """Whether a URL, as written in code, addresses this endpoint."""
        text = url.split("?", 1)[0].split("#", 1)[0]
        leading = text.startswith(_WILD)
        if leading:
            text = text[1:]
        elif re.match(r"https?://", text):
            text = re.sub(r"^https?://[^/]*", "", text)
        written = _segments(text)
        options = [self.base + self.path, self.path] if self.base else [self.path]
        return any(_same(written, wanted) for wanted in options) and (
            leading or text.startswith("/") or not written
        )


def _segments(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.strip().split("/") if part)


def _same(written: tuple[str, ...], wanted: tuple[str, ...]) -> bool:
    if len(written) != len(wanted):
        return False
    for code, spec in zip(written, wanted, strict=True):
        placeholder = spec.startswith("{") and spec.endswith("}")
        if _WILD in code:
            # An interpolated value is only known to fill a placeholder.
            if not placeholder:
                return False
        elif not placeholder and code != spec:
            return False
    return True


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
            for site in _http_calls(module.tree, module.columns):
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


# --------------------------------------------------------------------------
# reading HTTP calls
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Key:
    name: str
    #: Range of the key as written: the quoted string, or the keyword's name.
    range: SourceRange
    text: str


@dataclass
class _Site:
    method: str
    url: str
    shown_url: str
    callee: str
    symbol: str
    call_range: SourceRange
    keys: list[_Key]
    blocked: str = ""


def _http_calls(tree: ast.Module, columns: ColumnMap | None) -> list[_Site]:
    collector = _Calls(tree, columns)
    collector.visit(tree)
    return collector.sites


class _Calls(ast.NodeVisitor):
    def __init__(self, tree: ast.Module, columns: ColumnMap | None) -> None:
        self.columns = columns
        self.sites: list[_Site] = []
        self._module = _once_bound(tree)
        self._scopes: list[tuple[str, ast.AST, dict[str, ast.AST], dict[str, int]]] = []

    def _enter(self, node) -> None:
        bound = _once_bound(node) if not isinstance(node, ast.ClassDef) else {}
        uses = _load_counts(node)
        self._scopes.append((node.name, node, bound, uses))
        self.generic_visit(node)
        self._scopes.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter
    visit_ClassDef = _enter

    def visit_Call(self, node: ast.Call) -> None:
        site = self._site(node)
        if site is not None:
            self.sites.append(site)
        self.generic_visit(node)

    def _value_of(self, name: str) -> ast.AST | None:
        """What a name is bound to, when it is bound exactly once where it is used.

        Enclosing functions are searched outward, as Python resolves a free
        name; a class body is not, since its names are not visible to methods.
        """
        for _, scope, bound, _ in reversed(self._scopes):
            if isinstance(scope, ast.ClassDef):
                continue
            if name in bound:
                return bound[name]
            if _binds(scope, name):
                return None
        return self._module.get(name)

    def _site(self, node: ast.Call) -> _Site | None:
        func = node.func
        if not isinstance(func, ast.Attribute):
            return None
        verb = func.attr.lower()
        args = list(node.args)
        if verb == "request":
            if (
                not args
                or not isinstance(args[0], ast.Constant)
                or not isinstance(args[0].value, str)
            ):
                return None
            verb, args = args[0].value.lower(), args[1:]
        if verb not in _VERBS:
            return None
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        url_node = args[0] if args else keywords.get("url")
        params = keywords.get("params")
        if params is None and verb == "get" and receiver_name(func.value) == "requests":
            # `requests.get(url, params)` -- the one function that takes it
            # positionally.
            params = args[1] if len(args) > 1 else None
        if url_node is None or params is None:
            return None
        url = self._url(url_node)
        if url is None:
            return None
        keys, blocked = self._params(params)
        return _Site(
            method=verb.upper(),
            url=url,
            shown_url=url.replace(_WILD, "{...}"),
            callee=f"{receiver_name(func.value) or '<expr>'}.{func.attr}",
            symbol=".".join(name for name, *_ in self._scopes) or "<module>",
            call_range=SourceRange.of(node, self.columns),
            keys=keys,
            blocked=blocked,
        )

    def _url(self, node: ast.AST, depth: int = 0) -> str | None:
        """The URL as written, with every interpolated value replaced by a wildcard."""
        if depth > 5:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value.replace(_WILD, "")
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                else:
                    parts.append(_WILD)
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._url(node.left, depth + 1)
            right = self._url(node.right, depth + 1)
            return None if left is None or right is None else left + right
        if isinstance(node, ast.Name):
            value = self._value_of(node.id)
            if value is not None:
                resolved = self._url(value, depth + 1)
                if resolved is not None:
                    return resolved
            return _WILD
        return _WILD

    def _params(self, node: ast.AST, depth: int = 0) -> tuple[list[_Key], str]:
        if isinstance(node, ast.Dict):
            keys = []
            for key in node.keys:
                if key is None:
                    return [], "a `**` expansion in the parameters dictionary"
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.append(_Key(key.value, SourceRange.of(key, self.columns), ""))
            return keys, ""
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "dict"
            and not node.args
        ):
            keys = []
            for keyword in node.keywords:
                if keyword.arg is None:
                    return [], "a `**` expansion in `dict(...)`"
                keys.append(
                    _Key(keyword.arg, _keyword_name_range(keyword, self.columns), keyword.arg)
                )
            return keys, ""
        if isinstance(node, ast.Name) and depth == 0 and self._scopes:
            # Only a dictionary this function builds for this call: editing one
            # bound elsewhere would change it for every other user too.
            _, scope, bound, uses = self._scopes[-1]
            value = bound.get(node.id) if not isinstance(scope, ast.ClassDef) else None
            if value is None:
                return [], f"`{node.id}` is not a dictionary built once in this function"
            if uses.get(node.id, 0) > 1:
                return [], f"`{node.id}` is also used elsewhere in the function"
            return self._params(value, depth + 1)
        return [], "the parameters are built in a way PatchAhead cannot read"


def _once_bound(scope: ast.AST) -> dict[str, ast.AST]:
    """Names bound exactly once in ``scope``'s own body, by a plain assignment."""
    counts: dict[str, int] = {}
    values: dict[str, ast.AST] = {}
    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        arguments = scope.args
        for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]:
            counts[arg.arg] = counts.get(arg.arg, 0) + 1
    for node in iter_own_scope(scope):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            counts[node.id] = counts.get(node.id, 0) + 1
    # `global`/`nonlocal` anywhere below -- in this scope or a nested function --
    # means some other code can rebind the name.
    for node in ast.walk(scope):
        if isinstance(node, ast.Global | ast.Nonlocal):
            for name in node.names:
                counts[name] = counts.get(name, 0) + 2
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            values[node.targets[0].id] = node.value
    return {name: value for name, value in values.items() if counts.get(name) == 1}


def _binds(scope: ast.AST, name: str) -> bool:
    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        arguments = scope.args
        if name in {
            a.arg for a in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
        }:
            return True
    return any(
        isinstance(node, ast.Name) and node.id == name and not isinstance(node.ctx, ast.Load)
        for node in iter_own_scope(scope)
    )


def _load_counts(scope: ast.AST) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in ast.walk(scope):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            counts[node.id] = counts.get(node.id, 0) + 1
    return counts


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
