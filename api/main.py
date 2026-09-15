"""M2 FastAPI: create a call, dispatch the agent into its room, tear it down.

Three endpoints, no persistence:

    POST   /calls            create room + dispatch agent + mint caller token
    DELETE /calls/{call_id}  delete the room (the agent's job ends with it)
    GET    /healthz          liveness, plus whether LiveKit is reachable

Call state lives in an in-memory dict. Postgres is deliberately untouched in M2 —
EventLog, CallState and the JSON output arrive in M3 (PRD §10), which is also when
the schema gains meaning.

Run from the repository root (``Settings`` resolves ``.env`` relative to CWD):

    uvicorn api.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from livekit import api
from pydantic import BaseModel

from agent.config import get_settings

logger = logging.getLogger("da-voice.api")

# Must match WorkerOptions(agent_name=...) in agent/agent.py. With explicit dispatch
# enabled there, a worker only ever joins rooms it is dispatched to by name — if these
# two strings drift apart, POST /calls succeeds and no agent ever arrives.
AGENT_NAME = "da-voice"

# The browser needs the ws:// URL; the server-side API client needs the http:// one.
# Derived rather than configured separately so there is a single source of truth.
_WS_TO_HTTP = {"ws://": "http://", "wss://": "https://"}

CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


def http_url_from_ws(ws_url: str) -> str:
    """ws://host:7880 -> http://host:7880 (https for wss). Other schemes pass through."""
    for ws_scheme, http_scheme in _WS_TO_HTTP.items():
        if ws_url.startswith(ws_scheme):
            return http_scheme + ws_url[len(ws_scheme) :]
    return ws_url


def new_call_id() -> str:
    """Short, URL-safe, collision-resistant enough for a desktop prototype."""
    return f"c_{uuid4().hex[:12]}"


@dataclass
class CallRecord:
    call_id: str
    created_at: datetime


class CreateCallResponse(BaseModel):
    call_id: str
    room: str
    url: str
    token: str


class HealthResponse(BaseModel):
    status: str
    livekit_reachable: bool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.calls = {}
    app.state.lkapi = api.LiveKitAPI(
        url=http_url_from_ws(settings.livekit_url),
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )
    logger.info("api ready, livekit=%s", http_url_from_ws(settings.livekit_url))
    try:
        yield
    finally:
        await app.state.lkapi.aclose()


app = FastAPI(title="da-voice API", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    try:
        await app.state.lkapi.room.list_rooms(api.ListRoomsRequest())
        reachable = True
    except Exception as e:  # noqa: BLE001 - health must report, never raise
        logger.warning("livekit unreachable: %s", e)
        reachable = False
    return HealthResponse(status="ok", livekit_reachable=reachable)


@app.post("/calls", response_model=CreateCallResponse, status_code=201)
async def create_call() -> CreateCallResponse:
    """Create the room, dispatch the agent into it, and mint the caller's token."""
    settings = app.state.settings
    call_id = new_call_id()

    try:
        await app.state.lkapi.room.create_room(api.CreateRoomRequest(name=call_id))
    except Exception as e:  # noqa: BLE001
        logger.exception("failed to create room %s", call_id)
        raise HTTPException(status_code=502, detail=f"livekit create_room failed: {e}") from e

    try:
        await app.state.lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=call_id)
        )
    except Exception as e:  # noqa: BLE001
        # Don't leave an agent-less room behind for someone to connect to.
        logger.exception("failed to dispatch agent into %s, deleting room", call_id)
        try:
            await app.state.lkapi.room.delete_room(api.DeleteRoomRequest(room=call_id))
        except Exception:  # noqa: BLE001
            logger.exception("cleanup delete_room failed for %s", call_id)
        raise HTTPException(status_code=502, detail=f"livekit dispatch failed: {e}") from e

    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity("caller")
        .with_name("Caller")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=call_id,
                can_publish=True,
                can_subscribe=True,
            )
        )
        .to_jwt()
    )

    app.state.calls[call_id] = CallRecord(call_id=call_id, created_at=datetime.now(timezone.utc))
    logger.info("call created: %s", call_id)

    return CreateCallResponse(
        call_id=call_id,
        room=call_id,
        url=settings.livekit_url,
        token=token,
    )


@app.delete("/calls/{call_id}", status_code=204)
async def end_call(call_id: str) -> None:
    """Delete the room. The worker's job ends when the room goes away.

    Idempotent: deleting an unknown or already-deleted room is not an error, so the
    browser's End button can always be pressed without special-casing.
    """
    try:
        await app.state.lkapi.room.delete_room(api.DeleteRoomRequest(room=call_id))
    except Exception as e:  # noqa: BLE001
        logger.exception("failed to delete room %s", call_id)
        raise HTTPException(status_code=502, detail=f"livekit delete_room failed: {e}") from e

    app.state.calls.pop(call_id, None)
    logger.info("call ended: %s", call_id)
