"""The warm container's NC_PUBLIC_BASE_URL must not leak localhost into clip URLs."""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "service"))

from nc_perform.config import Settings  # noqa: E402
from nc_perform.runner import Runner, request_base  # noqa: E402
from ncperf.progress import with_preview  # noqa: E402

WORKER = "https://nc-perform-api.psemme.workers.dev"


class PublicBaseTest(unittest.TestCase):
    def test_request_base_accepts_an_origin_only(self):
        self.assertEqual(request_base({"public_base": WORKER + "/"}), WORKER)
        self.assertEqual(request_base({"public_base": "http://127.0.0.1:8787"}), "http://127.0.0.1:8787")
        for junk in (None, "", "localhost", "javascript:alert(1)", WORKER + "/x y", 42):
            self.assertEqual(request_base({"public_base": junk}), "")
        self.assertEqual(request_base(None), "")

    def test_job_base_wins_over_the_container_default(self):
        settings = Settings()
        self.assertEqual(settings.public_base_url, "http://localhost:8080")
        runner = Runner(settings, store=None, storage=None, signer=None)
        job = runner._job_settings("g-0123456789ab", {"public_base": WORKER})
        self.assertEqual(job.public_base_url, WORKER)
        self.assertEqual(settings.public_base_url, "http://localhost:8080", "the shared settings stay as they were")
        # Later helpers for the same job only know the id.
        self.assertEqual(runner._job_settings("g-0123456789ab").public_base_url, WORKER)
        self.assertIs(runner._job_settings("g-other0000000"), settings)
        sound = with_preview({"id": "c0", "chord": 0, "symbol": "Dm9", "title": "guitar Dm9", "supplier": "the University of Iowa", "license": "PD"}, "g-0123456789ab", job.public_base_url)
        url = sound.get("preview_url") or sound.get("url") or ""
        self.assertTrue(url.startswith(WORKER + "/api/jobs/g-0123456789ab/audio/"), url)
        self.assertNotIn("localhost", url)


if __name__ == "__main__":
    unittest.main()
