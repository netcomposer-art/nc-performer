"""Pitch-verified single notes, cached per instrument.

A chord can be built by stacking these at the MIDI pitch they were recorded at.
Nothing here pitch-shifts or time-stretches. A chromatic run is cut on silence,
with the same segmentation the renderer uses, and a slice is kept only when it
measures as that note.
"""
from __future__ import annotations

import html as html_lib
import json
import os
import re
import urllib.parse
import urllib.request

from . import chords as CH
from . import fetch
from . import licenses as LIC
from . import pitch
from .util import SR, decode

UA = "nc-performer-kit/0.1 (+https://netcomposer.art/agents/)"
MAP_VERSION = 1
UIOWA_AUTHOR = "University of Iowa Electronic Music Studios (Lawrence Fritts, director)"
UIOWA_LICENSE = "PD-equivalent"
PHILHARMONIA_PAGE = "https://philharmonia.co.uk/resources/sound-samples/"
# The live page (checked 2026-10) is a custom licence, not CC0 / CC BY / CC BY-SA / PD.
PHILHARMONIA_MARK = "must not be sold or made available"
# VSCO 2 CE is CC0, but the published note grid is a minor-third / fifth subset.
# These are the solo-violin pizzicato pitches in sgossner/VSCO-2-CE (filename notes).
VSCO_PIZZ_MIDIS = {55, 57, 60, 64, 67, 69, 72, 76, 79, 81, 88, 93, 96}

_NOTE = re.compile(r"([A-G](?:#|b)?)(-?\d)")
_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def _midi(name: str, octave: str) -> int:
    pc = _PC[name[0]]
    if "#" in name:
        pc += 1
    elif "b" in name:
        pc -= 1
    return (int(octave) + 1) * 12 + pc


def note_ok(meas: dict, expect: int, tol: float = 15.0) -> bool:
    """True when the measurement is the expected MIDI note, within tolerance."""
    return bool(pitch.accept(meas, expect, tol))


def philharmonia_compatible(text: str) -> bool:
    """Philharmonia's published terms are a custom restriction, not a CC/PD licence."""
    low = html_lib.unescape(text or "").lower().replace("\u2018", "'").replace("\u2019", "'")
    if PHILHARMONIA_MARK in low or ("as is" in low and "sampler" in low):
        return False
    found = LIC.page_licenses(text or "")
    return bool(found) and all(LIC.allowed(item) for item in found)


def voicing_covered(symbol: str, have: set[int]) -> bool:
    """True when some catalog voicing of this chord is a subset of have."""
    return any(set(notes) <= have for _name, notes in CH.voicings(symbol))


def _cache_path(instrument_id: str) -> str:
    folder = os.path.join(fetch.cache_dir(), "note-maps")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, instrument_id + ".json")


def _status_path() -> str:
    folder = os.path.join(fetch.cache_dir(), "note-maps")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "libraries.json")


def _load(instrument_id: str, blank: dict) -> dict:
    path = _cache_path(instrument_id)
    if not os.path.exists(path):
        return blank
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return blank
    if doc.get("v") != MAP_VERSION or doc.get("id") != instrument_id:
        return blank
    blank["notes"] = doc.get("notes") or {}
    if doc.get("note"):
        blank["note"] = doc["note"]
    if doc.get("errors"):
        blank["errors"] = doc["errors"]
    if doc.get("rejected"):
        blank["rejected"] = doc["rejected"]
    if doc.get("misses"):
        blank["misses"] = doc["misses"]
    return blank


def _save(mp: dict) -> None:
    path = _cache_path(mp["id"])
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(mp, fh, indent=1)
    os.replace(tmp, path)


def _get(url: str, timeout: int = 40) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _blocked(exc: BaseException) -> bool:
    msg = str(exc)
    return "Egress blocked" in msg or "not on NC_ALLOWED" in msg or "not on the allowlist" in msg


def _span(url: str) -> tuple[int, int] | None:
    base = urllib.parse.unquote(url.rsplit("/", 1)[-1])
    base = re.sub(r"\.(aiff|aif|wav)$", "", base, flags=re.I)
    found = [_midi(name, octave) for name, octave in _NOTE.findall(base)]
    if not found:
        return None
    return min(found), max(found)


def _uiowa_files(page: str) -> list[str]:
    html = _get(page).decode("utf-8", "replace")
    out = []
    for href in re.findall(r'href="([^"]+\.(?:aiff|aif|wav))"', html, flags=re.I):
        href = html_lib.unescape(href)
        if href.startswith("http"):
            url = href
        else:
            url = urllib.parse.urljoin(
                "https://theremin.music.uiowa.edu/",
                urllib.parse.quote(urllib.parse.unquote(href), safe="/:"),
            )
        out.append(url)
    return out


