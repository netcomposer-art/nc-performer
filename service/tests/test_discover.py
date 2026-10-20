"""Chord-matched source discovery, with fake searchers and no network."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
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
        rows = plan["source_mix"]["per_chord"]
        # Choir covers only Cm. Piano covers every chord, so the take stays on piano.
        self.assertTrue(all(row["instrument"] == "piano" for row in rows))
        self.assertTrue(all(row["match"] == "piano fallback" for row in rows))

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
        self.assertEqual(len(counts), 1)
        self.assertEqual(plan["source_mix"]["instrument_hint"], "choir")
        self.assertIn("musicmatter", plan["source_mix"]["queried"])
        origins = set()
        for salt in range(1, 16):
            other = discover.plan_for(COMP, seed=salt, instrument="choir", searchers=_searchers(pools), verify_live=False)
            origins.add(other["source_mix"]["per_chord"][0]["origin"])
            self.assertEqual(len({row["origin"] for row in other["source_mix"]["per_chord"]}), 1)
        self.assertGreaterEqual(len(origins), 2)

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
        self.assertIsNone(plan["source_mix"]["texture"])
        self.assertFalse(any(ly["id"] == "bed" for ly in plan["layers"]))
        spaced = dict(COMP, performance="Clear and open, with room tone and a little space.")
        plan = discover.plan_for(spaced, seed=5, searchers=_searchers(pools), verify_live=False)
        blob = json.dumps(plan)
        self.assertNotIn("Mama Tried", blob)
        self.assertEqual(plan["source_mix"]["texture"]["title"], "Rain room tone")
        bed = next(ly for ly in plan["layers"] if ly["id"] == "bed")
        self.assertIn("texture", bed["role"])
        hops = [bed["events"][i + 1]["t"] - bed["events"][i]["t"] for i in range(len(bed["events"]) - 1)]
        self.assertTrue(hops)
        self.assertTrue(all(abs(hop - 8.0) > 0.4 for hop in hops))
        self.assertLessEqual(plan["master"]["buses"]["pad"]["lufs"], -32)

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

    def test_progress_reports_real_hits_and_a_finding_line(self):
        seen = []

        def cb(line, sound):
            seen.append((line, sound))

        pools = {"musicmatter": _chords("musicmatter", "choir")}
        plan = discover.plan_for(
            COMP, seed=1, searchers=_searchers(pools), verify_live=False, progress_cb=cb,
        )
        lines = [line for line, _sound in seen if line]
        self.assertTrue(any(line.startswith("Finding ") and "Musicmatter" in line for line in lines))
        sounds = [sound for _line, sound in seen if sound]
        self.assertTrue(sounds)
        self.assertTrue(all(sound.get("title") and sound.get("supplier") and sound.get("license") for sound in sounds))
        self.assertTrue(any("choir" in sound["title"].lower() for sound in sounds))
        self.assertIsNone(plan.get("trimmed"))

    def test_no_sounds_raises(self):
        with self.assertRaises(ValueError):
            discover.plan_for(COMP, seed=1, searchers=_searchers({}), verify_live=False)

    def test_freesound_hits_are_not_labeled_with_the_query(self):
        html = '<a href="/people/ada/sounds/12345/">x</a><a href="/people/ada/sounds/12345/">x</a>'
        orig = discover._get
        discover._get = lambda url, timeout=20: html.encode()
        previous = os.environ.pop("FREESOUND_API_KEY", None)
        try:
            hits = discover.search_freesound(["choir", "synth"], "choir", ["Cm"], 1, host_ok_fn=lambda _h: True)
        finally:
            discover._get = orig
            if previous is not None:
                os.environ["FREESOUND_API_KEY"] = previous
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["instrument"], "texture")
        self.assertNotIn("choir", hits[0]["title"])

    def test_freesound_api_is_used_when_the_key_is_set(self):
        calls = []

        def fake(url, token, timeout=20):
            calls.append((url, token))
            return {"results": [{
                "id": 99,
                "name": "Soft rain",
                "username": "Ada",
                "license": "Creative Commons 0",
                "url": "https://freesound.org/people/ada/sounds/99/",
            }]}

        orig = discover._json_auth
        discover._json_auth = fake
        os.environ["FREESOUND_API_KEY"] = "test-key"
        try:
            hits = discover.search_freesound(["rain"], None, ["Cm"], 1, host_ok_fn=lambda _h: True)
        finally:
            discover._json_auth = orig
            os.environ.pop("FREESOUND_API_KEY", None)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["title"], "Soft rain")
        self.assertEqual(hits[0]["author"], "Ada")
        self.assertEqual(hits[0]["license"], "CC0-1.0")
        self.assertEqual(calls[0][1], "test-key")
        self.assertNotIn("test-key", calls[0][0])

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

    def test_room_tone_is_skipped_unless_the_words_ask_for_space(self):
        from ncperf import deadline, take

        self.assertFalse(take.phrase("let it breathe: some bars linger").get("bed"))
        self.assertTrue(take.phrase("a little space and some room tone").get("bed"))
        calls = []

        def run(terms, *_rest):
            calls.append(list(terms))
            return []

        fns = _searchers({"musicmatter": _chords("musicmatter", "choir")})
        for origin in ("freesound", "openverse", "internet_archive", "wikimedia", "ccmixter"):
            fns[origin] = run
        lines = []
        plan = discover.plan_for(
            COMP, seed=5, searchers=fns, verify_live=False,
            progress_cb=lambda line, _sound: lines.append(line),
        )
        self.assertEqual(validate(plan), [])
        self.assertFalse(any("room tone" in " ".join(terms) for terms in calls))
        self.assertFalse(any(line and "room tone" in line for line in lines))
        self.assertTrue(any(line == "Checking recorded notes…" for line in lines))
        self.assertIsNone(plan["source_mix"]["texture"])

        def stall(terms, *_rest):
            calls.append(list(terms))
            if "room tone" in terms:
                while True:
                    deadline.check()
                    time.sleep(0.01)
            return []

        calls.clear()
        for origin in ("freesound", "openverse", "internet_archive", "wikimedia", "ccmixter"):
            fns[origin] = stall
        spaced = dict(COMP, performance="Clear and open, with room tone and a little space.")
        started = time.perf_counter()
        plan = discover.plan_for(
            spaced, seed=5, searchers=fns, verify_live=False, texture_budget_s=0.25,
        )
        elapsed = time.perf_counter() - started
        # Five origins each stall for 0.25 s. Together that is one budget, not five.
        self.assertLess(elapsed, 1.0)
        room = [terms for terms in calls if "room tone" in terms]
        self.assertEqual(len(room), 5)
        self.assertEqual(validate(plan), [])
        self.assertIsNone(plan["source_mix"]["texture"])

    def test_a_stalled_origin_is_skipped(self):
        from ncperf import deadline

        def stall(*_args):
            while True:
                deadline.check()
                time.sleep(0.01)

        fns = _searchers({"musicmatter": _chords("musicmatter", "choir")})
        fns["wikimedia"] = stall
        started = time.perf_counter()
        plan = discover.plan_for(
            COMP, seed=1, searchers=fns, verify_live=False, origin_budget_s=0.25,
        )
        self.assertLess(time.perf_counter() - started, 3)
        self.assertEqual(validate(plan), [])
        self.assertTrue(plan["layers"])

    def test_stalled_origins_run_together(self):
        from ncperf import deadline

        def stall(*_args):
            while True:
                deadline.check()
                time.sleep(0.01)

        fns = _searchers({"musicmatter": _chords("musicmatter", "choir")})
        for origin in ("freesound", "openverse", "internet_archive", "wikimedia", "ccmixter"):
            fns[origin] = stall
        started = time.perf_counter()
        plan = discover.plan_for(
            COMP, seed=1, searchers=fns, verify_live=False, origin_budget_s=0.4,
        )
        # Five stalled origins at 0.4 s would be 2 s one after another.
        self.assertLess(time.perf_counter() - started, 1.2)
        self.assertEqual(validate(plan), [])
        self.assertTrue(plan["layers"])


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
        self.assertTrue(plan["source_mix"]["arrangement"])
        self.assertLess(plan["source_mix"]["predictability"]["self_similarity"], 0.95)
        self.assertIn(plan["source_mix"]["predictability"]["mode"], ("free", "orderly"))
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
        played = [name for row in plan["source_mix"]["arrangement"] for name in row["members"]]
        self.assertTrue(played)
        self.assertTrue(all("pizz" in name or "guitar" in name or "synth" in name for name in played))
        self.assertTrue(any("pizz" in name or "guitar" in name for name in played))
        self.assertTrue(any("synth" in name for name in played))
        self.assertFalse(any("piano" in name for name in played))
        self.assertNotIn("piano", [row["instrument"] for row in rows])
        self.assertLess(plan["source_mix"]["predictability"]["self_similarity"], 0.95)
        partner = [ly for ly in plan["layers"] if ly["id"] == "partner"]
        self.assertEqual(len(partner), 1)
        self.assertTrue(any("fat synth" in (ev.get("role") or "") for ev in partner[0].get("events") or []))
        self.assertEqual(partner[0]["type"], "clips")

    def test_partial_pluck_stays_in_the_cast_and_is_named(self):
        """A plucked library that misses one chord is not replaced by a choir that happens to cover it."""
        symbols = ("Ebsus2", "Cm-add9", "Ab6-9", "Bb7sus4", "Eb6")
        pools = {
            "musicmatter": [
                _clip("musicmatter", f"{inst} {sym}", inst, f"{prefix}-{sym}", kind="supplier", chord=sym)
                for inst, prefix in (("choir", "c"), ("fat synth", "f"), ("piano", "p"))
                for sym in symbols
            ],
        }
        # These voicings are real catalog notes. Their union is not an Ab6-9 voicing.
        covered = {
            "Ebsus2": [58, 63, 65],
            "Cm-add9": [55, 60, 63, 74],
            "Bb7sus4": [56, 58, 63, 65],
            "Eb6": [58, 60, 63, 67],
        }
        notes = {}
        for midis in covered.values():
            for midi in midis:
                notes[str(midi)] = _note("uiowa", "guitar", midi)
        guitar = {
            "origin": "uiowa", "instrument": "guitar", "pluck": True,
            "page": "https://theremin.music.uiowa.edu/MIS.html", "notes": notes,
        }
        comp = dict(
            COMP,
            composition="1 min. Ebsus2, Cm-add9, Ab6/9, Bb7sus4, Eb6.",
            performance="Layer a plucked string with a fat synth. Vary the color on each chord.",
        )
        plan = discover.plan_for(
            comp, seed=13, searchers=_searchers(pools), verify_live=False, note_maps=[guitar],
        )
        self.assertEqual(validate(plan), [])
        ensemble = " ".join(plan["source_mix"]["ensemble"])
        self.assertIn("guitar", ensemble)
        self.assertIn("fat synth", ensemble)
        self.assertNotIn("choir", ensemble)
        self.assertNotIn("piano", ensemble)
        missed = next(row for row in plan["source_mix"]["arrangement"] if row["symbol"] == "Ab6-9")
        self.assertNotIn("guitar", " ".join(missed["members"]))
        self.assertTrue(missed["members"])
        self.assertFalse(any(row["rest"] for row in plan["source_mix"]["arrangement"]))
        self.assertTrue(any("did not cover" in note and "Ab6-9" in note for note in plan["notes"]))
        self.assertTrue(plan["source_mix"]["coverage"])

    def test_short_chord_files_are_retriggered_before_they_go_quiet(self):
        from ncperf import take
        free = take.chord_overlaps(0.0, 12.0, "s", "role", 3, 1, 1.0, 0.02, False)
        starts = [event["t"] for event in free]
        hops = [b - a for a, b in zip(starts, starts[1:])]
        self.assertGreater(len(hops), 4)
        self.assertTrue(all(0.9 <= hop < 2.0 for hop in hops))
        self.assertGreater(max(hops) - min(hops), 0.05)
        self.assertGreater(starts[-1] + 2.35, 11.5)
        held = take.chord_overlaps(0.0, 24.0, "s", "role", 5, 2, 1.0, 0.0, False, continuous=True)
        held_hops = [b - a for a, b in zip([e["t"] for e in held], [e["t"] for e in held][1:])]
        self.assertTrue(held_hops)
        self.assertTrue(all(hop <= 1.4 for hop in held_hops))
        self.assertTrue(not _sustain_gaps(held, 24.0, take.SUSTAIN_AUDIBLE) or max(_sustain_gaps(held, 24.0, take.SUSTAIN_AUDIBLE)) <= 0.15)
        orderly = take.chord_overlaps(0.0, 12.0, "s", "role", 3, 1, 1.0, 0.0, True)
        oh = [b - a for a, b in zip([e["t"] for e in orderly], [e["t"] for e in orderly][1:])]
        self.assertTrue(oh)
        self.assertTrue(all(abs(hop - oh[0]) < 1e-9 for hop in oh))

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

    def test_one_origin_of_notes_still_plays(self):
        maps = [_map("uiowa", "cello pizzicato", pluck=True)]
        plan = discover.plan_for(
            COMP, seed=3, searchers=_searchers({}), verify_live=False, note_maps=maps,
        )
        rows = plan["source_mix"]["per_chord"]
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row["built"] == "assembled notes" for row in rows))
        self.assertTrue(all(row["origin"] == "uiowa" for row in rows))
        self.assertEqual(validate(plan), [])

    def test_ring_words_overlap_and_a_gap_is_only_for_space(self):
        from ncperf import take
        pools = {"musicmatter": _chords("musicmatter", "choir")}
        ringing = dict(COMP, performance="Bright and open; let each chord ring into the next.")
        plan = discover.plan_for(ringing, seed=1, searchers=_searchers(pools), verify_live=False)
        dur = plan["chords"][0]["dur"]
        events = next(ly["events"] for ly in plan["layers"] if ly["id"] == "strikes")
        self.assertGreater(events[0]["dur_s"], 4.2)
        self.assertGreater(events[0]["dur_s"], dur)
        self.assertLess(events[0]["t"], 0.1)
        self.assertFalse(any(row.get("breath") for row in plan["source_mix"]["arrangement"]))
        self.assertGreater(take.duration_cv([chord["dur"] for chord in plan["chords"]]), 0.18)
        gaps = _sustain_gaps(events, plan["length_s"], take.SUSTAIN_AUDIBLE)
        self.assertTrue(not gaps or max(gaps) <= 0.15)
        self.assertFalse(any(ly["id"] == "bed" for ly in plan["layers"]))
        self.assertGreaterEqual(plan["master"]["reverb"]["send_lu"], 3.0)
        self.assertEqual(plan["source_mix"]["predictability"]["mode"], "free")
        self.assertLess(plan["source_mix"]["predictability"]["self_similarity"], 0.95)
        self.assertFalse(any(row["rest"] for row in plan["source_mix"]["arrangement"]))

    def test_repeat_keeps_one_arrangement_and_free_does_not(self):
        pools = {
            "musicmatter": _chords("musicmatter", "choir") + _chords("musicmatter", "piano", prefix="p"),
        }
        free = discover.plan_for(
            dict(COMP, performance="Bright and open; let each chord ring into the next."),
            seed=2, searchers=_searchers(pools), verify_live=False,
        )
        locked = discover.plan_for(
            dict(COMP, performance="Repeat the same sounds over and over."),
            seed=2, searchers=_searchers(pools), verify_live=False,
        )
        self.assertEqual(locked["source_mix"]["predictability"]["mode"], "orderly")
        self.assertGreaterEqual(locked["source_mix"]["predictability"]["self_similarity"], 0.99)
        self.assertEqual(locked["source_mix"]["predictability"]["source_variation"], 0.0)
        self.assertEqual(len({tuple(row["members"]) for row in locked["source_mix"]["arrangement"]}), 1)
        self.assertEqual(free["source_mix"]["predictability"]["mode"], "free")
        self.assertLess(
            free["source_mix"]["predictability"]["self_similarity"],
            locked["source_mix"]["predictability"]["self_similarity"],
        )
        self.assertTrue(free["source_mix"]["arrangement"])
        for row, chord in zip(free["source_mix"]["per_chord"], free["chords"]):
            self.assertEqual(row["symbol"], chord["symbol"])

    def test_choir_then_synth_is_two_halves_not_a_slideshow(self):
        comp = dict(COMP, performance="A choir in the distance, then a soft synth answers. Keep the piano out of the lead.")
        pools = {
            "musicmatter": _chords("musicmatter", "choir", prefix="c") + _chords("musicmatter", "fat synth", prefix="f")
            + _chords("musicmatter", "piano", prefix="p"),
        }
        plan = discover.plan_for(comp, seed=12, searchers=_searchers(pools), verify_live=False)
        rows = plan["source_mix"]["per_chord"]
        self.assertTrue(all(row["instrument"] == "choir" for row in rows[:2]))
        self.assertTrue(all("synth" in row["instrument"] for row in rows[2:]))
        self.assertNotIn("piano", [row["instrument"] for row in rows])
        self.assertGreaterEqual(plan["master"]["reverb"]["send_lu"], 4.5)
        self.assertTrue(any("Surprise:" in note for note in plan["notes"]))

    def test_bwv_table_is_all_35_bars(self):
        from ncperf import interpret, take
        with open(os.path.join(ROOT, "examples", "bwv846", "composition.md"), encoding="utf-8") as handle:
            text = handle.read()
        table = take.bar_chords(text)
        self.assertIsNotNone(table)
        self.assertEqual(len(table), 35)
        self.assertEqual(table[0][0], "C")
        self.assertEqual(table[11][0], "Dbdim7")
        self.assertEqual(table[34][0], "C")
        length = interpret._length(text)
        seq, note = take.expand_sequence(table, length)
        self.assertEqual(len(seq), 35)
        self.assertIsNone(note)
        self.assertLess(length / len(seq), 5.0)
        long, cut = take.expand_sequence([("C", None)] * 50, 120)
        self.assertEqual(len(long), 40)
        self.assertIn("40", cut or "")
        comp = interpret.read_composition(os.path.join(ROOT, "examples", "bwv846", "composition.md"))
        words = f"{comp.get('composition') or ''} {comp.get('performance') or ''}"
        self.assertTrue(take.stated_pulse(words))
        self.assertEqual(take.broaden_bars(words), 3)
        slots = take.chord_slots([(35, length)], 1, True, 3)
        durs = [slot["dur"] for slot in slots]
        self.assertEqual(len(durs), 35)
        self.assertLess(take.duration_cv(durs), 0.12)
        self.assertGreater(durs[-1], durs[0] + 0.4)
        self.assertTrue(all(abs(d - durs[0]) < 0.002 for d in durs[:-3]))
        self.assertAlmostEqual(sum(durs), length, delta=0.05)
        raw_scan = CH.find_full(text)
        self.assertEqual(len(raw_scan), 52)
        self.assertNotEqual(raw_scan[1][0], "Dm7")
        parsed = CH.find_full(comp["composition"])
        self.assertEqual(len(parsed), 35)
        self.assertEqual([sym for sym, _bass in parsed[:3]], ["C", "Dm7", "G7"])
        self.assertEqual(parsed[11][0], "Dbdim7")
        self.assertEqual(parsed[-1][0], "C")
        self.assertNotIn("Bach", comp["composition"])
        self.assertIsNone(take.written_durations(comp["composition"], parsed))
        self.assertIsNone(take.written_durations(text, table))
        stub = interpret.stub({"title": comp["title"], "composition": text, "performance": comp["performance"]})
        self.assertEqual(len(stub["chords"]), 35)
        self.assertEqual([chord["symbol"] for chord in stub["chords"][:3]], ["C", "Dm7", "G7"])
        self.assertEqual(stub["chords"][11]["symbol"], "Dbdim7")

    def test_written_lengths_are_exact_and_organic_is_only_the_fallback(self):
        from ncperf import take
        pools = {
            "musicmatter": [
                _clip("musicmatter", f"Choir {sym}", "choir", f"w-{sym}", kind="supplier", chord=sym)
                for sym in ("Cm", "F", "G", "Am")
            ],
        }
        searchers = _searchers(pools)
        clock = "4/4, 60 BPM. [2 bars Cm][1 bar F][4 beats G]. At bar 4, 120 BPM. [1 bar Am]."
        # Cm occupies bars 1–2 (8 s). F is bar 3 (4 s). The change is at bar 4, so G's bar and Am's bar are at 120.
        one = discover.plan_for(
            {"title": "Clock", "composer": "anonymous", "composition": clock, "performance": "Let each chord ring into the next."},
            seed=3, searchers=searchers, verify_live=False,
        )
        two = discover.plan_for(
            {"title": "Clock", "composer": "anonymous", "composition": clock, "performance": "Let each chord ring into the next."},
            seed=9, searchers=searchers, verify_live=False,
        )
        durs = [chord["dur"] for chord in one["chords"]]
        self.assertEqual([chord["symbol"] for chord in one["chords"]], ["Cm", "F", "G", "Am"])
        self.assertEqual(durs, [8.0, 4.0, 2.0, 2.0])
        self.assertEqual([chord["dur"] for chord in two["chords"]], durs)
        self.assertAlmostEqual(one["length_s"], 16.0, places=3)
        self.assertTrue(one["source_mix"]["predictability"]["written_clock"])
        self.assertTrue(all(row["strikes_at"] == [0.0] for row in one["source_mix"]["arrangement"]))
        self.assertFalse(any(row["breath"] for row in one["source_mix"]["arrangement"]))
        short = discover.plan_for(
            {
                "title": "Beat",
                "composer": "anonymous",
                "composition": "4/4, 60 BPM. Cm (1 bar), F for 1 beat.",
                "performance": "Let each chord ring into the next.",
            },
            seed=1, searchers=searchers, verify_live=False,
        )
        self.assertEqual([chord["dur"] for chord in short["chords"]], [4.0, 1.0])
        self.assertGreater(1.25, short["chords"][1]["dur"])
        paren = take.written_durations("3/4, quarter = 60. Cm (2 bars), F (3 beats).", [("Cm", None), ("F", None)])
        self.assertEqual(paren, [6.0, 3.0])
        eighths = take.written_durations("6/8, 60 BPM. [1 bar Cm][1 bar F]", [("Cm", None), ("F", None)])
        self.assertEqual(eighths, [3.0, 3.0])
        crossed = take.written_durations("4/4, 60 BPM. [2 bars Cm]. At bar 2, 120 BPM.", [("Cm", None)])
        self.assertEqual(crossed, [6.0])
        self.assertIsNone(take.written_durations("[1 bar Cm][1 bar F]", [("Cm", None), ("F", None)]))
        changed = take.written_durations(
            "4/4, 60 BPM. One chord per bar: Cm | F | G. At bar 3, 120 BPM.",
            [("Cm", None), ("F", None), ("G", None)],
        )
        self.assertEqual(changed, [4.0, 4.0, 2.0])

    def test_multicolumn_table_is_not_read_row_by_row(self):
        from ncperf import take
        text = """The home key is C major, and a G pedal sits under the last bar.
