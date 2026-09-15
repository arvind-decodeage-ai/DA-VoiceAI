"""Tests for the EventLog (M3 T3).

Run from the repository root:

    .venv/bin/python -m pytest tests/test_events.py -q
"""

from __future__ import annotations

import sys
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from events import (  # noqa: E402
    M3_EVENT_TYPES,
    RESERVED_EVENT_TYPES,
    TURN_EVENT_TYPES,
    Event,
    EventLog,
    EventType,
)


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------


def test_empty_log():
    log = EventLog()
    assert len(log) == 0
    assert log.events == ()


def test_seq_is_assigned_by_the_log_and_starts_at_one():
    log = EventLog()
    first = log.append(EventType.CALL_STARTED)
    second = log.append(EventType.USER_TURN)

    assert first.seq == 1
    assert second.seq == 2


def test_append_order_is_preserved_and_seq_strictly_increases():
    log = EventLog()
    appended = [
        log.append(EventType.CALL_STARTED),
        log.append(EventType.USER_TURN, {"text": "hi"}),
        log.append(EventType.AGENT_TURN, {"text": "hello"}),
        log.append(EventType.SLOT_SET, {"slot": "name"}),
        log.append(EventType.CALL_ENDED),
    ]

    assert list(log.events) == appended
    seqs = [e.seq for e in log.events]
    assert seqs == sorted(seqs)
    assert all(b - a == 1 for a, b in zip(seqs, seqs[1:]))
    assert len(set(seqs)) == len(seqs)  # no collisions


def test_timestamps_are_utc_and_non_decreasing():
    log = EventLog()
    for _ in range(5):
        log.append(EventType.USER_TURN)

    timestamps = [e.ts for e in log.events]
    assert all(t.tzinfo is not None for t in timestamps)
    assert all(t.utcoffset() == timezone.utc.utcoffset(None) for t in timestamps)
    assert timestamps == sorted(timestamps)


def test_iteration_matches_the_events_view():
    log = EventLog()
    log.append(EventType.CALL_STARTED)
    log.append(EventType.CALL_ENDED)
    assert list(iter(log)) == list(log.events)
    assert len(log) == 2


# --------------------------------------------------------------------------
# Immutability
# --------------------------------------------------------------------------


def test_event_fields_cannot_be_reassigned():
    """Frozen by construction — the strongest guarantee available per field."""
    log = EventLog()
    event = log.append(EventType.USER_TURN, {"text": "hi"})

    for field, value in [("seq", 99), ("type", EventType.AGENT_TURN), ("payload", {})]:
        with pytest.raises(ValidationError):
            setattr(event, field, value)

    assert log.events[0].seq == 1
    assert log.events[0].type is EventType.USER_TURN


def test_events_view_is_a_tuple_and_cannot_be_appended_to():
    log = EventLog()
    log.append(EventType.CALL_STARTED)

    assert isinstance(log.events, tuple)
    with pytest.raises(AttributeError):
        log.events.append(Event(seq=99, ts=log.events[0].ts, type=EventType.CALL_ENDED))
    assert len(log) == 1


def test_rebinding_the_events_view_does_not_touch_the_log():
    log = EventLog()
    log.append(EventType.CALL_STARTED)
    view = log.events
    view = view + (Event(seq=2, ts=log.events[0].ts, type=EventType.CALL_ENDED),)

    assert len(view) == 2
    assert len(log) == 1  # the log is unaffected


def test_log_exposes_no_mutating_methods():
    """Guard against an update/delete path being added later by accident."""
    public = {name for name in dir(EventLog) if not name.startswith("_")}
    assert public == {"append", "events", "turn_count"}


def test_payload_is_copied_so_later_caller_mutation_cannot_rewrite_history():
    log = EventLog()
    payload = {"text": "original"}
    log.append(EventType.USER_TURN, payload)

    payload["text"] = "tampered"
    payload["added"] = "later"

    assert log.events[0].payload == {"text": "original"}


def test_nested_payload_is_deep_copied():
    log = EventLog()
    payload = {"slot": {"name": "original"}, "tags": ["a"]}
    log.append(EventType.SLOT_SET, payload)

    payload["slot"]["name"] = "tampered"
    payload["tags"].append("b")

    assert log.events[0].payload == {"slot": {"name": "original"}, "tags": ["a"]}


