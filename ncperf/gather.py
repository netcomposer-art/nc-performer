"""Gather a collection, then Perform it later without searching again.

Gather reads the note index and asks every supplier, with a short deadline.
It does not download the note libraries and it does not measure pitch.
Perform downloads the files that collection kept, in parallel.
One-shot Perform does not call this module, and it keeps the 20 second budgets.
"""
from __future__ import annotations

import contextvars
import os
import threading
import time

from . import deadline
from . import fetch
from . import progress
from .discover import arm_wall, plan_for, reset_wall
from .parallel import run_all

_fetch_until: contextvars.ContextVar[float | None] = contextvars.ContextVar("nc_gather_fetch", default=None)

# Wall clock for one Gather. Searches run together, so this is the slowest
# supplier, not the sum. The note index is already on disk, so Gather does
# not spend this clock downloading note libraries.
ORIGIN_S = 4.0
NOTE_S = 4.0
FETCH_S = 5.0


def picked_from_plan(plan: dict) -> tuple[list[str], list[str]]:
    """Keys and sample ids to avoid on the next Gather."""
    keys: list[str] = []
    samples: list[str] = []
    for src in (plan.get("sources") or {}).values():
        if not isinstance(src, dict):
            continue
        for field in ("url", "id", "manifest", "title", "page"):
            val = src.get(field)
            if val in (None, ""):
                continue
            text = str(val)
            keys.append(text)
            if field in ("url", "id"):
                samples.append(text)
    return list(dict.fromkeys(keys)), list(dict.fromkeys(samples))


def source_rows(plan: dict) -> list[dict]:
    """URLs for the sounds this collection kept. The bytes are fetched later."""
    rows = []
    for sid, src in (plan.get("sources") or {}).items():
        if not isinstance(src, dict):
            continue
        url = src.get("audio_url") or src.get("url")
        if not url:
            continue
        ext = os.path.splitext(str(url).split("?")[0])[1] or ".bin"
        rows.append({
            "sid": sid,
            "url": url,
            "ext": ext,
            "range_bytes": src.get("range_bytes"),
        })
    return rows


def build(comp: dict, *, seed: int = 1, instrument: str | None = None, avoid_keys=None, avoid_sample_ids=None, host_ok_fn=None, registry_url=None, verify_live: bool = True, searchers=None, note_maps=None, progress_cb=None) -> dict:
    """Search and pick. If the previous collection leaves a chord empty, keep those sounds."""
    kwargs = dict(
        seed=seed,
        instrument=instrument,
        host_ok_fn=host_ok_fn,
        searchers=searchers,
        registry_url=registry_url,
        verify_live=verify_live,
        note_maps=note_maps,
        progress_cb=progress_cb,
        origin_budget_s=ORIGIN_S,
        texture_budget_s=ORIGIN_S,
        note_budget_s=NOTE_S,
    )
    avoided = bool(avoid_keys or avoid_sample_ids)
    # One clock for the whole pick. A file-reach check cannot add its own 25 seconds
    # after the supplier searches have already used the deadline.
    wall = arm_wall(ORIGIN_S)
    try:
        try:
            plan = plan_for(comp, avoid_keys=avoid_keys, avoid_sample_ids=avoid_sample_ids, **kwargs)
        except ValueError:
            if not avoided:
                raise
            plan = plan_for(comp, avoid_keys=None, avoid_sample_ids=None, **kwargs)
    finally:
        reset_wall(wall)
    plan["from_gather"] = True
    picks = [str(item) for item in (plan.pop("gather_picks", None) or []) if item]
    # The note index already names the files. Downloading them is Perform's job,
    # and a preview fetches the one file someone actually plays.
    files = source_rows(plan)
    keys, samples = picked_from_plan(plan)
    keys = list(dict.fromkeys(picks + keys))
    samples = list(dict.fromkeys([item for item in picks if item] + samples))
    return {"plan": plan, "files": files, "picked_keys": keys, "picked_sample_ids": samples}


def _fetch_limit(budget_s: float) -> float:
    until = _fetch_until.get()
    if until is None:
        return budget_s
    left = until - time.monotonic()
    if left <= 0.05:
        raise deadline.SearchTimeout("gather fetch")
    return min(budget_s, left)


def cache_sources(plan: dict, budget_s: float = FETCH_S) -> list[dict]:
    """Download the chosen files together. The deadline is one clock for the whole set."""
    sources = plan.get("sources") or {}
    rows: list[dict] = []
    lock = threading.Lock()
    token = _fetch_until.set(time.monotonic() + budget_s)

    def one(sid: str, src: dict) -> None:
        if not isinstance(src, dict) or not src.get("kind"):
            return
        try:
            with deadline.limit(_fetch_limit(budget_s)):
                resolved = fetch.resolve(dict(src))
        except Exception:
            return
        path = resolved.get("path")
        url = resolved.get("audio_url")
        if not path or not url or not os.path.exists(path):
            return
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return
        if not data:
            return
        ext = os.path.splitext(path)[1] or ".bin"
        row = {
            "sid": sid,
            "url": url,
            "ext": ext,
            "range_bytes": src.get("range_bytes"),
            "data": data,
        }
        with lock:
            rows.append(row)

    jobs = [lambda sid=sid, src=src: one(sid, src) for sid, src in sources.items()]
    try:
        if jobs:
            run_all(jobs, workers=min(8, len(jobs)))
        return rows
    finally:
        _fetch_until.reset(token)


