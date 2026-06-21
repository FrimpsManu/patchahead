#!/usr/bin/env python3
"""Tiny eval harness for the changelog classifier.

    python evals/run_evals.py

Asserts PatchAhead classifies each fixture's breaking-change type correctly.
This is the "we evaluate our agent, not just vibe-check it" surface.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from patchahead import changelog_parser  # noqa: E402

FIXTURES = ROOT / "evals" / "fixtures"


def main():
    cases = [json.loads(p.read_text()) for p in sorted(FIXTURES.glob("*.json"))]
    passed = 0
    print(f"Running {len(cases)} classification evals\n")
    for case in cases:
        got = changelog_parser.classify(case["changelog"])
        ok = got == case["expected_change_type"]
        passed += ok
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {case['name']:<20} expected={case['expected_change_type']:<16} got={got}")
    print(f"\n{passed}/{len(cases)} passed")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
