# Orders SDK — v2.0.0 Release Notes

Two breaking changes in this release. Both affect the same call site, so they
have to be migrated together — which is what this document demonstrates:
PatchAhead applies every change from one document into one workspace, in order.

## Breaking changes

### Client method renamed: `fetch_orders` → `list_orders`

- **Before:** `client.fetch_orders(limit=10)`
- **After:** `client.list_orders(limit=10)`
- The deprecated `fetch_orders` method has been **removed**.
- **Migration:** call `list_orders` instead of `fetch_orders`.

> Risk: MEDIUM — calls raise `AttributeError` at runtime.

### Keyword argument renamed: `timeout_seconds` → `timeout`

The `timeout_seconds` keyword argument on `list_orders` was renamed to `timeout`.
The unit is unchanged (seconds); only the parameter name changed.

- **Before:** `client.list_orders(limit=10, timeout_seconds=30)`
- **After:** `client.list_orders(limit=10, timeout=30)`
- **Migration:** rename the `timeout_seconds=` keyword argument to `timeout=`.

> Risk: MEDIUM — calls raise `TypeError: unexpected keyword argument`.

## Non-breaking changes

- Added `created_at` to each order object.
