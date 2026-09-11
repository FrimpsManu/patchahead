"""Parsing structured change descriptions (JSON and YAML).

The escape hatch for when release-note prose is not good enough: a user (or a
future spec-diff tool) states the change explicitly and PatchAhead does no
guessing. Structured input therefore defaults to ``Confidence.HIGH`` -- the
uncertainty in the Markdown path is entirely about *reading English*, and that
uncertainty is absent here.

Schema, as a single change or a list of them under ``changes``::

    {
      "title":          "Order field renamed",          # required
      "kind":           "field_rename",                  # required
      "old_behavior":   "...",
      "new_behavior":   "...",
      "migration_hint": "...",
      "severity":       "high" | "medium" | "low",
      "confidence":     "high" | "medium" | "low",
      "target":     {"symbol": "total", "replacement": "amount", "owner": "order"},
      "pagination": {"page_param": "page", "total_pages_key": "total_pages", ...},
      "evidence":   ["quoted line", {"quote": "...", "line": 12, "note": "..."}]
    }
"""

from __future__ import annotations

import json
import logging
from typing import Any

from patchahead.domain.change import (
    BreakingChange,
    ChangeKind,
    Confidence,
    Evidence,
    PaginationContract,
    Severity,
    SymbolTarget,
)
from patchahead.ingest.base import ChangeDocument, ChangeParser, IngestError, register

log = logging.getLogger(__name__)

_VALID_KINDS = ", ".join(sorted(k.value for k in ChangeKind))


def _parse_yaml(text: str, path: str) -> Any:
    try:
        import yaml
    except ImportError:  # pragma: no cover - depends on the environment
        raise IngestError(
            f"cannot parse {path}: YAML support requires PyYAML "
            "(pip install 'patchahead[yaml]'). JSON change documents work without it."
        ) from None
    try:
        return yaml.safe_load(text)
    except Exception as exc:
        raise IngestError(f"{path} is not valid YAML: {exc}") from exc


def _evidence_from(raw: Any) -> list[Evidence]:
    if not isinstance(raw, list):
        return []
    evidence: list[Evidence] = []
    for item in raw:
        if isinstance(item, str):
            evidence.append(Evidence(quote=item))
        elif isinstance(item, dict):
            line = item.get("line")
            evidence.append(
                Evidence(
                    quote=str(item.get("quote", "")),
                    line=int(line) if isinstance(line, int) else None,
                    note=str(item.get("note", "")),
                )
            )
    return evidence


def change_from_mapping(data: dict[str, Any], path: str, source: str) -> BreakingChange:
    """Build one :class:`BreakingChange` from a mapping, validating as we go."""
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise IngestError(f"{path}: each change needs a non-empty `title`")

    raw_kind = data.get("kind") or data.get("change_type")
    if not isinstance(raw_kind, str):
        raise IngestError(f"{path}: change {title!r} needs a `kind`. One of: {_VALID_KINDS}")
    try:
        kind = ChangeKind(raw_kind.strip().lower())
    except ValueError:
        raise IngestError(
            f"{path}: change {title!r} has unknown kind {raw_kind!r}. One of: {_VALID_KINDS}"
        ) from None

    raw_target = data.get("target") or {}
    if not isinstance(raw_target, dict):
        raise IngestError(f"{path}: change {title!r} has a non-mapping `target`")
    target = SymbolTarget(
        symbol=str(raw_target.get("symbol", "") or ""),
        replacement=str(raw_target.get("replacement", "") or ""),
        owner=str(raw_target.get("owner", "") or ""),
    )

    raw_pagination = data.get("pagination")
    if raw_pagination is not None and not isinstance(raw_pagination, dict):
        raise IngestError(f"{path}: change {title!r} has a non-mapping `pagination`")

    return BreakingChange(
        title=title.strip(),
        kind=kind,
        target=target,
        old_behavior=str(data.get("old_behavior", "") or ""),
        new_behavior=str(data.get("new_behavior", "") or ""),
        migration_hint=str(data.get("migration_hint", "") or ""),
        severity=Severity.parse(data.get("severity"), Severity.MEDIUM),
        # Structured input is an explicit assertion by whoever wrote it; take it
        # at face value unless it says otherwise.
        confidence=Confidence.parse(data.get("confidence"), Confidence.HIGH),
        evidence=_evidence_from(data.get("evidence")),
        pagination=PaginationContract.from_dict(raw_pagination),
        source=source,
        classification_reason=str(
            data.get("classification_reason")
            or f"declared explicitly as `{kind.value}` in a structured change document"
        ),
    )


class StructuredChangeParser(ChangeParser):
    """Parses JSON and YAML change descriptions."""

    name = "structured"
    suffixes = (".json", ".yaml", ".yml")

    def supports(self, document: ChangeDocument) -> bool:
        return document.suffix in self.suffixes

    def parse(self, document: ChangeDocument) -> list[BreakingChange]:
        if document.suffix == ".json":
            try:
                data = json.loads(document.text)
            except json.JSONDecodeError as exc:
                raise IngestError(
                    f"{document.path} is not valid JSON: {exc.msg} "
                    f"(line {exc.lineno}, column {exc.colno})"
                ) from exc
        else:
            data = _parse_yaml(document.text, document.path)

        if isinstance(data, dict) and "changes" in data:
            entries = data["changes"]
        elif isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            entries = [data]
        else:
            raise IngestError(
                f"{document.path}: expected an object, a list of objects, or an "
                f"object with a `changes` list; got {type(data).__name__}"
            )

        if not isinstance(entries, list):
            raise IngestError(f"{document.path}: `changes` must be a list")
        if not entries:
            raise IngestError(f"{document.path}: contains no changes")

        changes = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise IngestError(
                    f"{document.path}: each change must be an object, got "
                    f"{type(entry).__name__}"
                )
            changes.append(change_from_mapping(entry, document.path, self.name))

        log.debug("parsed %s: %d structured change(s)", document.path, len(changes))
        return changes


register(StructuredChangeParser())
