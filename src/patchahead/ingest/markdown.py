"""Parsing release notes written as Markdown or plain text.

This is the format most upstream providers actually publish, and the least
structured. Two design rules follow from that:

**Classification is scored, not first-match.** Each migration family has
weighted signal phrases; the highest total wins, and the winning score and the
phrases that produced it are recorded on
:attr:`~patchahead.domain.change.BreakingChange.classification_reason` so a user
can see why a document was read the way it was.

**Uncertainty is represented, not smoothed over.** A document that matches
nothing yields ``ChangeKind.UNKNOWN``; a recognized-but-unhandled change yields
``ChangeKind.UNSUPPORTED``; a rename whose symbols could not be extracted keeps
``Confidence.LOW``. The prototype instead defaulted unparseable documents to the
demo's pagination text, which made every failure look like a confident success
(``docs/assessment.md`` §2.5).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from patchahead.domain.change import (
    BreakingChange,
    ChangeKind,
    Confidence,
    Evidence,
    PaginationContract,
    Severity,
    SymbolTarget,
)
from patchahead.ingest.base import ChangeDocument, ChangeParser, register

log = logging.getLogger(__name__)

#: Signal phrases per change kind, with weights. Phrases are matched as regular
#: expressions against the lowercased section text. Weights are relative: a
#: phrase that names the construct ("keyword argument") outranks one that merely
#: suggests it ("renamed").
_SIGNALS: dict[ChangeKind, list[tuple[str, int]]] = {
    ChangeKind.KWARG_RENAME: [
        (r"keyword argument", 5),
        (r"\bkwarg\b", 5),
        (r"\bparameter\b.{0,40}\brenamed\b", 4),
        (r"\brenamed\b.{0,40}\bparameter\b", 4),
        (r"\bargument\b.{0,40}\brenamed\b", 4),
        (r"\brenamed\b.{0,40}\bargument\b", 4),
        (r"`\w+=`", 3),
    ],
    ChangeKind.METHOD_RENAME: [
        (r"method (?:was )?renamed", 5),
        (r"renamed method", 5),
        (r"function (?:was )?renamed", 5),
        (r"renamed function", 5),
        (r"method renamed", 5),
        (r"deprecated method", 3),
        (r"`\w+\(\)`.{0,30}(?:->|→|renamed to)", 3),
        (r"\bmethod\b", 1),
    ],
    ChangeKind.FIELD_RENAME: [
        (r"field (?:was )?renamed", 5),
        (r"renamed field", 5),
        (r"field renamed", 5),
        (r"attribute (?:was )?renamed", 4),
        (r"property (?:was )?renamed", 4),
        (r"\bfield\b.{0,40}(?:->|→)", 3),
        (r"\bfield\b", 1),
    ],
    ChangeKind.PAGINATION_PAGE_TO_CURSOR: [
        (r"cursor-based", 5),
        (r"\bnext_cursor\b", 4),
        (r"\btotal_pages\b", 4),
        (r"page-based", 4),
        (r"\bhas_more\b", 3),
        (r"\bpagination\b", 3),
        (r"\bcursor\b", 2),
    ],
}

#: Changes PatchAhead can recognize but has no v1 handler for. Detecting them
#: explicitly lets the CLI say "this is an endpoint move, which v1 cannot
#: migrate" instead of misclassifying it as something it can.
_UNSUPPORTED_SIGNALS: list[tuple[str, int, str]] = [
    (r"endpoint (?:was )?(?:moved|changed|removed)", 5, "endpoint change"),
    (r"moved to a (?:new|different) endpoint", 5, "endpoint change"),
    (r"response (?:shape|format|schema) (?:has )?changed", 5, "response shape change"),
    (r"schema (?:was )?changed", 4, "response shape change"),
    (r"authentication (?:has )?changed", 4, "authentication change"),
    (r"\brate limit(?:ing)? (?:has )?changed", 4, "rate limiting change"),
    (r"now returns? an? (?:list|array|object) instead", 4, "response shape change"),
]

#: Minimum score before a classification is trusted at all.
_MIN_SCORE = 3
#: Score at or above which a classification is considered confident.
_STRONG_SCORE = 5

_HEADING = re.compile(r"^(#{2,4})\s+(.+?)\s*#*$", re.MULTILINE)
_NON_BREAKING_HEADING = re.compile(
    r"^#{1,4}\s+(?:non[- ]breaking|additions?|improvements?|deprecations?|"
    r"fixes|bug ?fixes|new features?)\b",
    re.IGNORECASE,
)

# `old` -> `new`, `old` → `new`, `old` to `new`, "old" renamed to "new"
_ARROW_RENAME = re.compile(
    r"`(?P<old>[\w.]+)(?:\(\))?=?`\s*(?:->|→|=>)\s*`(?P<new>[\w.]+)(?:\(\))?=?`"
)
_TO_RENAME = re.compile(
    r"`(?P<old>[\w.]+)(?:\(\))?=?`\s*(?:was\s+|has\s+been\s+)?"
    r"(?:renamed|changed)\s+to\s+`(?P<new>[\w.]+)(?:\(\))?=?`"
)
_RENAMED_TO = re.compile(
    r"renamed\s+(?:from\s+)?`(?P<old>[\w.]+)(?:\(\))?=?`\s+to\s+`(?P<new>[\w.]+)(?:\(\))?=?`"
)
# "`retries` parameter was renamed to `max_retries`" -- the same shape as
# _TO_RENAME but with a noun between the name and the verb. At most two plain
# words are allowed between them, and a backticked name is not a plain word, so
# this cannot skip over an intervening `owner` the way a greedy pattern would.
_NOUN_RENAME = re.compile(
    r"`(?P<old>[\w.]+)(?:\(\))?=?`\s+(?:\w+\s+){0,2}"
    r"(?:was|is|has\s+been)\s+(?:been\s+)?(?:renamed|changed)\s+to\s+"
    r"`(?P<new>[\w.]+)(?:\(\))?=?`"
)
_BOLD_FIELD = re.compile(r"\*\*(?P<label>[A-Za-z][\w /-]*?)\s*:?\*\*[:\s]*(?P<value>.+)")
_RISK = re.compile(r"risk:?\s*(high|medium|low)", re.IGNORECASE)

_STOPWORDS = {
    "the",
    "a",
    "an",
    "is",
    "was",
    "were",
    "to",
    "from",
    "in",
    "on",
    "of",
    "and",
    "or",
    "now",
    "new",
    "old",
    "this",
    "that",
    "it",
    "be",
    "been",
    "before",
    "after",
    "migration",
    "risk",
    "breaking",
    "change",
    "changes",
}


@dataclass
class _Section:
    """One ``###``-delimited chunk of a document."""

    title: str
    text: str
    start_line: int


