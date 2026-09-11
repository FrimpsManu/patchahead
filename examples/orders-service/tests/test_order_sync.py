"""Integration test for the downstream order-sync against the live upstream."""

from app import order_sync
from upstream.api_v2 import OrdersAPIClient


def test_sync_all_orders():
    orders = order_sync.sync_all_orders(OrdersAPIClient())

    assert len(orders) == 6, f"expected 6 orders, got {len(orders)}"
    assert sorted(o["id"] for o in orders) == [1, 2, 3, 4, 5, 6]
