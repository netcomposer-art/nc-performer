/** Ed25519 signatures over record_hash. The signed bytes are the hex digest as UTF-8.
 * Set etc.sha512Sync once. NC_SIGNING_KEY is a PKCS#8 PEM or "base64:" plus 32 raw bytes.
 */
import { etc, getPublicKey, sign, verify } from "@noble/ed25519";
import { sha512 } from "@noble/hashes/sha2";

etc.sha512Sync = (...m: Uint8Array[]) => sha512(etc.concatBytes(...m));

export type Signature = {
  status: "SIGNED" | "UNSIGNED";
  signed_by: string | null;
  algorithm: "Ed25519" | null;
  key_id: string | null;
  value: string | null;
  note: string;
};

export type Signer = { seed: Uint8Array; publicKey: Uint8Array; keyId: string };

export function seedFromSigningKey(text: string): Uint8Array {
  const t = text.trim();
  if (!t) throw new Error("empty signing key");
  if (t.startsWith("base64:")) {
    const raw = Uint8Array.from(atob(t.slice("base64:".length)), (c) => c.charCodeAt(0));
    if (raw.length !== 32) throw new Error("Ed25519 seed must be 32 bytes");
    return raw;
  }
  const body = t.replace(/-----[^-]+-----/g, "").replace(/\s+/g, "");
  const der = Uint8Array.from(atob(body), (c) => c.charCodeAt(0));
  for (let i = 0; i + 34 <= der.length; i++) {
    if (der[i] === 0x04 && der[i + 1] === 0x20) return der.slice(i + 2, i + 34);
  }
  if (der.length === 32) return der;
  throw new Error("Could not read an Ed25519 seed from NC_SIGNING_KEY");
}

export async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const copy = new Uint8Array(bytes);
  const digest = await crypto.subtle.digest("SHA-256", copy);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function signerFrom(text: string): Promise<Signer> {
  const seed = seedFromSigningKey(text);
  const publicKey = getPublicKey(seed);
  const keyId = (await sha256Hex(publicKey)).slice(0, 16);
  return { seed, publicKey, keyId };
}

export function signHash(signer: Signer, recordHash: string): string {
  const sig = sign(new TextEncoder().encode(recordHash), signer.seed);
  let s = "";
  for (const b of sig) s += String.fromCharCode(b);
  return btoa(s);
}

export function signatureFor(signer: Signer, recordHash: string, signedBy: string): Signature {
  return {
    status: "SIGNED",
    signed_by: signedBy,
    algorithm: "Ed25519",
    key_id: signer.keyId,
    value: signHash(signer, recordHash),
    note: "Signature covers record_hash (hex, UTF-8). It says this instance rendered this exact record.",
  };
}

/** Canonical JSON: sorted keys, no whitespace. Same rule as the kit's record_hash. */
export function canon(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map((v) => canon(v)).join(",") + "]";
  const obj = value as Record<string, unknown>;
  const keys = Object.keys(obj).sort();
  return "{" + keys.map((k) => JSON.stringify(k) + ":" + canon(obj[k])).join(",") + "}";
}

export async function recordHashOf(record: Record<string, unknown>): Promise<string> {
  const body: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(record)) {
    if (k !== "record_hash" && k !== "signature") body[k] = v;
  }
  return sha256Hex(new TextEncoder().encode(canon(body)));
}

export function verifyHash(signer: Signer, recordHash: string, valueB64: string): boolean {
  const bin = atob(valueB64);
  const sig = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) sig[i] = bin.charCodeAt(i);
  return verify(sig, new TextEncoder().encode(recordHash), signer.publicKey);
}

export function publicJwk(signer: Signer): Record<string, string> {
  let s = "";
  for (const b of signer.publicKey) s += String.fromCharCode(b);
  const x = btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return { kty: "OKP", crv: "Ed25519", x, kid: signer.keyId, use: "sig", alg: "Ed25519" };
}
