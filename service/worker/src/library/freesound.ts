/**
 * Freesound APIv2 for the Library harvester.
 *
 * Search and previews use the API key (FREESOUND_API_KEY, sent as `Authorization: Token ...`, never in a URL).
 * Original files need OAuth2 for one harvester account (FREESOUND_CLIENT_ID / FREESOUND_CLIENT_SECRET; see
 * oauth* below). Every request is counted against a daily cap (BudgetDO) and paced under Freesound's per-minute
 * limits: 60 a minute for the API, 30 a minute for original downloads.
 *
 * Terms (https://freesound.org/docs/api/terms_of_use.html): non-commercial use; no replicating Freesound (small
 * batches, a person approves every sound); credit creators; one API key only.
 */
import type { LibraryConfig } from "./config.ts";

export const FREESOUND_API = "https://freesound.org/apiv2";
export const SEARCH_FIELDS = "id,name,tags,username,license,url,previews,duration,type,samplerate,channels,filesize,pack";
/** Original downloads larger than this are skipped (a one-shot under 3 s is far smaller). */
export const MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024;
/** One request every 1.1 s stays under 60 a minute; downloads every 2.1 s stay under 30 a minute. */
export const API_GAP_MS = 1100;
export const DOWNLOAD_GAP_MS = 2100;
export const PREVIEW_GAP_MS = 250;

export type FsSound = {
  id: number;
  name: string;
  tags: string[];
  username: string;
  license: string;
  url: string;
  previews?: Record<string, string>;
  duration: number;
  type?: string;
  samplerate?: number;
  channels?: number;
  filesize?: number;
  pack?: string | null;
};

export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>;
export type BudgetKind = "api" | "download" | "preview";

export class BudgetExhausted extends Error {
  kind: BudgetKind;
  constructor(kind: BudgetKind) {
    super(`daily Freesound ${kind} budget used up`);
    this.kind = kind;
  }
}

export class RateLimited extends Error {
  detail: string;
  constructor(detail: string) {
    super(`Freesound rate limit: ${detail}`);
    this.detail = detail;
  }
}

export type SearchPage = { results: FsSound[]; next: boolean; count: number };

export function searchUrl(q: string, page: number, cfg: Pick<LibraryConfig, "page_size" | "min_duration_s" | "max_duration_s" | "sort">, licenseFilter = true): string {
  const filters = [`duration:[${cfg.min_duration_s} TO ${cfg.max_duration_s}]`];
  // Narrows the page to the licenses we accept. Each sound's license URL is still checked (classifyLicense).
  if (licenseFilter) filters.push('license:("Creative Commons 0" OR "Attribution")');
  const params = new URLSearchParams({
    query: q,
    filter: filters.join(" "),
    fields: SEARCH_FIELDS,
    page_size: String(cfg.page_size),
    page: String(page),
    sort: cfg.sort,
  });
  return `${FREESOUND_API}/search/?${params.toString()}`;
}

export function parseSound(raw: unknown): FsSound | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const id = Number(r.id);
  if (!Number.isInteger(id) || id <= 0) return null;
  const previews = r.previews && typeof r.previews === "object" ? (r.previews as Record<string, string>) : undefined;
  return {
    id,
    name: typeof r.name === "string" ? r.name : `Freesound ${id}`,
    tags: Array.isArray(r.tags) ? r.tags.filter((t): t is string => typeof t === "string") : [],
    username: typeof r.username === "string" ? r.username : "",
    license: typeof r.license === "string" ? r.license : "",
    url: typeof r.url === "string" ? r.url : `https://freesound.org/s/${id}/`,
    previews,
    duration: Number(r.duration) || 0,
    type: typeof r.type === "string" ? r.type.toLowerCase() : undefined,
    samplerate: Number(r.samplerate) || undefined,
    channels: Number(r.channels) || undefined,
    filesize: Number(r.filesize) || undefined,
    pack: typeof r.pack === "string" ? r.pack : null,
  };
}

export type ClientOptions = {
  fetch: Fetcher;
  apiKey: string;
  /** Count one request against today's cap. False: the cap is reached. */
  take: (kind: BudgetKind) => Promise<boolean>;
  sleep: (ms: number) => Promise<void>;
  now?: () => number;
  apiGapMs?: number;
  downloadGapMs?: number;
  previewGapMs?: number;
};

export class FreesoundClient {
  private opts: ClientOptions;
  private last: Record<BudgetKind, number> = { api: 0, download: 0, preview: 0 };
  requests: Record<BudgetKind, number> = { api: 0, download: 0, preview: 0 };

  constructor(opts: ClientOptions) {
    this.opts = opts;
  }

  private gap(kind: BudgetKind): number {
    if (kind === "api") return this.opts.apiGapMs ?? API_GAP_MS;
    if (kind === "download") return this.opts.downloadGapMs ?? DOWNLOAD_GAP_MS;
    return this.opts.previewGapMs ?? PREVIEW_GAP_MS;
  }

  /** Budget, then pace, then fetch. 429 stops the run: the caller ends the night's harvest. */
  async request(kind: BudgetKind, url: string, init?: RequestInit): Promise<Response> {
    if (!(await this.opts.take(kind))) throw new BudgetExhausted(kind);
    const now = this.opts.now ? this.opts.now() : Date.now();
    const wait = this.last[kind] + this.gap(kind) - now;
    if (wait > 0) await this.opts.sleep(wait);
    this.last[kind] = this.opts.now ? this.opts.now() : Date.now();
    this.requests[kind] += 1;
    const res = await this.opts.fetch(url, init);
    if (res.status === 429) {
      let detail = "";
      try {
        detail = String(((await res.json()) as { detail?: unknown }).detail || "");
      } catch {
        detail = "";
      }
      throw new RateLimited(detail || "429 Too Many Requests");
    }
    return res;
  }

