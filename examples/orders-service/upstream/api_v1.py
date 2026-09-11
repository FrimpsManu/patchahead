"""Vendored Orders API client -- v1.

Kept purely as documentation of the contract `app/` was originally written
against. Nothing imports this module; the service depends on ``api_v2``.
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


class OrdersAPIClient:
    """Page-based Orders API client (v1)."""

    version = "v1"

    def get_orders(self, page: int = 1, **_ignored):
        total_pages = (len(_ORDERS) + _PAGE_SIZE - 1) // _PAGE_SIZE
        start = (page - 1) * _PAGE_SIZE
        return {
            "orders": _ORDERS[start : start + _PAGE_SIZE],
            "page": page,
            "total_pages": total_pages,
        }

    def fetch_orders(self, limit=3, timeout_seconds=30.0):
        return _ORDERS[:limit]
