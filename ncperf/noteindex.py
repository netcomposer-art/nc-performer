"""A pitch map built once, then read at Perform time.

Each row is one note file: where it lives, which instrument it is, the pitch
it measured as, where the note starts and how long it lasts, and the license
and credit. The audio itself stays at the supplier. Perform and Gather read
this file and do not measure the libraries again. A refresh job rebuilds it.

The file is small enough to ship with the kit. A newer copy can be posted to
the container (from R2 key note-index/v1.json) or pointed at with
NC_NOTE_INDEX or NC_NOTE_INDEX_URL.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.request

BUNDLED = os.path.join(os.path.dirname(__file__), "data", "note-index.json")
CONTAINER = "/data/note-index.json"
INDEX_VERSION = 1
# The full chord set's recorded voicings use these MIDI notes.
PITCH_LO = 43
PITCH_HI = 82

_cache: tuple | None = None


def reset() -> None:
    """Forget the loaded file. The next load reads disk again."""
    global _cache
    _cache = None


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("libraries"), list):
        return None
    return doc


def _fetch_url(url: str) -> dict | None:
    """Download a remote index once into the cache directory."""
    from . import fetch
    dest = os.path.join(fetch.cache_dir(), "note-index-remote.json")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nc-performer-kit/0.1 (+https://netcomposer.art/agents/)"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read(8 * 1024 * 1024 + 1)
    except Exception:
        return _read_json(dest)
    if len(raw) > 8 * 1024 * 1024:
        return _read_json(dest)
    tmp = dest + ".part"
    try:
        with open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, dest)
    except OSError:
        return None
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return _read_json(dest)


def resolve_path() -> str | None:
    """The file Perform should read, or None when only a URL was configured."""
    override = os.environ.get("NC_NOTE_INDEX") or ""
    if override and os.path.isfile(override):
        return override
    if os.path.isfile(CONTAINER):
        return CONTAINER
    if os.path.isfile(BUNDLED):
        return BUNDLED
    return None


def load() -> dict | None:
    """The note index, or None when no readable index is installed."""
    global _cache
    override = os.environ.get("NC_NOTE_INDEX") or ""
    url = os.environ.get("NC_NOTE_INDEX_URL") or ""
    path = resolve_path()
    stamp = None
    if path and os.path.isfile(path):
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            stamp = None
    key = (override, url, path, stamp)
    if _cache is not None and _cache[0] == key:
        return _cache[1]
    doc = None
    if override and os.path.isfile(override):
        doc = _read_json(override)
    elif url:
        doc = _fetch_url(url)
    if doc is None and os.path.isfile(CONTAINER):
        doc = _read_json(CONTAINER)
    if doc is None and os.path.isfile(BUNDLED):
        doc = _read_json(BUNDLED)
    _cache = (key, doc)
    return doc


def index_to_maps(doc: dict) -> tuple[list[dict], dict]:
    """The same map shape notemap.ensure returns, with pitch already checked."""
    maps = []
    for lib in doc.get("libraries") or []:
        if not isinstance(lib, dict):
            continue
        notes = {}
        for midi, note in (lib.get("notes") or {}).items():
            if not isinstance(note, dict) or note.get("cents") is None:
                continue
            row = dict(note)
            row["pitch_checked"] = True
            row["midi"] = int(note.get("midi") or midi)
            row["expect_midi"] = int(note.get("expect_midi") or row["midi"])
            notes[str(int(midi))] = row
        maps.append({
            "v": 1,
            "id": lib.get("id") or "",
            "origin": lib.get("origin") or "",
            "instrument": lib.get("instrument") or "",
            "pluck": bool(lib.get("pluck")),
            "page": lib.get("page") or "",
            "author": lib.get("author") or "",
            "license": lib.get("license") or "",
            "note": lib.get("note") or "",
            "notes": notes,
        })
    status = {
        "v": 1,
        "skipped": list(doc.get("skipped") or []),
        "philharmonia_compatible": False,
        "source": "note-index",
    }
    return maps, status


def write_file(path: str, raw: bytes) -> str:
    """Replace the index at path. Used when the container receives a newer copy."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(raw)
    os.replace(tmp, path)
    reset()
    return path


def install_bytes(raw: bytes) -> str:
    """Write a posted index where load() will see it before the bundled file."""
    dest = os.environ.get("NC_NOTE_INDEX") or CONTAINER
    if dest == BUNDLED:
        dest = CONTAINER
    return write_file(dest, raw)


def _catalog_midis() -> set[int]:
    from . import chords as CH
    found: set[int] = set()
    for rows in (CH.CAT.get("chords") or {}).values():
        for row in rows or []:
            notes = row[1] if isinstance(row, (list, tuple)) and len(row) > 1 else []
            for midi in notes:
                found.add(int(midi))
    return {m for m in found if PITCH_LO <= m <= PITCH_HI}


