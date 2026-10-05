"""Scanning the patched copy for what is left of the old API.

Runs after the gates, against the workspace as patched. It is a report, not a
gate: a migration can be verified by its tests and still leave the old name
behind somewhere no test reaches, and saying where is the point. ``--require-
complete`` lets a CI run treat unfinished residuals as a failure.

Three sources, cheapest to read first:

1. **The handler's own analysis, re-run on the patched code.** Whatever it still
   finds was not rewritten -- below the confidence threshold, an unfamiliar
   shape, or another object that shares the name.
2. **Python tokens.** Strings and comments that mention the name, a string
   handed to ``getattr`` (an access no static rewrite can see), tests that
   still use the old name, and -- for a method rename -- any other use of the
   name, such as the ``from sdk import fetch_orders`` that the call-site rewrite
   leaves behind.
3. **Configuration and documentation files** that mention it.
"""

from __future__ import annotations

import ast
import builtins
import io
import logging
import re
import tokenize
from pathlib import Path

from patchahead.analysis import index as repo_index
from patchahead.analysis.edits import read_source
from patchahead.analysis.python_ast import ColumnMap, receiver_matches_owner
from patchahead.config import Config
from patchahead.domain.change import BreakingChange, ChangeKind
from patchahead.domain.completeness import CompletenessReport, Residual, ResidualKind
from patchahead.domain.impact import ImpactReport

log = logging.getLogger(__name__)

#: Text files worth reading for mentions, by suffix.
TEXT_FILES: dict[str, ResidualKind] = {
    ".yaml": ResidualKind.CONFIG,
    ".yml": ResidualKind.CONFIG,
    ".json": ResidualKind.CONFIG,
    ".toml": ResidualKind.CONFIG,
    ".ini": ResidualKind.CONFIG,
    ".cfg": ResidualKind.CONFIG,
    ".env": ResidualKind.CONFIG,
    ".md": ResidualKind.DOCS,
    ".rst": ResidualKind.DOCS,
    ".txt": ResidualKind.DOCS,
}
#: A text file larger than this is skipped rather than read.
MAX_TEXT_BYTES = 1_000_000
_DYNAMIC_ACCESS = re.compile(r"\b(?:getattr|hasattr|setattr|delattr)\s*\(")
_INSIGNIFICANT = (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT)


def scan(
    change: BreakingChange,
    remaining: ImpactReport,
    index: repo_index.RepoIndex,
    root: Path,
    config: Config,
) -> CompletenessReport:
    """Every place ``change``'s old name survives in the patched tree at ``root``.

    ``remaining`` is the handler's analysis re-run on the patched code and
    ``index`` the patched tree's index.
    """
    old, new = _names(change)
    report = CompletenessReport(old=old, new=new)
    seen: set[tuple[str, int]] = set()
    # Tokens the handler already read, site by site; the token scan below must
    # not read them again, less precisely.
    classified = {(f.path, f.reference.line, f.reference.col) for f in remaining.findings}

    for finding in remaining.findings:
        if finding.other_object:
            kind, reason = ResidualKind.OTHER_OBJECT, finding.unpatchable_reason
        elif finding.patchable:
            kind = ResidualKind.CODE
            reason = (
                f"confidence {finding.confidence.value} is below the "
                f"`{config.min_confidence.value}` threshold"
            )
        else:
            kind, reason = ResidualKind.CODE, finding.unpatchable_reason or finding.reason
        _add(
            report,
            seen,
            finding.path,
            finding.reference.line,
            kind,
            finding.reference.snippet,
            reason,
        )

    symbol = change.target.symbol
    if not symbol:
        return report
    word = re.compile(rf"\b{re.escape(symbol)}\b")

    for path in index.paths():
        module = index.modules[path]
        _scan_python(report, seen, change, path, module.source, word, classified)

    for path in _text_files(root, config):
        try:
            text = read_source(root / path)
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if word.search(line):
                kind = TEXT_FILES[Path(path).suffix.lower()]
                _add(report, seen, path, number, kind, line.strip(), f"mentions `{symbol}`")

    report.residuals.sort(key=lambda r: (list(ResidualKind).index(r.kind), r.path, r.line))
    log.debug(
        "completeness for `%s`: %d residual(s), %d unfinished",
        old,
        len(report.residuals),
        len(report.unfinished),
    )
    return report


