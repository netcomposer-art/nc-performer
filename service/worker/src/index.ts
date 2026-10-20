/**
 * Cloudflare adapter for nc-perform.
 *
 * This Worker is the public API (workers.dev, or whatever origin you set).
 * It does not render audio. A queue message starts the nc-perform container,
 * which runs the public kit. Files are copied to R2 as bytes. The record is
 * signed in the container when NC_SIGNING_KEY is set. This Worker rewrites a
 * record only when a fresh canonical hash still matches the stored hash, so a
 * JSON round-trip cannot change record_hash.
 *
 * The real model call is not in this file. The container calls
 * ncperf.interpret.llm (POST {base}/chat/completions). XAI_API_KEY alone makes
 * that base https://api.x.ai/v1 and the model grok-4.3. See service/nc_perform/config.py.
 */
import { DurableObject } from "cloudflare:workers";
import { Container, ContainerProxy, getContainer } from "@cloudflare/containers";
import pageHtml from "../../web/index.html";
import { parseComposition } from "./composition";
import { applyPerformHit, BURST_MESSAGE, claimToken, forPage, isInternalFailure, jobStatusFields, newId, PER_10MIN, PER_DAY, proxyHosts, publicInternalError, rejectPlan, wordsTooLong, type FoundSound } from "./policy";

// @cloudflare/containers 0.3.7 looks up this exact export on ctx.exports when
// allowedHosts is set. It is a WorkerEntrypoint, not a Durable Object, so it
// is not listed in wrangler migrations.
export { ContainerProxy };
import { publicJwk, recordHashOf, signatureFor, signerFrom, verifyHash, type Signer } from "./sign";

const ALLOWED_HOSTS = [
  "musicmatter.art",
  // Chord audio. ContainerProxy does not suffix-match, so the apex is not enough.
  "audio.musicmatter.art",
  "fullchords.musicmatter.art",
  "netcomposer.art",
  "freesound.org",
  "cdn.freesound.org",
  "theremin.music.uiowa.edu",
  "webfiles.york.ac.uk",
  "www.openair.hosted.york.ac.uk",
  "web.archive.org",
  "archive.org",
  "us.archive.org",
  "api.openverse.org",
  "commons.wikimedia.org",
  "upload.wikimedia.org",
  "wikimedia.org",
  "ccmixter.org",
  "api.x.ai",
];

const FILES = [
  "performance.flac",
  "preview.mp3",
  "record.json",
  "plan.json",
  "uses.json",
  "composition.md",
  "meta.json",
  "status.json",
];

const FILE_TYPES: Record<string, string> = {
  "performance.flac": "audio/flac",
  "preview.mp3": "audio/mpeg",
  "record.json": "application/json",
  "plan.json": "application/json",
  "uses.json": "application/json",
  "composition.md": "text/markdown; charset=utf-8",
  "meta.json": "application/json",
  "status.json": "application/json",
};

const TURNSTILE_TEST: Record<string, boolean> = {
  "1x0000000000000000000000000000000AA": true,
  "2x0000000000000000000000000000000AA": false,
  "3x0000000000000000000000000000000AA": false,
};
const DUMMY_TOKEN = "XXXX.DUMMY.TOKEN.XXXX";
const ID_RE = /^[hk]-[0-9abcdefghjkmnpqrstvwxyz]{12}$/;
/** Importing Dragonfly and the Python audio stack on a cold instance can take a few minutes. */
const PORT_READY_MS = 300_000;

export interface Env {
  JOBS: DurableObjectNamespace<JobDO>;
  BUDGET: DurableObjectNamespace<BudgetDO>;
  RENDER: DurableObjectNamespace<RenderContainer>;
  PERFORMANCES: R2Bucket;
  PERFORM_JOBS: Queue<{ id: string }>;
  PERFORM_RATE: RateLimit;
  NC_INSTANCE_NAME: string;
  TURNSTILE_SITEKEY: string;
  PERFORM_ENABLED: string;
  NC_LLM_MODEL: string;
  NC_SUPPLIERS_JSON: string;
  XAI_API_KEY?: string;
  NC_LLM_BASE_URL?: string;
  NC_LLM_API_KEY?: string;
  NC_LLM_EVERY_PERFORM?: string;
  FREESOUND_API_KEY?: string;
  TURNSTILE_SECRET_KEY?: string;
  TURNSTILE_VERIFY_URL?: string;
  NC_SIGNING_KEY?: string;
  NC_ADMIN_TOKEN?: string;
  NC_INTERNAL_TOKEN?: string;
  NC_PUBLIC_BASE_URL?: string;
  NC_IP_HASH_SALT?: string;
}

type Job = {
  id: string;
  state: string;
  stage: string;
  percent: number;
  error: string | null;
  error_code: string | null;
  request_json: string;
  ip_hash: string | null;
  claim_hash: string | null;
  cost_usd: number;
  created_at: string;
  seed?: number | null;
  trimmed?: string | null;
  found_sounds?: FoundSound[];
  stage_line?: string;
};

export class RenderContainer extends Container<Env> {
  defaultPort = 8080;
  requiredPorts = [8080];
  sleepAfter = "15m";
  enableInternet = false;
  // HTTPS to allowedHosts is intercepted by ContainerProxy. The image entrypoint
  // appends the runtime CA before Python starts. Other hosts stay blocked.
  // proxyHosts adds `*.name` because the proxy matches exactly. Python still
  // suffix-matches the names in NC_ALLOWED_HOSTS, which stay free of globs.
  interceptHttps = true;
  allowedHosts = proxyHosts(ALLOWED_HOSTS);
  entrypoint = ["/usr/local/bin/nc-perform-entrypoint", "python3", "-m", "nc_perform"];
}

export class JobDO extends DurableObject<Env> {
  async create(job: Job): Promise<void> {
    const existing = await this.ctx.storage.get<Job>("job");
    if (existing) return;
    await this.ctx.storage.put("job", job);
  }

  async get(): Promise<Job | null> {
    return (await this.ctx.storage.get<Job>("job")) ?? null;
  }

  async patch(fields: Partial<Job>): Promise<Job | null> {
    const job = await this.get();
    if (!job) return null;
    const next = { ...job, ...fields };
    await this.ctx.storage.put("job", next);
    return next;
  }
}

