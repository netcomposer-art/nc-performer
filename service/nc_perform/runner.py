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
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.error
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
import ncperf.assemble as assemble
import ncperf.assembly as assembly
from .limits import Reject, check_plan, load_suppliers, source_cap
from ncperf.cancel import MESSAGE as CANCELLED_MESSAGE
from ncperf.cancel import bind as bind_cancel
from ncperf.cancel import check as cancel_check
from ncperf.cancel import reset as reset_cancel
from ncperf import cancel as job_cancel
from ncperf.discover import plan_for
from ncperf.fit import fit_plan
import ncperf.gather as gather
from ncperf.gather import load_collection, public_sounds, restore_cache, save_collection, wipe_gather
from ncperf.progress import STAGE_LINES, merge_sounds, sounds_from_sources, with_preview
from .cancel import wipe_performance, write_cancelled_status
from .planner import PRICE_SOURCE, cache_key, compute_cost, llm_cost, llm_price_card, wants_model
from .sign import Signer, apply_signature
from .storage import Storage


STAGES = ("queued", "planning", "fetching", "measuring", "rendering", "reverb", "mastering", "verifying", "storing", "done")
PERCENT = {s: i * 10 for i, s in enumerate(STAGES)}
PERCENT["done"] = 100
PERCENT["gathering"] = 40
PERCENT["gathered"] = 100
_UNSET = object()


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_plus(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def egress_guard(settings: Settings):
    """Block the kit (and the LLM call) from any host outside the allowlist.

    urllib follows redirects inside urlopen, so each hop is checked again.
    A 5xx (including the proxy's 520 Origin is disallowed) is logged with the
    URL before it is stored on the job as ``HTTP Error …``.
    """
    orig_http = fetch.http
    orig_urlopen = urllib.request.urlopen

    def check(url: str):
        host = (urlparse(url).hostname or "").lower()
        if not settings.host_allowed(host):
            raise Reject(f"Egress blocked for host {host}. It is not on NC_ALLOWED_HOSTS.")

    class _RecheckRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            check(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(_RecheckRedirect)

    def http(url, *a, **kw):
        check(url)
        return orig_http(url, *a, **kw)

    def urlopen(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else req
        check(url if isinstance(url, str) else str(url))
        try:
            return opener.open(req, *a, **kw)
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                print(json.dumps({
                    "event": "egress_http",
                    "url": e.geturl() or url,
                    "status": e.code,
                    "reason": str(e.reason),
                }), flush=True)
            raise

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
        self._cancel_events: dict[str, threading.Event] = {}

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

    def request_cancel(self, job_id: str) -> None:
        """Wake a running render. The row is marked cancelled separately."""
        self._event_for(job_id).set()

    def _event_for(self, job_id: str) -> threading.Event:
        event = self._cancel_events.get(job_id)
        if event is None:
            event = threading.Event()
            self._cancel_events[job_id] = event
        return event

    def _guard(self, job_id: str) -> None:
        cancel_check()
        job = self.store.get(job_id)
        if job and job.get("state") == "cancelled":
            raise job_cancel.Cancelled(CANCELLED_MESSAGE)

    def _settle_cancelled(self, job_id: str) -> None:
        state = self.store.mark_cancelled(job_id, CANCELLED_MESSAGE)
        if state == "done":
            return
        wipe_performance(self.storage, job_id)
        wipe_gather(self.storage, job_id)
        write_cancelled_status(self.storage, job_id)

    def run_job(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if not job or job.get("state") in ("cancelled", "done", "failed", "rejected"):
            return
        token = bind_cancel(self._event_for(job_id))
        t0 = time.perf_counter()
        ru0 = resource.getrusage(resource.RUSAGE_SELF)
        stage_t = {}
        usage = {}
        try:
            try:
                self._run(job, stage_t, usage)
            except job_cancel.Cancelled:
                self._settle_cancelled(job_id)
            except Reject as e:
                self._fail(job_id, "rejected", e.code, str(e))
            except Exception as e:
                traceback.print_exc()
                self._fail(job_id, "failed", "error", str(e) or e.__class__.__name__)
        finally:
            reset_cancel(token)
            wall = time.perf_counter() - t0
            ru1 = resource.getrusage(resource.RUSAGE_SELF)
            cpu = max(0.0, (ru1.ru_utime + ru1.ru_stime) - (ru0.ru_utime + ru0.ru_stime))
            peak_mb = ru1.ru_maxrss / 1024.0  # Linux: kilobytes
            cost = compute_cost(cpu, wall, self.settings) + float(usage.get("usd") or 0)
            self.store.add_spend(cost)
            self.store.update(job_id, cost_usd=round(cost, 6))
            row = self.store.get(job_id) or {}
            req_row = row.get("request") if isinstance(row.get("request"), dict) else {}
            print(json.dumps({
                "event": "job",
                "id": job_id,
                "ip_hash": row.get("ip_hash"),
                "tester": req_row.get("tester") is True,
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
        self._guard(job_id)
        now = time.perf_counter()
        if mark:
            stage_t[mark[0]] = stage_t.get(mark[0], 0) + (now - mark[1])
        mark[:] = [stage, now]
        self._progress(job_id, stage=stage, stage_line=STAGE_LINES.get(stage, stage))

    def _snapshot(self, job_id: str) -> dict:
        job = self.store.get(job_id) or {}
        result = job.get("result")
        return dict(result) if isinstance(result, dict) else {}

    def _progress(self, job_id: str, *, stage: str | None = None, stage_line: str | None = None, sound: dict | None = None, sounds: list | None = None, seed=None, trimmed=_UNSET) -> None:
        result = self._snapshot(job_id)
        if stage_line:
            result["stage_line"] = stage_line
        if seed is not None:
            result["seed"] = int(seed)
        if trimmed is not _UNSET:
            result["trimmed"] = trimmed if isinstance(trimmed, str) and trimmed.strip() else None
        if sound or sounds:
            result["found_sounds"] = merge_sounds(result.get("found_sounds"), ([sound] if sound else []) + list(sounds or []))
        fields = {"result": result}
        if stage:
            fields["state"] = stage
            fields["stage"] = stage
            fields["percent"] = PERCENT.get(stage, 0)
        self.store.update(job_id, **fields)

    def _run(self, job: dict, stage_t: dict, usage: dict) -> None:
        settings = self.settings
        job_id = job["id"]
        req = job["request"]
        if self.store.spend_today() >= settings.daily_hard_usd or self.store.flag("perform_enabled", "1" if settings.perform_enabled else "0") == "0":
            raise Reject("Perform is resting until tomorrow.", "resting")

        if req.get("kind") == "gather":
            self._gather_job(job, stage_t)
            return
        if req.get("kind") == "keep-take":
            self._keep_job(job, stage_t)
            return

        mark = ["planning", time.perf_counter()]
        self._stage(job_id, "planning", stage_t, mark)

        comp = req["composition"]
        seed = int(req.get("seed") or 1)
        self._progress(job_id, seed=seed, trimmed=None)
        plan = req.get("plan")
        if plan is None and req.get("gather_id"):
            doc = load_collection(self.storage, req["gather_id"])
            if not doc or not isinstance(doc.get("plan"), dict):
                raise Reject("That Gather is not ready.", "rejected")
            plan = doc["plan"]
        if req.get("gather_id"):
            restore_cache(self.storage, req["gather_id"])
        processed = bool(req.get("processed")) and bool(settings.processed)
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
            if plan is None and not processed:
                instrument = (comp.get("instrument") or "").strip() or None

                def progress_cb(line, sound=None):
                    if isinstance(sound, dict):
                        sound = with_preview(sound, job_id, settings.public_base_url)
                    self._progress(job_id, stage_line=line, sound=sound, seed=seed)

                try:
                    built = gather.build(
                        comp,
                        seed=seed,
                        instrument=instrument,
                        host_ok_fn=settings.host_allowed,
                        registry_url=settings.suppliers_json,
                        progress_cb=progress_cb,
                    )
                except ValueError as exc:
                    raise Reject(str(exc)) from exc
                plan = built["plan"]
                mode = "raw"
            elif plan is None:
                plan, mode = self._interpret(
                    comp, seed, usage, force_stub=bool(req.get("force_stub")), job_id=job_id,
                )
            elif plan.get("assembly") == "raw":
                mode = "raw"
            elif not processed:
                raise Reject("This release assembles the recordings in order. Processing is off.")
            # Supplier manifests are checked against the live registry. A failure
            # here is a rejection, before any audio is written.
            try:
                suppliers = load_suppliers(settings)
            except Exception as e:
                # A plan with no supplier sources can still render (library freesound
                # and uiowa). If the registry itself is down, supplier sources fail closed.
                if any((s or {}).get("kind") == "supplier" for s in (plan.get("sources") or {}).values()):
                    raise Reject(f"Could not read the Supplier registry: {e}") from e
            # A generated plan must not fail its own validator. Pack or drop
            # until the caps hold, and record what was left out.
            fit_plan(
                plan,
                max_layers=settings.max_layers,
                max_sources=source_cap(plan, settings),
                max_chords=settings.max_chords,
                max_length_s=settings.max_length_s,
                max_plan_bytes=settings.max_plan_bytes,
            )
            check_plan(plan, settings, suppliers)
            trimmed = plan.get("trimmed")
            if plan.get("assembly") == "raw":
                heard = public_sounds(plan, job_id, settings.public_base_url.rstrip("/"), gather.source_rows(plan))
            else:
                heard = sounds_from_sources(plan.get("sources"))
            self._progress(
                job_id,
                seed=int(plan.get("seed") or seed),
                trimmed=trimmed if isinstance(trimmed, str) and trimmed.strip() else None,
                sounds=heard,
            )
            self._stage(job_id, "fetching", stage_t, mark)
            out = self._render(job_id, plan, comp, req, stage_t, mark)
        self._stage(job_id, "storing", stage_t, mark)
        self._store_outputs(job_id, out, req, mode, usage)
        self._guard(job_id)
        self._stage(job_id, "done", stage_t, mark)
        done = self._snapshot(job_id)
        done.update({
            "record_url": f"{settings.public_base_url}/records/{job_id}.json",
            "page_url": f"{settings.public_base_url}/records/{job_id}",
            "interpreter": mode,
            "seed": int(plan.get("seed") or seed),
            "stage_line": STAGE_LINES["done"],
        })
        trimmed = plan.get("trimmed")
        done["trimmed"] = trimmed if isinstance(trimmed, str) and trimmed.strip() else None
        if not self.store.mark_done(job_id, result=done):
            self._settle_cancelled(job_id)

    def _gather_job(self, job: dict, stage_t: dict) -> None:
        settings = self.settings
        job_id = job["id"]
        req = job["request"]
        mark = ["gathering", time.perf_counter()]
        self._stage(job_id, "gathering", stage_t, mark)
        comp = req["composition"]
        seed = int(req.get("seed") or 1)
        instrument = (comp.get("instrument") or "").strip() or None
        self._progress(job_id, seed=seed, trimmed=None)

        def progress_cb(line, sound=None):
            if isinstance(sound, dict):
                sound = with_preview(sound, job_id, settings.public_base_url)
            self._progress(job_id, stage_line=line, sound=sound, seed=seed)

        try:
            built = gather.build(
                comp,
                seed=seed,
                instrument=instrument,
                avoid_keys=req.get("avoid_keys"),
                avoid_sample_ids=req.get("avoid_sample_ids"),
                host_ok_fn=settings.host_allowed,
                registry_url=settings.suppliers_json,
                progress_cb=progress_cb,
            )
        except ValueError as exc:
            raise Reject(str(exc)) from exc
        plan = built["plan"]
        suppliers = None
        try:
            suppliers = load_suppliers(settings)
        except Exception as e:
            if any((s or {}).get("kind") == "supplier" for s in (plan.get("sources") or {}).values()):
                raise Reject(f"Could not read the Supplier registry: {e}") from e
        fit_plan(
            plan,
            max_layers=settings.max_layers,
            max_sources=source_cap(plan, settings),
            max_chords=settings.max_chords,
            max_length_s=settings.max_length_s,
            max_plan_bytes=settings.max_plan_bytes,
        )
        check_plan(plan, settings, suppliers)
        base = settings.public_base_url.rstrip("/")
        files = built["files"]
        clock = None
        if plan.get("assembly") == "raw":
            self._progress(job_id, stage_line="Copying the recordings…", seed=seed)
            try:
                files, measured = assembly.materialize(plan, files, budget_s=gather.FETCH_S)
            except ValueError as exc:
                raise Reject(str(exc)) from exc
            clock = assembly.written_clock(comp, plan)
        sounds = public_sounds(plan, job_id, base, files)
        if plan.get("assembly") == "raw":
            assembly.annotate_sounds(sounds, measured)
        trimmed = plan.get("trimmed")
        trimmed = trimmed if isinstance(trimmed, str) and trimmed.strip() else None
        doc = {
            "id": job_id,
            "seed": int(plan.get("seed") or seed),
            "claim_hash": req.get("claim_hash"),
            "composition": comp,
            "plan": plan,
            "sounds": sounds,
            "picked_keys": built["picked_keys"],
            "picked_sample_ids": built["picked_sample_ids"],
            "excluded_gather_id": req.get("excluded_gather_id"),
            "clock": clock,
        }
        save_collection(self.storage, job_id, doc, files)
        result = {
            "seed": int(plan.get("seed") or seed),
            "trimmed": trimmed,
            "found_sounds": sounds if plan.get("assembly") == "raw" else sounds_from_sources(plan.get("sources")),
            "sounds": sounds,
            "stage_line": STAGE_LINES["gathered"],
            "gather_id": job_id,
            "clock": clock,
        }
        self._guard(job_id)
        if not self.store.mark_gathered(job_id, result):
            self._settle_cancelled(job_id)

    def _keep_job(self, job: dict, stage_t: dict) -> None:
        """Check the page's plan, store its audio, and sign that take."""
        settings = self.settings
        job_id = job["id"]
        req = job["request"]
        mark = ["fetching", time.perf_counter()]
        self._stage(job_id, "fetching", stage_t, mark)
        gather_id = str(req.get("gather_id") or "")
        doc = load_collection(self.storage, gather_id)
        if not doc or not isinstance(doc.get("plan"), dict):
            raise Reject("That Gather is not ready.", "rejected")
        try:
            take_state = assembly.parse_take_state(req.get("take_state"), gather_id)
        except ValueError as exc:
            raise Reject(str(exc), "rejected") from exc
        kept = take_state["plan"]
        errors = assembly.validate(kept, doc, max_length_s=settings.max_length_s)
        if errors:
            raise Reject(errors[0], "rejected")
        upload = self.storage.get(req.get("upload_key") or f"jobs/{job_id}/take-audio")
        if not upload:
            raise Reject("The take audio was not stored.", "rejected")
        try:
            info = assembly.inspect_upload(upload, float(kept.get("total_s") or 0))
        except ValueError as exc:
            raise Reject(str(exc), "rejected") from exc
        title = req.get("title")
        if isinstance(title, str):
            title = title.strip() or None
        plan = assembly.plan_for_record(doc["plan"], kept, title=title)
        plan["interpreter"] = {"kind": "browser", "note": "browser assembly"}
        comp = dict(doc.get("composition") or {})
        if title:
            comp["title"] = title
        seed = int(plan.get("seed") or 1)
        self._progress(job_id, seed=seed, trimmed=None, sounds=doc.get("sounds") or [])
        out = self._store_uploaded(job_id, plan, take_state, upload, info, gather_id, comp, stage_t, mark)
        self._stage(job_id, "storing", stage_t, mark)
        self._store_outputs(job_id, out, req, "raw", {})
        self._read_back(job_id)
        self.storage.delete(f"jobs/{job_id}/take-audio")
        self._guard(job_id)
        self._stage(job_id, "done", stage_t, mark)
        done = self._snapshot(job_id)
        done.update({
            "record_url": f"{settings.public_base_url}/records/{job_id}.json",
            "page_url": f"{settings.public_base_url}/records/{job_id}",
            "interpreter": "browser",
            "seed": seed,
            "stage_line": STAGE_LINES["done"],
            "gather_id": gather_id,
        })
        done["trimmed"] = None
        if not self.store.mark_done(job_id, result=done):
            self._settle_cancelled(job_id)

    def _store_uploaded(self, job_id, plan, take_state, upload, info, gather_id, comp, stage_t, mark) -> dict:
        """Confirm the raw plan, then store the browser's file as the take."""
        settings = self.settings
        work = tempfile.mkdtemp(prefix="nc-keep-")
        try:
            os.environ["NC_PERFORMER"] = settings.instance_name
            comp_path = os.path.join(work, "composition.md")
            open(comp_path, "w", encoding="utf-8").write(_composition_md(comp, raw=True))
            plan_path = os.path.join(work, "plan.json")
            json.dump(plan, open(plan_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

            def log(line: str):
                self._stage(job_id, "rendering", stage_t, mark)

            self._stage(job_id, "rendering", stage_t, mark)
            try:
                assembly.render_take(
                    self.storage, gather_id, plan, take_state["plan"], work, log=log, encode=False,
                )
            except RuntimeError as exc:
                raise Reject(str(exc), "rejected") from exc
            try:
                master, preview = assembly.write_take_files(upload, work)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, RuntimeError) as exc:
                raise Reject("The take could not be stored.", "rejected") from exc
            with open(preview, "rb") as fh:
                self._publish_preview(job_id, fh.read())
            record_url = f"{settings.public_base_url}/records/{job_id}.json"
            _tag(master, preview, plan.get("title") or comp.get("title") or "Performance",
                 comp.get("composer") or "anonymous", record_url)
            self._stage(job_id, "verifying", stage_t, mark)
            rec_path = os.path.join(work, "record.json")
            rlog = json.load(open(os.path.join(work, "uses.json"), encoding="utf-8"))
            record.build(plan, plan_path, rlog, master, preview, rec_path,
                         performer=settings.instance_name, composition_path=comp_path)
            rec = json.load(open(rec_path, encoding="utf-8"))
            assembly.finish_browser_record(rec, take_state, info, master)
            apply_signature(rec, self.signer, settings.instance_name)
            note = rec.get("signature") or {}
            if note.get("status") == "SIGNED":
                note["note"] = (
                    "Signature covers record_hash (hex, UTF-8). The audio is the browser's take. "
                    "This instance checked the plan and stored that file."
                )
            elif note.get("status") == "UNSIGNED":
                note["note"] = (
                    "This instance has no NC_SIGNING_KEY. The record_hash still identifies the bytes. "
                    "The audio is the browser's take."
                )
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

    def _read_back(self, job_id: str) -> None:
        """done is not published until the stored bytes can be read back."""
        missing = []
        for name in ("performance.flac", "preview.mp3", "record.json", "plan.json", "uses.json", "composition.md"):
            got = self.storage.get(f"performances/{job_id}/{name}")
            if not got:
                missing.append(name)
        if missing:
            raise Reject("Stored files were not readable: " + ", ".join(missing), "verify")

    def _interpret(self, comp: dict, seed: int, usage: dict, force_stub: bool = False, job_id: str | None = None) -> tuple[dict, str]:
        settings = self.settings
        text = (comp.get("composition") or "") + "\n" + (comp.get("performance") or "")
        instrument = (comp.get("instrument") or "").strip() or None
        if instrument:
            text += "\ninstrument: " + instrument
        want = wants_model(text, settings) and not force_stub
        soft = self.store.spend_today() >= settings.daily_soft_usd
        discovered = None

        def progress_cb(line, sound=None):
            if job_id:
                self._progress(job_id, stage_line=line, sound=sound, seed=seed)

        try:
            discovered = plan_for(
                comp, seed=seed, instrument=instrument, host_ok_fn=settings.host_allowed,
                registry_url=settings.suppliers_json,
                progress_cb=progress_cb if job_id else None,
            )
            if isinstance(discovered, dict):
                discovered.pop("gather_picks", None)
        except Exception as exc:
            discovered = None
            discover_error = str(exc)
        else:
            discover_error = None
        if want and settings.llm_configured and not soft:
            key = cache_key(text, seed, settings.llm_model)
            cached = self.storage.get(f"plans/{key}.json")
            if cached:
                plan = json.loads(cached)
                usage["model"] = settings.llm_model
                usage["tokens"] = {"cached_plan": True}
                usage["usd"] = 0.0
                return plan, "llm-cache"
            plan, info = _llm(comp, seed, settings, discovered=discovered)
            usage["model"] = info.get("model") or settings.llm_model
            usage["tokens"] = info.get("usage") or {}
            usd = llm_cost(usage["tokens"], settings)
            usage["usd"] = usd
            self.storage.put(f"plans/{key}.json", json.dumps(plan).encode(), "application/json")
            return plan, "llm"
        if discovered is not None:
            if want and not settings.llm_configured:
                note = "LLM planning was not configured; the even-handed search was used."
            elif want and soft:
                note = "The daily AI budget is spent; the even-handed search was used."
            else:
                note = None
            if note:
                discovered.setdefault("notes", []).append(note)
            return discovered, "discover"
        if want and not settings.llm_configured:
            raise Reject(discover_error or "No licensed sounds came back, and no model is configured.")
        raise Reject(discover_error or "No licensed sounds came back from the sources that were queried.")

    def _render(self, job_id, plan, comp, req, stage_t, mark) -> dict:
        settings = self.settings
        work = tempfile.mkdtemp(prefix="nc-perform-")
        try:
            os.environ["NC_PERFORMER"] = settings.instance_name
            comp_path = os.path.join(work, "composition.md")
            raw = plan.get("assembly") == "raw"
            open(comp_path, "w", encoding="utf-8").write(_composition_md(comp, raw=raw))
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
            if raw:
                flac, mp3, rlog = assemble.render(plan, work, log=log)
            else:
                flac, mp3, rlog = Renderer(plan, work, log=log).run("performance")
                self._stage(job_id, "mastering", stage_t, mark)
            preview = os.path.join(work, "preview.mp3")
            os.replace(mp3, preview)
            # The page can play this as soon as the bytes are stored and read back.
            # The record and the signature finish after, and done still waits for them.
            with open(preview, "rb") as fh:
                self._publish_preview(job_id, fh.read())
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

    def _publish_preview(self, job_id: str, data: bytes) -> None:
        """Store the MP3 and say so only after those bytes can be read back.

        The record is not part of this claim. ``done`` still waits for it.
        """
        if not data:
            return
        key = f"performances/{job_id}/preview.mp3"
        self.storage.put(key, data, "audio/mpeg")
        got = self.storage.get(key)
        if not got or len(got) != len(data):
            return
        result = self._snapshot(job_id)
        result["preview_ready"] = True
        result["stage_line"] = "The recording is ready."
        self.store.update(job_id, result=result)

    def _store_outputs(self, job_id: str, files: dict, req: dict, mode: str, usage: dict | None = None) -> None:
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
        try:
            plan_doc = json.loads(files.get("plan.json") or b"{}")
        except Exception:
            plan_doc = {}
        trimmed = plan_doc.get("trimmed")
        snap = self._snapshot(job_id)
        seed = plan_doc.get("seed", (req or {}).get("seed"))
        try:
            seed = int(seed) if seed is not None else None
        except (TypeError, ValueError):
            seed = None
        status["trimmed"] = trimmed if isinstance(trimmed, str) and trimmed.strip() else None
        status["seed"] = seed
        status["found_sounds"] = merge_sounds(snap.get("found_sounds"), sounds_from_sources(plan_doc.get("sources")))
        status["stage_line"] = STAGE_LINES["done"]
        if (req or {}).get("tester") is True:
            status["tester"] = True
        card = llm_price_card(self.settings)
        spent = usage or {}
        status["cost_estimate"] = {
            "model": spent.get("model") or card["model"],
            "usd_per_million": card["usd_per_million"],
            "source": PRICE_SOURCE,
            "formula": card["formula"],
            "every_perform": card["every_perform"],
        }
        if spent.get("tokens"):
            status["cost_estimate"]["tokens"] = spent.get("tokens")
        if spent.get("usd") is not None and mode in ("llm", "llm-cache"):
            status["cost_estimate"]["usd"] = spent.get("usd")
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
        current = self.store.get(job_id)
        if current and current.get("state") == "cancelled":
            self._settle_cancelled(job_id)
            return
        self.store.update(job_id, state=state, stage=state, percent=100, error=message, error_code=code)
        failed = self.store.get(job_id) or {}
        failed_req = failed.get("request") if isinstance(failed.get("request"), dict) else {}
        note = {"id": job_id, "state": state, "error": message, "error_code": code, "at": _utc()}
        if failed_req.get("tester") is True:
            note["tester"] = True
        body = json.dumps(note).encode()
        try:
            self.storage.put(f"jobs/{job_id}/status.json", body, "application/json")
        except Exception:
            pass


def _iso_plus_hours(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _composition_md(comp: dict, raw: bool = False) -> str:
    title = comp.get("title") or "Untitled"
    composer = comp.get("composer") or "anonymous"
    transforms = "trim, fade, gain (no pitch-shift, no time-stretch)" if raw else "trim, gain, fade, filter, reverb, delay (no pitch-shift, no time-stretch)"
    return f"""# {title}

**Composer of this Composition:** {composer} · **License out:** CC BY-SA 4.0
**Allowed transforms:** {transforms}

## Composition

{(comp.get('composition') or '').strip()}

## Performance

{(comp.get('performance') or '').strip()}

## Material rules

- Inputs: CC0, CC BY, CC BY-SA or public domain only.
- No pitch-shifting and no time-stretching. Clips are trimmed and faded.
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


def _llm(comp: dict, seed: int, settings: Settings, discovered: dict | None = None) -> tuple[dict, dict]:
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
            discovered_plan=discovered,
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
