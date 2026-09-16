"""Read-only queries against the `shopify` Postgres schema.

Plain data-access functions, no LiveKit/agent concerns — the `function_tool`
wrapper that calls `get_order` lives in `agents/order_status.py`. This module
only reads the schema that `shopify_sync/` populates; it never writes to it,
and nothing here imports from or depends on `shopify_sync/`.
"""

from __future__ import annotations

from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row


def get_order(conn: psycopg.Connection, order_number: str) -> Optional[dict[str, Any]]:
    """Fetch one order by its customer-facing order number (`shopify.orders.order_number`
    — e.g. "52428", not Shopify's internal numeric id), with its line items,
    fulfillments, and refunds nested in. None if no such order exists.

    `order_number` is the right lookup key here, not the internal `shopify_id`:
    it's what a customer can actually read out from a confirmation email.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT shopify_id, order_number, customer_id, email, phone,
                   financial_status, fulfillment_status, currency, total_price,
                   created_at, cancelled_at, updated_at
            FROM shopify.orders
            WHERE order_number = %s
            """,
            (order_number,),
        )
        order = cur.fetchone()
        if order is None:
            return None

        cur.execute(
            """
            SELECT product_id, variant_id, title, sku, quantity, price
            FROM shopify.order_line_items
            WHERE order_id = %s
            ORDER BY shopify_id
            """,
            (order["shopify_id"],),
        )
        order["line_items"] = cur.fetchall()

        cur.execute(
            """
            SELECT status, tracking_company, tracking_number, tracking_url,
                   created_at, updated_at
            FROM shopify.fulfillments
            WHERE order_id = %s
            ORDER BY shopify_id
            """,
            (order["shopify_id"],),
        )
        order["fulfillments"] = cur.fetchall()

        cur.execute(
            """
            SELECT created_at, note, amount
            FROM shopify.refunds
            WHERE order_id = %s
            ORDER BY shopify_id
            """,
            (order["shopify_id"],),
        )
        order["refunds"] = cur.fetchall()

    return order
