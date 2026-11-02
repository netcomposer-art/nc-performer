/**
 * Library harvest settings. Defaults live here; an admin can replace them (PUT /api/library/admin/config), and
 * normalizeConfig clamps whatever is stored so a typo cannot blow the Freesound budget.
 *
 * Freesound limits (APIv2): 60 requests a minute and 2,000 a day; original downloads 30 a minute and 500 a day.
 * The nightly budget stays well under both.
 */

export type HarvestQuery = { q: string; role: string };

export type LibraryConfig = {
  enabled: boolean;
  queries: HarvestQuery[];
  /** New candidates kept per query per night. */
  per_query: number;
  /** New candidates per night across all queries (a small batch: Peter listens to every one). */
  max_candidates: number;
  /** Search pages read per query per night (seen sounds are skipped, so later nights go deeper). */
  max_pages: number;
  page_size: number;
  min_duration_s: number;
  max_duration_s: number;
  /** Candidates from one Freesound pack per night, so a batch is not one person's kit. */
  per_pack: number;
  sort: string;
  /** Freesound API requests per UTC day (search, me, token refresh). Hard ceiling 1,800 of Freesound's 2,000. */
  daily_api_cap: number;
  /** Original-file downloads per UTC day. Hard ceiling 400 of Freesound's 500. */
  daily_download_cap: number;
  /** Preview MP3 fetches per UTC day (CDN, not API, but still polite). */
  daily_preview_cap: number;
  /** No harvest while this many candidates already wait for review. */
  max_pending: number;
};

export const ROLES = ["kick", "snare", "clap", "hat", "tom", "perc", "cymbal", "fx", "tonal"];

/** Electronic drum sounds first: the seed shelf (VCSL) is acoustic and orchestral. */
export const DEFAULT_QUERIES: HarvestQuery[] = [
  { q: "808 kick", role: "kick" },
  { q: "909 kick", role: "kick" },
  { q: "electronic kick drum one shot", role: "kick" },
  { q: "808 closed hihat", role: "hat" },
  { q: "909 closed hihat", role: "hat" },
  { q: "808 open hihat", role: "hat" },
  { q: "909 open hihat", role: "hat" },
  { q: "808 clap", role: "clap" },
  { q: "909 clap", role: "clap" },
  { q: "electronic clap one shot", role: "clap" },
  { q: "808 snare", role: "snare" },
  { q: "909 snare", role: "snare" },
  { q: "electronic snare one shot", role: "snare" },
  { q: "808 rimshot", role: "perc" },
  { q: "909 rimshot", role: "perc" },
  { q: "808 cowbell", role: "perc" },
  { q: "808 clave", role: "perc" },
  { q: "808 conga", role: "perc" },
  { q: "electronic percussion one shot", role: "perc" },
  { q: "808 tom", role: "tom" },
  { q: "909 tom", role: "tom" },
  { q: "909 crash", role: "cymbal" },
  { q: "909 ride", role: "cymbal" },
];

export const DEFAULT_CONFIG: LibraryConfig = {
  enabled: true,
  queries: DEFAULT_QUERIES,
  per_query: 2,
  max_candidates: 40,
  max_pages: 2,
  page_size: 50,
  min_duration_s: 0.05,
  max_duration_s: 3,
  per_pack: 2,
  sort: "score",
  daily_api_cap: 500,
  daily_download_cap: 100,
  daily_preview_cap: 200,
  max_pending: 200,
};

const SORTS = new Set(["score", "duration_asc", "duration_desc", "created_desc", "created_asc", "downloads_desc", "downloads_asc", "rating_desc", "rating_asc"]);

function num(raw: unknown, fallback: number, lo: number, hi: number): number {
  const n = typeof raw === "number" ? raw : typeof raw === "string" && raw.trim() ? Number(raw) : NaN;
  if (!Number.isFinite(n)) return fallback;
  return Math.min(hi, Math.max(lo, n));
}

export function normalizeQueries(raw: unknown): HarvestQuery[] {
  if (!Array.isArray(raw)) return DEFAULT_QUERIES;
  const out: HarvestQuery[] = [];
  for (const item of raw) {
    const q = typeof item === "string" ? item : item && typeof item === "object" ? (item as { q?: unknown }).q : "";
    const role = item && typeof item === "object" ? (item as { role?: unknown }).role : undefined;
    const text = typeof q === "string" ? q.trim().slice(0, 120) : "";
    if (!text) continue;
    out.push({ q: text, role: typeof role === "string" && ROLES.includes(role) ? role : guessRole(text) });
    if (out.length >= 60) break;
  }
  return out.length ? out : DEFAULT_QUERIES;
}

/** The role a plain query names, for a query list given as strings. */
export function guessRole(q: string): string {
  const low = q.toLowerCase();
  if (/clap/.test(low)) return "clap";
  if (/hi-?hat|\bhh\b|\bhat/.test(low)) return "hat";
  if (/snare/.test(low)) return "snare";
  if (/kick|bass ?drum/.test(low)) return "kick";
  if (/\btom/.test(low)) return "tom";
  if (/crash|ride|cymbal/.test(low)) return "cymbal";
  return "perc";
}

export function normalizeConfig(raw: unknown): LibraryConfig {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const d = DEFAULT_CONFIG;
  const minDur = num(r.min_duration_s, d.min_duration_s, 0, 5);
  return {
    enabled: typeof r.enabled === "boolean" ? r.enabled : d.enabled,
    queries: "queries" in r ? normalizeQueries(r.queries) : d.queries,
    per_query: Math.round(num(r.per_query, d.per_query, 0, 20)),
    max_candidates: Math.round(num(r.max_candidates, d.max_candidates, 0, 150)),
    max_pages: Math.round(num(r.max_pages, d.max_pages, 1, 5)),
    page_size: Math.round(num(r.page_size, d.page_size, 5, 150)),
    min_duration_s: minDur,
    max_duration_s: num(r.max_duration_s, d.max_duration_s, Math.max(minDur, 0.1), 10),
    per_pack: Math.round(num(r.per_pack, d.per_pack, 1, 20)),
    sort: typeof r.sort === "string" && SORTS.has(r.sort) ? r.sort : d.sort,
    daily_api_cap: Math.round(num(r.daily_api_cap, d.daily_api_cap, 0, 1800)),
    daily_download_cap: Math.round(num(r.daily_download_cap, d.daily_download_cap, 0, 400)),
    daily_preview_cap: Math.round(num(r.daily_preview_cap, d.daily_preview_cap, 0, 1000)),
    max_pending: Math.round(num(r.max_pending, d.max_pending, 1, 1000)),
  };
}

/** Start each night at a different query, so a full batch is not always the first roles in the list. */
export function rotateQueries(queries: HarvestQuery[], dayNumber: number): HarvestQuery[] {
  if (!queries.length) return queries;
  const start = ((dayNumber % queries.length) + queries.length) % queries.length;
  return [...queries.slice(start), ...queries.slice(0, start)];
}
