/** The claim_token from POST /api/perform. Keep sends that same secret. */
export const CANCELLED_MESSAGE = "Stopped.";

const TERMINAL = new Set(["done", "failed", "rejected"]);

/** claim_token, or keep_token when the page sends the Keep name for the same secret. */
export function cancelToken(body: unknown): string {
  if (!body || typeof body !== "object") return "";
  const rec = body as Record<string, unknown>;
  for (const key of ["claim_token", "keep_token"]) {
    const value = rec[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

/**
 * done, failed, and rejected stay as they are.
 * cancelled is stopped again (idempotent). Anything still queued or running stops.
 */
export function cancelAction(state: string): "noop" | "stop" {
  if (TERMINAL.has(state)) return "noop";
  return "stop";
}

/** A container error after Stop must not rewrite the job as failed. */
export function queueErrorIsCancel(state: string | undefined): boolean {
  return state === "cancelled";
}