def test_reusing_one_payload_dict_across_appends_keeps_events_distinct():
    """A producer reusing a scratch dict is the realistic accident."""
    log = EventLog()
    scratch = {"turn": 1}
    log.append(EventType.USER_TURN, scratch)
    scratch["turn"] = 2
    log.append(EventType.USER_TURN, scratch)

    assert [e.payload["turn"] for e in log.events] == [1, 2]


# --------------------------------------------------------------------------
# Event types
# --------------------------------------------------------------------------


@pytest.mark.parametrize("event_type", list(EventType))
def test_every_declared_type_is_appendable(event_type):
    """All nine: the seven M3 types plus the two reserved ones."""
    log = EventLog()
    event = log.append(event_type, {"k": "v"})
    assert event.type is event_type
    assert log.events[0].type is event_type


def test_type_sets_partition_the_enum():
    assert M3_EVENT_TYPES | RESERVED_EVENT_TYPES == set(EventType)
    assert M3_EVENT_TYPES & RESERVED_EVENT_TYPES == set()
    assert len(M3_EVENT_TYPES) == 7
    assert RESERVED_EVENT_TYPES == {EventType.TOOL_CALL, EventType.GUARDRAIL_HIT}


def test_reserved_types_are_valid_but_have_no_producer_in_m3():
    """Declared so M4/M6 add writers without reshaping the log."""
    log = EventLog()
    for reserved in RESERVED_EVENT_TYPES:
        log.append(reserved)
    assert {e.type for e in log.events} == RESERVED_EVENT_TYPES


def test_unknown_type_is_rejected():
    log = EventLog()
    with pytest.raises(ValueError):  # pydantic's ValidationError is a ValueError
        log.append("not_a_real_event_type")  # type: ignore[arg-type]
    assert len(log) == 0


def test_payload_defaults_to_empty_dict():
    log = EventLog()
    assert log.append(EventType.CALL_STARTED).payload == {}


# --------------------------------------------------------------------------
# turn_idx — conversation-turn numbering, owned by the log
# --------------------------------------------------------------------------


def test_only_turn_bearing_events_carry_a_turn_idx():
    log = EventLog()
    started = log.append(EventType.CALL_STARTED)
    user = log.append(EventType.USER_TURN, {"text": "hi"})
    slot = log.append(EventType.SLOT_SET, {"slot": "name"})
    agent = log.append(EventType.AGENT_TURN, {"text": "hello"})

    assert started.turn_idx is None
    assert slot.turn_idx is None
    assert user.turn_idx == 1
    assert agent.turn_idx == 2


def test_turn_idx_is_coarser_than_seq():
    """One turn can emit several events; seq counts events, turn_idx exchanges."""
    log = EventLog()
    log.append(EventType.USER_TURN, {"text": "my order is late"})
    log.append(EventType.SLOT_SET, {"slot": "issue_type"})
    log.append(EventType.SLOT_SET, {"slot": "order_id"})
    log.append(EventType.AGENT_TURN, {"text": "got it"})

    assert [e.seq for e in log.events] == [1, 2, 3, 4]
    assert [e.turn_idx for e in log.events] == [1, None, None, 2]
    assert log.turn_count == 2


def test_turn_idx_increments_contiguously_across_interleaved_events():
    log = EventLog()
    for i in range(3):
        log.append(EventType.USER_TURN, {"i": i})
        log.append(EventType.SLOT_SET, {"i": i})
        log.append(EventType.AGENT_TURN, {"i": i})

    turns = [e.turn_idx for e in log.events if e.turn_idx is not None]
    assert turns == [1, 2, 3, 4, 5, 6]


def test_rejected_append_does_not_consume_a_turn_number():
    log = EventLog()
    log.append(EventType.USER_TURN)
    with pytest.raises(ValueError):
        log.append("nonsense")  # type: ignore[arg-type]
    assert log.append(EventType.AGENT_TURN).turn_idx == 2  # no gap
    assert log.turn_count == 2


def test_turn_count_starts_at_zero_and_tracks_turn_events_only():
    log = EventLog()
    assert log.turn_count == 0
    log.append(EventType.CALL_STARTED)
    assert log.turn_count == 0
    log.append(EventType.USER_TURN)
    assert log.turn_count == 1


def test_turn_event_types_are_the_two_speaking_events():
    assert TURN_EVENT_TYPES == {EventType.USER_TURN, EventType.AGENT_TURN}
    assert TURN_EVENT_TYPES <= M3_EVENT_TYPES
