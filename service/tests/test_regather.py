"""Re-gather one sound: a new recording for one chord, every other chord kept."""
from __future__ import annotations

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
from test_discover import COMP, _chords, _searchers  # noqa: E402
from test_service import _api, _close, _settings  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "service"))

from ncperf import assembly, gather  # noqa: E402
from nc_perform.ids import sha256_text  # noqa: E402

OLD = "g-0123456789ab"


def _tone(freq=220.0, seconds=0.8) -> bytes:
    n = int(seconds * 48000)
    t = np.linspace(0, seconds, n, endpoint=False)
    y = (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, np.column_stack([y, y]), 48000, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _with_bytes(files, freq):
    out = []
    for row in files:
        row = dict(row)
        row["data"] = _tone(freq)
        row["ext"] = ".wav"
        out.append(row)
    return out


def _pools(two=True):
    pools = {"wikimedia": _chords("wikimedia", "piano", kind="commons", prefix="pa")}
    if two:
        pools["wikimedia"] += _chords("wikimedia", "guitar", kind="commons", prefix="gb")
        pools["wikimedia"] += _chords("wikimedia", "organ", kind="commons", prefix="oc")
    return pools


def _previous(two=True, seed=3):
    comp = dict(COMP)
    built = gather.build(comp, seed=seed, searchers=_searchers(_pools(two)), verify_live=False, note_maps=[])
    plan = built["plan"]
    files, meta = assembly.materialize(plan, _with_bytes(built["files"], 220.0))
    sounds = assembly.annotate_sounds(gather.public_sounds(plan, OLD, "https://perform.example", files), meta)
    return {
        "id": OLD,
        "seed": seed,
        "claim_hash": sha256_text("old-secret"),
        "composition": comp,
        "plan": plan,
        "sounds": sounds,
        "picked_keys": built["picked_keys"],
        "picked_sample_ids": built["picked_sample_ids"],
        "clock": None,
        "files": [{k: v for k, v in row.items() if k != "data"} for row in files],
    }, files


def _regather(prev, index, two=True, job="g-abcdefghjkmn"):
    built = gather.build_slot(prev, index, seed=9, searchers=_searchers(_pools(two)), verify_live=False, note_maps=[])
    one = built["plan"]
    files, meta = assembly.materialize(one, _with_bytes(built["files"], 330.0))
    one_sounds = assembly.annotate_sounds(gather.public_sounds(one, job, "https://perform.example", files), meta)
    return gather.merge_slot(prev, index, one, one_sounds, files, built, job, sha256_text("new-secret"))


class RegatherTest(unittest.TestCase):
    def test_one_chord_is_new_and_every_other_chord_is_kept(self):
        prev, _files = _previous()
        doc, files = _regather(prev, 2)
        old_plan, new_plan = prev["plan"], doc["plan"]
        self.assertEqual(len(new_plan["clips"]), len(old_plan["clips"]))
        for i, (a, b) in enumerate(zip(old_plan["clips"], new_plan["clips"])):
            if i == 2:
                self.assertEqual((b["id"], b["chord"], b["symbol"], b["t"], b["dur"]), (a["id"], a["chord"], a["symbol"], a["t"], a["dur"]))
                self.assertNotEqual(b["title"], a["title"])
                self.assertTrue(all(sid.startswith("c2") for sid in b["sids"]))
            else:
                self.assertEqual(a, b)
        old_other = {k: v for k, v in old_plan["sources"].items() if not gather.slot_pattern(2).match(k)}
        new_other = {k: v for k, v in new_plan["sources"].items() if not gather.slot_pattern(2).match(k)}
        self.assertEqual(old_other, new_other)
        self.assertNotEqual(old_plan["sources"]["c2"].get("url"), new_plan["sources"]["c2"].get("url"))
        # The list the page shows: the same rows, now served by the new Gather, and one new row.
        self.assertEqual(len(doc["sounds"]), len(prev["sounds"]))
        for i, (a, b) in enumerate(zip(prev["sounds"], doc["sounds"])):
            self.assertIn("/api/jobs/g-abcdefghjkmn/audio/", b["preview_url"])
            self.assertEqual(b["pos"], i)
            if i != 2:
                self.assertEqual(a["title"], b["title"])
                self.assertEqual(a["duration_s"], b["duration_s"])
        self.assertNotEqual(prev["sounds"][2]["title"], doc["sounds"][2]["title"])
        self.assertTrue(doc["sounds"][2]["license"] and doc["sounds"][2]["credit"])
        self.assertEqual(doc["regather"], {"from": OLD, "chord": 2, "sids": ["c2"]})
        self.assertEqual(doc["composition"], prev["composition"])
        self.assertTrue(set(prev["picked_keys"]) <= set(doc["picked_keys"]))
        carried = [row for row in files if row.get("carried")]
        self.assertTrue(carried and all(not row["sid"].startswith("c2") for row in carried))
        self.assertEqual([row["sid"] for row in files if not row.get("carried")], ["c2"])

    def test_a_second_regather_skips_the_first_two_recordings(self):
        prev, _files = _previous()
        first, _ = _regather(prev, 1)
        first["files"] = []
        second, _ = _regather(first, 1, job="g-bcdefghjkmnp")
        titles = {prev["plan"]["clips"][1]["title"], first["plan"]["clips"][1]["title"], second["plan"]["clips"][1]["title"]}
        self.assertEqual(len(titles), 3)

    def test_no_other_recording_keeps_the_old_one(self):
        prev, _files = _previous()
        real = gather.build
        self.addCleanup(lambda: setattr(gather, "build", real))

        def nothing(*_a, **kw):
            self.assertTrue(kw["strict_avoid"])
            self.assertTrue(set(prev["picked_keys"]) <= set(kw["avoid_keys"]))
            raise ValueError("No exact chord recording")

        gather.build = nothing
        with self.assertRaisesRegex(ValueError, r"^No other recording of \w+ was found\. The old one stays\.$"):
            _regather(prev, 0)

        def same(*_a, **kw):
            # A supplier that answers with the very file it was asked to replace.
            one = json.loads(json.dumps(prev["plan"]))
            one["clips"] = [dict(one["clips"][0], id="c0", chord=0)]
            one["sources"] = {"c0": one["sources"]["c0"]}
            return {"plan": one, "files": [], "picked_keys": [], "picked_sample_ids": []}

        gather.build = same
        with self.assertRaisesRegex(ValueError, r"The old one stays"):
            _regather(prev, 0)

    def test_only_open_licences_reach_the_new_collection(self):
        prev, _files = _previous()
        doc, _ = _regather(prev, 4)
        for src in doc["plan"]["sources"].values():
            self.assertIn(src.get("license"), ("CC0-1.0", "CC-BY-4.0", "CC-BY-3.0", "CC-BY-SA-4.0", "CC-BY-SA-3.0", "PD", "PDM-1.0"))
        self.assertNotIn("time_stretch", json.dumps(doc["plan"]))

    def test_a_kept_take_of_the_new_collection_validates(self):
        prev, _files = _previous()
        doc, _ = _regather(prev, 0)
        entries, at = [], 0.0
        for i, row in enumerate(doc["sounds"]):
            entries.append({"pos": i, "chord": row["symbol"], "clip_id": row["clip_id"], "at_s": round(at, 3), "len_s": row["duration_s"],
                            "gain_db": row["gain_db"], "fade_in_ms": 12, "fade_out_ms": 12})
            at += row["duration_s"]
        plan = {"nc_assembly_version": "1", "gather_id": doc["id"], "seed": 1, "entries": entries, "total_s": round(at, 3)}
        self.assertEqual(assembly.validate(plan, doc), [])


def _post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class RegatherApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nc-regather-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        s = _settings(self.tmp, suppliers="http://127.0.0.1/suppliers.json", llm_base="", llm_model="")
        s.perform_enabled = True
        self.app, self.httpd = _api(s)
        self.addCleanup(_close, self.httpd)
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.prev, files = _previous()
        gather.save_collection(self.app.storage, OLD, self.prev, files)
        real = gather.build_slot
        searchers = _searchers(_pools())

        def build_slot(prev, index, **kwargs):
            kwargs.update(searchers=searchers, verify_live=False, note_maps=[])
            built = real(prev, index, **kwargs)
            built["files"] = _with_bytes(built["files"], 330.0)
            return built

        gather.build_slot = build_slot
        self.addCleanup(lambda: setattr(gather, "build_slot", real))
        # The fixtures name public hosts and the bundled supplier index; the
        # licence gate itself is covered above.
        from nc_perform import runner
        real_check = runner.check_plan
        runner.check_plan = lambda *a, **k: None
        self.addCleanup(lambda: setattr(runner, "check_plan", real_check))

    def test_claim_and_chord_are_checked(self):
        code, body = _post(self.origin + "/api/gather", {"regather_gather_id": OLD, "regather_chord": 1})
        self.assertEqual((code, body.get("error")), (400, "claim_token is required."))
        code, body = _post(self.origin + "/api/gather", {"regather_gather_id": OLD, "regather_chord": 1, "claim_token": "nope"})
        self.assertEqual(code, 403)
        code, body = _post(self.origin + "/api/gather", {"regather_gather_id": OLD, "regather_chord": 99, "claim_token": "old-secret"})
        self.assertEqual(code, 400)
        code, body = _post(self.origin + "/api/gather", {"regather_gather_id": "g-zzzzzzzzzzzz", "regather_chord": 0, "claim_token": "x"})
        self.assertEqual(code, 404)

    def test_regather_returns_a_whole_new_collection(self):
        code, body = _post(self.origin + "/api/gather", {"regather_gather_id": OLD, "regather_chord": 3, "claim_token": "old-secret"})
        self.assertEqual(code, 200, body)
        self.assertNotEqual(body["gather_id"], OLD)
        self.assertTrue(body["claim_token"])
        self.assertEqual(len(body["sounds"]), len(self.prev["sounds"]))
        for i, (a, b) in enumerate(zip(self.prev["sounds"], body["sounds"])):
            if i == 3:
                self.assertNotEqual(a["title"], b["title"])
            else:
                self.assertEqual(a["title"], b["title"])
        doc = gather.load_collection(self.app.storage, body["gather_id"])
        self.assertEqual(doc["regather"]["from"], OLD)
        self.assertEqual(doc["composition"], self.prev["composition"])


if __name__ == "__main__":
    unittest.main()