def public_sounds(plan: dict, gather_id: str, base: str, files: list[dict] | None = None) -> list[dict]:
    """The list the page shows. preview_url plays the cached file."""
    base = (base or "").rstrip("/")
    by_sid = {row.get("sid"): row for row in (files or []) if isinstance(row, dict)}
    sounds = []
    for sid, src in (plan.get("sources") or {}).items():
        if not isinstance(src, dict):
            continue
        title = str(src.get("title") or "").strip()
        license_id = src.get("license")
        if isinstance(license_id, dict):
            license_id = license_id.get("id") or ""
        license_id = str(license_id or "").strip()
        supplier = progress.origin_label(src.get("kind") or src.get("origin"))
        if not title or not license_id or not supplier:
            continue
        file = by_sid.get(sid) or {}
        preview = f"{base}/api/jobs/{gather_id}/audio/{sid}" if base else f"/api/jobs/{gather_id}/audio/{sid}"
        row = {
            "id": sid,
            "title": title,
            "supplier": supplier,
            "license": license_id,
            "preview_url": preview,
            "page": str(src.get("page") or ""),
        }
        source_url = file.get("url") or src.get("audio_url") or src.get("url")
        if source_url:
            row["source_url"] = source_url
        sounds.append(row)
    return sounds


def save_collection(storage, gather_id: str, doc: dict, files: list[dict]) -> None:
    import json
    body = dict(doc)
    stored = []
    for row in files or []:
        sid = str(row.get("sid") or "")
        if not sid:
            continue
        data = row.get("data")
        if isinstance(data, (bytes, bytearray)) and data:
            storage.put(f"gathers/{gather_id}/audio/{sid}", bytes(data), _audio_type(row.get("ext")))
        if not row.get("url") and not (isinstance(data, (bytes, bytearray)) and data):
            continue
        stored.append({
            "sid": sid,
            "url": row.get("url"),
            "ext": row.get("ext") or ".bin",
            "range_bytes": row.get("range_bytes"),
        })
    body["files"] = stored
    storage.put(
        f"gathers/{gather_id}/collection.json",
        json.dumps(body, ensure_ascii=False).encode(),
        "application/json",
    )
    storage.put(
        f"gathers/{gather_id}/meta.json",
        json.dumps({
            "id": gather_id,
            "claim_token_sha256": body.get("claim_hash"),
        }).encode(),
        "application/json",
    )
    storage.put(
        f"gathers/{gather_id}/status.json",
        json.dumps({
            "id": gather_id,
            "state": "gathered",
            "stage_line": "Gathered.",
            "seed": body.get("seed"),
            "sounds": body.get("sounds") or [],
        }, ensure_ascii=False).encode(),
        "application/json",
    )


def read_audio(storage, gather_id: str, sid: str) -> tuple[bytes, str] | None:
    """One gathered file. The first play downloads that file and keeps the bytes."""
    doc = load_collection(storage, gather_id) or {}
    row = next((item for item in (doc.get("files") or []) if isinstance(item, dict) and item.get("sid") == sid), None)
    data = storage.get(f"gathers/{gather_id}/audio/{sid}")
    if data:
        return bytes(data), _audio_type((row or {}).get("ext"))
    src = ((doc.get("plan") or {}).get("sources") or {}).get(sid)
    if not isinstance(src, dict) or not src.get("kind"):
        return None
    try:
        resolved = fetch.resolve(dict(src))
    except Exception:
        return None
    path = resolved.get("path")
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if not data:
        return None
    ext = os.path.splitext(path)[1] or (row or {}).get("ext") or ".bin"
    storage.put(f"gathers/{gather_id}/audio/{sid}", data, _audio_type(ext))
    return data, _audio_type(ext)


def load_collection(storage, gather_id: str) -> dict | None:
    import json
    raw = storage.get(f"gathers/{gather_id}/collection.json")
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def restore_cache(storage, gather_id: str) -> int:
    """Write the gathered audio where fetch.download looks, and return how many files landed."""
    doc = load_collection(storage, gather_id)
    if not doc:
        return 0
    n = 0
    for row in doc.get("files") or []:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("sid") or "")
        url = row.get("url")
        if not sid or not url:
            continue
        data = storage.get(f"gathers/{gather_id}/audio/{sid}")
        if not data:
            continue
        fetch.seed_file(url, data, ext=row.get("ext"), range_bytes=row.get("range_bytes"))
        n += 1
    return n


def wipe_gather(storage, gather_id: str) -> None:
    for key in storage.list(f"gathers/{gather_id}/"):
        storage.delete(key)


def _audio_type(ext: str | None) -> str:
    low = (ext or "").lower()
    if low == ".flac":
        return "audio/flac"
    if low == ".mp3":
        return "audio/mpeg"
    if low in (".wav", ".wave"):
        return "audio/wav"
    if low == ".ogg":
        return "audio/ogg"
    if low in (".aif", ".aiff"):
        return "audio/aiff"
    return "application/octet-stream"
