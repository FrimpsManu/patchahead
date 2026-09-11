"""AST analysis of Python source.

The prototype matched source text with regular expressions, which produced more
false positives than true ones (see ``docs/assessment.md`` §2.2). This module
replaces that with a single pass over the syntax tree that records the four
construct types the v1 migration families care about:

* subscripts with a constant string key -- ``order["total"]``
* ``.get("key")`` calls -- ``order.get("total")``
* attribute access -- ``order.total``
* calls and their keyword arguments -- ``fetch(timeout_seconds=30)``

Each record carries an exact source range, so patching can edit that range
instead of regenerating the file. That is what keeps diffs minimal and leaves
comments and formatting untouched.

The tree is walked once per file and the result is cached by
:class:`patchahead.analysis.index.RepoIndex`.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field

#: Node types that introduce a new enclosing scope for symbol naming.
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

MODULE_SCOPE = "<module>"


class ParseError(Exception):
    """Raised when a file cannot be parsed as Python."""


@dataclass(frozen=True)
class SourceRange:
    """An exact half-open range in a source file.

    ``line``/``end_line`` are 1-indexed; ``col``/``end_col`` are 0-indexed and
    ``end_col`` is exclusive -- identical to ``ast`` node attributes.
    """

    line: int
    col: int
    end_line: int
    end_col: int

    @classmethod
    def of(cls, node: ast.AST) -> SourceRange:
        return cls(
            line=node.lineno,
            col=node.col_offset,
            end_line=getattr(node, "end_lineno", None) or node.lineno,
            end_col=getattr(node, "end_col_offset", None) or node.col_offset,
        )


def receiver_name(node: ast.AST) -> str:
    """Best-effort dotted name of the expression a construct hangs off.

    ``order`` for ``order["total"]``, ``self.cache`` for ``self.cache["total"]``,
    ``get_orders()`` for ``get_orders()["total"]``, ``""`` when the receiver is
    something we cannot name (a comprehension, a subscript chain, a literal).

    This is the main signal for scoping a rename. It is a *name*, not a type:
    PatchAhead does no type inference, and this function must not be read as
    though it does. It narrows candidate sites; confidence grading handles the
    residual uncertainty.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = receiver_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        base = receiver_name(node.func)
        return f"{base}()" if base else ""
    return ""


def base_name(dotted: str) -> str:
    """The leftmost segment of a dotted receiver name (``a.b.c`` -> ``a``)."""
    return dotted.split(".", 1)[0].removesuffix("()") if dotted else ""


def called_name(node: ast.Call) -> str:
    """The name being called: ``list_orders`` for ``client.list_orders(...)``."""
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


@dataclass
class SubscriptAccess:
    """``<receiver>["<key>"]`` with a constant string key."""

    key: str
    receiver: str
    #: Range of the whole ``x["k"]`` expression.
    range: SourceRange
    #: Range of just the ``"k"`` literal, including its quotes.
    key_range: SourceRange
    symbol: str
    #: True for a store or delete context (``x["k"] = v``), which a rename
    #: must still handle but which reads differently in a report.
    is_write: bool = False


@dataclass
class GetCallAccess:
    """``<receiver>.get("<key>")`` or ``.get("<key>", default)``."""

    key: str
    receiver: str
    range: SourceRange
    key_range: SourceRange
    symbol: str


@dataclass
class AttributeAccess:
    """``<receiver>.<attr>`` outside of a call position."""

    attr: str
    receiver: str
    range: SourceRange
    #: Range of just the attribute name, excluding the dot.
    attr_range: SourceRange
    symbol: str
    is_write: bool = False


@dataclass
class CallSite:
    """A function or method call, with its keyword arguments."""

    name: str
    receiver: str
    range: SourceRange
    #: Range of just the callee name.
    name_range: SourceRange
    symbol: str
    #: keyword name -> range of the ``name=`` token (name only, not the value).
    keywords: dict[str, SourceRange] = field(default_factory=dict)
    node: ast.Call | None = None


@dataclass
class ModuleAnalysis:
    """Everything one pass over one module recorded."""

    path: str
    source: str
    tree: ast.Module
    subscripts: list[SubscriptAccess] = field(default_factory=list)
    get_calls: list[GetCallAccess] = field(default_factory=list)
    attributes: list[AttributeAccess] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)

    def line_text(self, line: int) -> str:
        """The stripped text of a 1-indexed source line, or ``""``."""
        lines = self.source.splitlines()
        if 1 <= line <= len(lines):
            return lines[line - 1].strip()
        return ""

    def walk_scope(self, symbol: str) -> Iterator[ast.AST]:
        """Yield nodes inside a named function/method, for shape matching."""
        for node in ast.walk(self.tree):
            if isinstance(node, _SCOPE_NODES) and node.name == symbol.rsplit(".", 1)[-1]:
                yield from ast.walk(node)


