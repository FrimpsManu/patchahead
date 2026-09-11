# Architecture assessment of the PatchAhead prototype

This is the written assessment produced *before* the v1 rework, from reading every
file, running the test suite, running both demo scenarios, running the evals, and
probing the pipeline against repositories other than the bundled demo.

## 1. What was structurally sound (and was kept)

The prototype had a genuinely good **idea of a pipeline**, and that shape survives:

- `changelog -> BreakingChange -> ImpactReport -> Patch -> TestRun -> reviewable summary`
- Typed artifacts between stages rather than free-form strings.
- A real unified diff as the output artifact.
- "Tests decide, not the model" as the acceptance criterion.
- Deterministic-first, LLM-optional, with the LLM never being the authority.
- Stage-level instrumentation (spans) around every step.
- A red -> green demo that actually demonstrates the value proposition.

These are the load-bearing ideas of the product and they were preserved. The rework
is about making them true for arbitrary repositories rather than for one fixture.

## 2. Verified defects

Each of these was confirmed by execution, not by reading.

### 2.1 The pagination "migration" was a hardcoded string paste (critical)

`patch_generator._CURSOR_BODY` was a literal containing the demo's function,
verbatim. `_pagination_patch` located the affected function and replaced its entire
body with that literal. Pointed at any other repository it does not migrate code, it
**overwrites it**. Probe against a 9-line file containing `fetch_invoices`:

```diff
-def fetch_invoices(api):
-    page = 1
-    out = []
+def sync_all_orders(api_client):
+    cursor = None
+    all_orders = []
     while True:
-        r = api.get_orders(page=page)
-        out.extend(r["items"])
+        response = api_client.get_orders(cursor=cursor)
+        all_orders.extend(response["orders"])
```

The user's function was renamed, its signature changed, its callers broken, and its
`"items"` response key silently replaced with `"orders"`. This is the single most
important defect: the headline migration family only worked on the fixture it was
written against.

### 2.2 Regex impact analysis produced majority false positives

`impact_analyzer` matched `["']total["']`, `\.total\b` and `get\(["']total["']` as
raw text, line by line. Probe against a file with one real hit and three unrelated
uses of the word:

```
a.py:4: LOG_TOTAL = "total"        # string constant  -> FALSE POSITIVE
a.py:6: subtotal = df.total        # pandas attribute -> FALSE POSITIVE
a.py:7: msg = "total"              # unrelated string -> FALSE POSITIVE
a.py:8: sum(o["total"] for o in orders)              -> true positive
```

All four were reported as impact, and `_rename_patch` then rewrote all four,
corrupting three of them. Confidence was computed as `0.4 + 0.15 * len(matches)`,
so *more false positives raised the confidence score*.

### 2.3 No tests of PatchAhead itself

`pyproject.toml` set `testpaths = ["demo/downstream/tests"]`. Those are the
deliberately-broken fixture tests, so `pytest` on a fresh clone was **red**:

```
FAILED demo/downstream/tests/test_order_report.py::test_total_revenue - KeyError
FAILED demo/downstream/tests/test_order_sync.py::test_sync_all_orders - KeyError
2 failed in 0.02s
```

There was not one test covering the parser, the analyzer, the patch generator, or
the agent. The only executable check was `evals/run_evals.py`, which tested exactly
one function (`classify`) against three fixtures.

### 2.4 It mutated the user's working tree in place

`patch_generator.apply()` called `Path(path).write_text(contents)` directly on the
repository. The only reason the demo was safe was `agent.reset_demo()`, which copied
a `demo/baseline/` file back over the target afterwards. That safety net exists only
for the two bundled fixtures.

### 2.5 Hardcoded assumptions and demo coupling

