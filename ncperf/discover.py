"""Find sounds for a Performance without preferring one supplier.

Registered Suppliers (today, musicmatter.art) are one origin among the open
commons: Freesound, Openverse, the Internet Archive, Wikimedia Commons,
ccMixter, and the bundled UIowa MIS library. Each origin is queried with the
same words. A failure in one origin is skipped. Selection rotates by seed and
will not spend most of the plan on a single origin unless the notes name it.

Clips are trimmed and faded. Nothing here asks for pitch-shift or time-stretch.
"""
from __future__ import annotations

import json
import random
import re
import urllib.parse
import urllib.request
from collections import Counter

from . import chords as CH
from . import interpret
from . import licenses as LIC

UA = "nc-performer-kit/0.1 (+https://netcomposer.art/agents/)"
ORIGINS = (
    "musicmatter",
    "freesound",
    "openverse",
    "internet_archive",
    "wikimedia",
    "ccmixter",
    "uiowa",
)
# Rotated when the notes do not name a timbre, so piano is not the default.
PALETTE = ("choir", "synth", "strings", "organ", "flute", "guitar", "harp", "percussion", "texture", "pluck")
LEXICON = (
    "piano", "rhodes", "synth", "arp", "choir", "voice", "organ", "flute", "guitar", "harp",
    "cello", "violin", "strings", "drum", "percussion", "bell", "pad", "texture", "pluck", "brass",
)
# File hosts the container is allowed to fetch. A hit on any other host is skipped.
FILE_HOSTS = (
    "musicmatter.art",
    "audio.musicmatter.art",
    "fullchords.musicmatter.art",
    "freesound.org",
    "cdn.freesound.org",
    "archive.org",
    "us.archive.org",
    "wikimedia.org",
    "commons.wikimedia.org",
    "upload.wikimedia.org",
    "ccmixter.org",
    "theremin.music.uiowa.edu",
)
# Names a catalog uses for a timbre. Matching is equal across origins.
HINT_ALIASES = {
    "synth": ("syntharp", "fatsynth", "fat synth"),
    "choir": ("jacobschoir", "chorus"),
    "strings": ("violin", "cello", "viola"),
    "flute": ("altoflute",),
    "percussion": ("drum",),
    "pluck": ("guitar", "harp", "pizzicato"),
    "voice": ("choir", "chorus"),
}
_MM_CACHE: dict[str, dict] = {}
# Words the notes tell us to leave out ("keep the piano out"). Set for one plan_for call.
_EXCLUDE: set[str] = set()


def host_ok(host: str | None, extra=None) -> bool:
    if extra is not None:
        return bool(extra(host))
    h = (host or "").lower().rstrip(".")
    return any(h == a or h.endswith("." + a) for a in FILE_HOSTS)


def _hostname(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _json(url: str):
    return json.loads(_get(url).decode("utf-8", "replace"))


def excluded_terms(text: str) -> set[str]:
    """Timbre words the notes reject, so they are not searched and rank last."""
    low = text.lower()
    out = set()
    for word in dict.fromkeys((*LEXICON, *PALETTE)):
        if re.search(rf"\b(no|without|not|exclude)\s+{re.escape(word)}\b", low):
            out.add(word)
        elif re.search(rf"\bkeep the {re.escape(word)} out\b", low):
            out.add(word)
        elif re.search(rf"\b{re.escape(word)} out of\b", low):
            out.add(word)
    return out


def terms_for(comp: dict, instrument: str | None, seed: int) -> list[str]:
    words = f"{comp.get('composition') or ''} {comp.get('performance') or ''}".lower()
    skip = excluded_terms(words)
    found = [w for w in LEXICON if w not in skip and re.search(rf"\b{re.escape(w)}\b", words)]
    hint = (instrument or "").strip().lower()
    if hint and hint not in found and hint not in skip:
        found.insert(0, hint)
    if found:
        return found[:4]
    rng = random.Random(seed)
    palette = [w for w in PALETTE if w not in skip]
    return list(rng.sample(palette or PALETTE, min(3, len(palette or PALETTE))))


def _mentions(text: str, word: str | None) -> bool:
    """True when word, or a catalog alias of it, appears as its own token."""
    if not word:
        return False
    for token in (word,) + HINT_ALIASES.get(word, ()):
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text):
            return True
    return False


