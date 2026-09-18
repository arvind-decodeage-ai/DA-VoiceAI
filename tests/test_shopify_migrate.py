"""Tests for the REST->GraphQL additive schema migration (shopify_sync/migrate.py).

Exercised against a real Postgres, matching the no-mocked-database
convention in test_shopify_tools.py / test_call_json_api.py.

Run from the repository root:

    .venv/bin/python -m pytest tests/test_shopify_migrate.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
load_dotenv(REPO / ".env")

import psycopg  # noqa: E402

from shopify_sync.migrate import migrate  # noqa: E402

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


def _columns(table: str) -> set[str]:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'shopify' AND table_name = %s",
                (table,),
            )
            return {row[0] for row in cur.fetchall()}


def _tables() -> set[str]:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'shopify'"
            )
            return {row[0] for row in cur.fetchall()}


def test_migrate_is_idempotent():
    """Running it twice in a row must not raise -- additive-only, re-runnable."""
    migrate()
    migrate()


def test_new_tables_exist():
    migrate()
    tables = _tables()
    assert {"returns", "return_line_items", "shipping_lines"} <= tables
    # Nothing dropped: the original six are still there.
    assert {"customers", "orders", "order_line_items", "fulfillments", "refunds", "sync_runs"} <= tables


def test_orders_gets_the_new_money_columns_for_all_five_moneybag_fields():
    migrate()
    cols = _columns("orders")
    for field in (
        "total_price",
        "subtotal_price",
        "total_discounts",
        "total_shipping_price",
        "total_refunded",
    ):
        for suffix in ("shop_amount", "shop_currency", "presentment_amount", "presentment_currency"):
            assert f"{field}_{suffix}" in cols, f"missing {field}_{suffix}"


def test_orders_gets_the_other_new_scalar_columns():
    migrate()
    cols = _columns("orders")
    assert {"processed_at", "closed_at", "tags", "display_fulfillment_status", "display_financial_status"} <= cols


def test_customers_gets_display_name_and_number_of_orders():
    migrate()
    cols = _columns("customers")
    assert {"display_name", "number_of_orders"} <= cols


def test_order_line_items_gets_variant_title_and_is_gift_card():
    migrate()
    cols = _columns("order_line_items")
    assert {"variant_title", "is_gift_card"} <= cols


def test_fulfillments_gets_display_status():
    migrate()
    assert "display_status" in _columns("fulfillments")


def test_returns_table_shape():
    migrate()
    cols = _columns("returns")
    assert {"shopify_id", "order_id", "name", "status", "created_at", "closed_at", "synced_at"} <= cols


def test_return_line_items_fks_to_returns_not_orders():
    """Approved decision: return_line_items' immediate parent is returns
    (Order.returns -> Return.returnLineItems), not orders directly."""
    migrate()
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ccu.table_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                WHERE tc.table_schema = 'shopify'
                    AND tc.table_name = 'return_line_items'
                    AND tc.constraint_type = 'FOREIGN KEY'
                """
            )
            referenced_tables = {row[0] for row in cur.fetchall()}
    assert referenced_tables == {"returns"}


def test_shipping_lines_table_shape():
    migrate()
    cols = _columns("shipping_lines")
    assert {"shopify_id", "order_id", "title", "source", "synced_at"} <= cols
