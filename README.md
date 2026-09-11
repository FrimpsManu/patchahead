# PatchAhead

**Find the downstream code an upstream API change breaks, propose a minimal
migration, and let your tests decide whether it worked.**

```console
$ patchahead migrate --repo ./my-service --change ./release-notes.md

Pagination is now cursor-based
  kind        pagination_page_to_cursor
  severity    high    confidence high

  1 finding(s) in 1 file(s), scanned 5 file(s) in 4ms
  + app/order_sync.py:12  high  sync_all_orders  while True: ... page=page ...

proposed diff
  @@ -9,16 +9,16 @@
   def sync_all_orders(api_client):
  -    page = 1
  +    cursor = None
       all_orders = []
       while True:
  -        response = api_client.get_orders(page=page)
  +        response = api_client.get_orders(cursor=cursor)
           all_orders.extend(response["orders"])
  -        if page >= response["total_pages"]:
  +        if not response.get("has_more"):
               break
  -        page += 1
  +        cursor = response.get("next_cursor")

validation
  [pass] syntax               1 modified file(s) parse as valid Python
  [pass] scope                1 file(s) changed, all named by the plan; 8 diff line(s)
  [pass] targeted_tests       tests/test_order_sync.py: 1 passed
  [pass] regression_tests     no new failures
  [pass] migration_assertion  tests that failed before the patch now pass

migrated: migrated 1 file(s); 5/5 gates passed
```

Your repository was never written to. That diff was produced in a temporary
copy, and the tests that verified it ran there.

---

## The idea

> Static evidence identifies risk. AI can propose. Tests verify. Humans approve.

Each of those is a separate stage with a separate output, and each stage can say
"I don't know":

| Stage | Produces | Can refuse |
|---|---|---|
| Ingest a change document | `BreakingChange` with graded confidence | yes — `unknown` / `unsupported` |
| Analyze the repository (AST) | `ImpactReport` with per-site confidence | yes — reports a site without patching it |
| Plan the migration | `MigrationPlan` you can read before anything changes | yes — `blocked_reason` |
| Generate a patch | `PatchProposal` + unified diff | yes — structured error |
| Validate | `ValidationResult`, five gates | it is the thing that refuses |

`MigrationResult.succeeded` is defined as "validation verified it". Nothing else
in the codebase is allowed to decide that a migration worked.

## What it is not

- **Not a general code-repair tool.** It performs four documented migration
  families (`patchahead handlers`). Everything else is reported as unsupported.
- **Not a sandbox.** It runs your repository's test command with your
  privileges. See [docs/safety.md](docs/safety.md).
- **Not a changelog monitor.** You give it a file. It does not watch registries,
  poll feeds, or open pull requests.
- **Not an autonomous agent.** It proposes; it never applies, commits, or merges.

## Install

```bash
pip install -e .            # core tool: no third-party runtime dependencies on 3.11+
pip install -e '.[llm]'     # optional: LLM proposals when the shape is unrecognized
pip install -e '.[all]'     # llm + yaml + sentry + web UI
patchahead --help
```

Python 3.10+.

## Your first migration

The repository ships a deliberately-broken example service so you can see the
whole thing work before pointing it at your own code:

```bash
# 1. What does this change break?
patchahead analyze --repo examples/orders-service --change examples/changes/pagination-cursor.md

# 2. What would you do about it? (nothing is changed)
patchahead migrate --repo examples/orders-service --change examples/changes/pagination-cursor.md --dry-run

# 3. Do it, in a temporary copy, and prove it with the tests.
patchahead migrate --repo examples/orders-service --change examples/changes/pagination-cursor.md
```

