"""Even-handed source discovery, with fake searchers and no network."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from ncperf import discover
from ncperf.interpret import from_plan
from ncperf.schema import validate


COMP = {
    "title": "Glass garden",
    "composer": "anonymous",
    "composition": "1 min. Cm, F, G, Am, Dm.",
    "performance": "Clear and open, like light through glass.",
}


def _clip(origin, title, instrument, key, fit=0, kind="commons"):
    if kind == "supplier":
        source = {"kind": "supplier", "manifest": f"https://musicmatter.art/{key}.nc.json", "pitched": False}
    elif kind == "freesound":
        source = {"kind": "freesound", "id": "".join(ch for ch in key if ch.isdigit()) or "325647", "pitched": False}
    else:
        source = {
            "kind": "commons",
            "url": f"https://upload.wikimedia.org/{key}.ogg",
            "page": f"https://commons.wikimedia.org/wiki/{key}",
            "license": "CC0-1.0",
            "title": title,
            "author": "Ada",
            "source": origin,
            "pitched": False,
        }
    return {
        "origin": origin,
        "title": title,
        "author": "Ada",
        "license": "CC0-1.0",
        "instrument": instrument,
        "fit": fit,
        "source": source,
        "key": key,
    }


def _searchers(pools):
    def make(origin):
        rows = pools.get(origin) or []

        def run(*_args):
            return list(rows)

        return run

    return {origin: make(origin) for origin in discover.ORIGINS}


class DiscoverTest(unittest.TestCase):
    def test_several_origins_and_a_musicmatter_cap(self):
        pools = {
            "musicmatter": [_clip("musicmatter", f"Piano {i}", "piano", f"mm{i}", kind="supplier") for i in range(8)],
            "freesound": [_clip("freesound", f"Air {i}", "texture", f"fs{i}", kind="freesound") for i in range(2)],
            "wikimedia": [_clip("wikimedia", "Organ", "organ", "wm0")],
            "ccmixter": [_clip("ccmixter", "Harp", "harp", "cc0")],
        }
        plan = discover.plan_for(COMP, seed=1, searchers=_searchers(pools), verify_live=False)
        self.assertEqual(validate(plan), [])
        self.assertEqual(plan["interpreter"]["kind"], "discover")
        self.assertNotIn("stretch_to_s", json.dumps(plan))
        mix = plan["source_mix"]
        self.assertGreaterEqual(len(mix["by_origin"]), 2)
        self.assertEqual(mix["queried"], list(discover.ORIGINS[1:]) + [discover.ORIGINS[0]])
        total = sum(mix["by_origin"].values())
        self.assertLessEqual(mix["by_origin"].get("musicmatter", 0) * 2, total)
        self.assertIn("source_mix", plan)
        self.assertTrue(plan["notes"][0].startswith("Source mix:"))

    def test_a_musicmatter_heavy_pool_does_not_take_the_plan(self):
        pools = {
            "musicmatter": [_clip("musicmatter", f"Piano {i}", "piano", f"mm{i}", kind="supplier") for i in range(8)],
            "freesound": [_clip("freesound", f"Air {i}", "texture", f"fs{i}", kind="freesound") for i in range(2)],
        }
        plan = discover.plan_for(COMP, seed=1, searchers=_searchers(pools), verify_live=False)
        mix = plan["source_mix"]["by_origin"]
        total = sum(mix.values())
        self.assertIn("freesound", mix)
        self.assertLessEqual(mix.get("musicmatter", 0) * 2, total)

    def test_only_one_origin_may_fill_the_plan(self):
        pools = {"musicmatter": [_clip("musicmatter", f"Piano {i}", "piano", f"mm{i}", kind="supplier") for i in range(8)]}
        plan = discover.plan_for(COMP, seed=3, searchers=_searchers(pools), verify_live=False)
        self.assertEqual(set(plan["source_mix"]["by_origin"]), {"musicmatter"})
        self.assertGreaterEqual(plan["source_mix"]["by_origin"]["musicmatter"], 5)

    def test_named_origin_relaxes_the_cap(self):
        comp = dict(COMP, performance="Use only freesound, nothing else.")
        pools = {
            "musicmatter": [_clip("musicmatter", f"Piano {i}", "piano", f"mm{i}", kind="supplier") for i in range(8)],
            "freesound": [_clip("freesound", f"Air {i}", "texture", f"fs{i}", kind="freesound") for i in range(6)],
        }
        plan = discover.plan_for(comp, seed=2, searchers=_searchers(pools), verify_live=False)
        self.assertEqual(set(plan["source_mix"]["by_origin"]), {"freesound"})

    def test_instrument_hint_is_a_timbre_not_a_supplier(self):
        pools = {
            "musicmatter": [
                _clip("musicmatter", "Piano", "piano", "p", fit=0, kind="supplier"),
                _clip("musicmatter", "Choir", "choir", "mc", fit=1, kind="supplier"),
            ],
            "wikimedia": [_clip("wikimedia", "Distant choir", "choir", "wc", fit=9)],
            "freesound": [_clip("freesound", "Noise", "texture", "fs0", kind="freesound")],
        }
        plan = discover.plan_for(COMP, seed=4, instrument="choir", searchers=_searchers(pools), verify_live=False)
        lead = plan["layers"][0]["events"][0]["role"]
        self.assertTrue(lead.startswith("choir (wikimedia)"))
        self.assertEqual(plan["source_mix"]["instrument_hint"], "choir")
        self.assertIn("wikimedia", plan["source_mix"]["by_origin"])
        self.assertIn("musicmatter", plan["source_mix"]["queried"])

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
        terms = discover.terms_for(
            {"composition": "Cm", "performance": "A choir and a synth. Keep the piano out of the lead."},
            None, 1,
        )
        self.assertNotIn("piano", terms)
        self.assertIn("choir", terms)
        self.assertIn("synth", terms)
        self.assertFalse(discover._mentions("harp arpeggio", "synth"))

    def test_discover_kind_survives_loading_a_plan(self):
        plan = discover.plan_for(
            COMP, seed=1,
            searchers=_searchers({"uiowa": [_clip("uiowa", "UIowa harp", "harp", "u0")]}),
            verify_live=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.json")
            json.dump(plan, open(path, "w", encoding="utf-8"))
            loaded = from_plan(path)
        self.assertEqual(loaded["interpreter"]["kind"], "discover")


if __name__ == "__main__":
    unittest.main()
