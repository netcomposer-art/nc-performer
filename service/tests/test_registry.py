"""A checked sound is not fetched, measured, or credited again."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "service"))

from ncperf import discover, registry  # noqa: E402
from nc_perform.api import build_app, serve  # noqa: E402
from nc_perform.config import Settings  # noqa: E402


URL = "https://musicmatter.art/sounds/example.nc.json"


def _credit() -> dict:
    return {
        "title": "Piano Cm",
        "author": "Ada",
        "license": "CC-BY-SA-4.0",
        "page": URL,
        "chord_symbol": "Cm",
        "bass_pc": 0,
        "audio_url": "https://audio.musicmatter.art/sounds/example.flac",
        "sha256": "abc",
    }


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nc-registry-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "checked-sounds.json")
        self.prev = {
            "NC_REGISTRY": os.environ.get("NC_REGISTRY"),
            "NC_CACHE": os.environ.get("NC_CACHE"),
            "NC_REGISTRY_BACKGROUND": os.environ.get("NC_REGISTRY_BACKGROUND"),
            "NC_REGISTRY_RECHECK_DAYS": os.environ.get("NC_REGISTRY_RECHECK_DAYS"),
        }
        os.environ["NC_REGISTRY"] = self.path
        os.environ["NC_CACHE"] = os.path.join(self.tmp, "cache")
        os.environ["NC_REGISTRY_BACKGROUND"] = "0"
        os.environ["NC_REGISTRY_RECHECK_DAYS"] = "90"
        registry.reset()
        self.addCleanup(self._restore)

    def _restore(self):
        registry.reset()
        for key, value in self.prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_a_hit_does_not_fetch_the_manifest(self):
        registry.remember_ok("musicmatter", URL, _credit())
        calls = []
        real = discover._json
        discover._json = lambda url: calls.append(url)
        try:
            credit = discover._manifest_credit(URL)
        finally:
            discover._json = real
        self.assertEqual(calls, [])
        self.assertEqual(credit["author"], "Ada")
        self.assertEqual(credit["chord_symbol"], "Cm")
        self.assertEqual(credit["bass_pc"], 0)
        self.assertEqual(credit["sha256"], "abc")
        snap = registry.session()
        self.assertEqual(snap["hits"], 1)
        self.assertEqual(snap["checks"], 0)

    def test_rejected_is_skipped_and_not_retried(self):
        registry.remember_rejected("musicmatter", URL, "bad_license", page=URL, title="x", author="y")
        calls = []
        real = discover._json
        discover._json = lambda url: calls.append(url)
        try:
            self.assertIsNone(discover._manifest_credit(URL))
            self.assertIsNone(discover._manifest_credit(URL))
        finally:
            discover._json = real
        self.assertEqual(calls, [])
        self.assertEqual(registry.session()["rejected"], 2)
        self.assertEqual(registry.peek("musicmatter", URL)["reason"], "bad_license")

    def test_a_new_sound_past_the_deadline_is_queued(self):
        calls = []
        real = discover._json
        discover._json = lambda url: calls.append(url)
        token = discover.arm_wall(0)
        try:
            self.assertIsNone(discover._manifest_credit(URL))
        finally:
            discover.reset_wall(token)
            discover._json = real
        self.assertEqual(calls, [])
        self.assertEqual(registry.session()["deferred"], 1)
        pending = registry.load()["pending"]
        self.assertEqual(pending[0]["why"], "new")
        self.assertEqual(pending[0]["supplier"], "musicmatter")

    def test_a_new_sound_with_time_left_is_checked_once(self):
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
                "chord": {"symbol": "Cm", "bass_midi": 48},
            }

        discover._json = fake
        try:
            first = discover._manifest_credit(URL)
            second = discover._manifest_credit(URL)
        finally:
            discover._json = real
        self.assertEqual(calls, [URL])
        self.assertEqual(first["author"], "Ada")
        self.assertEqual(second["sha256"], "abc")
        self.assertEqual(registry.peek("musicmatter", URL)["verdict"], "ok")
        self.assertEqual(registry.session()["checks"], 1)
        self.assertEqual(registry.session()["hits"], 1)

    def test_checker_version_and_fingerprint_recheck(self):
        registry.remember_ok("musicmatter", URL, _credit(), fingerprint={"sha256": "aaa"})
        doc = registry.load()
        key = registry.row_key("musicmatter", URL)
        doc["sounds"][key]["checker_version"] = 0
        action, _entry = registry.consult("musicmatter", URL)
        self.assertEqual(action, registry.CHECK)
        doc["sounds"][key]["checker_version"] = registry.CHECKER_VERSION
        action, _entry = registry.consult("musicmatter", URL, fingerprint={"sha256": "bbb"})
        self.assertEqual(action, registry.CHECK)
        action, entry = registry.consult("musicmatter", URL, fingerprint={"sha256": "aaa"})
        self.assertEqual(action, registry.USE)
        self.assertEqual(entry["verdict"], "ok")

    def test_a_stale_license_stays_ok_and_the_interval_is_a_setting(self):
        registry.remember_ok("musicmatter", URL, _credit())
        key = registry.row_key("musicmatter", URL)
        stale = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 100 * 86400))
        registry.load()["sounds"][key]["license_checked_at"] = stale
        registry.load()["pending"] = []
        action, _entry = registry.consult("musicmatter", URL)
        self.assertEqual(action, registry.USE)
        self.assertEqual(registry.load()["pending"][0]["why"], "license")
        os.environ["NC_REGISTRY_RECHECK_DAYS"] = "400"
        self.assertEqual(registry.recheck_days(), 400)
        registry.load()["pending"] = []
        action, _entry = registry.consult("musicmatter", URL)
        self.assertEqual(action, registry.USE)
        self.assertEqual(registry.load()["pending"], [])

    def test_stats_are_counts_only(self):
        registry.remember_ok("musicmatter", URL, _credit())
        registry.remember_rejected("freesound", "https://freesound.org/s/1/", "silent", page="https://freesound.org/s/1/")
        stats = registry.public_stats()
        self.assertEqual(stats["size"], 2)
        self.assertEqual(stats["ok"], 1)
        self.assertEqual(stats["rejected"], 1)
        self.assertEqual(stats["checker_version"], registry.CHECKER_VERSION)
        self.assertEqual(stats["recheck_days"], 90)
        self.assertNotIn("token", stats)
        blob = json.dumps(stats)
        self.assertNotIn("example.nc.json", blob)

    def test_config_and_health_show_the_counts(self):
        registry.remember_ok("musicmatter", URL, _credit())
        registry.remember_rejected("uiowa", "https://theremin.music.uiowa.edu/x.aiff", "too_short", page="https://theremin.music.uiowa.edu/x.aiff")
        settings = Settings()
        settings.storage = "file://" + os.path.join(self.tmp, "files")
        settings.data_dir = self.tmp
        settings.public_base_url = "http://127.0.0.1"
        settings.bind = "127.0.0.1:0"
        settings.queue_autostart = False
        settings.allowed_hosts = ("127.0.0.1",)
        settings.perform_enabled = True
        settings.instance_name = "nc-perform (self-hosted)"
        os.makedirs(os.path.join(self.tmp, "files"), exist_ok=True)
        app = build_app(settings)
        httpd = serve(app)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with urllib.request.urlopen(base + "/api/config", timeout=5) as resp:
            config = json.load(resp)
        with urllib.request.urlopen(base + "/health", timeout=5) as resp:
            health = json.load(resp)
        self.assertEqual(config["registry"]["size"], 2)
        self.assertEqual(config["registry"]["ok"], 1)
        self.assertEqual(config["registry"]["rejected"], 1)
        self.assertEqual(health["registry"], config["registry"])
        self.assertIs(config["browser_assembly"], True)
        self.assertNotIn("NC_TESTER_TOKEN", json.dumps(config))

    def test_the_worker_seed_matches_the_bundled_registry(self):
        with open(registry.BUNDLED, encoding="utf-8") as fh:
            stats = json.load(fh)["stats"]
        with open(os.path.join(ROOT, "service", "worker", "src", "policy.ts"), encoding="utf-8") as fh:
            policy = fh.read()
        self.assertEqual(stats, {"size": 10334, "ok": 8977, "rejected": 1357})
        self.assertIn("size: 10334", policy)
        self.assertIn("ok: 8977", policy)
        self.assertIn("rejected: 1357", policy)


if __name__ == "__main__":
    unittest.main()
