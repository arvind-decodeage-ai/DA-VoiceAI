/**
 * M2: browser call UI.
 *
 * T4 — Start a call, connect to the room, two-way audio.
 * T5 — live transcript, agent-state badge, call timer.
 *
 * T6 — End tears the call down: the browser leaves the room first, then the
 *      frontend calls DELETE /calls/{id} so the room is deleted and the worker's
 *      job ends. State returns to Start call.
 * T9 — after the call ends, the §8 document is fetched and shown.
 */

import { useCallback, useRef, useState } from "react";
import { LiveKitRoom, RoomAudioRenderer } from "@livekit/components-react";

import { createCall, endCall, type CallCredentials } from "./api";
import { CallJson } from "./CallJson";
import { StatusBar } from "./StatusBar";
import { Transcript } from "./Transcript";

type Phase = "idle" | "connecting" | "in-call" | "ending";

export default function App() {
  const [phase, setPhase] = useState<Phase>("idle");
  const [creds, setCreds] = useState<CallCredentials | null>(null);
  const [startedAt, setStartedAt] = useState(0);
  const [error, setError] = useState<string | null>(null);
  // Kept after teardown so the finished call's document can be fetched; the
  // live `creds` are cleared as soon as the room closes.
  const [finishedCallId, setFinishedCallId] = useState<string | null>(null);

  // The disconnect handler fires from inside LiveKitRoom, where a state value
  // captured at render time may be stale, so the call id is mirrored in a ref.
  const callIdRef = useRef<string | null>(null);
  // onDisconnected can fire more than once (End click, then unmount). The room
  // teardown is idempotent server-side, but there is no reason to send it twice.
  const teardownRef = useRef(false);

  const start = useCallback(async () => {
    setError(null);
    setPhase("connecting");
    try {
      const next = await createCall();
      callIdRef.current = next.call_id;
      setFinishedCallId(null);
      teardownRef.current = false;
      setCreds(next);
      setStartedAt(Date.now());
      setPhase("in-call");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPhase("idle");
    }
  }, []);

  /** End click: stop the media first. Teardown follows in onDisconnected. */
  const requestEnd = useCallback(() => {
    setPhase("ending"); // flips LiveKitRoom's connect prop to false
  }, []);

  /**
   * Runs once the browser has actually left the room — so the order is always
   * disconnect, then DELETE, never the reverse. Also covers disconnects we did
   * not initiate (agent left, room deleted elsewhere, network drop), which is
   * why the DELETE is idempotent server-side.
   */
  const handleDisconnected = useCallback(async () => {
    const callId = callIdRef.current;
    callIdRef.current = null;
    setCreds(null);
    setPhase("idle");
    if (callId) setFinishedCallId(callId);

    if (!callId || teardownRef.current) return;
    teardownRef.current = true;
    try {
      await endCall(callId);
    } catch (e) {
      // The UI is already back at Start; surface it so an orphaned room is visible
      // rather than silent.
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  return (
    <main className="app">
      <header>
        <h1>Decode Age - Voice Support</h1>
        <p className="sub">Browser call via LiveKit</p>
      </header>

      {error && <p className="error">{error}</p>}

      {phase !== "in-call" && phase !== "ending" && (
        <button className="primary" onClick={start} disabled={phase === "connecting"}>
          {phase === "connecting" ? "Connecting…" : "Start call"}
        </button>
      )}

      {(phase === "in-call" || phase === "ending") && creds && (
        <LiveKitRoom
          serverUrl={creds.url}
          token={creds.token}
          connect={phase === "in-call"}
          audio={true}
          video={false}
          onDisconnected={handleDisconnected}
          onError={(e) => setError(e.message)}
        >
          {/* Plays the agent's audio track. Without this you hear nothing. */}
          <RoomAudioRenderer />
          <section className="call">
            <StatusBar startedAt={startedAt} />
            <Transcript />
            <p className="call-id">call {creds.call_id}</p>
            <button className="danger" onClick={requestEnd} disabled={phase === "ending"}>
              {phase === "ending" ? "Ending…" : "End call"}
            </button>
          </section>
        </LiveKitRoom>
      )}

      {phase === "idle" && finishedCallId && <CallJson callId={finishedCallId} />}
    </main>
  );
}
