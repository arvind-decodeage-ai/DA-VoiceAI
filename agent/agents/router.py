"""RouterAgent — decides which intent a call is about (PRD §5, M4).

The only tool is `set_intent`. It replaces GreetAgent's temporary
`route_to_order_status` shortcut (M3): Greet no longer decides where the call
goes, it only reaches Router once identity is confirmed, and Router is the one
place `CallState.start_intent`/`can_start_intent` are exercised (declared in
M3, unused until now).

This slice only wires `order_status` to a real handoff — the other five
`Intent` values are M5 work. `set_intent` still accepts them (records the
slot, matches the PRD-table enum), it just has nowhere real to send the
caller yet, so it says so and stays in Router. Building throwaway stub agents
now for intents M5 will implement for real is more code than this
milestone's approved scope (RouterAgent + set_intent) calls for.
"""

from __future__ import annotations

from livekit.agents import Agent, RunContext, function_tool

from agents import compose_instructions
from agents.order_status import NAME as ORDER_STATUS_NAME
from agents.order_status import OrderStatusAgent
from agents.wrap import NAME as WRAP_NAME
from agents.wrap import WrapAgent
from events import EventLog, EventType
from state import CallState, Intent, Slot, Stage

NAME = "RouterAgent"

# Intents with a real resolve-stage agent in this slice. The rest of the
# Intent enum is valid input to set_intent (PRD §5 table) but has no handoff
# target until M5.
_ROUTABLE = {Intent.ORDER_STATUS}


class RouterAgent(Agent):
    def __init__(self, *, base_instructions: str, event_log: EventLog) -> None:
        super().__init__(instructions=compose_instructions(base_instructions, "router"))
        self._base_instructions = base_instructions
        self._log = event_log

    @function_tool
    async def set_intent(
        self, ctx: RunContext[CallState], intent: str, reason: str
    ) -> "Agent | str":
        """Record what the customer wants help with, and route them there.

        Args:
            intent: One of order_status, product_info, returns_refund,
                subscription, complaint, fallback. Pick the closest match to
                what the customer actually said.
            reason: A short phrase capturing why you picked this intent, in
                your own words.
        """
        try:
            parsed = Intent(intent)
        except ValueError:
            return (
                "That is not a recognised intent. Ask the customer what they "
                "need and call this again with one of the known categories."
            )

        state = ctx.userdata
        state.router.intent = Slot.fill(parsed.value)
        state.router.intent_reason = Slot.fill(reason)
        self._log.append(
            EventType.SLOT_SET,
            {"slot": "intent", "value": parsed.value, "by_agent": NAME},
        )
        self._log.append(
            EventType.SLOT_SET,
            {"slot": "intent_reason", "value": reason, "by_agent": NAME},
        )

        # Approved decision (M4 plan, RouterAgent + set_intent slice): the
        # 3-intent cap is checked *before* start_intent, never after — past
        # the cap the call goes to Wrap, not Fallback (PRD §5: Fallback is for
        # an unrecognised intent twice, a different condition). Checking here
        # keeps CallState.start_intent's own guard (raises past the cap) as a
        # true "should never happen" backstop rather than the primary gate.
        if not state.can_start_intent(parsed):
            self._log.append(
                EventType.AGENT_HANDOFF, {"from": NAME, "to": WRAP_NAME}
            )
            state.stage = Stage.WRAP
            return WrapAgent(
                base_instructions=self._base_instructions, event_log=self._log
            )

        if parsed not in _ROUTABLE:
            return (
                f"{parsed.value} is not available on this call yet. Help the "
                f"customer with something else, or ask if there's anything "
                f"more before closing."
            )

        state.start_intent(parsed)
        state.stage = Stage.RESOLVE
        self._log.append(
            EventType.AGENT_HANDOFF, {"from": NAME, "to": ORDER_STATUS_NAME}
        )
        return OrderStatusAgent(
            base_instructions=self._base_instructions, event_log=self._log
        )
