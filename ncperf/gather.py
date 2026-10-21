"""Gather a collection, then Perform it later without searching again.

Gather asks every supplier for a recording of each chord, with a short deadline.
It does not download the files and it does not measure pitch. A single-note
library is used only when the note index can stack that chord, and only for a
chord that has no whole-chord recording. Perform places those clips in order.
The layered engine remains available as build(..., processed=True).
"""
from __future__ import annotations

import contextvars
import os
import threading
import time

from . import deadline
from . import fetch
from . import progress
from . import assemble
from .discover import arm_wall, plan_for, reset_wall
from .parallel import run_all

_fetch_until: contextvars.ContextVar[float | None] = contextvars.ContextVar("nc_gather_fetch", default=None)

# Wall clock for one Gather. Searches run together, so this is the slowest
# supplier, not the sum. The note index is already on disk, so Gather does
# not spend this clock downloading note libraries.
ORIGIN_S = 4.0
NOTE_S = 4.0
FETCH_S = 5.0
# One clock for the whole raw Gather. A chord with no live hit by then uses the index.
GATHER_S = 8.0


def picked_from_plan(plan: dict) -> tuple[list[str], list[str]]:
    """File identities to avoid on the next Gather.

    A display title such as "piano Cm7" is every voicing of that chord, so it
    is not an identity. A sliced note is url#note=midi, so the other pitches
    in that same file stay available.
    """
    keys: list[str] = []
    samples: list[str] = []
    for src in (plan.get("sources") or {}).values():
        if not isinstance(src, dict):
            continue
        url = src.get("audio_url") or src.get("url")
        pitch = src.get("expect_midi")
        if pitch is None:
            pitch = src.get("midi")
        ident = src.get("id")
        if url and pitch is not None:
            text = f"{url}#note={pitch}"
            keys.append(text)
            samples.append(text)
            if ident not in (None, ""):
                keys.append(str(ident))
                samples.append(str(ident))
            continue
        for field in ("url", "audio_url", "id", "manifest", "page"):
            val = src.get(field)
            if val in (None, ""):
                continue
            text = str(val)
            keys.append(text)
            if field in ("url", "audio_url", "id"):
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


def build(comp: dict, *, seed: int = 1, instrument: str | None = None, avoid_keys=None, avoid_sample_ids=None, host_ok_fn=None, registry_url=None, verify_live: bool = True, searchers=None, note_maps=None, progress_cb=None, processed: bool = False, step_cb=None) -> dict:
    """Search and pick. The default is one raw clip per chord.

    ``processed=True`` keeps the layered plan for the later processing step.
    If the previous collection leaves a chord empty, the pick is tried once more
    without those exclusions.
    """
    if processed:
        return _build_layered(
            comp, seed=seed, instrument=instrument, avoid_keys=avoid_keys,
            avoid_sample_ids=avoid_sample_ids, host_ok_fn=host_ok_fn,
            registry_url=registry_url, verify_live=verify_live, searchers=searchers,
            note_maps=note_maps, progress_cb=progress_cb,
        )
    kwargs = dict(
        seed=seed, instrument=instrument, host_ok_fn=host_ok_fn, searchers=searchers,
        registry_url=registry_url, verify_live=verify_live, note_maps=note_maps,
        progress_cb=progress_cb, origin_budget_s=ORIGIN_S, avoid_keys=avoid_keys,
        avoid_sample_ids=avoid_sample_ids, gather_deadline_s=GATHER_S,
        settle_s=assemble.GATHER_SETTLE_S,
        step_cb=step_cb,
    )
    avoided = bool(avoid_keys or avoid_sample_ids)
    from . import registry
    registry.begin_session()
    wall = arm_wall(GATHER_S)
    try:
        try:
            plan = assemble.plan_raw(comp, **kwargs)
        except ValueError:
            if not avoided:
                raise
            kwargs["avoid_keys"] = None
            kwargs["avoid_sample_ids"] = None
            plan = assemble.plan_raw(comp, **kwargs)
    finally:
        reset_wall(wall)
        registry.log_session()
    plan["from_gather"] = True
    picks = [str(item) for item in (plan.pop("gather_picks", None) or []) if item]
    files = source_rows(plan)
    keys, samples = picked_from_plan(plan)
    keys = list(dict.fromkeys(picks + keys))
    samples = list(dict.fromkeys([item for item in picks if item] + samples))
    return {"plan": plan, "files": files, "picked_keys": keys, "picked_sample_ids": samples}


def _build_layered(comp: dict, *, seed: int = 1, instrument: str | None = None, avoid_keys=None, avoid_sample_ids=None, host_ok_fn=None, registry_url=None, verify_live: bool = True, searchers=None, note_maps=None, progress_cb=None) -> dict:
    """The layered search. Off unless processing is turned on."""
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
    from . import registry
    registry.begin_session()
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
        registry.log_session()
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


