---
name: New migration family
about: Propose a breaking-change type PatchAhead should be able to migrate
labels: migration-family
---

Before writing code, see
[docs/contributing.md](../../docs/contributing.md#adding-a-migration-family).
A family ships only with all six of: classification, AST impact analysis,
planning, patch generation, validation, and tests + docs.

**The upstream change, described as a vendor would describe it:**


**Downstream code before and after:**

```python
# before

# after

```

**How would impact analysis find it?** Which AST construct identifies a site
without needing type inference?


**What would it refuse?** Every family has cases it should decline rather than
guess at. What are this one's?


**A false positive it must avoid:** the code that looks like a site but is not.
