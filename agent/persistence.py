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

import json
import logging
from pathlib import Path
from typing import Any, Optional

import psycopg
from psycopg.types.json import Jsonb

from events import EventLog, EventType
from state import CallState

logger = logging.getLogger("da-voice.persistence")

#: Where the §8 document is written, per PRD §9 and the M3 acceptance criteria.
DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "out"

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
    *,
    out_dir: Optional[Path] = DEFAULT_OUT_DIR,
) -> Optional[Path]:
    """Write the whole log and the built document, then the ./out/ file.

    The database write is one transaction — everything or nothing, since a
    partial write would leave rows disagreeing with ``calls.result``, which is
    the drift this design exists to avoid.

    The file is written from the *same* ``document`` object, so the row and the
    file can never hold different content. They are not atomic with each other:
    the transaction commits first, and a filesystem failure afterwards leaves
    the row without the file. That ordering is deliberate — the database is the
    source of truth and ``GET /calls/{id}/json`` reads from it, so a missing
    file is a missing convenience copy, never a wrong one. Writing the file
    first would risk a file describing a call the database never recorded.

    Returns the path written, or None when no file was written.
    """
    with conn.transaction():
        _insert_events(conn, state.call_id, log)
        _insert_turns(conn, state.call_id, log)
        _insert_slots(conn, state.call_id, log)
        _finalize_row(conn, state, document)

    if out_dir is None:
        return None
    return write_document(out_dir, state.call_id, document)


def write_document(
    out_dir: Path, call_id: str, document: dict[str, Any]
) -> Optional[Path]:
    """Write the §8 document to ``<out_dir>/<call_id>.json``.

    Never raises: the call is already durably recorded in the database by the
    time this runs, so a filesystem problem must not turn a completed call into
    a failed one. It is logged loudly instead.
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{call_id}.json"
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("wrote %s", path)
        return path
    except Exception:
        logger.exception("failed to write the call document for %s", call_id)
        return None


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
