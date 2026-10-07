# Using PatchAhead

The README covers what PatchAhead is and how to try it. This page is the
reference: every command, the configuration, the exit codes, the optional AI
mode, and the web UI.

## Install

```bash
pip install patchahead                 # core tool: no third-party runtime deps on 3.11+
pip install 'patchahead[demo]'         # + the local UI and the bundled walkthrough
pip install 'patchahead[llm]'          # + AI proposals when a code shape is unrecognized
pip install 'patchahead[all]'          # everything
```

To work on PatchAhead itself, install from a clone instead:
[contributing.md](contributing.md).

Python 3.10 or newer.

| Extra | Brings | For |
|---|---|---|
| `web` | FastAPI, uvicorn | `patchahead web` against your own repository |
| `demo` | `web` + pytest | `patchahead demo`; the bundled repository's tests have to run, or nothing can be verified |
| `llm` | `anthropic` | the AI fallback, off by default |
| `yaml` | PyYAML | YAML change documents; JSON needs nothing |
| `sentry` | `sentry-sdk` | optional error reporting |

## Commands

```console
$ patchahead analyze  --repo ./my-service --change ./release-notes.md
$ patchahead migrate  --repo ./my-service --change ./release-notes.md
$ patchahead migrate  --repo ./my-service --change ./release-notes.md --dry-run
$ patchahead demo
$ patchahead web      --repo ./my-service --changes ./changes
$ patchahead handlers
```

| Command | What it does |
|---|---|
| `analyze` | Reads the change document and reports every affected site. Changes nothing. |
| `migrate` | Analyzes, plans, patches a temporary copy, and runs your tests against it. Your repository is never written to. |
| `migrate --dry-run` | Stops after planning: shows what it would change and why. |
| `demo` | Serves the local UI against a bundled example service with six scenarios. |
| `web` | The same UI pointed at your own repository. |
| `handlers` | Lists the migration families and what each one refuses to do. |
| `api-diff` | Compares two versions of a library and writes the breaking changes as a change document. |

Useful `migrate` options:

| Option | Effect |
|---|---|
| `--json` | Machine-readable output of the whole run |
| `--no-tests` | Skip the test run; the result is reported as unverified |
| `--test-command CMD` | Override the configured test command |
| `--min-confidence LEVEL` | Lowest confidence that is patched (`low`, `medium`, `high`) |
| `--pr-summary PATH` | Write a Markdown pull-request description |
| `--output-dir DIR` / `--no-artifacts` | Where to write the diff, plan and result, or don't |
| `--keep-workspace` | Leave the temporary copy on disk to inspect |
| `--use-llm` | Allow the AI fallback (see below) |
| `--require-complete` | Exit 1 while any code or test still uses an old name (see below) |

The bundled example service ships inside the package. `patchahead demo
--print-paths` prints where it is, so you can run the CLI against it:

```bash
repo=$(patchahead demo --print-paths | awk '/^repository/ {print $2}')
changes=$(patchahead demo --print-paths | awk '/^changes/ {print $2}')
patchahead migrate --repo "$repo" --change "$changes/pagination-cursor.md"
```

## Change documents