def _split_sections(document: ChangeDocument) -> list[_Section]:
    """Split a document at its headings, dropping non-breaking-change sections.

    A release note usually describes several changes. Treating the whole file as
    one blob lets a "Non-breaking changes" bullet contribute signal words to a
    breaking change's classification.
    """
    text = document.text
    matches = list(_HEADING.finditer(text))
    if not matches:
        return [_Section(title=_infer_title(text), text=text, start_line=1)]

    sections: list[_Section] = []
    suppressing = False
    for position, match in enumerate(matches):
        heading_line = text[: match.start()].count("\n") + 1
        heading_text = text[match.start() : match.end()]
        level = len(match.group(1))
        body_end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        body = text[match.end() : body_end]

        if _NON_BREAKING_HEADING.match(heading_text.strip()):
            # Suppress this heading and any sub-headings beneath it.
            suppressing = True
            suppress_level = level
            continue
        if suppressing and level > suppress_level:
            continue
        suppressing = False

        title = match.group(2).strip()
        if _looks_like_container_heading(title, body):
            continue
        sections.append(_Section(title=title, text=heading_text + body, start_line=heading_line))

    if not sections:
        return [_Section(title=_infer_title(text), text=text, start_line=1)]
    return sections


def _looks_like_container_heading(title: str, body: str) -> bool:
    """Whether a heading is a wrapper (``## Breaking changes``) with no content."""
    if not re.match(r"(?i)^(breaking changes?|changes?|release notes?)$", title.strip()):
        return False
    return not body.strip() or bool(_HEADING.search(body))


