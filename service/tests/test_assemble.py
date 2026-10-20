"""Raw assembly: one clip per chord, then a trim and a fade. No processing."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(__file__))
from test_discover import COMP, _chords, _clip, _searchers  # noqa: E402
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
    searchers = kwargs.pop("searchers", None) or _searchers(_pools())
    return assemble.plan_raw(
        comp, seed=seed, searchers=searchers, verify_live=False, note_maps=[], **kwargs,
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

    def test_each_chord_is_listed_as_soon_as_it_is_chosen(self):
        heard = []
        started = time.perf_counter()
        slow = {"done": False}

        def linger(*_args):
            time.sleep(3)
            slow["done"] = True
            return []

        searchers = _searchers(_pools())
        searchers["openverse"] = linger

        def cb(_line, sound=None):
            if sound:
                heard.append((time.perf_counter() - started, sound))

        plan = _plan(1, searchers=searchers, progress_cb=cb, settle_s=0.15, gather_deadline_s=3)
        elapsed = time.perf_counter() - started
        self.assertTrue(heard)
        self.assertLess(heard[0][0], 1.5)
        self.assertLess(elapsed, 2.0)
        self.assertFalse(slow["done"])
        chords = sorted({row["chord"] for _at, row in heard})
        self.assertEqual(chords, list(range(len(plan["chords"]))))
        for _at, row in heard:
            self.assertEqual(row["id"], f"c{row['chord']}")
            for key in ("title", "supplier", "license", "symbol"):
                self.assertTrue(row.get(key), row)

    def test_the_deadline_uses_the_disk_index_and_does_not_wait(self):
        from ncperf import discover

        tmp = tempfile.mkdtemp(prefix="nc-deadline-")
        self.addCleanup(shutil.rmtree, tmp, True)
        previous = os.environ.get("NC_CACHE")
        os.environ["NC_CACHE"] = tmp
        url = "https://example.test/suppliers-deadline.json"
        offerings = [
            {
                "audio": f"https://audio.musicmatter.art/fullchords/choir/{sym}.flac",
                "manifest": f"https://audio.musicmatter.art/fullchords/choir/{sym}.nc.json",
                "chord": sym,
                "instrument_name": "Choir",
                "default_voicing": True,
            }
            for sym in ("Cm", "F", "G", "Am", "Dm")
        ]
        discover._write_index_disk(url, offerings)
        discover._MM_CACHE.pop(url, None)
        slow = {"done": False}

        def linger(*_args):
            time.sleep(5)
            slow["done"] = True
            return []

        started = time.perf_counter()
        try:
            plan = assemble.plan_raw(
                COMP, seed=1, searchers={"openverse": linger}, verify_live=False, note_maps=[],
                registry_url=url, gather_deadline_s=0.35, settle_s=2,
            )
        finally:
            discover._MM_CACHE.pop(url, None)
            if previous is None:
                os.environ.pop("NC_CACHE", None)
            else:
                os.environ["NC_CACHE"] = previous
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertFalse(slow["done"])
        self.assertTrue(all(clip["built"] == "whole-chord file" for clip in plan["clips"]))
        self.assertTrue(all("Choir" in clip["title"] for clip in plan["clips"]))

    def test_a_slash_bass_prefers_the_matching_voicing(self):
        comp = dict(COMP, composition="4/4. quarter = 60. one chord per bar. C/E.")
        root = _clip("musicmatter", "piano C root", "piano", "piano-C-root", kind="supplier", chord="C")
        inv = _clip("musicmatter", "piano C inv", "piano", "piano-C-inv", kind="supplier", chord="C")
        root["bass_pc"] = 0
        inv["bass_pc"] = 4
        plan = _plan(1, comp=comp, searchers=_searchers({"musicmatter": [root, inv]}))
        clip = plan["clips"][0]
        self.assertEqual(clip["built"], "whole-chord file")
        self.assertEqual(clip["sids"], ["c0"])
        self.assertIn("inv", clip["title"])
        self.assertEqual(validate(plan), [])
        self.assertNotIn("stretch_to_s", json.dumps(plan))

    def test_a_slash_bass_stacks_an_indexed_bass_note(self):
        comp = dict(COMP, composition="4/4. quarter = 60. one chord per bar. C/E.")
        root = _clip("musicmatter", "piano C", "piano", "piano-C", kind="supplier", chord="C")
        root["bass_pc"] = 0
        note = {
            "kind": "uiowa_mis",
            "url": "https://theremin.music.uiowa.edu/Piano.mf.E3.aiff",
            "title": "piano E3",
            "author": "University of Iowa Electronic Music Studios (Lawrence Fritts, director)",
            "license": "PD-equivalent",
            "page": "https://theremin.music.uiowa.edu/MISpiano.html",
            "midi": 52,
            "expect_midi": 52,
            "pitched": True,
            "cents": 8.0,
        }
        maps = [{"id": "uiowa-piano", "origin": "uiowa", "instrument": "piano", "notes": {"52": note, "64": dict(note, midi=64, expect_midi=64, title="piano E4")}}]
        plan = assemble.plan_raw(
            comp, seed=1, searchers=_searchers({"musicmatter": [root]}), verify_live=False, note_maps=maps,
        )
        clip = plan["clips"][0]
        self.assertEqual(clip["built"], "chord and bass note")
        self.assertEqual(clip["sids"], ["c0", "c0b"])
        bass = plan["sources"]["c0b"]
        self.assertEqual(bass["kind"], "uiowa_mis")
        self.assertEqual(bass["title"], "piano E3")
        self.assertNotIn("cents", bass)
        self.assertNotIn("reverb", plan["master"])
        self.assertNotIn("stretch_to_s", json.dumps(plan))
        self.assertEqual(validate(plan), [])
        self.assertIn("piano E3", clip["credit"])

    def test_a_missing_bass_keeps_the_plain_chord(self):
        comp = dict(COMP, composition="8 sec. Dm7/C, G7/B, Am/C.")
        rows = [
            _clip("musicmatter", f"piano {sym}", "piano", f"p-{sym}", kind="supplier", chord=sym)
            for sym in ("Dm7", "G7", "Am")
        ]
        plan = _plan(1, comp=comp, searchers=_searchers({"musicmatter": rows}))
        self.assertEqual([clip["symbol"] for clip in plan["clips"]], ["Dm7", "G7", "Am"])
        self.assertTrue(all(clip["built"] == "whole-chord file" for clip in plan["clips"]))
        self.assertTrue(all(len(clip["sids"]) == 1 for clip in plan["clips"]))
        self.assertEqual(validate(plan), [])

    def test_the_disk_index_names_the_bass_and_a_slash_chord_plans(self):
        from ncperf import discover

        tmp = tempfile.mkdtemp(prefix="nc-bass-")
        self.addCleanup(shutil.rmtree, tmp, True)
        previous = os.environ.get("NC_CACHE")
        os.environ["NC_CACHE"] = tmp
        url = "https://example.test/suppliers-bass.json"
        offerings = [
            {
                "audio": "https://audio.musicmatter.art/fullchords/D/00a_main_Dm7_1_root.flac",
                "manifest": "https://musicmatter.art/sounds/fullchords-v4/D/00a_main_Dm7_1_root.nc.json",
                "instrument_name": "Piano",
            },
            {
                "audio": "https://audio.musicmatter.art/fullchords/D/00a_main_Dm7_4_inv3.flac",
                "manifest": "https://musicmatter.art/sounds/fullchords-v4/D/00a_main_Dm7_4_inv3.nc.json",
                "instrument_name": "Piano",
            },
        ]
        discover._write_index_disk(url, offerings)
        discover._MM_CACHE.pop(url, None)
        comp = dict(COMP, composition="4/4. quarter = 60. one chord per bar. Dm7/C.")
        try:
            plan = assemble.plan_raw(
                comp, seed=1, searchers={"openverse": lambda *_a: []}, verify_live=False, note_maps=[],
                registry_url=url, gather_deadline_s=0.4, settle_s=0,
            )
        finally:
            discover._MM_CACHE.pop(url, None)
            if previous is None:
                os.environ.pop("NC_CACHE", None)
            else:
                os.environ["NC_CACHE"] = previous
        clip = plan["clips"][0]
        self.assertEqual(clip["symbol"], "Dm7")
        self.assertEqual(clip["built"], "whole-chord file")
        self.assertIn("inv3", plan["sources"][clip["sids"][0]]["url"])

    def test_the_word_piano_does_not_take_the_whole_draw(self):
        comp = dict(COMP, performance="Piano. Let each chord ring. One ensemble, clear and unhurried.")
        families = [_plan(seed, comp=comp)["ensemble"]["family"] for seed in range(1, 11)]
        self.assertGreater(len(set(families)), 1)
        self.assertLess(families.count("piano"), 10)
        named = dict(COMP, performance="A choir, clear and unhurried.")
        choir = _plan(3, comp=named)
        self.assertEqual(choir["ensemble"]["family"], "choir")

    def test_a_note_family_that_covers_every_chord_joins_the_draw(self):
        groups = []
        for sym in ("Cm", "F", "G", "Am", "Dm"):
            groups.append([
                {"built": "whole-chord file", "origin": "musicmatter", "instrument": "piano", "tier": 0, "title": f"piano {sym}"},
                {"built": "assembled notes", "origin": "uiowa", "instrument": "guitar", "tier": 0, "title": f"guitar {sym}"},
            ])
        # Guitar misses the last chord, so it cannot cover the piece and does not fill the gap.
        short = [list(group) for group in groups]
        short[-1] = [row for row in short[-1] if row["instrument"] == "piano"]
        families = []
        for seed in range(1, 21):
            chosen, _primary = take.choose(groups, seed, ["piano"], None, False)
            self.assertTrue(chosen)
            self.assertTrue(all(row["instrument"] == chosen[0]["instrument"] for row in chosen))
            families.append(take.family_of(chosen[0]["instrument"]))
            held, _primary = take.choose(short, seed, ["piano"], None, False)
            self.assertTrue(all(row["instrument"] == "piano" for row in held))
        self.assertIn("piano", families)
        self.assertIn("guitar", families)
        choir_groups = []
        for sym in ("Cm", "F", "G", "Am", "Dm"):
            choir_groups.append([
                {"built": "whole-chord file", "origin": "musicmatter", "instrument": "choir", "tier": 0, "title": f"choir {sym}"},
                {"built": "assembled notes", "origin": "uiowa", "instrument": "guitar", "tier": 0, "title": f"guitar {sym}"},
            ])
        for seed in range(1, 6):
            chosen, _primary = take.choose(choir_groups, seed, ["choir"], None, False)
            self.assertTrue(all(row["instrument"] == "choir" for row in chosen))


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

    def test_warm_is_already_up(self):
        tmp = tempfile.mkdtemp(prefix="nc-warm-")
        self.addCleanup(shutil.rmtree, tmp, True)
        settings = _settings(tmp, suppliers="http://127.0.0.1/suppliers.json", llm_base="", llm_model="")
        _app, httpd = _api(settings)
        self.addCleanup(_close, httpd)
        origin = f"http://127.0.0.1:{httpd.server_address[1]}"
        with urllib.request.urlopen(origin + "/api/warm", timeout=5) as res:
            body = json.loads(res.read())
        self.assertEqual(body, {"ok": True, "warming": False})


class PreviewReadyTest(unittest.TestCase):
    def test_the_mp3_is_claimed_only_after_it_is_read_back(self):
        from nc_perform.api import _public_job
        from nc_perform.jobs import Store
        from nc_perform.runner import Runner

        tmp = tempfile.mkdtemp(prefix="nc-preview-")
        self.addCleanup(shutil.rmtree, tmp, True)
        settings = _settings(tmp, suppliers="http://127.0.0.1/suppliers.json", llm_base="", llm_model="")
        settings.public_base_url = "https://perform.example"

        class Mem:
            def __init__(self):
                self.rows = {}
                self.short = False

            def put(self, key, data, content_type="application/octet-stream", overwrite=True):
                self.rows[key] = bytes(data)

            def get(self, key):
                data = self.rows.get(key)
                if data is None:
                    return None
                return data[:-1] if self.short and data else data

        store = Store(os.path.join(tmp, "jobs.sqlite"))
        storage = Mem()
        store.create({"id": "h-0123456789ab", "state": "rendering", "stage": "rendering", "request": {}})
        runner = Runner(settings, store, storage, None)
        storage.short = True
        runner._publish_preview("h-0123456789ab", b"ID3-preview")
        job = store.get("h-0123456789ab")
        self.assertFalse((job.get("result") or {}).get("preview_ready"))
        storage.short = False
        runner._publish_preview("h-0123456789ab", b"ID3-preview")
        job = store.get("h-0123456789ab")
        self.assertTrue(job["result"]["preview_ready"])
        self.assertEqual(storage.rows["performances/h-0123456789ab/preview.mp3"], b"ID3-preview")

        class App:
            pass

        app = App()
        app.settings = settings
        app.storage = storage
        view = _public_job("h-0123456789ab", "rendering", "rendering", 40, None, app, job=job)
        self.assertTrue(view["preview_ready"])
        self.assertTrue(view["files"]["mp3"].endswith("/api/jobs/h-0123456789ab/files/preview.mp3"))
        from ncperf.progress import with_preview
        row = with_preview(
            {"chord": 0, "id": "c0", "title": "Choir Cm", "supplier": "Musicmatter", "license": "CC-BY-SA-4.0"},
            "g-0123456789ab",
            "https://perform.example",
        )
        self.assertEqual(row["preview_url"], "https://perform.example/api/jobs/g-0123456789ab/audio/c0")
        self.assertEqual(row["chord"], 0)


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
