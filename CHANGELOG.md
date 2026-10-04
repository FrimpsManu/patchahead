# Changelog

All notable changes to PatchAhead are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **A quieter web UI.** `patchahead demo` and `patchahead web` now use one
  neutral palette and a single accent colour, the system font, and more space.
  Colour is kept for meaning only: a small dot marks each outcome, and diffs keep
  their tints. Outcome labels are in sentence case ("Verified migration",
  "Refused"), the numbered step pills became a plain breadcrumb, and the copy
  is shorter. Light and dark mode follow the system. Nothing about what the page
  shows or how it behaves changed. The README GIF and screenshots were
  re-recorded.

## [0.3.1] — 2026-10-04

### Added

- **A demo GIF** at the top of the README: the six scenarios, a verified
  migration and its five gates, then a refusal. It is recorded by
  `scripts/record_demo.py`, which drives the real `patchahead demo` page with a
  headless browser.

### Changed

- **PyPI's one-line summary** now matches the project's description: "Updates
  your Python code when an API you depend on changes, and proves the fix with
  your tests."

## [0.3.0] — 2026-10-03

The first release published to PyPI.

### Added (a second real-world study)

- **Python 3.12's `unittest` removals, replayed on 12 projects** from the
  unedited `unittest` section of CPython's `Doc/whatsnew/3.12.rst`: 412 edits,
  288 identical to the maintainers', none of theirs missed, 0 wrong.
  `evals/realworld/replay.py --cases` runs any case list.
- **Sphinx and reStructuredText simple tables are read.** Cross-references
  (`` :meth:`.assertEqual` ``, `` :class:`~unittest.TestCase` ``, titled targets)
  become names -- a method or function role marks a call -- and `====`-bordered
  tables become pipe tables. The real note read 0 of its 15 renames before.

### Fixed

- **A new name under a different owner is a move, not a rename.** Read on the
  whole 3.12 "What's New", `imp.find_module()` -> `importlib.util.find_spec()`
  and four more were taken as renames that would have written `imp.find_spec()`.
  They are now reported, never applied.
- **A method a shared test base class defines governs test calls.** Essentia's
  `TestCase` defines its own `assert_`; calls to it in other test modules were
  renamed to `assertTrue`, bypassing the project's code. Definitions in test
  modules now count for test call sites, as application definitions do for
  application code; they still never block the application from migrating.

### Added (tests)

- **Tests that use the old API are migrated too** (`migrate_tests`, on by
  default). Replaying real migrations showed that people migrate their tests,
  and that 33 of the calls PatchAhead missed were in test files.
- **A test the patch edited never vouches for it.** The migration-assertion
  gate ignores tests in files the patch changed; if those are the only tests
  that went from failing to passing, the run is `patched_unverified`.
