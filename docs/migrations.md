# Supported migrations

PatchAhead v1 performs four migration families. Anything else is reported as
unsupported rather than attempted.

A family is only included when it has **all six**: change-document parsing, AST
impact analysis, planning, patch generation, validation, and tests. That bar is
why there are four and not twelve.

Run `patchahead handlers` for the same information from the tool itself.

---

## `field_rename`

A data field accessed by subscript, `.get()`, or attribute.

```diff
-    return sum(order["total"] for order in orders)
+    return sum(order["amount"] for order in orders)
```

**Recognized constructs**

| Construct | Rewritten |
|---|---|
| `order["total"]` | yes |
| `order.get("total")` / `.get("total", 0)` | yes |
| `order.total` | only when the receiver matches the owner the document names |
| `order[key]` (variable key) | no — reported |
| `LABEL = "total"` (bare string) | **not a finding at all** |

That last row is the important one. A string literal that is not in a subscript
or `.get()` position is not a field access, so it is never a candidate — not
"filtered out later", never seen.

**Confidence grading**

When the change document *asserts* an owner — "the field on each `order` object
was renamed" — only that receiver is patched:

| Site (owner asserted as `order`) | Confidence | Patched by default |
|---|---|---|
| `order["total"]` | high | yes |
| `self.order["total"]` | high | yes |
| `customer["total"]` | low | no — reported |
| `o["total"]` in `for o in orders` | low | no — reported |
| `df.total` | low | no — reported |

`order["total"]` and `customer["total"]` are different fields that happen to
share a name. Rewriting both is the corruption this handler exists to prevent,
so a receiver that is not the asserted owner is reported and left alone.

**The cost is real and accepted.** `for o in orders: o["total"]` is not migrated
automatically, because showing that `o` is an `order` needs type inference
PatchAhead does not do. A missed site is fixable by hand; a wrong edit in
unrelated code may not be noticed at all.

When the document asserts **no** owner, there is nothing to check against:

| Site (no owner asserted) | Confidence | Patched by default |
|---|---|---|
| `anything["total"]` | medium | yes |
| `anything.get("total")` | medium | yes |
| `anything.total` | low | no — reported |

A constant string key matching a renamed field is decent evidence on its own,
because that is how API responses arrive in Python. A bare attribute is not:
`.total` collides across unrelated libraries.

### Asserted versus inferred owners

Not every receiver in a document is a claim about ownership:

| Phrasing | Owner | Asserted? |
|---|---|---|
| "the field on each `order` object was renamed" | `order` | yes |
| `` `client.fetch_orders` -> `client.list_orders` `` | `client` | yes |
| "**Before:** `client.fetch_orders(limit=10)`" | `client` | no |
| a structured document's `"owner"` field | as given | yes |

The third row matters. `client` there is the *vendor's* example variable name,
not a statement about what your repository calls its client — so treating it as
a constraint would refuse to migrate `api_client.fetch_orders()`, which is the
same SDK call. An inferred owner raises confidence when it matches and lowers it
when it does not; it never vetoes.

**Does not**

- Rename keys that are not constant strings.
- Follow the field through assignment: `t = order["total"]` is renamed, a later
  use of `t` is not traced.
- Rename the field anywhere it is *produced* rather than consumed, if that code
  does not use one of the recognized constructs.

**Quote style is preserved.** `'total'` becomes `'amount'`, not `"amount"`.

---

## `method_rename`

A method or function called on an upstream client.

```diff
-    return client.fetch_orders(limit=10, timeout=5)
+    return client.list_orders(limit=10, timeout=5)
```

Only the callee name token is replaced, so arguments and formatting are
untouched.

**Confidence grading** follows the same asserted/inferred rule as
`field_rename` above:

| Site (receiver asserted as `client`) | Confidence | Patched by default |
|---|---|---|
| `client.fetch_orders()` | high | yes |
| `self.client.fetch_orders()` | high | yes |
| `analytics.fetch_orders()` | low | no — reported |

**Does not**

- Rewrite a bare reference (`callback = client.fetch_orders`) — reported, not
  patched, because passing it somewhere that expects the old name is a real
  possibility.
- Rename the *definition*. This family is for calls into an upstream SDK.
- Touch calls in a module that defines a function with the same name locally —
  renaming those would break the repository rather than migrate it.

---

## `kwarg_rename`

A keyword argument at call sites.

```diff
-    return client.fetch(limit=limit, timeout_seconds=DEFAULT)
+    return client.fetch(limit=limit, timeout=DEFAULT)
```

