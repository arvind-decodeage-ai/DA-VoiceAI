"""Shopify sync schema migration.

Creates the `shopify` Postgres schema and its tables (customers, orders,
order_line_items, fulfillments, refunds, sync_runs) against DATABASE_URL.
Safe to run more than once (CREATE SCHEMA/TABLE IF NOT EXISTS).

Entirely separate from db/migrate.py: this only ever touches the `shopify`
schema, never the `public` schema that calls/turns/events/slots live in.

Run with:

    python -m shopify_sync.migrate
"""

from __future__ import annotations

import logging

import psycopg

from shopify_sync.config import get_settings

logger = logging.getLogger("shopify_sync.migrate")
logging.basicConfig(level=logging.INFO)

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS shopify;

CREATE TABLE IF NOT EXISTS shopify.customers (
    shopify_id   BIGINT PRIMARY KEY,
    email        TEXT,
    phone        TEXT,
    first_name   TEXT,
    last_name    TEXT,
    created_at   TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ,
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shopify.orders (
    shopify_id         BIGINT PRIMARY KEY,
    order_number       TEXT,
    customer_id        BIGINT REFERENCES shopify.customers(shopify_id) ON DELETE SET NULL,
    email              TEXT,
    phone              TEXT,
    financial_status   TEXT,
    fulfillment_status TEXT,
    currency           TEXT,
    total_price        NUMERIC,
    created_at         TIMESTAMPTZ,
    cancelled_at       TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ,
    synced_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shopify.order_line_items (
    shopify_id   BIGINT PRIMARY KEY,
    order_id     BIGINT NOT NULL REFERENCES shopify.orders(shopify_id) ON DELETE CASCADE,
    product_id   BIGINT,
    variant_id   BIGINT,
    title        TEXT,
    sku          TEXT,
    quantity     INTEGER,
    price        NUMERIC,
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shopify.fulfillments (
    shopify_id        BIGINT PRIMARY KEY,
    order_id          BIGINT NOT NULL REFERENCES shopify.orders(shopify_id) ON DELETE CASCADE,
    status            TEXT,
    tracking_company  TEXT,
    tracking_number   TEXT,
    tracking_url      TEXT,
    created_at        TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ,
    synced_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shopify.refunds (
    shopify_id   BIGINT PRIMARY KEY,
    order_id     BIGINT NOT NULL REFERENCES shopify.orders(shopify_id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ,
    note         TEXT,
    amount       NUMERIC,
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shopify.sync_runs (
    id               BIGSERIAL PRIMARY KEY,
    resource         TEXT NOT NULL,
    started_at       TIMESTAMPTZ NOT NULL,
    finished_at      TIMESTAMPTZ,
    status           TEXT NOT NULL,
    records_synced   INTEGER NOT NULL DEFAULT 0,
    high_water_mark  TIMESTAMPTZ,
    error            TEXT
);

CREATE INDEX IF NOT EXISTS idx_shopify_orders_customer_id ON shopify.orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_shopify_line_items_order_id ON shopify.order_line_items(order_id);
CREATE INDEX IF NOT EXISTS idx_shopify_fulfillments_order_id ON shopify.fulfillments(order_id);
CREATE INDEX IF NOT EXISTS idx_shopify_refunds_order_id ON shopify.refunds(order_id);
CREATE INDEX IF NOT EXISTS idx_shopify_sync_runs_resource ON shopify.sync_runs(resource, started_at);
"""


def migrate() -> None:
    settings = get_settings()
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
    logger.info(
        "migration applied: shopify schema (customers, orders, order_line_items, "
        "fulfillments, refunds, sync_runs)"
    )


if __name__ == "__main__":
    migrate()
