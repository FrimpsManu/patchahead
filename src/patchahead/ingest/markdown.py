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

**A section can hold several changes.** Vendors list renames as bullets, table
rows, or two clauses of one sentence under a single heading. Each statement is
read on its own, so a table of four renamed methods yields four changes, and a
bullet about an endpoint move is reported as unsupported rather than dropped.
A section that states one rename is still read as a whole, which is what lets a
Before/After example under it supply an owner.
"""

from __future__ import annotations

import html
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
    (r"replaced by an? `[^`]+` (?:dict|dictionary|object|class|mapping)", 5, "restructuring"),
]

#: Minimum score before a classification is trusted at all.
_MIN_SCORE = 3
#: Score at or above which a classification is considered confident.
_STRONG_SCORE = 5

_HEADING = re.compile(r"^(#{2,4})\s+(.+?)\s*#*$", re.MULTILINE)
_NON_BREAKING_HEADING = re.compile(
    r"^#{1,4}\s+(?:non[- ]breaking|additions?|added|improvements?|enhancements?|"
    r"fixes|fixed|bug ?fixes|(?:new )?features?)\b",
    re.IGNORECASE,
)

#: Nouns a release note puts between a renamed name and the word "to": "renamed
#: the `retries` keyword argument to `max_retries`". Spelled out rather than
#: allowed as "any two words", because `\w+` would also match "renamed the
#: `client` argument passed to `fetch_orders`" -- which names two symbols and
#: renames neither of them.
_RENAME_NOUN = (
    r"(?:keyword|kwarg|argument|arg|parameter|param|field|attribute|property"
    r"|option|setting|method|function)s?"
)


def _name(group: str) -> str:
    """A backticked name, recording whether it was written as a call or a keyword."""
    return rf"`(?P<{group}>\.?[A-Za-z_][\w.]*)(?P<{group}_call>\(\))?(?P<{group}_kw>=)?`"


_OLD, _NEW = _name("old"), _name("new")
#: "of `get()` and `post()`", "on the `Charge` object": a clause naming what the
#: renamed thing belongs to, which may sit between the name and the verb.
_QUALIFIER = (
    r"(?:\s+(?:of|on|for|in)\s+(?:the\s+|each\s+|all\s+)?"
    r"(?:`[^`\n]+`(?:\s*(?:,|and|or)\s*`[^`\n]+`)*|[A-Z]\w*)(?:\s+[a-z]+)?)?"
)
_VERB = (
    r"(?:(?:(?:was|is|are|were|has\s+been|have\s+been)\s+)?(?:renamed|changed)\s+to"
    r"|(?:was|is|are|were|has\s+been|have\s+been)\s+replaced\s+(?:by|with)"
    r"|(?:is|are)\s+now(?:\s+(?:called|named|spelled|returned\s+as|exposed\s+as))?"
    r"|(?:is|was|has\s+been)\s+deprecated\s+in\s+favou?r\s+of)"
)
#: How each notation states a rename, most trustworthy first. ``\`a\` ->
#: \`b\``` is unambiguous; a subject-verb sentence can pick up the wrong name
#: from an intervening clause, which is why the qualifier is matched explicitly.
_RENAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"{_OLD}\s*(?:->|→|=>)\s*{_NEW}"),
    re.compile(
        rf"renamed\s+(?:from\s+)?(?:the\s+|an?\s+)?{_OLD}(?:\s+{_RENAME_NOUN}){{0,2}}"
        rf"\s+to\s+{_NEW}",
        re.IGNORECASE,
    ),
    re.compile(rf"{_OLD}(?:\s+[A-Za-z]+){{0,3}}?{_QUALIFIER}\s+{_VERB}\s+{_NEW}", re.IGNORECASE),
    re.compile(rf"\b(?:use|call)\s+{_NEW}\s+instead\s+of\s+{_OLD}", re.IGNORECASE),
    re.compile(rf"\breplace\s+{_OLD}\s+with\s+{_NEW}", re.IGNORECASE),
)
#: "Renamed `a()` to `b()` and `c()` to `d()`": the second pair, only read in a
#: statement that already says "renamed".
_AND_TO = re.compile(rf"(?:,|\band)\s+{_OLD}\s+to\s+{_NEW}")
#: Words that look like a new name after "is now" but describe a type or a state.
_NOT_A_NAME = {
    "int",
    "float",
    "str",
    "bool",
    "list",
    "dict",
    "tuple",
    "set",
    "bytes",
    "none",
    "null",
    "true",
    "false",
    "optional",
    "required",
    "datetime",
    "date",
    "decimal",
    "any",
    "object",
}
#: The construct a noun names, for deciding what kind of rename a statement is.
_NOUN_KINDS: tuple[tuple[re.Pattern[str], ChangeKind], ...] = (
    (
        re.compile(
            r"\b(?:keyword\s+arguments?|kwargs?|arguments?|args|parameters?|params?)\b", re.I
        ),
        ChangeKind.KWARG_RENAME,
    ),
    (re.compile(r"\b(?:methods?|functions?)\b", re.I), ChangeKind.METHOD_RENAME),
    (
        re.compile(r"\b(?:fields?|propert(?:y|ies)|attributes?|keys?)\b", re.I),
        ChangeKind.FIELD_RENAME,
    ),
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


_HTML_BLOCK = re.compile(r"<(?:h[1-6]|li|ul|ol|p|code|blockquote|details)\b", re.IGNORECASE)
_HTML_HEADING = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1\s*>", re.IGNORECASE | re.DOTALL)
_COMMITS_BLOCK = re.compile(
    r"<details>\s*<summary>\s*Commits\s*</summary>.*?</details>", re.IGNORECASE | re.DOTALL
)


def _from_html(text: str) -> str:
    """Rewrite HTML release notes -- what Dependabot puts in a pull request -- as Markdown.

    Headings become ``##``/``###``, list items become bullets, ``<code>``
    becomes backticks, and every other tag is dropped. A Dependabot "Commits"
    list is removed first: it is commit subjects, not release notes.
    """
    text = _COMMITS_BLOCK.sub("", text)
    text = _HTML_HEADING.sub(
        lambda m: f"\n{'#' * min(max(int(m.group(1)), 2), 4)} {m.group(2).strip()}\n", text
    )
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>|</p\s*>|</li\s*>|</?[uo]l[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text)


_UNDERLINE = re.compile(r"^([=\-~^\"'*+#])\1{2,}\s*$")


def _normalize(text: str) -> str:
    """Rewrite reStructuredText (and Markdown setext) notation as ATX Markdown.

    A double-backtick literal becomes a single-backtick one, and a title underlined with
    ``====`` or ``----`` becomes a ``##`` heading -- levels assigned in order
    of first appearance, as reStructuredText does. The underline is replaced
    with a blank line rather than removed, so line numbers in evidence still
    point at the original document.
    """
    if _HTML_BLOCK.search(text):
        text = _from_html(text)
    text = re.sub(r"``([^`\n]+)``", r"`\1`", text)
    lines = text.splitlines()
    levels: dict[str, int] = {}
    for index in range(1, len(lines)):
        match = _UNDERLINE.match(lines[index])
        title = lines[index - 1].strip()
        if not match or not title or _UNDERLINE.match(title) or title.startswith(("-", "*", "|")):
            continue
        level = levels.setdefault(match.group(1), min(2 + len(levels), 4))
        lines[index - 1] = f"{'#' * level} {title}"
        lines[index] = ""
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _split_sections(document: ChangeDocument) -> list[_Section]:
    """Split a document at its headings, dropping non-breaking-change sections.

    A release note usually describes several changes. Treating the whole file as
    one blob lets a "Non-breaking changes" bullet contribute signal words to a
    breaking change's classification.
    """
    text = _normalize(document.text)
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


@dataclass(frozen=True)
class _Rename:
    """One rename statement: ``old`` -> ``new`` as written, and where it was found."""

    old: str
    new: str
    #: How the names were written: a call ``x()`` or a keyword ``x=``.
    call: bool
    keyword: bool
    #: Notation rank (lower is more trustworthy), then position in the text.
    priority: int
    start: int
    end: int

    @property
    def symbol(self) -> str:
        return self.old.rsplit(".", 1)[-1]

    @property
    def replacement(self) -> str:
        return self.new.rsplit(".", 1)[-1]

    @property
    def qualifier(self) -> str:
        """The receiver prefix of a dotted old name -- an *asserted* owner."""
        return self.old.rsplit(".", 1)[0].lstrip(".") if "." in self.old.lstrip(".") else ""

    @property
    def is_move(self) -> bool:
        """``a.X.create`` -> ``b.y.create``: the name is unchanged, its home is not."""
        return self.symbol == self.replacement


def _find_renames(text: str) -> list[_Rename]:
    """Every rename a piece of text states, in document order.

    Several notations can match the same words, and they are not equally
    trustworthy: in "The `timeout_seconds` keyword argument on `fetch_orders`
    was renamed to `timeout`", a bare "`x` was renamed to `y`" reading would
    pick ``fetch_orders`` -> ``timeout``. Matches are therefore taken best
    notation first, and a match overlapping one already taken is discarded.
    """
    candidates: list[_Rename] = []
    for priority, pattern in enumerate(_RENAME_PATTERNS):
        candidates.extend(_rename(match, priority) for match in pattern.finditer(text))
    if re.search(r"\brenamed\b", text, re.IGNORECASE):
        candidates.extend(_rename(match, len(_RENAME_PATTERNS)) for match in _AND_TO.finditer(text))

    taken: list[_Rename] = []
    for candidate in sorted(candidates, key=lambda r: (r.priority, r.start)):
        if not _plausible(candidate):
            continue
        if any(candidate.start < r.end and r.start < candidate.end for r in taken):
            continue
        taken.append(candidate)
    return sorted(taken, key=lambda r: r.start)


def _rename(match: re.Match[str], priority: int) -> _Rename:
    return _Rename(
        old=match.group("old"),
        new=match.group("new"),
        call=bool(match.group("old_call") or match.group("new_call")),
        keyword=bool(match.group("old_kw") or match.group("new_kw")),
        priority=priority,
        start=match.start(),
        end=match.end(),
    )


def _plausible(rename: _Rename) -> bool:
    old, new = rename.symbol.lower(), rename.replacement.lower()
    if not old or not new or rename.old == rename.new:
        return False
    if old in _STOPWORDS or new in _STOPWORDS:
        return False
    # "`timeout` is now `float`" changes a type, not a name. A type is written
    # bare; `list()` or `dict=` is a call or a keyword, and a real new name.
    return rename.call or rename.keyword or new not in _NOT_A_NAME


@dataclass
class _Statement:
    """One bullet, table row, paragraph or heading inside a section."""

    text: str
    line: int


_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
_OLD_COLUMN = re.compile(r"(?i)\b(?:old|before|previous(?:ly)?|from|deprecated|removed)\b")
_NEW_COLUMN = re.compile(r"(?i)\b(?:new|after|replacement|now|to|use)\b")


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _statements(section: _Section) -> tuple[list[_Statement], str]:
    """Split a section into statements, plus the prose that frames them.

    A table row becomes ``old → new`` followed by its remaining cells, with the
    columns chosen by their headers ("Old"/"New", "Before"/"After", "v1"/"v2").
    The returned context -- the heading and any prose that is not a bullet or a
    row -- is what each statement is read against for the nouns and owners a
    row or terse bullet leaves out.
    """
    lines = section.text.splitlines()
    statements: list[_Statement] = []
    context: list[str] = []
    in_fence = False
    index = 0
    while index < len(lines):
        line = lines[index]
        number = section.start_line + index
        if line.strip().startswith("```"):
            in_fence = not in_fence
            index += 1
            continue
        if in_fence or not line.strip():
            index += 1
            continue

        if (
            line.lstrip().startswith("|")
            and index + 1 < len(lines)
            and _TABLE_RULE.match(lines[index + 1])
        ):
            header = _cells(line)
            old_col = next((i for i, c in enumerate(header) if _OLD_COLUMN.search(c)), 0)
            new_col = next(
                (i for i, c in enumerate(header) if i != old_col and _NEW_COLUMN.search(c)),
                1 if old_col == 0 else 0,
            )
            index += 2
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                cells = _cells(lines[index])
                if max(old_col, new_col) < len(cells):
                    rest = [c for i, c in enumerate(cells) if i not in (old_col, new_col)]
                    text = f"{cells[old_col]} → {cells[new_col]} {' '.join(rest)}".strip()
                    statements.append(_Statement(text, section.start_line + index))
                index += 1
            continue

        if _BULLET.match(line):
            parts = [_BULLET.sub("", line, count=1)]
            index += 1
            while (
                index < len(lines)
                and lines[index].strip()
                and not _BULLET.match(lines[index])
                and lines[index].startswith((" ", "\t"))
            ):
                parts.append(lines[index].strip())
                index += 1
            statements.append(_Statement(" ".join(parts), number))
            continue

        # A heading or a prose paragraph: a statement in its own right, and
        # the frame the bullets and rows are read against.
        parts = [line.strip().lstrip("#").strip()]
        index += 1
        while (
            not line.lstrip().startswith("#")
            and index < len(lines)
            and lines[index].strip()
            and not _BULLET.match(lines[index])
            and not lines[index].lstrip().startswith(("|", "#", "```"))
        ):
            parts.append(lines[index].strip())
            index += 1
        paragraph = " ".join(parts)
        statements.append(_Statement(paragraph, number))
        context.append(paragraph)
    return statements, "\n".join(context)


def _noun_kind(text: str, near: int | None = None) -> ChangeKind | None:
    """The construct a statement's nouns name; the noun nearest ``near`` wins."""
    best: tuple[int, ChangeKind] | None = None
    for pattern, kind in _NOUN_KINDS:
        for match in pattern.finditer(text):
            distance = abs(match.start() - near) if near is not None else 0
            if best is None or distance < best[0]:
                best = (distance, kind)
    if near is None and best is not None:
        kinds = {kind for pattern, kind in _NOUN_KINDS if pattern.search(text)}
        return best[1] if len(kinds) == 1 else None
    return best[1] if best else None


