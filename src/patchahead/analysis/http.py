"""Reading the HTTP calls a module makes: verb, URL, and query parameters.

Shared by the handlers that work on code calling an API directly
(:mod:`patchahead.handlers.query_params`, :mod:`patchahead.handlers.endpoint_move`).
A call counts when its callee is ``.get``/``.post``/... or ``.request("GET", ...)``
and its URL can be read: a string, an f-string, a ``+`` concatenation, or a name
bound once to one of those, with every interpolated value replaced by
:data:`WILD`. An :class:`Endpoint` says whether such a URL addresses it.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from patchahead.analysis.python_ast import (
    ColumnMap,
    SourceRange,
    _keyword_name_range,
    iter_own_scope,
    receiver_name,
)

VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}
WILD = "\0"


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
            segments(match.group(2) or ""),
            segments(match.group(3)),
        )

    def __str__(self) -> str:
        base = f"[/{'/'.join(self.base)}]" if self.base else ""
        return f"{self.method} {base}/{'/'.join(self.path)}"

    def matches(self, url: str) -> bool:
        """Whether a URL, as written in code, addresses this endpoint."""
        text = url.split("?", 1)[0].split("#", 1)[0]
        leading = text.startswith(WILD)
        if leading:
            text = text[1:]
        elif re.match(r"https?://", text):
            text = re.sub(r"^https?://[^/]*", "", text)
        written = segments(text)
        options = [self.base + self.path, self.path] if self.base else [self.path]
        return any(_same(written, wanted) for wanted in options) and (
            leading or text.startswith("/") or not written
        )


def segments(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.strip().split("/") if part)


def _same(written: tuple[str, ...], wanted: tuple[str, ...]) -> bool:
    if len(written) != len(wanted):
        return False
    for code, spec in zip(written, wanted, strict=True):
        placeholder = spec.startswith("{") and spec.endswith("}")
        if WILD in code:
            # An interpolated value is only known to fill a placeholder.
            if not placeholder:
                return False
        elif not placeholder and code != spec:
            return False
    return True


@dataclass(frozen=True)
class Key:
    name: str
    #: Range of the key as written: the quoted string, or the keyword's name.
    range: SourceRange
    text: str


@dataclass
class HttpCall:
    method: str
    #: The URL as written, :data:`WILD` for each interpolated value.
    url: str
    shown_url: str
    callee: str
    symbol: str
    call_range: SourceRange
    #: The query parameters' keys, when ``params=`` is passed and readable.
    keys: list[Key] = field(default_factory=list)
    #: Why ``params=`` could not be read, when it is passed and could not be.
    blocked: str = ""
    has_params: bool = False
    #: The string literals written in the call's own URL argument, in order.
    #: A part read through a name is not among them: its text may be shared.
    literals: list[ast.Constant | ast.JoinedStr] = field(default_factory=list)


def http_calls(tree: ast.Module, columns: ColumnMap | None) -> list[HttpCall]:
    collector = _Calls(tree, columns)
    collector.visit(tree)
    return collector.sites


class _Calls(ast.NodeVisitor):
    def __init__(self, tree: ast.Module, columns: ColumnMap | None) -> None:
        self.columns = columns
        self.sites: list[HttpCall] = []
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

    def _site(self, node: ast.Call) -> HttpCall | None:
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
        if verb not in VERBS:
            return None
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        url_node = args[0] if args else keywords.get("url")
        params = keywords.get("params")
        if params is None and verb == "get" and receiver_name(func.value) == "requests":
            # `requests.get(url, params)` -- the one function that takes it
            # positionally.
            params = args[1] if len(args) > 1 else None
        if url_node is None:
            return None
        literals: list[ast.Constant | ast.JoinedStr] = []
        url = self._url(url_node, literals=literals)
        if url is None:
            return None
        keys, blocked = self._params(params) if params is not None else ([], "")
        return HttpCall(
            method=verb.upper(),
            url=url,
            shown_url=url.replace(WILD, "{...}"),
            callee=f"{receiver_name(func.value) or '<expr>'}.{func.attr}",
            symbol=".".join(name for name, *_ in self._scopes) or "<module>",
            call_range=SourceRange.of(node, self.columns),
            keys=keys,
            blocked=blocked,
            has_params=params is not None,
            literals=literals,
        )

    def _url(self, node: ast.AST, depth: int = 0, literals: list | None = None) -> str | None:
        """The URL as written, with every interpolated value replaced by a wildcard.

        ``literals`` collects the string literals written in the call itself,
        not those read through a name, which may be shared with other calls.
        """
        if depth > 5:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if literals is not None:
                literals.append(node)
            return node.value.replace(WILD, "")
        if isinstance(node, ast.JoinedStr):
            if literals is not None:
                literals.append(node)
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                else:
                    parts.append(WILD)
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._url(node.left, depth + 1, literals)
            right = self._url(node.right, depth + 1, literals)
            return None if left is None or right is None else left + right
        if isinstance(node, ast.Name):
            value = self._value_of(node.id)
            if value is not None:
                resolved = self._url(value, depth + 1)
                if resolved is not None:
                    return resolved
            return WILD
        return WILD

    def _params(self, node: ast.AST, depth: int = 0) -> tuple[list[Key], str]:
        if isinstance(node, ast.Dict):
            keys = []
            for key in node.keys:
                if key is None:
                    return [], "a `**` expansion in the parameters dictionary"
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.append(Key(key.value, SourceRange.of(key, self.columns), ""))
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
                    Key(keyword.arg, _keyword_name_range(keyword, self.columns), keyword.arg)
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
