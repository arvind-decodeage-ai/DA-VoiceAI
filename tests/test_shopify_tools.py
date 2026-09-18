"""Tests for shopify_data.get_order()/get_order_detail() and the
lookup_order/get_order_details function_tools (M4 order-lookup slice,
extended by the REST->GraphQL migration's Step 5 narrow/wide payload
rewrite).

Exercised against a real Postgres (migrate() run first) with known fake
rows seeded directly by this file — not against whatever a live sync last
populated, since no real GraphQL sync has run against this schema yet, and
the old REST-synced rows don't carry the new display-enum/MoneyBag columns
this rewrite reads. Matches the no-mocked-database convention used
throughout this migration (test_shopify_pull.py, test_shopify_migrate.py).

Run from the repository root:

    .venv/bin/python -m pytest tests/test_shopify_tools.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent"))
load_dotenv(REPO / ".env")

import psycopg  # noqa: E402

from agents.order_status import OrderStatusAgent  # noqa: E402
from agents.wrap import WrapAgent  # noqa: E402
from events import EventLog, EventType  # noqa: E402
from shopify_data import get_order, get_order_detail  # noqa: E402
from shopify_sync.migrate import migrate  # noqa: E402
from state import CallState, Intent, SlotStatus, Stage  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "")

NOT_A_REAL_ORDER_NUMBER = "not-a-real-order-number-xyz"

# Fake ids in this file sit in this range, clear of any real Shopify id
# (14+ digits) and clear of test_shopify_pull.py's 90xxxxxxxxx/80xxxxxxxxx
# ranges. ON DELETE CASCADE from orders/customers cleans up every child row.
_ORDER_ID = 91_000_000_001
_ORDER_NUMBER = "91001"
_CUSTOMER_ID = 81_000_000_001
_LINE_ITEM_ID = 71_000_000_001
_FULFILLMENT_ID = 41_000_000_001
_REFUND_ID = 31_000_000_001
_RETURN_ID = 21_000_000_001
_RETURN_LINE_ITEM_ID = 11_000_000_001
_SHIPPING_LINE_ID = 1_000_002


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


class _Ctx:
    """Stands in for RunContext; the tool only ever reads `userdata`."""

    def __init__(self, state: CallState) -> None:
        self.userdata = state


BASE = "BASE PERSONA TEXT"


def _seed_full_order(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO shopify.customers (shopify_id, email, first_name, last_name)
            VALUES (%s, 'seed@example.com', 'Seed', 'Customer')
            """,
            (_CUSTOMER_ID,),
        )
        cur.execute(
            """
            INSERT INTO shopify.orders
                (shopify_id, order_number, customer_id, display_financial_status,
                 display_fulfillment_status, total_price_shop_amount,
                 total_price_shop_currency)
            VALUES (%s, %s, %s, 'PAID', 'FULFILLED', %s, 'INR')
            """,
            (_ORDER_ID, _ORDER_NUMBER, _CUSTOMER_ID, Decimal("1606.00")),
        )
        cur.execute(
            """
            INSERT INTO shopify.order_line_items
                (shopify_id, order_id, title, variant_title, sku, quantity, price)
            VALUES (%s, %s, 'β-NMN', '250mg', 'NMN-250', 2, %s)
            """,
            (_LINE_ITEM_ID, _ORDER_ID, Decimal("803.00")),
        )
        cur.execute(
            """
            INSERT INTO shopify.fulfillments
                (shopify_id, order_id, status, display_status, tracking_company,
                 tracking_number, tracking_url)
            VALUES (%s, %s, 'SUCCESS', 'DELIVERED', 'Blue Dart', 'BD123', 'https://track/BD123')
            """,
            (_FULFILLMENT_ID, _ORDER_ID),
        )
        cur.execute(
            """
            INSERT INTO shopify.refunds (shopify_id, order_id, note, amount)
            VALUES (%s, %s, 'Customer requested', %s)
            """,
            (_REFUND_ID, _ORDER_ID, Decimal("250.50")),
        )
        cur.execute(
            """
            INSERT INTO shopify.returns (shopify_id, order_id, name, status)
            VALUES (%s, %s, 'R1', 'OPEN')
            """,
            (_RETURN_ID, _ORDER_ID),
        )
        cur.execute(
            """
            INSERT INTO shopify.return_line_items (shopify_id, return_id, quantity)
            VALUES (%s, %s, 1)
            """,
            (_RETURN_LINE_ITEM_ID, _RETURN_ID),
        )
        cur.execute(
            """
            INSERT INTO shopify.shipping_lines (shopify_id, order_id, title, source)
            VALUES (%s, %s, 'Standard', 'shopify')
            """,
            (_SHIPPING_LINE_ID, _ORDER_ID),
        )
    conn.commit()


