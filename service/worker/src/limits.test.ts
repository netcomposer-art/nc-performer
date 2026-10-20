import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  applyPerformHit,
  BURST_LIMIT,
  DAY_S,
  DEFAULT_LIMITS,
  forPage,
  isInternalFailure,
  jobStatusFields,
  proxyHosts,
  publicInternalError,
  rejectPlan,
  BURST_PERIOD_S,
  PER_10MIN,
  PER_DAY,
  testerPass,
  WINDOW_S,
  corsHeaders,
  publishedRegistryStats,
  registryPublicStats,
} from "./policy.ts";

const T0 = 1_700_000_000;

function take(now: number, hits: number[]): number[] {
  const next = applyPerformHit(now, hits);
  assert.equal(next.error, null);
  return next.hits;
}

test("three performs are allowed, the fourth in the same 10 minutes is not", () => {
  let hits: number[] = [];
  for (let i = 0; i < PER_10MIN; i++) hits = take(T0 + i, hits);
  assert.equal(hits.length, 3);

  const blocked = applyPerformHit(T0 + 30, hits);
  assert.match(blocked.error || "", /3 every 10 minutes/);
  assert.match(blocked.error || "", /Try again after \d{1,2}:\d{2} [AP]M UTC/);
  assert.equal(blocked.hits.length, 3);
  assert.equal(blocked.retry_at, new Date((T0 + WINDOW_S) * 1000).toISOString().replace(/\.\d{3}Z$/, "Z"));
  assert.equal(blocked.retry_after_s, WINDOW_S - 30);
});

test("a fresh 10-minute window still counts toward the rolling daily cap", () => {
  let hits: number[] = [];
  for (let i = 0; i < PER_DAY; i++) hits = take(T0 + i * WINDOW_S, hits);
  assert.equal(hits.length, PER_DAY);
  const now = T0 + PER_DAY * WINDOW_S;
  const blocked = applyPerformHit(now, hits);
  assert.match(blocked.error || "", /Daily limit reached \(10 Performs from this network\)\. Try again after \d{1,2}:\d{2} [AP]M UTC\./);
  assert.equal(blocked.error?.includes("tomorrow"), false);
  assert.equal(blocked.hits.length, PER_DAY);
  const free = T0 + DAY_S;
  assert.equal(blocked.retry_at, new Date(free * 1000).toISOString().replace(/\.\d{3}Z$/, "Z"));
  assert.equal(blocked.retry_after_s, free - now);
});

test("a daily rejection does not consume another hit", () => {
  const hits = Array.from({ length: PER_DAY }, (_, i) => T0 - (PER_DAY - 1 - i) * WINDOW_S);
  const blocked = applyPerformHit(T0 + 5, hits);
  assert.match(blocked.error || "", /Daily limit/);
  assert.equal(blocked.error?.includes("10 minutes"), false);
  assert.equal(blocked.hits.length, PER_DAY);
  assert.equal(blocked.retry_after_s != null && blocked.retry_after_s > 0, true);
});

