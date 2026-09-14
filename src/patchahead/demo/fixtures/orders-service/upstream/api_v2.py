"""Vendored Orders API client -- v2 (the version this repo now depends on).

BREAKING CHANGES vs v1, all of which the `app/` code has not been migrated to:

1. Pagination moved from integer ``page`` + ``total_pages`` to opaque
   ``cursor`` + ``next_cursor`` + ``has_more``. The ``page`` and
   ``total_pages`` response fields are GONE.

   ``get_orders`` still ACCEPTS a ``page`` kwarg, so old callers do not get a
   loud ``TypeError`` -- they get something quieter and worse: a response with
   no ``total_pages``, which raises ``KeyError`` and (in sloppier code) can
   silently sync incomplete data.

2. The order money field ``total`` was renamed to ``amount``.

3. ``fetch_orders()`` was renamed to ``list_orders()``.

4. The ``timeout_seconds=`` keyword argument was renamed to ``timeout=``.

Fully in-memory and deterministic -- no network.
"""

_ORDERS = [
    {"id": 1, "customer": "Ada", "amount": 42.00},
    {"id": 2, "customer": "Linus", "amount": 13.50},
    {"id": 3, "customer": "Grace", "amount": 88.10},
    {"id": 4, "customer": "Alan", "amount": 27.75},
    {"id": 5, "customer": "Edsger", "amount": 64.20},
    {"id": 6, "customer": "Margaret", "amount": 9.99},
]

_PAGE_SIZE = 2
# Opaque cursor -> start index into the dataset.
_CURSOR_START = {None: 0, "c1": 2, "c2": 4}


class OrdersAPIClient:
    """Cursor-based Orders API client (v2)."""

    version = "v2"

    def get_orders(self, cursor=None, page=None, **_ignored):
        start = _CURSOR_START.get(cursor, 0)
        end = start + _PAGE_SIZE
        chunk = _ORDERS[start:end]
        has_more = end < len(_ORDERS)
        next_cursor = None
        if has_more:
            next_cursor = "c1" if end == 2 else "c2"
        return {"orders": chunk, "next_cursor": next_cursor, "has_more": has_more}

    def list_orders(self, limit=3, timeout=30.0):
        """v2 name for what v1 called ``fetch_orders(..., timeout_seconds=...)``."""
        if not isinstance(timeout, (int, float)):
            raise TypeError(f"timeout must be a number, got {timeout!r}")
        return _ORDERS[:limit]
