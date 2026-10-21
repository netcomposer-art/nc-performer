import { test } from "node:test";
import assert from "node:assert/strict";
import { CONTAINER_END, SAVE_END, SaveMeter, doneProgress, keepForward, progressFields } from "./progress.ts";

test("container progress is read only when it is a number, and never shows 100", () => {
  assert.deepEqual(progressFields({ state: "gathering", percent: 40 }), {});
  assert.deepEqual(progressFields(null), {});
  const got = progressFields({ progress: 62.7, progress_stage: "downloading", progress_done: 3, progress_total: 8, progress_label: "Downloading the recordings" });
  assert.equal(got.progress, 62);
  assert.equal(got.progress_stage, "downloading");
  assert.equal(progressFields({ progress: 100 }).progress, SAVE_END);
});

test("saving runs from the container's end to 99, monotonic, one unit per file", () => {
  const meter = new SaveMeter(4, CONTAINER_END);
  const seen = [meter.fields().progress!];
  for (let i = 0; i < 4; i++) seen.push(meter.tick().progress!);
  assert.deepEqual(seen, [85, 88, 92, 95, 99]);
  meter.grow(2);
  seen.push(meter.fields().progress!, meter.tick().progress!, meter.tick().progress!, meter.tick().progress!);
  for (let i = 1; i < seen.length; i++) assert.ok(seen[i] >= seen[i - 1], `went back at ${i}: ${seen}`);
  assert.ok(seen.every((p) => p < 100));
  assert.equal(meter.fields().progress_done, 6);
  assert.equal(meter.fields().progress_total, 6);
});

test("an empty copy step still sits at the container's end, not 100", () => {
  assert.equal(new SaveMeter(0).fields().progress, CONTAINER_END);
});

test("a late, smaller patch does not move the job back", () => {
  const cur = { progress: 90, progress_stage: "saving" };
  assert.deepEqual(keepForward(cur, { progress: 88, progress_stage: "saving", state: "gathering" }), { state: "gathering" });
  assert.equal(keepForward(cur, { progress: 91 }).progress, 91);
  assert.deepEqual(keepForward(cur, { state: "gathering" }), { state: "gathering" });
  assert.equal(keepForward({}, { progress: 0 }).progress, 0);
});

test("100 is only the finished Gather", () => {
  assert.deepEqual(doneProgress(8), { progress: 100, progress_stage: "done", progress_label: "Gathered", progress_done: 8, progress_total: 8 });
});
