"""The public API of a Python library, read from its source without running it.

A library is parsed, never imported: importing runs its code, and the point of
reading two versions side by side is to do it before trusting either. What is
recorded is what a caller can depend on -- public functions, classes, methods,
and each parameter's name, kind, position, and whether it has a default.

"Public" follows the conventions a caller relies on: a name without a leading
underscore, in a module whose path has no private component, filtered by
``__all__`` where a module declares one -- plus anything a public module
re-exports with ``from .impl import Name``, which is how most libraries expose
code that lives in private modules.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from patchahead.analysis.edits import read_source

log = logging.getLogger(__name__)

#: Directories in a source tree or wheel that are not the library's code.
_SKIP_DIRS = {"tests", "test", "testing", "docs", "examples", "benchmarks"}


@dataclass(frozen=True)
class Param:
    """One parameter, as a caller sees it."""

    name: str
    #: ``positional`` (positional-only), ``normal``, ``var_positional``,
    #: ``keyword`` (keyword-only), or ``var_keyword``.
    kind: str
    has_default: bool

    def render(self) -> str:
        prefix = {"var_positional": "*", "var_keyword": "**"}.get(self.kind, "")
        return f"{prefix}{self.name}{'=...' if self.has_default else ''}"


@dataclass
class Member:
    """A public function, class, or method, where it is defined."""

    #: Dotted module the definition lives in, e.g. ``storekit._client``.
    module: str
    #: ``fetch_all`` for a function, ``Client`` for a class, ``Client.fetch_all``
    #: for a method.
    qualname: str
    kind: str  # "function" | "class" | "method"
    params: tuple[Param, ...] = ()
    #: For a class: its public method names, used to recognize a renamed class.
    methods: frozenset[str] = frozenset()
    #: Marked deprecated: a ``@deprecated`` decorator, or a deprecation warning.
    deprecated: bool = False
    #: The sibling a deprecated member names as its replacement, in its
    #: warning, decorator, or docstring.
    deprecated_for: str = ""
    line: int = 0

    @property
    def name(self) -> str:
        return self.qualname.rsplit(".", 1)[-1]

    @property
    def container(self) -> str:
        """What holds it: the class for a method, the module otherwise."""
        return self.qualname.rsplit(".", 1)[0] if "." in self.qualname else ""

    def signature(self) -> str:
        return f"{self.qualname}({', '.join(p.render() for p in self.params)})"


@dataclass
class Surface:
    """Every public member of a library, keyed by the dotted path a caller uses."""

    #: ``storekit.Client.fetch_all`` -> the member, however it got there.
    public: dict[str, Member] = field(default_factory=dict)
    #: Modules that could not be parsed, with the reason.
    skipped: dict[str, str] = field(default_factory=dict)

    def by_definition(self) -> dict[tuple[str, str], Member]:
        """The same members keyed by where they are defined."""
        return {(m.module, m.qualname): m for m in self.public.values()}


def read(root: Path) -> Surface:
    """Read the public API of every top-level package or module under ``root``.

    ``root`` may also be a package directory itself; its parent is read then.
    """
    if (root / "__init__.py").exists():
        root = root.parent
    modules = _modules(root)
    definitions: dict[str, dict[str, Member]] = {}
    imports: dict[str, dict[str, tuple[str, str]]] = {}
    exported: dict[str, set[str] | None] = {}
    surface = Surface()

    for dotted, path in modules.items():
        try:
            tree = ast.parse(read_source(path), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            surface.skipped[dotted] = str(exc)
            continue
        definitions[dotted] = _definitions(dotted, tree)
        imports[dotted] = _imports(dotted, tree, path.name == "__init__.py")
        exported[dotted] = _all(tree)

    for dotted in modules:
        if dotted not in definitions or _private_module(dotted):
            continue
        allowed = exported[dotted]

        def visible(name: str, allowed: set[str] | None = allowed) -> bool:
            return name in allowed if allowed is not None else not name.startswith("_")

        for name, member in definitions[dotted].items():
            if "." not in name and visible(name):
                _publish(surface, f"{dotted}.{name}", member, definitions[dotted])
        for alias, (source, original) in imports[dotted].items():
            member = definitions.get(source, {}).get(original)
            if member is not None and visible(alias):
                _publish(surface, f"{dotted}.{alias}", member, definitions[source])
    return surface


def _publish(surface: Surface, path: str, member: Member, siblings: dict[str, Member]) -> None:
    surface.public.setdefault(path, member)
    if member.kind == "class":
        for qualname, method in siblings.items():
            if qualname.startswith(f"{member.qualname}."):
                surface.public.setdefault(f"{path}.{method.name}", method)


def _modules(root: Path) -> dict[str, Path]:
    """Dotted module name -> file, for every package or module at ``root``."""
    found: dict[str, Path] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        parts = relative.parts
        if any(p.endswith((".dist-info", ".egg-info", ".data")) for p in parts):
            continue
        if any(p in _SKIP_DIRS for p in parts[:-1]):
            continue
        # Only the importable tree: every directory above the file is a package.
        if any(
            not (root.joinpath(*parts[:i]) / "__init__.py").exists() for i in range(1, len(parts))
        ):
            continue
        dotted = ".".join(parts[:-1] + ((path.stem,) if path.stem != "__init__" else ()))
        if dotted:
            found[dotted] = path
    return found


def _private_module(dotted: str) -> bool:
    return any(part.startswith("_") for part in dotted.split("."))


def _all(tree: ast.Module) -> set[str] | None:
    """A module's literal ``__all__``, or None when it has none we can read."""
    for node in tree.body:
        names_all = isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        )
        if (
            names_all
            and isinstance(node.value, (ast.List, ast.Tuple))
            and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.value.elts
            )
        ):
            return {e.value for e in node.value.elts}
    return None


