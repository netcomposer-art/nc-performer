"""Search budgets: one slow reply is skipped, and a budget is not retried."""
from __future__ import annotations

import os
import socket
import sys
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from ncperf import deadline, fetch, notemap


class DeadlineTest(unittest.TestCase):
    def test_http_inside_a_budget_does_not_retry(self):
        calls = []

        def slow(req, timeout=None):
            calls.append(timeout)
            raise socket.timeout("timed out")

        orig = urllib.request.urlopen
        urllib.request.urlopen = slow
        try:
            started = time.perf_counter()
            with deadline.limit(0.3):
                with self.assertRaises(deadline.SearchTimeout):
                    fetch.http("https://example.invalid/note.aiff", timeout=600, tries=3)
            elapsed = time.perf_counter() - started
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(len(calls), 1)
        self.assertLess(elapsed, 2)

    def test_a_download_without_a_budget_keeps_its_timeout(self):
        seen = {}

        class Resp:
            def __init__(self):
                self._sent = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, n=-1):
                if self._sent:
                    return b""
                self._sent = True
                return b"ok"

            def geturl(self):
                return "https://example.invalid/x"

        def ok(req, timeout=None):
            seen["timeout"] = timeout
            return Resp()

        orig = urllib.request.urlopen
        urllib.request.urlopen = ok
        try:
            body, url = fetch.http("https://example.invalid/x", timeout=600, tries=3)
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(body, b"ok")
        self.assertEqual(url, "https://example.invalid/x")
        self.assertGreater(seen["timeout"], 590)
        self.assertLessEqual(seen["timeout"], 600)

    def test_a_slow_note_page_is_skipped(self):
        spec = {
            "id": "uiowa-piano",
            "origin": "uiowa",
            "instrument": "piano",
            "pluck": False,
            "kind": "uiowa_mis",
            "lo": 60,
            "hi": 60,
            "page": "https://example.invalid/piano",
            "file_re": "Piano",
            "author": "A",
            "license": "PD-equivalent",
        }
        mp = notemap._blank(spec)

        def slow(_page):
            while True:
                deadline.check()
                time.sleep(0.01)

        orig_files = notemap._uiowa_files
        orig_budget = notemap.NOTE_HTTP_S
        notemap._uiowa_files = slow
        notemap.NOTE_HTTP_S = 0.2
        try:
            started = time.perf_counter()
            notemap._fill_uiowa(spec, mp, {60})
            elapsed = time.perf_counter() - started
        finally:
            notemap._uiowa_files = orig_files
            notemap.NOTE_HTTP_S = orig_budget
        self.assertLess(elapsed, 2)
        self.assertTrue(any("timed out" in err for err in mp.get("errors") or []))
        self.assertEqual(mp["notes"], {})


if __name__ == "__main__":
    unittest.main()