4/4, 60 BPM, 8 bars.

| Bar | Chord | Bar | Chord |
|---|---|---|---|
| 1 | Cm | 5 | Am |
| 2 | F | 6 | Dm |
| 3 | G | 7 | Cm |
| 4 | Am | 8 | F |
"""
        order = ["Cm", "F", "G", "Am", "Am", "Dm", "Cm", "F"]
        scanned = [sym for sym, _bass in CH.find_full(text)]
        self.assertNotEqual(scanned, order)
        self.assertEqual(scanned[0], "C")
        self.assertEqual([sym for sym, _bass in take.composition_chords(text)], order)
        pools = {"musicmatter": _chords("musicmatter", "choir")}
        plan = discover.plan_for(
            {"title": "Columns", "composer": "anonymous", "composition": text, "performance": "Let each chord ring."},
            seed=1, searchers=_searchers(pools), verify_live=False,
        )
        self.assertEqual([chord["symbol"] for chord in plan["chords"]], order)

    def test_durations_vary_unless_the_words_lock_the_pulse(self):
        from ncperf import take
        from ncperf.record import with_chord_span
        pools = {"musicmatter": _chords("musicmatter", "choir")}
        free = discover.plan_for(
            dict(COMP, performance="Bright and open; let each chord ring into the next."),
            seed=11, searchers=_searchers(pools), verify_live=False,
        )
        other = discover.plan_for(
            dict(COMP, performance="Bright and open; let each chord ring into the next."),
            seed=21, searchers=_searchers(pools), verify_live=False,
        )
        steady = discover.plan_for(
            dict(COMP, performance="A steady pulse, the same tempo all the way."),
            seed=11, searchers=_searchers(pools), verify_live=False,
        )
        locked = discover.plan_for(
            dict(COMP, performance="Repeat the same sounds over and over."),
            seed=4, searchers=_searchers(pools), verify_live=False,
        )
        per_bar = discover.plan_for(
            dict(COMP, composition="1 min 20 s. Cm, F, G, Am. 10 seconds per chord.", performance="Let each chord ring into the next."),
            seed=1, searchers=_searchers(pools), verify_live=False,
        )

        def durs(plan):
            return [chord["dur"] for chord in plan["chords"]]

        free_durs = durs(free)
        self.assertGreater(take.duration_cv(free_durs), 0.22)
        self.assertFalse(take.looks_alternating(free_durs))
        self.assertNotEqual([round(d, 2) for d in free_durs], [round(d, 2) for d in durs(other)])
        self.assertEqual([c["symbol"] for c in free["chords"]], list(CHORDS))
        self.assertAlmostEqual(sum(free_durs), free["length_s"], delta=0.05)
        self.assertGreater(min(free_durs), 1.2)
        self.assertFalse(free["source_mix"]["predictability"]["pulse"])
        self.assertGreater(free["source_mix"]["predictability"]["duration_cv"], 0.22)
        offsets = [row["strikes_at"][0] for row in free["source_mix"]["arrangement"]]
        self.assertGreater(len(set(offsets)), 1)
        self.assertFalse(all(abs(off) < 0.001 for off in offsets))
        for row, chord in zip(free["source_mix"]["arrangement"], free["chords"]):
            self.assertEqual(row["start_s"], chord["t"])
            self.assertAlmostEqual(row["end_s"], chord["t"] + chord["dur"], places=3)
        self.assertLess(take.duration_cv(durs(steady)), 0.05)
        self.assertTrue(steady["source_mix"]["predictability"]["pulse"])
        self.assertGreater(take.duration_cv(durs(locked)), 0.22)
        self.assertGreaterEqual(locked["source_mix"]["predictability"]["self_similarity"], 0.99)
        self.assertEqual(locked["source_mix"]["predictability"]["source_variation"], 0.0)
        self.assertEqual(len(per_bar["chords"]), 8)
        self.assertTrue(all(abs(chord["dur"] - 10.0) < 0.05 for chord in per_bar["chords"]))
        self.assertLess(per_bar["source_mix"]["predictability"]["duration_cv"], 0.05)
        span = with_chord_span({"at_s": 1.1, "len_s": 2.0, "chord_index": 1}, free["chords"])
        self.assertEqual(span["chord_start_s"], free["chords"][1]["t"])
        self.assertAlmostEqual(span["chord_end_s"], free["chords"][1]["t"] + free["chords"][1]["dur"], places=3)
        self.assertEqual(span["at_s"], 1.1)

    def test_second_half_keeps_its_boundary_and_varies_inside_it(self):
        from ncperf import take
        symbols = ("Cm", "Cm7", "Cm9", "Am", "F")
        pools = {
            "musicmatter": [
                _clip("musicmatter", f"Choir {sym}", "choir", f"c-{sym}", kind="supplier", chord=sym)
                for sym in symbols
            ],
        }
        comp = {
            "title": "Ambient",
            "composer": "anonymous",
            "composition": "2 mins - half Cm, Cm7, Cm9. Second half Cm7, Am, F.",
            "performance": "ambient vibe, no harsh tones. Don't rush it.",
        }
        plan = discover.plan_for(comp, seed=1, searchers=_searchers(pools), verify_live=False)
        chords = plan["chords"]
        self.assertEqual(len(chords), 12)
        self.assertAlmostEqual(plan["length_s"], 120.0, delta=0.1)
        boundary = next(chord["t"] for chord in chords if chord["section"] == "B")
        self.assertAlmostEqual(boundary, 60.0, delta=0.05)
        left = [chord for chord in chords if chord["section"] == "A"]
        right = [chord for chord in chords if chord["section"] == "B"]
        self.assertTrue(all(chord["symbol"] in {"Cm", "Cm7", "Cm9"} for chord in left))
        self.assertTrue(all(chord["symbol"] in {"Cm7", "Am", "F"} for chord in right))
        self.assertEqual([chord["symbol"] for chord in left], ["Cm", "Cm7", "Cm9", "Cm", "Cm7", "Cm9"])
        self.assertEqual([chord["symbol"] for chord in right], ["Cm7", "Am", "F", "Cm7", "Am", "F"])
        self.assertGreater(take.duration_cv([chord["dur"] for chord in left]), 0.18)
        self.assertGreater(take.duration_cv([chord["dur"] for chord in right]), 0.18)
        self.assertAlmostEqual(sum(chord["dur"] for chord in left), 60.0, delta=0.05)
        self.assertAlmostEqual(sum(chord["dur"] for chord in chords), 120.0, delta=0.05)
        self.assertEqual(plan["sections"][0]["end_s"], plan["sections"][1]["start_s"])
        self.assertAlmostEqual(plan["sections"][1]["start_s"], 60.0, delta=0.05)


def _sustain_gaps(events, end: float, audible: float) -> list[float]:
    """Silence inside [0, end] if each copy only sounds for `audible` seconds."""
    spans = sorted((float(event["t"]), float(event["t"]) + audible) for event in events)
    cursor = 0.0
    gaps = []
    for start, stop in spans:
        if start > cursor + 1e-6:
            gaps.append(start - cursor)
        cursor = max(cursor, stop)
    if end > cursor + 1e-6:
        gaps.append(end - cursor)
    return gaps


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
        """A two-minute progression is not cut at 12 chords, and still shares one layer."""
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
        self.assertEqual(len(plan["chords"]), 15)
        self.assertEqual(Settings().max_layers, 16)
        self.assertLessEqual(len(plan["layers"]), 16)
        notes = [ly for ly in plan["layers"] if ly["type"] == "chord_notes"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(plan["chords"]), 15)
        self.assertEqual(len(notes[0]["per_chord"]), 15)
        self.assertFalse(any(row["rest"] for row in plan["source_mix"]["arrangement"]))
        self.assertGreater(notes[0]["note_s"], plan["chords"][0]["dur"])
        for pc in notes[0]["per_chord"]:
            self.assertIn(pc["bank"], plan["banks"])
            self.assertTrue(plan["banks"][pc["bank"]]["notes"])
        rows = plan["source_mix"]["per_chord"]
        self.assertTrue(all(row["built"] == "assembled notes" for row in rows))
        self.assertLessEqual(len(plan["sources"]), 24)
        self.assertEqual(plan["source_mix"]["predictability"]["mode"], "free")
        self.assertGreater(plan["source_mix"]["predictability"]["duration_cv"], 0.18)
        leads = [set(ly["lead_by_chord"].values()) for ly in plan["layers"] if ly.get("lead_by_chord")]
        self.assertTrue(leads)
        self.assertTrue(any(len(vals) > 1 for vals in leads))
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
        self.assertIn("sources", plan.get("trimmed") or "")
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
