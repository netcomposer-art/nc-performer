import { test } from "node:test";
import assert from "node:assert/strict";
import { classifyLicense } from "./licenses.ts";
import { DEFAULT_CONFIG, normalizeConfig, rotateQueries } from "./config.ts";
import { authorizeUrl, BudgetExhausted, exchangeCode, FreesoundClient, RateLimited, refreshTokens, searchUrl } from "./freesound.ts";
import { candidateId, freesoundIdOf, knownFrom, runHarvest, sourceKey } from "./harvest.ts";
import { runQualify } from "./qualify.ts";
import { accessToken, libraryRoute, type RouteDeps } from "./admin.ts";
import type { QualifyResult } from "./manifest.ts";
import { fixture, world } from "./fakes.test.ts";

// ---- license rule ----

test("licenses: CC0, BY and BY-SA in; NC, ND, Sampling+ and unknown out, with a reason", () => {
  const ok = (raw: string) => classifyLicense(raw);
  assert.deepEqual(ok("http://creativecommons.org/publicdomain/zero/1.0/"), { ok: true, spdx: "CC0-1.0", url: "https://creativecommons.org/publicdomain/zero/1.0/", label: "CC0 1.0" });
  assert.equal((ok("https://creativecommons.org/licenses/by/4.0/") as { spdx: string }).spdx, "CC-BY-4.0");
  assert.equal((ok("http://creativecommons.org/licenses/by/3.0/") as { spdx: string }).spdx, "CC-BY-3.0");
  assert.equal((ok("https://creativecommons.org/licenses/by-sa/4.0/") as { spdx: string }).spdx, "CC-BY-SA-4.0");
  for (const [raw, word] of [
    ["http://creativecommons.org/licenses/by-nc/3.0/", "NonCommercial"],
    ["https://creativecommons.org/licenses/by-nc-sa/4.0/", "NonCommercial"],
    ["https://creativecommons.org/licenses/by-nd/4.0/", "NoDerivatives"],
    ["http://creativecommons.org/licenses/sampling+/1.0/", "Sampling+"],
    ["Attribution NonCommercial", "NonCommercial"],
    ["Attribution", "version unknown"],
    ["", "no license"],
    ["All rights reserved", "not an accepted license"],
  ] as const) {
    const v = classifyLicense(raw);
    assert.equal(v.ok, false, raw);
    assert.match((v as { reason: string }).reason, new RegExp(word.replace("+", "\\+")), raw);
  }
});

// ---- config ----

test("config: clamps to Freesound's limits and keeps defaults", () => {
  const cfg = normalizeConfig({ daily_api_cap: 99999, daily_download_cap: 9999, max_candidates: -3, sort: "nope", queries: ["909 cowbell", { q: "kick 707", role: "kick" }, { q: "" }] });
  assert.equal(cfg.daily_api_cap, 1800);
  assert.equal(cfg.daily_download_cap, 400);
  assert.equal(cfg.max_candidates, 0);
  assert.equal(cfg.sort, "score");
  assert.deepEqual(cfg.queries, [{ q: "909 cowbell", role: "perc" }, { q: "kick 707", role: "kick" }]);
  assert.deepEqual(normalizeConfig(undefined), DEFAULT_CONFIG);
  assert.ok(DEFAULT_CONFIG.queries.slice(0, 10).every((q) => /808|909|electronic/.test(q.q)), "electronic drums first");
  assert.deepEqual(rotateQueries([{ q: "a", role: "kick" }, { q: "b", role: "kick" }, { q: "c", role: "kick" }], 4).map((q) => q.q), ["b", "c", "a"]);
});

// ---- Freesound client ----

