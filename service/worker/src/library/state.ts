/**
 * Library harvest state, kept in one Durable Object (LibraryDO) through its key-value storage. Pure: it takes any
 * KV with the Durable Object storage shape, so tests run it on a Map.
 *
 *   config                 harvest settings (normalizeConfig fills the rest)
 *   seen:<source>          every source sound ever looked at, and what became of it (never harvested twice)
 *   sha:<sha256>           fingerprint -> candidate id (duplicates are rejected)
 *   cand:<id>              a candidate: raw (harvested), pending (qualified, waiting for Peter), approved, rejected, failed
 *   batch:<id>             one night's log: queries, picks, every skip with its reason
 *   oauth                  the harvester account's Freesound tokens
 *   oauth_state:<state>    one-time OAuth2 state, 10 minutes
 *   lock:<name>            a run in progress
 */
import { normalizeConfig, type LibraryConfig } from "./config.ts";

export interface KV {
  get<T = unknown>(key: string): Promise<T | undefined>;
  put<T>(key: string, value: T): Promise<void>;
  delete(key: string): Promise<boolean>;
  list<T = unknown>(options: { prefix: string; limit?: number }): Promise<Map<string, T>>;
}

export type SeenState = "harvested" | "pending" | "approved" | "rejected" | "failed" | "skipped";
export type SeenRow = { id?: string; state: SeenState; reason?: string; at: string; batch?: string };

export type CandidateState = "raw" | "pending" | "approved" | "rejected" | "failed";
export type Candidate = {
  id: string;
  source_key: string;
  batch: string;
  state: CandidateState;
  quality: "original" | "preview";
  raw_key?: string;
  ext: string;
  query: string;
  role_hint: string;
  sound: {
    id: number;
    name: string;
    tags: string[];
    username: string;
    url: string;
    license: string;
    license_spdx: string;
    license_url: string;
    license_label: string;
    license_read_at: string;
    type?: string;
    pack?: string | null;
  };
  reason?: string;
  sha256?: string;
  manifest?: Record<string, unknown>;
  note?: string;
  decided_at?: string;
  updated_at: string;
};

export type BatchSkip = { source: string; name?: string; reason: string };
export type BatchQuery = { q: string; role: string; requests: number; seen: number; picked: number; error?: string };
export type BatchLog = {
  id: string;
  state: string;
  started_at: string;
  finished_at?: string;
  quality?: string;
  queries: BatchQuery[];
  picked: string[];
  skipped: BatchSkip[];
  requests: { api: number; download: number; preview: number };
  stop_reason?: string;
  qualified?: number;
  failed?: { id: string; reason: string }[];
};

export type OAuthRecord = {
  access_token: string;
  refresh_token: string;
  expires_at: number;
  scope?: string;
  username?: string;
  connected_at: string;
  refreshed_at?: string;
  broken?: string;
};

export interface LibraryStore {
  getConfig(): Promise<LibraryConfig>;
  setConfig(raw: unknown): Promise<LibraryConfig>;
  seenMany(keys: string[]): Promise<Record<string, SeenRow>>;
  markSeen(key: string, row: SeenRow): Promise<void>;
  claimSha(sha: string, id: string): Promise<string | null>;
  putCandidate(c: Candidate): Promise<void>;
  getCandidate(id: string): Promise<Candidate | null>;
  listCandidates(state?: CandidateState): Promise<Candidate[]>;
  countCandidates(): Promise<Record<string, number>>;
  decide(id: string, status: "approved" | "rejected" | "pending", at: string, note?: string): Promise<Candidate | null>;
  putBatch(b: BatchLog): Promise<void>;
  getBatch(id: string): Promise<BatchLog | null>;
  listBatches(limit: number): Promise<BatchLog[]>;
  getOAuth(): Promise<OAuthRecord | null>;
  setOAuth(rec: OAuthRecord | null): Promise<void>;
  createOAuthState(state: string, nowMs: number, ttlMs: number): Promise<void>;
  consumeOAuthState(state: string, nowMs: number): Promise<boolean>;
  acquireLock(name: string, nowMs: number, ttlMs: number): Promise<boolean>;
  releaseLock(name: string): Promise<void>;
}

export class LibraryState implements LibraryStore {
  private kv: KV;

  constructor(kv: KV) {
    this.kv = kv;
  }

  async getConfig(): Promise<LibraryConfig> {
    return normalizeConfig(await this.kv.get("config"));
  }