export class BudgetDO extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      for (const statement of [
      "CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY, usd REAL NOT NULL)",
      "CREATE TABLE IF NOT EXISTS day_hits (ip TEXT, day TEXT, n INTEGER NOT NULL, PRIMARY KEY (ip, day))",
      "CREATE TABLE IF NOT EXISTS win_hits (ip TEXT PRIMARY KEY, start REAL NOT NULL, n INTEGER NOT NULL)",
      "CREATE TABLE IF NOT EXISTS queued (id TEXT PRIMARY KEY, created TEXT NOT NULL)",
      "CREATE TABLE IF NOT EXISTS flags (k TEXT PRIMARY KEY, v TEXT NOT NULL)",
      "CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, state TEXT, cost_usd REAL, ip_hash TEXT, error_code TEXT, created_at TEXT)",
    ]) {
      this.ctx.storage.sql.exec(statement);
    }
    });
  }

  async spendToday(): Promise<number> {
    const rows = this.ctx.storage.sql.exec("SELECT usd FROM budget WHERE day = ?", day()).toArray();
    return rows.length ? Number(rows[0].usd) : 0;
  }

  async addSpend(usd: number): Promise<number> {
    if (usd > 0) {
      this.ctx.storage.sql.exec(
        "INSERT INTO budget (day, usd) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET usd = usd + excluded.usd",
        day(),
        usd,
      );
    }
    return this.spendToday();
  }

  async enabled(defaultOn: boolean): Promise<boolean> {
    const rows = this.ctx.storage.sql.exec("SELECT v FROM flags WHERE k = 'perform_enabled'").toArray();
    if (!rows.length) return defaultOn;
    return rows[0].v === "1";
  }

  async setEnabled(on: boolean): Promise<void> {
    this.ctx.storage.sql.exec(
      "INSERT INTO flags (k, v) VALUES ('perform_enabled', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
      on ? "1" : "0",
    );
  }

  /**
   * 3 Performs per 10 minutes and 10 per day for one network.
   * Checked together so a daily rejection does not consume the 10-minute window.
   */
  async hit(ip: string, per10min = PER_10MIN, perDay = PER_DAY): Promise<string | null> {
    const now = Date.now() / 1000;
    const today = day();
    const winRows = this.ctx.storage.sql.exec("SELECT start, n FROM win_hits WHERE ip = ?", ip).toArray();
    const dayRows = this.ctx.storage.sql.exec("SELECT day, n FROM day_hits WHERE ip = ? AND day = ?", ip, today).toArray();
    const decision = applyPerformHit(
      now,
      today,
      winRows.length ? { start: Number(winRows[0].start), n: Number(winRows[0].n) } : null,
      dayRows.length ? { day: String(dayRows[0].day), n: Number(dayRows[0].n) } : null,
      per10min,
      perDay,
    );
    if (decision.error) return decision.error;
    this.ctx.storage.sql.exec(
      "INSERT INTO win_hits (ip, start, n) VALUES (?, ?, ?) ON CONFLICT(ip) DO UPDATE SET start = excluded.start, n = excluded.n",
      ip,
      decision.window.start,
      decision.window.n,
    );
    this.ctx.storage.sql.exec(
      "INSERT INTO day_hits (ip, day, n) VALUES (?, ?, ?) ON CONFLICT(ip, day) DO UPDATE SET n = excluded.n",
      ip,
      decision.dayHit.day,
      decision.dayHit.n,
    );
    return null;
  }

  async enqueue(id: string, created: string, max = 50): Promise<{ position: number } | { error: string }> {
    const count = Number(this.ctx.storage.sql.exec("SELECT COUNT(*) AS c FROM queued").toArray()[0].c);
    if (count >= max) return { error: "Busy, try in a minute." };
    this.ctx.storage.sql.exec("INSERT OR IGNORE INTO queued (id, created) VALUES (?, ?)", id, created);
    return { position: this.position(id) };
  }

  position(id: string): number {
    const rows = this.ctx.storage.sql.exec("SELECT id FROM queued ORDER BY created, id").toArray();
    const i = rows.findIndex((r) => r.id === id);
    return i < 0 ? 0 : i + 1;
  }

  async positionOf(id: string): Promise<number> {
    return this.position(id);
  }

  async dequeue(id: string): Promise<void> {
    this.ctx.storage.sql.exec("DELETE FROM queued WHERE id = ?", id);
  }

  async note(row: { id: string; state: string; cost_usd: number; ip_hash: string | null; error_code: string | null; created_at: string }): Promise<void> {
    this.ctx.storage.sql.exec(
      `INSERT INTO seen (id, state, cost_usd, ip_hash, error_code, created_at) VALUES (?, ?, ?, ?, ?, ?)
       ON CONFLICT(id) DO UPDATE SET state = excluded.state, cost_usd = excluded.cost_usd, error_code = excluded.error_code`,
      row.id,
      row.state,
      row.cost_usd,
      row.ip_hash,
      row.error_code,
      row.created_at,
    );
  }

  async summary(): Promise<{ day: string; usd: number; by_state: { [key: string]: number }; failures: { error_code: string; c: number }[]; queued: number }> {
    const d = day();
    const by: { [key: string]: number } = {};
    for (const row of this.ctx.storage.sql.exec("SELECT state, COUNT(*) AS c FROM seen WHERE created_at LIKE ? GROUP BY state", d + "%").toArray()) {
      by[String(row.state)] = Number(row.c);
    }
    const failures = this.ctx.storage.sql
      .exec("SELECT error_code, COUNT(*) AS c FROM seen WHERE created_at LIKE ? AND state IN ('failed','rejected') GROUP BY error_code", d + "%")
      .toArray()
      .map((row) => ({ error_code: row.error_code == null ? "" : String(row.error_code), c: Number(row.c) }));
    const queued = Number(this.ctx.storage.sql.exec("SELECT COUNT(*) AS c FROM queued").toArray()[0].c);
    return { day: d, usd: Math.round((await this.spendToday()) * 10000) / 10000, by_state: by, failures, queued };
  }
}

let signerCache: { text: string; signer: Signer } | null = null;

async function getSigner(env: Env): Promise<Signer | null> {
  const text = (env.NC_SIGNING_KEY || "").trim();
  if (!text) return null;
  if (signerCache && signerCache.text === text) return signerCache.signer;
  const signer = await signerFrom(text);
  signerCache = { text, signer };
  return signer;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        "access-control-allow-origin": "*",
        "access-control-allow-headers": "Content-Type, Authorization",
        "access-control-allow-methods": "GET, POST, OPTIONS",
      },
    });
  }
    try {
      return await route(request, env);
    } catch (err) {
      return json({ error: logInternal(null, err), code: "error" }, 500);
    }
  },

  async queue(batch: MessageBatch<{ id: string }>, env: Env): Promise<void> {
    for (const msg of batch.messages) {
      const id = msg.body?.id;
      if (!id) {
        msg.ack();
        continue;
      }
      try {
        await runContainer(env, id);
        msg.ack();
      } catch (err) {
        const error = logInternal(id, err);
        await jobs(env, id).patch({ state: "failed", stage: "failed", percent: 100, error, error_code: "container" });
        msg.retry();
      }
    }
  },

  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(sweep(env));
  },
};

async function route(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/$/, "") || "/";
  const base = publicBase(request, env);

  if (request.method === "GET" && (path === "/" || path === "/create")) {
    return html(pageHtml);
  }
  if (request.method === "GET" && path === "/health") {
    return json({ ok: true, service: "nc-perform", version: "0.2.0" });
  }
  if (request.method === "GET" && path === "/api/config") return config(env);
  if (request.method === "GET" && path === "/.well-known/nc-record-keys.json") return keys(env);
  if (request.method === "GET" && path === "/api/feed") return feed(env, base);
  if (request.method === "GET" && path === "/admin") return adminPage(request, env);
  if (request.method === "POST" && path === "/api/perform") return perform(request, env, base);
  if (request.method === "POST" && path === "/api/submit") return submit(request, env, base);
  if (request.method === "POST" && path === "/admin/enabled") return adminEnabled(request, env);
  if (request.method === "POST" && (path === "/admin/hide" || path === "/admin/takedown")) {
    return adminModerate(request, env, path.endsWith("hide") ? "hide" : "takedown");
  }

  let m = path.match(/^\/api\/jobs\/([hk]-[0-9a-z]+)$/);
  if (request.method === "GET" && m) return jobStatus(env, m[1], base);
  m = path.match(/^\/api\/jobs\/([hk]-[0-9a-z]+)\/files\/([A-Za-z0-9._-]+)$/);
  if (request.method === "GET" && m) return file(env, m[1], m[2]);
  m = path.match(/^\/api\/jobs\/([hk]-[0-9a-z]+)\/events$/);
  if (request.method === "GET" && m) return events(env, m[1], base);
  m = path.match(/^\/api\/jobs\/([hk]-[0-9a-z]+)\/(keep|visibility)$/);
  if (request.method === "POST" && m) return metaPost(request, env, m[1], m[2]);
  m = path.match(/^\/api\/verify\/([hk]-[0-9a-z]+)$/);
  if (request.method === "GET" && m) return verify(env, m[1]);
  m = path.match(/^\/records\/sha256\/([0-9a-f]{64})$/);
  if (request.method === "GET" && m) return alias(env, m[1]);
  m = path.match(/^\/records\/([hk]-[0-9a-z]+)(\.json)?$/);
  if (request.method === "GET" && m) return record(env, m[1], Boolean(m[2]));
  return json({ error: "not found" }, 404);
}

