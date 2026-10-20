"""Gather a collection, then Perform that collection without searching again."""
from __future__ import annotations

import inspect
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from test_discover import COMP, _chords, _searchers  # noqa: E402
from test_service import _api, _close, _settings  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "service"))

from ncperf import discover, fetch, gather, notemap  # noqa: E402
from ncperf import deadline  # noqa: E402
from ncperf.schema import validate  # noqa: E402


def _post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read(), r.headers.get("Content-Type")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), None


def _commons_searchers(prefix="cm"):
    return _searchers({"wikimedia": _chords("wikimedia", "piano", kind="commons", prefix=prefix)})


def _local_plan(seed=1, prefix="cm"):
    plan = discover.plan_for(
        COMP, seed=seed, searchers=_commons_searchers(prefix), verify_live=False, note_maps=[],
    )
    plan.pop("gather_picks", None)
    for src in plan["sources"].values():
        src["url"] = "http://127.0.0.1/audio.ogg"
        src["page"] = "http://127.0.0.1/page"
    plan["from_gather"] = True
    return plan


class GatherKitTest(unittest.TestCase):
    def test_one_shot_keeps_the_20_second_budget(self):
        params = inspect.signature(discover.plan_for).parameters
        self.assertEqual(params["origin_budget_s"].default, 20)
        self.assertEqual(params["texture_budget_s"].default, 20)
        self.assertIsNone(params["note_budget_s"].default)
        self.assertEqual(gather.ORIGIN_S, 4.0)
        self.assertEqual(gather.NOTE_S, 4.0)

    def test_gather_passes_the_short_deadline(self):
        seen = {}
        real = gather.plan_for

        def wrapped(comp, **kwargs):
            seen["origin"] = kwargs.get("origin_budget_s")
            seen["texture"] = kwargs.get("texture_budget_s")
            seen["note"] = kwargs.get("note_budget_s")
            return real(comp, **kwargs)

        gather.plan_for = wrapped
        try:
            built = gather.build(
                COMP, seed=1, verify_live=False, note_maps=[], searchers=_commons_searchers(),
                processed=True,
            )
        finally:
            gather.plan_for = real
        self.assertEqual(seen["origin"], 4.0)
        self.assertEqual(seen["texture"], 4.0)
        self.assertEqual(seen["note"], 4.0)
        self.assertTrue(built["plan"].get("from_gather"))
        self.assertNotIn("gather_picks", built["plan"])
        self.assertTrue(built["picked_keys"])

    def test_avoid_drops_the_previous_keys_and_retries_when_nothing_is_left(self):
        plan = discover.plan_for(
            COMP, seed=1, searchers=_commons_searchers("cm"), verify_live=False, note_maps=[],
        )
        picks = list(plan["gather_picks"])
        self.assertTrue(picks)
        with self.assertRaises(ValueError):
            discover.plan_for(
                COMP, seed=1, searchers=_commons_searchers("cm"), verify_live=False, note_maps=[],
                avoid_keys=picks,
            )
        both = _searchers({
            "wikimedia": _chords("wikimedia", "piano", kind="commons", prefix="cm"),
            "ccmixter": _chords("ccmixter", "piano", kind="commons", prefix="mx"),
        })
        kept = discover.plan_for(
            COMP, seed=1, searchers=both, verify_live=False, note_maps=[], avoid_keys=picks,
        )
        self.assertEqual(validate(kept), [])
        self.assertFalse(set(picks) & set(kept["gather_picks"]))

        calls = []
        real = gather.plan_for

        def wrapped(comp, **kwargs):
            calls.append(kwargs.get("avoid_keys"))
            if kwargs.get("avoid_keys"):
                raise ValueError("No exact chord recording or native-pitch notes for: Cm")
            return real(comp, **kwargs)

        gather.plan_for = wrapped
        try:
            built = gather.build(
                COMP, seed=1, verify_live=False, note_maps=[], searchers=_commons_searchers(),
                avoid_keys=["cm-Cm"], processed=True,
            )
        finally:
            gather.plan_for = real
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], None)
        self.assertTrue(built["plan"]["layers"])

    def test_stalled_suppliers_share_one_deadline(self):
        def stall(*_args):
            while True:
                deadline.check()
                time.sleep(0.01)

        fns = _commons_searchers()
        for origin in ("freesound", "openverse", "internet_archive", "ccmixter", "musicmatter"):
            fns[origin] = stall
        started = time.perf_counter()
        plan = discover.plan_for(
            COMP, seed=1, searchers=fns, verify_live=False, note_maps=[],
            origin_budget_s=0.35, texture_budget_s=0.35,
        )
        self.assertLess(time.perf_counter() - started, 1.2)
        self.assertTrue(plan["layers"])

    def test_note_budget_stops_a_file_that_is_still_waiting(self):
        token = notemap.arm_budget(0.05)
        try:
            time.sleep(0.08)
            with self.assertRaises(deadline.SearchTimeout):
                notemap._file_budget()
        finally:
            notemap.reset_budget(token)
        self.assertEqual(notemap._file_budget(), float(notemap.NOTE_HTTP_S))

    def test_supplier_index_is_read_from_disk_the_second_time(self):
        tmp = tempfile.mkdtemp(prefix="nc-index-")
        self.addCleanup(shutil.rmtree, tmp, True)
        previous = os.environ.get("NC_CACHE")
        os.environ["NC_CACHE"] = tmp
        self.addCleanup(lambda: os.environ.__setitem__("NC_CACHE", previous) if previous else os.environ.pop("NC_CACHE", None))
        discover._MM_CACHE.clear()
        calls = []
        real = discover._json

        def fake(url):
            calls.append(url)
            if url.endswith("suppliers.json"):
                return {"suppliers": [{
                    "status": "active", "name": "Example",
                    "index": "https://example.test/index.json",
                }]}
            return {"offerings": [{"id": "one", "title": "Piano Cm"}]}

        discover._json = fake
        try:
            first = discover._mm_index("https://example.test/suppliers.json", host_ok_fn=lambda _h: True)
            self.assertEqual(first[0]["id"], "one")
            discover._MM_CACHE.clear()
            before = len(calls)
            second = discover._mm_index("https://example.test/suppliers.json", host_ok_fn=lambda _h: True)
            self.assertEqual(second[0]["title"], "Piano Cm")
            self.assertEqual(len(calls), before)
        finally:
            discover._json = real
            discover._MM_CACHE.clear()

    def test_restore_makes_the_download_a_cache_hit(self):
        tmp = tempfile.mkdtemp(prefix="nc-seed-")
        self.addCleanup(shutil.rmtree, tmp, True)
        previous = os.environ.get("NC_CACHE")
        os.environ["NC_CACHE"] = os.path.join(tmp, "cache")
        self.addCleanup(lambda: os.environ.__setitem__("NC_CACHE", previous) if previous else os.environ.pop("NC_CACHE", None))
        from nc_perform.storage import FileStorage
        storage = FileStorage(os.path.join(tmp, "files"))
        url = "https://example.test/kept.ogg"
        gather.save_collection(storage, "g-0123456789ab", {
            "id": "g-0123456789ab",
            "seed": 3,
            "claim_hash": "abc",
            "sounds": [],
        }, [{
            "sid": "n0",
            "url": url,
            "ext": ".ogg",
            "range_bytes": None,
            "data": b"OggS-cached",
        }])
        path = fetch.cache_path(url, None, ".ogg")
        if os.path.exists(path):
            os.remove(path)
        self.assertEqual(gather.restore_cache(storage, "g-0123456789ab"), 1)

        def boom(*_args, **_kwargs):
            raise AssertionError("download should use the gathered file")

        real = fetch.http
        fetch.http = boom
        try:
            got = fetch.download(url, ext=".ogg")
        finally:
            fetch.http = real
        with open(got, "rb") as fh:
            self.assertEqual(fh.read(), b"OggS-cached")


class GatherApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nc-gather-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        s = _settings(self.tmp, suppliers="http://127.0.0.1/suppliers.json", llm_base="", llm_model="")
        s.perform_enabled = True
        self.plan = _local_plan()
        self.sid = next(iter(self.plan["sources"]))
        self.calls = []
        self._real_build = gather.build
        gather.build = self._build
        self.app, self.httpd = _api(s)
        self.addCleanup(_close, self.httpd)
        self.addCleanup(lambda: setattr(gather, "build", self._real_build))
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _build(self, comp, **kwargs):
        self.calls.append(kwargs)
        plan = json.loads(json.dumps(self.plan))
        plan["seed"] = int(kwargs.get("seed") or plan["seed"])
        return {
            "plan": plan,
            "files": [{
                "sid": self.sid,
                "url": "https://example.test/preview.ogg",
                "ext": ".ogg",
                "range_bytes": None,
                "data": b"OggS-preview",
            }],
            "picked_keys": ["cm-Cm", "http://127.0.0.1/audio.ogg"],
            "picked_sample_ids": ["http://127.0.0.1/audio.ogg"],
        }

    def _body(self, **extra):
        body = {
            "title": "Glass garden",
            "composer": "anonymous",
            "composition": COMP["composition"],
            "performance": COMP["performance"],
            "seed": 11,
        }
        body.update(extra)
        return body

    def test_gather_returns_the_collection_and_a_preview(self):
        code, body = _post(self.origin + "/api/gather", self._body())
        self.assertEqual(code, 200, body)
        self.assertRegex(body["gather_id"], r"^g-[0-9abcdefghjkmnpqrstvwxyz]{12}$")
        self.assertEqual(body["id"], body["gather_id"])
        self.assertTrue(body["claim_token"])
        self.assertEqual(body["seed"], 11)
        self.assertIsNone(body["excluded_gather_id"])
        self.assertEqual(body["poll"], f"/api/jobs/{body['gather_id']}")
        self.assertGreaterEqual(len(body["sounds"]), 1)
        sound = next(row for row in body["sounds"] if row["id"] == self.sid)
        for key in ("id", "title", "supplier", "license", "preview_url", "page"):
            self.assertIn(key, sound)
        self.assertTrue(sound["preview_url"].endswith(f"/audio/{sound['id']}"))
        self.assertEqual(self.app.store.running_count(), 0)
        status, raw, _ctype = _get(self.origin + body["poll"])
        self.assertEqual(status, 200)
        page = json.loads(raw)
        self.assertEqual(page["state"], "gathered")
        self.assertEqual(page["stage"], "gathered")
        self.assertEqual(page["stage_line"], "Gathered.")
        self.assertEqual(page["gather_id"], body["gather_id"])
        self.assertEqual(page["sounds"][0]["title"], sound["title"])
        self.assertEqual(page["found_sounds"][0]["supplier"], sound["supplier"])
        self.assertNotIn("preview_url", page["found_sounds"][0])
        audio, data, ctype = _get(self.origin + "/api/" + sound["preview_url"].split("/api/", 1)[1])
        self.assertEqual(audio, 200, data)
        self.assertEqual(data, b"OggS-preview")
        self.assertIn("audio/ogg", ctype)

    def test_gather_again_excludes_the_previous_collection(self):
        code, first = _post(self.origin + "/api/gather", self._body())
        self.assertEqual(code, 200, first)
        missing, err = _post(self.origin + "/api/gather", self._body(exclude_gather_id=first["gather_id"]))
        self.assertEqual(missing, 400, err)
        self.assertEqual(err, {"error": "claim_token is required.", "code": "rejected"})
        wrong, err = _post(self.origin + "/api/gather", self._body(
            exclude_gather_id=first["gather_id"], claim_token="nope",
        ))
        self.assertEqual(wrong, 403, err)
        self.assertEqual(err["error"], "claim_token does not match this Gather.")
        code, second = _post(self.origin + "/api/gather", self._body(
            exclude_gather_id=first["gather_id"], claim_token=first["claim_token"], seed=12,
        ))
        self.assertEqual(code, 200, second)
        self.assertNotEqual(second["gather_id"], first["gather_id"])
        self.assertNotEqual(second["claim_token"], first["claim_token"])
        self.assertEqual(second["excluded_gather_id"], first["gather_id"])
        self.assertEqual(second["seed"], 12)
        self.assertIn("cm-Cm", self.calls[-1]["avoid_keys"])

    def test_perform_uses_the_kept_collection_and_one_shot_still_searches(self):
        code, gathered = _post(self.origin + "/api/gather", self._body())
        self.assertEqual(code, 200, gathered)
        code, denied = _post(self.origin + "/api/perform", {"gather_id": gathered["gather_id"]})
        self.assertEqual(code, 400, denied)
        self.assertEqual(denied, {"error": "claim_token is required.", "code": "rejected"})
        code, perf = _post(self.origin + "/api/perform", {
            "gather_id": gathered["gather_id"],
            "claim_token": gathered["claim_token"],
            "visibility": "unlisted",
            "composition": "different words must not start a new search",
        })
        self.assertEqual(code, 202, perf)
        self.assertRegex(perf["id"], r"^h-")
        self.assertNotEqual(perf["claim_token"], gathered["claim_token"])
        job = self.app.store.get(perf["id"])
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["request"]["gather_id"], gathered["gather_id"])
        self.assertEqual(job["request"]["plan"]["sources"], self.plan["sources"])
        self.assertNotIn("different words", job["request"]["composition"]["composition"])
        self.assertEqual(job["request"]["plan"].get("from_gather"), True)

        code, plain = _post(self.origin + "/api/perform", self._body())
        self.assertEqual(code, 202, plain)
        plain_job = self.app.store.get(plain["id"])
        self.assertIsNone(plain_job["request"]["plan"])
        self.assertIsNone(plain_job["request"]["gather_id"])

    def test_cancel_removes_the_collection(self):
        code, body = _post(self.origin + "/api/gather", self._body())
        self.assertEqual(code, 200, body)
        self.assertTrue(self.app.storage.get(f"gathers/{body['gather_id']}/collection.json"))
        code, page = _post(self.origin + f"/api/jobs/{body['gather_id']}/cancel", {"claim_token": body["claim_token"]})
        self.assertEqual(code, 200, page)
        self.assertEqual(page["state"], "cancelled")
        self.assertEqual(page["error"], "Stopped.")
        self.assertEqual(page["stage_line"], "Stopped.")
        self.assertEqual(self.app.store.running_count(), 0)
        self.assertFalse(self.app.storage.list(f"gathers/{body['gather_id']}/"))
        again, page = _post(self.origin + f"/api/jobs/{body['gather_id']}/cancel", {"claim_token": body["claim_token"]})
        self.assertEqual(again, 200, page)
        self.assertEqual(page["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
