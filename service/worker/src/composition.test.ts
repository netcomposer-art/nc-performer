import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { parseComposition } from "./composition.ts";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..", "..");

// Bar order from examples/bwv846/composition.md. A row-wise read of the three
// columns would put bar 13 (Dm/F) second, and a prose scan adds more chords.
const BWV = [
  "C", "Dm7/C", "G7/B", "C", "Am/C", "D7/C", "G/B", "Cmaj7/B", "Am7", "D7", "G",
  "C#dim7/G", "Dm/F", "Bdim7/F", "C/E", "Fmaj7/E", "Dm7", "G7", "C", "C7",
  "Fmaj7", "F#dim7", "Bdim7/Ab", "G7", "C/G", "G7sus4", "G7", "F#dim7/G", "C/G",
  "G7sus4", "G7", "C7", "Dm7/C", "G7/C", "C",
];

test("bwv846's three-column table is read in bar order, without the prose", () => {
  const text = readFileSync(join(repo, "examples", "bwv846", "composition.md"), "utf8");
  const parsed = parseComposition(text);
  const composition = String(parsed.composition);
  const chords = composition.split("One chord per bar: ")[1].split(" | ").map((cell) => cell.trim());
  assert.equal(chords.length, 35);
  assert.deepEqual(chords, BWV);
  assert.equal(chords[1], "Dm7/C");
  assert.notEqual(chords[1], "Dm/F");
  assert.match(composition, /^Meter: 4\/4 · Tempo: quarter note ≈ 69 BPM/);
  assert.equal(composition.includes("Bach"), false);
  assert.equal(composition.includes("pedal"), false);
  assert.equal(composition.includes("\n|"), false);
  const performance = String(parsed.performance);
  assert.match(performance, /Bach's C major Prelude/);
  assert.match(performance, /slight broadening over the last three bars/);
  assert.equal(performance.includes("| 1 |"), false);
});

test("a Composition section is kept as written", () => {
  const text = readFileSync(join(repo, "examples", "glass-garden.composition.md"), "utf8");
  const parsed = parseComposition(text);
  assert.match(String(parsed.composition), /Ebsus2, Cm-add9/);
  assert.match(String(parsed.performance), /Clear and open/);
});