def _store(mp: dict, midi: int, source: dict, cents) -> None:
    prev = mp["notes"].get(str(midi))
    if prev is not None and abs(prev.get("cents") or 99) <= abs(cents or 99):
        return
    mp["notes"][str(midi)] = dict(source, cents=cents, midi=midi)


def _source_from_resolve(spec: dict, resolved: dict, url: str | None, freesound_id: str | None, midi: int, segment: dict | None) -> dict:
    lic = resolved.get("license") or {}
    lid = lic.get("id") if isinstance(lic, dict) else str(lic or "")
    src = {
        "kind": spec["kind"],
        "title": resolved.get("title") or spec["instrument"],
        "author": resolved.get("author") or spec.get("author") or "",
        "license": lid,
        "page": spec.get("page") or "",
        "pitched": True,
        "expect_midi": midi,
    }
    if spec["kind"] == "uiowa_mis":
        src["url"] = url
        src["page"] = spec["page"]
        src["author"] = UIOWA_AUTHOR
        src["license"] = UIOWA_LICENSE
        src["title"] = f"{spec['instrument']} {pitch.NOTE(midi)}"
    else:
        src["id"] = str(freesound_id)
        src["page"] = f"https://freesound.org/s/{freesound_id}/"
        src["title"] = resolved.get("title") or src["title"]
        src["author"] = resolved.get("author") or ""
    if segment is not None:
        src["segment"] = segment
    return src


def _measure_slice(y, expect: int) -> dict | None:
    meas = pitch.measure(y, expect)
    if note_ok(meas, expect):
        return meas
    return None


def _measure_uiowa_file(spec: dict, mp: dict, url: str, needed: set[int]) -> None:
    span = _span(url)
    if not span:
        return
    print(json.dumps({"event": "note_file", "id": spec["id"], "url": url.rsplit("/", 1)[-1]}), flush=True)
    resolved = fetch.resolve({"kind": "uiowa_mis", "url": url, "page": spec["page"]})
    y = decode(resolved["path"])
    if span[0] == span[1]:
        meas = _measure_slice(y, span[0])
        if meas and span[0] in needed:
            _store(mp, span[0], _source_from_resolve(spec, resolved, url, None, span[0], None), meas.get("cents"))
        return
    mono = y.mean(1) if getattr(y, "ndim", 1) == 2 else y
    segs = pitch.segment_run(mono, SR)
    expected = span[1] - span[0] + 1
    targets = [m for m in sorted(needed) if span[0] <= m <= span[1] and str(m) not in mp["notes"]]
    if len(segs) == expected:
        pairs = [(m - span[0], m) for m in targets if 0 <= m - span[0] < len(segs)]
    else:
        pairs = [(i, span[0] + i) for i in range(len(segs)) if span[0] <= span[0] + i <= span[1]]
    for index, midi in pairs:
        if str(midi) in mp["notes"] and midi not in needed:
            continue
        t0, t1 = segs[index]
        sl = y[int(t0 * SR):int(t1 * SR)]
        meas = _measure_slice(sl, midi)
        if not meas:
            continue
        seg = {"index": int(index), "drop_db": 40.0}
        _store(mp, midi, _source_from_resolve(spec, resolved, url, None, midi, seg), meas.get("cents"))


def _open_midis(mp: dict, needed: set[int], lo: int, hi: int) -> list[int]:
    misses = {int(m) for m in mp.get("misses") or []}
    return [
        m for m in sorted(needed)
        if lo <= m <= hi and str(m) not in (mp.get("notes") or {}) and m not in misses
    ]


def _fill_uiowa(spec: dict, mp: dict, needed: set[int]) -> None:
    missing = _open_midis(mp, needed, spec["lo"], spec["hi"])
    if not missing:
        return
    try:
        files = _uiowa_files(spec["page"])
    except Exception as exc:
        mp.setdefault("errors", []).append(str(exc)[:180])
        return
    files = [u for u in files if re.search(spec["file_re"], urllib.parse.unquote(u)) and ".mono." not in u.lower()]
    files.sort(key=lambda u: (0 if ".mf." in u.lower() else 1, u))
    used = set()
    for midi in missing:
        if str(midi) in mp["notes"]:
            continue
        for url in files:
            if url in used:
                continue
            span = _span(url)
            if not span or not (span[0] <= midi <= span[1]):
                continue
            used.add(url)
            try:
                _measure_uiowa_file(spec, mp, url, needed)
            except Exception as exc:
                mp.setdefault("errors", []).append(str(exc)[:180])
                if _blocked(exc):
                    return
            if str(midi) in mp["notes"]:
                break
    still = [m for m in missing if str(m) not in mp["notes"]]
    mp["misses"] = sorted(set(int(m) for m in mp.get("misses") or []) | set(still))


