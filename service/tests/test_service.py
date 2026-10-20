"""Service tests, including one end-to-end Perform of a sample Composition.

The kit really interprets (against a local mock of the xAI OpenAI-compatible
API) and really renders. Storage is a local stand-in for R2 that speaks the
same S3 calls the service uses for Cloudflare R2. The queue is the service's
own SQLite queue, pumped on this thread so the test does not race a worker.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import wave
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "service"))

from ncperf.schema import validate
from nc_perform.config import Settings, load, tester_pass
from nc_perform.jobs import Store
from nc_perform.runner import Runner
from nc_perform.ids import ID_RE, new_id
from nc_perform.limits import Reject, check_plan
from nc_perform.planner import needs_llm
from nc_perform.runner import egress_guard
from nc_perform.sign import Signer, verify_signature
from nc_perform.storage import S3Storage, sign_v4
from nc_perform.api import build_app, serve


def _wav(path: str, seconds: float = 2.0, freq: float = 440.0, amp: float = 0.35) -> str:
    import math
    rate = 48000
    n = int(seconds * rate)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            v = int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))
            frames += int(v).to_bytes(2, "little", signed=True)
        w.writeframes(frames)
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return


def _close(httpd: ThreadingHTTPServer) -> None:
    # shutdown() waits on the server thread and deadlocks if that thread has
    # already exited. Closing the socket is enough for these tests.
    try:
        httpd.server_close()
    except Exception:
        pass


def _api(settings):
    """Build the app and accept HTTP on an ephemeral port."""
    from nc_perform.api import build_app, serve
    app = build_app(settings)
    httpd = serve(app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return app, httpd


def _serve(handler) -> tuple[ThreadingHTTPServer, str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address[:2]
    return httpd, f"http://{host}:{port}"


class ServiceTest(unittest.TestCase):
    def test_ids_and_llm_choice(self):
        ident = new_id("h")
        self.assertRegex(ident, r"^h-[0-9abcdefghjkmnpqrstvwxyz]{12}$")
        self.assertIsNone(ID_RE.match("h-ILLEGAL00000"))  # I, L, O, U are not Crockford
        glass = open(os.path.join(ROOT, "examples", "glass-garden.composition.md"), encoding="utf-8").read()
        self.assertFalse(needs_llm(glass))
        self.assertTrue(needs_llm("8 seconds. Cm. A melody line in triplets across two sections."))
        self.assertTrue(needs_llm("Verse then chorus, two sections."))
        self.assertFalse(needs_llm("online chords, Cm Am F, slow"))
        from nc_perform.api import choose_seed
        from nc_perform.planner import llm_price_card, wants_model
        self.assertEqual(choose_seed({"seed": 7}), 7)
        self.assertGreaterEqual(choose_seed({}), 1)
        bare = Settings()
        self.assertFalse(bare.llm_every_perform)
        self.assertFalse(wants_model("1 min. Cm, F, G.", bare))
        bare.llm_every_perform = True
        self.assertTrue(wants_model("1 min. Cm, F, G.", bare))
        card = llm_price_card(Settings())
        self.assertEqual(card["model"], "grok-4.3")
        self.assertEqual(card["usd_per_million"]["in"], 1.25)
        self.assertEqual(card["usd_per_million"]["cached"], 0.2)
        self.assertEqual(card["usd_per_million"]["out"], 2.5)
        self.assertIn("docs.x.ai", card["source"])
        self.assertFalse(card["every_perform"])

    def test_signing_roundtrip(self):
        signer = Signer.from_text("base64:" + __import__("base64").b64encode(os.urandom(32)).decode())
        rec = {"record_hash": {"value": "ab" * 32}, "signature": {}}
        from nc_perform.sign import apply_signature
        apply_signature(rec, signer, "nc-perform (self-hosted)")
        self.assertEqual(rec["signature"]["status"], "SIGNED")
        self.assertEqual(rec["signature"]["algorithm"], "Ed25519")
        self.assertTrue(verify_signature(rec, signer.public))
        rec["record_hash"]["value"] = "cd" * 32
        self.assertFalse(verify_signature(rec, signer.public))

    def test_url_sources_rejected(self):
        plan = _tiny_plan("http://127.0.0.1/m.nc.json")
        plan["sources"] = {"x": {"kind": "url", "url": "http://127.0.0.1/a.wav", "license": "CC0-1.0", "author": "A", "title": "t"}}
        plan["layers"][0]["events"][0]["source"] = "x"
        s = Settings(allowed_hosts=("127.0.0.1",), suppliers_json="http://127.0.0.1/suppliers.json")
        with self.assertRaises(Reject) as cm:
            check_plan(plan, s, {"suppliers": []})
        self.assertIn("kind=url", str(cm.exception))

    def test_freesound_and_commons_sources(self):
        s = Settings(allowed_hosts=("freesound.org", "cdn.freesound.org", "upload.wikimedia.org", "commons.wikimedia.org"))
        plan = _tiny_plan("https://musicmatter.art/x.nc.json")
        plan["sources"] = {
            "fs": {"kind": "freesound", "id": "325647", "pitched": False},
            "cm": {
                "kind": "commons",
                "url": "https://upload.wikimedia.org/wikipedia/commons/a/a.ogg",
                "page": "https://commons.wikimedia.org/wiki/File:A.ogg",
                "license": "CC0-1.0",
                "title": "A",
                "author": "Ada",
                "pitched": False,
            },
        }
        plan["layers"][0]["events"][0]["source"] = "fs"
        check_plan(plan, s, {"suppliers": []})
        plan["sources"]["fs"]["id"] = "not-a-number"
        with self.assertRaises(Reject) as bad_id:
            check_plan(plan, s, None)
        self.assertIn("not a sound id", str(bad_id.exception))
        plan["sources"]["fs"]["id"] = "325647"
        plan["sources"]["cm"]["url"] = "https://evil.example/a.ogg"
        with self.assertRaises(Reject) as bad_host:
            check_plan(plan, s, None)
        self.assertIn("evil.example", str(bad_host.exception))

    def test_uiowa_mis_audio_outside_the_old_library_is_allowed(self):
        s = Settings(allowed_hosts=("theremin.music.uiowa.edu",))
        plan = _tiny_plan("https://musicmatter.art/x.nc.json")
        plan["interpreter"] = {"kind": "discover"}
        url = "https://theremin.music.uiowa.edu/sound%20files/MIS/Strings/violin/Violin.pizz.mf.sulG.G3B3.aiff"
        plan["sources"] = {
            "pz": {
                "kind": "uiowa_mis",
                "url": url,
                "page": "https://theremin.music.uiowa.edu/MISviolin.html",
                "title": "Violin pizzicato G3",
                "author": "University of Iowa Electronic Music Studios",
                "license": "PD-equivalent",
                "pitched": True,
                "expect_midi": 55,
                "segment": {"index": 0},
            },
        }
        plan["layers"][0]["events"][0]["source"] = "pz"
        check_plan(plan, s, {"suppliers": []})
        plan["sources"]["pz"]["url"] = "https://philharmonia.co.uk/samples/violin.wav"
        with self.assertRaises(Reject) as bad:
            check_plan(plan, s, None)
        self.assertIn("not a UIowa MIS", str(bad.exception))

    def test_chord_audio_host_is_allowed(self):
        s = Settings()
        self.assertTrue(s.host_allowed("audio.musicmatter.art"))
        self.assertTrue(s.host_allowed("musicmatter.art"))
        self.assertTrue(s.host_allowed("api.openverse.org"))
        self.assertTrue(s.host_allowed("ia801234.us.archive.org"))
        self.assertTrue(s.host_allowed("upload.wikimedia.org"))
        self.assertTrue(s.host_allowed("ccmixter.org"))
        self.assertFalse(s.host_allowed("evil.example"))

    def test_egress_guard_rechecks_redirects(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/hop":
                    self.send_response(302)
                    self.send_header("Location", "http://evil.example/no")
                    self.end_headers()
                    return
                self.send_response(520)
                self.end_headers()

            def log_message(self, fmt, *args):
                return

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        settings = Settings(allowed_hosts=("127.0.0.1",))
        try:
            with egress_guard(settings):
                with self.assertRaises(Reject) as blocked:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/hop", timeout=5)
                self.assertIn("evil.example", str(blocked.exception))
                with self.assertRaises(urllib.error.HTTPError) as failed:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/err", timeout=5)
                self.assertEqual(failed.exception.code, 520)
                self.assertIn("520", str(failed.exception))
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_config_xai_defaults(self):
        old = {k: os.environ.get(k) for k in ("XAI_API_KEY", "NC_LLM_BASE_URL", "NC_LLM_MODEL", "NC_LLM_API_KEY")}
        try:
            os.environ["XAI_API_KEY"] = "test-not-a-real-key"
            os.environ.pop("NC_LLM_BASE_URL", None)
            os.environ.pop("NC_LLM_MODEL", None)
            os.environ.pop("NC_LLM_API_KEY", None)
            s = load()
            self.assertEqual(s.llm_base_url, "https://api.x.ai/v1")
            self.assertEqual(s.llm_model, "grok-4.3")
            self.assertEqual(s.llm_api_key, "test-not-a-real-key")
            os.environ["NC_LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
            os.environ["NC_LLM_MODEL"] = "grok-4.3"
            s2 = load()
            self.assertEqual(s2.llm_base_url, "http://127.0.0.1:9/v1")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_s3_mock_roundtrip_and_signature(self):
        state = {}

        class H(_Quiet):
            def _ok(self, code=200, body=b"", content_type="application/octet-stream"):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", content_type)
                self.end_headers()
                if body and self.command != "HEAD":
                    self.wfile.write(body)

            def _auth(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                url = base + self.path
                headers = {k: v for k, v in self.headers.items()}
                self.assert_sig(self.command, url, headers, body)
                return body

            def assert_sig(self, method, url, headers, body):
                folded = {k.lower(): v for k, v in headers.items()}
                auth = folded.get("authorization") or ""
                if not auth.startswith("AWS4-HMAC-SHA256"):
                    raise AssertionError("missing signature: " + auth)
                amz = folded["x-amz-date"]
                now = datetime.strptime(amz, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                unsigned = {}
                if "content-type" in folded and "content-type" in auth.lower():
                    unsigned["Content-Type"] = folded["content-type"]
                signed = sign_v4(method, url, unsigned, body, "AKIATEST", "secret", "auto", now=now)
                if signed["Authorization"] != auth:
                    raise AssertionError("signature mismatch\n got " + auth + "\n want " + signed["Authorization"])
                if folded.get("x-amz-content-sha256") != hashlib.sha256(body).hexdigest():
                    raise AssertionError("payload hash")

            def do_PUT(self):
                body = self._auth()
                key = urllib.request.url2pathname if False else None
                path = urllib.parse.unquote(self.path.split("?", 1)[0])
                state[path] = body
                self._ok(200, b"")

            def do_GET(self):
                self._auth()
                import urllib.parse
                parsed = urllib.parse.urlparse(self.path)
                if parsed.query.startswith("list-type=2"):
                    prefix = urllib.parse.parse_qs(parsed.query).get("prefix", [""])[0]
                    keys = [k.split("/", 1)[1] if k.startswith("/nc-performances/") else k for k in state]
                    # store keys as full path
                    xml = ["<ListBucketResult><IsTruncated>false</IsTruncated>"]
                    for path, _blob in state.items():
                        key = path.split("/nc-performances/", 1)[-1]
                        if key.startswith(prefix):
                            xml.append(f"<Contents><Key>{key}</Key></Contents>")
                    xml.append("</ListBucketResult>")
                    return self._ok(200, "".join(xml).encode(), "application/xml")
                path = urllib.parse.unquote(parsed.path)
                if path not in state:
                    return self._ok(404, b"missing")
                self._ok(200, state[path])

            def do_DELETE(self):
                self._auth()
                import urllib.parse
                path = urllib.parse.unquote(self.path.split("?", 1)[0])
                state.pop(path, None)
                self._ok(204, b"")

        import urllib.parse
        httpd, base = _serve(H)
        self.addCleanup(_close, httpd)
        store = S3Storage("nc-performances", base, "AKIATEST", "secret", "auto", unsigned=False)
        store.put("performances/h-test/meta.json", b'{"ok":1}', "application/json")
        self.assertEqual(store.get("performances/h-test/meta.json"), b'{"ok":1}')
        self.assertIn("performances/h-test/meta.json", store.list("performances/"))
        store.delete("performances/h-test/meta.json")
        self.assertIsNone(store.get("performances/h-test/meta.json"))

    def test_turnstile_test_keys_rate_and_budget(self):
        tmp = tempfile.mkdtemp(prefix="nc-gate-")
        self.addCleanup(shutil.rmtree, tmp, True)
        s = _settings(tmp, suppliers="http://127.0.0.1/suppliers.json", llm_base="", llm_model="")
        s.turnstile_secret = "1x0000000000000000000000000000000AA"
        s.rate_per_10min = 2
        s.queue_autostart = False
        s.perform_enabled = True
        app, httpd = _api(s)
        self.addCleanup(_close, httpd)
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}/api/perform"
        body = json.dumps({"composition": "1 min. Cm.", "performance": "soft", "title": "Gate"}).encode()

        def post(token=None):
            payload = json.loads(body)
            if token is not None:
                payload["turnstile_token"] = token
            req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        self.assertEqual(post(None)[0], 403)
        self.assertEqual(post("nope")[0], 403)
        self.assertEqual(post("XXXX.DUMMY.TOKEN.XXXX")[0], 202)
        self.assertEqual(post("XXXX.DUMMY.TOKEN.XXXX")[0], 202)
        code, msg = post("XXXX.DUMMY.TOKEN.XXXX")
        self.assertEqual(code, 429)
        self.assertIn("10 minutes", msg["error"])
        app.store.add_spend(10)
        # A new IP is not required: the hard cap is checked before the rate limit,
        # but this IP is already over the rate limit. Use a fresh app flag via spend
        # on a second server with a high rate.
        s2 = _settings(tmp + "-b", suppliers="http://127.0.0.1/suppliers.json", llm_base="", llm_model="")
        s2.queue_autostart = False
        s2.daily_hard_usd = 5
        app2, httpd2 = _api(s2)
        app2.store.add_spend(5)
        self.addCleanup(_close, httpd2)
        url2 = f"http://127.0.0.1:{httpd2.server_address[1]}/api/perform"
        req = urllib.request.Request(url2, body, {"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected 503")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 503)
            self.assertIn("resting", e.read().decode())

    def test_rolling_day_names_when_the_next_slot_frees(self):
        tmp = tempfile.mkdtemp(prefix="nc-rate-")
        self.addCleanup(shutil.rmtree, tmp, True)
        store = Store(os.path.join(tmp, "jobs.sqlite"))
        now = time.time()
        oldest = now - 2 * 3600
        for i in range(10):
            store.db.execute("INSERT INTO ip_hits (ip_hash, ts) VALUES (?, ?)", ("ip", oldest + i * 60))
        store.db.commit()
        block = store.hit("ip", 100, 10)
        self.assertIsNotNone(block)
        self.assertIn("Daily limit reached (10 Performs from this network).", block.error)
        self.assertNotIn("tomorrow", block.error)
        self.assertRegex(block.error, r"Try again after \d{1,2}:\d{2} [AP]M UTC\.")
        self.assertGreater(block.retry_after_s, 20 * 3600)
        self.assertAlmostEqual(block.retry_after_s, oldest + 86400 - time.time(), delta=2)
        self.assertRegex(block.retry_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(store.db.execute("SELECT COUNT(*) c FROM ip_hits WHERE ip_hash='ip'").fetchone()["c"], 10)
        fresh = "other"
        for i in range(3):
            store.db.execute("INSERT INTO ip_hits (ip_hash, ts) VALUES (?, ?)", (fresh, now - 20 + i))
        store.db.commit()
        window = store.hit(fresh, 3, 10)
        self.assertIn("3 every 10 minutes", window.error)
        self.assertRegex(window.error, r"Try again after \d{1,2}:\d{2} [AP]M UTC\.")
        self.assertGreater(window.retry_after_s, 500)
        self.assertEqual(store.db.execute("SELECT COUNT(*) c FROM ip_hits WHERE ip_hash=?", (fresh,)).fetchone()["c"], 3)

    def test_tester_pass_skips_network_limits_not_the_cost_cap(self):
        secret = "tester-secret-value"
        self.assertTrue(tester_pass(secret, secret))
        self.assertTrue(tester_pass("  " + secret + "  ", secret))
        self.assertFalse(tester_pass("wrong-token", secret))
        self.assertFalse(tester_pass(None, secret))
        self.assertFalse(tester_pass("", secret))
        self.assertFalse(tester_pass(secret, ""))
        self.assertFalse(tester_pass(secret, None))
        self.assertFalse(tester_pass("admin-secret", secret))
        self.assertNotIn("tester_token", inspect.getsource(Runner._run))
        self.assertNotIn("X-NC-Tester", inspect.getsource(Runner._interpret))
        self.assertIn("daily_soft_usd", inspect.getsource(Runner._interpret))
        self.assertIn("daily_hard_usd", inspect.getsource(Runner._run))

        tmp = tempfile.mkdtemp(prefix="nc-tester-")
        self.addCleanup(shutil.rmtree, tmp, True)
        s = _settings(tmp, suppliers="http://127.0.0.1/suppliers.json", llm_base="", llm_model="")
        s.turnstile_secret = "1x0000000000000000000000000000000AA"
        s.rate_per_10min = 5
        s.rate_per_ip_day = 1
        s.queue_autostart = False
        s.tester_token = secret
        s.admin_token = "admin-secret"
        app, httpd = _api(s)
        self.addCleanup(_close, httpd)
        port = httpd.server_address[1]
        base = f"http://127.0.0.1:{port}"

        def post(path, header=None, turnstile="XXXX.DUMMY.TOKEN.XXXX", composition="1 min. Cm."):
            payload = {"composition": composition, "performance": "soft", "title": "Gate"}
            if turnstile is not None:
                payload["turnstile_token"] = turnstile
            headers = {"Content-Type": "application/json"}
            if header is not None:
                headers["X-NC-Tester"] = header
            req = urllib.request.Request(base + path, json.dumps(payload).encode(), headers)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        code, body = post("/api/perform", header=secret, turnstile=None)
        self.assertEqual(code, 403)
        self.assertEqual(body["code"], "turnstile")
        code, body = post("/api/perform", header=secret, composition="x" * 5000)
        self.assertEqual(code, 400)
        self.assertNotIn(secret, json.dumps(body))

        code, body = post("/api/perform")
        self.assertEqual(code, 202, body)
        self.assertFalse(body.get("tester"))
        job_id = body["id"]
        with urllib.request.urlopen(base + "/api/jobs/" + job_id, timeout=10) as r:
            job = json.loads(r.read())
        self.assertFalse(job["tester"])
        self.assertNotIn(secret, json.dumps(job))

        code, body = post("/api/perform", header="wrong-token")
        self.assertEqual(code, 429)
        self.assertEqual(body["code"], "rate")
        self.assertIn("Daily limit reached (1 Performs from this network).", body["error"])
        self.assertNotIn("tomorrow", body["error"])
        self.assertRegex(body["error"], r"Try again after \d{1,2}:\d{2} [AP]M UTC\.")
        self.assertIsInstance(body["retry_after_s"], int)
        self.assertGreater(body["retry_after_s"], 0)
        self.assertRegex(body["retry_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertNotIn(secret, json.dumps(body))
        code, missing = post("/api/perform")
        self.assertEqual(code, 429)
        self.assertEqual(missing["code"], "rate")
        code, admin = post("/api/perform", header="admin-secret")
        self.assertEqual(code, 429)

        code, body = post("/api/perform", header=secret)
        self.assertEqual(code, 202, body)
        self.assertNotIn(secret, json.dumps(body))
        with urllib.request.urlopen(base + "/api/jobs/" + body["id"], timeout=10) as r:
            passed = json.loads(r.read())
        self.assertTrue(passed["tester"])
        self.assertNotIn(secret, json.dumps(passed))
        stored = app.store.get(body["id"])
        self.assertTrue(stored["request"]["tester"])
        self.assertNotIn(secret, json.dumps(stored["request"]))
        code, again = post("/api/perform", header=secret)
        self.assertEqual(code, 202, again)
        self.assertEqual(app.store.db.execute("SELECT COUNT(*) c FROM ip_hits").fetchone()["c"], 1)

        code, gathered = post("/api/gather")
        self.assertEqual(code, 429)
        self.assertIn("Daily limit reached", gathered["error"])
        app.runner.run_job = lambda job_id: None
        code, gathered = post("/api/gather", header=secret)
        self.assertNotEqual(code, 429)
        self.assertNotIn(secret, json.dumps(gathered))
        gather_id = gathered.get("gather_id") or gathered.get("id")
        self.assertTrue(str(gather_id).startswith("g-"))
        with urllib.request.urlopen(base + "/api/jobs/" + gather_id, timeout=10) as r:
            gjob = json.loads(r.read())
        self.assertTrue(gjob["tester"])
        self.assertNotIn(secret, json.dumps(gjob))
        self.assertEqual(app.store.db.execute("SELECT COUNT(*) c FROM ip_hits").fetchone()["c"], 1)

        hard = _settings(tmp + "-hard", suppliers="http://127.0.0.1/suppliers.json", llm_base="", llm_model="")
        hard.queue_autostart = False
        hard.tester_token = secret
        hard.daily_hard_usd = 5
        hard.daily_soft_usd = 3
        app_hard, httpd_hard = _api(hard)
        app_hard.store.add_spend(5)
        self.addCleanup(_close, httpd_hard)
        hard_base = f"http://127.0.0.1:{httpd_hard.server_address[1]}"
        req = urllib.request.Request(
            hard_base + "/api/perform",
            json.dumps({"composition": "1 min. Cm.", "performance": "soft", "title": "Gate"}).encode(),
            {"Content-Type": "application/json", "X-NC-Tester": secret},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected 503")
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            self.assertEqual(e.code, 503)
            self.assertIn("resting", raw)
            self.assertNotIn(secret, raw)
        self.assertEqual(app_hard.store.db.execute("SELECT COUNT(*) c FROM ip_hits").fetchone()["c"], 0)

        soft = _settings(tmp + "-soft", suppliers="http://127.0.0.1/suppliers.json", llm_base="", llm_model="")
        soft.queue_autostart = False
        soft.tester_token = secret
        soft.daily_hard_usd = 5
        soft.daily_soft_usd = 3
        app_soft, httpd_soft = _api(soft)
        app_soft.store.add_spend(4)
        self.addCleanup(_close, httpd_soft)
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd_soft.server_address[1]}/api/perform",
            json.dumps({"composition": "1 min. Cm.", "performance": "soft", "title": "Gate"}).encode(),
            {"Content-Type": "application/json", "X-NC-Tester": secret},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 202)

    def test_e2e_perform_through_the_kit(self):
        tmp = tempfile.mkdtemp(prefix="nc-e2e-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.environ["NC_CACHE"] = os.path.join(tmp, "cache")
        wav_path = os.path.join(tmp, "glass.wav")
        digest = _wav(wav_path)
        calls = {"n": 0, "auth": None, "model": None}

        class Supplier(_Quiet):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/suppliers.json":
                    body = json.dumps({"suppliers": [{
                        "id": "local", "name": "Local supplier", "display_name": "Local supplier",
                        "status": "active", "url": base_s + "/", "index": base_s + "/index.json",
                    }]}).encode()
                elif path == "/index.json":
                    body = json.dumps({
                        "default_license": "CC0-1.0",
                        "creator": {"name": "Local supplier", "link": base_s + "/"},
                        "offerings": [{"manifest": base_s + "/glass.nc.json"}],
                    }).encode()
                elif path == "/glass.nc.json":
                    body = json.dumps({
                        "title": "Glass tone",
                        "license": "CC0-1.0",
                        "audio": base_s + "/audio/glass.wav",
                        "sha256": digest,
                        "creator": {"name": "Local supplier", "link": base_s + "/"},
                    }).encode()
                elif path == "/audio/glass.wav":
                    body = open(wav_path, "rb").read()
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        supplier, base_s = _serve(Supplier)
        self.addCleanup(_close, supplier)
        plan = _tiny_plan(base_s + "/glass.nc.json")
        errs = validate(plan)
        self.assertEqual(errs, [], errs)
        plan_text = json.dumps(plan)

        class XAI(_Quiet):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = json.loads(self.rfile.read(n))
                calls["n"] += 1
                calls["auth"] = self.headers.get("Authorization")
                calls["model"] = raw.get("model")
                if "/chat/completions" not in self.path:
                    raise RuntimeError("unexpected path " + self.path)
                out = json.dumps({
                    "id": "mock",
                    "model": raw.get("model", "grok-4.3") + "-mock",
                    "choices": [{"message": {"role": "assistant", "content": plan_text}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 0}},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        xai, base_x = _serve(XAI)
        self.addCleanup(_close, xai)

        objects = {}

        class R2(_Quiet):
            def _read(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _send(self, code, body=b"", content_type="application/octet-stream"):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", content_type)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_PUT(self):
                import urllib.parse
                body = self._read()
                auth = self.headers.get("Authorization") or ""
                if not auth.startswith("AWS4-HMAC-SHA256") or "AKIATEST" not in auth:
                    return self._send(403, b"sig")
                if self.headers.get("x-amz-content-sha256") != hashlib.sha256(body).hexdigest():
                    return self._send(400, b"hash")
                path = urllib.parse.unquote(self.path.split("?", 1)[0])
                if self.headers.get("If-None-Match") == "*" and path in objects:
                    return self._send(412, b"exists")
                objects[path] = body
                self._send(200, b"")

            def do_GET(self):
                import urllib.parse
                parsed = urllib.parse.urlparse(self.path)
                if "list-type=2" in (parsed.query or ""):
                    prefix = urllib.parse.parse_qs(parsed.query).get("prefix", [""])[0]
                    xml = ["<ListBucketResult><IsTruncated>false</IsTruncated>"]
                    for path in objects:
                        key = urllib.parse.unquote(path).split("/nc-performances/", 1)[-1]
                        if key.startswith(prefix):
                            xml.append(f"<Contents><Key>{key}</Key></Contents>")
                    xml.append("</ListBucketResult>")
                    return self._send(200, "".join(xml).encode(), "application/xml")
                path = urllib.parse.unquote(parsed.path)
                if path not in objects:
                    return self._send(404, b"")
                self._send(200, objects[path])

            def do_DELETE(self):
                import urllib.parse
                objects.pop(urllib.parse.unquote(self.path.split("?", 1)[0]), None)
                self._send(204, b"")

        r2, base_r = _serve(R2)
        self.addCleanup(_close, r2)

        signer = Signer.from_text("base64:" + __import__("base64").b64encode(b"\x11" * 32).decode())
        # PEM round-trip is covered above; this seed is stable for the run.
        s = _settings(tmp, suppliers=base_s + "/suppliers.json", llm_base=base_x + "/v1", llm_model="grok-4.3")
        s.llm_api_key = "xai-test"
        s.s3_endpoint = base_r
        s.s3_access_key = "AKIATEST"
        s.s3_secret_key = "secret"
        s.storage = "s3://nc-performances"
        s.signing_key = "base64:" + __import__("base64").b64encode(signer.key.private_bytes(
            __import__("cryptography").hazmat.primitives.serialization.Encoding.Raw,
            __import__("cryptography").hazmat.primitives.serialization.PrivateFormat.Raw,
            __import__("cryptography").hazmat.primitives.serialization.NoEncryption(),
        )).decode()
        s.queue_autostart = False
        s.processed = True
        s.allowed_hosts = ("127.0.0.1",)
        app, httpd = _api(s)
        self.addCleanup(_close, httpd)
        port = httpd.server_address[1]
        s.public_base_url = f"http://127.0.0.1:{port}"
        api = s.public_base_url

        words = {
            "title": "Glass line",
            "composer": "",
            "composition": "8 seconds. Cm. A melody line in triplets across two sections.",
            "performance": "soft and clear, like glass",
            "visibility": "unlisted",
            "seed": 7,
        }
        req = urllib.request.Request(api + "/api/perform-processed", json.dumps(words).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            created = json.loads(r.read())
        self.assertEqual(r.status, 202)
        self.assertTrue(created["id"].startswith("h-"))
        self.assertTrue(created.get("claim_token"))
        self.assertEqual(app.runner.pump_available(), 1)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(calls["model"], "grok-4.3")
        self.assertEqual(calls["auth"], "Bearer xai-test")

        with urllib.request.urlopen(api + "/api/jobs/" + created["id"], timeout=10) as r:
            job = json.loads(r.read())
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(job["seed"], 7)
        self.assertIsNone(job["trimmed"])
        self.assertIsInstance(job["found_sounds"], list)
        self.assertTrue(job.get("stage_line"))
        self.assertTrue(all(row.get("title") and row.get("supplier") and row.get("license") for row in job["found_sounds"]))
        with urllib.request.urlopen(api + "/records/" + created["id"] + ".json", timeout=10) as r:
            rec = json.loads(r.read())
            self.assertIn("noindex", r.headers.get("X-Robots-Tag", ""))
        self.assertEqual(rec["record_type"], "netcomposer.performance_record")
        self.assertEqual(rec["performance"]["license_out"]["id"], "CC-BY-SA-4.0")
        self.assertFalse(rec["performance"]["rule_set"]["pitch_shift"])
        self.assertTrue(all(not snd.get("pitch_shift") for snd in rec["sounds_used"]))
        self.assertGreaterEqual(len(rec["performance"]["timeline"]), 1)
        self.assertTrue(all("spans" in snd for snd in rec["sounds_used"]))
        self.assertTrue(any(snd["spans"] for snd in rec["sounds_used"]))
        self.assertEqual(rec["performance"]["composition_ref"]["composer"]["display_name"], "anonymous")
        self.assertEqual(rec["signature"]["status"], "SIGNED")
        self.assertEqual(rec["signature"]["algorithm"], "Ed25519")
        self.assertIn("hacks_chain", rec)
        self.assertNotIn("plugin", json.dumps(rec).lower())
        signer2 = app.signer
        self.assertTrue(verify_signature(rec, signer2.public))
        # Signing did not change the hash the kit wrote.
        from ncperf.record import rhash
        self.assertEqual(rec["record_hash"]["value"], rhash(rec))

        meta_req = urllib.request.Request(api + "/api/jobs/" + created["id"] + "/files/meta.json")
        with urllib.request.urlopen(meta_req, timeout=10) as r:
            meta = json.loads(r.read())
        self.assertEqual(meta["visibility"], "unlisted")
        self.assertIsNone(meta["owner"])
        exp = datetime.strptime(meta["audio_expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        delta = exp - datetime.now(timezone.utc)
        self.assertTrue(timedelta(days=89) < delta < timedelta(days=91), delta)

        with urllib.request.urlopen(api + "/api/verify/" + created["id"], timeout=30) as r:
            ver = json.loads(r.read())
        self.assertTrue(ver["ok"], ver["lines"])
        self.assertTrue(ver["signature_ok"])

        keep = urllib.request.Request(
            api + "/api/jobs/" + created["id"] + "/keep",
            json.dumps({"claim_token": created["claim_token"]}).encode(),
            {"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(keep, timeout=10) as r:
            kept = json.loads(r.read())
        self.assertTrue(kept["keep"])
        self.assertIsNone(kept["audio_expires_at"])

        with urllib.request.urlopen(api + "/api/feed", timeout=10) as r:
            feed = json.loads(r.read())
        self.assertFalse(any(p["id"] == created["id"] for p in feed["performances"]))

        # Plan cache: same words and seed do not call xAI again. Do not render twice.
        # Stay inside the egress guard so discovery does not wander onto the public internet.
        comp = app.store.get(created["id"])["request"]["composition"]
        with egress_guard(s):
            _plan, mode = app.runner._interpret(comp, 7, {})
        self.assertEqual(mode, "llm-cache")
        self.assertEqual(calls["n"], 1)

        # Ordinary notes do not call xAI. Suppliers are blocked here, and the
        # pitch-checked notes still come from the local note index.
        from ncperf.interpret import read_composition
        glass = read_composition(os.path.join(ROOT, "examples", "glass-garden.composition.md"))
        glass["composer"] = glass.get("composer") or "anonymous"
        with egress_guard(s):
            discovered, mode = app.runner._interpret(glass, 1, {})
        self.assertEqual(calls["n"], 1)
        self.assertEqual(mode, "discover")
        self.assertTrue(any(src.get("pitch_checked") for src in (discovered.get("sources") or {}).values()))

        # Portable kit submission of this record, unsigned, through nc submit.
        rec2 = json.loads(json.dumps(rec))
        rec2["signature"] = {"status": "UNSIGNED", "signed_by": None, "algorithm": None, "key_id": None, "value": None}
        sub = os.path.join(tmp, "submit")
        os.makedirs(sub)
        json.dump(rec2, open(os.path.join(sub, "record.json"), "w"))
        for name in ("performance.flac", "preview.mp3", "plan.json"):
            urllib.request.urlretrieve(api + "/api/jobs/" + created["id"] + "/files/" + name, os.path.join(sub, name))
        from ncperf.submit import submit
        out = submit(os.path.join(sub, "record.json"), sub, api, os.path.join(sub, "plan.json"))
        self.assertTrue(out["id"].startswith("k-"))
        self.assertIn("not re-rendered", out["note"])
        with urllib.request.urlopen(api + "/records/" + out["id"] + ".json", timeout=10) as r:
            kit_rec = json.loads(r.read())
        self.assertEqual(kit_rec["record_hash"]["value"], rec["record_hash"]["value"])
        self.assertEqual(kit_rec["signature"]["status"], "UNSIGNED")

        keys = json.loads(urllib.request.urlopen(api + "/.well-known/nc-record-keys.json", timeout=10).read())
        self.assertEqual(keys["keys"][0]["kid"], app.signer.key_id)
        self.assertEqual(keys["keys"][0]["crv"], "Ed25519")


def _settings(tmp, suppliers, llm_base, llm_model="grok-4.3") -> Settings:
    s = Settings()
    s.storage = "file://" + os.path.join(tmp, "files")
    s.data_dir = tmp
    s.public_base_url = "http://127.0.0.1"
    s.suppliers_json = suppliers
    s.llm_base_url = llm_base
    s.llm_model = llm_model
    s.llm_api_key = "xai-test" if llm_base else ""
    s.bind = "127.0.0.1:0"
    s.queue_autostart = False
    s.allowed_hosts = ("127.0.0.1", "localhost")
    s.perform_enabled = True
    s.instance_name = "nc-perform (self-hosted)"
    os.makedirs(s.data_dir, exist_ok=True)
    os.makedirs(os.path.join(tmp, "files"), exist_ok=True)
    return s


def _tiny_plan(manifest: str) -> dict:
    return {
        "nc_plan_version": "0.1",
        "id": "glass-line",
        "title": "Glass line (test)",
        "composition": {
            "title": "Glass line",
            "composer": "anonymous",
            "words_verbatim": {
                "composition": "8 seconds. Cm. A melody line in triplets across two sections.",
                "performance": "soft and clear, like glass",
            },
        },
        "interpreter": {"kind": "llm", "model": "grok-4.3"},
        "seed": 7,
        "length_s": 6,
        "tail_s": 1,
        "pulse_bpm": 60,
        "sections": [
            {"id": "A", "start_s": 0, "end_s": 3, "harmony": "Cm"},
            {"id": "B", "start_s": 3, "end_s": 6, "harmony": "Cm"},
        ],
        "chords": [
            {"t": 0, "dur": 3, "symbol": "Cm", "voicing": [48, 55, 60], "section": "A"},
            {"t": 3, "dur": 3, "symbol": "Cm", "voicing": [48, 55, 60], "section": "B"},
        ],
        "sources": {"glass": {"kind": "supplier", "manifest": manifest, "pitched": False}},
        "banks": {},
        "layers": [{
            "id": "air",
            "type": "clips",
            "bus": "pad",
            "role": "glass tone",
            "events": [{
                "t": 0.1,
                "source": "glass",
                "dur_s": 1.0,
                "fade_in_s": 0.05,
                "fade_out_s": 0.3,
                "gain": 0.8,
                "flatten": False,
                "role": "glass tone, trimmed, not time-stretched",
            }],
        }],
        "master": {
            "buses": {"pad": {"lufs": -20}},
            "reverb": {"engine": "synthetic", "send_lu": 1.0, "rt60_s": 0.8, "predelay_ms": 10},
            "hp_hz": 30,
            "high_shelf": {"hz": 8000, "db": -1},
            "loudness": {"lufs": -17, "tp": -1.5, "method": "linear_tp_limiter"},
        },
        "quality": {"stretch_engine": "ffmpeg-rubberband", "resampler": "soxr"},
        "notes": ["Sample Composition rendered by the service test. No pitch-shifting."],
    }


if __name__ == "__main__":
    unittest.main(verbosity=2)
