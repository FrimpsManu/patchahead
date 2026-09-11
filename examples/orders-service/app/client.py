"""Thin wrapper over the upstream Orders API client.

Written against the v1 SDK surface: ``fetch_orders(..., timeout_seconds=...)``.
"""

DEFAULT_TIMEOUT_SECONDS = 15.0


def fetch_recent(api_client, limit=3):
    return api_client.fetch_orders(limit=limit, timeout_seconds=DEFAULT_TIMEOUT_SECONDS)


def fetch_one(api_client):
    orders = api_client.fetch_orders(limit=1, timeout_seconds=5.0)
    return orders[0] if orders else None
