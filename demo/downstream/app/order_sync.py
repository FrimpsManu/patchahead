"""Downstream order-sync integration.

Syncs ALL orders from the upstream Orders API. Written against the v1
(page-based) pagination contract.
"""


def sync_all_orders(api_client):
    cursor = None
    all_orders = []

    while True:
        response = api_client.get_orders(cursor=cursor)
        all_orders.extend(response["orders"])

        if not response.get("has_more"):
            break

        cursor = response.get("next_cursor")

    return all_orders
