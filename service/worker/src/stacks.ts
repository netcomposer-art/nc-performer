/**
 * Note stacks: chords Gather mixed from several single-note recordings.
 *
 * The mixed clip has no source URL, so a failed copy to R2 used to leave a
 * 404 the page could not recover from. Copies are retried. A stack that is
 * still missing is mixed again from its notes (the collection keeps
 * plan.clips[].sids and plan.sources[sid]). If that fails at Gather time,
 * the Gather fails with a sentence that names the chord.
 */

type Clip = { id?: unknown; sids?: unknown; symbol?: unknown; chord?: unknown };
type Sound = { id?: unknown; clip_id?: unknown; symbol?: unknown; pos?: unknown; chord?: unknown };
export type StackDoc = {
  plan?: unknown;
  sounds?: Sound[];
  files?: Array<{ sid?: string; url?: string | null; ext?: string }>;
};

function clipsOf(plan: unknown): Clip[] {
  const clips = plan && typeof plan === "object" ? (plan as { clips?: unknown }).clips : null;
  return Array.isArray(clips) ? (clips.filter((c) => c && typeof c === "object") as Clip[]) : [];
}

/** The note sids of a stacked clip, or [] when the clip is one file. */
export function stackParts(plan: unknown, cid: string): string[] {
  const clip = clipsOf(plan).find((c) => String(c.id ?? "") === cid);
  const sids = Array.isArray(clip?.sids) ? clip!.sids.map((s) => String(s ?? "")).filter(Boolean) : [];
  return sids.length > 1 ? sids : [];
}

/**
 * The one stored file a single-file clip plays, or null for a stack or an
 * unknown id. A loop's passes share one recording (c1, c3, c5 all name r1),
 * so the R2 object is gathers/{id}/audio/r1, not one per clip.
 */
export function singleSid(plan: unknown, cid: string): string | null {
  const clip = clipsOf(plan).find((c) => String(c.id ?? "") === cid);
  const sids = Array.isArray(clip?.sids) ? clip!.sids.map((s) => String(s ?? "")).filter(Boolean) : [];
  return sids.length === 1 ? sids[0] : null;
}

/** The R2 audio name a GET /api/jobs/{id}/audio/{sid} reads. A stack keeps its clip id. */
export function fileSidFor(doc: StackDoc | null, sid: string): string {
  if (!doc || stackParts(doc.plan, sid).length) return sid;
  return singleSid(doc.plan, sid) || sid;
}

/** Every stacked clip id in the plan. */
export function stackIds(plan: unknown): string[] {
  return clipsOf(plan).map((c) => String(c.id ?? "")).filter((cid) => cid && stackParts(plan, cid).length > 1);
}

/** "chord 2 (G13)" for the page. Positions count from one. */
export function chordLabel(doc: StackDoc, cid: string): string {
  const sounds = Array.isArray(doc.sounds) ? doc.sounds : [];
  const index = sounds.findIndex((s) => String(s?.clip_id ?? s?.id ?? "") === cid);
  const row = index >= 0 ? sounds[index] : null;
  const clip = clipsOf(doc.plan).find((c) => String(c.id ?? "") === cid);
  const posRaw = row?.pos ?? row?.chord ?? clip?.chord;
  const pos = typeof posRaw === "number" && Number.isFinite(posRaw) ? posRaw : index >= 0 ? index : Number(/^c(\d+)$/.exec(cid)?.[1] ?? NaN);
  const symbol = String(row?.symbol ?? clip?.symbol ?? "").trim();
  const head = Number.isFinite(pos) ? `chord ${pos + 1}` : "a chord";
  return symbol ? `${head} (${symbol})` : head;
}

/** The Gather's failure sentence when stacked clips could not be stored. */
export function missingStacksMessage(doc: StackDoc, cids: string[]): string {
  const names = cids.map((cid) => chordLabel(doc, cid));
  const list = names.length <= 1 ? names[0] || "a chord" : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
  return `Could not store ${list} from this Gather. Gather again.`;
}

