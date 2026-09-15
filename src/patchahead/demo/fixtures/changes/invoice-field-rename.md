# Billing API — v3.0.0 Release Notes

A release note from a *different* upstream service than the rest of the
documents here. It is bundled so the demo can show what PatchAhead does when a
change looks applicable and is not.

`orders-service` reads `order["total"]`. This document is about `invoice`
objects. The field name is identical; the object is not, and no amount of
string matching can tell the difference.

## Breaking changes

### Invoice field renamed: `total` → `amount`

The monetary field on each `invoice` object was renamed.

- **Before:** each invoice object had a `total` field.
- **After:** the field is now named `amount`. `total` has been **removed**.
- **Migration:** read `amount` instead of `total` on invoices.

> Risk: HIGH — billing code reading `invoice["total"]` will raise `KeyError`.
