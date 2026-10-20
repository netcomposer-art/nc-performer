import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createRequire } from "node:module";
import { handleContact, contactPreflight, type ContactEnv } from "./contact.ts";

const require = createRequire(import.meta.url);
const esbuild = require("esbuild") as typeof import("esbuild");
const here = dirname(fileURLToPath(import.meta.url));
const workerRoot = join(here, "..");
const repoRoot = join(workerRoot, "..", "..");

const PASS = "1x0000000000000000000000000000000AA";
const FAIL = "2x0000000000000000000000000000000AA";
const DUMMY = "XXXX.DUMMY.TOKEN.XXXX";

type Call = { url: string; init: RequestInit | undefined };

function mailer(status = 200, responseBody = '{"id":"email_1"}') {
  const calls: Call[] = [];
  const doFetch = (async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    if (String(url).includes("siteverify")) {
      const params = new URLSearchParams(String(init?.body || ""));
      const ok = params.get("response") === "token-ok";
      return new Response(JSON.stringify({ success: ok }), { status: 200 });
    }
    return new Response(responseBody, { status });
  }) as typeof fetch;
  return { calls, doFetch };
}

function env(over: Partial<ContactEnv> = {}): { env: ContactEnv; keys: string[] } {
  const keys: string[] = [];
  return {
    keys,
    env: {
      TURNSTILE_SECRET_KEY: PASS,
      RESEND_API_KEY: "re_test_key",
      CONTACT_TO: "owner@example.com",
      CONTACT_FROM: "NetComposer <onboarding@resend.dev>",
      CONTACT_RATE: {
        async limit(options) {
          keys.push(options.key);
          return { success: true };
        },
      },
      ...over,
    },
  };
}

function post(body: unknown, headers: Record<string, string> = {}, raw?: string): Request {
  return new Request("https://nc-perform-api.example/api/contact", {
    method: "POST",
    headers: { "content-type": "application/json", origin: "https://netcomposer.art", ...headers },
    body: raw ?? JSON.stringify(body),
  });
}

const good = { name: "Ada", email: "ada@example.com", message: "Hello from the garden.", turnstile_token: DUMMY };

test("a valid note sends one plain-text email and returns ok", async () => {
  const box = mailer();
  const { env: cfg, keys } = env();
  const res = await handleContact(post(good), cfg, box.doFetch);
  assert.equal(res.status, 200);
  const data = await res.json();
  assert.deepEqual(data, { ok: true });
  assert.equal(res.headers.get("access-control-allow-origin"), "https://netcomposer.art");
  assert.equal(box.calls.length, 1);
  assert.equal(box.calls[0].url, "https://api.resend.com/emails");
  const headers = box.calls[0].init?.headers as Record<string, string>;
  assert.equal(headers.Authorization, "Bearer re_test_key");
  const payload = JSON.parse(String(box.calls[0].init?.body));
  assert.equal(payload.from, "NetComposer <onboarding@resend.dev>");
  assert.deepEqual(payload.to, ["owner@example.com"]);
  assert.equal(payload.subject, "NetComposer contact: Ada");
  assert.equal(payload.reply_to, "ada@example.com");
  assert.match(payload.text, /Name: Ada/);
  assert.match(payload.text, /Email: ada@example.com/);
  assert.match(payload.text, /Hello from the garden\./);
  assert.match(payload.text, /Time: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z/);
  assert.equal(payload.html, undefined);
  assert.equal(keys.length, 1);
  assert.notEqual(keys[0], "203.0.113.4");
  const shown = JSON.stringify(data);
  assert.equal(shown.includes("re_test_key"), false);
  assert.equal(shown.includes("owner@example.com"), false);
});

test("the subject uses the email when the name is omitted, and the default sender is used", async () => {
  const box = mailer();
  const { env: cfg } = env({ CONTACT_FROM: "  " });
  const res = await handleContact(post({ email: "ada@example.com", message: "Hi", turnstile_token: DUMMY }), cfg, box.doFetch);
  assert.equal(res.status, 200);
  const payload = JSON.parse(String(box.calls[0].init?.body));
  assert.equal(payload.subject, "NetComposer contact: ada@example.com");
  assert.equal(payload.from, "NetComposer <onboarding@resend.dev>");
  assert.match(payload.text, /Name: \(none\)/);
});

