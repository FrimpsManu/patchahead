"""Comparing two versions of an OpenAPI spec into breaking changes.

A renamed property breaks every client that reads it -- ``order["total"]``
raises ``KeyError`` once the API sends ``amount`` -- and a spec says so in a
form a program can read. Every reading here is something PatchAhead may rewrite
code on, so a rename is read only when the evidence leaves one answer:

=============================================================  ==============  ==========
Old property                                                    Reading         Confidence
=============================================================  ==============  ==========
still there, newly deprecated, its description names exactly    field rename    high
  one other property of the same schema, with the same shape
gone; exactly one property added to the same schema with the    field rename    medium
  same shape, and no other gone property shares that candidate
gone; several same-shape candidates, or one shared by several   ambiguous       reported
gone; nothing of the same shape added                           removed         reported
the same endpoint's ``operationId`` changed                     method rename   medium
a query parameter gone, exactly one added with the same shape   param rename    medium
another parameter (path, header, cookie) renamed                param rename    reported
a new required parameter                                        new required    reported
an endpoint gone                                                removed         reported
=============================================================  ==============  ==========

A field rename's owner is the schema's name, asserted: ``Order.total`` renamed
is applied to ``order["total"]`` and never to ``customer["total"]``. A method
rename is applied to the method a generated client derives from the
``operationId`` (``listOrders`` -> ``list_orders``). A query parameter rename
is owned by its endpoint, ``GET [/v2]/orders`` with the server's base path in
brackets, and is applied only to calls that address that endpoint
(:mod:`patchahead.handlers.query_params`). Path parameters are positions in the
URL, not names a client sends; header and cookie parameters are reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from patchahead.domain.change import (
    BreakingChange,
    ChangeKind,
    Confidence,
    Evidence,
    Severity,
    SymbolTarget,
)
from patchahead.openapi.spec import Operation, Property, Schema, Spec

SOURCE = "openapi-diff"


@dataclass
class SpecDiff:
    """The breaking changes between two versions of a spec, in a stable order."""

    title: str
    old_version: str
    new_version: str
    changes: list[BreakingChange] = field(default_factory=list)
    schemas_compared: int = 0
    operations_compared: int = 0


def compare(old: Spec, new: Spec) -> SpecDiff:
    diff = SpecDiff(title=new.title or old.title, old_version=old.version, new_version=new.version)
    for name in sorted(old.schemas):
        if name in new.schemas:
            diff.schemas_compared += 1
            diff.changes.extend(_compare_schema(old.schemas[name], new.schemas[name]))
    for key in sorted(old.operations):
        before = old.operations[key]
        after = new.operations.get(key)
        if after is None:
            diff.changes.append(_endpoint_gone(before, new))
            continue
        diff.operations_compared += 1
        diff.changes.extend(_compare_operation(before, after, new.base_path))
    return diff


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------


def _compare_schema(old: Schema, new: Schema) -> list[BreakingChange]:
    changes: list[BreakingChange] = []
    claimed: set[str] = set()

    # A property kept for compatibility, deprecated, and pointing at its
    # replacement: the spec states the rename outright.
    for name, before in sorted(old.properties.items()):
        after = new.properties.get(name)
        if after is None or not after.deprecated or before.deprecated:
            continue
        pointed = _pointed_at(after.description, set(new.properties) - {name})
        if len(pointed) != 1:
            continue
        replacement = new.properties[pointed[0]]
        if replacement.shape != before.shape:
            changes.append(
                _reported(
                    f"`{old.name}.{name}` deprecated in favor of `{replacement.name}`, "
                    "which has a different type",
                    f"`{name}` is `{before.shape}` and `{replacement.name}` is "
                    f"`{replacement.shape}`; reading one as the other is not a rename",
                    old.name,
                    before,
                    replacement,
                )
            )
            continue
        claimed.add(replacement.name)
        changes.append(
            _field_rename(
                old.name,
                before,
                replacement,
                Confidence.HIGH,
                f"`{old.name}.{name}` is deprecated, and its description names "
                f"`{replacement.name}`, which has the same type",
            )
        )

    gone = sorted(set(old.properties) - set(new.properties))
    added = sorted(set(new.properties) - set(old.properties) - claimed)
    candidates = {
        name: [a for a in added if new.properties[a].shape == old.properties[name].shape]
        for name in gone
    }
    for name in gone:
        before = old.properties[name]
        matches = candidates[name]
        rivals = [g for g in gone if g != name and set(candidates[g]) & set(matches)]
        if len(matches) == 1 and not rivals:
            replacement = new.properties[matches[0]]
            changes.append(
                _field_rename(
                    old.name,
                    before,
                    replacement,
                    Confidence.MEDIUM,
                    f"`{old.name}.{name}` was removed and `{replacement.name}` added to the "
                    f"same schema, the only new property of the same type (`{before.shape}`)",
                )
            )
        elif matches:
            shown = ", ".join(f"`{m}`" for m in matches)
            why = (
                f"several new properties have the same type ({shown})"
                if len(matches) > 1
                else f"`{matches[0]}` is also the only candidate for "
                + ", ".join(f"`{r}`" for r in rivals)
            )
            changes.append(
                _reported(
                    f"`{old.name}.{name}` removed; its replacement is ambiguous",
                    f"{why}, so which one replaces `{name}` cannot be told from the spec",
                    old.name,
                    before,
                )
            )
        else:
            changes.append(
                _reported(
                    f"`{old.name}.{name}` removed",
                    f"no property of the same type (`{before.shape}`) was added to `{old.name}`",
                    old.name,
                    before,
                )
            )
    return changes


def _pointed_at(description: str, names: set[str]) -> list[str]:
    """The other property names a description mentions, as whole words."""
    found = []
    for name in sorted(names):
        if re.search(rf"(?<![\w.]){re.escape(name)}(?![\w])", description):
            found.append(name)
    return found


def _field_rename(
    schema: str, before: Property, after: Property, confidence: Confidence, why: str
) -> BreakingChange:
    return BreakingChange(
        title=f"`{schema}.{before.name}` renamed to `{after.name}`",
        kind=ChangeKind.FIELD_RENAME,
        target=SymbolTarget(
            symbol=before.name,
            replacement=after.name,
            owner=schema,
            # The schema is the object; a property of the same name on another
            # schema is a different field.
            owner_is_explicit=True,
        ),
        severity=Severity.HIGH,
        confidence=confidence,
        evidence=_evidence(schema, before, after),
        source=SOURCE,
        classification_reason=why,
    )


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------


def _compare_operation(
    before: Operation, after: Operation, base_path: str = ""
) -> list[BreakingChange]:
    changes: list[BreakingChange] = []
    old_id, new_id = before.operation_id, after.operation_id
    if old_id and new_id and old_id != new_id:
        old_method, new_method = snake_case(old_id), snake_case(new_id)
        if old_method != new_method:
            changes.append(
                BreakingChange(
                    title=f"`{before.label}`: operationId `{old_id}` renamed to `{new_id}`",
                    kind=ChangeKind.METHOD_RENAME,
                    target=SymbolTarget(
                        symbol=old_method,
                        replacement=new_method,
                        # What a caller names its API object is not in the spec.
                        owner="",
                        owner_is_explicit=False,
                    ),
                    severity=Severity.HIGH,
                    confidence=Confidence.MEDIUM,
                    evidence=[
                        Evidence(quote=f"before: operationId: {old_id}", note=before.label),
                        Evidence(quote=f"after:  operationId: {new_id}", note=after.label),
                    ],
                    source=SOURCE,
                    classification_reason=(
                        f"the same endpoint, `{before.label}`, changed its operationId; a "
                        f"generated client's `{old_method}()` becomes `{new_method}()`"
                    ),
                )
            )

    gone = sorted(set(before.parameters) - set(after.parameters))
    added = sorted(set(after.parameters) - set(before.parameters))
    for key in gone:
        location, name = key
        twins = [
            a
            for a in added
            if a[0] == location and after.parameters[a].shape == before.parameters[key].shape
        ]
        if len(twins) == 1 and location == "query":
            changes.append(_query_rename(before, name, twins[0][1], base_path))
            continue
        if len(twins) == 1:
            title = f"`{before.label}`: {location} parameter `{name}` renamed to `{twins[0][1]}`"
            why = (
                f"`{name}` was removed and `{twins[0][1]}` added in the same place with the "
                f"same type; a {location} parameter is not something PatchAhead rewrites"
            )
        else:
            title = f"`{before.label}`: {location} parameter `{name}` removed"
            why = "no single parameter of the same type replaced it"
        changes.append(_reported(title, why, before.label))
    for location, name in sorted(after.required - before.required):
        what = "new required" if (location, name) not in before.parameters else "now-required"
        changes.append(
            _reported(
                f"`{after.label}`: {what} {location} parameter `{name}`",
                "every existing call now has to pass it; what to pass is a decision, not a rename",
                after.label,
            )
        )
    return changes


def _query_rename(before: Operation, old: str, new: str, base_path: str) -> BreakingChange:
    base = f"[{base_path}]" if base_path else ""
    endpoint = f"{before.method.upper()} {base}{before.path}"
    return BreakingChange(
        title=f"`{before.label}`: query parameter `{old}` renamed to `{new}`",
        kind=ChangeKind.QUERY_PARAM_RENAME,
        target=SymbolTarget(symbol=old, replacement=new, owner=endpoint, owner_is_explicit=True),
        severity=Severity.HIGH,
        confidence=Confidence.MEDIUM,
        evidence=[
            Evidence(quote=f"before: {old} (query)", note=before.label),
            Evidence(quote=f"after:  {new} (query)", note=before.label),
        ],
        source=SOURCE,
        classification_reason=(
            f"`{old}` was removed from `{before.label}` and `{new}` added as a query "
            "parameter of the same type, the only one"
        ),
    )


def _endpoint_gone(before: Operation, new: Spec) -> BreakingChange:
    moved = [
        op
        for op in new.operations.values()
        if before.operation_id and op.operation_id == before.operation_id
    ]
    if moved:
        return _reported(
            f"`{before.label}` moved to `{moved[0].label}`",
            f"the operationId `{before.operation_id}` now belongs to `{moved[0].label}`; a "
            "generated client keeps working, and code that builds the URL itself needs the "
            "new path, which PatchAhead does not rewrite yet",
            before.label,
        )
    return _reported(
        f"`{before.label}` removed",
        "the endpoint is not in the new spec",
        before.label,
    )


def snake_case(operation_id: str) -> str:
    """The method a generated Python client names for an operationId.

    ``listOrders`` -> ``list_orders``, ``GetOrderByID`` -> ``get_order_by_id``,
    ``orders.list`` -> ``orders_list``.
    """
    text = re.sub(r"[^0-9A-Za-z]+", "_", operation_id)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    return text.strip("_").lower()


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def _reported(
    title: str,
    why: str,
    where: str,
    before: Property | None = None,
    after: Property | None = None,
) -> BreakingChange:
    evidence = []
    if before is not None:
        evidence = _evidence(where, before, after)
    return BreakingChange(
        title=title,
        kind=ChangeKind.UNSUPPORTED,
        severity=Severity.HIGH,
        confidence=Confidence.LOW,
        evidence=evidence,
        source=SOURCE,
        classification_reason=why,
    )


def _evidence(where: str, before: Property, after: Property | None) -> list[Evidence]:
    evidence = [Evidence(quote=f"before: {before.name}: {before.shape}", note=where)]
    if after is not None:
        flag = " (deprecated)" if after.deprecated else ""
        evidence.append(Evidence(quote=f"after:  {after.name}: {after.shape}{flag}", note=where))
    return evidence
