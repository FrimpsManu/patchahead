"""Method/function rename: ``client.fetch_orders()`` -> ``client.list_orders()``.

A call is a more specific construct than a bare word, so ``fetch_orders`` used
as a method call is decent evidence on its own. But "decent on its own" stops
mattering the moment the change document names a receiver: ``client`` and
``analytics`` are different objects, and only one of them got the new method.

========================================  ==========  ====================
Site (change document names ``client``)   Confidence  Patched by default?
========================================  ==========  ====================
``client.fetch_orders()``                 HIGH        yes
``self.client.fetch_orders()``            HIGH        yes
``analytics.fetch_orders()``              LOW         no -- reported
``fetch_orders()`` (bare, no receiver)    LOW         no -- reported
========================================  ==========  ====================

A ``from sdk import fetch_orders`` is renamed with the calls it serves --
otherwise the renamed calls would meet an import of a name that no longer
exists. An ``as`` alias is kept, so ``fo()`` in ``from sdk import fetch_orders
as fo`` needs no edit. An import of the repository's own definition, a relative
import, or one in a change that asserts a receiver is reported, not rewritten.

With no declared owner, a call is graded MEDIUM and patched only when the
name itself is evidence. Three things take that evidence away, and each turns
the site into a LOW finding that is reported rather than rewritten:

- The name is also a method of a Python built-in type -- ``get``, ``update``,
  ``items``, ``copy``. ``SETTINGS.get("timeout")`` and ``os.environ.get(...)``
  are dict calls, not SDK calls, and a shared name says nothing about which is
  which. Only a receiver matching the document's example is patched.
- A bare call to a Python built-in -- ``dict(...)`` for a ``dict`` ->
  ``model_dump`` rename -- that the module does not import from anywhere.
- The repository defines a function or method with that name in another
  module, so a call on an unrecognised receiver may be to its own code.

The edit replaces the callee name token and nothing else, so arguments,
formatting, and any chained call are preserved exactly.
"""

from __future__ import annotations

import ast
import builtins
import logging
from dataclasses import dataclass

from patchahead.analysis import MODULE_SCOPE, SourceRange, receiver_matches_owner
from patchahead.analysis.index import RepoIndex
from patchahead.config import Config
from patchahead.domain.change import BreakingChange, ChangeKind, Confidence
from patchahead.domain.impact import AccessKind, CodeReference, ImpactFinding, ImpactReport
from patchahead.domain.plan import MigrationPlan, Risk, TextEdit, Transformation
from patchahead.handlers.base import MigrationHandler, register

log = logging.getLogger(__name__)

