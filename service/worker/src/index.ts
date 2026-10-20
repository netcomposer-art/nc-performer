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
import {
  AFTER_SIGNING,
  EXPIRE_AUDIO,
  KEPT_FORM_OVERHEAD,
  KEPT_MP3,
  KEPT_TAIL_S,
  KEPT_WAV,
  KEPT_WAV_MAX_BYTES,
  KeepReject,
  dialsLine,
  inspectWav,
  jobLengthS,
  keptFiles,
  liveAudioMeta,
  parseDials,
  parseMultipart,
  tooLargeMessage,
  type LiveDials,
} from "./keep";
import { CANCELLED_MESSAGE, cancelToken, queueErrorIsCancel } from "./cancel";
import { REQUIRED_OUTPUTS, containerPageUpdate, missingOutputs, readableBytes } from "./outputs";
import { applyPerformHit, burstBlock, claimToken, forPage, isInternalFailure, jobStatusFields, newId, PER_10MIN, PER_DAY, proxyHosts, publicInternalError, rejectPlan, testerPass, wordsTooLong, type FoundSound, type RateBlock } from "./policy";

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

const FILES = [...REQUIRED_OUTPUTS];
const NOTE_INDEX_KEY = "note-index/v1.json";

const FILE_TYPES: Record<string, string> = {
  "performance.flac": "audio/flac",
  "preview.mp3": "audio/mpeg",
  "kept.wav": "audio/wav",
  "kept.mp3": "audio/mpeg",
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
const ID_RE = /^[hkg]-[0-9abcdefghjkmnpqrstvwxyz]{12}$/;
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
  NC_TESTER_TOKEN?: string;
  NC_INTERNAL_TOKEN?: string;
  NC_PUBLIC_BASE_URL?: string;
  NC_IP_HASH_SALT?: string;
  NC_PROCESSED?: string;
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
  sounds?: GatherSound[];
  stage_line?: string;
  tester?: boolean;
};

type GatherSound = {
  id: string;
  title: string;
  supplier: string;
  license: string;
  preview_url: string;
  page?: string;
  source_url?: string;
};

type GatherFile = { sid?: string; url?: string; ext?: string; range_bytes?: number | null };

