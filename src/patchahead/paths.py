"""Canonical paths, resolved relative to the repo root."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DEMO = ROOT / "demo"
UPSTREAM_DIR = DEMO / "upstream"
CHANGELOG_FILE = UPSTREAM_DIR / "changelog_v2.md"

DOWNSTREAM_APP_DIR = DEMO / "downstream" / "app"
ORDER_SYNC_FILE = DOWNSTREAM_APP_DIR / "order_sync.py"
BASELINE_FILE = DEMO / "baseline" / "order_sync.py"

TEST_PATH = DEMO / "downstream" / "tests" / "test_order_sync.py"

OUTPUTS = ROOT / "outputs"
PATCH_FILE = OUTPUTS / "patch.diff"
PR_SUMMARY_FILE = OUTPUTS / "pr_summary.md"
RUN_LOG_FILE = OUTPUTS / "run_log.json"
