"""Tests for shopify_sync/pull.py's GraphQL rewrite.

No live Shopify calls: a fake client (duck-typed to ShopifyClient's
graphql()/graphql_paginate() surface) stands in for the network, matching
this project's convention of fakes only for genuinely external things.
Exercised against a real Postgres (migrate() run first), same
no-mocked-database convention as test_shopify_tools.py.

Run from the repository root:

    .venv/bin/python -m pytest tests/test_shopify_pull.py -q
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import pytest
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
load_dotenv(REPO / ".env")

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from shopify_sync.migrate import migrate  # noqa: E402
from shopify_sync.pull import (  # noqa: E402
    _date_query,
    _decimal,
    _gid_to_int,
    _money_bag,
    pull_customers,
    pull_orders,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _db_reachable() -> bool:
    if not DATABASE_URL:
        return False
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_reachable(), reason="Postgres not reachable; run `docker compose up -d`"
)


class _FakeClient:
    """Duck-typed to ShopifyClient's graphql()/graphql_paginate() surface.

    `paginate_nodes` is what graphql_paginate() yields directly (the whole
    fake "connection" for the test, already page-flattened -- the pagination
    *mechanism* itself is client.py's job and is tested there). `graphql_queue`
    is a FIFO of responses for direct .graphql() calls, used by the
    exhaustion follow-up paths.
    """

    def __init__(
        self,
        paginate_nodes: list[dict[str, Any]] | None = None,
        graphql_queue: list[dict[str, Any]] | None = None,
    ) -> None:
        self.paginate_nodes = paginate_nodes or []
        self.graphql_queue = list(graphql_queue or [])
        self.graphql_calls: list[tuple[str, dict[str, Any]]] = []

    def graphql_paginate(
        self, document: str, variables: dict[str, Any], path: tuple[str, ...]
    ) -> Iterator[dict[str, Any]]:
        yield from self.paginate_nodes

    def graphql(self, document: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        self.graphql_calls.append((document, variables or {}))
        return self.graphql_queue.pop(0)


def _money_bag_json(amount: str, currency: str = "INR") -> dict[str, Any]:
    return {
        "shopMoney": {"amount": amount, "currencyCode": currency},
        "presentmentMoney": {"amount": amount, "currencyCode": currency},
    }


def _connection(nodes: list[dict[str, Any]], has_next: bool = False, cursor: str | None = None) -> dict[str, Any]:
    return {"pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": nodes}


def _order_node(shopify_id: int = 90000000001, **overrides: Any) -> dict[str, Any]:
    node = {
        "id": f"gid://shopify/Order/{shopify_id}",
        "legacyResourceId": str(shopify_id),
        "name": "#90001",
        "number": 90001,
        "email": "customer@example.com",
        "phone": None,
        "processedAt": "2026-09-01T10:00:00Z",
        "closedAt": None,
        "cancelledAt": None,
        "createdAt": "2026-09-01T10:00:00Z",
        "updatedAt": "2026-09-01T10:05:00Z",
        "tags": ["vip", "repeat"],
        "currencyCode": "INR",
        "displayFulfillmentStatus": "FULFILLED",
        "displayFinancialStatus": "PAID",
        "totalPriceSet": _money_bag_json("1606.0"),
        "subtotalPriceSet": _money_bag_json("1500.0"),
        "totalDiscountsSet": None,
        "totalShippingPriceSet": _money_bag_json("106.0"),
        "totalRefundedSet": _money_bag_json("0.0"),
        "customer": {
            "legacyResourceId": "80000000001",
            "email": "customer@example.com",
            "phone": None,
            "firstName": "Test",
            "lastName": "Customer",
            "displayName": "Test Customer",
            "numberOfOrders": 3,
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        },
        "lineItems": _connection([]),
        "fulfillments": [],
        "refunds": [],
        "returns": _connection([]),
        "shippingLines": _connection([]),
    }
    node.update(overrides)
    return node


# All fake ids in this file's fixtures deliberately sit in these ranges,
# well clear of any real Shopify id (real order/customer ids observed live
# are 14+ digits; these are 11). Deleting orders/customers in range cascades
# to every child table (order_line_items/fulfillments/refunds/returns/
# return_line_items/shipping_lines all FK ON DELETE CASCADE), so this is
# enough to leave the real synced data completely undisturbed.
_FAKE_ORDER_ID_RANGE = (90_000_000_000, 90_999_999_999)
_FAKE_CUSTOMER_ID_RANGE = (80_000_000_000, 80_999_999_999)


@pytest.fixture(autouse=True)
def _migrated_schema():
    migrate()
    yield
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM shopify.orders WHERE shopify_id BETWEEN %s AND %s",
                _FAKE_ORDER_ID_RANGE,
            )
            cur.execute(
                "DELETE FROM shopify.customers WHERE shopify_id BETWEEN %s AND %s",
                _FAKE_CUSTOMER_ID_RANGE,
            )
        conn.commit()


def _fetch_one(table: str, shopify_id: int) -> dict[str, Any] | None:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT * FROM shopify.{table} WHERE shopify_id = %s", (shopify_id,))
            return cur.fetchone()


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_gid_to_int_parses_the_numeric_suffix():
    assert _gid_to_int("gid://shopify/Order/16769483899251") == 16769483899251
    assert _gid_to_int("gid://shopify/LineItem/45231484797299") == 45231484797299
    assert _gid_to_int(None) is None


def test_decimal_parses_the_json_string_amount_as_decimal_not_float():
    """The core B1-of-this-slice fix: Shopify's Decimal scalar is a JSON
    *string* specifically so it never passes through binary floating
    point. float("1606.0") would already lose nothing here, but the
    contract must be Decimal end to end -- assert the type, not just the
    value, since a float that happens to print the same would pass a
    value-only check and still be wrong."""
    result = _decimal("1606.10")
    assert isinstance(result, Decimal)
    assert result == Decimal("1606.10")
    assert _decimal(None) is None


def test_decimal_handles_a_value_float_would_corrupt():
    # 0.1 + 0.2 != 0.3 in binary float; Decimal keeps it exact.
    assert _decimal("0.1") + _decimal("0.2") == Decimal("0.3")


def test_money_bag_flattens_shop_and_presentment_amounts():
    flat = _money_bag(_money_bag_json("1606.0", "INR"))
    assert flat == {
        "shop_amount": Decimal("1606.0"),
        "shop_currency": "INR",
        "presentment_amount": Decimal("1606.0"),
        "presentment_currency": "INR",
    }


def test_money_bag_is_none_safe_for_nullable_moneybag_fields():
    """subtotalPriceSet/totalDiscountsSet are nullable MoneyBag fields."""
    assert _money_bag(None) == {
        "shop_amount": None,
        "shop_currency": None,
        "presentment_amount": None,
        "presentment_currency": None,
    }


def test_date_query_builds_the_graphql_search_syntax():
    assert _date_query("2026-07-01", "2026-09-16") == "created_at:>='2026-07-01' AND created_at:<='2026-09-16'"
    assert _date_query("2026-07-01", None) == "created_at:>='2026-07-01'"
    assert _date_query(None, None) is None


def test_date_query_quotes_a_full_iso_timestamp_value():
    """The actual bug from the first real sync (2026-09-18): unquoted, the
    colons inside an ISO-8601 timestamp confuse Shopify's search-query
    parser -- confirmed live to silently widen an orders match window from
    ~14 to 41 results, and to make Shopify disregard a customers filter
    entirely (129,914 rows instead of ~55). This test asserts the quoting
    itself, not just that dates "roughly" filter -- a value-only check on
    real query results couldn't have caught this before it ran live."""
    query = _date_query("2026-09-18T09:07:36Z", None)
    assert query == "created_at:>='2026-09-18T09:07:36Z'"
    # The value must be fully wrapped in quotes, not partially -- e.g. a
    # bug that quoted only up to the first colon would still contain an
    # unquoted colon-bearing fragment.
    assert query.count("'") == 2
    assert query.index("'") < query.index("2026-09-18T09:07:36Z") < query.rindex("'")


# --------------------------------------------------------------------------
# pull_customers
# --------------------------------------------------------------------------


def test_pull_customers_upserts_and_tracks_high_water_mark():
    customer = {
        "legacyResourceId": "80000000042",
        "email": "a@example.com",
        "phone": "+911234567890",
        "firstName": "A",
        "lastName": "B",
        "displayName": "A B",
        "numberOfOrders": 7,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-09-01T00:00:00Z",
    }
    client = _FakeClient(paginate_nodes=[customer])
    with psycopg.connect(DATABASE_URL) as conn:
        count, hwm = pull_customers(client, conn)

    assert count == 1
    assert hwm is not None and hwm.isoformat().startswith("2026-09-01")

    row = _fetch_one("customers", 80000000042)
    assert row is not None
    assert row["display_name"] == "A B"
    assert row["number_of_orders"] == 7


# --------------------------------------------------------------------------
# pull_orders -- happy path, all nested tables
# --------------------------------------------------------------------------


def test_pull_orders_upserts_order_with_decimal_money_columns():
    order = _order_node(90000000101)
    client = _FakeClient(paginate_nodes=[order])
    with psycopg.connect(DATABASE_URL) as conn:
        order_count, *_rest = pull_orders(client, conn)

    assert order_count == 1
    row = _fetch_one("orders", 90000000101)
    assert row["order_number"] == "90001"  # from Order.number, not .name ("#90001")
    assert row["display_financial_status"] == "PAID"
    assert row["display_fulfillment_status"] == "FULFILLED"
    assert isinstance(row["total_price_shop_amount"], Decimal)
    assert row["total_price_shop_amount"] == Decimal("1606.0")
    assert row["total_price_shop_currency"] == "INR"
    assert row["subtotal_price_shop_amount"] == Decimal("1500.0")
    assert row["total_discounts_shop_amount"] is None  # nullable MoneyBag, no discount
    assert row["tags"] == "vip,repeat"


def test_pull_orders_upserts_the_embedded_customer_and_resolves_the_fk():
    order = _order_node(90000000102)
    client = _FakeClient(paginate_nodes=[order])
    with psycopg.connect(DATABASE_URL) as conn:
        pull_orders(client, conn)

    order_row = _fetch_one("orders", 90000000102)
    customer_row = _fetch_one("customers", 80000000001)
    assert customer_row is not None
    assert order_row["customer_id"] == 80000000001


def test_pull_orders_upserts_line_items_with_variant_title_and_gift_card_flag():
    order = _order_node(
        90000000103,
        lineItems=_connection(
            [
                {
                    "id": "gid://shopify/LineItem/70000000001",
                    "product": {"legacyResourceId": "60000000001"},
                    "variant": {"legacyResourceId": "50000000001"},
                    "title": "β-NMN",
                    "variantTitle": "250mg",
                    "sku": "NMN-250",
                    "quantity": 2,
                    "isGiftCard": False,
                    "originalUnitPriceSet": {"shopMoney": {"amount": "803.0", "currencyCode": "INR"}},
                }
            ]
        ),
    )
    client = _FakeClient(paginate_nodes=[order])
    with psycopg.connect(DATABASE_URL) as conn:
        _, line_item_count, *_rest = pull_orders(client, conn)

    assert line_item_count == 1
    row = _fetch_one("order_line_items", 70000000001)
    assert row["variant_title"] == "250mg"
    assert row["is_gift_card"] is False
    assert row["price"] == Decimal("803.0")


def test_pull_orders_fulfillment_uses_first_tracking_entry_and_display_status():
    order = _order_node(
        90000000104,
        fulfillments=[
            {
                "legacyResourceId": "40000000001",
                "status": "SUCCESS",
                "displayStatus": "DELIVERED",
                "createdAt": "2026-09-02T00:00:00Z",
                "updatedAt": "2026-09-03T00:00:00Z",
                "trackingInfo": [
                    {"company": "Blue Dart", "number": "BD123", "url": "https://track/BD123"}
                ],
            }
        ],
    )
    client = _FakeClient(paginate_nodes=[order])
    with psycopg.connect(DATABASE_URL) as conn:
        _, _, fulfillment_count, *_rest = pull_orders(client, conn)

    assert fulfillment_count == 1
    row = _fetch_one("fulfillments", 40000000001)
    assert row["display_status"] == "DELIVERED"
    assert row["tracking_company"] == "Blue Dart"
    assert row["tracking_number"] == "BD123"


def test_pull_orders_refund_amount_comes_from_total_refunded_set_not_summed_transactions():
    """Replaces the old float(t["amount"]) transaction-summing bug: the
    amount now comes straight from Shopify's own precomputed
    totalRefundedSet, and is a Decimal."""
    order = _order_node(
        90000000105,
        refunds=[
            {
                "legacyResourceId": "30000000001",
                "createdAt": "2026-09-04T00:00:00Z",
                "note": "Customer requested",
                "totalRefundedSet": {"shopMoney": {"amount": "250.50", "currencyCode": "INR"}},
            }
        ],
    )
    client = _FakeClient(paginate_nodes=[order])
    with psycopg.connect(DATABASE_URL) as conn:
        _, _, _, refund_count, _ = pull_orders(client, conn)

    assert refund_count == 1
    row = _fetch_one("refunds", 30000000001)
    assert row["amount"] == Decimal("250.50")
    assert isinstance(row["amount"], Decimal)


def test_pull_orders_returns_and_return_line_items_populated_independently_of_refunds():
    """Returns vs refunds are never conflated: a Return is populated from
    Order.returns, never inferred from the presence of a refund."""
    order = _order_node(
        90000000106,
        refunds=[],  # no refund at all
        returns=_connection(
            [
                {
                    "id": "gid://shopify/Return/20000000001",
                    "name": "R1",
                    "status": "OPEN",
                    "createdAt": "2026-09-05T00:00:00Z",
                    "closedAt": None,
                    "returnLineItems": _connection(
                        [{"id": "gid://shopify/ReturnLineItem/10000000001", "quantity": 1}]
                    ),
                }
            ]
        ),
    )
    client = _FakeClient(paginate_nodes=[order])
    with psycopg.connect(DATABASE_URL) as conn:
        pull_orders(client, conn)

    return_row = _fetch_one("returns", 20000000001)
    assert return_row is not None
    assert return_row["status"] == "OPEN"

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM shopify.return_line_items WHERE shopify_id = %s", (10000000001,)
            )
            rli_row = cur.fetchone()
    assert rli_row is not None
    assert rli_row["return_id"] == 20000000001
    assert rli_row["quantity"] == 1

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM shopify.refunds WHERE order_id = %s", (90000000106,))
            refund_count_for_order = cur.fetchone()[0]
    assert refund_count_for_order == 0  # no refund was ever synced for this order


