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
