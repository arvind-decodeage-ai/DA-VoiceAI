# M1 status — CLOSED (2026-09-15)

## Decision

M1 is **closed by deliberate risk acceptance**, not by meeting every PRD numeric threshold.
Two quantitative PRD thresholds remain unmet and are explicitly accepted as technical debt.

**M1 CLOSED — proceed to M2.** M2 proceeds exactly per the PRD; no M3 state-machine,
intent-routing, tool-calling or business-logic work is to be introduced as part of M2.

## Passed

Evidence: `out/m1_runs/acceptance-20260915-094659.{log,jsonl,summary.txt}` (verified).

- **11 consecutive Hindi/English voice turns** via `console` — 11 user turns, 11 assistant
  replies, 11 speech handles, **zero dropped turns**, zero session errors, zero swallowed
  exceptions on the speech handles.
- **Bilingual correctness** — `en-IN` and `hi-IN` both detected in one session; Hindi turns
  answered in Hindi text, English in English. (The earlier failure mode — English text read
  aloud by a `hi-IN` voice — did not recur.)
- **Barge-in works** — 4 interruptions, 3 interrupted speech handles. The remaining issue is
  cancellation *latency*, not absence of interruption handling.
- **No mocks** — real Sarvam STT/TTS and a real LLM on every turn; no fixtures, no tools.
- **No audio overlap** — the console-sink overlap detectors fired zero times.

## Failed, with accepted rationale

### Barge-in latency — measured 448-575 ms, PRD target < 300 ms

Constrained by the framework's interruption gate: `_interrupt_by_audio_activity()` only fires
once VAD accumulates `interruption.min_duration` of speech, default **0.5 s**
(`voice/turn.py:193`, gate at `voice/agent_activity.py:2271`). Measured from user-speech onset,
a ~500 ms floor is therefore structural, which is why every sample across six runs clusters at
448-576 ms rather than scattering.

Classified as an architectural/optimization item, not a defect in core business logic.
**Decision:** defer VAD/interruption-window optimization to M2 or later polish.

### End-of-speech to first audio — measured median 2003 ms, PRD target < 1500 ms

Dominant contributor is Sarvam STT transcript finalization (`transcript_delay` 1.3-1.5 s per
turn) before the turn is considered complete; `llm_ttft` (~300-400 ms) and `tts_ttfb`
(~150-220 ms) are minor by comparison. Identified fix paths: prompt/KV caching, and tuning
Sarvam endpoint/interim-transcript parameters so useful partials can be acted on earlier.

**Decision:** defer until the state machine and business logic are structurally stable;
optimizing before M3 risks rework.

Measurement caveat for whoever revisits this: the figure is *optimistic*. `_pending_eos` is
overwritten when multiple end-of-speech events precede the audio (last one wins), and a
false-interruption resume can attribute resumed audio to an unrelated EOS.

## Approved deviations

- **LLM provider** — PRD specifies OpenRouter; implementation uses **Groq** with
  `qwen/qwen3.8-27b`. Manager-approved; not an M1 blocker.
- **STT language sentinel** — PRD's literal `language="unknown"` is not valid for this plugin;
  `"auto"` is the real auto-detect sentinel (confirmed from plugin source).

## Known technical debt (tracked, not dropped)

1. Reduce interruption/barge-in latency below the PRD's 300 ms target.
2. Reduce median end-of-speech to first-audio latency below the PRD's 1.5 s target.

Revisit after the state machine and business logic are stable.

## Minor observations (not blockers)

- The acceptance summary flags one `<= 2 word` assistant reply, `'जिरी, आपका'`. That is the
  turn with a deliberate barge-in, so it is a **truncated interrupted reply**, not the old
  1-2 word collapse bug. The collapse did not reproduce anywhere in the acceptance run.
- A **silently dropped turn** was observed once in an earlier diagnostic run (2026-09-11,
  `preempt-off-20260911-120325.log`): a validly committed user turn produced no reply, no
  error and no log line for 7 s. It did not reproduce in the acceptance run. The investigation
  narrowed it to three unbounded awaits with no timeout and no logging in
  `AgentActivity._user_turn_completed_task` (`await asyncio.wait({old_task})`,
  `await asyncio.gather(*_interrupt_background_speeches())`, and
  `await self._cancel_speech_pause(...)` whose inner `_wait_for_generation()` has no watchdog,
  unlike the sibling `interrupt()` path which has a 5 s one). `tools/m1_harness.py` carries
  enter/exit stage probes that will name the blocking frame if it recurs.

## Current wiring (`agent/agent.py`)

- STT: Sarvam `STTRealtime`, `language="auto"`.
- LLM: `openai.LLM` against Groq (`https://api.groq.com/openai/v1`), `qwen/qwen3.8-27b`,
  `reasoning_effort="low"`, `max_completion_tokens=300`. OpenRouter path present but commented out.
- TTS: Sarvam `bulbul:v3`, `linear16`, speaker `"simran"`; `target_language_code` updated per
  turn from the STT-detected language via the `user_input_transcribed` hook.
- `turn_handling`: VAD-forced interruption mode + `endpointing.max_delay: 1.2`.
- Persona: `prompts/base_persona.md` (verbatim PRD §6) + `LANGUAGE_MATCH_INSTRUCTION` appended
  at `Agent()` construction, never merged into the file.

## Test harness

`tools/m1_harness.py` — diagnostic only, never modifies `agent/`. Runs the real entrypoint under
instrumentation: console-sink overlap detectors, verbatim conversation capture with spoken-at
timestamps, swallowed-error channels (session `error` events and `SpeechHandle.exception()`),
enter/exit stage probes, and HTTP-layer visibility for the LLM call. Artifacts land in
`out/m1_runs/<label>-<timestamp>.{log,jsonl,summary.txt}`; the summary is written incrementally
so it survives any exit path.

    .venv/bin/python tools/m1_harness.py --preemptive on --label <name>

## Gotchas (verified, still relevant for M2)

1. **`config.py`'s `env_file=".env"` resolves relative to CWD, not the file's location.** A bare
   `Settings()` probe run from `agent/` silently falls back to empty-string defaults. `agent.py`
   itself is fine because `load_dotenv()` walks up from its own file location; ad-hoc
   `python -c "from agent.config import Settings..."` checks must run from the repo root.
2. **Groq model availability changes over time on this account** — models discussed earlier in
   history may be deprecated; check before assuming.
3. **Rate-limit headers don't tell the whole story** — this Groq account has an undocumented
   ~1000 output-tokens-per-minute ceiling on preview models, which only surfaces as a real 429
   during use. This is why `max_completion_tokens=300` is set.
4. **`SARVAM_API_KEY`** (STT/TTS here) is a different key family from `SARVAM_CHAT_API_KEY` in
   the separate live-call/Exotel project. Don't conflate them.
5. **`.env` has a stale `LLM_FALLBACK_MODEL`** — an OpenRouter model id paired with a Groq base
   URL. Harmless today (nothing reads it); clean it up before any fallback logic is written.
