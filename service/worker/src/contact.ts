/**
 * POST /api/contact. One plain-text email through Resend. Nothing is stored.
 * The bot check uses the same Turnstile secret and dummy keys as Perform.
 */

export const CONTACT_LIMIT = 3;
export const CONTACT_PERIOD_S = 60;
export const DEFAULT_CONTACT_FROM = "NetComposer <onboarding@resend.dev>";

const RESEND_URL = "https://api.resend.com/emails";
const MAX_BODY = 20_000;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const TURNSTILE_TEST: Record<string, boolean> = {
  "1x0000000000000000000000000000000AA": true,
  "2x0000000000000000000000000000000AA": false,
  "3x0000000000000000000000000000000AA": false,
};
const DUMMY_TOKEN = "XXXX.DUMMY.TOKEN.XXXX";

const SITE_ORIGIN = "https://netcomposer.art";
const LOCAL_ORIGIN = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/;

export type ContactEnv = {
  RESEND_API_KEY?: string;
  CONTACT_TO?: string;
  CONTACT_FROM?: string;
  TURNSTILE_SECRET_KEY?: string;
  TURNSTILE_VERIFY_URL?: string;
  NC_IP_HASH_SALT?: string;
  CONTACT_RATE?: { limit(options: { key: string }): Promise<{ success: boolean }> };
};

export function contactReady(env: ContactEnv): boolean {
  return Boolean((env.RESEND_API_KEY || "").trim() && (env.CONTACT_TO || "").trim());
}

export function contactFrom(env: ContactEnv): string {
  return (env.CONTACT_FROM || "").trim() || DEFAULT_CONTACT_FROM;
}

export function contactOrigin(request: Request): { allow: boolean; echo: string | null } {
  const origin = request.headers.get("Origin");
  if (!origin) return { allow: true, echo: null };
  if (origin === SITE_ORIGIN || LOCAL_ORIGIN.test(origin)) return { allow: true, echo: origin };
  return { allow: false, echo: null };
}

export function contactPreflight(request: Request): Response {
  const gate = contactOrigin(request);
  if (!gate.allow) return contactJson({ error: "This origin is not allowed.", code: "origin" }, 403, null);
  return new Response(null, { status: 204, headers: corsHeaders(gate.echo) });
}

export async function handleContact(request: Request, env: ContactEnv, doFetch: typeof fetch = fetch): Promise<Response> {
  const gate = contactOrigin(request);
  if (!gate.allow) return contactJson({ error: "This origin is not allowed.", code: "origin" }, 403, null);

  let body: unknown;
  try {
    body = await readBody(request);
  } catch (err) {
    const tooBig = err instanceof Error && err.message === "large";
    const error = tooBig ? "The message is too long." : "Send valid JSON.";
    return contactJson({ error, code: "validation" }, 400, gate.echo);
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return contactJson({ error: "Send a JSON object.", code: "validation" }, 400, gate.echo);
  }
  const fields = body as Record<string, unknown>;
  if (honeypot(fields)) return contactJson({ ok: true }, 200, gate.echo);

  const parsed = validate(fields);
  if (!parsed.ok) return contactJson({ error: parsed.error, code: "validation" }, 400, gate.echo);
  if (!contactReady(env)) return contactJson({ error: "contact not configured", code: "contact" }, 503, gate.echo);

  const ip = clientIp(request);
  if (!(await turnstileOk(env, fields.turnstile_token, ip, doFetch))) {
    return contactJson({ error: "The bot check failed. Reload the page and try again.", code: "turnstile" }, 403, gate.echo);
  }
  if (!(await underLimit(env, ip))) {
    return contactJson(
      { error: "Too many messages from this network. Wait a minute and try again.", code: "rate" },
      429,
      gate.echo,
    );
  }

  const at = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const sent = await sendMail(env, parsed, at, doFetch);
  if (!sent) return contactJson({ error: "Could not send the message.", code: "email" }, 502, gate.echo);
  return contactJson({ ok: true }, 200, gate.echo);
}

function honeypot(body: Record<string, unknown>): boolean {
  if (!("website" in body) || body.website == null) return false;
  if (typeof body.website !== "string") return true;
  return body.website.trim() !== "";
}

