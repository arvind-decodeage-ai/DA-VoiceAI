"""WrapAgent — closes the call (PRD §5, M3 T6).

Summarises, asks "Did that resolve it for you?", and asks for a CSAT rating.
Its tools — ``record_csat`` and ``end_call`` — arrive in T7; until then the
rating is asked for conversationally and the wrap slots stay PENDING.
"""

from __future__ import annotations

from livekit.agents import Agent

from agents import compose_instructions
from events import EventLog

NAME = "WrapAgent"


class WrapAgent(Agent):
    def __init__(self, *, base_instructions: str, event_log: EventLog) -> None:
        super().__init__(instructions=compose_instructions(base_instructions, "wrap"))
        self._base_instructions = base_instructions
        self._log = event_log
