import { test } from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import { canon, seedFromSigningKey, signerFrom, signHash, verifyHash, signatureFor } from "./sign.ts";
import { needsLlm, newId, rejectPlan } from "./policy.ts";

test("crockford ids and llm choice", () => {
  const id = newId("h");
  assert.match(id, /^h-[0-9abcdefghjkmnpqrstvwxyz]{12}$/);
  assert.match(newId("g"), /^g-[0-9abcdefghjkmnpqrstvwxyz]{12}$/);
  assert.equal(needsLlm("1 min. Cm, Am, F. Clear and open."), false);
  assert.equal(needsLlm("A melody line in triplets across two sections."), true);
});

test("url sources are rejected before a render", () => {
  const err = rejectPlan({
    length_s: 10,
    tail_s: 1,
    sources: { x: { kind: "url" } },
    chords: [{}],
    layers: [{}],
  });
  assert.match(err || "", /kind=url/);
});

test("ed25519 sign and verify, including a PEM from node", async () => {
  const { privateKey } = generateKeyPairSync("ed25519");
  const pem = privateKey.export({ type: "pkcs8", format: "pem" }).toString();
  const seed = seedFromSigningKey(pem);
  assert.equal(seed.length, 32);
  const signer = await signerFrom(pem);
  const hash = "ab".repeat(32);
  const sig = signHash(signer, hash);
  assert.equal(verifyHash(signer, hash, sig), true);
  assert.equal(verifyHash(signer, "cd".repeat(32), sig), false);
  const block = signatureFor(signer, hash, "hosted Performer");
  assert.equal(block.status, "SIGNED");
  assert.equal(block.algorithm, "Ed25519");
  assert.equal(block.key_id, signer.keyId);
});

test("canonical json sorts keys", () => {
  assert.equal(canon({ b: 1, a: { d: 2, c: 3 } }), '{"a":{"c":3,"d":2},"b":1}');
});
