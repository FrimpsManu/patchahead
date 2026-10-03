# Replaying real migrations

Every other number in this project comes from a benchmark someone wrote for
it. These do not. Each study takes public commits in which maintainers migrated
their code **by hand**, runs PatchAhead on the commit before each one, and
compares what PatchAhead did with what the people did, call by call.

| Study | Change read from | Projects | Edits | Same as the maintainers | Wrong |
|---|---|---|---|---|---|
| [pydantic 1 -> 2](#study-1-pydantic-1---2) | two versions of the library (`api-diff`) | 10 | 97 | 49 | **0** |
| [Python 3.12's unittest removals](#study-2-python-312-removes-the-unittest-aliases) | the release note (CPython's "What's New") | 12 | 412 | 288 | **0** |

## Study 1: pydantic 1 -> 2

Ten public commits in which maintainers moved their code from pydantic 1 to
pydantic 2.

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

## Study 2: Python 3.12 removes the unittest aliases

Python 3.12 removed fifteen long-deprecated `unittest.TestCase` aliases --
`assertEquals`, `assertRegexpMatches`, `failUnless` and the rest -- and
projects replaced them by hand. This study differs from the first on purpose:

- **The change is read from the release note,** not from the library: the
  `unittest` section of CPython's own
  [`Doc/whatsnew/3.12.rst`](https://github.com/python/cpython/blob/3.12/Doc/whatsnew/3.12.rst),
  unedited. The renames are a reStructuredText table of Sphinx
  cross-references (`` :meth:`.assertEqual` ``), which PatchAhead could not read
  before this study -- it read 0 of the 15. It reads all 15 now.
- **Almost every edit is in test code,** so it exercises test migration.

Read on the whole 2,356-line "What's New" rather than one section, PatchAhead
finds exactly these 15 renames. Its first reading also took `imp`'s removal
table -- `imp.find_module()` -> `importlib.util.find_spec()` -- as five renames,
which would have written `imp.find_spec()`. A new name under a different owner
is now a move, reported and never applied.

| Project | Commit | Both renamed | Only PatchAhead | Only the person | Nobody |
|---|---|---|---|---|---|
| [MTG/essentia](https://github.com/MTG/essentia/commit/3a7e32e63417a2fec3b9f25b3317c47ebffd9bb9) | `3a7e32e` | 6 | 31 | 0 | 77 |
| [zentralopensource/zentral](https://github.com/zentralopensource/zentral/commit/2de7bd13e6dacb816d204d18bde7ba55553befe6) | `2de7bd1` | 2 | 0 | 0 | 0 |
| [scoursen/django-softdelete](https://github.com/scoursen/django-softdelete/commit/5d4bde14ae1b0402a7436c57a66001ea8553fa9b) | `5d4bde1` | 97 | 0 | 0 | 0 |
| [javifalces/HFTFramework](https://github.com/javifalces/HFTFramework/commit/3d81f42eb31bd7a361df585b613a915b481108a3) | `3d81f42` | 1 | 0 | 0 | 0 |
| [NatLabRockies/REopt_API](https://github.com/NatLabRockies/REopt_API/commit/13436f2faa3ff2673e3d97205c75f0ab847c1121) | `13436f2` | 46 | 92 | 0 | 24 |
| [sstsimulator/sst-elements](https://github.com/sstsimulator/sst-elements/commit/6685f3a501dfa6cf2d424deb509f2f319db3142d) | `6685f3a` | 18 | 0 | 0 | 0 |
| [ultimate-pa/hanfor](https://github.com/ultimate-pa/hanfor/commit/cbb0dbcc9520c8366bcb6b4f429d452bc7e686b2) | `cbb0dbc` | 4 | 0 | 0 | 0 |
| [odin-detector/odin-data](https://github.com/odin-detector/odin-data/commit/40cb73aabff5436a2e2a303e33b8ba7d64a6362d) | `40cb73a` | 28 | 0 | 0 | 0 |
| [cnobile2012/dcolumn](https://github.com/cnobile2012/dcolumn/commit/cfcc7fe0077e9bd92288c94c3b3f5a81cfb2c139) | `cfcc7fe` | 40 | 0 | 0 | 0 |
| [AlexsLemonade/scpca-portal](https://github.com/AlexsLemonade/scpca-portal/commit/dec6b75e7c3acca6126d672ba29cc49107622639) | `dec6b75` | 1 | 0 | 0 | 0 |
| [ruebeckscube/localmusic](https://github.com/ruebeckscube/localmusic/commit/5133fc50fe1553d9e49dbd22c52f3c044af4032c) | `5133fc5` | 13 | 0 | 0 | 0 |
| [openimis/openimis-be-policy_py](https://github.com/openimis/openimis-be-policy_py/commit/131848bcab1889ec2240a8b83eff8854683e96f2) | `131848b` | 32 | 1 | 0 | 0 |
| **Total** | | **288** | **124** | **0** | **101** |

One commit per project; where a project migrated over several commits, the
first is replayed, so no call is counted twice.

- **0 calls the maintainers renamed were missed.**
- **Every edit only PatchAhead made is on `self` in a test class:** 93 were
  later made the same way on the projects' default branches, and 31 are calls
  essentia still makes to the removed aliases.
- **What nobody renamed:** 24 lines of commented-out code in REopt_API (not
  calls), and essentia's 77 `self.assert_(...)` calls. Essentia's test base
  class defines its own `assert_` shim, so PatchAhead leaves them: a call to a
  method the project defines is a call to the project's code. Replaying
  essentia is how that rule came to cover a test base class in another module,
  not just the calling one.

## What this does and does not show

- **It measures editing, not verification.** The projects' tests were not run:
  installing ten projects' dependencies is out of scope. The five checks and
  red-to-green evidence apply when PatchAhead runs in a project's own
  environment.
- **Two upgrades, 22 projects.** Chosen because the migrations were
  single-purpose commits. It is a sample, not a census.
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
# Study 1
patchahead api-diff pydantic 1.10.13 2.0 --out changes.json
python evals/realworld/replay.py --changes changes.json

# Study 2: the unittest section of CPython's Doc/whatsnew/3.12.rst
python evals/realworld/replay.py --changes unittest-3.12.rst \
    --cases evals/realworld/cases_unittest.json
```

It needs network access and `git`. The cases are in
[`evals/realworld/cases.json`](../evals/realworld/cases.json); `--json` prints
every disagreement with its reason. Replaying the same commits gives the same
numbers.
