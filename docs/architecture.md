# Architecture

## The shape of a run

```
change document ─▶ ingest ─────▶ BreakingChange
                                      │
repository ──────▶ AST index ──▶ ImpactReport
                                      │
                             handler.plan ─▶ MigrationPlan
                                      │
                   isolated workspace ─▶ PatchProposal
                                      │
                         validation ──▶ ValidationResult
                                      │
                              MigrationResult
```

Every arrow is a typed object. No stage passes a loosely-structured dict to
another stage, and every stage can decline.

## Packages

| Package | Responsibility |
|---|---|
| `patchahead.domain` | The objects that cross stage boundaries. No behavior beyond serialization and derived properties. |
| `patchahead.ingest` | Change documents → `BreakingChange`. One parser per format, registered. |
| `patchahead.analysis` | Parsing Python (`python_ast`), caching it per repository (`index`), applying range edits and diffing (`edits`). |
| `patchahead.handlers` | One class per migration family: `supports/analyze/plan/generate`. |
| `patchahead.workspace` | `Repository` (read-only) and `Workspace` (writable temp copy). |
| `patchahead.testing` | Mapping modules to tests, and running a test command into a `TestRun`. |
| `patchahead.validation` | The five gates. The only thing that may call a migration successful. |
| `patchahead.llm` | Optional proposal engine, plus the checks that make its output untrusted-by-default. |
| `patchahead.engine` | The orchestrator. The one path the CLI, the web UI, and the tests share. |
| `patchahead.reporting` | Rendering results as terminal text or a Markdown PR body. |
| `patchahead.cli` | `argparse` front end. |
| `patchahead.web` | The localhost UI and the page it serves, as package data so a wheel install carries them. |
| `patchahead.demo` | Bundled fixtures and the scenario list `patchahead demo` narrates. Staging only: a scenario picks a change document and whether tests run, and nothing else. |
| `patchahead.config` | `[tool.patchahead]` loading and validation. |
| `patchahead.observability` | Logging, redaction, timing, optional Sentry. |

## Five decisions worth explaining

### 1. Read-only and writable are different types

`Repository` is how PatchAhead sees your source tree. It has no `write` method,
so there is no call to make by mistake. `Workspace.materialize(repository)`
produces a writable copy in a temporary directory, and all patching and all test
execution happen there.

This is the structural version of the safety promise. The prototype wrote
directly to the user's files and relied on a fixture-only restore function to
undo it — which only worked for the two bundled demos.

A directory copy is used rather than `git worktree`, because a worktree captures
*committed* state. Analyzing something other than what the user is looking at
would be a surprising default.

### 2. Columns are converted from bytes to characters at one boundary

`ast` reports `col_offset` as an offset into the line's **UTF-8 encoding**;
Python strings are indexed by **character**. They agree only while a line is
pure ASCII. Slicing source with a raw `col_offset` therefore cuts at the wrong
place on any line containing non-ASCII text — a line starting
`name = "José"` produced `order[""amount"`, which is not even valid Python.

The conversion happens in `analysis/python_ast.py`, at the single point where
`ast` data enters the system: every `SourceRange` the module emits is already
character-based, so no consumer downstream has to know `ast` counts differently.
Helpers that compute a column arithmetically (`_attribute_name_range`,
`_keyword_name_range`) do the arithmetic in byte space and convert only the
result, because that is the space `end_col_offset` and `str.encode()` share.

A handler that builds a range from a raw `ast` node must pass
`ModuleAnalysis.columns` to `SourceRange.of`. There is one such handler
(pagination), and the adversarial eval covers it.

### 3. Patches are range edits, not regenerated files

Handlers emit `TextEdit(line, col, end_line, end_col, new_text)` derived from
`ast` node positions. `apply_edits` applies them to the original text from the
end backwards.

The alternative — `ast.unparse` — discards every comment, blank line, and
formatting choice in the file, turning a two-token rename into a whole-file
rewrite that no reviewer would approve. A keyword-argument rename here is
literally a one-token diff.

Overlapping edits are rejected rather than merged: two handlers disagreeing
about the same range is a bug, and a plausible-looking merge would hide it.

### 4. Confidence is graded, and grading decides what gets patched

Every `ImpactFinding` carries `confidence` (high/medium/low), a `reason` in
plain English, and `patchable`. The `min_confidence` setting is the threshold
for acting; everything below it is *reported* so a human can look.

Three buckets, not a float, because a float implies a calibration PatchAhead
does not have. Three buckets can be explained to a reviewer and asserted against
fixtures.