test("a hit older than 24 hours drops out and one from 23 hours ago still counts", () => {
  const recent = T0 - 23 * 3600;
  let hits: number[] = [];
  for (let i = 0; i < PER_DAY; i++) hits = take(recent + i * WINDOW_S, hits);
  const blocked = applyPerformHit(T0, hits);
  assert.match(blocked.error || "", /Daily limit reached \(10 Performs/);
  assert.equal(blocked.error?.includes("tomorrow"), false);
  const freed = applyPerformHit(recent + DAY_S + 1, hits);
  assert.equal(freed.error, null);
  assert.equal(freed.hits[0] > recent, true);
});

test("the tester token matches only the whole secret", async () => {
  assert.equal(await testerPass("correct-token", "correct-token"), true);
  assert.equal(await testerPass("  correct-token  ", "correct-token"), true);
  assert.equal(await testerPass("wrong-token", "correct-token"), false);
  assert.equal(await testerPass("correct-token-extra", "correct-token"), false);
  assert.equal(await testerPass("correct", "correct-token"), false);
  assert.equal(await testerPass(null, "correct-token"), false);
  assert.equal(await testerPass("", "correct-token"), false);
  assert.equal(await testerPass("correct-token", ""), false);
  assert.equal(await testerPass("correct-token", null), false);
  assert.equal(await testerPass("correct-token", undefined), false);
});

test("internal failures stay a short sentence and keep the job id", () => {
  const id = "h-abcdefghjkmn";
  const shown = publicInternalError(id);
  assert.match(shown, /Something went wrong while performing/);
  assert.match(shown, new RegExp(id));
  assert.equal(shown.includes("ContainerProxy"), false);
  assert.equal(shown.includes("undefined"), false);
  assert.equal(publicInternalError(null).includes("("), false);
});

test("proxy allowlist covers subdomains the exact apex would miss", () => {
  const hosts = proxyHosts(["musicmatter.art", "audio.musicmatter.art", "*.example.com", ""]);
  assert.ok(hosts.includes("musicmatter.art"));
  assert.ok(hosts.includes("*.musicmatter.art"));
  assert.ok(hosts.includes("audio.musicmatter.art"));
  assert.ok(hosts.includes("*.audio.musicmatter.art"));
  assert.ok(hosts.includes("*.example.com"));
  assert.equal(hosts.includes("*.*.example.com"), false);
});

test("raw assembly keeps one file per chord past the layered source cap", () => {
  const sources: Record<string, { kind: string }> = {};
  for (let i = 0; i < 35; i++) sources["c" + i] = { kind: "supplier" };
  const plan = {
    assembly: "raw",
    sources,
    chords: Array.from({ length: 35 }, () => ({ t: 0 })),
    layers: [{}],
    length_s: 120,
    tail_s: 0,
  };
  assert.equal(rejectPlan(plan), null);
  const layered = { ...plan, assembly: "layered" };
  assert.match(rejectPlan(layered) || "", /at most 24 sources/);
});

test("a Performance can have 16 layers and not 17", () => {
  assert.equal(DEFAULT_LIMITS.maxLayers, 16);
  const plan = { sources: {}, chords: [{ t: 0 }], layers: Array.from({ length: 16 }, () => ({})), length_s: 10, tail_s: 0 };
  assert.equal(rejectPlan(plan), null);
  plan.layers.push({});
  assert.match(rejectPlan(plan) || "", /at most 16 layers/);
});

test("a raw HTTP 520 is replaced and a license rejection is kept", () => {
  const id = "h-abcdefghjkmn";
  assert.equal(isInternalFailure("HTTP Error 520: Server Error"), true);
  assert.equal(isInternalFailure("Origin is disallowed"), true);
  const shown = forPage(id, "HTTP Error 520: Server Error");
  assert.match(shown || "", /Something went wrong while performing/);
  assert.match(shown || "", new RegExp(id));
  assert.equal(shown?.includes("520"), false);
  assert.equal(shown?.includes("HTTP Error"), false);
  assert.equal(forPage(id, "license not allowed: CC-BY-NC"), "license not allowed: CC-BY-NC");
  assert.equal(forPage(id, null), null);
});

function parseJsonc(text: string): Record<string, unknown> {
  const stripped = text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  return JSON.parse(stripped) as Record<string, unknown>;
}

test("wrangler rate limit is a 10-or-60 second burst, not the 10-minute window", () => {
  const file = join(dirname(fileURLToPath(import.meta.url)), "..", "wrangler.jsonc");
  const cfg = parseJsonc(readFileSync(file, "utf8"));
  const rates = cfg.ratelimits as { name: string; simple: { limit: number; period: number } }[];
  assert.equal(rates.length, 1);
  assert.equal(rates[0].name, "PERFORM_RATE");
  assert.equal(rates[0].simple.limit, BURST_LIMIT);
  assert.equal(rates[0].simple.period, BURST_PERIOD_S);
  assert.ok(rates[0].simple.period === 10 || rates[0].simple.period === 60);
  assert.notEqual(rates[0].simple.period, WINDOW_S);

  const r2 = cfg.r2_buckets as { binding: string; bucket_name: string }[];
  assert.deepEqual(r2, [{ binding: "PERFORMANCES", bucket_name: "nc-performances" }]);

  const containers = cfg.containers as {
    class_name: string;
    image: string;
    image_build_context: string;
    max_instances: number;
    instance_type: string;
  }[];
  assert.equal(containers[0].class_name, "RenderContainer");
  assert.equal(containers[0].image, "../Dockerfile");
  assert.equal(containers[0].image_build_context, "../..");
  assert.equal(containers[0].max_instances, 3);
  assert.equal(containers[0].instance_type, "standard-3");

  const migrations = cfg.migrations as { tag: string; new_sqlite_classes: string[] }[];
  assert.deepEqual(migrations[0].new_sqlite_classes, ["JobDO", "BudgetDO", "RenderContainer"]);

  const queues = cfg.queues as {
    producers: { binding: string; queue: string }[];
    consumers: { queue: string; max_batch_size: number; max_concurrency: number; max_retries: number }[];
  };
  assert.deepEqual(queues.producers, [{ binding: "PERFORM_JOBS", queue: "perform-jobs" }]);
  assert.equal(queues.consumers[0].queue, "perform-jobs");
  assert.equal(queues.consumers[0].max_batch_size, 1);
  assert.equal(queues.consumers[0].max_concurrency, 3);
  assert.equal(queues.consumers[0].max_retries, 1);

  const objects = cfg.durable_objects as { bindings: { name: string; class_name: string }[] };
  assert.deepEqual(objects.bindings, [
    { name: "JOBS", class_name: "JobDO" },
    { name: "BUDGET", class_name: "BudgetDO" },
    { name: "RENDER", class_name: "RenderContainer" },
  ]);
});

test("registry stats are counts only", () => {
  assert.deepEqual(registryPublicStats({ size: 12, ok: 9, rejected: 3, checker_version: 1, recheck_days: 90 }), {
    size: 12,
    ok: 9,
    rejected: 3,
    checker_version: 1,
    recheck_days: 90,
  });
  assert.deepEqual(registryPublicStats(null), {
    size: 0,
    ok: 0,
    rejected: 0,
    checker_version: null,
    recheck_days: null,
  });
  assert.equal(registryPublicStats({ size: 4, token: "secret" }).size, 4);
  assert.deepEqual(publishedRegistryStats(null), {
    size: 10334,
    ok: 8977,
    rejected: 1357,
    checker_version: 1,
    recheck_days: 90,
  });
  assert.equal(publishedRegistryStats({ size: 4, ok: 4, rejected: 0 }).size, 4);
});

test("public job status always has trimmed, seed, found sounds, and a stage line", () => {
  const planning = jobStatusFields({
    stage: "planning",
    seed: 11,
    trimmed: null,
    stage_line: "Finding a choir on Freesound…",
    found_sounds: [
      { title: "Choir", supplier: "Freesound", license: "CC0-1.0" },
      { title: "", supplier: "Freesound", license: "CC0-1.0" },
      { title: "Choir", supplier: "Freesound", license: "CC0-1.0" },
    ],
  });
  assert.equal(planning.seed, 11);
  assert.equal(planning.trimmed, null);
  assert.equal(planning.stage_line, "Finding a choir on Freesound…");
  assert.deepEqual(planning.found_sounds, [{ title: "Choir", supplier: "Freesound", license: "CC0-1.0" }]);

  const idle = jobStatusFields({ stage: "queued", position: 2, seed: 4 });
  assert.equal(idle.trimmed, null);
  assert.deepEqual(idle.found_sounds, []);
  assert.equal(idle.stage_line, "Queued (position 2)");
  assert.equal(idle.seed, 4);
  assert.equal(planning.tester, false);
  assert.equal(idle.tester, false);
  assert.equal(jobStatusFields({ stage: "queued", tester: true }).tester, true);
});

test("a chord row keeps its position and preview, and a later pick replaces that chord", () => {
  const live = jobStatusFields({
    stage: "gathering",
    found_sounds: [
      { title: "Choir Cm", supplier: "Musicmatter", license: "CC-BY-SA-4.0", chord: 0, id: "c0", preview_url: "/api/jobs/g-0123456789ab/audio/c0", symbol: "Cm" },
      { title: "Choir F", supplier: "Musicmatter", license: "CC-BY-SA-4.0", chord: 1, id: "c1", preview_url: "/api/jobs/g-0123456789ab/audio/c1", symbol: "F" },
      { title: "Piano Cm", supplier: "Musicmatter", license: "CC-BY-SA-4.0", chord: 0, id: "c0", preview_url: "/api/jobs/g-0123456789ab/audio/c0", symbol: "Cm" },
    ],
  });
  assert.deepEqual(live.found_sounds, [
    { title: "Piano Cm", supplier: "Musicmatter", license: "CC-BY-SA-4.0", chord: 0, id: "c0", preview_url: "/api/jobs/g-0123456789ab/audio/c0", symbol: "Cm" },
    { title: "Choir F", supplier: "Musicmatter", license: "CC-BY-SA-4.0", chord: 1, id: "c1", preview_url: "/api/jobs/g-0123456789ab/audio/c1", symbol: "F" },
  ]);
});

test("a gathered chord keeps the fields the page assembles with", () => {
  const live = jobStatusFields({
    stage: "gathered",
    found_sounds: [{
      title: "Choir Cm",
      supplier: "Musicmatter",
      license: "CC-BY-SA-4.0",
      chord: 0,
      id: "c0",
      preview_url: "/api/jobs/g-0123456789ab/audio/c0",
      symbol: "Cm",
      clip_id: "c0",
      pos: 0,
      url: "/api/jobs/g-0123456789ab/audio/c0",
      gain_db: 1.5,
      duration_s: 2.5,
      start_s: 0,
      end_s: 2.5,
      loudness_lufs: -21.5,
      credit: "Choir Cm by Ada",
    }],
  });
  assert.equal(live.found_sounds[0].gain_db, 1.5);
  assert.equal(live.found_sounds[0].duration_s, 2.5);
  assert.equal(live.found_sounds[0].clip_id, "c0");
  assert.equal(corsHeaders("https://netcomposer.art")["access-control-allow-origin"], "https://netcomposer.art");
  assert.equal(corsHeaders("https://play.netcomposer.art").vary, "Origin");
  assert.equal(corsHeaders("https://evil.example")["access-control-allow-origin"], "*");
  assert.equal(corsHeaders(null)["access-control-allow-origin"], "*");
});
