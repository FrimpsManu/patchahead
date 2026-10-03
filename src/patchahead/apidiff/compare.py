"""Comparing two versions of a library's public API into breaking changes.

Every reading here is something PatchAhead will rewrite code on, so a match is
made only when the evidence leaves one answer, and everything else is reported:

==========================================================  ============  ==========
Old public member                                            Reading       Confidence
==========================================================  ============  ==========
still there, deprecated, hands its work to one sibling       rename        high
gone; exactly one new sibling with an identical signature    rename        medium
gone; a class with the same name elsewhere / the same        moved         reported
  methods under a new name                                   rename        medium
gone; several possible replacements                          ambiguous     reported
gone; nothing like it                                        removed       reported
a keyword parameter gone, one added at the same position,    kwarg rename  high
  same kind, same default-ness
a keyword parameter gone with no counterpart                 removed       reported
a new parameter without a default                            new required  reported
==========================================================  ============  ==========

A parameter folded into ``**kwargs`` is still accepted, so it is not a break.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from patchahead.apidiff.surface import Member, Param, Surface
from patchahead.domain.change import (
    BreakingChange,
    ChangeKind,
    Confidence,
    Evidence,
    Severity,
    SymbolTarget,
)

#: Parameter kinds a caller can pass by keyword.
_KEYWORD_KINDS = {"normal", "keyword"}


@dataclass
class ApiDiff:
    """The breaking changes between two versions, in a stable order."""

    package: str
    old_version: str
    new_version: str
    changes: list[BreakingChange] = field(default_factory=list)
    members_compared: int = 0


def compare(
    old: Surface, new: Surface, package: str = "", versions: tuple[str, str] = ("", "")
) -> ApiDiff:
    diff = ApiDiff(package=package, old_version=versions[0], new_version=versions[1])
    new_by_definition = new.by_definition()
    old_paths = set(old.public)

    # One reading per definition, however many modules re-export it. A
    # definition counts as gone only when none of its public paths survive: a
    # re-export one module dropped is not a change a caller has to make.
    paths_of: dict[tuple[str, str], list[str]] = {}
    for path, member in old.public.items():
        paths_of.setdefault((member.module, member.qualname), []).append(path)

    gone_classes: list[str] = []
    for key in sorted(paths_of, key=lambda k: _canonical(k, paths_of[k])):
        paths = paths_of[key]
        path = _canonical(key, paths)
        before = old.public[path]
        diff.members_compared += 1
        if any(p.startswith(f"{gone}.") for p in paths for gone in gone_classes):
            # A method of a class that was renamed, moved, or removed: the
            # class's own reading covers it.
            continue
        surviving = [p for p in [path, *sorted(paths)] if p in new.public]
        if surviving:
            diff.changes.extend(
                _compare_member(surviving[0], before, new.public[surviving[0]], new)
            )
            continue
        if before.kind == "class":
            gone_classes.extend(paths)
        diff.changes.append(_gone(path, before, old, new, old_paths, new_by_definition))
    return diff


def _canonical(key: tuple[str, str], paths: list[str]) -> str:
    """The path a definition is best known by: where it is defined, else the shortest."""
    defined = f"{key[0]}.{key[1]}"
    return defined if defined in paths else min(paths, key=lambda p: (len(p), p))


def _compare_member(path: str, before: Member, after: Member, new: Surface) -> list[BreakingChange]:
    changes: list[BreakingChange] = []
    if after.deprecated and not before.deprecated and before.kind != "class":
        changes.append(_deprecated(path, before, after, new))
    changes.extend(_parameter_changes(path, before, after))
    return changes


def _deprecated(path: str, before: Member, after: Member, new: Surface) -> BreakingChange:
    """A member newly deprecated in favor of a sibling: a rename only if a drop-in one."""
    if not after.deprecated_for:
        return _unsupported(
            path,
            before,
            f"`{path}` is deprecated",
            f"`{before.qualname}` is newly deprecated and names no replacement",
            after,
        )
    replacement = f"{after.container + '.' if after.container else ''}{after.deprecated_for}"
    module = path[: -len(before.qualname) - 1]
    target = new.public.get(f"{module}.{replacement}")
    if target is not None and _accepts_every_call(after.params, target.params):
        return _rename(
            path,
            before,
            replacement,
            Confidence.HIGH,
            f"`{before.qualname}` is newly deprecated in favor of `{replacement}`, "
            f"which accepts every call it does",
        )
    return _unsupported(
        path,
        before,
        f"`{path}` is deprecated in favor of `{after.deprecated_for}`",
        f"`{before.qualname}` is newly deprecated in favor of `{replacement}`, which "
        f"does not accept every call it does -- not a rename PatchAhead can apply",
        target or after,
    )


def _gone(
    path: str,
    before: Member,
    old: Surface,
    new: Surface,
    old_paths: set[str],
    new_by_definition: dict[tuple[str, str], Member],
) -> BreakingChange:
    module, _, _ = path.rpartition(f".{before.qualname}")
    added = {p: m for p, m in new.public.items() if p not in old_paths}

    # Same name, different module: a move, not a rename.
    elsewhere = [p for p, m in added.items() if m.qualname == before.qualname]
    if elsewhere:
        return _unsupported(
            path,
            before,
            f"`{path}` moved to `{elsewhere[0]}`",
            f"`{before.qualname}` is no longer at `{path}` but is at "
            f"`{', '.join(sorted(elsewhere))}` -- a move, which PatchAhead v1 cannot migrate",
        )

    # A new sibling that looks exactly like it: same container, same signature
    # (for a class: same public methods).
    def sibling(member: Member) -> bool:
        if member.kind != before.kind or member.container != before.container:
            return False
        if before.kind == "class":
            return bool(before.methods) and member.methods == before.methods
        return _accepts_every_call(before.params, member.params) and _accepts_every_call(
            member.params, before.params
        )

    candidates = sorted(
        {m.qualname: m for p, m in added.items() if p.startswith(f"{module}.") and sibling(m)}
    )
    if len(candidates) == 1:
        return _rename(
            path,
            before,
            candidates[0],
            Confidence.MEDIUM,
            f"`{before.qualname}` is gone and `{candidates[0]}` is new, with "
            + (
                "the same public methods"
                if before.kind == "class"
                else f"an identical signature `{before.signature().split('(', 1)[1]}`"
            ),
        )
    if candidates:
        return _unsupported(
            path,
            before,
            f"`{path}` was removed; several new members could replace it",
            f"`{before.qualname}` is gone and {len(candidates)} new members look like it "
            f"({', '.join(f'`{c}`' for c in candidates)}); PatchAhead does not pick one",
        )
    return _unsupported(
        path,
        before,
        f"`{path}` was removed",
        f"`{before.qualname}` is gone and nothing in the new version looks like its replacement",
    )


def _parameter_changes(path: str, before: Member, after: Member) -> list[BreakingChange]:
    if any(p.kind == "var_keyword" for p in after.params):
        # Anything removed is still accepted by **kwargs; nothing to migrate.
        removed: list[Param] = []
    else:
        names_after = {p.name for p in after.params}
        removed = [
            p for p in before.params if p.kind in _KEYWORD_KINDS and p.name not in names_after
        ]
    names_before = {p.name for p in before.params}
    added = [p for p in after.params if p.name not in names_before and p.kind in _KEYWORD_KINDS]
    callable_name = before.name if before.kind != "class" else before.qualname

    changes: list[BreakingChange] = []
    paired: set[str] = set()
    for gone in removed:
        twin = [
            p
            for p in added
            if p.kind == gone.kind
            and p.has_default == gone.has_default
            and _position(after.params, p) == _position(before.params, gone)
        ]
        if len(twin) == 1 and len(removed) == len(added) == 1 and _related(gone.name, twin[0].name):
            paired.add(twin[0].name)
            changes.append(
                BreakingChange(
                    title=f"`{gone.name}=` on `{path}` renamed to `{twin[0].name}=`",
                    kind=ChangeKind.KWARG_RENAME,
                    target=SymbolTarget(
                        symbol=gone.name,
                        replacement=twin[0].name,
                        owner=callable_name,
                        owner_is_explicit=True,
                    ),
                    severity=Severity.HIGH,
                    confidence=Confidence.HIGH,
                    evidence=_evidence(before, after),
                    source="api-diff",
                    classification_reason=(
                        f"`{gone.name}` was removed and `{twin[0].name}` added at the same "
                        f"position, with the same kind and default"
                    ),
                )
            )
        else:
            changes.append(
                _unsupported(
                    path,
                    before,
                    f"`{gone.name}=` removed from `{path}`",
                    f"the `{gone.name}` parameter of `{path}` was removed, and no single "
                    f"new parameter replaces it",
                    after,
                )
            )
    for new in added:
        if new.name not in paired and not new.has_default:
            changes.append(
                _unsupported(
                    path,
                    before,
                    f"`{path}` has a new required parameter `{new.name}`",
                    f"calls to `{path}` must now pass `{new.name}`; PatchAhead cannot "
                    f"invent the value",
                    after,
                )
            )
    return changes


def _related(old: str, new: str) -> bool:
    """Whether two parameter names plausibly name the same thing.

    Position, kind and default alone pair unrelated options -- a library that
    drops `use_proxy` and adds `slots` in its place has not renamed anything.
    A shared word (`verify_ssl` / `verify`, `info` / `current_info`) is the
    evidence a rename leaves behind.
    """

    def words(name: str) -> set[str]:
        spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()
        return {w for w in spaced.split("_") if len(w) >= 3}

    return bool(words(old) & words(new))


def _accepts_every_call(old: tuple[Param, ...], new: tuple[Param, ...]) -> bool:
    """Whether every call valid against ``old`` is also valid against ``new``.

    Positional parameters keep their order and names; every name a caller can
    pass by keyword is still accepted; whatever ``new`` adds has a default.
    """
    by_name = {p.name: p for p in new}
    old_positional = [p for p in old if p.kind in ("positional", "normal")]
    new_positional = [p for p in new if p.kind in ("positional", "normal")]
    if [p.name for p in new_positional[: len(old_positional)]] != [p.name for p in old_positional]:
        return False
    for param in old:
        if param.kind in ("var_positional", "var_keyword"):
            if not any(p.kind == param.kind for p in new):
                return False
            continue
        counterpart = by_name.get(param.name)
        if counterpart is None:
            return False
        if param.kind == "normal" and counterpart.kind != "normal":
            return False
        if param.has_default and not counterpart.has_default:
            return False
    known = {p.name for p in old}
    return all(
        p.has_default or p.kind in ("var_positional", "var_keyword")
        for p in new
        if p.name not in known
    )


def _position(params: tuple[Param, ...], param: Param) -> int:
    same_kind = [p.name for p in params if p.kind == param.kind]
    return same_kind.index(param.name) if param.kind != "keyword" else 0


def _rename(
    path: str, before: Member, replacement: str, confidence: Confidence, why: str
) -> BreakingChange:
    new_name = replacement.rsplit(".", 1)[-1]
    owner = before.container if before.kind == "method" else ""
    return BreakingChange(
        title=f"`{path}` renamed to `{new_name}`",
        kind=ChangeKind.METHOD_RENAME,
        target=SymbolTarget(
            symbol=before.name,
            replacement=new_name,
            owner=owner,
            # The class is known; what a caller names its instance is not. A
            # hint ranks a matching receiver higher without vetoing the rest.
            owner_is_explicit=False,
        ),
        severity=Severity.HIGH,
        confidence=confidence,
        evidence=[Evidence(quote=f"before: {before.signature()}", note=before.module)],
        source="api-diff",
        classification_reason=why,
    )


def _unsupported(
    path: str, before: Member, title: str, why: str, after: Member | None = None
) -> BreakingChange:
    return BreakingChange(
        title=title,
        kind=ChangeKind.UNSUPPORTED,
        severity=Severity.HIGH,
        confidence=Confidence.LOW,
        evidence=_evidence(before, after),
        source="api-diff",
        classification_reason=why,
    )


def _evidence(before: Member, after: Member | None) -> list[Evidence]:
    evidence = [Evidence(quote=f"before: {before.signature()}", note=before.module)]
    if after is not None:
        evidence.append(Evidence(quote=f"after:  {after.signature()}", note=after.module))
    return evidence