def _fit(blob: str, terms: list[str], hint: str | None, chords: list[str]) -> int:
    text = blob.lower()
    score = 0
    if _mentions(text, hint):
        score += 5
    for term in terms:
        if _mentions(text, term):
            score += 2
    for sym in chords:
        if sym.lower() in text:
            score += 3
    for word in _EXCLUDE:
        if _mentions(text, word):
            score -= 5
    return score


def _cand(origin: str, title: str, author: str, license_id: str, blob: str, source: dict, terms, hint, chords) -> dict | None:
    if not LIC.allowed(LIC.from_id(license_id)[0]):
        return None
    url = source.get("url") or source.get("manifest") or ""
    if url and not host_ok(_hostname(url), source.get("_host_ok")):
        return None
    page = source.get("page") or ""
    if page and not host_ok(_hostname(page), source.get("_host_ok")):
        return None
    source = {k: v for k, v in source.items() if k != "_host_ok"}
    return {
        "origin": origin,
        "title": title or origin,
        "author": author or "unknown",
        "license": LIC.from_id(license_id)[0],
        "instrument": _instrument_label(blob, hint),
        "fit": _fit(blob, terms, hint, chords),
        "source": source,
        "key": url or title,
    }


def _instrument_label(blob: str, hint: str | None) -> str:
    text = blob.lower()
    # A direct word (choir, synth) wins over a catalog alias such as full-chord-set.
    if hint and re.search(rf"(?<![a-z0-9]){re.escape(hint)}(?![a-z0-9])", text):
        return hint
    for word in LEXICON:
        if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text):
            return word
    if _mentions(text, hint):
        return hint
    for word in LEXICON:
        if _mentions(text, word):
            return word
    return "texture"


def _mm_index(registry_url: str | None, host_ok_fn=None) -> list[dict]:
    url = registry_url or "https://netcomposer.art/suppliers.json"
    if url in _MM_CACHE:
        return _MM_CACHE[url]
    offerings = []
    try:
        reg = _json(url)
        for sup in reg.get("suppliers") or []:
            if sup.get("status") != "active" or not sup.get("index"):
                continue
            if not host_ok(_hostname(sup["index"]), host_ok_fn):
                continue
            idx = _json(sup["index"])
            for off in idx.get("offerings") or []:
                off = dict(off)
                off["_supplier"] = sup.get("name") or sup.get("id") or "supplier"
                offerings.append(off)
    except Exception as exc:
        print(json.dumps({"event": "discover_origin", "origin": "musicmatter", "error": str(exc)[:200]}), flush=True)
        return []
    _MM_CACHE[url] = offerings
    return offerings


def search_musicmatter(terms, hint, chords, seed, host_ok_fn=None, registry_url=None) -> list[dict]:
    offerings = _mm_index(registry_url, host_ok_fn)
    by: dict[str, list] = {}
    for off in offerings:
        label = str(off.get("instrument_name") or off.get("instrument") or off.get("collection") or "unspecified")
        by.setdefault(label, []).append(off)
    rng = random.Random(seed + 3)
    scored = []
    for label, group in by.items():
        off = group[seed % len(group)]
        blob = " ".join(str(off.get(k) or "") for k in ("instrument_name", "instrument", "collection", "chord", "quality", "audio"))
        # The piano full-chord set has no instrument_name. The other collections do.
        if not off.get("instrument_name") and not re.search(r"synth|choir|arp", blob, re.I):
            blob += " piano"
        src = {"kind": "supplier", "manifest": off.get("manifest"), "pitched": False, "_host_ok": host_ok_fn}
        cand = _cand("musicmatter", label, off.get("_supplier") or "musicmatter", "CC-BY-SA-4.0", f"{label} {blob}", src, terms, hint, chords)
        if cand:
            scored.append(cand)
    ranked = [(-c["fit"], rng.random(), c) for c in scored]
    ranked.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in ranked[:8]]


def search_freesound(terms, hint, chords, seed, host_ok_fn=None, registry_url=None) -> list[dict]:
    q = urllib.parse.quote(" ".join(terms))
    url = f"https://freesound.org/search/?q={q}&f=license:%22Creative%20Commons%200%22&page=1"
    html = _get(url).decode("utf-8", "replace")
    ids = []
    for sid in re.findall(r'href="/people/[^"]+/sounds/(\d+)/"', html):
        if sid not in ids:
            ids.append(sid)
    rng = random.Random(seed + 5)
    rng.shuffle(ids)
    out = []
    for sid in ids[:6]:
        src = {"kind": "freesound", "id": sid, "pitched": False, "_host_ok": host_ok_fn}
        cand = _cand("freesound", f"freesound {sid}", "Freesound", "CC0-1.0", f"freesound {sid}", src, terms, hint, chords)
        if cand:
            out.append(cand)
    return out


