"""Integration test for revenue reporting.

Upstream version selected via ``ORDERS_API_VERSION`` (defaults to ``v2``):
  - v1 -> orders carry `total`  (original code passes)
  - v2 -> orders carry `amount` (original code fails: KeyError 'total')

After PatchAhead applies the field rename, the v2 run passes.
"""

import os


def _orders():
    version = os.environ.get("ORDERS_API_VERSION", "v2")
    if version == "v1":
        from upstream.api_v1 import OrdersAPIClientV1

        return OrdersAPIClientV1().get_orders(page=1)["orders"]
    from upstream.api_v2 import OrdersAPIClientV2

    return OrdersAPIClientV2().get_orders(cursor=None)["orders"]


def test_total_revenue():
    from app import order_report

    revenue = order_report.total_revenue(_orders())

    assert revenue > 0
    assert isinstance(revenue, float)
