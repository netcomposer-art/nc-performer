import { test } from "node:test";
import assert from "node:assert/strict";
import { carriedCopies, chordInPlan, containerFiles, regatherRequest, slotPattern, withTimeout } from "./regather.ts";

test("a re-gather names a Gather and a chord", () => {
  assert.equal(regatherRequest({}), null);
  assert.deepEqual(regatherRequest({ regather_gather_id: "g-abc123def456", regather_chord: 2 }), { gather_id: "g-abc123def456", chord: 2 });
  assert.deepEqual(regatherRequest({ regather_gather_id: "h-abc123def456", regather_chord: 2 }), { error: "not found", status: 404 });
  for (const chord of [-1, 1.5, "2", null, true]) {
    assert.equal((regatherRequest({ regather_gather_id: "g-abc123def456", regather_chord: chord }) as { status: number }).status, 400);
  }
  assert.ok(chordInPlan({ clips: [{}, {}, {}] }, 2));
  assert.ok(!chordInPlan({ clips: [{}, {}, {}] }, 3));
  assert.ok(!chordInPlan(null, 0));
});

test("the slot is the chord, its bass and its notes, and nothing else", () => {
  const slot = slotPattern(1);
  for (const sid of ["c1", "c1b", "c1n0", "c1n12"]) assert.ok(slot.test(sid), sid);
  for (const sid of ["c10", "c11n0", "c0", "c1x", "c12b"]) assert.ok(!slot.test(sid), sid);
});

test("every other chord's audio is copied R2 to R2", () => {
  const keys = [
    "gathers/g-old111111111/audio/c0",
    "gathers/g-old111111111/audio/c1",
    "gathers/g-old111111111/audio/c1n0",
    "gathers/g-old111111111/audio/c1b",
    "gathers/g-old111111111/audio/c10",
    "gathers/g-old111111111/audio/c2n3",
    "gathers/g-old111111111/collection.json",
  ];
  assert.deepEqual(carriedCopies(keys, "g-old111111111", "g-new222222222", 1), [
    ["gathers/g-old111111111/audio/c0", "gathers/g-new222222222/audio/c0"],
    ["gathers/g-old111111111/audio/c10", "gathers/g-new222222222/audio/c10"],
    ["gathers/g-old111111111/audio/c2n3", "gathers/g-new222222222/audio/c2n3"],
  ]);
});

test("the container is asked only for the new chord's files", () => {
  const files = [{ sid: "c0", carried: true }, { sid: "c1" }, { sid: "c2", carried: true }];
  assert.deepEqual(containerFiles({}, files), files);
  assert.deepEqual(containerFiles({ regather: { sids: ["c1"] } }, files), [{ sid: "c1" }]);
});

test("a slow container call is given up", async () => {
  await assert.rejects(withTimeout(new Promise(() => {}), 20, "seeding"), /seeding took longer than 0 s/);
  assert.equal(await withTimeout(Promise.resolve(4), 50, "x"), 4);
});

import { applyPerformHit, applyRegatherHit, PER_10MIN, PER_DAY } from "./policy.ts";
import { regatherProblem, REGATHER_WORDS_ERROR } from "./regather.ts";

const T = 1_800_000_000;
const spread = (n: number, from: number, step: number) => Array.from({ length: n }, (_v, i) => from + i * step);

test("a re-gather does not use the daily count", () => {
  // Ten full Gathers today, none in the last 10 minutes: a full Gather is refused, a re-gather is not.
  const day = spread(PER_DAY, T - 80_000, 7_000);
  assert.match(String(applyPerformHit(T, day).error), /Daily limit/);
  const ok = applyRegatherHit(T, day, [], PER_10MIN);
  assert.equal(ok.error, null);
  // Re-gathers never fill the day for a full Gather either.
  const regathers = spread(2, T - 300, 100);
  assert.equal(applyPerformHit(T, spread(PER_DAY - 1, T - 80_000, 7_000), PER_10MIN, PER_DAY, regathers).error, null);
});

