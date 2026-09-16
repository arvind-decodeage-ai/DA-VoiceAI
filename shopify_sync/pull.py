"""Manual historical pull: Shopify customers + orders (with nested line items,
fulfillments, and refunds) into the `shopify` Postgres schema.

Idempotent: every record is upserted by its Shopify id, so re-running this
script never creates duplicates. Manual/on-demand only for this first
version — no cron, no webhook listener, no always-on worker. Each run is
recorded in shopify.sync_runs (start/end time, status, record count, and a
high-water mark on the resource's updated_at) so a future incremental sync
can pick up from where the last successful run left off.

Run with:

    python -m shopify_sync.pull
    python -m shopify_sync.pull --created-at-min 2026-07-01T00:00:00Z --created-at-max 2026-09-16T00:00:00Z

--created-at-min/--created-at-max scope customers and orders to their own
created_at (Shopify's created_at_min/created_at_max params on both
customers.json and orders.json). Line items, fulfillments, and refunds have
no independent date-filtered endpoint — Shopify only returns them nested
inside an order — so they're implicitly scoped by following whichever
orders matched the window, unfiltered by their own timestamps. Regardless
of the customers window, every order's embedded `customer` object is always
upserted (Shopify includes it in the order response at no extra cost) so an
order from a customer whose account predates the window still resolves its
orders.customer_id foreign key.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from typing import Any

import psycopg

from shopify_sync.client import ShopifyClient
from shopify_sync.config import get_settings
from shopify_sync.migrate import migrate

logger = logging.getLogger("shopify_sync.pull")
logging.basicConfig(level=logging.INFO)

PAGE_SIZE = "250"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


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
    high_water_mark: datetime | None,
    error: str | None = None,
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


def _upsert_customer(conn: psycopg.Connection, c: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO shopify.customers
                (shopify_id, email, phone, first_name, last_name, created_at, updated_at, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (shopify_id) DO UPDATE SET
                email = EXCLUDED.email,
                phone = EXCLUDED.phone,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                synced_at = EXCLUDED.synced_at
            """,
            (
                c["id"],
                c.get("email"),
                c.get("phone"),
                c.get("first_name"),
                c.get("last_name"),
                _parse_dt(c.get("created_at")),
                _parse_dt(c.get("updated_at")),
            ),
        )
    conn.commit()