# Method names a call on *any* object might have: every public method of the
# built-in types. A rename of `get` matches `os.environ.get` and `config.get`
# as readily as `client.get`, so the name alone proves nothing.
_BUILTIN_METHOD_NAMES = frozenset(
    name
    for kind in (dict, list, tuple, set, frozenset, str, bytes, int, float, object)
    for name in dir(kind)
    if not name.startswith("_")
)
_BUILTIN_NAMES = frozenset(name for name in dir(builtins) if not name.startswith("_"))


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
        "When the change names a receiver, only that receiver is patched. "
        "`analytics.fetch_orders()` is reported but never rewritten for a "
        "`client.fetch_orders` rename.",
        "A module that defines a function with the same name locally is left "
        "alone entirely -- those calls are to its own code.",
        "Only `from x import name` imports are renamed, and only when `x` is not "
        "the repository's own code and no receiver is asserted.",
        "With no receiver named, a name shared with a built-in type's method "
        "(`get`, `update`, `items`), a bare built-in call (`dict()`), or a name "
        "the repository defines elsewhere is reported but not rewritten.",
    )

    def supports(self, change: BreakingChange) -> bool:
        return change.kind in self.kinds and change.target.is_rename

    def analyze(self, change: BreakingChange, index: RepoIndex, config: Config) -> ImpactReport:
        old = change.target.symbol
        # Only an *asserted* receiver constrains which call sites may be patched.
        owner = change.target.owner if change.target.owner_is_explicit else ""
        hint = "" if change.target.owner_is_explicit else change.target.owner
        findings: list[ImpactFinding] = []
        definers = [path for path in index.non_test_paths() if _defines(index.modules[path], old)]

        for path in index.non_test_paths():
            module = index.modules[path]

            # A repository that *defines* this name owns it; renaming calls to
            # its own function would break the code rather than migrate it.
            defines_locally = path in definers
            ambiguity = _ambiguity(old, module, [p for p in definers if p != path])

            for call in module.calls:
                if call.name != old:
                    continue
                confidence, reason, patchable, blocked = self._grade(
                    call.receiver, owner, defines_locally, hint, ambiguity
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
                        other_object=bool(owner)
                        and not receiver_matches_owner(call.receiver, owner),
                    )
                )

            # `from sdk import fetch_orders`: renamed with the calls it serves.
            for node in ast.walk(module.tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                for alias in node.names:
                    if alias.name != old:
                        continue
                    confidence, reason, patchable, blocked = self._grade_import(
                        node, owner, definers
                    )
                    name = _alias_name_range(alias, module.columns)
                    findings.append(
                        ImpactFinding(
                            reference=CodeReference(
                                path=path,
                                line=name.line,
                                col=name.col,
                                end_line=name.end_line,
                                end_col=name.end_col,
                                snippet=module.line_text(name.line),
                            ),
                            symbol=MODULE_SCOPE,
                            matched_contract=f"from {'.' * node.level}{node.module or ''} "
                            f"import {old}",
                            access=AccessKind.IMPORT,
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
        self,
        receiver: str,
        owner: str,
        defines_locally: bool,
        hint: str = "",
        ambiguity: _Ambiguity | None = None,
    ) -> tuple[Confidence, str, bool, str]:
        """Grade one call site.

        Returns ``(confidence, reason, patchable, blocked_reason)``.
        """
        ambiguity = ambiguity or _Ambiguity()
        if defines_locally:
            return (
                Confidence.LOW,
                "this module also defines a function with the renamed name, so "
                "these calls are probably to local code, not to the upstream SDK",
                False,
                "the module defines a function with this name locally",
            )
        if owner and receiver_matches_owner(receiver, owner):
            return (
                Confidence.HIGH,
                f"method call on `{owner}`, the receiver the change document names",
                True,
                "",
            )
        if owner:
            return (
                Confidence.LOW,
                f"call to the renamed method, but on "
                f"`{receiver or '<no receiver>'}` rather than `{owner}`, which the "
                f"change document names as the receiver",
                False,
                f"receiver is `{receiver or '<no receiver>'}`, not the declared receiver `{owner}`",
            )
        if hint and receiver_matches_owner(receiver, hint):
            return (
                Confidence.HIGH,
                f"method call on `{receiver}`, matching the receiver used in the "
                f"change document's example",
                True,
                "",
            )
        blocked = ambiguity.method if receiver else ambiguity.bare
        if blocked:
            return (
                Confidence.LOW,
                f"call to `{receiver + '.' if receiver else ''}{blocked.name}()`, but "
                f"{blocked.why}; name the receiver in the change document to migrate it",
                False,
                blocked.short,
            )
        if receiver:
            return (
                Confidence.MEDIUM,
                f"method call with the renamed name on `{receiver}`; the change "
                f"document asserts no receiver, so this could not be narrowed",
                True,
                "",
            )
        return (
            Confidence.MEDIUM,
            "bare function call with the renamed name and no receiver to check",
            True,
            "",
        )

    def _grade_import(
        self, node: ast.ImportFrom, owner: str, definers: list[str]
    ) -> tuple[Confidence, str, bool, str]:
        """Grade one ``from x import name``. Returns the same tuple as :meth:`_grade`."""
        source = f"{'.' * node.level}{node.module or ''}"
        if node.level or (node.module and _is_repo_module(node.module, definers)):
            return (
                Confidence.LOW,
                f"imports `{source}`'s own definition of the name, which is the "
                f"repository's code rather than the upstream SDK",
                False,
                "imports the repository's own definition",
            )
        if owner:
            return (
                Confidence.LOW,
                f"imports a function with the renamed name, but the change document "
                f"names `{owner}` as the receiver; an importable function may be a "
                f"different thing",
                False,
                f"the change asserts the receiver `{owner}`; a module-level import may differ",
            )
        return (
            Confidence.MEDIUM,
            f"imports the renamed name from `{source}`; renamed together with its calls",
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
            is_import = finding.access is AccessKind.IMPORT
            plan.transformations.append(
                Transformation(
                    reference=reference,
                    old=old if is_import else f"{old}(",
                    new=new if is_import else f"{new}(",
                    symbol=finding.symbol,
                    confidence=finding.confidence,
                    edit=TextEdit(
                        line=reference.line,
                        col=reference.col,
                        end_line=reference.end_line or reference.line,
                        end_col=reference.end_col or reference.col,
                        new_text=new,
                        description=f"rename {'import' if is_import else 'call'} "
                        f"`{old}` to `{new}`",
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


@dataclass(frozen=True)
class _Reason:
    """Why a name match is not evidence on its own."""

    name: str
    why: str
    short: str


@dataclass(frozen=True)
class _Ambiguity:
    """What, in one module, makes a call to the renamed name unconvincing.

    ``method`` applies to ``obj.name()`` calls, ``bare`` to ``name()`` calls.
    ``None`` means the name is distinctive enough to act on.
    """

    method: _Reason | None = None
    bare: _Reason | None = None


def _ambiguity(name: str, module, other_definers: list[str]) -> _Ambiguity:
    elsewhere = None
    if other_definers:
        shown = ", ".join(f"`{path}`" for path in other_definers[:3])
        elsewhere = _Reason(
            name,
            f"the repository defines its own `{name}` in {shown}, so this may be a "
            f"call to that code rather than to the upstream SDK",
            f"the repository defines its own `{name}` elsewhere",
        )

    method = elsewhere
    if name in _BUILTIN_METHOD_NAMES:
        method = _Reason(
            name,
            f"`{name}` is also a method of Python's built-in types, so the name "
            f"alone cannot tell this call apart from, say, a dict's",
            f"`{name}` is also a built-in type's method; receiver not recognised",
        )

    sources = _import_sources(module, name)
    if sources:
        # `from sdk import fetch_orders` is the evidence a bare call otherwise
        # lacks -- unless what it imports is the repository's own definition.
        local = [
            source
            for source in sources
            if source is None or _is_repo_module(source, other_definers)
        ]
        bare = elsewhere if local else None
    elif name in _BUILTIN_NAMES:
        bare = _Reason(
            name,
            f"`{name}` here is Python's built-in -- the module does not import a "
            f"`{name}` from anywhere",
            f"`{name}()` is the Python built-in, not an imported SDK function",
        )
    else:
        bare = elsewhere
    return _Ambiguity(method=method, bare=bare)


def _alias_name_range(alias: ast.alias, columns) -> SourceRange:
    """The range of the imported name alone -- ``fetch_orders`` in ``fetch_orders as fo``."""
    start = SourceRange.of(alias, columns)
    return SourceRange(
        line=start.line, col=start.col, end_line=start.line, end_col=start.col + len(alias.name)
    )


def _import_sources(module, name: str) -> list[str | None]:
    """Modules a module imports ``name`` from; ``None`` for a relative import."""
    return [
        None if node.level else node.module
        for node in ast.walk(module.tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if (alias.asname or alias.name) == name
    ]


def _is_repo_module(dotted: str, definers: list[str]) -> bool:
    """Whether ``dotted`` names one of the repository files that define the name."""
    modules = (path.removesuffix(".py").replace("/", ".") for path in definers)
    return any(module == dotted or module.endswith(f".{dotted}") for module in modules)


def _defines(module, name: str) -> bool:
    """Whether a module defines a function or method with this name."""
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in ast.walk(module.tree)
    )


def _related_tests(index: RepoIndex, findings: list[ImpactFinding]) -> list[str]:
    from patchahead.testing import discovery

    return discovery.tests_for_paths(index, [f.path for f in findings])


register(MethodRenameHandler())
