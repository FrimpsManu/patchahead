---
name: False positive or false negative
about: PatchAhead reported code it should not have, or missed code it should have found
labels: impact-accuracy
---

Impact precision is the metric that matters most in PatchAhead, and it is
measured by `evals/`. A reproducible case here usually becomes an eval case, so
this is the highest-value bug report the project can receive.

**The change document** (the smallest version that reproduces it):

```markdown

```

**The code** (the smallest file that reproduces it):

```python

```

**What `patchahead analyze` reported:**

```console
$ patchahead analyze --repo . --change change.md

```

**What it should have reported, and why:**


**Version:** `patchahead --version`
