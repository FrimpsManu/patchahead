# Changelog

All notable changes to PatchAhead are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`patchahead demo`.** One command, no configuration: it serves the local UI
  against a bundled, deliberately-broken example service with six scenarios.
  Three end in a verified migration; the other three do not, on purpose — one is
  refused, one is rejected by the tests, and one is patched with the tests
  switched off. It is the real engine throughout: a scenario supplies a change
  document and whether tests execute, both ordinary engine inputs, and
  `tests/test_web.py` asserts that running through a scenario and calling
  `engine.migrate` directly produce the same diff and the same verdict.
- **`patchahead web`**, the same UI pointed at a repository of your own,
  replacing `python web/server.py`.
- **A rebuilt UI** that tells the pipeline as six numbered steps — upstream
  change, impact, plan, patch, the five gates, outcome — with an unmistakable
  `VERIFIED MIGRATION` / `PATCHED, NOT VERIFIED` / `REFUSED` / `REJECTED BY THE
  TESTS` verdict at the top, and the release note shown beside what PatchAhead
  made of it.
- **A `demo` extra** (`pip install 'patchahead[demo]'`): the web UI plus pytest,
  because a migration is only verified when tests actually run, and without a
  runner every scenario reports — honestly but uselessly — that the test command
  could not start. `patchahead demo` says so on startup if pytest is missing.
- **`docs/demo-recording.md`**, a 45-second recording sequence, and
  `docs/media/`, holding real screenshots of the running UI.

### Changed

- **The bundled example moved into the package**, from `examples/orders-service`
  and `examples/changes` to `patchahead/demo/fixtures/`, and the web UI's page
  from `web/index.html` to `patchahead/web/static/`. `patchahead demo` has to
  work from `pip install` in an empty directory, and files that only exist in a
  git checkout do not. `patchahead demo --print-paths` prints where they landed.
  `tests/test_packaging.py` builds a real wheel and sdist and compares their
  contents against the fixture tree on disk, so a `package-data` pattern one
  directory too shallow fails the build instead of silently shipping a demo with
  no repository in it.
- **The README leads with the demo**, the proof numbers, and the refusal
  scenario rather than with architecture.

### Fixed

- **Renames no longer cross object boundaries.** When a change document asserts
  an owner, a site whose receiver is not that owner is reported and left alone.
  `order["total"]` and `customer["total"]` on the same line are different
  fields; both were being rewritten. Same for `client.fetch_orders()` versus
  `analytics.fetch_orders()`.
- **UTF-8 source is no longer corrupted.** `ast` reports columns as UTF-8 byte
  offsets and Python strings are indexed by character, so an edit on a line
  containing non-ASCII text landed at the wrong position — a line beginning
  `name = "José"` produced `order[""amount"`, which is not valid Python.
  Conversion now happens where `ast` data enters the system.
- **A loop in a nested function is migrated once.** Loop discovery walked into
  nested scopes, matching the same loop as both the outer and the inner
  function and emitting two overlapping sets of edits for it.
- **`migrated` now requires red-to-green evidence.** A green-to-green run is
  reported `patched_unverified`: no gate objected, but nothing demonstrated the
  migration did anything.

### Changed

- **LLM proposals are checked against a full function contract** — `async`-ness,
  name, every parameter with its kind and annotation, defaults, return
  annotation, and decorators. The previous check compared only the name and
  parameter names, so a model could silently drop `async`, remove a default,
  change an annotation, or delete a decorator and still be accepted.