The most mechanically reliable of the four: `ast` hands back the exact range of
the name token, and only that token changes. The value expression is untouched,
so this migration cannot change *what* is passed, only what it is called.

**Scoping.** When the change document names the function, only calls to that
function are rewritten:

```python
client.fetch_orders(timeout_seconds=5)   # renamed   (high)
socket.connect(timeout_seconds=9)        # left alone (low, reported)
```

When the document names no function, every call using the keyword is a medium
candidate. Run `analyze` first to see the list.

**Does not**

- See values passed positionally or expanded from `**kwargs`.
- Rename the parameter in a function definition.

---

## `pagination_page_to_cursor`

Page-based pagination rewritten to cursor-based.

This one recognizes a *shape* and rewrites four spans inside it:

```python
page = 1                                     # (A) initializer
while True:                                  # (B) unconditional loop
    response = client.get_orders(page=page)  # (C) call passing the page
    ...
    if page >= response["total_pages"]:      # (D) guard on the page count
        break
    ...
    page += 1                                # (E) advance
```

becomes

```python
cursor = None                                     # (A)
while True:                                       # (B) untouched
    response = client.get_orders(cursor=cursor)   # (C)
    ...
    if not response.get("has_more"):              # (D)
        break
    ...
    cursor = response.get("next_cursor")          # (E)
```

Statements between the numbered lines are not touched. A 40-line sync function
produces a four-line diff, with its name, signature, docstring, accumulator,
response keys, and logging exactly as written.

**Accepted variations**

- Any variable names (`page`/`p`, `response`/`r`).
- `page += 1` or `page = page + 1`.
- `response["total_pages"]` or `response.get("total_pages")`.
- Comparison direction: `>=`, `>`, `==`.
- Non-default field names, via a structured change document (below) — e.g.
  `pageCount` / `hasMore` / `nextAfter`.
- A name collision: if `cursor` is already bound in the function, a
  non-colliding name is chosen.

**Refused, with a stated reason**

- `while page <= total_pages:` — the condition is in the `while`, not a guarded
  `break`.
- A loop in a nested function belongs to *that* function. It is migrated as
  part of it, once, not also as part of the enclosing one.
- Any of (A)–(E) missing.
- **The page variable used anywhere else in the function** — logged, returned,
  stored. Replacing it with a cursor could change behavior, so the loop is left
  for a human.
- Recursive pagers and generator-based pagers.
- A `total_pages` read that is not inside a recognized loop — reported, not
  patched, since rewriting it in isolation would need the surrounding logic to
  change too.

These are exactly the cases `--use-llm` exists for.

**Assumes** the new API exposes `has_more` and `next_cursor` (configurable). It
cannot verify that from your code — your tests do.

---

## Not supported in v1

| Change | Status |
|---|---|
| Response-shape changes (nesting, list→object) | recognized, reported as `unsupported` |
| Endpoint moves | recognized, reported as `unsupported` |
| Authentication changes | recognized, reported as `unsupported` |
| Rate-limit behavior changes | recognized, reported as `unsupported` |
| Anything unclassifiable | reported as `unknown` |

"Recognized, reported as unsupported" is deliberate and better than the
alternative: PatchAhead says *this is an endpoint move, which v1 cannot
migrate*, instead of misclassifying it as something it can.

---

## Structured change documents

When release-note prose is too vague — or you are generating changes from a spec
diff — state the change explicitly. JSON or YAML:

```json
{
  "title": "Pagination is now cursor-based",
  "kind": "pagination_page_to_cursor",
  "migration_hint": "Iterate with cursor/next_cursor; stop when has_more is false.",
  "severity": "high",
  "target": {"symbol": "page", "replacement": "cursor", "owner": "get_orders"},
  "pagination": {
    "page_param": "page",
    "total_pages_key": "pageCount",
    "cursor_param": "after",
    "next_cursor_key": "nextAfter",
    "has_more_key": "hasMore"
  }
}
```

Several changes in one file:

```json
{"changes": [{"title": "...", "kind": "field_rename", "...": "..."}]}
```

Structured input defaults to `confidence: high` — the uncertainty in the
Markdown path is entirely about reading English, and that uncertainty is absent
here. Unknown `kind` values and missing required fields are hard errors.

YAML needs `pip install 'patchahead[yaml]'`; JSON always works.

---

## Adding a family

See [contributing.md](contributing.md). The short version: one handler class,
one `ChangeKind` member, one import. The core engine does not change.
