/**
 * Sliced notes in the PERFORMANCES bucket. The page reads
 * notes/{slug}/manifest.json and notes/{slug}/{midi}.opus.
 * FLAC masters live beside them and are not on a public route.
 * The daily expiry job lists performances/ and jobs/ only.
 */
import { corsHeaders, GATHER_CACHE } from "./policy.ts";

export const NOTE_OPUS_CACHE = GATHER_CACHE;
/** A republish replaces the manifest. Five minutes, and not immutable. */
export const NOTE_MANIFEST_CACHE = "public, max-age=300";

/** Prefixes the daily job lists. notes/ is the instrument library and is not here. */
export const EXPIRY_PREFIXES = ["performances/", "jobs/"] as const;

const SLUG = "[a-z0-9]+(?:-[a-z0-9]+)*";
const MIDI = "0|[1-9][0-9]{0,2}";

export type NoteHit = {
  key: string;
  cache: string;
  contentType: string;
};

export function noteObject(path: string): NoteHit | "hidden" | null {
  const manifest = path.match(new RegExp(`^/api/notes/(${SLUG})/manifest\\.json$`));
  if (manifest) {
    return {
      key: `notes/${manifest[1]}/manifest.json`,
      cache: NOTE_MANIFEST_CACHE,
      contentType: "application/json",
    };
  }
  const opus = path.match(new RegExp(`^/api/notes/(${SLUG})/(${MIDI})\\.opus$`));
  if (opus) {
    const midi = Number(opus[2]);
    if (midi > 127) return "hidden";
    return {
      key: `notes/${opus[1]}/${midi}.opus`,
      cache: NOTE_OPUS_CACHE,
      contentType: "audio/ogg",
    };
  }
  if (path === "/api/notes" || path.startsWith("/api/notes/")) return "hidden";
  return null;
}

/** False for the sliced-note library, including a key that merely begins with notes/. */
export function expiryMayDelete(key: string): boolean {
  if (key.startsWith("notes/")) return false;
  return EXPIRY_PREFIXES.some((prefix) => key.startsWith(prefix));
}

type Stored = {
  body: ReadableStream | ArrayBuffer | Uint8Array | string;
};

/**
 * A Response for /api/notes/..., or null when the path is some other route.
 * Masters, chord files, and unknown names are 404 and are not read from R2.
 */
export async function publishedNoteResponse(
  origin: string | null,
  path: string,
  get: (key: string) => Promise<Stored | null>,
): Promise<Response | null> {
  const asked = noteObject(path);
  if (asked == null) return null;
  if (asked === "hidden") return missing(origin);
  const obj = await get(asked.key);
  if (!obj) return missing(origin);
  return new Response(obj.body, {
    status: 200,
    headers: {
      "content-type": asked.contentType,
      "cache-control": asked.cache,
      ...corsHeaders(origin),
    },
  });
}

function missing(origin: string | null): Response {
  return new Response(JSON.stringify({ error: "not found" }), {
    status: 404,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      ...corsHeaders(origin),
    },
  });
}
