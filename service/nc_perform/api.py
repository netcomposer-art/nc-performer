"""HTTP API for the nc-perform image.

Public routes are what a browser and ``nc submit`` call. ``/internal/*`` is
what the Cloudflare queue consumer calls on the container. Both use the same
runner.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ncperf import record as R

from . import keep as K
from . import pages
from .config import Settings, load, tester_pass
from .ids import ID_RE, claim_token, new_id, sha256_text
from .jobs import Store
from .limits import Reject, check_words
from .planner import llm_price_card
from ncperf.cancel import MESSAGE as CANCELLED_MESSAGE
from ncperf.gather import load_collection, wipe_gather
from .cancel import token_from, wipe_performance, write_cancelled_status
from .runner import Runner, claim_matches, store_kit_submission
from .sign import Signer
from .storage import Storage, open_storage

def _registry_stats() -> dict:
    from ncperf import registry
    return registry.public_stats()


def web_index() -> str:
    """The Perform page. Dev layout keeps it in service/web. The image copies
    it next to the installed package so ``python -m nc_perform`` still finds it.
    """
    override = os.environ.get("NC_WEB_INDEX")
    if override and os.path.exists(override):
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(os.path.dirname(here), "web", "index.html"),
        os.path.join(here, "web", "index.html"),
        "/opt/nc-performer/service/web/index.html",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]

# Cloudflare's published Turnstile test secrets. They are not real credentials.
# https://developers.cloudflare.com/turnstile/troubleshooting/testing/
_TURNSTILE_TEST = {
    "1x0000000000000000000000000000000AA": True,   # always passes
    "2x0000000000000000000000000000000AA": False,  # always fails
    "3x0000000000000000000000000000000AA": False,  # interactive; no widget here, so fail
}
_DUMMY_TOKEN = "XXXX.DUMMY.TOKEN.XXXX"

FILE_TYPES = {
    "performance.flac": "audio/flac",
    "preview.mp3": "audio/mpeg",
    "kept.wav": "audio/wav",
    "kept.mp3": "audio/mpeg",
    "record.json": "application/json",
    "plan.json": "application/json",
    "uses.json": "application/json",
    "composition.md": "text/markdown; charset=utf-8",
    "meta.json": "application/json",
    "status.json": "application/json",
}


_TURNSTILE_CODE = re.compile(r"^[a-z0-9-]{1,64}$")
_TURNSTILE_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


def turnstile_failure(codes, hostname) -> dict:
    """Siteverify error-codes and hostname. Drop anything that could echo a secret."""
    errors = []
    if isinstance(codes, list):
        for code in codes:
            if isinstance(code, str) and _TURNSTILE_CODE.fullmatch(code):
                errors.append(code)
            if len(errors) >= 8:
                break
    host = hostname if isinstance(hostname, str) and _TURNSTILE_HOST.fullmatch(hostname) else None
    return {"turnstile_errors": errors, "hostname": host}


def _log_turnstile(failure: dict) -> None:
    sys.stderr.write(json.dumps({
        "event": "turnstile",
        "errors": failure["turnstile_errors"],
        "hostname": failure["hostname"],
    }) + "\n")


def verify_turnstile(settings: Settings, token: str | None, _ip: str) -> tuple[bool, dict]:
    """Return whether the token passed, and the safe siteverify failure fields.

    The three documented test secrets are decided locally so a self-check does
    not need to reach Cloudflare. Any other secret is posted to siteverify
    (or to TURNSTILE_VERIFY_URL). A missing token is not posted.
    """
    secret = (settings.turnstile_secret or "").strip()
    raw = token.strip() if isinstance(token, str) else ""
    if not secret:
        return True, {}
    if secret in _TURNSTILE_TEST:
        if _TURNSTILE_TEST[secret] and raw == _DUMMY_TOKEN:
            return True, {}
        failure = turnstile_failure(["missing-input-response" if not raw else "invalid-input-response"], None)
        _log_turnstile(failure)
        return False, failure
    if not raw:
        failure = turnstile_failure(["missing-input-response"], None)
        _log_turnstile(failure)
        return False, failure
    # remoteip is omitted. Turnstile compares it with the IP that solved the
    # widget, and a mismatch rejects a fresh token. The Worker checks once.
    # The container hop (/internal/jobs) does not check again.
    data = urllib.parse.urlencode({
        "secret": secret,
        "response": raw,
    }).encode()
    req = urllib.request.Request(
        settings.turnstile_verify_url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read().decode())
    if not isinstance(body, dict):
        body = {}
    if body.get("success"):
        return True, {}
    failure = turnstile_failure(body.get("error-codes"), body.get("hostname"))
    _log_turnstile(failure)
    return False, failure


def _turnstile_denied(handler, failure: dict):
    handler._send(403, {
        "error": "The bot check failed. Reload the page and try again.",
        "code": "turnstile",
        "turnstile_errors": failure.get("turnstile_errors") or [],
        "hostname": failure.get("hostname"),
    })


def _json_error(handler, status: int, message: str, code: str = "error"):
    handler._send(status, {"error": message, "code": code}, "application/json")


class App:
    def __init__(self, settings: Settings, store: Store, storage: Storage, signer: Signer | None, runner: Runner):
        self.settings = settings
        self.store = store
        self.storage = storage
        self.signer = signer
        self.runner = runner

    def perform_enabled(self) -> bool:
        flag = self.store.flag("perform_enabled")
        if flag is None:
            return self.settings.perform_enabled
        return flag == "1"


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def _cors(self):
            from ncperf.assembly import allow_origin
            origin, vary = allow_origin(self.headers.get("Origin"))
            self.send_header("Access-Control-Allow-Origin", origin)
            if vary:
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-NC-Tester")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _send(self, status, body, content_type="application/json", extra=None):
            if isinstance(body, str):
                raw = body.encode()
            elif isinstance(body, (dict, list)):
                raw = json.dumps(body, ensure_ascii=False).encode()
            else:
                raw = body
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            if not (extra or {}).get("Cache-Control"):
                self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def _read(self, limit: int) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                raise Reject(f"Request is too large ({n} bytes).")
            return self.rfile.read(n) if n else b""

        def _ip(self) -> str:
            fwd = self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For") or ""
            return fwd.split(",")[0].strip() or (self.client_address[0] if self.client_address else "")

        def _ip_hash(self) -> str:
            import hashlib
            return hashlib.sha256(f"{app.settings.ip_hash_salt}:{self._ip()}".encode()).hexdigest()

        def _rate_block_or_tester(self) -> tuple[bool, bool]:
            """(allowed, tester). A matching X-NC-Tester skips the 10-minute and daily counters.

            Turnstile, size limits, and the daily cost caps are checked by the caller.
            The header value is not stored.
            """
            tester = tester_pass(self.headers.get("X-NC-Tester"), app.settings.tester_token)
            if tester:
                return True, True
            s = app.settings
            block = app.store.hit(self._ip_hash(), s.rate_per_10min, s.rate_per_ip_day)
            if not block:
                return True, False
            self._send(429, {
                "error": block.error,
                "code": "rate",
                "retry_after_s": block.retry_after_s,
                "retry_at": block.retry_at,
            })
            return False, False

        def do_OPTIONS(self):
            self._send(204, b"")

        def do_GET(self):
            try:
                self._get()
            except Reject as e:
                _json_error(self, 400, str(e), e.code)
            except Exception as e:
                _json_error(self, 500, str(e))

        def do_POST(self):
            try:
                self._post()
            except Reject as e:
                _json_error(self, 400, str(e), e.code)
            except Exception as e:
                _json_error(self, 500, str(e))

        def _get(self):
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/create", "/create/"):
                page = open(web_index(), encoding="utf-8").read()
                # data-perform-api stays empty when the page is served by this process.
                self._send(200, page, "text/html; charset=utf-8")
                return
            if path == "/health":
                from . import __version__
                self._send(200, {
                    "ok": True,
                    "service": "nc-perform",
                    "version": __version__,
                    "registry": _registry_stats(),
                })
                return
            if path == "/api/warm":
                # This process is already up. The Worker starts the container.
                self._send(200, {"ok": True, "warming": False})
                return
            if path == "/api/config":
                s = app.settings
                self._send(200, {
                    "turnstile_sitekey": s.turnstile_sitekey or None,
                    "turnstile_required": s.turnstile_required,
                    "max_words": s.max_words,
                    "max_length_s": s.max_length_s,
                    "instance": s.instance_name,
                    "perform_enabled": app.perform_enabled(),
                    "llm": bool(s.llm_configured),
                    "llm_model": s.llm_model or None,
                    "llm_price": llm_price_card(s),
                    "browser_assembly": True,
                    "registry": _registry_stats(),
                })
                return
            if path == "/.well-known/nc-record-keys.json":
                keys = []
                if app.signer:
                    keys.append(app.signer.jwk())
                self._send(200, {"keys": keys})
                return
            if path == "/api/feed":
                self._send(200, {"performances": _feed(app)})
                return
            if path == "/admin":
                if not _admin_ok(app, self):
                    return self._send(401, {"error": "admin token required"})
                self._send(200, pages.admin_page(app.store.summary_today(), app.perform_enabled()), "text/html; charset=utf-8")
                return
            m = re.fullmatch(r"/api/jobs/([hkg]-[0-9a-z]+)", path)
            if m:
                return self._job(m.group(1))
            m = re.fullmatch(r"/api/jobs/([hkg]-[0-9a-z]+)/files/([a-z0-9._-]+)", path)
            if m:
                return self._file(m.group(1), m.group(2))
            m = re.fullmatch(r"/api/jobs/([hkg]-[0-9a-z]+)/audio/([A-Za-z0-9._-]+)", path)
            if m:
                return self._gather_audio(m.group(1), m.group(2))
            m = re.fullmatch(r"/api/jobs/([hkg]-[0-9a-z]+)/events", path)
            if m:
                return self._events(m.group(1))
            m = re.fullmatch(r"/api/verify/([hkg]-[0-9a-z]+)", path)
            if m:
                return self._verify(m.group(1))
            m = re.fullmatch(r"/records/(sha256/[0-9a-f]{64})", path)
            if m:
                return self._alias(m.group(1).split("/", 1)[1])
            m = re.fullmatch(r"/records/([hkg]-[0-9a-z]+)(\.json)?", path)
            if m:
                return self._record(m.group(1), bool(m.group(2)))
            if path.startswith("/internal/"):
                return self._internal_get(path)
            self._send(404, {"error": "not found"})

        def _post(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/gather":
                return self._gather()
            if path == "/api/perform":
                return self._perform()
            if path == "/api/keep-take":
                return self._keep_take()
            if path == "/api/perform-processed":
                if not app.settings.processed:
                    return self._send(404, {"error": "not found"})
                return self._perform(processed=True)
            m = re.fullmatch(r"/api/jobs/([hkg]-[0-9a-z]+)/cancel", path)
            if m:
                return self._cancel(m.group(1))
            m = re.fullmatch(r"/api/jobs/([hkg]-[0-9a-z]+)/(keep|visibility)", path)
            if m:
                return self._meta_post(m.group(1), m.group(2))
            if path == "/api/submit":
                return self._submit()
            if path == "/admin/enabled":
                return self._admin_enabled()
            if path == "/admin/hide" or path == "/admin/takedown":
                return self._admin_moderation(path.rsplit("/", 1)[1])
            if path == "/internal/jobs":
                return self._internal_post()
            if path == "/internal/cache":
                return self._internal_cache()
            if path == "/internal/gather-file":
                return self._internal_gather_file()
            if path == "/internal/take-audio":
                return self._internal_take_audio()
            if path == "/internal/note-index":
                return self._internal_note_index()
            if path == "/internal/registry":
                return self._internal_registry()
            if path == "/internal/registry/check":
                return self._internal_registry_check()
            m = re.fullmatch(r"/internal/jobs/([hkg]-[0-9a-z]+)/cancel", path)
            if m:
                return self._internal_cancel(m.group(1))
            self._send(404, {"error": "not found"})

        def _gather(self):
            s = app.settings
            if not app.perform_enabled() or app.store.spend_today() >= s.daily_hard_usd:
                return self._send(503, {"error": "Perform is resting until tomorrow.", "code": "resting"})
            body = json.loads(self._read(s.max_plan_bytes + 8000).decode() or "{}")
            ok, failure = verify_turnstile(s, body.get("turnstile_token"), self._ip())
            if s.turnstile_required and not ok:
                return _turnstile_denied(self, failure)
            comp = _composition(body)
            check_words(comp, s)
            if "instrument" in body and body.get("instrument") not in (None, ""):
                comp["instrument"] = _instrument(body.get("instrument"))
            avoid_keys = None
            avoid_samples = None
            excluded = body.get("exclude_gather_id")
            if excluded not in (None, ""):
                excluded = str(excluded)
                if not ID_RE.match(excluded) or not excluded.startswith("g-"):
                    return self._send(404, {"error": "not found"})
                token = token_from(body)
                if not token:
                    return self._send(400, {"error": "claim_token is required.", "code": "rejected"})
                previous = _gather_doc(app, excluded)
                if not previous:
                    return self._send(404, {"error": "not found"})
                if not claim_matches(previous.get("claim_hash"), token):
                    return self._send(403, {"error": "claim_token does not match this Gather."})
                avoid_keys = previous.get("picked_keys") or []
                avoid_samples = previous.get("picked_sample_ids") or []
            else:
                excluded = None
            allowed, tester = self._rate_block_or_tester()
            if not allowed:
                return
            if app.store.queued_count() >= s.max_queue or app.store.running_count() >= s.max_concurrency:
                return self._send(503, {"error": "Busy, try in a minute.", "code": "busy"})
            seed = choose_seed(body)
            token = claim_token()
            job_id = new_id("g")
            req = {
                "kind": "gather",
                "composition": comp,
                "visibility": "unlisted",
                "seed": int(seed),
                "claim_hash": sha256_text(token),
                "avoid_keys": avoid_keys,
                "avoid_sample_ids": avoid_samples,
                "excluded_gather_id": excluded,
                "tester": tester,
            }
            app.store.create({
                "id": job_id,
                "state": "gathering",
                "stage": "gathering",
                "percent": 40,
                "request": req,
                "ip_hash": self._ip_hash(),
                "claim_hash": req["claim_hash"],
            })
            app.runner.run_job(job_id)
            job = app.store.get(job_id) or {}
            state = job.get("state")
            if state == "gathered":
                return self._send(200, _gather_body(app, job_id, token, job, excluded))
            if state == "cancelled":
                return self._send(200, _public_job(job_id, state, job.get("stage"), job.get("percent"), job.get("error"), app, None, job=job))
            code = 400 if state == "rejected" else 500
            return self._send(code, {
                "error": job.get("error") or "Gather failed.",
                "code": job.get("error_code") or "error",
                "gather_id": job_id,
                "poll": f"/api/jobs/{job_id}",
            })

        def _perform(self, processed=False):
            s = app.settings
            if not app.perform_enabled() or app.store.spend_today() >= s.daily_hard_usd:
                return self._send(503, {"error": "Perform is resting until tomorrow.", "code": "resting"})
            body = json.loads(self._read(s.max_plan_bytes + 8000).decode() or "{}")
            ok, failure = verify_turnstile(s, body.get("turnstile_token"), self._ip())
            if s.turnstile_required and not ok:
                return _turnstile_denied(self, failure)
            gathered = _perform_gather(app, body)
            if isinstance(gathered, tuple):
                return self._send(gathered[0], gathered[1])
            if gathered:
                comp = gathered["composition"]
                plan = gathered["plan"]
                seed = int(gathered["seed"])
                gather_id = gathered["gather_id"]
            else:
                comp = _composition(body)
                check_words(comp, s)
                plan = body.get("plan")
                seed = None
                gather_id = None
            if plan is not None and not isinstance(plan, dict):
                raise Reject("plan must be a JSON object.")
            if not gathered and plan is not None and not processed and plan.get("assembly") != "raw":
                raise Reject("This release assembles the recordings in order. Processing is off.")
            allowed, tester = self._rate_block_or_tester()
            if not allowed:
                return
            if app.store.queued_count() >= s.max_queue:
                return self._send(503, {"error": "Busy, try in a minute.", "code": "busy"})
            visibility = body.get("visibility") or "unlisted"
            if visibility not in ("unlisted", "public"):
                raise Reject("visibility must be unlisted or public.")
            # Same words and the same seed reuse a cached plan. "Perform again"
            # changes the seed on purpose so the next plan is new.
            # A kept Gather already has its seed inside the plan.
            if seed is None:
                seed = choose_seed(body)
            token = claim_token()
            job_id = new_id("h")
            req = {
                "composition": comp,
                "plan": plan,
                "gather_id": gather_id,
                "visibility": visibility,
                "seed": int(seed),
                "keep": bool(body.get("keep")),
                "claim_hash": sha256_text(token),
                "tester": tester,
                "processed": bool(processed),
            }
            app.store.create({
                "id": job_id,
                "state": "queued",
                "stage": "queued",
                "request": req,
                "ip_hash": self._ip_hash(),
                "claim_hash": req["claim_hash"],
            })
            pos = app.store.position(job_id)
            self._send(202, {
                "id": job_id,
                "status": "queued",
                "position": pos,
                "poll": f"/api/jobs/{job_id}",
                "claim_token": token,
                "record_url": f"{s.public_base_url}/records/{job_id}.json",
                "note": "Save claim_token. It is not shown again. A later account can use it to adopt this Performance. v1 has no accounts.",
            })

        def _keep_take(self):
            """Store the browser's WAV or FLAC and sign the record for that file."""
            s = app.settings
            if not app.perform_enabled() or app.store.spend_today() >= s.daily_hard_usd:
                return self._send(503, {"error": "Perform is resting until tomorrow.", "code": "resting"})
            ctype = self.headers.get("Content-Type") or ""
            if "multipart/form-data" not in ctype.lower():
                return self._send(400, {"error": "Save the take as multipart/form-data.", "code": "rejected"})
            from ncperf import assembly
            raw = self._read(assembly.TAKE_AUDIO_MAX + s.max_plan_bytes + 16 * 1024)
            parts = K.parse_multipart(raw, ctype)

            def text(name):
                blob = parts.get(name)
                if blob is None:
                    return None
                try:
                    return blob.decode("utf-8")
                except UnicodeDecodeError:
                    raise Reject(f"{name} must be text.")

            body = {
                "gather_id": (text("gather_id") or "").strip(),
                "claim_token": (text("claim_token") or "").strip(),
                "turnstile_token": text("turnstile_token") or "",
            }
            ok, failure = verify_turnstile(s, body.get("turnstile_token"), self._ip())
            if s.turnstile_required and not ok:
                return _turnstile_denied(self, failure)
            gathered = _perform_gather(app, body)
            if isinstance(gathered, tuple):
                return self._send(gathered[0], gathered[1])
            if not gathered:
                return self._send(400, {"error": "gather_id is required.", "code": "rejected"})
            state_bytes = parts.get("take_state")
            if not state_bytes:
                return self._send(400, {"error": "take_state is required.", "code": "rejected"})
            if len(state_bytes) > s.max_plan_bytes:
                return self._send(400, {"error": "take_state is too large.", "code": "rejected"})
            try:
                state_obj = json.loads(state_bytes)
            except json.JSONDecodeError:
                return self._send(400, {"error": "take_state must be JSON.", "code": "rejected"})
            try:
                take_state = assembly.parse_take_state(state_obj, gathered["gather_id"])
            except ValueError as exc:
                return self._send(400, {"error": str(exc), "code": "rejected"})
            doc = _gather_doc(app, gathered["gather_id"])
            errors = assembly.validate(take_state["plan"], doc or {}, max_length_s=s.max_length_s)
            if errors:
                return self._send(400, {"error": errors[0], "errors": errors, "code": "rejected"})
            audio = parts.get("audio")
            if not audio:
                return self._send(400, {"error": "Attach the take as the form field 'audio'.", "code": "rejected"})
            try:
                assembly.inspect_upload(audio, float(take_state["plan"].get("total_s") or 0))
            except ValueError as exc:
                return self._send(400, {"error": str(exc), "code": "rejected"})
            title = text("title")
            if title not in (None, ""):
                title = title.strip()
                if not title or len(title) > 200:
                    return self._send(400, {"error": "title must be a short name.", "code": "rejected"})
            else:
                title = None
            listed_raw = text("listed")
            if listed_raw not in (None, ""):
                listed_text = listed_raw.strip().lower()
                if listed_text not in ("true", "false"):
                    return self._send(400, {"error": "listed must be true or false.", "code": "rejected"})
                listed = listed_text == "true"
            else:
                listed = False
            allowed, tester = self._rate_block_or_tester()
            if not allowed:
                return
            if app.store.queued_count() >= s.max_queue or app.store.running_count() >= s.max_concurrency:
                return self._send(503, {"error": "Busy, try in a minute.", "code": "busy"})
            token = claim_token()
            job_id = new_id("h")
            upload_key = f"jobs/{job_id}/take-audio"
            kind = assembly._audio_kind(audio) or "wav"
            app.storage.put(upload_key, audio, "audio/flac" if kind == "flac" else "audio/wav")
            req = {
                "kind": "keep-take",
                "composition": gathered["composition"],
                "gather_id": gathered["gather_id"],
                "take_state": take_state,
                "upload_key": upload_key,
                "visibility": "public" if listed else "unlisted",
                "seed": int(take_state["plan"].get("seed") or gathered["seed"]),
                "keep": True,
                "title": title,
                "claim_hash": sha256_text(token),
                "tester": tester,
            }
            app.store.create({
                "id": job_id,
                "state": "queued",
                "stage": "queued",
                "request": req,
                "ip_hash": self._ip_hash(),
                "claim_hash": req["claim_hash"],
            })
            app.runner.run_job(job_id)
            job = app.store.get(job_id) or {}
            state = job.get("state")
            record_url = f"{s.public_base_url}/records/{job_id}.json"
            page_url = f"{s.public_base_url}/records/{job_id}"
            if state == "done":
                return self._send(200, {
                    "id": job_id,
                    "gather_id": gathered["gather_id"],
                    "status": "done",
                    "state": "done",
                    "claim_token": token,
                    "record_url": record_url,
                    "page_url": page_url,
                    "poll": f"/api/jobs/{job_id}",
                })
            code = 400 if state == "rejected" else 500
            return self._send(code, {
                "error": job.get("error") or "Keep failed.",
                "code": job.get("error_code") or "error",
                "id": job_id,
                "gather_id": gathered["gather_id"],
                "poll": f"/api/jobs/{job_id}",
            })

        def _job(self, job_id):
            if not ID_RE.match(job_id):
                return self._send(404, {"error": "not found"})
            job = app.store.get(job_id)
            if not job:
                meta = _read_json(app.storage, f"performances/{job_id}/meta.json")
                status = _read_json(app.storage, f"performances/{job_id}/status.json") or _read_json(app.storage, f"jobs/{job_id}/status.json")
                if not meta and not status:
                    return self._send(404, {"error": "not found"})
                state = (status or {}).get("state") or "done"
                return self._send(200, _public_job(job_id, state, state, 100 if state == "done" else 0, (status or {}).get("error"), app, status=status))
            pos = app.store.position(job_id) if job["state"] == "queued" else None
            self._send(200, _public_job(job_id, job["state"], job["stage"], job["percent"], job.get("error"), app, pos, job=job))

        def _gather_audio(self, job_id, sid, fetch_missing=True):
            if not ID_RE.match(job_id) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", sid):
                return self._send(404, {"error": "not found"})
            # The worker copies stored bytes after Gather. It must not download
            # the collection. A person playing one preview fetches that file.
            from ncperf.assembly import CACHE_CONTROL
            if not fetch_missing:
                doc = load_collection(app.storage, job_id) or {}
                row = next((f for f in (doc.get("files") or []) if isinstance(f, dict) and f.get("sid") == sid), None)
                data = app.storage.get(f"gathers/{job_id}/audio/{sid}")
                if not data:
                    return self._send(404, {"error": "not found"})
                from ncperf.gather import _audio_type
                return self._send(200, data, _audio_type((row or {}).get("ext")), extra={"Cache-Control": CACHE_CONTROL})
            from ncperf.gather import read_audio
            got = read_audio(app.storage, job_id, sid)
            if not got:
                return self._send(404, {"error": "not found"})
            data, ctype = got
            self._send(200, data, ctype, extra={"Cache-Control": CACHE_CONTROL})

        def _file(self, job_id, name):
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name):
                return self._send(404, {"error": "not found"})
            if name not in FILE_TYPES and not _named_in_record(app, job_id, name):
                return self._send(404, {"error": "not found"})
            if _expired_audio(app, job_id, name):
                return self._send(410, {"error": "This audio has expired. The record is still here.", "code": "expired"})
            if _tombstone(app, job_id) and _is_audio(name):
                return self._send(410, {"error": "This Performance was taken down.", "code": "gone"})
            data = app.storage.get(f"performances/{job_id}/{name}")
            if data is None:
                return self._send(404, {"error": "not found"})
            extra = {}
            meta = _read_json(app.storage, f"performances/{job_id}/meta.json") or {}
            if meta.get("visibility") != "public":
                extra["X-Robots-Tag"] = "noindex"
            self._send(200, data, _ctype(name), extra)

        def _record(self, job_id, as_json: bool):
            meta = _read_json(app.storage, f"performances/{job_id}/meta.json") or {}
            tomb = meta.get("tombstone")
            raw = app.storage.get(f"performances/{job_id}/record.json")
            if tomb:
                body = {"tombstone": True, "id": job_id, "record_hash": tomb.get("record_hash"), "reason": tomb.get("reason")}
                return self._send(410, body if as_json else pages_tomb(job_id, tomb), "application/json" if as_json else "text/html; charset=utf-8")
            if raw is None:
                return self._send(404, {"error": "not found"})
            extra = {"Cache-Control": "public, max-age=31536000, immutable"}
            if meta.get("visibility") != "public":
                extra["X-Robots-Tag"] = "noindex"
            if as_json:
                return self._send(200, raw, "application/json", extra)
            rec = json.loads(raw)
            self._send(200, pages.record_page(rec, meta, job_id), "text/html; charset=utf-8", extra)

        def _alias(self, digest: str):
            job_id = app.storage.get(f"aliases/sha256/{digest}")
            if not job_id:
                return self._send(404, {"error": "not found"})
            ident = job_id.decode().strip()
            self.send_response(302)
            self._cors()
            self.send_header("Location", f"/records/{ident}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _verify(self, job_id):
            raw = app.storage.get(f"performances/{job_id}/record.json")
            if raw is None:
                return self._send(404, {"error": "not found"})
            import tempfile
            import shutil
            d = tempfile.mkdtemp(prefix="nc-verify-")
            try:
                path = os.path.join(d, "record.json")
                open(path, "wb").write(raw)
                rec = json.loads(raw)
                names = [os.path.basename(o.get("file") or "") for o in (rec.get("performance") or {}).get("outputs") or []]
                names += ["performance.flac", "preview.mp3"]
                for name in names:
                    if not name:
                        continue
                    blob = app.storage.get(f"performances/{job_id}/{name}")
                    if blob and not os.path.exists(os.path.join(d, name)):
                        open(os.path.join(d, name), "wb").write(blob)
                ok, lines = R.verify(path, audio_dir=d)
            finally:
                shutil.rmtree(d, ignore_errors=True)
            signed = False
            rec = json.loads(raw)
            if app.signer:
                from .sign import verify_signature
                signed = verify_signature(rec, app.signer.public)
            self._send(200, {"ok": ok, "lines": lines, "signature_ok": signed, "id": job_id})

        def _events(self, job_id):
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = None
            deadline = time.monotonic() + app.settings.job_timeout_s
            while time.monotonic() < deadline:
                job = app.store.get(job_id)
                if not job:
                    break
                view = _public_job(job_id, job["state"], job["stage"], job["percent"], job.get("error"), app, job=job)
                heard = tuple(
                    (row.get("chord"), row.get("id"), row.get("title"), row.get("supplier"), row.get("license"), row.get("preview_url"))
                    for row in (view.get("found_sounds") or [])
                    if isinstance(row, dict)
                )
                snap = (job["state"], job["stage"], job["percent"], view.get("stage_line"), heard, view.get("trimmed"), view.get("preview_ready"))
                if snap != last:
                    payload = json.dumps(view)
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    last = snap
                if job["state"] in ("done", "failed", "rejected", "cancelled", "gathered"):
                    break
                app.runner._stop.wait(0.25)
            self.close_connection = True

        def _cancel(self, job_id):
            job = app.store.get(job_id)
            if not job:
                return self._send(404, {"error": "not found"})
            try:
                body = json.loads(self._read(8000).decode() or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise Reject("Body must be JSON.")
            if not isinstance(body, dict):
                raise Reject("Body must be a JSON object.")
            token = token_from(body)
            if not token:
                return self._send(400, {"error": "claim_token is required.", "code": "rejected"})
            meta = _read_json(app.storage, f"performances/{job_id}/meta.json") or {}
            stored = job.get("claim_hash") or meta.get("claim_token_sha256")
            if not claim_matches(stored, token):
                return self._send(403, {"error": "claim_token does not match this Performance."})
            if job.get("state") in ("done", "failed", "rejected"):
                return self._send(200, _public_job(
                    job_id, job["state"], job["stage"], job["percent"], job.get("error"), app, None, job=job,
                ))
            app.runner.request_cancel(job_id)
            left = app.store.mark_cancelled(job_id, CANCELLED_MESSAGE)
            job = app.store.get(job_id) or job
            if left in ("done", "failed", "rejected"):
                return self._send(200, _public_job(
                    job_id, job["state"], job["stage"], job["percent"], job.get("error"), app, None, job=job,
                ))
            wipe_performance(app.storage, job_id)
            wipe_gather(app.storage, job_id)
            write_cancelled_status(app.storage, job_id)
            job = app.store.get(job_id) or job
            return self._send(200, _public_job(
                job_id, job["state"], job["stage"], job["percent"], job.get("error"), app, None, job=job,
            ))

        def _meta_post(self, job_id, action):
            ctype = self.headers.get("Content-Type") or ""
            if action == "keep" and "multipart/form-data" in ctype.lower():
                return self._keep_processed(job_id, ctype)
            try:
                body = json.loads(self._read(8000).decode() or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise Reject("Body must be JSON.")
            if not isinstance(body, dict):
                raise Reject("Body must be a JSON object.")
            meta = _read_json(app.storage, f"performances/{job_id}/meta.json")
            if not meta:
                return self._send(404, {"error": "not found"})
            if not claim_matches(meta.get("claim_token_sha256"), body.get("claim_token")):
                return self._send(403, {"error": "claim_token does not match this Performance."})
            if action == "keep":
                # JSON keep leaves the original audio where it is. It does not
                # delete a dialed file or clear live_dials from an earlier keep.
                meta["keep"] = True
                meta["retention"] = "forever"
                meta["audio_expires_at"] = None
            else:
                vis = body.get("visibility")
                if vis not in ("unlisted", "public"):
                    raise Reject("visibility must be unlisted or public.")
                meta["visibility"] = vis
                if vis == "public":
                    from datetime import timedelta
                    meta["public_after"] = (datetime.now(timezone.utc) + timedelta(hours=app.settings.public_delay_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    meta["audio_expires_at"] = None
                    meta["keep"] = True
                    meta["retention"] = "forever"
                else:
                    meta["public_after"] = None
            app.storage.put(f"performances/{job_id}/meta.json", json.dumps(meta, indent=1).encode(), "application/json")
            self._send(200, {"id": job_id, "visibility": meta["visibility"], "keep": meta["keep"], "audio_expires_at": meta["audio_expires_at"]})

        def _keep_processed(self, job_id, ctype):
            """Store the browser WAV. The signed record is not written back."""
            raw = self._read(K.KEPT_WAV_MAX_BYTES + K.KEPT_FORM_OVERHEAD)
            parts = K.parse_multipart(raw, ctype)
            meta = _read_json(app.storage, f"performances/{job_id}/meta.json")
            if not meta:
                return self._send(404, {"error": "not found"})
            if meta.get("tombstone"):
                return self._send(410, {"error": "This Performance was taken down.", "code": "gone"})
            token_b = parts.get("claim_token")
            if not token_b:
                raise Reject("claim_token is required.")
            try:
                token = token_b.decode("utf-8")
            except UnicodeDecodeError:
                raise Reject("claim_token must be text.")
            if not claim_matches(meta.get("claim_token_sha256"), token):
                return self._send(403, {"error": "claim_token does not match this Performance."})
            audio = parts.get("audio")
            dials_raw = parts.get("dials")
            if not audio:
                raise Reject("Attach the dialed audio as the form field 'audio'.")
            if dials_raw is None:
                raise Reject("Attach dials as the form field 'dials'.")
            if len(audio) > K.KEPT_WAV_MAX_BYTES:
                raise Reject(K.too_large_message(len(audio)))
            rec_raw = app.storage.get(f"performances/{job_id}/record.json")
            try:
                record = json.loads(rec_raw) if rec_raw else None
            except json.JSONDecodeError:
                record = None
            info = K.inspect_wav(audio, K.job_length_s(record) + K.KEPT_TAIL_S)
            dials = K.parse_dials(dials_raw)
            app.storage.put(f"performances/{job_id}/{K.KEPT_WAV}", audio, "audio/wav")
            mp3 = K.encode_kept_mp3(audio)
            if mp3:
                app.storage.put(f"performances/{job_id}/{K.KEPT_MP3}", mp3, "audio/mpeg")
            elif app.storage.exists(f"performances/{job_id}/{K.KEPT_MP3}"):
                app.storage.delete(f"performances/{job_id}/{K.KEPT_MP3}")
            meta["keep"] = True
            meta["retention"] = "forever"
            meta["audio_expires_at"] = None
            meta["live_dials"] = dials
            meta["live_audio"] = K.live_audio_meta(info, bool(mp3))
            app.storage.put(f"performances/{job_id}/meta.json", json.dumps(meta, indent=1).encode(), "application/json")
            base = app.settings.public_base_url.rstrip("/")
            self._send(200, {
                "ok": True,
                "processed": True,
                "dials": dials,
                "audio_url": f"{base}/api/jobs/{job_id}/files/{K.KEPT_WAV}",
                "id": job_id,
                "visibility": meta.get("visibility"),
                "keep": True,
                "audio_expires_at": None,
            })

        def _submit(self):
            s = app.settings
            raw = self._read(s.max_submit_bytes)
            ctype = self.headers.get("Content-Type") or ""
            parts = _parse_multipart(raw, ctype)
            rec_bytes = parts.get("record")
            if not rec_bytes:
                raise Reject("Attach the record as the form field 'record'.")
            try:
                rec = json.loads(rec_bytes)
            except json.JSONDecodeError as e:
                raise Reject(f"record is not JSON: {e}") from e
            import tempfile
            import shutil
            d = tempfile.mkdtemp(prefix="nc-submit-")
            try:
                open(os.path.join(d, "record.json"), "wb").write(rec_bytes)
                outputs = (rec.get("performance") or {}).get("outputs") or []
                by_name = {k: v for k, v in parts.items() if k not in ("record",)}
                for o in outputs:
                    name = os.path.basename(o.get("file") or "")
                    blob = by_name.get(name) or by_name.get(name.lower())
                    if blob is None and "flac" in (o.get("format") or "").lower():
                        blob = by_name.get("flac")
                    if blob is None and "mp3" in (o.get("format") or "").lower():
                        blob = by_name.get("mp3")
                    if blob is None:
                        raise Reject(f"Missing audio for {name}. Send it as a form field named {name}.")
                    open(os.path.join(d, name), "wb").write(blob)
                if parts.get("plan"):
                    open(os.path.join(d, "plan.json"), "wb").write(parts["plan"])
                ok, lines = R.verify(os.path.join(d, "record.json"), audio_dir=d)
                if not ok:
                    raise Reject("This record did not verify:\n" + "\n".join(lines), "verify")
                sig = rec.get("signature") or {}
                if sig.get("status") == "SIGNED" and sig.get("signed_by") == s.instance_name:
                    raise Reject("A kit submission cannot carry this instance's signature. Submit the unsigned record.")
                files = {"record.json": rec_bytes}
                if parts.get("plan"):
                    files["plan.json"] = parts["plan"]
                for o in outputs:
                    name = os.path.basename(o.get("file") or "")
                    blob = open(os.path.join(d, name), "rb").read()
                    files[name] = blob
                    fmt = (o.get("format") or name).lower()
                    if "flac" in fmt:
                        files["performance.flac"] = blob
                    elif "mp3" in fmt:
                        files["preview.mp3"] = blob
            finally:
                shutil.rmtree(d, ignore_errors=True)
            token = claim_token()
            job_id = new_id("k")
            store_kit_submission(s, app.storage, job_id, files, sha256_text(token))
            self._send(201, {
                "id": job_id,
                "status": "verified",
                "note": "Kit render, verified by hash; not re-rendered. Not signed by this instance.",
                "claim_token": token,
                "record_url": f"{s.public_base_url}/records/{job_id}.json",
                "page_url": f"{s.public_base_url}/records/{job_id}",
            })

        def _admin_enabled(self):
            if not _admin_ok(app, self):
                return
            body = json.loads(self._read(2000).decode() or "{}")
            app.store.set_flag("perform_enabled", "1" if body.get("enabled", True) else "0")
            self._send(200, {"perform_enabled": app.perform_enabled()})

        def _admin_moderation(self, action):
            if not _admin_ok(app, self):
                return
            body = json.loads(self._read(4000).decode() or "{}")
            job_id = body.get("id") or ""
            if not ID_RE.match(job_id):
                raise Reject("id must be an h- or k- record id.")
            meta = _read_json(app.storage, f"performances/{job_id}/meta.json")
            if not meta:
                return self._send(404, {"error": "not found"})
            if action == "hide":
                meta["hidden"] = True
                meta["visibility"] = "unlisted"
                meta["public_after"] = None
                app.storage.put(f"performances/{job_id}/meta.json", json.dumps(meta, indent=1).encode(), "application/json")
                return self._send(200, {"id": job_id, "hidden": True})
            raw = app.storage.get(f"performances/{job_id}/record.json")
            rec = json.loads(raw) if raw else {}
            rh = (rec.get("record_hash") or {}).get("value")
            if raw:
                try:
                    app.storage.put(f"sealed/{job_id}/record.json", raw, "application/json", overwrite=False)
                except FileExistsError:
                    pass
            reason = body.get("reason") or "taken down"
            tomb = {"record_hash": rh, "reason": reason, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            meta["tombstone"] = tomb
            app.storage.put(f"performances/{job_id}/meta.json", json.dumps(meta, indent=1).encode(), "application/json")
            app.storage.put(f"performances/{job_id}/record.json", json.dumps({
                "tombstone": True, "id": job_id, "record_hash": rh, "reason": reason,
            }).encode(), "application/json")
            for name in K.AUDIO_FILES:
                app.storage.delete(f"performances/{job_id}/{name}")
            self._send(200, {"id": job_id, "tombstone": tomb})

        def _internal_cancel(self, job_id):
            """Stop one job. The Worker calls this so a shared container stays up."""
            if not _internal_ok(app, self):
                return self._send(401, {"error": "internal token required"})
            job = app.store.get(job_id)
            if not job:
                return self._send(404, {"error": "not found"})
            if job.get("state") in ("done", "failed", "rejected"):
                return self._send(200, {"id": job_id, "state": job["state"], "stopped": False})
            app.runner.request_cancel(job_id)
            left = app.store.mark_cancelled(job_id, CANCELLED_MESSAGE)
            job = app.store.get(job_id) or job
            if left in ("done", "failed", "rejected"):
                return self._send(200, {"id": job_id, "state": job.get("state"), "stopped": False})
            wipe_performance(app.storage, job_id)
            wipe_gather(app.storage, job_id)
            write_cancelled_status(app.storage, job_id)
            return self._send(200, {"id": job_id, "state": "cancelled", "stopped": True})

        def _internal_post(self):
            if not _internal_ok(app, self):
                return self._send(401, {"error": "internal token required"})
            body = json.loads(self._read(app.settings.max_plan_bytes + 20000).decode() or "{}")
            # The Worker already created the id and the claim hash.
            job_id = body.get("id") or new_id("h")
            if not ID_RE.match(job_id):
                raise Reject("bad id")
            if not app.store.get(job_id):
                app.store.create({
                    "id": job_id,
                    "state": "queued",
                    "request": body.get("request") or body,
                    "ip_hash": body.get("ip_hash"),
                    "claim_hash": (body.get("request") or body).get("claim_hash"),
                })
            threading.Thread(target=lambda: _run_claimed(app, job_id), daemon=True).start()
            self._send(202, {"id": job_id, "state": "queued"})

        def _internal_get(self, path):
            if not _internal_ok(app, self):
                return self._send(401, {"error": "internal token required"})
            if path == "/internal/registry/meta":
                from ncperf import registry
                return self._send(200, {"etag": registry.current_etag()})
            if path == "/internal/registry":
                from ncperf import registry
                raw = registry.export_bytes()
                if not raw:
                    return self._send(204, b"")
                registry.mark_clean()
                return self._send(200, raw, "application/json")
            m = re.fullmatch(r"/internal/jobs/([hkg]-[0-9a-z]+)", path)
            if m:
                job = app.store.get(m.group(1))
                if not job:
                    return self._send(404, {"error": "not found"})
                from ncperf.progress import public_fields
                fields = public_fields(job, stage=job.get("stage"))
                result = job.get("result") if isinstance(job.get("result"), dict) else {}
                payload = {
                    "id": job["id"], "state": job["state"], "stage": job["stage"],
                    "percent": job["percent"], "error": job.get("error"),
                    "cost_usd": job.get("cost_usd") or 0,
                    "seed": fields["seed"],
                    "trimmed": fields["trimmed"],
                    "found_sounds": fields["found_sounds"],
                    "sounds": result.get("sounds") or [],
                    "stage_line": fields["stage_line"],
                    "preview_ready": result.get("preview_ready") is True,
                }
                if "clock" in result:
                    payload["clock"] = result.get("clock")
                return self._send(200, payload)
            m = re.fullmatch(r"/internal/jobs/([hkg]-[0-9a-z]+)/collection", path)
            if m:
                raw = app.storage.get(f"gathers/{m.group(1)}/collection.json")
                if not raw:
                    return self._send(404, {"error": "not found"})
                return self._send(200, raw, "application/json")
            m = re.fullmatch(r"/internal/jobs/([hkg]-[0-9a-z]+)/audio/([A-Za-z0-9._-]+)", path)
            if m:
                return self._gather_audio(m.group(1), m.group(2), fetch_missing=False)
            m = re.fullmatch(r"/internal/jobs/([hkg]-[0-9a-z]+)/files/([a-z0-9._-]+)", path)
            if m:
                return self._file(m.group(1), m.group(2))
            self._send(404, {"error": "not found"})

        def _internal_cache(self):
            if not _internal_ok(app, self):
                return self._send(401, {"error": "internal token required"})
            url = self.headers.get("X-Audio-Url") or ""
            if not url.startswith("https://"):
                raise Reject("X-Audio-Url is required.")
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 80 * 1024 * 1024:
                raise Reject("Audio is missing or too large.")
            data = self.rfile.read(n)
            ext = self.headers.get("X-Audio-Ext") or None
            range_raw = self.headers.get("X-Audio-Range")
            range_bytes = int(range_raw) if range_raw else None
            from ncperf import fetch as kit_fetch
            kit_fetch.seed_file(url, data, ext=ext, range_bytes=range_bytes)
            self._send(204, b"")

        def _internal_gather_file(self):
            """Copy one gathered object from R2 into this container before Keep renders it."""
            if not _internal_ok(app, self):
                return self._send(401, {"error": "internal token required"})
            key = self.headers.get("X-Gather-Key") or ""
            if not re.fullmatch(r"gathers/g-[0-9a-z]+/(collection\.json|meta\.json|status\.json|audio/[A-Za-z0-9._-]{1,80})", key):
                raise Reject("X-Gather-Key is not a gathered file.")
            data = _copied_bytes(
                self, 80 * 1024 * 1024,
                "The gathered file is missing or too large.",
                "The gathered file is incomplete.",
            )
            app.storage.put(key, data, self.headers.get("Content-Type") or "application/octet-stream")
            self._send(204, b"")

        def _internal_take_audio(self):
            """Copy the browser's take from R2 into this container before Save signs it."""
            if not _internal_ok(app, self):
                return self._send(401, {"error": "internal token required"})
            job_id = self.headers.get("X-Take-Job") or ""
            if not re.fullmatch(r"h-[0-9a-z]+", job_id):
                raise Reject("X-Take-Job is not a Performance id.")
            from ncperf.assembly import TAKE_AUDIO_MAX
            data = _copied_bytes(
                self, TAKE_AUDIO_MAX,
                "The take is missing or too large.",
                "The take is incomplete.",
            )
            app.storage.put(f"jobs/{job_id}/take-audio", data, self.headers.get("Content-Type") or "application/octet-stream")
            self._send(204, b"")

        def _internal_note_index(self):
            if not _internal_ok(app, self):
                return self._send(401, {"error": "internal token required"})
            raw = self._read(8 * 1024 * 1024)
            if not raw:
                raise Reject("The note index is empty.")
            try:
                doc = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise Reject("The note index is not JSON.") from exc
            if not isinstance(doc, dict) or not isinstance(doc.get("libraries"), list):
                raise Reject("The note index has no libraries.")
            from ncperf import noteindex
            noteindex.install_bytes(raw)
            self._send(204, b"")

        def _internal_registry(self):
            if not _internal_ok(app, self):
                return self._send(401, {"error": "internal token required"})
            from ncperf import registry
            raw = self._read(registry.MAX_BYTES)
            if not raw:
                raise Reject("The registry is empty.")
            try:
                registry.install_bytes(raw)
            except ValueError as exc:
                raise Reject(str(exc)) from exc
            registry.set_etag(self.headers.get("X-Registry-Etag") or "")
            self._send(204, b"")

        def _internal_registry_check(self):
            if not _internal_ok(app, self):
                return self._send(401, {"error": "internal token required"})
            from ncperf import registry
            registry.kick()
            self._send(202, {"ok": True, "pending": registry.pending_count()})

    return Handler


def _run_claimed(app: App, job_id: str):
    """claim_next might grab a different job. Run this id if it is still queued."""
    job = app.store.get(job_id)
    if job and job["state"] == "queued":
        app.store.update(job_id, state="planning", stage="planning", percent=5)
        app.runner.run_job(job_id)


def settings_timeout_loops(app: App) -> int:
    return max(1, app.settings.job_timeout_s)


def _admin_ok(app: App, handler) -> bool:
    token = app.settings.admin_token
    if not token:
        handler._send(404, {"error": "admin is off until NC_ADMIN_TOKEN is set"})
        return False
    got = handler.headers.get("Authorization") or ""
    import hmac
    if not hmac.compare_digest(got, f"Bearer {token}"):
        handler._send(401, {"error": "admin token required"})
        return False
    return True


def _declared_length(handler) -> int:
    """Byte length of one copied object.

    The port proxy may omit Content-Length and send the body chunked, or it
    may keep the bytes length-delimited while dropping that header. A positive
    Content-Length wins. Otherwise the Worker's X-NC-Content-Length is the
    count. Zero means the body was not framed.
    """
    def parse(name: str) -> int | None:
        raw = handler.headers.get(name)
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    stated = parse("Content-Length")
    hinted = parse("X-NC-Content-Length")
    if stated is not None and stated > 0:
        return stated
    if hinted is not None and hinted > 0:
        return hinted
    return stated or 0


def _read_chunked(rfile, limit: int, missing: str, incomplete: str) -> bytes:
    out = bytearray()
    while True:
        line = rfile.readline(128)
        if not line:
            raise Reject(incomplete)
        try:
            size = int(line.split(b";", 1)[0].strip() or b"0", 16)
        except ValueError as exc:
            raise Reject(incomplete) from exc
        if size < 0:
            raise Reject(incomplete)
        if size == 0:
            while True:
                extra = rfile.readline(1024)
                if extra in (b"\r\n", b"\n", b""):
                    break
            break
        if len(out) + size > limit:
            raise Reject(missing)
        block = rfile.read(size)
        if len(block) != size:
            raise Reject(incomplete)
        out += block
        delim = rfile.read(2)
        if delim != b"\r\n":
            raise Reject(incomplete)
    return bytes(out)


def _copied_bytes(handler, limit: int, missing: str, incomplete: str) -> bytes:
    """The body of one Keep copy. A missing length is the caller's rejection."""
    if "chunked" in (handler.headers.get("Transfer-Encoding") or "").lower():
        data = _read_chunked(handler.rfile, limit, missing, incomplete)
        if not data:
            raise Reject(missing)
        return data
    n = _declared_length(handler)
    if n <= 0 or n > limit:
        raise Reject(missing)
    data = handler.rfile.read(n)
    if len(data) != n:
        raise Reject(incomplete)
    return data


def _internal_ok(app: App, handler) -> bool:
    token = app.settings.internal_token
    if not token:
        return True
    got = handler.headers.get("Authorization") or ""
    import hmac
    return hmac.compare_digest(got, f"Bearer {token}")


def _composition(body: dict) -> dict:
    from ncperf import interpret as I
    if body.get("words"):
        comp = I.read_composition(body["words"])
    else:
        comp = {"title": body.get("title") or "Untitled", "composition": body.get("composition") or "",
                "performance": body.get("performance") or "", "composer": None, "file": None}
    if body.get("title"):
        comp["title"] = body["title"]
    if body.get("composition"):
        comp["composition"] = body["composition"]
    if body.get("performance") is not None and "performance" in body:
        comp["performance"] = body["performance"]
    # Create posts the performance notes as prompt. A blank performance keeps them.
    if not str(comp.get("performance") or "").strip() and body.get("prompt"):
        comp["performance"] = body.get("prompt")
    # Never invent a person's name. Blank stays anonymous.
    typed = body["composer"] if "composer" in body else comp.get("composer")
    comp["composer"] = (typed or "").strip() or "anonymous"
    if "instrument" in body and body.get("instrument") not in (None, ""):
        comp["instrument"] = _instrument(body.get("instrument"))
    return comp


def choose_seed(body: dict) -> int:
    """An omitted seed is random, so the first take is not always seed 1.

    perform_again also picks a new seed. An explicit seed is kept.
    """
    import secrets

    if body.get("perform_again") or body.get("seed") is None:
        return secrets.randbelow(2**31 - 1) or 1
    try:
        return int(body.get("seed")) or 1
    except (TypeError, ValueError):
        return 1


def _instrument(raw) -> str:
    text = str(raw or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}", text):
        raise Reject("instrument must be a short name, like choir, synth, or piano. It is a timbre hint, not a supplier.")
    return text


def _gather_doc(app: App, gather_id: str) -> dict | None:
    doc = load_collection(app.storage, gather_id)
    if not doc:
        return None
    if not doc.get("claim_hash"):
        meta = _read_json(app.storage, f"gathers/{gather_id}/meta.json") or {}
        job = app.store.get(gather_id) or {}
        doc["claim_hash"] = job.get("claim_hash") or meta.get("claim_token_sha256")
    return doc


def _gather_body(app: App, job_id: str, token: str, job: dict, excluded) -> dict:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    body = {
        "gather_id": job_id,
        "id": job_id,
        "claim_token": token,
        "seed": result.get("seed"),
        "sounds": result.get("sounds") or [],
        "poll": f"/api/jobs/{job_id}",
        "excluded_gather_id": excluded,
    }
    if "clock" in result:
        body["clock"] = result.get("clock")
    return body


def _perform_gather(app: App, body: dict):
    """None when this Perform searches. A tuple is an HTTP error. A dict is the kept collection."""
    raw = body.get("gather_id")
    if raw in (None, ""):
        return None
    gather_id = str(raw)
    if not ID_RE.match(gather_id) or not gather_id.startswith("g-"):
        return 404, {"error": "not found"}
    token = token_from(body)
    if not token:
        return 400, {"error": "claim_token is required.", "code": "rejected"}
    doc = _gather_doc(app, gather_id)
    job = app.store.get(gather_id)
    if not doc:
        if job and job.get("state") not in ("gathered", "failed", "rejected", "cancelled", "done"):
            return 409, {"error": "That Gather is not ready.", "code": "rejected", "gather_id": gather_id, "state": job.get("state")}
        return 404, {"error": "not found"}
    claim = doc.get("claim_hash") or (job or {}).get("claim_hash")
    if not claim_matches(claim, token):
        return 403, {"error": "claim_token does not match this Gather."}
    plan = doc.get("plan")
    comp = doc.get("composition")
    if not isinstance(plan, dict) or not isinstance(comp, dict):
        return 409, {"error": "That Gather is not ready.", "code": "rejected", "gather_id": gather_id}
    seed = plan.get("seed") if plan.get("seed") is not None else doc.get("seed") or 1
    try:
        seed = int(seed) or 1
    except (TypeError, ValueError):
        seed = 1
    return {"gather_id": gather_id, "plan": plan, "composition": comp, "seed": seed}


def _was_tester(job, status) -> bool:
    req = (job or {}).get("request") if isinstance((job or {}).get("request"), dict) else {}
    if req.get("tester") is True:
        return True
    return isinstance(status, dict) and status.get("tester") is True


def _public_job(job_id, state, stage, percent, error, app: App, position=None, job=None, status=None) -> dict:
    from ncperf.progress import public_fields

    if status is None and state == "done":
        status = _read_json(app.storage, f"performances/{job_id}/status.json") or _read_json(app.storage, f"jobs/{job_id}/status.json") or {}
    fields = public_fields(job, status if isinstance(status, dict) else None, stage=stage, position=position)
    out = {
        "id": job_id,
        "state": state,
        "stage": stage,
        "percent": percent or 0,
        "position": position,
        "error": error,
        "seed": fields["seed"],
        "trimmed": fields["trimmed"],
        "found_sounds": fields["found_sounds"],
        "stage_line": fields["stage_line"],
        "tester": _was_tester(job, status if isinstance(status, dict) else None),
    }
    sounds = None
    result = (job or {}).get("result") if isinstance((job or {}).get("result"), dict) else {}
    if isinstance(result.get("sounds"), list):
        sounds = result["sounds"]
    elif isinstance(status, dict) and isinstance(status.get("sounds"), list):
        sounds = status["sounds"]
    if str(job_id).startswith("g-"):
        out["gather_id"] = job_id
        out["sounds"] = sounds or []
        if "clock" in result:
            out["clock"] = result.get("clock")
        elif isinstance(status, dict) and "clock" in status:
            out["clock"] = status.get("clock")
    elif sounds:
        out["sounds"] = sounds
    preview_ready = result.get("preview_ready") is True or (isinstance(status, dict) and status.get("preview_ready") is True)
    if preview_ready and state != "done":
        base = app.settings.public_base_url.rstrip("/")
        out["preview_ready"] = True
        out["files"] = {"mp3": f"{base}/api/jobs/{job_id}/files/preview.mp3"}
    if state == "done":
        base = app.settings.public_base_url.rstrip("/")
        stored = status if isinstance(status, dict) else {}
        if stored.get("cost_estimate"):
            out["cost_estimate"] = stored["cost_estimate"]
        files = {
            "flac": f"{base}/api/jobs/{job_id}/files/performance.flac",
            "mp3": f"{base}/api/jobs/{job_id}/files/preview.mp3",
            "record": f"{base}/records/{job_id}.json",
            "plan": f"{base}/api/jobs/{job_id}/files/plan.json",
            "page": f"{base}/records/{job_id}",
        }
        meta = _read_json(app.storage, f"performances/{job_id}/meta.json") or {}
        extra = K.kept_files(meta, base, job_id)
        if extra:
            files.update(extra["files"])
            out["live_dials"] = extra["live_dials"]
            out["kept_note"] = extra["kept_note"]
        out["files"] = files
    return out


def _read_json(storage: Storage, key: str):
    raw = storage.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _tombstone(app: App, job_id: str) -> bool:
    meta = _read_json(app.storage, f"performances/{job_id}/meta.json") or {}
    return bool(meta.get("tombstone"))


def _is_audio(name: str) -> bool:
    low = name.lower()
    return low.endswith(".flac") or low.endswith(".mp3") or low.endswith(".wav")


def _ctype(name: str) -> str:
    if name in FILE_TYPES:
        return FILE_TYPES[name]
    low = name.lower()
    if low.endswith(".flac"):
        return "audio/flac"
    if low.endswith(".mp3"):
        return "audio/mpeg"
    if low.endswith(".wav"):
        return "audio/wav"
    if low.endswith(".json"):
        return "application/json"
    return "application/octet-stream"


def _named_in_record(app: App, job_id: str, name: str) -> bool:
    rec = _read_json(app.storage, f"performances/{job_id}/record.json") or {}
    for o in (rec.get("performance") or {}).get("outputs") or []:
        if os.path.basename(o.get("file") or "") == name:
            return True
    return False


def _expired_audio(app: App, job_id: str, name: str) -> bool:
    if name not in ("performance.flac", "preview.mp3") and not _is_audio(name):
        return False
    meta = _read_json(app.storage, f"performances/{job_id}/meta.json") or {}
    exp = meta.get("audio_expires_at")
    if not exp:
        return False
    return exp < datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _feed(app: App) -> list[dict]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for key in app.storage.list("performances/"):
        if not key.endswith("/meta.json"):
            continue
        meta = _read_json(app.storage, key)
        if not meta:
            continue
        if meta.get("visibility") != "public" or meta.get("hidden") or meta.get("tombstone"):
            continue
        after = meta.get("public_after") or ""
        if after and after > now:
            continue
        base = app.settings.public_base_url.rstrip("/")
        item = {
            "id": meta.get("id"),
            "url": f"{base}/records/{meta.get('id')}",
            "record": f"{base}/records/{meta.get('id')}.json",
            "created_at": meta.get("created_at"),
        }
        item.update(K.kept_feed(meta, base, meta.get("id")))
        out.append(item)
    return out


def pages_tomb(job_id: str, tomb: dict) -> str:
    import html as _html
    reason = _html.escape(tomb.get("reason") or "taken down")
    rh = _html.escape(tomb.get("record_hash") or "")
    return f"<!DOCTYPE html><html><head><meta name='robots' content='noindex'><title>Gone</title></head><body><h1>410 Gone</h1><p>{reason}</p><p>record_hash {rh}</p></body></html>"


def _parse_multipart(body: bytes, content_type: str, missing: str = "Submit the record as multipart/form-data.") -> dict[str, bytes]:
    m = re.search(r"boundary=([^;]+)", content_type)
    if not m:
        raise Reject(missing)
    boundary = m.group(1).strip().strip('"').encode()
    parts = {}
    for chunk in body.split(b"--" + boundary):
        if b"Content-Disposition" not in chunk:
            continue
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            head, sep, data = chunk.partition(b"\n\n")
        data = data.rstrip(b"\r\n")
        if data.endswith(b"--"):
            data = data[:-2].rstrip(b"\r\n")
        name_m = re.search(br'name="([^"]+)"', head)
        if not name_m:
            continue
        parts[name_m.group(1).decode()] = data
        file_m = re.search(br'filename="([^"]+)"', head)
        if file_m:
            parts[os.path.basename(file_m.group(1).decode())] = data
    return parts


def build_app(settings: Settings | None = None) -> App:
    settings = settings or load()
    os.makedirs(settings.data_dir, exist_ok=True)
    store = Store(os.path.join(settings.data_dir, "jobs.sqlite"))
    storage = open_storage(settings)
    signer = Signer.from_text(settings.signing_key) if settings.signing_key.strip() else None
    runner = Runner(settings, store, storage, signer)
    return App(settings, store, storage, signer, runner)


def serve(app: App) -> ThreadingHTTPServer:
    host, _, port = app.settings.bind.rpartition(":")
    httpd = ThreadingHTTPServer((host or "0.0.0.0", int(port or 8080)), make_handler(app))
    if app.settings.queue_autostart and app.settings.role != "container":
        n = max(1, app.settings.max_concurrency)
        for _ in range(n):
            threading.Thread(target=app.runner.pump_forever, daemon=True).start()
    return httpd


def main():
    app = build_app()
    httpd = serve(app)
    host, port = httpd.server_address[:2]
    print(f"nc-perform listening on http://{host}:{port}  storage={app.settings.storage}", flush=True)
    httpd.serve_forever()
