"""Completed-call persistence (M3 T8).

The database is a projection of the EventLog, exactly like the §8 document — so
every row written here is derived from the log, never from state gathered
separately. That is what keeps the JSON and the tables from disagreeing.

Write shape, per the M3 plan (Persistence):

  1. Call start   — one ``calls`` row with status='in_progress'. One write,
                    before any conversation, so an abandoned or crashed call
                    leaves a visible record rather than nothing.
  2. During       — no database access at all. Zero turn-path risk, by
                    construction rather than by mechanism.
  3. ``end_call`` — one transaction writes events, turns and slots from the
                    log, and finalises the ``calls`` row with the document.

Accepted trade-off: a crash mid-call loses that call's turns, leaving only the
in_progress row. Bounded and visible rather than silent or partial — and there
is a test that kills a call to prove it.
"""

from __future__ import annotations

from typing import Any, Optional

import psycopg
from psycopg.types.json import Jsonb

from events import EventLog, EventType
from state import CallState

STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"

#: Log events that become a `turns` row, and the §8 role they map to.
_TURN_ROLES = {EventType.USER_TURN: "user", EventType.AGENT_TURN: "agent"}


def start_call(conn: psycopg.Connection, state: CallState) -> None:
    """Record that a call began. Idempotent on call_id so a retried job cannot
    duplicate the row or clobber the original start time."""
    conn.execute(
        """
        INSERT INTO calls (id, started_at, language, status)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (state.call_id, state.started_at, state.current_language, STATUS_IN_PROGRESS),
    )


def finalize_call(
    conn: psycopg.Connection,
    state: CallState,
    log: EventLog,
    document: dict[str, Any],
) -> None:
    """Write the whole log and the built document, in one transaction.

    Everything or nothing: a partial write would leave rows that disagree with
    ``calls.result``, which is precisely the drift this design exists to avoid.
    """
    with conn.transaction():
        _insert_events(conn, state.call_id, log)
        _insert_turns(conn, state.call_id, log)
        _insert_slots(conn, state.call_id, log)
        _finalize_row(conn, state, document)


def _insert_events(conn: psycopg.Connection, call_id: str, log: EventLog) -> None:
    for event in log:
        detail = dict(event.payload)
        # seq and turn_idx are log-assigned, so they travel as part of the row's
        # detail rather than being recomputed by anything reading the table.
        detail["seq"] = event.seq
        if event.turn_idx is not None:
            detail["turn_idx"] = event.turn_idx
        conn.execute(
            "INSERT INTO events (call_id, event_type, detail, ts) VALUES (%s, %s, %s, %s)",
            (call_id, event.type.value, Jsonb(detail), event.ts),
        )


def _insert_turns(conn: psycopg.Connection, call_id: str, log: EventLog) -> None:
    for event in log:
        role = _TURN_ROLES.get(event.type)
        if role is None:
            continue
        conn.execute(
            """
            INSERT INTO turns (call_id, turn_idx, role, agent_name, text, ts)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                call_id,
                event.turn_idx,
                role,
                event.payload.get("by_agent"),
                event.payload.get("text"),
                event.ts,
            ),
        )


def _insert_slots(conn: psycopg.Connection, call_id: str, log: EventLog) -> None:
    """One row per slot_set event — the history, not just the final value.

    A slot that was corrected mid-call leaves both writes, which the flat
    ``slots`` map in the §8 document cannot show.
    """
    for event in log:
        if event.type is not EventType.SLOT_SET:
            continue
        key = event.payload.get("slot")
        if not key:
            continue
        conn.execute(
            "INSERT INTO slots (call_id, key, value, ts) VALUES (%s, %s, %s, %s)",
            (call_id, key, event.payload.get("value"), event.ts),
        )


def _finalize_row(
    conn: psycopg.Connection, state: CallState, document: dict[str, Any]
) -> None:
    resolution = document.get("resolution") or {}
    status: Optional[str] = resolution.get("status") or STATUS_COMPLETED
    conn.execute(
        """
        UPDATE calls
           SET ended_at = %s, duration_s = %s, language = %s, status = %s, result = %s
         WHERE id = %s
        """,
        (
            document.get("ended_at"),
            document.get("duration_s"),
            document.get("language"),
            status,
            Jsonb(document),
            state.call_id,
        ),
    )
