import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  applyPerformHit,
  BURST_LIMIT,
  publicInternalError,
  BURST_PERIOD_S,
  PER_10MIN,
  PER_DAY,
  WINDOW_S,
  type Counter,
  type DayCounter,
} from "./policy.ts";

const T0 = 1_700_000_000;
const DAY = "2026-10-19";

function take(now: number, window: Counter | null, dayHit: DayCounter | null) {
  const next = applyPerformHit(now, DAY, window, dayHit);
  assert.equal(next.error, null);
  return next;
}

test("three performs are allowed, the fourth in the same 10 minutes is not", () => {
  let window: Counter | null = null;
  let dayHit: DayCounter | null = null;
  for (let i = 0; i < PER_10MIN; i++) {
    const next = take(T0 + i, window, dayHit);
    window = next.window;
    dayHit = next.dayHit;
  }
  assert.equal(window?.n, 3);
  assert.equal(dayHit?.n, 3);

  const blocked = applyPerformHit(T0 + 30, DAY, window, dayHit);
  assert.match(blocked.error || "", /3 every 10 minutes/);
  assert.equal(blocked.window.n, 3);
  assert.equal(blocked.dayHit.n, 3);
});

test("a fresh 10-minute window still counts toward the daily cap", () => {
  let window: Counter | null = null;
  let dayHit: DayCounter | null = null;
  for (let i = 0; i < PER_DAY; i++) {
    const next = take(T0 + i * WINDOW_S, window, dayHit);
    window = next.window;
    dayHit = next.dayHit;
  }
  assert.equal(dayHit?.n, PER_DAY);
  const blocked = applyPerformHit(T0 + PER_DAY * WINDOW_S, DAY, window, dayHit);
  assert.match(blocked.error || "", /Daily limit reached \(10 Performs/);
  assert.equal(blocked.dayHit.n, PER_DAY);
  assert.equal(blocked.window.n, 0);
});

test("a daily rejection does not consume the 10-minute window", () => {
  const window = { start: T0, n: 1 };
  const dayHit = { day: DAY, n: PER_DAY };
  const blocked = applyPerformHit(T0 + 5, DAY, window, dayHit);
  assert.match(blocked.error || "", /Daily limit/);
  assert.equal(blocked.window.n, 1);
  assert.equal(blocked.dayHit.n, PER_DAY);
});

test("the next UTC day starts a new daily count inside an open 10-minute window", () => {
  const window = { start: T0, n: PER_10MIN };
  const dayHit = { day: DAY, n: PER_DAY };
  const stillToday = applyPerformHit(T0 + 10, DAY, window, dayHit);
  assert.match(stillToday.error || "", /10 minutes/);
  const tomorrow = applyPerformHit(T0 + 10, "2026-10-20", window, dayHit);
  assert.match(tomorrow.error || "", /10 minutes/);
  assert.equal(tomorrow.dayHit.n, 0);
  const later = applyPerformHit(T0 + WINDOW_S, "2026-10-20", window, dayHit);
  assert.equal(later.error, null);
  assert.equal(later.window.n, 1);
  assert.equal(later.dayHit.day, "2026-10-20");
  assert.equal(later.dayHit.n, 1);
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
