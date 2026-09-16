"""Tests for shopify_data.get_order and the lookup_order function_tool
(M4 order-lookup slice — tasks 3-4 only, see shopify_data.py / agents/order_status.py).

Exercised against a real Postgres, real `shopify.*` rows (no fixtures were
added for this slice — it reads whatever shopify_sync.pull last populated),
matching the no-mocked-database convention in test_call_json_api.py.

Run from the repository root:

    .venv/bin/python -m pytest tests/test_shopify_tools.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent"))
load_dotenv(REPO / ".env")

import psycopg  # noqa: E402

from agents.order_status import OrderStatusAgent  # noqa: E402
from events import EventLog, EventType  # noqa: E402
from shopify_data import get_order  # noqa: E402
from state import CallState, SlotStatus  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "")

NOT_A_REAL_ORDER_NUMBER = "not-a-real-order-number-xyz"


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


@pytest.fixture
def sample_order_number():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT order_number FROM shopify.orders LIMIT 1")
            row = cur.fetchone()
    if row is None:
        pytest.skip("no shopify.orders rows present; run `python -m shopify_sync.pull` first")
    return row[0]


# --- shopify_data.get_order --------------------------------------------------


def test_get_order_returns_order_with_nested_related_data(sample_order_number):
    with psycopg.connect(DATABASE_URL) as conn:
        order = get_order(conn, sample_order_number)

    assert order is not None
    assert order["order_number"] == sample_order_number
    assert isinstance(order["line_items"], list)
    assert isinstance(order["fulfillments"], list)
    assert isinstance(order["refunds"], list)


def test_get_order_line_items_belong_to_the_order(sample_order_number):
    with psycopg.connect(DATABASE_URL) as conn:
        order = get_order(conn, sample_order_number)

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM shopify.order_line_items WHERE order_id = %s",
                (order["shopify_id"],),
            )
            expected_count = cur.fetchone()[0]

    assert len(order["line_items"]) == expected_count


def test_get_order_not_found_returns_none():
    with psycopg.connect(DATABASE_URL) as conn:
        order = get_order(conn, NOT_A_REAL_ORDER_NUMBER)
    assert order is None


# --- lookup_order function_tool ----------------------------------------------


def test_lookup_order_tool_fills_slot_and_returns_summary(sample_order_number):
    log = EventLog()
    state = CallState(call_id="c_test_order_lookup")
    agent = OrderStatusAgent(base_instructions=BASE, event_log=log)

    result = asyncio.run(agent.lookup_order(_Ctx(state), sample_order_number))

    assert sample_order_number in result
    assert "financial status" in result
    assert state.slots.order_status.order_id.value == sample_order_number
    assert state.slots.order_status.order_id.status == SlotStatus.FILLED

    slot_events = [e for e in log.events if e.type is EventType.SLOT_SET]
    assert len(slot_events) == 1
    assert slot_events[0].payload == {
        "slot": "order_id",
        "value": sample_order_number,
        "by_agent": "OrderStatusAgent",
    }


def test_lookup_order_tool_strips_leading_hash(sample_order_number):
    log = EventLog()
    state = CallState(call_id="c_test_order_lookup_hash")
    agent = OrderStatusAgent(base_instructions=BASE, event_log=log)

    result = asyncio.run(agent.lookup_order(_Ctx(state), f"#{sample_order_number}"))

    assert "no order found" not in result.lower()
    assert sample_order_number in result
    assert state.slots.order_status.order_id.status == SlotStatus.FILLED


def test_lookup_order_tool_not_found_message_and_no_slot_fill():
    log = EventLog()
    state = CallState(call_id="c_test_order_lookup_missing")
    agent = OrderStatusAgent(base_instructions=BASE, event_log=log)

    result = asyncio.run(agent.lookup_order(_Ctx(state), NOT_A_REAL_ORDER_NUMBER))

    assert "no order found" in result.lower()
    assert state.slots.order_status.order_id.status == SlotStatus.PENDING
    assert not [e for e in log.events if e.type is EventType.SLOT_SET]