@pytest.fixture
def seeded_order():
    migrate()
    with psycopg.connect(DATABASE_URL) as conn:
        _seed_full_order(conn)
    yield _ORDER_NUMBER
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM shopify.orders WHERE shopify_id = %s", (_ORDER_ID,))
            cur.execute("DELETE FROM shopify.customers WHERE shopify_id = %s", (_CUSTOMER_ID,))
        conn.commit()


# --- shopify_data.get_order (narrow, speech-shaped) --------------------------


def test_get_order_returns_the_narrow_speech_shaped_payload(seeded_order):
    with psycopg.connect(DATABASE_URL) as conn:
        order = get_order(conn, seeded_order)

    assert order == {
        "order_number": "91001",
        "status_phrase": "paid and fulfilled",
        "items": [{"title": "β-NMN", "quantity": 2}],
        "tracking": {"carrier": "Blue Dart", "number": "BD123", "url": "https://track/BD123"},
        "return_or_refund": "one refund of ₹250.50 issued; a return is open",
        "total": "₹1,606.00",
    }


def test_get_order_status_phrase_covers_legacy_fulfillment_enum_members(seeded_order):
    """OPEN/PENDING_FULFILLMENT/RESTOCKED are marked legacy but still
    returned by the API -- must still map to a real phrase, not fall
    through to the raw enum string."""
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE shopify.orders SET display_fulfillment_status = %s WHERE shopify_id = %s",
                ("RESTOCKED", _ORDER_ID),
            )
        conn.commit()
        order = get_order(conn, seeded_order)
    assert order["status_phrase"] == "paid and restocked"


def test_get_order_not_found_returns_none():
    with psycopg.connect(DATABASE_URL) as conn:
        order = get_order(conn, NOT_A_REAL_ORDER_NUMBER)
    assert order is None


def test_get_order_with_no_tracking_or_refund_or_return_omits_them(seeded_order):
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM shopify.fulfillments WHERE order_id = %s", (_ORDER_ID,))
            cur.execute("DELETE FROM shopify.refunds WHERE order_id = %s", (_ORDER_ID,))
            cur.execute("DELETE FROM shopify.return_line_items WHERE return_id = %s", (_RETURN_ID,))
            cur.execute("DELETE FROM shopify.returns WHERE order_id = %s", (_ORDER_ID,))
        conn.commit()
        order = get_order(conn, seeded_order)

    assert order["tracking"] is None
    assert order["return_or_refund"] is None


# --- shopify_data.get_order_detail (wide) -------------------------------------


def test_get_order_detail_returns_every_column_and_nested_row(seeded_order):
    with psycopg.connect(DATABASE_URL) as conn:
        order = get_order_detail(conn, seeded_order)

    assert order is not None
    assert order["order_number"] == "91001"
    assert order["display_financial_status"] == "PAID"
    assert len(order["line_items"]) == 1
    assert order["line_items"][0]["variant_title"] == "250mg"
    assert len(order["fulfillments"]) == 1
    assert order["fulfillments"][0]["display_status"] == "DELIVERED"
    assert len(order["refunds"]) == 1
    assert len(order["returns"]) == 1
    assert order["returns"][0]["return_line_items"][0]["quantity"] == 1
    assert len(order["shipping_lines"]) == 1
    assert order["shipping_lines"][0]["source"] == "shopify"


def test_get_order_detail_not_found_returns_none():
    with psycopg.connect(DATABASE_URL) as conn:
        assert get_order_detail(conn, NOT_A_REAL_ORDER_NUMBER) is None


# --- lookup_order function_tool ----------------------------------------------


