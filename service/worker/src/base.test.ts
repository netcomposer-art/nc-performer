import { test } from "node:test";
import assert from "node:assert/strict";
import { containerBase } from "./base.ts";

const WORKER = "https://nc-perform-api.psemme.workers.dev";

test("the configured base wins", () => {
  assert.equal(containerBase("https://perform.example/", WORKER), "https://perform.example");
});

test("an unset base falls back to the Worker's own origin, never empty", () => {
  assert.equal(containerBase(undefined, WORKER), WORKER);
  assert.equal(containerBase("", WORKER + "/"), WORKER);
  assert.equal(containerBase("   ", new URL(WORKER + "/api/warm").origin), WORKER);
});

test("junk is not a base", () => {
  assert.equal(containerBase("not a url", WORKER), WORKER);
  assert.equal(containerBase("ftp://x.example", WORKER), WORKER);
  assert.equal(containerBase(null, null), "");
});