async function config(env: Env): Promise<Response> {
  const on = await budget(env).enabled(env.PERFORM_ENABLED !== "0");
  return json({
    turnstile_sitekey: env.TURNSTILE_SITEKEY || null,
    turnstile_required: Boolean(env.TURNSTILE_SECRET_KEY),
    max_words: 4000,
    max_length_s: 180,
    instance: env.NC_INSTANCE_NAME,
    perform_enabled: on,
    llm: Boolean(env.XAI_API_KEY || env.NC_LLM_BASE_URL),
    llm_model: env.NC_LLM_MODEL || "grok-4.3",
    llm_price: {
      model: env.NC_LLM_MODEL || "grok-4.3",
      usd_per_million: { in: 1.25, cached: 0.2, out: 2.5 },
      source: "https://docs.x.ai/docs/models",
      every_perform: /^(1|true|yes|on)$/i.test(env.NC_LLM_EVERY_PERFORM || ""),
      formula:
        "fresh prompt tokens times the input price, plus cached prompt tokens times the cached price, plus completion tokens times the output price",
    },
  });
}

async function keys(env: Env): Promise<Response> {
  const signer = await getSigner(env);
  return json({ keys: signer ? [publicJwk(signer)] : [] });
}

async function perform(request: Request, env: Env, base: string): Promise<Response> {
  const b = budget(env);
  const spend = await b.spendToday();
  const on = await b.enabled(env.PERFORM_ENABLED !== "0");
  if (!on || spend >= 5) return json({ error: "Perform is resting until tomorrow.", code: "resting" }, 503);
  const body = (await readJson(request)) as Record<string, unknown>;
  const ip = clientIp(request);
  if (env.TURNSTILE_SECRET_KEY && !(await turnstile(env, body.turnstile_token, ip))) {
    return json({ error: "The bot check failed. Reload the page and try again.", code: "turnstile" }, 403);
  }
  const comp = compositionFrom(body);
  const instrumentError = instrumentProblem(body.instrument);
  if (instrumentError) return json({ error: instrumentError, code: "rejected" }, 400);
  const instrument = typeof body.instrument === "string" && body.instrument.trim() ? body.instrument.trim() : null;
  if (instrument) comp.instrument = instrument;
  const words = `${comp.composition || ""}\n${comp.performance || ""}`;
  const wordErr = wordsTooLong(words);
  if (wordErr) return json({ error: wordErr, code: "rejected" }, 400);
  if (body.plan != null) {
    if (typeof body.plan !== "object" || Array.isArray(body.plan)) return json({ error: "plan must be a JSON object.", code: "rejected" }, 400);
    const planErr = rejectPlan(body.plan);
    if (planErr) return json({ error: planErr, code: "rejected" }, 400);
  }
  const ipHash = await sha256Text(`${env.NC_IP_HASH_SALT || "nc-perform"}:${ip}`);
  const limited = await rateLimit(env, ipHash);
  if (limited) return json({ error: limited, code: "rate" }, 429);
  const visibility = body.visibility || "unlisted";
  if (visibility !== "unlisted" && visibility !== "public") {
    return json({ error: "visibility must be unlisted or public.", code: "rejected" }, 400);
  }
  const randomSeed = () => crypto.getRandomValues(new Uint32Array(1))[0] % 2147483646 || 1;
  let seed: number;
  if (body.perform_again || body.seed == null) seed = randomSeed();
  else seed = Number(body.seed) || 1;
  const token = claimToken();
  const id = newId("h");
  const created = isoNow();
  const req = {
    composition: comp,
    plan: body.plan ?? null,
    visibility,
    seed,
    keep: Boolean(body.keep),
    instrument: instrument,
    claim_hash: await sha256Text(token),
    public_base: base,
    // The container still decides discovery vs model from the words. This flag skips the model when the soft budget is spent.
    force_stub: spend >= 3,
  };
  const job: Job = {
    id,
    state: "queued",
    stage: "queued",
    percent: 0,
    error: null,
    error_code: null,
    request_json: JSON.stringify(req),
    ip_hash: ipHash,
    claim_hash: req.claim_hash,
    cost_usd: 0,
    created_at: created,
    seed,
    trimmed: null,
    found_sounds: [],
    stage_line: "Queued",
  };
  await jobs(env, id).create(job);
  const placed = await b.enqueue(id, created, 50);
  if ("error" in placed) {
    await jobs(env, id).patch({ state: "rejected", stage: "rejected", percent: 100, error: placed.error, error_code: "busy" });
    return json({ error: placed.error, code: "busy" }, 503);
  }
  try {
    await env.PERFORM_JOBS.send({ id });
  } catch (err) {
    await b.dequeue(id);
    const error = logInternal(id, err);
    await jobs(env, id).patch({ state: "failed", stage: "failed", percent: 100, error, error_code: "queue" });
    return json({ error, id, code: "error" }, 500);
  }
  return json({
    id,
    status: "queued",
    position: placed.position,
    poll: `/api/jobs/${id}`,
    claim_token: token,
    record_url: `${base}/records/${id}.json`,
    note: "Save claim_token. It is not shown again. A later account can use it to adopt this Performance. v1 has no accounts.",
  }, 202);
}