def _names(change: BreakingChange) -> tuple[str, str]:
    if change.kind is ChangeKind.PAGINATION_PAGE_TO_CURSOR:
        return change.pagination.page_param, change.pagination.cursor_param
    return change.target.symbol, change.target.replacement


def _scan_python(
    report: CompletenessReport,
    seen: set[tuple[str, int]],
    change: BreakingChange,
    path: str,
    source: str,
    word: re.Pattern[str],
    classified: set[tuple[str, int, int]] = frozenset(),
) -> None:
    symbol = change.target.symbol
    is_test = repo_index.is_test_path(path)
    lines = source.splitlines()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return

    # `dict` the method and `dict` the built-in type share a name. A bare `dict`
    # -- in `-> dict`, `isinstance(x, dict)`, `dict(...)` -- is the built-in,
    # unless this module imports the name from somewhere.
    shadows_builtin = symbol in vars(builtins) and not re.search(
        rf"^\s*(?:from\s+\S+\s+)?import\b[^\n#]*\b{re.escape(symbol)}\b", source, re.MULTILINE
    )
    previous = None
    for token in tokens:
        line = token.start[0]
        text = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        after_dot = previous is not None and previous.string == "."
        if token.type not in _INSIGNIFICANT:
            previous = token
        if (path, line, token.start[1]) in classified:
            continue
        if token.type == tokenize.NAME and token.string == symbol:
            if (
                change.kind is ChangeKind.METHOD_RENAME
                and shadows_builtin
                and not after_dot
                and not re.match(rf"(?:async\s+)?def\s+{re.escape(symbol)}\b", text)
            ):
                continue
            if is_test:
                _add(
                    report,
                    seen,
                    path,
                    line,
                    ResidualKind.TEST,
                    text,
                    "a test still uses the old name",
                )
            elif change.kind is ChangeKind.METHOD_RENAME:
                # Field and keyword names are ordinary variable names too; a
                # method's name appearing as a name is an import, a reference,
                # or a definition -- each worth a look.
                _add(report, seen, path, line, ResidualKind.CODE, text, _name_reason(text, symbol))
        elif token.type == tokenize.STRING and word.search(token.string):
            kind, reason = _string_residual(change, token, lines, text, is_test)
            if kind in (ResidualKind.CODE, ResidualKind.TEST) and "dictionary" in reason:
                other = _dict_owner(change, source, token.start)
                if other:
                    kind = ResidualKind.OTHER_OBJECT
                    reason = (
                        f"a dictionary for `{other}`, not `{change.target.owner}`: its "
                        f"`{symbol}` is that object's own field"
                    )
            _add(report, seen, path, line, kind, text, reason)
        elif token.type == tokenize.COMMENT and word.search(token.string):
            _add(
                report,
                seen,
                path,
                line,
                ResidualKind.COMMENT,
                text,
                f"a comment mentions `{symbol}`",
            )


def _string_residual(
    change: BreakingChange,
    token: tokenize.TokenInfo,
    lines: list[str],
    text: str,
    is_test: bool,
) -> tuple[ResidualKind, str]:
    """Classify a string literal that contains the old name."""
    symbol = change.target.symbol
    literal = token.string.strip("rbuRBUfF").strip("'\"")
    if literal == symbol and _DYNAMIC_ACCESS.search(text):
        return (
            ResidualKind.DYNAMIC,
            "the old name is passed as a string, which no static rewrite can follow",
        )
    single_line = token.start[0] == token.end[0]
    if literal == symbol and change.kind is ChangeKind.FIELD_RENAME and single_line:
        role = _key_role(lines[token.start[0] - 1], token.start[1], token.end[1])
        if role:
            return (
                (ResidualKind.TEST, f"a test {role}")
                if is_test
                else (
                    ResidualKind.CODE,
                    f"code {role}",
                )
            )
    return ResidualKind.STRING, f"a string mentions `{symbol}`"


