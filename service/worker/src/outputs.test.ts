import { test } from "node:test";
import assert from "node:assert/strict";
import {
  REQUIRED_OUTPUTS,
  STORING_LINE,
  containerPageUpdate,
  missingOutputs,
  readableBytes,
} from "./outputs.ts";

test("container done stays storing until the files are read back", () => {
  const view = containerPageUpdate({
    state: "done",
    stage: "done",
    percent: 100,
    stage_line: "Done",
  });
  assert.equal(view.state, "storing");
  assert.equal(view.stage, "storing");
  assert.equal(view.percent, 90);
  assert.equal(view.stage_line, STORING_LINE);
  assert.equal(view.stage_line, "Storing the Performance…");
});

test("a running stage is published as the container reported it", () => {
  const view = containerPageUpdate({
    state: "running",
    stage: "planning",
    percent: 20,
    stage_line: "Finding a piano on Freesound…",
  });
  assert.equal(view.state, "running");
  assert.equal(view.stage, "planning");
  assert.equal(view.percent, 20);
  assert.equal(view.stage_line, "Finding a piano on Freesound…");
});

test("a file is readable only when the read-back returns a size and a first byte", () => {
  assert.equal(readableBytes(12, 1), true);
  assert.equal(readableBytes(0, 1), false);
  assert.equal(readableBytes(12, 0), false);
  assert.equal(readableBytes(null, 4), false);
  assert.equal(readableBytes(4, null), false);
});

test("done requires every output, including the record, the preview, and meta", () => {
  const missing = missingOutputs(["performance.flac", "preview.mp3"]);
  assert.ok(missing.includes("record.json"));
  assert.ok(missing.includes("meta.json"));
  assert.ok(missing.includes("status.json"));
  assert.ok(missing.includes("plan.json"));
  assert.ok(missing.includes("composition.md"));
  assert.ok(missing.includes("uses.json"));
  assert.deepEqual(missingOutputs([...REQUIRED_OUTPUTS]), []);
  assert.equal(missingOutputs([...REQUIRED_OUTPUTS, "kept.wav"]).length, 0);
});