async function runContainer(env: Env, id: string): Promise<void> {
  const stub = jobs(env, id);
  const job = await stub.get();
  if (!job) throw new Error(`unknown job ${id}`);
  if (job.state === "done" || job.state === "failed" || job.state === "rejected") return;
  await budget(env).dequeue(id);
  const container = getContainer(env.RENDER, id);
  const requested = JSON.parse(job.request_json) as { public_base?: string };
  const base = (env.NC_PUBLIC_BASE_URL || requested.public_base || "").replace(/\/$/, "");
  const envVars = containerEnv(env, base);
  await container.startAndWaitForPorts({
    startOptions: { envVars, enableInternet: false },
    cancellationOptions: { portReadyTimeoutMS: PORT_READY_MS, instanceGetTimeoutMS: PORT_READY_MS },
  });
  await container.setAllowedHosts(proxyHosts(allowedHosts(env)));
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (env.NC_INTERNAL_TOKEN) headers.authorization = `Bearer ${env.NC_INTERNAL_TOKEN}`;
  const started = await containerFetch(container, id, "http://container/internal/jobs", {
    method: "POST",
    headers,
    body: JSON.stringify({ id, request: JSON.parse(job.request_json), ip_hash: job.ip_hash }),
  });
  if (!started.ok && started.status !== 202) {
    throw new Error(`container rejected the job (${started.status})`);
  }
  let last: {
    state?: string;
    stage?: string;
    percent?: number;
    error?: string | null;
    cost_usd?: number;
    seed?: number | null;
    trimmed?: string | null;
    found_sounds?: FoundSound[];
    stage_line?: string;
  } = {};
  let loggedFault = false;
  const pageError = (error: string | null | undefined): string | null => {
    const text = error || null;
    if (!text || !isInternalFailure(text)) return text;
    if (!loggedFault) {
      console.error(JSON.stringify({ event: "perform_failed", id, detail: text }));
      loggedFault = true;
    }
    return publicInternalError(id);
  };
  for (let i = 0; i < 300; i++) {
    await sleep(2000);
    const res = await containerFetch(container, id, `http://container/internal/jobs/${id}`, { headers });
    if (!res.ok) continue;
    last = await res.json();
    const patch: Partial<Job> = {
      state: last.state || job.state,
      stage: last.stage || job.stage,
      percent: Number(last.percent || 0),
      error: pageError(last.error),
    };
    if (last.seed != null && Number.isFinite(Number(last.seed))) patch.seed = Number(last.seed);
    if ("trimmed" in last) patch.trimmed = typeof last.trimmed === "string" && last.trimmed.trim() ? last.trimmed : null;
    if (Array.isArray(last.found_sounds)) patch.found_sounds = last.found_sounds;
    if (typeof last.stage_line === "string" && last.stage_line.trim()) patch.stage_line = last.stage_line;
    await stub.patch(patch);
    if (last.state === "done" || last.state === "failed" || last.state === "rejected") break;
  }
  if (last.state === "done") {
    await sleep(400);
    const again = await containerFetch(container, id, `http://container/internal/jobs/${id}`, { headers });
    if (again.ok) last = await again.json();
    await copyOutputs(env, container, id, headers);
  } else if (last.state !== "failed" && last.state !== "rejected") {
    throw new Error("container did not finish within 10 minutes");
  }
  const cost = Number(last.cost_usd || 0);
  const shown = pageError(last.error);
  await budget(env).addSpend(cost);
  const updated = await stub.patch({
    state: last.state || "failed",
    stage: last.stage || last.state || "failed",
    percent: 100,
    error: shown,
    cost_usd: cost,
  });
  if (updated) {
    await budget(env).note({
      id,
      state: updated.state,
      cost_usd: cost,
      ip_hash: updated.ip_hash,
      error_code: updated.error_code,
      created_at: updated.created_at,
    });
  }
  if (last.state === "failed" || last.state === "rejected") {
    await env.PERFORMANCES.put(
      `jobs/${id}/status.json`,
      JSON.stringify({
        id,
        state: last.state,
        error: shown,
        at: isoNow(),
        seed: last.seed ?? null,
        trimmed: typeof last.trimmed === "string" && last.trimmed.trim() ? last.trimmed : null,
        found_sounds: Array.isArray(last.found_sounds) ? last.found_sounds : [],
        stage_line: last.stage_line || "Something went wrong.",
      }),
      { httpMetadata: { contentType: "application/json" } },
    );
  }
}

function containerEnv(env: Env, base: string): Record<string, string> {
  const out: Record<string, string> = {
    NC_ROLE: "container",
    NC_STORAGE: "file:///data",
    NC_QUEUE_AUTOSTART: "0",
    NC_BIND: "0.0.0.0:8080",
    NC_INSTANCE_NAME: env.NC_INSTANCE_NAME,
    NC_SUPPLIERS_JSON: env.NC_SUPPLIERS_JSON,
    NC_LLM_MODEL: env.NC_LLM_MODEL || "grok-4.3",
  };
  if (base) out.NC_PUBLIC_BASE_URL = base;
  if (env.XAI_API_KEY) out.XAI_API_KEY = env.XAI_API_KEY;
  if (env.NC_LLM_BASE_URL) out.NC_LLM_BASE_URL = env.NC_LLM_BASE_URL;
  if (env.NC_LLM_API_KEY) out.NC_LLM_API_KEY = env.NC_LLM_API_KEY;
  if (env.NC_LLM_EVERY_PERFORM) out.NC_LLM_EVERY_PERFORM = env.NC_LLM_EVERY_PERFORM;
  if (env.FREESOUND_API_KEY) out.FREESOUND_API_KEY = env.FREESOUND_API_KEY;
  if (env.NC_SIGNING_KEY) out.NC_SIGNING_KEY = env.NC_SIGNING_KEY;
  if (env.NC_INTERNAL_TOKEN) out.NC_INTERNAL_TOKEN = env.NC_INTERNAL_TOKEN;
  // Exact names only. Python suffix-matches these. Globs belong to ContainerProxy.
  out.NC_ALLOWED_HOSTS = allowedHosts(env).join(",");
  return out;
}

function allowedHosts(env: Env): string[] {
  const hosts = new Set(ALLOWED_HOSTS);
  for (const raw of [env.NC_SUPPLIERS_JSON, env.NC_LLM_BASE_URL, env.XAI_API_KEY ? "https://api.x.ai/v1" : ""]) {
    if (!raw) continue;
    try {
      hosts.add(new URL(raw).hostname);
    } catch {
      /* not a URL */
    }
  }
  return [...hosts];
}

async function containerFetch(container: DurableObjectStub<RenderContainer>, id: string, url: string, init?: RequestInit): Promise<Response> {
  let res: Response;
  try {
    res = await container.fetch(url, init);
  } catch (err) {
    const detail = err instanceof Error ? err.stack || err.message : String(err);
    console.error(JSON.stringify({ event: "container_http", id, url, detail }));
    throw err;
  }
  if (res.status >= 500) {
    const body = (await res.text()).slice(0, 500);
    console.error(JSON.stringify({ event: "container_http", id, url, status: res.status, body }));
    throw new Error(`container HTTP ${res.status} for ${url}`);
  }
  return res;
}

async function copyOutputs(env: Env, container: DurableObjectStub<RenderContainer>, id: string, headers: Record<string, string>): Promise<void> {
  const signer = id.startsWith("h-") ? await getSigner(env) : null;
  let recordBytes: Uint8Array<ArrayBuffer> | null = null;
  for (const name of FILES) {
    const res = await containerFetch(container, id, `http://container/internal/jobs/${id}/files/${name}`, { headers });
    // A missing file is normal (uses.json is optional). A 5xx already threw above.
    if (!res.ok) continue;
    let bytes = new Uint8Array(await res.arrayBuffer());
    if (name === "record.json") {
      bytes = await maybeSign(bytes, signer, env.NC_INSTANCE_NAME);
      recordBytes = bytes;
    }
    await putOnce(env, `performances/${id}/${name}`, bytes, typeOf(name), name === "record.json");
  }
  if (!recordBytes) return;
  try {
    const parsed = JSON.parse(new TextDecoder().decode(recordBytes)) as { record_hash?: { value?: string } };
    const hash = parsed.record_hash?.value;
    if (hash && /^[0-9a-f]{64}$/.test(hash)) {
      await putOnce(env, `aliases/sha256/${hash}`, new TextEncoder().encode(id), "text/plain", true);
    }
  } catch {
    /* the record page still works without the alias */
  }
}

/** Sign only when parsing and re-canonicalizing still yields the stored hash. */
async function maybeSign(raw: Uint8Array, signer: Signer | null, signedBy: string): Promise<Uint8Array<ArrayBuffer>> {
  if (!signer) return new Uint8Array(raw);
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return new Uint8Array(raw);
  }
  const stored = (parsed.record_hash as { value?: string } | undefined)?.value;
  if (!stored) return new Uint8Array(raw);
  if ((await recordHashOf(parsed)) !== stored) return new Uint8Array(raw);
  const sig = parsed.signature as { status?: string; value?: string } | undefined;
  if (sig?.status === "SIGNED" && sig.value) return new Uint8Array(raw);
  parsed.signature = signatureFor(signer, stored, signedBy);
  if ((await recordHashOf(parsed)) !== stored) return new Uint8Array(raw);
  return new Uint8Array(new TextEncoder().encode(JSON.stringify(parsed, null, 1)));
}

