# Orders SDK — v2.0.0 Release Notes

## Breaking changes

### Keyword argument renamed: `timeout_seconds` → `timeout`

The `timeout_seconds` keyword argument on `fetch_orders` was renamed to `timeout`.
The unit is unchanged (seconds); only the parameter name changed.

- **Before:** `client.fetch_orders(limit=10, timeout_seconds=30)`
- **After:** `client.fetch_orders(limit=10, timeout=30)`
- **Migration:** rename the `timeout_seconds=` keyword argument to `timeout=`.

> Risk: MEDIUM — calls raise `TypeError: unexpected keyword argument`.