Grading is what makes name-matching safe enough to act on. PatchAhead does no
type inference; `receiver_name` yields a *name*, not a type.

The decisive rule is the receiver check. When a change document **asserts** an
owner, a site whose receiver is not that owner is graded LOW and never patched
— `customer["total"]` survives an `order.total` rename, and so does `o["total"]`
in `for o in orders`. That second case is a false negative PatchAhead accepts on
purpose: proving `o` is an `order` needs type inference, and a missed site is
recoverable in a way a wrong edit is not.

An owner *inferred* from an illustrative snippet is treated differently — it
raises confidence on a match and lowers it on a mismatch, but never vetoes. See
`SymbolTarget.owner_is_explicit` and `docs/migrations.md`.

### 5. Handlers are a registry, not a branch

`find_handler(change)` returns the first registered handler that supports the
change. Nothing outside `patchahead.handlers` has an `if change.kind == ...`
branch. `selftest_registry()` asserts the invariants — every actionable kind has
exactly one handler, every handler is identifiable — and the test suite runs it,
so a half-added family fails CI rather than shipping as a kind nothing can
migrate.

### 6. Changes from one document are patched together and validated once

A release note usually describes several breaking changes, and they are
frequently interdependent: renaming `fetch_orders` to `list_orders` leaves the
call broken until the `timeout_seconds` rename lands too. Validating after each
individual patch would fail both of them for the absence of the other.

So `migrate` runs in two phases: patch every change into one workspace (each
re-analyzed against the workspace as it stands, so line numbers stay correct),
then validate the combined result once. That combined state is what a reviewer
would actually merge.

## The validation gates

| # | Gate | Fails when |
|---|---|---|
| 1 | `syntax` | A modified file no longer parses. |
| 2 | `scope` | Files outside the plan changed, or the change exceeds the configured limits. |
| 3 | `targeted_tests` | The tests mapped to the changed modules fail. |
| 4 | `regression_tests` | The patch broke a test that passed before it. |
| 5 | `migration_assertion` | The targeted tests did not go from failing to passing. |

Order matters. Gates 1 and 2 are near-instant and decisive, and they run *before*
anything executes repository code — so a patch that produced invalid Python, or
touched forty unrelated files, never reaches the stage that runs a test command.

Two subtleties worth knowing:

**Gate 4 is baseline-relative.** "Regression" means *newly* failing. A repository
broken by three upstream changes must still be able to migrate the first one, so
a test that was already red stays red without failing the gate. Only tests this
patch turned from passing to failing count.

**Gate 5 needs evidence on both sides, and it defines success.** A patch can
leave a green suite green without having fixed anything.
`ValidationResult.verified` — which `MigrationResult.succeeded` is defined in
terms of — requires this gate to have *passed*, meaning a test that failed
before the patch passes after it:

| Before -> after | Gate 5 | Outcome |
|---|---|---|
| red -> green | PASSED | `migrated` |
| green -> green | SKIPPED | `patched_unverified` |
| no runnable tests | SKIPPED | `patched_unverified` |
| a test this patch broke | gate 4 fails first | `validation_failed` |

The gate prefers the targeted run's evidence and falls back to the full suite,
because a test command that cannot be narrowed to specific files still produces
perfectly good red-to-green proof — it is just spread across the whole run.

## Where the LLM sits

Nowhere on the default path. `--use-llm` is consulted in exactly one place:
`engine._llm_or_blocked`, reached only when a deterministic handler returned a
plan with `blocked_reason` set.

The model receives the breaking change, the failing test output, and the source
of the functions the findings point at. It returns JSON. That JSON is checked
against the impact report, parsed as Python, and compared against a
`FunctionContract` extracted from the original — `async`-ness, name, every
parameter with its kind and annotation, defaults, return annotation, and
decorators. Anything but a body rewrite is discarded in full; the model is not
asked to try again. Whatever survives goes through the same five gates.

The contract is enforced structurally rather than requested in the prompt,
because a prompt is a request and a comparison is a guarantee.

## Performance

Analysis parses each file once per run and shares the index across handlers. On
the bundled example (5 modules) analysis is ~4ms; on PatchAhead's own `src/`
(~25 modules) it is single-digit milliseconds. Every result carries a `timings`
dict, and `-v` prints it, so any performance claim can be checked rather than
believed. The dominant cost of `migrate` is the test subprocess, not analysis.

`max_workspace_files` (default 20000) refuses to copy a repository larger than
that rather than hanging on it.

## Extending

Adding a migration family touches two files: the new handler module and the
registry import. See [contributing.md](contributing.md).