- `SymbolTarget.owner_is_explicit` distinguishes an **asserted** owner ("the
  field on each `order` object", a dotted `client.fetch_orders` rename, a
  structured document's `owner` field) from one **inferred** from an
  illustrative snippet. Only an asserted owner vetoes a mismatched receiver, so
  `api_client.fetch_orders()` still migrates when the vendor's example happened
  to call its variable `client`.

### Added

- **Direct LLM contract regression tests** (`TestContractPreservation`,
  `TestContractPositiveControls`) over a fixture using every construct the
  check compares: a decorator, `async`, positional-only and keyword-only
  parameters, `*args`, `**kwargs`, defaults with and without values,
  annotations, and a return annotation. One named test per dimension, each
  verified to fail when the contract comparison is disabled. Positive controls
  confirm a body-only rewrite is accepted and that formatting-only differences
  in annotations and defaults do not cause a false rejection.
- **Engine-level green-to-green tests** asserting `engine.migrate()` returns
  `PATCHED_UNVERIFIED` with `succeeded is False` when the suite was already
  green, paired with the red-to-green `MIGRATED` case over the same patch.
- An **adversarial evaluation suite** (`evals/datasets/adversarial/`, 20 cases)
  covering unrelated objects sharing a field name, strings and comments
  containing the name, Unicode before and on the edited line, nested functions
  and classes, comprehensions and lambdas, decorated async methods, multiline
  calls, already-migrated and partially-migrated repositories, and unsupported
  shapes. It checks the patched *source*, not just the site list, and it was
  verified to fail when each fix above is reverted.
- `iter_own_scope`, a shared scope-limited AST traversal.
- `docs/safety.md` now separates three things it previously blurred: where
  PatchAhead writes, what the workspace isolates (filesystem writes inside the
  copy, and nothing else), and what a test command can do (anything you can).

## [0.2.0] — 2026-09-11

The prototype rebuilt as a tool that works on repositories other than the one it
was written against. `docs/assessment.md` records what was wrong with 0.1.0 and
how each defect was verified.

### Added

- **A real CLI** — `patchahead analyze | migrate | handlers`, with `--dry-run`,
  `--use-llm`, `--no-tests`, `--json`, `--output-dir`, `--pr-summary`,
  `--keep-workspace`, `--min-confidence`, and meaningful exit codes.
- **An explicit domain model** — `BreakingChange`, `CodeReference`,
  `ImpactFinding`, `ImpactReport`, `ImpactGraph`, `MigrationPlan`,
  `PatchProposal`, `ValidationResult`, `MigrationResult`. No dicts across stage
  boundaries.
- **Workspace isolation** — `Repository` is read-only by construction;
  `Workspace` is a temporary copy. The user's tree is never written to.
- **A validation engine** with five gates (syntax, scope, targeted tests,
  regression, migration assertion) producing a structured result. The regression
  gate is baseline-relative, so a repository broken by several upstream changes
  can still migrate one of them.
- **Plugin-style migration handlers** — `supports/analyze/plan/generate`, with a
  registry self-test that fails CI if a change kind has no handler.
- **Two new migration families** — `method_rename` and `kwarg_rename`.
- **Structured change documents** — JSON and YAML, in addition to Markdown.
- **Project configuration** — `[tool.patchahead]` in `pyproject.toml` or
  `.patchahead.toml`. Unknown keys are errors.
- **A test suite for PatchAhead itself** — 200+ tests: unit, integration, and
  end-to-end against real repositories.
- **Executable evaluations** — `evals/run.py` reports classification accuracy,
  impact precision/recall, and migration success rate. Run in CI.
- **Documentation** — architecture, migrations, safety and threat model,
  contributing (with a worked "add a migration family" walkthrough).
- **GitHub Actions** — tests on Python 3.10–3.13, lint, evals, a CLI smoke test,
  and packaging validation.

### Changed

- **Pagination migration is now a real AST transform.** 0.1.0 pasted a hardcoded
  copy of the demo's own function over whatever function it found, renaming it
  and changing its signature. It now recognizes a documented loop shape and
  rewrites four spans, preserving the function's name, signature, docstring,
  response keys, and everything else.
- **Impact analysis moved from regex to AST.** On the assessment's four-line
  test case, 0.1.0 reported three false positives out of four findings and
  rewrote all of them. Constructs that are not field accesses are now
  structurally invisible, and findings carry graded confidence.
- **Patches are range edits**, so diffs contain only the tokens that changed.
- **The demo became an example repository** (`examples/orders-service`), reached
  through the ordinary engine path with no special-casing.
- **The web UI is a view over the engine**, with no demo-only business logic.
- **LLM failures are structured errors**, not `None`. The model's output is
  rejected — not repaired — if it renames a function, changes a signature, names
  an unimplicated file, or does not parse.

### Removed

- `run_demo.py`, `scenarios.py`, `paths.py`, and the Redis "memory" feature,
  which returned a hardcoded list of fictional migrations when Redis was absent
  and displayed it as "seen before".
- Six accidental zero-byte files committed by mistyped shell commands.
- `response_shape_change` and `endpoint_change` as claimed capabilities. They
  were classified but nothing could migrate them; they are now reported as
  unsupported.

### Fixed

- `pytest` on a fresh clone was red: `testpaths` pointed at the deliberately
  broken demo fixtures.
- Unparseable change documents silently defaulted to the demo's pagination text,
  making every parse failure look like a confident success.
- A patch that modified no files passed validation.
- `--no-tests` reported `migrated`; it now reports `patched_unverified`.
- The web UI's scenario selector did not affect the run.
- `import anthropic` ran before the availability check, so a missing optional
  dependency escaped as an unhandled `ModuleNotFoundError`.
- A bare `python -m pytest` resolved via `PATH` to whatever interpreter came
  first, often one without pytest installed.

## [0.1.0]

Hackathon prototype. Two hardcoded demo scenarios.

[Unreleased]: https://github.com/FrimpsManu/patchahead/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/FrimpsManu/patchahead/releases/tag/v0.2.0
