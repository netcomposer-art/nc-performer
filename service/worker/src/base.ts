/**
 * The public origin a container writes into clip, record and page URLs.
 *
 * NC_PUBLIC_BASE_URL wins when it is set. Otherwise the Worker's own origin
 * (the request that started the job or the warm-up) is used. An empty result
 * would let the container fall back to http://localhost:8080, which no
 * browser can fetch, so a usable origin is always returned when one is known.
 */
export function containerBase(configured: string | undefined | null, fallback: string | undefined | null): string {
  for (const raw of [configured, fallback]) {
    const base = clean(raw);
    if (base) return base;
  }
  return "";
}

function clean(raw: string | undefined | null): string {
  const text = String(raw || "").trim().replace(/\/+$/, "");
  if (!text) return "";
  try {
    const url = new URL(text);
    if (url.protocol !== "https:" && url.protocol !== "http:") return "";
    if (url.pathname !== "/" && url.pathname !== "") return url.origin + url.pathname.replace(/\/+$/, "");
    return url.origin;
  } catch {
    return "";
  }
}
