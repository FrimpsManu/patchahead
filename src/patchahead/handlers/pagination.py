"""Page-based -> cursor-based pagination.

This is the family the prototype got most wrong. Its "migration" was a literal
string containing the demo's own function, pasted over whatever function it
found; pointed at another repository it renamed the user's function, changed its
signature, and swapped its response key (``docs/assessment.md`` §2.1).

This implementation recognizes a *shape* and rewrites four specific spans inside
it, leaving everything else -- the function name, the signature, the accumulator,
any logging, the formatting -- exactly as the author wrote it.

The recognized shape
--------------------

.. code-block:: python

    page = 1                                   # (A) initializer
    while True:                                # (B) unconditional loop
        response = client.get_orders(page=page)  # (C) call passing the page
        ...
        if page >= response["total_pages"]:    # (D) guard on the page count
            break
        ...
        page += 1                              # (E) advance

becomes

.. code-block:: python

    cursor = None                              # (A)
    while True:                                # (B) untouched
        response = client.get_orders(cursor=cursor)  # (C)
        ...
        if not response.get("has_more"):       # (D)
            break
        ...
        cursor = response.get("next_cursor")   # (E)

Statements between the numbered lines are not touched. Only the four spans
change, so a 40-line sync function produces a four-line diff.

Failing closed
--------------

If any of (A)-(E) is missing, or the page variable is used anywhere *else* in
the function (logged, returned, stored), the handler refuses: it reports what it
found, states which part of the shape it could not match, and produces no patch.
A pagination loop that does something unusual is exactly the case where a
confident wrong answer is most expensive, and it is the case
``--use-llm`` exists for.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field

from patchahead.analysis.index import RepoIndex
from patchahead.analysis.python_ast import ModuleAnalysis, SourceRange
from patchahead.config import Config
from patchahead.domain.change import BreakingChange, ChangeKind, Confidence, PaginationContract
from patchahead.domain.impact import AccessKind, CodeReference, ImpactFinding, ImpactReport
from patchahead.domain.plan import MigrationPlan, Risk, TextEdit, Transformation
from patchahead.handlers.base import MigrationHandler, register

log = logging.getLogger(__name__)


@dataclass
class PageLoop:
    """A recognized page-based pagination loop, with every span to rewrite."""

    #: Name of the integer page counter, e.g. ``page``.
    page_var: str
    #: Name the paginated response is bound to, e.g. ``response``.
    response_var: str
    #: Enclosing function.
    symbol: str
    #: (A) ``page = 1``
    init_range: SourceRange
    #: (C) the ``page=page`` keyword inside the call
    call_keyword_range: SourceRange
    #: (D) the ``if`` test expression guarding the break
    guard_range: SourceRange
    #: (E) ``page += 1``
    advance_range: SourceRange
    #: Line of the ``while`` statement, for reporting.
    loop_line: int
    #: Names already bound in the enclosing function, for collision avoidance.
    bound_names: set[str] = field(default_factory=set)


@dataclass
class LoopRejection:
    """A ``while True`` loop that looked like pagination but was not migratable."""

    symbol: str
    line: int
    reason: str


def _is_true_literal(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _response_key_access(node: ast.AST, key: str) -> str:
    """The variable name in ``<name>[key]`` or ``<name>.get(key)``, else ``""``."""
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == key
        and isinstance(node.value, ast.Name)
    ):
        return node.value.id
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == key
    ):
        return node.func.value.id
    return ""


def _contains_break(statements: list[ast.stmt]) -> bool:
    for statement in statements:
        for node in ast.walk(statement):
            if isinstance(node, ast.Break):
                return True
    return False


def _is_increment(node: ast.AST, name: str) -> bool:
    """``name += 1`` or ``name = name + 1``."""
    if (
        isinstance(node, ast.AugAssign)
        and isinstance(node.op, ast.Add)
        and isinstance(node.target, ast.Name)
        and node.target.id == name
        and isinstance(node.value, ast.Constant)
        and node.value.value == 1
    ):
        return True
    return (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
        and isinstance(node.value, ast.BinOp)
        and isinstance(node.value.op, ast.Add)
        and isinstance(node.value.left, ast.Name)
        and node.value.left.id == name
        and isinstance(node.value.right, ast.Constant)
        and node.value.right.value == 1
    )


def _functions(tree: ast.Module):
    """Every function definition, with its dotted name."""

    def walk(node: ast.AST, prefix: list[str]):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = ".".join(prefix + [child.name])
                yield name, child
                yield from walk(child, prefix + [child.name])
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, prefix + [child.name])
            else:
                yield from walk(child, prefix)

    yield from walk(tree, [])


def find_page_loops(
    module: ModuleAnalysis, contract: PaginationContract
) -> tuple[list[PageLoop], list[LoopRejection]]:
    """Find every migratable page loop, and every near-miss with its reason."""
    loops: list[PageLoop] = []
    rejections: list[LoopRejection] = []

    for symbol, function in _functions(module.tree):
        bound = {
            node.id
            for node in ast.walk(function)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        }
        bound |= {argument.arg for argument in function.args.args}

        for loop in (n for n in ast.walk(function) if isinstance(n, ast.While)):
            if not _is_true_literal(loop.test):
                continue

            match, reason = _match_loop(function, loop, symbol, contract, bound)
            if match is not None:
                loops.append(match)
            elif reason:
                rejections.append(LoopRejection(symbol=symbol, line=loop.lineno, reason=reason))

    return loops, rejections


def _match_loop(
    function: ast.AST,
    loop: ast.While,
    symbol: str,
    contract: PaginationContract,
    bound: set[str],
) -> tuple[PageLoop | None, str]:
    """Match one ``while True`` loop against the recognized shape."""
    # (C) a call passing the page parameter, assigned to a name.
    call_keyword: ast.keyword | None = None
    page_var = ""
    response_var = ""
    for node in ast.walk(loop):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        if not isinstance(node.targets[0], ast.Name) or not isinstance(node.value, ast.Call):
            continue
        for keyword in node.value.keywords:
            if keyword.arg == contract.page_param and isinstance(keyword.value, ast.Name):
                call_keyword = keyword
                page_var = keyword.value.id
                response_var = node.targets[0].id
                break
        if call_keyword is not None:
            break

    if call_keyword is None:
        # Not a page loop at all; this `while True` is something else entirely.
        return None, ""

    # (D) an `if` guarding a break, testing the total-pages key on the response.
    guard: ast.expr | None = None
    for node in ast.walk(loop):
        if not isinstance(node, ast.If) or not _contains_break(node.body):
            continue
        for inner in ast.walk(node.test):
            if _response_key_access(inner, contract.total_pages_key) == response_var:
                guard = node.test
                break
        if guard is not None:
            break

    if guard is None:
        return None, (
            f"found a `{contract.page_param}=` paginated call but no `if ...: break` "
            f'guarded by `{response_var}["{contract.total_pages_key}"]`, so the loop\'s '
            f"termination condition could not be identified"
        )

    # (E) the page advance.
    advance: ast.stmt | None = None
    for node in ast.walk(loop):
        if isinstance(node, (ast.AugAssign, ast.Assign)) and _is_increment(node, page_var):
            advance = node
            break

    if advance is None:
        return None, (
            f"found a `{contract.page_param}=` paginated call and a "
            f"`{contract.total_pages_key}` guard, but no `{page_var} += 1` advance"
        )

    # (A) the initializer, before the loop, in the enclosing function.
    init: ast.stmt | None = None
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == page_var
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, int)
            and node.lineno < loop.lineno
        ):
            init = node
            break

    if init is None:
        return None, (
            f"found a page loop over `{page_var}` but no integer initializer "
            f"(`{page_var} = 1`) before the loop"
        )

    # The page variable must be used *only* in the four spans we rewrite. If it
    # is logged, returned, or stored anywhere else, replacing it with a cursor
    # would silently change behavior.
    allowed: set[int] = set()
    for node in ast.walk(init):
        allowed.add(id(node))
    for node in ast.walk(call_keyword):
        allowed.add(id(node))
    for node in ast.walk(guard):
        allowed.add(id(node))
    for node in ast.walk(advance):
        allowed.add(id(node))

    stray = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and node.id == page_var and id(node) not in allowed
    ]
    if stray:
        lines = ", ".join(str(node.lineno) for node in sorted(stray, key=lambda n: n.lineno)[:4])
        return None, (
            f"`{page_var}` is also used outside the pagination loop's own "
            f"bookkeeping (line(s) {lines}); replacing it with a cursor could change "
            f"behavior, so this loop is left for a human"
        )

    return (
        PageLoop(
            page_var=page_var,
            response_var=response_var,
            symbol=symbol,
            init_range=SourceRange.of(init),
            call_keyword_range=SourceRange.of(call_keyword),
            guard_range=SourceRange.of(guard),
            advance_range=SourceRange.of(advance),
            loop_line=loop.lineno,
            bound_names=bound,
        ),
        "",
    )


def choose_cursor_name(preferred: str, taken: set[str]) -> str:
    """Pick a cursor variable name that does not collide with an existing one."""
    if preferred not in taken:
        return preferred
    for suffix in ("_token", "_value", "2"):
        candidate = f"{preferred}{suffix}"
        if candidate not in taken:
            return candidate
    index = 2
    while f"{preferred}{index}" in taken:
        index += 1
    return f"{preferred}{index}"


class PaginationHandler(MigrationHandler):
    """Migrates a page-based pagination loop to a cursor-based one."""

    name = "pagination_page_to_cursor"
    kinds = (ChangeKind.PAGINATION_PAGE_TO_CURSOR,)
    summary = "Rewrite a page-based pagination loop to use a cursor"
    limitations = (
        "Only the documented `while True` + guarded-`break` shape. A `while page "
        "<= total_pages:` loop, a recursive pager, or a generator is reported and "
        "refused.",
        "The page variable must be used only by the loop's own bookkeeping. If it "
        "is logged or returned, the loop is left for a human.",
        "Assumes the new API exposes `has_more` and `next_cursor` (configurable "
        "per change document). It cannot verify that from the code.",
    )

    def analyze(self, change: BreakingChange, index: RepoIndex, config: Config) -> ImpactReport:
        contract = change.pagination
        findings: list[ImpactFinding] = []

        for path in index.non_test_paths():
            module = index.modules[path]
            loops, rejections = find_page_loops(module, contract)
            migratable_lines = {loop.loop_line for loop in loops}

            for loop in loops:
                findings.append(
                    ImpactFinding(
                        reference=CodeReference(
                            path=path,
                            line=loop.loop_line,
                            snippet=module.line_text(loop.loop_line),
                        ),
                        symbol=loop.symbol,
                        matched_contract=(
                            f"while True: ... {contract.page_param}={loop.page_var} ... "
                            f'{loop.response_var}["{contract.total_pages_key}"] ... '
                            f"{loop.page_var} += 1"
                        ),
                        access=AccessKind.PAGE_LOOP,
                        reason=(
                            f"page-based pagination loop over `{loop.page_var}`, "
                            f"terminating on `{contract.total_pages_key}`, which the "
                            f"new API no longer returns"
                        ),
                        confidence=Confidence.HIGH,
                        source_text="",
                        patchable=True,
                    )
                )

            for rejection in rejections:
                findings.append(
                    ImpactFinding(
                        reference=CodeReference(
                            path=path,
                            line=rejection.line,
                            snippet=module.line_text(rejection.line),
                        ),
                        symbol=rejection.symbol,
                        matched_contract=f"while True: ... {contract.page_param}=...",
                        access=AccessKind.PAGE_LOOP,
                        reason=rejection.reason,
                        confidence=Confidence.MEDIUM,
                        source_text="",
                        patchable=False,
                        unpatchable_reason=rejection.reason,
                    )
                )

            # Any remaining read of the removed key is a real break even when it
            # is not inside a loop we can rewrite -- report it so nothing is lost.
            for access in module.subscripts + module.get_calls:  # type: ignore[operator]
                if access.key != contract.total_pages_key:
                    continue
                if any(abs(access.range.line - line) < 12 for line in migratable_lines):
                    continue  # already covered by a migratable loop
                findings.append(
                    ImpactFinding(
                        reference=CodeReference(
                            path=path,
                            line=access.range.line,
                            col=access.range.col,
                            end_line=access.range.end_line,
                            end_col=access.range.end_col,
                            snippet=module.line_text(access.range.line),
                        ),
                        symbol=access.symbol,
                        matched_contract=(
                            f'{access.receiver or "<expr>"}["{contract.total_pages_key}"]'
                        ),
                        access=AccessKind.SUBSCRIPT,
                        reason=(
                            f"reads `{contract.total_pages_key}`, which the new API no "
                            f"longer returns, but not inside a pagination loop this "
                            f"handler recognizes"
                        ),
                        confidence=Confidence.HIGH,
                        source_text="",
                        patchable=False,
                        unpatchable_reason=(
                            "not part of a recognized page loop; rewriting it in "
                            "isolation would need the surrounding logic to change too"
                        ),
                    )
                )

        findings.sort(key=lambda f: (f.reference.path, f.reference.line))
        return ImpactReport(
            change=change,
            findings=findings,
            related_tests=_related_tests(index, findings),
            files_scanned=index.file_count,
            skipped_files=dict(index.skipped),
        )

    def plan(
        self,
        change: BreakingChange,
        report: ImpactReport,
        index: RepoIndex,
        config: Config,
    ) -> MigrationPlan:
        contract = change.pagination
        plan = MigrationPlan(
            change=change,
            handler=self.name,
            expected_tests=list(report.related_tests),
            risk=Risk.MEDIUM,
            rationale=(
                f"Rewrite each recognized page loop to iterate on "
                f"`{contract.cursor_param}`/`{contract.next_cursor_key}` and terminate "
                f"on `{contract.has_more_key}`. Four spans change per loop: the "
                f"initializer, the call's `{contract.page_param}=` argument, the break "
                f"guard, and the advance. The enclosing function's name, signature, "
                f"and every other statement are untouched."
            ),
        )

        # Planning rebuilds the loop structures from the index analysis used, so
        # the spans it edits are exactly the spans analysis matched.
        for path in report.affected_files:
            module = index.modules.get(path)
            if module is None:
                continue
            loops, _ = find_page_loops(module, contract)
            for loop in loops:
                cursor_var = choose_cursor_name(contract.cursor_param, loop.bound_names)
                specs = [
                    (
                        loop.init_range,
                        f"{loop.page_var} = <int>",
                        f"{cursor_var} = None",
                        "start from no cursor instead of page 1",
                    ),
                    (
                        loop.call_keyword_range,
                        f"{contract.page_param}={loop.page_var}",
                        f"{contract.cursor_param}={cursor_var}",
                        "pass the cursor instead of the page number",
                    ),
                    (
                        loop.guard_range,
                        f'{loop.page_var} ... {loop.response_var}["{contract.total_pages_key}"]',
                        f'not {loop.response_var}.get("{contract.has_more_key}")',
                        "terminate on has_more instead of the page count",
                    ),
                    (
                        loop.advance_range,
                        f"{loop.page_var} += 1",
                        f'{cursor_var} = {loop.response_var}.get("{contract.next_cursor_key}")',
                        "advance by cursor instead of incrementing the page",
                    ),
                ]
                for source_range, old, new, description in specs:
                    plan.transformations.append(
                        Transformation(
                            reference=CodeReference(
                                path=path,
                                line=source_range.line,
                                col=source_range.col,
                                end_line=source_range.end_line,
                                end_col=source_range.end_col,
                                snippet=module.line_text(source_range.line),
                            ),
                            old=old,
                            new=new,
                            symbol=loop.symbol,
                            confidence=Confidence.HIGH,
                            edit=TextEdit(
                                line=source_range.line,
                                col=source_range.col,
                                end_line=source_range.end_line,
                                end_col=source_range.end_col,
                                new_text=new,
                                description=description,
                            ),
                        )
                    )
                log.debug("planned cursor migration for %s in %s", loop.symbol, path)

        for finding in report.findings:
            if not finding.patchable:
                plan.skipped.append(
                    f"{finding.reference} ({finding.symbol}): {finding.unpatchable_reason}"
                )

        if not plan.transformations:
            plan.blocked_reason = "no pagination loop matching the supported shape was found. " + (
                "PatchAhead found page-based code it could not safely rewrite; "
                "see the skipped list for why, and consider `--use-llm`."
                if report.findings
                else "No page-based pagination was found in this repository."
            )
        return plan


def _related_tests(index: RepoIndex, findings: list[ImpactFinding]) -> list[str]:
    from patchahead.testing import discovery

    return discovery.tests_for_paths(index, [f.path for f in findings])


register(PaginationHandler())
