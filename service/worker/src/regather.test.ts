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