def test_pull_orders_shipping_lines_with_source():
    order = _order_node(
        90000000107,
        shippingLines=_connection(
            [{"id": "gid://shopify/ShippingLine/1000001", "title": "Standard", "source": "shopify"}]
        ),
    )
    client = _FakeClient(paginate_nodes=[order])
    with psycopg.connect(DATABASE_URL) as conn:
        pull_orders(client, conn)

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM shopify.shipping_lines WHERE shopify_id = %s", (1000001,))
            row = cur.fetchone()
    assert row is not None
    assert row["title"] == "Standard"
    assert row["source"] == "shopify"


def test_pull_orders_tracks_high_water_mark_from_updated_at():
    order = _order_node(90000000108, updatedAt="2026-09-10T12:00:00Z")
    client = _FakeClient(paginate_nodes=[order])
    with psycopg.connect(DATABASE_URL) as conn:
        _, _, _, _, hwm = pull_orders(client, conn)
    assert hwm is not None and hwm.isoformat().startswith("2026-09-10")


# --------------------------------------------------------------------------
# One transaction per order
# --------------------------------------------------------------------------


def test_pull_orders_rolls_back_the_whole_order_on_a_failure_partway_through():
    """One transaction per order: if any of an order's upserts fails, none
    of that order's rows (order, line items, fulfillments, ...) should be
    left committed."""
    order = _order_node(
        90000000109,
        lineItems=_connection(
            [
                {
                    "id": "gid://shopify/LineItem/70000000099",
                    "product": None,
                    "variant": None,
                    "title": "Bad row",
                    "variantTitle": None,
                    "sku": None,
                    # quantity is a string that can't be inserted into an
                    # INTEGER column -- forces a failure mid-transaction.
                    "quantity": "not-a-number",
                    "isGiftCard": None,
                    "originalUnitPriceSet": None,
                }
            ]
        ),
    )
    client = _FakeClient(paginate_nodes=[order])
    with psycopg.connect(DATABASE_URL) as conn:
        with pytest.raises(Exception):
            pull_orders(client, conn)

    assert _fetch_one("orders", 90000000109) is None
    assert _fetch_one("order_line_items", 70000000099) is None