def _merge_seed_maps(seeds: list[str]) -> None:
    """Keep the fullest measured map for each library in the working cache."""
    from . import fetch
    folder = os.path.join(fetch.cache_dir(), "note-maps")
    os.makedirs(folder, exist_ok=True)
    best: dict[str, dict] = {}

    def consider(path: str) -> None:
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(doc, dict) or not doc.get("id") or not isinstance(doc.get("notes"), dict):
            return
        prev = best.get(doc["id"])
        if prev is None or len(doc["notes"]) > len(prev.get("notes") or {}):
            best[doc["id"]] = doc

    roots = list(seeds) + [fetch.cache_dir()]
    for root in roots:
        folder_in = os.path.join(root, "note-maps")
        if not os.path.isdir(folder_in):
            continue
        for name in os.listdir(folder_in):
            if name.endswith(".json") and name != "libraries.json":
                consider(os.path.join(folder_in, name))
        status = os.path.join(folder_in, "libraries.json")
        dest_status = os.path.join(folder, "libraries.json")
        if os.path.isfile(status) and not os.path.isfile(dest_status):
            shutil.copyfile(status, dest_status)
    for doc in best.values():
        dest = os.path.join(folder, doc["id"] + ".json")
        current = None
        if os.path.isfile(dest):
            try:
                with open(dest, encoding="utf-8") as fh:
                    current = json.load(fh)
            except (OSError, json.JSONDecodeError):
                current = None
        if isinstance(current, dict) and len(current.get("notes") or {}) > len(doc.get("notes") or {}):
            continue
        tmp = dest + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, dest)


def _stage_file(url: str, ext: str, seeds: list[str]) -> str | None:
    from . import fetch
    dest = fetch.cache_path(url, None, ext)
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return dest
    name = os.path.basename(dest)
    for seed in seeds:
        src = os.path.join(seed, name)
        if os.path.isfile(src) and os.path.getsize(src) > 0:
            try:
                os.link(src, dest)
            except OSError:
                shutil.copyfile(src, dest)
            return dest
    return None


def _audio_path(note: dict, seeds: list[str]) -> str | None:
    """Local audio for one indexed note. Downloads only when the cache has no copy."""
    from . import fetch
    kind = note.get("kind")
    if kind == "freesound" and not note.get("audio_url") and note.get("id"):
        resolved = fetch.resolve({"kind": "freesound", "id": str(note["id"])})
        note["audio_url"] = resolved.get("audio_url")
        if resolved.get("author"):
            note["author"] = resolved["author"]
        if resolved.get("title"):
            note["title"] = resolved["title"]
        lic = resolved.get("license") or {}
        if isinstance(lic, dict) and lic.get("id"):
            note["license"] = lic["id"]
        return resolved.get("path")
    url = note.get("audio_url") or note.get("url")
    if not url:
        return None
    ext = os.path.splitext(str(url).split("?")[0])[1] or ".bin"
    found = _stage_file(str(url), ext, seeds)
    if found:
        if kind == "uiowa_mis" and not note.get("audio_url"):
            note["audio_url"] = url
        return found
    if kind == "uiowa_mis":
        resolved = fetch.resolve({"kind": "uiowa_mis", "url": url, "page": note.get("page")})
        note["audio_url"] = resolved.get("audio_url") or url
        return resolved.get("path")
    return fetch.download(str(url), ext=ext)


def _annotate(mp: dict, seeds: list[str], checked: str) -> None:
    """Record onset and length from audio already measured. Does not measure pitch."""
    from . import pitch
    from .util import SR, decode, onset
    groups: dict[str, list] = {}
    for midi, note in (mp.get("notes") or {}).items():
        if not isinstance(note, dict) or note.get("cents") is None:
            continue
        key = str(note.get("audio_url") or note.get("url") or note.get("id") or midi)
        groups.setdefault(key, []).append(note)
    for rows in groups.values():
        try:
            path = _audio_path(rows[0], seeds)
        except Exception as exc:
            print(json.dumps({"event": "note_index_audio", "error": str(exc)[:160]}), flush=True)
            continue
        if not path or not os.path.isfile(path):
            continue
        try:
            y = decode(path)
        except Exception as exc:
            print(json.dumps({"event": "note_index_decode", "error": str(exc)[:160]}), flush=True)
            continue
        segs = None
        for note in rows:
            note["pitch_checked"] = True
            note["credit_checked"] = checked
            seg = note.get("segment") if isinstance(note.get("segment"), dict) else None
            if seg is not None and seg.get("index") is not None:
                if segs is None:
                    mono = y.mean(1) if getattr(y, "ndim", 1) == 2 else y
                    segs = pitch.segment_run(mono, SR, float(seg.get("drop_db") or 40.0))
                index = int(seg["index"])
                if 0 <= index < len(segs):
                    t0, t1 = segs[index]
                    kept = dict(seg)
                    kept["t0"] = round(float(t0), 4)
                    kept["t1"] = round(float(t1), 4)
                    note["segment"] = kept
                    note["onset_s"] = round(float(t0), 4)
                    note["length_s"] = round(float(t1 - t0), 4)
                continue
            attack = onset(y) / SR
            note["onset_s"] = round(float(attack), 4)
            note["length_s"] = round(len(y) / SR, 4)
        for note in rows:
            if note.get("url") and not note.get("audio_url"):
                note["audio_url"] = note["url"]


