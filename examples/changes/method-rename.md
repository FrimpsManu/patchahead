# Orders SDK — v2.0.0 Release Notes

## Breaking changes

### Client method renamed: `fetch_orders` → `list_orders`

- **Before:** `client.fetch_orders(limit=10)`
- **After:** `client.list_orders(limit=10)`
- The deprecated `fetch_orders` method has been **removed**.
- **Migration:** call `list_orders` instead of `fetch_orders`. The signature is unchanged.

> Risk: MEDIUM — calls raise `AttributeError` at runtime.
