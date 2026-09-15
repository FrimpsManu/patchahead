"""Integration test for the thin upstream client wrapper."""

from app import client
from upstream.api_v2 import OrdersAPIClient


def test_fetch_recent():
    orders = client.fetch_recent(OrdersAPIClient(), limit=2)

    assert [o["id"] for o in orders] == [1, 2]


def test_fetch_one():
    assert client.fetch_one(OrdersAPIClient())["id"] == 1
