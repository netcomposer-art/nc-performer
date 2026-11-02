/**
 * Library HTTP routes on nc-perform-api.
 *
 * Admin (Authorization: Bearer NC_ADMIN_TOKEN), used by musicmatter.art tools/library/*.py:
 *   GET  /api/library/admin/status                     secrets present, OAuth account, budget, counts, last batches
 *   GET  /api/library/admin/config                     harvest settings      PUT: change some of them
 *   POST /api/library/admin/harvest                    run a harvest now (then qualify), same budget as the night
 *   POST /api/library/admin/qualify                    qualify what is harvested but not measured yet
 *   GET  /api/library/admin/candidates[?state=pending] the review queue, with manifests
 *   GET  /api/library/admin/candidates/<id>/<file>     audio.flac | preview.mp3 | manifest.nc.json
 *   POST /api/library/admin/decisions                  {decisions: {id: {status, at?, note?}}} from Peter's review
 *   GET  /api/library/admin/batches[/<id>]             harvest logs, every skip with its reason
 *   POST /api/library/admin/freesound/disconnect       forget the harvester account's tokens
 *
 * Freesound OAuth2 for the harvester account (one-time, in a browser):
 *   GET  /api/library/freesound/connect                a form that asks for NC_ADMIN_TOKEN
 *   POST /api/library/freesound/connect                checks the token, then sends the browser to Freesound
 *   GET  /api/library/freesound/callback               Freesound sends the browser back here with a code
 */
import { authorizeUrl, exchangeCode, refreshTokens, FreesoundClient, OAuthError, type Fetcher } from "./freesound.ts";
import type { LibraryStore, OAuthRecord } from "./state.ts";

export type LibraryMessage = { id: string; kind: "library-harvest" | "library-qualify"; batch: string };

export type RouteDeps = {
  store: LibraryStore;
  blobs: { get(key: string): Promise<Uint8Array | null>; delete(key: string): Promise<void> };
  enqueue: (msg: LibraryMessage) => Promise<void>;
  fetch: Fetcher;
  now: () => Date;
  adminToken?: string;
  apiKey?: string;
  clientId?: string;
  clientSecret?: string;
  budgetToday: () => Promise<Record<string, number>>;
  randomHex: (bytes: number) => string;
  log: (event: Record<string, unknown>) => void;
};

const FILES: Record<string, string> = {
  "audio.flac": "audio/flac",
  "preview.mp3": "audio/mpeg",
  "manifest.nc.json": "application/json",
};

/** Refresh this long before the access token expires (they last 24 hours). */
const REFRESH_EARLY_MS = 10 * 60 * 1000;
const STATE_TTL_MS = 10 * 60 * 1000;

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data, null, 1), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

function esc(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] || c);
}

function page(title: string, body: string, status = 200): Response {
  const doc = `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>${esc(title)}</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:2rem auto;padding:0 16px;color:#222;background:#fafafa}input,button{font:inherit;padding:.5rem;margin:.25rem 0}input{width:100%;box-sizing:border-box}code{background:#eee;padding:0 .2rem}</style></head><body>${body}</body></html>`;
  return new Response(doc, { status, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "referrer-policy": "no-referrer" } });
}

export function safeEqual(a: string, b: string): boolean {
  const aa = new TextEncoder().encode(a);
  const bb = new TextEncoder().encode(b);
  const len = Math.max(aa.length, bb.length);
  let diff = aa.length ^ bb.length;
  for (let i = 0; i < len; i++) diff |= (aa[i] || 0) ^ (bb[i] || 0);
  return diff === 0;
}

