"""EventLog — the single source of truth for a call (M3 T3).

Everything downstream is a projection of this log: ``CallState`` is live
convenience for the agents, the §8 JSON document is built from the log by
``output.py::JSONBuilder``, and the database rows are the same log written to
Postgres. One producer, three consumers, so they cannot disagree — which only
holds if the log itself is never rewritten after the fact.

Hence the shape: ``seq`` is assigned by the log, not supplied by callers; there
is no update, delete, insert-at or reorder method; ``Event`` is frozen; and
payloads are copied on the way in.

T3 covers the log itself. Wiring it to real session events is T4.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field


class EventType(str, Enum):
    """Every kind of thing that can happen in a call.

    The reserved members have no producer in M3. They are declared now so that
    M4 (tools) and M6 (guardrails) add writers without reshaping a log that the
    JSON document and the database projection already depend on.
    """

    CALL_STARTED = "call_started"
    USER_TURN = "user_turn"
    AGENT_TURN = "agent_turn"
    SLOT_SET = "slot_set"
    AGENT_HANDOFF = "agent_handoff"
    CSAT_RECORDED = "csat_recorded"
    CALL_ENDED = "call_ended"

    # Reserved: valid to append, no producer until M4/M6.
    TOOL_CALL = "tool_call"
    GUARDRAIL_HIT = "guardrail_hit"


M3_EVENT_TYPES = frozenset(
    {
        EventType.CALL_STARTED,
        EventType.USER_TURN,
        EventType.AGENT_TURN,
        EventType.SLOT_SET,
        EventType.AGENT_HANDOFF,
        EventType.CSAT_RECORDED,
        EventType.CALL_ENDED,
    }
)

RESERVED_EVENT_TYPES = frozenset({EventType.TOOL_CALL, EventType.GUARDRAIL_HIT})


class Event(BaseModel):
    """One thing that happened, at a point in the call.

    Frozen: fields cannot be reassigned after construction. ``payload`` is a
    plain dict, so its *contents* are still reachable by anyone holding the
    Event; the log defends against the realistic accident — a producer reusing
    or mutating the dict it passed in — by deep-copying on append.
    """

    model_config = ConfigDict(frozen=True)

    seq: int
    ts: datetime
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)


class EventLog:
    """Append-only, ordered record of one call.

    The public surface is deliberately two things: ``append`` and a read-only
    view. No setter, no delete, no reorder — if a mutation method existed, the
    log would stop being something the JSON and the database can both be
    derived from.
    """

    def __init__(self) -> None:
        self._events: list[Event] = []

    def append(self, type: EventType, payload: Mapping[str, Any] | None = None) -> Event:
        """Record an event and return it.

        ``seq`` and ``ts`` are assigned here rather than accepted from the
        caller: a caller-supplied sequence number could collide, skip or go
        backwards, and the whole point of the log is that its order is not open
        to interpretation. The first event has ``seq == 1``.

        The payload is deep-copied, so a caller that reuses or later mutates the
        dict it passed cannot change what was recorded.
        """
        event = Event(
            seq=len(self._events) + 1,
            ts=datetime.now(timezone.utc),
            type=type,
            payload=copy.deepcopy(dict(payload)) if payload is not None else {},
        )
        self._events.append(event)
        return event

    @property
    def events(self) -> tuple[Event, ...]:
        """Read-only view. A tuple, so callers cannot append or reorder it."""
        return tuple(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __repr__(self) -> str:
        return f"EventLog({len(self._events)} events)"