export const COPY_DELAYS_MS = [250, 1000];

export type Attempt = { ok: boolean; tries: number; error: string };

/** Try up to delays.length + 1 times (three by default). A throw counts as a failed try. */
export async function withRetry(
  attempt: (n: number) => Promise<boolean>,
  delays: readonly number[] = COPY_DELAYS_MS,
  sleep: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
): Promise<Attempt> {
  let error = "";
  const tries = delays.length + 1;
  for (let n = 1; n <= tries; n++) {
    try {
      if (await attempt(n)) return { ok: true, tries: n, error: "" };
      error = "not ok";
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    }
    if (n < tries) await sleep(delays[n - 1] ?? 0);
  }
  return { ok: false, tries, error };
}

/**
 * Stacks that did not reach R2: a failed copy, or no file row at all.
 * failed is the list of sids whose copy gave up.
 */
export function lostStacks(doc: StackDoc, failed: Iterable<string>): string[] {
  const gone = new Set(failed);
  const rows = new Set((doc.files || []).map((f) => String(f?.sid || "")));
  return stackIds(doc.plan).filter((cid) => gone.has(cid) || !rows.has(cid));
}

export type AudioFallback = {
  /** Download a single-file clip from its source URL. */
  upstream: (url: string) => Promise<ArrayBuffer | null>;
  /** Mix a stack again from its notes. */
  rebuild: (cid: string) => Promise<ArrayBuffer | null>;
  /** Keep the bytes in R2 so the next play is a plain read. */
  store: (bytes: ArrayBuffer) => Promise<void>;
};

export const AUDIO_MAX_BYTES = 80 * 1024 * 1024;

/** What gatherAudio does when R2 has no object for sid. null means 404. */
export async function recoverGatherAudio(
  doc: StackDoc | null,
  sid: string,
  deps: AudioFallback,
): Promise<{ bytes: ArrayBuffer; via: "rebuilt" | "upstream" } | null> {
  if (!doc) return null;
  const fits = (b: ArrayBuffer | null): b is ArrayBuffer => !!b && b.byteLength > 0 && b.byteLength <= AUDIO_MAX_BYTES;
  if (stackParts(doc.plan, sid).length) {
    let bytes: ArrayBuffer | null = null;
    try {
      bytes = await deps.rebuild(sid);
    } catch {
      bytes = null;
    }
    if (!fits(bytes)) return null;
    await deps.store(bytes);
    return { bytes, via: "rebuilt" };
  }
  const row = (doc.files || []).find((f) => f?.sid === sid);
  if (!row?.url || !row.url.startsWith("https://")) return null;
  let bytes: ArrayBuffer | null = null;
  try {
    bytes = await deps.upstream(row.url);
  } catch {
    bytes = null;
  }
  if (!fits(bytes)) return null;
  await deps.store(bytes);
  return { bytes, via: "upstream" };
}

export type ClipReader<T> = AudioFallback & {
  /** The stored R2 object for one audio name, or null. */
  get: (fileSid: string) => Promise<T | null>;
};

/**
 * What gatherAudio serves for one clip id: the stored object (under the name
 * fileSidFor gives), else the recovered bytes (kept under that same name),
 * else null for a 404. store() receives the bytes for fileSid.
 */
export async function readGatherClip<T>(
  doc: StackDoc | null,
  sid: string,
  deps: ClipReader<T>,
): Promise<{ fileSid: string; obj: T } | { fileSid: string; bytes: ArrayBuffer; via: "rebuilt" | "upstream" } | null> {
  const fileSid = fileSidFor(doc, sid);
  const obj = await deps.get(fileSid);
  if (obj) return { fileSid, obj };
  if (!doc) return null;
  const got = await recoverGatherAudio(doc, fileSid, deps);
  return got ? { fileSid, ...got } : null;
}