type GatherDoc = {
  id?: string;
  seed?: number;
  claim_hash?: string;
  composition?: Record<string, unknown>;
  plan?: unknown;
  sounds?: GatherSound[];
  picked_keys?: string[];
  picked_sample_ids?: string[];
  files?: GatherFile[];
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
    const finished = job.state === "done" || job.state === "failed" || job.state === "rejected" || job.state === "cancelled" || job.state === "gathered";
    if (finished && fields.state && fields.state !== job.state) {
      const next = { ...job };
      if (typeof fields.cost_usd === "number") next.cost_usd = fields.cost_usd;
      await this.ctx.storage.put("job", next);
      return next;
    }
    const next = { ...job, ...fields };
    await this.ctx.storage.put("job", next);
    return next;
  }

  /** One step, so a poll cannot land between the read and the write. */
  async cancel(message: string): Promise<{ job: Job | null; stopped: boolean }> {
    const job = await this.get();
    if (!job) return { job: null, stopped: false };
    if (job.state === "done" || job.state === "failed" || job.state === "rejected") return { job, stopped: false };
    if (job.state === "cancelled") return { job, stopped: true };
    const next: Job = {
      ...job,
      state: "cancelled",
      stage: "cancelled",
      percent: 100,
      error: message,
      error_code: "cancelled",
      stage_line: message,
    };
    await this.ctx.storage.put("job", next);
    return { job: next, stopped: true };
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
      "CREATE TABLE IF NOT EXISTS ip_hits (ip TEXT, ts REAL)",
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
   * 3 Performs per 10 minutes and 10 per rolling 24 hours for one network.
   * Checked together so a rejection does not consume either window.
   */
  async hit(ip: string, per10min = PER_10MIN, perDay = PER_DAY): Promise<RateBlock | null> {
    const now = Date.now() / 1000;
    this.ctx.storage.sql.exec("DELETE FROM ip_hits WHERE ts < ?", now - 86400);
    const rows = this.ctx.storage.sql.exec("SELECT ts FROM ip_hits WHERE ip = ? AND ts >= ?", ip, now - 86400).toArray();
    const decision = applyPerformHit(now, rows.map((row) => Number(row.ts)), per10min, perDay);
    if (decision.error) {
      return { error: decision.error, retry_after_s: decision.retry_after_s || 1, retry_at: decision.retry_at || "" };
    }
    this.ctx.storage.sql.exec("INSERT INTO ip_hits (ip, ts) VALUES (?, ?)", ip, now);
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
        "access-control-allow-headers": "Content-Type, Authorization, X-NC-Tester",
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
        const current = await jobs(env, id).get();
        if (queueErrorIsCancel(current?.state)) {
          await wipePerformance(env, id);
          await writeCancelledStatus(env, id);
          msg.ack();
          continue;
        }
        if (current && (current.state === "done" || current.state === "failed" || current.state === "rejected")) {
          msg.ack();
          continue;
        }
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
  if (request.method === "POST" && path === "/api/gather") return gather(request, env, base);
  if (request.method === "POST" && path === "/api/perform") return perform(request, env, base);
  if (request.method === "POST" && path === "/api/perform-processed") {
    if (!processedOn(env)) return json({ error: "not found" }, 404);
    return perform(request, env, base, true);
  }
  if (request.method === "POST" && path === "/api/submit") return submit(request, env, base);
  if (request.method === "POST" && path === "/admin/enabled") return adminEnabled(request, env);
  if (request.method === "POST" && (path === "/admin/hide" || path === "/admin/takedown")) {
    return adminModerate(request, env, path.endsWith("hide") ? "hide" : "takedown");
  }

  let m = path.match(/^\/api\/jobs\/([hkg]-[0-9a-z]+)$/);
  if (request.method === "GET" && m) return jobStatus(env, m[1], base);
  m = path.match(/^\/api\/jobs\/([hkg]-[0-9a-z]+)\/files\/([A-Za-z0-9._-]+)$/);
  if (request.method === "GET" && m) return file(env, m[1], m[2]);
  m = path.match(/^\/api\/jobs\/([hkg]-[0-9a-z]+)\/audio\/([A-Za-z0-9._-]+)$/);
  if (request.method === "GET" && m) return gatherAudio(env, m[1], m[2]);
  m = path.match(/^\/api\/jobs\/([hkg]-[0-9a-z]+)\/events$/);
  if (request.method === "GET" && m) return events(env, m[1], base);
  m = path.match(/^\/api\/jobs\/([hkg]-[0-9a-z]+)\/cancel$/);
  if (request.method === "POST" && m) return cancelJob(request, env, m[1], base);
  m = path.match(/^\/api\/jobs\/([hkg]-[0-9a-z]+)\/(keep|visibility)$/);
  if (request.method === "POST" && m) return metaPost(request, env, m[1], m[2]);
  m = path.match(/^\/api\/verify\/([hkg]-[0-9a-z]+)$/);
  if (request.method === "GET" && m) return verify(env, m[1]);
  m = path.match(/^\/records\/sha256\/([0-9a-f]{64})$/);
  if (request.method === "GET" && m) return alias(env, m[1]);
  m = path.match(/^\/records\/([hkg]-[0-9a-z]+)(\.json)?$/);
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

async function gather(request: Request, env: Env, base: string): Promise<Response> {
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
  let excluded: string | null = null;
  let avoidKeys: string[] | null = null;
  let avoidSamples: string[] | null = null;
  if (typeof body.exclude_gather_id === "string" && body.exclude_gather_id.trim()) {
    excluded = body.exclude_gather_id.trim();
    if (!ID_RE.test(excluded) || !excluded.startsWith("g-")) return json({ error: "not found" }, 404);
    const token = typeof body.claim_token === "string" ? body.claim_token.trim() : "";
    if (!token) return json({ error: "claim_token is required.", code: "rejected" }, 400);
    const previous = await readJsonKey(env, `gathers/${excluded}/collection.json`) as GatherDoc | null;
    const previousJob = await jobs(env, excluded).get();
    if (!previous) return json({ error: "not found" }, 404);
    const claim = previous.claim_hash || previousJob?.claim_hash;
    if (!(await claimMatches(claim, token))) {
      return json({ error: "claim_token does not match this Gather." }, 403);
    }
    avoidKeys = Array.isArray(previous.picked_keys) ? previous.picked_keys : [];
    avoidSamples = Array.isArray(previous.picked_sample_ids) ? previous.picked_sample_ids : [];
  }
  const ipHash = await sha256Text(`${env.NC_IP_HASH_SALT || "nc-perform"}:${ip}`);
  const tester = await testerPass(request.headers.get("X-NC-Tester"), env.NC_TESTER_TOKEN);
  const limited = await rateLimit(env, ipHash, tester);
  if (limited) return rateJson(limited);
  const randomSeed = () => crypto.getRandomValues(new Uint32Array(1))[0] % 2147483646 || 1;
  const seed = body.seed == null ? randomSeed() : (Number(body.seed) || 1);
  const token = claimToken();
  const id = newId("g");
  const created = isoNow();
  const req = {
    kind: "gather",
    composition: comp,
    visibility: "unlisted",
    seed,
    instrument,
    claim_hash: await sha256Text(token),
    public_base: base,
    avoid_keys: avoidKeys,
    avoid_sample_ids: avoidSamples,
    excluded_gather_id: excluded,
    force_stub: spend >= 3,
    tester,
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
    sounds: [],
    stage_line: "Queued",
    tester,
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
    return json({ error, id, gather_id: id, code: "error" }, 500);
  }
  return json({
    gather_id: id,
    id,
    claim_token: token,
    seed,
    sounds: [],
    pending: true,
    status: "queued",
    position: placed.position,
    poll: `/api/jobs/${id}`,
    excluded_gather_id: excluded,
  }, 202);
}

async function keptGather(env: Env, body: Record<string, unknown>): Promise<{ gather_id: string; plan: unknown; composition: Record<string, unknown>; seed: number } | Response | null> {
  const raw = body.gather_id;
  if (typeof raw !== "string" || !raw.trim()) return null;
  const gatherId = raw.trim();
  if (!ID_RE.test(gatherId) || !gatherId.startsWith("g-")) return json({ error: "not found" }, 404);
  const token = typeof body.claim_token === "string" ? body.claim_token.trim() : "";
  if (!token) return json({ error: "claim_token is required.", code: "rejected" }, 400);
  const doc = await readJsonKey(env, `gathers/${gatherId}/collection.json`) as GatherDoc | null;
  const gatherJob = await jobs(env, gatherId).get();
  if (!doc) {
    if (gatherJob && !["gathered", "failed", "rejected", "cancelled", "done"].includes(gatherJob.state)) {
      return json({ error: "That Gather is not ready.", code: "rejected", gather_id: gatherId, state: gatherJob.state }, 409);
    }
    return json({ error: "not found" }, 404);
  }
  const claim = doc.claim_hash || gatherJob?.claim_hash;
  if (!(await claimMatches(claim, token))) return json({ error: "claim_token does not match this Gather." }, 403);
  if (!doc.plan || !doc.composition || typeof doc.composition !== "object") {
    return json({ error: "That Gather is not ready.", code: "rejected", gather_id: gatherId }, 409);
  }
  const planErr = rejectPlan(doc.plan);
  if (planErr) return json({ error: planErr, code: "rejected" }, 400);
  const planSeed = (doc.plan as { seed?: number }).seed;
  const seed = Number(doc.seed || planSeed) || 1;
  return { gather_id: gatherId, plan: doc.plan, composition: doc.composition, seed };
}

function processedOn(env: Env): boolean {
  const value = (env.NC_PROCESSED || "").trim().toLowerCase();
  return value === "1" || value === "true" || value === "yes" || value === "on";
}

async function perform(request: Request, env: Env, base: string, processed = false): Promise<Response> {
  const b = budget(env);
  const spend = await b.spendToday();
  const on = await b.enabled(env.PERFORM_ENABLED !== "0");
  if (!on || spend >= 5) return json({ error: "Perform is resting until tomorrow.", code: "resting" }, 503);
  const body = (await readJson(request)) as Record<string, unknown>;
  const ip = clientIp(request);
  if (env.TURNSTILE_SECRET_KEY && !(await turnstile(env, body.turnstile_token, ip))) {
    return json({ error: "The bot check failed. Reload the page and try again.", code: "turnstile" }, 403);
  }
  const kept = await keptGather(env, body);
  if (kept instanceof Response) return kept;
  const comp = kept ? kept.composition : compositionFrom(body);
  const instrumentError = kept ? null : instrumentProblem(body.instrument);
  if (instrumentError) return json({ error: instrumentError, code: "rejected" }, 400);
  const instrument = typeof body.instrument === "string" && body.instrument.trim() ? body.instrument.trim() : null;
  if (!kept && instrument) comp.instrument = instrument;
  if (!kept) {
    const words = `${comp.composition || ""}\n${comp.performance || ""}`;
    const wordErr = wordsTooLong(words);
    if (wordErr) return json({ error: wordErr, code: "rejected" }, 400);
  }
  if (!kept && body.plan != null) {
    if (typeof body.plan !== "object" || Array.isArray(body.plan)) return json({ error: "plan must be a JSON object.", code: "rejected" }, 400);
    const planErr = rejectPlan(body.plan);
    if (planErr) return json({ error: planErr, code: "rejected" }, 400);
    const posted = body.plan as { assembly?: string };
    if (!processed && posted.assembly !== "raw") {
      return json({ error: "This release assembles the recordings in order. Processing is off.", code: "rejected" }, 400);
    }
  }
  const ipHash = await sha256Text(`${env.NC_IP_HASH_SALT || "nc-perform"}:${ip}`);
  const tester = await testerPass(request.headers.get("X-NC-Tester"), env.NC_TESTER_TOKEN);
  const limited = await rateLimit(env, ipHash, tester);
  if (limited) return rateJson(limited);
  const visibility = body.visibility || "unlisted";
  if (visibility !== "unlisted" && visibility !== "public") {
    return json({ error: "visibility must be unlisted or public.", code: "rejected" }, 400);
  }
  const randomSeed = () => crypto.getRandomValues(new Uint32Array(1))[0] % 2147483646 || 1;
  let seed: number;
  if (kept) seed = kept.seed;
  else if (body.perform_again || body.seed == null) seed = randomSeed();
  else seed = Number(body.seed) || 1;
  const token = claimToken();
  const id = newId("h");
  const created = isoNow();
  const req = {
    composition: comp,
    plan: kept ? kept.plan : (body.plan ?? null),
    gather_id: kept ? kept.gather_id : null,
    visibility,
    seed,
    keep: Boolean(body.keep),
    instrument: instrument,
    claim_hash: await sha256Text(token),
    public_base: base,
    // Raw assembly does not call the model. This flag still skips it on the processed endpoint when the soft budget is spent.
    // A tester pass does not change it, and it does not change the $5 rest either.
    force_stub: spend >= 3,
    tester,
    processed: processed && processedOn(env),
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
    tester,
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
  if (job.state === "cancelled") {
    await wipePerformance(env, id);
    await writeCancelledStatus(env, id);
    return;
  }
  if (job.state === "done" || job.state === "failed" || job.state === "rejected" || job.state === "gathered") return;
  await budget(env).dequeue(id);
  if (await abandonIfCancelled(env, id)) return;
  const container = getContainer(env.RENDER, id);
  const requested = JSON.parse(job.request_json) as { public_base?: string; gather_id?: string; kind?: string };
  const base = (env.NC_PUBLIC_BASE_URL || requested.public_base || "").replace(/\/$/, "");
  const envVars = containerEnv(env, base);
  await container.startAndWaitForPorts({
    startOptions: { envVars, enableInternet: false },
    cancellationOptions: { portReadyTimeoutMS: PORT_READY_MS, instanceGetTimeoutMS: PORT_READY_MS },
  });
  await container.setAllowedHosts(proxyHosts(allowedHosts(env)));
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (env.NC_INTERNAL_TOKEN) headers.authorization = `Bearer ${env.NC_INTERNAL_TOKEN}`;
  try {
    await seedNoteIndex(env, container, id, headers);
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    console.error(JSON.stringify({ event: "note_index", id, detail }));
  }
  if (requested.gather_id && requested.kind !== "gather") {
    await seedGatherCache(env, container, id, requested.gather_id, headers);
  }
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
    sounds?: GatherSound[];
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
    if (await abandonIfCancelled(env, id)) return;
    await sleep(2000);
    if (await abandonIfCancelled(env, id)) return;
    const res = await containerFetch(container, id, `http://container/internal/jobs/${id}`, { headers });
    if (!res.ok) continue;
    last = await res.json();
    const page = containerPageUpdate(last);
    const patch: Partial<Job> = {
      state: page.state,
      stage: page.stage,
      percent: page.percent,
      error: pageError(last.error),
    };
    if (last.seed != null && Number.isFinite(Number(last.seed))) patch.seed = Number(last.seed);
    if ("trimmed" in last) patch.trimmed = typeof last.trimmed === "string" && last.trimmed.trim() ? last.trimmed : null;
    if (Array.isArray(last.found_sounds)) patch.found_sounds = last.found_sounds;
    if (Array.isArray(last.sounds)) patch.sounds = last.sounds;
    if (page.stage_line) patch.stage_line = page.stage_line;
    if (await abandonIfCancelled(env, id)) return;
    // Publish gathered only after the files are in R2. Until then the page stays on gathering.
    if (last.state === "gathered") {
      await stub.patch({ ...patch, state: "gathering", stage: "gathering", percent: 90, stage_line: "Gathering the sounds…" });
      break;
    }
    await stub.patch(patch);
    if (last.state === "done" || last.state === "failed" || last.state === "rejected") break;
  }
  if (await abandonIfCancelled(env, id)) return;
  if (last.state === "gathered") {
    const doc = await copyGather(env, container, id, headers);
    last.sounds = Array.isArray(doc.sounds) ? doc.sounds : [];
    last.stage_line = "Gathered.";
    if (doc.seed != null) last.seed = Number(doc.seed);
  } else if (last.state === "done") {
    await sleep(400);
    if (await abandonIfCancelled(env, id)) return;
    const again = await containerFetch(container, id, `http://container/internal/jobs/${id}`, { headers });
    if (again.ok) last = await again.json();
    if (last.state === "done") {
      if (await abandonIfCancelled(env, id)) return;
      await copyOutputs(env, container, id, headers);
      if (await abandonIfCancelled(env, id)) return;
      await confirmStored(env, id);
    }
  } else if (last.state !== "failed" && last.state !== "rejected") {
    throw new Error("container did not finish within 10 minutes");
  }
  if (await abandonIfCancelled(env, id)) return;
  const cost = Number(last.cost_usd || 0);
  const shown = pageError(last.error);
  await budget(env).addSpend(cost);
  const finishedLine = last.state === "done"
    ? (typeof last.stage_line === "string" && last.stage_line.trim() && last.stage_line !== "Storing the Performance…"
      ? last.stage_line
      : "Done")
    : last.state === "gathered"
      ? "Gathered."
      : undefined;
  const updated = await stub.patch({
    state: last.state || "failed",
    stage: last.stage || last.state || "failed",
    percent: 100,
    error: shown,
    cost_usd: cost,
    ...(Array.isArray(last.sounds) ? { sounds: last.sounds } : {}),
    ...(finishedLine ? { stage_line: finishedLine } : {}),
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
        ...(job.tester ? { tester: true } : {}),
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
  if (processedOn(env)) out.NC_PROCESSED = "1";
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

function audioType(ext: string | undefined): string {
  const low = (ext || "").toLowerCase();
  if (low === ".flac") return "audio/flac";
  if (low === ".mp3") return "audio/mpeg";
  if (low === ".wav" || low === ".wave") return "audio/wav";
  if (low === ".ogg") return "audio/ogg";
  if (low === ".aif" || low === ".aiff") return "audio/aiff";
  return "application/octet-stream";
}

async function seedNoteIndex(env: Env, container: DurableObjectStub<RenderContainer>, id: string, headers: Record<string, string>): Promise<void> {
  // A newer index in R2 replaces the copy shipped in the image. Metadata only.
  const obj = await env.PERFORMANCES.get(NOTE_INDEX_KEY);
  if (!obj || obj.size <= 0 || obj.size > 8 * 1024 * 1024) return;
  const seeded = await containerFetch(container, id, "http://container/internal/note-index", {
    method: "POST",
    headers,
    body: await obj.arrayBuffer(),
  });
  if (!seeded.ok && seeded.status !== 204) {
    console.error(JSON.stringify({ event: "note_index", id, status: seeded.status }));
  }
}

async function copyGather(env: Env, container: DurableObjectStub<RenderContainer>, id: string, headers: Record<string, string>): Promise<GatherDoc> {
  const res = await containerFetch(container, id, `http://container/internal/jobs/${id}/collection`, { headers });
  if (!res.ok) throw new Error("gather collection was not stored");
  const raw = await res.arrayBuffer();
  await env.PERFORMANCES.put(`gathers/${id}/collection.json`, raw, { httpMetadata: { contentType: "application/json" } });
  const doc = JSON.parse(new TextDecoder().decode(raw)) as GatherDoc;
  for (const file of doc.files || []) {
    const sid = String(file.sid || "");
    if (!/^[A-Za-z0-9._-]{1,80}$/.test(sid)) continue;
    const audio = await containerFetch(container, id, `http://container/internal/jobs/${id}/audio/${sid}`, { headers });
    if (!audio.ok) continue;
    await env.PERFORMANCES.put(`gathers/${id}/audio/${sid}`, await audio.arrayBuffer(), {
      httpMetadata: { contentType: audioType(file.ext) },
    });
  }
  await env.PERFORMANCES.put(`gathers/${id}/meta.json`, JSON.stringify({ id, claim_token_sha256: doc.claim_hash || null }), {
    httpMetadata: { contentType: "application/json" },
  });
  const storedJob = await jobs(env, id).get();
  await env.PERFORMANCES.put(
    `gathers/${id}/status.json`,
    JSON.stringify({
      id,
      state: "gathered",
      seed: doc.seed ?? null,
      sounds: doc.sounds || [],
      stage_line: "Gathered.",
      ...(storedJob?.tester ? { tester: true } : {}),
    }),
    { httpMetadata: { contentType: "application/json" } },
  );
  return doc;
}

async function seedGatherCache(env: Env, container: DurableObjectStub<RenderContainer>, performId: string, gatherId: string, headers: Record<string, string>): Promise<void> {
  const obj = await env.PERFORMANCES.get(`gathers/${gatherId}/collection.json`);
  if (!obj) return;
  const doc = (await obj.json()) as GatherDoc;
  for (const file of doc.files || []) {
    if (!file.sid || !file.url) continue;
    const audio = await env.PERFORMANCES.get(`gathers/${gatherId}/audio/${file.sid}`);
    if (!audio) continue;
    const seedHeaders: Record<string, string> = {
      ...headers,
      "content-type": "application/octet-stream",
      "x-audio-url": file.url,
      "x-audio-ext": file.ext || "",
    };
    if (file.range_bytes != null) seedHeaders["x-audio-range"] = String(file.range_bytes);
    const seeded = await containerFetch(container, performId, "http://container/internal/cache", {
      method: "POST",
      headers: seedHeaders,
      body: await audio.arrayBuffer(),
    });
    if (!seeded.ok && seeded.status !== 204) {
      console.error(JSON.stringify({ event: "gather_seed", id: performId, gather_id: gatherId, sid: file.sid, status: seeded.status }));
    }
  }
}

const READBACK_TRIES = 5;
const READBACK_WAIT_MS = 200;

/** Read each output back from R2. done is not published until this returns. */
async function confirmStored(env: Env, id: string): Promise<void> {
  let missing: string[] = [...REQUIRED_OUTPUTS];
  for (let attempt = 0; attempt < READBACK_TRIES; attempt++) {
    const readable: string[] = [];
    for (const name of REQUIRED_OUTPUTS) {
      try {
        // One byte, not the whole FLAC. A missing or empty object fails the read-back.
        const obj = await env.PERFORMANCES.get(`performances/${id}/${name}`, { range: { offset: 0, length: 1 } });
        if (await objectHasBytes(obj)) readable.push(name);
      } catch (err) {
        const detail = err instanceof Error ? err.message : String(err);
        console.error(JSON.stringify({ event: "readback", id, name, detail }));
      }
    }
    missing = missingOutputs(readable);
    if (!missing.length) return;
    if (attempt + 1 < READBACK_TRIES) await sleep(READBACK_WAIT_MS);
  }
  throw new Error(`Stored files were not readable: ${missing.join(", ")}`);
}

async function objectHasBytes(obj: R2ObjectBody | null): Promise<boolean> {
  if (!obj || obj.size <= 0 || !obj.body) return false;
  let first = 0;
  const reader = obj.body.getReader();
  try {
    const { value, done } = await reader.read();
    if (!done && value) first = value.byteLength;
  } catch {
    return false;
  } finally {
    try {
      await reader.cancel();
    } catch {
      /* the first byte was enough */
    }
  }
  return readableBytes(obj.size, first);
}

async function copyOutputs(env: Env, container: DurableObjectStub<RenderContainer>, id: string, headers: Record<string, string>): Promise<void> {
  const signer = id.startsWith("h-") ? await getSigner(env) : null;
  let recordBytes: Uint8Array<ArrayBuffer> | null = null;
  for (const name of FILES) {
    const res = await containerFetch(container, id, `http://container/internal/jobs/${id}/files/${name}`, { headers });
    // A missing file is left for confirmStored to reject. A 5xx already threw above.
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
  tester?: boolean;
};

async function jobStatus(env: Env, id: string, base: string): Promise<Response> {
  if (!ID_RE.test(id)) return json({ error: "not found" }, 404);
  const job = await jobs(env, id).get();
  if (!job) {
    const gatherStatus = id.startsWith("g-") ? await readJsonKey(env, `gathers/${id}/status.json`) : null;
    const meta = await readJsonKey(env, `performances/${id}/meta.json`);
    const status = gatherStatus || (await readJsonKey(env, `performances/${id}/status.json`)) || (await readJsonKey(env, `jobs/${id}/status.json`));
    if (!meta && !status) return json({ error: "not found" }, 404);
    const stored = (status || {}) as { state?: string; error?: string | null; trimmed?: string | null; seed?: number | null; found_sounds?: unknown; sounds?: GatherSound[]; stage_line?: string; cost_estimate?: unknown; tester?: boolean };
    const state = String(stored.state || "done");
    const storedMeta = state === "done" ? (meta as Meta | null) : null;
    const ghost = Array.isArray(stored.sounds) ? ({ sounds: stored.sounds } as Job) : null;
    const percent = state === "done" || state === "gathered" || state === "cancelled" ? 100 : 0;
    return json(publicJob(id, state, state, percent, stored.error || null, base, null, {
      trimmed: stored.trimmed ?? null,
      seed: stored.seed ?? null,
      found_sounds: stored.found_sounds ?? [],
      stage_line: stored.stage_line,
      cost_estimate: stored.cost_estimate,
      tester: stored.tester === true,
    }, ghost, storedMeta));
  }
  const pos = job.state === "queued" ? await budget(env).positionOf(id) : null;
  const extras = job.state === "done" ? await doneExtras(env, id) : undefined;
  const doneMeta = job.state === "done" ? ((await readJsonKey(env, `performances/${id}/meta.json`)) as Meta | null) : null;
  return json(publicJob(id, job.state, job.stage, job.percent, job.error, base, pos, extras, job, doneMeta));
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
  if (status.tester === true) out.tester = true;
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
  meta?: Meta | null,
) {
  const fields = jobStatusFields({
    stage,
    position,
    seed: extras && extras.seed != null ? extras.seed : job?.seed,
    trimmed: extras && "trimmed" in extras ? extras.trimmed : job?.trimmed,
    found_sounds: extras && extras.found_sounds != null ? extras.found_sounds : job?.found_sounds,
    stage_line: extras?.stage_line || job?.stage_line,
    tester: job?.tester === true || extras?.tester === true,
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
    tester: fields.tester,
  };
  if (id.startsWith("g-")) {
    out.gather_id = id;
    out.sounds = job?.sounds || [];
  } else if (job?.sounds && job.sounds.length) {
    out.sounds = job.sounds;
  }
  if (state === "done") {
    if (extras?.cost_estimate) out.cost_estimate = extras.cost_estimate;
    const files: Record<string, string> = {
      flac: `${base}/api/jobs/${id}/files/performance.flac`,
      mp3: `${base}/api/jobs/${id}/files/preview.mp3`,
      record: `${base}/records/${id}.json`,
      plan: `${base}/api/jobs/${id}/files/plan.json`,
      page: `${base}/records/${id}`,
    };
    const extra = keptFiles(meta || null, base, id);
    if (extra) {
      Object.assign(files, extra.files);
      out.live_dials = extra.live_dials;
      out.kept_note = extra.kept_note;
    }
    out.files = files;
  }
  return out;
}

async function gatherAudio(env: Env, id: string, sid: string): Promise<Response> {
  if (!ID_RE.test(id) || !/^[A-Za-z0-9._-]{1,80}$/.test(sid)) return json({ error: "not found" }, 404);
  let obj = await env.PERFORMANCES.get(`gathers/${id}/audio/${sid}`);
  const doc = (await readJsonKey(env, `gathers/${id}/collection.json`)) as GatherDoc | null;
  const row = (doc?.files || []).find((file) => file.sid === sid);
  if (!obj && row?.url && row.url.startsWith("https://")) {
    // Gather stored the URL. The first play fetches that one file.
    try {
      const upstream = await fetch(row.url);
      if (upstream.ok) {
        const bytes = await upstream.arrayBuffer();
        if (bytes.byteLength > 0 && bytes.byteLength <= 80 * 1024 * 1024) {
          await env.PERFORMANCES.put(`gathers/${id}/audio/${sid}`, bytes, {
            httpMetadata: { contentType: audioType(row.ext) },
          });
          obj = await env.PERFORMANCES.get(`gathers/${id}/audio/${sid}`);
        }
      }
    } catch {
      obj = null;
    }
  }
  if (!obj) return json({ error: "not found" }, 404);
  return new Response(obj.body, {
    headers: {
      "content-type": audioType(row?.ext),
      "cache-control": "public, max-age=3600",
      "access-control-allow-origin": "*",
    },
  });
}

async function file(env: Env, id: string, name: string): Promise<Response> {
  if (!ID_RE.test(id) || !/^[A-Za-z0-9._-]{1,80}$/.test(name)) return json({ error: "not found" }, 404);
  const meta = (await readJsonKey(env, `performances/${id}/meta.json`)) as Meta | null;
  if (meta?.tombstone && isAudio(name)) return json({ error: "This Performance was taken down.", code: "gone" }, 410);
  if (isAudio(name) && meta?.audio_expires_at && meta.audio_expires_at < isoNow()) {
    for (const audioName of EXPIRE_AUDIO) await env.PERFORMANCES.delete(`performances/${id}/${audioName}`);
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
        const doneMeta = job.state === "done" ? ((await readJsonKey(env, `performances/${id}/meta.json`)) as Meta | null) : null;
        const pub = publicJob(id, job.state, job.stage, job.percent, job.error, base, null, extras, job, doneMeta);
        const line = JSON.stringify(pub);
        if (line !== last) {
          controller.enqueue(encoder.encode(`data: ${line}\n\n`));
          last = line;
        }
        if (job.state === "done" || job.state === "failed" || job.state === "rejected" || job.state === "cancelled") break;
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

async function cancelJob(request: Request, env: Env, id: string, base: string): Promise<Response> {
  const stub = jobs(env, id);
  const job = await stub.get();
  if (!job) return json({ error: "not found" }, 404);
  let body: unknown;
  try {
    body = await readJson(request);
  } catch {
    return json({ error: "Body must be JSON.", code: "rejected" }, 400);
  }
  const token = cancelToken(body);
  if (!token) return json({ error: "claim_token is required.", code: "rejected" }, 400);
  let stored = job.claim_hash;
  if (!stored) {
    const meta = (await readJsonKey(env, `performances/${id}/meta.json`)) as { claim_token_sha256?: string } | null;
    stored = meta?.claim_token_sha256 || null;
  }
  if (!(await claimMatches(stored, token))) {
    return json({ error: "claim_token does not match this Performance." }, 403);
  }
  const result = await stub.cancel(CANCELLED_MESSAGE);
  if (!result.job) return json({ error: "not found" }, 404);
  if (!result.stopped) {
    const current = result.job;
    const pos = current.state === "queued" ? await budget(env).positionOf(id) : null;
    const extras = current.state === "done" ? await doneExtras(env, id) : undefined;
    const doneMeta = current.state === "done" ? ((await readJsonKey(env, `performances/${id}/meta.json`)) as Meta | null) : null;
    return json(publicJob(id, current.state, current.stage, current.percent, current.error, base, pos, extras, current, doneMeta));
  }
  await budget(env).dequeue(id);
  try {
    await getContainer(env.RENDER, id).destroy();
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    console.error(JSON.stringify({ event: "cancel_container", id, detail }));
  }
  await wipePerformance(env, id);
  await writeCancelledStatus(env, id);
  const fresh = (await stub.get()) || result.job;
  try {
    await budget(env).note({
      id,
      state: "cancelled",
      cost_usd: fresh.cost_usd || 0,
      ip_hash: fresh.ip_hash,
      error_code: "cancelled",
      created_at: fresh.created_at,
    });
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    console.error(JSON.stringify({ event: "cancel_note", id, detail }));
  }
  return json(publicJob(id, fresh.state, fresh.stage, fresh.percent, fresh.error, base, null, undefined, fresh));
}

async function abandonIfCancelled(env: Env, id: string): Promise<boolean> {
  const job = await jobs(env, id).get();
  if (job && job.state !== "cancelled") return false;
  await wipePerformance(env, id);
  await writeCancelledStatus(env, id);
  return true;
}

const RECORD_HASH = /^[0-9a-f]{64}$/;

async function wipePerformance(env: Env, id: string): Promise<void> {
  const rec = (await readJsonKey(env, `performances/${id}/record.json`)) as { record_hash?: { value?: string } } | null;
  const hash = rec?.record_hash?.value;
  if (typeof hash === "string" && RECORD_HASH.test(hash)) await env.PERFORMANCES.delete(`aliases/sha256/${hash}`);
  await deletePrefix(env, `performances/${id}/`);
  await deletePrefix(env, `gathers/${id}/`);
}

async function deletePrefix(env: Env, prefix: string): Promise<void> {
  let cursor: string | undefined;
  do {
    const page = await env.PERFORMANCES.list({ prefix, cursor, limit: 500 });
    const keys = page.objects.map((obj) => obj.key);
    if (keys.length) await env.PERFORMANCES.delete(keys);
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
}

async function writeCancelledStatus(env: Env, id: string): Promise<void> {
  await env.PERFORMANCES.put(
    `jobs/${id}/status.json`,
    JSON.stringify({
      id,
      state: "cancelled",
      error: CANCELLED_MESSAGE,
      stage_line: CANCELLED_MESSAGE,
      at: isoNow(),
    }),
    { httpMetadata: { contentType: "application/json" } },
  );
}

async function metaPost(request: Request, env: Env, id: string, action: string): Promise<Response> {
  const ctype = request.headers.get("content-type") || "";
  if (action === "keep" && ctype.toLowerCase().includes("multipart/form-data")) {
    return keepProcessed(request, env, id, ctype);
  }
  let body: Record<string, unknown>;
  try {
    body = (await readJson(request)) as Record<string, unknown>;
  } catch {
    return json({ error: "Body must be JSON.", code: "rejected" }, 400);
  }
  const meta = (await readJsonKey(env, `performances/${id}/meta.json`)) as Meta | null;
  if (!meta) return json({ error: "not found" }, 404);
  if (!(await claimMatches(meta.claim_token_sha256, body.claim_token))) {
    return json({ error: "claim_token does not match this Performance." }, 403);
  }
  if (action === "keep") {
    // JSON keep leaves the original audio where it is. It does not delete a
    // dialed file or clear live_dials from an earlier keep.
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

async function keepProcessed(request: Request, env: Env, id: string, ctype: string): Promise<Response> {
  const cap = KEPT_WAV_MAX_BYTES + KEPT_FORM_OVERHEAD;
  const len = Number(request.headers.get("content-length") || 0);
  if (len > cap) return json({ error: `Request is too large (${len} bytes).`, code: "rejected" }, 400);
  const raw = new Uint8Array(await request.arrayBuffer());
  if (raw.byteLength > cap) return json({ error: `Request is too large (${raw.byteLength} bytes).`, code: "rejected" }, 400);
  const meta = (await readJsonKey(env, `performances/${id}/meta.json`)) as Meta | null;
  if (!meta) return json({ error: "not found" }, 404);
  if (meta.tombstone) return json({ error: "This Performance was taken down.", code: "gone" }, 410);
  try {
    const parts = parseMultipart(raw, ctype);
    const tokenBytes = parts.claim_token;
    if (!tokenBytes) return json({ error: "claim_token is required.", code: "rejected" }, 400);
    let token: string;
    try {
      token = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(tokenBytes);
    } catch {
      return json({ error: "claim_token must be text.", code: "rejected" }, 400);
    }
    if (!(await claimMatches(meta.claim_token_sha256, token))) {
      return json({ error: "claim_token does not match this Performance." }, 403);
    }
    const audio = parts.audio;
    const dialsRaw = parts.dials;
    if (!audio || audio.byteLength < 1) return json({ error: "Attach the dialed audio as the form field 'audio'.", code: "rejected" }, 400);
    if (!dialsRaw) return json({ error: "Attach dials as the form field 'dials'.", code: "rejected" }, 400);
    if (audio.byteLength > KEPT_WAV_MAX_BYTES) return json({ error: tooLargeMessage(audio.byteLength), code: "rejected" }, 400);
    const wavBytes = new Uint8Array(audio);
    // Read the signed record only to learn the job length. Do not write it back.
    const recObj = await env.PERFORMANCES.get(`performances/${id}/record.json`);
    let record: Record<string, unknown> | null = null;
    if (recObj) {
      try {
        record = JSON.parse(new TextDecoder().decode(await recObj.arrayBuffer()));
      } catch {
        record = null;
      }
    }
    const info = inspectWav(wavBytes, jobLengthS(record) + KEPT_TAIL_S);
    let dialsText: string;
    try {
      dialsText = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(dialsRaw);
    } catch {
      return json({ error: "dials must be JSON.", code: "rejected" }, 400);
    }
    const dials = parseDials(dialsText);
    await env.PERFORMANCES.put(`performances/${id}/${KEPT_WAV}`, wavBytes, { httpMetadata: { contentType: "audio/wav" } });
    meta.keep = true;
    meta.retention = "forever";
    meta.audio_expires_at = null;
    meta.live_dials = dials;
    meta.live_audio = liveAudioMeta(info, false);
    await env.PERFORMANCES.put(`performances/${id}/meta.json`, JSON.stringify(meta, null, 1), {
      httpMetadata: { contentType: "application/json" },
    });
    const base = publicBase(request, env);
    return json({
      ok: true,
      processed: true,
      dials,
      audio_url: `${base}/api/jobs/${id}/files/${KEPT_WAV}`,
      id,
      visibility: meta.visibility,
      keep: true,
      audio_expires_at: null,
    });
  } catch (err) {
    if (err instanceof KeepReject) return json({ error: err.message, code: "rejected" }, 400);
    throw err;
  }
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
      const item: Record<string, unknown> = {
        id: meta.id,
        url: `${base}/records/${meta.id}`,
        record: `${base}/records/${meta.id}.json`,
        created_at: meta.created_at,
      };
      const extra = keptFiles(meta, base, meta.id || "");
      if (extra) {
        item.live_dials = extra.live_dials;
        item.kept_audio = extra.files.kept_wav;
        item.note = AFTER_SIGNING;
        if (extra.files.kept_mp3) item.kept_preview = extra.files.kept_mp3;
      }
      performances.push(item);
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
  for (const audioName of EXPIRE_AUDIO) await env.PERFORMANCES.delete(`performances/${id}/${audioName}`);
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
      for (const audioName of EXPIRE_AUDIO) await env.PERFORMANCES.delete(`performances/${id}/${audioName}`);
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

async function rateLimit(env: Env, ipHash: string, tester: boolean): Promise<RateBlock | null> {
  // Binding period is 10 seconds (Wrangler allows only 10 or 60). It stops a
  // burst before those requests touch the counters below. A tester pass skips
  // only the 10-minute and daily counters. The burst, Turnstile, size limits,
  // and the $3/$5 caps still apply. The token is not logged.
  try {
    const result = await env.PERFORM_RATE.limit({ key: ipHash });
    if (!result.success) return burstBlock(Date.now() / 1000);
  } catch {
    // Local tests have no binding. The Durable Object still enforces both windows.
  }
  if (tester) return null;
  return budget(env).hit(ipHash);
}

function rateJson(block: RateBlock): Response {
  return json({ error: block.error, code: "rate", retry_after_s: block.retry_after_s, retry_at: block.retry_at }, 429);
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
  live_dials?: LiveDials | null;
  live_audio?: { wav?: string; mp3?: string | null; sample_rate?: number; duration_s?: number; note?: string } | null;
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
  const jid = esc(id);
  const dials = meta.live_dials;
  let intro = "";
  let player = `/api/jobs/${jid}/files/preview.mp3`;
  let downloads = `<a href="/api/jobs/${jid}/files/performance.flac">Download FLAC</a><a href="/api/jobs/${jid}/files/preview.mp3">Download MP3</a>`;
  if (dials) {
    player = meta.live_audio?.mp3 ? `/api/jobs/${jid}/files/${KEPT_MP3}` : `/api/jobs/${jid}/files/${KEPT_WAV}`;
    intro = `<p>This is the kept sound with the Live dials. ${esc(AFTER_SIGNING)}</p><p>Live dials: ${esc(dialsLine(dials))}</p>`;
    const mp3Link = meta.live_audio?.mp3 ? `<a href="/api/jobs/${jid}/files/${KEPT_MP3}">Download dialed MP3</a>` : "";
    downloads =
      `<a href="/api/jobs/${jid}/files/${KEPT_WAV}">Download dialed WAV</a>${mp3Link}` +
      `<a href="/api/jobs/${jid}/files/performance.flac">Download original FLAC (signed render)</a>` +
      `<a href="/api/jobs/${jid}/files/preview.mp3">Download original MP3 (signed render)</a>`;
  }
  return `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">${robots}<title>${title}</title>
<style>body{font:16px/1.45 Georgia,serif;margin:0;background:#f6f3ec;color:#1c1915}main{max-width:42rem;margin:0 auto;padding:1.5rem 1rem 4rem}audio{width:100%;margin:1rem 0}a{color:#6b3a1f}.row{display:flex;flex-wrap:wrap;gap:.5rem;margin:.8rem 0}.row a,button{background:#1c1915;color:#f6f3ec;text-decoration:none;border:0;border-radius:999px;padding:.45rem .8rem;font:14px sans-serif;cursor:pointer}.badge{font:13px sans-serif;letter-spacing:.04em;text-transform:uppercase}pre{white-space:pre-wrap;font:12px/1.4 ui-monospace,monospace;background:#fff;padding:.8rem;border-radius:8px}</style></head>
<body><main><p class="badge"><a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a> · ${esc(meta.origin || "hosted")}</p>
<h1>${title}</h1><p>Composed by ${composer}. Interpreter: ${interp}.</p>
${intro}<audio controls src="${player}"></audio>
<div class="row">${downloads}
<a href="/records/${jid}.json">Download record</a><a href="/api/jobs/${jid}/files/plan.json">Download plan</a>
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
  if (low.endsWith(".wav")) return "audio/wav";
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
  headers.set("access-control-allow-headers", "Content-Type, Authorization, X-NC-Tester");
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
