/**
 * Test doubles for the Library harvester: a recorded Freesound (fixtures/*.json), an in-memory R2 and budget, and a
 * fake clock. Named *.test.ts so tsc's Workers build leaves it out like the other tests; it holds no tests itself.
 */
import { readFileSync } from "node:fs";
import { FreesoundClient, type BudgetKind } from "./freesound.ts";
import type { BlobStore, Known } from "./harvest.ts";
import { knownFrom } from "./harvest.ts";
import { LibraryState, MemoryKV } from "./state.ts";

export function fixture(name: string): unknown {
  return JSON.parse(readFileSync(new URL(`./fixtures/${name}`, import.meta.url), "utf8"));
}

export type Call = { url: string; method: string; headers: Record<string, string>; body: string };

/** A recorded Freesound: search pages by query, previews and originals as small byte strings, OAuth endpoints. */
export class FakeFreesound {
  calls: Call[] = [];
  searches: Record<string, unknown[]> = {
    "808 kick": [fixture("search-808-kick-p1.json"), fixture("search-808-kick-p2.json")],
    "909 closed hihat": [fixture("search-909-closed-hihat.json")],
  };
  /** Status to answer for a URL substring (e.g. rate limit or a refused download). */
  fail: { match: string; status: number; body?: unknown }[] = [];
  rejectLicenseFilter = false;
  tokens = 0;

  fetch = async (input: string, init?: RequestInit): Promise<Response> => {
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((v, k) => (headers[k] = v));
    this.calls.push({ url: input, method: init?.method || "GET", headers, body: typeof init?.body === "string" ? init.body : "" });
    for (const f of this.fail) {
      if (input.includes(f.match)) return new Response(JSON.stringify(f.body ?? { detail: "failed" }), { status: f.status });
    }
    const url = new URL(input);
    if (url.pathname === "/apiv2/search/") {
      if (this.rejectLicenseFilter && (url.searchParams.get("filter") || "").includes("license:")) {
        return new Response(JSON.stringify({ detail: "Invalid filter" }), { status: 400 });
      }
      const pages = this.searches[url.searchParams.get("query") || ""] || [];
      const page = Number(url.searchParams.get("page") || "1");
      const doc = pages[page - 1] || { count: 0, next: null, results: [] };
      return new Response(JSON.stringify(doc), { headers: { "content-type": "application/json" } });
    }
    if (url.hostname === "cdn.freesound.org") return new Response(`ID3-preview-${url.pathname}`);
    let m = url.pathname.match(/^\/apiv2\/sounds\/(\d+)\/download\/$/);
    if (m) return new Response(`RIFF-original-${m[1]}`);
    if (url.pathname === "/apiv2/oauth2/access_token/") {
      this.tokens += 1;
      const doc = fixture("token.json") as Record<string, unknown>;
      return new Response(JSON.stringify({ ...doc, access_token: `acc-${this.tokens}`, refresh_token: `ref-${this.tokens}` }));
    }
    if (url.pathname === "/apiv2/me/") return new Response(JSON.stringify(fixture("me.json")));
    m = null;
    return new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
  };
}

export class MemoryBlobs implements BlobStore {
  data = new Map<string, { body: Uint8Array; type: string }>();
  async put(key: string, body: Uint8Array | string, contentType: string): Promise<void> {
    this.data.set(key, { body: typeof body === "string" ? new TextEncoder().encode(body) : body, type: contentType });
  }
  async get(key: string): Promise<Uint8Array | null> {
    return this.data.get(key)?.body ?? null;
  }
  async delete(key: string): Promise<void> {
    this.data.delete(key);
  }
  text(key: string): string {
    const b = this.data.get(key)?.body;
    return b ? new TextDecoder().decode(b) : "";
  }
}

export class FakeClock {
  t: number;
  slept: number[] = [];
  constructor(iso = "2026-11-03T04:15:00Z") {
    this.t = Date.parse(iso);
  }
  now = () => this.t;
  date = () => new Date(this.t);
  sleep = async (ms: number) => {
    this.slept.push(ms);
    this.t += ms;
  };
}

export class FakeBudget {
  used: Record<BudgetKind, number> = { api: 0, download: 0, preview: 0 };
  caps: Record<BudgetKind, number> = { api: 500, download: 100, preview: 200 };
  take = async (kind: BudgetKind) => {
    if (this.used[kind] >= this.caps[kind]) return false;
    this.used[kind] += 1;
    return true;
  };
}

export function world() {
  const fs = new FakeFreesound();
  const clock = new FakeClock();
  const budget = new FakeBudget();
  const store = new LibraryState(new MemoryKV());
  const blobs = new MemoryBlobs();
  const logs: Record<string, unknown>[] = [];
  const known: Known = knownFrom(fixture("musicmatter-library.json"), fixture("musicmatter-rejected.json"));
  const client = () => new FreesoundClient({ fetch: fs.fetch, apiKey: "key-123", take: budget.take, sleep: clock.sleep, now: clock.now });
  return { fs, clock, budget, store, blobs, logs, known, client, log: (e: Record<string, unknown>) => logs.push(e) };
}