function validate(body: Record<string, unknown>): { ok: true; name: string; email: string; message: string } | { ok: false; error: string } {
  let name = "";
  if (body.name != null && body.name !== "") {
    if (typeof body.name !== "string") return { ok: false, error: "name must be text." };
    if (body.name.length > 100) return { ok: false, error: "name must be 100 characters or fewer." };
    name = oneLine(body.name);
  }
  if (typeof body.email !== "string" || !body.email.trim()) return { ok: false, error: "email is required." };
  if (body.email.length > 200) return { ok: false, error: "email must be 200 characters or fewer." };
  const email = body.email.trim();
  if (!EMAIL_RE.test(email)) return { ok: false, error: "email is not valid." };
  if (typeof body.message !== "string") return { ok: false, error: "message is required." };
  if (body.message.length > 5000) return { ok: false, error: "message must be 5000 characters or fewer." };
  const message = body.message.trim();
  if (!message) return { ok: false, error: "message is required." };
  return { ok: true, name, email, message };
}

function oneLine(value: string): string {
  return value.replace(/[\r\n\t]+/g, " ").replace(/ +/g, " ").trim();
}

async function readBody(request: Request): Promise<unknown> {
  const text = await request.text();
  if (text.length > MAX_BODY) throw new Error("large");
  if (!text.trim()) return {};
  return JSON.parse(text);
}

async function underLimit(env: ContactEnv, ip: string): Promise<boolean> {
  if (!env.CONTACT_RATE) return true;
  try {
    const result = await env.CONTACT_RATE.limit({ key: await rateKey(env, ip) });
    return result.success;
  } catch {
    return true;
  }
}

async function rateKey(env: ContactEnv, ip: string): Promise<string> {
  const salt = env.NC_IP_HASH_SALT || "nc-perform";
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`${salt}:contact:${ip}`));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function turnstileOk(env: ContactEnv, token: unknown, ip: string, doFetch: typeof fetch): Promise<boolean> {
  const secret = (env.TURNSTILE_SECRET_KEY || "").trim();
  if (!secret) return true;
  if (secret in TURNSTILE_TEST) return TURNSTILE_TEST[secret] && token === DUMMY_TOKEN;
  if (typeof token !== "string" || !token) return false;
  try {
    const res = await doFetch(env.TURNSTILE_VERIFY_URL || "https://challenges.cloudflare.com/turnstile/v0/siteverify", {
      method: "POST",
      body: new URLSearchParams({ secret, response: token, remoteip: ip }),
    });
    const data = (await res.json()) as { success?: boolean };
    return Boolean(data.success);
  } catch {
    return false;
  }
}

async function sendMail(
  env: ContactEnv,
  fields: { name: string; email: string; message: string },
  at: string,
  doFetch: typeof fetch,
): Promise<boolean> {
  const key = (env.RESEND_API_KEY || "").trim();
  const to = (env.CONTACT_TO || "").trim();
  try {
    const res = await doFetch(RESEND_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${key}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        from: contactFrom(env),
        to: [to],
        subject: `NetComposer contact: ${fields.name || fields.email}`,
        reply_to: fields.email,
        text: `Name: ${fields.name || "(none)"}\nEmail: ${fields.email}\nTime: ${at}\n\n${fields.message}\n`,
      }),
      signal: AbortSignal.timeout(15_000),
    });
    await res.text().catch(() => "");
    if (!res.ok) {
      console.error(JSON.stringify({ event: "contact_send_failed", status: res.status }));
      return false;
    }
    return true;
  } catch {
    console.error(JSON.stringify({ event: "contact_send_failed", status: 0 }));
    return false;
  }
}

function clientIp(request: Request): string {
  return (request.headers.get("CF-Connecting-IP") || request.headers.get("X-Forwarded-For") || "").split(",")[0].trim() || "unknown";
}

function corsHeaders(echo: string | null): Headers {
  const headers = new Headers();
  headers.set("vary", "Origin");
  headers.set("access-control-allow-headers", "Content-Type");
  headers.set("access-control-allow-methods", "POST, OPTIONS");
  if (echo) headers.set("access-control-allow-origin", echo);
  return headers;
}

function contactJson(data: unknown, status: number, echo: string | null): Response {
  const headers = corsHeaders(echo);
  headers.set("content-type", "application/json; charset=utf-8");
  headers.set("cache-control", "no-store");
  return new Response(JSON.stringify(data), { status, headers });
}
