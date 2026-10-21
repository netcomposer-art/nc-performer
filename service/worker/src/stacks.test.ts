import { test } from "node:test";
import assert from "node:assert/strict";
import { chordLabel, lostStacks, missingStacksMessage, recoverGatherAudio, stackIds, stackParts, withRetry, type StackDoc } from "./stacks.ts";

const doc: StackDoc = {
  plan: {
    clips: [
      { id: "c0", chord: 0, symbol: "Dm9", sids: ["c0n0", "c0n1", "c0n2", "c0n3"] },
      { id: "c1", chord: 1, symbol: "G13", sids: ["c1n0", "c1n1", "c1n2", "c1n3"] },
      { id: "c2", chord: 2, symbol: "Cadd9", sids: ["c2"] },
    ],
  },
  sounds: [
    { id: "c0", clip_id: "c0", pos: 0, symbol: "Dm9" },
    { id: "c1", clip_id: "c1", pos: 1, symbol: "G13" },
    { id: "c2", clip_id: "c2", pos: 2, symbol: "Cadd9" },
  ],
  files: [
    { sid: "c0", url: null, ext: ".wav" },
    { sid: "c0n0", url: "https://theremin.music.uiowa.edu/a.aif#note=50", ext: ".aif" },
    { sid: "c2", url: "https://audio.musicmatter.art/fat/Cadd9.flac", ext: ".flac" },
  ],
};
const bytes = (n: number) => new Uint8Array(n).fill(1).buffer;
const noSleep = async () => {};

test("stacks are clips with more than one note", () => {
  assert.deepEqual(stackIds(doc.plan), ["c0", "c1"]);
  assert.deepEqual(stackParts(doc.plan, "c1"), ["c1n0", "c1n1", "c1n2", "c1n3"]);
  assert.deepEqual(stackParts(doc.plan, "c2"), []);
  assert.deepEqual(stackParts(doc.plan, "nope"), []);
});

test("a copy is tried three times, and a throw is a failed try", async () => {
  let calls = 0;
  const waits: number[] = [];
  const got = await withRetry(async () => {
    calls += 1;
    if (calls === 1) throw new Error("container HTTP 503");
    return calls === 3;
  }, [250, 1000], async (ms) => { waits.push(ms); });
  assert.deepEqual(got, { ok: true, tries: 3, error: "" });
  assert.deepEqual(waits, [250, 1000]);
  const never = await withRetry(async () => false, [1, 1], noSleep);
  assert.equal(never.ok, false);
  assert.equal(never.tries, 3);
});

test("a stack is lost when its copy failed or it has no file row", () => {
  assert.deepEqual(lostStacks(doc, []), ["c1"]);
  assert.deepEqual(lostStacks(doc, ["c0", "c2"]), ["c0", "c1"]);
});

test("the page sentence names the chord from one", () => {
  assert.equal(chordLabel(doc, "c1"), "chord 2 (G13)");
  assert.equal(missingStacksMessage(doc, ["c1"]), "Could not store chord 2 (G13) from this Gather. Gather again.");
  assert.equal(missingStacksMessage(doc, ["c0", "c1"]), "Could not store chord 1 (Dm9) and chord 2 (G13) from this Gather. Gather again.");
});

test("a missing stack is mixed again and kept, not a 404", async () => {
  const stored: number[] = [];
  const asked: string[] = [];
  const got = await recoverGatherAudio(doc, "c0", {
    upstream: async () => { throw new Error("a stack has no source URL"); },
    rebuild: async (cid) => { asked.push(cid); return bytes(64); },
    store: async (b) => { stored.push(b.byteLength); },
  });
  assert.equal(got?.via, "rebuilt");
  assert.deepEqual(asked, ["c0"]);
  assert.deepEqual(stored, [64]);
});

test("a stack that cannot be rebuilt stays a 404 and stores nothing", async () => {
  const stored: number[] = [];
  for (const rebuild of [async () => null, async () => { throw new Error("422"); }, async () => bytes(0)]) {
    const got = await recoverGatherAudio(doc, "c1", { upstream: async () => bytes(8), rebuild, store: async (b) => { stored.push(b.byteLength); } });
    assert.equal(got, null);
  }
  assert.deepEqual(stored, []);
});

test("a single file still comes from its https source", async () => {
  const urls: string[] = [];
  const got = await recoverGatherAudio(doc, "c2", {
    upstream: async (url) => { urls.push(url); return bytes(16); },
    rebuild: async () => { throw new Error("not a stack"); },
    store: async () => {},
  });
  assert.equal(got?.via, "upstream");
  assert.deepEqual(urls, ["https://audio.musicmatter.art/fat/Cadd9.flac"]);
  assert.equal(await recoverGatherAudio(doc, "zz", { upstream: async () => bytes(1), rebuild: async () => bytes(1), store: async () => {} }), null);
  assert.equal(await recoverGatherAudio(null, "c0", { upstream: async () => bytes(1), rebuild: async () => bytes(1), store: async () => {} }), null);
});
