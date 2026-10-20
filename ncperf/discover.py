"""Find sounds for a Performance without preferring one supplier.

Registered Suppliers (today, musicmatter.art) are one origin among the open
commons: Freesound, Openverse, the Internet Archive, Wikimedia Commons,
ccMixter, and the bundled UIowa MIS library. Each origin is queried with the
same words. A failure in one origin is skipped.

A chord strike has to be that chord. It is either a whole-chord file of that
root and quality, or the chord built from single notes that were recorded at
those pitches (one sample per tone, layered with a small onset jitter). Notes
are not pitch-shifted or time-stretched. No origin supplies more than half the
strikes unless the notes name that origin, and unless it is the only origin
that can play the chords. Texture is unpitched: a field recording, room tone,
or pad, never a song. Every source kept in the plan has a title, an author, a
license, and a page.
"""
from __future__ import annotations

import html as html_lib
import json
import random
import re
import urllib.parse
import urllib.request
import zlib
from collections import Counter

from . import chords as CH
from . import interpret
from . import licenses as LIC
from . import notemap
from . import pitch
from .fit import fit_plan

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
        "title": title or "",
        "author": author or "",
        "license": LIC.from_id(license_id)[0],
        "instrument": _instrument_label(blob, hint),
        "fit": _fit(blob, terms, hint, chords),
        "source": source,
        "key": url or title,
        "text": blob,
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


# A texture bed may be a pad or a field recording. A song, a cover, or a live
# concert is never a bed, even when the uploader marked the file CC0.
_RIGHTS_BAD = re.compile(
    r"\b(live|concert|cover|karaoke|tribute|bootleg|lyrics|album|song|remix|band|singer|beat|bpm|loop|mix)\b"
    r"|live\s*@|mama tried|full mix",
    re.I,
)
# A bed has to be a field recording, room tone, or a pad. "Ambient" and "noise" alone
# are too broad: they also describe songs and processed collages.
_TEXTURE_OK = re.compile(
    r"\b(field recording|room\s*tone|foley|sound effect|sfx|rain|wind|waves|ocean|"
    r"forest|hiss|hum|whoosh|rustle|crickets|stream|thunder|pad)\b",
    re.I,
)
_TEXTURE_BAD = re.compile(r"\b(pitched|stretch(?:ed|ing)?|time-?stretch|looped|song|melody|chord)\b", re.I)
_NOTE_NAME = re.compile(r"(?<![A-Za-z])[A-G](?:#|b)?\d(?!\d)")
_CREDIT_KEYS = ("title", "author", "license", "page")


def _rights_ok(blob: str) -> bool:
    return not _RIGHTS_BAD.search(blob or "")


def _declared_chord(text: str, wanted: list[str] | None) -> str | None:
    """The one composition chord named in a title, if the title names exactly one."""
    found = []
    for sym, _bass in CH.find_full(text or ""):
        if wanted is None or sym in wanted:
            found.append(sym)
    found = list(dict.fromkeys(found))
    if len(found) == 1:
        return found[0]
    return None


def _offering_symbol(off: dict) -> str | None:
    """Root and quality from the offering. The filename is the fallback for piano files, which have no chord field."""
    raw = off.get("chord")
    if isinstance(raw, str) and raw.strip():
        spelled = CH.spell(raw.strip())
        if spelled:
            return spelled
    root = off.get("root")
    if root is not None and str(root).strip():
        spelled = CH.spell(str(root).strip() + str(off.get("quality") or ""))
        if spelled:
            return spelled
    name = (off.get("audio") or off.get("manifest") or "").rsplit("/", 1)[-1]
    name = re.sub(r"\.(flac|nc\.json|wav|mp3|ogg)$", " ", name, flags=re.I).replace("_", " ")
    found = [sym for sym, _bass in CH.find_full(name)]
    if not found:
        return None
    return max(found, key=len)


def _offering_instrument(off: dict) -> str:
    name = str(off.get("instrument_name") or "").strip().lower()
    if name:
        if "choir" in name or "chorus" in name:
            return "choir"
        if "fat" in name and "synth" in name:
            return "fat synth"
        if "arp" in name:
            return "synth arp"
        if "synth" in name:
            return "synth"
        if "piano" in name:
            return "piano"
        return _instrument_label(name, None)
    blob = " ".join(str(off.get(k) or "") for k in ("instrument", "collection", "audio", "manifest")).lower()
    if "jacobschoir" in blob:
        return "choir"
    if "fatsynth" in blob:
        return "fat synth"
    if "syntharp" in blob:
        return "synth arp"
    return "piano"