# --------------------------------------------------------------------------
# Nested-connection exhaustion (fulfillments/refunds have no escape hatch,
# but lineItems/returns/shippingLines are cursor-paginated and must be
# paginated to exhaustion when the inline page wasn't everything).
# --------------------------------------------------------------------------


def test_pull_orders_exhausts_line_items_beyond_the_first_page():
    order = _order_node(
        90000000110,
        lineItems=_connection(
            [
                {
                    "id": "gid://shopify/LineItem/70000000201",
                    "product": None,
                    "variant": None,
                    "title": "Item 1",
                    "variantTitle": None,
                    "sku": None,
                    "quantity": 1,
                    "isGiftCard": False,
                    "originalUnitPriceSet": None,
                }
            ],
            has_next=True,
            cursor="cursor-1",
        ),
    )
    follow_up_response = {
        "data": {
            "order": {
                "lineItems": _connection(
                    [
                        {
                            "id": "gid://shopify/LineItem/70000000202",
                            "product": None,
                            "variant": None,
                            "title": "Item 2",
                            "variantTitle": None,
                            "sku": None,
                            "quantity": 1,
                            "isGiftCard": False,
                            "originalUnitPriceSet": None,
                        }
                    ]
                )
            }
        }
    }
    client = _FakeClient(paginate_nodes=[order], graphql_queue=[follow_up_response])
    with psycopg.connect(DATABASE_URL) as conn:
        _, line_item_count, *_rest = pull_orders(client, conn)

    assert line_item_count == 2
    assert _fetch_one("order_line_items", 70000000201) is not None
    assert _fetch_one("order_line_items", 70000000202) is not None
    assert len(client.graphql_calls) == 1
    assert client.graphql_calls[0][1]["after"] == "cursor-1"
