#!/usr/bin/env python3
"""The PatchAhead evaluation benchmark.

    python evals/run.py                      # every suite
    python evals/run.py classification       # one suite
    python evals/run.py --format json        # machine-readable
    python evals/run.py --format markdown    # a report you can paste
    python evals/run.py --out report.md --format markdown

Five suites, each measuring something that fails independently:

``classification``
    Does it read a change document correctly -- kind, symbols, owner, whether
    ownership was asserted or merely illustrated -- and does it *refuse* the
    documents it should? Includes confidence calibration.

``impact``
    Given a change and a repository, which sites does it find, and which does it
    rewrite? Precision, recall and F1 against hand-labelled ground truth.

``adversarial``
    The same measurement on a dataset written to fool it.

``migrations``
    Whole engine runs against real repositories, sorted into a five-way outcome
    taxonomy rather than a pass rate, plus patch size.

``validation``
    Do the five gates reach the right verdict -- including the two that only a
    misbehaving patch generator can trigger?

Exit status is 0 only when every suite is green. A recorded *known gap* -- a
case PatchAhead is expected to fail, with a stated reason -- does not fail the
run, but a known gap that has started passing does: the marker is stale and
stale markers are how a benchmark starts lying.

Every number printed comes from a run that just happened. Nothing is cached,
recorded from a previous version, or written down by hand.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from evals.harness.dataset import DatasetError  # noqa: E402
from evals.harness.report import RENDERERS  # noqa: E402
from evals.harness.result import BenchmarkResult  # noqa: E402
from evals.suites import SUITES  # noqa: E402

# Kept as module-level names because they are the benchmark's public surface:
# the CI test module and any future tooling call these directly rather than
# reaching into `evals.suites`.
run_classification = SUITES["classification"]
run_impact = SUITES["impact"]
run_adversarial = SUITES["adversarial"]
run_migrations = SUITES["migrations"]
run_validation = SUITES["validation"]


def run_benchmark(names: list[str] | None = None) -> BenchmarkResult:
    """Run the named suites, or all of them, in registry order."""
    selected = names or list(SUITES)
    return BenchmarkResult(suites=[SUITES[name]() for name in selected])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals/run.py", description="Run the PatchAhead evaluation benchmark."
    )
    # `choices` is deliberately not used here: with `nargs="*"`, argparse
    # validates the empty default against it and rejects "run everything".
    parser.add_argument(
        "suites",
        nargs="*",
        metavar="SUITE",
        help=f"suites to run, any of: {', '.join(SUITES)} (default: all)",
    )
    parser.add_argument(
        "--format",
        default="text",
        choices=sorted(RENDERERS),
        help="output format (default: text)",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help="write the report to this file as well as stdout",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="shorthand for --format json",
    )
    args = parser.parse_args(argv)

    unknown = [name for name in args.suites if name not in SUITES]
    if unknown:
        parser.error(f"unknown suite(s): {', '.join(unknown)}. Choose from: {', '.join(SUITES)}")

    try:
        result = run_benchmark(args.suites)
    except DatasetError as error:
        # A malformed dataset is not a low score, it is a broken measurement.
        # Reporting it as a score would be the worst of both worlds.
        print(f"dataset error: {error}", file=sys.stderr)
        return 2

    rendered = RENDERERS["json" if args.as_json else args.format](result)
    print(rendered)
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
