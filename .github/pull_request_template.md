## What this changes


## Why


## Checklist

- [ ] `python -m pytest` passes
- [ ] `python evals/run.py` passes (and no threshold was lowered)
- [ ] `ruff check src tests evals` passes
- [ ] `CHANGELOG.md` updated under `[Unreleased]` if behavior changed
- [ ] `docs/migrations.md` updated if a migration family was added or changed

## If this adds or changes a migration family

- [ ] Tests cover a false positive it must **not** patch
- [ ] Tests cover a shape it refuses, asserting the stated reason
- [ ] An eval case was added to `evals/datasets/`
- [ ] Its `limitations` are documented on the handler and in `docs/migrations.md`
