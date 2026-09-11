"""The migration handler interface and registry.

A *migration family* is one class. It answers four questions, in order:

``supports(change)``
    Is this change mine?
``analyze(change, index, config)``
    Where in this repository is the old contract used, and how sure am I?
``plan(change, report, config)``
    Which of those sites will I change, into what, and why not the others?
``generate(plan, workspace)``
    Apply the plan and produce a diff.

Nothing outside this package needs an ``if change.kind == ...`` branch. The
engine selects a handler through :func:`find_handler` and calls the interface,
so adding a fifth family touches exactly two files: the new handler module and
the registry import in ``handlers/__init__.py``.

Handlers must **fail closed**. A handler that cannot recognize the code shape it
is looking at returns a plan with ``blocked_reason`` set, never a guess. The
prototype's pagination transform guessed, and overwrote user code with the
demo's function body (``docs/assessment.md`` §2.1).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from patchahead.analysis.index import RepoIndex
from patchahead.config import Config
from patchahead.domain.change import BreakingChange, ChangeKind
from patchahead.domain.impact import ImpactReport
from patchahead.domain.patch import FileEdit, PatchProposal
from patchahead.domain.plan import MigrationPlan
from patchahead.workspace import Workspace

log = logging.getLogger(__name__)


class MigrationHandler(ABC):
    """Base class for a migration family."""

    #: Stable identifier, recorded on plans and shown by ``patchahead handlers``.
    name: str = ""
    #: The change kinds this handler claims.
    kinds: tuple[ChangeKind, ...] = ()
    #: One-line description for ``patchahead handlers``.
    summary: str = ""
    #: What this handler explicitly does *not* do, shown in docs and the CLI.
    limitations: tuple[str, ...] = ()

    def supports(self, change: BreakingChange) -> bool:
        """Whether this handler can act on ``change``.

        The default is kind matching. Override to add preconditions -- for
        example, the rename handlers also require both symbol names.
        """
        return change.kind in self.kinds

    @abstractmethod
    def analyze(
        self, change: BreakingChange, index: RepoIndex, config: Config
    ) -> ImpactReport:
        """Find every use of the old contract, with graded confidence."""

    @abstractmethod
    def plan(
        self,
        change: BreakingChange,
        report: ImpactReport,
        index: RepoIndex,
        config: Config,
    ) -> MigrationPlan:
        """Decide which findings to rewrite and into what.

        Takes the index as well as the report because some families need the
        code again -- the pagination handler rebuilds its loop structures to
        emit edits. Re-parsing inside the handler would cost a second pass and
        could disagree with what analysis saw.
        """

    def generate(self, plan: MigrationPlan, workspace: Workspace) -> PatchProposal:
        """Apply a plan inside a workspace and produce a diff.

        The default implementation applies the plan's text edits file by file
        and is correct for every deterministic handler; the edits themselves are
        where a handler's intelligence lives. Override only for a family that
        cannot express its change as range edits.
        """
        from patchahead.analysis import edits as edit_utils

        if plan.blocked_reason:
            return PatchProposal(plan=plan, engine="deterministic", error=plan.blocked_reason)
        if plan.is_empty:
            return PatchProposal(
                plan=plan,
                engine="deterministic",
                error="the plan contains no transformations",
            )

        files: list[FileEdit] = []
        entries: list[tuple[str, str, str]] = []
        for path in plan.target_files:
            try:
                original = workspace.read(path)
            except OSError as exc:
                return PatchProposal(
                    plan=plan, engine="deterministic", error=f"cannot read {path}: {exc}"
                )
            path_edits = plan.edits_for(path)
            try:
                patched = edit_utils.apply_edits(original, path_edits)
            except edit_utils.EditError as exc:
                return PatchProposal(
                    plan=plan,
                    engine="deterministic",
                    error=f"cannot apply edits to {path}: {exc}",
                )
            workspace.write(path, patched)
            files.append(
                FileEdit(
                    path=path,
                    old_source=original,
                    new_source=patched,
                    edit_count=len(path_edits),
                )
            )
            entries.append((path, original, patched))

        return PatchProposal(
            plan=plan,
            files=files,
            diff=edit_utils.combined_diff(entries),
            engine="deterministic",
            explanation=plan.rationale,
        )


_HANDLERS: list[MigrationHandler] = []


def register(handler: MigrationHandler) -> MigrationHandler:
    """Add a handler to the registry."""
    _HANDLERS.append(handler)
    return handler


def registered() -> list[MigrationHandler]:
    return list(_HANDLERS)


def find_handler(change: BreakingChange) -> MigrationHandler | None:
    """The first registered handler that supports ``change``, or ``None``."""
    for handler in _HANDLERS:
        if handler.supports(change):
            return handler
    return None


def supported_kinds() -> list[ChangeKind]:
    """Every change kind some registered handler claims."""
    kinds: list[ChangeKind] = []
    for handler in _HANDLERS:
        for kind in handler.kinds:
            if kind not in kinds:
                kinds.append(kind)
    return kinds


def selftest_registry() -> list[str]:
    """Check the registry's invariants. Returns a list of problems.

    Enforces the project rule from ``docs/migrations.md``: every actionable
    :class:`~patchahead.domain.change.ChangeKind` has a handler, every handler
    is identifiable, and no two handlers claim the same kind. This is asserted
    by the test suite so a half-added migration family fails CI rather than
    shipping as a kind nothing can migrate.
    """
    problems: list[str] = []
    claimed: dict[ChangeKind, str] = {}

    for handler in _HANDLERS:
        if not handler.name:
            problems.append(f"{type(handler).__name__} has no `name`")
        if not handler.kinds:
            problems.append(f"handler `{handler.name}` claims no change kinds")
        if not handler.summary:
            problems.append(f"handler `{handler.name}` has no `summary`")
        for kind in handler.kinds:
            if kind in claimed:
                problems.append(
                    f"change kind `{kind.value}` is claimed by both "
                    f"`{claimed[kind]}` and `{handler.name}`"
                )
            claimed[kind] = handler.name

    for kind in ChangeKind:
        if kind.is_actionable and kind not in claimed:
            problems.append(f"change kind `{kind.value}` has no handler")

    return problems
