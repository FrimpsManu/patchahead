# orders-service (PatchAhead example repository)

A small, self-contained Python service that integrates with a third-party
**Orders API**. It exists so you can run PatchAhead against a *separate*
repository without writing one first.

This repo is **intentionally broken**. The vendored upstream client in
`upstream/` is the new (v2) version of the Orders API; the integration code in
`app/` is still written against the old (v1) contract. So:

```console
$ cd examples/orders-service
$ python -m pytest
...
FAILED tests/test_order_sync.py::test_sync_all_orders - KeyError: 'total_pages'
FAILED tests/test_order_report.py::test_total_revenue - KeyError: 'total'
FAILED tests/test_client.py::test_fetch_recent - TypeError: ... 'timeout_seconds'
3 failed
```

That is the situation PatchAhead is for. Each failure corresponds to one
breaking change described in `../changes/`:

| Change document | Breaking change | Breaks |
|---|---|---|
| `../changes/pagination-cursor.md` | page-based -> cursor-based pagination | `app/order_sync.py` |
| `../changes/field-rename.md` | order field `total` -> `amount` | `app/order_report.py` |
| `../changes/kwarg-rename.md` | `timeout_seconds=` -> `timeout=` | `app/client.py` |
| `../changes/method-rename.md` | `fetch_orders()` -> `list_orders()` | `app/client.py` |
| `../changes/pagination-cursor.json` | the same pagination change, as structured JSON | `app/order_sync.py` |

From the PatchAhead repo root:

```bash
patchahead analyze --repo examples/orders-service --change examples/changes/pagination-cursor.md
patchahead migrate --repo examples/orders-service --change examples/changes/pagination-cursor.md
```

`migrate` copies this repo into a temporary workspace, patches the copy, runs the
tests there, and prints a diff. This directory is left untouched.

`upstream/api_v1.py` is kept only as documentation of the contract the `app/`
code was originally written against. Nothing imports it.
