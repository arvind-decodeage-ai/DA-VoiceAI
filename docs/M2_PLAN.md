# M2 plan — UI + dispatch

**Status:** awaiting approval. No implementation started.
**Depends on:** M1, closed 2026-09-15 by deliberate risk acceptance (see `M1_STATUS.md`).

## Objective

PRD §10 M2: a React UI with Start/End, live transcript, stage badge and timer; a FastAPI
`POST /calls` that mints a LiveKit token and dispatches the worker into a room.

**PRD acceptance:** click Start in browser, talk to agent, click End, room closes cleanly.

## Scope

### In

- `api/main.py` — FastAPI with three endpoints: `POST /calls`, `DELETE /calls/{call_id}`,
  `GET /healthz`. Creates the room, dispatches the agent, mints a participant access token,
  and tears the room down on end.
- `web/` — minimal Vite + React app: Start/End buttons, mic/speaker, live transcript,
  status badge, call timer.
- `agent/agent.py` — one line: `agent_name="da-voice"` in `WorkerOptions`, to enable
  explicit dispatch.
- `pyproject.toml` — add `fastapi` and `uvicorn`.
- `README.md` — how to run the four processes.

### Out (per the PRD's M2 definition; these are M3+)

- `CallState`, `EventLog`, `JSONBuilder`, the `./out/<call_id>.json` output and its viewer
- Conversation stages, intent routing, handoffs, `set_intent` — anything resembling a state machine
- Tools of any kind, fixture endpoints (`/tools/*`), `GET /calls/{id}/json`
- Guardrails, input rails, output filters, silence timers, call caps
- Persistence of any kind — Postgres is untouched in M2; call state lives in an in-memory dict
- Recordings (MinIO/Egress), Langfuse tracing
- The two accepted M1 latency debts — M2 does not attempt to fix barge-in or end-of-speech latency

## Architecture

```
browser :3000                FastAPI :8000              LiveKit :7880         worker
─────────────                ─────────────              ─────────────         ──────
[Start] ──── POST /calls ──▶  create_room(call_id)
                             create_dispatch(call_id,
                                 "da-voice")  ─────────▶ job assigned ───────▶ entrypoint()
             ◀── {call_id,    mint AccessToken                                 ctx.connect()
                  url, token}
  connect(url, token) ──────────────────────────────────▶ room
  mic ⇄ audio ⇄ agent audio                              ◀──────────────────── publishes audio
  transcript ◀── text stream (lk.transcription) ────────────────────────────── RoomIO
  badge     ◀── participant attribute (lk.agent.state) ─────────────────────── RoomIO

[End]  ──── room.disconnect()
       ──── DELETE /calls/{id} ─▶ delete_room(call_id) ─▶ job ends ──────────▶ shutdown
```

The transcript and badge need **no agent-side work**: `RoomIO` already publishes
`ATTRIBUTE_AGENT_STATE` on every `agent_state_changed` (`room_io.py:434-439`), and
`RoomOutputOptions.transcription_enabled` defaults to `True` (`types.py:276`). The UI
subscribes via `useVoiceAssistant()` / `useTranscriptions()` from `@livekit/components-react`.
If either fails to arrive in a real room, that is a finding to report — not something to work
around by adding a custom data channel.

### Decisions taken during design

| Question | Decision | Why |
|---|---|---|
| Stage badge with no stages until M3 | Show live `agent_state` (listening/thinking/speaking) | Satisfies the PRD bullet with real data, zero M3 work, component reusable in M3 |
| Dispatch mechanism | Explicit: `agent_name` + `create_dispatch` | Matches the PRD's "dispatches", gives the API control, same pattern telephony will use |
| UI stack | Minimal Vite + React | ~300 lines we own vs. a large opinionated starter M2 would spend time deleting |
| End semantics | API calls `delete_room`; verified via `list_rooms` | Deterministic; the alternative makes "cleanly" depend on an empty-room timeout |
| Persistence | None | Nothing in M2's acceptance needs a stored row; M3 designs the schema with `CallState` |
| Process orchestration | Four documented commands, four terminals | Keeps each log stream separately readable — the worker log is our diagnostic surface |

## Deviations from the PRD

| # | PRD says | We do | Status |
|---|---|---|---|
| 1 | LLM via OpenRouter | Groq, `qwen/qwen3.8-27b` | Manager-approved in M1; carried forward unchanged |
| 2 | UI is the "LiveKit React starter (Next.js)" (§4) | Minimal Vite + React using `@livekit/components-react` | Approved during M2 design. Same UI capability; avoids importing a large starter whose own token route duplicates `POST /calls` |
| 3 | — (no PRD statement) | `agent_name="da-voice"` added to `WorkerOptions` | Approved during M2 design. **Disables automatic dispatch** (`worker.py:219`): `python agent/agent.py dev` will no longer auto-join a manually created room. `console` mode is unaffected — it simulates a job rather than receiving a dispatch, so the M1 acceptance path is preserved |

