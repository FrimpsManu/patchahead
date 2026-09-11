# Changelog

All notable changes to PatchAhead are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
