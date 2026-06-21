"""Upstream Orders API -- v1 (page-based pagination).

Simulates a third-party Orders API. v1 returns results in fixed-size
pages indexed by an integer ``page``, and reports how many pages exist
via ``total_pages``. This is the contract the downstream app was built
against. It is fully in-memory and deterministic (no network).
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


class OrdersAPIClientV1:
    """Page-based Orders API client (v1)."""

    version = "v1"

    def get_orders(self, page: int = 1, **_ignored):
        total = len(_ORDERS)
        total_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
        start = (page - 1) * _PAGE_SIZE
        end = start + _PAGE_SIZE
        return {
            "orders": _ORDERS[start:end],
            "page": page,
            "total_pages": total_pages,
        }
