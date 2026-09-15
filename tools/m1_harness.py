"""M1 diagnostic harness — runs the existing console agent under instrumentation.

Purely diagnostic. It does NOT change M1 behaviour: `agent/agent.py` is imported and
its `entrypoint` is run unmodified. The only runtime patch is the optional
`--preemptive off` toggle, which exists solely to A/B the framework default.

Covers exactly three things:

  1. Overlap detectors — surfaces every occurrence of the framework's own
     "capture_frame called while previous flush is in progress" and
     "flush called while previous flush is in progress" (livekit/agents/cli/_legacy.py).
     Each hit prints a banner the moment it fires and is repeated in the end summary.
  2. Hindi reply + filler-collapse repro — records every conversation item verbatim
     (plus per-turn detected STT language) to a JSONL file, and flags any assistant
     reply of <= 2 words.
  3. preemptive_generation A/B — `--preemptive on|off` toggles the framework default
     so the same filler sequence can be run both ways.

  4. dropped-turn discriminator (H1/H2/H3) — makes the LLM call visible at the HTTP
     layer and surfaces the two error channels the framework swallows in silence:
       * openai/httpx/httpcore lifted to DEBUG *in the log file only*, so a Groq
         request that is sent-but-unanswered is distinguishable from one never sent.
       * session "error" events (agent_session._on_error returns without logging for
         the first `max_unrecoverable_errors` LLM/STT/TTS errors).
       * SpeechHandle.exception(), which the framework stores and never surfaces
         outside session.run()/RunResult.
     Read the verdict table in the summary: no POST => gate (H3); POST with no
     response => Groq stalled (H1); POST with an error => swallowed exception (H2).

Usage (from the repo root):

    .venv/bin/python tools/m1_harness.py --preemptive on  --label run1
    .venv/bin/python tools/m1_harness.py --preemptive off --label run2

Artifacts land in out/m1_runs/<label>-<timestamp>.{log,jsonl,summary.txt}
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- detectors

DETECTOR_PATTERNS = {
    "capture_frame_during_flush": "capture_frame called while previous flush is in progress",
    "flush_during_flush": "flush called while previous flush is in progress",
}

DETECTOR_HITS: list[dict[str, object]] = []
CONVERSATION: list[dict[str, object]] = []
TRANSCRIPTS: list[dict[str, object]] = []
AGENT_STATES: list[dict[str, object]] = []
ERRORS: list[dict[str, object]] = []
SPEECH_HANDLES: list[dict[str, object]] = []

# loggers that answer the H1/H2/H3 question; DEBUG in the file, hidden from the console
HTTP_LOGGERS = ("openai", "httpx", "httpcore")


class _FileOnlyFilter(logging.Filter):
    """Keeps the HTTP-layer chatter out of the operator's console; the file still gets it."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.split(".")[0] in HTTP_LOGGERS


