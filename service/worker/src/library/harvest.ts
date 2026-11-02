/**
 * Nightly Library harvest (design v1, step 3): search Freesound by role, keep only accepted licenses and short
 * one-shots, skip everything seen before (including every sound in musicmatter.art's library.json and
 * rejected.json), and store the picked files in R2 under candidates/raw/ for the qualify step.
 *
 * Original files when the harvester account is connected (OAuth2), the HQ MP3 preview otherwise; preview-quality
 * candidates say so ("quality": "preview") so they can be upgraded later.
 */
import { rotateQueries } from "./config.ts";
import { classifyLicense } from "./licenses.ts";
import { BudgetExhausted, RateLimited, type FreesoundClient, type FsSound } from "./freesound.ts";
import type { BatchLog, BatchSkip, Candidate, LibraryStore } from "./state.ts";

export interface BlobStore {
  put(key: string, body: Uint8Array | string, contentType: string): Promise<void>;
  get(key: string): Promise<Uint8Array | null>;
  delete(key: string): Promise<void>;
}

/** What musicmatter.art already holds: Freesound ids and fingerprints in library.json and rejected.json. */
export type Known = { freesound: Set<string>; sha: Set<string>; ids: Set<string> };

export type HarvestDeps = {
  store: LibraryStore;
  client: FreesoundClient;
  blobs: BlobStore;
  /** An OAuth2 access token for original downloads, or null for previews. */
  accessToken: () => Promise<string | null>;
  known: () => Promise<Known>;
  now: () => Date;
  log: (event: Record<string, unknown>) => void;
};

const LOOP = /\b(loop|loops|bpm|groove|pattern|break ?beat|fill)\b/i;

export function sourceKey(id: number | string): string {
  return `freesound:${id}`;
}

export function slugify(text: string): string {
  return text.toLowerCase().replace(/\.(wav|wave|aif|aiff|flac|ogg|mp3|m4a)$/i, "").replace(/[^a-z0-9]+/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "");
}

/** Candidate id: stable per Freesound sound, readable, safe as a file name and an R2 key. */
export function candidateId(sound: Pick<FsSound, "id" | "name">): string {
  return `fs-${sound.id}-${slugify(sound.name)}`.slice(0, 80).replace(/-$/, "");
}

export function dayNumber(d: Date): number {
  return Math.floor(d.getTime() / 86_400_000);
}

