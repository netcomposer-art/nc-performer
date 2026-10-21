"""Gather reports finer progress: finished units out of a known total, per step.

The page reads progress (0-100), progress_stage, progress_done and
progress_total from the job payload. It never goes down, it is under 100
until the Gather is finished, and it moves while the Gather runs, not only at
the end.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import threading
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

from ncperf import assemble, assembly, fetch, gather  # noqa: E402
from ncperf.progress import GATHER_CONTAINER_END, GatherMeter  # noqa: E402


def _wav(freq=220.0, seconds=0.6) -> bytes:
    n = int(seconds * 48000)
    t = np.linspace(0, seconds, n, endpoint=False)
    y = (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, np.column_stack([y, y]), 48000, format="WAV", subtype="PCM_16")
    return buf.getvalue()


class Steps:
    """Records step_cb calls, and the meter's public fields after each."""

    def __init__(self):
        self.calls = []
        self.fields = []
        self.meter = GatherMeter(self.fields.append)

    def __call__(self, step, done=None, total=None):
        self.calls.append((step, done, total))
        self.meter.step_cb(step, done, total)


def _monotonic(test, values):
    for a, b in zip(values, values[1:]):
        test.assertLessEqual(a, b, values)


class MeterTest(unittest.TestCase):
    def test_units_out_of_a_known_total_never_back_never_100(self):
        seen = []
        m = GatherMeter(seen.append)
        m.begin("searching", 4)
        for _ in range(4):
            m.tick("searching")
        m.begin("picking", 5)
        for _ in range(3):
            m.tick("picking")
        m.tick("searching")          # late thread from an earlier step
        m.begin("searching", 9)      # an earlier step cannot restart
        m.begin("downloading", 7)
        for _ in range(10):          # more ticks than units
            m.tick("downloading")
        m.begin("copying", 5)
        m.finish("copying")
        m.begin("storing", 6)
        m.finish("storing")
        values = [f["progress"] for f in seen]
        _monotonic(self, values)
        self.assertEqual(values[-1], GATHER_CONTAINER_END)
        self.assertTrue(all(v < 100 for v in values))
        self.assertGreater(len(set(values)), 10)
        last = seen[-1]
        self.assertEqual((last["progress_stage"], last["progress_done"], last["progress_total"]), ("storing", 6, 6))
        downloading = [f for f in seen if f["progress_stage"] == "downloading"]
        self.assertEqual(downloading[-1]["progress_done"], 7)

    def test_threads_cannot_pull_it_back(self):
        seen = []
        lock = threading.Lock()

        def emit(fields):
            with lock:
                seen.append(fields["progress"])

        m = GatherMeter(emit)
        m.begin("downloading", 400)
        threads = [threading.Thread(target=lambda: [m.tick("downloading") for _ in range(50)]) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        _monotonic(self, seen)
        self.assertEqual(m.fields()["progress_done"], 400)


class KitStepsTest(unittest.TestCase):
    def test_search_and_pick_count_suppliers_then_chords(self):
        steps = Steps()
        pools = {"musicmatter": _chords("musicmatter", "choir", kind="commons", prefix="choir")}
        plan = assemble.plan_raw(COMP, seed=1, searchers=_searchers(pools), verify_live=False, note_maps=[], step_cb=steps)
        begins = [(s, t) for s, d, t in steps.calls if t is not None]
        self.assertEqual(begins[0][0], "searching")
        self.assertEqual(begins[0][1], 1 + len(_searchers(pools)))
        self.assertEqual(begins[1], ("picking", len(plan["clips"])))
        ticks = [s for s, d, t in steps.calls if t is None and d is None]
        # A slow supplier can still be out when the pick starts; the step closes anyway.
        self.assertLessEqual(ticks.count("searching"), begins[0][1])
        searching = [f for f in steps.fields if f["progress_stage"] == "searching"]
        self.assertEqual(searching[-1]["progress_done"], begins[0][1])
        self.assertEqual(ticks.count("picking"), len(plan["clips"]))
        values = [f["progress"] for f in steps.fields]
        _monotonic(self, values)
        self.assertEqual(steps.fields[-1]["progress_stage"], "picking")
        self.assertEqual(steps.fields[-1]["progress_done"], len(plan["clips"]))

    def test_download_and_copy_count_each_file_and_each_clip_including_a_note_stack(self):
        def source(title, url):
            return {"kind": "commons", "url": url, "page": "http://127.0.0.1/p", "license": "CC0-1.0", "title": title, "author": "Ada"}

        plan = {
            "assembly": "raw",
            "sources": {
                "c0": source("Cm", "http://127.0.0.1/c0.wav"),
                "c1n0": source("note 50", "http://127.0.0.1/g.aif#note=50"),
                "c1n1": source("note 57", "http://127.0.0.1/g.aif#note=57"),
                "c1n2": source("note 65", "http://127.0.0.1/g.aif#note=65"),
            },
            "clips": [
                {"id": "c0", "chord": 0, "sids": ["c0"], "symbol": "Cm"},
                {"id": "c1", "chord": 1, "sids": ["c1n0", "c1n1", "c1n2"], "symbol": "F"},
            ],
        }
        tmp = tempfile.mkdtemp(prefix="nc-progress-")
        self.addCleanup(shutil.rmtree, tmp, True)
        real = fetch.resolve

        def resolve(src):
            path = os.path.join(tmp, str(abs(hash(src["url"]))) + ".wav")
            with open(path, "wb") as fh:
                fh.write(_wav())
            return {**src, "path": path, "audio_url": src["url"]}

        fetch.resolve = resolve
        self.addCleanup(lambda: setattr(fetch, "resolve", real))
        steps = Steps()
        steps.meter.begin("picking", 2)
        steps.meter.finish("picking")
        files, meta = assembly.materialize(plan, gather.source_rows(plan), step_cb=steps)
        self.assertIn("c1", meta)
        begins = [(s, t) for s, d, t in steps.calls if t is not None]
        self.assertEqual(begins, [("downloading", 4), ("copying", 2)])
        ticks = [s for s, d, t in steps.calls if t is None and d is None]
        self.assertEqual(ticks.count("downloading"), 4)
        self.assertEqual(ticks.count("copying"), 2)
        storing = steps
        mark = len(steps.calls)
        from nc_perform.storage import FileStorage
        gather.save_collection(FileStorage(os.path.join(tmp, "files")), "g-0123456789ab", {"id": "g-0123456789ab", "plan": plan, "sounds": []}, files, step_cb=storing)
        stored = [d for s, d, t in storing.calls[mark:] if s == "storing" and t is None and d is None]
        with_data = sum(1 for row in files if row.get("data"))
        self.assertEqual(len(stored), with_data)
        self.assertEqual(storing.fields[-1]["progress"], GATHER_CONTAINER_END)
        _monotonic(self, [f["progress"] for f in steps.fields])


class GatherJobProgressTest(unittest.TestCase):
    """A whole Gather on the service: the job row moves while it runs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nc-gather-progress-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        s = _settings(self.tmp, suppliers="http://127.0.0.1/suppliers.json", llm_base="", llm_model="")
        s.perform_enabled = True
        real_build, real_resolve = gather.build, fetch.resolve
        pools = {"wikimedia": _chords("wikimedia", "piano", kind="commons", prefix="cm")}

        def build(comp, **kwargs):
            kwargs.update(searchers=_searchers(pools), verify_live=False, note_maps=[], host_ok_fn=None)
            out = real_build(comp, **kwargs)
            for src in out["plan"]["sources"].values():
                src["url"] = "http://127.0.0.1/audio.wav"
                src["page"] = "http://127.0.0.1/page"
            for row in out["files"]:
                row["url"] = "http://127.0.0.1/audio.wav"
            return out

        def resolve(src):
            fd, path = tempfile.mkstemp(suffix=".wav", dir=self.tmp)
            with os.fdopen(fd, "wb") as fh:
                fh.write(_wav())
            return {**src, "path": path, "audio_url": src.get("url")}

        gather.build, fetch.resolve = build, resolve
        self.addCleanup(lambda: setattr(gather, "build", real_build))
        self.addCleanup(lambda: setattr(fetch, "resolve", real_resolve))
        self.app, self.httpd = _api(s)
        self.addCleanup(_close, self.httpd)
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.rows = []
        real_update = self.app.store.update

        def update(job_id, **fields):
            real_update(job_id, **fields)
            job = self.app.store.get(job_id) or {}
            result = job.get("result") or {}
            if "progress" in result:
                self.rows.append((job.get("state"), result["progress"], result.get("progress_stage"), result.get("progress_done"), result.get("progress_total")))

        self.app.store.update = update

    def test_the_job_reports_each_unit_and_reaches_100_only_when_gathered(self):
        body = {"title": "Glass garden", "composer": "anonymous", "composition": COMP["composition"], "performance": COMP["performance"], "seed": 11}
        req = urllib.request.Request(self.origin + "/api/gather", json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                got = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            self.fail(exc.read().decode())
        self.assertIn("gather_id", got, got)
        values = [row[1] for row in self.rows]
        _monotonic(self, values)
        self.assertTrue(all(v < 100 for v in values), values)
        stages = [row[2] for row in self.rows]
        for name in ("searching", "picking", "downloading", "copying", "storing"):
            self.assertIn(name, stages, stages)
        # Many updates while it ran, not one at the end.
        self.assertGreaterEqual(len(set(values)), 8, values)
        running = [row for row in self.rows if row[0] not in ("gathered", "done")]
        self.assertEqual({row[2] for row in running}, {"searching", "picking", "downloading", "copying", "storing"})
        for row in self.rows:
            self.assertLessEqual(row[3], row[4], row)
        gid = got["gather_id"]
        with urllib.request.urlopen(self.origin + f"/api/jobs/{gid}", timeout=10) as r:
            page = json.loads(r.read())
        self.assertEqual(page["progress"], 100)
        self.assertEqual(page["progress_stage"], "done")
        self.assertEqual(page["percent"], 100)
        self.assertIn("found_sounds", page)
        job = self.app.store.get(gid)
        self.assertEqual(job["result"]["progress"], GATHER_CONTAINER_END)


if __name__ == "__main__":
    unittest.main()
