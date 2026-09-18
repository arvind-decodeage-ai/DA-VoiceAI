"""Read-only queries against the `shopify` Postgres schema.

Plain data-access functions, no LiveKit/agent concerns — the `function_tool`
wrappers that call these live in `agents/order_status.py`. This module only
reads the schema that `shopify_sync/` populates; it never writes to it, and
nothing here imports from or depends on `shopify_sync/`.

Two payload shapes, by design (Step 5 of the REST->GraphQL migration plan):
`get_order()` returns a narrow, speech-shaped summary — a handful of fields
meant to go straight into a voice model's context, not the ~30+ raw columns
and nested lists the schema actually holds. `get_order_detail()` returns
everything, for a second, less-frequently-used tool when a caller drills in
beyond what the narrow summary covers. Widening the default payload instead
of adding a second tool would put NUMERIC/enum noise into every turn's
context regardless of whether the caller ever asks for it.

Display-enum -> speech wording lives here (the read layer), not in the
database: `shopify.orders.display_fulfillment_status` etc. store Shopify's
raw enum verbatim (a record of what Shopify said), and phrasing it for TTS
is a presentation concern, kept separate from that record.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

# Full enum coverage (verified live against the 2026-07 API), including
# members Shopify marks legacy but still returns: OPEN, PENDING_FULFILLMENT,
# and RESTOCKED for fulfillment status.
_FINANCIAL_STATUS_PHRASES = {
    "PENDING": "payment pending",
    "AUTHORIZED": "payment authorized",
    "PARTIALLY_PAID": "partially paid",
    "PAID": "paid",
    "PARTIALLY_REFUNDED": "partially refunded",
    "REFUNDED": "refunded",
    "VOIDED": "voided",
    "EXPIRED": "payment expired",
}

_FULFILLMENT_STATUS_PHRASES = {
    "UNFULFILLED": "not yet fulfilled",
    "PARTIALLY_FULFILLED": "partially fulfilled",
    "FULFILLED": "fulfilled",
    "IN_PROGRESS": "being fulfilled",
    "ON_HOLD": "on hold",
    "SCHEDULED": "scheduled to ship",
    "REQUEST_DECLINED": "fulfillment request declined",
    "OPEN": "open",  # legacy, still returned
    "PENDING_FULFILLMENT": "fulfillment pending",  # legacy, still returned
    "RESTOCKED": "restocked",  # legacy, still returned
}

# ReturnStatus enum (verified live): CANCELED, CLOSED, OPEN, REQUESTED, DECLINED.
_RETURN_STATUS_PHRASES = {
    "REQUESTED": "requested",
    "OPEN": "open",
    "CLOSED": "closed",
    "DECLINED": "declined",
    "CANCELED": "canceled",
}

_CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


def _status_phrase(financial_status: Optional[str], fulfillment_status: Optional[str]) -> str:
    financial = _FINANCIAL_STATUS_PHRASES.get(financial_status, financial_status or "status unknown")
    fulfillment = _FULFILLMENT_STATUS_PHRASES.get(fulfillment_status, fulfillment_status or "status unknown")
    return f"{financial} and {fulfillment}"


def _format_money(amount: Optional[Decimal], currency: Optional[str]) -> Optional[str]:
    """Formatted for speech, in the order's own stored currency — never the
    shop's current default, since a shop's primary currency can change and
    older orders retain their original (the reason the migration stores
    currency per order in the first place)."""
    if amount is None:
        return None
    symbol = _CURRENCY_SYMBOLS.get(currency or "")
    if symbol:
        return f"{symbol}{amount:,.2f}"
    return f"{amount:,.2f} {currency}".strip()


def _return_or_refund_statement(
    refunds: list[dict[str, Any]], returns: list[dict[str, Any]], currency: Optional[str]
) -> Optional[str]:
    """A plain statement of refund/return state — built from `refunds` and
    `returns` independently, never one inferred from the other (a Return is
    not created for every refund; see the migration plan's `pull.py` note)."""
    statements: list[str] = []

    if refunds:
        total = sum((r["amount"] for r in refunds if r["amount"] is not None), Decimal("0"))
        formatted = _format_money(total, currency) if total else None
        if len(refunds) == 1:
            statements.append(f"one refund of {formatted} issued" if formatted else "one refund issued")
        else:
            statements.append(
                f"{len(refunds)} refunds totalling {formatted} issued"
                if formatted
                else f"{len(refunds)} refunds issued"
            )

    if returns:
        if len(returns) == 1:
            status = returns[0]["status"]
            phrase = _RETURN_STATUS_PHRASES.get(status, (status or "unknown").lower())
            statements.append(f"a return is {phrase}")
        else:
            statements.append(f"{len(returns)} returns on file")

    return "; ".join(statements) if statements else None


def get_order(conn: psycopg.Connection, order_number: str) -> Optional[dict[str, Any]]:
    """Fetch one order by its customer-facing order number
    (`shopify.orders.order_number` — e.g. "52428", not Shopify's internal
    numeric id) and return a narrow, speech-shaped summary. None if no such
    order exists.

    `order_number` is the right lookup key here, not the internal
    `shopify_id`: it's what a customer can actually read out from a
    confirmation email.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT shopify_id, order_number, display_financial_status,
                   display_fulfillment_status, total_price_shop_amount,
                   total_price_shop_currency
            FROM shopify.orders
            WHERE order_number = %s
            """,
            (order_number,),
        )
        order = cur.fetchone()
        if order is None:
            return None

        cur.execute(
            "SELECT title, quantity FROM shopify.order_line_items "
            "WHERE order_id = %s ORDER BY shopify_id",
            (order["shopify_id"],),
        )
        items = cur.fetchall()

        cur.execute(
            "SELECT tracking_company, tracking_number, tracking_url "
            "FROM shopify.fulfillments "
            "WHERE order_id = %s AND tracking_number IS NOT NULL "
            "ORDER BY shopify_id LIMIT 1",
            (order["shopify_id"],),
        )
        tracking_row = cur.fetchone()

        cur.execute(
            "SELECT amount FROM shopify.refunds WHERE order_id = %s",
            (order["shopify_id"],),
        )
        refunds = cur.fetchall()

        cur.execute(
            "SELECT status FROM shopify.returns WHERE order_id = %s ORDER BY shopify_id",
            (order["shopify_id"],),
        )
        returns = cur.fetchall()

    return {
        "order_number": order["order_number"],
        "status_phrase": _status_phrase(
            order["display_financial_status"], order["display_fulfillment_status"]
        ),
        "items": [{"title": i["title"], "quantity": i["quantity"]} for i in items],
        "tracking": (
            {
                "carrier": tracking_row["tracking_company"],
                "number": tracking_row["tracking_number"],
                "url": tracking_row["tracking_url"],
            }
            if tracking_row
            else None
        ),
        "return_or_refund": _return_or_refund_statement(
            refunds, returns, order["total_price_shop_currency"]
        ),
        "total": _format_money(order["total_price_shop_amount"], order["total_price_shop_currency"]),
    }