def _voicing_kind(off: dict) -> str:
    gesture = str(off.get("gesture") or "").lower()
    voicing = str(off.get("voicing") or "").lower()
    path = f"{off.get('audio') or ''} {off.get('manifest') or ''}".lower()
    if gesture == "alt" or "inv" in voicing or re.search(r"inv\d", path):
        return "alt"
    return "exact"


def _hint_ok(instrument: str, hint: str | None) -> bool:
    if not hint:
        return True
    inst = (instrument or "").lower()
    if hint == "piano":
        return inst == "piano"
    if hint in ("synth", "arp"):
        return "synth" in inst or inst == "arp"
    if hint in ("choir", "voice"):
        return inst == "choir"
    return hint == inst or hint in inst


def _blob(cand: dict) -> str:
    src = cand.get("source") or {}
    return " ".join(str(x or "") for x in (
        cand.get("title"), cand.get("text"), cand.get("author"), cand.get("instrument"), src.get("title"),
    ))


def _texture_ok(cand: dict) -> bool:
    """Unpitched bed only. A chord file, a note, or a song excerpt is not texture."""
    if cand.get("origin") in ("musicmatter", "uiowa"):
        return False
    if cand.get("chord_symbol") or cand.get("pitched") or cand.get("_wrong_chord"):
        return False
    inst = cand.get("instrument") or ""
    if any(word in inst for word in (
        "piano", "rhodes", "synth", "choir", "voice", "organ", "flute", "guitar", "harp",
        "cello", "violin", "string", "brass", "pluck", "bell", "arp",
    )):
        return False
    blob = _blob(cand)
    if not _rights_ok(blob) or _TEXTURE_BAD.search(blob):
        return False
    if str(cand.get("og_type") or "").lower() == "song":
        return False
    if _NOTE_NAME.search(blob) or CH.find_full(blob):
        return False
    return bool(_TEXTURE_OK.search(blob))


def _manifest_credit(url: str) -> dict | None:
    if not url:
        return None
    try:
        manifest = _json(url)
    except Exception as exc:
        print(json.dumps({"event": "discover_manifest", "error": str(exc)[:160]}), flush=True)
        return None
    lid = manifest.get("license")
    if isinstance(lid, dict):
        lid = lid.get("id") or lid.get("name")
    parsed = LIC.from_id(str(lid or ""))
    if not lid or not LIC.allowed(parsed[0]):
        return None
    creator = manifest.get("creator") or {}
    if isinstance(creator, str):
        author = creator
    else:
        author = creator.get("name") or ""
    title = manifest.get("title") or ""
    if not str(title).strip() or not str(author).strip():
        return None
    chord = manifest.get("chord")
    symbol = None
    bass_pc = None
    if isinstance(chord, dict):
        symbol = CH.spell(str(chord.get("symbol") or ""))
        if chord.get("bass_midi") is not None:
            bass_pc = int(chord["bass_midi"]) % 12
    elif isinstance(chord, str):
        symbol = CH.spell(chord)
    return {
        "title": str(title).strip(),
        "author": str(author).strip(),
        "license": parsed[0],
        "page": url,
        "chord_symbol": symbol,
        "bass_pc": bass_pc,
    }


def _freesound_credit(sid: str) -> dict | None:
    if not sid:
        return None
    try:
        raw = _get(f"https://freesound.org/s/{sid}/")
    except Exception as exc:
        print(json.dumps({"event": "discover_license", "origin": "freesound", "error": str(exc)[:160]}), flush=True)
        return None
    page = raw.decode("utf-8", "replace")
    found = LIC.page_licenses(page)
    if len(found) != 1 or not LIC.allowed(found[0]):
        return None
    title = re.search(r'og:audio:title" content="([^"]*)"', page)
    artist = re.search(r'og:audio:artist" content="([^"]*)"', page)
    og_type = re.search(r'og:type" content="([^"]*)"', page)
    if not title or not artist:
        return None
    # The site-wide meta description mentions "ambience" for every sound. Use the sound's own blurb.
    desc = re.search(r'twitter:description" content="([^"]*)"', page, re.I)
    if not desc:
        desc = re.search(r'property="og:description" content="([^"]*)"', page, re.I)
    tags = " ".join(urllib.parse.unquote(t).replace("-", " ") for t in re.findall(r"/browse/tags/([^\"/?#]+)", page))
    parts = [html_lib.unescape(title.group(1)), html_lib.unescape(artist.group(1))]
    if desc:
        parts.append(html_lib.unescape(desc.group(1)))
    if tags:
        parts.append(html_lib.unescape(tags))
    text = " ".join(parts)
    if not parts[0].strip() or not parts[1].strip():
        return None
    return {
        "title": parts[0].strip(),
        "author": parts[1].strip(),
        "license": found[0],
        "page": f"https://freesound.org/s/{sid}/",
        "text": text,
        "og_type": (og_type.group(1).strip().lower() if og_type else ""),
    }


