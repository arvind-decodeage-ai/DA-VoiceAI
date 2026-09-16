"""Tests for the Greet/Wrap agents and the code-enforced handoff (M3 T6).

Run from the repository root:

    .venv/bin/python -m pytest tests/test_agents.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from agents import compose_instructions, stage_prompt  # noqa: E402
from agents.greet import GreetAgent  # noqa: E402
from agents.wrap import WrapAgent  # noqa: E402
from events import EventLog, EventType  # noqa: E402
from state import CallState, ResolutionStatus, Slot, SlotStatus, Stage  # noqa: E402

BASE = "BASE PERSONA TEXT"


class _Ctx:
    """Stands in for RunContext; the tools only ever read `userdata`."""

    def __init__(self, state: CallState) -> None:
        self.userdata = state


@pytest.fixture
def setup():
    log = EventLog()
    state = CallState(call_id="c_test")
    agent = GreetAgent(base_instructions=BASE, event_log=log)
    return agent, _Ctx(state), state, log


# --------------------------------------------------------------------------
# Instructions
# --------------------------------------------------------------------------


def test_stage_prompts_load():
    assert "OPENING" in stage_prompt("greet")
    assert "CLOSING" in stage_prompt("wrap")


def test_instructions_are_base_persona_then_stage():
    composed = compose_instructions(BASE, "greet")
    assert composed.startswith(BASE)
    assert "OPENING" in composed


def test_agents_carry_their_stage_instructions():
    log = EventLog()
    greet = GreetAgent(base_instructions=BASE, event_log=log)
    wrap = WrapAgent(base_instructions=BASE, event_log=log)
    assert "OPENING" in greet.instructions and BASE in greet.instructions
    assert "CLOSING" in wrap.instructions and BASE in wrap.instructions


# --------------------------------------------------------------------------
# Slot capture
# --------------------------------------------------------------------------


def test_record_caller_name_fills_the_slot_and_logs_it(setup):
    agent, ctx, state, log = setup
    asyncio.run(agent.record_caller_name(ctx, "  Arvind  "))

    assert state.greet.name.value == "Arvind"  # stripped by the Slot validator
    assert state.greet.name.status is SlotStatus.FILLED
    assert state.caller.name == "Arvind"

    slot_events = [e for e in log.events if e.type is EventType.SLOT_SET]
    assert len(slot_events) == 1
    assert slot_events[0].payload["slot"] == "name"
    assert slot_events[0].payload["by_agent"] == "GreetAgent"


def test_confirm_identity_true_fills_the_slot(setup):
    agent, ctx, state, log = setup
    asyncio.run(agent.confirm_identity(ctx, True))

    assert state.greet.identity_confirmed.status is SlotStatus.FILLED
    assert any(
        e.payload.get("slot") == "identity_confirmed"
        for e in log.events
        if e.type is EventType.SLOT_SET
    )


def test_declining_to_give_a_name_resolves_the_stage_rather_than_blocking_it(setup):
    """A customer may refuse. UNAVAILABLE keeps that distinguishable from unasked."""
    agent, ctx, state, _ = setup
    asyncio.run(agent.confirm_identity(ctx, False))

    assert state.greet.identity_confirmed.status is SlotStatus.UNAVAILABLE
    assert state.greet.name.status is SlotStatus.UNAVAILABLE
    assert state.caller.name is None
    assert state.can_leave_stage() is True  # the call is not stuck


# --------------------------------------------------------------------------
# The code-enforced gate
# --------------------------------------------------------------------------


def test_handoff_is_refused_while_slots_are_outstanding(setup):
    agent, ctx, state, log = setup
    result = asyncio.run(agent.move_to_wrap(ctx))

    assert isinstance(result, str)  # a message back to the model, not a handoff
    assert "identity_confirmed" in result and "name" in result
    assert state.stage is Stage.GREET
    assert not [e for e in log.events if e.type is EventType.AGENT_HANDOFF]


def test_handoff_is_refused_when_only_one_slot_is_filled(setup):
    agent, ctx, state, _ = setup
    asyncio.run(agent.record_caller_name(ctx, "Arvind"))
    result = asyncio.run(agent.move_to_wrap(ctx))

    assert isinstance(result, str)
    assert "identity_confirmed" in result
    assert "name" not in result  # already captured, so not reported as missing
    assert state.stage is Stage.GREET


def test_handoff_succeeds_once_the_slots_are_resolved(setup):
    agent, ctx, state, log = setup
    asyncio.run(agent.record_caller_name(ctx, "Arvind"))
    asyncio.run(agent.confirm_identity(ctx, True))
    result = asyncio.run(agent.move_to_wrap(ctx))

    assert isinstance(result, WrapAgent)  # returning an Agent performs the handoff
    assert state.stage is Stage.WRAP

    handoffs = [e for e in log.events if e.type is EventType.AGENT_HANDOFF]
    assert len(handoffs) == 1
    assert handoffs[0].payload == {"from": "GreetAgent", "to": "WrapAgent"}


def test_handoff_carries_the_base_persona_into_wrap(setup):
    agent, ctx, _, _ = setup
    asyncio.run(agent.record_caller_name(ctx, "Arvind"))
    asyncio.run(agent.confirm_identity(ctx, True))
    wrap = asyncio.run(agent.move_to_wrap(ctx))

    assert BASE in wrap.instructions


def test_the_gate_reads_callstate_not_the_agent(setup):
    """The predicate lives on CallState so M4 extends one function, not each agent."""
    _, _, state, _ = setup
    assert state.stage is Stage.GREET
    assert state.can_leave_stage() is False
    assert state.blocking_slots() == ["identity_confirmed", "name"]

    state.greet.identity_confirmed = Slot.fill("yes")
    state.greet.name = Slot.fill("Arvind")
    assert state.can_leave_stage() is True
    assert state.blocking_slots() == []


def test_wrap_is_terminal():
    state = CallState(call_id="c_test", stage=Stage.WRAP)
    assert state.stage_slots() is None
    assert state.can_leave_stage() is True
    assert state.blocking_slots() == []


def test_stage_defaults_to_greet():
    assert CallState(call_id="c_test").stage is Stage.GREET


def test_stage_survives_serialization():
    state = CallState(call_id="c_test", stage=Stage.WRAP)
    assert CallState.model_validate_json(state.model_dump_json()).stage is Stage.WRAP
    assert state.model_dump(mode="json")["stage"] == "wrap"


# --------------------------------------------------------------------------
# T7 — WrapAgent tools (record_csat, end_call)
#
# Live verification is deferred (M3 plan D7): both are tools, and a
# tool-calling turn cannot complete under the current Groq ITPM ceiling.
# --------------------------------------------------------------------------


class _SessionStub:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _WrapCtx(_Ctx):
    """RunContext stand-in for the wrap tools: adds session + wait_for_playout."""

    def __init__(self, state: CallState) -> None:
        super().__init__(state)
        self.session = _SessionStub()
        self.playout_waited = False

    async def wait_for_playout(self) -> None:
        self.playout_waited = True


@pytest.fixture
def wrap_setup():
    log = EventLog()
    state = CallState(call_id="c_test", stage=Stage.WRAP)
    return WrapAgent(base_instructions=BASE, event_log=log), _WrapCtx(state), state, log


def test_wrap_exposes_its_three_tools():
    tools = sorted(
        t.info.name for t in WrapAgent(base_instructions=BASE, event_log=EventLog()).tools
    )
    assert tools == ["confirm_resolution", "end_call", "record_csat"]


@pytest.mark.parametrize("rating", [1, 2, 3, 4, 5])
def test_record_csat_accepts_the_valid_range(rating, wrap_setup):
    agent, ctx, state, log = wrap_setup
    asyncio.run(agent.record_csat(ctx, rating))

    assert state.wrap.csat.value == str(rating)
    events = [e for e in log.events if e.type is EventType.CSAT_RECORDED]
    assert len(events) == 1
    assert events[0].payload["csat"] == rating  # the int, not the slot string


@pytest.mark.parametrize("rating", [0, 6, -1, 99])
def test_record_csat_refuses_out_of_range_without_writing_anything(rating, wrap_setup):
    """A bad rating must not reach the log: §8 constrains csat to 1-5."""
    agent, ctx, state, log = wrap_setup
    result = asyncio.run(agent.record_csat(ctx, rating))

    assert isinstance(result, str) and "not a valid rating" in result
    assert state.wrap.csat.status is SlotStatus.PENDING
    assert not [e for e in log.events if e.type is EventType.CSAT_RECORDED]


def test_record_csat_refuses_a_non_integer(wrap_setup):
    agent, ctx, state, log = wrap_setup
    result = asyncio.run(agent.record_csat(ctx, "five"))  # type: ignore[arg-type]

    assert isinstance(result, str) and "not a valid rating" in result
    assert not [e for e in log.events if e.type is EventType.CSAT_RECORDED]


def test_csat_is_readable_by_jsonbuilder_from_the_log(wrap_setup):
    """The event carries the authoritative number; the slot only gates the stage."""
    from output import build_call_json

    agent, ctx, state, log = wrap_setup
    asyncio.run(agent.record_csat(ctx, 4))
    assert build_call_json(log, state)["csat"] == 4


def test_end_call_flushes_playout_before_closing(wrap_setup):
    """PRD §10 M3: end_call waits for the TTS flush before the room closes."""
    agent, ctx, _, log = wrap_setup
    asyncio.run(agent.end_call(ctx))

    assert ctx.playout_waited is True
    assert ctx.session.closed is True

    ended = [e for e in log.events if e.type is EventType.CALL_ENDED]
    assert len(ended) == 1
    assert ended[0].payload["reason"] == "end_call"


def test_end_call_logs_the_end_before_closing_the_session(wrap_setup):
    """Ordering matters: a session closed first could lose the event."""
    agent, ctx, _, log = wrap_setup

    order: list[str] = []
    original = ctx.session.aclose

    async def _tracking_close() -> None:
        order.append(f"close after {len(log)} events")
        await original()

    ctx.session.aclose = _tracking_close  # type: ignore[assignment]
    asyncio.run(agent.end_call(ctx))

    assert order == ["close after 1 events"]  # call_ended was already appended


def test_last_csat_wins_if_the_customer_revises_it(wrap_setup):
    from output import build_call_json

    agent, ctx, state, log = wrap_setup
    asyncio.run(agent.record_csat(ctx, 2))
    asyncio.run(agent.record_csat(ctx, 5))
    assert build_call_json(log, state)["csat"] == 5


def test_confirm_resolution_true_fills_the_slot_and_sets_resolved(wrap_setup):
    agent, ctx, state, log = wrap_setup
    asyncio.run(agent.confirm_resolution(ctx, True))

    assert state.wrap.summary_confirmed.value == "yes"
    assert state.wrap.summary_confirmed.status is SlotStatus.FILLED
    assert state.resolution.status is ResolutionStatus.RESOLVED

    events = [e for e in log.events if e.type is EventType.SLOT_SET]
    assert len(events) == 1
    assert events[0].payload["slot"] == "summary_confirmed"
    assert events[0].payload["resolved"] is True


def test_confirm_resolution_false_records_the_answer_without_inventing_a_status(wrap_setup):
    """§8's enum has no value for "completed but not resolved".

    escalated, callback and timeout would all assert something that did not
    happen, and abandoned already means a final user turn went unanswered.
    """
    agent, ctx, state, log = wrap_setup
    asyncio.run(agent.confirm_resolution(ctx, False))

    assert state.wrap.summary_confirmed.value == "no"
    assert state.wrap.summary_confirmed.status is SlotStatus.FILLED
    assert state.resolution.status is None  # not guessed

    events = [e for e in log.events if e.type is EventType.SLOT_SET]
    assert events[0].payload["resolved"] is False  # the answer is not lost


def test_a_negative_resolution_still_reaches_the_json(wrap_setup):
    from output import build_call_json

    agent, ctx, state, log = wrap_setup
    asyncio.run(agent.confirm_resolution(ctx, False))

    doc = build_call_json(log, state)
    assert doc["slots"]["summary_confirmed"] == "no"
    assert doc["resolution"]["status"] is None


def test_a_resolved_call_reaches_the_json_as_resolved(wrap_setup):
    from output import build_call_json

    agent, ctx, state, log = wrap_setup
    asyncio.run(agent.confirm_resolution(ctx, True))
    asyncio.run(agent.record_csat(ctx, 5))

    doc = build_call_json(log, state)
    assert doc["resolution"]["status"] == "resolved"
    assert doc["slots"]["summary_confirmed"] == "yes"
    assert doc["csat"] == 5


def test_confirm_resolution_false_refusal_guidance_promises_nothing(wrap_setup):
    """The message goes back to the model; it must not seed an invented next step."""
    agent, ctx, _, _ = wrap_setup
    reply = asyncio.run(agent.confirm_resolution(ctx, False))
    assert "not promise" in reply or "Do not promise" in reply


def test_the_full_wrap_sequence_produces_a_complete_document(wrap_setup):
    from output import build_call_json

    agent, ctx, state, log = wrap_setup
    asyncio.run(agent.confirm_resolution(ctx, True))
    asyncio.run(agent.record_csat(ctx, 4))
    asyncio.run(agent.end_call(ctx))

    doc = build_call_json(log, state)
    assert doc["resolution"]["status"] == "resolved"
    assert doc["csat"] == 4
    assert doc["ended_at"] is not None
    assert state.wrap.is_complete()  # both PRD §5 wrap slots captured
