"""Applying source ranges edits, and rendering diffs.

Edits are applied to the original text by range, not by regenerating the module
from its AST. ``ast.unparse`` would discard every comment, blank line, and
formatting choice in the file, turning a two-token rename into a whole-file
rewrite that no reviewer would approve. Range edits keep the diff to exactly the
tokens that changed.
"""

from __future__ import annotations

import ast
import difflib

from patchahead.domain.plan import TextEdit


class EditError(Exception):
    """Raised when a set of edits cannot be applied safely."""


def _offset(line_starts: list[int], line: int, col: int, length: int) -> int:
    """Absolute character offset for a 1-indexed line and 0-indexed column."""
    if line < 1 or line > len(line_starts):
        raise EditError(f"line {line} is out of range (file has {len(line_starts)} lines)")
    offset = line_starts[line - 1] + col
    if offset > length:
        raise EditError(f"position {line}:{col} is past the end of the file")
    return offset


def apply_edits(source: str, edits: list[TextEdit]) -> str:
    """Apply ``edits`` to ``source`` and return the new text.

    Edits are applied from the end of the file backwards so earlier offsets stay
    valid. Overlapping edits are rejected rather than silently resolved: two
    handlers disagreeing about the same range is a bug, and producing a
    plausible-looking merge would hide it.
    """
    if not edits:
        return source

    line_starts = [0]
    for index, char in enumerate(source):
        if char == "\n":
            line_starts.append(index + 1)
    length = len(source)

    spans: list[tuple[int, int, str]] = []
    for edit in edits:
        start = _offset(line_starts, edit.line, edit.col, length)
        end = _offset(line_starts, edit.end_line, edit.end_col, length)
        if end < start:
            raise EditError(
                f"edit at {edit.line}:{edit.col} ends before it starts "
                f"({edit.end_line}:{edit.end_col})"
            )
        spans.append((start, end, edit.new_text))

    spans.sort(key=lambda span: (span[0], span[1]))
    for (_, end, _), (next_start, _, _) in zip(spans, spans[1:], strict=False):
        if next_start < end:
            raise EditError(
                f"overlapping edits: span ending at offset {end} overlaps the span "
                f"starting at offset {next_start}"
            )

    result = source
    for start, end, new_text in reversed(spans):
        result = result[:start] + new_text + result[end:]
    return result


def is_parseable(source: str, path: str = "<patched>") -> tuple[bool, str]:
    """Whether ``source`` parses as Python, and the error if it does not.

    Used by the syntax gate. ``ast.parse`` rather than ``compile`` because it
    catches the same syntax errors without executing any compile-time side
    effects, and gives a cleaner message.
    """
    try:
        ast.parse(source, filename=path)
    except SyntaxError as exc:
        return False, f"{path}:{exc.lineno or '?'}:{exc.offset or '?'}: {exc.msg}"
    except ValueError as exc:
        return False, f"{path}: {exc}"
    return True, ""


def unified_diff(old: str, new: str, path: str, context: int = 3) -> str:
    """A git-style unified diff for one file.

    ``a/``-``b/`` prefixes and a trailing newline make the output directly
    consumable by ``git apply`` and ``patch -p1``.
    """
    if old == new:
        return ""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context,
    )
    text = "".join(diff)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def combined_diff(files: list[tuple[str, str, str]], context: int = 3) -> str:
    """A single diff across several files, ordered by path.

    ``files`` is a list of ``(path, old_source, new_source)``.
    """
    parts = [
        unified_diff(old, new, path, context=context)
        for path, old, new in sorted(files, key=lambda item: item[0])
    ]
    return "".join(part for part in parts if part)
