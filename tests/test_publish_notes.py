"""The note publisher: which files go to R2, and the read-back check.

Run: python3 tests/test_publish_notes.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ncperf.publish_notes import (  # noqa: E402
    BUCKET,
    check_remote,
    get_argv,
    library_ids,
    parse_instruments,
    plan_uploads,
    publish,
    put_argv,
    redact,
)
from ncperf.slicer import build_note_manifest, note_name  # noqa: E402


def _spec(**over):
    spec = {
        "id": "uiowa-guitar",
        "instrument": "guitar",
        "pluck": True,
        "license": "PD-equivalent",
        "page": "https://theremin.music.uiowa.edu/MISguitar.html",
        "author": "University of Iowa Electronic Music Studios (Lawrence Fritts, director)",
        "lo": 40,
        "hi": 88,
        "velocity": ["mf"],
    }
    spec.update(over)
    return spec


def _note(midi, slug):
    return {
        "midi": midi,
        "name": note_name(midi),
        "file": f"notes/{slug}/{midi}.opus",
        "master": f"notes/{slug}/{midi}.flac",
        "cents": 2.0,
        "cents_method": "fft",
        "dur_s": 1.5,
        "lufs": -32.0,
        "peak_db": -6.0,
        "sr": 44100,
        "channels": 2,
        "source": {
            "url": "https://theremin.music.uiowa.edu/sound%20files/MIS/Piano_Other/guitar/Guitar.mf.sulG.C4B4.stereo.aif",
            "t0": 1.0,
            "t1": 2.5,
            "license": "PD-equivalent",
        },
    }


def _tree(root, slug, midis):
    spec = _spec(instrument="cello pizzicato" if slug != "guitar" else "guitar", id="uiowa-" + slug)
    if slug == "cello-pizzicato":
        spec = _spec(id="uiowa-cello-pizz", instrument="cello pizzicato")
    notes = {midi: _note(midi, slug) for midi in midis}
    doc = build_note_manifest(spec, notes, [], [])
    os.makedirs(os.path.join(root, "manifests"), exist_ok=True)
    os.makedirs(os.path.join(root, "notes", slug), exist_ok=True)
    with open(os.path.join(root, "manifests", slug + ".json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    for midi in midis:
        for ext, payload in ((".opus", b"O" * (10 + midi)), (".flac", b"F" * (20 + midi))):
            with open(os.path.join(root, "notes", slug, f"{midi}{ext}"), "wb") as fh:
                fh.write(payload)
    with open(os.path.join(root, "gap-report.json"), "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    return doc


class PlanTest(unittest.TestCase):
    def test_default_names_map_to_the_iowa_libraries(self):
        names = parse_instruments("Guitar, piano, cello, guitar")
        self.assertEqual(names, ["guitar", "piano", "cello"])
        self.assertEqual(library_ids(names), ["uiowa-guitar", "uiowa-piano", "uiowa-cello-pizz"])

    def test_the_new_sampler_names_map_to_their_libraries(self):
        names = parse_instruments("bass, strings, vibes, kalimba")
        self.assertEqual(names, ["bass", "strings", "vibes", "kalimba"])
        self.assertEqual(library_ids(names), [
            "karoryfer-darkblack",
            "uiowa-cello-arco",
            "uiowa-vibraphone-dampen",
            "vcsl-kalimba",
        ])

    def test_an_unknown_instrument_is_refused(self):
        with self.assertRaises(SystemExit):
            parse_instruments("violin")
        with self.assertRaises(SystemExit):
            parse_instruments("  ")

    def test_only_opus_flac_and_the_manifest_are_uploaded(self):
        tmp = tempfile.mkdtemp(prefix="nc-pub-")
        self.addCleanup(shutil.rmtree, tmp, True)
        _tree(tmp, "guitar", [60, 64])
        os.makedirs(os.path.join(tmp, "renders"), exist_ok=True)
        with open(os.path.join(tmp, "renders", "check.wav"), "wb") as fh:
            fh.write(b"WAV")
        items = plan_uploads(tmp)
        self.assertEqual([item.key for item in items], [
            "notes/guitar/60.flac",
            "notes/guitar/60.opus",
            "notes/guitar/64.flac",
            "notes/guitar/64.opus",
            "notes/guitar/manifest.json",
        ])
        self.assertEqual(items[-1].cache_control, "public, max-age=300")
        self.assertEqual(items[1].content_type, "audio/ogg")
        self.assertEqual(items[1].cache_control, "public, max-age=31536000, immutable")
        self.assertTrue(all("gap-report" not in item.key and not item.key.endswith(".wav") for item in items))

    def test_a_file_the_manifest_does_not_name_is_refused(self):
        tmp = tempfile.mkdtemp(prefix="nc-pub-")
        self.addCleanup(shutil.rmtree, tmp, True)
        _tree(tmp, "guitar", [64])
        with open(os.path.join(tmp, "notes", "guitar", "99.opus"), "wb") as fh:
            fh.write(b"extra")
        with self.assertRaises(SystemExit):
            plan_uploads(tmp)

    def test_a_master_key_is_not_rewritten(self):
        tmp = tempfile.mkdtemp(prefix="nc-pub-")
        self.addCleanup(shutil.rmtree, tmp, True)
        _tree(tmp, "guitar", [64])
        path = os.path.join(tmp, "manifests", "guitar.json")
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["notes"]["64"]["master"] = "notes/guitar/60.flac"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        with self.assertRaises(SystemExit):
            plan_uploads(tmp)


class UploadTest(unittest.TestCase):
    def test_put_uses_remote_wrangler_and_does_not_carry_a_secret(self):
        item = plan_uploads(self._one())[0]
        argv = put_argv(["npx", "wrangler"], item)
        self.assertIn("--remote", argv)
        self.assertIn("--force", argv)
        self.assertIn(f"{BUCKET}/{item.key}", argv)
        self.assertNotIn("CLOUDFLARE_API_TOKEN", " ".join(argv))
        self.assertNotIn("secret", " ".join(argv).lower())
        os.environ["CLOUDFLARE_API_TOKEN"] = "super-secret-token-value"
        self.addCleanup(os.environ.pop, "CLOUDFLARE_API_TOKEN", None)
        self.assertNotIn(os.environ["CLOUDFLARE_API_TOKEN"], " ".join(argv))
        self.assertIn("[redacted]", redact("token super-secret-token-value seen"))

    def test_read_back_must_match_count_and_size(self):
        self.assertEqual(check_remote([("notes/guitar/64.opus", 10)], [("notes/guitar/64.opus", 10)]), [])
        mismatch = check_remote([("notes/guitar/64.opus", 10)], [("notes/guitar/64.opus", 4)])
        self.assertTrue(any("size:" in line for line in mismatch))
        missing = check_remote([("a", 1), ("b", 2)], [("a", 1)])
        self.assertTrue(any(line.startswith("count:") or line.startswith("missing:") for line in missing))

    def test_upload_writes_audio_before_the_manifest_and_a_second_run_replaces_the_same_keys(self):
        tmp = self._one()
        items = plan_uploads(tmp)
        calls = []

        def run(argv):
            kind = "put" if "put" in argv else "get"
            path = argv[argv.index(kind) + 1]
            _bucket, key = path.split("/", 1)
            if kind == "put":
                calls.append(("put", key))
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            dest = argv[argv.index("--file") + 1]
            local = next(item.local for item in items if item.key == key)
            shutil.copy(local, dest)
            calls.append(("get", key))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        work = tempfile.mkdtemp(prefix="nc-verify-")
        self.addCleanup(shutil.rmtree, work, True)
        lines = publish(items, ["npx", "wrangler"], run, work)
        puts = [key for kind, key in calls if kind == "put"]
        gets = [key for kind, key in calls if kind == "get"]
        self.assertEqual(puts[-1], "notes/guitar/manifest.json")
        self.assertLess(calls.index(("put", puts[-1])), calls.index(("get", gets[0])))
        self.assertEqual(puts, gets)
        self.assertTrue(any(line.startswith("verified ") for line in lines))
        again = []

        def run_again(argv):
            kind = "put" if "put" in argv else "get"
            path = argv[argv.index(kind) + 1]
            key = path.split("/", 1)[1]
            if kind == "put":
                again.append(key)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            dest = argv[argv.index("--file") + 1]
            local = next(item.local for item in items if item.key == key)
            shutil.copy(local, dest)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        publish(items, ["npx", "wrangler"], run_again, work)
        self.assertEqual(again, puts)
        fetched = get_argv(["npx", "wrangler"], "notes/guitar/64.opus", os.path.join(work, "out"))
        self.assertIn("--remote", fetched)
        self.assertIn(f"{BUCKET}/notes/guitar/64.opus", fetched)

    def test_a_short_read_back_fails_the_run(self):
        tmp = self._one()
        items = plan_uploads(tmp)

        def run(argv):
            if "get" in argv:
                dest = argv[argv.index("--file") + 1]
                with open(dest, "wb") as fh:
                    fh.write(b"x")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        work = tempfile.mkdtemp(prefix="nc-verify-")
        self.addCleanup(shutil.rmtree, work, True)
        with self.assertRaises(SystemExit) as caught:
            publish(items, ["npx", "wrangler"], run, work)
        self.assertIn("size:", str(caught.exception))

    def _one(self):
        tmp = tempfile.mkdtemp(prefix="nc-pub-")
        self.addCleanup(shutil.rmtree, tmp, True)
        _tree(tmp, "guitar", [64])
        return tmp


if __name__ == "__main__":
    unittest.main()
