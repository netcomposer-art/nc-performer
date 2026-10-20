"""Raw assembly: one clip per chord, then a trim and a fade. No processing."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(__file__))
from test_discover import COMP, _chords, _searchers  # noqa: E402
from test_service import _api, _close, _settings  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "service"))

from ncperf import assemble, fetch, gather, record, take  # noqa: E402
from ncperf.schema import validate  # noqa: E402
from nc_perform.config import Settings  # noqa: E402
from nc_perform.limits import source_cap  # noqa: E402


def _pools():
    return {
        "musicmatter": (
            _chords("musicmatter", "choir", prefix="choir")
            + _chords("musicmatter", "piano", prefix="piano")
            + _chords("musicmatter", "fat synth", prefix="fat")
        ),
    }


def _plan(seed=1, **kwargs):
    comp = kwargs.pop("comp", COMP)
    return assemble.plan_raw(
        comp, seed=seed, searchers=_searchers(_pools()), verify_live=False, note_maps=[], **kwargs,
    )


class RawPlanTest(unittest.TestCase):
    def test_one_clip_per_chord_and_no_processing(self):
        plan = _plan(1)
        self.assertEqual(plan["assembly"], "raw")
        self.assertEqual(validate(plan), [])
        self.assertEqual(len(plan["clips"]), len(plan["chords"]))
        self.assertEqual(len(plan["layers"]), 1)
        self.assertTrue(all(clip["built"] == "whole-chord file" for clip in plan["clips"]))
        self.assertTrue(all(clip.get("credit") and clip.get("license") for clip in plan["clips"]))
        self.assertNotIn("reverb", plan["master"])
        self.assertEqual(plan["tail_s"], 0)
        self.assertNotIn("stretch_to_s", json.dumps(plan))
        durs = [chord["dur"] for chord in plan["chords"]]
        self.assertGreater(take.duration_cv(durs), 0.22)
        self.assertFalse(take.looks_alternating(durs))

    def test_the_words_can_ask_for_a_choir(self):
        comp = dict(COMP, performance="A choir, clear and unhurried.")
        plan = _plan(1, comp=comp)
        self.assertEqual(plan["ensemble"]["family"], "choir")
        self.assertTrue(all(clip["family"] == "choir" for clip in plan["clips"]))

    def test_a_piano_hint_does_not_choose_the_ensemble(self):
        plain = _plan(4)
        hinted = _plan(4, instrument="Piano")
        self.assertEqual(plain["ensemble"], hinted["ensemble"])
        families = []
        for seed in range(1, 11):
            families.append(_plan(seed)["ensemble"]["family"])
        self.assertGreater(len(set(families)), 1)
        self.assertLess(families.count("piano"), 10)

    def test_gather_again_picks_something_else(self):
        first = _plan(2)
        second = _plan(2, avoid_keys=first["gather_picks"])
        self.assertNotEqual(first["ensemble"]["family"], second["ensemble"]["family"])
        self.assertFalse(set(first["gather_picks"]) & set(second["gather_picks"]))

    def test_a_written_tempo_is_the_clock(self):
        comp = dict(COMP, composition="4/4. quarter = 60. one chord per bar. at bar 3, 90 BPM. Cm, F, G, Am, Dm.")
        plan = _plan(1, comp=comp)
        durs = [chord["dur"] for chord in plan["chords"]]
        self.assertEqual(len(durs), 5)
        self.assertAlmostEqual(durs[0], 4.0, places=2)
        self.assertAlmostEqual(durs[1], 4.0, places=2)
        self.assertLess(durs[2], 4.0)

    def test_gather_build_is_raw_unless_processing_is_asked_for(self):
        built = gather.build(
            COMP, seed=1, verify_live=False, note_maps=[], searchers=_searchers(_pools()),
        )
        self.assertEqual(built["plan"]["assembly"], "raw")
        self.assertEqual(len(built["plan"]["clips"]), len(built["plan"]["chords"]))
        sounds = gather.public_sounds(built["plan"], "g-0123456789ab", "http://127.0.0.1:9", built["files"])
        self.assertEqual(len(sounds), len(built["plan"]["chords"]))
        self.assertTrue(all(row.get("credit") and row.get("preview_url") for row in sounds))
        layered = gather.build(
            COMP, seed=1, verify_live=False, note_maps=[], searchers=_searchers(_pools()), processed=True,
        )
        self.assertNotEqual(layered["plan"].get("assembly"), "raw")
        self.assertIn("source_mix", layered["plan"])

    def test_source_cap_lets_a_prelude_keep_every_chord(self):
        settings = Settings()
        self.assertEqual(source_cap({}, settings), 24)
        self.assertEqual(source_cap({"assembly": "raw"}, settings), 40 * 8)
        self.assertGreater(source_cap({"assembly": "raw"}, settings), 35)


class RawRenderTest(unittest.TestCase):
    def test_a_short_clip_ends_and_only_the_fades_are_applied(self):
        tmp = tempfile.mkdtemp(prefix="nc-raw-")
        self.addCleanup(shutil.rmtree, tmp, True)
        quiet = os.path.join(tmp, "quiet.wav")
        loud = os.path.join(tmp, "loud.wav")
        _tone(quiet, 0.02)
        _tone(loud, 0.9)
        paths = {"c0": quiet, "c1": loud}

        def resolve(src):
            path = paths[src["_path"]]
            return {
                "path": path,
                "audio_url": "http://127.0.0.1/" + src["_path"] + ".wav",
                "what": "test clip",
                "source": "Local",
                "source_url": "http://127.0.0.1/page",
                "title": src["title"],
                "author": src["author"],
                "supplier": {"display_name": src["author"]},
                "license": {"id": "CC0-1.0", "name": "CC0 1.0", "uri": "https://creativecommons.org/publicdomain/zero/1.0/"},
                "sha256": "abc",
                "size_bytes": os.path.getsize(path),
            }

        real = fetch.resolve
        fetch.resolve = resolve
        try:
            plan = _render_plan()
            flac, mp3, log = assemble.render(plan, tmp, log=lambda *_a: None)
            y, sr = sf.read(flac, always_2d=True)
            self.assertEqual(sr, 48000)
            self.assertAlmostEqual(len(y) / sr, 6.0, delta=0.02)
            # The file is half a second. The rest of the three-second slot stays silent.
            gap = y[int(0.8 * sr):int(2.7 * sr)]
            self.assertLess(float(np.max(np.abs(gap))), 1e-4)
            self.assertLessEqual(float(np.max(np.abs(y))), 0.98 + 1e-6)
            self.assertEqual(log["quality"]["stretch"], [])
            self.assertGreater(float(np.max(np.abs(y[: int(0.3 * sr)]))), 1e-3)
            plan_path = os.path.join(tmp, "plan.json")
            with open(plan_path, "w", encoding="utf-8") as fh:
                json.dump(plan, fh, indent=1)
            rec_path = os.path.join(tmp, "record.json")
            rec = record.build(plan, plan_path, log, flac, mp3, rec_path)
            self.assertFalse(any(hack.get("tool") == "reverb" for hack in rec["hacks_chain"]))
            self.assertTrue(all(sound.get("spans") for sound in rec["sounds_used"]))
            self.assertTrue(all(span.get("chord_start_s") is not None for sound in rec["sounds_used"] for span in sound["spans"]))
            ok, lines = record.verify(rec_path, audio_dir=tmp)
            self.assertTrue(ok, lines)
        finally:
            fetch.resolve = real


class RawApiTest(unittest.TestCase):
    def test_processed_endpoint_is_off(self):
        tmp = tempfile.mkdtemp(prefix="nc-raw-api-")
        self.addCleanup(shutil.rmtree, tmp, True)
        settings = _settings(tmp, suppliers="http://127.0.0.1/suppliers.json", llm_base="", llm_model="")
        settings.processed = False
        app, httpd = _api(settings)
        self.addCleanup(_close, httpd)
        origin = f"http://127.0.0.1:{httpd.server_address[1]}"
        req = urllib.request.Request(
            origin + "/api/perform-processed",
            json.dumps({"composition": "1 min. Cm.", "performance": "soft", "title": "Off"}).encode(),
            {"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected 404")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)


def _tone(path, amp, seconds=0.5, freq=220.0):
    n = int(seconds * 48000)
    t = np.linspace(0, seconds, n, endpoint=False)
    y = amp * np.sin(2 * np.pi * freq * t)
    sf.write(path, np.column_stack([y, y]), 48000)


def _render_plan():
    def source(sid, title):
        return {
            "kind": "commons",
            "url": "http://127.0.0.1/" + sid + ".wav",
            "page": "http://127.0.0.1/page",
            "license": "CC0-1.0",
            "title": title,
            "author": "Ada",
            "source": "local",
            "_path": sid,
        }

    return {
        "nc_plan_version": "0.1",
        "assembly": "raw",
        "title": "Two chords",
        "composition": {
            "title": "Two chords",
            "composer": "Ada",
            "words_verbatim": {"composition": "Cm, F.", "performance": "plain"},
        },
        "interpreter": {"kind": "discover", "note": "raw assembly"},
        "seed": 1,
        "length_s": 6,
        "tail_s": 0,
        "chords": [
            {"t": 0, "dur": 3, "symbol": "Cm", "voicing": [48, 55, 60]},
            {"t": 3, "dur": 3, "symbol": "F", "voicing": [53, 57, 60]},
        ],
        "sources": {"c0": source("c0", "Quiet Cm"), "c1": source("c1", "Loud F")},
        "banks": {},
        "layers": [{
            "id": "chords",
            "type": "clips",
            "events": [
                {"t": 0, "source": "c0", "dur_s": 3, "chord_index": 0},
                {"t": 3, "source": "c1", "dur_s": 3, "chord_index": 1},
            ],
        }],
        "master": {"buses": {"dry": {}}},
        "clips": [
            {"id": "c0", "chord": 0, "sids": ["c0"], "dur": 3, "t": 0, "title": "Quiet Cm"},
            {"id": "c1", "chord": 1, "sids": ["c1"], "dur": 3, "t": 3, "title": "Loud F"},
        ],
    }
