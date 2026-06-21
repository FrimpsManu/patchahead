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

from patchahead import agent, observability as obs, scenarios, test_runner  # noqa: E402

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


def run_one(scenario_id):
    sc = scenarios.get(scenario_id)
    print()
    print(bold(cyan(f"  ▶ scenario: {sc.label}  ")) + c(f"[{sc.id}]", "90"))

    # ---- 1. Baseline: original code works against upstream v1 --------------
    agent.reset_demo(sc.id)
    header(1, "Downstream tests pass against upstream v1")
    v1 = test_runner.run_tests(api_version="v1", test_path=sc.test_path)
    print("    " + (green("PASS") if v1.passed else red("FAIL")) + f"  {v1.summary}")
    print(f"    {c('original code works against the v1 contract', '90')}")

    # ---- 2..5 run the agent against v2 -------------------------------------
    agent.reset_demo(sc.id)
    print()
    print("    " + c("running PatchAhead agent (instrumented)…", "90"))
    report = agent.run(sc.id)
    bc, impact, patch = report.breaking_change, report.impact, report.patch

    header(2, "Upstream ships v2 — the same tests now fail")
    print("    " + red("FAIL") + f"  {report.tests_before.summary}")
    for t in report.tests_before.failing_tests[:3]:
        print(f"    {red('✗')} {t}")
    print(f"    {c('downstream still uses the v1 contract, which v2 changed', '90')}")

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
    agent.reset_demo(sc.id)
    return 0 if report.succeeded else 1


def main():
    banner()
    arg = sys.argv[1] if len(sys.argv) > 1 else scenarios.DEFAULT
    if arg == "all":
        ids = list(scenarios.SCENARIOS.keys())
    elif arg in scenarios.SCENARIOS:
        ids = [arg]
    else:
        print(red(f"unknown scenario '{arg}'. options: ") + ", ".join(scenarios.SCENARIOS) + ", all")
        return 2
    code = 0
    for sid in ids:
        code |= run_one(sid)
    print()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
