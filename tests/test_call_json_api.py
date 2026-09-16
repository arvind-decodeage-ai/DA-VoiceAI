"""Tests for the ./out/ document write and GET /calls/{id}/json (M3 T9).

The endpoint is exercised through FastAPI's TestClient against a real Postgres,
with rows written by the real persistence path — no mocked database, because
what is being tested is that the API reads what the agent actually wrote.

Run from the repository root:

    .venv/bin/python -m pytest tests/test_call_json_api.py -q
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent"))
load_dotenv(REPO / ".env")

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from events import EventLog, EventType  # noqa: E402
from output import build_call_json  # noqa: E402
from persistence import finalize_call, start_call, write_document  # noqa: E402
from state import CallState  # noqa: E402

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
def call_id():
    cid = f"c_t9_{uuid.uuid4().hex[:10]}"
    yield cid
    with psycopg.connect(DATABASE_URL) as c:
        c.execute("DELETE FROM calls WHERE id = %s", (cid,))
        c.commit()


@pytest.fixture
def client():
    """Import the FastAPI app with the `agent` PACKAGE resolvable.

    Repo-level name collision: `agent/agent.py` and the `agent/` package share a
    name. tests/test_agent_wiring.py imports the module (agent/ is on sys.path,
    so `import agent` finds the file), which shadows the package that
    api/main.py needs for `from agent.config import ...`. Swap the binding for
    the duration of this fixture and restore it, so neither test file breaks the
    other regardless of run order.
    """
    stale = sys.modules.pop("agent", None)
    if str(REPO) in sys.path:
        sys.path.remove(str(REPO))
    sys.path.insert(0, str(REPO))
    try:
        from api.main import app

        with TestClient(app) as c:
            yield c
    finally:
        sys.modules.pop("agent", None)
        if stale is not None:
            sys.modules["agent"] = stale


def _finished_call(call_id: str, out_dir: Path | None):
    """Run a whole call through the real persistence path."""
    state = CallState(call_id=call_id)
    state.note_language("en-IN")

    log = EventLog()
    log.append(EventType.CALL_STARTED, {"room": call_id})
    log.append(EventType.AGENT_TURN, {"text": "How can I help?", "by_agent": "GreetAgent"})
    log.append(EventType.USER_TURN, {"text": "where is my order", "language": "en-IN"})
    log.append(EventType.CSAT_RECORDED, {"csat": 4, "by_agent": "WrapAgent"})
    log.append(EventType.CALL_ENDED, {"reason": "end_call"})

    document = build_call_json(log, state)
    with psycopg.connect(DATABASE_URL) as conn:
        start_call(conn, state)
        conn.commit()
        path = finalize_call(conn, state, log, document, out_dir=out_dir)
        conn.commit()
    return document, path


# --------------------------------------------------------------------------
# ./out/<call_id>.json
# --------------------------------------------------------------------------


def test_finalize_writes_the_document_to_out(call_id, tmp_path):
    document, path = _finished_call(call_id, tmp_path)

    assert path == tmp_path / f"{call_id}.json"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == document


def test_the_file_and_the_row_hold_identical_content(call_id, tmp_path):
    """Both come from one document object, so they cannot diverge."""
    _, path = _finished_call(call_id, tmp_path)

    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute("SELECT result FROM calls WHERE id = %s", (call_id,)).fetchone()

    assert json.loads(path.read_text(encoding="utf-8")) == row[0]


def test_out_dir_is_created_if_missing(call_id, tmp_path):
    target = tmp_path / "nested" / "out"
    _, path = _finished_call(call_id, target)
    assert path is not None and path.parent == target


def test_document_written_is_unicode_readable(call_id, tmp_path):
    document = {"call_id": call_id, "transcript": [{"text": "मेरा ऑर्डर कहाँ है?"}]}
    path = write_document(tmp_path, call_id, document)
    assert path is not None
    raw = path.read_text(encoding="utf-8")
    assert "मेरा ऑर्डर कहाँ है?" in raw  # not \\u escapes


def test_a_file_write_failure_does_not_break_the_call(call_id, tmp_path):
    """The row is already committed; a filesystem problem must not undo that."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")

    document, path = _finished_call(call_id, blocker)  # out_dir is a file
    assert path is None  # the write failed, and said so

    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute("SELECT result FROM calls WHERE id = %s", (call_id,)).fetchone()
    assert row[0] == document  # the call is still durably recorded


def test_out_dir_none_skips_the_file(call_id):
    _, path = _finished_call(call_id, None)
    assert path is None


# --------------------------------------------------------------------------
# GET /calls/{id}/json
# --------------------------------------------------------------------------


def test_endpoint_serves_the_document_for_a_finished_call(client, call_id, tmp_path):
    document, _ = _finished_call(call_id, tmp_path)

    response = client.get(f"/calls/{call_id}/json")
    assert response.status_code == 200
    assert response.json() == document


def test_endpoint_reads_the_database_not_the_file(client, call_id, tmp_path):
    """The file is a convenience copy and may be absent; the row is the truth."""
    document, path = _finished_call(call_id, tmp_path)
    path.unlink()

    response = client.get(f"/calls/{call_id}/json")
    assert response.status_code == 200
    assert response.json() == document


def test_unknown_call_is_404(client):
    assert client.get("/calls/c_does_not_exist/json").status_code == 404


def test_a_call_still_in_progress_is_404(client, call_id):
    """No document yet is the same to a caller as no such call."""
    state = CallState(call_id=call_id)
    with psycopg.connect(DATABASE_URL) as conn:
        start_call(conn, state)
        conn.commit()

    response = client.get(f"/calls/{call_id}/json")
    assert response.status_code == 404
    assert "no call document" in response.json()["detail"]


def test_served_document_validates_against_the_schema(client, call_id, tmp_path):
    from jsonschema import Draft202012Validator

    _finished_call(call_id, tmp_path)
    schema = json.loads((Path(__file__).parent / "call_output.schema.json").read_text())

    response = client.get(f"/calls/{call_id}/json")
    Draft202012Validator(schema).validate(response.json())


def test_the_api_still_creates_no_rows(client, call_id):
    """M2's position stands for writes: the agent writes, the API reads."""
    with psycopg.connect(DATABASE_URL) as conn:
        before = conn.execute("SELECT count(*) FROM calls").fetchone()[0]

    client.get(f"/calls/{call_id}/json")  # 404, and must not insert anything

    with psycopg.connect(DATABASE_URL) as conn:
        after = conn.execute("SELECT count(*) FROM calls").fetchone()[0]
    assert before == after
