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
  // Raw assembly is one file per chord. A stack may hold several note files.
  // The layered cap stays 24. The 64 KiB plan cap still applies above.
  const sourceCap = p.assembly === "raw" ? Math.max(limits.maxSources, limits.maxChords * 8) : limits.maxSources;
  if (nSources > sourceCap) return `A Performance can use at most ${sourceCap} sources.`;
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
 * The sentence a failed Save may show. A container rejection stays.
 * A traceback, a proxy fault, or a secret becomes the public sentence.
 * The caller logs the raw exception.
 */
export function failureForPage(id: string | null, detail: string): string {
  const line = detail.split(/\r?\n/, 1)[0].trim();
  if (!line || isInternalFailure(line) || !isPublicReason(line)) return publicInternalError(id);
  return line;
}

function isPublicReason(line: string): boolean {
  if (line.length > 400) return false;
  if (/bearer\s|secret key|password|traceback|\.py", line |\/tmp\/|\/data\/|\/workspace/i.test(line)) return false;
  return true;
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
  regathers: readonly number[] | null = null,
): RateDecision {
  const kept = (hits || []).filter((ts) => ts >= now - DAY_S).sort((a, b) => a - b);
  // Re-gathers fill the 10-minute window, never the day.
  const win = [...kept, ...(regathers || [])].filter((ts) => ts >= now - WINDOW_S).sort((a, b) => a - b);
  if (win.length >= per10min) {
    return blocked(now, win[0] + WINDOW_S, windowMessage(per10min, clockUtc(win[0] + WINDOW_S)), kept);
  }
  if (kept.length >= perDay) {
    return blocked(now, kept[0] + DAY_S, dayMessage(perDay, clockUtc(kept[0] + DAY_S)), kept);
  }
  return { error: null, retry_after_s: null, retry_at: null, hits: [...kept, now] };
}

/**
 * One re-gather (one sound gathered again). Peter (2026-10-21): it is not in
 * the daily count, but it shares the 10-minute window with every Gather and
 * Perform. `hits` are the counted hits, `regathers` the re-gathers so far.
 * A full Gather's window check passes both lists to applyPerformHit.
 */
export function applyRegatherHit(
  now: number,
  hits: readonly number[] | null,
  regathers: readonly number[] | null,
  per10min = PER_10MIN,
): RateDecision {
  const win = [...(hits || []), ...(regathers || [])].filter((ts) => ts >= now - WINDOW_S).sort((a, b) => a - b);
  if (win.length >= per10min) {
    return blocked(now, win[0] + WINDOW_S, windowMessage(per10min, clockUtc(win[0] + WINDOW_S)), win);
  }
  return { error: null, retry_after_s: null, retry_at: null, hits: [...win, now] };
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

export type FoundSound = {
  title: string;
  supplier: string;
  license: string;
  chord?: number;
  id?: string;
  preview_url?: string;
  symbol?: string;
  clip_id?: string;
  url?: string;
  author?: string;
  credit?: string;
  page?: string;
  pos?: number;
  start_s?: number;
  end_s?: number;
  duration_s?: number;
  loudness_lufs?: number;
  gain_db?: number;
};

/** Clip responses: echo netcomposer.art, and allow every other caller. */
export function corsHeaders(origin: string | null | undefined): Record<string, string> {
  const headers: Record<string, string> = {
    "access-control-allow-headers": "Content-Type, Authorization, X-NC-Tester",
    "access-control-allow-methods": "GET, POST, OPTIONS",
  };
  const text = (origin || "").trim();
  if (/^https:\/\/([a-z0-9-]+\.)*netcomposer\.art$/i.test(text)) {
    headers["access-control-allow-origin"] = text;
    headers.vary = "Origin";
  } else {
    headers["access-control-allow-origin"] = "*";
  }
  return headers;
}

export const GATHER_CACHE = "public, max-age=31536000, immutable";

export function foundSounds(rows: unknown): FoundSound[] {
  if (!Array.isArray(rows)) return [];
  const out: FoundSound[] = [];
  const seen = new Map<string, number>();
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const item = row as Record<string, unknown>;
    const title = String(item.title || "").trim();
    const supplier = String(item.supplier || "").trim();
    const license = String(item.license || "").trim();
    if (!title || !supplier || !license) continue;
    const chord = typeof item.chord === "number" && Number.isFinite(item.chord) ? Math.trunc(item.chord) : null;
    const key = chord == null ? `${title}\0${supplier}\0${license}` : `chord\0${chord}`;
    const sound: FoundSound = { title, supplier, license };
    if (chord != null) sound.chord = chord;
    if (typeof item.id === "string" && item.id.trim()) sound.id = item.id.trim();
    if (typeof item.preview_url === "string" && item.preview_url.trim()) sound.preview_url = item.preview_url.trim();
    if (typeof item.symbol === "string" && item.symbol.trim()) sound.symbol = item.symbol.trim();
    for (const field of ["clip_id", "url", "author", "credit", "page"] as const) {
      const value = item[field];
      if (typeof value === "string" && value.trim()) sound[field] = value.trim();
    }
    if (typeof item.pos === "number" && Number.isFinite(item.pos)) sound.pos = Math.trunc(item.pos);
    for (const field of ["start_s", "end_s", "duration_s", "loudness_lufs", "gain_db"] as const) {
      const value = item[field];
      if (typeof value === "number" && Number.isFinite(value)) sound[field] = value;
    }
    const at = seen.get(key);
    if (at != null) {
      out[at] = sound;
      continue;
    }
    seen.set(key, out.length);
    out.push(sound);
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

/**
 * The create page starts watching only when the POST body has `state`.
 * It treats a Gather as finished only when a later status says `done`.
 * The stored job stays `gathered` so it is not a Performance.
 */
export function pageGatherState(id: string, state: string): string {
  if (id.startsWith("g-") && state === "gathered") return "done";
  return state;
}

/** Fields the page needs on the Worker 202. `status` alone does not start the watch. */
export function queuedGatherFields(): { state: "queued"; status: "queued"; pending: true; sounds: [] } {
  return { state: "queued", status: "queued", pending: true, sounds: [] };
}

export type RegistryStats = {
  size: number;
  ok: number;
  rejected: number;
  checker_version: number | null;
  recheck_days: number | null;
};

/**
 * Counts in ncperf/data/checked-sounds.json. The Worker serves /health and
 * /api/config, so it cannot see that file. These numbers stand in until R2
 * has registry/v1-stats.json. A job that copies the registry up replaces them.
 */
export const BUNDLED_REGISTRY_STATS: RegistryStats = {
  size: 10334,
  ok: 8977,
  rejected: 1357,
  checker_version: 1,
  recheck_days: 90,
};

/** Counts from registry/v1-stats.json. Missing or junk fields become zeros, never a secret. */
export function registryPublicStats(doc: unknown): RegistryStats {
  const row = doc && typeof doc === "object" ? (doc as Record<string, unknown>) : {};
  const num = (value: unknown): number => {
    const n = typeof value === "number" ? value : Number(value);
    return Number.isFinite(n) && n >= 0 ? Math.trunc(n) : 0;
  };
  const optional = (value: unknown): number | null => {
    if (value == null || value === "") return null;
    const n = typeof value === "number" ? value : Number(value);
    return Number.isFinite(n) && n >= 0 ? n : null;
  };
  return {
    size: num(row.size),
    ok: num(row.ok),
    rejected: num(row.rejected),
    checker_version: optional(row.checker_version),
    recheck_days: optional(row.recheck_days),
  };
}

/** The JSON field on Gather, Perform, and Save. */
export function turnstileToken(body: unknown): string {
  const value = body && typeof body === "object" ? (body as Record<string, unknown>).turnstile_token : "";
  return typeof value === "string" ? value.trim() : "";
}

/**
 * Siteverify fields. remoteip is left off: Turnstile compares it to the IP that
 * solved the widget, and a mismatch rejects a fresh token.
 */
export function siteverifyFields(secret: string, token: string): { secret: string; response: string } {
  return { secret, response: token };
}

const TURNSTILE_CODE = /^[a-z0-9-]{1,64}$/;
const TURNSTILE_HOST = /^[a-z0-9.-]{1,253}$/i;

export type TurnstileFailure = { turnstile_errors: string[]; hostname: string | null };

/** Keep siteverify error-codes and hostname. Drop anything that is not a short code or hostname. */
export function turnstileFailure(codes: unknown, hostname: unknown): TurnstileFailure {
  const turnstile_errors: string[] = [];
  if (Array.isArray(codes)) {
    for (const code of codes) {
      if (typeof code === "string" && TURNSTILE_CODE.test(code)) turnstile_errors.push(code);
      if (turnstile_errors.length >= 8) break;
    }
  }
  const host = typeof hostname === "string" && TURNSTILE_HOST.test(hostname) ? hostname : null;
  return { turnstile_errors, hostname: host };
}

export function turnstileDeniedBody(failure: TurnstileFailure): {
  error: string;
  code: "turnstile";
  turnstile_errors: string[];
  hostname: string | null;
} {
  return {
    error: "The bot check failed. Reload the page and try again.",
    code: "turnstile",
    turnstile_errors: failure.turnstile_errors,
    hostname: failure.hostname,
  };
}

/** Form body with an explicit content type. The secret is trimmed so a trailing newline is not the secret. */
export function siteverifyRequest(secret: string, token: string): {
  method: "POST";
  headers: { "content-type": "application/x-www-form-urlencoded" };
  body: string;
} {
  return {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(siteverifyFields(secret.trim(), token)).toString(),
  };
}

/** R2 wins when the stats object exists. A missing object is the image seed, not an empty catalog. */
export function publishedRegistryStats(stored: unknown): RegistryStats {
  if (stored == null) return { ...BUNDLED_REGISTRY_STATS };
  return registryPublicStats(stored);
}
