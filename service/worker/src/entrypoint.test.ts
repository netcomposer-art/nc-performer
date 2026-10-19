import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const esbuild = require("esbuild") as typeof import("esbuild");

const here = dirname(fileURLToPath(import.meta.url));
const workerRoot = join(here, "..");
const repoRoot = join(workerRoot, "..", "..");

function parseJsonc(text: string): { migrations: { new_sqlite_classes?: string[] }[] } {
  const stripped = text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  return JSON.parse(stripped);
}

function containersRoot(): string {
  return join(dirname(require.resolve("@cloudflare/containers")), "..");
}

function workerEntrypointNames(): string[] {
  const dts = readFileSync(join(containersRoot(), "dist/lib/container.d.ts"), "utf8");
  return [...dts.matchAll(/export declare class (\w+) extends WorkerEntrypoint/g)].map((match) => match[1]);
}

type StartCall = { enableInternet?: boolean };
type InterceptCall = { kind: string; host?: string };

function fakeCtx(exportsObject: Record<string, unknown>) {
  const kv = new Map<string, unknown>();
  const calls: { start: StartCall[]; intercept: InterceptCall[] } = { start: [], intercept: [] };
  const ctx = {
    id: { toString: () => "container-test" },
    exports: exportsObject,
    container: {
      running: false,
      start(config: StartCall) {
        this.running = true;
        calls.start.push(config);
      },
      monitor() {
        return new Promise(() => {});
      },
      getTcpPort() {
        return { fetch: async () => new Response("ok") };
      },
      async interceptOutboundHttp(host: string) {
        calls.intercept.push({ kind: "http", host });
      },
      async interceptOutboundHttps(host: string) {
        calls.intercept.push({ kind: "https", host });
      },
      async interceptAllOutboundHttp() {
        calls.intercept.push({ kind: "all-http" });
      },
    },
    storage: {
      sql: { exec: () => [] },
      kv: {
        get: (key: string) => kv.get(key),
        put: (key: string, value: unknown) => kv.set(key, value),
      },
      get: async (key: string) => kv.get(key),
      put: async (key: string, value: unknown) => {
        kv.set(key, value);
      },
      setAlarm: async () => {},
      deleteAlarm: async () => {},
      sync: async () => {},
    },
    blockConcurrencyWhile(fn: () => Promise<unknown>) {
      return fn();
    },
    abort() {},
    calls,
  };
  return ctx;
}

async function bundleWorker(): Promise<Record<string, unknown>> {
  const dir = mkdtempSync(join(tmpdir(), "nc-entry-"));
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

test("the worker exports every WorkerEntrypoint from the locked containers package", async () => {
  const pkg = JSON.parse(readFileSync(join(containersRoot(), "package.json"), "utf8")) as { version: string };
  assert.equal(pkg.version, "0.3.7");
  const names = workerEntrypointNames();
  assert.deepEqual(names, ["ContainerProxy"]);
  const mod = await bundleWorker();
  for (const name of names) {
    const exported = mod[name];
    assert.equal(typeof exported, "function", `worker entrypoint is missing ${name}`);
    assert.equal(Object.getPrototypeOf(exported).name, "WorkerEntrypoint");
  }
  const cfg = parseJsonc(readFileSync(join(workerRoot, "wrangler.jsonc"), "utf8"));
  const classes = cfg.migrations.flatMap((step) => step.new_sqlite_classes || []);
  for (const name of names) {
    assert.equal(classes.includes(name), false, `${name} is not a Durable Object and must not be a migration`);
  }
  assert.deepEqual(classes, ["JobDO", "BudgetDO", "RenderContainer"]);
});

test("starting the container without ContainerProxy fails the way production did", async () => {
  const mod = await bundleWorker();
  const ctx = fakeCtx({});
  const container = new (mod.RenderContainer as new (ctx: unknown, env: unknown) => {
    startAndWaitForPorts(opts: unknown): Promise<void>;
  })(ctx, {});
  await new Promise((resolve) => setTimeout(resolve, 0));
  await assert.rejects(
    () => container.startAndWaitForPorts({ startOptions: { enableInternet: false }, cancellationOptions: { portReadyTimeoutMS: 1000, instanceGetTimeoutMS: 1000 } }),
    /export ContainerProxy from the containers package/,
  );
  assert.equal(ctx.calls.start.length, 0);
});

test("the exported ContainerProxy is what the container start path calls for egress", async () => {
  const mod = await bundleWorker();
  const Proxy = mod.ContainerProxy as new (ctx: unknown, env: unknown) => unknown;
  const seen: unknown[] = [];
  const ctx = fakeCtx({
    ContainerProxy(options: { props: unknown }) {
      seen.push(options.props);
      return new Proxy({ props: options.props }, {});
    },
  });
  const container = new (mod.RenderContainer as new (ctx: unknown, env: unknown) => {
    startAndWaitForPorts(opts: unknown): Promise<void>;
    interceptHttps: boolean;
    entrypoint: string[];
  })(ctx, {});
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(container.interceptHttps, true);
  assert.deepEqual(container.entrypoint, ["/usr/local/bin/nc-perform-entrypoint", "python3", "-m", "nc_perform"]);
  await container.startAndWaitForPorts({
    startOptions: { enableInternet: false },
    cancellationOptions: { portReadyTimeoutMS: 1000, instanceGetTimeoutMS: 1000, waitInterval: 1 },
  });
  assert.equal(ctx.calls.start.length, 1);
  assert.equal(ctx.calls.start[0].enableInternet, false);
  assert.ok(ctx.calls.intercept.some((call) => call.kind === "https" && call.host === "*"));
  assert.ok(ctx.calls.intercept.some((call) => call.kind === "all-http"));
  const props = seen[0] as { allowedHosts?: string[]; enableInternet?: boolean };
  assert.equal(props.enableInternet, false);
  assert.ok(props.allowedHosts?.includes("musicmatter.art"));
  assert.ok(props.allowedHosts?.includes("*.musicmatter.art"));
  assert.ok(props.allowedHosts?.includes("audio.musicmatter.art"));
  assert.ok(props.allowedHosts?.includes("api.x.ai"));
});

test("the container entrypoint appends the runtime CA and keeps temp off /tmp", () => {
  const dir = mkdtempSync(join(tmpdir(), "nc-ca-"));
  const home = join(dir, "home");
  const sys = join(dir, "system.crt");
  const extra = join(dir, "cloudflare.crt");
  const bundle = join(dir, "bundle.crt");
  writeFileSync(sys, "SYSTEM-CA\n");
  writeFileSync(extra, "CLOUDFLARE-CA\n");
  const script = join(repoRoot, "service/container-entrypoint.sh");
  chmodSync(script, 0o755);
  const env = { ...process.env, HOME: home, TMPDIR: "/tmp", NC_SYSTEM_CA: sys, NC_CLOUDFLARE_CA: extra, NC_CA_BUNDLE: bundle };
  delete env.NC_CACHE;
  const ran = spawnSync(script, ["sh", "-c", "printf '%s\\n' \"$SSL_CERT_FILE\" \"$TMPDIR\" \"$NC_CACHE\""], {
    env,
    encoding: "utf8",
  });
  assert.equal(ran.status, 0, ran.stderr);
  const [cert, tmp, cache] = ran.stdout.split("\n");
  assert.equal(cert, bundle);
  assert.equal(tmp, join(home, "tmp"));
  assert.equal(cache, join(home, ".cache", "nc_kit"));
  const text = readFileSync(bundle, "utf8");
  assert.match(text, /SYSTEM-CA/);
  assert.match(text, /CLOUDFLARE-CA/);
});