test("a filled honeypot returns ok and sends nothing", async () => {
  const box = mailer();
  const { env: cfg, keys } = env();
  const res = await handleContact(post({ ...good, website: "http://spam.test" }), cfg, box.doFetch);
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { ok: true });
  assert.equal(box.calls.length, 0);
  assert.equal(keys.length, 0);
  const again = await handleContact(post({ website: ["not-a-string"], email: "nope" }), cfg, box.doFetch);
  assert.equal(again.status, 200);
  assert.equal(box.calls.length, 0);
});

test("validation failures are 400 and do not call Resend", async () => {
  const cases: { body: unknown; match: RegExp }[] = [
    { body: {}, match: /email is required/ },
    { body: { email: "not-an-email", message: "Hi", turnstile_token: DUMMY }, match: /not valid/ },
    { body: { email: "a@b", message: "Hi", turnstile_token: DUMMY }, match: /not valid/ },
    { body: { email: `${"a".repeat(190)}@example.com`, message: "Hi", turnstile_token: DUMMY }, match: /200 characters/ },
    { body: { email: "ada@example.com", turnstile_token: DUMMY }, match: /message is required/ },
    { body: { email: "ada@example.com", message: "   ", turnstile_token: DUMMY }, match: /message is required/ },
    { body: { email: "ada@example.com", message: "x".repeat(5001), turnstile_token: DUMMY }, match: /5000/ },
    { body: { name: "n".repeat(101), email: "ada@example.com", message: "Hi", turnstile_token: DUMMY }, match: /100/ },
    { body: { name: 4, email: "ada@example.com", message: "Hi", turnstile_token: DUMMY }, match: /name must be text/ },
  ];
  for (const item of cases) {
    const box = mailer();
    const { env: cfg } = env();
    const res = await handleContact(post(item.body), cfg, box.doFetch);
    assert.equal(res.status, 400, JSON.stringify(item.body));
    const data = (await res.json()) as { error: string; code: string };
    assert.equal(data.code, "validation");
    assert.match(data.error, item.match);
    assert.equal(box.calls.length, 0);
    assert.equal(JSON.stringify(data).includes("re_test_key"), false);
  }
  const box = mailer();
  const { env: cfg } = env();
  const bad = await handleContact(post(null, {}, "{"), cfg, box.doFetch);
  assert.equal(bad.status, 400);
  const huge = await handleContact(post(null, {}, "x".repeat(20_001)), cfg, box.doFetch);
  assert.equal(huge.status, 400);
  assert.match(((await huge.json()) as { error: string }).error, /too long/);
});

test("a 100 character name and a 5000 character message are accepted", async () => {
  const box = mailer();
  const { env: cfg } = env();
  const res = await handleContact(
    post({ name: "n".repeat(100), email: "a@" + "b".repeat(195) + ".co", message: "m".repeat(5000), turnstile_token: DUMMY }),
    cfg,
    box.doFetch,
  );
  assert.equal(res.status, 200, JSON.stringify(await res.clone().json()));
  const payload = JSON.parse(String(box.calls[0].init?.body));
  assert.equal(payload.to[0], "owner@example.com");
  assert.equal(payload.text.includes("m".repeat(5000)), true);
});

test("Turnstile failure is 403 and sends nothing", async () => {
  const box = mailer();
  const { env: cfg } = env({ TURNSTILE_SECRET_KEY: FAIL });
  const res = await handleContact(post(good), cfg, box.doFetch);
  assert.equal(res.status, 403);
  const data = (await res.json()) as { code: string; error: string };
  assert.equal(data.code, "turnstile");
  assert.match(data.error, /bot check/);
  assert.equal(box.calls.length, 0);
  assert.equal(JSON.stringify(data).includes(FAIL), false);
});

test("a live Turnstile secret is posted to siteverify and is not echoed", async () => {
  const box = mailer();
  const { env: cfg } = env({ TURNSTILE_SECRET_KEY: "live-secret" });
  const denied = await handleContact(post({ ...good, turnstile_token: "nope" }), cfg, box.doFetch);
  assert.equal(denied.status, 403);
  assert.equal(String(box.calls[0].url).includes("siteverify"), true);
  assert.equal(box.calls.some((call) => call.url.includes("api.resend.com")), false);
  assert.equal(JSON.stringify(await denied.json()).includes("live-secret"), false);

  const allowed = await handleContact(post({ ...good, turnstile_token: "token-ok" }), cfg, box.doFetch);
  assert.equal(allowed.status, 200);
  assert.equal(box.calls.filter((call) => call.url.includes("api.resend.com")).length, 1);
});

