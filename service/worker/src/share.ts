/**
 * Instrument links (specs/instrument-links.md, Peter 2026-11-25: "unlisted for now, use netcomposer.art").
 *
 * A short link is one instrument's Preset, stored as JSON under share/{id}.json in the PERFORMANCES bucket:
 *   POST /api/share        body: a share envelope  →  { id }
 *   GET  /api/share/{id}   →  the envelope (immutable)
 * Links are unlisted: the id is 10 random base-36 characters, and nothing lists the share/ prefix. The daily
 * expiry job lists performances/ and jobs/ only, so links never expire. Audio never travels here: an envelope is
 * settings only (a NYX pad's own sample stays in the player's browser).
 */
import { corsHeaders } from "./policy.ts";

/** The envelope version this route accepts. */
export const SHARE_V = 1;
/** Largest envelope accepted (bytes of JSON). */
export const SHARE_MAX_BYTES = 128 * 1024;
/** The instruments a link can carry: each one's Preset format. */
export const SHARE_FORMATS = [
  "netcomposer.synth",
  "netcomposer.rhythm",
  "netcomposer.bass",
  "netcomposer.nyx",
  "netcomposer.calliope",
] as const;
export const SHARE_CACHE = "public, max-age=31536000, immutable";

const ID_LEN = 10;
const ID = new RegExp(`^/api/share/([0-9a-z]{${ID_LEN}})$`);
const NAME_MAX = 80;
const BY_MAX = 40;

export type ShareEnvelope = {
  v: 1;
  kind: "preset";
  format: (typeof SHARE_FORMATS)[number];
  fv?: number;
  data: unknown;
  name?: string;
  by?: string;
  from?: string;
};

/** The problem with an envelope, in a short sentence, or null when it can be stored. */
export function shareProblem(raw: unknown): string | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return "A link needs an object.";
  const e = raw as Record<string, unknown>;
  if (e.v !== SHARE_V) return "Unknown link version.";
  if (e.kind !== "preset") return "Only presets can be shared.";
  if (typeof e.format !== "string" || !(SHARE_FORMATS as readonly string[]).includes(e.format)) {
    return "That instrument can't be shared yet.";
  }
  if (!("data" in e) || e.data === null || typeof e.data !== "object") return "The preset is empty.";
  if (e.fv !== undefined && (typeof e.fv !== "number" || !Number.isFinite(e.fv))) return "Bad format version.";
  if (e.name !== undefined && (typeof e.name !== "string" || e.name.length > NAME_MAX)) return "The name is too long.";
  if (e.by !== undefined && (typeof e.by !== "string" || e.by.length > BY_MAX)) return "The author name is too long.";
  if (e.from !== undefined && (typeof e.from !== "string" || !isOurLink(e.from))) return "Bad remix link.";
  return null;
}

/** The site's hosts: netcomposer.art now, triki.art later (Peter, 2026-11-25), so a move needs no change here. */
export const SHARE_HOSTS = ["netcomposer.art", "triki.art"] as const;

/** A link this site made: https://netcomposer.art/... or https://triki.art/... (no other host, at most 4 KB). */
export function isOurLink(text: string): boolean {
  if (text.length > 4096) return false;
  try {
    const u = new URL(text);
    return u.protocol === "https:" && (SHARE_HOSTS as readonly string[]).includes(u.hostname);
  } catch {
    return false;
  }
}

/** A fresh unlisted id: 10 characters of 0-9a-z from crypto randomness. */
export function newShareId(random: (n: number) => Uint8Array = (n) => crypto.getRandomValues(new Uint8Array(n))): string {
  const abc = "0123456789abcdefghijklmnopqrstuvwxyz";
  let id = "";
  while (id.length < ID_LEN) {
    for (const b of random(ID_LEN * 2)) {
      if (b < 252) id += abc[b % 36]; // 252 = 7 × 36: no bias
      if (id.length === ID_LEN) break;
    }
  }
  return id;
}

export type ShareDeps = {
  get(key: string): Promise<{ text(): Promise<string> } | null>;
  put(key: string, body: string, meta: Record<string, string>): Promise<void>;
  /** False when this caller has made too many links just now. */
  allow(request: Request): Promise<boolean>;
  random?: (n: number) => Uint8Array;
  now?: () => Date;
};

function reply(origin: string | null, status: number, data: unknown, cache = "no-store"): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": cache, ...corsHeaders(origin) },
  });
}

/** A Response for /api/share routes, or null when the path is some other route. */
export async function shareRoute(request: Request, deps: ShareDeps): Promise<Response | null> {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/$/, "");
  const origin = request.headers.get("Origin");
  if (path !== "/api/share" && !path.startsWith("/api/share/")) return null;

  if (request.method === "POST" && path === "/api/share") {
    if (!(await deps.allow(request))) return reply(origin, 429, { error: "Too many links just now. Try again in a moment.", code: "rate" });
    const text = await request.text();
    if (new TextEncoder().encode(text).length > SHARE_MAX_BYTES) return reply(origin, 413, { error: "This preset is too large to share.", code: "size" });
    let body: unknown;
    try {
      body = JSON.parse(text);
    } catch {
      return reply(origin, 400, { error: "A link needs JSON.", code: "bad" });
    }
    const problem = shareProblem(body);
    if (problem) return reply(origin, 400, { error: problem, code: "bad" });
    const id = newShareId(deps.random);
    const at = (deps.now?.() ?? new Date()).toISOString();
    await deps.put(`share/${id}.json`, JSON.stringify(body), { created: at, format: (body as ShareEnvelope).format });
    return reply(origin, 201, { id });
  }

  const m = path.match(ID);
  if (request.method === "GET" && m) {
    const obj = await deps.get(`share/${m[1]}.json`);
    if (!obj) return reply(origin, 404, { error: "This link doesn't exist.", code: "missing" });
    return new Response(await obj.text(), {
      status: 200,
      headers: { "content-type": "application/json; charset=utf-8", "cache-control": SHARE_CACHE, ...corsHeaders(origin) },
    });
  }
  return reply(origin, 404, { error: "not found" });
}
