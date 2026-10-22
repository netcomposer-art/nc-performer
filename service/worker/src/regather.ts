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

/** The body's re-gather request, an error sentence, or null when it is a plain Gather. */
export function regatherRequest(body: Record<string, unknown>): RegatherSpec | { error: string; status: number } | null {
  const raw = body.regather_gather_id;
  if (raw == null || raw === "") return null;
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

