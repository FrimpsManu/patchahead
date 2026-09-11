---
name: Release note parsed incorrectly
about: A real-world release note was classified wrongly, or its symbols were not extracted
labels: ingestion
---

Real-world phrasing is exactly what the classifier needs. Paste it verbatim --
do not tidy it up, since the tidying is often what fixes it.

**The release note, as published:**

```markdown

```

**What PatchAhead made of it** (`patchahead analyze --repo . --change notes.md`
prints a `why` line explaining the classification):

```console

```

**What it should be:**

- Change kind:
- Old symbol / new symbol:
- Owning object or function, if the note names one:

**Where it was published** (a link helps, so the phrasing can be checked against
other notes from the same vendor):
