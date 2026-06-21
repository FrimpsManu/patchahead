"""Upstream Orders API -- v2 (cursor-based pagination).

BREAKING CHANGE vs v1: pagination moved from integer ``page`` +
``total_pages`` to opaque ``cursor`` + ``next_cursor`` + ``has_more``.
The ``page`` and ``total_pages`` response fields are GONE.

Crucially, ``get_orders`` still ACCEPTS a ``page`` kwarg, so old callers
do not get a loud ``TypeError`` -- they get something quieter and worse:
a response with no ``total_pages``, which raises ``KeyError`` and (in
sloppier code) can silently sync incomplete data. This is exactly the
class of "semantic" breakage Dependabot can't see.
"""

_ORDERS = [
    {"id": 1, "customer": "Ada", "total": 42.00},
    {"id": 2, "customer": "Linus", "total": 13.50},
    {"id": 3, "customer": "Grace", "total": 88.10},
    {"id": 4, "customer": "Alan", "total": 27.75},
    {"id": 5, "customer": "Edsger", "total": 64.20},
    {"id": 6, "customer": "Margaret", "total": 9.99},
]

_PAGE_SIZE = 2
# Opaque cursor -> start index into the dataset.
_CURSOR_START = {None: 0, "c1": 2, "c2": 4}


class OrdersAPIClientV2:
    """Cursor-based Orders API client (v2)."""

    version = "v2"

    def get_orders(self, cursor=None, page=None, **_ignored):
        start = _CURSOR_START.get(cursor, 0)
        end = start + _PAGE_SIZE
        chunk = _ORDERS[start:end]
        next_start = end
        has_more = next_start < len(_ORDERS)
        next_cursor = None
        if has_more:
            next_cursor = "c1" if next_start == 2 else "c2"
        return {
            "orders": chunk,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }
