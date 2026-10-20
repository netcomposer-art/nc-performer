import { test } from "node:test";
import assert from "node:assert/strict";
import { CANCELLED_MESSAGE, cancelAction, cancelToken, queueErrorIsCancel } from "./cancel.ts";

test("cancel accepts the claim token or the same secret under keep_token", () => {
  assert.equal(cancelToken({ claim_token: "  abc  " }), "abc");
  assert.equal(cancelToken({ keep_token: "kept" }), "kept");
  assert.equal(cancelToken({ claim_token: "claim", keep_token: "kept" }), "claim");
  assert.equal(cancelToken({}), "");
  assert.equal(cancelToken(null), "");
  assert.equal(cancelToken({ claim_token: "   " }), "");
});

test("a finished or refused job is left alone, and a repeat stop is a stop", () => {
  assert.equal(cancelAction("done"), "noop");
  assert.equal(cancelAction("failed"), "noop");
  assert.equal(cancelAction("rejected"), "noop");
  assert.equal(cancelAction("queued"), "stop");
  assert.equal(cancelAction("planning"), "stop");
  assert.equal(cancelAction("storing"), "stop");
  assert.equal(cancelAction("cancelled"), "stop");
  assert.equal(CANCELLED_MESSAGE, "Stopped.");
});

test("a container error after Stop does not become a failure", () => {
  assert.equal(queueErrorIsCancel("cancelled"), true);
  assert.equal(queueErrorIsCancel("planning"), false);
  assert.equal(queueErrorIsCancel(undefined), false);
});