class _DetectorFilter(logging.Filter):
    """Never filters anything out — it only watches the stream for the two markers."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for name, pattern in DETECTOR_PATTERNS.items():
            if pattern in msg:
                hit = {
                    "detector": name,
                    "ts": time.time(),
                    "wall": datetime.now().isoformat(timespec="milliseconds"),
                    "logger": record.name,
                    "message": msg,
                }
                DETECTOR_HITS.append(hit)
                _refresh_summary()
                sys.stderr.write(
                    f"\n\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
                    f"!! OVERLAP DETECTOR FIRED: {name}\n"
                    f"!! {hit['wall']}  {record.name}\n"
                    f"!! {msg}\n"
                    f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n\n"
                )
                sys.stderr.flush()
        return True


# ---------------------------------------------------------------- session hooks


def _install_session_hooks() -> None:
    """Attach read-only listeners to every AgentSession, without touching agent.py.

    DIAGNOSTIC-ONLY / READ-ONLY. Every listener below observes and records. None of
    them mutate session state, consume an event, suppress an exception, or change any
    control flow: `session.on(...)` registers an additional listener alongside the
    framework's own, and the SpeechHandle done-callback only reads `.exception()`.
    Removing this whole function changes what we can SEE, never what the agent DOES.
    """
    from livekit.agents import AgentSession

    orig_init = AgentSession.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        orig_init(self, *args, **kwargs)

        def _on_item(ev) -> None:  # ConversationItemAddedEvent
            item = ev.item
            role = getattr(item, "role", None)
            text = getattr(item, "text_content", None)
            raw = getattr(item, "raw_text_content", None)
            entry = {
                "kind": "conversation_item",
                "wall": datetime.now().isoformat(timespec="milliseconds"),
                "role": role,
                "text": text,
                "raw_text": raw,
                "word_count": len((text or "").split()),
                # assistant items are committed to the chat context when the speech
                # finishes (and for interrupted speech, only when the NEXT turn commits),
                # so "wall" can lag the audio by seconds. This is the moment the agent
                # actually started speaking, taken from the last agent_state->speaking.
                "spoken_at": _last_speaking_start() if role == "assistant" else None,
            }
            CONVERSATION.append(entry)
            _append_jsonl(entry)

        def _on_transcript(ev) -> None:  # UserInputTranscribedEvent
            if not getattr(ev, "is_final", False):
                return
            entry = {
                "kind": "user_transcript_final",
                "wall": datetime.now().isoformat(timespec="milliseconds"),
                "language": getattr(ev, "language", None),
                "text": getattr(ev, "transcript", None),
            }
            TRANSCRIPTS.append(entry)
            _append_jsonl(entry)

        def _on_agent_state(ev) -> None:  # AgentStateChangedEvent
            entry = {
                "kind": "agent_state",
                "wall": datetime.now().isoformat(timespec="milliseconds"),
                "old_state": getattr(ev, "old_state", None),
                "new_state": getattr(ev, "new_state", None),
            }
            AGENT_STATES.append(entry)
            _append_jsonl(entry)

        def _on_error(ev) -> None:  # ErrorEvent
            # DIAGNOSTIC-ONLY. AgentSession._on_error returns early -- with no log line
            # at all -- for the first `max_unrecoverable_errors` LLM/STT/TTS errors.
            # This listener is a second, independent subscriber: it records the event
            # and does nothing else. The framework's own handling is untouched.
            err = getattr(ev, "error", None)
            entry = {
                "kind": "session_error",
                "wall": datetime.now().isoformat(timespec="milliseconds"),
                "error_type": type(err).__name__,
                "error": repr(err)[:600],
                "recoverable": getattr(err, "recoverable", None),
                "source": type(getattr(ev, "source", None)).__name__,
            }
            ERRORS.append(entry)
            _append_jsonl(entry)
            sys.stderr.write(f"\n!! SESSION ERROR (recorded, not handled): {entry['error']}\n")
            sys.stderr.flush()

        def _on_speech_created(ev) -> None:  # SpeechCreatedEvent
            handle = getattr(ev, "speech_handle", None)
            if handle is None:
                return
            created = datetime.now().isoformat(timespec="milliseconds")
            source = getattr(ev, "source", None)

            def _done(h) -> None:
                # DIAGNOSTIC-ONLY. `.exception()` is a plain getter over a value the
                # framework already stored; reading it neither clears nor re-raises it.
                try:
                    err = h.exception()
                except BaseException:
                    err = None
                entry = {
                    "kind": "speech_handle_done",
                    "wall": datetime.now().isoformat(timespec="milliseconds"),
                    "created_at": created,
                    "source": source,
                    "interrupted": bool(getattr(h, "interrupted", False)),
                    "error_type": type(err).__name__ if err is not None else None,
                    "error": repr(err)[:600] if err is not None else None,
                }
                SPEECH_HANDLES.append(entry)
                _append_jsonl(entry)
                if err is not None:
                    sys.stderr.write(
                        f"\n!! SPEECH HANDLE CARRIED A SWALLOWED ERROR: {entry['error']}\n"
                    )
                    sys.stderr.flush()

            handle.add_done_callback(_done)

        self.on("conversation_item_added", _on_item)
        self.on("user_input_transcribed", _on_transcript)
        self.on("agent_state_changed", _on_agent_state)
        self.on("error", _on_error)
        self.on("speech_created", _on_speech_created)

    AgentSession.__init__ = patched_init  # type: ignore[method-assign]


# ---------------------------------------------------------------- stage probes

PROBE_LOG = logging.getLogger("m1.probe")
STAGES: list[dict[str, object]] = []
_STAGE_SEQ = [0]


def _stage(event: str, name: str, seq: int, detail: str = "") -> None:
    entry = {
        "kind": "stage",
        "wall": datetime.now().isoformat(timespec="milliseconds"),
        "event": event,
        "stage": name,
        "seq": seq,
        "detail": detail,
    }
    STAGES.append(entry)
    _append_jsonl(entry)
    PROBE_LOG.info("%-6s %s#%d %s", event, name, seq, detail)


def _wrap_async(cls, attr: str, watchdog_after: float = 2.0):  # type: ignore[no-untyped-def]
    orig = getattr(cls, attr)

    async def wrapper(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        _STAGE_SEQ[0] += 1
        seq = _STAGE_SEQ[0]
        _stage("ENTER", attr, seq)
        done = False

        async def _watchdog() -> None:
            waited = 0.0
            while not done:
                await asyncio.sleep(watchdog_after)
                if done:
                    return
                waited += watchdog_after
                PROBE_LOG.warning(
                    "STILL INSIDE %s#%d after %.1fs — this frame is blocking the turn",
                    attr,
                    seq,
                    waited,
                )

        wd = asyncio.create_task(_watchdog())
        try:
            return await orig(self, *args, **kwargs)
        except BaseException as e:  # observed, re-raised unchanged
            _stage("RAISE", attr, seq, f"{type(e).__name__}: {e}")
            raise
        finally:
            done = True
            wd.cancel()
            _stage("EXIT", attr, seq)

    setattr(cls, attr, wrapper)

def _wrap_sync(cls, attr: str):  # type: ignore[no-untyped-def]
    orig = getattr(cls, attr)

    def wrapper(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        _STAGE_SEQ[0] += 1
        seq = _STAGE_SEQ[0]
        _stage("ENTER", attr, seq)
        try:
            return orig(self, *args, **kwargs)
        except BaseException as e:
            _stage("RAISE", attr, seq, f"{type(e).__name__}: {e}")
            raise
        finally:
            _stage("EXIT", attr, seq)

    setattr(cls, attr, wrapper)


def _install_stage_probes(watchdog_after: float = 2.0) -> None:
    """Enter/exit tracing for the three frames between turn-commit and LLM dispatch.

    DIAGNOSTIC-ONLY / READ-ONLY. Each wrapper logs, delegates to the original method
    with the original arguments, and returns its result unchanged. Exceptions are
    re-raised untouched (logged on the way past, never swallowed). A watchdog task
    reports a frame that has been entered but not exited -- it only observes; it never
    cancels, times out, or otherwise interferes with the awaited work.

    A silent hang shows up as an ENTER with no matching EXIT, naming the frame that
    blocked. Together with the source, that discriminates the three unbounded awaits
    in _user_turn_completed_task:
      * no ENTER of _cancel_speech_pause and no ENTER of _generate_reply
            -> blocked on `await asyncio.wait({old_task})` or on
               `await asyncio.gather(*_interrupt_background_speeches())`
      * ENTER _cancel_speech_pause with no EXIT
            -> blocked there (suspect: the unbounded _wait_for_generation inside it)
      * ENTER/EXIT both, but no _generate_reply
            -> an early return between them
      * ENTER _generate_reply -> the reply task exists; the fault is downstream
    """
    from livekit.agents.voice.agent_activity import AgentActivity
    from livekit.agents.voice.speech_handle import SpeechHandle

    _wrap_async(AgentActivity, "_user_turn_completed_task", watchdog_after)
    _wrap_async(AgentActivity, "_cancel_speech_pause", watchdog_after)
    _wrap_sync(AgentActivity, "_generate_reply")
    # the prime suspect await inside _cancel_speech_pause
    _wrap_async(SpeechHandle, "_wait_for_generation", watchdog_after)


def _last_speaking_start() -> str | None:
    """Wall time of the most recent agent_state -> speaking transition, if any."""
    for entry in reversed(AGENT_STATES):
        if entry.get("new_state") == "speaking":
            return str(entry["wall"])
    return None


# ---------------------------------------------------------------- artifacts

_JSONL_PATH: Path | None = None


def _append_jsonl(entry: dict[str, object]) -> None:
    if _JSONL_PATH is None:
        return
    with _JSONL_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _refresh_summary()


_SUMMARY_CTX: dict[str, object] = {}


def _refresh_summary(quiet: bool = True) -> None:
    """Rewrite the summary from whatever has been recorded so far.

    Called after every recorded event, so the summary survives any exit path -- the
    console CLI installs its own SIGINT/SIGTERM handlers (cli/_legacy.py:1590) and
    exits without running atexit, which is why the first round of runs produced none.
    """
    if not _SUMMARY_CTX:
        return
    _write_summary(
        _SUMMARY_CTX["summary_path"],  # type: ignore[arg-type]
        bool(_SUMMARY_CTX["preemptive"]),
        _SUMMARY_CTX["log_path"],  # type: ignore[arg-type]
        _SUMMARY_CTX["jsonl_path"],  # type: ignore[arg-type]
        quiet=quiet,
    )


def _write_summary(
    path: Path, preemptive: bool, log_path: Path, jsonl_path: Path, quiet: bool = False
) -> None:
    lines: list[str] = []
    lines.append("M1 HARNESS SUMMARY")
    lines.append(f"  finished          : {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"  preemptive_generation enabled : {preemptive}")
    lines.append(f"  full log          : {log_path}")
    lines.append(f"  conversation jsonl: {jsonl_path}")
    lines.append("")

    lines.append("1. OVERLAP DETECTORS")
    if DETECTOR_HITS:
        lines.append(f"  *** {len(DETECTOR_HITS)} HIT(S) — ROOT CAUSE CONFIRMED ***")
        for h in DETECTOR_HITS:
            lines.append(f"    [{h['wall']}] {h['detector']}: {h['message']}")
    else:
        lines.append("  no hits — the console sink never saw two generations writing at once")
    lines.append("")

    langs = sorted({str(t.get("language")) for t in TRANSCRIPTS if t.get("language")})
    lines.append("2. CONVERSATION (verbatim)")
    lines.append(f"  detected STT languages this run: {', '.join(langs) or '(none)'}")
    collapses = [
        c for c in CONVERSATION if c.get("role") == "assistant" and int(c.get("word_count", 0)) <= 2
    ]
    if collapses:
        lines.append(f"  *** {len(collapses)} assistant reply/replies of <= 2 words (COLLAPSE) ***")
        for c in collapses:
            lines.append(f"    [{c['wall']}] {c['text']!r}")
    else:
        lines.append("  no assistant reply collapsed to <= 2 words")
    lines.append("")
    for c in CONVERSATION:
        if c["kind"] != "conversation_item":
            continue
        spoken = c.get("spoken_at")
        when = f"spoken {str(spoken)[11:]}" if spoken else f"committed {str(c['wall'])[11:]}"
        lines.append(
            f"  [{when}] {str(c['role']):<9} ({c['word_count']:>2}w) {c['text']!r}"
        )
    lines.append("")
    lines.append("  user transcripts (final, with detected language):")
    for t in TRANSCRIPTS:
        lines.append(f"    [{t['wall']}] lang={t['language']!r} {t['text']!r}")
    lines.append("")

    lines.append("3. DROPPED-TURN CHANNELS (diagnostic-only observation)")
    if ERRORS:
        lines.append(f"  *** {len(ERRORS)} session error event(s) — SWALLOWED BY THE FRAMEWORK ***")
        for e in ERRORS:
            lines.append(
                f"    [{e['wall']}] {e['source']} {e['error_type']} "
                f"recoverable={e['recoverable']}: {e['error']}"
            )
    else:
        lines.append("  no session error events")
    failed = [h for h in SPEECH_HANDLES if h.get("error")]
    if failed:
        lines.append(f"  *** {len(failed)} speech handle(s) carried a stored exception ***")
        for h in failed:
            lines.append(f"    [{h['wall']}] {h['error_type']}: {h['error']}")
    else:
        lines.append("  no speech handle carried a stored exception")
    interrupted = [h for h in SPEECH_HANDLES if h.get("interrupted")]
    lines.append(
        f"  speech handles: {len(SPEECH_HANDLES)} total, {len(interrupted)} interrupted"
    )
    lines.append("")
    lines.append("  agent state timeline:")
    for a in AGENT_STATES:
        lines.append(f"    [{a['wall']}] {a['old_state']} -> {a['new_state']}")
    lines.append("")
    open_stages: dict[int, dict[str, object]] = {}
    for st in STAGES:
        if st["event"] == "ENTER":
            open_stages[int(st["seq"])] = st  # type: ignore[arg-type]
        elif st["event"] == "EXIT":
            open_stages.pop(int(st["seq"]), None)  # type: ignore[arg-type]
    if open_stages:
        lines.append("  *** FRAMES ENTERED BUT NEVER EXITED — THESE BLOCKED A TURN ***")
        for st in open_stages.values():
            lines.append(f"    [{st['wall']}] {st['stage']}#{st['seq']}")
    else:
        lines.append("  stage probes: every frame entered was also exited (no hang observed)")
    lines.append("")
    lines.append("  stage trace:")
    for st in STAGES:
        detail = f" {st['detail']}" if st["detail"] else ""
        lines.append(f"    [{st['wall']}] {str(st['event']):<5} {st['stage']}#{st['seq']}{detail}")

    lines.append("")
    lines.append("  HOW TO READ A DROPPED TURN (a user turn with no reply after it):")
    lines.append("    a) stage trace above — an ENTER with no EXIT names the blocking frame:")
    lines.append("         no _cancel_speech_pause, no _generate_reply -> blocked on one of the")
    lines.append("           two unbounded awaits at the top of _user_turn_completed_task")
    lines.append("         _cancel_speech_pause entered, never exited  -> blocked there")
    lines.append("         _wait_for_generation entered, never exited  -> the unbounded await")
    lines.append("         _generate_reply entered -> a reply task exists; fault is downstream")
    lines.append("    b) then the HTTP layer, grep the .log for 'HTTP Request' / 'api.groq.com':")
    lines.append("         POST sent, no response    -> H1, provider stalled")
    lines.append("         POST sent, error response -> H2, swallowed exception")
    lines.append("         no POST, with a hung frame above -> H3, blocked before dispatch")
    lines.append("    NB: the LLM request is dispatched at agent_activity.py:3290, BEFORE the")
    lines.append("        authorization gates at :3413 — so a stuck gate still produces a POST.")

    text = "\n".join(lines)
    path.write_text(text, encoding="utf-8")
    if not quiet:
        print("\n\n" + text + f"\n\nsummary written to {path}\n")


# ---------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--preemptive",
        choices=["on", "off"],
        default="on",
        help="toggle the framework's preemptive_generation default (A/B for the collapse)",
    )
    ap.add_argument("--label", default="run", help="artifact filename prefix")
    ap.add_argument(
        "--mode", default="console", help="livekit cli mode to run (default: console)"
    )
    args = ap.parse_args()

    os.chdir(REPO)
    sys.path.insert(0, str(REPO / "agent"))

    from dotenv import load_dotenv

    load_dotenv(REPO / ".env")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = REPO / "out" / "m1_runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"{args.label}-{stamp}.log"
    jsonl_path = run_dir / f"{args.label}-{stamp}.jsonl"
    summary_path = run_dir / f"{args.label}-{stamp}.summary.txt"

    global _JSONL_PATH
    _JSONL_PATH = jsonl_path

    # --- file logging + detectors, installed before livekit's own setup_logging runs
    from livekit.agents.cli.log import JsonFormatter

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    file_handler.setLevel(logging.DEBUG)
    # NB: the detector must sit on the HANDLER, not on the root logger. A filter on a
    # logger only sees records logged directly on that logger, never ones propagated up
    # from child loggers -- and every record we care about comes from a child logger.
    file_handler.addFilter(_DetectorFilter())

    root = logging.getLogger()
    root.addHandler(file_handler)
    root.setLevel(logging.DEBUG)

    # the console CLI's _configure_logger() calls root.setLevel(...) and would raise our
    # floor back up; re-assert DEBUG and keep our handler attached after it runs.
    from livekit.agents.cli import _legacy

    orig_configure = _legacy._configure_logger

    def patched_configure(*a, **kw):  # type: ignore[no-untyped-def]
        orig_configure(*a, **kw)
        r = logging.getLogger()
        if file_handler not in r.handlers:
            r.addHandler(file_handler)
        r.setLevel(logging.DEBUG)
        # _silence_noisy_loggers() pins the "livekit" tree to WARN; lift it so the file
        # captures the plugin/framework detail (TTS ws sessions, endpointing, etc).
        for name in ("livekit", "livekit.agents", "livekit.plugins", "da-voice.agent"):
            logging.getLogger(name).setLevel(logging.DEBUG)
        # H1/H2/H3 discriminator: the openai SDK + its HTTP stack are pinned to WARNING
        # by _silence_noisy_loggers(), which is why the dropped turn left no trace of the
        # Groq call. Lift them so a sent-but-unanswered request is visible.
        for name in HTTP_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
        # ...but keep the operator's console readable: only the file handler takes DEBUG,
        # and the HTTP chatter never reaches the console at all.
        for h in r.handlers:
            if h is not file_handler:
                h.setLevel(logging.INFO)
                h.addFilter(_FileOnlyFilter())

    _legacy._configure_logger = patched_configure  # type: ignore[assignment]

    # --- preemptive_generation A/B (the only behavioural knob, and only when asked)
    from livekit.agents.voice import turn as turn_mod

    preemptive_enabled = args.preemptive == "on"
    if not preemptive_enabled:
        turn_mod._PREEMPTIVE_GENERATION_DEFAULTS["enabled"] = False
    effective = turn_mod._PREEMPTIVE_GENERATION_DEFAULTS["enabled"]

    _install_session_hooks()
    _install_stage_probes()

    # --- import the real M1 agent, unmodified
    import agent as m1_agent  # agent/agent.py

    from livekit.agents import WorkerOptions, cli

    _SUMMARY_CTX.update(
        summary_path=summary_path,
        preemptive=bool(effective),
        log_path=log_path,
        jsonl_path=jsonl_path,
    )
    _refresh_summary()  # write a valid (empty) summary immediately

    atexit.register(_refresh_summary, False)

    # The console CLI installs its OWN SIGINT/SIGTERM handlers via signal.signal()
    # (cli/_legacy.py:1590), which would replace anything we registered here. Wrap the
    # registration instead so our flush runs first and the CLI's handler still runs.
    _orig_signal = signal.signal

    def _chained_signal(sig, handler):  # type: ignore[no-untyped-def]
        if sig in (signal.SIGINT, signal.SIGTERM) and callable(handler):
            inner = handler

            def _wrapped(signum, frame):  # type: ignore[no-untyped-def]
                _refresh_summary(quiet=False)
                return inner(signum, frame)

            return _orig_signal(sig, _wrapped)
        return _orig_signal(sig, handler)

    signal.signal = _chained_signal  # type: ignore[assignment]

    print("=" * 78)
    print(f"M1 HARNESS   preemptive_generation={'ON' if effective else 'OFF'}   mode={args.mode}")
    print(f"  log     : {log_path}")
    print(f"  jsonl   : {jsonl_path}")
    print(f"  summary : {summary_path}  (written on exit)")
    print("-" * 78)
    print("SCRIPT FOR THIS RUN:")
    print("  1. English opener, e.g. 'Hi, I want to check my order status.'")
    print("  2. Two or three more real English turns, to build actual context.")
    print("  3. One Hindi turn, e.g. 'मेरा ऑर्डर कहाँ है?'  -> expect Hindi TEXT back.")
    print("  4. The filler that collapsed before: 'Any questions you have for me?'")
    print("  5. One more ordinary turn after it, to see whether a collapse persists.")
    print("  Ctrl+C to end the run and write the summary.")
    print("=" * 78)

    settings = m1_agent.get_settings()
    sys.argv = [sys.argv[0], args.mode]
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=m1_agent.entrypoint,
            ws_url=settings.livekit_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        )
    )


if __name__ == "__main__":
    main()