test("search: new /apiv2/search/ endpoint, token in a header, never in the URL, license and duration filter", async () => {
  const w = world();
  const res = await w.client().search("808 kick", 1, DEFAULT_CONFIG);
  assert.ok(!("status" in res));
  const call = w.fs.calls[0];
  const url = new URL(call.url);
  assert.equal(url.pathname, "/apiv2/search/");
  assert.equal(url.searchParams.get("token"), null);
  assert.equal(call.headers.authorization, "Token key-123");
  assert.equal(url.searchParams.get("filter"), 'duration:[0.05 TO 3] license:("Creative Commons 0" OR "Attribution")');
  assert.match(url.searchParams.get("fields") || "", /license/);
  assert.equal(res.results.length, 8);
  assert.equal(res.next, true);
  assert.equal(new URL(searchUrl("x", 2, DEFAULT_CONFIG, false)).searchParams.get("filter"), "duration:[0.05 TO 3]");
});

test("pacing: API calls 1.1 s apart, downloads 2.1 s apart (60 and 30 a minute)", async () => {
  const w = world();
  const c = w.client();
  await c.search("808 kick", 1, DEFAULT_CONFIG);
  await c.search("808 kick", 2, DEFAULT_CONFIG);
  await c.search("808 kick", 3, DEFAULT_CONFIG);
  assert.deepEqual(w.clock.slept, [1100, 1100]);
  const s = { id: 1, name: "x", tags: [], username: "u", license: "", url: "", duration: 1 };
  await c.original(s, "tok");
  await c.original(s, "tok");
  assert.equal(w.clock.slept.at(-1), 2100);
  assert.equal(w.fs.calls.at(-1)?.headers.authorization, "Bearer tok");
});

test("budget: a spent daily cap stops requests before they are sent; 429 stops the run", async () => {
  const w = world();
  w.budget.caps.api = 1;
  const c = w.client();
  await c.search("808 kick", 1, DEFAULT_CONFIG);
  await assert.rejects(() => c.search("808 kick", 2, DEFAULT_CONFIG), BudgetExhausted);
  assert.equal(w.fs.calls.length, 1);
  const w2 = world();
  w2.fs.fail.push({ match: "/search/", status: 429, body: { detail: "Request was throttled (60/minute)" } });
  await assert.rejects(() => w2.client().search("808 kick", 1, DEFAULT_CONFIG), (err: unknown) => err instanceof RateLimited && /60\/minute/.test(err.message));
});

test("oauth: authorize URL, code exchange and refresh post the documented form", async () => {
  const w = world();
  const u = new URL(authorizeUrl("cid", "st8"));
  assert.equal(u.origin + u.pathname, "https://freesound.org/apiv2/oauth2/authorize/");
  assert.deepEqual(Object.fromEntries(u.searchParams), { client_id: "cid", response_type: "code", state: "st8" });
  const t = await exchangeCode(w.fs.fetch, "cid", "sec", "code-9", 1000);
  assert.equal(t.access_token, "acc-1");
  assert.equal(t.expires_at, 1000 + 86399 * 1000);
  assert.deepEqual(Object.fromEntries(new URLSearchParams(w.fs.calls[0].body)), { client_id: "cid", client_secret: "sec", grant_type: "authorization_code", code: "code-9" });
  await refreshTokens(w.fs.fetch, "cid", "sec", "ref-1", 0);
  assert.equal(new URLSearchParams(w.fs.calls[1].body).get("grant_type"), "refresh_token");
  assert.equal(new URLSearchParams(w.fs.calls[1].body).get("refresh_token"), "ref-1");
});

// ---- harvest ----

function harvestDeps(w: ReturnType<typeof world>, token: string | null = null) {
  return {
    store: w.store,
    client: w.client(),
    blobs: w.blobs,
    accessToken: async () => token,
    known: async () => w.known,
    now: w.clock.date,
    log: w.log,
  };
}

async function onlyQueries(w: ReturnType<typeof world>, queries: { q: string; role: string }[], extra: Record<string, unknown> = {}) {
  await w.store.setConfig({ queries, ...extra });
}

