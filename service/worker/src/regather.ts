/**
 * Re-gather one sound: a new Gather whose other chords are the previous
 * Gather's, unchanged. The container searches only the chosen chord and holds
 * only its new file. The Worker copies every other chord's audio R2 to R2 from
 * the previous Gather, so there is no per-file container call for them.
 */

export type RegatherSpec = { gather_id: string; chord: number };

type Doc = { plan?: unknown; files?: Array<{ sid?: string; carried?: boolean }>; regather?: { from?: string; chord?: number; sids?: string[] } };

const ID = /^g-[0-9a-z]{6,40}$/;

/** The sids of chord `index`: c3, c3b (bass), c3n0… (notes). */
export function slotPattern(index: number): RegExp {
  return new RegExp(`^c${Math.trunc(index)}(?:b|n\\d+)?$`);
}

/** Fields that would make a re-gather a new Gather. It uses its Gather's words. */
export const REGATHER_FORBIDDEN = ["words", "composition", "performance", "prompt", "title", "composer", "instrument", "exclude_gather_id", "seed"];
export const REGATHER_WORDS_ERROR = "A re-gather uses its Gather's words. Send only regather_gather_id, regather_chord and claim_token.";

/** The body's re-gather request, an error sentence, or null when it is a plain Gather. */
export function regatherRequest(body: Record<string, unknown>): RegatherSpec | { error: string; status: number } | null {
  const raw = body.regather_gather_id;
  if (raw == null || raw === "") return null;
  // A re-gather is outside the daily count (Peter, 2026-10-21), so it may not carry its own words.
  if (REGATHER_FORBIDDEN.some((key) => body[key] != null && body[key] !== "")) return { error: REGATHER_WORDS_ERROR, status: 400 };
  const id = typeof raw === "string" ? raw.trim() : "";
  if (!ID.test(id)) return { error: "not found", status: 404 };
  const chord = body.regather_chord;
  if (typeof chord !== "number" || !Number.isInteger(chord) || chord < 0) {
    return { error: "regather_chord must name a chord in that Gather.", status: 400 };
  }
  return { gather_id: id, chord };
}

/** True when `chord` is a clip of the previous Gather's plan. */
export function chordInPlan(plan: unknown, chord: number): boolean {
  const clips = plan && typeof plan === "object" ? (plan as { clips?: unknown }).clips : null;
  return Array.isArray(clips) && chord >= 0 && chord < clips.length && !!clips[chord] && typeof clips[chord] === "object";
}

/** Previous audio keys to copy into the new Gather: every object that is not the re-gathered chord's. */
export function carriedCopies(keys: string[], fromId: string, toId: string, chord: number): Array<[string, string]> {
  const prefix = `gathers/${fromId}/audio/`;
  const slot = slotPattern(chord);
  const out: Array<[string, string]> = [];
  for (const key of keys) {
    if (!key.startsWith(prefix)) continue;
    const sid = key.slice(prefix.length);
    if (!/^[A-Za-z0-9._-]{1,80}$/.test(sid) || slot.test(sid)) continue;
    out.push([key, `gathers/${toId}/audio/${sid}`]);
  }
  return out;
}

/** The file rows the container holds. A plain Gather: all of them. A re-gather: only the new chord's. */
export function containerFiles<T extends { sid?: string; carried?: boolean }>(doc: Doc, files: T[]): T[] {
  if (!doc.regather) return files;
  const own = new Set((doc.regather.sids || []).map(String));
  return files.filter((file) => !file.carried && own.has(String(file.sid || "")));
}

/** Run `work`, or reject after `ms`. A container call here never waits forever. */
export async function withTimeout<T>(work: Promise<T>, ms: number, what: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const late = new Promise<never>((_ok, no) => {
    timer = setTimeout(() => no(new Error(`${what} took longer than ${Math.round(ms / 1000)} s`)), ms);
  });
  try {
    return await Promise.race([work, late]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}


type Row = Record<string, unknown>;
type Collection = { composition?: unknown; plan?: unknown; sounds?: unknown; regather?: { from?: string; chord?: number } };
const URL_FIELDS = new Set(["url", "preview_url", "cache", "gather_id"]);

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}
function rowsOf(value: unknown): Row[] {
  return Array.isArray(value) ? (value as Row[]) : [];
}
function planOf(doc: Collection | null): { clips: Row[]; sources: Record<string, unknown> } {
  const plan = (doc?.plan && typeof doc.plan === "object" ? doc.plan : {}) as { clips?: unknown; sources?: unknown };
  const sources = plan.sources && typeof plan.sources === "object" ? (plan.sources as Record<string, unknown>) : {};
  return { clips: rowsOf(plan.clips), sources };
}

/**
 * Why this collection is not a one-chord re-gather of `parent`, or null.
 * Same words, same chord count, the chord asked for, and every other chord's
 * sources, clip and sound row unchanged (sound urls move to the new id).
 */
export function regatherProblem(asked: RegatherSpec | null, parent: Collection | null, doc: Collection): string | null {
  if (!asked) return "a re-gather collection on a plain Gather";
  if (!parent) return "the parent Gather is gone";
  if (!doc.regather || doc.regather.from !== asked.gather_id || doc.regather.chord !== asked.chord) return "not the re-gather that was asked for";
  if (!same(parent.composition, doc.composition)) return "other words";
  const a = planOf(parent), b = planOf(doc);
  if (a.clips.length !== b.clips.length || a.clips.length === 0) return "another chord count";
  for (let i = 0; i < a.clips.length; i++) {
    if (i === asked.chord) continue;
    const slot = slotPattern(i);
    const pick = (src: Record<string, unknown>) => Object.keys(src).filter((k) => slot.test(k)).sort().map((k) => [k, src[k]]);
    if (!same(pick(a.sources), pick(b.sources)) || !same(a.clips[i], b.clips[i])) return `chord ${i + 1} changed`;
    const strip = (row: Row | undefined) => Object.fromEntries(Object.entries(row || {}).filter(([k]) => !URL_FIELDS.has(k)).sort());
    if (!same(strip(rowsOf(parent.sounds)[i]), strip(rowsOf(doc.sounds)[i]))) return `chord ${i + 1} changed`;
  }
  const slots = a.clips.map((_c, i) => slotPattern(i));
  const loose = new Set([...Object.keys(a.sources), ...Object.keys(b.sources)].filter((k) => !slots.some((p) => p.test(k))));
  for (const k of loose) if (!same(a.sources[k], b.sources[k])) return "other sources changed";
  return null;
}