Then point it at your own repository. Release notes in Markdown work; so does a
structured JSON/YAML description if the prose is too vague
(see [docs/migrations.md](docs/migrations.md#structured-change-documents)).

## Supported change types

```console
$ patchahead handlers
```

| Family | Example | Notes |
|---|---|---|
| `field_rename` | `order["total"]` → `order["amount"]` | subscripts, `.get()`, attributes |
| `method_rename` | `client.fetch_orders()` → `client.list_orders()` | call sites only |
| `kwarg_rename` | `fetch(timeout_seconds=5)` → `fetch(timeout=5)` | explicit keyword arguments only |
| `pagination_page_to_cursor` | `page`/`total_pages` → `cursor`/`has_more` | one documented loop shape |

Each has parser support, AST impact analysis, planning, patch generation,
validation, tests, and documentation — that is the bar for inclusion, and
[docs/migrations.md](docs/migrations.md) states exactly what each one refuses to
do. v1 supports four families deliberately; breadth without correctness is
worse than nothing here.

## Safety model

1. **Your tree is never written to.** `Repository` has no `write` method.
   Patching happens in a `Workspace` — a temporary copy. That copy confines
   *PatchAhead's own writes*; it is not a sandbox, and your test command still
   runs with your privileges. [docs/safety.md](docs/safety.md) separates the
   three.
2. **Minimal edits.** Patches replace AST-derived source *ranges*, not whole
   files, so comments, formatting, and blank lines survive and diffs stay small.
3. **Fail closed.** A code shape a handler does not recognize produces a stated
   refusal, never a guess.
4. **Five gates.** syntax → scope → targeted tests → regression → migration
   assertion. `migrated` requires a test that *failed before the patch and
   passes after it*; a green-to-green run is reported `patched_unverified`.
5. **No auto-merge.** Ever. The output is a diff and a review checklist.

Full threat model, including what PatchAhead does *not* protect you from:
[docs/safety.md](docs/safety.md).

## LLM mode

Off by default. `--use-llm` is used in exactly one situation: a deterministic
handler found real impact but **refused to plan** because the code shape was
unfamiliar. A mechanical AST rename does not need a model.

What the model gets: the breaking change, the failing test output, and **only
the functions the impact findings point at**. Not the file, never the repository.

What happens to its answer: rejected — not repaired — if it is not valid JSON,
names a file the impact report does not implicate, does not parse as Python,
renames a function, or changes a signature. Whatever survives goes through the
same five gates as a deterministic patch.

```bash
export ANTHROPIC_API_KEY=...
patchahead migrate --repo ./my-service --change ./notes.md --use-llm
```

A repository can forbid this outright with `allow_llm = false`; `--use-llm`
cannot override it.

## Configuration

Optional. Sensible defaults work with no configuration at all. Put this in your
`pyproject.toml`, or in a `.patchahead.toml`:

```toml
[tool.patchahead]
test_command = "python -m pytest"   # what verifies a migration
source_dirs = ["src"]               # where to look (tests are still found repo-wide)
exclude = ["vendor"]                # added to the built-in exclusions
max_changed_files = 10              # scope gate limit
max_diff_lines = 400                # scope gate limit
min_confidence = "medium"           # findings below this are reported, not patched
allow_llm = true                    # false forbids `--use-llm` for this repository
output_dir = ".patchahead"          # where the diff, plan, and result are written
```

An unknown key is an error, not a silent no-op.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Succeeded (analysis ran; migration verified; dry run completed) |
| 1 | Impact found but not migrated — validation failed, or no plan was possible |
| 2 | Usage error — bad arguments, missing file, unreadable config |
| 3 | The change is real but unsupported by this version |

## Architecture

```
change document ─▶ ingest ─────▶ BreakingChange   (kind, target, confidence, evidence)
                                       │
repository ──────▶ AST index ──▶ ImpactReport     (site, symbol, confidence, reason)
                                       │
                              handler.plan ─────▶ MigrationPlan  (inspectable; --dry-run stops here)
                                       │
                    isolated workspace ─▶ PatchProposal (range edits → unified diff)
                                       │
                          validation ──▶ ValidationResult (5 gates)
                                       │
                                MigrationResult ─▶ diff + PR summary  (a human approves)
```

The CLI, the web UI, and the tests all call `patchahead.engine`. There is no
second code path for demos. [docs/architecture.md](docs/architecture.md).

## Web UI (optional)

```bash
pip install -e '.[web]'
python web/server.py --repo ./my-service --changes ./changes
```

A view over the same engine — change, impact, plan, diff, gates, PR summary.
Binds to localhost only; it runs your test command.

## Development

```bash
pip install -e '.[dev]'
python -m pytest            # the test suite
python -m pytest -m "not slow"   # skip tests that spawn a real pytest
python evals/run.py         # classification / impact / migration metrics
ruff check src tests
```

[docs/contributing.md](docs/contributing.md) walks through adding a migration
family — the main way to extend PatchAhead without touching the core engine.

## Limitations

Stated plainly, because a migration tool that overstates its reach is worse than
no tool:

- **Python only.** No TypeScript, Go, or anything else.
- **No type inference.** Impact analysis matches *names*. When a change document
  asserts an owner, only that receiver is patched — so `customer["total"]`
  survives an `order.total` rename, and so does `o["total"]` in
  `for o in orders`, because `o` cannot be shown to be an `order`. That is a
  deliberate false negative: unrecoverable wrong edits are worse than
  recoverable missed ones.
- **No dataflow.** `t = order["total"]` is renamed; a later use of `t` is not
  traced.
- **One pagination loop shape.** Documented in `docs/migrations.md`. Anything
  else is refused.
- **Test discovery is name-based.** `app/client.py` → `tests/test_client.py`.
  It does not trace imports, which is why the regression gate always runs the
  full suite too.
- **Release-note parsing is heuristic.** Measured, not assumed:
  `python evals/run.py` reports current accuracy. Structured JSON/YAML input
  exists for when prose is not good enough.
- **Single repository, local only.** No monorepo-aware cross-package analysis,
  no GitHub integration.

## Roadmap

Not implemented. Listed so the scope above stays unambiguous:

- OpenAPI / spec-diff ingestion
- SDK release monitoring
- A GitHub App that opens the migration PR
- TypeScript support
- More migration families (response-shape changes, endpoint moves)
- Dependency-graph-aware impact analysis
- Richer test selection (import-graph based)
- CI-native mode

## License

MIT. See [LICENSE](LICENSE).
