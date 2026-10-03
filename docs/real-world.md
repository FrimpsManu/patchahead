# Replaying real migrations

Every other number in this project comes from a benchmark someone wrote for
it. This one does not. It takes ten public commits in which maintainers moved
their code from pydantic 1 to pydantic 2 **by hand**, runs PatchAhead on the
commit before each one, and compares what PatchAhead did with what the people
did, call by call.

Nothing was given to PatchAhead but the two pydantic versions: the change
document is the output of

```bash
patchahead api-diff pydantic 1.10.13 2.0 --out changes.json
```

which reads five renames out of the two releases -- `dict` -> `model_dump`,
`parse_obj` and `from_orm` -> `model_validate`, `construct` ->
`model_construct`, `schema` -> `model_json_schema` -- and reports the rest.

## Results

Each row counts call sites, in the commit before the migration, of a method
PatchAhead renames.

| Project | Commit | Both renamed | Only PatchAhead | Only the person | Nobody |
|---|---|---|---|---|---|
| [conda/menuinst](https://github.com/conda/menuinst/commit/bac0f48b1d883e4faea6beee13f45ab511c2a01b) | `bac0f48` | 1 | 2 | 0 | 1 |
| [gtalarico/pyairtable](https://github.com/gtalarico/pyairtable/commit/54849c686d3a5f49eca7b006d11ab6455eb91fd2) | `54849c6` | 15 | 1 | 0 | 46 |
| [OpenFreeEnergy/alchemiscale](https://github.com/OpenFreeEnergy/alchemiscale/commit/92389ac2bf59d99903ca2db1205ddd40ca9b2c1c) | `92389ac` | 3 | 36 | 0 | 0 |
| [SeldonIO/MLServer](https://github.com/SeldonIO/MLServer/commit/58cf8dc7f00091137c8b55470ad42b7d3f977419) | `58cf8dc` | 0 | 5 | 36 | 6 |
| [epflgraph/graphchatbot](https://github.com/epflgraph/graphchatbot/commit/15aec5f43fa90d8d92941489b4ae3725cde5dae2) | `15aec5f` | 4 | 0 | 0 | 0 |
| [TisoneK/InjectX](https://github.com/TisoneK/InjectX/commit/547f8fea9b2f971c7be4f6ccfbc1620f9ea97351) | `547f8fe` | 5 | 0 | 0 | 0 |
| [TolulopeBabajide/awade](https://github.com/TolulopeBabajide/awade/commit/a680f7b0f156a6c9cdce4e4979b58e82807c5b9a) | `a680f7b` | 16 | 1 | 0 | 15 |
| [hywooga/FastApi-Todos](https://github.com/hywooga/FastApi-Todos/commit/115a53092085987fd5b918ea9eed4d20d1970e46) | `115a530` | 2 | 0 | 0 | 0 |
| [nepovtor/block_finance_MVP](https://github.com/nepovtor/block_finance_MVP/commit/a95feeca406aaf973b8becd721024dd94eed03cf) | `a95feec` | 1 | 0 | 0 | 0 |
| [Njoselson/tenant-legal-tools](https://github.com/Njoselson/tenant-legal-tools/commit/e200ea9e006ee5800a44d67a95f554a4a489a91f) | `e200ea9` | 2 | 3 | 0 | 0 |
| **Total** | | **49** | **48** | **36** | **68** |

### Every edit PatchAhead made was correct

PatchAhead made 97 edits -- in application code and, since tests are migrated
too, in test files. 49 match what the maintainers did. For the other 48, each
receiver was traced to its class in the project's source: every one is a
pydantic `BaseModel` subclass (`Scope`, `ScopedKey`, `CredentialedEntity` and
the `scope_test` fixture in alchemiscale; `InferenceRequest`, `TensorData` and
others in MLServer; `SourceMetadata`, `AdminAuditLogResponse` elsewhere), so
every rename is a valid pydantic 2 migration. They are calls the maintainers
had not migrated in that commit:

- 4 were later made the same way on the projects' default branches;
- 8 still use the old, deprecated call today;
- 36 are on lines that have since been rewritten -- alchemiscale, for one,
  later replaced `scope.dict()` with a `to_dict()` helper of its own.

**Wrong edits found: 0 of 97.**

That number has a history worth stating. The first replay with test migration
switched on made **11 wrong edits**: in one project's tests,
`patch.dict("os.environ", ...)` -- `unittest.mock.patch.dict` -- became
`patch.model_dump(...)`. A receiver bound to a standard-library import is now
never treated as the upgraded library's object, the case is in the adversarial
benchmark, and the 11 are gone. Replaying real code is how it was found.

### What PatchAhead did not do, and why

Of the 36 calls only the maintainers renamed:

| Why | Calls |
|---|---|
| The project **defines its own method** with that name. MLServer overrides `dict()` and `parse_obj()` on its base classes; renaming the calls without the overrides would silently skip MLServer's own logic. | 35 |
| Inside a comment, not a call | 1 |

Test files are migrated too: before they were, 33 of the calls PatchAhead
missed were in tests. A test PatchAhead edits is never counted as evidence that
its edit worked -- see [architecture.md](architecture.md#tests-are-migrated-but-do-not-vouch).

The maintainers also renamed 10 `.json()` calls to `.model_dump_json()`, which
PatchAhead reports but does not apply: `model_dump_json` takes no `encoder=`
or `models_as_dict=`, so it does not accept every call `json` did.

Of the call sites PatchAhead was free to change (excluding the 35 overrides
and the comment), it renamed every one the maintainers renamed, and more
besides.

## What this does and does not show

- **It measures editing, not verification.** The projects' tests were not run:
  installing ten projects' dependencies is out of scope. The five checks and
  red-to-green evidence apply when PatchAhead runs in a project's own
  environment.
- **Pydantic only.** One library, ten projects, chosen because the migrations
  were single-purpose commits. It is a sample, not a census.
- **"Correct" means API-correct.** Each disputed rename was checked against the
  receiver's class, not run.

## What it changed in PatchAhead

The first replay found that people migrate the tests that call the old API and
PatchAhead did not -- 33 of its misses. Tests are now migrated, with the rule
that keeps the evidence honest: a test PatchAhead edited is not counted as
proof that its edit worked. The second replay, with tests in scope, found the
`patch.dict` wrong edits above, and a method call on an unnamed receiver graded
as the built-in `dict()`. Both are fixed and in the benchmark.

## Reproduce it

```bash
patchahead api-diff pydantic 1.10.13 2.0 --out changes.json
python evals/realworld/replay.py --changes changes.json
```

It needs network access and `git`. The cases are in
[`evals/realworld/cases.json`](../evals/realworld/cases.json); `--json` prints
every disagreement with its reason. Replaying the same commits gives the same
numbers.
