# PatchAhead

**Fix breaking API changes before they break production.**

PatchAhead is an AI compatibility agent. It watches upstream API/SDK
changelogs and spec diffs, detects **semantic** breaking changes, maps them
to the affected downstream code, applies a **minimal** migration patch, runs
the tests, and produces a **human-reviewable PR summary** — before the change
ever reaches your build.

---

## The problem

Modern apps depend on fast-moving third-party APIs. When an upstream provider
changes the *meaning* of a response — not just a version number — your
integration breaks in ways your tooling can't see coming:

- **ChatGPT / Claude** help only *after* you already know what to ask.
- **Dependabot** bumps versions, but doesn't understand API *behavior*.
- **Incident agents** react *after* production has already broken.

PatchAhead acts *before* the break, turning a changelog into a verified
migration PR.

> Dependabot updates versions. **PatchAhead updates meaning.**

---

## What it does (golden scenario)

An upstream **Orders API** migrates pagination from page-based
(`page` / `total_pages`) to cursor-based (`cursor` / `next_cursor` /
`has_more`). A downstream e-commerce app that syncs all orders silently
breaks: it reads `total_pages`, which no longer exists.

PatchAhead:

1. **Parses** the upstream changelog into a structured breaking change.
2. **Runs** the integration test against v2 → it fails (`KeyError: 'total_pages'`).
3. **Localizes** the affected function (`sync_all_orders`) with a confidence score.
4. **Generates + applies** a minimal patch (loop body only; signature preserved).
5. **Re-runs** the test → it passes. Proof the migration works.
6. **Writes** a GitHub-style PR summary. **Never auto-merges.**

---

## Run it

```bash
pip install -r requirements.txt
python run_demo.py        # the full story, end-to-end, ~1s, offline
python evals/run_evals.py # classifier evals
```

Artifacts land in `outputs/`: `patch.diff`, `pr_summary.md`, `run_log.json`.

### Web dashboard (demo UI)

```bash
pip install fastapi uvicorn
python web/server.py        # -> http://127.0.0.1:8000
```

Press **Run migration**. The dashboard runs the real agent and shows the
pipeline (parse → test → analyze → patch → test → PR), the live red→green
test flip, the unified diff, the span waterfall, and the reviewable PR — the
same workflow as the CLI, in one screen for judges.

---

## The five questions, answered by the product (not the pitch)

| # | Question | Where it's answered |
|---|----------|--------------------|
| 1 | What changed upstream? | parsed `BreakingChange` (type, risk, before/after) |
| 2 | Where is the app affected? | `ImpactReport` (file, function, matched lines, confidence) |
| 3 | What patch was applied? | unified diff in `outputs/patch.diff` |
| 4 | How do we know it worked? | test run before (fail) vs after (pass) |
| 5 | Can a human review it? | `outputs/pr_summary.md` + auto-merge disabled |

---

## Architecture

```
changelog ──▶ changelog_parser ──▶ BreakingChange
                                        │
downstream code ──▶ impact_analyzer ──▶ ImpactReport
                                        │
                    patch_generator ──▶ PatchResult ──▶ apply
                                        │
                    test_runner (before / after) ──▶ TestRun
                                        │
                    pr_summary ──▶ reviewable PR  (human approves)
```

Every stage runs as a span inside one `patchahead.migration_run` transaction.

---

## How it works

- **Deterministic by default** so the demo is reliable and offline. Pattern
  search localizes impact; a surgical transform rewrites only the affected
  function body and emits a real unified diff.
- **Claude (optional)** — set `PATCHAHEAD_USE_LLM=1` + `ANTHROPIC_API_KEY` to
  let Claude propose the migration and explanations. The LLM *proposes*, tests
  *verify*, humans *approve*; any LLM failure falls back to the deterministic
  path automatically.

---

## Observability (Sentry)

The whole workflow is instrumented. With `SENTRY_DSN` set, spans
(`parse_changelog`, `run_tests_before`, `analyze_impact`, `generate_patch`,
`apply_patch`, `run_tests_after`, `generate_pr_summary`) report to Sentry as a
single transaction, tagged with `change_type`, `risk_level`, `tests_before`,
`tests_after`, and `fallback_used`. Without a DSN it prints console traces, so
the run is visibly instrumented either way. PatchAhead is about *preventing*
the reliability incident Sentry would otherwise catch.

---

## Safety / human-in-the-loop

PatchAhead never auto-merges. It produces a reviewable PR with upstream
evidence, affected files, the diff, test proof, a risk level, and a review
checklist. The LLM proposes; tests verify; humans approve.

---

## Memory (Redis, optional)

With `REDIS_URL` set, PatchAhead records each migrated breaking-change pattern
and retrieves similar prior migrations by type ("we've handled a
page→cursor migration before"). Degrades to an in-process seed list otherwise.

---

## Scope & honest claims

- For the hackathon we implemented one realistic breaking-change scenario:
  **page-based → cursor-based pagination.**
- The architecture generalizes to OpenAPI diffs, SDK changelogs, and
  integration-test failures (the classifier already handles field/method
  renames; see `evals/`).
- PatchAhead does not auto-merge. The LLM proposes, tests verify, humans approve.

---

## Roadmap

- Real changelog ingestion from the web (Browserbase) and OpenAPI spec diffs.
- More change types end-to-end (field rename, endpoint move, method rename).
- GitHub App that opens the PR directly on the affected repo.

## Team

Built at the UC Berkeley AI Hackathon 2026.
