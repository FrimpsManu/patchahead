#!/usr/bin/env python3
"""PatchAhead — golden demo.

    python run_demo.py

Shows the full story end-to-end:
  1. Downstream tests pass against upstream v1.
  2. Upstream ships v2 (cursor pagination); the same tests now fail.
  3. PatchAhead parses the change, localizes impact, applies a minimal patch.
  4. Tests pass again — proof the migration works.
  5. A human-reviewable PR summary + Sentry-style trace are produced.
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from patchahead import agent, observability as obs, test_runner  # noqa: E402

NOCOLOR = bool(os.environ.get("NO_COLOR"))


def c(text, code):
    return text if NOCOLOR else f"\033[{code}m{text}\033[0m"


def bold(t):
    return c(t, "1")


def green(t):
    return c(t, "92")


def red(t):
    return c(t, "91")


def cyan(t):
    return c(t, "96")


def yellow(t):
    return c(t, "93")


def header(n, title):
    print()
    print(bold(cyan(f"━━━ {n}. {title} ")) + cyan("━" * max(0, 50 - len(title))))


def banner():
    print()
    print(bold("  ┌" + "─" * 56 + "┐"))
    print(bold("  │  PatchAhead") + "  fix breaking API changes before they" + bold("   │"))
    print(bold("  │") + "             break production." + " " * 27 + bold("│"))
    print(bold("  └" + "─" * 56 + "┘"))


def main():
    banner()

    # ---- 1. Baseline: original code works against upstream v1 --------------
    agent.reset_demo()
    header(1, "Downstream tests pass against upstream v1")
    v1 = test_runner.run_tests(api_version="v1")
    print("    " + (green("PASS") if v1.passed else red("FAIL")) + f"  {v1.summary}")
    print(f"    {c('orders synced via page/total_pages — all good on v1', '90')}")

    # ---- 2..5 run the agent against v2 -------------------------------------
    agent.reset_demo()
    print()
    print("    " + c("running PatchAhead agent (instrumented)…", "90"))
    report = agent.run()
    bc, impact, patch = report.breaking_change, report.impact, report.patch

    header(2, "Upstream ships v2 — the same tests now fail")
    print("    " + red("FAIL") + f"  {report.tests_before.summary}")
    for t in report.tests_before.failing_tests[:3]:
        print(f"    {red('✗')} {t}")
    print(f"    {c('downstream still reads page/total_pages, which v2 removed', '90')}")

    header(3, "PatchAhead analyzes the breaking change")
    print(f"    change_type : {yellow(bc.change_type)}    risk : {yellow(bc.risk_level)}")
    print(f"    before      : {bc.old_behavior}")
    print(f"    after       : {bc.new_behavior}")
    print(f"    affected    : {green(', '.join(impact.affected_functions) or 'none')} "
          f"in {os.path.relpath(impact.affected_files[0], ROOT)}")
    print(f"    confidence  : {impact.confidence}")
    if report.similar_migrations:
        print(f"    seen before : {report.similar_migrations[0]['title']}")

    header(4, "PatchAhead applies the migration patch")
    engine = "Claude" if patch.source == "llm" else "deterministic"
    print(f"    engine      : {engine}")
    for line in patch.diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            print("    " + green(line))
        elif line.startswith("-") and not line.startswith("---"):
            print("    " + red(line))
        elif line.startswith("@@"):
            print("    " + cyan(line))
        else:
            print("    " + c(line, "90"))

    header(5, "Tests pass — migration verified")
    print("    " + (green("PASS") if report.tests_after.passed else red("FAIL"))
          + f"  {report.tests_after.summary}")

    header(6, "Human-reviewable PR summary + trace")
    print(f"    PR summary  : {os.path.relpath(report.pr_summary_path, ROOT)}")
    print(f"    patch diff  : {os.path.relpath(report.patch_path, ROOT)}")
    print(f"    run log     : {os.path.relpath(ROOT + '/outputs/run_log.json', ROOT)}")
    print(f"    trace       : {obs.backend()} backend  "
          f"({'Sentry transaction sent' if obs.ENABLED else 'set SENTRY_DSN to ship spans to Sentry'})")
    print(f"    auto-merge  : {red('disabled')} — PatchAhead proposes, a human approves.")

    print()
    verdict = green("✔ DEMO PASSED") if report.succeeded else red("✘ DEMO DID NOT REACH GREEN")
    print(bold(verdict) + c("   (failed on v2 → patched → passed on v2)", "90"))
    print()
    return 0 if report.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
