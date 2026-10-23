import { test } from "node:test";
import assert from "node:assert/strict";
import { EXPIRY_PREFIXES, expiryMayDelete, noteObject, publishedNoteResponse } from "./notes.ts";

test("opus and the manifest name the R2 keys the page fetches", () => {
  assert.deepEqual(noteObject("/api/notes/guitar/64.opus"), {
    key: "notes/guitar/64.opus",
    cache: "public, max-age=31536000, immutable",
    contentType: "audio/ogg",
  });
  assert.deepEqual(noteObject("/api/notes/cello-pizzicato/manifest.json"), {
    key: "notes/cello-pizzicato/manifest.json",
    cache: "public, max-age=300",
    contentType: "application/json",
  });
  const low = noteObject("/api/notes/piano/0.opus");
  assert.equal(low !== "hidden" && low !== null && low.key, "notes/piano/0.opus");
});

test("masters and unknown note paths are hidden and not fetched", async () => {
  for (const path of [
    "/api/notes/guitar/64.flac",
    "/api/notes/guitar/064.opus",
    "/api/notes/guitar/128.opus",
    "/api/notes/guitar/chords/Cm.opus",
    "/api/notes/Guitar/64.opus",
    "/api/notes/../performances/h-0123456789ab/record.json",
    "/api/notes",
  ]) {
    assert.equal(noteObject(path), "hidden", path);
    let called = false;
    const res = await publishedNoteResponse("https://netcomposer.art", path, async () => {
      called = true;
      return { body: "nope" };
    });
    assert.equal(called, false, path);
    assert.equal(res?.status, 404, path);
    assert.equal(res?.headers.get("cache-control"), "no-store");
    assert.equal(res?.headers.get("access-control-allow-origin"), "https://netcomposer.art");
  }
  assert.equal(noteObject("/api/jobs/g-0123456789ab/audio/c0"), null);
  assert.equal(await publishedNoteResponse(null, "/health", async () => ({ body: "" })), null);
});

test("a stored note is served with the long cache and site CORS", async () => {
  const seen: string[] = [];
  const res = await publishedNoteResponse("https://play.netcomposer.art", "/api/notes/guitar/64.opus", async (key) => {
    seen.push(key);
    return { body: "OPUS" };
  });
  assert.deepEqual(seen, ["notes/guitar/64.opus"]);
  assert.equal(res?.status, 200);
  assert.equal(await res?.text(), "OPUS");
  assert.equal(res?.headers.get("content-type"), "audio/ogg");
  assert.equal(res?.headers.get("cache-control"), "public, max-age=31536000, immutable");
  assert.equal(res?.headers.get("access-control-allow-origin"), "https://play.netcomposer.art");
  assert.equal(res?.headers.get("vary"), "Origin");
});

test("the manifest uses a five minute cache and a missing object is 404", async () => {
  const hit = await publishedNoteResponse("https://evil.example", "/api/notes/piano/manifest.json", async () => ({ body: "{}" }));
  assert.equal(hit?.headers.get("content-type"), "application/json");
  assert.equal(hit?.headers.get("cache-control"), "public, max-age=300");
  assert.equal(hit?.headers.get("cache-control")?.includes("immutable"), false);
  assert.equal(hit?.headers.get("access-control-allow-origin"), "*");
  const miss = await publishedNoteResponse(null, "/api/notes/piano/60.opus", async () => null);
  assert.equal(miss?.status, 404);
  assert.equal(miss?.headers.get("cache-control"), "no-store");
});

test("the expiry job does not touch notes/", () => {
  assert.deepEqual(EXPIRY_PREFIXES, ["performances/", "jobs/"]);
  assert.equal(EXPIRY_PREFIXES.some((prefix) => prefix.startsWith("notes/")), false);
  assert.equal(expiryMayDelete("notes/guitar/64.opus"), false);
  assert.equal(expiryMayDelete("notes/guitar/64.flac"), false);
  assert.equal(expiryMayDelete("notes/piano/manifest.json"), false);
  assert.equal(expiryMayDelete("performances/h-0123456789ab/performance.flac"), true);
  assert.equal(expiryMayDelete("jobs/h-0123456789ab/status.json"), true);
  assert.equal(expiryMayDelete("gathers/g-0123456789ab/audio/c0"), false);
});