- In tests, a field is renamed only on the object the change names (tests also
  check the application's own output), a mock's setup moves with the calls,
  and a module that defines its own fake with the old name is left alone.

### Fixed

- **A receiver from the standard library is not the upgraded library's
  object.** With tests in scope, replaying real migrations renamed
  `patch.dict("os.environ", ...)` -- `unittest.mock` -- to `patch.model_dump`
  11 times in one project. A call on a name bound to a standard-library import
  is now reported, never renamed.
- The real-world replay now covers tests: 97 edits, 49 identical to the
  maintainers', 0 wrong.

### Added (real-world evidence)

- **`docs/real-world.md`**: ten public commits in which maintainers migrated
  from pydantic 1 to 2 by hand, replayed with PatchAhead from nothing but
  `api-diff pydantic 1.10.13 2.0`. Of 67 edits, 35 match the maintainers' and
  the other 32 are valid renames on pydantic models the maintainers had not
  migrated yet; 0 are wrong. The calls it left alone are in test files (33),
  calls to methods the project overrides (16), and a comment.
- `evals/realworld/replay.py` reproduces it (network and `git` required; not
  part of CI).

### Fixed

- **A method call on an unnamed receiver is not a bare call.**
  `requests[0].dict()` was graded as a call to the built-in `dict()`, with the
  wrong explanation. Found replaying MLServer.

### Added (CI)

- **A GitHub Action** (`action.yml`) for Dependabot and Renovate pull requests.
  It reads the release notes in the pull request and compares the two versions
  of each package it bumps, migrates a temporary copy, runs the tests, and
  reports through step outputs, the job summary, and an optional pull-request
  comment that re-runs update in place. `apply` writes a verified patch into
  the checkout for a later step; it never commits. `fail-on` decides when the
  step fails. A CI job runs the action against the bundled example on every
  build.
- **Release notes checked against the library.** A release-note rename whose
  new name the upgraded version does not define is dropped, with a note.
- **Dependabot's HTML release notes are read**: headings, lists and `<code>`
  become Markdown, the commit list is ignored, and a change the release notes
  and the changelog both state is read once.
- `MigrationRun.diff` (one diff of the whole run) and `MigrationRun.outcome`
  (the run's verdict in one word), both in `--json`.

### Changed

- **An owner matches a qualified receiver.** `api_client` and `self._client`
  are instances of `Client`, and `get_client()` returns one; a name ending in
  `_<owner>` now names that owner. Two adversarial cases recorded as gaps --
  `self._order` read in another method, `factory.get_client().fetch_orders()`
  -- now pass. `client_config` still does not match.

### Added (comparing library versions)

- **`patchahead api-diff`** reads the breaking changes out of two versions of a
  library, for when there is no usable release note, and writes the ones
  PatchAhead can migrate as a change document for `migrate --change`. Versions
  come from PyPI as wheels, checked against their published SHA-256, or from
  local directories and `.whl` files; nothing is installed or run.
- It reads method and function renames (an old member gone with one
  compatible new sibling, or newly deprecated in favor of a sibling it names),
  class renames, and keyword-argument renames (same slot, related names), and
  reports moves, removals, new required parameters, and replacements that take
  different arguments. A rename is applied only when the new member accepts
  every call the old one did.
- On real releases: pydantic 1.10.13 -> 2.0 yields `dict` -> `model_dump`,
  `parse_obj` and `from_orm` -> `model_validate`, `construct` ->
  `model_construct`, and `schema` -> `model_json_schema`, and reports `json`,
  `copy` and `validate`, whose replacements take other arguments; requests
  2.31.0 -> 2.32.3 reports `get_connection` and renames nothing.
- An `api_diff` benchmark suite of 20 before-and-after libraries, including the
  readings that went wrong on those real releases before they were fixed.
- Structured change documents accept `"owner_explicit": false`, for an owner
  that is a hint rather than an assertion.

### Fixed (imports)

- **A method rename now renames `from sdk import old_name` with its calls.** The
  call sites were rewritten and the import was not, so the patched module
  failed at import time -- caught by the tests, and named by the new
  completeness report, but still a migration PatchAhead should have finished.
  An `as` alias is kept, so its uses need no edit. Imports of the repository's
  own code, relative imports, and module-level imports in a change that names a
  receiver are reported instead.

### Added (completeness)

- **A completeness report after every patched migration.** Passing tests show
  the code they run works, not that the migration is finished. PatchAhead now
  re-analyzes the patched copy and scans its Python tokens, configuration, and
  documentation for every place the old name survives, sorted into code it did
  not rewrite (an import, a dictionary built with the old key), dynamic access
  (`getattr(obj, "old_name")`, which fails only at runtime), tests still using
  the old name, sites on another object left alone on purpose, and mentions in
  strings, comments, config, and docs. It is in the terminal output, `--json`,
  and the pull-request summary.
- **`--require-complete`** exits 1 while any code, dynamic access, or test still
  uses an old name.
- `ImpactFinding.other_object` records a site whose owner the change document
  asserted is something else, so "left on purpose" is structured rather than
  read from a message.

### Documentation

- **A shorter README in plain words**, leading with the problem and a real
  run, with two architecture diagrams: how one run flows, and how the packages
  depend on each other. The reference material it used to carry -- every
  command, configuration, exit codes, AI mode, the web UI -- moved to
  `docs/usage.md`.
- **"Adding a migration family touches two files" was not true.** It touches
  four places: the `ChangeKind`, the classifier's signals, the handler, and the
  registry import. Corrected in `docs/architecture.md`, `docs/contributing.md`,
  and `handlers/base.py`.

### Added

- **A release workflow** that publishes to PyPI through trusted publishing when
  a GitHub release is published, after checking that the tag matches the
  version and that the built wheel installs and runs. It does nothing until a
  maintainer sets up the trusted publisher.

### Changed (reading release notes)

- **One section can hold several changes.** Each bullet, table row, and
  paragraph is read on its own, so a table of four renamed methods yields four
  changes, and "Renamed `a()` to `b()` and `c()` to `d()`" yields two. A section
  that states a single rename is still read as a whole, so its Before/After
  example can still supply an owner.
- **Changes that cannot be migrated are reported, not dropped.** A bullet about
  an endpoint move or an authentication change next to two renames used to be
  lost; it is now an `unsupported` change with a reason.
- **More phrasings:** tables (columns chosen by their "Old"/"New",
  "Before"/"After", "v1"/"v2" headers), "is now (called)", "has been replaced
  by", "deprecated in favor of", "use X instead of Y", "replace X with Y",
  conventional-commit footers, and reStructuredText (underlined headings,
  double-backtick literals). Keep-a-Changelog `Added` and `Features` sections no
  longer contribute changes; `Deprecations` sections now do, since a deprecation
  that names its replacement is a migration.
- **The kind of rename is read from how the names are written**: `x()` is a
  call, `x=` a keyword, a noun beside the name ("the `x` property"), or how the
  document's code examples use it -- before falling back to section-wide
  signals.
- **An owner named as a class matches the instance in code.** `Charge` matches
  `charge`, `PaymentIntent` matches `payment_intent`. An API reference's
  capitalized object name ("on the Charge object") is read without backticks.
- A `release_notes` benchmark suite: 33 whole documents, scored change by
  change, with `misread_changes` asserted at zero.

### Fixed (reading release notes)

- **A clause between a name and its verb no longer becomes the name.** "The
  `verify_ssl` keyword argument of `get()` and `post()` has been renamed to
  `verify`" was read as `post` -> `verify`.
- **A namespace move is not a rename.** `openai.Completion.create()` ->
  `client.completions.create()` keeps the name `create`; it is reported as
  unsupported rather than read as a no-op rename.
- **A type change is not a rename.** "The `timeout` argument is now `float`" is
  no longer read as `timeout` -> `float`.
- **Cursor pagination that never names its cursor is not migrated.** A note
  moving to `starting_after` or `NextToken` was migrated with the default names
  `cursor` and `next_cursor`, which that vendor never used. It is now reported
  as unsupported.

### Fixed (reliability)

- **`.git` and other top-level dot-directories are no longer copied into the
  workspace.** The exclusion check stripped `"./"` as a *set of characters*, so
  `.git` became `git` and matched nothing -- nor did `.tox`, `.mypy_cache`,
  `.pytest_cache`, `.hg` or `.patchahead`. A large `.git` was copied on every
  run, and `max_workspace_files`, which counted correctly, did not bound what
  was actually copied.
- **A timed-out test command no longer leaves its test runner running.** Only
  the shell was killed; the process it started kept running in a workspace
  that was then deleted. The command now leads its own process group, and the
  group is killed.
- **Test output that is not UTF-8 no longer crashes the run.** It ended the
  migration with an unexpected `UnicodeDecodeError`; undecodable bytes are now
  replaced.
- **Windows (`\r\n`) line endings are preserved.** Files were read with
  newline translation and written back with `\n`, so a one-token rename
  rewrote every line, and the diff did not apply to the original file.
  Sources are now read and written byte-for-byte, and LLM-proposed functions
  are written in the file's own line ending.
- **The same release note reads the same way on every run.** Pagination field
  names were collected into a `set`, so with several candidates the one chosen
  depended on the hash seed. The first candidate in document order now wins.
- **Analysis no longer slows quadratically with the number of findings.** Test
  discovery ran once per finding rather than once per file: 15,000 findings
  took 11.6s to analyze, and now take 0.8s.

### Security

- **The web UI can no longer be driven by other websites.** It bound to
  `127.0.0.1` but accepted any request, so a page open in the same browser could
  POST to `/api/migrate` -- running the repository's test command, or with
  `use_llm=true` sending its source to the model provider on the user's key --
  and a DNS-rebinding page could read `/api/context`. Every request must now
  address a loopback host, a cross-origin `Origin` is refused, and anything but
  a read needs a per-process token embedded in the served page.
- **Sentry events no longer carry source code.** sentry-sdk attaches every
  stack frame's local variables by default, and here those include whole files
  of the repository. `include_local_variables` is off, and the event scrubber
  removes frame locals as well.
- **Test commands lose the model's credentials once model-written code is in
  the workspace.** `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are withheld
  from the test command after an LLM proposal is applied.
- **An LLM proposal that moves a method out of its class is rejected.** The
  contract check read the proposal on its own, so a method sent back
  unindented -- which still parses, as a module-level function -- was accepted.
  Placement is now checked against the patched file.
- **A rejected LLM proposal writes nothing.** Every file is patched and checked
  before any is written; a rejection on the second file used to leave the first
  one modified in the shared workspace.

### Fixed (wrong edits reported as verified)

- **A common method name no longer renames unrelated calls.** A `get` ->
  `retrieve` rename, with the receiver taken from the document's example rather
  than asserted, rewrote `os.environ.get(...)` and a settings dict's `.get(...)`
  along with `client.get(...)`. A test covering only the client call then
  reported the whole patch as `migrated`. A name that is also a method of a
  built-in type (`get`, `update`, `items`, `copy`, ...) now needs a receiver
  matching the document before it is patched; other sites are reported.
- **A bare call to a Python built-in is not an SDK function.** A Pydantic-style
  `.dict()` -> `.model_dump()` rename rewrote `dict(data)` too. The built-in is
  now left alone unless the module imports a `dict` from somewhere.
- **A name the repository defines in another module is not renamed on name
  alone.** The guard against renaming calls to the repository's own code only
  looked at the calling module, so `repo.fetch_all()` was rewritten when
  `ProductRepo.fetch_all` lived in `repo.py`. An explicit
  `from sdk import fetch_all` still counts as evidence; an import of the
  repository's own definition does not.
- **A renamed key on a nested object is not patched without an owner.**
  `charge["customer"]["amount_cents"]` reads the customer's field, not the
  charge's, and was rewritten whenever the document named no owner.
- Four adversarial eval cases cover these, each verified to fail before the fix.

### Changed (exit codes)

- **`patched_unverified` exits 1 unless `--no-tests` was passed.** It exited 0,
  which the README defined as "migration verified": tests that ran and proved
  nothing, or a test runner that could not start, looked like a pass in CI.
- **`no_impact` on a suite that was already failing exits 1** and says so. A
  change document PatchAhead misread finds nothing, and "Nothing to migrate"
  with exit 0 hid the failures the change had caused. On a passing suite it
  still exits 0.

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
- **A `demo` extra** (`pip install -e '.[demo]'`): the web UI plus pytest,
  because a migration is only verified when tests actually run, and without a
  runner every scenario reports — honestly but uselessly — that the test command
  could not start. `patchahead demo` says so on startup if pytest is missing.
- **`docs/demo-recording.md`**, a 45-second recording sequence, and
  `docs/media/`, holding real screenshots of the running UI.

### Fixed (documentation)

- **The evaluation claim now matches what the harness computes.** Patch
  precision, recall and false positives come from the 28 site-detection cases
  (`impact` + `adversarial`, 31 expected patch sites) -- not from all 48, whose
  other 20 measure classification accuracy and end-to-end migration. The number
  is unchanged and the scope is now stated.
- **"The only thing mocked is the Anthropic API" was no longer true.** It is the
  only external *service* that is mocked, and the demo tests substitute the
  server start, port probe and browser launch -- functions this project owns,
  where the behaviour under test is the wiring. The README, `docs/contributing.md`
  and `tests/conftest.py` all said the stronger thing.
- **Install instructions no longer promise a PyPI release that does not exist.**
  PatchAhead is not published and has no publishing workflow, so the README, the
  recording script and the optional-dependency error messages all name the
  source install that actually works. The `patchahead[...]` spellings are noted
  as what they become after a release rather than presented as current.
- **`CodeReference` said its columns were `ast` byte offsets.** They have been
  character offsets since the UTF-8 fix; the docstring had not caught up, which
  is worse than no comment on a field a contributor would index a string with.
- **`domain/validation.py` said `ValidationResult.passed` decided success.** It
  is necessary and not sufficient: `MigrationResult.succeeded` requires
  `verified`, which requires the migration-assertion gate to have passed.

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
- **The evaluation benchmark is a package with its own tests.** Datasets load
  into typed, strictly-validated case objects: an unknown key, a missing
  required key, a value of the wrong shape, a duplicate id, or an empty dataset
  is a load error rather than a silently unscored field. A mistyped
  `expect_patched` used to assert nothing and pass, which is the same failure
  mode as a deleted test.
- **A `validation` suite.** The subsystem that decides what "working" means had
  no evaluation coverage. Ten cases assert the verdict of every gate, including
  the syntax and scope failures that only a misbehaving patch generator can
  produce and that no deterministic handler can reach.
- **`known_gap` cases.** A dataset may record a case PatchAhead is expected to
  fail, with a stated reason. It is scored honestly and reported, does not fail
  the build — and *does* fail the build if it starts passing, so a closed gap
  cannot leave a stale marker behind. A known gap may only under-patch: one that
  rewrites the wrong code fails regardless of the marker.
- **Metrics for the dimensions that had none.** F1, confidence calibration,
  refusal precision and recall, separate owner and owner-assertion accuracy, a
  five-way migration outcome taxonomy (successful / missed / safe refusal /
  incorrect / unnecessary), patch size, and per-gate verdict counts. Headline
  numbers exclude known gaps so the regression floor stays meaningful;
  `*_including_gaps` numbers include them so the floor cannot hide a limitation.
- **Adversarial cases for aliases, imports, several affected files and partial
  migration**, and `docs/evaluation.md` describing all of it.
- **A Markdown benchmark report**, written by
  `python evals/run.py --format markdown --out <path>` and uploaded as a CI
  artifact.

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
- **A keyword rename with no named function no longer rewrites every call using
  that keyword.** `retries` is an ordinary word: a change document that renames
  it without saying which function it belongs to gives nothing to distinguish
  the upstream SDK's `retries=` from another library's, and
  `send_email(to=..., retries=5)` was being rewritten on the strength of the
  shared name. The sites are now reported and the migration reports
  `not_plannable`. The refusal does not depend on the confidence threshold, so
  `--min-confidence low` does not reopen it.
- **The migration-assertion gate no longer fails a patch for the absence of
  evidence.** It reports whether a test went red-to-green, so its answers are
  PASSED and SKIPPED; breakage is the regression gate's verdict, given once. Two
  shapes used to come out FAILED: a suite still red with nothing repaired, and a
  patch that repaired one test while breaking another. It also read the
  *regression gate's* status as "the suite is green" — and that gate is
  baseline-relative, so it passes over a red suite — producing the
  self-contradictory "the run is green but none of the tests that failed before
  the patch were among them" on a repository that was simply already broken.
- **A test runner that cannot start is no longer reported as a code
  regression.** Exit 127 (command not found) and 126 (not executable) mean
  nothing ran; the targeted gate skipped on them and the regression gate called
  them a test failure, then blamed the patch. Detection now reads the exit
  status rather than the shell's wording — `bash` says "command not found" and
  `dash` says "not found", and only the first spelling was matched. Both gates
  skip with the same message, which names `test_command` and the missing runner
  so the reader has a next step, and the migration stays unverified.
- **The classifier reads "renamed the `x` keyword argument to `y`".** The rename
  patterns required the renamed name to sit directly beside the word `to`, so a
  common release-note shape was reported as unreadable. The intervening words
  are matched from a fixed noun list rather than as "any two words", so
  "renamed the `client` argument passed to `fetch_orders`" — which renames
  nothing — still does not match.

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

[Unreleased]: https://github.com/FrimpsManu/patchahead/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/FrimpsManu/patchahead/releases/tag/v0.3.1
[0.3.0]: https://github.com/FrimpsManu/patchahead/releases/tag/v0.3.0
[0.2.0]: https://github.com/FrimpsManu/patchahead/commit/61772cbef4d6a0274fe175e63b4d25602f1606ea
