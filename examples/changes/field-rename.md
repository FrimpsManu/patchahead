# Orders API — v2.0.0 Release Notes (Orders schema)

## Breaking changes

### Order field renamed: `total` → `amount`

The monetary field on each `order` object was renamed.

- **Before:** each order object had a `total` field.
- **After:** the field is now named `amount`. `total` has been **removed**.
- **Migration:** read `amount` instead of `total`.

> Risk: HIGH — code reading `order["total"]` will raise `KeyError` and
> revenue numbers will silently break.