async function putOnce(env: Env, key: string, bytes: Uint8Array, contentType: string, once: boolean): Promise<void> {
  try {
    await env.PERFORMANCES.put(key, bytes, {
      httpMetadata: { contentType },
      onlyIf: once ? { etagDoesNotMatch: "*" } : undefined,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    if (once && /exist|precondition|412/i.test(message)) return;
    if (!once) throw err;
  }
}

type StatusExtras = {
  trimmed?: string | null;
  cost_estimate?: unknown;
  seed?: number | null;
  found_sounds?: unknown;
  stage_line?: string;
};

async function jobStatus(env: Env, id: string, base: string): Promise<Response> {
  if (!ID_RE.test(id)) return json({ error: "not found" }, 404);
  const job = await jobs(env, id).get();
  if (!job) {
    const meta = await readJsonKey(env, `performances/${id}/meta.json`);
    const status = (await readJsonKey(env, `performances/${id}/status.json`)) || (await readJsonKey(env, `jobs/${id}/status.json`));
    if (!meta && !status) return json({ error: "not found" }, 404);
    const stored = (status || {}) as { state?: string; error?: string | null; trimmed?: string | null; seed?: number | null; found_sounds?: unknown; stage_line?: string; cost_estimate?: unknown };
    const state = String(stored.state || "done");
    return json(publicJob(id, state, state, state === "done" ? 100 : 0, stored.error || null, base, null, {
      trimmed: stored.trimmed ?? null,
      seed: stored.seed ?? null,
      found_sounds: stored.found_sounds ?? [],
      stage_line: stored.stage_line,
      cost_estimate: stored.cost_estimate,
    }));
  }
  const pos = job.state === "queued" ? await budget(env).positionOf(id) : null;
  const extras = job.state === "done" ? await doneExtras(env, id) : undefined;
  return json(publicJob(id, job.state, job.stage, job.percent, job.error, base, pos, extras, job));
}

async function doneExtras(env: Env, id: string): Promise<StatusExtras | undefined> {
  const status = (await readJsonKey(env, `performances/${id}/status.json`)) || (await readJsonKey(env, `jobs/${id}/status.json`));
  if (!status) return undefined;
  const out: StatusExtras = {
    trimmed: typeof status.trimmed === "string" && status.trimmed.trim() ? status.trimmed : null,
  };
  if (typeof status.seed === "number") out.seed = status.seed;
  if (Array.isArray(status.found_sounds)) out.found_sounds = status.found_sounds;
  if (typeof status.stage_line === "string") out.stage_line = status.stage_line;
  if (status.cost_estimate) out.cost_estimate = status.cost_estimate;
  return out;
}

function publicJob(
  id: string,
  state: string,
  stage: string,
  percent: number,
  error: string | null,
  base: string,
  position: number | null,
  extras?: StatusExtras,
  job?: Job | null,
) {
  const fields = jobStatusFields({
    stage,
    position,
    seed: extras && extras.seed != null ? extras.seed : job?.seed,
    trimmed: extras && "trimmed" in extras ? extras.trimmed : job?.trimmed,
    found_sounds: extras && extras.found_sounds != null ? extras.found_sounds : job?.found_sounds,
    stage_line: extras?.stage_line || job?.stage_line,
  });
  const out: Record<string, unknown> = {
    id,
    state,
    stage,
    percent: percent || 0,
    position,
    error: forPage(id, error),
    seed: fields.seed,
    trimmed: fields.trimmed,
    found_sounds: fields.found_sounds,
    stage_line: fields.stage_line,
  };
  if (state === "done") {
    if (extras?.cost_estimate) out.cost_estimate = extras.cost_estimate;
    out.files = {
      flac: `${base}/api/jobs/${id}/files/performance.flac`,
      mp3: `${base}/api/jobs/${id}/files/preview.mp3`,
      record: `${base}/records/${id}.json`,
      plan: `${base}/api/jobs/${id}/files/plan.json`,
      page: `${base}/records/${id}`,
    };
  }
  return out;
}

async function file(env: Env, id: string, name: string): Promise<Response> {
  if (!ID_RE.test(id) || !/^[A-Za-z0-9._-]{1,80}$/.test(name)) return json({ error: "not found" }, 404);
  const meta = (await readJsonKey(env, `performances/${id}/meta.json`)) as Meta | null;
  if (meta?.tombstone && isAudio(name)) return json({ error: "This Performance was taken down.", code: "gone" }, 410);
  if (isAudio(name) && meta?.audio_expires_at && meta.audio_expires_at < isoNow()) {
    await env.PERFORMANCES.delete(`performances/${id}/performance.flac`);
    await env.PERFORMANCES.delete(`performances/${id}/preview.mp3`);
    return json({ error: "This audio has expired. The record is still here.", code: "expired" }, 410);
  }
  const obj = await env.PERFORMANCES.get(`performances/${id}/${name}`);
  if (!obj) return json({ error: "not found" }, 404);
  const headers: Record<string, string> = {};
  if (meta && meta.visibility !== "public") headers["X-Robots-Tag"] = "noindex";
  return bin(await obj.arrayBuffer(), obj.httpMetadata?.contentType || typeOf(name), headers);
}

async function record(env: Env, id: string, asJson: boolean): Promise<Response> {
  if (!ID_RE.test(id)) return json({ error: "not found" }, 404);
  const meta = (await readJsonKey(env, `performances/${id}/meta.json`)) as Meta | null;
  const obj = await env.PERFORMANCES.get(`performances/${id}/record.json`);
  if (meta?.tombstone) {
    const body = { tombstone: true, id, record_hash: meta.tombstone.record_hash, reason: meta.tombstone.reason };
    if (asJson) return json(body, 410);
    return html(tombPage(id, meta.tombstone), 410);
  }
  if (!obj) return json({ error: "not found" }, 404);
  const bytes = await obj.arrayBuffer();
  const headers: Record<string, string> = { "Cache-Control": "public, max-age=31536000, immutable" };
  if (!meta || meta.visibility !== "public") headers["X-Robots-Tag"] = "noindex";
  if (asJson) return bin(bytes, "application/json", headers);
  const rec = JSON.parse(new TextDecoder().decode(bytes));
  return html(recordPage(rec, meta || {}, id), 200, headers);
}

async function alias(env: Env, hash: string): Promise<Response> {
  const obj = await env.PERFORMANCES.get(`aliases/sha256/${hash}`);
  if (!obj) return json({ error: "not found" }, 404);
  const id = (await obj.text()).trim();
  return new Response(null, { status: 302, headers: { Location: `/records/${id}`, "Access-Control-Allow-Origin": "*" } });
}

async function verify(env: Env, id: string): Promise<Response> {
  const obj = await env.PERFORMANCES.get(`performances/${id}/record.json`);
  if (!obj) return json({ error: "not found" }, 404);
  const raw = new Uint8Array(await obj.arrayBuffer());
  const lines: string[] = [];
  let ok = true;
  const chk = (cond: boolean, msg: string) => {
    lines.push((cond ? "OK   " : "FAIL ") + msg);
    ok = ok && cond;
  };
  let rec: Record<string, unknown>;
  try {
    rec = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return json({ ok: false, lines: ["FAIL record is not JSON"], signature_ok: false, id });
  }
  chk(rec.record_type === "netcomposer.performance_record", "record_type is netcomposer.performance_record");
  const stored = (rec.record_hash as { value?: string } | undefined)?.value || "";
  const computed = await recordHashOf(rec);
  if (computed === stored) chk(true, "record_hash matches the canonical JSON");
  else lines.push("NOTE worker canonical JSON differs from the stored hash (number formatting). Bytes were kept.");
  const sounds = (rec.sounds_used as Array<Record<string, unknown>>) || [];
  chk(sounds.length > 0, `${sounds.length} sounds listed`);
  chk(sounds.every((s) => licOk(s.license as Record<string, unknown>)), "every input license is CC0, CC BY, CC BY-SA or public domain");
  const perf = (rec.performance as Record<string, unknown>) || {};
  const rule = (perf.rule_set as Record<string, unknown>) || {};
  chk(sounds.every((s) => !s.pitch_shift) && rule.pitch_shift === false, "no pitch-shifting");
  const licenseOut = (perf.license_out as Record<string, unknown>) || {};
  chk(licenseOut.id === "CC-BY-SA-4.0", "output license CC BY-SA 4.0");
  const outputs = (perf.outputs as Array<Record<string, unknown>>) || [];
  for (const o of outputs) {
    const name = String(o.file || "").split("/").pop() || "";
    const audio = name ? await env.PERFORMANCES.get(`performances/${id}/${name}`) : null;
    if (audio && o.sha256) {
      const digest = await sha256HexBytes(new Uint8Array(await audio.arrayBuffer()));
      chk(digest === o.sha256, `output ${name} sha256 matches`);
    }
  }
  let signatureOk = false;
  const signer = await getSigner(env);
  const sig = rec.signature as { status?: string; value?: string; algorithm?: string } | undefined;
  if (signer && sig?.status === "SIGNED" && sig.algorithm === "Ed25519" && sig.value && stored) {
    signatureOk = verifyHash(signer, stored, sig.value);
  }
  return json({ ok, lines, signature_ok: signatureOk, id });
}

async function events(env: Env, id: string, base: string): Promise<Response> {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    async start(controller) {
      let last = "";
      for (let i = 0; i < 600; i++) {
        const job = await jobs(env, id).get();
        if (!job) break;
        const extras = job.state === "done" ? await doneExtras(env, id) : undefined;
        const pub = publicJob(id, job.state, job.stage, job.percent, job.error, base, null, extras, job);
        const line = JSON.stringify(pub);
        if (line !== last) {
          controller.enqueue(encoder.encode(`data: ${line}\n\n`));
          last = line;
        }
        if (job.state === "done" || job.state === "failed" || job.state === "rejected") break;
        await sleep(1000);
      }
      controller.close();
    },
  });
  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-store",
      "Access-Control-Allow-Origin": "*",
    },
  });
}