test("harvest: license, length, loop and seen gates, with reasons; previews without OAuth", async () => {
  const w = world();
  await onlyQueries(w, [{ q: "808 kick", role: "kick" }, { q: "909 closed hihat", role: "hat" }], { per_query: 10, per_pack: 2 });
  const batch = await runHarvest(harvestDeps(w), "n-2026-11-03-aaa");
  assert.equal(batch.state, "harvested");
  assert.equal(batch.quality, "preview");
  // 100001 + 100007 from one pack (per_pack 2), 100002 BY 4.0, 100009 BY 3.0 on page 2; hats: 200001, 200002.
  assert.deepEqual(batch.picked.sort(), [
    "fs-100001-tr808-kick-long", "fs-100002-808-kick-short", "fs-100007-808-kick-2", "fs-100009-808-kick-deep",
    "fs-200001-909-closed-hihat", "fs-200002-909-ch-2",
  ]);
  const reasons = Object.fromEntries(batch.skipped.map((s) => [s.source, s.reason]));
  assert.match(reasons["freesound:100003"], /^license: NonCommercial/);
  assert.match(reasons["freesound:100004"], /^license: Sampling\+/);
  assert.match(reasons["freesound:100005"], /loop/);
  assert.match(reasons["freesound:100006"], /longer than 3 s/);
  assert.equal(reasons["freesound:100008"], undefined, "pack limit is not a rejection: another night");
  assert.equal(reasons["freesound:200003"], undefined, "already rejected on musicmatter.art: skipped silently");
  assert.equal(batch.queries.find((q) => q.q === "909 closed hihat")?.seen, 1);
  // Raw previews in R2, candidates recorded with credit and license.
  assert.equal(w.blobs.text("candidates/raw/fs-100002-808-kick-short.mp3"), "ID3-preview-/previews/100/100002_1234-hq.mp3");
  const previewCall = w.fs.calls.find((c) => c.url.startsWith("https://cdn.freesound.org/"));
  assert.equal(previewCall?.headers.authorization, undefined, "the API key never goes to the CDN");
  const c = await w.store.getCandidate("fs-100002-808-kick-short");
  assert.equal(c?.quality, "preview");
  assert.equal(c?.sound.license_spdx, "CC-BY-4.0");
  assert.equal(c?.sound.username, "beatsmith");
  assert.ok(w.blobs.text("candidates/batches/n-2026-11-03-aaa.json").includes("NonCommercial"));
  // License rejects are remembered, so later nights do not spend requests on them.
  const seen = await w.store.seenMany([sourceKey(100003), sourceKey(100008)]);
  assert.equal(seen[sourceKey(100003)].state, "skipped");
  assert.equal(seen[sourceKey(100008)], undefined);
  assert.equal(w.fs.calls.filter((c) => c.url.includes("/download/")).length, 0);
});

test("harvest: a second night never re-harvests what it has seen", async () => {
  const w = world();
  await onlyQueries(w, [{ q: "808 kick", role: "kick" }], { per_query: 10, per_pack: 2 });
  await runHarvest(harvestDeps(w), "n-1");
  const again = await runHarvest(harvestDeps(w), "n-2");
  assert.deepEqual(again.picked, ["fs-100008-808-kick-3"], "only the sound the pack limit held back");
  const third = await runHarvest(harvestDeps(w), "n-3");
  assert.deepEqual(third.picked, []);
});

