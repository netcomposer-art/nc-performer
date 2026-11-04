/**
 * The NC manifest (.nc.json) of a harvested candidate, in the same shape as musicmatter.art's VCSL seed manifests
 * (tools/library/seed_vcsl.py), so library.json stays one format. Credit is the original creator's; musicmatter.art
 * is only the curator.
 */
import type { Candidate } from "./state.ts";

export const SITE = "https://musicmatter.art";
export const CURATOR = { name: "musicmatter.art", link: `${SITE}/` };

export type QualifyResult = {
  ok: boolean;
  reason?: string;
  sha256?: string;
  source_sha256?: string;
  size_bytes?: number;
  role?: string;
  role_basis?: string;
  tags?: string[];
  title?: string;
  measures?: Record<string, unknown>;
  measures_version?: string;
  playback?: Record<string, unknown>;
  format?: Record<string, unknown>;
  processing?: string[];
  wave?: string;
  note?: string;
  flac?: Uint8Array;
  preview?: Uint8Array;
};

export function creatorLink(username: string): string {
  return `https://freesound.org/people/${encodeURIComponent(username)}/`;
}

export function buildManifest(c: Candidate, r: QualifyResult): Record<string, unknown> {
  const role = r.role || c.role_hint;
  const title = r.title || c.sound.name;
  return {
    nc_version: "0.2",
    kind: "one-shot",
    role,
    title,
    tags: r.tags || [role],
    creator: { name: c.sound.username, link: creatorLink(c.sound.username) },
    curator: CURATOR,
    license: c.sound.license_spdx,
    license_url: c.sound.license_url,
    attribution: `"${c.sound.name}" by ${c.sound.username} (${c.sound.url}), ${c.sound.license_label}`,
    source: {
      origin: "freesound",
      id: String(c.sound.id),
      url: c.sound.url,
      name: c.sound.name,
      username: c.sound.username,
      license_read: c.sound.license,
      license_read_at: c.sound.license_read_at,
      file: c.quality === "original" ? "original" : "preview-hq-mp3",
      original_type: c.sound.type || null,
      pack: c.sound.pack || null,
      sha256: r.source_sha256,
      harvest: { batch: c.batch, query: c.query },
    },
    quality: c.quality,
    audio: `${SITE}/library/audio/${role}/${c.id}.flac`,
    format: r.format,
    sha256: r.sha256,
    size_bytes: r.size_bytes,
    processing: r.processing,
    measures: r.measures,
    playback: r.playback,
    curation: { status: "pending" },
    ...(r.note ? { review_note: r.note } : {}),
  };
}
