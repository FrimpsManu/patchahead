"""Scenario registry.

Each scenario is one upstream breaking change that flows through the exact
same agent pipeline. Adding a new change type is data, not new control flow:
point at a changelog, a baseline (broken-on-v2) downstream file, the live
file to patch, and a test.
"""

from dataclasses import dataclass
from pathlib import Path

from patchahead import paths


@dataclass(frozen=True)
class Scenario:
    id: str
    label: str
    changelog_file: Path
    baseline_file: Path
    target_file: Path
    test_path: Path


_TESTS = paths.DEMO / "downstream" / "tests"
_BASE = paths.DEMO / "baseline"
_APP = paths.DOWNSTREAM_APP_DIR
_UP = paths.UPSTREAM_DIR

PAGINATION = Scenario(
    id="pagination",
    label="Pagination → cursor",
    changelog_file=_UP / "changelog_v2.md",
    baseline_file=_BASE / "order_sync.py",
    target_file=_APP / "order_sync.py",
    test_path=_TESTS / "test_order_sync.py",
)

FIELD_RENAME = Scenario(
    id="field_rename",
    label="Field rename: total → amount",
    changelog_file=_UP / "changelog_field.md",
    baseline_file=_BASE / "order_report.py",
    target_file=_APP / "order_report.py",
    test_path=_TESTS / "test_order_report.py",
)

SCENARIOS = {s.id: s for s in (PAGINATION, FIELD_RENAME)}
DEFAULT = "pagination"


def get(scenario_id: str) -> Scenario:
    return SCENARIOS.get(scenario_id or DEFAULT, PAGINATION)
