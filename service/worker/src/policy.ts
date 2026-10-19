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
  maxLayers: 8,
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
