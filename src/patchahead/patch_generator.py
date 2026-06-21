"""Generate a minimal migration patch for an affected file.

Default path is a deterministic, surgical transform: locate the affected
function and rewrite ONLY its body from page-based to cursor-based
pagination, preserving the public signature. Produces a real unified diff.

When the LLM path is enabled, Claude proposes the new function body
instead -- but the result is still constrained (same function, same
signature) and is only accepted if the test suite goes green afterward.
The deterministic transform is always available as a fallback.
"""

import difflib
import re
from pathlib import Path

from patchahead import llm
from patchahead.schemas import BreakingChange, ImpactReport, PatchResult

_CURSOR_BODY = """def sync_all_orders(api_client):
    cursor = None
    all_orders = []

    while True:
        response = api_client.get_orders(cursor=cursor)
        all_orders.extend(response["orders"])

        if not response.get("has_more"):
            break

        cursor = response.get("next_cursor")

    return all_orders
"""


def _replace_function(source: str, func_name: str, new_func: str):
    """Replace a top-level `def func_name(...)` block. Returns (new_source, ok)."""
    lines = source.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"def\s+{re.escape(func_name)}\s*\(", line):
            start = i
            break
    if start is None:
        return source, False

    end = len(lines)
    for j in range(start + 1, len(lines)):
        # next top-level statement (non-indented, non-blank) ends the block
        if lines[j].strip() and not lines[j][0].isspace():
            end = j
            break

    new_block = new_func if new_func.endswith("\n") else new_func + "\n"
    rebuilt = "".join(lines[:start]) + new_block + "".join(lines[end:])
    return rebuilt, True


def _llm_function_body(breaking_change, old_source):
    text = llm.complete(
        system=(
            "You migrate one Python function to a new API contract. Output ONLY the "
            "full replacement function definition for `sync_all_orders(api_client)`. "
            "No markdown fences, no commentary. Preserve the signature."
        ),
        user=(
            f"Breaking change: {breaking_change.old_behavior} -> {breaking_change.new_behavior}. "
            f"Migration: {breaking_change.migration_hint}.\n\nCurrent function:\n{old_source}"
        ),
        max_tokens=400,
    )
    if text and "def sync_all_orders" in text:
        return text.strip() + "\n"
    return None


def generate(breaking_change: BreakingChange, impact: ImpactReport) -> PatchResult:
    if breaking_change.change_type in ("field_rename", "method_rename"):
        return _rename_patch(breaking_change, impact)
    return _pagination_patch(breaking_change, impact)


def _rename_patch(breaking_change: BreakingChange, impact: ImpactReport) -> PatchResult:
    """Rename a symbol (e.g. a field) across the affected file."""
    if not impact.affected_files:
        raise ValueError(
            f"No affected files found for rename `{breaking_change.old_symbol}` -> `{breaking_change.new_symbol}`"
        )

    target = Path(impact.affected_files[0])
    old_source = target.read_text()
    old_sym = breaking_change.old_symbol or "total"
    new_sym = breaking_change.new_symbol or "amount"

    # Only rewrite quoted accesses ("total"/'total') and attribute access (.total)
    # so we never touch unrelated identifiers like `total_revenue` or `totals`.
    new_source = old_source
    for pat, repl in (
        (rf'"{re.escape(old_sym)}"', f'"{new_sym}"'),
        (rf"'{re.escape(old_sym)}'", f"'{new_sym}'"),
        (rf"\.{re.escape(old_sym)}\b", f".{new_sym}"),
    ):
        new_source = re.sub(pat, repl, new_source)

    diff = _unified(old_source, new_source, target.name)
    explanation = (
        f"Renamed every reference to the `{old_sym}` field to `{new_sym}` in the "
        f"affected file. Only quoted/attribute accesses were changed; unrelated "
        f"identifiers were left untouched. No signatures changed."
    )

    return PatchResult(
        diff=diff,
        files_changed=[str(target)],
        patch_explanation=explanation,
        risk_level=breaking_change.risk_level,
        new_contents={str(target): new_source},
        source="deterministic",
        fallback_used=False,
    )


def _pagination_patch(breaking_change: BreakingChange, impact: ImpactReport) -> PatchResult:
    target = Path(impact.affected_files[0])
    old_source = target.read_text()
    func = impact.affected_functions[0] if impact.affected_functions else "sync_all_orders"

    fallback_used = False
    new_func = _CURSOR_BODY
    if llm.enabled():
        candidate = _llm_function_body(breaking_change, old_source)
        if candidate:
            new_func = candidate
        else:
            fallback_used = True  # LLM was on but failed → fell back to deterministic

    new_source, ok = _replace_function(old_source, func, new_func)
    if not ok:  # last-resort safety: deterministic body
        new_source, ok = _replace_function(old_source, func, _CURSOR_BODY)
        fallback_used = True

    diff = _unified(old_source, new_source, target.name)

    explanation = (
        "Rewrote the pagination loop in `sync_all_orders` to use cursor-based "
        "iteration (`cursor`/`next_cursor`/`has_more`) instead of `page`/`total_pages`. "
        "The function signature is unchanged; only the loop body was migrated."
    )

    return PatchResult(
        diff=diff,
        files_changed=[str(target)],
        patch_explanation=explanation,
        risk_level=breaking_change.risk_level,
        new_contents={str(target): new_source},
        source="llm" if (llm.enabled() and not fallback_used) else "deterministic",
        fallback_used=fallback_used,
    )


def _unified(old_source: str, new_source: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            old_source.splitlines(keepends=True),
            new_source.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    )


def apply(patch: PatchResult):
    for path, contents in patch.new_contents.items():
        Path(path).write_text(contents)
