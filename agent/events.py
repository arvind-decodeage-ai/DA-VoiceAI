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


# Turn-bearing events: the ones that represent somebody actually speaking, and
# therefore the ones that carry a turn_idx. Everything else (slot_set,
# agent_handoff, tool_call, ...) happens *within* or *between* turns and leaves
# turn_idx unset.
TURN_EVENT_TYPES = frozenset({EventType.USER_TURN, EventType.AGENT_TURN})


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
    #: Conversation-turn number, assigned by the log to turn-bearing events
    #: only; None for everything else. Coarser than ``seq``: one turn can emit
    #: several events (a user_turn plus the slot_set it produced). PRD §8's
    #: qa[] entries carry both a question and an answer under a single
    #: turn_idx, so this indexes an exchange, not an event.
    turn_idx: int | None = None
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
        self._turn_count = 0

    def append(self, type: EventType, payload: Mapping[str, Any] | None = None) -> Event:
        """Record an event and return it.

        ``seq`` and ``ts`` are assigned here rather than accepted from the
        caller: a caller-supplied sequence number could collide, skip or go
        backwards, and the whole point of the log is that its order is not open
        to interpretation. The first event has ``seq == 1``.

        Turn-bearing events (``user_turn``, ``agent_turn``) also get a
        ``turn_idx``, likewise assigned here. Turn numbering lives in the log
        and nowhere else: CallState, the JSON document and the database rows
        all read these numbers rather than each counting turns for themselves,
        which is the only way three projections of one log stay in agreement.

        The payload is deep-copied, so a caller that reuses or later mutates the
        dict it passed cannot change what was recorded.
        """
        # Validate the type before advancing the turn counter, so a rejected
        # append leaves no gap in the turn numbering.
        type = EventType(type)

        turn_idx = None
        if type in TURN_EVENT_TYPES:
            turn_idx = self._turn_count + 1

        event = Event(
            seq=len(self._events) + 1,
            ts=datetime.now(timezone.utc),
            type=type,
            turn_idx=turn_idx,
            payload=copy.deepcopy(dict(payload)) if payload is not None else {},
        )
        if turn_idx is not None:
            self._turn_count = turn_idx
        self._events.append(event)
        return event

    @property
    def turn_count(self) -> int:
        """Number of turn-bearing events appended so far."""
        return self._turn_count

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
