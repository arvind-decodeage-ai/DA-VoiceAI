"""M0 smoke-test entry point.

Proves the Python LiveKit Agents environment and the local LiveKit server (from
docker-compose.yml) are wired up correctly. Deliberately contains no STT, TTS, LLM,
VAD, turn-detection, or conversation logic — that begins at M1.

Run with:

    python agent/agent.py dev
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv
from livekit.agents import JobContext, WorkerOptions, cli

from config import get_settings

load_dotenv()

logger = logging.getLogger("da-voice.agent")
logging.basicConfig(level=logging.INFO)


async def entrypoint(ctx: JobContext) -> None:
    settings = get_settings()  # proves config loads cleanly; unused otherwise in M0
    await ctx.connect()
    logger.info("agent ready")


if __name__ == "__main__":
    settings = get_settings()
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            ws_url=settings.livekit_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        )
    )
