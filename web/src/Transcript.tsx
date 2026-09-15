/**
 * M2 T5: live transcript.
 *
 * `useTranscriptions()` returns one entry per text stream, with the text growing
 * as the stream fills. The agent publishes these automatically —
 * `RoomOutputOptions.transcription_enabled` defaults to true, so no agent-side
 * work was needed. Speaker is decided by participant identity: the API mints the
 * caller's token with identity "caller", and the worker joins as "agent-<job id>".
 */

import { useEffect, useRef } from "react";
import { useTranscriptions } from "@livekit/components-react";

const CALLER_IDENTITY = "caller";

export function Transcript() {
  const transcriptions = useTranscriptions();
  const endRef = useRef<HTMLDivElement>(null);

  // Keep the newest line in view as the conversation grows.
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [transcriptions]);

  const lines = [...transcriptions].sort(
    (a, b) => a.streamInfo.timestamp - b.streamInfo.timestamp,
  );

  if (lines.length === 0) {
    return (
      <div className="transcript empty">
        <p>Say something — the transcript appears here.</p>
      </div>
    );
  }

  return (
    <div className="transcript">
      {lines.map((line) => {
        const isCaller = line.participantInfo.identity === CALLER_IDENTITY;
        return (
          <p key={line.streamInfo.id} className={isCaller ? "line caller" : "line agent"}>
            <span className="who">{isCaller ? "You" : "Agent"}</span>
            <span className="what">{line.text}</span>
          </p>
        );
      })}
      <div ref={endRef} />
    </div>
  );
}