test("harvest: originals with an OAuth token; a refused download falls back to previews", async () => {
  const w = world();
  await onlyQueries(w, [{ q: "909 closed hihat", role: "hat" }], { per_query: 10 });
  const b = await runHarvest(harvestDeps(w, "acc-1"), "n-1");
  assert.equal(b.quality, "original");
  assert.equal(w.blobs.text("candidates/raw/fs-200001-909-closed-hihat.wav"), "RIFF-original-200001");
  assert.equal((await w.store.getCandidate("fs-200001-909-closed-hihat"))?.quality, "original");
  const w2 = world();
  await onlyQueries(w2, [{ q: "909 closed hihat", role: "hat" }], { per_query: 10 });
  w2.fs.fail.push({ match: "/download/", status: 401 });
  const b2 = await runHarvest(harvestDeps(w2, "acc-1"), "n-1");
  assert.equal(b2.quality, "mixed");
  assert.equal((await w2.store.getCandidate("fs-200002-909-ch-2"))?.quality, "preview");
  assert.equal(w2.fs.calls.filter((c) => c.url.includes("/download/")).length, 1, "stops trying originals after a 401");
});

test("harvest: stops cleanly when the budget or the rate limit says so", async () => {
  const w = world();
  w.budget.caps.api = 1;
  await onlyQueries(w, [{ q: "808 kick", role: "kick" }, { q: "909 closed hihat", role: "hat" }], { per_query: 10, per_pack: 9 });
  const b = await runHarvest(harvestDeps(w), "n-1");
  assert.equal(b.state, "harvested");
  assert.match(b.stop_reason || "", /budget/);
  assert.ok(b.picked.length > 0);
  const w2 = world();
  w2.fs.fail.push({ match: "/search/", status: 429 });
  const b2 = await runHarvest(harvestDeps(w2), "n-1");
  assert.match(b2.stop_reason || "", /rate limit/);
});

test("harvest: a 400 on the license filter retries without it; every license is still checked", async () => {
  const w = world();
  w.fs.rejectLicenseFilter = true;
  await onlyQueries(w, [{ q: "808 kick", role: "kick" }], { per_query: 10, max_pages: 1, per_pack: 9 });
  const b = await runHarvest(harvestDeps(w), "n-1");
  assert.ok(b.picked.includes("fs-100002-808-kick-short"));
  assert.ok(b.skipped.some((s) => /NonCommercial/.test(s.reason)));
});

test("harvest: never harvests blind (rejected.json unreadable), off switch, full review queue", async () => {
  const w = world();
  const b = await runHarvest({ ...harvestDeps(w), known: async () => { throw new Error("HTTP 503"); } }, "n-1");
  assert.equal(b.state, "failed");
  assert.match(b.stop_reason || "", /rejected\.json/);
  assert.equal(w.fs.calls.length, 0);
  const w2 = world();
  await w2.store.setConfig({ enabled: false });
  assert.equal((await runHarvest(harvestDeps(w2), "n-1")).state, "disabled");
  const w3 = world();
  await w3.store.setConfig({ max_pending: 1, queries: [{ q: "808 kick", role: "kick" }] });
  const first = await runHarvest(harvestDeps(w3), "n-1");
  assert.equal(first.picked.length, 1, "room for one");
  assert.equal((await runHarvest(harvestDeps(w3), "n-2")).state, "skipped");
});

test("known: Freesound ids from musicmatter.art rejected.json and library.json, by origin, URL or candidate id", () => {
  const k = knownFrom(fixture("musicmatter-library.json"), fixture("musicmatter-rejected.json"));
  assert.ok(k.freesound.has("200003"));
  assert.ok(k.sha.has("60da01ab26a46833e073cfb879baa0a1c58b2b44a36342f5c5797b42a9cb7a2b"));
  assert.ok(k.ids.has("vcsl-hand-claps-soloclap-vl1"));
  assert.equal(freesoundIdOf({ url: "https://freesound.org/s/4242/" }), "4242");
  assert.equal(freesoundIdOf({}, "fs-77-kick"), "77");
  assert.equal(candidateId({ id: 5, name: "My Kick!!.WAV" }), "fs-5-my-kick");
});

// ---- qualify ----

