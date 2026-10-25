"""Assembly plan v1: Gather publishes clips, Save stores the page's audio."""
from __future__ import annotations

import hashlib
import io
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
from test_service import _api, _close, _settings  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "service"))

from ncperf import assembly, gather, registry  # noqa: E402
from nc_perform.ids import sha256_text  # noqa: E402

VECTORS = os.path.join(ROOT, "ncperf", "data", "assembly-vectors.json")
GATHER = "g-0123456789ab"


def _tone(seconds=0.8, amp=0.2, freq=220.0) -> bytes:
    n = int(seconds * 48000)
    t = np.linspace(0, seconds, n, endpoint=False)
    y = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, np.column_stack([y, y]), 48000, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _plan() -> dict:
    def source(sid, title):
        return {
            "kind": "commons",
            "url": f"http://127.0.0.1/{sid}.wav",
            "page": f"http://127.0.0.1/{sid}",
            "license": "CC0-1.0",
            "title": title,
            "author": "Ada",
        }

    def clip(index, symbol, title):
        return {
            "id": f"c{index}",
            "chord": index,
            "symbol": symbol,
            "sids": [f"c{index}"],
            "title": title,
            "author": "Ada",
            "supplier": "Local",
            "license": "CC0-1.0",
            "credit": f"{title} by Ada",
            "page": f"http://127.0.0.1/c{index}",
            "t": index * 2,
            "dur": 2,
        }

    return {
        "nc_plan_version": "0.1",
        "assembly": "raw",
        "title": "Two chords",
        "composition": {
            "title": "Two chords",
            "composer": "Ada",
            "words_verbatim": {"composition": "Cm, F.", "performance": ""},
        },
        "interpreter": {"kind": "discover", "note": "raw assembly"},
        "seed": 4,
        "length_s": 4,
        "tail_s": 0,
        "chords": [
            {"t": 0, "dur": 2, "symbol": "Cm", "voicing": [60], "section": "A"},
            {"t": 2, "dur": 2, "symbol": "F", "voicing": [65], "section": "A"},
        ],
        "sources": {"c0": source("c0", "Quiet Cm"), "c1": source("c1", "Loud F")},
        "banks": {},
        "layers": [{"id": "chords", "type": "clips", "role": "one recording per chord", "events": []}],
        "master": {"buses": {"dry": {}}},
        "clips": [clip(0, "Cm", "Quiet Cm"), clip(1, "F", "Loud F")],
    }


def _post(url, body, headers=None):
    head = {"Content-Type": "application/json"}
    if headers:
        head.update(headers)
    req = urllib.request.Request(url, json.dumps(body).encode(), head)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.loads(response.read()), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers


def _form(fields):
    boundary = "----nc-take"
    chunks = []
    for name, value, filename in fields:
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
        if filename:
            head += f'; filename="{filename}"'
        blob = value if isinstance(value, bytes) else value.encode()
        chunks.append(head.encode() + b"\r\n\r\n" + blob + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _post_form(url, fields):
    body, ctype = _form(fields)
    req = urllib.request.Request(url, body, {"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.loads(response.read()), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers


class OrganicVectorsTest(unittest.TestCase):
    def test_the_page_can_match_the_published_vectors(self):
        live = assembly.reference_vectors()
        self.assertEqual([row["name"] for row in live], [
            "five-chords-seed-1",
            "five-chords-seed-2",
            "floor-eight-seed-3",
            "pulse-four-seed-1",
            "pulse-broaden-seed-4",
            "two-sections-seed-7",
            "written-60bpm-bars",
        ])
        floor = next(row for row in live if row["name"] == "floor-eight-seed-3")
        self.assertGreaterEqual(floor["min_s"], 1.25)
        self.assertFalse(floor["looks_alternating"])
        self.assertGreaterEqual(floor["duration_cv"], 0.22)
        written = live[-1]
        self.assertEqual(written["durations_s"], [4.0, 8.0, 4.0])
        self.assertFalse(written["floor_applied"])
        stored = json.loads(open(VECTORS, encoding="utf-8").read())
        self.assertEqual(stored, live)


class AssemblyPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nc-assembly-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.reg = os.environ.get("NC_REGISTRY")
        path = os.path.join(self.tmp, "registry.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"sounds": {}}, fh)
        os.environ["NC_REGISTRY"] = path
        registry.reset()
        self.addCleanup(self._restore_registry)
        self.plan = _plan()
        tone = _tone()
        files = [
            {"sid": "c0", "url": "http://127.0.0.1/c0.wav", "ext": ".wav", "data": tone},
            {"sid": "c1", "url": "http://127.0.0.1/c1.wav", "ext": ".wav", "data": _tone(freq=330.0)},
        ]
        self.files, self.meta = assembly.materialize(self.plan, files)
        self.sounds = gather.public_sounds(self.plan, GATHER, "https://perform.example", self.files)
        assembly.annotate_sounds(self.sounds, self.meta)
        self.doc = {
            "id": GATHER,
            "seed": 4,
            "claim_hash": sha256_text("gather-secret"),
            "composition": {"title": "Two chords", "composer": "Ada", "composition": "Cm, F.", "performance": ""},
            "plan": self.plan,
            "sounds": self.sounds,
            "clock": None,
            "files": [{"sid": row["sid"], "url": row.get("url"), "ext": row.get("ext")} for row in self.files],
        }

    def _restore_registry(self):
        registry.reset()
        if self.reg is None:
            os.environ.pop("NC_REGISTRY", None)
        else:
            os.environ["NC_REGISTRY"] = self.reg

    def _entry(self, sound, at, length=None):
        return {
            "pos": sound["pos"],
            "chord": sound["symbol"],
            "clip_id": sound["clip_id"],
            "at_s": at,
            "len_s": sound["duration_s"] if length is None else length,
            "gain_db": sound["gain_db"],
            "fade_in_ms": 12,
            "fade_out_ms": 12,
        }

    def _good(self, length=None):
        entries = [self._entry(self.sounds[0], 0, length), self._entry(self.sounds[1], 0.8, length)]
        return {
            "nc_assembly_version": "1",
            "gather_id": GATHER,
            "seed": 9,
            "entries": entries,
            "total_s": 1.6,
        }

    def test_gather_publishes_the_clip_the_page_plays(self):
        row = self.sounds[0]
        for key in ("clip_id", "pos", "url", "start_s", "end_s", "duration_s", "gain_db", "loudness_lufs", "license", "credit"):
            self.assertIn(key, row)
        self.assertEqual(row["url"], "https://perform.example/api/jobs/g-0123456789ab/audio/c0")
        self.assertEqual(row["pos"], 0)
        self.assertGreater(row["duration_s"], 0.7)
        self.assertLess(abs(row["gain_db"] - (assembly.assemble.CLIP_LUFS - row["loudness_lufs"])), 0.02)
        self.assertIsNone(assembly.written_clock(self.doc["composition"], self.plan))
        clock = assembly.written_clock(
            {"composition": "4/4. 60 BPM. [1 bar Cm] [1 bar F].", "performance": ""},
            self.plan,
        )
        self.assertEqual(clock["durations_s"], [4.0, 4.0])
        self.assertEqual(clock["tempo"][0]["bpm"], 60.0)

    def test_keep_rejects_a_plan_that_is_not_this_gather(self):
        good = self._good()
        self.assertEqual(assembly.validate(good, self.doc), [])
        stretch = self._good(length=self.sounds[0]["duration_s"] + 0.05)
        self.assertTrue(any("time-stretched" in item for item in assembly.validate(stretch, self.doc)))
        foreign = self._good()
        foreign["entries"][1]["clip_id"] = "c9"
        self.assertTrue(any("not in this Gather" in item for item in assembly.validate(foreign, self.doc)))
        swapped = self._good()
        swapped["entries"][0]["chord"] = "F"
        self.assertTrue(any("not Cm" in item for item in assembly.validate(swapped, self.doc)))
        faded = self._good()
        faded["entries"][0]["fade_in_ms"] = 80
        self.assertTrue(any("50 ms" in item for item in assembly.validate(faded, self.doc)))
        loud = self._good()
        loud["entries"][0]["gain_db"] = self.sounds[0]["gain_db"] + 3
        self.assertTrue(any("level match" in item for item in assembly.validate(loud, self.doc)))
        long = self._good()
        long["total_s"] = 181
        long["entries"][1]["at_s"] = 180
        self.assertTrue(any("180" in item for item in assembly.validate(long, self.doc)))
        self.doc["clock"] = {"written": True, "durations_s": [1.0, 1.0], "slots": [{"t": 0, "dur": 1}, {"t": 1, "dur": 1}]}
        off = self._good()
        off["total_s"] = 2
        off["entries"][0]["at_s"] = 0
        off["entries"][0]["len_s"] = 0.4
        off["entries"][1]["at_s"] = 0.2
        off["entries"][1]["len_s"] = 0.4
        self.assertTrue(any("written lengths" in item for item in assembly.validate(off, self.doc)))

    def test_upload_must_match_the_plan_and_be_audible(self):
        audio = _tone(seconds=1.6, amp=0.2)
        info = assembly.inspect_upload(audio, 1.6)
        self.assertEqual(info["format"], "wav")
        self.assertEqual(info["upload_sha256"], hashlib.sha256(audio).hexdigest())
        with self.assertRaises(ValueError) as silent:
            assembly.inspect_upload(_tone(seconds=1.6, amp=0.0), 1.6)
        self.assertIn("silent", str(silent.exception))
        with self.assertRaises(ValueError) as foreign:
            assembly.inspect_upload(b"not audio", 1.6)
        self.assertIn("WAV or FLAC", str(foreign.exception))
        buf = io.BytesIO()
        n = int(1.6 * 48000)
        y = (0.2 * np.sin(2 * np.pi * 220 * np.linspace(0, 1.6, n, endpoint=False))).astype(np.float32)
        sf.write(buf, np.column_stack([y, y]), 48000, format="FLAC")
        flac = buf.getvalue()
        packed = assembly.inspect_upload(flac, 1.6)
        self.assertEqual(packed["format"], "flac")
        bad = assembly.parse_take_state({"plan": self._good()}, GATHER)
        self.assertEqual(bad["processing"]["chain"], [])
        with self.assertRaises(ValueError):
            assembly.parse_take_state({"plan": self._good(), "processing": {"chain": [";;;"]}}, GATHER)

    def test_take_state_v1_stays_a_list_of_names(self):
        raw = {
            "nc_take_version": "1",
            "plan": self._good(),
            "processing": {"chain": [" filter ", "dials"], "params": {"wet": 0.2}, "schema": 2},
        }
        got = assembly.parse_take_state(raw, GATHER)
        self.assertEqual(got["nc_take_version"], "1")
        self.assertEqual(got["processing"], {"chain": ["filter", "dials"], "params": {"wet": 0.2}})
        with self.assertRaises(ValueError) as objects:
            assembly.parse_take_state({
                "nc_take_version": "1",
                "plan": self._good(),
                "processing": {"chain": [self._hack("lowpass")]},
            }, GATHER)
        self.assertIn("short names", str(objects.exception))

    def test_take_state_v2_keeps_the_hack_stack(self):
        chain = [
            self._hack("lowpass", params={"cutoff": 0, "cutoff_hz": None, "q": 0.707}),
            self._hack("delay", params={"delay_beats": 0.75, "delay_timing": "1/8 dotted", "wet": 0}),
            self._hack("plate", params={"mix": 0, "decay": 0.5}),
            self._limiter(),
        ]
        got = assembly.parse_take_state({
            "nc_take_version": "2",
            "plan": self._good(),
            "processing": {"schema": 2, "chain": chain},
        }, GATHER)
        self.assertEqual(got["nc_take_version"], "2")
        self.assertEqual(got["processing"], {"schema": 2, "chain": chain})
        rec = {"performance": {"outputs": [{}]}}
        assembly.finish_browser_record(rec, got, {"upload_sha256": "ab" * 32}, os.path.join(self.tmp, "missing.flac"))
        self.assertEqual(rec["take_state"]["nc_take_version"], "2")
        self.assertEqual(rec["take_state"]["processing"]["chain"], chain)
        self.assertEqual(rec["take_state"]["processing"]["schema"], 2)
        self.assertEqual(rec["hacks_chain"][1]["chain"], chain)
        self.assertTrue(rec["record_hash"]["value"])

    def test_take_state_v2_new_hacks_and_duplicates(self):
        chain = [
            self._hack("delay", 1, params={"delay_s": 0.25, "feedback": 0.4, "wet": 0.3}),
            self._hack("delay", 2, params={"delay_s": 0.5, "feedback": 0.2, "wet": 0.2}),
            self._hack("eq", params={"low_db": 3, "mid_db": -2, "mid_hz": 1200, "high_db": 0}),
            self._hack("drive", params={"drive_db": 9, "output_db": 0, "mix": 1}),
            self._hack("compressor", params={"threshold_db": -24, "ratio": 4, "attack_ms": 10, "release_ms": 150, "makeup_db": 6}),
            self._hack("width", params={"width": 1.4, "balance": 0}),
            self._hack("eq", 2, params={"low_db": 0, "mid_db": 0, "mid_hz": 1000, "high_db": -3}),
            self._limiter(),
        ]
        got = assembly.parse_take_state({
            "nc_take_version": "2",
            "plan": self._good(),
            "processing": {"schema": 2, "chain": chain},
        }, GATHER)
        self.assertEqual(got["processing"]["chain"], chain)
        ten = [self._hack("delay", n) for n in range(1, 11)] + [self._limiter()]
        got = assembly.parse_take_state({
            "nc_take_version": "2",
            "plan": self._good(),
            "processing": {"schema": 2, "chain": ten},
        }, GATHER)
        self.assertEqual(len(got["processing"]["chain"]), 11)

    def test_take_state_v2_filters_and_eq_v2(self):
        eq2 = self._hack("eq", params={"low_db": 18, "mid_db": -18, "mid_hz": 1000, "high_db": 6,
                                       "low_hz": 150, "high_hz": 6000, "mid_q": 0.99})
        eq2["version"] = 2
        chain = [
            self._hack("highpass", params={"cutoff": 0, "cutoff_hz": None, "q": 0.707}),
            self._hack("highpass", 2, params={"cutoff": 40, "cutoff_hz": 126, "q": 1.464}),
            self._hack("bandpass", params={"centre_hz": 1020, "q": 0.4, "mix": 1}),
            eq2,
            self._hack("eq", 2, params={"low_db": 3, "mid_db": 0, "mid_hz": 1000, "high_db": 0}),
            self._hack("plate", params={"mix": 0.2, "decay": 0.5}),
            self._limiter(),
        ]
        got = assembly.parse_take_state({
            "nc_take_version": "2",
            "plan": self._good(),
            "processing": {"schema": 2, "chain": chain},
        }, GATHER)
        self.assertEqual(got["processing"]["chain"], chain)

    def test_take_state_v2_rejections(self):
        def parse(chain):
            assembly.parse_take_state({
                "nc_take_version": "2",
                "plan": self._good(),
                "processing": {"schema": 2, "chain": chain},
            }, GATHER)

        eleven = [self._hack("lowpass", n) for n in range(11)] + [self._limiter()]
        with self.assertRaises(ValueError) as too_many:
            parse(eleven)
        self.assertIn("10", str(too_many.exception))
        with self.assertRaises(ValueError) as order:
            parse([self._limiter(), self._hack("lowpass")])
        self.assertIn("last", str(order.exception))
        unknown = self._hack("lowpass")
        unknown["type"] = "ring"
        with self.assertRaises(ValueError) as kind:
            parse([unknown, self._limiter()])
        self.assertIn("Unknown", str(kind.exception))
        nan = self._hack("lowpass", params={"cutoff_hz": float("nan")})
        with self.assertRaises(ValueError) as broken:
            parse([nan, self._limiter()])
        self.assertIn("finite", str(broken.exception))

    def test_take_state_v2_stores_schema_3_without_running_it(self):
        chain = [
            self._hack("lowpass", params={"cutoff": 75, "cutoff_hz": 500}),
            self._limiter(),
        ]
        chain[0]["auto"] = {"param": "cutoff_hz", "from": 500, "to": 200, "over": "piece"}
        got = assembly.parse_take_state({
            "nc_take_version": "2",
            "plan": self._good(),
            "processing": {
                "schema": 3,
                "master": {"chain": chain},
                "tracks": {"t1": {"chain": []}, "t8": {"chain": []}},
            },
        }, GATHER)
        self.assertEqual(got["nc_take_version"], "2")
        self.assertEqual(got["processing"]["schema"], 3)
        self.assertEqual(got["processing"]["master"]["chain"], chain)
        self.assertEqual(got["processing"]["tracks"]["t1"], {"chain": []})
        self.assertEqual(got["processing"]["tracks"]["t8"], {"chain": []})
        rec = {"performance": {"outputs": [{}]}}
        assembly.finish_browser_record(rec, got, {"upload_sha256": "ab" * 32}, os.path.join(self.tmp, "missing.flac"))
        self.assertEqual(rec["hacks_chain"][1]["chain"], chain)
        self.assertIn("did not re-run", rec["hacks_chain"][1]["type"])
        self.assertEqual(rec["take_state"]["processing"]["schema"], 3)

    def test_schema_3_rejections(self):
        def parse(processing):
            assembly.parse_take_state({
                "nc_take_version": "2",
                "plan": self._good(),
                "processing": processing,
            }, GATHER)

        master = {"chain": [self._hack("lowpass"), self._limiter()]}
        tracks = {f"t{i}": {"chain": []} for i in range(1, 10)}
        with self.assertRaises(ValueError) as too_many:
            parse({"schema": 3, "master": master, "tracks": tracks})
        self.assertIn("8", str(too_many.exception))
        with self.assertRaises(ValueError) as track_lim:
            parse({"schema": 3, "master": master, "tracks": {"t2": {"chain": [self._limiter()]}}})
        self.assertIn("limiter", str(track_lim.exception).lower())
        wet = self._hack("delay", params={"wet": 0})
        wet["auto"] = {"param": "wet", "from": 0, "to": 1, "over": "piece"}
        with self.assertRaises(ValueError) as glide:
            parse({"schema": 3, "master": {"chain": [wet, self._limiter()]}})
        self.assertIn("cutoff_hz", str(glide.exception))
        with self.assertRaises(ValueError) as on_two:
            parse({"schema": 2, "chain": [wet, self._limiter()]})
        self.assertIn("schema 3", str(on_two.exception))
        zero = self._hack("bandpass")
        zero["auto"] = {"param": "centre_hz", "from": 0, "to": 800, "over": "piece"}
        with self.assertRaises(ValueError) as freq:
            parse({"schema": 3, "master": {"chain": [zero, self._limiter()]}})
        self.assertIn("above 0", str(freq.exception))

    def _hack(self, type_name, n=1, params=None):
        return {
            "id": f"{type_name}-{n}",
            "type": type_name,
            "version": 1,
            "bypass": False,
            "params": {} if params is None else params,
        }

    def _limiter(self):
        return {
            "id": "limiter",
            "type": "limiter",
            "version": 1,
            "bypass": False,
            "fixed": True,
            "params": {"ceiling_db": -1, "lookahead_s": 0.0015},
        }

    def test_save_stores_the_upload_and_signs_the_record(self):
        from nc_perform.storage import FileStorage
        storage = FileStorage(os.path.join(self.tmp, "files"))
        gather.save_collection(storage, GATHER, self.doc, self.files)
        kept = self._good(length=0.4)
        take = {
            "nc_take_version": "1",
            "plan": kept,
            "processing": {"chain": ["filter", "dials"], "params": {"cutoff_hz": 800, "wet": 0.2}},
        }
        audio = _tone(seconds=1.6, amp=0.2)
        settings = _settings(self.tmp, suppliers="http://127.0.0.1/suppliers.json", llm_base="", llm_model="")
        settings.public_base_url = "https://perform.example"
        app, httpd = _api(settings)
        self.addCleanup(_close, httpd)
        app.storage = storage
        app.runner.storage = storage
        origin = f"http://127.0.0.1:{httpd.server_address[1]}"
        code, denied, _headers = _post_form(origin + "/api/keep-take", [
            ("gather_id", GATHER, None),
            ("take_state", json.dumps(take), None),
            ("audio", audio, "take.wav"),
        ])
        self.assertEqual(code, 400, denied)
        self.assertEqual(denied["error"], "claim_token is required.")
        short = _tone(seconds=0.2, amp=0.2)
        code, mismatch, _headers = _post_form(origin + "/api/keep-take", [
            ("gather_id", GATHER, None),
            ("claim_token", "gather-secret", None),
            ("take_state", json.dumps(take), None),
            ("audio", short, "take.wav"),
        ])
        self.assertEqual(code, 400, mismatch)
        self.assertIn("plan is", mismatch["error"])
        code, body, _headers = _post_form(origin + "/api/keep-take", [
            ("gather_id", GATHER, None),
            ("claim_token", "gather-secret", None),
            ("take_state", json.dumps(take), None),
            ("title", "Kept two", None),
            ("listed", "false", None),
            ("audio", audio, "take.wav"),
        ])
        self.assertEqual(code, 200, body)
        self.assertTrue(body["record_url"].endswith(f"/records/{body['id']}.json"))
        self.assertTrue(body["page_url"].endswith(f"/records/{body['id']}"))
        rec = json.loads(storage.get(f"performances/{body['id']}/record.json"))
        self.assertEqual(rec["assembly_plan"]["seed"], 9)
        self.assertEqual(rec["take_state"]["processing"]["chain"], ["filter", "dials"])
        self.assertEqual(rec["performance"]["take_audio"]["upload_sha256"], hashlib.sha256(audio).hexdigest())
        self.assertEqual(rec["performance"]["take_audio"]["source"], "browser")
        self.assertEqual(rec["hacks_chain"][1]["tool"], "browser-processing")
        self.assertTrue(rec["performance"]["timeline"])
        self.assertGreaterEqual(len(rec["sounds_used"]), 2)
        self.assertTrue(rec["sounds_used"][0]["credit_line"])
        self.assertIn("signature", rec)
        self.assertTrue(storage.get(f"performances/{body['id']}/preview.mp3"))
        flac = storage.get(f"performances/{body['id']}/performance.flac")
        self.assertTrue(flac)
        self.assertNotEqual(hashlib.sha256(flac).hexdigest(), hashlib.sha256(audio).hexdigest())
        self.assertIsNone(storage.get(f"jobs/{body['id']}/take-audio"))
        code, plain, _headers = _post(origin + "/api/perform", {
            "title": "Still one shot",
            "composition": "1 min. Cm.",
            "performance": "plain",
        })
        self.assertEqual(code, 202, plain)
        queued = app.store.get(plain["id"])
        self.assertIsNone(queued["request"]["plan"])
        self.assertIsNone(queued["request"].get("gather_id"))


class ClipCorsTest(unittest.TestCase):
    def test_netcomposer_is_echoed_and_the_clip_is_cached(self):
        self.assertEqual(assembly.allow_origin("https://netcomposer.art"), ("https://netcomposer.art", True))
        self.assertEqual(assembly.allow_origin("https://www.netcomposer.art")[0], "https://www.netcomposer.art")
        self.assertEqual(assembly.allow_origin("https://not-netcomposer.art"), ("*", False))
        tmp = tempfile.mkdtemp(prefix="nc-cors-")
        self.addCleanup(shutil.rmtree, tmp, True)
        settings = _settings(tmp, suppliers="http://127.0.0.1/suppliers.json", llm_base="", llm_model="")
        app, httpd = _api(settings)
        self.addCleanup(_close, httpd)
        gather.save_collection(app.storage, GATHER, {
            "id": GATHER,
            "seed": 1,
            "claim_hash": "abc",
            "sounds": [],
        }, [{"sid": "c0", "url": "https://example.test/c0.wav", "ext": ".wav", "data": b"RIFFclip"}])
        origin = f"http://127.0.0.1:{httpd.server_address[1]}"
        req = urllib.request.Request(
            origin + f"/api/jobs/{GATHER}/audio/c0",
            headers={"Origin": "https://play.netcomposer.art"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            self.assertEqual(response.read(), b"RIFFclip")
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://play.netcomposer.art")
            self.assertIn("31536000", response.headers["Cache-Control"])
            self.assertEqual(response.headers["Vary"], "Origin")


if __name__ == "__main__":
    unittest.main()


class StackRebuildTest(unittest.TestCase):
    """A note stack whose mixed clip went missing is mixed again from its notes."""

    def setUp(self):
        from nc_perform.storage import FileStorage
        self.tmp = tempfile.mkdtemp(prefix="nc-stack-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.reg = os.environ.get("NC_REGISTRY")
        path = os.path.join(self.tmp, "registry.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"sounds": {}}, fh)
        os.environ["NC_REGISTRY"] = path
        registry.reset()
        self.addCleanup(self._restore_registry)

        def note(sid, midi):
            return {
                "kind": "uiowa",
                "url": f"http://127.0.0.1/guitar.aif#note={midi}",
                "page": "http://127.0.0.1/guitar",
                "license": "PD",
                "title": f"guitar note {midi}",
                "author": "University of Iowa",
            }

        self.plan = _plan()
        self.plan["sources"]["c1n0"] = note("c1n0", 50)
        self.plan["sources"]["c1n1"] = note("c1n1", 57)
        self.plan["sources"]["c1n2"] = note("c1n2", 65)
        self.plan["sources"].pop("c1", None)
        self.plan["clips"][1]["sids"] = ["c1n0", "c1n1", "c1n2"]
        self.notes = {"c1n0": _tone(freq=146.8), "c1n1": _tone(seconds=1.1, freq=220.0), "c1n2": _tone(freq=349.2)}
        files = [{"sid": "c0", "url": "http://127.0.0.1/c0.wav", "ext": ".wav", "data": _tone()}]
        for sid, data in self.notes.items():
            files.append({"sid": sid, "url": self.plan["sources"][sid]["url"], "ext": ".wav", "data": data})
        self.files, self.meta = assembly.materialize(self.plan, files)
        self.sounds = gather.public_sounds(self.plan, GATHER, "https://perform.example", self.files)
        assembly.annotate_sounds(self.sounds, self.meta)
        doc = {"id": GATHER, "seed": 4, "plan": self.plan, "sounds": self.sounds, "clock": None}
        self.storage = FileStorage(os.path.join(self.tmp, "files"))
        gather.save_collection(self.storage, GATHER, doc, self.files)
        self.mixed = bytes(self.storage.get(f"gathers/{GATHER}/audio/c1"))

    def _restore_registry(self):
        registry.reset()
        if self.reg is None:
            os.environ.pop("NC_REGISTRY", None)
        else:
            os.environ["NC_REGISTRY"] = self.reg

    def _drop(self, sid):
        os.remove(os.path.join(self.tmp, "files", "gathers", GATHER, "audio", sid))

    def test_collection_keeps_what_a_rebuild_needs(self):
        doc = gather.load_collection(self.storage, GATHER)
        self.assertEqual(assembly.stack_parts(doc["plan"], "c1"), ["c1n0", "c1n1", "c1n2"])
        self.assertEqual(assembly.stack_parts(doc["plan"], "c0"), [])
        stored = {row["sid"]: row for row in doc["files"]}
        for sid in ("c1n0", "c1n1", "c1n2"):
            self.assertIn(sid, stored)
            self.assertTrue(doc["plan"]["sources"][sid]["url"].endswith(stored[sid]["url"].split("/")[-1]))

    def test_rebuild_from_stored_notes_matches_gather(self):
        self._drop("c1")
        self.assertIsNone(self.storage.get(f"gathers/{GATHER}/audio/c1"))
        wav = assembly.rebuild_stack(self.storage, GATHER, "c1")
        self.assertEqual(wav, self.mixed)
        self.assertEqual(bytes(self.storage.get(f"gathers/{GATHER}/audio/c1")), self.mixed)

    def test_rebuild_downloads_a_missing_note(self):
        self._drop("c1")
        self._drop("c1n1")
        asked = []
        real = gather.cache_sources

        def fake(plan, budget_s=5.0):
            asked.append(sorted((plan.get("sources") or {}).keys()))
            return [{"sid": "c1n1", "url": "http://127.0.0.1/guitar.aif", "ext": ".wav", "data": self.notes["c1n1"]}]

        gather.cache_sources = fake
        self.addCleanup(setattr, gather, "cache_sources", real)
        wav = assembly.rebuild_stack(self.storage, GATHER, "c1")
        self.assertEqual(asked, [["c1n1"]])
        self.assertEqual(wav, self.mixed)

    def test_rebuild_refuses_when_a_note_cannot_be_found(self):
        self._drop("c1")
        self._drop("c1n2")
        real = gather.cache_sources
        gather.cache_sources = lambda plan, budget_s=5.0: []
        self.addCleanup(setattr, gather, "cache_sources", real)
        with self.assertRaises(ValueError):
            assembly.rebuild_stack(self.storage, GATHER, "c1")
        with self.assertRaises(ValueError):
            assembly.rebuild_stack(self.storage, GATHER, "c0")

    def test_internal_stack_route(self):
        self._drop("c1")
        settings = _settings(self.tmp, suppliers="http://127.0.0.1/suppliers.json", llm_base="", llm_model="")
        settings.internal_token = "inside"
        app, httpd = _api(settings)
        self.addCleanup(_close, httpd)
        app.storage = self.storage
        app.runner.storage = self.storage
        origin = f"http://127.0.0.1:{httpd.server_address[1]}"

        def post(path, token):
            req = urllib.request.Request(origin + path, data=b"{}", method="POST", headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.read(), r.headers.get("Content-Type")
            except urllib.error.HTTPError as e:
                return e.code, e.read(), e.headers.get("Content-Type")

        code, _body, _ctype = post(f"/internal/jobs/{GATHER}/stack/c1", "wrong")
        self.assertEqual(code, 401)
        code, body, ctype = post(f"/internal/jobs/{GATHER}/stack/c1", "inside")
        self.assertEqual(code, 200, body[:200])
        self.assertEqual(ctype, "audio/wav")
        self.assertEqual(body, self.mixed)
        code, body, _ctype = post(f"/internal/jobs/{GATHER}/stack/c0", "inside")
        self.assertEqual(code, 422)
        self.assertIn("not a note stack", json.loads(body)["error"])