  private tokenHeaders(): Record<string, string> {
    return { Authorization: `Token ${this.opts.apiKey}`, Accept: "application/json" };
  }

  async search(q: string, page: number, cfg: Pick<LibraryConfig, "page_size" | "min_duration_s" | "max_duration_s" | "sort">, licenseFilter = true): Promise<SearchPage | { status: number; error: string }> {
    const res = await this.request("api", searchUrl(q, page, cfg, licenseFilter), { headers: this.tokenHeaders() });
    if (!res.ok) return { status: res.status, error: (await res.text()).slice(0, 300) };
    const doc = (await res.json()) as { results?: unknown[]; next?: unknown; count?: unknown };
    const results = (Array.isArray(doc.results) ? doc.results : []).map(parseSound).filter((s): s is FsSound => s !== null);
    return { results, next: Boolean(doc.next), count: Number(doc.count) || results.length };
  }

  /** The HQ MP3 preview: no OAuth needed. */
  async preview(sound: FsSound): Promise<{ bytes: Uint8Array; ext: string } | { status: number; error: string }> {
    const url = sound.previews?.["preview-hq-mp3"] || sound.previews?.["preview_hq_mp3"];
    if (!url) return { status: 0, error: "no HQ MP3 preview" };
    // Previews are public CDN files: no key on these requests.
    const res = await this.request("preview", url);
    if (!res.ok) return { status: res.status, error: `preview HTTP ${res.status}` };
    return { bytes: new Uint8Array(await res.arrayBuffer()), ext: "mp3" };
  }

  /** The original file (OAuth2 Bearer token). */
  async original(sound: FsSound, accessToken: string): Promise<{ bytes: Uint8Array; ext: string } | { status: number; error: string }> {
    if (sound.filesize && sound.filesize > MAX_DOWNLOAD_BYTES) return { status: 0, error: `original is larger than ${MAX_DOWNLOAD_BYTES} bytes` };
    const res = await this.request("download", `${FREESOUND_API}/sounds/${sound.id}/download/`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (!res.ok) return { status: res.status, error: `download HTTP ${res.status}` };
    const bytes = new Uint8Array(await res.arrayBuffer());
    if (bytes.byteLength > MAX_DOWNLOAD_BYTES) return { status: 0, error: "original is too large" };
    return { bytes, ext: originalExt(sound) };
  }

  async me(accessToken: string): Promise<{ username: string } | null> {
    const res = await this.request("api", `${FREESOUND_API}/me/`, { headers: { Authorization: `Bearer ${accessToken}` } });
    if (!res.ok) return null;
    const doc = (await res.json()) as { username?: unknown };
    return typeof doc.username === "string" ? { username: doc.username } : null;
  }
}

export function originalExt(sound: FsSound): string {
  const t = (sound.type || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  if (["wav", "aif", "aiff", "flac", "ogg", "mp3", "m4a"].includes(t)) return t;
  return "wav";
}

// ---- OAuth2 (https://freesound.org/docs/api/authentication.html) ----

export type TokenSet = { access_token: string; refresh_token: string; expires_at: number; scope?: string };

export function authorizeUrl(clientId: string, state: string): string {
  const params = new URLSearchParams({ client_id: clientId, response_type: "code", state });
  return `${FREESOUND_API}/oauth2/authorize/?${params.toString()}`;
}

export class OAuthError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function tokenRequest(fetcher: Fetcher, form: Record<string, string>, now: number): Promise<TokenSet> {
  const res = await fetcher(`${FREESOUND_API}/oauth2/access_token/`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded", Accept: "application/json" },
    body: new URLSearchParams(form).toString(),
  });
  if (!res.ok) {
    let detail = "";
    try {
      const doc = (await res.json()) as { error?: unknown; error_description?: unknown; detail?: unknown };
      detail = String(doc.error_description || doc.error || doc.detail || "");
    } catch {
      detail = "";
    }
    throw new OAuthError(`Freesound token request failed (${res.status}${detail ? `: ${detail}` : ""})`, res.status);
  }
  const doc = (await res.json()) as { access_token?: unknown; refresh_token?: unknown; expires_in?: unknown; scope?: unknown };
  if (typeof doc.access_token !== "string" || typeof doc.refresh_token !== "string") {
    throw new OAuthError("Freesound token response had no tokens", 502);
  }
  const expiresIn = Number(doc.expires_in) || 86399;
  return {
    access_token: doc.access_token,
    refresh_token: doc.refresh_token,
    expires_at: now + expiresIn * 1000,
    ...(typeof doc.scope === "string" ? { scope: doc.scope } : {}),
  };
}

export function exchangeCode(fetcher: Fetcher, clientId: string, clientSecret: string, code: string, now: number): Promise<TokenSet> {
  return tokenRequest(fetcher, { client_id: clientId, client_secret: clientSecret, grant_type: "authorization_code", code }, now);
}

export function refreshTokens(fetcher: Fetcher, clientId: string, clientSecret: string, refreshToken: string, now: number): Promise<TokenSet> {
  return tokenRequest(fetcher, { client_id: clientId, client_secret: clientSecret, grant_type: "refresh_token", refresh_token: refreshToken }, now);
}
