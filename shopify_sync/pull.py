"""Shopify sync: customers + orders (with nested line items, fulfillments,
refunds, returns, and shipping lines) into the `shopify` Postgres schema,
via the GraphQL Admin API (REST->GraphQL migration, 2026-07).

Idempotent: every record is upserted by its Shopify id (parsed from the
GraphQL global id, or read straight off `legacyResourceId` where the type
exposes it), so re-running this script never creates duplicates. Manual/
on-demand only for this version — no cron, no webhook listener, no
always-on worker. Each run is recorded in shopify.sync_runs (start/end
time, status, record count, and a high-water mark on the resource's
updatedAt) so a future incremental sync can pick up from where the last
successful run left off. This bookkeeping is unchanged from the REST
version — only what feeds it (GraphQL nodes instead of REST dicts) changed.

Run with:

    python -m shopify_sync.pull
    python -m shopify_sync.pull --created-at-min 2026-07-01T00:00:00Z --created-at-max 2026-09-16T00:00:00Z

--created-at-min/--created-at-max scope customers and orders to their own
createdAt, translated into a GraphQL search-query string
(`created_at:>=... created_at:<=...`) — the same filtering semantics as
REST's created_at_min/created_at_max params, different transport. Line
items, fulfillments, refunds, returns, and shipping lines have no
independent date-filtered endpoint — Shopify only returns them nested
inside an order — so they're implicitly scoped by following whichever
orders matched the window, unfiltered by their own timestamps. Regardless
of the customers window, every order's embedded `customer` object is
always upserted (Shopify includes it in the order response at no extra
cost) so an order from a customer whose account predates the window still
resolves its orders.customer_id foreign key.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import psycopg

from shopify_sync.client import ShopifyClient
from shopify_sync.config import get_settings
from shopify_sync.migrate import migrate

logger = logging.getLogger("shopify_sync.pull")
logging.basicConfig(level=logging.INFO)

ORDERS_PAGE_SIZE = 250  # Shopify's hard per-connection ceiling; see Investigation.
LINE_ITEMS_PAGE_SIZE = 20  # real observed max per order: 9 (all 4,121 orders, 2026-09)
FULFILLMENTS_FIRST = 10  # first-only, no pageInfo -- see the call site below
REFUNDS_FIRST = 10  # first-only, no pageInfo -- see the call site below
TRACKING_INFO_FIRST = 3  # first-only, no pageInfo -- see the call site below;
# TODO: no historical basis for this number (REST never exposed trackingInfo
# as a list). Verify against the first real GraphQL sync and raise it if any
# fulfillment actually carries more than 3 tracking numbers.
RETURNS_PAGE_SIZE = 10
RETURN_LINE_ITEMS_PAGE_SIZE = 10
SHIPPING_LINES_PAGE_SIZE = 10
CUSTOMERS_PAGE_SIZE = 250


# --------------------------------------------------------------------------
# GraphQL documents
# --------------------------------------------------------------------------

_MONEY_BAG_FIELDS = """
    shopMoney { amount currencyCode }
    presentmentMoney { amount currencyCode }
