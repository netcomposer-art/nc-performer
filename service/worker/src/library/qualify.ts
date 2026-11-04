/**
 * Qualify step: send each harvested file to the container (decode to 48 kHz, trim, measure, fingerprint, role, tags),
 * reject duplicates by fingerprint, and store what passes in R2 for review:
 *
 *   candidates/<id>/audio.flac          24-bit FLAC at 48 kHz (what publish copies into musicmatter.art)
 *   candidates/<id>/preview.mp3         level-matched 128 kbps preview for the review page
 *   candidates/<id>/manifest.nc.json    NC manifest, curation pending
 *
 * A failed gate is recorded with its reason, on the candidate, in the batch log and in the seen table (so the sound
 * is not harvested again).
 */
import { buildManifest, type QualifyResult } from "./manifest.ts";
import type { BlobStore, Known } from "./harvest.ts";
import type { BatchLog, Candidate, LibraryStore } from "./state.ts";

export type QualifyMeta = { id: string; name: string; tags: string[]; query: string; role: string; quality: string; ext: string };

export type QualifyDeps = {
  store: LibraryStore;
  blobs: BlobStore;
  qualify: (raw: Uint8Array, meta: QualifyMeta) => Promise<QualifyResult>;
  known: () => Promise<Known>;
  now: () => Date;
  log: (event: Record<string, unknown>) => void;
};

/** Container errors in a row before the run stops (the rest wait for the next run). */
const MAX_CONTAINER_ERRORS = 3;

function iso(d: Date): string {
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

export async function runQualify(deps: QualifyDeps, batchId: string): Promise<BatchLog | null> {
  const { store, blobs, now, log } = deps;
  const raws = await store.listCandidates("raw");
  const batch = (await store.getBatch(batchId)) || null;
  let known: Known = { freesound: new Set(), sha: new Set(), ids: new Set() };
  try {
    known = await deps.known();
  } catch (err) {
    log({ event: "library_qualify_known", detail: err instanceof Error ? err.message : String(err) });
  }
  let qualified = 0;
  const failed: { id: string; reason: string }[] = [];
  let errors = 0;
  const fail = async (c: Candidate, reason: string) => {
    const at = iso(now());
    await store.putCandidate({ ...c, state: "failed", reason, updated_at: at, raw_key: undefined });
    await store.markSeen(c.source_key, { id: c.id, state: "failed", reason, at, batch: c.batch });
    if (c.raw_key) await blobs.delete(c.raw_key);
    failed.push({ id: c.id, reason });
  };
  for (const c of raws) {
    if (!c.raw_key) {
      await fail(c, "raw file missing");
      continue;
    }
    const raw = await blobs.get(c.raw_key);
    if (!raw) {
      await fail(c, "raw file missing");
      continue;
    }
    let r: QualifyResult;
    try {
      r = await deps.qualify(raw, {
        id: c.id,
        // Sent as a header: keep it small.
        name: c.sound.name.slice(0, 200),
        tags: c.sound.tags.slice(0, 30).map((t) => t.slice(0, 40)),
        query: c.query,
        role: c.role_hint,
        quality: c.quality,
        ext: c.ext,
      });
      errors = 0;
    } catch (err) {
      errors += 1;
      log({ event: "library_qualify_error", id: c.id, detail: err instanceof Error ? err.message : String(err) });
      if (errors >= MAX_CONTAINER_ERRORS) break;
      continue;
    }
    if (!r.ok || !r.sha256 || !r.flac || !r.preview) {
      await fail(c, r.reason || "qualify returned no audio");
      continue;
    }
    if (known.sha.has(r.sha256) || (r.source_sha256 && known.sha.has(r.source_sha256))) {
      await fail(c, "duplicate of a sound already on musicmatter.art");
      continue;
    }
    const owner = (await store.claimSha(r.sha256, c.id)) || (r.source_sha256 ? await store.claimSha(r.source_sha256, c.id) : null);
    if (owner) {
      await fail(c, `duplicate of ${owner}`);
      continue;
    }
    const manifest = buildManifest(c, r);
    await blobs.put(`candidates/${c.id}/audio.flac`, r.flac, "audio/flac");
    await blobs.put(`candidates/${c.id}/preview.mp3`, r.preview, "audio/mpeg");
    await blobs.put(`candidates/${c.id}/manifest.nc.json`, JSON.stringify(manifest, null, 2), "application/json");
    await store.putCandidate({
      ...c,
      state: "pending",
      raw_key: undefined,
      sha256: r.sha256,
      manifest,
      ...(r.note ? { note: r.note } : {}),
      updated_at: iso(now()),
    });
    await store.markSeen(c.source_key, { id: c.id, state: "pending", at: iso(now()), batch: c.batch });
    await blobs.delete(c.raw_key);
    qualified += 1;
  }
  log({ event: "library_qualify", batch: batchId, qualified, failed: failed.length, left: raws.length - qualified - failed.length });
  if (!batch) return null;
  const next: BatchLog = {
    ...batch,
    state: "qualified",
    qualified: (batch.qualified || 0) + qualified,
    failed: [...(batch.failed || []), ...failed],
  };
  await store.putBatch(next);
  await blobs.put(`candidates/batches/${batchId}.json`, JSON.stringify(next, null, 1), "application/json");
  return next;
}
