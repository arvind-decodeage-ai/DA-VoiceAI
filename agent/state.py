"""CallState + slot schemas (PRD §5/§8/§9, M3 T2).

Scope note: this is the LIVE, in-session state, held in ``session.userdata``
(M3 plan decision D3 — in-process, not Redis). It is NOT the final call JSON:
that is ``output.py::JSONBuilder``, built from the EventLog. CallState must
never grow fields that belong only to the final document
(``compliance_flags``, ``guardrail_events``, ``latency``, ``recording_url``) —
those are JSONBuilder's job, sourced from the EventLog.

For the same reason there is no turn counter here. The M3 plan gives the
EventLog sole ownership of ``turn_idx`` so there is exactly one counter in the
system; anything needing it reads the log.

Deviation #5 (per-turn language matching, not PRD §5's literal "detect +
lock"): there is no single locked ``language`` field. ``current_language``
reflects the most recent turn and ``languages_seen`` is every language observed
this call. Which single value becomes the final JSON's top-level ``language``
(§8) is a JSONBuilder-time decision, not a CallState concern.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# PRD §5 flow ("anything else, <3 intents") and §10 M5 ("Multi-intent up to 3
# per call"). Not a §11 non-functional requirement.
MAX_INTENTS_PER_CALL = 3

# Deviation #8: a hard call-duration cap, not specified by PRD §11's NFRs.
# Added deliberately — an unbounded "anything else?" Router loop-back (PRD §5)
# has unbounded LLM/tool-call cost per call and is a free vector for someone to
# tie up the agent indefinitely. Once FORCE_WRAP_AFTER elapses, the call is
# forced toward a deterministic close (see should_force_wrap() below and the
# forced-close flow in agent.py) instead of being allowed to keep looping.
#
# This is the same mechanism PRD §10 M6 already plans ("8 min ... caps") —
# not a separate one. FORCE_WRAP_AFTER is kept as a single tunable constant
# specifically so M6 changes the duration (3 min -> 8 min) without
# restructuring should_force_wrap() or the forced-close flow it triggers.
FORCE_WRAP_AFTER = timedelta(minutes=3)


class Intent(str, Enum):
    ORDER_STATUS = "order_status"
    PRODUCT_INFO = "product_info"
    RETURNS_REFUND = "returns_refund"
    SUBSCRIPTION = "subscription"
    COMPLAINT = "complaint"
    FALLBACK = "fallback"


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    CALLBACK = "callback"
    ABANDONED = "abandoned"
    TIMEOUT = "timeout"
    # Deviation #8 (see FORCE_WRAP_AFTER above): a call closed by the
    # duration cap, not by TIMEOUT's existing meaning (STT/TTS/OpenRouter
    # infra failure -> graceful exit, PRD §11). Named to describe the
    # mechanism, not the existing TIMEOUT value, since the two causes are
    # different and conflating them would lose that distinction in the data.
    WRAP_FORCED = "wrap_forced"


class Stage(str, Enum):
    """Where the call is in the conversation flow (PRD §5).

    Added in T6, when the Greet -> Wrap machine first needed it. ROUTE and
    RESOLVE are declared but have no producer until M4/M5, for the same reason
    the resolve-stage slot schemas are declared: the shape settles once.
    """

    GREET = "greet"
    ROUTE = "route"
    RESOLVE = "resolve"
    WRAP = "wrap"


class SlotStatus(str, Enum):
    """Distinguishes "not yet asked" from "asked, customer had nothing to give".

    Required for the code-enforced Wrap gate (PRD §5): a stage's required slots
    must be FILLED or UNAVAILABLE, never PENDING.
    """

    PENDING = "pending"
    FILLED = "filled"
    UNAVAILABLE = "unavailable"


class Slot(BaseModel):
    """A single piece of captured information, with explicit fill state.

    A FILLED slot must carry a real value. Blank and whitespace-only strings are
    rejected rather than accepted, because the Wrap gate is enforced in code
    (PRD §5) and a slot "filled" with ``""`` would satisfy that gate while
    carrying no information — an empty STT transcript could otherwise open the
    gate. Use :meth:`mark_unavailable` for "asked, nothing to give"; that is
    what UNAVAILABLE is for.

    Values are stripped of surrounding whitespace on the way in.
    """

    value: Optional[str] = None
    status: SlotStatus = SlotStatus.PENDING

    @field_validator("value")
    @classmethod
    def _strip_value(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value is not None else None

    @field_validator("status")
    @classmethod
    def _value_consistency(cls, status: SlotStatus, info) -> SlotStatus:
        # `value` is declared first, so it is already validated and present in
        # info.data by the time this runs.
        value = info.data.get("value")
        if status is SlotStatus.FILLED and not value:
            raise ValueError(
                "status=FILLED requires a non-empty value; "
                "use mark_unavailable() for 'asked, nothing to give'"
            )
        return status

    @property
    def is_resolved(self) -> bool:
        """True when this slot no longer blocks a stage exit."""
        return self.status in (SlotStatus.FILLED, SlotStatus.UNAVAILABLE)

    @classmethod
    def fill(cls, value: str) -> "Slot":
        return cls(value=value, status=SlotStatus.FILLED)

    @classmethod
    def mark_unavailable(cls) -> "Slot":
        return cls(value=None, status=SlotStatus.UNAVAILABLE)


class StageSlots(BaseModel):
    """Shared behaviour for per-stage slot containers."""

    def required_fields(self) -> list[str]:
        """Which fields must be resolved before the stage can be exited."""
        raise NotImplementedError

    def is_complete(self) -> bool:
        return all(getattr(self, name).is_resolved for name in self.required_fields())

    def unresolved_fields(self) -> list[str]:
        return [
            name for name in self.required_fields() if not getattr(self, name).is_resolved
        ]


# --------------------------------------------------------------------------
# Per-stage slot schemas — PRD §5 "Must capture (slots)" column
#
# Only Greet and Wrap are exercised in M3. The resolve-stage schemas are
# declared here because CallState's shape is meant to be designed once, so M4
# and M5 add agents rather than reshaping state that the EventLog, JSONBuilder
# and the database projection all depend on. They are pure declarations: no
# behaviour, no tools, no agents.
# --------------------------------------------------------------------------


class GreetSlots(StageSlots):
    # `language` deliberately absent — see module docstring, deviation #5.
    identity_confirmed: Slot = Field(default_factory=Slot)
    name: Slot = Field(default_factory=Slot)

    def required_fields(self) -> list[str]:
        return ["identity_confirmed", "name"]


class RouterSlots(StageSlots):
    intent: Slot = Field(default_factory=Slot)
    intent_reason: Slot = Field(default_factory=Slot)

    def required_fields(self) -> list[str]:
        return ["intent"]  # intent_reason is descriptive, not gating


class OrderStatusSlots(StageSlots):
    order_id: Slot = Field(default_factory=Slot)
    issue_type: Slot = Field(default_factory=Slot)

    def required_fields(self) -> list[str]:
        return ["order_id", "issue_type"]


class ProductInfoSlots(StageSlots):
    product: Slot = Field(default_factory=Slot)
    question: Slot = Field(default_factory=Slot)

    def required_fields(self) -> list[str]:
        return ["product", "question"]


class ReturnsRefundSlots(StageSlots):
    order_id: Slot = Field(default_factory=Slot)
    reason: Slot = Field(default_factory=Slot)
    eligible: Slot = Field(default_factory=Slot)

    def required_fields(self) -> list[str]:
        return ["order_id", "reason", "eligible"]


class SubscriptionSlots(StageSlots):
    subscription_id: Slot = Field(default_factory=Slot)
    action: Slot = Field(default_factory=Slot)

    def required_fields(self) -> list[str]:
        return ["subscription_id", "action"]


class ComplaintSlots(StageSlots):
    issue: Slot = Field(default_factory=Slot)
    product: Slot = Field(default_factory=Slot)
    severity: Slot = Field(default_factory=Slot)

    def required_fields(self) -> list[str]:
        return ["issue", "product", "severity"]


class FallbackSlots(StageSlots):
    callback_slot: Slot = Field(default_factory=Slot)

    def required_fields(self) -> list[str]:
        return ["callback_slot"]


class WrapSlots(StageSlots):
    # `csat` here is the gate slot — it records that a rating was captured. The
    # authoritative numeric value lives in the EventLog's csat_recorded event,
    # which is what JSONBuilder reads for §8's integer "csat" field.
    summary_confirmed: Slot = Field(default_factory=Slot)
    csat: Slot = Field(default_factory=Slot)

    def required_fields(self) -> list[str]:
        return ["summary_confirmed", "csat"]


class IntentSlots(BaseModel):
    """Container for the resolve-stage slot sets.

    Only the agents actually visited get populated; the rest sit at their
    defaults (all PENDING), which is harmless because gating only ever checks
    the active intent's block.
    """

    order_status: OrderStatusSlots = Field(default_factory=OrderStatusSlots)
    product_info: ProductInfoSlots = Field(default_factory=ProductInfoSlots)
    returns_refund: ReturnsRefundSlots = Field(default_factory=ReturnsRefundSlots)
    subscription: SubscriptionSlots = Field(default_factory=SubscriptionSlots)
    complaint: ComplaintSlots = Field(default_factory=ComplaintSlots)
    fallback: FallbackSlots = Field(default_factory=FallbackSlots)

    def for_intent(self, intent: Intent) -> StageSlots:
        return getattr(self, intent.value)


class Caller(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    customer_id: Optional[str] = None


class Resolution(BaseModel):
    status: Optional[ResolutionStatus] = None
    summary: Optional[str] = None
    ticket_id: Optional[str] = None


class CallState(BaseModel):
    """Live per-call state, carried in ``session.userdata``."""

    call_id: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Per-turn language tracking (deviation #5 — see module docstring).
    #
    # `languages_seen` is a list rather than a set on purpose. Sets are not
    # JSON-native: this state is dumped through JSONBuilder and stored in
    # `calls.result` (JSONB), and psycopg's JSON adapter uses json.dumps, which
    # raises TypeError on a set. A list also gives deterministic order, which
    # keeps dumps and test assertions stable. Order is first-seen; duplicates
    # are not appended.
    current_language: Optional[str] = None
    languages_seen: list[str] = Field(default_factory=list)

    stage: Stage = Stage.GREET

    caller: Caller = Field(default_factory=Caller)
    greet: GreetSlots = Field(default_factory=GreetSlots)
    router: RouterSlots = Field(default_factory=RouterSlots)
    slots: IntentSlots = Field(default_factory=IntentSlots)
    wrap: WrapSlots = Field(default_factory=WrapSlots)

    intents_handled: list[Intent] = Field(default_factory=list)
    active_intent: Optional[Intent] = None

    resolution: Resolution = Field(default_factory=Resolution)

    def note_language(self, lang: str) -> None:
        self.current_language = lang
        if lang not in self.languages_seen:
            self.languages_seen.append(lang)

    def can_start_intent(self, intent: Intent) -> bool:
        """Whether ``start_intent`` would succeed.

        Re-entering an intent already handled this call is always allowed — the
        cap counts distinct intents, not visits.
        """
        if intent in self.intents_handled:
            return True
        return len(self.intents_handled) < MAX_INTENTS_PER_CALL

    def start_intent(self, intent: Intent) -> None:
        """Make ``intent`` active, recording it as handled.

        Raises ValueError past the cap. This is deliberately an exception and
        not a silent no-op: silently ignoring the cap would leave
        ``active_intent`` pointing at an intent the call never recorded, so the
        JSON and the database would disagree with what actually happened —
        exactly the class of drift the single-source-of-truth design exists to
        prevent.

        It is not this model's job to decide where an over-cap call goes
        either. PRD §5 routes the "anything else" loop back to Router only while
        under three intents, so at the cap the state machine sends the caller to
        Wrap — not to Fallback, which §5 reserves for an unrecognised intent
        twice over. The call site checks :meth:`can_start_intent` and routes
        accordingly; reaching the raise means a caller skipped that check, which
        is a programming error worth surfacing loudly.
        """
        if intent not in self.intents_handled:
            if len(self.intents_handled) >= MAX_INTENTS_PER_CALL:
                raise ValueError(
                    f"multi-intent cap of {MAX_INTENTS_PER_CALL} reached "
                    f"(PRD §5); check can_start_intent() before calling"
                )
            self.intents_handled.append(intent)
        self.active_intent = intent

    def stage_slots(self) -> Optional[StageSlots]:
        """The slot container gating the current stage, if it has one."""
        if self.stage is Stage.GREET:
            return self.greet
        if self.stage is Stage.ROUTE:
            return self.router
        if self.stage is Stage.RESOLVE:
            return self.active_slots()
        return None  # WRAP is terminal: nothing gates leaving it

    def can_leave_stage(self) -> bool:
        """The single gate predicate (PRD §5, "enforced in code, not the prompt").

        One function so M4 extends it for route and resolve rather than each
        agent inventing its own rule. WRAP is terminal, so it is always True.
        """
        slots = self.stage_slots()
        if slots is None:
            return self.stage is Stage.WRAP
        return slots.is_complete()

    def blocking_slots(self) -> list[str]:
        """Which slots are still holding the current stage open."""
        slots = self.stage_slots()
        return slots.unresolved_fields() if slots is not None else []

    def active_slots(self) -> Optional[StageSlots]:
        if self.active_intent is None:
            return None
        return self.slots.for_intent(self.active_intent)

    def ready_for_wrap(self) -> bool:
        """Code-enforced gate (PRD §5).

        Wrap is reachable only when the active intent's required slots are
        resolved. Never checked via prompt.
        """
        active = self.active_slots()
        if active is None:
            return False
        return active.is_complete()

    def should_force_wrap(self) -> bool:
        """Deviation #8 (see FORCE_WRAP_AFTER). True once the call has run at
        or past the duration cap, regardless of stage or active intent — the
        cap is a hard ceiling, not a stage-specific gate like the others here.
        """
        return datetime.now(timezone.utc) - self.started_at >= FORCE_WRAP_AFTER