def test_lookup_order_tool_fills_slot_and_returns_the_narrow_summary(seeded_order):
    log = EventLog()
    state = CallState(call_id="c_test_order_lookup")
    agent = OrderStatusAgent(base_instructions=BASE, event_log=log)

    result = asyncio.run(agent.lookup_order(_Ctx(state), seeded_order))

    assert seeded_order in result
    assert "paid and fulfilled" in result
    assert "Blue Dart" in result
    assert state.slots.order_status.order_id.value == seeded_order
    assert state.slots.order_status.order_id.status == SlotStatus.FILLED

    slot_events = [e for e in log.events if e.type is EventType.SLOT_SET]
    assert len(slot_events) == 1
    assert slot_events[0].payload == {
        "slot": "order_id",
        "value": seeded_order,
        "by_agent": "OrderStatusAgent",
    }


def test_lookup_order_tool_strips_leading_hash(seeded_order):
    log = EventLog()
    state = CallState(call_id="c_test_order_lookup_hash")
    agent = OrderStatusAgent(base_instructions=BASE, event_log=log)

    result = asyncio.run(agent.lookup_order(_Ctx(state), f"#{seeded_order}"))

    assert "no order found" not in result.lower()
    assert seeded_order in result
    assert state.slots.order_status.order_id.status == SlotStatus.FILLED


def test_lookup_order_tool_not_found_message_still_fills_the_slot():
    """B1 fix: order_id records that the customer gave us a number to work
    with, regardless of whether it resolved to a real order — a not-found
    lookup is still a real, completed attempt, not "never asked". Before the
    fix, order_id stayed PENDING here, which is exactly the bug that left
    move_to_wrap permanently blocked on a not-found order (see the
    move_to_wrap test below)."""
    log = EventLog()
    state = CallState(call_id="c_test_order_lookup_missing")
    agent = OrderStatusAgent(base_instructions=BASE, event_log=log)

    result = asyncio.run(agent.lookup_order(_Ctx(state), NOT_A_REAL_ORDER_NUMBER))

    assert "no order found" in result.lower()
    assert state.slots.order_status.order_id.status == SlotStatus.FILLED
    assert state.slots.order_status.order_id.value == NOT_A_REAL_ORDER_NUMBER

    slot_events = [e for e in log.events if e.type is EventType.SLOT_SET]
    assert len(slot_events) == 1
    assert slot_events[0].payload == {
        "slot": "order_id",
        "value": NOT_A_REAL_ORDER_NUMBER,
        "by_agent": "OrderStatusAgent",
    }


def test_lookup_order_not_found_then_move_to_wrap_succeeds():
    """B1: a real live-call bug (c_4524e2408263) — lookup_order was called
    with a genuine order number that just didn't match any order, and
    move_to_wrap then refused with "still missing: order_id" because
    Slot.fill only ran on the success branch. A not-found lookup is still a
    completed attempt at the slot, so move_to_wrap must be reachable
    afterward (issue_type is marked UNAVAILABLE by move_to_wrap itself, same
    as the found-order path)."""
    log = EventLog()
    state = CallState(call_id="c_test_order_lookup_missing_then_wrap")
    state.start_intent(Intent.ORDER_STATUS)
    state.stage = Stage.RESOLVE
    agent = OrderStatusAgent(base_instructions=BASE, event_log=log)

    asyncio.run(agent.lookup_order(_Ctx(state), NOT_A_REAL_ORDER_NUMBER))
    result = asyncio.run(agent.move_to_wrap(_Ctx(state)))

    assert isinstance(result, WrapAgent), (
        "move_to_wrap refused with a string instead of handing off to Wrap "
        f"(got: {result!r})"
    )


# --- get_order_details function_tool (wide drill-in) --------------------------


def test_get_order_details_tool_returns_wide_detail(seeded_order):
    log = EventLog()
    state = CallState(call_id="c_test_order_detail")
    agent = OrderStatusAgent(base_instructions=BASE, event_log=log)

    result = asyncio.run(agent.get_order_details(_Ctx(state), seeded_order))

    assert seeded_order in result
    assert "NMN-250" in result  # sku, only in the wide payload
    assert "Return R1" in result
    assert "Shipping line: Standard" in result


def test_get_order_details_tool_not_found():
    log = EventLog()
    state = CallState(call_id="c_test_order_detail_missing")
    agent = OrderStatusAgent(base_instructions=BASE, event_log=log)

    result = asyncio.run(agent.get_order_details(_Ctx(state), NOT_A_REAL_ORDER_NUMBER))

    assert "no order found" in result.lower()
