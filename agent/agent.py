"""M1: single-agent voice round trip.

A single LiveKit Agent with the base veteran-CS persona, no tools, no multi-agent
routing. Wires up:

  microphone -> Silero VAD -> LiveKit multilingual turn detector -> Sarvam realtime
  STT (saaras:v3-realtime) -> LLM (Groq via the OpenAI-compatible interface;
  OpenRouter kept available but commented out below) -> Sarvam TTS (bulbul:v3,
  linear16) -> speaker

Also instruments, using the framework's own session events (not invented timers):

  - end-of-speech -> first-audio-byte latency, per turn
  - barge-in (TTS interruption) latency, per interruption

Run with:

    python agent/agent.py console
"""

from __future__ import annotations

import logging
import statistics
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli
from livekit.agents.voice.events import (
    AgentStateChangedEvent,
    UserInputTranscribedEvent,
    UserStateChangedEvent,
)
from livekit.plugins import openai, silero
from livekit.plugins.sarvam import TTS as SarvamTTS
from livekit.plugins.sarvam import STTRealtime
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from config import get_settings

load_dotenv()

logger = logging.getLogger("da-voice.agent")
logging.basicConfig(level=logging.INFO)

BASE_PERSONA = (Path(__file__).parent / "prompts" / "base_persona.md").read_text()

# Not part of PRD §6 — base_persona.md stays verbatim PRD text. This is a separate,
# operational instruction: the M1 test log showed every reply coming back in English
# regardless of detected input language (e.g. a reply to a Hindi question, in English).
# Appended, not merged into BASE_PERSONA, so the PRD persona file remains unmodified.
LANGUAGE_MATCH_INSTRUCTION = (
    "Always reply in the same language the customer just used. If the customer spoke "
    "in Hindi, reply in Hindi. If the customer spoke in English, reply in English."
)


class TurnMetrics:
    """Measures M1's two timed acceptance criteria from AgentSession's own state
    events (UserStateChangedEvent / AgentStateChangedEvent), each of which carries a
    `created_at` timestamp stamped by the framework at the moment the underlying
    state actually changed (VAD end-of-speech, or the first TTS audio frame starting
    to forward) — not a timestamp taken later in our own event handler.
    """

    def __init__(self) -> None:
        self.eos_to_audio_s: list[float] = []
        self.bargein_cancel_s: list[float] = []
        self._pending_eos: float | None = None
        self._agent_speaking = False
        self._pending_interrupt_onset: float | None = None

    def on_user_state_changed(self, ev: UserStateChangedEvent) -> None:
        if ev.old_state == "speaking" and ev.new_state == "listening":
            # End of the user's speech, as detected by VAD/turn-detector.
            self._pending_eos = ev.created_at
            logger.info("[metrics] end-of-speech detected (t=%.3f)", ev.created_at)
        if ev.new_state == "speaking" and self._agent_speaking:
            # User started talking while the agent's TTS was still playing: barge-in onset.
            self._pending_interrupt_onset = ev.created_at
            logger.info("[metrics] barge-in onset detected (t=%.3f)", ev.created_at)

    def on_agent_state_changed(self, ev: AgentStateChangedEvent) -> None:
        if ev.new_state == "speaking":
            self._agent_speaking = True
            if self._pending_eos is not None:
                latency = ev.created_at - self._pending_eos
                self.eos_to_audio_s.append(latency)
                logger.info(
                    "[metrics] first audio byte (t=%.3f) — end-of-speech->audio latency=%.0fms",
                    ev.created_at,
                    latency * 1000,
                )
                self._pending_eos = None
        else:
            if self._agent_speaking and self._pending_interrupt_onset is not None:
                cancel_latency = ev.created_at - self._pending_interrupt_onset
                self.bargein_cancel_s.append(cancel_latency)
                logger.info(
                    "[metrics] TTS cancelled for barge-in (t=%.3f) — cancel latency=%.0fms",
                    ev.created_at,
                    cancel_latency * 1000,
                )
                self._pending_interrupt_onset = None
            self._agent_speaking = False

    def report(self) -> None:
        if self.eos_to_audio_s:
            median = statistics.median(self.eos_to_audio_s)
            logger.info(
                "[metrics] end-of-speech->first-audio-byte over %d turns: %s | median=%.0fms",
                len(self.eos_to_audio_s),
                [f"{v * 1000:.0f}ms" for v in self.eos_to_audio_s],
                median * 1000,
            )
        if self.bargein_cancel_s:
            logger.info(
                "[metrics] barge-in cancel latency over %d interruptions: %s",
                len(self.bargein_cancel_s),
                [f"{v * 1000:.0f}ms" for v in self.bargein_cancel_s],
            )


