"""Tests for the session-event -> EventLog mapping (M3 T4).

Exercises `turn_event_for_item` against real `ChatMessage` objects, which is
what the framework actually hands the `conversation_item_added` handler.

Run from the repository root:

    .venv/bin/python -m pytest tests/test_agent_wiring.py -q
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from livekit.agents.llm import ChatMessage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from agent import (  # noqa: E402
    WRAP_FORCED_CLOSING_LINE,
    ForcedWrapScheduler,
    do_forced_wrap,
    turn_event_for_item,
)
from events import EventLog, EventType  # noqa: E402
from state import (  # noqa: E402
    FORCE_WRAP_AFTER,
    MAX_INTENTS_PER_CALL,
    CallState,
    Intent,
    ResolutionStatus,
    SlotStatus,
    Stage,
)


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


# --------------------------------------------------------------------------
# Deviation #8 — fakes
#
# do_forced_wrap only ever calls session.say(...) and session.aclose() — no
# other AgentSession surface — so a real AgentSession is unnecessary here.
# --------------------------------------------------------------------------


def _set_event() -> asyncio.Event:
    ev = asyncio.Event()
    ev.set()
    return ev


class _FakeSpeechHandle:
    def __init__(self, playout_event: asyncio.Event) -> None:
        self._playout_event = playout_event

    async def wait_for_playout(self) -> None:
        await self._playout_event.wait()


class _FakeSession:
    def __init__(self, *, playout_event: asyncio.Event) -> None:
        self.say_calls: list[tuple[str, object]] = []
        self.aclose_calls = 0
        self._playout_event = playout_event

    def say(self, text: str, *, allow_interruptions=None, **kwargs) -> _FakeSpeechHandle:
        self.say_calls.append((text, allow_interruptions))
        return _FakeSpeechHandle(self._playout_event)

    async def aclose(self) -> None:
        self.aclose_calls += 1


def _elapsed_state(*, past_cap: bool = True) -> CallState:
    offset = FORCE_WRAP_AFTER + timedelta(seconds=1) if past_cap else timedelta(0)
    return CallState(call_id="c_test", started_at=datetime.now(timezone.utc) - offset)


# --------------------------------------------------------------------------
# Deviation #8 — do_forced_wrap
# --------------------------------------------------------------------------


def test_do_forced_wrap_speaks_the_exact_line_with_barge_in_suppressed():
    async def run() -> None:
        session = _FakeSession(playout_event=_set_event())
        log = EventLog()
        state = _elapsed_state()
        await do_forced_wrap(session, log, state)
        assert session.say_calls == [(WRAP_FORCED_CLOSING_LINE, False)]

    asyncio.run(run())


def test_do_forced_wrap_sets_stage_resolution_and_logs_call_ended():
    async def run() -> None:
        session = _FakeSession(playout_event=_set_event())
        log = EventLog()
        state = _elapsed_state()
        await do_forced_wrap(session, log, state)

        assert state.stage is Stage.WRAP
        assert state.resolution.status is ResolutionStatus.WRAP_FORCED

        ended = [e for e in log.events if e.type is EventType.CALL_ENDED]
        assert len(ended) == 1
        assert ended[0].payload == {"reason": "wrap_forced"}

    asyncio.run(run())


def test_do_forced_wrap_closes_the_session_after_playout():
    async def run() -> None:
        session = _FakeSession(playout_event=_set_event())
        log = EventLog()
        state = _elapsed_state()
        await do_forced_wrap(session, log, state)
        assert session.aclose_calls == 1

    asyncio.run(run())


def test_do_forced_wrap_leaves_slots_untouched():
    """No retroactive UNAVAILABLE marking — the incomplete state honestly
    reflects a call cut short, per the approved plan."""

    async def run() -> None:
        session = _FakeSession(playout_event=_set_event())
        log = EventLog()
        state = _elapsed_state()
        state.start_intent(Intent.ORDER_STATUS)
        await do_forced_wrap(session, log, state)
        assert state.slots.order_status.order_id.status is SlotStatus.PENDING
        assert state.slots.order_status.issue_type.status is SlotStatus.PENDING

    asyncio.run(run())


def test_do_forced_wrap_is_a_noop_if_the_call_already_ended():
    async def run() -> None:
        session = _FakeSession(playout_event=_set_event())
        log = EventLog()
        log.append(EventType.CALL_ENDED, {"reason": "end_call"})
        state = _elapsed_state()

        await do_forced_wrap(session, log, state)

        assert session.say_calls == []
        assert session.aclose_calls == 0
        assert state.resolution.status is None

    asyncio.run(run())


# --------------------------------------------------------------------------
# Deviation #8 — ForcedWrapScheduler
# --------------------------------------------------------------------------


def test_scheduler_ignores_user_turns():
    async def run() -> None:
        session = _FakeSession(playout_event=_set_event())
        log = EventLog()
        state = _elapsed_state()
        scheduler = ForcedWrapScheduler(session=session, event_log=log, call_state=state)

        scheduler.maybe_schedule(EventType.USER_TURN)
        await asyncio.sleep(0)  # let any scheduled task get a chance to run

        assert scheduler.task is None
        assert session.say_calls == []

    asyncio.run(run())


def test_scheduler_does_nothing_before_the_cap():
    async def run() -> None:
        session = _FakeSession(playout_event=_set_event())
        log = EventLog()
        state = _elapsed_state(past_cap=False)
        scheduler = ForcedWrapScheduler(session=session, event_log=log, call_state=state)

        scheduler.maybe_schedule(EventType.AGENT_TURN)
        await asyncio.sleep(0)

        assert scheduler.task is None
        assert session.say_calls == []

    asyncio.run(run())


def test_scheduler_schedules_and_completes_once_the_cap_is_reached():
    async def run() -> None:
        session = _FakeSession(playout_event=_set_event())
        log = EventLog()
        state = _elapsed_state()
        scheduler = ForcedWrapScheduler(session=session, event_log=log, call_state=state)

        scheduler.maybe_schedule(EventType.AGENT_TURN)
        assert scheduler.task is not None
        await scheduler.task

        assert session.say_calls == [(WRAP_FORCED_CLOSING_LINE, False)]
        assert state.resolution.status is ResolutionStatus.WRAP_FORCED

    asyncio.run(run())


def test_scheduler_does_not_schedule_a_second_task_while_the_first_is_pending():
    """The race in question: a second agent-turn event fires while
    do_forced_wrap is still suspended inside wait_for_playout(). Asserts on
    scheduling itself (task identity), not just on the eventual event-log
    state — a weaker end-state-only assertion could pass even with a latent
    double-scheduling bug."""

    async def run() -> None:
        playout_event = asyncio.Event()  # left unset: wait_for_playout() blocks
        session = _FakeSession(playout_event=playout_event)
        log = EventLog()
        state = _elapsed_state()
        scheduler = ForcedWrapScheduler(session=session, event_log=log, call_state=state)

        scheduler.maybe_schedule(EventType.AGENT_TURN)
        await asyncio.sleep(0)  # let the task start and suspend on wait_for_playout()
        first_task = scheduler.task
        assert first_task is not None
        assert not first_task.done()

        # A second agent turn lands while the first forced-wrap is still in flight.
        scheduler.maybe_schedule(EventType.AGENT_TURN)

        assert scheduler.task is first_task  # no second task was created
        assert len(session.say_calls) == 1  # say() was only ever called once

        playout_event.set()
        await first_task

        assert len(session.say_calls) == 1  # still only once, after completion too

    asyncio.run(run())


# --------------------------------------------------------------------------
# Deviation #8 — precedence vs. the 3-intent cap
# --------------------------------------------------------------------------


def test_time_cap_still_fires_when_the_intent_cap_is_already_reached():
    """This asserts the two mechanisms are genuinely independent: reaching
    MAX_INTENTS_PER_CALL does not block the time cap from firing. Per the
    documented precedence rule, the time check always has final say for the
    turn it runs in, since it is evaluated after any tool calls that turn
    already made. Drives CallState directly (not through RouterAgent.set_intent)
    because this test is about the scheduler/state-layer independence, not
    about Router's own cap handling — that is covered in test_agents.py."""

    async def run() -> None:
        session = _FakeSession(playout_event=_set_event())
        log = EventLog()
        state = _elapsed_state()
        for intent in (Intent.ORDER_STATUS, Intent.PRODUCT_INFO, Intent.COMPLAINT):
            state.start_intent(intent)
        assert len(state.intents_handled) == MAX_INTENTS_PER_CALL

        scheduler = ForcedWrapScheduler(session=session, event_log=log, call_state=state)
        scheduler.maybe_schedule(EventType.AGENT_TURN)
        await scheduler.task

        assert state.resolution.status is ResolutionStatus.WRAP_FORCED

    asyncio.run(run())
