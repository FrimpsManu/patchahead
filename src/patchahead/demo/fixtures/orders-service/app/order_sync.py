"""Downstream order-sync integration.

Syncs ALL orders from the upstream Orders API. Written against the v1
(page-based) pagination contract.
"""


def sync_all_orders(api_client):
    page = 1
    all_orders = []

    while True:
        response = api_client.get_orders(page=page)
        all_orders.extend(response["orders"])

        if page >= response["total_pages"]:
            break

        page += 1

    return all_orders
