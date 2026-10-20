"""Chord-matched source discovery, with fake searchers and no network."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "service"))

from collections import Counter

from ncperf import chords as CH
from ncperf import discover, licenses as LIC, notemap
from ncperf.fetch import IOWA_TERMS
from ncperf.fit import fit_plan
from ncperf.interpret import from_plan
from ncperf.schema import validate
from nc_perform.config import Settings
from nc_perform.limits import check_plan


COMP = {
    "title": "Glass garden",
    "composer": "anonymous",
    "composition": "1 min. Cm, F, G, Am, Dm.",
    "performance": "Clear and open, like light through glass.",
}
CHORDS = ("Cm", "F", "G", "Am", "Dm")


def _clip(origin, title, instrument, key, fit=0, kind="commons", chord=None, voicing="exact",
          pitched=None, text=None, license_id="CC0-1.0", author="Ada"):
    if kind == "supplier":
        page = f"https://musicmatter.art/{key}.nc.json"
        source = {
            "kind": "supplier", "manifest": page, "pitched": chord is not None,
            "title": title, "author": author, "license": "CC-BY-SA-4.0", "page": page,
        }
        license_id = "CC-BY-SA-4.0"
    elif kind == "freesound":
        sid = "".join(ch for ch in key if ch.isdigit()) or "325647"
        page = f"https://freesound.org/s/{sid}/"
        source = {
            "kind": "freesound", "id": sid, "pitched": False,
            "title": title, "author": author, "license": license_id, "page": page,
        }
    else:
        page = f"https://commons.wikimedia.org/wiki/{key}"
        source = {
            "kind": "commons",
            "url": f"https://upload.wikimedia.org/{key}.ogg",
            "page": page,
            "license": license_id,
            "title": title,
            "author": author,
            "source": origin,
            "pitched": False,
        }
    cand = {
        "origin": origin,
        "title": title,
        "author": author,
        "license": license_id,
        "instrument": instrument,
        "fit": fit,
        "source": source,
        "key": key,
        "text": text if text is not None else title,
        "page": page,
    }
    if chord:
        cand["chord_symbol"] = chord
        cand["voicing_kind"] = voicing
        cand["pitched"] = True
    if pitched is not None:
        cand["pitched"] = pitched
    return cand


def _chords(origin, instrument, kind="supplier", voicing="exact", prefix="mm"):
    return [
        _clip(origin, f"{instrument} {sym}", instrument, f"{prefix}-{sym}", kind=kind, chord=sym, voicing=voicing)
        for sym in CHORDS
    ]


def _searchers(pools):
    def make(origin):
        rows = pools.get(origin) or []

        def run(*_args):
            return list(rows)

        return run

    return {origin: make(origin) for origin in discover.ORIGINS}


class DiscoverTest(unittest.TestCase):
    def test_strikes_match_the_composition_chord(self):
        wrong = _clip("musicmatter", "Choir A7", "choir", "a7", kind="supplier", chord="A7", voicing="exact")
        pools = {
            "musicmatter": _chords("musicmatter", "choir", prefix="choir") + _chords("musicmatter", "fat synth", prefix="fat") + [wrong],
            "freesound": [_clip("freesound", "Rain room tone", "texture", "fs9", kind="freesound", text="field recording of rain, room tone")],
        }
        plan = discover.plan_for(COMP, seed=11, searchers=_searchers(pools), verify_live=False)
        self.assertEqual(validate(plan), [])
        self.assertNotIn("stretch_to_s", json.dumps(plan))
        rows = plan["source_mix"]["per_chord"]
        self.assertEqual([row["symbol"] for row in rows], list(CHORDS))
        for row in rows:
            self.assertEqual(row["match"], "exact")
            self.assertIn(row["symbol"], row["title"])
            self.assertNotIn("A7", row["title"])
        blob = json.dumps(plan["sources"])
        self.assertNotIn("a7", blob)
        self.assertNotIn("A7", blob)
        for src in plan["sources"].values():
            for key in ("title", "author", "license", "page"):
                self.assertTrue(src.get(key), src)
            self.assertTrue(LIC.allowed(src["license"]))

    def test_alt_voicing_then_piano(self):
        pools = {
            "musicmatter": [
                _clip("musicmatter", "Choir Cm inversion", "choir", "cm-alt", kind="supplier", chord="Cm", voicing="alt"),
                *[_clip("musicmatter", f"Piano {sym}", "piano", f"p-{sym}", kind="supplier", chord=sym, voicing="exact") for sym in CHORDS],
            ],
        }
        plan = discover.plan_for(COMP, seed=1, searchers=_searchers(pools), verify_live=False)
        rows = {row["symbol"]: row for row in plan["source_mix"]["per_chord"]}
        self.assertEqual(rows["Cm"]["match"], "alt voicing")
        self.assertEqual(rows["Cm"]["instrument"], "choir")
        for sym in ("F", "G", "Am", "Dm"):
            self.assertEqual(rows[sym]["match"], "piano fallback")
            self.assertEqual(rows[sym]["instrument"], "piano")

    def test_a_wrong_chord_does_not_fill_a_gap(self):
        pools = {"musicmatter": [_clip("musicmatter", "Choir A7", "choir", "a7", kind="supplier", chord="A7")]}
        with self.assertRaises(ValueError) as caught:
            discover.plan_for(COMP, seed=1, searchers=_searchers(pools), verify_live=False)
        self.assertIn("Cm", str(caught.exception))

    def test_only_the_origin_that_has_the_chord_is_used(self):
        pools = {"musicmatter": _chords("musicmatter", "choir")}
        plan = discover.plan_for(COMP, seed=3, searchers=_searchers(pools), verify_live=False)
        self.assertEqual(set(plan["source_mix"]["by_origin"]), {"musicmatter"})
        self.assertGreaterEqual(plan["source_mix"]["by_origin"]["musicmatter"], 5)

    def test_named_origin_is_used_only_when_it_has_the_chord(self):
        comp = dict(COMP, performance="Use only freesound, nothing else.")
        pools = {
            "musicmatter": _chords("musicmatter", "choir"),
            "freesound": _chords("freesound", "choir", kind="freesound", prefix="fs"),
        }
        plan = discover.plan_for(comp, seed=2, searchers=_searchers(pools), verify_live=False)
        self.assertEqual(set(plan["source_mix"]["by_origin"]), {"freesound"})

        pools["freesound"] = [_clip("freesound", "Rain room tone", "texture", "fs1", kind="freesound", text="field recording of rain, room tone")]
        plan = discover.plan_for(comp, seed=2, searchers=_searchers(pools), verify_live=False)
        strikes = [row["origin"] for row in plan["source_mix"]["per_chord"]]
        self.assertEqual(set(strikes), {"musicmatter"})

    def test_instrument_hint_prefers_that_timbre(self):
        pools = {
            "musicmatter": _chords("musicmatter", "piano", prefix="p") + _chords("musicmatter", "choir", prefix="c"),
            "wikimedia": _chords("wikimedia", "choir", kind="commons", prefix="w"),
        }
        pools["freesound"] = _chords("freesound", "choir", kind="freesound", prefix="fs")
        plan = discover.plan_for(COMP, seed=4, instrument="choir", searchers=_searchers(pools), verify_live=False)
        for row in plan["source_mix"]["per_chord"]:
            self.assertEqual(row["instrument"], "choir")
            self.assertNotEqual(row["match"], "piano fallback")
            self.assertEqual(row["built"], "whole-chord file")
        counts = Counter(row["origin"] for row in plan["source_mix"]["per_chord"])
        self.assertGreaterEqual(len(counts), 3)
        self.assertLessEqual(max(counts.values()), 2)
        self.assertEqual(plan["source_mix"]["instrument_hint"], "choir")
        self.assertIn("musicmatter", plan["source_mix"]["queried"])

    def test_a_live_cover_is_not_texture(self):
        cover = _clip(
            "internet_archive",
            "Ramblin' Band LIVE @ BurnDown Denver - Mama Tried",
            "texture",
            "mama",
            text="live concert cover of Mama Tried",
        )
        rain = _clip(
            "freesound", "Rain room tone", "texture", "fs3", kind="freesound",
            text="field recording of rain, room tone",
        )
        pools = {"musicmatter": _chords("musicmatter", "choir"), "internet_archive": [cover], "freesound": [rain]}
        plan = discover.plan_for(COMP, seed=5, searchers=_searchers(pools), verify_live=False)
        blob = json.dumps(plan)
        self.assertNotIn("Mama Tried", blob)
        self.assertNotIn("Ramblin", blob)
        self.assertEqual(plan["source_mix"]["texture"]["title"], "Rain room tone")
        self.assertEqual(plan["layers"][1]["id"], "bed")
        self.assertIn("texture", plan["layers"][1]["role"])

    def test_a_source_without_a_license_is_not_used(self):
        bare = _clip("wikimedia", "Choir Cm", "choir", "bare", chord="Cm")
        bare["license"] = ""
        bare["source"].pop("license")
        pools = {
            "wikimedia": [bare],
            "musicmatter": [c for c in _chords("musicmatter", "choir") if c["chord_symbol"] != "Cm"],
        }
        with self.assertRaises(ValueError) as caught:
            discover.plan_for(COMP, seed=1, searchers=_searchers(pools), verify_live=False)
        self.assertIn("Cm", str(caught.exception))

    def test_no_sounds_raises(self):
        with self.assertRaises(ValueError):
            discover.plan_for(COMP, seed=1, searchers=_searchers({}), verify_live=False)

    def test_freesound_hits_are_not_labeled_with_the_query(self):
        html = '<a href="/people/ada/sounds/12345/">x</a><a href="/people/ada/sounds/12345/">x</a>'
        orig = discover._get
        discover._get = lambda url, timeout=20: html.encode()
        try:
            hits = discover.search_freesound(["choir", "synth"], "choir", ["Cm"], 1, host_ok_fn=lambda _h: True)
        finally:
            discover._get = orig
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["instrument"], "texture")
        self.assertNotIn("choir", hits[0]["title"])

    def test_a_named_instrument_beats_the_chord_set_collection(self):
        blob = "Choir jacobschoir full-chord-set-v4-jacobschoir"
        self.assertEqual(discover._instrument_label(blob, None), "choir")
        self.assertEqual(discover._instrument_label("Fat Synth fatsynth full-chord-set-v4-fatsynth", None), "synth")
        self.assertEqual(discover._instrument_label("full-chord-set-v4 piano", None), "piano")
        self.assertIn("piano", discover.excluded_terms("Keep the piano out of the lead."))

    def test_offering_labels_use_root_and_quality(self):
        choir = {
            "audio": "https://audio.musicmatter.art/fullchords/jacobschoir/Eb/05_suspended_Ebsus2_root.flac",
            "manifest": "https://audio.musicmatter.art/fullchords/jacobschoir/Eb/05_suspended_Ebsus2_root.nc.json",
            "chord": "Ebsus2", "root": "Eb", "quality": "sus2", "gesture": "bounce", "voicing": "root",
            "instrument_name": "Choir", "default_voicing": True,
        }
        alt = dict(choir, gesture="alt", voicing="inv1", default_voicing=False)
        piano = {
            "audio": "https://audio.musicmatter.art/fullchords/Eb/05_suspended_Ebsus2_root.flac",
            "manifest": "https://musicmatter.art/sounds/fullchords-v4/Eb/05_suspended_Ebsus2_root.nc.json",
            "collection": "full-chord-set-v4",
        }
        self.assertEqual(discover._offering_symbol(choir), "Ebsus2")
        self.assertEqual(discover._offering_symbol(piano), "Ebsus2")
        self.assertEqual(discover._offering_instrument(choir), "choir")
        self.assertEqual(discover._offering_instrument(piano), "piano")
        self.assertEqual(discover._voicing_kind(choir), "exact")
        self.assertEqual(discover._voicing_kind(alt), "alt")
        six = {"audio": "https://audio.musicmatter.art/fullchords/Ab/x_Ab6-9_root.flac", "manifest": "m.nc.json"}
        self.assertEqual(discover._offering_symbol(six), "Ab6-9")

    def test_uiowa_terms_are_public_domain_equivalent_and_unused(self):
        self.assertIn("without restrictions", IOWA_TERMS)
        self.assertTrue(LIC.allowed("PD-equivalent"))
        # Single notes are assembled from the pitch map, not returned as beds.
        self.assertEqual(discover.search_uiowa(["violin"], None, ["Ebsus2"], 1), [])


def _note(origin, instrument, midi, pluck=False):
    sample = {
        "midi": midi,
        "title": f"{instrument} {midi}",
        "author": "Ada",
        "license": "PD-equivalent" if origin == "uiowa" else "CC0-1.0",
        "page": "https://theremin.music.uiowa.edu/MIS.html" if origin == "uiowa" else f"https://freesound.org/s/{600000 + midi}/",
        "pitched": True,
        "expect_midi": midi,
        "cents": 1.0,
    }
    if origin == "uiowa":
        sample["kind"] = "uiowa_mis"
        sample["url"] = f"https://theremin.music.uiowa.edu/sound%20files/MIS/{instrument}/{midi}.aiff"
    else:
        sample["kind"] = "freesound"
        sample["id"] = str(600000 + midi)
    return sample


def _map(origin, instrument, pluck=False):
    notes = {}
    for sym in CHORDS:
        _name, midis = min(CH.voicings(sym), key=lambda row: (len(row[1]), max(row[1]) - min(row[1])))
        for midi in midis:
            notes[str(midi)] = _note(origin, instrument, midi)
    return {
        "origin": origin,
        "instrument": instrument,
        "pluck": pluck,
        "page": "https://theremin.music.uiowa.edu/MIS.html",
        "notes": notes,
    }


class AssembledStrikeTest(unittest.TestCase):
    def test_native_pitch_notes_count_toward_the_strike_cap(self):
        pools = {"musicmatter": _chords("musicmatter", "choir")}
        maps = [_map("uiowa", "cello pizzicato", pluck=True), _map("freesound", "flute")]
        plan = discover.plan_for(
            COMP, seed=11, searchers=_searchers(pools), verify_live=False, note_maps=maps,
        )
        self.assertEqual(validate(plan), [])
        self.assertNotIn("stretch_to_s", json.dumps(plan))
        rows = plan["source_mix"]["per_chord"]
        self.assertEqual([row["symbol"] for row in rows], list(CHORDS))
        counts = Counter(row["origin"] for row in rows)
        self.assertLessEqual(max(counts.values()), 2)
        self.assertGreaterEqual(len(counts), 3)
        self.assertLessEqual(counts.get("musicmatter", 0), 2)
        self.assertTrue(any(row["built"] == "assembled notes" for row in rows))
        self.assertEqual(plan["source_mix"]["share"], plan["source_mix"]["strike_share"])
        self.assertLessEqual(len(plan["layers"]), Settings().max_layers)
        self.assertEqual(Settings().max_layers, 16)
        self.assertLessEqual(sum(1 for ly in plan["layers"] if ly["type"] == "chord_notes"), 1)
        for src in plan["sources"].values():
            for key in ("title", "author", "license", "page"):
                self.assertTrue(src.get(key), src)

    def test_pluck_is_a_real_plucked_string(self):
        comp = dict(COMP, performance="Layer a plucked string with a fat synth. Vary the color on each chord.")
        pools = {"musicmatter": _chords("musicmatter", "fat synth", prefix="fat")}
        maps = [
            _map("uiowa", "violin pizzicato", pluck=True),
            _map("freesound", "guitar", pluck=True),
            _map("uiowa", "piano"),
        ]
        plan = discover.plan_for(
            comp, seed=13, searchers=_searchers(pools), verify_live=False, note_maps=maps,
        )
        rows = plan["source_mix"]["per_chord"]
        counts = Counter(row["origin"] for row in rows)
        self.assertLessEqual(counts.get("musicmatter", 0), 2)
        self.assertLessEqual(max(counts.values()), 2)
        plucked = [row for row in rows if row["instrument"] in ("violin pizzicato", "guitar")]
        self.assertGreaterEqual(len(plucked), 2)
        for row in plucked:
            self.assertEqual(row["built"], "assembled notes")
        self.assertNotIn("piano", [row["instrument"] for row in rows])

    def test_sparse_and_custom_libraries_are_not_a_chord(self):
        glass = ("Ebsus2", "Cm-add9", "Ab6-9", "Bb7sus4", "Eb6")
        for sym in glass:
            self.assertFalse(notemap.voicing_covered(sym, notemap.VSCO_PIZZ_MIDIS))
            self.assertFalse(notemap.voicing_covered(sym, {60}))
        custom = (
            "You are free to use these samples. The only restriction is that they must not be "
            "sold or made available 'as is' (as a sampler instrument). "
            "https://creativecommons.org/licenses/by-sa/4.0/"
        )
        self.assertFalse(notemap.philharmonia_compatible(custom))
        self.assertTrue(notemap.philharmonia_compatible(
            "Licensed https://creativecommons.org/publicdomain/zero/1.0/ with no further restriction."
        ))
        self.assertFalse(notemap.note_ok({"midi": 60, "cents": 2, "spread_cents": 3}, 61))
        self.assertTrue(notemap.note_ok({"midi": 60, "cents": 4, "spread_cents": 3}, 60))

    def test_discover_kind_survives_loading_a_plan(self):
        plan = discover.plan_for(
            COMP, seed=1,
            searchers=_searchers({"wikimedia": _chords("wikimedia", "choir", kind="commons", prefix="w")}),
            verify_live=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.json")
            json.dump(plan, open(path, "w", encoding="utf-8"))
            loaded = from_plan(path)
        self.assertEqual(loaded["interpreter"]["kind"], "discover")


def _fs(i: int) -> dict:
    return {
        "kind": "freesound",
        "id": str(i),
        "title": f"note {i}",
        "author": "Ada",
        "license": "CC0-1.0",
        "page": f"https://freesound.org/s/{i}/",
        "pitched": True,
    }


def _shell() -> dict:
    return {
        "nc_plan_version": "0.1",
        "title": "Packed",
        "composition": {"title": "Packed", "words_verbatim": {"composition": "C"}},
        "interpreter": {"kind": "discover"},
        "seed": 1,
        "length_s": 30,
        "tail_s": 4,
        "chords": [],
        "sources": {},
        "banks": {},
        "layers": [],
        "master": {"buses": {"keys": {}}},
        "notes": [],
    }


class LayerCapTest(unittest.TestCase):
    def test_many_assembled_chords_pass_the_real_validator(self):
        """Twelve assembled strikes share one layer, under the 16-layer cap."""
        comp = dict(
            COMP,
            composition="2 mins. Cm, F, G, Am, Dm.",
            performance="Bright and open; let each chord ring into the next.",
        )
        maps = [_map("uiowa", "cello pizzicato", pluck=True), _map("freesound", "flute")]
        pools = {
            "freesound": [_clip(
                "freesound", "Rain room tone", "texture", "325647", kind="freesound",
                text="field recording of rain, room tone",
            )],
        }
        plan = discover.plan_for(
            comp, seed=11, searchers=_searchers(pools), verify_live=False, note_maps=maps,
        )
        self.assertEqual(validate(plan), [])
        self.assertEqual(len(plan["chords"]), 12)
        self.assertEqual(Settings().max_layers, 16)
        self.assertLessEqual(len(plan["layers"]), 16)
        notes = [ly for ly in plan["layers"] if ly["type"] == "chord_notes"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(notes[0]["per_chord"]), 12)
        for pc in notes[0]["per_chord"]:
            self.assertIn(pc["bank"], plan["banks"])
            self.assertTrue(plan["banks"][pc["bank"]]["notes"])
        rows = plan["source_mix"]["per_chord"]
        self.assertTrue(all(row["built"] == "assembled notes" for row in rows))
        counts = Counter(row["origin"] for row in rows)
        self.assertGreaterEqual(len(counts), 2)
        self.assertLessEqual(max(counts.values()), max(1, len(rows) // 2))
        self.assertNotIn("stretch_to_s", json.dumps(plan))
        check_plan(plan, Settings(), None)

    def test_split_layers_are_packed_instead_of_rejected(self):
        plan = _shell()
        for i in range(20):
            sid = f"n{i}"
            plan["sources"][sid] = _fs(810000 + i)
            plan["banks"][f"b{i}"] = {"notes": {"60": [sid]}}
            plan["chords"].append({
                "t": round(i * 1.0, 3), "dur": 1.0, "symbol": "C", "voicing": [60],
            })
            plan["layers"].append({
                "id": f"notes-{i}",
                "type": "chord_notes",
                "bus": "keys",
                "bank": f"b{i}",
                "style": "strike",
                "per_chord": [{"chord": i, "strikes_at": [0.0]}],
            })
        plan["sources"]["bed"] = _fs(325647)
        plan["layers"].append({
            "id": "bed",
            "type": "clips",
            "bus": "pad",
            "role": "texture room tone",
            "events": [{"t": 0.0, "source": "bed", "dur_s": 10.0, "gain": 0.2}],
        })
        self.assertGreater(len(plan["layers"]), 16)
        added = fit_plan(plan)
        self.assertLessEqual(len(plan["layers"]), 16)
        notes = [ly for ly in plan["layers"] if ly["type"] == "chord_notes"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(notes[0]["per_chord"]), 20)
        banks = {pc["bank"] for pc in notes[0]["per_chord"]}
        self.assertGreaterEqual(len(banks), 20)
        self.assertTrue(any("packed into one layer" in note for note in added))
        check_plan(plan, Settings(), None)
        self.assertEqual(fit_plan(plan), [])

    def test_one_layer_per_note_shares_a_strike(self):
        plan = _shell()
        plan["chords"] = [{"t": 0, "dur": 8, "symbol": "C", "voicing": [60, 64, 67]}]
        for i, midi in enumerate((60, 64, 67)):
            sid = f"n{i}"
            plan["sources"][sid] = _fs(820000 + i)
            plan["banks"][f"b{i}"] = {"notes": {str(midi): [sid]}}
            plan["layers"].append({
                "id": f"note-{i}",
                "type": "chord_notes",
                "bank": f"b{i}",
                "per_chord": [{"chord": 0, "strikes_at": [0.0]}],
            })
        fit_plan(plan)
        notes = [ly for ly in plan["layers"] if ly["type"] == "chord_notes"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(notes[0]["per_chord"]), 1)
        bank = notes[0]["per_chord"][0]["bank"]
        self.assertEqual(set(plan["banks"][bank]["notes"]), {"60", "64", "67"})
        check_plan(plan, Settings(), None)

    def test_too_many_sources_are_trimmed_not_rejected(self):
        plan = _shell()
        voicing = list(range(40, 70))
        plan["chords"] = [{"t": 0, "dur": 8, "symbol": "C", "voicing": list(voicing)}]
        notes = {}
        for midi in voicing:
            sid = f"n{midi}"
            plan["sources"][sid] = _fs(830000 + midi)
            notes[str(midi)] = [sid]
        plan["banks"]["b"] = {"notes": notes}
        plan["layers"] = [{
            "id": "notes",
            "type": "chord_notes",
            "bank": "b",
            "per_chord": [{"chord": 0, "bank": "b", "strikes_at": [0.0]}],
        }]
        added = fit_plan(plan, max_sources=24)
        self.assertLessEqual(len(plan["sources"]), 24)
        self.assertGreaterEqual(len(plan["chords"][0]["voicing"]), 1)
        self.assertTrue(any("sources" in note for note in added))
        check_plan(plan, Settings(), None)
        self.assertEqual(fit_plan(plan, max_sources=24), [])

    def test_overlong_piece_is_shortened_not_rejected(self):
        plan = _shell()
        plan["length_s"] = 400
        plan["tail_s"] = 4
        plan["chords"] = [{"t": 0, "dur": 400, "symbol": "C", "voicing": [60]}]
        plan["sources"] = {"n": _fs(840000)}
        plan["banks"] = {"b": {"notes": {"60": ["n"]}}}
        plan["layers"] = [{
            "id": "notes",
            "type": "chord_notes",
            "bank": "b",
            "per_chord": [{"chord": 0, "bank": "b", "strikes_at": [0.0]}],
        }]
        added = fit_plan(plan)
        self.assertLessEqual(plan["length_s"] + plan["tail_s"], 180)
        self.assertGreaterEqual(plan["chords"][0]["dur"], 0.1)
        self.assertTrue(any("shortened" in note for note in added))
        check_plan(plan, Settings(), None)
        self.assertEqual(fit_plan(plan), [])


if __name__ == "__main__":
    unittest.main()
