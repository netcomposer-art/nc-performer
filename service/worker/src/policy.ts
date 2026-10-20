/** Limits that match service/nc_perform. The container checks the plan again. */

export const CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz";

const LINE = /\b(melod(?:y|ic|ies)|motif|leitmotif|a line|the line|lead line|sing(?:ing)?|sung)\b/i;
const TRIPLET = /\btriplets?\b/i;
const SECTIONS = /\b(sections?|verses?|choruses?|bridges?|intros?|codas?|part [a-d]|movement)\b/i;

export function needsLlm(text: string): boolean {
  return LINE.test(text) || TRIPLET.test(text) || SECTIONS.test(text);
}

export function newId(prefix: "h" | "k" = "h"): string {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  let n = 0n;
  for (const b of bytes) n = (n << 8n) | BigInt(b);
  n &= (1n << 60n) - 1n;
  let out = "";
  for (let i = 0; i < 12; i++) {
    out = CROCKFORD[Number(n & 31n)] + out;
    n >>= 5n;
  }
  return prefix + "-" + out;
}

export function claimToken(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export type LimitSettings = {
  maxWords: number;
  maxPlanBytes: number;
  maxLengthS: number;
  maxSources: number;
  maxChords: number;
  maxLayers: number;
};

export const DEFAULT_LIMITS: LimitSettings = {
  maxWords: 4000,
  maxPlanBytes: 64 * 1024,
  maxLengthS: 180,
  maxSources: 24,
  maxChords: 40,
  maxLayers: 16,
};

export function rejectPlan(plan: unknown, limits: LimitSettings = DEFAULT_LIMITS): string | null {
  if (!plan || typeof plan !== "object") return "plan must be a JSON object.";
  const raw = JSON.stringify(plan);
  if (raw.length > limits.maxPlanBytes) return `The plan is ${raw.length} bytes. The limit is ${limits.maxPlanBytes}.`;
  const p = plan as Record<string, unknown>;
  const sources = (p.sources || {}) as Record<string, { kind?: string }>;
  const nSources = Object.keys(sources).length;
  if (nSources > limits.maxSources) return `A Performance can use at most ${limits.maxSources} sources.`;
  const chords = Array.isArray(p.chords) ? p.chords.length : 0;
  if (chords > limits.maxChords) return `A Performance can have at most ${limits.maxChords} chords.`;
  const layers = Array.isArray(p.layers) ? p.layers.length : 0;
  if (layers > limits.maxLayers) return `A Performance can have at most ${limits.maxLayers} layers.`;
  const length = Number(p.length_s || 0) + Number(p.tail_s || 0);
  if (length > limits.maxLengthS) return `The music plus its tail is ${length}s. The limit is ${limits.maxLengthS}s.`;
  for (const [sid, src] of Object.entries(sources)) {
    if (src?.kind === "url") return `Source ${sid} is kind=url. Hosted Perform does not fetch arbitrary URLs.`;
  }
  return null;
}

export function wordsTooLong(text: string, max = DEFAULT_LIMITS.maxWords): string | null {
  if (text.length > max) return `Those words are ${text.length} characters. The limit is ${max}.`;
  return null;
}

/**
 * Product limits: 3 Performs per 10 minutes and 10 per day, per network.
 * Cloudflare's rate-limit binding cannot express either window. Wrangler only
 * accepts a period of 10 or 60 seconds, so the binding is a short burst guard
 * and these counters live in BudgetDO (and in the Python store for self-host).
 */
export const PER_10MIN = 3;
export const WINDOW_S = 600;
export const PER_DAY = 10;

/** Edge burst guard. `period` in wrangler.jsonc must stay 10 or 60. */
export const BURST_LIMIT = 3;
export const BURST_PERIOD_S = 10;

export const BURST_MESSAGE =
  "Too many Performs from this network in a few seconds. Wait a moment and try again.";

export function windowMessage(limit: number): string {
  return `Too many Performs from this network (${limit} every 10 minutes). Try again in a few minutes.`;
}

export function dayMessage(limit: number): string {
  return `Daily limit reached (${limit} Performs from this network). It resets tomorrow.`;
}

/** Shown for Worker faults. The real error is logged; the id is how to find it. */
export function publicInternalError(id: string | null): string {
  const sentence = "Something went wrong while performing. Please try again.";
  return id ? `${sentence} (${id})` : sentence;
}

/**
 * ContainerProxy matches hosts exactly, or with `*` globs. `musicmatter.art`
 * does not match `audio.musicmatter.art`. Add the wildcard beside each exact name.
 * A name that is already a glob is kept as written.
 */
export function proxyHosts(hosts: readonly string[]): string[] {
  const out = new Set<string>();
  for (const raw of hosts) {
    const h = raw.trim().toLowerCase().replace(/\.$/, "");
    if (!h || h.includes("://") || h.includes("/") || h.includes(":")) continue;
    out.add(h);
    if (!h.includes("*")) out.add(`*.${h}`);
  }
  return [...out];
}

/** True when the text is an internal fault. License and schema rejections stay visible. */
export function isInternalFailure(message: string): boolean {
  return /HTTP Error [45]\d\d\b/.test(message)
    || /ContainerProxy|Origin is disallowed|Network connection lost|container did not finish|container rejected|container HTTP \d{3}/.test(message)
    || /SSLCertVerificationError|CERTIFICATE_VERIFY_FAILED|Permission denied|\[Errno \d+\]/.test(message)
    || message.includes("Traceback");
}

/** Swap an internal fault for the public sentence. Leave a rejection message as it is. */
export function forPage(id: string | null, error: string | null): string | null {
  if (!error) return null;
  return isInternalFailure(error) ? publicInternalError(id) : error;
}

export type Counter = { start: number; n: number };
export type DayCounter = { day: string; n: number };

/**
 * One Perform against the 10-minute and daily windows.
 * Both caps are checked before either count increases, so a rejection
 * does not consume the other window. `now` is unix seconds.
 * The 10-minute window is fixed from its first hit and lasts WINDOW_S.
 */
export function applyPerformHit(
  now: number,
  day: string,
  window: Counter | null,
  dayHit: DayCounter | null,
  per10min = PER_10MIN,
  perDay = PER_DAY,
): { error: string | null; window: Counter; dayHit: DayCounter } {
  const open = window != null && now - window.start < WINDOW_S;
  const start = open ? window.start : now;
  const winN = open ? window.n : 0;
  const dayN = dayHit != null && dayHit.day === day ? dayHit.n : 0;
  if (winN >= per10min) {
    return { error: windowMessage(per10min), window: { start, n: winN }, dayHit: { day, n: dayN } };
  }
  if (dayN >= perDay) {
    return { error: dayMessage(perDay), window: { start, n: winN }, dayHit: { day, n: dayN } };
  }
  return {
    error: null,
    window: { start, n: winN + 1 },
    dayHit: { day, n: dayN + 1 },
  };
}
