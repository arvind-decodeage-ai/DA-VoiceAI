"""Tests for JSONBuilder (M3 T5).

Built from synthetic EventLog fixtures — no live call needed, which is the
point of keeping the builder pure.

Run from the repository root:

    .venv/bin/python -m pytest tests/test_output.py -q
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from events import EventLog, EventType  # noqa: E402
from output import build_call_json  # noqa: E402
from state import CallState, Intent, ResolutionStatus, Slot  # noqa: E402

SCHEMA = json.loads((Path(__file__).parent / "call_output.schema.json").read_text())
STARTED_AT = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    Draft202012Validator.check_schema(SCHEMA)
    return Draft202012Validator(SCHEMA)


def _state(**kwargs) -> CallState:
    return CallState(call_id="c_test", started_at=STARTED_AT, **kwargs)


def _conversation() -> EventLog:
    """A normal completed call: greeting, two exchanges, a close."""
    log = EventLog()
    log.append(EventType.CALL_STARTED, {"room": "c_test"})
    log.append(EventType.AGENT_TURN, {"text": "How can I help?", "interrupted": False})
    log.append(EventType.USER_TURN, {"text": "Where is my order?", "language": "en-IN"})
    log.append(EventType.AGENT_TURN, {"text": "What is the order number?", "interrupted": False})
    log.append(EventType.USER_TURN, {"text": "ORD-1", "language": "en-IN"})
    log.append(EventType.AGENT_TURN, {"text": "Noted, thank you.", "interrupted": False})
    log.append(EventType.CALL_ENDED)
    return log


# --------------------------------------------------------------------------
# Schema conformance
# --------------------------------------------------------------------------


def test_empty_call_validates(validator):
    """Even a call where nothing happened must produce a valid document."""
    doc = build_call_json(EventLog(), _state())
    validator.validate(doc)


def test_full_conversation_validates(validator):
    validator.validate(build_call_json(_conversation(), _state()))


def test_richly_populated_call_validates(validator):
    log = EventLog()
    log.append(EventType.CALL_STARTED, {"room": "c_test"})
    log.append(EventType.AGENT_TURN, {"text": "How can I help?", "interrupted": False})
    log.append(EventType.USER_TURN, {"text": "Where is my order?", "language": "en-IN"})
    log.append(EventType.AGENT_TURN, {"text": "What is the order number?", "interrupted": False})
    log.append(EventType.USER_TURN, {"text": "ORD-1", "language": "en-IN"})
    # Exercises the tool_calls[].result wrapped-string shape (M4 Gate 2)
    # against the real schema, not just this file's own assertions — placed
    # in the window _build_qa actually attributes to an answer (after it,
    # before the next turn), not appended at the end where it would land
    # outside every qa entry's window and never reach the validator at all.
    log.append(
        EventType.TOOL_CALL,
        {
            "name": "lookup_order",
            "args": {"order_id": "ORD-1"},
            "result": {"text": "Order ORD-1: financial status paid."},
        },
    )
    log.append(EventType.AGENT_TURN, {"text": "Noted, thank you.", "interrupted": False})
    log.append(EventType.CALL_ENDED)
    log.append(EventType.CSAT_RECORDED, {"csat": 4})
    state = _state()
    state.caller.name = "Arvind"
    state.caller.phone = "+91..."
    state.start_intent(Intent.ORDER_STATUS)
    state.greet.name = Slot.fill("Arvind")
    state.greet.identity_confirmed = Slot.mark_unavailable()
    state.resolution.status = ResolutionStatus.RESOLVED
    state.resolution.summary = "Noted the order number."
    doc = build_call_json(log, state)
    # Confirms the tool call actually landed in a qa entry (not merely that
    # the document validates, which would pass vacuously if it landed
    # nowhere).
    assert doc["qa"][1]["tool_calls"] == [
        {
            "name": "lookup_order",
            "args": {"order_id": "ORD-1"},
            "result": {"text": "Order ORD-1: financial status paid."},
        }
    ]
    validator.validate(doc)


def test_later_milestone_fields_are_present_but_empty(validator):
    """M6/M7 fields are emitted, never omitted, so the shape is stable."""
    doc = build_call_json(_conversation(), _state())
    assert doc["compliance_flags"] == []
    assert doc["guardrail_events"] == []
    assert doc["latency"] == {"avg_turn_ms": None, "p95_turn_ms": None}
    assert doc["recording_url"] is None


def test_document_is_json_serializable():
    json.dumps(build_call_json(_conversation(), _state()))  # must not raise


def test_builder_is_pure():
    log, state = _conversation(), _state()
    first = build_call_json(log, state)
    second = build_call_json(log, state)
    assert first == second
    assert len(log) == 7  # building did not append anything


# --------------------------------------------------------------------------
# qa[] pairing
# --------------------------------------------------------------------------


def test_qa_pairs_each_question_with_the_following_answer():
    doc = build_call_json(_conversation(), _state())
    qa = doc["qa"]

    assert len(qa) == 2
    assert qa[0]["question"] == "How can I help?"
    assert qa[0]["answer_raw"] == "Where is my order?"
    assert qa[1]["question"] == "What is the order number?"
    assert qa[1]["answer_raw"] == "ORD-1"


def test_qa_turn_idx_is_the_answers_number_and_comes_from_the_log():
    log = _conversation()
    doc = build_call_json(log, _state())

    user_turn_ids = [e.turn_idx for e in log.events if e.type is EventType.USER_TURN]
    assert [entry["turn_idx"] for entry in doc["qa"]] == user_turn_ids


def test_user_speaking_first_yields_an_empty_question():
    log = EventLog()
    log.append(EventType.USER_TURN, {"text": "hello?", "language": "en-IN"})
    qa = build_call_json(log, _state())["qa"]

    assert len(qa) == 1
    assert qa[0]["question"] == ""
    assert qa[0]["answer_raw"] == "hello?"


def test_trailing_unanswered_question_produces_no_qa_entry():
    log = EventLog()
    log.append(EventType.AGENT_TURN, {"text": "Anything else?", "interrupted": False})
    assert build_call_json(log, _state())["qa"] == []


def test_answer_normalized_is_the_identity_until_m6():
    log = EventLog()
    log.append(EventType.USER_TURN, {"text": "  ORD-1  ", "language": "en-IN"})
    entry = build_call_json(log, _state())["qa"][0]
    assert entry["answer_raw"] == "  ORD-1  "
    assert entry["answer_normalized"] == "ORD-1"


def test_slot_set_between_answer_and_next_turn_is_attributed_to_that_answer():
    log = EventLog()
    log.append(EventType.AGENT_TURN, {"text": "Your name?", "interrupted": False})
    log.append(EventType.USER_TURN, {"text": "Arvind", "language": "en-IN"})
    log.append(EventType.SLOT_SET, {"slot": "name", "value": "Arvind"})
    log.append(EventType.AGENT_TURN, {"text": "Thanks.", "interrupted": False})
    log.append(EventType.USER_TURN, {"text": "bye", "language": "en-IN"})

    qa = build_call_json(log, _state())["qa"]
    assert qa[0]["slot"] == "name"
    assert qa[1]["slot"] is None  # no slot_set followed the second answer


def test_tool_calls_default_to_empty_when_none_occurred():
    assert all(entry["tool_calls"] == [] for entry in build_call_json(_conversation(), _state())["qa"])


def test_tool_call_between_answer_and_next_turn_is_attributed_to_that_answer():
    log = EventLog()
    log.append(EventType.AGENT_TURN, {"text": "What is the order number?", "interrupted": False})
    log.append(EventType.USER_TURN, {"text": "52428", "language": "en-IN"})
    log.append(
        EventType.TOOL_CALL,
        {
            "name": "lookup_order",
            "args": {"order_id": "52428"},
            "result": {"text": "Order 52428: financial status paid."},
        },
    )
    log.append(EventType.AGENT_TURN, {"text": "Your order is paid.", "interrupted": False})
    log.append(EventType.USER_TURN, {"text": "thanks", "language": "en-IN"})

    qa = build_call_json(log, _state())["qa"]
    assert qa[0]["tool_calls"] == [
        {
            "name": "lookup_order",
            "args": {"order_id": "52428"},
            "result": {"text": "Order 52428: financial status paid."},
        }
    ]
    assert qa[1]["tool_calls"] == []  # no tool call followed the second answer


def test_multiple_tool_calls_for_one_answer_all_attach_in_order():
    log = EventLog()
    log.append(EventType.AGENT_TURN, {"text": "Let me check.", "interrupted": False})
    log.append(EventType.USER_TURN, {"text": "ORD-1", "language": "en-IN"})
    log.append(EventType.TOOL_CALL, {"name": "lookup_order", "args": {}, "result": {"text": "first"}})
    log.append(EventType.TOOL_CALL, {"name": "lookup_order", "args": {}, "result": {"text": "second"}})
    log.append(EventType.AGENT_TURN, {"text": "Done.", "interrupted": False})

    qa = build_call_json(log, _state())["qa"]
    assert [c["result"]["text"] for c in qa[0]["tool_calls"]] == ["first", "second"]


def test_tool_call_error_result_is_a_found_false_object():
    log = EventLog()
    log.append(EventType.AGENT_TURN, {"text": "What is the order number?", "interrupted": False})
    log.append(EventType.USER_TURN, {"text": "99999", "language": "en-IN"})
    log.append(
        EventType.TOOL_CALL,
        {
            "name": "lookup_order",
            "args": {"order_id": "99999"},
            "result": {"found": False, "reason": "connection refused"},
        },
    )
    log.append(EventType.AGENT_TURN, {"text": "I couldn't look that up.", "interrupted": False})

    qa = build_call_json(log, _state())["qa"]
    assert qa[0]["tool_calls"][0]["result"] == {"found": False, "reason": "connection refused"}


# --------------------------------------------------------------------------
# Open item 2 — the unanswered final turn
# --------------------------------------------------------------------------


def test_unanswered_final_user_turn_marks_the_call_abandoned():
    """LLM failure, crash, disconnect and hangup all end a call this way."""
    log = EventLog()
    log.append(EventType.AGENT_TURN, {"text": "What is the order number?", "interrupted": False})
    log.append(EventType.USER_TURN, {"text": "ORD-1", "language": "en-IN"})
    log.append(EventType.CALL_ENDED)

    doc = build_call_json(log, _state())
    assert doc["resolution"]["status"] == "abandoned"
    # The exchange still appears; only the missing follow-up is inferred.
    assert len(doc["qa"]) == 1


def test_an_explicit_resolution_is_never_overwritten():
    log = EventLog()
    log.append(EventType.USER_TURN, {"text": "thanks, bye", "language": "en-IN"})
    log.append(EventType.CALL_ENDED)
    state = _state()
    state.resolution.status = ResolutionStatus.RESOLVED

    assert build_call_json(log, state)["resolution"]["status"] == "resolved"


def test_a_call_ending_on_an_agent_turn_is_not_abandoned():
    assert build_call_json(_conversation(), _state())["resolution"]["status"] is None


def test_no_synthetic_qa_entry_is_invented_for_the_missing_reply():
    log = EventLog()
    log.append(EventType.USER_TURN, {"text": "hello", "language": "en-IN"})
    log.append(EventType.CALL_ENDED)
    qa = build_call_json(log, _state())["qa"]
    assert len(qa) == 1
    assert qa[0]["answer_raw"] == "hello"


# --------------------------------------------------------------------------
# Transcript ordering
# --------------------------------------------------------------------------


def test_transcript_follows_the_conversation():
    doc = build_call_json(_conversation(), _state())
    assert [(line["role"], line["text"]) for line in doc["transcript"]] == [
        ("agent", "How can I help?"),
        ("user", "Where is my order?"),
        ("agent", "What is the order number?"),
        ("user", "ORD-1"),
        ("agent", "Noted, thank you."),
    ]


def test_transcript_orders_a_late_committed_reply_by_when_it_was_spoken():
    """An interrupted reply commits after the turn that cut it off.

    Arrival order would put the agent's words after the interruption; spoken_at
    puts them where the customer actually heard them.
    """
    spoken_first = (STARTED_AT + timedelta(seconds=1)).isoformat()
    log = EventLog()
    log.append(EventType.USER_TURN, {"text": "wait", "language": "en-IN"})
    log.append(EventType.AGENT_TURN, {"text": "Let me ch", "interrupted": True, "spoken_at": spoken_first})

    doc = build_call_json(log, _state())
    assert [line["role"] for line in doc["transcript"]] == ["agent", "user"]


def test_transcript_uses_arrival_time_when_spoken_at_is_missing():
    log = EventLog()
    log.append(EventType.AGENT_TURN, {"text": "hi", "interrupted": False})
    log.append(EventType.USER_TURN, {"text": "hello", "language": "en-IN"})
    assert [line["role"] for line in build_call_json(log, _state())["transcript"]] == ["agent", "user"]


# --------------------------------------------------------------------------
# Scalar fields
# --------------------------------------------------------------------------


def test_language_is_the_predominant_one_not_the_last():
    log = EventLog()
    log.append(EventType.USER_TURN, {"text": "a", "language": "hi-IN"})
    log.append(EventType.USER_TURN, {"text": "b", "language": "hi-IN"})
    log.append(EventType.USER_TURN, {"text": "c", "language": "en-IN"})

    assert build_call_json(log, _state())["language"] == "hi-IN"


def test_language_ties_break_on_first_appearance():
    log = EventLog()
    log.append(EventType.USER_TURN, {"text": "a", "language": "hi-IN"})
    log.append(EventType.USER_TURN, {"text": "b", "language": "en-IN"})
    assert build_call_json(log, _state())["language"] == "hi-IN"


def test_language_falls_back_to_callstate_when_the_log_has_none():
    """console --text has no STT, so no turn carries a language."""
    log = EventLog()
    log.append(EventType.USER_TURN, {"text": "a", "language": None})
    state = _state()
    state.note_language("en-IN")
    assert build_call_json(log, state)["language"] == "en-IN"


def test_language_is_null_when_nothing_is_known():
    assert build_call_json(EventLog(), _state())["language"] is None


def test_duration_comes_from_call_ended():
    log = EventLog()
    log.append(EventType.CALL_ENDED)
    doc = build_call_json(log, _state())
    assert doc["ended_at"] is not None
    assert doc["duration_s"] >= 0


def test_duration_is_null_for_a_call_with_no_end_event():
    doc = build_call_json(EventLog(), _state())
    assert doc["ended_at"] is None
    assert doc["duration_s"] is None


def test_csat_comes_from_the_log_and_the_last_value_wins():
    log = EventLog()
    log.append(EventType.CSAT_RECORDED, {"csat": 3})
    log.append(EventType.CSAT_RECORDED, {"csat": 5})
    assert build_call_json(log, _state())["csat"] == 5


def test_csat_is_null_when_never_recorded():
    assert build_call_json(_conversation(), _state())["csat"] is None


def test_slots_flattens_resolved_slots_and_keeps_unavailable_as_null():
    state = _state()
    state.greet.name = Slot.fill("Arvind")
    state.greet.identity_confirmed = Slot.mark_unavailable()

    slots = build_call_json(EventLog(), state)["slots"]
    assert slots["name"] == "Arvind"
    assert slots["identity_confirmed"] is None
    assert "order_id" not in slots  # never asked, so absent rather than null


def test_intents_come_from_callstate():
    state = _state()
    state.start_intent(Intent.ORDER_STATUS)
    state.start_intent(Intent.COMPLAINT)
    assert build_call_json(EventLog(), state)["intents"] == ["order_status", "complaint"]


def test_agent_name_is_empty_before_any_handoff_and_tracks_them_after():
    log = EventLog()
    log.append(EventType.AGENT_TURN, {"text": "hi", "interrupted": False})
    log.append(EventType.USER_TURN, {"text": "hello", "language": "en-IN"})
    log.append(EventType.AGENT_HANDOFF, {"to": "WrapAgent"})
    log.append(EventType.AGENT_TURN, {"text": "anything else?", "interrupted": False})
    log.append(EventType.USER_TURN, {"text": "no", "language": "en-IN"})

    doc = build_call_json(log, _state())
    assert doc["qa"][0]["agent"] == ""  # single unnamed agent pre-T6
    assert doc["qa"][1]["agent"] == "WrapAgent"
    assert [line["agent"] for line in doc["transcript"]] == ["", "", "WrapAgent", ""]