test("the 10-minute limit still applies to re-gathers", () => {
  assert.match(String(applyRegatherHit(T, [], spread(PER_10MIN, T - 500, 100), PER_10MIN).error), /3 every 10 minutes/);
  assert.match(String(applyRegatherHit(T, [T - 60], spread(PER_10MIN - 1, T - 500, 100), PER_10MIN).error), /every 10 minutes/);
  // And a full Gather after three re-gathers waits too.
  assert.match(String(applyPerformHit(T, [], PER_10MIN, PER_DAY, spread(PER_10MIN, T - 500, 100)).error), /every 10 minutes/);
  // Older than 10 minutes: free again.
  assert.equal(applyRegatherHit(T, [], spread(PER_10MIN, T - 2000, 100), PER_10MIN).error, null);
});

test("a full Gather cannot pass as a re-gather", () => {
  for (const key of ["composition", "words", "performance", "prompt", "title", "instrument", "exclude_gather_id", "seed"]) {
    const got = regatherRequest({ regather_gather_id: "g-abc123def456", regather_chord: 0, claim_token: "k", [key]: "Am F C G" }) as { error: string; status: number };
    assert.deepEqual(got, { error: REGATHER_WORDS_ERROR, status: 400 }, key);
  }
  const parent = {
    composition: { composition: "Dm9 G13 Cadd9" },
    plan: { clips: [{ id: "c0" }, { id: "c1" }, { id: "c2" }], sources: { c0: { u: 1 }, c1: { u: 2 }, c1n0: { u: 3 }, c2: { u: 4 } } },
    sounds: [{ title: "a", url: "/api/jobs/g-old/audio/c0" }, { title: "b" }, { title: "c" }],
  };
  const good = {
    ...parent,
    plan: { clips: parent.plan.clips, sources: { c0: { u: 1 }, c1: { u: 9 }, c2: { u: 4 } } },
    sounds: [{ title: "a", url: "/api/jobs/g-new/audio/c0" }, { title: "new" }, { title: "c" }],
    regather: { from: "g-old", chord: 1 },
  };
  const asked = { gather_id: "g-old", chord: 1 };
  assert.equal(regatherProblem(asked, parent, good), null);
  assert.ok(regatherProblem(null, parent, good));
  assert.ok(regatherProblem(asked, null, good));
  assert.ok(regatherProblem({ gather_id: "g-old", chord: 2 }, parent, good));
  assert.ok(regatherProblem(asked, parent, { ...good, composition: { composition: "Am F C G" } }));
  assert.ok(regatherProblem(asked, parent, { ...good, plan: { ...good.plan, clips: [...good.plan.clips, { id: "c3" }] } }));
  assert.match(String(regatherProblem(asked, parent, { ...good, plan: { ...good.plan, sources: { ...good.plan.sources, c2: { u: 5 } } } })), /chord 3/);
  assert.match(String(regatherProblem(asked, parent, { ...good, sounds: [{ title: "z" }, { title: "new" }, { title: "c" }] })), /chord 1/);
});

import { readFileSync } from "node:fs";

test("a re-gather still passes the bot check and the burst guard", () => {
  const worker = readFileSync(new URL("./index.ts", import.meta.url), "utf8");
  const start = worker.indexOf("async function gather(");
  const body = worker.slice(start, worker.indexOf("\nasync function ", start + 10));
  const gate = body.indexOf("await turnstile(env, turnstileToken(body))");
  const denied = body.indexOf("if (!gate.ok) return json(turnstileDeniedBody(gate), 403)");
  const again = body.indexOf("regatherRequest(body)");
  assert.ok(gate > 0 && denied > gate && again > denied, "Turnstile is checked before the re-gather is read");
  assert.match(body, /rateLimit\(env, ipHash, tester, regather \? "regather" : "perform"\)/);
  const limiter = worker.slice(worker.indexOf("async function rateLimit("), worker.indexOf("function rateJson("));
  assert.ok(limiter.indexOf("PERFORM_RATE.limit") < limiter.indexOf("if (tester) return null"), "the burst binding runs for every kind");
  assert.match(limiter, /hit\(ipHash, PER_10MIN, PER_DAY, kind\)/);
});