def _date_params(created_at_min: str | None, created_at_max: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if created_at_min:
        params["created_at_min"] = created_at_min
    if created_at_max:
        params["created_at_max"] = created_at_max
    return params


def pull_customers(
    client: ShopifyClient,
    conn: psycopg.Connection,
    created_at_min: str | None = None,
    created_at_max: str | None = None,
) -> tuple[int, datetime | None]:
    count = 0
    high_water_mark: datetime | None = None
    params = {"limit": PAGE_SIZE, **_date_params(created_at_min, created_at_max)}
    for customer in client.paginate("customers.json", params, "customers"):
        _upsert_customer(conn, customer)
        count += 1
        updated_at = _parse_dt(customer.get("updated_at"))
        if updated_at and (high_water_mark is None or updated_at > high_water_mark):
            high_water_mark = updated_at
    return count, high_water_mark


def _upsert_order(conn: psycopg.Connection, o: dict[str, Any]) -> None:
    customer = o.get("customer") or {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO shopify.orders
                (shopify_id, order_number, customer_id, email, phone, financial_status,
                 fulfillment_status, currency, total_price, created_at, cancelled_at,
                 updated_at, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (shopify_id) DO UPDATE SET
                order_number = EXCLUDED.order_number,
                customer_id = EXCLUDED.customer_id,
                email = EXCLUDED.email,
                phone = EXCLUDED.phone,
                financial_status = EXCLUDED.financial_status,
                fulfillment_status = EXCLUDED.fulfillment_status,
                currency = EXCLUDED.currency,
                total_price = EXCLUDED.total_price,
                created_at = EXCLUDED.created_at,
                cancelled_at = EXCLUDED.cancelled_at,
                updated_at = EXCLUDED.updated_at,
                synced_at = EXCLUDED.synced_at
            """,
            (
                o["id"],
                o.get("order_number") or o.get("name"),
                customer.get("id"),
                o.get("email"),
                o.get("phone"),
                o.get("financial_status"),
                o.get("fulfillment_status"),
                o.get("currency"),
                o.get("total_price"),
                _parse_dt(o.get("created_at")),
                _parse_dt(o.get("cancelled_at")),
                _parse_dt(o.get("updated_at")),
            ),
        )
    conn.commit()


def _upsert_line_item(conn: psycopg.Connection, order_id: int, li: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO shopify.order_line_items
                (shopify_id, order_id, product_id, variant_id, title, sku, quantity, price, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (shopify_id) DO UPDATE SET
                order_id = EXCLUDED.order_id,
                product_id = EXCLUDED.product_id,
                variant_id = EXCLUDED.variant_id,
                title = EXCLUDED.title,
                sku = EXCLUDED.sku,
                quantity = EXCLUDED.quantity,
                price = EXCLUDED.price,
                synced_at = EXCLUDED.synced_at
            """,
            (
                li["id"],
                order_id,
                li.get("product_id"),
                li.get("variant_id"),
                li.get("title"),
                li.get("sku"),
                li.get("quantity"),
                li.get("price"),
            ),
        )
    conn.commit()


def _upsert_fulfillment(conn: psycopg.Connection, order_id: int, f: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO shopify.fulfillments
                (shopify_id, order_id, status, tracking_company, tracking_number,
                 tracking_url, created_at, updated_at, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (shopify_id) DO UPDATE SET
                order_id = EXCLUDED.order_id,
                status = EXCLUDED.status,
                tracking_company = EXCLUDED.tracking_company,
                tracking_number = EXCLUDED.tracking_number,
                tracking_url = EXCLUDED.tracking_url,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                synced_at = EXCLUDED.synced_at
            """,
            (
                f["id"],
                order_id,
                f.get("status"),
                f.get("tracking_company"),
                f.get("tracking_number"),
                f.get("tracking_url"),
                _parse_dt(f.get("created_at")),
                _parse_dt(f.get("updated_at")),
            ),
        )
    conn.commit()


def _refund_amount(refund: dict[str, Any]) -> float | None:
    transactions = refund.get("transactions") or []
    amounts = [float(t["amount"]) for t in transactions if t.get("amount") is not None]
    return sum(amounts) if amounts else None


def _upsert_refund(conn: psycopg.Connection, order_id: int, r: dict[str, Any]) -> None:
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
                r["id"],
                order_id,
                _parse_dt(r.get("created_at")),
                r.get("note"),
                _refund_amount(r),
            ),
        )
    conn.commit()


def pull_orders(
    client: ShopifyClient,
    conn: psycopg.Connection,
    created_at_min: str | None = None,
    created_at_max: str | None = None,
) -> tuple[int, int, int, int, datetime | None]:
    order_count = 0
    line_item_count = 0
    fulfillment_count = 0
    refund_count = 0
    high_water_mark: datetime | None = None

    params = {
        "limit": PAGE_SIZE,
        "status": "any",
        **_date_params(created_at_min, created_at_max),
    }
    for order in client.paginate("orders.json", params, "orders"):
        # Upserted regardless of the customers window: an order's customer may have
        # been created outside it, and this is the FK target for orders.customer_id.
        embedded_customer = order.get("customer")
        if embedded_customer:
            _upsert_customer(conn, embedded_customer)

        _upsert_order(conn, order)
        order_count += 1
        order_id = order["id"]

        for li in order.get("line_items") or []:
            _upsert_line_item(conn, order_id, li)
            line_item_count += 1

        for f in order.get("fulfillments") or []:
            _upsert_fulfillment(conn, order_id, f)
            fulfillment_count += 1

        for r in order.get("refunds") or []:
            _upsert_refund(conn, order_id, r)
            refund_count += 1

        updated_at = _parse_dt(order.get("updated_at"))
        if updated_at and (high_water_mark is None or updated_at > high_water_mark):
            high_water_mark = updated_at

    return order_count, line_item_count, fulfillment_count, refund_count, high_water_mark


def run(created_at_min: str | None = None, created_at_max: str | None = None) -> None:
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
