"""Local names that stand for another one: ``current = order``, ``for o in orders``.

A rename is applied only on the object the change document names, and the
receiver's *name* is the evidence (see :func:`receiver_matches_owner`). That
misses code that reaches the object under another name::

    def report(order):
        current = order
        return current["total"]          # `current` is `order`

    def total(orders):
        return sum(o["total"] for o in orders)   # `o` is one of the `orders`

This module finds those two shapes, and only where nothing else in the function
could make the name mean something else. A name counts as an alias when, in the
function that uses it:

- it is bound **exactly once**, by ``name = other`` (``other`` a plain or dotted
  name, never a call) or as the single target of a ``for`` loop or a
  comprehension over a plain or dotted name;
- it is not a parameter, and no ``global`` or ``nonlocal`` statement names it,
  in the function or in any function nested inside it.

Any other binding -- a second assignment, ``+=``, ``with ... as``, an
``except ... as``, an import, tuple unpacking, a walrus -- counts as a binding
too, so the name is bound more than once and is not an alias. Module-level code
is never resolved: any function can rebind a module global.

This is name resolution within one function, not type inference. It says
``current`` is whatever ``order`` is; whether ``order`` is the owner is still
decided by its name.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from patchahead.analysis.python_ast import iter_own_scope, receiver_name

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
#: Guards against a pathological chain; real code has one or two links.
_MAX_LINKS = 5


@dataclass(frozen=True)
class Alias:
    """What a local name stands for."""

    #: The receiver it stands for, as :func:`receiver_name` spells it.
    origin: str
    #: True when the name is one item of ``origin`` (a loop variable).
    element: bool
    #: The line that made it an alias.
    line: int
    #: Where the name means this, as ``(line, col, end_line, end_col)`` in raw
    #: ``ast`` positions; ``None`` for the whole function. A comprehension's
    #: variable exists only inside the comprehension.
    within: tuple[int, int, int, int] | None = None

    def covers(self, line: int, col: int) -> bool:
        if self.within is None:
            return True
        start_line, start_col, end_line, end_col = self.within
        return (start_line, start_col) <= (line, col) < (end_line, end_col)


def function_aliases(function: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Alias]:
    """The names in ``function`` that are aliases, resolved through chains."""
    bindings: dict[str, int] = {}
    candidates: dict[str, Alias] = {}
    excluded: set[str] = set()

    def bind(name: str, alias: Alias | None = None) -> None:
        bindings[name] = bindings.get(name, 0) + 1
        if alias is not None:
            candidates[name] = alias

    arguments = function.args
    for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]:
        bind(arg.arg)
    for arg in (arguments.vararg, arguments.kwarg):
        if arg is not None:
            bind(arg.arg)

    # A nested function can rebind this function's names with `nonlocal`.
    for node in ast.walk(function):
        if isinstance(node, ast.Global | ast.Nonlocal):
            excluded.update(node.names)

    for node in iter_own_scope(function):
        for child in ast.iter_child_nodes(node):
            if child is not function and isinstance(child, _SCOPES) and hasattr(child, "name"):
                bind(child.name)
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bind(node.id)
        elif isinstance(node, ast.Lambda):
            # `lambda o: o["total"]` is walked as part of this function, and its
            # parameter shadows any `o` here.
            lambda_args = node.args
            for arg in [*lambda_args.posonlyargs, *lambda_args.args, *lambda_args.kwonlyargs]:
                bind(arg.arg)
            for arg in (lambda_args.vararg, lambda_args.kwarg):
                if arg is not None:
                    bind(arg.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bind(node.name)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                bind((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.MatchAs | ast.MatchStar) and node.name:
            bind(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bind(node.rest)

        # The two shapes that make an alias. Their target Names are counted by
        # the Store-context rule above when the walk reaches them; this only
        # records what they would stand for.
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, origin = node.targets[0], _plain(node.value)
            if isinstance(target, ast.Name) and origin:
                candidates[target.id] = Alias(origin, False, node.lineno)
        elif isinstance(node, ast.For | ast.AsyncFor):
            target, origin = node.target, _plain(node.iter)
            if isinstance(target, ast.Name) and origin:
                candidates[target.id] = Alias(origin, True, node.lineno)
        elif isinstance(node, _COMPREHENSIONS):
            for generator in node.generators:
                target, origin = generator.target, _plain(generator.iter)
                if isinstance(target, ast.Name) and origin:
                    within = (
                        node.lineno,
                        node.col_offset,
                        node.end_lineno or node.lineno,
                        node.end_col_offset or node.col_offset,
                    )
                    candidates[target.id] = Alias(origin, True, node.lineno, within)

    direct = {
        name: alias
        for name, alias in candidates.items()
        if bindings.get(name) == 1 and name not in excluded
    }
    return {name: _resolve(alias, direct) for name, alias in direct.items()}


def _plain(node: ast.AST) -> str:
    """A plain or dotted name, or ``""`` -- never a call or a subscript."""
    if isinstance(node, ast.Name | ast.Attribute):
        name = receiver_name(node)
        return "" if "()" in name else name
    return ""


def _resolve(alias: Alias, direct: dict[str, Alias]) -> Alias:
    """Follow ``b = a; a = order`` back to ``order``.

    Only through plain aliases at the head of the origin: an element of an
    element is not something a name can tell.
    """
    for _ in range(_MAX_LINKS):
        head, _, rest = alias.origin.partition(".")
        link = direct.get(head)
        if link is None or link.element or link == alias:
            break
        origin = f"{link.origin}.{rest}" if rest else link.origin
        alias = Alias(origin, alias.element, alias.line, alias.within)
    return alias


def singular(name: str) -> str:
    """``orders`` -> ``order``, ``entries`` -> ``entry``; ``""`` when unsure.

    Used for the item of a collection, so it is deliberately plain: a name it
    cannot singularize with confidence yields ``""``, and the site stays
    unresolved.
    """
    if name.endswith("ies") and len(name) > 3:
        return name[:-3] + "y"
    # `status`, `analysis`, `address` are singular already.
    if name.endswith("s") and not name.endswith(("ss", "us", "is")) and len(name) > 1:
        return name[:-1]
    return ""


def resolve_receiver(
    receiver: str, aliases: dict[str, Alias], line: int = 0, col: int = 0
) -> tuple[str, Alias | None]:
    """The receiver with an alias at its head replaced by what it stands for.

    ``current["total"]`` with ``current = order`` resolves to ``order``;
    ``o["total"]`` with ``for o in orders`` resolves to ``order``, the item of
    ``orders``. Returns ``("", None)`` when the head is not an alias, or is an
    item of something whose name does not singularize.
    """
    if not receiver or not aliases:
        return "", None
    head, _, rest = receiver.partition(".")
    alias = aliases.get(head)
    if alias is None or not alias.covers(line, col):
        return "", None
    origin = alias.origin
    if alias.element:
        *parents, last = origin.split(".")
        item = singular(last)
        if not item:
            return "", None
        origin = ".".join([*parents, item])
    return (f"{origin}.{rest}" if rest else origin), alias


def through_alias(access, owner: str) -> tuple[str, str]:
    """The receiver to grade ``access`` by, and how it was reached.

    The written receiver, unless it does not name ``owner`` and the name it
    stands for does: ``current["total"]`` after ``current = order`` is graded
    as ``order``. The second value says so for the report, or is ``""``.
    """
    from patchahead.analysis.python_ast import receiver_matches_owner

    alias = access.alias
    if (
        alias is None
        or not owner
        or receiver_matches_owner(access.receiver, owner)
        or not receiver_matches_owner(access.resolved, owner)
    ):
        return access.receiver, ""
    name = access.receiver.split(".", 1)[0]
    if alias.element:
        return access.resolved, f"`{name}` is an item of `{alias.origin}` (line {alias.line})"
    return access.resolved, f"`{name}` is `{alias.origin}` (line {alias.line})"