def _infer_title(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return "Upstream breaking change"


def classify(text: str) -> tuple[ChangeKind, int, str]:
    """Classify a block of release-note text.

    Returns the kind, the winning score, and a human-readable reason naming the
    phrases that decided it.
    """
    low = text.lower()

    scores: dict[ChangeKind, tuple[int, list[str]]] = {}
    for kind, signals in _SIGNALS.items():
        total = 0
        hits: list[str] = []
        for pattern, weight in signals:
            if re.search(pattern, low):
                total += weight
                hits.append(pattern)
        if total:
            scores[kind] = (total, hits)

    unsupported_score = 0
    unsupported_label = ""
    for pattern, weight, label in _UNSUPPORTED_SIGNALS:
        if weight > unsupported_score and re.search(pattern, low):
            unsupported_score, unsupported_label = weight, label

    if not scores and not unsupported_score:
        return ChangeKind.UNKNOWN, 0, "no recognized breaking-change signals in the document"

    best_kind, (best_score, best_hits) = (
        max(scores.items(), key=lambda item: item[1][0])
        if scores
        else (ChangeKind.UNKNOWN, (0, []))
    )

    if unsupported_score > best_score:
        return (
            ChangeKind.UNSUPPORTED,
            unsupported_score,
            f"looks like a {unsupported_label}, which PatchAhead v1 cannot migrate",
        )
    if best_score < _MIN_SCORE:
        return (
            ChangeKind.UNKNOWN,
            best_score,
            f"weak signals only (score {best_score}, need {_MIN_SCORE}); "
            f"closest match was {best_kind.value}",
        )

    phrases = ", ".join(sorted(hit.replace("\\b", "") for hit in best_hits)[:4])
    return best_kind, best_score, f"matched {best_kind.value} signals: {phrases}"


def _extract_rename(text: str, kind: ChangeKind = ChangeKind.UNKNOWN) -> tuple[str, str, str]:
    """Pull ``old`` and ``new`` symbols out of a rename description.

    Several notations can match the same sentence, and they are not equally
    trustworthy. ``\\`a\\` -> \\`b\\``` states a rename unambiguously; ``\\`a\\`
    ... was renamed to \\`b\\``` can span an intervening clause and pick up the
    wrong left-hand side -- in `"The \\`timeout_seconds\\` keyword argument on
    \\`fetch_orders\\` was renamed to \\`timeout\\`"` it matches ``fetch_orders``
    -> ``timeout``. So candidates are ranked by notation first and document
    position second, which prefers the explicit arrow in a heading.

    Returns ``(old, new, qualifier)``. ``qualifier`` is the receiver prefix of a
    dotted old name, which is an *asserted* owner -- see
    :class:`~patchahead.domain.change.SymbolTarget`.
    """
    # (notation priority, position, old, new)
    candidates: list[tuple[int, int, str, str]] = []
    for priority, pattern in enumerate((_ARROW_RENAME, _RENAMED_TO, _TO_RENAME, _NOUN_RENAME)):
        for match in pattern.finditer(text):
            old, new = match.group("old"), match.group("new")
            if old.lower() in _STOPWORDS or new.lower() in _STOPWORDS:
                continue
            if old == new:
                continue
            candidates.append((priority, match.start(), old, new))

    if not candidates:
        return "", "", ""

    if kind is ChangeKind.KWARG_RENAME:
        # A keyword argument is the thing that appears as `name=` somewhere in
        # the notes. That is a much stronger signal than notation order.
        keyword_like = [
            candidate
            for candidate in candidates
            if re.search(rf"\b{re.escape(candidate[2])}\s*=", text)
        ]
        if keyword_like:
            candidates = keyword_like

    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
    _, _, old, new = candidates[0]

    # A dotted name qualifies the symbol: `client.fetch_orders -> client.list_orders`
    # states *which* receiver was renamed, so the prefix is an asserted owner.
    qualifier = old.rsplit(".", 1)[0] if "." in old else ""
    return old.rsplit(".", 1)[-1], new.rsplit(".", 1)[-1], qualifier


def _extract_owner(text: str, symbol: str, kind: ChangeKind) -> tuple[str, bool]:
    """Find the object or function the symbol belongs to.

    The owner is what keeps a rename scoped. Without it a field rename of
    ``total`` is a repository-wide search for the word "total"; with it, the
    search is for ``order["total"]``.

    Returns ``(owner, is_explicit)``. Phrasings that *assert* ownership -- "on
    each `order` object", "the keyword argument on `fetch_orders`" -- are
    explicit, and a receiver mismatch then refuses to patch. A receiver merely
    scraped out of an illustrative snippet is not: the vendor writing
    ``client.fetch_orders(limit=10)`` is naming their own example variable, not
    making a claim about what a downstream repository calls its client.
    """
    if not symbol:
        return "", False
    escaped = re.escape(symbol)

    if kind is ChangeKind.KWARG_RENAME:
        # Which function an argument belongs to is definitional rather than a
        # naming coincidence, so both spellings count as assertions.
        match = re.search(rf"(\w+)\s*\([^)]*\b{escaped}\s*=", text)
        if match:
            return match.group(1), True
        # "the `timeout_seconds` keyword argument on `fetch_orders`"
        match = re.search(rf"`{escaped}`[^`\n]{{0,60}}?\bon\s+`(\w+)`", text, re.IGNORECASE)
        if match:
            return match.group(1), True
        return "", False

    if kind is ChangeKind.METHOD_RENAME:
        # "the method on `client` was renamed" asserts a receiver.
        match = re.search(
            rf"\bon\s+(?:the\s+)?`(\w+)`[^`\n]{{0,60}}?`?{escaped}`?", text, re.IGNORECASE
        )
        if match and match.group(1).lower() not in _STOPWORDS:
            return match.group(1), True
        # `client.fetch_orders(...)` in a Before/After example: a hint, not a
        # constraint. See the docstring.
        match = re.search(rf"`?(\w+)\.{escaped}\s*\(", text)
        if match and match.group(1).lower() not in _STOPWORDS:
            return match.group(1), False
        return "", False

    # Field rename. "on each `order` object" asserts which object owns the
    # field; a bare `order["total"]` snippet only illustrates it.
    for pattern in (
        r"\bon\s+(?:each|the|an|a)\s+`(\w+)`\s+object",
        rf"`(\w+)`\s+object[^.\n]{{0,60}}?`{escaped}`",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match and match.group(1).lower() not in _STOPWORDS:
            return match.group(1), True
    for pattern in (
        rf"(\w+)\s*\[\s*[\"']{escaped}[\"']\s*\]",
        rf"`(\w+)\.{escaped}`",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match and match.group(1).lower() not in _STOPWORDS:
            return match.group(1), False
    return "", False


def _extract_labeled(text: str, *labels: str) -> str:
    """Pull the value of a ``**Label:** value`` bullet."""
    for match in _BOLD_FIELD.finditer(text):
        label = match.group("label").strip().lower()
        for wanted in labels:
            if label.startswith(wanted.lower()):
                return match.group("value").strip().rstrip(".")
    return ""


def _collect_evidence(section: _Section, kind: ChangeKind, symbol: str) -> list[Evidence]:
    """Quote the lines that justify the classification, with line numbers."""
    interesting = [
        "renamed",
        "removed",
        "migration",
        "breaking",
        "deprecated",
        "before",
        "after",
        "instead of",
    ]
    if symbol:
        interesting.append(symbol.lower())
    if kind is ChangeKind.PAGINATION_PAGE_TO_CURSOR:
        interesting.extend(["cursor", "total_pages", "has_more", "page"])

    evidence: list[Evidence] = []
    for offset, line in enumerate(section.text.splitlines()):
        stripped = line.strip().lstrip("#-*> ").strip()
        if not stripped:
            continue
        low = stripped.lower()
        matched = [word for word in interesting if word in low]
        if not matched:
            continue
        evidence.append(
            Evidence(
                quote=stripped[:240],
                line=section.start_line + offset,
                note=f"mentions {matched[0]}",
            )
        )
        if len(evidence) >= 5:
            break
    return evidence


def _pagination_contract(text: str) -> PaginationContract:
    """Read non-default pagination field names out of the notes."""
    contract = PaginationContract()
    identifiers = set(re.findall(r"`(\w+)`", text)) | set(
        re.findall(r"\b(\w*(?:cursor|page|more)\w*)\b", text.lower())
    )
    for candidate in identifiers:
        low = candidate.lower()
        if low.endswith("_pages") or low == "total_pages":
            contract.total_pages_key = candidate
        elif low.startswith("next") and "cursor" in low:
            contract.next_cursor_key = candidate
        elif low in ("has_more", "hasmore", "more"):
            contract.has_more_key = candidate
    return contract


def _severity(text: str) -> Severity:
    match = _RISK.search(text)
    if match:
        return Severity.parse(match.group(1), Severity.MEDIUM)
    return Severity.MEDIUM


def _confidence(kind: ChangeKind, score: int, target: SymbolTarget) -> Confidence:
    """Grade how sure we are that we read this document correctly.

    High requires both a strong classification signal *and* successful symbol
    extraction, because a rename we cannot name is a rename we cannot perform.
    """
    if not kind.is_actionable:
        return Confidence.LOW
    if kind is ChangeKind.PAGINATION_PAGE_TO_CURSOR:
        return Confidence.HIGH if score >= _STRONG_SCORE + 4 else Confidence.MEDIUM
    if not target.is_rename:
        return Confidence.LOW
    if score >= _STRONG_SCORE and target.owner:
        return Confidence.HIGH
    if score >= _STRONG_SCORE:
        return Confidence.MEDIUM
    return Confidence.LOW


def parse_section(section: _Section, source: str = "markdown") -> BreakingChange:
    """Turn one document section into a :class:`BreakingChange`."""
    kind, score, reason = classify(section.text)
    old, new, qualifier = _extract_rename(section.text, kind)
    owner, owner_is_explicit = _extract_owner(section.text, old, kind)
    if qualifier:
        # A dotted rename statement outranks anything scraped from prose.
        owner, owner_is_explicit = qualifier, True
    target = SymbolTarget(
        symbol=old,
        replacement=new,
        owner=owner,
        owner_is_explicit=owner_is_explicit,
    )

    if kind.is_actionable and kind is not ChangeKind.PAGINATION_PAGE_TO_CURSOR and not old:
        reason = (
            f"{reason}; could not extract the old and new names, so no migration can be planned"
        )

    change = BreakingChange(
        title=section.title,
        kind=kind,
        target=target,
        old_behavior=_extract_labeled(section.text, "before", "old", "previously"),
        new_behavior=_extract_labeled(section.text, "after", "new", "now"),
        migration_hint=_extract_labeled(section.text, "migration", "action", "fix"),
        severity=_severity(section.text),
        confidence=_confidence(kind, score, target),
        evidence=_collect_evidence(section, kind, old),
        source=source,
        classification_reason=reason,
    )
    if kind is ChangeKind.PAGINATION_PAGE_TO_CURSOR:
        change.pagination = _pagination_contract(section.text)
    return change


class MarkdownChangeParser(ChangeParser):
    """Parses Markdown, reStructuredText, and plain-text release notes."""

    name = "markdown"
    suffixes = (".md", ".markdown", ".txt", ".rst", ".text", "")

    def supports(self, document: ChangeDocument) -> bool:
        return document.suffix in self.suffixes

    def parse(self, document: ChangeDocument) -> list[BreakingChange]:
        sections = _split_sections(document)
        changes = [parse_section(section, source=self.name) for section in sections]

        # A multi-section document usually has prose sections that classify as
        # UNKNOWN. Drop those *only if* at least one real change was found, so a
        # document with nothing in it still reports honestly rather than
        # returning an empty list that reads like "no breaking changes".
        actionable = [c for c in changes if c.kind is not ChangeKind.UNKNOWN]
        result = actionable or changes[:1]
        log.debug(
            "parsed %s: %d section(s) -> %d change(s) [%s]",
            document.path,
            len(sections),
            len(result),
            ", ".join(c.kind.value for c in result),
        )
        return result


register(MarkdownChangeParser())
