"""Tests for completed-call persistence (M3 T8).

These hit a real Postgres — the point is the actual write path and a real
transaction, not a mocked one. They skip if the database is unreachable.

Driven from synthetic EventLog fixtures, the same approach T5 used: a live
tool-calling conversation cannot complete under the current ITPM ceiling
(M3 plan D7), so the log is built directly.

Run from the repository root:

    .venv/bin/python -m pytest tests/test_persistence.py -q
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))
load_dotenv(REPO / ".env")

import psycopg  # noqa: E402

from events import EventLog, EventType  # noqa: E402
from output import build_call_json  # noqa: E402
from persistence import (  # noqa: E402
    STATUS_IN_PROGRESS,
    finalize_call,
    start_call,
)
from state import CallState, ResolutionStatus, Slot  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _db_reachable() -> bool:
    if not DATABASE_URL:
        return False
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_reachable(), reason="Postgres not reachable; run `docker compose up -d`"
)


@pytest.fixture
def conn():
    with psycopg.connect(DATABASE_URL) as c:
        yield c


@pytest.fixture
def call_id():
    """A unique id per test, with its rows removed afterwards.

    calls is the parent with ON DELETE CASCADE, so one delete cleans everything.
    """
    cid = f"c_test_{uuid.uuid4().hex[:10]}"
    yield cid
    with psycopg.connect(DATABASE_URL) as c:
        c.execute("DELETE FROM calls WHERE id = %s", (cid,))
        c.commit()


def _rows(conn, table: str, call_id: str) -> list[tuple]:
    return conn.execute(
        f"SELECT * FROM {table} WHERE call_id = %s ORDER BY id", (call_id,)
    ).fetchall()


def _conversation(state: CallState) -> EventLog:
    """A completed call: greeting, one exchange, a slot write, a close."""
    log = EventLog()
    log.append(EventType.CALL_STARTED, {"room": state.call_id})
    log.append(EventType.AGENT_TURN, {"text": "How can I help?", "by_agent": "GreetAgent"})
    log.append(EventType.USER_TURN, {"text": "My name is Arvind", "language": "en-IN"})
    log.append(
        EventType.SLOT_SET,
        {"slot": "name", "value": "Arvind", "by_agent": "GreetAgent"},
    )
    log.append(EventType.AGENT_TURN, {"text": "Thanks, Arvind.", "by_agent": "GreetAgent"})
    log.append(EventType.CSAT_RECORDED, {"csat": 5, "by_agent": "WrapAgent"})
    log.append(EventType.CALL_ENDED, {"reason": "end_call"})
    return log


# --------------------------------------------------------------------------
# Call start
# --------------------------------------------------------------------------


def test_start_call_writes_one_in_progress_row(conn, call_id):
    state = CallState(call_id=call_id)
    start_call(conn, state)
    conn.commit()

    rows = conn.execute(
        "SELECT id, status, ended_at, result FROM calls WHERE id = %s", (call_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == STATUS_IN_PROGRESS
    assert rows[0][2] is None  # ended_at
    assert rows[0][3] is None  # result


def test_start_call_is_idempotent(conn, call_id):
    """A retried job must not duplicate the row or move the start time."""
    state = CallState(call_id=call_id, started_at=datetime(2026, 9, 16, tzinfo=timezone.utc))
    start_call(conn, state)
    conn.commit()

    later = CallState(call_id=call_id, started_at=datetime(2026, 9, 17, tzinfo=timezone.utc))
    start_call(conn, later)
    conn.commit()

    rows = conn.execute(
        "SELECT started_at FROM calls WHERE id = %s", (call_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0].day == 16  # original preserved


def test_no_writes_happen_during_the_call(conn, call_id):
    """The design guarantees this by construction: building the log touches nothing."""
    state = CallState(call_id=call_id)
    start_call(conn, state)
    conn.commit()

    _conversation(state)  # a whole conversation, in memory

    assert _rows(conn, "events", call_id) == []
    assert _rows(conn, "turns", call_id) == []
    assert _rows(conn, "slots", call_id) == []


# --------------------------------------------------------------------------
# Finalisation
# --------------------------------------------------------------------------


def test_finalize_writes_every_event_turn_and_slot(conn, call_id):
    state = CallState(call_id=call_id)
    state.note_language("en-IN")
    start_call(conn, state)
    conn.commit()

    log = _conversation(state)
    finalize_call(conn, state, log, build_call_json(log, state))
    conn.commit()

    assert len(_rows(conn, "events", call_id)) == len(log)  # every event, none dropped
    assert len(_rows(conn, "turns", call_id)) == 3  # two agent, one user
    assert len(_rows(conn, "slots", call_id)) == 1


def test_turns_carry_the_logs_turn_idx_and_roles(conn, call_id):
    state = CallState(call_id=call_id)
    start_call(conn, state)
    conn.commit()
    log = _conversation(state)
    finalize_call(conn, state, log, build_call_json(log, state))
    conn.commit()

    rows = conn.execute(
        "SELECT turn_idx, role, agent_name, text FROM turns WHERE call_id = %s ORDER BY turn_idx",
        (call_id,),
    ).fetchall()
    assert [r[0] for r in rows] == [1, 2, 3]  # the log's numbering, not recomputed
    assert [r[1] for r in rows] == ["agent", "user", "agent"]
    assert rows[0][2] == "GreetAgent"
    assert rows[1][3] == "My name is Arvind"


def test_events_keep_seq_and_turn_idx_in_detail(conn, call_id):
    state = CallState(call_id=call_id)
    start_call(conn, state)
    conn.commit()
    log = _conversation(state)
    finalize_call(conn, state, log, build_call_json(log, state))
    conn.commit()

    rows = conn.execute(
        "SELECT event_type, detail FROM events WHERE call_id = %s ORDER BY (detail->>'seq')::int",
        (call_id,),
    ).fetchall()
    assert [r[0] for r in rows] == [e.type.value for e in log]
    assert [r[1]["seq"] for r in rows] == [e.seq for e in log]
    user_turn = next(r for r in rows if r[0] == "user_turn")
    assert user_turn[1]["turn_idx"] == 2


def test_result_column_holds_the_document_and_agrees_with_the_rows(conn, call_id):
    """calls.result and the normalized tables are two views of one log."""
    state = CallState(call_id=call_id)
    state.note_language("en-IN")
    state.resolution.status = ResolutionStatus.RESOLVED
    start_call(conn, state)
    conn.commit()

    log = _conversation(state)
    document = build_call_json(log, state)
    finalize_call(conn, state, log, document)
    conn.commit()

    row = conn.execute(
        "SELECT status, language, duration_s, ended_at, result FROM calls WHERE id = %s",
        (call_id,),
    ).fetchone()
    status, language, duration_s, ended_at, result = row

    assert status == "resolved"
    assert language == "en-IN"
    assert result["call_id"] == call_id
    assert result["csat"] == 5
    assert duration_s == result["duration_s"]
    assert ended_at is not None

    # the document's transcript and the turns table describe the same turns
    turn_count = len(_rows(conn, "turns", call_id))
    assert turn_count == len(result["transcript"])


def test_status_falls_back_to_completed_when_no_resolution_was_recorded(conn, call_id):
    state = CallState(call_id=call_id)
    start_call(conn, state)
    conn.commit()
    log = _conversation(state)
    finalize_call(conn, state, log, build_call_json(log, state))
    conn.commit()

    status = conn.execute("SELECT status FROM calls WHERE id = %s", (call_id,)).fetchone()[0]
    assert status == "completed"


def test_slot_history_keeps_both_writes_when_a_value_is_corrected(conn, call_id):
    """The flat slots map in §8 shows the final value; the table shows the history."""
    state = CallState(call_id=call_id)
    start_call(conn, state)
    conn.commit()

    log = EventLog()
    log.append(EventType.SLOT_SET, {"slot": "name", "value": "Arvin", "by_agent": "GreetAgent"})
    log.append(EventType.SLOT_SET, {"slot": "name", "value": "Arvind", "by_agent": "GreetAgent"})
    log.append(EventType.CALL_ENDED, {"reason": "end_call"})
    state.greet.name = Slot.fill("Arvind")

    finalize_call(conn, state, log, build_call_json(log, state))
    conn.commit()

    values = [
        r[0]
        for r in conn.execute(
            "SELECT value FROM slots WHERE call_id = %s ORDER BY id", (call_id,)
        ).fetchall()
    ]
    assert values == ["Arvin", "Arvind"]


def test_finalize_is_all_or_nothing(conn, call_id):
    """A failure mid-write must leave no partial rows to disagree with result."""
    state = CallState(call_id=call_id)
    start_call(conn, state)
    conn.commit()

    log = _conversation(state)
    document = build_call_json(log, state)
    # A document that cannot be adapted to JSONB fails inside the transaction,
    # after the event rows have already been inserted.
    document["resolution"] = {"status": None, "summary": object(), "ticket_id": None}

    with pytest.raises(Exception):
        finalize_call(conn, state, log, document)
    conn.rollback()

    assert _rows(conn, "events", call_id) == []
    assert _rows(conn, "turns", call_id) == []
    assert _rows(conn, "slots", call_id) == []
    row = conn.execute("SELECT status, result FROM calls WHERE id = %s", (call_id,)).fetchone()
    assert row[0] == STATUS_IN_PROGRESS  # still open, not half-finished
    assert row[1] is None


# --------------------------------------------------------------------------
# Crash loss — the plan's literal done-when, proved by killing a real process
# --------------------------------------------------------------------------


CRASH_SCRIPT = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {agent_dir!r})
    import psycopg
    from events import EventLog, EventType
    from persistence import start_call
    from state import CallState

    state = CallState(call_id={call_id!r})
    with psycopg.connect({dsn!r}) as conn:
        start_call(conn, state)
        conn.commit()

        # A conversation happens entirely in memory, as designed.
        log = EventLog()
        log.append(EventType.CALL_STARTED, {{"room": state.call_id}})
        log.append(EventType.AGENT_TURN, {{"text": "How can I help?"}})
        log.append(EventType.USER_TURN, {{"text": "my order is late"}})
        log.append(EventType.SLOT_SET, {{"slot": "name", "value": "Arvind"}})

        print("READY", flush=True)
        time.sleep(60)  # killed here, before finalize_call is ever reached
    """
)


