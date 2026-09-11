# Contributing to PatchAhead

The full guide lives in [docs/contributing.md](docs/contributing.md), including a
worked walkthrough of adding a new migration family.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
python -m pytest                  # the suite
python -m pytest -m "not slow"    # fast subset, no subprocesses
python evals/run.py               # measured accuracy and success rate
ruff check src tests
```

## What is most useful

1. **A migration family.** The main extension point; see
   [docs/contributing.md](docs/contributing.md#adding-a-migration-family).
2. **A false positive or a false negative.** Open an issue with the smallest
   repository and change document that reproduces it. These are the highest-value
   bug reports PatchAhead can receive — impact precision is the metric that
   matters most, and it is measured by `evals/`.
3. **A release note that is parsed wrongly.** Paste it into an issue. Real-world
   phrasing is exactly what the classifier needs.

## Ground rules

- Refuse rather than guess: every "can't handle this" path states a reason.
- No silent failures.
- Typed objects across stage boundaries, never dicts.
- Never lower an eval threshold to make a build green.
