"""The evaluation suites, and the registry the runner selects from.

Registration is a dict rather than an ``if/elif`` in the runner for the same
reason the migration handlers are a registry: adding a suite should touch the
suite's own module and one line here, and forgetting to wire one up should be
impossible rather than merely unlikely.
"""

from __future__ import annotations

from collections.abc import Callable

from evals.harness.result import SuiteResult
from evals.suites import classification, migrations, sites, validation

#: Ordered cheapest-first, so a dataset mistake or a classification regression
#: surfaces in milliseconds instead of after the suites that spawn subprocesses.
SUITES: dict[str, Callable[[], SuiteResult]] = {
    "classification": classification.run,
    "impact": sites.run_impact,
    "adversarial": sites.run_adversarial,
    "migrations": migrations.run,
    "validation": validation.run,
}

__all__ = ["SUITES", "classification", "migrations", "sites", "validation"]
