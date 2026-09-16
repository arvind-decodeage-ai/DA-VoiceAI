"""WrapAgent — closes the call (PRD §5, M3 T6/T7).

Summarises, asks "Did that resolve it for you?", captures a CSAT rating, and
ends the call. Wrap is the terminal stage: nothing gates leaving it, so
``end_call`` is deliberately not slot-gated the way the Greet handoff is —
PRD §5 gives Wrap the exit condition "end_call fired", nothing more.

KNOWN M3 LIMITATION — these tools are reachable but unreached in a realistic
call. M3 implements only Greet -> Wrap; PRD §5's Router and Resolve stages are
M4/M5. So the handoff fires as soon as Greet's slots fill, and any customer who
actually wants help lands in the closing agent, because there is nowhere else
to route them. Observed live in call c_fe37d4dde720 (2026-09-16): WrapAgent
spent thirteen turns handling a product complaint, never reached "Did that
resolve it for you?", and so confirm_resolution and record_csat were offered on
all 17 post-handoff requests and called zero times — leaving resolution.status
and csat null. Structural, not a defect: M4's Router is the fix, not a tighter
gate here.

The closing sequence is therefore only verifiable in a scripted call where the
customer says goodbye and does not ask for help. end_call has still never run
live (that same call ended via participant disconnect, reason
"session_shutdown"), so its wait_for_playout guarantee remains unverified
rather than failed.
"""

from __future__ import annotations

from livekit.agents import Agent, RunContext, function_tool

from agents import compose_instructions
from events import EventLog, EventType
from state import CallState, ResolutionStatus, Slot

NAME = "WrapAgent"

CSAT_MIN = 1
CSAT_MAX = 5


class WrapAgent(Agent):
    def __init__(self, *, base_instructions: str, event_log: EventLog) -> None:
        super().__init__(instructions=compose_instructions(base_instructions, "wrap"))
        self._base_instructions = base_instructions
        self._log = event_log

    @function_tool
    async def confirm_resolution(self, ctx: RunContext[CallState], resolved: bool) -> str:
        """Record the customer's answer to "Did that resolve it for you?".

        Args:
            resolved: True if the customer said their issue was resolved,
                False if they said it was not.
        """
        state = ctx.userdata
        state.wrap.summary_confirmed = Slot.fill("yes" if resolved else "no")

        if resolved:
            state.resolution.status = ResolutionStatus.RESOLVED
        # When the customer says it was NOT resolved, resolution.status is left
        # unset on purpose. §8's enum is resolved | escalated | callback |
        # abandoned | timeout, and none of them describes "the call completed
        # normally and the customer was not satisfied": there is no escalation
        # path in M3 and the persona promises no transfers, nothing scheduled a
        # callback, no timer fired, and `abandoned` is already how T5 marks a
        # final user turn that went unanswered — reusing it here would make that
        # signal ambiguous. The answer is not lost: it is in this event and in
        # slots.summary_confirmed. Whether §8 needs a sixth value is a schema
        # decision, recorded as an open item rather than invented here.
        self._log.append(
            EventType.SLOT_SET,
            {
                "slot": "summary_confirmed",
                "value": state.wrap.summary_confirmed.value,
                "resolved": resolved,
                "by_agent": NAME,
            },
        )
        if resolved:
            return "Resolution confirmed. Ask the customer to rate the call from one to five."
        return (
            "Noted that it was not resolved. Do not promise a fix, a transfer or a "
            "callback. Ask the customer to rate the call from one to five."
        )

    @function_tool
    async def record_csat(self, ctx: RunContext[CallState], rating: int) -> str:
        """Record the customer's satisfaction rating, one to five.

        Args:
            rating: The rating the customer gave, from 1 to 5.
        """
        if not isinstance(rating, int) or not CSAT_MIN <= rating <= CSAT_MAX:
            # Returned to the model, not the customer: ask again rather than
            # writing a value the §8 schema would reject.
            return (
                f"That is not a valid rating. Ask the customer for a number "
                f"between {CSAT_MIN} and {CSAT_MAX}."
            )

        state = ctx.userdata
        # The slot records *that* a rating was captured, so the wrap stage reads
        # as complete; the number itself lives in the event, which is what
        # JSONBuilder reads for §8's integer "csat".
        state.wrap.csat = Slot.fill(str(rating))
        self._log.append(EventType.CSAT_RECORDED, {"csat": rating, "by_agent": NAME})
        return f"Rating of {rating} recorded. Thank the customer and close the call."

    @function_tool
    async def end_call(self, ctx: RunContext[CallState]) -> None:
        """End the call once the customer is done and has been thanked."""
        # PRD §10 M3: "end_call waits for TTS flush before closing". This waits
        # for the assistant's spoken response *preceding* this tool call — the
        # closing line — so the customer hears it in full before the room goes.
        await ctx.wait_for_playout()

        self._log.append(EventType.CALL_ENDED, {"reason": "end_call", "by_agent": NAME})

        # Closing the session makes the agent leave; the browser sees the
        # disconnect and calls DELETE /calls/{id}, which is the M2 T6 teardown
        # path (M3 plan D6 — no new room-closing mechanism).
        await ctx.session.aclose()
