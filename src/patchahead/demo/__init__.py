"""The bundled demo: fixtures, and the scenarios that narrate them.

``patchahead demo`` exists so that someone who has never seen this project can
run one command and watch a real migration happen. Everything here is staging --
*which* repository, *which* change document, and a sentence about what to look
at. None of it is migration logic: the demo calls
:func:`patchahead.engine.migrate` exactly as the CLI does, against a real
directory, running real tests. A demo with its own code path proves nothing
about the product, which is the mistake the prototype made.

Two things make the staging honest rather than decorative:

* every scenario declares the outcome it expects, and
  ``tests/test_demo.py`` runs the real engine and asserts reality matches. A
  scenario whose story stops being true fails the build.
* the scenario list deliberately includes migrations that do **not** succeed.
  A demo composed only of green checkmarks would be advertising the opposite of
  what this tool is for: the interesting claim is not "it rewrites code", it is
  "it knows when not to, and it will not call a patch verified without
  evidence".

The fixtures live inside the package (``patchahead/demo/fixtures/``) rather than
in a top-level ``examples/`` directory, because ``patchahead demo`` has to work
from ``pip install 'patchahead[demo]'`` in an empty directory, not only from a
git checkout.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DemoError",
    "Expectation",
    "Scenario",
    "SCENARIOS",
    "changes_root",
    "find",
    "fixtures_root",
    "repo_root",
    "scenarios",
]


class DemoError(Exception):
    """The bundled demo data is missing or unreadable."""


class Expectation(str, enum.Enum):
    """What a scenario is expected to demonstrate.

    These are the four states a migration run can end in, and telling them apart
    is the whole point of the demo. ``VERIFIED`` is the only one that means "this
    worked"; the other three are the ones a tool that overclaims would blur
    together.
    """

    #: Tests failed before the patch and pass after it. The one honest success.
    VERIFIED = "verified"
    #: A patch was produced and nothing verified it. Not a success.
    UNVERIFIED = "patched_unverified"
    #: Impact was found and PatchAhead declined to rewrite it, with a reason.
    REFUSED = "refused"
    #: A patch was produced and the tests rejected it.
    FAILED = "validation_failed"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class Scenario:
    """One bundled story: a change document, and what it demonstrates."""

    #: URL-safe identifier, used by ``--scenario`` and by the web UI.
    id: str
    #: Short label for the picker.
    title: str
    #: Migration family, shown as a chip. Free text, not a ``ChangeKind``: one
    #: scenario deliberately carries two families in a single document.
    family: str
    #: Filename inside ``fixtures/changes``.
    document: str
    expect: Expectation
    #: One sentence: what this scenario is for.
    headline: str
    #: One sentence: where to look on screen once it finishes.
    watch_for: str
    #: ``False`` runs the migration with test execution disabled, which is the
    #: only way to reach ``patched_unverified`` on this repository -- its suite
    #: is red before the patch, so every other path produces evidence one way or
    #: the other.
    run_tests: bool = True

    @property
    def change_path(self) -> Path:
        path = changes_root() / self.document
        if not path.is_file():
            raise DemoError(f"bundled change document is missing: {path}")
        return path

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "family": self.family,
            "document": self.document,
            "expect": self.expect.value,
            "headline": self.headline,
            "watch_for": self.watch_for,
            "run_tests": self.run_tests,
        }


#: Ordered deliberately: the clean red-to-green migration first, because it is
#: the claim everything else qualifies, then the two harder successes, then the
#: three ways a run can end without one.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="field-rename",
        title="A renamed field",
        family="field_rename",
        document="field-rename.md",
        expect=Expectation.VERIFIED,
        headline=(
            "The upstream `order` object renamed `total` to `amount`. Two reads break; "
            "a display string that merely contains the word does not."
        ),
        watch_for=(
            '`TOTAL_LABEL = "total"` is untouched in the diff. It is a string, not a '
            "field access, so it was never a candidate."
        ),
    ),
    Scenario(
        id="pagination",
        title="Page numbers to cursors",
        family="pagination_page_to_cursor",
        document="pagination-cursor.md",
        expect=Expectation.VERIFIED,
        headline=(
            "The endpoint dropped `page`/`total_pages` for `cursor`/`next_cursor`/"
            "`has_more`. This is a loop rewrite, not a rename."
        ),
        watch_for=(
            "The function keeps its name, signature, docstring and accumulator. Four "
            "spans inside one loop change; nothing else does."
        ),
    ),
    Scenario(
        id="sdk-v2",
        title="Two changes, one call site",
        family="method_rename + kwarg_rename",
        document="sdk-v2.md",
        expect=Expectation.VERIFIED,
        headline=(
            "One release note, two breaking changes, both landing on the same line. "
            "Neither works alone -- they have to be applied together."
        ),
        watch_for=(
            "Two plans, one workspace, one validation run. Run the `method-rename` "
            "scenario to see what half of this migration does."
        ),
    ),
    Scenario(
        id="receiver-mismatch",
        title="Same field name, different object",
        family="field_rename",
        document="invoice-field-rename.md",
        expect=Expectation.REFUSED,
        headline=(
            "A change document about `invoice` objects, run against code that only "
            "touches `order` objects. The field name matches. Nothing else does."
        ),
        watch_for=(
            "Both sites are found, explained and graded low -- then left alone. This "
            "is the scenario that matters: a tool that rewrote them would be wrong."
        ),
    ),
    Scenario(
        id="method-rename",
        title="Half a migration",
        family="method_rename",
        document="method-rename.md",
        expect=Expectation.FAILED,
        headline=(
            "Only the method rename, without the keyword-argument rename that shipped "
            "with it. The patch is correct and the result still does not work."
        ),
        watch_for=(
            "`targeted_tests` fails, so the run is rejected. PatchAhead does not "
            "report a migration it cannot stand behind."
        ),
    ),
    Scenario(
        id="no-evidence",
        title="The same patch, with the tests switched off",
        family="field_rename",
        document="field-rename.md",
        expect=Expectation.UNVERIFIED,
        run_tests=False,
        headline=(
            "Byte-for-byte the diff from the first scenario, with test execution "
            "disabled -- the shape of any repository whose tests do not cover a change."
        ),
        watch_for=(
            "Identical diff, different verdict: PATCHED, NOT VERIFIED. Without a test "
            "that failed before and passes after, there is nothing to verify it."
        ),
    ),
)


def fixtures_root() -> Path:
    """Directory holding the bundled demo data.

    Package data, so it is present in a wheel install and in an editable
    checkout alike.
    """
    root = Path(__file__).resolve().parent / "fixtures"
    if not root.is_dir():
        raise DemoError(
            f"the bundled demo fixtures are missing from the installed package "
            f"(expected {root}). Reinstall patchahead, or run from a git checkout."
        )
    return root


def repo_root() -> Path:
    """The bundled example repository. Read-only as far as the engine is concerned."""
    repo = fixtures_root() / "orders-service"
    if not repo.is_dir():
        raise DemoError(f"the bundled example repository is missing: {repo}")
    return repo


def changes_root() -> Path:
    changes = fixtures_root() / "changes"
    if not changes.is_dir():
        raise DemoError(f"the bundled change documents are missing: {changes}")
    return changes


def scenarios() -> tuple[Scenario, ...]:
    """Every bundled scenario, in presentation order."""
    return SCENARIOS


def find(scenario_id: str) -> Scenario:
    for scenario in SCENARIOS:
        if scenario.id == scenario_id:
            return scenario
    known = ", ".join(scenario.id for scenario in SCENARIOS)
    raise DemoError(f"no such demo scenario: {scenario_id!r}. Available: {known}")
