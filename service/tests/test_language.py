"""The Composition language, pinned to the page's vectors.

ncperf/data/language-vectors.json is the contract shared with netcomposer.art.
A number that is a whole value compares equal to an integer, because the page
emits ``4`` and Python may hold ``4.0``.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

from test_discover import _clip, _searchers  # noqa: E402

from ncperf import assemble, chords as CH, gather, interpret, language, take  # noqa: E402
from ncperf.schema import validate  # noqa: E402

VECTORS = os.path.join(ROOT, "ncperf", "data", "language-vectors.json")
PETER = "Tempo 90, T1 - [Cm(4) fm9, gm7]loop(4), LP 500->200: T2 - steady break beat, T3 - notes, create melody, dorian."


def _canon(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        if abs(value - round(value)) < 1e-9:
            return int(round(value))
        return float(f"{value:.3f}")
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return [_canon(item) for item in value]
    if isinstance(value, dict):
        return {key: _canon(item) for key, item in value.items()}
    return value


def _project(text: str) -> dict:
    parsed = language.parse(text)
    return {
        "bpm": parsed["bpm"],
        "clock": parsed["clock"],
        "seed": parsed["seed"],
        "meter": parsed["meter"],
        "gather": parsed["gather"],
        "played": [
            {
                "kind": ev.get("kind"),
                "symbol": ev.get("symbol") or None,
                "bass": None if ev.get("bass") is None else ev.get("bass"),
                "beats": ev.get("beats"),
                "bars": ev.get("bars"),
                "seconds": ev.get("seconds"),
                "recording": None if ev.get("recording") is None else ev.get("recording"),
            }
            for ev in parsed["played"]
        ],
        "hacks": [
            {"abbr": hack["abbr"], "type": hack["type"], "value": hack["value"], "ramp": hack["ramp"]}
            for hack in parsed["hacks"]
        ],
        "words": parsed["words"],
        "notes": parsed["notes"],
        "iread": parsed["iread"],
    }


def _tone_clip(symbol, key):
    return _clip("musicmatter", f"piano {symbol}", "piano", key, kind="supplier", chord=symbol)


class LanguageVectorTest(unittest.TestCase):
    def test_vectors_match_the_page_contract(self):
        with open(VECTORS, encoding="utf-8") as handle:
            rows = json.load(handle)
        self.assertGreaterEqual(len(rows), 15)
        for row in rows:
            got = _project(row["text"])
            for key in ("bpm", "clock", "seed", "meter", "gather", "played", "hacks", "words", "notes", "iread"):
                self.assertEqual(_canon(got[key]), _canon(row[key]), f"{row['name']} {key}\n{got[key]!r}")
            if row.get("place"):
                placed = language.place(language.parse(row["text"])["played"], row.get("place_caps"))
                self.assertEqual(_canon(placed), _canon(row["place"]), row["name"])

    def test_lp_dial_and_seed_forms(self):
        self.assertEqual(language.lp_dial(500), 75)
        for text in ("seed 4182", "seed4182", "sd4182", "sd 4182"):
            self.assertEqual(language.parse(text + " C")["seed"], 4182)

    def test_old_clocks_stay_on_the_old_reader(self):
        brackets = "90 BPM\n[1 bar C][2 beats A]"
        chords = [("C", None), ("A", None)]
        expected = take.written_durations(brackets, chords)
        self.assertEqual(expected, [round(4 * 60 / 90, 3), round(2 * 60 / 90, 3)])
        self.assertEqual(take.written_durations("tempo 90\n[1 bar C][2 beats A]", chords), expected)
        self.assertEqual(take.written_durations("q90\n[1 bar C][2 beats A]", chords), expected)
        faster = take.written_durations("tempo 80\n90 BPM\n[1 bar C]", [("C", None)])
        self.assertEqual(faster, [round(4 * 60 / 90, 3)])
        opening = take._opening_bpm("quarter = 60. one chord per bar. at bar 3, 90 BPM", take._tempo_changes("quarter = 60. one chord per bar. at bar 3, 90 BPM"))
        self.assertEqual(opening, 60)
        self.assertEqual(
            [sym for sym, _bass in take.composition_chords("Cm # not Am\nF")],
            ["Cm", "F"],
        )
        plain = "Cm, F, G, Am, Dm."
        self.assertEqual(take.composition_chords(plain), CH.find_full(plain))
        self.assertEqual(language.uncomment("F#m"), "F#m")
        self.assertFalse(language.uses_language("90 BPM\n[1 bar C][2 beats A]"))
        self.assertFalse(interpret.states_length('"32 bars 90 BPM"'))
        self.assertEqual(interpret._length('"32 bars 90 BPM"'), 120.0)
        self.assertFalse(interpret.states_length("tempo 90"))
        self.assertAlmostEqual(interpret._length("32 bars, tempo 90"), 32 * 4 * 60 / 90, places=3)


class PeterGatherTest(unittest.TestCase):
    def test_peters_line_gathers_twelve_chords_from_three_recordings(self):
        pools = {"musicmatter": [_tone_clip(sym, f"mm-{sym}") for sym in ("Cm", "Fm9", "Gm7")]}
        comp = {"title": "Peter", "composer": "Peter", "composition": PETER, "performance": ""}
        plan = assemble.plan_raw(
            comp, seed=7, searchers=_searchers(pools), verify_live=False, note_maps=[],
        )
        self.assertEqual(validate(plan), [])
        self.assertEqual(plan["seed"], 7)
        self.assertEqual(len(plan["clips"]), 12)
        self.assertEqual(len(plan["chords"]), 12)
        self.assertEqual([clip["id"] for clip in plan["clips"]], [f"c{i}" for i in range(12)])
        self.assertEqual([chord["symbol"] for chord in plan["chords"]], ["Cm", "Fm9", "Gm7"] * 4)
        self.assertEqual([clip["sids"] for clip in plan["clips"]], [["r0"], ["r1"], ["r2"]] * 4)
        self.assertEqual(len(plan["sources"]), 3)
        self.assertAlmostEqual(plan["chords"][0]["dur"], 2.667, places=3)
        self.assertAlmostEqual(plan["chords"][1]["dur"], 0.1, places=3)
        self.assertAlmostEqual(plan["chords"][2]["dur"], 0.1, places=3)
        self.assertEqual(plan["master"], {"buses": {"dry": {}}})
        self.assertNotIn("stretch_to_s", json.dumps(plan))
        self.assertNotIn("lowpass", json.dumps(plan["master"]))
        self.assertTrue(any("Live" in note for note in plan["notes"]))
        self.assertIn("ramp", " ".join(plan["notes"]).lower())
        sounds = gather.public_sounds(plan, "g-0123456789ab", "http://127.0.0.1:9")
        self.assertEqual(len(sounds), 12)
        caps = [1, 3.5, 2] * 4
        language.apply_language_durations(plan, caps)
        placed = language.place(plan["language_clock"]["played"], caps)
        self.assertEqual(plan["chords"][0]["t"], placed["entries"][0]["at_s"])
        self.assertEqual(plan["chords"][0]["dur"], placed["entries"][0]["len_s"])
        self.assertEqual(plan["chords"][1]["t"], 2.667)
        self.assertEqual(plan["chords"][1]["dur"], 3.5)
        self.assertEqual(plan["chords"][2]["t"], 6.167)
        self.assertEqual(plan["chords"][2]["dur"], 2)
        self.assertEqual(plan["clips"][3]["sids"], ["r0"])
        self.assertEqual(plan["clips"][3]["symbol"], "Cm")
        self.assertEqual(plan["clips"][3]["t"], plan["chords"][3]["t"])
        self.assertEqual(plan["chords"][3]["t"], placed["entries"][3]["at_s"])
        self.assertEqual(plan["clips"][4]["sids"], ["r1"])

    def test_a_written_seed_wins_over_the_request(self):
        pools = {"musicmatter": [_tone_clip("Cm", "mm-Cm")]}
        comp = {"title": "Seed", "composer": "", "composition": "seed 4182 Cm", "performance": ""}
        plan = assemble.plan_raw(
            comp, seed=3, searchers=_searchers(pools), verify_live=False, note_maps=[],
        )
        self.assertEqual(plan["seed"], 4182)
        self.assertNotIn("language_clock", plan)

    def test_a_legacy_list_is_not_a_language_clock(self):
        pools = {"musicmatter": [_tone_clip(sym, f"mm-{sym}") for sym in ("Cm", "F", "G", "Am", "Dm")]}
        comp = {"title": "Old", "composer": "", "composition": "Cm, F, G, Am, Dm.", "performance": ""}
        plan = assemble.plan_raw(
            comp, seed=1, searchers=_searchers(pools), verify_live=False, note_maps=[],
        )
        self.assertNotIn("language_clock", plan)
        self.assertEqual([clip["sids"][0] for clip in plan["clips"]], ["c0", "c1", "c2", "c3", "c4"])
        self.assertEqual(validate(plan), [])


if __name__ == "__main__":
    unittest.main()