def _fill_freesound(spec: dict, mp: dict, needed: set[int]) -> None:
    misses = {int(m) for m in mp.get("misses") or []}
    for cand in spec.get("candidates") or []:
        midi = int(cand["midi"])
        if midi not in needed or str(midi) in mp["notes"] or midi in misses:
            continue
        sid = str(cand["id"])
        print(json.dumps({"event": "note_file", "id": spec["id"], "freesound": sid, "midi": midi}), flush=True)
        try:
            _get(f"https://freesound.org/s/{sid}/")
            resolved = fetch.resolve({"kind": "freesound", "id": sid})
        except Exception as exc:
            mp.setdefault("errors", []).append(f"{sid}: {str(exc)[:140]}")
            if _blocked(exc):
                return
            continue
        lid = (resolved.get("license") or {}).get("id")
        author = resolved.get("author") or ""
        if not lid or not LIC.allowed(lid) or author.strip().lower() in ("", "unknown", "?", "none"):
            continue
        try:
            y = decode(resolved["path"])
            meas = _measure_slice(y, midi)
        except Exception as exc:
            mp.setdefault("errors", []).append(f"{sid}: {str(exc)[:140]}")
            continue
        if not meas:
            mp.setdefault("rejected", []).append({"id": sid, "midi": midi, "reason": "pitch"})
            misses.add(midi)
            continue
        _store(mp, midi, _source_from_resolve(spec, resolved, None, sid, midi, None), meas.get("cents"))
    mp["misses"] = sorted(misses)


def _blank(spec: dict) -> dict:
    return {
        "v": MAP_VERSION,
        "id": spec["id"],
        "origin": spec["origin"],
        "instrument": spec["instrument"],
        "pluck": bool(spec.get("pluck")),
        "page": spec.get("page") or "",
        "author": spec.get("author") or "",
        "license": spec.get("license") or "",
        "note": spec.get("note") or "",
        "notes": {},
    }


