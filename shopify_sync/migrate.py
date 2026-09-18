"""Shopify sync schema migration.

Creates the `shopify` Postgres schema and its tables (customers, orders,
order_line_items, fulfillments, refunds, returns, return_line_items,
shipping_lines, sync_runs) against DATABASE_URL. Safe to run more than
once (CREATE SCHEMA/TABLE IF NOT EXISTS, ADD COLUMN IF NOT EXISTS).

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


-- ============================================================
-- REST -> GraphQL migration (2026-07). Additive only: no DROP, no RENAME,
-- no data loss. Every ADD COLUMN/new table is IF NOT EXISTS so this stays
-- re-runnable, same as the block above.
--
-- Nullability is deliberately not mirrored from the GraphQL API here (e.g.
-- totalPriceSet/totalShippingPriceSet/totalRefundedSet are MoneyBag! in the
-- schema) -- consistent with every other business-data column in this
-- file, only shopify_id PKs/FKs carry NOT NULL. A future backfill gap or a
-- field Shopify itself omits should not be able to fail the upsert.
-- ============================================================

ALTER TABLE shopify.orders
    ADD COLUMN IF NOT EXISTS processed_at              TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS closed_at                  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS tags                        TEXT,
    ADD COLUMN IF NOT EXISTS display_fulfillment_status  TEXT,
    ADD COLUMN IF NOT EXISTS display_financial_status    TEXT,

    -- totalPriceSet: MoneyBag! (non-null in the API)
    ADD COLUMN IF NOT EXISTS total_price_shop_amount            NUMERIC,
    ADD COLUMN IF NOT EXISTS total_price_shop_currency          TEXT,
    ADD COLUMN IF NOT EXISTS total_price_presentment_amount     NUMERIC,
    ADD COLUMN IF NOT EXISTS total_price_presentment_currency   TEXT,

    -- subtotalPriceSet: MoneyBag (nullable)
    ADD COLUMN IF NOT EXISTS subtotal_price_shop_amount          NUMERIC,
    ADD COLUMN IF NOT EXISTS subtotal_price_shop_currency        TEXT,
    ADD COLUMN IF NOT EXISTS subtotal_price_presentment_amount   NUMERIC,
    ADD COLUMN IF NOT EXISTS subtotal_price_presentment_currency TEXT,

    -- totalDiscountsSet: MoneyBag (nullable)
    ADD COLUMN IF NOT EXISTS total_discounts_shop_amount          NUMERIC,
    ADD COLUMN IF NOT EXISTS total_discounts_shop_currency        TEXT,
    ADD COLUMN IF NOT EXISTS total_discounts_presentment_amount   NUMERIC,
    ADD COLUMN IF NOT EXISTS total_discounts_presentment_currency TEXT,

    -- totalShippingPriceSet: MoneyBag! (non-null)
    ADD COLUMN IF NOT EXISTS total_shipping_price_shop_amount           NUMERIC,
    ADD COLUMN IF NOT EXISTS total_shipping_price_shop_currency         TEXT,
    ADD COLUMN IF NOT EXISTS total_shipping_price_presentment_amount    NUMERIC,
    ADD COLUMN IF NOT EXISTS total_shipping_price_presentment_currency  TEXT,

    -- totalRefundedSet: MoneyBag! (non-null)
    ADD COLUMN IF NOT EXISTS total_refunded_shop_amount          NUMERIC,
    ADD COLUMN IF NOT EXISTS total_refunded_shop_currency        TEXT,
    ADD COLUMN IF NOT EXISTS total_refunded_presentment_amount   NUMERIC,
    ADD COLUMN IF NOT EXISTS total_refunded_presentment_currency TEXT;

ALTER TABLE shopify.customers
    ADD COLUMN IF NOT EXISTS display_name       TEXT,
    ADD COLUMN IF NOT EXISTS number_of_orders    INTEGER;

ALTER TABLE shopify.order_line_items
    ADD COLUMN IF NOT EXISTS variant_title  TEXT,
    ADD COLUMN IF NOT EXISTS is_gift_card    BOOLEAN;

-- Fulfillment.displayStatus, matching the order-level display-enum pattern
-- (raw enum stored here; TTS wording is a read/tool-layer concern).
ALTER TABLE shopify.fulfillments
    ADD COLUMN IF NOT EXISTS display_status TEXT;

-- Return: a first-class object hanging directly off Order, so it FKs
-- straight to orders like fulfillments/refunds/order_line_items do.
CREATE TABLE IF NOT EXISTS shopify.returns (
    shopify_id    BIGINT PRIMARY KEY,
    order_id      BIGINT NOT NULL REFERENCES shopify.orders(shopify_id) ON DELETE CASCADE,
    name          TEXT,
    status        TEXT,
    created_at    TIMESTAMPTZ,
    closed_at     TIMESTAMPTZ,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_shopify_returns_order_id ON shopify.returns(order_id);

-- ReturnLineItem: FKs to returns, its true immediate parent in the
-- GraphQL nesting (Order.returns -> Return.returnLineItems), not to
-- orders directly -- matches this schema's existing convention of no
-- redundant denormalized FKs (refunds has no customer_id either, even
-- though it indirectly belongs to one via the order).
CREATE TABLE IF NOT EXISTS shopify.return_line_items (
    shopify_id    BIGINT PRIMARY KEY,
    return_id     BIGINT NOT NULL REFERENCES shopify.returns(shopify_id) ON DELETE CASCADE,
    quantity      INTEGER,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_shopify_return_line_items_return_id ON shopify.return_line_items(return_id);

-- ShippingLine: a direct Order-level connection, no intermediate entity.
CREATE TABLE IF NOT EXISTS shopify.shipping_lines (
    shopify_id    BIGINT PRIMARY KEY,
    order_id      BIGINT NOT NULL REFERENCES shopify.orders(shopify_id) ON DELETE CASCADE,
    title         TEXT,
    source        TEXT,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_shopify_shipping_lines_order_id ON shopify.shipping_lines(order_id);
"""


def migrate() -> None:
    settings = get_settings()
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
    logger.info(
        "migration applied: shopify schema (customers, orders, order_line_items, "
        "fulfillments, refunds, returns, return_line_items, shipping_lines, sync_runs)"
    )


if __name__ == "__main__":
    migrate()