def _rename_kind(rename: _Rename, statement: str, context: str) -> tuple[ChangeKind, int, str]:
    """Decide what kind of rename one statement states.

    The way the names are written is the strongest evidence (``x()`` is called,
    ``x=`` is passed); then a noun beside the old name ("the `x` keyword
    argument"); then the scored signals of the statement; then the nouns of the
    heading and prose around it.
    """
    if rename.keyword:
        return ChangeKind.KWARG_RENAME, _STRONG_SCORE, "written as a keyword argument (`name=`)"
    if rename.call:
        return ChangeKind.METHOD_RENAME, _STRONG_SCORE, "written as a call (`name()`)"
    noun = _noun_kind(statement, near=rename.start)
    if noun is not None:
        return noun, _STRONG_SCORE, f"named as a {noun.value.split('_')[0]} in the statement"
    kind, score, reason = classify(statement)
    if kind.is_actionable and kind is not ChangeKind.PAGINATION_PAGE_TO_CURSOR:
        return kind, score, reason
    shape = _example_kind(rename.symbol, context)
    if shape is not None:
        return shape, _STRONG_SCORE, f"used as a {shape.value.split('_')[0]} in the examples"
    noun = _noun_kind(context)
    if noun is not None:
        return noun, _STRONG_SCORE, f"named as a {noun.value.split('_')[0]} in the section"
    return ChangeKind.UNKNOWN, 0, "a rename, but nothing says of what"