  async setConfig(raw: unknown): Promise<LibraryConfig> {
    const merged = { ...((await this.kv.get<Record<string, unknown>>("config")) || {}), ...((raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>) };
    const cfg = normalizeConfig(merged);
    await this.kv.put("config", cfg);
    return cfg;
  }

  async seenMany(keys: string[]): Promise<Record<string, SeenRow>> {
    const out: Record<string, SeenRow> = {};
    for (const key of keys) {
      const row = await this.kv.get<SeenRow>(`seen:${key}`);
      if (row) out[key] = row;
    }
    return out;
  }

  async markSeen(key: string, row: SeenRow): Promise<void> {
    const old = await this.kv.get<SeenRow>(`seen:${key}`);
    // A person's decision is final: nothing downgrades a rejection or an approval.
    if (old && (old.state === "rejected" || old.state === "approved") && row.state !== old.state) return;
    await this.kv.put(`seen:${key}`, row);
  }

  /** Claim a fingerprint for a candidate. Returns the id that already holds it, or null when the claim is new. */
  async claimSha(sha: string, id: string): Promise<string | null> {
    const owner = await this.kv.get<string>(`sha:${sha}`);
    if (owner && owner !== id) return owner;
    if (!owner) await this.kv.put(`sha:${sha}`, id);
    return null;
  }

  async putCandidate(c: Candidate): Promise<void> {
    await this.kv.put(`cand:${c.id}`, c);
  }

  async getCandidate(id: string): Promise<Candidate | null> {
    return (await this.kv.get<Candidate>(`cand:${id}`)) ?? null;
  }

  async listCandidates(state?: CandidateState): Promise<Candidate[]> {
    const rows = [...(await this.kv.list<Candidate>({ prefix: "cand:" })).values()];
    return rows.filter((c) => !state || c.state === state).sort((a, b) => (a.batch + a.id).localeCompare(b.batch + b.id));
  }

  async countCandidates(): Promise<Record<string, number>> {
    const out: Record<string, number> = {};
    for (const c of (await this.kv.list<Candidate>({ prefix: "cand:" })).values()) out[c.state] = (out[c.state] || 0) + 1;
    return out;
  }

  async decide(id: string, status: "approved" | "rejected" | "pending", at: string, note?: string): Promise<Candidate | null> {
    const c = await this.getCandidate(id);
    if (!c) return null;
    if (c.state === "raw" || c.state === "failed") return c;
    const next: Candidate = { ...c, state: status, decided_at: at, updated_at: at, ...(note ? { note } : {}) };
    await this.putCandidate(next);
    if (status !== "pending") {
      await this.kv.put(`seen:${c.source_key}`, { id, state: status, at, batch: c.batch, ...(note ? { reason: note } : {}) } satisfies SeenRow);
    }
    return next;
  }

  async putBatch(b: BatchLog): Promise<void> {
    await this.kv.put(`batch:${b.id}`, b);
  }

  async getBatch(id: string): Promise<BatchLog | null> {
    return (await this.kv.get<BatchLog>(`batch:${id}`)) ?? null;
  }

  async listBatches(limit: number): Promise<BatchLog[]> {
    const rows = [...(await this.kv.list<BatchLog>({ prefix: "batch:" })).values()];
    return rows.sort((a, b) => b.started_at.localeCompare(a.started_at)).slice(0, limit);
  }

  async getOAuth(): Promise<OAuthRecord | null> {
    return (await this.kv.get<OAuthRecord>("oauth")) ?? null;
  }

  async setOAuth(rec: OAuthRecord | null): Promise<void> {
    if (rec) await this.kv.put("oauth", rec);
    else await this.kv.delete("oauth");
  }

  async createOAuthState(state: string, nowMs: number, ttlMs: number): Promise<void> {
    // Old states are swept here, so the table stays a handful of rows.
    for (const [key, exp] of await this.kv.list<number>({ prefix: "oauth_state:" })) {
      if (exp < nowMs) await this.kv.delete(key);
    }
    await this.kv.put(`oauth_state:${state}`, nowMs + ttlMs);
  }

  async consumeOAuthState(state: string, nowMs: number): Promise<boolean> {
    if (!state) return false;
    const exp = await this.kv.get<number>(`oauth_state:${state}`);
    if (exp === undefined) return false;
    await this.kv.delete(`oauth_state:${state}`);
    return exp >= nowMs;
  }

  async acquireLock(name: string, nowMs: number, ttlMs: number): Promise<boolean> {
    const until = await this.kv.get<number>(`lock:${name}`);
    if (until !== undefined && until > nowMs) return false;
    await this.kv.put(`lock:${name}`, nowMs + ttlMs);
    return true;
  }

  async releaseLock(name: string): Promise<void> {
    await this.kv.delete(`lock:${name}`);
  }
}

/** A Map with the Durable Object storage shape, for tests and local runs. */
export class MemoryKV implements KV {
  data = new Map<string, unknown>();

  async get<T = unknown>(key: string): Promise<T | undefined> {
    const v = this.data.get(key);
    return v === undefined ? undefined : (structuredClone(v) as T);
  }

  async put<T>(key: string, value: T): Promise<void> {
    this.data.set(key, structuredClone(value));
  }

  async delete(key: string): Promise<boolean> {
    return this.data.delete(key);
  }

  async list<T = unknown>(options: { prefix: string; limit?: number }): Promise<Map<string, T>> {
    const out = new Map<string, T>();
    for (const key of [...this.data.keys()].sort()) {
      if (!key.startsWith(options.prefix)) continue;
      out.set(key, structuredClone(this.data.get(key)) as T);
      if (options.limit && out.size >= options.limit) break;
    }
    return out;
  }
}
