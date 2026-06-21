# Orders API — v2.0.0 Release Notes

## Breaking changes

### Pagination is now cursor-based

The Orders endpoint no longer uses page-based pagination.

- **Before (v1):** `GET /orders?page=1` returned `page` and `total_pages`.
- **After (v2):** `GET /orders?cursor=<cursor>` returns `next_cursor` and `has_more`.
- The `page` and `total_pages` response fields have been **removed**.
- **Migration:** iterate using `cursor` / `next_cursor` and stop when `has_more` is `false`.

> Risk: HIGH — integrations that read `total_pages` will raise `KeyError`
> and may silently sync incomplete data.

## Non-breaking changes

- Added `created_at` to each order object.
- Improved rate-limit headers.
