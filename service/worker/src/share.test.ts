import { test } from "node:test";
import assert from "node:assert/strict";
import { newShareId, SHARE_CACHE, SHARE_MAX_BYTES, shareProblem, shareRoute, type ShareDeps } from "./share.ts";

const good = { v: 1, kind: "preset", format: "netcomposer.synth", fv: 3, data: { vco: { wave: "saw" } }, name: "Glass pad", by: "Peter" };

function deps(over: Partial<ShareDeps> = {}) {
  const store = new Map<string, { body: string; meta: Record<string, string> }>();
  const d: ShareDeps = {
    get: async (k) => (store.has(k) ? { text: async () => store.get(k)!.body } : null),
    put: async (k, body, meta) => void store.set(k, { body, meta }),
    allow: async () => true,
    now: () => new Date("2026-11-25T12:00:00Z"),
    ...over,
  };
  return { d, store };
}

const post = (body: string, origin = "https://netcomposer.art") =>
  new Request("https://nc-perform-api.psemme.workers.dev/api/share", { method: "POST", body, headers: { Origin: origin, "Content-Type": "application/json" } });
const get = (id: string, origin = "https://netcomposer.art") =>
  new Request(`https://nc-perform-api.psemme.workers.dev/api/share/${id}`, { headers: { Origin: origin } });

test("a good envelope is stored under share/ and read back as it was sent", async () => {
  const { d, store } = deps();
  const res = (await shareRoute(post(JSON.stringify(good)), d))!;
  assert.equal(res.status, 201);
  assert.equal(res.headers.get("access-control-allow-origin"), "https://netcomposer.art");
  const { id } = (await res.json()) as { id: string };
  assert.match(id, /^[0-9a-z]{10}$/);
  assert.deepEqual([...store.keys()], [`share/${id}.json`]);
  assert.deepEqual(store.get(`share/${id}.json`)!.meta, { created: "2026-11-25T12:00:00.000Z", format: "netcomposer.synth" });
  const back = (await shareRoute(get(id), d))!;
  assert.equal(back.status, 200);
  assert.equal(back.headers.get("cache-control"), SHARE_CACHE);
  assert.deepEqual(await back.json(), good);
});

test("an unknown id is 404 and other paths are not this route", async () => {
  const { d } = deps();
  assert.equal((await shareRoute(get("0123456789"), d))!.status, 404);
  assert.equal(await shareRoute(get("../jobs/x"), d), null); // the URL normalizes to /api/jobs/x: not this route
  assert.equal((await shareRoute(get("ABCDEFGHIJ"), d))!.status, 404);
  assert.equal(await shareRoute(new Request("https://x.dev/api/notes/piano/60.opus"), d), null);
  assert.equal(await shareRoute(new Request("https://x.dev/api/shared"), d), null);
});

test("envelope rules: only presets of the five instruments, short names, our own remix links", () => {
  assert.equal(shareProblem(good), null);
  assert.equal(shareProblem({ ...good, from: "https://netcomposer.art/synth/#s=0123456789" }), null);
  assert.ok(shareProblem(null));
  assert.ok(shareProblem([good]));
  assert.ok(shareProblem({ ...good, v: 2 }));
  assert.ok(shareProblem({ ...good, kind: "scene" }));
  assert.ok(shareProblem({ ...good, format: "netcomposer.lucy" }));
  assert.ok(shareProblem({ ...good, data: null }));
  assert.ok(shareProblem({ ...good, data: "x" }));
  assert.ok(shareProblem({ ...good, fv: "3" }));
  assert.ok(shareProblem({ ...good, name: "x".repeat(81) }));
  assert.ok(shareProblem({ ...good, by: "x".repeat(41) }));
  assert.ok(shareProblem({ ...good, from: "https://evil.example/synth/" }));
  assert.ok(shareProblem({ ...good, from: "http://netcomposer.art/synth/" }));
  assert.equal(shareProblem({ ...good, from: "https://triki.art/synth/#s=0123456789" }), null);
  assert.ok(shareProblem({ ...good, from: "https://www.triki.art/synth/" }));
  assert.ok(shareProblem({ ...good, from: "https://triki.art.evil.example/synth/" }));
  for (const f of ["netcomposer.synth", "netcomposer.rhythm", "netcomposer.bass", "netcomposer.nyx", "netcomposer.calliope"]) {
    assert.equal(shareProblem({ ...good, format: f }), null, f);
  }
});

test("bad bodies are refused and nothing is stored", async () => {
  const { d, store } = deps();
  assert.equal((await shareRoute(post("not json"), d))!.status, 400);
  assert.equal((await shareRoute(post(JSON.stringify({ ...good, format: "netcomposer.lucy" })), d))!.status, 400);
  const big = JSON.stringify({ ...good, data: { blob: "x".repeat(SHARE_MAX_BYTES) } });
  assert.equal((await shareRoute(post(big), d))!.status, 413);
  assert.equal(store.size, 0);
});

test("a caller over the burst limit gets 429 and nothing is stored", async () => {
  const { d, store } = deps({ allow: async () => false });
  const res = (await shareRoute(post(JSON.stringify(good)), d))!;
  assert.equal(res.status, 429);
  assert.equal(((await res.json()) as { code: string }).code, "rate");
  assert.equal(store.size, 0);
});

test("other origins can read a link too (any page may open one)", async () => {
  const { d } = deps();
  const { id } = (await (await shareRoute(post(JSON.stringify(good)), d))!.json()) as { id: string };
  const res = (await shareRoute(get(id, "https://example.com"), d))!;
  assert.equal(res.headers.get("access-control-allow-origin"), "*");
});

test("ids are 10 unbiased base-36 characters", () => {
  let n = 0;
  const seq = () => Uint8Array.from({ length: 20 }, () => (n++ % 2 ? 255 : 35)); // 255 is skipped, 35 is "z"
  assert.equal(newShareId(seq), "zzzzzzzzzz");
  const seen = new Set(Array.from({ length: 200 }, () => newShareId()));
  assert.equal(seen.size, 200);
});

test("the preset library's category and tags travel, checked as the site checks them", () => {
  assert.equal(shareProblem({ ...good, category: "Pads & Strings", tags: ["warm", "slow attack"] }), null);
  assert.equal(shareProblem({ ...good, category: "" }), "Bad category.");
  assert.equal(shareProblem({ ...good, tags: ["x".repeat(25)] }), "Bad tags.");
  assert.equal(shareProblem({ ...good, tags: Array.from({ length: 9 }, (_, i) => `t${i}`) }), "Bad tags.");
});