function iso(d: Date): string {
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function newBatchId(now: Date, randomHex: (n: number) => string, prefix = "h"): string {
  return `${prefix}-${iso(now).slice(0, 10)}-${randomHex(3)}`;
}

function adminDenied(request: Request, deps: RouteDeps): Response | null {
  const token = deps.adminToken || "";
  if (!token) return json({ error: "admin is off until NC_ADMIN_TOKEN is set" }, 404);
  if (!safeEqual(request.headers.get("Authorization") || "", `Bearer ${token}`)) return json({ error: "admin token required" }, 401);
  return null;
}

/**
 * The harvester account's access token, refreshed when it is about to expire. Null when no account is connected,
 * the client secrets are missing, or Freesound refused the refresh (the harvest then uses previews).
 */
export async function accessToken(deps: Pick<RouteDeps, "store" | "fetch" | "now" | "clientId" | "clientSecret" | "log">): Promise<string | null> {
  const rec = await deps.store.getOAuth();
  if (!rec || rec.broken) return null;
  const nowMs = deps.now().getTime();
  if (rec.expires_at - REFRESH_EARLY_MS > nowMs) return rec.access_token;
  if (!deps.clientId || !deps.clientSecret) {
    deps.log({ event: "library_oauth", detail: "token expired and FREESOUND_CLIENT_ID / FREESOUND_CLIENT_SECRET are not set" });
    return null;
  }
  try {
    const t = await refreshTokens(deps.fetch, deps.clientId, deps.clientSecret, rec.refresh_token, nowMs);
    await deps.store.setOAuth({ ...rec, ...t, refreshed_at: iso(deps.now()) });
    return t.access_token;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    deps.log({ event: "library_oauth", detail: message });
    if (err instanceof OAuthError && (err.status === 400 || err.status === 401)) {
      // Revoked or expired for good: stop trying until someone connects again.
      await deps.store.setOAuth({ ...rec, broken: message });
    }
    return null;
  }
}

async function status(deps: RouteDeps): Promise<Response> {
  const oauth = await deps.store.getOAuth();
  const cfg = await deps.store.getConfig();
  return json({
    secrets: {
      FREESOUND_API_KEY: Boolean(deps.apiKey),
      FREESOUND_CLIENT_ID: Boolean(deps.clientId),
      FREESOUND_CLIENT_SECRET: Boolean(deps.clientSecret),
    },
    freesound_account: oauth
      ? { connected: !oauth.broken, username: oauth.username || null, connected_at: oauth.connected_at, expires_at: new Date(oauth.expires_at).toISOString(), ...(oauth.broken ? { broken: oauth.broken } : {}) }
      : { connected: false },
    quality: oauth && !oauth.broken ? "original" : "preview",
    enabled: cfg.enabled,
    caps: { daily_api_cap: cfg.daily_api_cap, daily_download_cap: cfg.daily_download_cap, daily_preview_cap: cfg.daily_preview_cap, max_candidates: cfg.max_candidates, max_pending: cfg.max_pending },
    used_today: await deps.budgetToday(),
    candidates: await deps.store.countCandidates(),
    batches: (await deps.store.listBatches(5)).map((b) => ({ id: b.id, state: b.state, started_at: b.started_at, picked: b.picked.length, skipped: b.skipped.length, qualified: b.qualified ?? null, stop_reason: b.stop_reason ?? null })),
  });
}

async function candidates(url: URL, deps: RouteDeps, base: string): Promise<Response> {
  const state = url.searchParams.get("state") || "pending";
  const valid = ["raw", "pending", "approved", "rejected", "failed", "all"];
  if (!valid.includes(state)) return json({ error: `state must be one of ${valid.join(", ")}` }, 400);
  const limit = Math.min(500, Math.max(1, Number(url.searchParams.get("limit")) || 200));
  const rows = (await deps.store.listCandidates(state === "all" ? undefined : (state as "pending"))).slice(0, limit);
  return json({
    state,
    count: rows.length,
    candidates: rows.map((c) => ({
      id: c.id,
      state: c.state,
      batch: c.batch,
      quality: c.quality,
      query: c.query,
      ...(c.reason ? { reason: c.reason } : {}),
      ...(c.note ? { note: c.note } : {}),
      manifest: c.manifest ?? null,
      files: c.state === "pending" || c.state === "approved"
        ? {
            audio: `${base}/api/library/admin/candidates/${c.id}/audio.flac`,
            preview: `${base}/api/library/admin/candidates/${c.id}/preview.mp3`,
            manifest: `${base}/api/library/admin/candidates/${c.id}/manifest.nc.json`,
          }
        : null,
    })),
  });
}

async function decisions(request: Request, deps: RouteDeps): Promise<Response> {
  let body: { decisions?: Record<string, { status?: string; at?: string; note?: string }> };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return json({ error: "body must be JSON: {\"decisions\": {\"<id>\": {\"status\": \"approved\" | \"rejected\"}}}" }, 400);
  }
  const out: Record<string, string[]> = { approved: [], rejected: [], pending: [], unknown: [], not_reviewable: [] };
  for (const [id, d] of Object.entries(body.decisions || {})) {
    const status = d?.status;
    if (status !== "approved" && status !== "rejected" && status !== "pending") continue;
    const at = typeof d.at === "string" && d.at ? d.at : iso(deps.now());
    const c = await deps.store.decide(id, status, at, typeof d.note === "string" ? d.note.slice(0, 500) : undefined);
    if (!c) out.unknown.push(id);
    else if (c.state !== status) out.not_reviewable.push(id);
    else {
      out[status].push(id);
      if (status === "rejected") {
        for (const name of Object.keys(FILES)) await deps.blobs.delete(`candidates/${id}/${name}`);
      }
    }
  }
  return json(out);
}