test("Turnstile stays off when the secret is unset, same as Perform", async () => {
  const box = mailer();
  const { env: cfg } = env({ TURNSTILE_SECRET_KEY: "" });
  const res = await handleContact(post({ email: "ada@example.com", message: "Hi" }), cfg, box.doFetch);
  assert.equal(res.status, 200);
  assert.equal(box.calls.length, 1);
});

test("the fourth message in the rate-limit window is 429", async () => {
  const box = mailer();
  const { env: cfg } = env({
    CONTACT_RATE: { limit: async () => ({ success: false }) },
  });
  const res = await handleContact(post(good, { "cf-connecting-ip": "203.0.113.8" }), cfg, box.doFetch);
  assert.equal(res.status, 429);
  const data = (await res.json()) as { code: string };
  assert.equal(data.code, "rate");
  assert.equal(box.calls.length, 0);
});

test("the same IP keeps one rate-limit key", async () => {
  const box = mailer();
  const { env: cfg, keys } = env();
  const headers = { "cf-connecting-ip": "203.0.113.8" };
  await handleContact(post(good, headers), cfg, box.doFetch);
  await handleContact(post(good, headers), cfg, box.doFetch);
  assert.equal(keys[0], keys[1]);
  assert.equal(keys[0].includes("203.0.113.8"), false);
});

test("Resend failures are 502 and do not echo the key or the upstream body", async () => {
  const box = mailer(500, "re_test_key leaked and owner@example.com");
  const { env: cfg } = env();
  const res = await handleContact(post(good), cfg, box.doFetch);
  assert.equal(res.status, 502);
  const data = (await res.json()) as { error: string; code: string };
  assert.equal(data.code, "email");
  assert.match(data.error, /Could not send/);
  const shown = JSON.stringify(data);
  assert.equal(shown.includes("re_test_key"), false);
  assert.equal(shown.includes("owner@example.com"), false);

  const down = mailer();
  down.doFetch = (async () => {
    throw new Error("network down re_test_key");
  }) as typeof fetch;
  const { env: cfg2 } = env();
  const res2 = await handleContact(post(good), cfg2, down.doFetch);
  assert.equal(res2.status, 502);
  assert.equal(JSON.stringify(await res2.json()).includes("re_test_key"), false);
});

test("missing contact secrets return 503 and Perform config stays readable", async () => {
  for (const missing of [{ RESEND_API_KEY: "" }, { CONTACT_TO: "  " }, { RESEND_API_KEY: undefined, CONTACT_TO: undefined }] as Partial<ContactEnv>[]) {
    const box = mailer();
    const { env: cfg } = env(missing);
    const res = await handleContact(post(good), cfg, box.doFetch);
    assert.equal(res.status, 503);
    assert.deepEqual(await res.json(), { error: "contact not configured", code: "contact" });
    assert.equal(box.calls.length, 0);
  }
});

test("contact CORS allows the site and localhost only", async () => {
  const allowed = ["https://netcomposer.art", "http://localhost:8080", "http://127.0.0.1:5173", "https://localhost:8443"];
  for (const origin of allowed) {
    const box = mailer();
    const { env: cfg } = env();
    const res = await handleContact(post(good, { origin }), cfg, box.doFetch);
    assert.equal(res.status, 200, origin);
    assert.equal(res.headers.get("access-control-allow-origin"), origin);
    const pre = contactPreflight(new Request("https://nc-perform-api.example/api/contact", { method: "OPTIONS", headers: { origin } }));
    assert.equal(pre.status, 204);
    assert.equal(pre.headers.get("access-control-allow-origin"), origin);
  }
  for (const origin of ["https://evil.example", "https://netcomposer.art.evil.com", "http://localhost.evil.com", "http://netcomposer.art"]) {
    const box = mailer();
    const { env: cfg } = env();
    const res = await handleContact(post(good, { origin }), cfg, box.doFetch);
    assert.equal(res.status, 403, origin);
    assert.equal(res.headers.get("access-control-allow-origin"), null);
    assert.equal(box.calls.length, 0);
  }
  const bare = mailer();
  const { env: cfg } = env();
  const res = await handleContact(post(good, { origin: "" }), cfg, bare.doFetch);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("access-control-allow-origin"), null);
});

async function bundleWorker(): Promise<{ default: { fetch(request: Request, env: unknown): Promise<Response> } }> {
  const dir = mkdtempSync(join(tmpdir(), "nc-contact-"));
  const stub = join(dir, "cloudflare-workers-stub.mjs");
  writeFileSync(
    stub,
    `export class DurableObject { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }
     export class WorkerEntrypoint { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }
`,
  );
  const outfile = join(dir, "worker.mjs");
  await esbuild.build({
    absWorkingDir: workerRoot,
    entryPoints: [join(here, "index.ts")],
    outfile,
    bundle: true,
    format: "esm",
    platform: "neutral",
    loader: { ".html": "text" },
    alias: { "cloudflare:workers": stub },
    logLevel: "silent",
  });
  return import(pathToFileURL(outfile).href);
}

