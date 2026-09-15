/**
 * M2: browser call UI.
 *
 * T4 — Start a call, connect to the room, two-way audio.
 * T5 — live transcript, agent-state badge, call timer.
 *
 * The End button still only disconnects the browser; wiring it to
 * DELETE /calls/{id} so the room is torn down is T6.
 */

import { useCallback, useState } from "react";
import { LiveKitRoom, RoomAudioRenderer } from "@livekit/components-react";

import { createCall, type CallCredentials } from "./api";
import { StatusBar } from "./StatusBar";
import { Transcript } from "./Transcript";

type Phase = "idle" | "connecting" | "in-call";

export default function App() {
  const [phase, setPhase] = useState<Phase>("idle");
  const [creds, setCreds] = useState<CallCredentials | null>(null);
  const [startedAt, setStartedAt] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const start = useCallback(async () => {
    setError(null);
    setPhase("connecting");
    try {
      setCreds(await createCall());
      setStartedAt(Date.now());
      setPhase("in-call");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPhase("idle");
    }
  }, []);

  const stop = useCallback(() => {
    // T6 will also call DELETE /calls/{id} here.
    setCreds(null);
    setPhase("idle");
  }, []);

  return (
    <main className="app">
      <header>
        <h1>Decode Age — Voice Support</h1>
        <p className="sub">M2 · browser call via LiveKit</p>
      </header>

      {error && <p className="error">{error}</p>}

      {phase !== "in-call" && (
        <button className="primary" onClick={start} disabled={phase === "connecting"}>
          {phase === "connecting" ? "Connecting…" : "Start call"}
        </button>
      )}

      {phase === "in-call" && creds && (
        <LiveKitRoom
          serverUrl={creds.url}
          token={creds.token}
          connect={true}
          audio={true}
          video={false}
          onDisconnected={stop}
          onError={(e) => setError(e.message)}
        >
          {/* Plays the agent's audio track. Without this you hear nothing. */}
          <RoomAudioRenderer />
          <section className="call">
            <StatusBar startedAt={startedAt} />
            <Transcript />
            <p className="call-id">call {creds.call_id}</p>
            <button className="danger" onClick={stop}>
              End call
            </button>
          </section>
        </LiveKitRoom>
      )}
    </main>
  );
}