def test_a_call_killed_mid_conversation_leaves_exactly_one_in_progress_row(conn, call_id):
    """Crash loss is bounded and visible — proved, not described.

    SIGKILL, so no atexit hook, no finally block and no graceful shutdown can
    run. Whatever is in the database afterwards is what a real crash leaves.
    """
    script = CRASH_SCRIPT.format(
        agent_dir=str(REPO / "agent"), call_id=call_id, dsn=DATABASE_URL
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(REPO),
    )
    try:
        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            line = proc.stdout.readline()
            if line.strip() == "READY":
                ready = True
                break
            if proc.poll() is not None:
                break
        assert ready, f"child never reached READY: {proc.stderr.read()[:400]}"

        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        assert proc.returncode == -signal.SIGKILL
    finally:
        if proc.poll() is None:  # pragma: no cover - only on an unexpected path
            proc.kill()

    # Everything below is asserted from a fresh connection: the child's own
    # connection died with it, so this is the durable state a crash leaves.
    with psycopg.connect(DATABASE_URL) as fresh:
        calls = fresh.execute(
            "SELECT status, ended_at, duration_s, result FROM calls WHERE id = %s", (call_id,)
        ).fetchall()
        assert len(calls) == 1, "exactly one calls row"
        status, ended_at, duration_s, result = calls[0]
        assert status == STATUS_IN_PROGRESS
        assert ended_at is None
        assert duration_s is None
        assert result is None, "no result — the document was never built"

        for table in ("turns", "events", "slots"):
            rows = fresh.execute(
                f"SELECT count(*) FROM {table} WHERE call_id = %s", (call_id,)
            ).fetchone()[0]
            assert rows == 0, f"{table} must be empty after a mid-call crash"


def test_the_crashed_call_is_distinguishable_from_one_that_never_started(conn, call_id):
    """The in_progress row is the point: the loss is visible, not silent."""
    state = CallState(call_id=call_id)
    start_call(conn, state)
    conn.commit()

    open_calls = conn.execute(
        "SELECT count(*) FROM calls WHERE id = %s AND status = %s",
        (call_id, STATUS_IN_PROGRESS),
    ).fetchone()[0]
    assert open_calls == 1
