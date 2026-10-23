import { test } from "node:test";
import assert from "node:assert/strict";
import { chordLabel, fileSidFor, lostStacks, missingStacksMessage, readGatherClip, recoverGatherAudio, singleSid, stackIds, stackParts, withRetry, type StackDoc } from "./stacks.ts";

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

// [Cm(4) Fm9]x4 since #28: every Fm9 pass names the shared recording r1.
// Live gather g-vk3jbv94bhfh answered 404 for c1/c3/c5/c7 while audio/r1 was stored.
const loop: StackDoc = {
  plan: {
    clips: [
      { id: "c0", chord: 0, symbol: "Cm", sids: ["r0n0", "r0n1", "r0n2"] },
      { id: "c1", chord: 1, symbol: "Fm9", sids: ["r1"] },
      { id: "c2", chord: 2, symbol: "Cm", sids: ["r0n0", "r0n1", "r0n2"] },
      { id: "c3", chord: 3, symbol: "Fm9", sids: ["r1"] },
    ],
  },
  files: [
    { sid: "r0n0", url: "https://theremin.music.uiowa.edu/c3.aif#note=48", ext: ".aif" },
    { sid: "r1", url: "https://audio.musicmatter.art/fullchords/fatsynth/F/02_ninths_Fm9_root.flac", ext: ".flac" },
  ],
};

test("a single-file clip names its one stored file; a stack and an unknown id do not", () => {
  assert.equal(singleSid(loop.plan, "c3"), "r1");
  assert.equal(singleSid(loop.plan, "c1"), "r1");
  assert.equal(singleSid(doc.plan, "c2"), "c2");
  assert.equal(singleSid(loop.plan, "c0"), null);
  assert.equal(singleSid(loop.plan, "c9"), null);
  assert.equal(fileSidFor(loop, "c3"), "r1");
  assert.equal(fileSidFor(loop, "c0"), "c0");
  assert.equal(fileSidFor(loop, "r1"), "r1");
  assert.equal(fileSidFor(null, "c3"), "c3");
});

test("gatherAudio: c3 of a loop returns r1's stored bytes, not a 404", async () => {
  const r1 = bytes(331781);
  const r2 = new Map<string, ArrayBuffer>([["gathers/g-vk3jbv94bhfh/audio/r1", r1]]);
  const asked: string[] = [];
  const deps = {
    get: async (fileSid: string) => { asked.push(fileSid); return r2.get(`gathers/g-vk3jbv94bhfh/audio/${fileSid}`) ?? null; },
    upstream: async () => { throw new Error("stored bytes, no download"); },
    rebuild: async () => { throw new Error("not a stack"); },
    store: async () => { throw new Error("nothing to store"); },
  };
  for (const sid of ["c1", "c3"]) {
    const got = await readGatherClip(loop, sid, deps);
    assert.ok(got && "obj" in got, sid);
    assert.equal(got.fileSid, "r1");
    assert.equal(got.obj, r1);
  }
  assert.deepEqual(asked, ["r1", "r1"]);
  assert.equal(await readGatherClip(loop, "c9", deps), null);
});

test("gatherAudio: a loop pass missing from R2 downloads r1's source once and keeps it as r1", async () => {
  const urls: string[] = [];
  const stored: number[] = [];
  const got = await readGatherClip(loop, "c3", {
    get: async () => null,
    upstream: async (url) => { urls.push(url); return bytes(32); },
    rebuild: async () => { throw new Error("not a stack"); },
    store: async (b) => { stored.push(b.byteLength); },
  });
  assert.ok(got && "bytes" in got);
  assert.equal(got.fileSid, "r1");
  assert.equal(got.via, "upstream");
  assert.deepEqual(urls, ["https://audio.musicmatter.art/fullchords/fatsynth/F/02_ninths_Fm9_root.flac"]);
  assert.deepEqual(stored, [32]);
});

test("gatherAudio: a loop's stack is still rebuilt by its clip id", async () => {
  const asked: string[] = [];
  const got = await readGatherClip(loop, "c2", {
    get: async () => null,
    upstream: async () => null,
    rebuild: async (cid) => { asked.push(cid); return bytes(64); },
    store: async () => {},
  });
  assert.ok(got && "bytes" in got);
  assert.equal(got.fileSid, "c2");
  assert.equal(got.via, "rebuilt");
  assert.deepEqual(asked, ["c2"]);
});
