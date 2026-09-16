"""Stage agents for the M3 conversation machine (PRD §5).

Greet -> Wrap, with the transition enforced in code rather than in the prompt.
M4 and M5 add Router and the resolve agents; the gate predicate they extend
lives on CallState (``can_leave_stage``), not in any one agent.
"""

from __future__ import annotations

from pathlib import Path

_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def stage_prompt(name: str) -> str:
    """Load a stage prompt (``greet``, ``wrap``, ...) from agent/prompts/."""
    return (_PROMPTS / f"{name}.md").read_text().strip()


def compose_instructions(base_instructions: str, stage: str) -> str:
    """Base persona (plus any operational instructions) followed by the stage.

    The base is passed in rather than read here so there is one place that
    decides what the shared persona is — ``agent.py`` — and the stage agents
    stay ignorant of it.
    """
    return f"{base_instructions}\n\n{stage_prompt(stage)}"


__all__ = ["compose_instructions", "stage_prompt"]