def _slim_note(note: dict) -> dict:
    keep = (
        "kind", "url", "id", "audio_url", "cents", "onset_s", "length_s", "segment",
        "title", "author", "license", "page", "expect_midi", "midi", "pitched", "credit_checked",
    )
    out = {}
    for field in keep:
        if note.get(field) not in (None, ""):
            out[field] = note[field]
    out["pitch_checked"] = True
    return out


def _write_doc(out_path: str, doc: dict) -> None:
    folder = os.path.dirname(out_path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = out_path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out_path)
    reset()


def build_index(out_path: str, seeds: list[str] | None = None, lo: int = PITCH_LO, hi: int = PITCH_HI, only: list[str] | None = None) -> dict:
    """Fill missing catalog pitches, then write the index. This is the offline job."""
    from . import notemap
    seeds = [s for s in (seeds or []) if s]
    _merge_seed_maps(seeds)
    needed = {m for m in _catalog_midis() if lo <= m <= hi}
    checked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    wanted = {name for name in (only or []) if name}
    libraries = []
    for spec in notemap._specs():
        if wanted and spec["id"] not in wanted:
            continue
        mp = notemap._load(spec["id"], notemap._blank(spec))
        before = len(mp.get("notes") or {})
        try:
            if spec["kind"] == "uiowa_mis":
                notemap._fill_uiowa(spec, mp, needed)
            else:
                notemap._fill_freesound(spec, mp, needed)
        except Exception as exc:
            mp.setdefault("errors", []).append(str(exc)[:180])
        _annotate(mp, seeds, checked)
        notemap._save(mp)
        notes = {}
        for midi, note in sorted((mp.get("notes") or {}).items(), key=lambda item: int(item[0])):
            if not isinstance(note, dict) or note.get("cents") is None:
                continue
            if not (note.get("url") or note.get("id")):
                continue
            notes[str(int(midi))] = _slim_note(note)
        print(json.dumps({
            "event": "note_index_library",
            "id": spec["id"],
            "before": before,
            "notes": len(notes),
            "missing": sorted(m for m in needed if spec.get("lo", lo) <= m <= spec.get("hi", hi) and str(m) not in notes),
        }), flush=True)
        libraries.append({
            "id": spec["id"],
            "instrument": spec["instrument"],
            "origin": spec["origin"],
            "kind": spec["kind"],
            "pluck": bool(spec.get("pluck")),
            "page": spec.get("page") or "",
            "author": spec.get("author") or "",
            "license": spec.get("license") or "",
            "note": spec.get("note") or "",
            "notes": notes,
        })
        status = notemap.library_status(refresh=False)
        doc = {
            "v": INDEX_VERSION,
            "built": checked,
            "pitches": [lo, hi],
            "libraries": libraries,
            "skipped": status.get("skipped") or [],
        }
        _write_doc(out_path, doc)
    print(json.dumps({
        "event": "note_index",
        "path": out_path,
        "bytes": os.path.getsize(out_path),
        "libraries": len(libraries),
        "notes": sum(len(lib["notes"]) for lib in libraries),
    }), flush=True)
    return doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the note index. Does not run at Perform time.")
    parser.add_argument("--out", default=BUNDLED, help="Where to write note-index.json")
    parser.add_argument("--seed", action="append", default=[], help="A cache directory that already has note-maps or audio")
    parser.add_argument("--lo", type=int, default=PITCH_LO)
    parser.add_argument("--hi", type=int, default=PITCH_HI)
    parser.add_argument("--library", action="append", default=[], help="Build only these library ids")
    args = parser.parse_args(argv)
    build_index(args.out, seeds=args.seed, lo=args.lo, hi=args.hi, only=args.library)
    return 0


if __name__ == "__main__":
    sys.exit(main())