async function connectForm(): Promise<Response> {
  return page("Connect Freesound", `<h1>Connect the Library's Freesound account</h1>
<p>This lets the nightly Library harvest download original files (lossless) instead of MP3 previews. You do this once,
signed in to Freesound as the NetComposer harvester account.</p>
<form method="post" action="/api/library/freesound/connect"><label>Admin token (NC_ADMIN_TOKEN)<br><input type="password" name="token" autocomplete="off" required></label>
<button type="submit">Continue to Freesound</button></form>`);
}

async function connectStart(request: Request, deps: RouteDeps): Promise<Response> {
  const wantsJson = Boolean(request.headers.get("Authorization"));
  let supplied = "";
  if (wantsJson) supplied = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  else {
    try {
      supplied = String((await request.formData()).get("token") || "");
    } catch {
      supplied = "";
    }
  }
  const token = deps.adminToken || "";
  if (!token) return page("Not available", "<p>Admin is off until NC_ADMIN_TOKEN is set.</p>", 404);
  if (!safeEqual(supplied.trim(), token)) {
    return wantsJson ? json({ error: "admin token required" }, 401) : page("Wrong token", '<p>That admin token is not right. <a href="/api/library/freesound/connect">Try again</a>.</p>', 401);
  }
  if (!deps.clientId || !deps.clientSecret) {
    const msg = "FREESOUND_CLIENT_ID and FREESOUND_CLIENT_SECRET are not set on the Worker yet. Add them as GitHub secrets and run Deploy Perform.";
    return wantsJson ? json({ error: msg }, 503) : page("Not configured", `<p>${esc(msg)}</p>`, 503);
  }
  const state = deps.randomHex(24);
  await deps.store.createOAuthState(state, deps.now().getTime(), STATE_TTL_MS);
  const target = authorizeUrl(deps.clientId, state);
  if (wantsJson) return json({ authorize_url: target, expires_in_s: STATE_TTL_MS / 1000 });
  return new Response(null, { status: 303, headers: { Location: target, "cache-control": "no-store", "referrer-policy": "no-referrer" } });
}

