import { test } from "node:test";
import assert from "node:assert/strict";
import { mapPool } from "./pool.ts";

test("copies overlap instead of waiting for each file", async () => {
  const hopMs = 30;
  const files = 16;
  const started: number[] = [];
  const t0 = Date.now();
  await mapPool(Array.from({ length: files }, (_, i) => i), 8, async () => {
    started.push(Date.now() - t0);
    await new Promise((resolve) => setTimeout(resolve, hopMs));
  });
  const elapsed = Date.now() - t0;
  // Sixteen serial hops would be about 16 * 30 ms. Two waves of eight are about 60 ms.
  assert.equal(started.length, files);
  assert.ok(elapsed < hopMs * 6, `parallel copy took ${elapsed} ms`);
  const firstWave = started.filter((at) => at < hopMs).length;
  assert.ok(firstWave >= 8, `only ${firstWave} copies started in the first wave`);
});
