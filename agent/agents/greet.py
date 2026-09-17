"""GreetAgent — opens the call and captures who is speaking (PRD §5, M3 T6).

Captures ``name`` and ``identity_confirmed``, then hands off to Wrap. The
handoff is gated in code: PRD §5 requires that Wrap is reachable only once the
stage's required slots are resolved, and "this is enforced in code, not in the
prompt". The tool below refuses while anything is outstanding, so the model
cannot talk its way past it — the worst it can do is ask the customer again.

Identification is conversational. ``identify_customer``, the fixture-backed
lookup in PRD §5, is deferred to M4 with the rest of the fixture tooling
(M3 plan decision D2), so nothing here claims to look a customer up.
"""

from __future__ import annotations

from livekit.agents import Agent, RunContext, function_tool

from agents import compose_instructions
from agents.router import NAME as ROUTER_NAME
from agents.router import RouterAgent
from agents.wrap import NAME as WRAP_NAME
from agents.wrap import WrapAgent
from events import EventLog, EventType
from state import CallState, Slot, Stage

NAME = "GreetAgent"


class GreetAgent(Agent):
    def __init__(self, *, base_instructions: str, event_log: EventLog) -> None:
        super().__init__(instructions=compose_instructions(base_instructions, "greet"))
        self._base_instructions = base_instructions
        self._log = event_log

    @function_tool
    async def record_caller_name(self, ctx: RunContext[CallState], name: str) -> str:
        """Record the customer's name once they have given it.

        Args:
            name: The customer's name, exactly as they said it.
        """
        # One live call (c_1becfed0f253, 2026-09-16) finished with empty slots
        # and no sign of this tool firing; not reproduced since, and
        # c_fe37d4dde720 captured the name correctly. That run predated
        # LK_OPENAI_DEBUG so no request payload exists, and isolated probes
        # found the schema, model, streaming and token cap all working.
        # Unresolved and inconclusive rather than fixed.
        state = ctx.userdata
        state.greet.name = Slot.fill(name)
        state.caller.name = state.greet.name.value
        self._log.append(
            EventType.SLOT_SET,
            {"slot": "name", "value": state.greet.name.value, "by_agent": NAME},
        )
        return f"Noted the name as {state.greet.name.value}. Read it back to confirm."

    @function_tool
    async def confirm_identity(self, ctx: RunContext[CallState], confirmed: bool) -> str:
        """Record whether the customer confirmed the name you read back.

        Args:
            confirmed: True if they confirmed it; False if they declined to give
                a name or said it was wrong.
        """
        state = ctx.userdata
        # A refusal is an answer: UNAVAILABLE resolves the slot so the call can
        # continue, while staying distinguishable from "never asked".
        state.greet.identity_confirmed = (
            Slot.fill("yes") if confirmed else Slot.mark_unavailable()
        )
        if not confirmed:
            state.greet.name = Slot.mark_unavailable()
            state.caller.name = None
        self._log.append(
            EventType.SLOT_SET,
            {
                "slot": "identity_confirmed",
                "value": state.greet.identity_confirmed.value,
                "by_agent": NAME,
            },
        )
        return "Identity step complete. Continue helping the customer."

    @function_tool
    async def move_to_wrap(self, ctx: RunContext[CallState]) -> "Agent | str":
        """Move to closing the call, once the customer is finished.

        Call this when the customer signals the conversation is over.
        """
        state = ctx.userdata
        if not state.can_leave_stage():
            missing = ", ".join(state.blocking_slots())
            # Returning a string sends this back to the model, not the customer.
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
    async def route_to_router(self, ctx: RunContext[CallState]) -> "Agent | str":
        """Move to finding out what the customer needs help with.

        Call this once identity is confirmed and the customer has something
        they want help with.
        """
        state = ctx.userdata
        # Same identity gate as move_to_wrap: PRD §5 identifies the customer
        # before moving on to anything else, and this reuses that exact check
        # rather than inventing a second one.
        if not state.can_leave_stage():
            missing = ", ".join(state.blocking_slots())
            return (
                f"Not yet — still missing: {missing}. Ask the customer for it "
                f"first, then call this again."
            )

        self._log.append(
            EventType.AGENT_HANDOFF, {"from": NAME, "to": ROUTER_NAME}
        )
        state.stage = Stage.ROUTE
        return RouterAgent(
            base_instructions=self._base_instructions, event_log=self._log
        )
