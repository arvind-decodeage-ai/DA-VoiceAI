/**
 * M3 T9: the call document viewer.
 *
 * PRD M3 requires the JSON be "written to ./out/ and viewable in UI". This is
 * the UI half; it reads the document from GET /calls/{id}/json, which serves it
 * from calls.result rather than the file.
 */

import { useEffect, useState } from "react";

import { fetchCallJson } from "./api";

type Status = "waiting" | "ready" | "missing" | "error";

export function CallJson({ callId }: { callId: string }) {
  const [status, setStatus] = useState<Status>("waiting");
  const [doc, setDoc] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setStatus("waiting");

    fetchCallJson(callId)
      .then((result) => {
        if (cancelled) return;
        if (result === null) {
          setStatus("missing");
        } else {
          setDoc(result);
          setStatus("ready");
        }
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
        setStatus("error");
      });

    return () => {
      cancelled = true;
    };
  }, [callId]);

  return (
    <section className="calljson">
      <h2>Call record</h2>
      <p className="call-id">{callId}</p>

      {status === "waiting" && <p className="muted">Waiting for the call record…</p>}
      {status === "missing" && (
        <p className="muted">
          No record yet. The agent writes it as the call shuts down — if it never
          appears, the call did not reach the end.
        </p>
      )}
      {status === "error" && <p className="error">{error}</p>}
      {status === "ready" && <pre className="json">{JSON.stringify(doc, null, 2)}</pre>}
    </section>
  );
}