def cache_sources(plan: dict, budget_s: float = FETCH_S, on_done=None) -> list[dict]:
    """Download the chosen files together. The deadline is one clock for the whole set.

    ``on_done()`` is called once per source as it finishes, found or not.
    """
    sources = plan.get("sources") or {}
    rows: list[dict] = []
    lock = threading.Lock()
    token = _fetch_until.set(time.monotonic() + budget_s)

    def one(sid: str, src: dict) -> None:
        try:
            fetch_one(sid, src)
        finally:
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass

    def fetch_one(sid: str, src: dict) -> None:
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
    """The list the page shows. preview_url plays the cached file.

    A raw collection is one row per chord, with the credit for that clip.
    """
    clips = plan.get("clips") if plan.get("assembly") == "raw" else None
    if isinstance(clips, list) and clips:
        return _clip_sounds(clips, gather_id, base, files)
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


def _clip_sounds(clips: list, gather_id: str, base: str, files: list[dict] | None) -> list[dict]:
    base = (base or "").rstrip("/")
    by_sid = {row.get("sid"): row for row in (files or []) if isinstance(row, dict)}
    sounds = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        sid = str(clip.get("id") or "")
        title = str(clip.get("title") or "").strip()
        license_id = str(clip.get("license") or "").strip()
        supplier = str(clip.get("supplier") or "").strip()
        if not sid or not title or not license_id or not supplier:
            continue
        preview = f"{base}/api/jobs/{gather_id}/audio/{sid}" if base else f"/api/jobs/{gather_id}/audio/{sid}"
        row = {
            "id": sid,
            "title": title,
            "supplier": supplier,
            "license": license_id,
            "credit": str(clip.get("credit") or ""),
            "preview_url": preview,
            "page": str(clip.get("page") or ""),
            "chord": clip.get("chord"),
            "symbol": clip.get("symbol") or "",
        }
        sids = [str(item) for item in (clip.get("sids") or []) if item]
        file = by_sid.get(sids[0]) if len(sids) == 1 else {}
        source_url = (file or {}).get("url")
        if not source_url and len(sids) == 1:
            source_url = None
        if source_url:
            row["source_url"] = source_url
        sounds.append(row)
    return sounds


def save_collection(storage, gather_id: str, doc: dict, files: list[dict], step_cb=None) -> None:
    import json
    body = dict(doc)
    stored = []

    def step(done=None, total=None):
        if step_cb:
            try:
                step_cb("storing", done, total)
            except Exception:
                pass

    step(total=sum(1 for row in files or [] if row.get("sid") and isinstance(row.get("data"), (bytes, bytearray)) and row.get("data")))
    for row in files or []:
        sid = str(row.get("sid") or "")
        if not sid:
            continue
        data = row.get("data")
        if isinstance(data, (bytes, bytearray)) and data:
            storage.put(f"gathers/{gather_id}/audio/{sid}", bytes(data), _audio_type(row.get("ext")))
            step()
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
    status = {
        "id": gather_id,
        "state": "gathered",
        "stage_line": "Gathered.",
        "seed": body.get("seed"),
        "sounds": body.get("sounds") or [],
    }
    if "clock" in body:
        status["clock"] = body.get("clock")
    storage.put(
        f"gathers/{gather_id}/status.json",
        json.dumps(status, ensure_ascii=False).encode(),
        "application/json",
    )


def _clip_for(doc: dict, sid: str) -> dict | None:
    plan = doc.get("plan") if isinstance(doc.get("plan"), dict) else {}
    for clip in plan.get("clips") or []:
        if isinstance(clip, dict) and str(clip.get("id") or "") == sid:
            return clip
    return None


def _stacked_preview(storage, gather_id: str, clip: dict) -> tuple[bytes, str] | None:
    sid = str(clip.get("id") or "")
    if not sid:
        return None
    key = f"gathers/{gather_id}/audio/{sid}"
    data = storage.get(key)
    if data:
        return bytes(data), "audio/wav"
    plan = {}
    doc = load_collection(storage, gather_id) or {}
    if isinstance(doc.get("plan"), dict):
        plan = doc["plan"]
    try:
        from .assemble import preview_clip
        data = preview_clip(plan, clip)
    except Exception:
        return None
    if not data:
        return None
    storage.put(key, data, "audio/wav")
    return data, "audio/wav"


def read_audio(storage, gather_id: str, sid: str) -> tuple[bytes, str] | None:
    """One gathered file. The first play downloads that file and keeps the bytes.

    A raw clip id plays that chord. A stack of notes is mixed on this request
    and kept, so the next play does not build it again.
    """
    doc = load_collection(storage, gather_id) or {}
    clip = _clip_for(doc, sid)
    if clip and len(clip.get("sids") or []) > 1:
        return _stacked_preview(storage, gather_id, clip)
    if clip and clip.get("sids"):
        sid = str(clip["sids"][0])
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
