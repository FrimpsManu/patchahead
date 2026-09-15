# Contributing

## Setup

```bash
git clone https://github.com/FrimpsManu/patchahead
cd patchahead
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

python -m pytest                  # the suite (~15s; spawns real pytest subprocesses)
python -m pytest -m "not slow"    # ~2s, no subprocesses
python evals/run.py               # the evaluation benchmark (see docs/evaluation.md)
ruff check src tests
```

Everything must be green before a pull request: tests, evals, and ruff.

---

## Adding a migration family

This is the main way to extend PatchAhead, and it touches **two files**: your new
handler, and one import line. The core engine does not change.

The worked example below is a real family PatchAhead does not ship: renaming an
**import**, `from vendor import OldClient` → `from vendor import NewClient`.

### Step 0 — decide whether it belongs

A family ships only when it has all six of:

- change-document parsing (the classifier recognizes it)
- AST impact analysis (finds sites, grades confidence)
- planning (decides what to rewrite, and states what it won't)
- patch generation (range edits)
- validation (goes through the existing gates)
- tests and documentation

If you can't do the impact analysis without type inference, or the transform
can't be expressed as a range edit, it is probably not a v1 family. Say so in an
issue before writing code; "PatchAhead refuses this" is a legitimate outcome.

### Step 1 — add the change kind

`src/patchahead/domain/change.py`:

```python
class ChangeKind(str, enum.Enum):
    FIELD_RENAME = "field_rename"
    ...
    IMPORT_RENAME = "import_rename"     # new
```

`selftest_registry()` now fails, and so does the test suite — every actionable
kind must have a handler. That is the point: a half-added family cannot ship.

### Step 2 — teach the classifier

`src/patchahead/ingest/markdown.py`, in `_SIGNALS`:

```python
ChangeKind.IMPORT_RENAME: [
    (r"class (?:was )?renamed", 5),
    (r"\bimport\b.{0,40}\brenamed\b", 4),
    (r"exported name", 3),
],
```

Weights are relative: a phrase that names the construct outranks one that merely
suggests it. Add a case to `evals/datasets/classification/cases.json` — including
one that must *not* match, so you can see you haven't stolen signal from another
family.

### Step 3 — write the handler

`src/patchahead/handlers/import_rename.py`:

```python
"""Import rename: ``from vendor import OldClient`` -> ``NewClient``."""

from patchahead.analysis.index import RepoIndex
from patchahead.config import Config
from patchahead.domain.change import BreakingChange, ChangeKind, Confidence
from patchahead.domain.impact import AccessKind, CodeReference, ImpactFinding, ImpactReport
from patchahead.domain.plan import MigrationPlan, Risk, TextEdit, Transformation
from patchahead.handlers.base import MigrationHandler, register


class ImportRenameHandler(MigrationHandler):
    name = "import_rename"
    kinds = (ChangeKind.IMPORT_RENAME,)
    summary = "Rename an imported name: from pkg import Old -> New"
    limitations = (
        "Only `from pkg import Name`. A module-level `import pkg` followed by "
        "`pkg.Name` is reported but not rewritten.",
        "Does not rewrite a dynamic import.",
    )

    def supports(self, change: BreakingChange) -> bool:
        return change.kind in self.kinds and change.target.is_rename

    def analyze(self, change, index: RepoIndex, config: Config) -> ImpactReport:
        findings = []
        for path in index.non_test_paths():
            module = index.modules[path]
            for node in ast.walk(module.tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                for alias in node.names:
                    if alias.name != change.target.symbol:
                        continue
                    findings.append(
                        ImpactFinding(
                            reference=CodeReference(
                                path=path,
                                line=node.lineno,
                                col=..., end_line=..., end_col=...,
                                snippet=module.line_text(node.lineno),
                            ),
                            symbol="<module>",
                            matched_contract=f"from {node.module} import {alias.name}",
                            access=AccessKind.CALL,
                            reason=f"imports `{alias.name}` from `{node.module}`",
                            confidence=(
                                Confidence.HIGH
                                if node.module == change.target.owner
                                else Confidence.MEDIUM
                            ),
                            source_text=alias.name,
                        )
                    )
        return ImpactReport(
            change=change,
            findings=findings,
            related_tests=discovery.tests_for_paths(index, [f.path for f in findings]),
            files_scanned=index.file_count,
            skipped_files=dict(index.skipped),
        )

    def plan(self, change, report, index, config) -> MigrationPlan:
        plan = MigrationPlan(
            change=change,
            handler=self.name,
            expected_tests=list(report.related_tests),
            risk=Risk.LOW,
            rationale=f"Rename the imported name `{change.target.symbol}`.",
        )
        for finding in report.findings:
            if not finding.patchable or finding.confidence < config.min_confidence:
                plan.skipped.append(f"{finding.reference}: {finding.reason}")
                continue
            reference = finding.reference
            plan.transformations.append(
                Transformation(
                    reference=reference,
                    old=change.target.symbol,
                    new=change.target.replacement,
                    symbol=finding.symbol,
                    confidence=finding.confidence,
                    edit=TextEdit(
                        reference.line, reference.col,
                        reference.end_line, reference.end_col,
                        change.target.replacement,
                        description="rename the imported name",
                    ),
                )
            )
        if not plan.transformations:
            plan.blocked_reason = f"no imports of `{change.target.symbol}` were found"
        return plan


register(ImportRenameHandler())
```

You do **not** write `generate()`. The base class applies your plan's range
edits and produces the diff, which is correct for any family that can express
itself as range edits.

### Step 4 — register it

`src/patchahead/handlers/__init__.py`:

```python
from patchahead.handlers import import_rename as import_rename  # noqa: E402,F401
```

That is the last core change. `find_handler` picks it up; the CLI lists it; the
engine, validation, reporting, and the web UI all work unchanged.

### Step 5 — test it

`tests/test_handlers.py`. Cover, at minimum:

- the happy path, asserting the **patched source**, not just that it changed
- one **false positive that must not be patched** — this is the test that
  matters most
- a shape you refuse, asserting `blocked_reason` says why
- a no-impact repository
- confidence grading at each level you produce

Then an end-to-end case in `tests/test_engine_e2e.py` and eval cases in
`evals/datasets/migrations/cases.json` — at least one that must migrate and one
that must be refused. [docs/evaluation.md](evaluation.md) describes the suites,
the strict dataset schema, and what to do when your case fails because
PatchAhead is genuinely not there yet: mark it `known_gap` with a reason rather
than deleting it or weakening the expectation.

### Step 6 — document it

A section in `docs/migrations.md` with the same structure as the others:
recognized constructs, confidence table, and — the part people actually need —
**does not**.

---

## Adding a change-document format

Subclass `ChangeParser`, implement `supports` and `parse`, call `register()`,
import the module in `patchahead/ingest/__init__.py`. OpenAPI spec diffs would
slot in exactly here, producing `BreakingChange` objects at
`confidence: HIGH` because a spec diff is not a guess.

---

## Conventions

**Comments explain why, not what.** `# increment the counter` above `i += 1`
is noise. A comment stating why the pagination handler refuses a loop whose page
variable is logged is load-bearing.

**Refuse rather than guess.** Every "we can't handle this" path must set a
reason a user can read and act on. `blocked_reason`, `unpatchable_reason`, and
`unsupported_reason` all exist for this.

**No silent failures.** No `except Exception: pass`. If an optional integration
degrades, log why at debug level and record it. The prototype's
`except Exception: return None` made a bad API key indistinguishable from "the
LLM is off", and fixing that is half of what this rewrite was.

**Types at boundaries.** Anything crossing a stage boundary is a domain object.
No dicts.

**Tests use real repositories.** The `make_repo` fixture writes files to a temp
directory and the real engine runs over them. The Anthropic API is the one
*external service* that is mocked, because it is remote, paid, and
non-deterministic — everything else the engine does, including subprocess test
runs and wheel builds, happens for real.

Substituting a function you own is a narrower thing and is fine where the
behaviour under test is the wiring rather than the effect: `tests/test_demo.py`
replaces the server start, the port probe and the browser launch, because the
question there is "was it called with the right arguments", not "does uvicorn
bind a socket". Never substitute a piece of the migration pipeline — analysis,
planning, patching or a gate — to make a test easier.

**Assert on outcomes, not on implementation.** `assert 'o["amount"]' in patched`
is a good test. `assert handler._grade_subscript(...) == ...` is not.

---

## Pull requests

- One change per PR.
- Tests, evals, and ruff green.
- If you changed behavior, say so in `CHANGELOG.md` under `[Unreleased]`.
- If you added a family, update `docs/migrations.md`.
- Never lower an eval threshold to make a build pass. If a change genuinely
  trades accuracy for something else, say so explicitly in the PR and make the
  case.
- Never delete an eval case to make a build pass, and never weaken one into
  asserting less. Deleting a case fails nothing, which is exactly why it is the
  dangerous option. If PatchAhead cannot yet do what the case asks, mark it
  `known_gap` with a reason: the case keeps running, the miss stays visible, and
  the build goes red again the day it starts passing.