def get_order_detail(conn: psycopg.Connection, order_number: str) -> Optional[dict[str, Any]]:
    """Fetch one order with every column and every nested row — the wide
    detail behind `get_order()`'s narrow summary, for a caller who drills in
    beyond what the summary covers (exact SKU, a return's line items, a
    shipping line's source, etc.). None if no such order exists.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM shopify.orders WHERE order_number = %s", (order_number,))
        order = cur.fetchone()
        if order is None:
            return None

        cur.execute(
            "SELECT * FROM shopify.order_line_items WHERE order_id = %s ORDER BY shopify_id",
            (order["shopify_id"],),
        )
        order["line_items"] = cur.fetchall()

        cur.execute(
            "SELECT * FROM shopify.fulfillments WHERE order_id = %s ORDER BY shopify_id",
            (order["shopify_id"],),
        )
        order["fulfillments"] = cur.fetchall()

        cur.execute(
            "SELECT * FROM shopify.refunds WHERE order_id = %s ORDER BY shopify_id",
            (order["shopify_id"],),
        )
        order["refunds"] = cur.fetchall()

        cur.execute(
            "SELECT * FROM shopify.returns WHERE order_id = %s ORDER BY shopify_id",
            (order["shopify_id"],),
        )
        returns = cur.fetchall()
        for ret in returns:
            cur.execute(
                "SELECT * FROM shopify.return_line_items WHERE return_id = %s ORDER BY shopify_id",
                (ret["shopify_id"],),
            )
            ret["return_line_items"] = cur.fetchall()
        order["returns"] = returns

        cur.execute(
            "SELECT * FROM shopify.shipping_lines WHERE order_id = %s ORDER BY shopify_id",
            (order["shopify_id"],),
        )
        order["shipping_lines"] = cur.fetchall()

    return order