def _example_kind(symbol: str, text: str) -> ChangeKind | None:
    """How the old name is used in the document's code examples, if only one way.

    ``client.fetch_all(limit=10)`` shows a call; ``fetch(timeout_seconds=5)`` a
    keyword; ``order["total"]`` a field. Two different uses decide nothing.
    """
    escaped = re.escape(symbol)
    uses = {
        kind
        for kind, pattern in (
            (ChangeKind.METHOD_RENAME, rf"(?<![\w\[\"'])\.?{escaped}\s*\("),
            (ChangeKind.KWARG_RENAME, rf"[(,]\s*{escaped}\s*=(?!=)"),
            (ChangeKind.FIELD_RENAME, rf"\[\s*[\"']{escaped}[\"']\s*\]"),
        )
        if re.search(pattern, text)
    }
    return uses.pop() if len(uses) == 1 else None


def _unsupported_label(text: str) -> str:
    low = text.lower()
    for pattern, _weight, label in _UNSUPPORTED_SIGNALS:
        if re.search(pattern, low):
            return label
    return ""


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
        # "... of `Client.get()`" -- one function only; "of `get()` and `post()`"
        # names two, and neither alone is the owner.
        match = re.search(
            rf"`{escaped}=?`[^`\n]{{0,60}}?\b(?:on|of|for)\s+(?:the\s+)?"
            r"`(?:[\w.]*\.)?(\w+)(?:\(\))?`(?!\s*(?:,|and|or)\s*`)",
            text,
            re.IGNORECASE,
        )
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
        match = re.search(
            rf"`{escaped}(?:\(\))?`[^\n]{{0,60}}?\bon\s+(?:the\s+)?`(\w+)`", text, re.IGNORECASE
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
        r"\bon\s+(?:(?:each|the|an|a|all)\s+)?`(\w+)`\s+objects?",
        # An API reference's capitalized object name, written without backticks.
        r"\bon\s+(?:(?:each|the|an|a|all)\s+)?([A-Z]\w*)\s+objects?",
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
        stripped = _plain(line.strip().lstrip("#-*> ")).strip()
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
    # In document order, backticked names first: they are the vendor's literal
    # spelling. The first candidate for each field wins. This used to iterate a
    # `set`, so with several candidates the field chosen depended on the hash
    # seed -- the same note could produce a different migration on each run.
    identifiers = dict.fromkeys(
        re.findall(r"`(\w+)`", text) + re.findall(r"\b(\w*(?:cursor|page|more)\w*)\b", text.lower())
    )
    assigned: set[str] = set()
    for candidate in identifiers:
        low = candidate.lower()
        if low.endswith("_pages") or low == "total_pages":
            key = "total_pages_key"
        elif low.startswith("next") and "cursor" in low:
            key = "next_cursor_key"
        elif low in ("has_more", "hasmore", "more"):
            key = "has_more_key"
        else:
            continue
        if key not in assigned:
            assigned.add(key)
            setattr(contract, key, candidate)
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


def parse_section(section: _Section, source: str = "markdown") -> list[BreakingChange]:
    """Turn one document section into the breaking changes it states."""
    kind, score, reason = classify(section.text)
    if kind is ChangeKind.PAGINATION_PAGE_TO_CURSOR:
        return [_pagination_change(section, score, reason, source)]

    statements, context = _statements(section)
    renames: list[tuple[_Rename, _Statement]] = []
    seen: set[tuple[str, str]] = set()
    unsupported: list[tuple[_Statement, str]] = []
    for statement in statements:
        found = _find_renames(statement.text)
        for rename in found:
            key = (rename.old.lstrip("."), rename.new.lstrip("."))
            if key not in seen:
                seen.add(key)
                renames.append((rename, statement))
        label = "" if found else _unsupported_label(statement.text)
        if label:
            unsupported.append((statement, label))

    if len(renames) == 1 and not unsupported:
        # One rename: the whole section describes it, so its Before/After
        # examples and prose all count as evidence for owner and kind.
        rename, statement = renames[0]
        return [_rename_change(rename, statement.text, section.text, section, source)]
    if renames or len(unsupported) > 1:
        changes = [
            _rename_change(
                rename, statement.text, f"{statement.text}\n{context}", section, source, statement
            )
            for rename, statement in renames
        ]
        changes.extend(
            _unsupported_change(statement, label, section, source)
            for statement, label in unsupported
        )
        return changes
    return [_section_change(section, kind, score, reason, source)]


def _rename_change(
    rename: _Rename,
    statement: str,
    local: str,
    section: _Section,
    source: str,
    located: _Statement | None = None,
) -> BreakingChange:
    """A change for one rename statement, read against ``local`` for its owner."""
    title = section.title if located is None else _plain(statement)
    evidence = (
        _collect_evidence(section, ChangeKind.UNKNOWN, rename.symbol)
        if located is None
        else [Evidence(quote=_plain(statement)[:240], line=located.line, note="states the rename")]
    )
    if rename.is_move:
        return BreakingChange(
            title=title,
            kind=ChangeKind.UNSUPPORTED,
            severity=_severity(section.text),
            confidence=Confidence.LOW,
            evidence=evidence,
            source=source,
            classification_reason=(
                f"`{rename.old}` -> `{rename.new}` keeps the name `{rename.symbol}` and "
                f"changes where it lives -- a move to a different module or object, "
                f"which PatchAhead v1 cannot migrate"
            ),
        )

    context = local if located is None else local.split("\n", 1)[-1]
    kind, score, reason = _rename_kind(rename, statement, context)
    if kind is ChangeKind.UNKNOWN and located is None:
        kind, score, reason = classify(section.text)
        if kind is ChangeKind.PAGINATION_PAGE_TO_CURSOR:
            kind = ChangeKind.UNKNOWN
    owner, owner_is_explicit = _extract_owner(local, rename.symbol, kind)
    if rename.qualifier and kind is not ChangeKind.KWARG_RENAME:
        # A dotted rename statement outranks anything scraped from prose.
        owner, owner_is_explicit = rename.qualifier, True
    target = SymbolTarget(
        symbol=rename.symbol,
        replacement=rename.replacement,
        owner=owner,
        owner_is_explicit=owner_is_explicit,
    )
    text = section.text if located is None else statement
    return BreakingChange(
        title=title,
        kind=kind,
        target=target,
        old_behavior=_extract_labeled(text, "before", "old", "previously"),
        new_behavior=_extract_labeled(text, "after", "new", "now"),
        migration_hint=_extract_labeled(text, "migration", "action", "fix"),
        severity=_severity(section.text),
        confidence=_confidence(kind, score, target),
        evidence=evidence
        if located is not None
        else _collect_evidence(section, kind, rename.symbol),
        source=source,
        classification_reason=f"`{rename.old}` -> `{rename.new}`: {reason}",
    )


def _unsupported_change(
    statement: _Statement, label: str, section: _Section, source: str
) -> BreakingChange:
    return BreakingChange(
        title=_plain(statement.text),
        kind=ChangeKind.UNSUPPORTED,
        severity=_severity(section.text),
        confidence=Confidence.LOW,
        evidence=[Evidence(quote=_plain(statement.text)[:240], line=statement.line)],
        source=source,
        classification_reason=f"looks like a {label}, which PatchAhead v1 cannot migrate",
    )


def _section_change(
    section: _Section, kind: ChangeKind, score: int, reason: str, source: str
) -> BreakingChange:
    """A section that states no rename: classified as a whole, as before."""
    if kind.is_actionable:
        reason = (
            f"{reason}; could not extract the old and new names, so no migration can be planned"
        )
    target = SymbolTarget()
    return BreakingChange(
        title=section.title,
        kind=kind,
        target=target,
        old_behavior=_extract_labeled(section.text, "before", "old", "previously"),
        new_behavior=_extract_labeled(section.text, "after", "new", "now"),
        migration_hint=_extract_labeled(section.text, "migration", "action", "fix"),
        severity=_severity(section.text),
        confidence=_confidence(kind, score, target),
        evidence=_collect_evidence(section, kind, ""),
        source=source,
        classification_reason=reason,
    )


def _pagination_change(section: _Section, score: int, reason: str, source: str) -> BreakingChange:
    """A page-to-cursor change, if the note names the cursor it moves to.

    The handler writes the cursor parameter and response fields into the
    repository. A note that describes cursor pagination without naming the
    parameter -- `starting_after`, `NextToken` -- would have PatchAhead write
    default names the vendor never used, so it is reported instead.
    """
    names_cursor = re.search(r"`cursor=?`|\bcursor=", section.text)
    change = BreakingChange(
        title=section.title,
        kind=ChangeKind.PAGINATION_PAGE_TO_CURSOR if names_cursor else ChangeKind.UNSUPPORTED,
        old_behavior=_extract_labeled(section.text, "before", "old", "previously"),
        new_behavior=_extract_labeled(section.text, "after", "new", "now"),
        migration_hint=_extract_labeled(section.text, "migration", "action", "fix"),
        severity=_severity(section.text),
        evidence=_collect_evidence(section, ChangeKind.PAGINATION_PAGE_TO_CURSOR, ""),
        source=source,
        classification_reason=reason
        if names_cursor
        else (
            "a move to cursor-based pagination, but the note never names a `cursor` "
            "parameter, so the names PatchAhead would write are a guess"
        ),
    )
    change.confidence = _confidence(change.kind, score, change.target)
    if names_cursor:
        change.pagination = _pagination_contract(section.text)
    return change


def _plain(text: str) -> str:
    """Statement text without Markdown emphasis, for titles and quotes."""
    return re.sub(r"\*\*|__", "", text).strip()


def _distinct(changes: list[BreakingChange]) -> list[BreakingChange]:
    """Drop a rename the document states twice, keeping the first statement.

    Release notes repeat themselves: a GitHub release and the project's
    changelog, both quoted in one Dependabot pull request, describe the same
    change. Patching it twice would report the second as "no impact".
    """
    seen: set[tuple[str, str, str, str]] = set()
    kept: list[BreakingChange] = []
    for change in changes:
        target = change.target
        key = (change.kind.value, target.symbol, target.replacement, target.owner)
        if target.is_rename and key in seen:
            continue
        seen.add(key)
        kept.append(change)
    return kept


class MarkdownChangeParser(ChangeParser):
    """Parses Markdown, reStructuredText, and plain-text release notes."""

    name = "markdown"
    suffixes = (".md", ".markdown", ".txt", ".rst", ".text", "")

    def supports(self, document: ChangeDocument) -> bool:
        return document.suffix in self.suffixes

    def parse(self, document: ChangeDocument) -> list[BreakingChange]:
        sections = _split_sections(document)
        changes = [
            change for section in sections for change in parse_section(section, source=self.name)
        ]

        # A multi-section document usually has prose sections that classify as
        # UNKNOWN. Drop those *only if* at least one real change was found, so a
        # document with nothing in it still reports honestly rather than
        # returning an empty list that reads like "no breaking changes".
        actionable = _distinct([c for c in changes if c.kind is not ChangeKind.UNKNOWN])
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