"""

SYNC_ORDERS = f"""
query SyncOrders($first: Int!, $after: String, $query: String) {{
  orders(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      id
      legacyResourceId
      name
      number
      email
      phone
      processedAt
      closedAt
      cancelledAt
      createdAt
      updatedAt
      tags
      currencyCode
      displayFulfillmentStatus
      displayFinancialStatus

      totalPriceSet {{ {_MONEY_BAG_FIELDS} }}
      subtotalPriceSet {{ {_MONEY_BAG_FIELDS} }}
      totalDiscountsSet {{ {_MONEY_BAG_FIELDS} }}
      totalShippingPriceSet {{ {_MONEY_BAG_FIELDS} }}
      totalRefundedSet {{ {_MONEY_BAG_FIELDS} }}

      customer {{
        legacyResourceId
        email
        phone
        firstName
        lastName
        displayName
        numberOfOrders
        createdAt
        updatedAt
      }}

      lineItems(first: {LINE_ITEMS_PAGE_SIZE}) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          id
          product {{ legacyResourceId }}
          variant {{ legacyResourceId }}
          title
          variantTitle
          sku
          quantity
          isGiftCard
          originalUnitPriceSet {{ shopMoney {{ amount currencyCode }} }}
        }}
      }}

      # fulfillments: first-only, NO pageInfo/cursor. Anything beyond `first`
      # is unreachable through this field -- there is no pagination escape
      # hatch, unlike lineItems/returns/shippingLines above and below.
      fulfillments(first: {FULFILLMENTS_FIRST}) {{
        legacyResourceId
        status
        displayStatus
        createdAt
        updatedAt
        # trackingInfo: also first-only, no pageInfo/cursor. Same caveat.
        trackingInfo(first: {TRACKING_INFO_FIRST}) {{ company number url }}
      }}

      # refunds: first-only, NO pageInfo/cursor -- same caveat as fulfillments.
      refunds(first: {REFUNDS_FIRST}) {{
        legacyResourceId
        createdAt
        note
        totalRefundedSet {{ shopMoney {{ amount currencyCode }} }}
      }}

      returns(first: {RETURNS_PAGE_SIZE}) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          id
          name
          status
          createdAt
          closedAt
          returnLineItems(first: {RETURN_LINE_ITEMS_PAGE_SIZE}) {{
            pageInfo {{ hasNextPage endCursor }}
            nodes {{ id quantity }}
          }}
        }}
      }}

      shippingLines(first: {SHIPPING_LINES_PAGE_SIZE}) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ id title source }}
      }}
    }}
  }}
}}
"""

# Follow-up documents: only reached when an order's nested connection's
# pageInfo.hasNextPage is True on the page fetched inline above (rare, given
# the headroom in the page sizes chosen against this store's observed
# maxima) -- paginated to exhaustion, unlike fulfillments/refunds/trackingInfo.

ORDER_LINE_ITEMS_PAGE = f"""
query OrderLineItemsPage($id: ID!, $first: Int!, $after: String) {{
  order(id: $id) {{
    lineItems(first: $first, after: $after) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        id
        product {{ legacyResourceId }}
        variant {{ legacyResourceId }}
        title
        variantTitle
        sku
        quantity
        isGiftCard
        originalUnitPriceSet {{ shopMoney {{ amount currencyCode }} }}
      }}
    }}
  }}
}}
"""

ORDER_RETURNS_PAGE = f"""
query OrderReturnsPage($id: ID!, $first: Int!, $after: String) {{
  order(id: $id) {{
    returns(first: $first, after: $after) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        id
        name
        status
        createdAt
        closedAt
        returnLineItems(first: {RETURN_LINE_ITEMS_PAGE_SIZE}) {{
          pageInfo {{ hasNextPage endCursor }}
          nodes {{ id quantity }}
        }}
      }}
    }}
  }}
}}
"""

RETURN_LINE_ITEMS_PAGE = f"""
query ReturnLineItemsPage($id: ID!, $first: Int!, $after: String) {{
  return(id: $id) {{
    returnLineItems(first: $first, after: $after) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{ id quantity }}
    }}
  }}
}}
"""

ORDER_SHIPPING_LINES_PAGE = f"""
query OrderShippingLinesPage($id: ID!, $first: Int!, $after: String) {{
  order(id: $id) {{
    shippingLines(first: $first, after: $after) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{ id title source }}
    }}
  }}
}}
"""

SYNC_CUSTOMERS = """
query SyncCustomers($first: Int!, $after: String, $query: String) {
  customers(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      legacyResourceId
      email
      phone
      firstName
      lastName
      displayName
      numberOfOrders
      createdAt
      updatedAt
    }
  }
}
"""


# --------------------------------------------------------------------------
# GID / Decimal helpers
# --------------------------------------------------------------------------


def _gid_to_int(gid: Optional[str]) -> Optional[int]:
    """Parse the numeric suffix out of a GraphQL global id
    (``gid://shopify/Order/12345`` -> ``12345``).

    Verified against live data (2026-09) that this numeric suffix is
    identical to the corresponding REST resource's integer id for every
    existing table (cross-checked an already-synced order and its line
    item), so this preserves every existing BIGINT PK/FK with no
    remapping. Prefer `legacyResourceId` (already a bare string) wherever
    the type exposes it; this is only needed for the types that don't
    (LineItem, Return, ReturnLineItem, ShippingLine).
    """
    if not gid:
        return None
    return int(gid.rsplit("/", 1)[-1])


def _legacy_id(value: Optional[str]) -> Optional[int]:
    """`legacyResourceId` comes back as a string; None-safe int conversion."""
    return int(value) if value is not None else None


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _decimal(value: Optional[str]) -> Optional[Decimal]:
    """Shopify's `Decimal` scalar is serialized as a JSON *string*
    specifically so it never passes through binary floating point.
    Parsing it with `float()` (the REST-era bug this replaces) would
    silently reintroduce exactly the precision loss the string
    serialization exists to avoid.
    """
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _money_bag(money_bag: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Flatten a MoneyBag {shopMoney, presentmentMoney} into the four
    columns every MoneyBag field gets. None-safe: several MoneyBag fields
    are nullable at the API level (subtotalPriceSet, totalDiscountsSet)."""
    if not money_bag:
        return {
            "shop_amount": None,
            "shop_currency": None,
            "presentment_amount": None,
            "presentment_currency": None,
        }
    shop = money_bag.get("shopMoney") or {}
    presentment = money_bag.get("presentmentMoney") or {}
    return {
        "shop_amount": _decimal(shop.get("amount")),
        "shop_currency": shop.get("currencyCode"),
        "presentment_amount": _decimal(presentment.get("amount")),
        "presentment_currency": presentment.get("currencyCode"),
    }


def _date_query(created_at_min: Optional[str], created_at_max: Optional[str]) -> Optional[str]:
    """Build a GraphQL search-query string from the same
    --created-at-min/--created-at-max CLI args REST used, translated to
    Shopify's query syntax (the GraphQL equivalent of REST's
    created_at_min/created_at_max params)."""
    clauses = []
    if created_at_min:
        clauses.append(f"created_at:>={created_at_min}")
    if created_at_max:
        clauses.append(f"created_at:<={created_at_max}")
    return " AND ".join(clauses) if clauses else None


# --------------------------------------------------------------------------
# sync_runs bookkeeping -- unchanged from the REST version
# --------------------------------------------------------------------------


def _start_run(conn: psycopg.Connection, resource: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO shopify.sync_runs (resource, started_at, status)
            VALUES (%s, now(), 'running')
            RETURNING id
            """,
            (resource,),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def _finish_run(
    conn: psycopg.Connection,
    run_id: int,
    *,
    status: str,
    records_synced: int,
    high_water_mark: Optional[datetime],
    error: Optional[str] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE shopify.sync_runs
            SET finished_at = now(),
                status = %s,
                records_synced = %s,
                high_water_mark = %s,
                error = %s
            WHERE id = %s
            """,
            (status, records_synced, high_water_mark, error, run_id),
        )
    conn.commit()


# --------------------------------------------------------------------------
# Upserts -- one per shopify.* table, all operating on GraphQL node shapes
# --------------------------------------------------------------------------


def _upsert_customer(conn: psycopg.Connection, c: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO shopify.customers
                (shopify_id, email, phone, first_name, last_name, display_name,
                 number_of_orders, created_at, updated_at, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (shopify_id) DO UPDATE SET
                email = EXCLUDED.email,
                phone = EXCLUDED.phone,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                display_name = EXCLUDED.display_name,
                number_of_orders = EXCLUDED.number_of_orders,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                synced_at = EXCLUDED.synced_at
            """,
            (
                _legacy_id(c["legacyResourceId"]),
                c.get("email"),
                c.get("phone"),
                c.get("firstName"),
                c.get("lastName"),
                c.get("displayName"),
                c.get("numberOfOrders"),
                _parse_dt(c.get("createdAt")),
                _parse_dt(c.get("updatedAt")),
            ),
        )


def _upsert_order(conn: psycopg.Connection, o: dict[str, Any]) -> None:
    total_price = _money_bag(o.get("totalPriceSet"))
    subtotal_price = _money_bag(o.get("subtotalPriceSet"))
    total_discounts = _money_bag(o.get("totalDiscountsSet"))
    total_shipping_price = _money_bag(o.get("totalShippingPriceSet"))
    total_refunded = _money_bag(o.get("totalRefundedSet"))
    customer = o.get("customer") or {}

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO shopify.orders
                (shopify_id, order_number, customer_id, email, phone,
                 display_financial_status, display_fulfillment_status,
                 currency, total_price, processed_at, closed_at, tags,
                 created_at, cancelled_at, updated_at,
                 total_price_shop_amount, total_price_shop_currency,
                 total_price_presentment_amount, total_price_presentment_currency,
                 subtotal_price_shop_amount, subtotal_price_shop_currency,
                 subtotal_price_presentment_amount, subtotal_price_presentment_currency,
                 total_discounts_shop_amount, total_discounts_shop_currency,
                 total_discounts_presentment_amount, total_discounts_presentment_currency,
                 total_shipping_price_shop_amount, total_shipping_price_shop_currency,
                 total_shipping_price_presentment_amount, total_shipping_price_presentment_currency,
                 total_refunded_shop_amount, total_refunded_shop_currency,
                 total_refunded_presentment_amount, total_refunded_presentment_currency,
                 synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, now())
            ON CONFLICT (shopify_id) DO UPDATE SET
                order_number = EXCLUDED.order_number,
                customer_id = EXCLUDED.customer_id,
                email = EXCLUDED.email,
                phone = EXCLUDED.phone,
                display_financial_status = EXCLUDED.display_financial_status,
                display_fulfillment_status = EXCLUDED.display_fulfillment_status,
                currency = EXCLUDED.currency,
                total_price = EXCLUDED.total_price,
                processed_at = EXCLUDED.processed_at,
                closed_at = EXCLUDED.closed_at,
                tags = EXCLUDED.tags,
                created_at = EXCLUDED.created_at,
                cancelled_at = EXCLUDED.cancelled_at,
                updated_at = EXCLUDED.updated_at,
                total_price_shop_amount = EXCLUDED.total_price_shop_amount,
                total_price_shop_currency = EXCLUDED.total_price_shop_currency,
                total_price_presentment_amount = EXCLUDED.total_price_presentment_amount,
                total_price_presentment_currency = EXCLUDED.total_price_presentment_currency,
                subtotal_price_shop_amount = EXCLUDED.subtotal_price_shop_amount,
                subtotal_price_shop_currency = EXCLUDED.subtotal_price_shop_currency,
                subtotal_price_presentment_amount = EXCLUDED.subtotal_price_presentment_amount,
                subtotal_price_presentment_currency = EXCLUDED.subtotal_price_presentment_currency,
                total_discounts_shop_amount = EXCLUDED.total_discounts_shop_amount,
                total_discounts_shop_currency = EXCLUDED.total_discounts_shop_currency,
                total_discounts_presentment_amount = EXCLUDED.total_discounts_presentment_amount,
                total_discounts_presentment_currency = EXCLUDED.total_discounts_presentment_currency,
                total_shipping_price_shop_amount = EXCLUDED.total_shipping_price_shop_amount,
                total_shipping_price_shop_currency = EXCLUDED.total_shipping_price_shop_currency,
                total_shipping_price_presentment_amount = EXCLUDED.total_shipping_price_presentment_amount,
                total_shipping_price_presentment_currency = EXCLUDED.total_shipping_price_presentment_currency,
                total_refunded_shop_amount = EXCLUDED.total_refunded_shop_amount,
                total_refunded_shop_currency = EXCLUDED.total_refunded_shop_currency,
                total_refunded_presentment_amount = EXCLUDED.total_refunded_presentment_amount,
                total_refunded_presentment_currency = EXCLUDED.total_refunded_presentment_currency,
                synced_at = EXCLUDED.synced_at
            """,
            (
                _legacy_id(o["legacyResourceId"]),
                str(o["number"]) if o.get("number") is not None else None,
                _legacy_id(customer.get("legacyResourceId")),
                o.get("email"),
                o.get("phone"),
                o.get("displayFinancialStatus"),
                o.get("displayFulfillmentStatus"),
                o.get("currencyCode"),
                total_price["shop_amount"],
                _parse_dt(o.get("processedAt")),
                _parse_dt(o.get("closedAt")),
                ",".join(o["tags"]) if o.get("tags") else None,
                _parse_dt(o.get("createdAt")),
                _parse_dt(o.get("cancelledAt")),
                _parse_dt(o.get("updatedAt")),
                total_price["shop_amount"],
                total_price["shop_currency"],
                total_price["presentment_amount"],
                total_price["presentment_currency"],
                subtotal_price["shop_amount"],
                subtotal_price["shop_currency"],
                subtotal_price["presentment_amount"],
                subtotal_price["presentment_currency"],
                total_discounts["shop_amount"],
                total_discounts["shop_currency"],
                total_discounts["presentment_amount"],
                total_discounts["presentment_currency"],
                total_shipping_price["shop_amount"],
                total_shipping_price["shop_currency"],
                total_shipping_price["presentment_amount"],
                total_shipping_price["presentment_currency"],
                total_refunded["shop_amount"],
                total_refunded["shop_currency"],
                total_refunded["presentment_amount"],
                total_refunded["presentment_currency"],
            ),
        )


def _upsert_line_item(conn: psycopg.Connection, order_id: int, li: dict[str, Any]) -> None:
    product = li.get("product") or {}
    variant = li.get("variant") or {}
    unit_price = _money_bag({"shopMoney": (li.get("originalUnitPriceSet") or {}).get("shopMoney")})
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO shopify.order_line_items
                (shopify_id, order_id, product_id, variant_id, title, variant_title,
                 sku, quantity, price, is_gift_card, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (shopify_id) DO UPDATE SET
                order_id = EXCLUDED.order_id,
                product_id = EXCLUDED.product_id,
                variant_id = EXCLUDED.variant_id,
                title = EXCLUDED.title,
                variant_title = EXCLUDED.variant_title,
                sku = EXCLUDED.sku,
                quantity = EXCLUDED.quantity,
                price = EXCLUDED.price,
                is_gift_card = EXCLUDED.is_gift_card,
                synced_at = EXCLUDED.synced_at
            """,
            (
                _gid_to_int(li["id"]),
                order_id,
                _legacy_id(product.get("legacyResourceId")),
                _legacy_id(variant.get("legacyResourceId")),
                li.get("title"),
                li.get("variantTitle"),
                li.get("sku"),
                li.get("quantity"),
                unit_price["shop_amount"],
                li.get("isGiftCard"),
            ),
        )


def _upsert_fulfillment(conn: psycopg.Connection, order_id: int, f: dict[str, Any]) -> None:
    tracking_list = f.get("trackingInfo") or []
    tracking = tracking_list[0] if tracking_list else {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO shopify.fulfillments
                (shopify_id, order_id, status, display_status, tracking_company,
                 tracking_number, tracking_url, created_at, updated_at, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (shopify_id) DO UPDATE SET
                order_id = EXCLUDED.order_id,
                status = EXCLUDED.status,
                display_status = EXCLUDED.display_status,
                tracking_company = EXCLUDED.tracking_company,
                tracking_number = EXCLUDED.tracking_number,
                tracking_url = EXCLUDED.tracking_url,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                synced_at = EXCLUDED.synced_at
            """,
            (
                _legacy_id(f["legacyResourceId"]),
                order_id,
                f.get("status"),
                f.get("displayStatus"),
                tracking.get("company"),
                tracking.get("number"),
                tracking.get("url"),
                _parse_dt(f.get("createdAt")),
                _parse_dt(f.get("updatedAt")),
            ),
        )


def _upsert_refund(conn: psycopg.Connection, order_id: int, r: dict[str, Any]) -> None:
    # Shopify's own precomputed total, not a manual sum over transactions --
    # replaces the old float(t["amount"]) summing bug entirely (both the
    # float-precision issue and the redundant manual aggregation).
    total = _money_bag(r.get("totalRefundedSet"))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO shopify.refunds
                (shopify_id, order_id, created_at, note, amount, synced_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (shopify_id) DO UPDATE SET
                order_id = EXCLUDED.order_id,
                created_at = EXCLUDED.created_at,
                note = EXCLUDED.note,
                amount = EXCLUDED.amount,
                synced_at = EXCLUDED.synced_at
            """,
            (
                _legacy_id(r["legacyResourceId"]),
                order_id,
                _parse_dt(r.get("createdAt")),
                r.get("note"),
                total["shop_amount"],
            ),
        )


def _upsert_return(conn: psycopg.Connection, order_id: int, ret: dict[str, Any]) -> int:
    return_id = _gid_to_int(ret["id"])
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO shopify.returns
                (shopify_id, order_id, name, status, created_at, closed_at, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (shopify_id) DO UPDATE SET
                order_id = EXCLUDED.order_id,
                name = EXCLUDED.name,
                status = EXCLUDED.status,
                created_at = EXCLUDED.created_at,
                closed_at = EXCLUDED.closed_at,
                synced_at = EXCLUDED.synced_at
            """,
            (
                return_id,
                order_id,
                ret.get("name"),
                ret.get("status"),
                _parse_dt(ret.get("createdAt")),
                _parse_dt(ret.get("closedAt")),
            ),
        )
    return return_id


def _upsert_return_line_item(conn: psycopg.Connection, return_id: int, rli: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO shopify.return_line_items
                (shopify_id, return_id, quantity, synced_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (shopify_id) DO UPDATE SET
                return_id = EXCLUDED.return_id,
                quantity = EXCLUDED.quantity,
                synced_at = EXCLUDED.synced_at
            """,
            (_gid_to_int(rli["id"]), return_id, rli.get("quantity")),
        )


def _upsert_shipping_line(conn: psycopg.Connection, order_id: int, sl: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO shopify.shipping_lines
                (shopify_id, order_id, title, source, synced_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (shopify_id) DO UPDATE SET
                order_id = EXCLUDED.order_id,
                title = EXCLUDED.title,
                source = EXCLUDED.source,
                synced_at = EXCLUDED.synced_at
            """,
            (_gid_to_int(sl["id"]), order_id, sl.get("title"), sl.get("source")),
        )


# --------------------------------------------------------------------------
# Nested-connection exhaustion -- only reached when the page fetched inline
# with the order wasn't the whole connection (rare: page sizes above were
# chosen with real headroom over this store's observed maxima).
# --------------------------------------------------------------------------


def _exhaust_line_items(client: ShopifyClient, order_gid: str, first_page: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = list(first_page["nodes"])
    page_info = first_page["pageInfo"]
    while page_info.get("hasNextPage"):
        body = client.graphql(
            ORDER_LINE_ITEMS_PAGE,
            {"id": order_gid, "first": LINE_ITEMS_PAGE_SIZE, "after": page_info["endCursor"]},
        )
        connection = body["data"]["order"]["lineItems"]
        nodes.extend(connection["nodes"])
        page_info = connection["pageInfo"]
    return nodes


def _exhaust_return_line_items(
    client: ShopifyClient, return_gid: str, first_page: dict[str, Any]
) -> list[dict[str, Any]]:
    nodes = list(first_page["nodes"])
    page_info = first_page["pageInfo"]
    while page_info.get("hasNextPage"):
        body = client.graphql(
            RETURN_LINE_ITEMS_PAGE,
            {
                "id": return_gid,
                "first": RETURN_LINE_ITEMS_PAGE_SIZE,
                "after": page_info["endCursor"],
            },
        )
        connection = body["data"]["return"]["returnLineItems"]
        nodes.extend(connection["nodes"])
        page_info = connection["pageInfo"]
    return nodes


def _exhaust_returns(
    client: ShopifyClient, order_gid: str, first_page: dict[str, Any]
) -> list[dict[str, Any]]:
    nodes = list(first_page["nodes"])
    page_info = first_page["pageInfo"]
    while page_info.get("hasNextPage"):
        body = client.graphql(
            ORDER_RETURNS_PAGE,
            {"id": order_gid, "first": RETURNS_PAGE_SIZE, "after": page_info["endCursor"]},
        )
        connection = body["data"]["order"]["returns"]
        nodes.extend(connection["nodes"])
        page_info = connection["pageInfo"]

    # Each return's own returnLineItems may itself need exhausting.
    for ret in nodes:
        rli_connection = ret["returnLineItems"]
        if rli_connection["pageInfo"].get("hasNextPage"):
            ret["returnLineItems"] = {
                "nodes": _exhaust_return_line_items(client, ret["id"], rli_connection)
            }
    return nodes


def _exhaust_shipping_lines(
    client: ShopifyClient, order_gid: str, first_page: dict[str, Any]
) -> list[dict[str, Any]]:
    nodes = list(first_page["nodes"])
    page_info = first_page["pageInfo"]
    while page_info.get("hasNextPage"):
        body = client.graphql(
            ORDER_SHIPPING_LINES_PAGE,
            {"id": order_gid, "first": SHIPPING_LINES_PAGE_SIZE, "after": page_info["endCursor"]},
        )
        connection = body["data"]["order"]["shippingLines"]
        nodes.extend(connection["nodes"])
        page_info = connection["pageInfo"]
    return nodes


# --------------------------------------------------------------------------
# pull_customers / pull_orders
# --------------------------------------------------------------------------


def pull_customers(
    client: ShopifyClient,
    conn: psycopg.Connection,
    created_at_min: Optional[str] = None,
    created_at_max: Optional[str] = None,
) -> tuple[int, Optional[datetime]]:
    count = 0
    high_water_mark: Optional[datetime] = None
    variables = {"first": CUSTOMERS_PAGE_SIZE, "query": _date_query(created_at_min, created_at_max)}

    for customer in client.graphql_paginate(SYNC_CUSTOMERS, variables, ("customers",)):
        with conn.transaction():
            _upsert_customer(conn, customer)
        count += 1
        updated_at = _parse_dt(customer.get("updatedAt"))
        if updated_at and (high_water_mark is None or updated_at > high_water_mark):
            high_water_mark = updated_at

    return count, high_water_mark


def pull_orders(
    client: ShopifyClient,
    conn: psycopg.Connection,
    created_at_min: Optional[str] = None,
    created_at_max: Optional[str] = None,
) -> tuple[int, int, int, int, datetime | None]:
    order_count = 0
    line_item_count = 0
    fulfillment_count = 0
    refund_count = 0
    high_water_mark: Optional[datetime] = None

    variables = {"first": ORDERS_PAGE_SIZE, "query": _date_query(created_at_min, created_at_max)}

    for order in client.graphql_paginate(SYNC_ORDERS, variables, ("orders",)):
        order_gid = order["id"]
        order_id = _legacy_id(order["legacyResourceId"])

        line_items = order["lineItems"]["nodes"]
        if order["lineItems"]["pageInfo"].get("hasNextPage"):
            line_items = _exhaust_line_items(client, order_gid, order["lineItems"])

        returns = order["returns"]["nodes"]
        if order["returns"]["pageInfo"].get("hasNextPage"):
            returns = _exhaust_returns(client, order_gid, order["returns"])
        else:
            for ret in returns:
                if ret["returnLineItems"]["pageInfo"].get("hasNextPage"):
                    ret["returnLineItems"] = {
                        "nodes": _exhaust_return_line_items(
                            client, ret["id"], ret["returnLineItems"]
                        )
                    }

        shipping_lines = order["shippingLines"]["nodes"]
        if order["shippingLines"]["pageInfo"].get("hasNextPage"):
            shipping_lines = _exhaust_shipping_lines(client, order_gid, order["shippingLines"])

        # One transaction per order: all of its upserts (order, customer,
        # line items, fulfillments, refunds, returns, return line items,
        # shipping lines) commit together or not at all -- unlike the old
        # REST version, which committed per row.
        with conn.transaction():
            embedded_customer = order.get("customer")
            if embedded_customer:
                _upsert_customer(conn, embedded_customer)

            _upsert_order(conn, order)

            for li in line_items:
                _upsert_line_item(conn, order_id, li)
                line_item_count += 1

            for f in order.get("fulfillments") or []:
                _upsert_fulfillment(conn, order_id, f)
                fulfillment_count += 1

            for r in order.get("refunds") or []:
                _upsert_refund(conn, order_id, r)
                refund_count += 1

            # Returns vs refunds are never conflated: populated independently
            # from Order.returns and Order.refunds respectively. A Return is
            # not created for every refund (returnRequest/returnCreate only).
            for ret in returns:
                return_id = _upsert_return(conn, order_id, ret)
                for rli in ret["returnLineItems"]["nodes"]:
                    _upsert_return_line_item(conn, return_id, rli)

            for sl in shipping_lines:
                _upsert_shipping_line(conn, order_id, sl)

        order_count += 1
        updated_at = _parse_dt(order.get("updatedAt"))
        if updated_at and (high_water_mark is None or updated_at > high_water_mark):
            high_water_mark = updated_at

    return order_count, line_item_count, fulfillment_count, refund_count, high_water_mark


def run(created_at_min: Optional[str] = None, created_at_max: Optional[str] = None) -> None:
    settings = get_settings()

    # Idempotent, so safe to (re-)apply before every pull.
    migrate()

    logger.info(
        "starting Shopify historical pull for shop=%s (created_at_min=%s, created_at_max=%s)",
        settings.shopify_shop_domain,
        created_at_min or "-",
        created_at_max or "-",
    )

    with psycopg.connect(settings.database_url) as conn, ShopifyClient(
        shop_domain=settings.shopify_shop_domain,
        api_version=settings.shopify_api_version,
        access_token=settings.shopify_access_token,
        client_id=settings.shopify_client_id,
        client_secret=settings.shopify_client_secret,
    ) as client:
        customers_run_id = _start_run(conn, "customers")
        try:
            customer_count, customers_hwm = pull_customers(client, conn, created_at_min, created_at_max)
        except Exception as e:  # noqa: BLE001
            _finish_run(conn, customers_run_id, status="failed", records_synced=0, high_water_mark=None, error=str(e))
            logger.error("customer pull FAILED: %s", e)
            raise
        _finish_run(
            conn, customers_run_id, status="success", records_synced=customer_count, high_water_mark=customers_hwm
        )
        logger.info("customer pull OK: %d customers synced", customer_count)

        orders_run_id = _start_run(conn, "orders")
        try:
            order_count, line_item_count, fulfillment_count, refund_count, orders_hwm = pull_orders(
                client, conn, created_at_min, created_at_max
            )
        except Exception as e:  # noqa: BLE001
            _finish_run(conn, orders_run_id, status="failed", records_synced=0, high_water_mark=None, error=str(e))
            logger.error("order pull FAILED: %s", e)
            raise
        _finish_run(
            conn, orders_run_id, status="success", records_synced=order_count, high_water_mark=orders_hwm
        )
        logger.info(
            "order pull OK: %d orders, %d line items, %d fulfillments, %d refunds synced",
            order_count,
            line_item_count,
            fulfillment_count,
            refund_count,
        )

    logger.info("Shopify historical pull complete")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--created-at-min", default=None, help="ISO 8601, e.g. 2026-07-01T00:00:00Z")
    parser.add_argument("--created-at-max", default=None, help="ISO 8601, e.g. 2026-09-16T00:00:00Z")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(created_at_min=args.created_at_min, created_at_max=args.created_at_max)
