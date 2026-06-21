"""Find the downstream code affected by a BreakingChange.

Deterministic pattern search localizes the breakage to a file + function,
with a confidence score. (Claude can explain *why* in plain English, but
the localization itself is mechanical and reproducible.)
"""

import re
from pathlib import Path

from patchahead.schemas import BreakingChange, ImpactReport

# change_type -> regex patterns that indicate old-contract usage.
_PATTERNS = {
    "pagination": [
        r"\btotal_pages\b",
        r"get_orders\(\s*page\s*=",
        r"\bpage\s*\+=\s*1\b",
        r"\bpage\s*=\s*1\b",
    ],
}


def _patterns_for(breaking_change: BreakingChange):
    if breaking_change.change_type in ("field_rename", "method_rename"):
        old_symbol = breaking_change.old_symbol or "total"
        s = re.escape(old_symbol)
        # quoted access ("total" / 'total'), attribute access (.total), get("total")
        return [
            rf'["\']{s}["\']',
            rf"\.{s}\b",
            rf'get\(\s*["\']{s}["\']',
        ]
    return _PATTERNS.get(breaking_change.change_type, [])


def _enclosing_function(lines, idx):
    for j in range(idx, -1, -1):
        m = re.match(r"\s*def\s+(\w+)\s*\(", lines[j])
        if m:
            return m.group(1)
    return "<module>"


def analyze(breaking_change: BreakingChange, app_dir: Path) -> ImpactReport:
    patterns = _patterns_for(breaking_change)
    affected_files, affected_functions, matches = [], [], []

    for py in sorted(Path(app_dir).glob("*.py")):
        text = py.read_text()
        lines = text.splitlines()
        file_hit = False
        for i, line in enumerate(lines):
            for pat in patterns:
                if re.search(pat, line):
                    file_hit = True
                    fn = _enclosing_function(lines, i)
                    if fn not in affected_functions:
                        affected_functions.append(fn)
                    matches.append(f"{py.name}:{i + 1}: {line.strip()}")
        if file_hit:
            affected_files.append(str(py))

    confidence = min(1.0, 0.4 + 0.15 * len(matches)) if matches else 0.0
    reason = (
        f"Found {len(matches)} usage(s) of the old contract "
        f"({breaking_change.change_type}). The affected code reads fields or "
        f"calls that the new upstream response no longer provides."
        if matches
        else "No affected usage found."
    )

    return ImpactReport(
        affected_files=affected_files,
        affected_functions=affected_functions,
        old_patterns_found=matches,
        reason=reason,
        confidence=round(confidence, 2),
        source="deterministic",
    )