def _dict_owner(change: BreakingChange, source: str, start: tuple[int, int]) -> str:
    """The other object a dictionary literal is named for, or ``""``.

    ``{"name": "Ada", "total": 250}`` is a customer's when it is the value of a
    ``"customer"`` key, assigned to ``customer``, or passed as ``customer=``. Only
    an asserted owner is checked against; a dictionary named for the owner, or
    for nothing, is not another object's.
    """
    owner = change.target.owner if change.target.owner_is_explicit else ""
    if not owner:
        return ""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ""
    columns = ColumnMap(source)
    line, col = start
    for node in ast.walk(tree):
        named = _named_dicts(node)
        for name, value in named:
            if not isinstance(value, ast.Dict):
                continue
            for key in value.keys:
                if (
                    isinstance(key, ast.Constant)
                    and key.lineno == line
                    and columns.char_col(key.lineno, key.col_offset) == col
                ):
                    return "" if receiver_matches_owner(name, owner) else name
    return ""


def _named_dicts(node: ast.AST) -> list[tuple[str, ast.AST]]:
    """``(name, value)`` for each value ``node`` gives a name to."""
    if isinstance(node, ast.Dict):
        return [
            (key.value, value)
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return [(target.id, node.value)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
        return [(node.target.id, node.value)]
    if isinstance(node, ast.Call):
        return [(k.arg, k.value) for k in node.keywords if k.arg]
    return []


def _key_role(line: str, start: int, end: int) -> str:
    """How a string literal equal to a field name is used, if as a key at all.

    ``x["total"]`` and ``x.get("total")`` read the field; ``{"total": 5}``
    builds a payload or fixture with it. Any other string -- a label, a
    message -- only mentions the word.
    """
    before, after = line[:start].rstrip(), line[end:].lstrip()
    if before.endswith("[") and after.startswith("]"):
        return "reads the old field by key"
    if before.endswith(".get("):
        return "reads the old field with .get()"
    if after.startswith(":") and before.endswith(("{", ",")):
        return "builds a dictionary with the old field name"
    return ""


def _name_reason(line: str, symbol: str) -> str:
    if re.match(r"(?:from\s+\S+\s+)?import\b", line):
        return "an import of the old name; call sites were rewritten, imports are not"
    if re.match(rf"(?:async\s+)?def\s+{re.escape(symbol)}\b", line):
        return "a definition with the old name in this repository"
    return "a use of the old name that is not a call PatchAhead could rewrite"


def _add(
    report: CompletenessReport,
    seen: set[tuple[str, int]],
    path: str,
    line: int,
    kind: ResidualKind,
    snippet: str,
    reason: str,
) -> None:
    """Record a residual once per line: the first, most specific reading wins.

    Except that unfinished work is never hidden: on a line with both a
    customer's `{"total": 1}` and an order's `{"total": 2}`, the order's is what
    the line reports, whichever comes first.
    """
    residual = Residual(path=path, line=line, kind=kind, snippet=snippet[:200], reason=reason)
    if (path, line) in seen:
        for index, existing in enumerate(report.residuals):
            if (existing.path, existing.line) == (path, line):
                if kind.unfinished and not existing.kind.unfinished:
                    report.residuals[index] = residual
                return
        return
    seen.add((path, line))
    report.residuals.append(residual)


def _text_files(root: Path, config: Config) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in TEXT_FILES or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if repo_index.is_excluded(relative, config.exclude):
            continue
        try:
            if path.stat().st_size > MAX_TEXT_BYTES:
                continue
        except OSError:
            continue
        found.append(relative)
    return found