def _imports(dotted: str, tree: ast.Module, is_package: bool) -> dict[str, tuple[str, str]]:
    """``alias -> (source module, original name)`` for a module's top-level imports."""
    package = dotted if is_package else dotted.rpartition(".")[0]
    found: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            base = package.split(".")
            base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
            source = ".".join(base + ([node.module] if node.module else []))
        else:
            source = node.module or ""
        for alias in node.names:
            if alias.name != "*":
                found[alias.asname or alias.name] = (source, alias.name)
    return found


def _definitions(dotted: str, tree: ast.Module) -> dict[str, Member]:
    members: dict[str, Member] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            members[node.name] = _function(dotted, node.name, node, "function", tree.body)
        elif isinstance(node, ast.ClassDef):
            methods = [
                child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (not child.name.startswith("_") or child.name == "__init__")
            ]
            init = next((m for m in methods if m.name == "__init__"), None)
            members[node.name] = Member(
                module=dotted,
                qualname=node.name,
                kind="class",
                params=_params(init, drop_first=True) if init else (),
                methods=frozenset(m.name for m in methods if m.name != "__init__"),
                line=node.lineno,
            )
            for method in methods:
                if method.name == "__init__":
                    continue
                qualname = f"{node.name}.{method.name}"
                members[qualname] = _function(dotted, qualname, method, "method", node.body)
    return members


def _function(dotted: str, qualname: str, node, kind: str, scope: list[ast.stmt]) -> Member:
    is_static = any(_decorator_name(d) == "staticmethod" for d in node.decorator_list)
    deprecated, replacement = _deprecation(node, scope)
    return Member(
        module=dotted,
        qualname=qualname,
        kind=kind,
        params=_params(node, drop_first=kind == "method" and not is_static),
        deprecated=deprecated,
        deprecated_for=replacement,
        line=node.lineno,
    )


def _params(node, drop_first: bool) -> tuple[Param, ...]:
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    defaults_from = len(positional) - len(args.defaults)
    params: list[Param] = []
    for index, arg in enumerate(positional):
        kind = "positional" if index < len(args.posonlyargs) else "normal"
        params.append(Param(arg.arg, kind, index >= defaults_from))
    if args.vararg:
        params.append(Param(args.vararg.arg, "var_positional", False))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        params.append(Param(arg.arg, "keyword", default is not None))
    if args.kwarg:
        params.append(Param(args.kwarg.arg, "var_keyword", False))
    return tuple(params[1:] if drop_first and params and params[0].kind != "keyword" else params)


def _decorator_name(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    return target.id if isinstance(target, ast.Name) else ""


_DEPRECATION_CATEGORIES = {"DeprecationWarning", "PendingDeprecationWarning", "FutureWarning"}


def _deprecation(node, scope: list[ast.stmt]) -> tuple[bool, str]:
    """Whether a function is deprecated, and the sibling it names as replacement.

    Deprecated means a ``@deprecated`` decorator, or a ``warnings.warn`` the body
    always raises that is about deprecation: a deprecation category, or
    "deprecat" in the message. A warning about something else -- an insecure
    option -- is not, and nor is one raised only for a deprecated argument.

    The replacement must be *named*: a sibling (a method of the same class, or
    a function of the same module) mentioned in the warning, the decorator, or
    the docstring. Calling a sibling is not naming it -- a deprecated function
    usually calls helpers, often as its whole body.
    """
    texts: list[str] = []
    deprecated = False
    for decorator in node.decorator_list:
        if "deprecat" in _decorator_name(decorator).lower():
            deprecated = True
            texts.extend(_strings(decorator))
    # Only a warning the body always raises deprecates the function. One inside
    # an `if` -- `if skip_defaults is not None: warn(...)` -- deprecates an
    # argument, and the function itself is as current as ever.
    warn_calls = [
        stmt.value
        for stmt in node.body
        if isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and _decorator_name(stmt.value) == "warn"
    ]
    for call in warn_calls:
        if _about_deprecation(call):
            deprecated = True
            texts.extend(_strings(call))
    if not deprecated:
        return False, ""
    docstring = ast.get_docstring(node)
    if docstring:
        texts.append(docstring)

    siblings = {
        n.name
        for n in scope
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name != node.name
    }
    named = {
        name for name in siblings for text in texts if re.search(rf"\b{re.escape(name)}\b", text)
    }
    return True, named.pop() if len(named) == 1 else ""


def _strings(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _about_deprecation(call: ast.Call) -> bool:
    for arg in [*call.args, *(k.value for k in call.keywords)]:
        if _decorator_name(arg) in _DEPRECATION_CATEGORIES:
            return True
        if any("deprecat" in text.lower() for text in _strings(arg)):
            return True
    return False