const bundled = bundleWorker();

function pageEnv(over: Record<string, unknown> = {}) {
  return {
    PERFORM_ENABLED: "1",
    TURNSTILE_SITEKEY: "0x4AAAAAAFbvFabFcwn6LUki",
    NC_INSTANCE_NAME: "test",
    NC_LLM_MODEL: "grok-4.3",
    NC_SUPPLIERS_JSON: "https://netcomposer.art/suppliers.json",
    BUDGET: {
      idFromName: () => "global",
      get: () => ({
        spendToday: async () => 0,
        enabled: async () => true,
      }),
    },
    ...over,
  };
}

test("the worker route serves contact, config, and Perform when contact secrets are missing", async () => {
  const mod = await bundled;
  const health = await mod.default.fetch(new Request("https://example.com/health"), pageEnv());
  assert.equal(health.status, 200);
  assert.equal(((await health.json()) as { ok: boolean }).ok, true);

  const hidden = await mod.default.fetch(new Request("https://example.com/api/config"), pageEnv());
  assert.equal(hidden.status, 200);
  const hiddenBody = (await hidden.json()) as { contact: boolean; perform_enabled: boolean };
  assert.equal(hiddenBody.contact, false);
  assert.equal(hiddenBody.perform_enabled, true);

  const shown = await mod.default.fetch(
    new Request("https://example.com/api/config"),
    pageEnv({ RESEND_API_KEY: "re_test_key", CONTACT_TO: "owner@example.com" }),
  );
  const shownBody = (await shown.json()) as { contact: boolean };
  assert.equal(shownBody.contact, true);
  const shownText = JSON.stringify(shownBody);
  assert.equal(shownText.includes("re_test_key"), false);
  assert.equal(shownText.includes("owner@example.com"), false);

  const off = await mod.default.fetch(
    new Request("https://example.com/api/contact", {
      method: "POST",
      headers: { origin: "https://netcomposer.art", "content-type": "application/json" },
      body: JSON.stringify(good),
    }),
    pageEnv(),
  );
  assert.equal(off.status, 503);
  assert.deepEqual(await off.json(), { error: "contact not configured", code: "contact" });

  const pre = await mod.default.fetch(
    new Request("https://example.com/api/contact", { method: "OPTIONS", headers: { origin: "http://localhost:8080" } }),
    pageEnv(),
  );
  assert.equal(pre.status, 204);
  assert.equal(pre.headers.get("access-control-allow-origin"), "http://localhost:8080");

  const other = await mod.default.fetch(
    new Request("https://example.com/api/perform", { method: "OPTIONS" }),
    pageEnv(),
  );
  assert.equal(other.status, 204);
  assert.equal(other.headers.get("access-control-allow-origin"), "*");

  const perform = await mod.default.fetch(
    new Request("https://example.com/api/perform", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ composition: "x".repeat(4001) }),
    }),
    pageEnv(),
  );
  assert.equal(perform.status, 400);
  assert.match(((await perform.json()) as { error: string }).error, /The limit is 4000/);
});

test("deploy skips empty contact secrets and wrangler keeps the plain from address", () => {
  const yml = readFileSync(join(repoRoot, ".github/workflows/deploy-perform.yml"), "utf8");
  assert.match(yml, /RESEND_API_KEY: \$\{\{ secrets\.RESEND_API_KEY \}\}/);
  assert.match(yml, /CONTACT_TO: \$\{\{ secrets\.CONTACT_TO \}\}/);
  assert.match(yml, /put RESEND_API_KEY "\$\{RESEND_API_KEY\}"/);
  assert.match(yml, /put CONTACT_TO "\$\{CONTACT_TO\}"/);
  assert.match(yml, /Skipping \$\{name\}: GitHub secret \$\{name\} is empty/);
  assert.equal(yml.includes("require RESEND_API_KEY"), false);
  assert.equal(yml.includes("require CONTACT_TO"), false);
  const readme = readFileSync(join(repoRoot, "service/README.md"), "utf8");
  assert.match(readme, /onboarding@resend\.dev/);
  assert.match(readme, /only delivers to the Resend account owner's own address/);
  assert.match(readme, /verify the domain in Resend and set `CONTACT_FROM`/);
});