function iso(d: Date): string {
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

/** Freesound ids and fingerprints in musicmatter.art's library.json and rejected.json. */
export function knownFrom(library: unknown, rejected: unknown): Known {
  const known: Known = { freesound: new Set(), sha: new Set(), ids: new Set() };
  const add = (id: string, entry: Record<string, unknown> | undefined) => {
    known.ids.add(id);
    if (!entry) return;
    if (typeof entry.sha256 === "string") known.sha.add(entry.sha256);
    const src = (entry.source && typeof entry.source === "object" ? entry.source : {}) as Record<string, unknown>;
    if (typeof src.sha256 === "string") known.sha.add(src.sha256);
    const fsid = freesoundIdOf(src, id);
    if (fsid) known.freesound.add(fsid);
  };
  const sounds = (library && typeof library === "object" ? (library as { sounds?: unknown }).sounds : null);
  if (Array.isArray(sounds)) {
    for (const s of sounds) if (s && typeof s === "object" && typeof (s as { id?: unknown }).id === "string") add((s as { id: string }).id, s as Record<string, unknown>);
  }
  if (rejected && typeof rejected === "object" && !Array.isArray(rejected)) {
    for (const [id, entry] of Object.entries(rejected as Record<string, unknown>)) add(id, entry as Record<string, unknown>);
  }
  return known;
}

export function freesoundIdOf(source: Record<string, unknown>, candidate = ""): string | null {
  if (source.origin === "freesound" && (typeof source.id === "string" || typeof source.id === "number")) return String(source.id);
  const url = typeof source.url === "string" ? source.url : "";
  const m = url.match(/freesound\.org\/(?:people\/[^/]+\/)?(?:sounds|s)\/(\d+)/);
  if (m) return m[1];
  const c = candidate.match(/^fs-(\d+)(?:-|$)/);
  return c ? c[1] : null;
}

export async function runHarvest(deps: HarvestDeps, batchId: string): Promise<BatchLog> {
  const { store, client, blobs, now, log } = deps;
  const started = now();
  const batch: BatchLog = {
    id: batchId,
    state: "harvesting",
    started_at: iso(started),
    queries: [],
    picked: [],
    skipped: [],
    requests: client.requests,
  };
  const finish = async (state: string, stop?: string): Promise<BatchLog> => {
    batch.state = state;
    batch.finished_at = iso(now());
    batch.requests = { ...client.requests };
    if (stop) batch.stop_reason = stop;
    await store.putBatch(batch);
    await blobs.put(`candidates/batches/${batchId}.json`, JSON.stringify(batch, null, 1), "application/json");
    log({ event: "library_harvest", batch: batchId, state, stop, picked: batch.picked.length, skipped: batch.skipped.length, requests: batch.requests });
    return batch;
  };

  const existing = await store.getBatch(batchId);
  if (existing && existing.state !== "harvesting") return existing;
  const cfg = await store.getConfig();
  if (!cfg.enabled) return finish("disabled", "harvest is turned off in the Library config");
  const counts = await store.countCandidates();
  const waiting = (counts.pending || 0) + (counts.raw || 0);
  if (waiting >= cfg.max_pending) return finish("skipped", `${waiting} candidates already wait for review (max_pending ${cfg.max_pending})`);

  let known: Known;
  try {
    known = await deps.known();
  } catch (err) {
    // Never harvest blind: without rejected.json a rejected sound could come back.
    return finish("failed", `could not read musicmatter.art library.json / rejected.json: ${err instanceof Error ? err.message : String(err)}`);
  }

  let token: string | null = null;
  try {
    token = await deps.accessToken();
  } catch (err) {
    log({ event: "library_oauth", detail: err instanceof Error ? err.message : String(err) });
  }
  batch.quality = token ? "original" : "preview";

  const room = Math.min(cfg.max_candidates, cfg.max_pending - waiting);
  const packs = new Map<string, number>();
  let licenseFilter = true;
  const skip = async (sound: FsSound, reason: string, remember: boolean) => {
    const entry: BatchSkip = { source: sourceKey(sound.id), name: sound.name, reason };
    batch.skipped.push(entry);
    if (remember) await store.markSeen(sourceKey(sound.id), { state: "skipped", reason, at: iso(now()), batch: batchId });
  };

  try {
    outer: for (const query of rotateQueries(cfg.queries, dayNumber(started))) {
      if (batch.picked.length >= room) break;
      const row = { q: query.q, role: query.role, requests: 0, seen: 0, picked: 0 } as BatchLog["queries"][number];
      batch.queries.push(row);
      for (let page = 1; page <= cfg.max_pages && row.picked < cfg.per_query; page++) {
        row.requests += 1;
        let res = await client.search(query.q, page, cfg, licenseFilter);
        if ("status" in res && res.status === 400 && licenseFilter) {
          // The license filter is a convenience; every license is checked below anyway.
          licenseFilter = false;
          log({ event: "library_search_filter", detail: res.error });
          row.requests += 1;
          res = await client.search(query.q, page, cfg, false);
        }
        if ("status" in res) {
          row.error = `search HTTP ${res.status}`;
          break;
        }
        const seen = await store.seenMany(res.results.map((s) => sourceKey(s.id)));
        for (const sound of res.results) {
          if (batch.picked.length >= room || row.picked >= cfg.per_query) break;
          const key = sourceKey(sound.id);
          if (seen[key] || known.freesound.has(String(sound.id))) {
            row.seen += 1;
            continue;
          }
          const lic = classifyLicense(sound.license);
          if (!lic.ok) {
            await skip(sound, `license: ${lic.reason}`, true);
            continue;
          }
          if (sound.duration > cfg.max_duration_s) {
            await skip(sound, `longer than ${cfg.max_duration_s} s (${sound.duration.toFixed(2)} s)`, true);
            continue;
          }
          if (LOOP.test(sound.name.replace(/[_.]+/g, " "))) {
            await skip(sound, "looks like a loop, not a one-shot", true);
            continue;
          }
          const pack = sound.pack || "";
          if (pack && (packs.get(pack) || 0) >= cfg.per_pack) continue;
          const id = candidateId(sound);
          if (known.ids.has(id)) {
            row.seen += 1;
            continue;
          }
          let file = token ? await client.original(sound, token) : await client.preview(sound);
          let quality: "original" | "preview" = token ? "original" : "preview";
          if ("status" in file && token && (file.status === 401 || file.status === 403)) {
            log({ event: "library_oauth", detail: `download refused (${file.status}); using previews for the rest of the night` });
            token = null;
            batch.quality = "mixed";
            file = await client.preview(sound);
            quality = "preview";
          }
          if ("status" in file) {
            // Not remembered: a failed download is tried again another night.
            await skip(sound, file.error, false);
            continue;
          }
          const at = iso(now());
          const rawKey = `candidates/raw/${id}.${file.ext}`;
          await blobs.put(rawKey, file.bytes, "application/octet-stream");
          const cand: Candidate = {
            id,
            source_key: key,
            batch: batchId,
            state: "raw",
            quality,
            raw_key: rawKey,
            ext: file.ext,
            query: query.q,
            role_hint: query.role,
            sound: {
              id: sound.id,
              name: sound.name,
              tags: sound.tags,
              username: sound.username,
              url: sound.url,
              license: sound.license,
              license_spdx: lic.spdx,
              license_url: lic.url,
              license_label: lic.label,
              license_read_at: at,
              type: sound.type,
              pack: sound.pack ?? null,
            },
            updated_at: at,
          };
          await store.putCandidate(cand);
          await store.markSeen(key, { id, state: "harvested", at, batch: batchId });
          if (pack) packs.set(pack, (packs.get(pack) || 0) + 1);
          batch.picked.push(id);
          row.picked += 1;
          if (batch.picked.length >= room) break outer;
        }
        if (!res.next) break;
      }
    }
  } catch (err) {
    if (err instanceof BudgetExhausted) return finish("harvested", err.message);
    if (err instanceof RateLimited) return finish("harvested", err.message);
    batch.stop_reason = err instanceof Error ? err.message : String(err);
    await store.putBatch(batch);
    throw err;
  }
  return finish("harvested");
}