def search_openverse(terms, hint, chords, seed, host_ok_fn=None, registry_url=None) -> list[dict]:
    q = urllib.parse.quote(" ".join(terms))
    doc = _json(f"https://api.openverse.org/v1/audio/?q={q}&license=cc0,by,by-sa&page_size=8")
    out = []
    for row in doc.get("results") or []:
        lic = (row.get("license") or "").lower()
        ver = row.get("license_version") or "4.0"
        if lic in ("cc0", "zero"):
            lid = "CC0-1.0"
        elif lic in ("by", "cc-by"):
            lid = f"CC-BY-{ver}"
        elif lic in ("by-sa", "cc-by-sa"):
            lid = f"CC-BY-SA-{ver}"
        else:
            continue
        if not LIC.allowed(lid):
            continue
        audio = row.get("url") or ""
        page = row.get("foreign_landing_url") or audio
        title = row.get("title") or "Openverse audio"
        creator = row.get("creator") or row.get("source") or "unknown"
        blob = f"{title} {row.get('source') or ''} {row.get('tags') or ''}"
        src = {
            "kind": "commons", "url": audio, "page": page, "license": lid,
            "title": title, "author": creator, "source": f"Openverse ({row.get('source') or 'audio'})",
            "pitched": False, "_host_ok": host_ok_fn,
        }
        dur = row.get("duration")
        if isinstance(dur, (int, float)) and dur > 20000:
            src["range_bytes"] = 1_200_000
        cand = _cand("openverse", title, creator, lid, blob, src, terms, hint, chords)
        if cand:
            out.append(cand)
    return out[:6]


def search_internet_archive(terms, hint, chords, seed, host_ok_fn=None, registry_url=None) -> list[dict]:
    q = urllib.parse.quote("(" + " OR ".join(terms) + ") AND mediatype:audio AND format:MP3")
    url = (
        "https://archive.org/advancedsearch.php?q=" + q
        + "&fl[]=identifier&fl[]=title&fl[]=creator&fl[]=licenseurl&rows=8&output=json"
    )
    docs = (_json(url).get("response") or {}).get("docs") or []
    out = []
    for doc in docs:
        lid = (LIC.from_url(doc.get("licenseurl") or "") or (None,))[0]
        if not lid or not LIC.allowed(lid):
            continue
        ident = doc.get("identifier")
        if not ident:
            continue
        audio = _archive_mp3(ident)
        if not audio:
            continue
        title = doc.get("title") or ident
        if isinstance(title, list):
            title = title[0]
        creator = doc.get("creator") or "Internet Archive"
        if isinstance(creator, list):
            creator = creator[0]
        page = f"https://archive.org/details/{ident}"
        src = {
            "kind": "commons", "url": audio, "page": page, "license": lid,
            "title": str(title), "author": str(creator), "source": "Internet Archive",
            "range_bytes": 1_200_000, "pitched": False, "_host_ok": host_ok_fn,
        }
        blob = f"{title} {creator}"
        cand = _cand("internet_archive", str(title), str(creator), lid, blob, src, terms, hint, chords)
        if cand:
            out.append(cand)
        if len(out) >= 4:
            break
    return out