PatchAhead reads release notes in Markdown, reStructuredText, or plain text:
headings, bullet lists, tables, and the usual phrasings ("renamed to", "is
now", "deprecated in favor of", "use X instead of Y"). Each bullet or table row
is read as its own change.

When the prose is too vague, write the change as JSON (or YAML with the `yaml`
extra). See [migrations.md](migrations.md#structured-change-documents).

## Comparing two versions of a library

When there is no usable release note, read the breaking changes out of the
library itself:

```bash
patchahead api-diff storekit 4.9.0 5.0.0 --out changes.json      # from PyPI
patchahead api-diff --old ./storekit-4.9 --new ./storekit-5.0 --out changes.json
patchahead migrate --repo ./my-service --change changes.json
```

`--old`/`--new` take a directory or a `.whl` file. `--json` prints every
change, including the ones only reported. `--out` writes only the changes
PatchAhead can migrate; review it before passing it to `migrate`.

| What changed | Read as |
|---|---|
| Old method gone; exactly one new sibling accepts the same calls | a rename |
| Old method newly deprecated in favor of a sibling it names, which accepts every call it did | a rename |
| A keyword parameter replaced in the same slot by one with a related name | a keyword rename |
| Same name in another module | reported: a move |
| Deprecated in favor of something with different arguments | reported |
| Removed, several look-alikes, a new required parameter | reported |

Nothing is installed or run. PyPI versions are downloaded as wheels from the
index's JSON API (set `PATCHAHEAD_PYPI_URL` for a mirror that serves the same
API) and checked against their published SHA-256. A release with only a source
distribution is refused, because building one runs the package's code. A
compiled library with no Python source has nothing to read.

## Comparing two versions of an OpenAPI spec

For a web API, the spec says what changed:

```bash
patchahead openapi-diff shop-v1.yaml shop-v2.yaml --out changes.json
patchahead migrate --repo ./my-service --change changes.json
```

It reads OpenAPI 3.0 and 3.1 (`components.schemas`) and Swagger 2.0
(`definitions`), as JSON, or as YAML with `pip install 'patchahead[yaml]'`.
References within the file are followed; references to other files are not.

| What changed | Read as |
|---|---|
| A property deprecated, its description naming exactly one other property of the same type | a field rename (high confidence) |
| A property gone, exactly one property of the same type added to the same schema | a field rename (medium) |
| The same endpoint's `operationId` changed | a method rename, for a generated client (`listOrders` -> `list_orders`) |
| A query parameter gone, exactly one of the same type added | a query parameter rename, owned by its endpoint (`GET [/v2]/orders`) |
| An operationId at a new path with the same verb, only fixed words changed | an endpoint move (`/pages/deployment` -> `/pages/deployments`) |
| Several same-type candidates, a different type, a property moved to another schema | reported |
| A header or cookie parameter renamed, a parameter removed, a new required parameter, an endpoint removed, or moved with other placeholders | reported |

A field rename's owner is the schema: `Order.total` renamed to `amount`
rewrites `order["total"]`, `self.order["total"]` and `current["total"]` after
`current = order`, and never `customer["total"]`. A query parameter rename is
applied only to calls whose verb and URL address its endpoint (see
`docs/migrations.md`).

For code using a client generated from the spec, a camelCase property's
attribute is renamed too: `totalAmount` -> `grandTotal` also renames
`order.total_amount` to `order.grand_total`, on the same schema. The names
follow what generators produce (checked against openapi-python-client:
`totalAmount` -> `total_amount`, `orderID` -> `order_id`, `listOrders` ->
`list_orders`). Generators disagree about digits (`lineItems2` is
`line_items_2` to some and `line_items2` to others), so a name with a digit
beside a letter gets no attribute rename.

## GitHub Action

```yaml
on: pull_request
permissions:
  contents: read
  pull-requests: write
jobs:
  patchahead:
    if: github.actor == 'dependabot[bot]'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - id: patchahead
        uses: FrimpsManu/patchahead@v0.8.2
        with:
          from-pull-request: true
          install-command: pip install -r requirements-dev.txt
          comment: true
```

| Input | Default | What it does |
|---|---|---|
| `from-pull-request` | `false` | Read the triggering pull request: its release notes, and the versions it bumps |
| `compare-versions` | `true` | Compare the old and new version of each bumped package, and drop release-note readings the new version contradicts |
| `change` | | A change document to migrate, instead of or as well as the pull request |
| `openapi-spec` | | OpenAPI spec files in the repository; each one a pull request changed is compared with its base-branch version |
| `repo` | `.` | The repository to migrate, relative to the checkout |
| `install-command` | | How to install your project and its test dependencies |
| `test-command` | | Overrides the configured test command |
| `python-version` | `3.12` | Python for PatchAhead and your tests |
| `require-complete` | `false` | Count it as not migrated while code or tests still use an old name |
| `comment` | `false` | Post the summary on the pull request, updated in place on re-runs |
| `apply` | `false` | Write a verified patch into the checked-out files for a later step; never commits |
| `open-pull-request` | `false` | Open a verified patch as a pull request; see below |
| `github-token` | `github.token` | Token for the comment and the pull request |
| `fail-on` | `error` | `never`, `error` (it could not run), or `not-migrated` |

Outputs: `outcome`, `succeeded`, `complete`, `exit-code`, `diff` (a file),
`summary` (a file), and `pull-request-url`.

### When a pull request updates an OpenAPI spec

Keep a copy of an API's spec in the repository and name it in `openapi-spec`.
When a pull request changes it, PatchAhead compares the base branch's version
with the pull request's, the way `openapi-diff` does, and migrates the code in
the same run:

```yaml
on:
  pull_request:
    paths: [openapi.yaml]
...
      - uses: FrimpsManu/patchahead@v0.8.2
        with:
          openapi-spec: openapi.yaml
          install-command: pip install -r requirements-dev.txt
          open-pull-request: true
```

Several specs go one per line, or separated by commas, relative to `repo`. A
spec the pull request did not change adds nothing; one that is new in the pull
request has no earlier version to compare, and the summary says so. The base
commit is fetched if the checkout does not have it. YAML specs work without any
extra setup in the Action.

### Opening the fix as a pull request

```yaml
permissions:
  contents: write
  pull-requests: write
...
      - uses: FrimpsManu/patchahead@v0.8.2
        with:
          from-pull-request: true
          install-command: pip install -r requirements-dev.txt
          open-pull-request: true
```

When the run ends in a verified, complete migration (outcome `migrated`, exit
code 0), PatchAhead commits the patch to the branch `patchahead/<target>` and
opens a pull request into `<target>`:

- **On a pull request**, the target is that pull request's branch. Merging the
  fix's pull request adds the fix to the bump. The bump's own branch is never
  pushed to; Dependabot would rebase the commit away.
- **On a push or a manual run**, the target is the branch the run was on.

A re-run updates the same branch and pull request. Anything short of a verified
migration opens nothing. It also opens nothing, with the reason in the log, when:

- the pull request comes from a fork, since the fix cannot be pushed there;
- the patch does not apply to the target branch;
- someone else has pushed to `patchahead/<target>`. PatchAhead does not
  overwrite commits that are not its own.

The commit is made in a separate git worktree, so the checkout is left as it
was for later steps. Two settings are needed: the permissions above, and
**Allow GitHub Actions to create and approve pull requests** in Settings ->
Actions -> General. Without them the step fails and says which is missing.

Pull requests opened with the default `GITHUB_TOKEN` do not start other
workflows, so your CI will not run on the fix by itself. Pass a personal access
token or a GitHub App token as `github-token` if it should.

**Dependabot.** A workflow Dependabot triggers gets a read-only `GITHUB_TOKEN`,
so `comment: true` needs `pull-requests: write` in `permissions`, and
`open-pull-request: true` needs `contents: write` as well. It also sees
Dependabot secrets rather than Actions secrets, so `--use-llm` is not part of
the action.

## What is left after a migration

Passing tests show that the code they run works, not that the migration is
finished. After patching, PatchAhead searches the patched copy for every place
the old name still appears, and sorts what it finds:

| Kind | Example | Counts as unfinished |
|---|---|---|
| `code` | `from sdk import fetch_orders`; `{"total": 5}` sent to an API that renamed the field | yes |
| `dynamic` | `getattr(client, "fetch_orders")`, which fails only at runtime | yes |
| `test` | `client.fetch_orders.return_value = []` | yes |
| `other_object` | `customer["total"]`, or `{"customer": {"total": 250}}`, when the note was about `order` | no, left on purpose |
| `string`, `comment`, `config`, `docs` | a label, a comment, `settings.yaml`, a README | no, worth a look |

A dictionary literal with the old key is the owner's unless the code names it
for something else: the value of a `"customer"` key, assigned to `customer`, or
passed as `customer=`. One named for nothing stays unfinished, and when a line
holds both, the unfinished one is what the line reports.

The terminal lists the unfinished ones and counts the rest (`-v` lists them
too); `--json` and the pull-request summary carry all of them. With
`--require-complete`, the run exits 1 while anything unfinished remains.

## Configuration

Optional. Put it in your repository's `pyproject.toml`, or in a
`.patchahead.toml`:

```toml
[tool.patchahead]
test_command = "python -m pytest"   # what verifies a migration
source_dirs = ["src"]               # where to look (tests are still found repo-wide)
exclude = ["vendor"]                # added to the built-in exclusions
max_changed_files = 10              # scope check limit
max_diff_lines = 400                # scope check limit
min_confidence = "medium"           # findings below this are reported, not patched
allow_llm = true                    # false forbids --use-llm for this repository
migrate_tests = true                # also migrate tests that use the old API
output_dir = ".patchahead"          # where the diff, plan, and result are written
test_timeout_seconds = 300          # a test run longer than this is stopped
max_workspace_files = 20000         # refuse to copy a larger repository
```

An unknown key is an error, not a silent no-op.

## Exit codes

Meant for CI: exit 0 is a claim a pipeline can act on.

| Code | Meaning |
|---|---|
| 0 | Analysis ran; or the migration was verified by a test going from failing to passing; or a dry run completed; or there was nothing to migrate and the tests pass; or `--no-tests` was passed, so the patch is unverified by request |
| 1 | Not migrated: a check failed, no plan was possible, the tests ran (or could not start) without proving the patch, nothing was found while the tests were already failing, or `--require-complete` was passed and code or tests still use an old name |
| 2 | Usage error: bad arguments, missing file, unreadable configuration |
| 3 | The change is real but this version cannot migrate it |
| 4 | Interrupted |

## AI mode

Off by default. `--use-llm` is used in exactly one situation: a migration
handler found real impact but refused to plan because the code shape was
unfamiliar.

- **What the model sees:** the breaking change, the failing test output, and
  only the functions the findings point at. Never whole files, never the
  repository.
- **What happens to its answer:** rejected, not repaired, if it is not valid
  JSON, touches a file the analysis did not flag, does not parse, renames a
  function, changes a signature or decorator, or moves a function out of its
  class. Whatever survives goes through the same five checks.
- **Credentials:** once model-written code is in the temporary copy, your tests
  run there without `ANTHROPIC_API_KEY`.
- **Opting out:** `allow_llm = false` in a repository's config forbids it, and
  `--use-llm` cannot override that.
- **Model:** `claude-opus-5-5` by default; set `PATCHAHEAD_MODEL` to use another.

```bash
export ANTHROPIC_API_KEY=...
patchahead migrate --repo ./my-service --change ./notes.md --use-llm
```

**How well it works.** `evals/llm/` holds ten repositories in which the
deterministic handlers find the affected code and refuse to plan: an order
reached through `enumerate`, `zip`, `.values()`, a lambda, or a helper's return
value, and traps where the same word is another object's field. Run three times
each with `claude-opus-5-5`, all 30 attempts were verified by the tests, and the
model never changed another object's field -- including in the two traps where
no test covers that field, so a wrong edit would have passed every gate. It is a
small benchmark, of field renames only, written for this project: evidence that
the fallback behaves, not an accuracy figure for code in general. It is not run
in CI, since it calls a model; `python evals/llm/run.py` reruns it.

## Web UI

```bash
pip install 'patchahead[demo]' && patchahead demo                 # bundled walkthrough
pip install 'patchahead[web]'  && patchahead web --repo ./my-service --changes ./changes
```

The same page in both cases: the upstream change, the impact, the plan, the
diff, the five checks, and the pull-request summary, all produced by the same
engine as the CLI. It binds to `127.0.0.1`, refuses requests from other
websites, and runs your test command. See [safety.md](safety.md).

## Development

```bash
pip install -e '.[dev]'
python -m pytest                   # the test suite
python -m pytest -m "not slow"     # skip tests that spawn a real pytest
python evals/run.py                # the evaluation benchmark
ruff check src tests evals
```

[contributing.md](contributing.md) walks through adding a migration family.
To record the demo: [demo-recording.md](demo-recording.md).
