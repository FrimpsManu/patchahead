"""The PatchAhead evaluation harness: datasets, metrics, results, reporting.

Split out of the single script the suites used to live in, for one reason: the
harness is now something the suites can be *wrong about*, so it needs its own
tests. ``tests/test_eval_harness.py`` covers it, including the property that
matters most -- that the benchmark is capable of failing.
"""

from evals.harness.dataset import (
    CaseSpec,
    ClassificationCase,
    DatasetError,
    MigrationCase,
    SiteCase,
    ValidationCase,
    describe,
    load,
)
from evals.harness.metrics import Calibration, ConfusionMatrix, Distribution, Tally
from evals.harness.repo import changed_line_count, temporary_repo, touched_text
from evals.harness.result import BenchmarkResult, CaseResult, CaseStatus, SuiteResult

__all__ = [
    "BenchmarkResult",
    "Calibration",
    "CaseResult",
    "CaseSpec",
    "CaseStatus",
    "ClassificationCase",
    "ConfusionMatrix",
    "DatasetError",
    "Distribution",
    "MigrationCase",
    "SiteCase",
    "SuiteResult",
    "Tally",
    "ValidationCase",
    "changed_line_count",
    "describe",
    "load",
    "temporary_repo",
    "touched_text",
]