## Acceptance criteria

1. **Start works** — clicking Start in the browser connects and the agent responds over real
   audio for at least 3 turns, at least one of them Hindi.
2. **Live transcript** — both sides render in the UI as they speak.
3. **Badge and timer** — the badge tracks `listening → thinking → speaking` across a turn;
   the timer counts up from connect.
4. **Clean close** — clicking End disconnects the browser; `list_rooms()` no longer contains
   the call_id; the worker logs its job exiting; no orphaned agent process remains.
5. **No M1 regression** — the M1 console path still passes via
   `tools/m1_harness.py`, with no dropped turns and no session errors.

Criteria 1–4 are the PRD's own bullet, made checkable. Criterion 5 exists because M2 changes
`agent/` for the first time since M1 closed.

## Task breakdown

Each task states what "done" means. Tasks are ordered; T1 gates everything else.

**T1 — `agent_name` + M1 regression (gate)**
Add `agent_name="da-voice"` to `WorkerOptions`. Re-run `tools/m1_harness.py --preemptive on`
for a short console session.
*Done when:* the harness summary shows turns completing with zero dropped turns and zero
session errors, confirming console mode is unaffected by the dispatch change. If it regresses,
stop and report — nothing else in M2 proceeds.

**T2 — FastAPI endpoints**
Add `fastapi`/`uvicorn` to `pyproject.toml`. Implement `POST /calls` (create room, dispatch,
mint token), `DELETE /calls/{call_id}` (delete room), `GET /healthz`. CORS for the Vite origin.
Derive the server-side `http://` LiveKit URL from `LIVEKIT_URL` rather than adding a second
env var.
*Done when:* `curl -X POST localhost:8000/calls` returns a call_id, ws url and token.

**T3 — Dispatch verified without a browser**
Drive T2 by hand and watch the worker.
*Done when:* after `POST /calls`, the worker log shows a job accepted for that room and
`list_rooms()` contains it; after `DELETE`, the room is gone and the job exits. This proves
the whole server side before any UI exists.

**T4 — Web scaffold and connection**
Vite + React app; Start button calls `POST /calls` and connects with the returned token;
mic publishes; agent audio plays.
*Done when:* two-way audio works in the browser — acceptance criterion 1.

**T5 — Transcript, badge, timer**
`useTranscriptions()` into a scrolling transcript; `useVoiceAssistant().state` into the badge;
a timer from connect.
*Done when:* acceptance criteria 2 and 3 hold.

**T6 — End flow**
End button disconnects the room and calls `DELETE /calls/{id}`; UI returns to its idle state.
*Done when:* acceptance criterion 4 holds, verified with `list_rooms()` and the worker log.

**T7 — Acceptance run and docs**
Full M2 acceptance pass against all five criteria; update `README.md` with the four run
commands; write `docs/M2_STATUS.md` recording the result.
*Done when:* all five criteria are evidenced, in the M1 pattern — measured, not asserted.

## New dependencies

- Python: `fastapi`, `uvicorn`. (`livekit-api` is already available — `LiveKitAPI` exposes both
  `room` and `agent_dispatch` services.)
- Web: `vite`, `react`, `react-dom`, `@livekit/components-react`, `livekit-client`.

## How it will be run

```
term 1   docker compose up -d
term 2   uvicorn api.main:app --reload --port 8000
term 3   python agent/agent.py start
term 4   cd web && npm run dev
```

Note `start`, not `dev`: with explicit dispatch the worker takes assigned jobs.

## Risks

- **Transcript/badge assumption.** Verified from the framework source, not yet observed in a
  real room. If either doesn't arrive, T5 becomes an investigation rather than wiring.
- **Dispatch change is the first edit to `agent/` since M1 closed.** T1 exists specifically to
  bound that risk before anything is built on top of it.
- **Known platform quirk — implicit empty-name dispatch (documented behaviour, no action).**
  `create_room` implicitly creates a dispatch with an *empty* `agent_name` alongside the
  explicit one we create, e.g. for a single call:

  ```
  AD_c47xF3rVzpnw  room: c_985052df5898              <- implicit, empty agent_name
  AD_4YM9dyHoLrJM  agent_name: "da-voice"            <- ours
  ```

  Harmless in this setup: nothing matches the empty entry and exactly one agent joins
  (verified in T3). It would only matter if a legacy or undispatched worker — one running
  without `agent_name` — were running concurrently, which would join through the implicit
  dispatch and put two agents in one room. Worth remembering if doubled audio is ever
  observed again. No code change.

- **The M1 dropped-turn bug is unresolved.** It did not reproduce in the M1 acceptance run and
  is not an M2 blocker, but if it surfaces during M2 testing it will look like a UI hang. The
  harness stage probes remain available to identify it.