async function callback(url: URL, deps: RouteDeps): Promise<Response> {
  const state = url.searchParams.get("state") || "";
  const okState = await deps.store.consumeOAuthState(state, deps.now().getTime());
  if (url.searchParams.get("error")) {
    return page("Not connected", `<p>Freesound said: ${esc(url.searchParams.get("error"))}. Nothing changed. <a href="/api/library/freesound/connect">Start again</a>.</p>`, 400);
  }
  if (!okState) return page("Link expired", '<p>This link expired or was already used. <a href="/api/library/freesound/connect">Start again</a>.</p>', 400);
  const code = url.searchParams.get("code") || "";
  if (!code || !deps.clientId || !deps.clientSecret) return page("Not connected", "<p>Freesound did not send a code.</p>", 400);
  const nowMs = deps.now().getTime();
  let tokens;
  try {
    tokens = await exchangeCode(deps.fetch, deps.clientId, deps.clientSecret, code, nowMs);
  } catch (err) {
    deps.log({ event: "library_oauth", detail: err instanceof Error ? err.message : String(err) });
    return page("Not connected", `<p>${esc(err instanceof Error ? err.message : "Freesound refused the code.")} <a href="/api/library/freesound/connect">Start again</a>.</p>`, 502);
  }
  let username: string | undefined;
  try {
    const me = await new FreesoundClient({ fetch: deps.fetch, apiKey: deps.apiKey || "", take: async () => true, sleep: async () => {} }).me(tokens.access_token);
    username = me?.username;
  } catch {
    username = undefined;
  }
  const rec: OAuthRecord = { ...tokens, connected_at: iso(deps.now()), ...(username ? { username } : {}) };
  await deps.store.setOAuth(rec);
  deps.log({ event: "library_oauth", detail: "connected", username: username || null });
  return page("Connected", `<h1>Connected</h1><p>The Library harvester now uses the Freesound account <strong>${esc(username || "(name unknown)")}</strong>.
The nightly harvest downloads original files from now on. You can close this tab.</p>`);
}

/** Library routes, or null when the path is not one of them. */
export async function libraryRoute(request: Request, deps: RouteDeps, base: string): Promise<Response | null> {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/$/, "");
  if (!path.startsWith("/api/library/")) return null;
  const method = request.method;
  if (path === "/api/library/freesound/connect") {
    if (method === "GET") return connectForm();
    if (method === "POST") return connectStart(request, deps);
  }
  if (path === "/api/library/freesound/callback" && method === "GET") return callback(url, deps);
  if (!path.startsWith("/api/library/admin/")) return json({ error: "not found" }, 404);
  const denied = adminDenied(request, deps);
  if (denied) return denied;
  if (path === "/api/library/admin/status" && method === "GET") return status(deps);
  if (path === "/api/library/admin/config") {
    if (method === "GET") return json(await deps.store.getConfig());
    if (method === "PUT" || method === "POST") {
      let body: unknown;
      try {
        body = await request.json();
      } catch {
        return json({ error: "body must be JSON" }, 400);
      }
      return json(await deps.store.setConfig(body));
    }
  }
  if (path === "/api/library/admin/harvest" && method === "POST") {
    const batch = newBatchId(deps.now(), deps.randomHex, "m");
    await deps.enqueue({ id: batch, kind: "library-harvest", batch });
    return json({ queued: true, batch, status: `${base}/api/library/admin/batches/${batch}` }, 202);
  }
  if (path === "/api/library/admin/qualify" && method === "POST") {
    const batch = url.searchParams.get("batch") || newBatchId(deps.now(), deps.randomHex, "q");
    await deps.enqueue({ id: batch, kind: "library-qualify", batch });
    return json({ queued: true, batch }, 202);
  }
  if (path === "/api/library/admin/candidates" && method === "GET") return candidates(url, deps, base);
  let m = path.match(/^\/api\/library\/admin\/candidates\/([a-z0-9-]{1,80})\/([a-z0-9.]{1,20})$/);
  if (m && method === "GET") {
    const type = FILES[m[2]];
    if (!type) return json({ error: "not found" }, 404);
    const bytes = await deps.blobs.get(`candidates/${m[1]}/${m[2]}`);
    if (!bytes) return json({ error: "not found" }, 404);
    return new Response(bytes, { headers: { "content-type": type, "cache-control": "no-store" } });
  }
  if (path === "/api/library/admin/decisions" && method === "POST") return decisions(request, deps);
  if (path === "/api/library/admin/batches" && method === "GET") return json({ batches: await deps.store.listBatches(30) });
  m = path.match(/^\/api\/library\/admin\/batches\/([a-z0-9-]{1,40})$/);
  if (m && method === "GET") {
    const b = await deps.store.getBatch(m[1]);
    return b ? json(b) : json({ error: "not found" }, 404);
  }
  if (path === "/api/library/admin/freesound/disconnect" && method === "POST") {
    await deps.store.setOAuth(null);
    return json({ connected: false });
  }
  return json({ error: "not found" }, 404);
}