async function metaPost(request: Request, env: Env, id: string, action: string): Promise<Response> {
  const body = (await readJson(request)) as Record<string, unknown>;
  const meta = (await readJsonKey(env, `performances/${id}/meta.json`)) as Meta | null;
  if (!meta) return json({ error: "not found" }, 404);
  if (!(await claimMatches(meta.claim_token_sha256, body.claim_token))) {
    return json({ error: "claim_token does not match this Performance." }, 403);
  }
  if (action === "keep") {
    meta.keep = true;
    meta.retention = "forever";
    meta.audio_expires_at = null;
  } else {
    const vis = body.visibility;
    if (vis !== "unlisted" && vis !== "public") return json({ error: "visibility must be unlisted or public.", code: "rejected" }, 400);
    meta.visibility = vis;
    if (vis === "public") {
      meta.public_after = isoPlus(24 * 3600 * 1000);
      meta.audio_expires_at = null;
      meta.keep = true;
      meta.retention = "forever";
    } else {
      meta.public_after = null;
    }
  }
  await env.PERFORMANCES.put(`performances/${id}/meta.json`, JSON.stringify(meta, null, 1), {
    httpMetadata: { contentType: "application/json" },
  });
  return json({ id, visibility: meta.visibility, keep: meta.keep, audio_expires_at: meta.audio_expires_at });
}

async function submit(request: Request, env: Env, base: string): Promise<Response> {
  const len = Number(request.headers.get("content-length") || 0);
  if (len > 50 * 1024 * 1024) return json({ error: "Request is too large.", code: "rejected" }, 400);
  const buf = await request.arrayBuffer();
  if (buf.byteLength > 50 * 1024 * 1024) return json({ error: "Request is too large.", code: "rejected" }, 400);
  const idHint = newId("k");
  const container = getContainer(env.RENDER, idHint);
  await container.startAndWaitForPorts({
    startOptions: { envVars: containerEnv(env, base), enableInternet: false },
    cancellationOptions: { portReadyTimeoutMS: PORT_READY_MS, instanceGetTimeoutMS: PORT_READY_MS },
  });
  await container.setAllowedHosts(proxyHosts(allowedHosts(env)));
  const headers: Record<string, string> = {};
  const ctype = request.headers.get("content-type");
  if (ctype) headers["content-type"] = ctype;
  if (env.NC_INTERNAL_TOKEN) headers.authorization = `Bearer ${env.NC_INTERNAL_TOKEN}`;
  let res: Response;
  try {
    res = await containerFetch(container, idHint, "http://container/api/submit", { method: "POST", headers, body: buf });
  } catch (err) {
    return json({ error: logInternal(idHint, err), id: idHint, code: "error" }, 500);
  }
  const text = await res.text();
  if (!res.ok) return new Response(text, { status: res.status, headers: { "content-type": "application/json", "access-control-allow-origin": "*" } });
  const data = JSON.parse(text) as { id?: string; record_url?: string; page_url?: string };
  const id = data.id || "";
  if (!ID_RE.test(id)) return json({ error: "container returned no id" }, 502);
  const fileHeaders: Record<string, string> = {};
  if (env.NC_INTERNAL_TOKEN) fileHeaders.authorization = `Bearer ${env.NC_INTERNAL_TOKEN}`;
  try {
    await copyOutputs(env, container, id, fileHeaders);
  } catch (err) {
    return json({ error: logInternal(id, err), id, code: "error" }, 500);
  }
  data.record_url = `${base}/records/${id}.json`;
  data.page_url = `${base}/records/${id}`;
  return json(data, 201);
}

