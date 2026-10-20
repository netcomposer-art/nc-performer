"""Stop a Perform. The claim token is the same secret Keep uses."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from test_service import _api, _close, _settings  # noqa: E402


def _post(url, body):
    req = urllib.request.Request(
        url, json.dumps(body).encode(), {"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class CancelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nc-cancel-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        s = _settings(self.tmp, suppliers="http://127.0.0.1/suppliers.json", llm_base="", llm_model="")
        s.perform_enabled = True
        self.app, self.httpd = _api(s)
        self.addCleanup(_close, self.httpd)
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _perform(self):
        code, body = _post(self.origin + "/api/perform", {
            "composition": "1 min. Cm.",
            "performance": "soft",
            "title": "Stop",
        })
        self.assertEqual(code, 202, body)
        return body["id"], body["claim_token"]

    def _cancel(self, job_id, body):
        return _post(self.origin + f"/api/jobs/{job_id}/cancel", body)

    def test_queued_cancel_drops_the_job_and_is_idempotent(self):
        job_id, token = self._perform()
        self.assertEqual(self.app.store.queued_count(), 1)
        code, body = self._cancel(job_id, {"claim_token": token})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["id"], job_id)
        self.assertEqual(body["state"], "cancelled")
        self.assertEqual(body["stage"], "cancelled")
        self.assertEqual(body["percent"], 100)
        self.assertIsNone(body["position"])
        self.assertEqual(body["error"], "Stopped.")
        self.assertEqual(body["stage_line"], "Stopped.")
        self.assertIsInstance(body["seed"], int)
        self.assertIsNone(body["trimmed"])
        self.assertEqual(body["found_sounds"], [])
        self.assertNotIn("files", body)
        self.assertEqual(self.app.store.queued_count(), 0)
        self.assertEqual(self.app.store.running_count(), 0)
        self.assertEqual(self.app.runner.pump_available(), 0)
        self.assertEqual(self.app.store.get(job_id)["state"], "cancelled")
        again_code, again = self._cancel(job_id, {"claim_token": token})
        self.assertEqual(again_code, 200)
        self.assertEqual(again["state"], "cancelled")
        self.assertEqual(again["error"], "Stopped.")
        self.assertNotIn("files", again)
        status = json.loads(self.app.storage.get(f"jobs/{job_id}/status.json"))
        self.assertEqual(status["state"], "cancelled")
        self.assertEqual(status["error"], "Stopped.")
        self.assertEqual(status["stage_line"], "Stopped.")

    def test_cancel_removes_partial_files_and_the_record_alias(self):
        job_id, token = self._perform()
        self.app.store.update(job_id, state="planning", stage="planning", percent=20)
        digest = "ab" * 32
        self.app.storage.put(
            f"performances/{job_id}/record.json",
            json.dumps({"record_hash": {"value": digest}}).encode(),
            "application/json",
        )
        self.app.storage.put(f"aliases/sha256/{digest}", job_id.encode(), "text/plain")
        self.app.storage.put(f"performances/{job_id}/preview.mp3", b"partial-mp3", "audio/mpeg")
        self.assertEqual(self.app.store.running_count(), 1)
        code, body = self._cancel(job_id, {"claim_token": "  " + token + "  "})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["state"], "cancelled")
        self.assertIsNone(self.app.storage.get(f"performances/{job_id}/preview.mp3"))
        self.assertIsNone(self.app.storage.get(f"performances/{job_id}/record.json"))
        self.assertIsNone(self.app.storage.get(f"aliases/sha256/{digest}"))
        self.assertEqual(_get(self.origin + f"/api/jobs/{job_id}/files/preview.mp3")[0], 404)
        self.assertEqual(_get(self.origin + f"/records/{job_id}.json")[0], 404)
        self.assertEqual(self.app.store.running_count(), 0)
        self.app.store.update(job_id, state="planning", stage="fetching", percent=30, error=None)
        self.assertEqual(self.app.store.get(job_id)["state"], "cancelled")

    def test_done_failed_and_rejected_are_left_alone(self):
        for state in ("done", "failed", "rejected"):
            job_id, token = self._perform()
            self.app.store.update(job_id, state=state, stage=state, percent=100, error="kept" if state != "done" else None)
            self.app.storage.put(f"performances/{job_id}/preview.mp3", b"finished", "audio/mpeg")
            code, body = self._cancel(job_id, {"claim_token": token})
            self.assertEqual(code, 200, body)
            self.assertEqual(body["state"], state)
            self.assertEqual(self.app.storage.get(f"performances/{job_id}/preview.mp3"), b"finished")
            if state == "done":
                self.assertIn("files", body)
                self.assertIn("preview.mp3", body["files"]["mp3"])
            else:
                self.assertNotIn("files", body)
                self.assertEqual(body["error"], "kept")

    def test_keep_token_is_the_same_secret_and_claim_wins(self):
        job_id, token = self._perform()
        code, body = self._cancel(job_id, {"keep_token": token})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["state"], "cancelled")
        other, other_token = self._perform()
        code, body = self._cancel(other, {"claim_token": "nope", "keep_token": other_token})
        self.assertEqual(code, 403, body)
        self.assertEqual(body, {"error": "claim_token does not match this Performance."})
        self.assertEqual(self.app.store.get(other)["state"], "queued")

    def test_auth_errors(self):
        job_id, _token = self._perform()
        code, body = self._cancel(job_id, {})
        self.assertEqual(code, 400, body)
        self.assertEqual(body, {"error": "claim_token is required.", "code": "rejected"})
        code, body = self._cancel(job_id, {"claim_token": "wrong"})
        self.assertEqual(code, 403)
        self.assertEqual(body["error"], "claim_token does not match this Performance.")
        self.assertEqual(self.app.store.get(job_id)["state"], "queued")
        code, body = self._cancel("h-abcdefghjkmn", {"claim_token": "wrong"})
        self.assertEqual(code, 404)
        self.assertEqual(body, {"error": "not found"})

    def test_a_running_search_stops(self):
        from ncperf import deadline

        job_id, token = self._perform()
        started = threading.Event()

        def hang(_job, _stage_t, _usage):
            started.set()
            while True:
                deadline.check()
                time.sleep(0.01)

        self.app.runner._run = hang
        thread = threading.Thread(target=self.app.runner.run_job, args=(job_id,), daemon=True)
        thread.start()
        self.assertTrue(started.wait(2))
        self.app.storage.put(f"performances/{job_id}/preview.mp3", b"partial", "audio/mpeg")
        code, body = self._cancel(job_id, {"claim_token": token})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["state"], "cancelled")
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.app.store.get(job_id)["state"], "cancelled")
        self.assertIsNone(self.app.storage.get(f"performances/{job_id}/preview.mp3"))

    def test_stop_is_not_swallowed_by_a_search(self):
        from ncperf import cancel, deadline, discover

        event = threading.Event()
        event.set()
        token = cancel.bind(event)

        def stall(*_args):
            try:
                deadline.check()
            except Exception:
                return []
            return []

        try:
            with self.assertRaises(cancel.Cancelled):
                discover.plan_for(
                    {"title": "Stop", "composition": "1 min. Cm.", "performance": "soft"},
                    seed=1,
                    searchers={"freesound": stall},
                    verify_live=False,
                )
        finally:
            cancel.reset(token)


if __name__ == "__main__":
    unittest.main()
