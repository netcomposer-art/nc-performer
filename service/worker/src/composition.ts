/**
 * Read a composition.md the way Create does (netcomposer.art/create parseMd).
 *
 * A published bar table, such as BWV 846's three columns, is sorted by bar
 * number. Chord-like words in the prose are not part of the progression.
 */

export function parseComposition(txt: string): Record<string, unknown> {
  const title = (txt.match(/^#\s+(.+)$/m) || [null, "Untitled"])[1]?.trim() || "Untitled";
  const sec = (name: string) => {
    const matched = new RegExp(`^##\\s+${name}[^\\n]*\\n([\\s\\S]*?)(?=^##\\s|$(?![\\s\\S]))`, "mi").exec(txt);
    return matched ? matched[1].trim() : null;
  };
  let composition = sec("Composition");
  let performance = sec("Performance");
  const anchors = sec("Chord anchors");
  if (composition == null && anchors && !/^\s*>/m.test(txt)) {
    const cells: string[] = [];
    for (const row of anchors.split("\n")) {
      const line = row.trim();
      if (line[0] === "|") {
        line.replace(/^\||\|$/g, "").split("|").forEach((cell) => cells.push(cell.trim()));
      }
    }
    const pairs: Array<[number, string]> = [];
    for (let k = 0; k + 1 < cells.length; k += 2) {
      if (/^\d+$/.test(cells[k]) && cells[k + 1]) {
        pairs.push([Number(cells[k]), cells[k + 1].replace(/\s*\(.*?\)/g, "").trim()]);
      }
    }
    pairs.sort((a, b) => a[0] - b[0]);
    const meta = txt
      .split("\n")
      .filter((line) => /^\*\*(Meter|Tempo)/.test(line))
      .map((line) => line.replace(/\*\*/g, ""))
      .join(" ");
    const chords = pairs.map((pair) => pair[1]).join(" | ");
    composition = (meta ? `${meta}. One chord per bar: ` : "One chord per bar: ") + chords;
    performance = performance || sec("Prose") || "";
  }
  if (composition == null) {
    const words = sec("Composer's words") || txt;
    const parts = words.split(/\bPerformance\s*[-:\u2013\u2014]/);
    composition = parts[0].replace(/^\s*Composition\s*[-:\u2013\u2014]\s*/, "").trim();
    performance = performance || (parts[1] || "").trim();
  }
  const composer = (txt.match(/\*\*Composer[^*]*:\*\*\s*([^·\n]+)/) || [null, null])[1];
  return {
    title,
    composition: composition || "",
    performance: performance || "",
    file: null,
    composer: (composer || "").trim() || null,
  };
}

/** The Create page posts the performance notes as `prompt`, not `performance`. */
export function performanceFrom(body: Record<string, unknown>, parsed = ""): string {
  const written = typeof body.performance === "string" ? body.performance : parsed;
  if (written.trim()) return written;
  return typeof body.prompt === "string" ? body.prompt : written;
}