function okResult(id: string, role = "kick", sha = `sha-${id}`): QualifyResult {
  return {
    ok: true, sha256: sha, source_sha256: `src-${id}`, size_bytes: 3, role, role_basis: "name", tags: ["electronic", role, "808"],
    title: id, measures: { length_s: 0.5, lufs: -12, peak_dbfs: -1, attack_ms: 2, decay_s: 0.3, centroid_hz: 90, low_share: 0.9, hit_dbfs: -10 },
    measures_version: "1", playback: { gain_db: -4, method: "family level match" }, format: { mime: "audio/flac", lossless: false },
    processing: ["resample"], wave: "z".repeat(48), flac: new Uint8Array([1, 2, 3]), preview: new Uint8Array([4, 5]),
  };
}

test("qualify: pending candidates get audio, preview and an NC manifest crediting the creator", async () => {
  const w = world();
  await onlyQueries(w, [{ q: "808 kick", role: "kick" }], { per_query: 2, max_pages: 1 });
  await runHarvest(harvestDeps(w), "n-1");
  const sent: string[] = [];
  const b = await runQualify({
    store: w.store, blobs: w.blobs, known: async () => w.known, now: w.clock.date, log: w.log,
    qualify: async (raw, meta) => {
      sent.push(`${meta.id}:${meta.quality}:${meta.ext}:${new TextDecoder().decode(raw).slice(0, 3)}`);
      return okResult(meta.id);
    },
  }, "n-1");
  assert.equal(b?.qualified, 2);
  assert.deepEqual(sent.sort(), ["fs-100001-tr808-kick-long:preview:mp3:ID3", "fs-100002-808-kick-short:preview:mp3:ID3"]);
  const man = JSON.parse(w.blobs.text("candidates/fs-100002-808-kick-short/manifest.nc.json"));
  assert.equal(man.kind, "one-shot");
  assert.deepEqual(man.creator, { name: "beatsmith", link: "https://freesound.org/people/beatsmith/" });
  assert.deepEqual(man.curator, { name: "musicmatter.art", link: "https://musicmatter.art/" });
  assert.equal(man.license, "CC-BY-4.0");
  assert.equal(man.license_url, "https://creativecommons.org/licenses/by/4.0/");
  assert.equal(man.source.origin, "freesound");
  assert.equal(man.source.id, "100002");
  assert.equal(man.source.url, "https://freesound.org/people/beatsmith/sounds/100002/");
  assert.equal(man.quality, "preview");
  assert.equal(man.audio, "https://musicmatter.art/library/audio/kick/fs-100002-808-kick-short.flac");
  assert.equal(man.curation.status, "pending");
  assert.match(man.attribution, /by beatsmith .* CC BY 4\.0/);
  assert.equal(w.blobs.data.get("candidates/fs-100002-808-kick-short/audio.flac")?.type, "audio/flac");
  assert.equal(w.blobs.data.has("candidates/raw/fs-100002-808-kick-short.mp3"), false, "raw file removed");
  assert.equal((await w.store.listCandidates("pending")).length, 2);
});

test("qualify: failed gates and duplicate fingerprints are rejected with reasons and never come back", async () => {
  const w = world();
  await onlyQueries(w, [{ q: "808 kick", role: "kick" }], { per_query: 10, max_pages: 2, per_pack: 9 });
  await runHarvest(harvestDeps(w), "n-1");
  const b = await runQualify({
    store: w.store, blobs: w.blobs, known: async () => w.known, now: w.clock.date, log: w.log,
    qualify: async (_raw, meta) => {
      if (meta.id.startsWith("fs-100001")) return { ok: false, reason: "clipped (flat-topped at full scale)" };
      if (meta.id.startsWith("fs-100007")) return okResult(meta.id, "kick", "same-sha");
      if (meta.id.startsWith("fs-100008")) return okResult(meta.id, "kick", "same-sha");
      if (meta.id.startsWith("fs-100009")) return okResult(meta.id, "kick", "60da01ab26a46833e073cfb879baa0a1c58b2b44a36342f5c5797b42a9cb7a2b");
      return okResult(meta.id);
    },
  }, "n-1");
  const reasons = Object.fromEntries((b?.failed || []).map((f) => [f.id, f.reason]));
  assert.match(reasons["fs-100001-tr808-kick-long"], /clipped/);
  assert.match(reasons["fs-100008-808-kick-3"], /duplicate of fs-100007/);
  assert.match(reasons["fs-100009-808-kick-deep"], /already on musicmatter\.art/);
  assert.equal((await w.store.seenMany([sourceKey(100001)]))[sourceKey(100001)].state, "failed");
  const again = await runHarvest(harvestDeps(w), "n-2");
  assert.deepEqual(again.picked, []);
});

