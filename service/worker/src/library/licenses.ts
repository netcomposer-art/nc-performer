/**
 * Library license rule (design v1, "Licensing and attribution"): CC0, CC BY, CC BY-SA and public domain get in;
 * NC, ND, Sampling+ and anything else is rejected at harvest with the reason recorded. Read from the license URL each
 * sound carries, never from a search filter alone.
 */

export type LicenseVerdict =
  | { ok: true; spdx: string; url: string; label: string }
  | { ok: false; reason: string; raw: string };

const CC0_URL = "https://creativecommons.org/publicdomain/zero/1.0/";
const PDM_URL = "https://creativecommons.org/publicdomain/mark/1.0/";

export function classifyLicense(raw: unknown): LicenseVerdict {
  const text = typeof raw === "string" ? raw.trim() : "";
  const low = text.toLowerCase();
  if (!low) return { ok: false, reason: "no license given", raw: text };
  if (low.includes("sampling+") || low.includes("sampling%2b") || /\bsampling\s*plus\b/.test(low)) {
    return { ok: false, reason: "Sampling+ is not an open license", raw: text };
  }
  if (low.includes("publicdomain/zero") || low === "creative commons 0" || low === "cc0" || low === "cc0-1.0") {
    return { ok: true, spdx: "CC0-1.0", url: CC0_URL, label: "CC0 1.0" };
  }
  if (low.includes("publicdomain/mark")) {
    return { ok: true, spdx: "PDM-1.0", url: PDM_URL, label: "Public Domain Mark 1.0" };
  }
  const m = low.match(/creativecommons\.org\/licenses\/([a-z+-]+)\/(\d\.\d)/);
  if (m) {
    const kind = m[1];
    const version = m[2];
    if (kind.includes("nc")) return { ok: false, reason: `NonCommercial (CC ${kind.toUpperCase()} ${version})`, raw: text };
    if (kind.includes("nd")) return { ok: false, reason: `NoDerivatives (CC ${kind.toUpperCase()} ${version})`, raw: text };
    const url = `https://creativecommons.org/licenses/${kind}/${version}/`;
    if (kind === "by") return { ok: true, spdx: `CC-BY-${version}`, url, label: `CC BY ${version}` };
    if (kind === "by-sa") return { ok: true, spdx: `CC-BY-SA-${version}`, url, label: `CC BY-SA ${version}` };
    return { ok: false, reason: `unsupported Creative Commons license (${kind} ${version})`, raw: text };
  }
  if (/noncommercial/.test(low)) return { ok: false, reason: "NonCommercial", raw: text };
  if (low === "attribution" || low === "attribution sharealike") {
    return { ok: false, reason: "license version unknown (no license URL)", raw: text };
  }
  return { ok: false, reason: `not an accepted license (${text.slice(0, 80)})`, raw: text };
}