class _Collector(ast.NodeVisitor):
    """Single-pass collector. Tracks the enclosing scope as it descends."""

    def __init__(self, analysis: ModuleAnalysis) -> None:
        self.analysis = analysis
        self._scope: list[str] = []
        # Attribute nodes that are a call's callee, so they are recorded as
        # calls rather than double-counted as plain attribute reads.
        self._callee_nodes: set[int] = set()

    @property
    def symbol(self) -> str:
        return ".".join(self._scope) if self._scope else MODULE_SCOPE

    def _enter(self, node: ast.AST) -> None:
        self._scope.append(node.name)  # type: ignore[attr-defined]
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter
    visit_ClassDef = _enter

    def visit_Call(self, node: ast.Call) -> None:
        name = called_name(node)
        if name:
            if isinstance(node.func, ast.Attribute):
                self._callee_nodes.add(id(node.func))
                name_range = _attribute_name_range(node.func)
                receiver = receiver_name(node.func.value)
            else:
                name_range = SourceRange.of(node.func)
                receiver = ""

            keywords: dict[str, SourceRange] = {}
            for keyword in node.keywords:
                if keyword.arg is None:  # `**kwargs`
                    continue
                keywords[keyword.arg] = _keyword_name_range(keyword)

            self.analysis.calls.append(
                CallSite(
                    name=name,
                    receiver=receiver,
                    range=SourceRange.of(node),
                    name_range=name_range,
                    symbol=self.symbol,
                    keywords=keywords,
                    node=node,
                )
            )

            # `x.get("k")` is a distinct access shape from a generic call.
            if (
                name == "get"
                and isinstance(node.func, ast.Attribute)
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                self.analysis.get_calls.append(
                    GetCallAccess(
                        key=node.args[0].value,
                        receiver=receiver,
                        range=SourceRange.of(node),
                        key_range=SourceRange.of(node.args[0]),
                        symbol=self.symbol,
                    )
                )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        key_node = node.slice
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            self.analysis.subscripts.append(
                SubscriptAccess(
                    key=key_node.value,
                    receiver=receiver_name(node.value),
                    range=SourceRange.of(node),
                    key_range=SourceRange.of(key_node),
                    symbol=self.symbol,
                    is_write=not isinstance(node.ctx, ast.Load),
                )
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if id(node) not in self._callee_nodes:
            self.analysis.attributes.append(
                AttributeAccess(
                    attr=node.attr,
                    receiver=receiver_name(node.value),
                    range=SourceRange.of(node),
                    attr_range=_attribute_name_range(node),
                    symbol=self.symbol,
                    is_write=not isinstance(node.ctx, ast.Load),
                )
            )
        self.generic_visit(node)


def _attribute_name_range(node: ast.Attribute) -> SourceRange:
    """Range of just the attribute name in ``value.attr``.

    ``ast`` gives no direct position for the name, but the node ends exactly at
    the end of the attribute, so the name occupies the final ``len(attr)``
    columns. This holds even across a line continuation, because ``end_lineno``
    is the line the name is on.
    """
    end_line = node.end_lineno or node.lineno
    end_col = node.end_col_offset or node.col_offset
    return SourceRange(
        line=end_line,
        col=max(0, end_col - len(node.attr)),
        end_line=end_line,
        end_col=end_col,
    )


def _keyword_name_range(node: ast.keyword) -> SourceRange:
    """Range of the ``name`` token in ``name=value``.

    The keyword node starts at the name, so the name occupies the first
    ``len(arg)`` columns of the node.
    """
    name = node.arg or ""
    return SourceRange(
        line=node.lineno,
        col=node.col_offset,
        end_line=node.lineno,
        end_col=node.col_offset + len(name),
    )


def analyze_source(source: str, path: str) -> ModuleAnalysis:
    """Parse and collect from one module.

    Raises :class:`ParseError` with the offending line for unparseable input,
    so callers can report which file was skipped and why.
    """
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise ParseError(f"{path}:{exc.lineno or '?'}: {exc.msg}") from exc
    except ValueError as exc:  # e.g. source containing null bytes
        raise ParseError(f"{path}: {exc}") from exc

    analysis = ModuleAnalysis(path=path, source=source, tree=tree)
    _Collector(analysis).visit(tree)
    return analysis