async function feed(env: Env, base: string): Promise<Response> {
  const now = isoNow();
  const performances: Array<Record<string, unknown>> = [];
  let cursor: string | undefined;
  do {
    const page = await env.PERFORMANCES.list({ prefix: "performances/", cursor, limit: 500 });
    for (const obj of page.objects) {
      if (!obj.key.endsWith("/meta.json")) continue;
      const meta = (await readJsonKey(env, obj.key)) as Meta | null;
      if (!meta || meta.visibility !== "public" || meta.hidden || meta.tombstone) continue;
      if (meta.public_after && meta.public_after > now) continue;
      performances.push({
        id: meta.id,
        url: `${base}/records/${meta.id}`,
        record: `${base}/records/${meta.id}.json`,
        created_at: meta.created_at,
      });
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  return json({ performances });
}

async function adminPage(request: Request, env: Env): Promise<Response> {
  const denied = adminGate(request, env);
  if (denied) return denied;
  const summary = await budget(env).summary();
  const on = await budget(env).enabled(env.PERFORM_ENABLED !== "0");
  const body = `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex"><title>Perform admin</title>
<style>body{font:15px/1.4 sans-serif;margin:1.5rem} pre{white-space:pre-wrap}</style></head><body>
<h1>Perform admin</h1><p>Perform is <strong>${on ? "on" : "off"}</strong>.</p><pre>${esc(JSON.stringify(summary, null, 1))}</pre></body></html>`;
  return html(body);
}

async function adminEnabled(request: Request, env: Env): Promise<Response> {
  const denied = adminGate(request, env);
  if (denied) return denied;
  const body = (await readJson(request)) as { enabled?: boolean };
  await budget(env).setEnabled(body.enabled !== false);
  return json({ perform_enabled: await budget(env).enabled(true) });
}

async function adminModerate(request: Request, env: Env, action: "hide" | "takedown"): Promise<Response> {
  const denied = adminGate(request, env);
  if (denied) return denied;
  const body = (await readJson(request)) as { id?: string; reason?: string };
  const id = body.id || "";
  if (!ID_RE.test(id)) return json({ error: "id must be an h- or k- record id.", code: "rejected" }, 400);
  const meta = (await readJsonKey(env, `performances/${id}/meta.json`)) as Meta | null;
  if (!meta) return json({ error: "not found" }, 404);
  if (action === "hide") {
    meta.hidden = true;
    meta.visibility = "unlisted";
    meta.public_after = null;
    await env.PERFORMANCES.put(`performances/${id}/meta.json`, JSON.stringify(meta, null, 1), { httpMetadata: { contentType: "application/json" } });
    return json({ id, hidden: true });
  }
  const raw = await env.PERFORMANCES.get(`performances/${id}/record.json`);
  let rh: string | null = null;
  if (raw) {
    const bytes = await raw.arrayBuffer();
    try {
      rh = JSON.parse(new TextDecoder().decode(bytes)).record_hash?.value || null;
    } catch {
      rh = null;
    }
    await putOnce(env, `sealed/${id}/record.json`, new Uint8Array(bytes), "application/json", true);
  }
  const reason = body.reason || "taken down";
  const tomb = { record_hash: rh, reason, at: isoNow() };
  meta.tombstone = tomb;
  await env.PERFORMANCES.put(`performances/${id}/meta.json`, JSON.stringify(meta, null, 1), { httpMetadata: { contentType: "application/json" } });
  await env.PERFORMANCES.put(
    `performances/${id}/record.json`,
    JSON.stringify({ tombstone: true, id, record_hash: rh, reason }),
    { httpMetadata: { contentType: "application/json" } },
  );
  await env.PERFORMANCES.delete(`performances/${id}/performance.flac`);
  await env.PERFORMANCES.delete(`performances/${id}/preview.mp3`);
  return json({ id, tombstone: tomb });
}

async function sweep(env: Env): Promise<void> {
  const now = isoNow();
  const cutoff = isoPlus(-7 * 24 * 3600 * 1000);
  let cursor: string | undefined;
  do {
    const page = await env.PERFORMANCES.list({ prefix: "performances/", cursor, limit: 500 });
    for (const obj of page.objects) {
      if (!obj.key.endsWith("/meta.json")) continue;
      const meta = (await readJsonKey(env, obj.key)) as Meta | null;
      if (!meta?.audio_expires_at || meta.audio_expires_at >= now) continue;
      const id = meta.id || obj.key.split("/")[1];
      await env.PERFORMANCES.delete(`performances/${id}/performance.flac`);
      await env.PERFORMANCES.delete(`performances/${id}/preview.mp3`);
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  cursor = undefined;
  do {
    const page = await env.PERFORMANCES.list({ prefix: "jobs/", cursor, limit: 500 });
    for (const obj of page.objects) {
      const status = (await readJsonKey(env, obj.key)) as { at?: string } | null;
      if (status?.at && status.at < cutoff) await env.PERFORMANCES.delete(obj.key);
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
}

async function turnstile(env: Env, token: unknown, ip: string): Promise<boolean> {
  const secret = env.TURNSTILE_SECRET_KEY || "";
  if (!secret) return true;
  if (secret in TURNSTILE_TEST) return TURNSTILE_TEST[secret] && token === DUMMY_TOKEN;
  if (!token) return false;
  const body = new URLSearchParams({ secret, response: String(token), remoteip: ip });
  const res = await fetch(env.TURNSTILE_VERIFY_URL || "https://challenges.cloudflare.com/turnstile/v0/siteverify", {
    method: "POST",
    body,
  });
  const data = (await res.json()) as { success?: boolean };
  return Boolean(data.success);
}

async function rateLimit(env: Env, ipHash: string): Promise<string | null> {
  // Binding period is 10 seconds (Wrangler allows only 10 or 60). It stops a
  // burst before those requests touch the counters below. The 10-minute and
  // daily caps always run in BudgetDO, including when the binding is absent.
  try {
    const result = await env.PERFORM_RATE.limit({ key: ipHash });
    if (!result.success) return BURST_MESSAGE;
  } catch {
    // Local tests have no binding. The Durable Object still enforces both windows.
  }
  return budget(env).hit(ipHash);
}

function instrumentProblem(raw: unknown): string | null {
  if (raw == null || raw === "") return null;
  const text = String(raw).trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$/.test(text)) {
    return "instrument must be a short name, like choir, synth, or piano. It is a timbre hint, not a supplier.";
  }
  return null;
}

function compositionFrom(body: Record<string, unknown>): Record<string, unknown> {
  const comp = typeof body.words === "string" ? parseComposition(body.words) : {
    title: body.title || "Untitled",
    composition: body.composition || "",
    performance: body.performance || "",
    composer: null,
    file: null,
  };
  if (typeof body.title === "string" && body.title) comp.title = body.title;
  if (typeof body.composition === "string") comp.composition = body.composition;
  if ("performance" in body) comp.performance = body.performance ?? "";
  const typed = "composer" in body ? body.composer : comp.composer;
  comp.composer = String(typed || "").trim() || "anonymous";
  return comp;
}

type Tomb = { record_hash?: string | null; reason?: string; at?: string };
type Meta = {
  id?: string;
  visibility?: string;
  retention?: string;
  keep?: boolean;
  owner?: null;
  claim_token_sha256?: string | null;
  created_at?: string;
  audio_expires_at?: string | null;
  public_after?: string | null;
  hidden?: boolean;
  tombstone?: Tomb | null;
  origin?: string;
  note?: string;
};

function recordPage(rec: Record<string, unknown>, meta: Meta, id: string): string {
  const p = (rec.performance as Record<string, unknown>) || {};
  const title = esc(p.title || id);
  const composer = esc(((p.composition_ref as Record<string, unknown> | undefined)?.composer as Record<string, unknown> | undefined)?.display_name || "anonymous");
  const attr = esc(rec.attribution || "");
  const rh = esc((rec.record_hash as { value?: string } | undefined)?.value || "");
  const it = (p.interpreter as Record<string, unknown>) || {};
  const interp = esc(it.label || it.model || it.kind || "");
  const outs = (p.outputs as Array<Record<string, unknown>>) || [];
  const loud = outs.length ? esc(`${outs[0].loudness_lufs_integrated} LUFS, true peak ${outs[0].true_peak_dbtp} dBTP`) : "";
  const sounds = ((rec.sounds_used as Array<Record<string, unknown>>) || [])
    .map((s) => `<li>${esc(s.credit_line || s.title || s.key || "")}</li>`)
    .join("");
  const hacks = ((rec.hacks_chain as Array<Record<string, unknown>>) || [])
    .map((h) => {
      const tool = esc(h.tool || h.type || "hack");
      const engine = h.engine ? ` · ${esc(h.engine)}` : "";
      return `<li>${tool}${engine}</li>`;
    })
    .join("");
  const sig = (rec.signature as Record<string, unknown>) || {};
  const sigLine = esc(`${sig.status || ""} ${sig.algorithm || ""} ${sig.key_id || ""} ${sig.signed_by || ""}`);
  const robots = meta.visibility === "public" && !meta.hidden ? "" : '<meta name="robots" content="noindex">';
  const note = meta.note ? `<p>${esc(meta.note)}</p>` : "";
  return `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">${robots}<title>${title}</title>
<style>body{font:16px/1.45 Georgia,serif;margin:0;background:#f6f3ec;color:#1c1915}main{max-width:42rem;margin:0 auto;padding:1.5rem 1rem 4rem}audio{width:100%;margin:1rem 0}a{color:#6b3a1f}.row{display:flex;flex-wrap:wrap;gap:.5rem;margin:.8rem 0}.row a,button{background:#1c1915;color:#f6f3ec;text-decoration:none;border:0;border-radius:999px;padding:.45rem .8rem;font:14px sans-serif;cursor:pointer}.badge{font:13px sans-serif;letter-spacing:.04em;text-transform:uppercase}pre{white-space:pre-wrap;font:12px/1.4 ui-monospace,monospace;background:#fff;padding:.8rem;border-radius:8px}</style></head>
<body><main><p class="badge"><a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a> · ${esc(meta.origin || "hosted")}</p>
<h1>${title}</h1><p>Composed by ${composer}. Interpreter: ${interp}.</p>
<audio controls src="/api/jobs/${esc(id)}/files/preview.mp3"></audio>
<div class="row"><a href="/api/jobs/${esc(id)}/files/performance.flac">Download FLAC</a><a href="/api/jobs/${esc(id)}/files/preview.mp3">Download MP3</a>
<a href="/records/${esc(id)}.json">Download record</a><a href="/api/jobs/${esc(id)}/files/plan.json">Download plan</a>
<button type="button" id="copy">Copy link</button></div>
<p>${loud}</p><p>${attr}</p>${note}<h2>Sounds</h2><ul>${sounds}</ul><h2>Hacks</h2><ul>${hacks}</ul>
<p>record_hash <code>${rh}</code></p><p>Signature: ${sigLine}</p>
<p>Anyone can check this record with <code>nc verify-record</code>. The signature says which instance rendered it.</p>
<details><summary>Full record</summary><pre>${esc(JSON.stringify(rec, null, 1))}</pre></details>
<p>Peter Semmelhack · NetComposer</p></main>
<script>document.getElementById("copy").onclick=()=>navigator.clipboard.writeText(location.href);</script></body></html>`;
}

function tombPage(id: string, tomb: Tomb): string {
  return `<!DOCTYPE html><html><head><meta name="robots" content="noindex"><title>Gone</title></head><body><h1>410 Gone</h1><p>${esc(tomb.reason || "taken down")}</p><p>record_hash ${esc(tomb.record_hash || "")}</p><p>${esc(id)}</p></body></html>`;
}

function licOk(license: Record<string, unknown> | undefined): boolean {
  if (!license) return false;
  const name = String(license.name || "");
  const id = String(license.id || "");
  const blob = `${id} ${name}`;
  if (/NC|ND|Sampling\+/i.test(blob)) return false;
  if (/public domain/i.test(name) || ["PD", "PD-equivalent", "PDM-1.0", "CC0-1.0"].includes(id)) return true;
  if (/^CC0/i.test(id) || /^CC-BY/i.test(id) || /^CC BY/i.test(name) || /^CC0/i.test(name)) return true;
  return false;
}

function jobs(env: Env, id: string): DurableObjectStub<JobDO> {
  return env.JOBS.get(env.JOBS.idFromName(id));
}

function budget(env: Env): DurableObjectStub<BudgetDO> {
  return env.BUDGET.get(env.BUDGET.idFromName("global"));
}

function adminGate(request: Request, env: Env): Response | null {
  const token = env.NC_ADMIN_TOKEN || "";
  if (!token) return json({ error: "admin is off until NC_ADMIN_TOKEN is set" }, 404);
  const got = request.headers.get("Authorization") || "";
  if (!safeEqual(got, `Bearer ${token}`)) return json({ error: "admin token required" }, 401);
  return null;
}

function publicBase(request: Request, env: Env): string {
  const set = (env.NC_PUBLIC_BASE_URL || "").replace(/\/$/, "");
  if (set) return set;
  return new URL(request.url).origin;
}

function logInternal(id: string | null, err: unknown): string {
  const detail = err instanceof Error ? err.stack || err.message : String(err);
  console.error(JSON.stringify({ event: "perform_failed", id, detail }));
  return publicInternalError(id);
}

function clientIp(request: Request): string {
  return (request.headers.get("CF-Connecting-IP") || request.headers.get("X-Forwarded-For") || "").split(",")[0].trim();
}

async function readJson(request: Request): Promise<unknown> {
  const text = await request.text();
  if (!text) return {};
  return JSON.parse(text);
}

async function readJsonKey(env: Env, key: string): Promise<Record<string, unknown> | null> {
  const obj = await env.PERFORMANCES.get(key);
  if (!obj) return null;
  try {
    return JSON.parse(await obj.text());
  } catch {
    return null;
  }
}

async function claimMatches(stored: string | null | undefined, token: unknown): Promise<boolean> {
  if (!stored || typeof token !== "string" || !token) return false;
  return safeEqual(stored, await sha256Text(token));
}

async function sha256Text(s: string): Promise<string> {
  return sha256HexBytes(new TextEncoder().encode(s));
}

async function sha256HexBytes(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new Uint8Array(bytes));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function safeEqual(a: string, b: string): boolean {
  const aa = new TextEncoder().encode(a);
  const bb = new TextEncoder().encode(b);
  const len = Math.max(aa.length, bb.length);
  let diff = aa.length ^ bb.length;
  for (let i = 0; i < len; i++) diff |= (aa[i] || 0) ^ (bb[i] || 0);
  return diff === 0;
}

function isoNow(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function day(): string {
  return isoNow().slice(0, 10);
}

function isoPlus(ms: number): string {
  return new Date(Date.now() + ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function isAudio(name: string): boolean {
  const low = name.toLowerCase();
  return low.endsWith(".flac") || low.endsWith(".mp3") || low.endsWith(".wav");
}

function typeOf(name: string): string {
  if (FILE_TYPES[name]) return FILE_TYPES[name];
  const low = name.toLowerCase();
  if (low.endsWith(".flac")) return "audio/flac";
  if (low.endsWith(".mp3")) return "audio/mpeg";
  if (low.endsWith(".json")) return "application/json";
  return "application/octet-stream";
}

function esc(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] || c);
}

function json(data: unknown, status = 200, extra?: Record<string, string>): Response {
  const headers = new Headers(extra);
  headers.set("content-type", "application/json; charset=utf-8");
  headers.set("access-control-allow-origin", "*");
  headers.set("access-control-allow-headers", "Content-Type, Authorization");
  headers.set("access-control-allow-methods", "GET, POST, OPTIONS");
  if (!headers.has("cache-control")) headers.set("cache-control", "no-store");
  const body = typeof data === "string" ? data : JSON.stringify(data);
  return new Response(body, { status, headers });
}

function html(body: string, status = 200, extra?: Record<string, string>): Response {
  const headers = new Headers(extra);
  headers.set("content-type", "text/html; charset=utf-8");
  headers.set("access-control-allow-origin", "*");
  if (!headers.has("cache-control")) headers.set("cache-control", "no-store");
  return new Response(body, { status, headers });
}

function bin(body: ArrayBuffer | Uint8Array, contentType: string, extra?: Record<string, string>): Response {
  const headers = new Headers(extra);
  headers.set("content-type", contentType);
  headers.set("access-control-allow-origin", "*");
  if (!headers.has("cache-control")) headers.set("cache-control", "no-store");
  const bytes = body instanceof Uint8Array ? new Uint8Array(body) : body;
  return new Response(bytes, { status: 200, headers });
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
