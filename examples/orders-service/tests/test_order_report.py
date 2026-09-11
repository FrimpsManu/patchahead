"""Integration test for revenue reporting against the live upstream."""

from app import order_report
from upstream.api_v2 import OrdersAPIClient


def _orders():
    return OrdersAPIClient().get_orders(cursor=None)["orders"]


def test_total_revenue():
    assert order_report.total_revenue(_orders()) == 55.5


def test_revenue_by_customer():
    by_customer = order_report.revenue_by_customer(_orders())

    assert by_customer == {"Ada": 42.0, "Linus": 13.5}


def test_summary_label_is_not_a_data_field():
    # `TOTAL_LABEL` is display text, not an API field. A migration that renames
    # the `total` field must leave it alone.
    assert order_report.TOTAL_LABEL == "total"
