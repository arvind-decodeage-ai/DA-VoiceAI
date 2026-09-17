"""Tests for CallState and the slot schemas (M3 T2).

Run from the repository root:

    .venv/bin/python -m pytest tests/test_state.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# agent/ is imported by path, matching how agent.py itself imports `config`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from state import (  # noqa: E402
    FORCE_WRAP_AFTER,
    MAX_INTENTS_PER_CALL,
    CallState,
    GreetSlots,
    Intent,
    ResolutionStatus,
    RouterSlots,
    Slot,
    SlotStatus,
)


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------


def test_callstate_defaults():
    state = CallState(call_id="c_test")

    assert state.call_id == "c_test"
    assert state.current_language is None
    assert state.languages_seen == []
    assert state.intents_handled == []
    assert state.active_intent is None

    assert state.caller.name is None
    assert state.caller.phone is None
    assert state.caller.customer_id is None

    assert state.resolution.status is None
    assert state.resolution.summary is None
    assert state.resolution.ticket_id is None

    # Every slot starts unasked, so nothing accidentally opens a gate.
    assert state.greet.identity_confirmed.status is SlotStatus.PENDING
    assert state.greet.name.status is SlotStatus.PENDING
    assert state.wrap.csat.status is SlotStatus.PENDING
    assert state.slots.order_status.order_id.status is SlotStatus.PENDING


def test_started_at_is_timezone_aware_utc():
    state = CallState(call_id="c_test")
    assert state.started_at.tzinfo is not None
    assert state.started_at.utcoffset() == timezone.utc.utcoffset(None)


def test_no_turn_counter_on_callstate():
    """The EventLog owns turn_idx; two counters would be two sources of truth."""
    assert "turn_idx" not in CallState.model_fields


# --------------------------------------------------------------------------
# Slot semantics
# --------------------------------------------------------------------------


def test_slot_fill_and_unavailable():
    filled = Slot.fill("ORD-123")
    assert filled.value == "ORD-123"
    assert filled.status is SlotStatus.FILLED
    assert filled.is_resolved

    unavailable = Slot.mark_unavailable()
    assert unavailable.value is None
    assert unavailable.status is SlotStatus.UNAVAILABLE
    assert unavailable.is_resolved

    assert not Slot().is_resolved


def test_slot_value_is_stripped():
    assert Slot.fill("  Arvind  ").value == "Arvind"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_filled_rejects_blank_values(blank):
    """A blank value must not satisfy the code-enforced Wrap gate.

    An empty STT transcript would otherwise produce a FILLED slot carrying no
    information, opening a gate that PRD §5 requires to be enforced in code.
    """
    with pytest.raises(ValueError):
        Slot.fill(blank)
    with pytest.raises(ValueError):
        Slot(value=blank, status=SlotStatus.FILLED)


def test_filled_rejects_null_value():
    with pytest.raises(ValueError):
        Slot(value=None, status=SlotStatus.FILLED)


def test_pending_slot_may_hold_a_value():
    """Captured-but-unconfirmed is legal; only FILLED asserts completeness."""
    slot = Slot(value="maybe")
    assert slot.status is SlotStatus.PENDING
    assert not slot.is_resolved


# --------------------------------------------------------------------------
# Stage gating
# --------------------------------------------------------------------------


def test_stage_slots_completeness_and_unresolved():
    greet = GreetSlots()
    assert not greet.is_complete()
    assert greet.unresolved_fields() == ["identity_confirmed", "name"]

    greet.identity_confirmed = Slot.fill("yes")
    assert greet.unresolved_fields() == ["name"]

    # UNAVAILABLE resolves a slot just as FILLED does.
    greet.name = Slot.mark_unavailable()
    assert greet.is_complete()
    assert greet.unresolved_fields() == []


def test_router_gates_on_intent_only():
    router = RouterSlots()
    router.intent = Slot.fill("order_status")
    assert router.is_complete()  # intent_reason is descriptive, not gating


def test_ready_for_wrap_requires_an_active_intent():
    state = CallState(call_id="c_test")
    assert state.active_slots() is None
    assert state.ready_for_wrap() is False


def test_ready_for_wrap_tracks_the_active_intent_slots():
    state = CallState(call_id="c_test")
    state.start_intent(Intent.ORDER_STATUS)
    assert state.ready_for_wrap() is False

    state.slots.order_status.order_id = Slot.fill("ORD-1")
    assert state.ready_for_wrap() is False

    state.slots.order_status.issue_type = Slot.fill("late")
    assert state.ready_for_wrap() is True


# --------------------------------------------------------------------------
# Language tracking (deviation #5)
# --------------------------------------------------------------------------


def test_note_language_tracks_current_and_dedupes_in_order():
    state = CallState(call_id="c_test")
    state.note_language("en-IN")
    state.note_language("hi-IN")
    state.note_language("en-IN")

    assert state.current_language == "en-IN"
    assert state.languages_seen == ["en-IN", "hi-IN"]  # first-seen order, no dupes


# --------------------------------------------------------------------------
# Intents and the multi-intent cap
# --------------------------------------------------------------------------


def test_start_intent_records_and_activates():
    state = CallState(call_id="c_test")
    state.start_intent(Intent.ORDER_STATUS)
    assert state.active_intent is Intent.ORDER_STATUS
    assert state.intents_handled == [Intent.ORDER_STATUS]


def test_reentering_an_intent_does_not_duplicate_it():
    state = CallState(call_id="c_test")
    state.start_intent(Intent.ORDER_STATUS)
    state.start_intent(Intent.COMPLAINT)
    state.start_intent(Intent.ORDER_STATUS)
    assert state.intents_handled == [Intent.ORDER_STATUS, Intent.COMPLAINT]
    assert state.active_intent is Intent.ORDER_STATUS


def test_intent_cap_raises_on_a_fourth_distinct_intent():
    state = CallState(call_id="c_test")
    for intent in (Intent.ORDER_STATUS, Intent.PRODUCT_INFO, Intent.COMPLAINT):
        state.start_intent(intent)

    assert len(state.intents_handled) == MAX_INTENTS_PER_CALL
    assert state.can_start_intent(Intent.SUBSCRIPTION) is False
    with pytest.raises(ValueError, match="cap"):
        state.start_intent(Intent.SUBSCRIPTION)

    # State is unchanged by the refusal.
    assert state.active_intent is Intent.COMPLAINT
    assert Intent.SUBSCRIPTION not in state.intents_handled


def test_can_reenter_a_handled_intent_at_the_cap():
    """The cap counts distinct intents, not visits."""
    state = CallState(call_id="c_test")
    for intent in (Intent.ORDER_STATUS, Intent.PRODUCT_INFO, Intent.COMPLAINT):
        state.start_intent(intent)

    assert state.can_start_intent(Intent.ORDER_STATUS) is True
    state.start_intent(Intent.ORDER_STATUS)
    assert state.active_intent is Intent.ORDER_STATUS


# --------------------------------------------------------------------------
# Deviation #8 — forced call-duration wrap
# --------------------------------------------------------------------------


def test_should_force_wrap_is_false_well_before_the_cap():
    state = CallState(call_id="c_test")
    assert state.should_force_wrap() is False


def test_should_force_wrap_is_true_once_the_cap_has_elapsed():
    state = CallState(
        call_id="c_test",
        started_at=datetime.now(timezone.utc) - FORCE_WRAP_AFTER - timedelta(seconds=1),
    )
    assert state.should_force_wrap() is True


def test_should_force_wrap_boundary_is_inclusive():
    """>=, not >: a call exactly at the cap is forced, not given a free turn."""
    state = CallState(
        call_id="c_test",
        started_at=datetime.now(timezone.utc) - FORCE_WRAP_AFTER,
    )
    assert state.should_force_wrap() is True


def test_should_force_wrap_ignores_stage_and_active_intent():
    """The cap is a hard ceiling, not a stage-specific gate like the others."""
    state = CallState(
        call_id="c_test",
        started_at=datetime.now(timezone.utc) - FORCE_WRAP_AFTER - timedelta(seconds=1),
    )
    state.start_intent(Intent.ORDER_STATUS)
    assert state.should_force_wrap() is True


# --------------------------------------------------------------------------
# Serialization / round-trip
# --------------------------------------------------------------------------


def _populated_state() -> CallState:
    state = CallState(call_id="c_ser", started_at=datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc))
    state.note_language("en-IN")
    state.note_language("hi-IN")
    state.caller.name = "Arvind"
    state.greet.identity_confirmed = Slot.fill("yes")
    state.greet.name = Slot.fill("Arvind")
    state.start_intent(Intent.ORDER_STATUS)
    state.slots.order_status.order_id = Slot.fill("ORD-1")
    state.slots.order_status.issue_type = Slot.mark_unavailable()
    state.wrap.summary_confirmed = Slot.fill("yes")
    state.resolution.status = ResolutionStatus.RESOLVED
    return state


def test_json_mode_dump_is_json_serializable():
    """The dump must survive plain json.dumps.

    This is the path that matters: JSONBuilder writes ./out/<call_id>.json and
    psycopg's JSON adapter calls json.dumps for the calls.result JSONB column.
    A set or a raw datetime in the dump would raise TypeError there.
    """
    dumped = _populated_state().model_dump(mode="json")
    encoded = json.dumps(dumped)  # must not raise
    assert json.loads(encoded) == dumped

    assert isinstance(dumped["languages_seen"], list)
    assert dumped["languages_seen"] == ["en-IN", "hi-IN"]
    assert dumped["active_intent"] == "order_status"
    assert dumped["resolution"]["status"] == "resolved"
    assert dumped["greet"]["identity_confirmed"]["status"] == "filled"
    assert isinstance(dumped["started_at"], str)


def test_round_trip_preserves_everything():
    original = _populated_state()
    restored = CallState.model_validate_json(original.model_dump_json())

    assert restored == original
    assert restored.started_at == original.started_at
    assert restored.active_intent is Intent.ORDER_STATUS
    assert restored.slots.order_status.issue_type.status is SlotStatus.UNAVAILABLE
    assert restored.ready_for_wrap() is True  # behaviour survives, not just data


def test_round_trip_through_plain_dicts():
    original = _populated_state()
    restored = CallState.model_validate(json.loads(json.dumps(original.model_dump(mode="json"))))
    assert restored == original


# --------------------------------------------------------------------------
# session.userdata
# --------------------------------------------------------------------------


@pytest.fixture
def event_loop_set():
    """AgentSession grabs the current event loop at construction.

    In the real agent it is built inside a running loop (entrypoint is async),
    so provide one here rather than letting it fall back to the deprecated
    get_event_loop() path.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_callstate_round_trips_through_session_userdata(event_loop_set):
    """CallState must survive the real AgentSession.userdata channel (D3).

    Constructed bare on purpose: agent.py builds AgentSession with live Sarvam
    STT/TTS and an LLM client, which a unit test must not instantiate. userdata
    is independent of those, and this asserts against the real class rather than
    an assumed API — session.userdata raises ValueError when unset, so passing
    it at construction is the only supported route.
    """
    from livekit.agents import AgentSession

    state = CallState(call_id="c_userdata")
    session = AgentSession(userdata=state)

    assert session.userdata is state

    # Mutation through the session is visible on the original object: the agents
    # and tools share one CallState, they do not each get a copy.
    session.userdata.note_language("hi-IN")
    session.userdata.greet.name = Slot.fill("Arvind")

    assert state.current_language == "hi-IN"
    assert state.greet.name.value == "Arvind"
    assert session.userdata.greet.name.is_resolved


def test_session_userdata_raises_when_unset(event_loop_set):
    """Documents the framework behaviour the wiring task has to respect."""
    from livekit.agents import AgentSession

    session = AgentSession()
    with pytest.raises(ValueError):
        _ = session.userdata