def _ensure_credit(cand: dict, verify_live: bool, bass=None) -> dict | None:
    """Copy title, author, license, and page onto the source. Drop the clip when any of them is missing."""
    cand = dict(cand)
    src = dict(cand.get("source") or {})
    kind = src.get("kind")
    if verify_live and kind == "supplier":
        credit = _manifest_credit(src.get("manifest") or "")
        if not credit:
            return None
        if cand.get("chord_symbol") and credit.get("chord_symbol") and credit["chord_symbol"] != cand["chord_symbol"]:
            return None
        if credit.get("chord_symbol"):
            cand["chord_symbol"] = credit["chord_symbol"]
        if credit.get("bass_pc") is not None:
            cand["bass_pc"] = credit["bass_pc"]
        src.update({k: credit[k] for k in _CREDIT_KEYS})
    elif verify_live and kind == "freesound":
        credit = _freesound_credit(str(src.get("id") or ""))
        if not credit:
            return None
        src.update({k: credit[k] for k in _CREDIT_KEYS})
        cand["text"] = credit["text"]
        cand["og_type"] = credit.get("og_type") or ""
    else:
        for key in _CREDIT_KEYS:
            if not src.get(key):
                src[key] = cand.get(key)
        if kind == "supplier" and not src.get("page"):
            src["page"] = src.get("manifest")
        if kind == "freesound" and not src.get("page") and src.get("id"):
            src["page"] = f"https://freesound.org/s/{src['id']}/"
    if bass is not None and cand.get("bass_pc") != bass:
        return None
    missing = [key for key in _CREDIT_KEYS if not str(src.get(key) or "").strip()]
    if missing:
        return None
    if str(src.get("author") or "").strip().lower() in ("unknown", "?", "none"):
        return None
    lid = LIC.from_id(str(src.get("license") or ""))[0]
    if not LIC.allowed(lid):
        return None
    src["license"] = lid
    cand["source"] = src
    cand["title"] = str(src["title"]).strip()
    cand["author"] = str(src["author"]).strip()
    cand["license"] = lid
    cand["page"] = src["page"]
    return cand


def _match_label(cand: dict) -> str:
    if cand.get("instrument") == "piano":
        return "piano fallback"
    if cand.get("voicing_kind") == "alt":
        return "alt voicing"
    return "exact"


def _tier(cand: dict) -> int:
    piano = cand.get("instrument") == "piano"
    alt = cand.get("voicing_kind") == "alt"
    if not piano and not alt:
        return 0
    if not piano and alt:
        return 1
    if piano and not alt:
        return 2
    return 3


def _strike_order(cands: list[dict], hint: str | None, seed: int, index: int, named: str | None) -> list[dict]:
    pool = [c for c in cands if c.get("origin") != "uiowa" and _rights_ok(_blob(c))]
    for word in _EXCLUDE:
        pool = [c for c in pool if not _hint_ok(c.get("instrument") or "", word)]
    if named:
        named_pool = [c for c in pool if c.get("origin") == named]
        if named_pool:
            pool = named_pool
    if hint == "piano":
        pianos = [c for c in pool if c.get("instrument") == "piano"]
        if pianos:
            pool = pianos
    ordered: list[dict] = []
    for tier_n in range(4):
        tier = [c for c in pool if _tier(c) == tier_n]
        def rotate(names: list[str]) -> list[str]:
            if not names:
                return []
            start = (seed + index) % len(names)
            return names[start:] + names[:start]

        if hint and hint != "piano":
            hinted_inst = sorted({c.get("instrument") or "" for c in tier if _hint_ok(c.get("instrument") or "", hint)})
            other_inst = sorted({c.get("instrument") or "" for c in tier if (c.get("instrument") or "") not in hinted_inst})
            instruments = rotate(hinted_inst) + rotate(other_inst)
        else:
            instruments = rotate(sorted({c.get("instrument") or "" for c in tier}))
        if not instruments:
            continue
        buckets = {inst: [c for c in tier if (c.get("instrument") or "") == inst] for inst in instruments}
        for inst in instruments:
            rows = sorted(buckets[inst], key=lambda c: (c.get("title") or "", str(c.get("key") or "")))
            if rows:
                shift = (seed + index) % len(rows)
                rows = rows[shift:] + rows[:shift]
            buckets[inst] = rows
        while any(buckets.values()):
            for inst in instruments:
                if buckets[inst]:
                    ordered.append(buckets[inst].pop(0))
    return ordered


