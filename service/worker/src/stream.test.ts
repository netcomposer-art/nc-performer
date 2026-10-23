import { test } from "node:test";
import assert from "node:assert/strict";
import { byteLength, storeObject, storeResponse, type Body, type Sized } from "./stream.ts";

// Node has no FixedLengthStream. This one counts bytes the same way and errors on a mismatch.
const lengths: number[] = [];
const sized: Sized = (length) => {
  lengths.push(length);
  let seen = 0;
  return new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, ctl) { seen += chunk.byteLength; ctl.enqueue(chunk); },
    flush() { if (seen !== length) throw new Error(`expected ${length} bytes, got ${seen}`); },
  });
};
async function drain(body: Body): Promise<number> {
  if (body instanceof ArrayBuffer) return body.byteLength;
  let n = 0;
  for await (const chunk of body as unknown as AsyncIterable<Uint8Array>) n += chunk.byteLength;
  return n;
}
/** A response that is only readable as a stream: arrayBuffer() fails the test. */
function streamOnly(size: number, headers: Record<string, string>): Response {
  const res = new Response(new Uint8Array(size), { headers });
  res.arrayBuffer = async () => { throw new Error("buffered the whole file"); };
  return res;
}

test("byteLength takes only a positive whole count", () => {
  assert.equal(byteLength("54489424"), 54489424);
  assert.equal(byteLength(7), 7);
  for (const bad of [null, undefined, "", "0", "-1", "1.5", "abc", 0]) assert.equal(byteLength(bad as string | number | null | undefined), null);
});

test("a 54 MB note file with a Content-Length is streamed into R2, never buffered", async () => {
  lengths.length = 0;
  const kinds: string[] = [];
  let stored = 0;
  const ok = await storeResponse(streamOnly(54_489_424, { "content-length": "54489424" }), async (body) => {
    kinds.push(body instanceof ArrayBuffer ? "buffer" : "stream");
    stored = await drain(body);
  }, sized);
  assert.equal(ok, true);
  assert.deepEqual(kinds, ["stream"]);
  assert.deepEqual(lengths, [54_489_424]);
  assert.equal(stored, 54_489_424);
});

test("eight big copies at once stay streams (the Gather that hung at 99%)", async () => {
  const sizes = [6_923_592, 54_489_424, 51_317_062, 51_317_062, 25_247_878, 54_489_424, 51_317_062, 2_307_868];
  const kinds = await Promise.all(sizes.map(async (n) => {
    let kind = "";
    await storeResponse(streamOnly(n, { "content-length": String(n) }), async (body) => { kind = body instanceof ArrayBuffer ? "buffer" : "stream"; await drain(body); }, sized);
    return kind;
  }));
  assert.deepEqual(kinds, sizes.map(() => "stream"));
});

test("no Content-Length falls back to the buffered copy; empty and errors store nothing", async () => {
  const puts: number[] = [];
  const put = async (body: Body) => { puts.push(await drain(body)); };
  const noLength = new Response(new ReadableStream<Uint8Array>({ start(c) { c.enqueue(new Uint8Array(12)); c.close(); } }));
  assert.equal(await storeResponse(noLength, put, sized), true);
  assert.equal(await storeResponse(new Response(new Uint8Array(0), { headers: { "content-length": "0" } }), put, sized), false);
  assert.equal(await storeResponse(new Response(new ReadableStream({ start(c) { c.close(); } })), put, sized), false);
  assert.equal(await storeResponse(new Response("not found", { status: 404 }), put, sized), false);
  assert.deepEqual(puts, [12]);
});

test("a short body is an error, not a truncated file in R2", async () => {
  await assert.rejects(storeResponse(new Response(new Uint8Array(10), { headers: { "content-length": "20" } }), drain, sized));
});

test("an R2-to-R2 carried copy streams the object at its size", async () => {
  lengths.length = 0;
  const kinds: string[] = [];
  const obj = { body: new Response(new Uint8Array(51_317_062)).body!, size: 51_317_062 };
  assert.equal(await storeObject(obj, async (body) => { kinds.push(body instanceof ArrayBuffer ? "buffer" : "stream"); assert.equal(await drain(body), 51_317_062); }, sized), true);
  assert.deepEqual(kinds, ["stream"]);
  assert.deepEqual(lengths, [51_317_062]);
  assert.equal(await storeObject(null, async () => { throw new Error("no put"); }, sized), false);
  assert.equal(await storeObject({ body: new Response(new Uint8Array(0)).body!, size: 0 }, async () => { throw new Error("no put"); }, sized), false);
});
