"""OrderStatusAgent — order lookups against the synced Shopify data.

Minimal skeleton for this slice: just enough to host `lookup_order`. Stage
requirements, routing, and handoff logic are the rest of M4 (T1-T9) and are
not started here.
"""

from __future__ import annotations

from typing import Any

import psycopg
from livekit.agents import Agent, RunContext, function_tool

from agents import compose_instructions
from agents.wrap import NAME as WRAP_NAME
from agents.wrap import WrapAgent
from config import get_settings
from events import EventLog, EventType
from shopify_data import get_order
from state import CallState, Slot, Stage

NAME = "OrderStatusAgent"


class OrderStatusAgent(Agent):
    def __init__(self, *, base_instructions: str, event_log: EventLog) -> None:
        super().__init__(instructions=compose_instructions(base_instructions, "order_status"))
        self._base_instructions = base_instructions
        self._log = event_log

    @function_tool
    async def lookup_order(self, ctx: RunContext[CallState], order_id: str) -> str:
        """Look up an order by the order number the customer gives you.

        Args:
            order_id: The order number as the customer said it (e.g. "52428").
                Strip any leading "#" before passing it here.
        """
        state = ctx.userdata
        order_number = order_id.strip().lstrip("#")

        settings = get_settings()
        with psycopg.connect(settings.database_url, connect_timeout=5) as conn:
            order = get_order(conn, order_number)

        if order is None:
            return (
                f"No order found with number {order_id}. Ask the customer to "
                f"double-check the order number."
            )

        state.slots.order_status.order_id = Slot.fill(order_id)
        self._log.append(
            EventType.SLOT_SET,
            {"slot": "order_id", "value": order_id, "by_agent": NAME},
        )

        return _format_order_summary(order)

    @function_tool
    async def move_to_wrap(self, ctx: RunContext[CallState]) -> "Agent | str":
        """Move to closing the call, once the order question is resolved.

        Call this when the customer is done with this order question.
        """
        state = ctx.userdata
        # This slice has no tool that captures `issue_type` (no issue-type
        # capture was built here — see the task breakdown). Marking it
        # UNAVAILABLE reuses the existing "asked, nothing to give" semantics
        # (same as Greet's identity_confirmed) so the real code-enforced gate
        # can still resolve, rather than inventing a second gate mechanism.
        slots = state.slots.order_status
        if not slots.issue_type.is_resolved:
            slots.issue_type = Slot.mark_unavailable()

        if not state.can_leave_stage():
            missing = ", ".join(state.blocking_slots())
            return (
                f"Not yet — still missing: {missing}. Ask the customer for it "
                f"first, then call this again."
            )

        self._log.append(
            EventType.AGENT_HANDOFF, {"from": NAME, "to": WRAP_NAME}
        )
        state.stage = Stage.WRAP
        return WrapAgent(
            base_instructions=self._base_instructions, event_log=self._log
        )


def _format_order_summary(order: dict[str, Any]) -> str:
    parts = [
        f"Order {order['order_number']}: financial status {order['financial_status']}, "
        f"fulfillment status {order['fulfillment_status']}."
    ]

    line_items = order.get("line_items") or []
    if line_items:
        items = "; ".join(f"{li['quantity']}x {li['title']}" for li in line_items)
        parts.append(f"Items: {items}.")

    fulfillments = order.get("fulfillments") or []
    tracked = [f for f in fulfillments if f.get("tracking_number")]
    if tracked:
        tracking = "; ".join(
            f"{f.get('tracking_company') or 'carrier'} {f['tracking_number']}"
            for f in tracked
        )
        parts.append(f"Tracking: {tracking}.")
    elif fulfillments:
        parts.append("Fulfillment exists but no tracking number is on file yet.")
    else:
        parts.append("Not yet fulfilled.")

    refunds = order.get("refunds") or []
    if refunds:
        total = sum(r["amount"] for r in refunds if r.get("amount") is not None)
        currency = order.get("currency") or ""
        parts.append(f"{len(refunds)} refund(s) on this order totalling {total} {currency}".strip() + ".")

    return " ".join(parts)
