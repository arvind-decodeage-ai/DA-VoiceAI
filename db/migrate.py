"""M0 database migration.

Creates the four tables (calls, turns, events, slots) described in PRD §8/§9 against
DATABASE_URL. Safe to run more than once (CREATE TABLE IF NOT EXISTS).

Run with:

    python db/migrate.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from config import get_settings  # noqa: E402

load_dotenv()

logger = logging.getLogger("da-voice.migrate")
logging.basicConfig(level=logging.INFO)

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id           TEXT PRIMARY KEY,
    started_at   TIMESTAMPTZ,
    ended_at     TIMESTAMPTZ,
    duration_s   INTEGER,
    language     TEXT,
    status       TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    id          BIGSERIAL PRIMARY KEY,
    call_id     TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    turn_idx    INTEGER NOT NULL,
    role        TEXT NOT NULL,
    agent_name  TEXT,
    text        TEXT,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    id          BIGSERIAL PRIMARY KEY,
    call_id     TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL,
    detail      JSONB,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS slots (
    id          BIGSERIAL PRIMARY KEY,
    call_id     TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    value       TEXT,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- M3 D4: the built PRD §8 document, stored whole. The normalized tables above
-- stay the queryable projection; this column is the authoritative artifact and
-- the home for §8 fields that have no column of their own (csat, resolution,
-- compliance_flags, latency, recording_url). Added with IF NOT EXISTS so this
-- migration remains safe to re-run, like everything above it.
ALTER TABLE calls ADD COLUMN IF NOT EXISTS result JSONB;

CREATE INDEX IF NOT EXISTS idx_turns_call_id ON turns(call_id);
CREATE INDEX IF NOT EXISTS idx_events_call_id ON events(call_id);
CREATE INDEX IF NOT EXISTS idx_slots_call_id ON slots(call_id);
"""


def migrate() -> None:
    settings = get_settings()
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
    logger.info("migration applied: calls (+result), turns, events, slots")


if __name__ == "__main__":
    migrate()
