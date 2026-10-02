# Using PatchAhead

The README covers what PatchAhead is and how to try it. This page is the
reference: every command, the configuration, the exit codes, the optional AI
mode, and the web UI.

## Install

PatchAhead is not on PyPI yet, so install it from a clone:

```bash
git clone https://github.com/FrimpsManu/patchahead
cd patchahead
pip install -e .                 # core tool: no third-party runtime deps on 3.11+
pip install -e '.[demo]'         # + the local UI and the bundled walkthrough
pip install -e '.[llm]'          # + AI proposals when a code shape is unrecognized
pip install -e '.[all]'          # everything
```

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
| 1 | Not migrated: a check failed, no plan was possible, the tests ran (or could not start) without proving the patch, or nothing was found while the tests were already failing |
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

```bash
export ANTHROPIC_API_KEY=...
patchahead migrate --repo ./my-service --change ./notes.md --use-llm
```

## Web UI

```bash
pip install -e '.[demo]' && patchahead demo                 # bundled walkthrough
pip install -e '.[web]'  && patchahead web --repo ./my-service --changes ./changes
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
