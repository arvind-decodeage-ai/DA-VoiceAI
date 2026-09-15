"""Tests for the session-event -> EventLog mapping (M3 T4).

Exercises `turn_event_for_item` against real `ChatMessage` objects, which is
what the framework actually hands the `conversation_item_added` handler.

Run from the repository root:

    .venv/bin/python -m pytest tests/test_agent_wiring.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

from livekit.agents.llm import ChatMessage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from agent import turn_event_for_item  # noqa: E402
from events import EventLog, EventType  # noqa: E402


def _assistant(text: str, *, interrupted: bool = False) -> ChatMessage:
    return ChatMessage(
        type="message", role="assistant", content=[text], interrupted=interrupted
    )


def _user(text: str) -> ChatMessage:
    return ChatMessage(type="message", role="user", content=[text])


def test_user_item_maps_to_user_turn_with_language():
    mapped = turn_event_for_item(_user("मेरा ऑर्डर कहाँ है?"), language="hi-IN", spoken_at=None)
    assert mapped is not None
    event_type, payload = mapped
    assert event_type is EventType.USER_TURN
    assert payload == {"text": "मेरा ऑर्डर कहाँ है?", "language": "hi-IN"}


def test_completed_agent_item_is_not_interrupted():
    mapped = turn_event_for_item(
        _assistant("Sure, I can help with that."), language=None, spoken_at="2026-09-15T12:00:00+00:00"
    )
    assert mapped is not None
    event_type, payload = mapped
    assert event_type is EventType.AGENT_TURN
    assert payload["interrupted"] is False
    assert payload["text"] == "Sure, I can help with that."
    assert payload["spoken_at"] == "2026-09-15T12:00:00+00:00"


def test_interrupted_agent_item_carries_the_flag():
    """The barge-in flag is only knowable here; no later consumer can recover it."""
    mapped = turn_event_for_item(
        _assistant("Sure, I can help with", interrupted=True), language=None, spoken_at=None
    )
    assert mapped is not None
    _, payload = mapped
    assert payload["interrupted"] is True


def test_interrupted_is_always_a_bool():
    """Guards against a None or a truthy object reaching the log and then JSON."""
    _, payload = turn_event_for_item(_assistant("hi"), language=None, spoken_at=None)
    assert payload["interrupted"] is False
    assert isinstance(payload["interrupted"], bool)


def test_non_turn_items_produce_nothing():
    class Handoff:  # stands in for AgentHandoff and any other item kind
        role = None

    assert turn_event_for_item(Handoff(), language=None, spoken_at=None) is None


def test_empty_content_becomes_empty_string_not_none():
    mapped = turn_event_for_item(
        ChatMessage(type="message", role="assistant", content=[]), language=None, spoken_at=None
    )
    assert mapped is not None
    _, payload = mapped
    assert payload["text"] == ""


def test_mapped_events_append_cleanly_and_carry_turn_idx():
    """End to end through the log, the way the handler uses it."""
    log = EventLog()
    for item, lang in [
        (_assistant("How can I help?"), None),
        (_user("where is my order"), "en-IN"),
        (_assistant("Let me ch", interrupted=True), None),
    ]:
        mapped = turn_event_for_item(item, language=lang, spoken_at=None)
        assert mapped is not None
        log.append(*mapped)

    assert [e.type for e in log.events] == [
        EventType.AGENT_TURN,
        EventType.USER_TURN,
        EventType.AGENT_TURN,
    ]
    assert [e.turn_idx for e in log.events] == [1, 2, 3]
    assert [e.payload.get("interrupted") for e in log.events] == [False, None, True]
