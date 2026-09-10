# da-voice

Decode Age Customer Support Voice Agent — desktop prototype (no telephony).

This directory is a self-contained `uv` project living inside the `DA-VoiceAI` git
repository, on branch `feat/desktop-voice-agent`. It does not import from, depend on,
or modify the existing `app/` (live-call/Exotel) code in this repo.

## Milestone status

- **M0 — Environment**: infrastructure, config, skeleton, DB migration. No STT/TTS/LLM/
  conversation logic yet (that starts at M1).

## M0 setup

```bash
cd da-voice
cp .env.example .env        # fill in SARVAM_API_KEY / OPENROUTER_API_KEY later; empty is fine for M0
uv sync
docker compose up -d
docker compose ps           # all 5 services should report healthy
python db/migrate.py        # creates calls, turns, events, slots tables
python agent/agent.py dev   # should log "agent ready" and stay connected to LiveKit
```

## Services (docker-compose.yml)

| Service | Port(s) | Purpose |
|---|---|---|
| livekit | 7880, 7881 | WebRTC media server (dev mode) |
| postgres | 5433 (host) → 5432 (container) | calls/turns/events/slots persistence |
| redis | 6379 | per-call state (used from M3+) |
| minio | 9000 (API), 9001 (console) | recordings/object storage (used from M7+) |
| langfuse | 3001 | tracing (used from M7+) |

> **Note:** this host already runs a native (apt) PostgreSQL 16 service on port 5432 for
> the existing production DA-VoiceAI app. To avoid a conflict, da-voice's Postgres
> container publishes on host port **5433** instead (internally it's still 5432, so
> other containers on the `da-voice_default` network — e.g. langfuse — reach it at
> `postgres:5432` unaffected). `DATABASE_URL` in `.env.example` reflects this
> (`localhost:5433`). If you run da-voice on a host without a conflicting Postgres,
> you can safely change the mapping back to `"5432:5432"` and drop the `5433`.

`docker compose down` followed by `docker compose up -d` preserves Postgres/Redis/MinIO
data via named volumes (`postgres_data`, `redis_data`, `minio_data`).

Langfuse is self-hosted using the single-container v2 image, which stores its own
tables in the same `postgres` service (but in a separate `langfuse` database,
auto-created on first init via `db/init/01-create-langfuse-db.sql` — this avoids the
`events` table colliding with da-voice's own `events` table in `da_voice`) — no extra
ClickHouse/S3/queue infrastructure is introduced, keeping the service count at exactly
five.

On first visit to `http://localhost:3001`, Langfuse requires creating an account/org/
project through its UI and generating a public/secret key pair to put in `.env`
(`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`). This is a manual, one-time step and is
not required for M0 (tracing is wired up in M7).

## Verification commands

```bash
docker compose up -d
docker compose ps
uv sync
python db/migrate.py
python agent/agent.py dev
```

## Scope

M0 only. No microphone capture, STT, TTS, LLM calls, conversation agents, guardrails,
timers, UI, or telephony. See the PRD for the full milestone plan (M1–M8).
