"""Checked-sounds registry.

Each sound is checked once. The row says ok or rejected, why, the pitch and
chord that were found, how long and how loud it is, the license and the credit,
and a fingerprint of the bytes. Gather and Perform read the row. They do not
download the file to check it again, do not measure its pitch again, and do
not open its credit page again.

A sound that is not in the registry is checked once, then written here. If the
Gather deadline is too close for that check, the sound is left out of this
take and queued. The next take can use it after the background check.

The file is one JSON document. Hosted Perform keeps it in R2 at
``registry/v1.json`` and copies it into the container the same way as the note
index. A bulk catalog that is read once and then looked up in memory does not
need a new D1 binding or a KV key per sound.

Recheck only when the fingerprint changes, ``checker_version`` changes, or the
license row is older than ``NC_REGISTRY_RECHECK_DAYS`` (default 90). That
number is a setting. It is not a cap. A license recheck runs in the background
and does not block the take that already has an ok row.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CHECKER_VERSION = 1
BUNDLED = os.path.join(os.path.dirname(__file__), "data", "checked-sounds.json")
CONTAINER = "/data/checked-sounds.json"
R2_KEY = "registry/v1.json"
MAX_BYTES = 8 * 1024 * 1024
MIN_CHECK_S = 0.35
REASONS = (
    "",
    "bad_license",
    "missing_credit",
    "wrong_pitch",
    "wrong_chord",
    "unreachable",
    "silent",
    "clipped",
    "too_short",
)

USE = "use"
SKIP = "skip"
CHECK = "check"
DEFER = "defer"

_LOCK = threading.Lock()
_MEM: dict | None = None
_DIRTY = False
_SESSION = {"hits": 0, "rejected": 0, "deferred": 0, "checks": 0, "license_due": 0}
_PENDING_EVENT = threading.Event()
_WORKER: threading.Thread | None = None


def recheck_days() -> float:
    """How many days an ok license may sit before a background recheck.

    ``NC_REGISTRY_RECHECK_DAYS`` raises or lowers this. Nothing here refuses
    a longer interval.
    """
    raw = os.environ.get("NC_REGISTRY_RECHECK_DAYS", "90")
    try:
        days = float(raw)
    except (TypeError, ValueError):
        return 90.0
    if days < 0:
        return 90.0
    return days


def reset() -> None:
    """Drop the in-process copy. Tests use this between files."""
    global _MEM, _DIRTY
    with _LOCK:
        _MEM = None
        _DIRTY = False
        for key in _SESSION:
            _SESSION[key] = 0


def begin_session() -> None:
    """Start counters for one Gather or Perform."""
    with _LOCK:
        for key in _SESSION:
            _SESSION[key] = 0


def canonical_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urllib.parse.urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw.split("#", 1)[0]
    path = parts.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def row_key(supplier: str, url: str, note: str | int | None = None) -> str:
    """Supplier id plus the canonical URL. A sliced file adds the MIDI note."""
    base = canonical_url(url)
    if note is not None and str(note) != "":
        base = f"{base}#note={note}"
    return f"{(supplier or '').strip().lower()}\n{base}"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _empty() -> dict:
    return {
        "v": 1,
        "checker_version": CHECKER_VERSION,
        "built": _now(),
        "recheck_days": recheck_days(),
        "sounds": {},
        "aliases": {},
        "pending": [],
        "stats": {"size": 0, "ok": 0, "rejected": 0},
    }


def _cache_path() -> str:
    from . import fetch
    return os.path.join(fetch.cache_dir(), "checked-sounds.json")


def _can_write_dir(path: str) -> bool:
    return os.path.isdir(path) and os.access(path, os.W_OK)


def writable_path() -> str:
    override = os.environ.get("NC_REGISTRY") or ""
    if override and os.path.abspath(override) != os.path.abspath(BUNDLED):
        return override
    if _can_write_dir("/data"):
        return CONTAINER
    return _cache_path()


def _read_file(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("sounds"), dict):
        return None
    return doc


def _load_disk() -> dict:
    override = os.environ.get("NC_REGISTRY") or ""
    if override:
        return _read_file(override) or _empty()
    for path in (CONTAINER, _cache_path(), BUNDLED):
        doc = _read_file(path)
        if doc is not None:
            return doc
    return _empty()


def _stamp_stats(doc: dict) -> dict:
    sounds = doc.get("sounds") or {}
    ok = 0
    rejected = 0
    for entry in sounds.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("verdict") == "rejected":
            rejected += 1
        elif entry.get("verdict") == "ok":
            ok += 1
    doc["stats"] = {"size": ok + rejected, "ok": ok, "rejected": rejected}
    doc["checker_version"] = CHECKER_VERSION
    doc["recheck_days"] = recheck_days()
    return doc


def load() -> dict:
    global _MEM, _DIRTY
    publish = False
    with _LOCK:
        if _MEM is None:
            _MEM = _stamp_stats(_load_disk())
            # The bundled seed has not been written to this machine yet. The
            # next pull can put it in R2, so hosted /api/config can count it.
            if (_MEM.get("sounds")) and not os.path.isfile(writable_path()):
                _DIRTY = True
                publish = True
        doc = _MEM
    if publish:
        _flush()
    return doc


def public_stats() -> dict:
    """Counts safe to put on /api/config and /health. No URLs and no secrets."""
    stats = dict((load().get("stats") or {}))
    return {
        "size": int(stats.get("size") or 0),
        "ok": int(stats.get("ok") or 0),
        "rejected": int(stats.get("rejected") or 0),
        "checker_version": CHECKER_VERSION,
        "recheck_days": recheck_days(),
    }


def session() -> dict:
    with _LOCK:
        snap = dict(_SESSION)
    total = snap["hits"] + snap["rejected"] + snap["deferred"] + snap["checks"]
    snap["lookups"] = total
    snap["hit_rate"] = (snap["hits"] / total) if total else None
    return snap


def log_session() -> dict:
    snap = session()
    print(json.dumps({"event": "checked_sounds", **snap}), flush=True)
    return snap


def _count(name: str) -> None:
    with _LOCK:
        _SESSION[name] = _SESSION.get(name, 0) + 1


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age_days(entry: dict) -> float | None:
    stamp = _parse_time(entry.get("license_checked_at") or entry.get("checked_at"))
    if stamp is None:
        return None
    return (datetime.now(timezone.utc) - stamp).total_seconds() / 86400.0


def _fingerprint_changed(entry: dict, fresh: dict | None) -> bool:
    if not fresh:
        return False
    old = entry.get("fingerprint") if isinstance(entry.get("fingerprint"), dict) else {}
    for field in ("etag", "last_modified", "size", "sha256"):
        left = old.get(field)
        right = fresh.get(field)
        if left not in (None, "") and right not in (None, "") and str(left) != str(right):
            return True
    return False


def time_left() -> float | None:
    """Seconds left on the Gather wall, or None when no wall is armed."""
    from . import discover
    until = discover._plan_until.get()
    if until is None:
        return None
    return until - time.monotonic()


def _background_on() -> bool:
    return os.environ.get("NC_REGISTRY_BACKGROUND", "1").strip().lower() not in ("0", "false", "no", "off")


def _resolve(doc: dict, key: str) -> dict | None:
    sounds = doc.get("sounds") or {}
    found = sounds.get(key)
    if isinstance(found, dict):
        return found
    alias = (doc.get("aliases") or {}).get(key)
    if alias:
        found = sounds.get(alias)
        if isinstance(found, dict):
            return found
    return None


def peek(supplier: str, url: str, note: str | int | None = None) -> dict | None:
    """The stored row, if any. Does not count a lookup and does not queue work."""
    doc = load()
    if note is not None:
        found = _resolve(doc, row_key(supplier, url, note))
        if found is not None:
            return found
    return _resolve(doc, row_key(supplier, url))


def consult(supplier: str, url: str, fingerprint: dict | None = None, note: str | int | None = None) -> tuple[str, dict | None]:
    """What this take should do with one sound.

    ``use`` means the stored credit and pitch are enough.
    ``skip`` means the row is rejected. This take does not try again.
    ``check`` means the sound is new, or the fingerprint or checker changed,
    and the Gather wall still has time.
    ``defer`` means it needs a check and the wall does not. It is queued.
    """
    if not url or not supplier:
        return CHECK, None
    entry = peek(supplier, url, note)
    if entry is None:
        left = time_left()
        if left is not None and left < MIN_CHECK_S:
            enqueue(supplier, url, "new", note=note)
            _count("deferred")
            return DEFER, None
        _count("checks")
        return CHECK, None
    version = int(entry.get("checker_version") or 0)
    changed = version != CHECKER_VERSION or _fingerprint_changed(entry, fingerprint)
    if changed:
        left = time_left()
        if left is not None and left < MIN_CHECK_S:
            enqueue(supplier, url, "fingerprint" if _fingerprint_changed(entry, fingerprint) else "version", note=note)
            _count("deferred")
            return DEFER, None
        _count("checks")
        return CHECK, entry
    age = _age_days(entry)
    due = age is None or age >= recheck_days()
    if entry.get("verdict") == "rejected":
        _count("rejected")
        if due:
            enqueue(supplier, url, "license", note=note)
            _count("license_due")
        return SKIP, entry
    if due:
        enqueue(supplier, url, "license", note=note)
        _count("license_due")
    _count("hits")
    return USE, entry


def credit_of(entry: dict | None) -> dict | None:
    """The credit dict Gather already knows how to copy onto a source."""
    if not entry or entry.get("verdict") != "ok":
        return None
    title = str(entry.get("title") or "").strip()
    author = str(entry.get("author") or "").strip()
    license_id = str(entry.get("license") or "").strip()
    page = str(entry.get("page") or "").strip()
    if not title or not author or not license_id or not page:
        return None
    out = {"title": title, "author": author, "license": license_id, "page": page}
    if entry.get("chord"):
        out["chord_symbol"] = entry["chord"]
    if entry.get("bass_pc") is not None:
        out["bass_pc"] = entry["bass_pc"]
    if entry.get("audio_url"):
        out["audio_url"] = entry["audio_url"]
    finger = entry.get("fingerprint") if isinstance(entry.get("fingerprint"), dict) else {}
    if finger.get("sha256"):
        out["sha256"] = finger["sha256"]
    if entry.get("loudness_lufs") is not None:
        out["loudness_lufs"] = entry["loudness_lufs"]
    if entry.get("duration_s") is not None:
        out["duration_s"] = entry["duration_s"]
    if entry.get("pitch_midi") is not None:
        out["pitch_midi"] = entry["pitch_midi"]
    if entry.get("cents") is not None:
        out["cents"] = entry["cents"]
    return out


def _fingerprint_from(credit: dict | None, fingerprint: dict | None) -> dict:
    out = {}
    if isinstance(fingerprint, dict):
        for field in ("etag", "last_modified", "size", "sha256"):
            if fingerprint.get(field) not in (None, ""):
                out[field] = fingerprint[field]
    if credit and credit.get("sha256") and "sha256" not in out:
        out["sha256"] = credit["sha256"]
    return out


def remember(supplier: str, url: str, entry: dict, note: str | int | None = None) -> None:
    """Write one checked row. The bundled file is never overwritten."""
    if not supplier or not url or not isinstance(entry, dict):
        return
    verdict = entry.get("verdict")
    if verdict not in ("ok", "rejected"):
        return
    reason = entry.get("reason") or ""
    if reason not in REASONS:
        reason = ""
    global _DIRTY
    doc = load()
    row = {
        "verdict": verdict,
        "reason": reason,
        "license": entry.get("license") or "",
        "title": entry.get("title") or "",
        "author": entry.get("author") or "",
        "page": entry.get("page") or canonical_url(url),
        "checked_at": entry.get("checked_at") or _now(),
        "license_checked_at": entry.get("license_checked_at") or entry.get("checked_at") or _now(),
        "checker_version": CHECKER_VERSION,
    }
    for field in ("pitch_midi", "cents", "chord", "note", "duration_s", "loudness_lufs", "audio_url", "bass_pc"):
        if entry.get(field) not in (None, ""):
            row[field] = entry[field]
    finger = entry.get("fingerprint") if isinstance(entry.get("fingerprint"), dict) else {}
    kept = {k: finger[k] for k in ("etag", "last_modified", "size", "sha256") if finger.get(k) not in (None, "")}
    if kept:
        row["fingerprint"] = kept
    with _LOCK:
        doc.setdefault("sounds", {})[row_key(supplier, url, note)] = row
        _stamp_stats(doc)
        doc["built"] = _now()
        _DIRTY = True
    _flush()


def remember_ok(supplier: str, url: str, credit: dict, fingerprint: dict | None = None, note: str | int | None = None, **fields) -> None:
    entry = {
        "verdict": "ok",
        "reason": "",
        "license": credit.get("license"),
        "title": credit.get("title"),
        "author": credit.get("author"),
        "page": credit.get("page") or url,
        "audio_url": credit.get("audio_url"),
        "chord": credit.get("chord_symbol") or fields.get("chord"),
        "bass_pc": credit.get("bass_pc", fields.get("bass_pc")),
        "pitch_midi": fields.get("pitch_midi"),
        "cents": fields.get("cents"),
        "note": fields.get("note"),
        "duration_s": fields.get("duration_s", credit.get("duration_s")),
        "loudness_lufs": fields.get("loudness_lufs", credit.get("loudness_lufs")),
        "fingerprint": _fingerprint_from(credit, fingerprint),
    }
    remember(supplier, url, entry, note=note)


def remember_rejected(supplier: str, url: str, reason: str, **fields) -> None:
    remember(supplier, url, {"verdict": "rejected", "reason": reason if reason in REASONS else "unreachable", **fields}, note=fields.get("note"))


def enqueue(supplier: str, url: str, why: str, note: str | int | None = None) -> None:
    if not supplier or not url:
        return
    global _DIRTY
    doc = load()
    key = (supplier.strip().lower(), canonical_url(url), None if note is None else str(note), why)
    with _LOCK:
        pending = doc.setdefault("pending", [])
        for item in pending:
            if (item.get("supplier"), item.get("url"), item.get("note"), item.get("why")) == key:
                break
        else:
            pending.append({
                "supplier": key[0],
                "url": key[1],
                "note": key[2],
                "why": why,
                "at": _now(),
            })
            if len(pending) > 500:
                del pending[:-500]
            _DIRTY = True
    _flush()
    kick()


def _pop_pending() -> dict | None:
    global _DIRTY
    doc = load()
    with _LOCK:
        pending = doc.get("pending") or []
        if not pending:
            return None
        item = pending.pop(0)
        doc["pending"] = pending
        _DIRTY = True
    _flush()
    return item


def _flush() -> None:
    doc = load()
    path = writable_path()
    folder = os.path.dirname(path)
    if folder:
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            return
    tmp = path + ".part"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def export_bytes() -> bytes | None:
    """The document when this process changed it. None when R2 is already current."""
    with _LOCK:
        dirty = _DIRTY
        doc = _MEM
    if not dirty or not isinstance(doc, dict):
        return None
    raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_BYTES:
        return None
    return raw


def mark_clean() -> None:
    global _DIRTY
    with _LOCK:
        _DIRTY = False


def _merge(base: dict, incoming: dict) -> dict:
    sounds = dict(base.get("sounds") or {})
    aliases = dict(base.get("aliases") or {})
    aliases.update({k: v for k, v in (incoming.get("aliases") or {}).items() if isinstance(v, str)})
    for key, row in (incoming.get("sounds") or {}).items():
        if not isinstance(row, dict):
            continue
        prev = sounds.get(key)
        if not isinstance(prev, dict) or str(row.get("checked_at") or "") >= str(prev.get("checked_at") or ""):
            sounds[key] = row
    pending = []
    seen = set()
    for item in list(base.get("pending") or []) + list(incoming.get("pending") or []):
        if not isinstance(item, dict):
            continue
        mark = (item.get("supplier"), item.get("url"), item.get("note"), item.get("why"))
        if mark in seen:
            continue
        seen.add(mark)
        pending.append(item)
    built = max(str(base.get("built") or ""), str(incoming.get("built") or ""))
    doc = _empty()
    doc["built"] = built or _now()
    doc["sounds"] = sounds
    doc["aliases"] = aliases
    doc["pending"] = pending[-500:]
    return _stamp_stats(doc)


def install_bytes(raw: bytes) -> str:
    """Merge a posted registry over the rows already in this process."""
    global _MEM, _DIRTY
    try:
        incoming = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("The registry is not JSON.") from exc
    if not isinstance(incoming, dict) or not isinstance(incoming.get("sounds"), dict):
        raise ValueError("The registry has no sounds.")
    current = load()
    merged = _merge(current, incoming)
    with _LOCK:
        _MEM = merged
        _DIRTY = True
    path = writable_path()
    _flush()
    return path


def install_doc(doc: dict) -> None:
    install_bytes(json.dumps(doc).encode("utf-8"))


def kick() -> None:
    """Start the background checker. A new sound does not wait on this thread."""
    global _WORKER
    if not _background_on():
        return
    _PENDING_EVENT.set()
    with _LOCK:
        worker = _WORKER
        if worker is not None and worker.is_alive():
            return
        worker = threading.Thread(target=_drain, name="nc-registry", daemon=True)
        _WORKER = worker
        worker.start()


def pending_count() -> int:
    return len(load().get("pending") or [])


def _drain() -> None:
    idle = 0
    while True:
        item = _pop_pending()
        if item is None:
            if not _PENDING_EVENT.wait(2.0):
                idle += 1
                if idle >= 2 and not (load().get("pending") or []):
                    return
            else:
                idle = 0
            _PENDING_EVENT.clear()
            continue
        idle = 0
        try:
            _check_item(item)
        except Exception as exc:
            print(json.dumps({"event": "registry_check", "error": str(exc)[:180]}), flush=True)


def _check_item(item: dict) -> None:
    supplier = str(item.get("supplier") or "")
    url = str(item.get("url") or "")
    why = str(item.get("why") or "")
    note = item.get("note")
    if not supplier or not url:
        return
    if supplier == "freesound":
        _check_freesound(supplier, url, why, note)
        return
    if supplier in ("musicmatter", "supplier") or url.endswith(".nc.json"):
        _check_manifest(supplier, url, why, note)
        return
    if why == "license":
        _touch_license(supplier, url, note)
        return


def _check_manifest(supplier: str, url: str, why: str, note) -> None:
    from .discover import _manifest_credit_fetch
    credit, reason = _manifest_credit_fetch(url)
    if credit:
        remember_ok(supplier, url, credit, note=note)
        return
    # A dead host does not erase an ok row. The next take can queue it again.
    if why == "license" and reason == "unreachable":
        return
    remember_rejected(supplier, url, reason or "unreachable", page=url, note=note)


def _check_freesound(supplier: str, url: str, why: str, note) -> None:
    from .discover import _freesound_credit_fetch
    match = re.search(r"/s/(\d+)", url)
    if not match:
        remember_rejected(supplier, url, "unreachable", page=url, note=note)
        return
    credit, reason = _freesound_credit_fetch(match.group(1))
    if credit:
        remember_ok(supplier, url, credit, note=note)
        return
    if why == "license" and reason == "unreachable":
        return
    remember_rejected(supplier, url, reason or "unreachable", page=url, note=note)


def _touch_license(supplier: str, url: str, note) -> None:
    """A page we cannot re-parse. A live page keeps the row; a dead one does not."""
    entry = peek(supplier, url, note)
    if not entry:
        return
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "nc-performer-kit/0.1 (+https://netcomposer.art/agents/)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            status = getattr(resp, "status", 200)
            if status >= 400:
                raise OSError(f"HTTP {status}")
    except Exception:
        return
    kept = dict(entry)
    kept["license_checked_at"] = _now()
    remember(supplier, url, kept, note=note)


def stored_pitch(supplier: str, url: str, expect: int) -> dict | None:
    """A pitch already checked for this note. None when this take must measure."""
    entry = peek(supplier, url, expect)
    if not entry or entry.get("verdict") != "ok":
        return None
    midi = entry.get("pitch_midi")
    if midi is None or int(midi) != int(expect):
        return None
    _count("hits")
    return {"accepted": True, "cents": entry.get("cents"), "checked": "registry", "midi": int(midi)}


def _catalog_bass() -> dict[str, int]:
    from . import chords as CH
    by_name: dict[str, int] = {}
    for rows in (CH.CAT.get("chords") or {}).values():
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 2 or not row[1]:
                continue
            name = str(row[0]).rsplit("/", 1)[-1]
            by_name[name] = int(min(row[1])) % 12
    return by_name


def _root_pc(symbol: str) -> int | None:
    match = re.match(r"([A-G])(#|b)?", symbol or "")
    if not match:
        return None
    pc = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[match.group(1)]
    if match.group(2) == "#":
        pc += 1
    elif match.group(2) == "b":
        pc -= 1
    return pc % 12


def seed_musicmatter(offerings: list, checked: str | None = None) -> dict:
    """One row per catalog offering. Does not download the audio."""
    from . import discover
    checked = checked or _now()
    author = "Peter Semmelhack (musicmatter.art)"
    try:
        author = discover.CH.CAT.get("creator") or author
    except Exception:
        pass
    bass_names = _catalog_bass()
    sounds = {}
    for off in offerings:
        if not isinstance(off, dict) or not off.get("manifest"):
            continue
        url = canonical_url(str(off["manifest"]))
        symbol = discover._offering_symbol(off)
        audio = off.get("audio") or ""
        stem = re.sub(r"\.(flac|wav|aiff?|mp3|ogg|nc\.json)$", "", str(audio).rsplit("/", 1)[-1], flags=re.I)
        bass = bass_names.get(stem)
        if bass is None and symbol and re.search(r"(^|[_\-])root([_\-.]|$)", stem, re.I):
            bass = _root_pc(symbol)
        inst = off.get("instrument_name") or discover._offering_instrument(off)
        title = f"{inst} {symbol}".strip() if symbol else stem or url.rsplit("/", 1)[-1]
        row = {
            "license": "CC-BY-SA-4.0",
            "title": title,
            "author": author,
            "page": url,
            "audio_url": audio or None,
            "checked_at": checked,
            "license_checked_at": checked,
            "checker_version": CHECKER_VERSION,
        }
        if bass is not None:
            row["bass_pc"] = bass
        if symbol:
            row["verdict"] = "ok"
            row["reason"] = ""
            row["chord"] = symbol
        else:
            row["verdict"] = "rejected"
            row["reason"] = "wrong_chord"
        sounds[row_key("musicmatter", url)] = {k: v for k, v in row.items() if v not in (None,)}
    return sounds


def seed_note_index(doc: dict, checked: str | None = None) -> tuple[dict, dict]:
    """Rows from the pitch-checked note index, plus libraries that were refused."""
    checked = checked or (doc.get("built") if isinstance(doc, dict) else None) or _now()
    from . import licenses as LIC
    sounds = {}
    aliases = {}
    for lib in (doc.get("libraries") or []):
        if not isinstance(lib, dict):
            continue
        supplier = str(lib.get("origin") or lib.get("id") or "note")
        for midi, note in (lib.get("notes") or {}).items():
            if not isinstance(note, dict):
                continue
            url = note.get("page") or note.get("audio_url") or note.get("url") or ""
            if not url:
                continue
            pitched = note.get("pitch_checked") and note.get("midi") is not None
            expect = note.get("expect_midi")
            midi_n = note.get("midi")
            wrong = expect is not None and midi_n is not None and int(expect) != int(midi_n)
            license_id = str(note.get("license") or lib.get("license") or "")
            allowed = bool(license_id) and LIC.allowed(LIC.from_id(license_id)[0])
            reason = ""
            verdict = "ok"
            if not allowed:
                verdict = "rejected"
                reason = "bad_license"
            elif not str(note.get("title") or "").strip() or not str(note.get("author") or lib.get("author") or "").strip():
                verdict = "rejected"
                reason = "missing_credit"
            elif wrong or not pitched:
                verdict = "rejected"
                reason = "wrong_pitch"
            row = {
                "verdict": verdict,
                "reason": reason,
                "license": LIC.from_id(license_id)[0] if license_id else "",
                "title": note.get("title") or "",
                "author": note.get("author") or lib.get("author") or "",
                "page": note.get("page") or lib.get("page") or url,
                "audio_url": note.get("audio_url") or note.get("url"),
                "pitch_midi": midi_n,
                "cents": note.get("cents"),
                "note": str(midi),
                "duration_s": note.get("length_s"),
                "checked_at": note.get("credit_checked") or checked,
                "license_checked_at": note.get("credit_checked") or checked,
                "checker_version": CHECKER_VERSION,
            }
            # One file can hold many notes. The note is part of the key.
            audio_key = str(note.get("audio_url") or note.get("url") or url)
            stored = {k: v for k, v in row.items() if v not in (None, "")}
            sound_key = row_key(supplier, audio_key, midi)
            sounds[sound_key] = stored
            # Freesound credit lookups use the sound page, not the CDN file.
            page = str(note.get("page") or "")
            if supplier == "freesound" and page:
                aliases[row_key(supplier, page)] = sound_key
    for skipped in (doc.get("skipped") or []):
        if not isinstance(skipped, dict):
            continue
        text = str(skipped.get("reason") or "")
        if "custom licence" not in text.lower() and "custom license" not in text.lower():
            continue
        page = skipped.get("page") or ""
        if not page:
            continue
        sounds[row_key("philharmonia", str(page))] = {
            "verdict": "rejected",
            "reason": "bad_license",
            "license": "",
            "title": skipped.get("library") or "Philharmonia",
            "author": "",
            "page": page,
            "checked_at": checked,
            "license_checked_at": checked,
            "checker_version": CHECKER_VERSION,
        }
    return sounds, aliases


def summarize(sounds: dict) -> dict:
    by_supplier: dict[str, dict] = {}
    by_reason: dict[str, int] = {}
    ok = rejected = 0
    for key, entry in sounds.items():
        supplier = str(key).split("\n", 1)[0]
        bucket = by_supplier.setdefault(supplier, {"ok": 0, "rejected": 0})
        if entry.get("verdict") == "rejected":
            rejected += 1
            bucket["rejected"] += 1
            reason = entry.get("reason") or "rejected"
            by_reason[reason] = by_reason.get(reason, 0) + 1
        else:
            ok += 1
            bucket["ok"] += 1
    return {"size": ok + rejected, "ok": ok, "rejected": rejected, "by_supplier": by_supplier, "by_reason": by_reason}


def backfill(offerings: list, notes: dict | None, out_path: str) -> dict:
    checked = _now()
    note_sounds, aliases = seed_note_index(notes or {}, checked)
    sounds = {}
    sounds.update(note_sounds)
    sounds.update(seed_musicmatter(offerings, checked))
    doc = _empty()
    doc["built"] = checked
    doc["sounds"] = sounds
    doc["aliases"] = aliases
    doc["pending"] = []
    _stamp_stats(doc)
    folder = os.path.dirname(out_path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = out_path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out_path)
    report = summarize(sounds)
    report["bytes"] = os.path.getsize(out_path)
    report["path"] = out_path
    report["checker_version"] = CHECKER_VERSION
    report["recheck_days"] = recheck_days()
    print(json.dumps({"event": "registry_backfill", **report}), flush=True)
    return report


def _offerings_from(path: str | None) -> list:
    if path:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if isinstance(doc, list):
            return doc
        if isinstance(doc, dict):
            return list(doc.get("offerings") or [])
        return []
    from .discover import _read_index_disk
    disk = _read_index_disk("https://netcomposer.art/suppliers.json")
    if disk:
        return disk
    from .discover import _mm_index
    return _mm_index(None, None)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Backfill the checked-sounds registry. Does not run at Perform time.")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--out", default=BUNDLED)
    parser.add_argument("--index", default="", help="A musicmatter offerings JSON. Omit to read the disk cache or the network.")
    parser.add_argument("--notes", default=os.path.join(os.path.dirname(__file__), "data", "note-index.json"))
    args = parser.parse_args(argv)
    notes = None
    if args.notes and os.path.isfile(args.notes):
        with open(args.notes, encoding="utf-8") as fh:
            notes = json.load(fh)
    offerings = _offerings_from(args.index or None)
    backfill(offerings, notes, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