def _specs() -> list[dict]:
    """Libraries that publish single notes or chromatic runs at native pitch."""
    uiowa = "https://theremin.music.uiowa.edu/"
    return [
        {
            "id": "uiowa-piano", "origin": "uiowa", "instrument": "piano", "pluck": False,
            "kind": "uiowa_mis", "lo": 24, "hi": 96, "page": uiowa + "MISpiano.html",
            "file_re": r"Piano\.mf\.", "author": UIOWA_AUTHOR, "license": UIOWA_LICENSE,
        },
        {
            "id": "uiowa-cello-pizz", "origin": "uiowa", "instrument": "cello pizzicato", "pluck": True,
            "kind": "uiowa_mis", "lo": 36, "hi": 84, "page": uiowa + "MIScello.html",
            "file_re": r"Cello\.pizz\.mf\.", "author": UIOWA_AUTHOR, "license": UIOWA_LICENSE,
        },
        {
            "id": "uiowa-violin-pizz", "origin": "uiowa", "instrument": "violin pizzicato", "pluck": True,
            "kind": "uiowa_mis", "lo": 55, "hi": 96, "page": uiowa + "MISviolin.html",
            "file_re": r"Violin\.pizz\.mf\.", "author": UIOWA_AUTHOR, "license": UIOWA_LICENSE,
        },
        {
            "id": "uiowa-guitar", "origin": "uiowa", "instrument": "guitar", "pluck": True,
            "kind": "uiowa_mis", "lo": 40, "hi": 88, "page": uiowa + "MISguitar.html",
            "file_re": r"Guitar\.mf\..*stereo", "author": UIOWA_AUTHOR, "license": UIOWA_LICENSE,
        },
        {
            "id": "uiowa-marimba", "origin": "uiowa", "instrument": "marimba", "pluck": False,
            "kind": "uiowa_mis", "lo": 36, "hi": 96, "page": uiowa + "MISmarimba.html",
            "file_re": r"Marimba\.yarn\.mf\.", "author": UIOWA_AUTHOR, "license": UIOWA_LICENSE,
        },
        {
            "id": "uiowa-flute", "origin": "uiowa", "instrument": "flute", "pluck": False,
            "kind": "uiowa_mis", "lo": 59, "hi": 96, "page": uiowa + "MISflute.html",
            "file_re": r"Flute\.nonvib\.mf\.", "author": UIOWA_AUTHOR, "license": UIOWA_LICENSE,
        },
        {
            "id": "uiowa-vibraphone", "origin": "uiowa", "instrument": "vibraphone", "pluck": False,
            "kind": "uiowa_mis", "lo": 48, "hi": 89, "page": uiowa + "MISvibraphone.html",
            "file_re": r"Vibraphone\.shortsustain\.mf\.", "author": UIOWA_AUTHOR, "license": UIOWA_LICENSE,
        },
        {
            "id": "freesound-piano", "origin": "freesound", "instrument": "piano", "pluck": False,
            "kind": "freesound", "page": "https://freesound.org/people/feelander/packs/17718/",
            "author": "feelander", "license": "CC0-1.0",
            "note": "Freesound user feelander, CC0. The titles are University of Iowa MIS piano notes re-uploaded; each file is pitch-checked and credited on its Freesound page.",
            "candidates": [
                {"midi": 46, "id": "316038"},  # Bb2
                {"midi": 48, "id": "316080"},  # C3
                {"midi": 50, "id": "316072"},  # D3
                {"midi": 51, "id": "316047"},  # Eb3
                {"midi": 52, "id": "316067"},  # E3
                {"midi": 55, "id": "316017"},  # G3
                {"midi": 56, "id": "316012"},  # Ab3
                {"midi": 57, "id": "316005"},  # A3
                {"midi": 58, "id": "316043"},  # Bb3
                {"midi": 59, "id": "316063"},  # B3
                {"midi": 60, "id": "316083"},  # C4
                {"midi": 62, "id": "316029"},  # D4
                {"midi": 63, "id": "316050"},  # Eb4
                {"midi": 64, "id": "316053"},  # E4
                {"midi": 65, "id": "316025"},  # F4
                {"midi": 67, "id": "316055"},  # G4
            ],
        },
        {
            "id": "freesound-cello", "origin": "freesound", "instrument": "cello", "pluck": False,
            "kind": "freesound", "page": "https://freesound.org/people/MTG/sounds/",
            "author": "MTG", "license": "CC-BY-3.0",
            "note": "Good-sounds single notes by MTG on Freesound, CC BY 3.0. Kept only when the file measures as the named pitch.",
            "candidates": [
                {"midi": 48, "id": "248000"},  # C3
                {"midi": 50, "id": "247998"},  # D3
                {"midi": 51, "id": "247482"},  # D#3
                {"midi": 52, "id": "247996"},  # E3
                {"midi": 53, "id": "247995"},  # F3
                {"midi": 55, "id": "247993"},  # G3
                {"midi": 56, "id": "247487"},  # G#3
                {"midi": 57, "id": "247991"},  # A3
                {"midi": 58, "id": "247489"},  # A#3
                {"midi": 59, "id": "247989"},  # B3
                {"midi": 62, "id": "247986"},  # D4
                {"midi": 63, "id": "247494"},  # D#4
                {"midi": 64, "id": "247984"},  # E4
                {"midi": 65, "id": "247983"},  # F4
                {"midi": 67, "id": "247981"},  # G4
            ],
        },
        {
            "id": "freesound-flute", "origin": "freesound", "instrument": "flute", "pluck": False,
            "kind": "freesound", "page": "https://freesound.org/people/MTG/sounds/",
            "author": "MTG", "license": "CC-BY-3.0",
            "note": "Good-sounds flute notes by MTG on Freesound, CC BY 3.0, pitch-checked.",
            "candidates": [
                {"midi": 59, "id": "246943"},  # B3
                {"midi": 60, "id": "246981"},  # C4
                {"midi": 63, "id": "246646"},  # D#4
                {"midi": 64, "id": "246648"},  # E4
                {"midi": 67, "id": "247047"},  # G4
                {"midi": 70, "id": "246660"},  # A#4
            ],
        },
    ]


