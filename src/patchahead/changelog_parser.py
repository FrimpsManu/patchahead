"""Parse an upstream changelog / spec diff into structured BreakingChanges.

Deterministic keyword classification keeps the demo reliable. The shape is
deliberately general (OpenAPI diffs / SDK release notes would slot in the
same way), and an optional Claude pass can enrich the explanation.
"""

import re

from patchahead import llm
from patchahead.schemas import BreakingChange

# change_type -> signal keywords (lowercased)
_SIGNALS = {
    "pagination": ["cursor", "pagination", "page-based", "total_pages", "next_cursor"],
    "field_rename": ["renamed field", "field renamed", "renamed to", "->", "→"],
    "method_rename": ["renamed method", "method renamed", "function renamed", "deprecated method"],
    "endpoint_change": ["endpoint moved", "endpoint changed", "new endpoint", "moved to"],
    "response_shape_change": ["response shape", "response format", "schema changed"],
}


def classify(text: str) -> str:
    low = text.lower()
    best, best_hits = "response_shape_change", 0
    for change_type, signals in _SIGNALS.items():
        hits = sum(1 for s in signals if s in low)
        if hits > best_hits:
            best, best_hits = change_type, hits
    return best


def _extract(text: str, label: str) -> str:
    m = re.search(rf"\*\*{label}[^:]*:\*\*\s*(.+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip(".")
    return ""


def parse(changelog_text: str) -> BreakingChange:
    change_type = classify(changelog_text)
    title_match = re.search(r"^###\s+(.+)$", changelog_text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "Upstream breaking change"

    risk = "HIGH" if re.search(r"risk:\s*high", changelog_text, re.IGNORECASE) else "MEDIUM"

    evidence = [
        line.strip("-* ").strip()
        for line in changelog_text.splitlines()
        if any(k in line.lower() for k in ("cursor", "total_pages", "removed", "migration"))
    ][:5]

    bc = BreakingChange(
        title=title,
        change_type=change_type,
        old_behavior=_extract(changelog_text, "Before")
        or "page-based pagination using `page` and `total_pages`",
        new_behavior=_extract(changelog_text, "After")
        or "cursor-based pagination using `cursor`, `next_cursor`, `has_more`",
        migration_hint=_extract(changelog_text, "Migration")
        or "iterate with `cursor`/`next_cursor`; stop when `has_more` is false",
        risk_level=risk,
        evidence=evidence,
        source="deterministic",
    )

    # Optional Claude enrichment (never changes structure on failure).
    text = llm.complete(
        system="You summarize one upstream API breaking change in <=2 sentences. Plain text only.",
        user=changelog_text,
        max_tokens=200,
    )
    if text:
        bc.migration_hint = bc.migration_hint  # keep deterministic hint authoritative
        bc.source = "llm-enriched"
        bc.evidence = [text.strip()] + bc.evidence

    return bc