test("qualify: container errors leave the raw file for the next run", async () => {
  const w = world();
  await onlyQueries(w, [{ q: "909 closed hihat", role: "hat" }], { per_query: 10 });
  await runHarvest(harvestDeps(w), "n-1");
  await runQualify({ store: w.store, blobs: w.blobs, known: async () => w.known, now: w.clock.date, log: w.log, qualify: async () => { throw new Error("container HTTP 503"); } }, "n-1");
  assert.equal((await w.store.listCandidates("raw")).length, 2);
  const b = await runQualify({ store: w.store, blobs: w.blobs, known: async () => w.known, now: w.clock.date, log: w.log, qualify: async (_r, m) => okResult(m.id, "hat") }, "n-1");
  assert.equal(b?.qualified, 2);
});

// ---- admin routes and OAuth ----

function routeDeps(w: ReturnType<typeof world>, extra: Partial<RouteDeps> = {}): RouteDeps & { queued: unknown[] } {
  const queued: unknown[] = [];
  let n = 0;
  return {
    store: w.store,
    blobs: w.blobs,
    enqueue: async (m) => { queued.push(m); },
    fetch: w.fs.fetch,
    now: w.clock.date,
    adminToken: "admin-secret",
    apiKey: "key-123",
    clientId: "cid",
    clientSecret: "csecret",
    budgetToday: async () => ({ "freesound-api": 3 }),
    randomHex: (b) => String(++n).padStart(b * 2, "0"),
    log: w.log,
    queued,
    ...extra,
  };
}

const BASE = "https://nc-perform-api.psemme.workers.dev";
function req(path: string, init: RequestInit & { token?: string } = {}): Request {
  const headers = new Headers(init.headers);
  if (init.token) headers.set("Authorization", `Bearer ${init.token}`);
  return new Request(BASE + path, { ...init, headers });
}

test("admin: every admin route needs NC_ADMIN_TOKEN", async () => {
  const w = world();
  const d = routeDeps(w);
  for (const [path, method] of [["/api/library/admin/status", "GET"], ["/api/library/admin/candidates", "GET"], ["/api/library/admin/decisions", "POST"], ["/api/library/admin/harvest", "POST"]]) {
    assert.equal((await libraryRoute(req(path, { method }), d, BASE))?.status, 401, path);
    assert.equal((await libraryRoute(req(path, { method, token: "wrong" }), d, BASE))?.status, 401, path);
  }
  assert.equal((await libraryRoute(req("/api/library/admin/status"), routeDeps(w, { adminToken: "" }), BASE))?.status, 404);
  assert.equal(await libraryRoute(req("/api/jobs/x"), d, BASE), null);
  const st = await (await libraryRoute(req("/api/library/admin/status", { token: "admin-secret" }), d, BASE))!.json() as Record<string, any>;
  assert.deepEqual(st.secrets, { FREESOUND_API_KEY: true, FREESOUND_CLIENT_ID: true, FREESOUND_CLIENT_SECRET: true });
  assert.equal(st.quality, "preview");
  assert.equal(st.used_today["freesound-api"], 3);
});

