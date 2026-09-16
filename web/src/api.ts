/** Thin client for the da-voice API (FastAPI, :8000). */

const API_BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export type CallCredentials = {
  call_id: string;
  room: string;
  url: string;
  token: string;
};

/** Create a room, dispatch the agent into it, and get the caller's token. */
export async function createCall(): Promise<CallCredentials> {
  const res = await fetch(`${API_BASE}/calls`, { method: "POST" });
  if (!res.ok) {
    throw new Error(`POST /calls failed: ${res.status} ${await res.text()}`);
  }
  return res.json();
}

/** Delete the room. Wired up in T6; idempotent server-side. */
export async function endCall(callId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/calls/${callId}`, { method: "DELETE" });
  if (!res.ok) {
    throw new Error(`DELETE /calls/${callId} failed: ${res.status}`);
  }
}

/** Fetch the PRD §8 document for a finished call.
 *
 * Persistence runs during the agent's shutdown, which lands a moment after the
 * room closes, so the document is not there the instant End is pressed. Retry a
 * few times before giving up; a 404 here means "not ready yet", not "gone".
 */
export async function fetchCallJson(
  callId: string,
  { attempts = 6, delayMs = 1000 }: { attempts?: number; delayMs?: number } = {},
): Promise<unknown | null> {
  for (let i = 0; i < attempts; i++) {
    const res = await fetch(`${API_BASE}/calls/${callId}/json`);
    if (res.ok) return res.json();
    if (res.status !== 404) {
      throw new Error(`GET /calls/${callId}/json failed: ${res.status}`);
    }
    await new Promise((r) => setTimeout(r, delayMs));
  }
  return null;
}