def search_musicmatter(terms, hint, chords, seed, host_ok_fn=None, registry_url=None) -> list[dict]:
    """Offerings whose root and quality are one of the composition's chords."""
    offerings = _mm_index(registry_url, host_ok_fn)
    wanted = {CH.spell(s) or s for s in chords}
    rng = random.Random(seed + 3)
    buckets: dict[tuple, list] = {}
    for off in offerings:
        if not off.get("manifest"):
            continue
        sym = _offering_symbol(off)
        if not sym or sym not in wanted:
            continue
        inst = _offering_instrument(off)
        kind = _voicing_kind(off)
        buckets.setdefault((sym, inst, kind), []).append(off)
    out = []
    for (sym, inst, kind), group in buckets.items():
        group = sorted(group, key=lambda o: (0 if o.get("default_voicing") else 1, rng.random()))
        for off in group[:2]:
            blob = " ".join(str(off.get(k) or "") for k in (
                "instrument_name", "instrument", "collection", "chord", "root", "quality", "audio",
            ))
            title = f"{off.get('instrument_name') or inst} {sym}"
            author = off.get("_supplier") or "musicmatter"
            src = {
                "kind": "supplier",
                "manifest": off.get("manifest"),
                "pitched": True,
                "page": off.get("manifest"),
                "title": title,
                "author": author,
                "license": "CC-BY-SA-4.0",
                "_host_ok": host_ok_fn,
            }
            cand = _cand("musicmatter", title, author, "CC-BY-SA-4.0", f"{title} {blob}", src, terms, hint, chords)
            if not cand:
                continue
            cand["chord_symbol"] = sym
            cand["voicing_kind"] = kind
            cand["pitched"] = True
            cand["instrument"] = inst
            out.append(cand)
    return out


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
        if not _rights_ok(blob):
            continue
        cand = _cand("openverse", title, creator, lid, blob, src, terms, hint, chords)
        if cand:
            declared = _declared_chord(str(title), chords)
            if declared:
                cand["chord_symbol"] = declared
                cand["voicing_kind"] = "exact"
                cand["pitched"] = True
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
        if not _rights_ok(blob):
            continue
        cand = _cand("internet_archive", str(title), str(creator), lid, blob, src, terms, hint, chords)
        if cand:
            declared = _declared_chord(str(title), chords)
            if declared:
                cand["chord_symbol"] = declared
                cand["voicing_kind"] = "exact"
                cand["pitched"] = True
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
        blob = f"{title} {artist}"
        if not _rights_ok(blob):
            continue
        cand = _cand("wikimedia", title, artist, lid, blob, src, terms, hint, chords)
        if cand:
            declared = _declared_chord(str(title), chords)
            if declared:
                cand["chord_symbol"] = declared
                cand["voicing_kind"] = "exact"
                cand["pitched"] = True
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
        blob = f"{title} {tags}"
        if not _rights_ok(blob):
            continue
        cand = _cand("ccmixter", title, author, lid, blob, src, terms, hint, chords)
        if cand:
            declared = _declared_chord(str(title), chords)
            if declared:
                cand["chord_symbol"] = declared
                cand["voicing_kind"] = "exact"
                cand["pitched"] = True
            out.append(cand)
    return out[:4]


def search_uiowa(terms, hint, chords, seed, host_ok_fn=None, registry_url=None) -> list[dict]:
    """UIowa MIS is single notes and chromatic runs, not beds or whole-chord files.

    Strikes use those notes through the pitch-verified map (see ncperf.notemap).
    This search stays empty so a run is not dropped in as texture.
    """
    return []


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