def library_status(refresh: bool = False) -> dict:
    """Record which candidate libraries were rejected or too sparse to use."""
    path = _status_path()
    if os.path.exists(path) and not refresh:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
    text = ""
    live = False
    try:
        text = _get(PHILHARMONIA_PAGE).decode("utf-8", "replace")
        live = True
    except Exception as exc:
        text = ""
        err = str(exc)[:160]
    else:
        err = ""
    compatible = philharmonia_compatible(text) if text else False
    skipped = [
        {
            "library": "Philharmonia Orchestra",
            "page": PHILHARMONIA_PAGE,
            "used": False,
            "reason": (
                "Rejected. The sound-samples page allows use in a finished work but forbids selling "
                "or redistributing the files as samples or as a sampler instrument. That is a custom "
                "licence, not CC0, CC BY, CC BY-SA, or public domain."
                + ("" if live else f" Live page was not re-read ({err}).")
            ),
        },
        {
            "library": "VSCO 2 Community Edition",
            "page": "https://github.com/sgossner/VSCO-2-CE",
            "used": False,
            "reason": (
                "The library is CC0 and the GitHub tree is fetchable, but its notes are a sparse grid "
                "(about a minor third or a fifth apart). No catalog voicing of these chords is covered, "
                "so the files were not used. The Freesound copies of the same grid disagree between title and filename."
            ),
        },
        {
            "library": "Wikimedia Commons",
            "page": "https://commons.wikimedia.org/",
            "used": False,
            "reason": (
                "Searches for instrument notes returned photographs, a G-major pizzicato scale, and one short "
                "pizzicato file. None of those is a pitch-checked single note for every tone of one of these chords."
            ),
        },
    ]
    doc = {"v": MAP_VERSION, "skipped": skipped, "philharmonia_compatible": compatible}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    return doc


def ensure(symbols: list[str], refresh_status: bool = False) -> tuple[list[dict], dict]:
    """Load or build a pitch-verified note map for each instrument. Network when a note is missing."""
    needed: set[int] = set()
    for sym in symbols:
        for _name, notes in CH.voicings(sym):
            needed.update(int(n) for n in notes)
    maps = []
    for spec in _specs():
        mp = _load(spec["id"], _blank(spec))
        before = set(mp["notes"])
        if spec["kind"] == "uiowa_mis":
            _fill_uiowa(spec, mp, needed)
        else:
            _fill_freesound(spec, mp, needed)
        if set(mp["notes"]) != before or not os.path.exists(_cache_path(spec["id"])):
            _save(mp)
        maps.append(mp)
        print(json.dumps({
            "event": "note_map",
            "id": spec["id"],
            "notes": len(mp["notes"]),
            "errors": len(mp.get("errors") or []),
            "rejected": len(mp.get("rejected") or []),
        }), flush=True)
    return maps, library_status(refresh=refresh_status)


def assemblies_for(symbols: list[str], maps: list[dict], exclude: set[str] | None = None) -> dict[str, list[dict]]:
    """For each symbol, instruments that can play some catalog voicing at native pitch."""
    skip = {w.lower() for w in (exclude or set())}
    out: dict[str, list[dict]] = {}
    for sym in symbols:
        rows = []
        voiced = CH.voicings(sym)
        for mp in maps:
            inst = (mp.get("instrument") or "").lower()
            if any(word == inst or word in inst.split() or (word == "piano" and inst == "piano") for word in skip):
                continue
            covered = []
            have = {int(k) for k in (mp.get("notes") or {})}
            for name, notes in voiced:
                notes = [int(n) for n in notes]
                if notes and set(notes) <= have:
                    rootish = 0 if ("root" in name and "rootless" not in name) else 1
                    covered.append((len(notes), max(notes) - min(notes), rootish, name, notes))
            if not covered:
                continue
            _length, _span, _root, name, notes = sorted(covered)[0]
            samples = []
            licenses = []
            authors = []
            for midi in notes:
                sample = dict(mp["notes"][str(midi)])
                samples.append(sample)
                if sample.get("license") and sample["license"] not in licenses:
                    licenses.append(sample["license"])
                if sample.get("author") and sample["author"] not in authors:
                    authors.append(sample["author"])
            rows.append({
                "built": "assembled notes",
                "origin": mp.get("origin") or "",
                "instrument": mp.get("instrument") or "",
                "pluck": bool(mp.get("pluck")),
                "tier": 0,
                "voicing": notes,
                "voicing_name": name,
                "samples": samples,
                "license": licenses[0] if licenses else "",
                "licenses": licenses,
                "author": "; ".join(authors),
                "page": mp.get("page") or (samples[0].get("page") if samples else ""),
                "title": f"{mp.get('instrument')} {sym} from {len(notes)} native-pitch notes",
                "library_note": mp.get("note") or "",
            })
        out[sym] = rows
    return out
