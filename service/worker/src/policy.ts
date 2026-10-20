/** Limits that match service/nc_perform. The container checks the plan again. */

export const CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz";

const LINE = /\b(melod(?:y|ic|ies)|motif|leitmotif|a line|the line|lead line|sing(?:ing)?|sung)\b/i;
const TRIPLET = /\btriplets?\b/i;
const SECTIONS = /\b(sections?|verses?|choruses?|bridges?|intros?|codas?|part [a-d]|movement)\b/i;

export function needsLlm(text: string): boolean {
  return LINE.test(text) || TRIPLET.test(text) || SECTIONS.test(text);
}

export function newId(prefix: "h" | "k" | "g" = "h"): string {
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

export const DAY_S = 86400;

export type RateBlock = { error: string; retry_after_s: number; retry_at: string };

export type RateDecision = {
  error: string | null;
  retry_after_s: number | null;
  retry_at: string | null;
  hits: number[];
};

/** UTC clock for the fallback sentence. The page should format retry_at locally. */
export function clockUtc(unix: number): string {
  const shown = new Date(Math.ceil(unix) * 1000);
  if (shown.getUTCSeconds() !== 0 || shown.getUTCMilliseconds() !== 0) {
    shown.setUTCSeconds(0, 0);
    shown.setUTCMinutes(shown.getUTCMinutes() + 1);
  }
  let hour = shown.getUTCHours();
  const minute = String(shown.getUTCMinutes()).padStart(2, "0");
  const ampm = hour < 12 ? "AM" : "PM";
  hour = hour % 12 || 12;
  return `${hour}:${minute} ${ampm} UTC`;
}

export function retryAtIso(unix: number): string {
  return new Date(unix * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function windowMessage(limit: number, when: string): string {
  return `Too many Performs from this network (${limit} every 10 minutes). Try again after ${when}.`;
}

export function dayMessage(limit: number, when: string): string {
  return `Daily limit reached (${limit} Performs from this network). Try again after ${when}.`;
}

export function burstBlock(now: number): RateBlock {
  return { error: BURST_MESSAGE, retry_after_s: BURST_PERIOD_S, retry_at: retryAtIso(now + BURST_PERIOD_S) };
}

/**
 * True when X-NC-Tester matches NC_TESTER_TOKEN. An empty secret never matches.
 * Both sides are hashed first so the compare does not stop on length. The token
 * is not returned.
 */
export async function testerPass(header: string | null | undefined, secret: string | null | undefined): Promise<boolean> {
  const token = (secret || "").trim();
  const got = (header || "").trim();
  if (!token || !got) return false;
  const encode = new TextEncoder();
  const a = new Uint8Array(await crypto.subtle.digest("SHA-256", encode.encode(got)));
  const b = new Uint8Array(await crypto.subtle.digest("SHA-256", encode.encode(token)));
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
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

/**
 * One Perform against the rolling 10-minute and 24-hour windows.
 * Both caps are checked before a hit is added, so a rejection does not
 * consume either window. `now` and `hits` are unix seconds.
 * The next free slot is the oldest hit in that window plus the window length.
 */
export function applyPerformHit(
  now: number,
  hits: readonly number[] | null,
  per10min = PER_10MIN,
  perDay = PER_DAY,
): RateDecision {
  const kept = (hits || []).filter((ts) => ts >= now - DAY_S).sort((a, b) => a - b);
  const win = kept.filter((ts) => ts >= now - WINDOW_S);
  if (win.length >= per10min) {
    return blocked(now, win[0] + WINDOW_S, windowMessage(per10min, clockUtc(win[0] + WINDOW_S)), kept);
  }
  if (kept.length >= perDay) {
    return blocked(now, kept[0] + DAY_S, dayMessage(perDay, clockUtc(kept[0] + DAY_S)), kept);
  }
  return { error: null, retry_after_s: null, retry_at: null, hits: [...kept, now] };
}

function blocked(now: number, freeAt: number, error: string, hits: number[]): RateDecision {
  const at = Math.max(freeAt, now);
  return {
    error,
    retry_after_s: Math.max(1, Math.ceil(at - now)),
    retry_at: retryAtIso(at),
    hits,
  };
}

/** Short lines the Create page shows. A search replaces the stage line with "Finding …". */
export const STAGE_LINES: Record<string, string> = {
  queued: "Queued",
  gathering: "Gathering the sounds…",
  gathered: "Gathered.",
  planning: "Reading the notes…",
  fetching: "Fetching the recordings…",
  measuring: "Checking each note's pitch…",
  rendering: "Playing the chords…",
  reverb: "Adding the room…",
  mastering: "Setting the loudness…",
  verifying: "Checking the record…",
  storing: "Storing the Performance…",
  done: "Done",
  failed: "Something went wrong.",
  rejected: "This Performance was refused.",
  cancelled: "Stopped.",
};

export type FoundSound = { title: string; supplier: string; license: string };

export function foundSounds(rows: unknown): FoundSound[] {
  if (!Array.isArray(rows)) return [];
  const out: FoundSound[] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const item = row as Record<string, unknown>;
    const title = String(item.title || "").trim();
    const supplier = String(item.supplier || "").trim();
    const license = String(item.license || "").trim();
    if (!title || !supplier || !license) continue;
    const key = `${title}\0${supplier}\0${license}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ title, supplier, license });
  }
  return out;
}

export function jobStatusFields(input: {
  stage?: string | null;
  position?: number | null;
  seed?: unknown;
  trimmed?: unknown;
  found_sounds?: unknown;
  stage_line?: unknown;
  tester?: unknown;
}): { seed: number | null; trimmed: string | null; found_sounds: FoundSound[]; stage_line: string; tester: boolean } {
  let seed: number | null = null;
  if (typeof input.seed === "number" && Number.isFinite(input.seed)) seed = input.seed;
  else if (typeof input.seed === "string" && input.seed.trim() && Number.isFinite(Number(input.seed))) seed = Number(input.seed);
  const trimmed = typeof input.trimmed === "string" && input.trimmed.trim() ? input.trimmed.trim() : null;
  const stage = input.stage || "";
  let line = typeof input.stage_line === "string" && input.stage_line.trim() ? input.stage_line.trim() : STAGE_LINES[stage] || stage || STAGE_LINES.planning;
  if (stage === "queued" && input.position) line = `Queued (position ${input.position})`;
  return { seed, trimmed, found_sounds: foundSounds(input.found_sounds), stage_line: line, tester: input.tester === true };
}