def _pluckish(name: str) -> bool:
    n = (name or "").lower()
    return any(word in n for word in ("pizz", "guitar", "harp", "pluck"))


def _mentioned(text: str) -> list[str]:
    low = text.lower()
    out = []
    for word in ("pluck", "pizz", *LEXICON):
        if word not in out and re.search(rf"\b{re.escape(word)}", low):
            out.append(word)
    return out


def _wants(opt: dict, word: str) -> bool:
    if word in ("pluck", "pizz") and opt.get("pluck"):
        return True
    return _hint_ok(opt.get("instrument") or "", word)


def assign_strikes(groups: list[list[dict]], seed: int, hint: str | None, mentioned: list[str], cap: int) -> list[dict] | None:
    """One strike per chord, with no origin used more than ``cap`` times."""
    n = len(groups)
    chosen: list = [None] * n
    counts: Counter = Counter()
    used: Counter = Counter()
    pluck_wanted = any(word in ("pluck", "pizz") for word in mentioned)

    def rank(i: int, opt: dict):
        inst = opt.get("instrument") or ""
        origin = opt.get("origin") or ""
        return (
            0 if pluck_wanted and opt.get("pluck") else 1,
            0 if hint and _wants(opt, hint) else 1,
            0 if any(_wants(opt, word) for word in mentioned) else 1,
            counts[origin],
            int(opt.get("tier") or 0),
            1 if used[inst] else 0,
            zlib.adler32(f"{seed}|{i}|{origin}|{inst}|{opt.get('built')}".encode()) % 1000,
        )

    def bt(i: int) -> bool:
        if i == n:
            return True
        seen = set()
        for opt in sorted(groups[i], key=lambda row: rank(i, row)):
            origin = opt.get("origin") or ""
            inst = opt.get("instrument") or ""
            if counts[origin] >= cap:
                continue
            pair = (origin, inst, opt.get("built"))
            if pair in seen:
                continue
            seen.add(pair)
            counts[origin] += 1
            used[inst] += 1
            chosen[i] = opt
            if bt(i + 1):
                return True
            counts[origin] -= 1
            used[inst] -= 1
            chosen[i] = None
        return False

    if not bt(0):
        return None
    return chosen


