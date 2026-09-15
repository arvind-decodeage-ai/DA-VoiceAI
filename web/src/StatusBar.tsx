/**
 * M2 T5: status badge + call timer.
 *
 * The badge shows the agent's own state, which the worker publishes as the
 * `lk.agent.state` participant attribute on every state change (verified live in
 * T3). Conversation stages — Greet / Router / Resolve / Wrap — do not exist until
 * M3; this component is the place they will surface when they do.
 */

import { useEffect, useState } from "react";
import { useConnectionState, useVoiceAssistant } from "@livekit/components-react";

function formatElapsed(seconds: number): string {
  const mm = Math.floor(seconds / 60)
    .toString()
    .padStart(2, "0");
  const ss = Math.floor(seconds % 60)
    .toString()
    .padStart(2, "0");
  return `${mm}:${ss}`;
}

export function StatusBar({ startedAt }: { startedAt: number }) {
  const { state } = useVoiceAssistant();
  const connection = useConnectionState();
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const tick = () => setElapsed((Date.now() - startedAt) / 1000);
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [startedAt]);

  // `state` is undefined until the agent participant joins and publishes its
  // first attribute — a second or two after connect.
  const agentState = state ?? "connecting";

  return (
    <div className="statusbar">
      <span className={`dot ${connection}`} aria-hidden="true" />
      <span className="connection">{connection}</span>
      <span className={`badge state-${agentState}`}>{agentState}</span>
      <span className="timer" aria-label="call duration">
        {formatElapsed(elapsed)}
      </span>
    </div>
  );
}
