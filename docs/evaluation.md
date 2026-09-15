# Evaluation

PatchAhead's claim is not "it produces a diff". It is "it finds the code an
upstream change breaks, refuses the cases it cannot prove, and never edits
anything else". Those are measurable claims, and this is how they are measured.

```bash
python evals/run.py                       # every suite
python evals/run.py adversarial           # one suite
python evals/run.py --format markdown     # a report you can paste
python evals/run.py --format json         # machine-readable
```

Exit status is 0 only when every suite is green. CI runs it on every push, so a
change that degrades precision, loosens a validation gate, or starts editing
already-migrated code fails the build rather than being noticed at a release.

Every number the benchmark prints is computed from a run that just happened.
Nothing is cached, recorded from a previous version, or written down by hand --
including the numbers quoted in the README and in pull request descriptions.

## The suites

| Suite | Question | Dataset |
|---|---|---|
| `classification` | Does it read a change document correctly, and refuse the ones it should? | `evals/datasets/classification/` |
| `impact` | Which sites does it find, and which does it rewrite? | `evals/datasets/impact/` |
| `adversarial` | The same, on cases written to fool it | `evals/datasets/adversarial/` |
| `migrations` | Does a whole engine run take a real repository from red to green? | `evals/datasets/migrations/` |
| `validation` | Do the five gates reach the right verdict? | `evals/datasets/validation/` |

They run cheapest-first, so a dataset mistake or a classification regression
surfaces in milliseconds rather than after the suites that spawn subprocesses.

## What each suite measures

### `classification`

Five things, scored separately because they fail separately: the migration
family, the old and new names, the owner, and **whether the document asserted
that owner or merely illustrated it**. The last one decides whether a receiver
mismatch refuses or only lowers confidence, so it gets its own number.

Two more properties of the reading rather than its content:

* **Refusal precision and recall.** A classifier scored only on what it can do
  is not a classifier. The expensive error is confidently misreading a document
  it should have declined, and that is `refusal_false_negatives`, asserted at
  zero.
* **Calibration.** Accuracy bucketed by the confidence PatchAhead reported. If
  low-confidence readings are as accurate as high-confidence ones, the grading
  carries no information and should not be shown to users as though it does.
  `calibration_monotonic` is 1 when accuracy never rises as confidence falls.

### `impact` and `adversarial`

A *site* is `path:line`. The datasets label three kinds:

| Label | Meaning | Getting it wrong is |
|---|---|---|
| `expect_patched` | must be rewritten | a false negative -- a missed site |
| `expect_reported_only` | must be found and explained, **not** rewritten | a false positive -- a wrong edit |
| `expect_source_contains` | must be in the repository after patching | a mangled edit |

The third one exists because a site list can be right while the edit is still
wrong. A byte-versus-character column error lands on the correct line and
corrupts it, and only reading the patched source catches that.

`patch_false_positives` is the metric this project is built around. It is
asserted at zero and is not negotiable downward.

The adversarial dataset covers unrelated objects sharing a field name, strings
and comments containing it, Unicode before and on an edit site, nested functions
and classes, comprehensions and lambdas, decorated async methods, multiline
calls, module aliases, imported versus locally-defined symbols, several affected
files, partially-migrated repositories, and unsupported code shapes.

### `migrations`

Real directories, real `pytest` subprocesses, real gates. Runs are sorted into a
five-way taxonomy rather than a pass rate:

| Outcome | Meaning |
|---|---|
| `successful_migration` | should migrate, did, and the tests proved it |
| `missed_migration` | should migrate, did not -- a capability gap |
| `safe_refusal` | should not migrate, and declined with a stated reason |
| `incorrect_migration` | should not migrate, reported success anyway |
| `unnecessary_migration` | nothing needed changing and something changed |

The last two are wrong edits reaching a user and are asserted at zero.
`missed_migration` is a limitation, and limitations are survivable.

Patch size is recorded too (`changed_lines_mean`, `changed_files_max`). There is
no correct number of changed lines, but a rename whose mean diff doubles has
stopped being a rename, and nobody notices that without a number.

### `validation`

The validation engine is the only thing permitted to call a migration
successful, so it gets its own suite. Each case declares the status every gate
must reach:

| Scenario | Required verdict |
|---|---|
| red before, green after | `migration_assertion` PASSED -- verified |
| green before, green after | `migration_assertion` SKIPPED -- no evidence |
| a test the patch broke | `regression_tests` FAILED |
| a test that was already red | `regression_tests` PASSED -- not a regression |
| no tests in the repository | test gates SKIPPED, never PASSED |
| `--no-tests` | test gates SKIPPED, for a stated reason |
| a patch that does not parse | `syntax` FAILED, later gates never run |
| a file the plan did not name | `scope` FAILED, later gates never run |
| an empty patch | `syntax` and `scope` SKIPPED, so `passed` stays false |

Two of those cannot be produced by running the engine: a deterministic handler
does not emit invalid Python, and it does not touch files its plan omits. Those
cases run in `direct` mode, which writes a crafted patch into a real workspace
and hands it to the real validation engine. Without them, the two gates that
exist specifically to catch a misbehaving patch generator -- the ones that will
matter most when the LLM path is used in anger -- would have no coverage.

`expect_gate_detail` guards the subtler failure: a gate reaching the right
status for the wrong reason. `migration_assertion` SKIPPED because the suite was
already green and SKIPPED because nothing ever ran are the same status and very
different facts.

## Known gaps

A benchmark written by the same person who wrote the implementation drifts
toward cases the implementation already handles, and then reports 100% forever.
The mechanism against that is the `known_gap` marker:

```json
{
  "id": "loop_variable_aliases_the_owner",
  "known_gap": "the loop variable `o` is an `order`, but showing that needs alias analysis",
  "change": { "kind": "field_rename", "symbol": "total", "replacement": "amount", "owner": "order" },
  "files": { "app/loop.py": "..." },
  "expect_patched": ["app/loop.py:4"]
}
```

The case states what a **correct** tool does, runs, and is scored honestly. Then:

* if it fails, it is reported as a known gap and does **not** fail the suite;
* if it **passes**, the suite **fails** -- the gap is closed and the marker is
  stale, and stale markers are how a benchmark starts lying in the other
  direction;
* a known gap may only **under**-patch. If one produces a false positive, the
  suite fails regardless of the marker. Under-migrating is a limitation a user
  can work around; a wrong edit in unrelated code is the failure this project
  exists to prevent, and no marker in a dataset makes it acceptable.

Headline metrics exclude known gaps, so a recorded limitation does not erode the
regression floor CI asserts. `*_including_gaps` metrics include them, so the
floor does not hide the limitation. Both are printed on every run.

### The gaps that remain

All four are in `adversarial`, all four are the same gap wearing different
clothes, and all four are *under*-patching: PatchAhead finds the site, explains
it, and declines to rewrite it. None can be a false positive -- the harness
counts a wrong edit as fatal and no marker excuses it -- so what each costs is a
hand-edit, not a broken call site.

| Case | What it needs | Why it stays |
|---|---|---|
| `loop_variable_aliases_the_owner` | `for o in orders:` -- knowing `o` is an `order` | Alias analysis over the iterable |
| `a_local_variable_aliases_the_owner` | `current = order` | Local dataflow |
| `an_attribute_aliases_the_owner_across_methods` | `self._order` set in `__init__`, read elsewhere | Cross-method attribute tracking |
| `a_chained_call_returns_the_named_receiver` | `factory.get_client().fetch_orders()` | Return-type information |

Each needs PatchAhead to track *what a name refers to* rather than what it is
called, which is a different kind of analysis from the syntactic matching
everything here is built on -- a dataflow layer, not a patch to an existing
handler. That is a deliberate boundary, not an oversight: the alternative
available today is to rewrite on the name alone, which is exactly the
false-positive behaviour the adversarial suite exists to prevent. Until the
analysis exists, refusing is the correct answer, and these four cases keep the
cost of refusing visible in every run.

## Adding a case

1. Pick the suite whose question your case asks.
2. Add an object to that suite's `cases.json`. Keys are validated strictly: an
   unknown key, a missing required key, a value of the wrong shape, a duplicate
   id, or an empty dataset is a load error, not a low score. A mistyped key used
   to be silently unscored, which is the same failure mode as a deleted test.
3. Run `python evals/run.py <suite>`.
4. If it fails, decide which side is wrong. If PatchAhead is wrong and the fix
   belongs in a later change, mark the case `known_gap` with a reason -- do not
   delete it or weaken the expectation.

`tests/test_eval_harness.py` tests the harness itself, including the property
the whole thing depends on: that the benchmark is capable of failing PatchAhead.
If you change the scoring code, that file is where you prove it still can.

## Current results

Run `python evals/run.py` to produce them. They are deliberately not copied
here: a number in a document is a claim, and a number from a command is a
measurement.