def plan_for(comp: dict, seed: int = 1, instrument: str | None = None, host_ok_fn=None, searchers=None, registry_url=None, verify_live: bool = True, note_maps=None) -> dict:
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
    seen = {hit["key"] for hits in groups.values() for hit in hits if hit.get("key")}
    texture_terms = ["field recording", "room tone", "ambience"]
    for origin in order:
        if origin in ("musicmatter", "uiowa"):
            continue
        fn = fns.get(origin)
        if not fn:
            continue
        try:
            extra = fn(texture_terms, None, syms, seed + 17, host_ok_fn, registry_url) or []
        except Exception as exc:
            errors[origin + ":texture"] = str(exc)[:180]
            extra = []
        for hit in extra:
            if not hit or hit.get("key") in seen:
                continue
            seen.add(hit["key"])
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
    pool = [c for hits in groups.values() for c in hits]
    for cand in pool:
        if cand.get("chord_symbol"):
            spelled = CH.spell(str(cand["chord_symbol"])) or cand["chord_symbol"]
            cand["chord_symbol"] = spelled
            if spelled not in syms:
                cand["_wrong_chord"] = True
        else:
            declared = _declared_chord(cand.get("title") or "", syms)
            if declared:
                cand["chord_symbol"] = declared
                cand["voicing_kind"] = cand.get("voicing_kind") or "exact"
                cand["pitched"] = True
        if cand.get("origin") != "musicmatter" and not _rights_ok(_blob(cand)):
            cand["_blocked"] = True

    def accept(cand: dict, sym: str | None, bass) -> dict | None:
        if cand.get("_blocked"):
            return None
        got = _ensure_credit(cand, verify_live, bass=bass)
        if not got:
            return None
        if sym and got.get("chord_symbol") != sym:
            declared = _declared_chord(got.get("title") or "", [sym])
            if declared != sym:
                return None
            got["chord_symbol"] = sym
            got["voicing_kind"] = got.get("voicing_kind") or "exact"
            got["pitched"] = True
        if not _rights_ok(_blob(got)):
            return None
        if verify_live and (not _declared_on_page(got) or not _audio_reachable(got)):
            return None
        src = {k: v for k, v in got["source"].items() if not str(k).startswith("_")}
        got["source"] = src
        return got

    sources = {}
    events = []
    banks: dict = {}
    note_layers = []
    assembled: list[dict] = []
    used: dict[str, str] = {}
    sample_ids: dict[str, str] = {}

    def sid_for(cand: dict, suffix: str) -> str:
        if cand["key"] in used:
            return used[cand["key"]]
        sid = f"{cand['origin'][:2]}-{suffix}"
        used[cand["key"]] = sid
        sources[sid] = cand["source"]
        return sid

    def sid_for_sample(sample: dict) -> str:
        seg = (sample.get("segment") or {}).get("index")
        key = f"{sample.get('url') or sample.get('id')}|{seg}"
        if key in sample_ids:
            return sample_ids[key]
        sid = f"n{len(sources)}"
        while sid in sources:
            sid = f"n{len(sources)}x{len(sample_ids)}"
        sample_ids[key] = sid
        keep = {}
        for field in ("kind", "url", "id", "page", "title", "author", "license", "pitched", "expect_midi", "segment"):
            if sample.get(field) not in (None, ""):
                keep[field] = sample[field]
        sources[sid] = keep
        return sid

    if note_maps is None and verify_live:
        try:
            note_maps, library_meta = notemap.ensure(syms)
        except Exception as exc:
            note_maps, library_meta = [], {"skipped": [], "error": str(exc)[:180]}
    else:
        library_meta = {}
        if note_maps is None:
            note_maps = []
    assembly = notemap.assemblies_for(syms, note_maps or [], _EXCLUDE)

    seq = found
    reps = max(1, round(length / (len(seq) * 9.0)))
    seq = (seq * reps)[:12]
    dur = length / len(seq)
    chords = []
    per_chord = []
    option_groups: list[list[dict]] = []
    for i, (sym, bass) in enumerate(seq):
        usable = [
            c for c in pool
            if c.get("chord_symbol") == sym and not c.get("_wrong_chord") and not c.get("_blocked") and c.get("origin") != "uiowa"
        ]
        opts = []
        for row in assembly.get(sym) or []:
            if any(_hint_ok(row.get("instrument") or "", word) for word in _EXCLUDE):
                continue
            opts.append(dict(row))
        for cand in _strike_order(usable, hint, seed, i, None):
            opts.append({
                "built": "whole-chord file",
                "origin": cand.get("origin"),
                "instrument": cand.get("instrument"),
                "pluck": _pluckish(cand.get("instrument") or ""),
                "tier": _tier(cand),
                "cand": cand,
            })
        option_groups.append(opts)
    if named and option_groups and all(any(row.get("origin") == named for row in group) for group in option_groups):
        option_groups = [[row for row in group if row.get("origin") == named] for group in option_groups]
    origins_here = {row.get("origin") for group in option_groups for row in group}
    missing = []

    def add_whole(i: int, sym: str, bass, picked: dict) -> None:
        sid = sid_for(picked, str(len(sources)))
        match = _match_label(picked)
        chords.append({
            "t": round(i * dur, 3),
            "dur": round(dur, 3),
            "symbol": sym,
            "voicing": _voicing(sym),
            "section": "A" if i < len(seq) / 2 else "B",
        })
        events.append({
            "t": round(i * dur + 0.35, 3),
            "source": sid,
            "offset_s": 0.0,
            "dur_s": 4.2,
            "fade_in_s": 0.02,
            "fade_out_s": 1.4,
            "gain": 0.72,
            "flatten": False,
            "role": f"{picked['instrument']} ({picked['origin']}) {sym} {match}: {picked['title'][:80]}",
        })
        per_chord.append({
            "symbol": sym,
            "bass": bass,
            "origin": picked["origin"],
            "instrument": picked["instrument"],
            "built": "whole-chord file",
            "match": match,
            "title": picked["title"],
            "author": picked["author"],
            "license": picked["license"],
            "licenses": [picked["license"]],
            "page": picked["page"],
            "source": sid,
        })

    def add_assembled(i: int, sym: str, bass, opt: dict) -> None:
        voicing = [int(n) for n in opt["voicing"]]
        bank_id = f"strike-{i}"
        notes = {}
        tones = []
        for sample in opt["samples"]:
            sid = sid_for_sample(sample)
            midi = int(sample["midi"])
            notes.setdefault(str(midi), []).append(sid)
            tones.append({
                "midi": midi,
                "note": pitch.NOTE(midi),
                "title": sample.get("title"),
                "author": sample.get("author"),
                "license": sample.get("license"),
                "page": sample.get("page"),
                "cents": sample.get("cents"),
            })
        banks[bank_id] = {"notes": notes}
        # One layer for every assembled strike. A bank per chord keeps each
        # instrument's samples together without spending a layer per note.
        assembled.append({"chord": i, "bank": bank_id})
        chords.append({
            "t": round(i * dur, 3),
            "dur": round(dur, 3),
            "symbol": sym,
            "voicing": voicing,
            "section": "A" if i < len(seq) / 2 else "B",
        })
        per_chord.append({
            "symbol": sym,
            "bass": bass,
            "origin": opt["origin"],
            "instrument": opt["instrument"],
            "built": "assembled notes",
            "match": "native pitch",
            "title": opt.get("title"),
            "author": opt.get("author"),
            "license": opt.get("license"),
            "licenses": list(opt.get("licenses") or []),
            "page": opt.get("page"),
            "voicing": voicing,
            "tones": tones,
        })

    if len(origins_here) < 2:
        for i, (sym, bass) in enumerate(seq):
            usable = [
                c for c in pool
                if c.get("chord_symbol") == sym and not c.get("_wrong_chord") and not c.get("_blocked") and c.get("origin") != "uiowa"
            ]
            picked = None
            for cand in _strike_order(usable, hint, seed, i, named):
                picked = accept(cand, sym, bass)
                if picked:
                    break
            if not picked:
                missing.append(sym)
                continue
            add_whole(i, sym, bass, picked)
        if missing:
            _EXCLUDE = previous_exclude
            raise ValueError(
                "No exact chord recording for: " + ", ".join(dict.fromkeys(missing))
                + ". A pitched clip in another key was not used."
            )
    else:
        cap = max(1, len(seq) // 2)
        dead: list[set] = [set() for _ in seq]
        chosen = None
        missing_syms: list[str] = []
        for _attempt in range(48):
            current = []
            for i, group in enumerate(option_groups):
                current.append([
                    row for row in group
                    if not (row.get("built") == "whole-chord file" and id(row.get("cand")) in dead[i])
                ])
            missing_syms = [seq[i][0] for i, group in enumerate(current) if not group]
            if missing_syms:
                chosen = None
                break
            chosen = assign_strikes(current, seed, hint, _mentioned(words), cap)
            if not chosen:
                missing_syms = [sym for sym, _bass in seq]
                break
            failed = False
            for i, opt in enumerate(chosen):
                if opt.get("built") != "whole-chord file":
                    continue
                if opt.get("accepted"):
                    continue
                got = accept(opt["cand"], seq[i][0], seq[i][1])
                if not got:
                    dead[i].add(id(opt["cand"]))
                    failed = True
                    break
                opt["accepted"] = got
            if not failed:
                break
        else:
            chosen = None
        if not chosen:
            _EXCLUDE = previous_exclude
            raise ValueError(
                "No exact chord recording or native-pitch notes for: "
                + ", ".join(dict.fromkeys(missing_syms or [sym for sym, _bass in seq]))
                + ". A pitched clip in another key was not used."
            )
        for i, opt in enumerate(chosen):
            sym, bass = seq[i]
            if opt.get("built") == "assembled notes":
                add_assembled(i, sym, bass, opt)
            else:
                add_whole(i, sym, bass, opt["accepted"])
    bed = None
    textures = []
    for cand in pool:
        if cand.get("_blocked") or cand.get("_wrong_chord") or cand.get("chord_symbol"):
            continue
        if cand.get("origin") in ("musicmatter", "uiowa") or cand.get("pitched"):
            continue
        kind = (cand.get("source") or {}).get("kind")
        if _texture_ok(cand) or (verify_live and kind == "freesound"):
            textures.append(cand)
    random.Random(seed + 29).shuffle(textures)
    for cand in textures:
        got = accept(cand, None, None)
        if not got:
            continue
        got["instrument"] = _instrument_label(got.get("text") or got.get("title") or "", None)
        if _declared_chord(got.get("title") or "", syms):
            continue
        if not _texture_ok(got):
            continue
        bed = got
        break
    if assembled:
        note_layers.append({
            "id": "notes",
            "type": "chord_notes",
            "bus": "keys",
            "bank": assembled[0]["bank"],
            "style": "strike",
            "jitter_s": 0.06,
            "offset_s": 0.05,
            "gain": 0.72,
            "first_gain": 0.85,
            "note_s": 6.5,
            "fade_out_s": 2.4,
            "last_note_s": 9.0,
            "last_fade_out_s": 4.0,
            "role": "assembled native-pitch strikes sharing one layer; each chord uses its own samples",
            "per_chord": [
                {"chord": row["chord"], "strikes_at": [0.0], "bank": row["bank"]}
                for row in assembled
            ],
        })
    layers = []
    if events:
        layers.append({
            "id": "strikes",
            "type": "clips",
            "bus": "keys",
            "role": "whole-chord strikes matched to the composition, trimmed, not time-stretched",
            "events": events,
        })
    layers.extend(note_layers)
    bed_note = "Texture: none. No unpitched field recording, room tone, or pad passed the rights check."
    if bed:
        sid = sid_for(bed, "bed")
        bed_events = []
        t = 0.0
        while t < length - 1.0:
            bed_events.append({
                "t": round(t, 3),
                "source": sid,
                "offset_s": 0.0,
                "dur_s": 10.0,
                "fade_in_s": 1.2,
                "fade_out_s": 1.2,
                "gain": 0.22,
                "flatten": False,
                "role": f"texture ({bed['origin']}) {bed['title'][:80]}",
            })
            t += 8.0
        layers.append({
            "id": "bed",
            "type": "clips",
            "bus": "pad",
            "role": f"texture {bed['instrument']} ({bed['origin']}), unaltered clips layered and crossfaded",
            "events": bed_events,
        })
        bed_note = f"Texture: {bed['title']} ({bed['origin']}, {bed['license']})."
    mix_counts: Counter = Counter(row["origin"] for row in per_chord)
    instruments = []
    for row in per_chord:
        if row["instrument"] not in instruments:
            instruments.append(row["instrument"])
    if bed and bed["instrument"] not in instruments:
        instruments.append(bed["instrument"])
    total = sum(mix_counts.values()) or 1
    mix = {
        "by_origin": dict(mix_counts),
        "share": {k: round(v / total, 2) for k, v in mix_counts.items()},
        "strike_share": {k: round(v / total, 2) for k, v in mix_counts.items()},
        "instruments": instruments,
        "queried": order,
        "returned": {k: len(v) for k, v in groups.items()},
        "terms": query,
        "instrument_hint": hint,
        "per_chord": per_chord,
        "texture": None if not bed else {
            "origin": bed["origin"],
            "title": bed["title"],
            "author": bed["author"],
            "license": bed["license"],
            "page": bed["page"],
        },
        "libraries": library_meta.get("skipped") or [],
        "rule": (
            "A strike is that chord: a whole-chord file of the same root, quality, and bass, "
            "or the chord assembled from single notes recorded at those pitches. "
            "No origin is more than half the strikes unless the notes name it, or it is the only origin that can play them. "
            "Share counts strikes only; the texture bed is separate. "
            "Nothing is pitch-shifted or time-stretched. Every source has a title, author, license, and page."
        ),
    }
    chord_note = "; ".join(
        f"{row['symbol']} {row.get('built', row['match'])} via {row['instrument']} ({row['origin']})"
        for row in per_chord
    )
    share_note = ", ".join(f"{name} {count}/{total}" for name, count in mix_counts.items())
    note = (
        "Per chord: " + chord_note
        + ". Strike share: " + (share_note or "none")
        + ". " + bed_note
        + " No pitch-shift and no time-stretch. Assembled tones keep their recorded pitch, share one layer, and sound together with a small onset jitter."
    )
    title = (comp.get("title") or "Untitled")
    soft = bool(re.search(r"ambient|soft|calm|slow|gentle|dark|no harsh", words, re.I))
    _EXCLUDE = previous_exclude
    plan = {
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
            "note": "Each chord strike is that chord, either a whole-chord file or notes assembled at their recorded pitch. No origin is more than half the strikes unless the notes name it. Texture is an unpitched field recording, room tone, or pad.",
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
        "banks": banks,
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
    fit_plan(plan)
    return plan
