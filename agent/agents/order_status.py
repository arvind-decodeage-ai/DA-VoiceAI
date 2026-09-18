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
from shopify_data import get_order, get_order_detail
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

        # B1 fix (live call c_4524e2408263): order_id records that the customer
        # gave us a number to work with, unconditional on whether it resolved
        # to a real order — a not-found lookup is a completed attempt at this
        # slot, not "never asked". Filling it only on the success branch (as
        # this used to do) left order_id permanently PENDING after any
        # not-found lookup, so move_to_wrap could never leave the stage for a
        # customer whose order number simply didn't match one on file.
        state.slots.order_status.order_id = Slot.fill(order_id)
        self._log.append(
            EventType.SLOT_SET,
            {"slot": "order_id", "value": order_id, "by_agent": NAME},
        )

        settings = get_settings()
        with psycopg.connect(settings.database_url, connect_timeout=5) as conn:
            order = get_order(conn, order_number)

        if order is None:
            return (
                f"No order found with number {order_id}. Ask the customer to "
                f"double-check the order number."
            )

        return _format_order_summary(order)

    @function_tool
    async def get_order_details(self, ctx: RunContext[CallState], order_id: str) -> str:
        """Look up the full detail behind an order already found with
        lookup_order — exact SKUs, a return's individual line items, a
        shipping line's source, and similar detail the spoken summary
        doesn't cover. Call this only when the customer asks for something
        `lookup_order`'s answer didn't already give you.

        Args:
            order_id: The same order number already looked up.
        """
        order_number = order_id.strip().lstrip("#")

        settings = get_settings()
        with psycopg.connect(settings.database_url, connect_timeout=5) as conn:
            order = get_order_detail(conn, order_number)

        if order is None:
            return f"No order found with number {order_id}."

        return _format_order_detail(order)

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

    @function_tool
    async def move_to_router(self, ctx: RunContext[CallState]) -> "Agent | str":
        """Move to finding out what else the customer needs, once this order
        question is resolved but the customer has something else to discuss.

        Call this instead of `move_to_wrap` when the customer says there is
        something else, not when they are done.
        """
        # Approved decision (M4 plan, RouterAgent + set_intent slice, PRD §5
        # flow: resolve -> "anything else, <3 intents" -> Router): a new tool,
        # not a rename of move_to_wrap. "Done" and "something else" are two
        # different customer answers that must reach two different
        # destinations (Wrap vs. Router), so one tool can't serve both.
        state = ctx.userdata
        slots = state.slots.order_status
        if not slots.issue_type.is_resolved:
            slots.issue_type = Slot.mark_unavailable()

        if not state.can_leave_stage():
            missing = ", ".join(state.blocking_slots())
            return (
                f"Not yet — still missing: {missing}. Ask the customer for it "
                f"first, then call this again."
            )

        # Deferred import: RouterAgent imports OrderStatusAgent (to hand off on
        # set_intent), so importing RouterAgent at module level here would be
        # circular.
        from agents.router import NAME as ROUTER_NAME
        from agents.router import RouterAgent

        self._log.append(
            EventType.AGENT_HANDOFF, {"from": NAME, "to": ROUTER_NAME}
        )
        state.stage = Stage.ROUTE
        return RouterAgent(
            base_instructions=self._base_instructions, event_log=self._log
        )


def _format_order_summary(order: dict[str, Any]) -> str:
    """Renders `shopify_data.get_order()`'s narrow, speech-shaped payload —
    not raw columns. See that module for the display-enum -> wording
    mapping and the currency-aware money formatting."""
    parts = [f"Order {order['order_number']}: {order['status_phrase']}."]

    items = order.get("items") or []
    if items:
        item_text = "; ".join(f"{i['quantity']}x {i['title']}" for i in items)
        parts.append(f"Items: {item_text}.")

    tracking = order.get("tracking")
    if tracking:
        parts.append(f"Tracking: {tracking['carrier'] or 'carrier'} {tracking['number']}.")
    else:
        parts.append("No tracking number on file yet.")

    if order.get("return_or_refund"):
        parts.append(order["return_or_refund"].capitalize() + ".")

    if order.get("total"):
        parts.append(f"Order total: {order['total']}.")

    return " ".join(parts)


def _format_order_detail(order: dict[str, Any]) -> str:
    """Renders `shopify_data.get_order_detail()`'s wide payload — every
    column, every nested row. For the drill-in tool only; not sent by
    default (see `_format_order_summary`)."""
    parts = [
        f"Order {order['order_number']} (id {order['shopify_id']}): "
        f"{order['display_financial_status']} / {order['display_fulfillment_status']}."
    ]

    line_items = order.get("line_items") or []
    if line_items:
        item_text = "; ".join(
            f"{li['quantity']}x {li['title']}"
            + (f" ({li['variant_title']})" if li.get("variant_title") else "")
            + (f" sku {li['sku']}" if li.get("sku") else "")
            for li in line_items
        )
        parts.append(f"Items: {item_text}.")

    fulfillments = order.get("fulfillments") or []
    for f in fulfillments:
        tracking = (
            f", tracking {f.get('tracking_company') or 'carrier'} {f['tracking_number']}"
            if f.get("tracking_number")
            else ""
        )
        parts.append(f"Fulfillment: {f.get('display_status') or f.get('status')}{tracking}.")

    refunds = order.get("refunds") or []
    for r in refunds:
        parts.append(f"Refund: {r.get('amount')} on {r.get('created_at')} ({r.get('note') or 'no note'}).")

    returns = order.get("returns") or []
    for ret in returns:
        line_item_text = "; ".join(
            f"{rli['quantity']}x return line item" for rli in ret.get("return_line_items") or []
        )
        parts.append(f"Return {ret['name']}: {ret['status']}." + (f" Items: {line_item_text}." if line_item_text else ""))

    shipping_lines = order.get("shipping_lines") or []
    for sl in shipping_lines:
        parts.append(f"Shipping line: {sl.get('title')} (source: {sl.get('source')}).")

    return " ".join(parts)