def _archive_mp3(ident: str) -> str | None:
    try:
        meta = _json(f"https://archive.org/metadata/{urllib.parse.quote(ident)}")
    except Exception:
        return None
    best = None
    for f in meta.get("files") or []:
        name = f.get("name") or ""
        if not name.lower().endswith(".mp3"):
            continue
        try:
            size = int(f.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size and (size < 80_000 or size > 12_000_000):
            continue
        if best is None or size < best[0]:
            best = (size or 9_000_000, name)
    if not best:
        return None
    return f"https://archive.org/download/{ident}/{urllib.parse.quote(best[1])}"


def _wm_license(name: str) -> str | None:
    n = re.sub(r"<[^>]+>", "", name or "").lower()
    if "noncommercial" in n or "noderiv" in n or "-nc" in n or "-nd" in n:
        return None
    if "cc0" in n or "zero" in n:
        return "CC0-1.0"
    if "public domain" in n:
        return "PD"
    m = re.search(r"by-sa[^0-9]*(\d\.\d)", n) or re.search(r"by sa[^0-9]*(\d\.\d)", n)
    if m:
        return f"CC-BY-SA-{m.group(1)}"
    m = re.search(r"by[^0-9]*(\d\.\d)", n)
    if "by" in n and m:
        return f"CC-BY-{m.group(1)}"
    return None


def search_wikimedia(terms, hint, chords, seed, host_ok_fn=None, registry_url=None) -> list[dict]:
    q = urllib.parse.quote(" ".join(terms) + " audio")
    url = (
        "https://commons.wikimedia.org/w/api.php?action=query&generator=search"
        f"&gsrnamespace=6&gsrlimit=5&gsrsearch={q}&prop=imageinfo&iiprop=url|mime|extmetadata&format=json"
    )
    pages = (_json(url).get("query") or {}).get("pages") or {}
    out = []
    for page in pages.values():
        info = (page.get("imageinfo") or [None])[0] or {}
        mime = info.get("mime") or ""
        if not str(mime).startswith("audio/"):
            continue
        meta = info.get("extmetadata") or {}
        lid = _wm_license((meta.get("LicenseShortName") or {}).get("value") or "")
        if not lid:
            continue
        audio = info.get("url") or ""
        title = (page.get("title") or "Wikimedia audio").replace("File:", "")
        artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value") or "Wikimedia Commons")
        desc = f"https://commons.wikimedia.org/wiki/{urllib.parse.quote(page.get('title') or '')}"
        src = {
            "kind": "commons", "url": audio, "page": desc, "license": lid,
            "title": title, "author": artist, "source": "Wikimedia Commons",
            "pitched": False, "_host_ok": host_ok_fn,
        }
        cand = _cand("wikimedia", title, artist, lid, f"{title} {artist}", src, terms, hint, chords)
        if cand:
            out.append(cand)
    return out


def search_ccmixter(terms, hint, chords, seed, host_ok_fn=None, registry_url=None) -> list[dict]:
    q = urllib.parse.quote(" ".join(terms))
    rows = _json(f"https://ccmixter.org/api/query?f=json&limit=6&search={q}")
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        name = row.get("license_name") or ""
        low = name.lower()
        if "noncommercial" in low or "non-commercial" in low or "noderiv" in low or "no deriv" in low:
            continue
        lid = (LIC.from_url(row.get("license_url") or "") or (None,))[0]
        if not lid or not LIC.allowed(lid):
            continue
        files = [f for f in (row.get("files") or []) if (f.get("file_format_info") or {}).get("media-type") == "audio"]
        if not files:
            continue
        files.sort(key=lambda f: int(f.get("file_rawsize") or 10**12))
        audio = files[0].get("download_url") or ""
        title = row.get("upload_name") or "ccMixter"
        author = row.get("user_real_name") or row.get("user_name") or "ccMixter"
        src = {
            "kind": "commons", "url": audio, "page": row.get("file_page_url") or audio, "license": lid,
            "title": title, "author": author, "source": "ccMixter",
            "range_bytes": 1_200_000, "pitched": False, "_host_ok": host_ok_fn,
        }
        tags = row.get("upload_tags") or ""
        cand = _cand("ccmixter", title, author, lid, f"{title} {tags}", src, terms, hint, chords)
        if cand:
            out.append(cand)
    return out[:4]


def search_uiowa(terms, hint, chords, seed, host_ok_fn=None, registry_url=None) -> list[dict]:
    lib = interpret._data("library.json")
    banks = lib.get("banks") or {}
    names = sorted(banks)
    if not names:
        return []
    wanted = [n for n in names if any(t in n or n in t for t in terms)]
    rng = random.Random(seed + 11)
    if not wanted:
        rng.shuffle(names)
        wanted = names[:1]
    out = []
    for name in wanted:
        notes = (banks.get(name) or {}).get("notes") or {}
        ids = [sid for group in notes.values() for sid in group]
        if not ids:
            continue
        sid = ids[seed % len(ids)]
        src = dict(lib["sources"][sid])
        kind = src.get("kind")
        if kind == "uiowa_mis":
            origin = "uiowa"
            author = "University of Iowa Electronic Music Studios"
            license_id = "PD"
        elif kind == "freesound":
            if not host_ok("freesound.org", host_ok_fn) or not host_ok("cdn.freesound.org", host_ok_fn):
                continue
            origin = "freesound"
            author = "Freesound"
            license_id = "CC0-1.0"
        elif kind == "supplier":
            origin = "musicmatter"
            author = "musicmatter"
            license_id = "CC-BY-SA-4.0"
        else:
            continue
        src["_host_ok"] = host_ok_fn
        src.setdefault("pitched", False)
        cand = _cand(origin, f"{name}", author, license_id, name, src, terms, hint, chords)
        if cand:
            out.append(cand)
    return out


SEARCHERS = {
    "musicmatter": search_musicmatter,
    "freesound": search_freesound,
    "openverse": search_openverse,
    "internet_archive": search_internet_archive,
    "wikimedia": search_wikimedia,
    "ccmixter": search_ccmixter,
    "uiowa": search_uiowa,
}


def _named_origin(text: str) -> str | None:
    low = text.lower()
    aliases = {
        "musicmatter": ("musicmatter", "music matter"),
        "freesound": ("freesound",),
        "openverse": ("openverse",),
        "internet_archive": ("internet archive", "archive.org"),
        "wikimedia": ("wikimedia", "commons"),
        "ccmixter": ("ccmixter", "cc mixter"),
        "uiowa": ("uiowa", "university of iowa"),
    }
    hits = [name for name, words in aliases.items() if any(w in low for w in words)]
    if len(hits) == 1 and re.search(r"\b(only|just|exclusively|from)\b", low):
        return hits[0]
    return None


def _select(groups: dict[str, list], order: list[str], n: int, hint: str | None, named: str | None) -> list[dict]:
    chosen: list[dict] = []
    seen = set()
    counts: Counter = Counter()

    def add(cand) -> bool:
        if cand["key"] in seen:
            return False
        seen.add(cand["key"])
        chosen.append(cand)
        counts[cand["origin"]] += 1
        return True

    if named:
        for cand in groups.get(named) or []:
            if len(chosen) >= n:
                break
            add(cand)
    if hint and not named and len(chosen) < n:
        pool = [c for group in groups.values() for c in group if c["instrument"] == hint or hint in c["title"].lower()]
        pool.sort(key=lambda c: (-c["fit"], c["origin"], c["title"]))
        if pool:
            add(pool[0])
    cap = n if named else max(1, n // 2)
    guard = 0
    while len(chosen) < n and guard < 8:
        guard += 1
        moved = False
        for origin in ([named] + [o for o in order if o != named]) if named else order:
            group = groups.get(origin) or []
            if counts[origin] >= cap:
                continue
            for cand in group:
                if cand["key"] in seen:
                    continue
                add(cand)
                moved = True
                break
            if len(chosen) >= n:
                break
        if not moved:
            # Repeat sounds we already accepted before letting one origin take the rest.
            # A single origin may fill the plan only when nothing else came back.
            if cap < n and len(counts) < 2:
                cap = n
                continue
            break
    return chosen


def _voicing(sym: str) -> list[int]:
    picked = CH.pick(sym)
    if picked and len(picked) > 1 and picked[1]:
        return list(picked[1])[:6]
    return [60, 64, 67]


def _audio_reachable(cand: dict) -> bool:
    """Drop a file the renderer would not be allowed to download."""
    src = cand.get("source") or {}
    url = src.get("url") or ""
    if not url or src.get("kind") in ("freesound", "supplier"):
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-2047"})
        with urllib.request.urlopen(req, timeout=25) as r:
            if r.status >= 400:
                return False
            r.read(32)
        return True
    except Exception as exc:
        print(json.dumps({"event": "discover_fetch", "origin": cand.get("origin"), "error": str(exc)[:160]}), flush=True)
        return False


def _declared_on_page(cand: dict) -> bool:
    """Commons clips stay only when the live page shows the license we will claim."""
    src = cand.get("source") or {}
    if src.get("kind") != "commons":
        return True
    page = src.get("page") or ""
    try:
        html = _get(page).decode("utf-8", "replace")
    except Exception as exc:
        print(json.dumps({"event": "discover_license", "origin": cand.get("origin"), "error": str(exc)[:160]}), flush=True)
        return False
    parsed = LIC.from_id(src.get("license"))
    found = LIC.page_licenses(html)
    uri = (parsed[2] or "").rstrip("/")
    if parsed[0] in found or (uri and uri in html) or (parsed[0] == "PD" and "public domain" in html.lower()):
        return True
    return False


def plan_for(comp: dict, seed: int = 1, instrument: str | None = None, host_ok_fn=None, searchers=None, registry_url=None, verify_live: bool = True) -> dict:
    """Build a plan from an even-handed search. Raises ValueError if nothing usable came back."""
    found = CH.find_full(comp.get("composition") or "") or CH.find_full(
        f"{comp.get('composition') or ''} {comp.get('performance') or ''}"
    )
    if not found:
        raise ValueError('no chord symbols found in the Composition (e.g. "Cm, Am, F")')
    syms = [s for s, _ in found]
    words = f"{comp.get('composition') or ''} {comp.get('performance') or ''}"
    length = interpret._length(words)
    hint = (instrument or "").strip().lower() or None
    global _EXCLUDE
    previous_exclude = _EXCLUDE
    _EXCLUDE = excluded_terms(words)
    query = terms_for(comp, hint, seed)
    named = _named_origin(words)
    order = list(ORIGINS)
    rot = seed % len(order)
    order = order[rot:] + order[:rot]
    if named and named in order:
        order = [named] + [o for o in order if o != named]
    fns = searchers or SEARCHERS
    groups: dict[str, list] = {}
    errors = {}
    for origin in order:
        fn = fns.get(origin)
        if not fn:
            continue
        try:
            hits = fn(query, hint, syms, seed, host_ok_fn, registry_url) or []
        except Exception as exc:
            errors[origin] = str(exc)[:180]
            hits = []
        hits = [h for h in hits if h]
        hits.sort(key=lambda c: (-c["fit"], c["title"]))
        for hit in hits:
            groups.setdefault(hit.get("origin") or origin, []).append(hit)
    for origin in ORIGINS:
        groups.setdefault(origin, [])
    print(json.dumps({
        "event": "discover",
        "terms": query,
        "instrument": hint,
        "order": order,
        "counts": {k: len(v) for k, v in groups.items()},
        "errors": errors,
    }, ensure_ascii=False), flush=True)
    n = min(6, max(len(syms), 2))
    rejected: set[str] = set()
    chosen: list[dict] = []
    for _attempt in range(8):
        usable = {origin: [c for c in hits if c["key"] not in rejected] for origin, hits in groups.items()}
        picked = _select(usable, order, n, hint, named)
        if not picked:
            chosen = []
            break
        if not verify_live:
            chosen = picked
            break
        bad = [c for c in picked if not _declared_on_page(c) or not _audio_reachable(c)]
        if not bad:
            chosen = picked
            break
        for cand in bad:
            rejected.add(cand["key"])
    if not chosen:
        _EXCLUDE = previous_exclude
        raise ValueError("No licensed sounds came back from the sources that were queried.")
    # One clip per chord, repeating the chosen set so a short pool still covers the form.
    sources = {}
    events = []
    used: dict[str, str] = {}

    def sid_for(cand: dict, suffix: str) -> str:
        if cand["key"] in used:
            return used[cand["key"]]
        sid = f"{cand['origin'][:2]}-{suffix}"
        used[cand["key"]] = sid
        sources[sid] = cand["source"]
        return sid

    seq = found
    reps = max(1, round(length / (len(seq) * 9.0)))
    seq = (seq * reps)[:12]
    dur = length / len(seq)
    chords = []
    strike_cands: list[dict] = []
    for i, (sym, _bass) in enumerate(seq):
        cand = chosen[i % len(chosen)]
        strike_cands.append(cand)
        sid = sid_for(cand, str(i))
        chords.append({
            "t": round(i * dur, 3),
            "dur": round(dur, 3),
            "symbol": sym,
            "voicing": _voicing(sym),
            "section": "A" if i < len(seq) / 2 else "B",
        })
        events.append({
            "t": round(i * dur + 0.2, 3),
            "source": sid,
            "offset_s": 0.0,
            "dur_s": round(min(8.0, max(2.0, dur * 0.9)), 3),
            "fade_in_s": 0.03,
            "fade_out_s": 1.2,
            "gain": 0.72 if i % len(chosen) == 0 else 0.58,
            "flatten": False,
            "role": f"{cand['instrument']} ({cand['origin']}) {cand['title'][:80]}",
        })
    bed = None
    for cand in chosen:
        if cand["origin"] != chosen[0]["origin"]:
            bed = cand
            break
    layers = [{
        "id": "strikes",
        "type": "clips",
        "bus": "keys",
        "role": "chord colors from several sources, trimmed, not time-stretched",
        "events": events,
    }]
    if bed:
        sid = sid_for(bed, "bed")
        layers.append({
            "id": "bed",
            "type": "clips",
            "bus": "pad",
            "role": f"bed {bed['instrument']} ({bed['origin']}), trimmed, not time-stretched",
            "events": [{
                "t": 0.0,
                "source": sid,
                "offset_s": 0.0,
                "dur_s": round(min(12.0, length), 3),
                "fade_in_s": 1.5,
                "fade_out_s": 2.0,
                "gain": 0.28,
                "flatten": False,
                "role": f"bed {bed['instrument']} ({bed['origin']}) {bed['title'][:80]}",
            }],
        })
    mix_counts: Counter = Counter(c["origin"] for c in strike_cands)
    instruments = []
    for cand in strike_cands:
        if cand["instrument"] not in instruments:
            instruments.append(cand["instrument"])
    if bed:
        mix_counts[bed["origin"]] += 1
        if bed["instrument"] not in instruments:
            instruments.append(bed["instrument"])
    total = sum(mix_counts.values()) or 1
    mix = {
        "by_origin": dict(mix_counts),
        "share": {k: round(v / total, 2) for k, v in mix_counts.items()},
        "instruments": instruments,
        "queried": order,
        "returned": {k: len(v) for k, v in groups.items()},
        "terms": query,
        "instrument_hint": hint,
        "rule": "Same query to every origin. Round-robin selection. One origin is capped at half the clips unless the notes name that origin, or it is the only origin that returned sounds.",
    }
    note = (
        "Source mix: " + ", ".join(f"{k} {v}" for k, v in mix_counts.most_common())
        + ". Instruments: " + ", ".join(instruments)
        + ". No pitch-shift and no time-stretch; clips are trimmed and faded."
    )
    title = (comp.get("title") or "Untitled")
    soft = bool(re.search(r"ambient|soft|calm|slow|gentle|dark|no harsh", words, re.I))
    _EXCLUDE = previous_exclude
    return {
        "nc_plan_version": "0.1",
        "id": (re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "untitled"),
        "title": title + " (discovered performance)",
        "composition": {
            "title": title,
            **({"composer": comp["composer"]} if comp.get("composer") else {}),
            "words_verbatim": {"composition": comp.get("composition") or "", "performance": comp.get("performance") or ""},
        },
        "interpreter": {
            "kind": "discover",
            "at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
            "note": "Sounds were chosen by querying every origin with the same words and rotating the pick. musicmatter is one Supplier, not the default.",
        },
        "seed": int(seed),
        "length_s": round(length, 3),
        "tail_s": 4.0,
        "pulse_bpm": 60,
        "pitch_tolerance_cents": 15,
        "sections": [
            {"id": "A", "start_s": 0, "end_s": round(length / 2, 3), "harmony": " ".join(syms)},
            {"id": "B", "start_s": round(length / 2, 3), "end_s": round(length, 3), "harmony": " ".join(syms)},
        ],
        "chords": chords,
        "sources": sources,
        "banks": {},
        "layers": layers,
        "master": {
            "buses": {
                "keys": {"filters": [{"type": "lp", "hz": 5000 if soft else 8000, "order": 2}], "lufs": -20.0, "pan": -0.05},
                "pad": {"filters": [{"type": "lp", "hz": 2500 if soft else 6000, "order": 2}], "lufs": -24.0, "pan": 0.08},
            },
            "reverb": {"engine": "dragonfly", "send_lu": 4.0 if soft else 1.8, "params": {"decay_s": 5.0 if soft else 2.6}},
            "hp_hz": 30,
            "high_shelf": {"hz": 9000, "db": -3.0 if soft else -1.0},
            "loudness": {"lufs": -17, "tp": -1.5, "method": "linear_tp_limiter"},
        },
        "source_mix": mix,
        "notes": [note],
    }
