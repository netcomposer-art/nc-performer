"""The note index is read at Perform time. The libraries are not measured again."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "service"))

from ncperf import chords as CH  # noqa: E402
from ncperf import creditcache, discover, fetch, gather, noteindex, notemap, pitch  # noqa: E402
from ncperf.render import Renderer  # noqa: E402
from ncperf.util import SR  # noqa: E402


def _index(notes: dict) -> dict:
    return {
        "v": 1,
        "built": "2026-10-20T00:00:00Z",
        "pitches": [43, 82],
        "libraries": [{
            "id": "uiowa-piano",
            "instrument": "piano",
            "origin": "uiowa",
            "kind": "uiowa_mis",
            "pluck": False,
            "page": "https://theremin.music.uiowa.edu/MISpiano.html",
            "author": "University of Iowa Electronic Music Studios (Lawrence Fritts, director)",
            "license": "PD-equivalent",
            "note": "",
            "notes": notes,
        }],
        "skipped": [{"library": "Philharmonia Orchestra", "used": False, "reason": "custom licence"}],
    }


def _note(midi: int) -> dict:
    return {
        "kind": "uiowa_mis",
        "url": f"https://theremin.music.uiowa.edu/sound%20files/MIS/Piano_Other/piano/Piano.mf.{midi}.aiff",
        "audio_url": f"https://theremin.music.uiowa.edu/sound%20files/MIS/Piano_Other/piano/Piano.mf.{midi}.aiff",
        "cents": 4.0,
        "onset_s": 0.01,
        "length_s": 2.0,
        "title": f"piano {midi}",
        "author": "University of Iowa Electronic Music Studios (Lawrence Fritts, director)",
        "license": "PD-equivalent",
        "page": "https://theremin.music.uiowa.edu/MISpiano.html",
        "expect_midi": midi,
        "midi": midi,
        "pitched": True,
        "credit_checked": "2026-10-20T00:00:00Z",
    }


class NoteIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nc-index-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "note-index.json")
        self.prev_index = os.environ.get("NC_NOTE_INDEX")
        self.prev_cache = os.environ.get("NC_CACHE")
        os.environ["NC_NOTE_INDEX"] = self.path
        os.environ["NC_CACHE"] = os.path.join(self.tmp, "cache")
        noteindex.reset()
        creditcache.reset()
        self.addCleanup(self._restore)

    def _restore(self):
        noteindex.reset()
        creditcache.reset()
        if self.prev_index is None:
            os.environ.pop("NC_NOTE_INDEX", None)
        else:
            os.environ["NC_NOTE_INDEX"] = self.prev_index
        if self.prev_cache is None:
            os.environ.pop("NC_CACHE", None)
        else:
            os.environ["NC_CACHE"] = self.prev_cache

    def _write(self, doc: dict) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        noteindex.reset()

    def test_ensure_reads_the_index_and_does_not_measure(self):
        voiced = [int(n) for n in CH.voicings("C")[0][1]]
        self._write(_index({str(m): _note(m) for m in voiced}))
        calls = []
        real_fill = notemap._fill_uiowa
        real_measure = pitch.measure
        real_download = fetch.download
        notemap._fill_uiowa = lambda *a, **k: calls.append("fill")
        pitch.measure = lambda *a, **k: calls.append("measure")
        fetch.download = lambda *a, **k: calls.append("download")
        try:
            maps, status = notemap.ensure(["C"])
            plan = discover.plan_for(
                {"title": "C", "composition": "1 min. C.", "performance": "piano"},
                seed=3,
                instrument="piano",
                verify_live=True,
                searchers={"freesound": lambda *a, **k: []},
            )
        finally:
            notemap._fill_uiowa = real_fill
            pitch.measure = real_measure
            fetch.download = real_download
        self.assertEqual(calls, [])
        self.assertEqual(maps[0]["id"], "uiowa-piano")
        self.assertTrue(maps[0]["notes"][str(voiced[0])]["pitch_checked"])
        self.assertEqual(status["source"], "note-index")
        self.assertEqual(plan["sources"][next(iter(plan["sources"]))]["pitch_checked"], True)
        self.assertIn("onset_s", next(iter(plan["sources"].values())))
        self.assertTrue(any(ly["type"] == "chord_notes" for ly in plan["layers"]))

    def test_a_missing_index_does_not_measure_either(self):
        real_load = noteindex.load
        noteindex.load = lambda: None
        calls = []
        real_fill = notemap._fill_uiowa
        notemap._fill_uiowa = lambda *a, **k: calls.append("fill")
        try:
            maps, status = notemap.ensure(["C"])
        finally:
            noteindex.load = real_load
            notemap._fill_uiowa = real_fill
        self.assertEqual(maps, [])
        self.assertIn("not installed", status["error"])
        self.assertEqual(calls, [])

    def test_manifest_credit_is_not_fetched_twice(self):
        calls = []
        real = discover._json

        def fake(url):
            calls.append(url)
            return {
                "license": "CC0-1.0",
                "title": "Piano Cm",
                "creator": {"name": "Ada"},
                "audio": "https://example.test/cm.flac",
                "sha256": "abc",
            }

        discover._json = fake
        try:
            first = discover._manifest_credit("https://example.test/cm.nc.json")
            second = discover._manifest_credit("https://example.test/cm.nc.json")
        finally:
            discover._json = real
        self.assertEqual(calls, ["https://example.test/cm.nc.json"])
        self.assertEqual(first["author"], "Ada")
        self.assertEqual(second["audio_url"], "https://example.test/cm.flac")
        self.assertEqual(second["sha256"], "abc")

    def test_gather_does_not_download_the_kept_files(self):
        calls = []
        real = fetch.resolve
        fetch.resolve = lambda *a, **k: calls.append("resolve")
        try:
            built = gather.build(
                {"title": "C", "composition": "1 min. C.", "performance": "soft"},
                seed=1,
                verify_live=False,
                note_maps=[],
                searchers={"wikimedia": lambda *a, **k: []},
            )
        except ValueError:
            built = None
        finally:
            fetch.resolve = real
        self.assertEqual(calls, [])
        if built:
            self.assertFalse(any(row.get("data") for row in built["files"]))

    def test_preview_fetches_one_file(self):
        from nc_perform.storage import FileStorage
        storage = FileStorage(os.path.join(self.tmp, "files"))
        wav = os.path.join(self.tmp, "one.wav")
        with open(wav, "wb") as fh:
            fh.write(b"RIFF-one")
        plan = {"sources": {"n0": {
            "kind": "uiowa_mis",
            "url": "https://theremin.music.uiowa.edu/sound%20files/MIS/Piano_Other/piano/n.aiff",
            "title": "piano C4",
            "author": "Ada",
            "license": "PD-equivalent",
            "page": "https://theremin.music.uiowa.edu/MISpiano.html",
        }}}
        gather.save_collection(storage, "g-0123456789ab", {
            "id": "g-0123456789ab",
            "seed": 1,
            "claim_hash": "abc",
            "plan": plan,
            "sounds": [],
        }, gather.source_rows(plan))
        self.assertIsNone(storage.get("gathers/g-0123456789ab/audio/n0"))
        calls = []
        real = fetch.resolve

        def fake(src):
            calls.append(src.get("url"))
            return {"path": wav, "audio_url": src["url"]}

        fetch.resolve = fake
        try:
            first = gather.read_audio(storage, "g-0123456789ab", "n0")
            second = gather.read_audio(storage, "g-0123456789ab", "n0")
        finally:
            fetch.resolve = real
        self.assertEqual(first[0], b"RIFF-one")
        self.assertEqual(second[0], b"RIFF-one")
        self.assertEqual(calls, [plan["sources"]["n0"]["url"]])


class RenderIndexTest(unittest.TestCase):
    def test_a_stored_slice_is_not_segmented_or_measured(self):
        import numpy as np
        import soundfile as sf
        tmp = tempfile.mkdtemp(prefix="nc-slice-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "n.wav")
        y = np.zeros((SR, 2))
        y[int(0.2 * SR):int(0.5 * SR)] = 0.2
        sf.write(path, y, SR)
        plan = {
            "seed": 1,
            "length_s": 1.0,
            "tail_s": 0.0,
            "sources": {"n0": {
                "kind": "uiowa_mis",
                "url": "https://theremin.music.uiowa.edu/sound%20files/MIS/n.aiff",
                "audio_url": "https://theremin.music.uiowa.edu/sound%20files/MIS/n.aiff",
                "pitch_checked": True,
                "expect_midi": 60,
                "cents": 3.0,
                "onset_s": 0.0,
                "segment": {"index": 1, "t0": 0.2, "t1": 0.5, "drop_db": 40},
                "title": "piano C4",
                "author": "Ada",
                "license": "PD-equivalent",
                "page": "https://theremin.music.uiowa.edu/MISpiano.html",
            }},
            "banks": {},
            "layers": [],
        }

        def fake_resolve(_src):
            return {"path": path, "license": {"name": "PD"}, "author": "Ada", "title": "piano C4"}

        def boom(*_a, **_k):
            raise AssertionError("pitch was measured at render time")

        real_resolve = fetch.resolve
        real_measure = pitch.measure
        real_fft = pitch.measure_fft
        real_seg = pitch.segment_run
        fetch.resolve = fake_resolve
        pitch.measure = boom
        pitch.measure_fft = boom
        pitch.segment_run = boom
        try:
            renderer = Renderer(plan, tmp, log=lambda *_a: None)
            got = renderer.measured("n0", 60)
            audio = renderer.source("n0")["y"]
        finally:
            fetch.resolve = real_resolve
            pitch.measure = real_measure
            pitch.measure_fft = real_fft
            pitch.segment_run = real_seg
        self.assertTrue(got["accepted"])
        self.assertEqual(got["checked"], "index")
        self.assertEqual(got["cents"], 3.0)
        self.assertEqual(len(audio), int(0.3 * SR))


if __name__ == "__main__":
    unittest.main()