async def entrypoint(ctx: JobContext) -> None:
    settings = get_settings()
    await ctx.connect()

    session = AgentSession(
        stt=STTRealtime(
            language="auto",  # PRD says language="unknown"; this plugin's auto-detect
            # sentinel is "auto" — see M1 final report for the substitution rationale.
            api_key=settings.sarvam_api_key,
        ),
        llm=openai.LLM(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            # qwen/qwen3.8-27b on Groq defaults to "thinking" mode and, with no cap,
            # can run its completion out to the model's 16384-token ceiling — either
            # one blows well past this account's 1000 output-tokens-per-minute limit
            # in a single turn. max_completion_tokens=300 caps a single reply
            # comfortably above a ~25-word spoken turn (PRD persona) while leaving
            # OTPM headroom for more than one turn landing in the same rolling minute.
            #
            # reasoning_effort: was "none" (fully non-thinking); the M1 test log showed
            # several real-session turns (short/ambiguous filler inputs like "Okay." or
            # "Any questions you have for me?") collapse to a 1-2 word assistant reply
            # ("Is", "No") — a complete, non-truncated LLM output, not a token-cap cutoff.
            # An isolated single-turn repro at "none" did NOT reproduce the collapse, so
            # this isn't confirmed as the fix — but "low" produced strictly fuller, more
            # natural replies on every repro case tested, at a negligible latency cost
            # (30-155ms measured), so it's a low-risk mitigation pending the real re-test.
            reasoning_effort="low",
            max_completion_tokens=300,
            # --- OpenRouter (commented out; PRD's originally specified provider) ---
            # model=settings.openrouter_model,
            # api_key=settings.openrouter_api_key,
            # base_url="https://openrouter.ai/api/v1",
        ),
        tts=SarvamTTS(
            model="bulbul:v3",
            output_audio_codec="linear16",
            api_key=settings.sarvam_api_key,
            speaker="simran"
        ),
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
        # PRD calls for Silero VAD + turn detector for barge-in, local only — without
        # this, the framework opportunistically tries LiveKit Cloud's hosted
        # AdaptiveInterruptionDetector first (since interruption_detection is
        # otherwise unset and console/dev mode is active), which 401s against
        # agent-gateway.livekit.cloud (no Cloud credentials configured) before
        # falling back to VAD. Forcing mode="vad" skips that cloud attempt entirely.
        #
        # endpointing.max_delay: framework default is 2.5s, applied only when the
        # turn detector is unsure the user is actually done (end_of_turn_probability
        # below its per-language threshold) — the M1 test log's worst turn (2602ms
        # end_of_turn) hit exactly this path. Lowered to 1.2s to cut that tail;
        # min_delay (0.3s, the confident-EOU path) is left untouched. 1.2s is a
        # test value, not a final one — it sits at the outer edge of, not safely
        # past, the range where genuine trailing-off pauses commonly occur, so it
        # needs re-validation specifically on ambiguous/compound utterances, not
        # just simple ones.
        turn_handling={"interruption": {"mode": "vad"}, "endpointing": {"max_delay": 1.2}},
    )

    # Bilingual TTS: Sarvam's TTS plugin defaults target_language_code to "en-IN" and
    # never changes it on its own — the M1 test log showed replies coming back in
    # English even after a confidently-detected Hindi turn. STT's transcript.final
    # already carries the detected language per turn (surfaced here as
    # UserInputTranscribedEvent.language); follow it so TTS output language tracks
    # what the customer is actually speaking. Only final transcripts are trusted —
    # partials can flip language mid-utterance before STT settles.
    def _on_user_input_transcribed(ev: UserInputTranscribedEvent) -> None:
        if ev.is_final and ev.language and session.tts is not None:
            session.tts.update_options(target_language_code=ev.language)

    session.on("user_input_transcribed", _on_user_input_transcribed)

    metrics = TurnMetrics()
    session.on("user_state_changed", metrics.on_user_state_changed)
    session.on("agent_state_changed", metrics.on_agent_state_changed)

    async def _report_metrics() -> None:
        metrics.report()

    ctx.add_shutdown_callback(_report_metrics)

    agent = Agent(instructions=f"{BASE_PERSONA}\n\n{LANGUAGE_MATCH_INSTRUCTION}")

    await session.start(agent=agent, room=ctx.room)
    logger.info("agent ready")


if __name__ == "__main__":
    settings = get_settings()
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            # M2: explicit dispatch. The API's POST /calls creates the room and calls
            # AgentDispatchService.create_dispatch(room, agent_name) to place this worker
            # in it. Setting agent_name DISABLES automatic dispatch, so a worker started
            # with `dev` no longer joins rooms on its own — it only takes assigned jobs,
            # which is why M2 runs it with `start`. `console` is unaffected: it simulates
            # a job locally rather than receiving a dispatch, so the M1 path is unchanged.
            agent_name="da-voice",
            ws_url=settings.livekit_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        )
    )
