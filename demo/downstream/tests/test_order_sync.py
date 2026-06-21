"""Integration test for the downstream order-sync.

The upstream version is selected via the ``ORDERS_API_VERSION`` env var
(defaults to ``v2``), so PatchAhead can run the SAME test against the old
and new upstream contracts:

  - v1  -> page-based   (original code passes)
  - v2  -> cursor-based (original code fails: KeyError 'total_pages')

After PatchAhead migrates the code, the v2 run passes.
"""

import os


def _client():
    version = os.environ.get("ORDERS_API_VERSION", "v2")
    if version == "v1":
        from upstream.api_v1 import OrdersAPIClientV1

        return OrdersAPIClientV1()
    from upstream.api_v2 import OrdersAPIClientV2

    return OrdersAPIClientV2()


def test_sync_all_orders():
    from app import order_sync

    orders = order_sync.sync_all_orders(_client())

    assert len(orders) == 6, f"expected 6 orders, got {len(orders)}"
    assert sorted(o["id"] for o in orders) == [1, 2, 3, 4, 5, 6]