test("oauth: connect form, admin-token gate, one-time state, callback stores tokens and the account name", async () => {
  const w = world();
  const d = routeDeps(w);
  const form = await libraryRoute(req("/api/library/freesound/connect"), d, BASE);
  assert.equal(form?.status, 200);
  assert.match(await form!.text(), /name="token"/);
  const bad = await libraryRoute(req("/api/library/freesound/connect", { method: "POST", body: new URLSearchParams({ token: "nope" }) }), d, BASE);
  assert.equal(bad?.status, 401);
  const go = await libraryRoute(req("/api/library/freesound/connect", { method: "POST", body: new URLSearchParams({ token: "admin-secret" }) }), d, BASE);
  assert.equal(go?.status, 303);
  const target = new URL(go!.headers.get("Location")!);
  assert.equal(target.origin + target.pathname, "https://freesound.org/apiv2/oauth2/authorize/");
  const state = target.searchParams.get("state")!;
  assert.equal(target.searchParams.get("client_id"), "cid");
  const done = await libraryRoute(req(`/api/library/freesound/callback?code=abc&state=${state}`), d, BASE);
  assert.equal(done?.status, 200);
  assert.match(await done!.text(), /nc-harvester/);
  const rec = await w.store.getOAuth();
  assert.equal(rec?.access_token, "acc-1");
  assert.equal(rec?.username, "nc-harvester");
  assert.equal(new URLSearchParams(w.fs.calls.find((c) => c.url.endsWith("/access_token/"))!.body).get("code"), "abc");
  const replay = await libraryRoute(req(`/api/library/freesound/callback?code=abc&state=${state}`), d, BASE);
  assert.equal(replay?.status, 400, "a state works once");
  const denied = await libraryRoute(req(`/api/library/freesound/callback?error=access_denied&state=zzz`), d, BASE);
  assert.equal(denied?.status, 400);
  const noApp = await libraryRoute(req("/api/library/freesound/connect", { method: "POST", token: "admin-secret" }), routeDeps(w, { clientId: undefined }), BASE);
  assert.equal(noApp?.status, 503);
});

test("oauth: state expires after 10 minutes", async () => {
  const w = world();
  const d = routeDeps(w);
  const go = await libraryRoute(req("/api/library/freesound/connect", { method: "POST", token: "admin-secret" }), d, BASE);
  const { authorize_url } = await go!.json() as { authorize_url: string };
  const state = new URL(authorize_url).searchParams.get("state");
  w.clock.t += 11 * 60 * 1000;
  assert.equal((await libraryRoute(req(`/api/library/freesound/callback?code=abc&state=${state}`), d, BASE))?.status, 400);
});

test("oauth: access token refreshes before expiry; a refused refresh falls back to previews and says why", async () => {
  const w = world();
  const d = routeDeps(w);
  await w.store.setOAuth({ access_token: "old", refresh_token: "r0", expires_at: w.clock.t + 60 * 60 * 1000, connected_at: "x" });
  assert.equal(await accessToken(d), "old");
  w.clock.t += 55 * 60 * 1000;
  assert.equal(await accessToken(d), "acc-1");
  assert.equal((await w.store.getOAuth())?.refresh_token, "ref-1");
  w.clock.t += 25 * 3600 * 1000;
  w.fs.fail.push({ match: "/access_token/", status: 400, body: { error: "invalid_grant" } });
  assert.equal(await accessToken(d), null);
  assert.match((await w.store.getOAuth())?.broken || "", /invalid_grant/);
  assert.equal(await accessToken(d), null);
  assert.equal(await accessToken({ ...d, store: world().store }), null, "no account connected");
});