- `paths.py` hardcoded `ORDER_SYNC_FILE`, `TEST_PATH`, `BASELINE_FILE`, `CHANGELOG_FILE`.
- `pr_summary.TITLE` was the literal string `"Update Orders API integration for cursor-based pagination"`, used for every non-rename change.
- `changelog_parser.parse` defaulted `old_behavior`/`new_behavior`/`migration_hint` to the demo's pagination text whenever extraction failed — so a document it could not parse still produced a confident-looking pagination change.
- `impact_analyzer` defaulted `old_symbol` to `"total"` and `_rename_patch` defaulted `new_symbol` to `"amount"`.
- `test_runner` hardcoded `ORDERS_API_VERSION` and always ran from the PatchAhead repo root.
- `impact_analyzer` scanned `app_dir.glob("*.py")` — one directory, non-recursive.
- The `scenarios.py` registry required every new change type to ship a baseline file inside PatchAhead's own tree.

### 2.6 Silent failure everywhere

`llm.complete` wrapped every call in `except Exception: return None`. A bad API key,
a malformed response, and a network outage were indistinguishable from "LLM
disabled". `memory.remember` and `memory._client` did the same. There was no logging
subsystem at all — output was `print()` with ANSI escapes.

### 2.7 Unsupported or overstated README claims

| Claim | Reality |
|---|---|
| "watches upstream API/SDK changelogs and spec diffs" | Reads one local file passed by path. No watching, no spec diffs. |
| "Adding a change type is data, not new control flow" | `patch_generator.generate` was an `if/elif` on `change_type`, and each type needed a new hardcoded transform. |
| "the classifier also recognizes method renames and endpoint changes" | It classified them; nothing downstream could analyze or migrate them. |
| "Memory ... retrieves similar prior migrations" | Without `REDIS_URL` it returned a hardcoded two-item list of fictional migrations, which the demo printed as "seen before". |
| `changelog_parser` docstring: "OpenAPI diffs / SDK release notes would slot in the same way" | No ingestion abstraction existed. |

### 2.8 Accidental files committed

`` ` ``, `cursor`, `del`, `python`, `response[total_pages]`, `src/git` — six
zero-byte files created by mistyped shell commands, all tracked in git.

### 2.9 Other coupling

- Root `conftest.py` injected the demo directories onto `sys.path` for the whole repo.
- `web/server.py` called `agent.reset_demo()` before every run — the dashboard could only ever work on the bundled fixtures.
- `web/index.html` posted to `/api/run` without the `scenario` parameter, so the scenario selector did not affect the run.
- `llm.py` defaulted to a stale model id.
- `observability.init()` tagged every Sentry transaction `environment="hackathon"`.

## 3. Root cause

One sentence: **the prototype had a pipeline but no domain model of a migration**.

Because there was no representation of "what transformation are we applying, to
which syntactic construct, on what evidence", each stage had to re-derive intent
from `change_type` strings, and the only way to make a stage work was to special-case
the fixture. Impact analysis matched text instead of code; patch generation
substituted text instead of editing code; and nothing could state what it was
unable to do.

## 4. What the rework changes

1. **A domain model first** — `BreakingChange` carries an explicit `ChangeKind`
   and a `SymbolTarget`; plans, proposals, and validation results are typed objects.
2. **AST analysis instead of regex** for Python code, with graded confidence and
   evidence on every finding, and text-range edits so diffs stay minimal.
3. **Handlers as plugins** — a migration family is one class registered in one
   place, implementing `supports/analyze/plan/generate`.
4. **Isolated workspaces** — the user's tree is never written to by default.
5. **A validation engine** with five explicit gates producing a structured result.
6. **Fail closed** — an unrecognized code shape is reported as unsupported with a
   reason, rather than patched with a guess.
7. **A real CLI and a real test suite** for PatchAhead itself.

## 5. What was deliberately not done

- No rewrite of the pipeline stages that worked — the parse/analyze/patch/verify
  sequence, the artifact types, and the red->green proof are the same ideas.
- No GitHub App. No OpenAPI ingestion. No TypeScript support. These are roadmap.
- No `response_shape_change` handler. The prototype classified it; nothing could
  migrate it; a robust general implementation is not in reach for v1, so v1 reports
  it as unsupported rather than pretending.
