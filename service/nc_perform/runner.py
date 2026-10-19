"""Run one Performance with the public kit.

Stages reported to the job row match the hosted plan:
queued, planning, fetching, measuring, rendering, reverb, mastering,
verifying, storing, done, failed, rejected.

The kit prints source and layer lines. Those lines move the stage. Pitch is
never shifted: the kit has no field for it, and verify-record checks.
"""
from __future__ import annotations

import contextlib
import json
import os
import resource
import shutil
import tempfile
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import ncperf.fetch as fetch
import ncperf.interpret as interpret
import ncperf.record as record
from ncperf.render import Renderer

from .config import Settings
from .ids import sha256_text
from .jobs import Store
from .limits import Reject, check_plan, load_suppliers
from .planner import cache_key, compute_cost, llm_cost, needs_llm
from .sign import Signer, apply_signature
from .storage import Storage


STAGES = ("queued", "planning", "fetching", "measuring", "rendering", "reverb", "mastering", "verifying", "storing", "done")
PERCENT = {s: i * 10 for i, s in enumerate(STAGES)}
PERCENT["done"] = 100


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_plus(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def egress_guard(settings: Settings):
    """Block the kit (and the LLM call) from any host outside the allowlist."""
    orig_http = fetch.http
    orig_urlopen = urllib.request.urlopen

    def check(url: str):
        host = (urlparse(url).hostname or "").lower()
        if not settings.host_allowed(host):
            raise Reject(f"Egress blocked for host {host}. It is not on NC_ALLOWED_HOSTS.")

    def http(url, *a, **kw):
        check(url)
        return orig_http(url, *a, **kw)

    def urlopen(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else req
        check(url)
        return orig_urlopen(req, *a, **kw)

    fetch.http = http
    urllib.request.urlopen = urlopen
    try:
        yield
    finally:
        fetch.http = orig_http
        urllib.request.urlopen = orig_urlopen


class Runner:
    def __init__(self, settings: Settings, store: Store, storage: Storage, signer: Signer | None):
        self.settings = settings
        self.store = store
        self.storage = storage
        self.signer = signer
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def pump_forever(self):
        # One thread per slot (see nc_perform.__main__). claim_next is the lock,
        # so NC_MAX_CONCURRENCY threads are the cap.
        while not self._stop.is_set():
            job = self.store.claim_next()
            if not job:
                self._stop.wait(0.25)
                continue
            self.run_job(job["id"])

    def pump_available(self, limit: int = 10) -> int:
        """Run queued jobs on this thread. Tests use this. Returns how many ran."""
        n = 0
        while n < limit:
            job = self.store.claim_next()
            if not job:
                break
            self.run_job(job["id"])
            n += 1
        return n

    def run_job(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if not job:
            return
        t0 = time.perf_counter()
        ru0 = resource.getrusage(resource.RUSAGE_SELF)
        stage_t = {}
        usage = {}
        try:
            self._run(job, stage_t, usage)
        except Reject as e:
            self._fail(job_id, "rejected", e.code, str(e))
        except Exception as e:
            traceback.print_exc()
            self._fail(job_id, "failed", "error", str(e) or e.__class__.__name__)
        finally:
            wall = time.perf_counter() - t0
            ru1 = resource.getrusage(resource.RUSAGE_SELF)
            cpu = max(0.0, (ru1.ru_utime + ru1.ru_stime) - (ru0.ru_utime + ru0.ru_stime))
            peak_mb = ru1.ru_maxrss / 1024.0  # Linux: kilobytes
            cost = compute_cost(cpu, wall, self.settings) + float(usage.get("usd") or 0)
            self.store.add_spend(cost)
            self.store.update(job_id, cost_usd=round(cost, 6))
            row = self.store.get(job_id) or {}
            print(json.dumps({
                "event": "job",
                "id": job_id,
                "ip_hash": row.get("ip_hash"),
                "outcome": row.get("state"),
                "error_code": row.get("error_code"),
                "stage_s": {k: round(v, 3) for k, v in stage_t.items()},
                "cpu_s": round(cpu, 3),
                "wall_s": round(wall, 3),
                "peak_rss_mb": round(peak_mb, 1),
                "llm_model": usage.get("model"),
                "tokens": usage.get("tokens"),
                "est_usd": round(cost, 6),
            }, ensure_ascii=False), flush=True)

    def _stage(self, job_id: str, stage: str, stage_t: dict, mark: list):
        now = time.perf_counter()
        if mark:
            stage_t[mark[0]] = stage_t.get(mark[0], 0) + (now - mark[1])
        mark[:] = [stage, now]
        self.store.update(job_id, state=stage, stage=stage, percent=PERCENT.get(stage, 0))

    def _run(self, job: dict, stage_t: dict, usage: dict) -> None:
        settings = self.settings
        job_id = job["id"]
        req = job["request"]
        mark = ["planning", time.perf_counter()]
        self._stage(job_id, "planning", stage_t, mark)

        if self.store.spend_today() >= settings.daily_hard_usd or self.store.flag("perform_enabled", "1" if settings.perform_enabled else "0") == "0":
            raise Reject("Perform is resting until tomorrow.", "resting")

        comp = req["composition"]
        seed = int(req.get("seed") or 1)
        plan = req.get("plan")
        mode = "plan"
        os.environ["NC_SUPPLIERS_JSON"] = settings.suppliers_json
        os.environ["NC_PERFORMER"] = settings.instance_name
        # The kit reads the registry URL once, at import. Point that at this
        # instance's setting so a self-hoster is not stuck on netcomposer.art.
        if fetch.REGISTRY != settings.suppliers_json:
            fetch.REGISTRY = settings.suppliers_json
            fetch._reg.clear()
        with egress_guard(settings):
            suppliers = None
            if plan is None:
                plan, mode = self._interpret(comp, seed, usage, force_stub=bool(req.get("force_stub")))
            # Supplier manifests are checked against the live registry. A failure
            # here is a rejection, before any audio is written.
            try:
                suppliers = load_suppliers(settings)
            except Exception as e:
                # A plan with no supplier sources can still render (library freesound
                # and uiowa). If the registry itself is down, supplier sources fail closed.
                if any((s or {}).get("kind") == "supplier" for s in (plan.get("sources") or {}).values()):
                    raise Reject(f"Could not read the Supplier registry: {e}") from e
            check_plan(plan, settings, suppliers)
            self._stage(job_id, "fetching", stage_t, mark)
            out = self._render(job_id, plan, comp, req, stage_t, mark)
        self._stage(job_id, "storing", stage_t, mark)
        self._store_outputs(job_id, out, req, mode)
        self._stage(job_id, "done", stage_t, mark)
        self.store.update(job_id, state="done", stage="done", percent=100, result={
            "record_url": f"{settings.public_base_url}/records/{job_id}.json",
            "page_url": f"{settings.public_base_url}/records/{job_id}",
            "interpreter": mode,
        })

    def _interpret(self, comp: dict, seed: int, usage: dict, force_stub: bool = False) -> tuple[dict, str]:
        settings = self.settings
        text = (comp.get("composition") or "") + "\n" + (comp.get("performance") or "")
        want = needs_llm(text) and not force_stub
        soft = self.store.spend_today() >= settings.daily_soft_usd
        if want and settings.llm_configured and not soft:
            key = cache_key(text, seed, settings.llm_model)
            cached = self.storage.get(f"plans/{key}.json")
            if cached:
                plan = json.loads(cached)
                usage["model"] = settings.llm_model
                usage["tokens"] = {"cached_plan": True}
                usage["usd"] = 0.0
                return plan, "llm-cache"
            plan, info = _llm(comp, seed, settings)
            usage["model"] = info.get("model") or settings.llm_model
            usage["tokens"] = info.get("usage") or {}
            usd = llm_cost(usage["tokens"], settings)
            usage["usd"] = usd
            self.storage.put(f"plans/{key}.json", json.dumps(plan).encode(), "application/json")
            return plan, "llm"
        if want and not settings.llm_configured:
            note = "LLM planning was not configured (no XAI_API_KEY or NC_LLM_BASE_URL); the stub ran."
        elif want and soft:
            note = "The daily AI budget is spent; the stub ran."
        else:
            note = None
        try:
            plan = interpret.stub(comp, seed=seed)
        except ValueError as e:
            raise Reject(str(e)) from e
        if note:
            plan.setdefault("notes", []).append(note)
            plan["interpreter"]["note"] = (plan["interpreter"].get("note") or "") + " " + note
        return plan, "stub"

    def _render(self, job_id, plan, comp, req, stage_t, mark) -> dict:
        settings = self.settings
        work = tempfile.mkdtemp(prefix="nc-perform-")
        try:
            os.environ["NC_PERFORMER"] = settings.instance_name
            comp_path = os.path.join(work, "composition.md")
            open(comp_path, "w", encoding="utf-8").write(_composition_md(comp))
            plan_path = os.path.join(work, "plan.json")
            json.dump(plan, open(plan_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

            def log(line: str):
                low = line.lower()
                if line.startswith("  source") or "license" in low:
                    self._stage(job_id, "fetching", stage_t, mark)
                elif "measured" in low or line.startswith("layer"):
                    self._stage(job_id, "measuring" if "measured" in low else "rendering", stage_t, mark)
                elif "reverb" in low or "dragonfly" in low:
                    self._stage(job_id, "reverb", stage_t, mark)

            self._stage(job_id, "rendering", stage_t, mark)
            # The 10 minute cap is enforced by the caller: the Cloudflare queue
            # consumer stops waiting, and `docker` can be given a stop timeout.
            # Renderer itself is one blocking call into the kit.
            flac, mp3, rlog = Renderer(plan, work, log=log).run("performance")
            self._stage(job_id, "mastering", stage_t, mark)
            preview = os.path.join(work, "preview.mp3")
            os.replace(mp3, preview)
            master = os.path.join(work, "performance.flac")
            if flac != master:
                os.replace(flac, master)
            record_url = f"{settings.public_base_url}/records/{job_id}.json"
            _tag(master, preview, plan.get("title") or comp.get("title") or "Performance",
                 comp.get("composer") or "anonymous", record_url)
            self._stage(job_id, "verifying", stage_t, mark)
            rec_path = os.path.join(work, "record.json")
            record.build(plan, plan_path, rlog, master, preview, rec_path,
                         performer=settings.instance_name, composition_path=comp_path)
            rec = json.load(open(rec_path, encoding="utf-8"))
            apply_signature(rec, self.signer, settings.instance_name)
            json.dump(rec, open(rec_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
            ok, lines = record.verify(rec_path, audio_dir=work)
            if not ok:
                raise Reject("verify-record failed:\n" + "\n".join(lines), "verify")
            files = {}
            for name in ("performance.flac", "preview.mp3", "record.json", "plan.json", "uses.json", "composition.md"):
                files[name] = open(os.path.join(work, name), "rb").read()
            return files
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _store_outputs(self, job_id: str, files: dict, req: dict, mode: str) -> None:
        base = f"performances/{job_id}/"
        types = {
            "performance.flac": "audio/flac",
            "preview.mp3": "audio/mpeg",
            "record.json": "application/json",
            "plan.json": "application/json",
            "uses.json": "application/json",
            "composition.md": "text/markdown",
        }
        for name, data in files.items():
            # The record is written once. A second run of the same id must not replace it.
            overwrite = name != "record.json"
            self.storage.put(base + name, data, types.get(name, "application/octet-stream"), overwrite=overwrite)
        rec = json.loads(files["record.json"])
        rh = (rec.get("record_hash") or {}).get("value")
        if rh:
            try:
                self.storage.put(f"aliases/sha256/{rh}", job_id.encode(), "text/plain", overwrite=False)
            except FileExistsError:
                pass
        meta = self._meta(job_id, req)
        self.storage.put(base + "meta.json", json.dumps(meta, indent=1).encode(), "application/json")
        status = {"id": job_id, "state": "done", "interpreter": mode, "at": _utc()}
        self.storage.put(base + "status.json", json.dumps(status, indent=1).encode(), "application/json")

    def _meta(self, job_id: str, req: dict) -> dict:
        visibility = req.get("visibility") or "unlisted"
        if visibility not in ("unlisted", "public"):
            visibility = "unlisted"
        keep = bool(req.get("keep"))
        public_audio = visibility == "public" or keep
        created = _utc()
        return {
            "id": job_id,
            "visibility": visibility,
            "retention": "forever" if public_audio else f"{self.settings.audio_retain_days}d",
            "keep": keep or visibility == "public",
            "owner": None,  # v1 has no accounts. A later account fills this without touching the record.
            "claim_token_sha256": req.get("claim_hash"),
            "created_at": created,
            "audio_expires_at": None if public_audio else _iso_plus(self.settings.audio_retain_days),
            "public_after": _iso_plus_hours(self.settings.public_delay_hours) if visibility == "public" else None,
            "hidden": False,
            "tombstone": None,
            "origin": "hosted",
        }

    def _fail(self, job_id: str, state: str, code: str, message: str) -> None:
        self.store.update(job_id, state=state, stage=state, percent=100, error=message, error_code=code)
        body = json.dumps({"id": job_id, "state": state, "error": message, "error_code": code, "at": _utc()}).encode()
        try:
            self.storage.put(f"jobs/{job_id}/status.json", body, "application/json")
        except Exception:
            pass


def _iso_plus_hours(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _composition_md(comp: dict) -> str:
    title = comp.get("title") or "Untitled"
    composer = comp.get("composer") or "anonymous"
    return f"""# {title}

**Composer of this Composition:** {composer} · **License out:** CC BY-SA 4.0
**Allowed transforms:** trim, gain, fade, filter, reverb, delay, time_stretch (no pitch-shift)

## Composition

{(comp.get('composition') or '').strip()}

## Performance

{(comp.get('performance') or '').strip()}

## Material rules

- Inputs: CC0, CC BY, CC BY-SA or public domain only.
- No pitch-shifting. Time-stretch is fine.
- Output license: CC BY-SA 4.0.
"""


def _tag(flac: str, mp3: str, title: str, artist: str, record_url: str) -> None:
    """Write the title, the CC BY-SA 4.0 URI, and the record URL into the files
    so the credit survives a download. FLAC is rewritten losslessly at 24-bit."""
    import subprocess
    comment = f"CC BY-SA 4.0. Record: {record_url}"
    license_uri = "https://creativecommons.org/licenses/by-sa/4.0/"
    tmp_f = flac + ".tag.flac"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-i", flac,
        "-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
        "-metadata", f"title={title}", "-metadata", f"artist={artist}",
        "-metadata", f"comment={comment}", "-metadata", f"license={license_uri}",
        "-metadata", f"description={comment}", tmp_f,
    ], check=True)
    os.replace(tmp_f, flac)
    tmp_m = mp3 + ".tag.mp3"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-i", mp3, "-c:a", "libmp3lame", "-b:a", "320k",
        "-metadata", f"title={title}", "-metadata", f"artist={artist}",
        "-metadata", f"comment={comment}", "-metadata", f"license={license_uri}",
        tmp_m,
    ], check=True)
    os.replace(tmp_m, mp3)


def _llm(comp: dict, seed: int, settings: Settings) -> tuple[dict, dict]:
    """Call the kit's LLM interpreter and keep the token usage it would otherwise drop.

    The real request is the kit's ``ncperf.interpret.llm``: POST
    ``{base}/chat/completions`` with ``Authorization: Bearer <key>`` and
    ``model`` (hosted default ``grok-4.3``). This function only records
    ``usage`` from that response.
    """
    last = {}
    orig = interpret._post

    def wrapped(base, key, body, timeout=180):
        r = orig(base, key, body, timeout=timeout)
        last["usage"] = r.get("usage") or {}
        last["model"] = r.get("model")
        return r

    interpret._post = wrapped
    try:
        plan = interpret.llm(
            comp, seed=seed, base_url=settings.llm_base_url,
            model=settings.llm_model, api_key=settings.llm_api_key, log=lambda *a: None,
        )
    except SystemExit as e:
        raise Reject(str(e) or "The AI plan was not valid, and the stub could not replace it.") from e
    finally:
        interpret._post = orig
    return plan, last


def store_kit_submission(settings: Settings, storage: Storage, job_id: str, files: dict, claim_hash: str) -> dict:
    """Store a kit record that verify-record already accepted. Not signed."""
    base = f"performances/{job_id}/"
    types = {"performance.flac": "audio/flac", "preview.mp3": "audio/mpeg",
             "record.json": "application/json", "plan.json": "application/json"}
    for name, data in files.items():
        storage.put(base + name, data, types.get(name, "application/octet-stream"), overwrite=name != "record.json")
    rec = json.loads(files["record.json"])
    rh = (rec.get("record_hash") or {}).get("value")
    if rh:
        try:
            storage.put(f"aliases/sha256/{rh}", job_id.encode(), "text/plain", overwrite=False)
        except FileExistsError:
            pass
    meta = {
        "id": job_id,
        "visibility": "unlisted",
        "retention": f"{settings.audio_retain_days}d",
        "keep": False,
        "owner": None,
        "claim_token_sha256": claim_hash,
        "created_at": _utc(),
        "audio_expires_at": _iso_plus(settings.audio_retain_days),
        "public_after": None,
        "hidden": False,
        "tombstone": None,
        "origin": "kit",
        "verified": "hash",
        "reproduced": False,
        "note": "Kit render, verified by hash; not re-rendered.",
    }
    storage.put(base + "meta.json", json.dumps(meta, indent=1).encode(), "application/json")
    return meta


def claim_matches(stored_hash: str | None, token: str | None) -> bool:
    if not stored_hash or not token:
        return False
    import hmac
    return hmac.compare_digest(stored_hash, sha256_text(token))