test("review loop: candidates list and files, then Peter's decisions; rejected sounds are never harvested again", async () => {
  const w = world();
  await onlyQueries(w, [{ q: "909 closed hihat", role: "hat" }], { per_query: 10 });
  await runHarvest(harvestDeps(w), "n-1");
  await runQualify({ store: w.store, blobs: w.blobs, known: async () => w.known, now: w.clock.date, log: w.log, qualify: async (_r, m) => okResult(m.id, "hat") }, "n-1");
  const d = routeDeps(w);
  const list = await (await libraryRoute(req("/api/library/admin/candidates", { token: "admin-secret" }), d, BASE))!.json() as { count: number; candidates: Array<{ id: string; files: Record<string, string>; manifest: Record<string, unknown> }> };
  assert.equal(list.count, 2);
  const one = list.candidates[0];
  assert.equal(one.files.preview, `${BASE}/api/library/admin/candidates/${one.id}/preview.mp3`);
  assert.equal(one.manifest.role, "hat");
  const audio = await libraryRoute(req(`/api/library/admin/candidates/${one.id}/audio.flac`, { token: "admin-secret" }), d, BASE);
  assert.equal(audio?.headers.get("content-type"), "audio/flac");
  assert.deepEqual([...new Uint8Array(await audio!.arrayBuffer())], [1, 2, 3]);
  const preview = await libraryRoute(req(`/api/library/admin/candidates/${one.id}/preview.mp3`, { token: "admin-secret" }), d, BASE);
  assert.equal(preview?.status, 200);
  assert.equal(preview?.headers.get("content-type"), "audio/mpeg");
  const manifestFile = await libraryRoute(req(`/api/library/admin/candidates/${one.id}/manifest.nc.json`, { token: "admin-secret" }), d, BASE);
  assert.equal((await manifestFile!.json() as { role: string }).role, "hat");
  assert.equal((await libraryRoute(req(`/api/library/admin/candidates/${one.id}/secrets.txt`, { token: "admin-secret" }), d, BASE))?.status, 404);
  const res = await libraryRoute(req("/api/library/admin/decisions", {
    method: "POST", token: "admin-secret",
    body: JSON.stringify({ decisions: { "fs-200001-909-closed-hihat": { status: "approved" }, "fs-200002-909-ch-2": { status: "rejected", note: "too harsh" }, "nope": { status: "rejected" } } }),
  }), d, BASE);
  assert.deepEqual(await res!.json(), { approved: ["fs-200001-909-closed-hihat"], rejected: ["fs-200002-909-ch-2"], pending: [], unknown: ["nope"], not_reviewable: [] });
  assert.equal(w.blobs.data.has("candidates/fs-200002-909-ch-2/audio.flac"), false, "rejected audio deleted");
  assert.equal((await w.store.listCandidates("pending")).length, 0);
  const seen = await w.store.seenMany([sourceKey(200002)]);
  assert.equal(seen[sourceKey(200002)].state, "rejected");
  assert.equal(seen[sourceKey(200002)].reason, "too harsh");
  // A later harvest or qualify cannot downgrade a decision.
  await w.store.markSeen(sourceKey(200002), { state: "harvested", at: "x" });
  assert.equal((await w.store.seenMany([sourceKey(200002)]))[sourceKey(200002)].state, "rejected");
  w.store.setConfig({ max_pending: 50 });
  assert.deepEqual((await runHarvest(harvestDeps(w), "n-2")).picked, []);
});

test("admin: harvest now and config", async () => {
  const w = world();
  const d = routeDeps(w);
  const res = await libraryRoute(req("/api/library/admin/harvest", { method: "POST", token: "admin-secret" }), d, BASE);
  assert.equal(res?.status, 202);
  assert.deepEqual(d.queued, [{ id: "m-2026-11-03-000001", kind: "library-harvest", batch: "m-2026-11-03-000001" }]);
  const put = await libraryRoute(req("/api/library/admin/config", { method: "PUT", token: "admin-secret", body: JSON.stringify({ per_query: 3, daily_api_cap: 5000 }) }), d, BASE);
  const cfg = await put!.json() as Record<string, number>;
  assert.equal(cfg.per_query, 3);
  assert.equal(cfg.daily_api_cap, 1800);
  assert.equal((await w.store.getConfig()).per_query, 3);
});
