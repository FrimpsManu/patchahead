"""Revenue reporting over synced orders.

Reads each order's `total` field. Written against the v1 Orders schema.
"""

# Unrelated identifiers that merely contain the word "total". A correct
# migration must not touch any of these.
TOTAL_LABEL = "total"


def total_revenue(orders):
    return round(sum(order["total"] for order in orders), 2)


def revenue_by_customer(orders):
    totals = {}
    for order in orders:
        name = order["customer"]
        totals[name] = round(totals.get(name, 0) + order["total"], 2)
    return totals


def format_summary(orders):
    return f"{TOTAL_LABEL}: {total_revenue(orders)}"
