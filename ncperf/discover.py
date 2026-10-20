"""Find sounds for a Performance without preferring one supplier.

Registered Suppliers (today, musicmatter.art) are one origin among the open
commons: Freesound, Openverse, the Internet Archive, Wikimedia Commons,
ccMixter, and the bundled UIowa MIS library. Each origin is queried with the
same words. A failure in one origin is skipped. An origin that does not answer
within 20 seconds is skipped too. The origins are asked together, so the
wait is the slowest one of them, not the sum. A room-tone search runs only
when the words ask for air or space. Each of those origins also has 20
seconds, and a stall in one does not skip the rest.

A chord strike has to be that chord. It is either a whole-chord file of that
root and quality, or the chord built from single notes that were recorded at
those pitches (one sample per tone). Notes are not pitch-shifted or
time-stretched. One take keeps one ensemble and reuses those sources across
chords. The next take can choose a different ensemble. A take is not refused
because only one or two origins answered. Texture is unpitched: a field
recording, room tone, or pad, never a song, and only when the words ask for
air or space. Every source kept in the plan has a title, an author, a
license, and a page.
"""
from __future__ import annotations

import contextvars
import hashlib
import html as html_lib
import json
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.request
import zlib
from collections import Counter

from . import cancel as job_cancel
from . import chords as CH
from . import creditcache
from . import deadline
from . import fetch
from . import interpret
from . import licenses as LIC
from . import notemap
from .parallel import run_all, submit
from . import pitch
from . import progress
from . import take
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
_MM_LOCK = threading.Lock()
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
    return deadline.read_url(url, timeout, {"User-Agent": UA})


def _json(url: str):
    return json.loads(_get(url).decode("utf-8", "replace"))


def _json_auth(url: str, token: str, timeout: int = 20) -> dict:
    """GET JSON with a bearer-style token header. The token is not put in the URL."""
    raw = deadline.read_url(url, timeout, {"User-Agent": UA, "Authorization": f"Token {token}"})
    return json.loads(raw.decode("utf-8", "replace"))


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
    """Search words from the prompt. An instrument hint is not one of them.

    The hint is not the ensemble and it does not steer the suppliers. When the
    prompt names nothing, the seed rotates a palette that is not piano-first.
    """
    del instrument
    words = f"{comp.get('composition') or ''} {comp.get('performance') or ''}".lower()
    skip = excluded_terms(words)
    found = [w for w in LEXICON if w not in skip and re.search(rf"\b{re.escape(w)}\b", words)]
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
    """How well a clip matches the prompt. The instrument hint adds nothing."""
    del hint
    text = blob.lower()
    score = 0
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
    """Name the instrument the text actually says. A hint does not rename it.

    Piano is not preferred when the text also names another instrument.
    """
    del hint
    text = blob.lower()
    hits = [
        word for word in LEXICON
        if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text)
    ]
    if not hits:
        hits = [word for word in LEXICON if _mentions(text, word)]
    if not hits:
        return "texture"
    specific = [word for word in hits if word not in ("piano", "rhodes")]
    return specific[0] if specific else hits[0]


def _index_disk(url: str) -> str:
    folder = os.path.join(fetch.cache_dir(), "indexes")
    os.makedirs(folder, exist_ok=True)
    name = hashlib.sha1(url.encode()).hexdigest()[:20] + ".json"
    return os.path.join(folder, name)


def _read_index_disk(url: str):
    path = _index_disk(url)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, list) else None


def _write_index_disk(url: str, offerings: list) -> None:
    path = _index_disk(url)
    tmp = f"{path}.part.{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(offerings, fh)
        os.replace(tmp, path)
    except OSError:
        pass
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _mm_index(registry_url: str | None, host_ok_fn=None) -> list[dict]:
    url = registry_url or "https://netcomposer.art/suppliers.json"
    with _MM_LOCK:
        cached = _MM_CACHE.get(url)
    if cached is not None:
        return cached
    disk = _read_index_disk(url)
    if disk is not None:
        with _MM_LOCK:
            return _MM_CACHE.setdefault(url, disk)
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
    except deadline.SearchTimeout:
        raise
    except Exception as exc:
        print(json.dumps({"event": "discover_origin", "origin": "musicmatter", "error": str(exc)[:200]}), flush=True)
        return []
    with _MM_LOCK:
        stored = _MM_CACHE.setdefault(url, offerings)
    if stored is offerings:
        _write_index_disk(url, offerings)
    return stored


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
    # Catalog filenames write F# as Fsharp. The chord parser reads the sharp sign.
    name = name.replace("sharp", "#").replace("flat", "b")
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
    # The original full-chord set has no instrument name. Those files are piano.
    # A named choir, synth, or other instrument is labeled above and is not folded in here.
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


def _manifest_credit(url: str, supplier: str = "musicmatter") -> dict | None:
    if not url:
        return None
    from . import registry
    action, entry = registry.consult(supplier, url)
    if action == registry.USE:
        return registry.credit_of(entry)
    if action in (registry.SKIP, registry.DEFER):
        return None
    cached = creditcache.get("manifest:" + url)
    if cached:
        registry.remember_ok(supplier, url, cached)
        return cached
    if registry.time_left() is not None and registry.time_left() < registry.MIN_CHECK_S:
        registry.enqueue(supplier, url, "new")
        return None
    credit, _reason = _manifest_credit_fetch(url)
    if credit:
        creditcache.put("manifest:" + url, credit)
        registry.remember_ok(supplier, url, credit)
        return credit
    registry.remember_rejected(supplier, url, _reason or "unreachable", page=url)
    return None


def _manifest_credit_fetch(url: str) -> tuple[dict | None, str]:
    try:
        manifest = _json(url)
    except Exception as exc:
        print(json.dumps({"event": "discover_manifest", "error": str(exc)[:160]}), flush=True)
        return None, "unreachable"
    lid = manifest.get("license")
    if isinstance(lid, dict):
        lid = lid.get("id") or lid.get("name")
    parsed = LIC.from_id(str(lid or ""))
    if not lid or not LIC.allowed(parsed[0]):
        return None, "bad_license"
    creator = manifest.get("creator") or {}
    if isinstance(creator, str):
        author = creator
    else:
        author = creator.get("name") or ""
    title = manifest.get("title") or ""
    if not str(title).strip() or not str(author).strip():
        return None, "missing_credit"
    chord = manifest.get("chord")
    symbol = None
    bass_pc = None
    if isinstance(chord, dict):
        symbol = CH.spell(str(chord.get("symbol") or ""))
        if chord.get("bass_midi") is not None:
            bass_pc = int(chord["bass_midi"]) % 12
    elif isinstance(chord, str):
        symbol = CH.spell(chord)
    credit = {
        "title": str(title).strip(),
        "author": str(author).strip(),
        "license": parsed[0],
        "page": url,
        "chord_symbol": symbol,
        "bass_pc": bass_pc,
    }
    if manifest.get("audio"):
        credit["audio_url"] = manifest["audio"]
    if manifest.get("sha256"):
        credit["sha256"] = manifest["sha256"]
    return credit, ""


def _freesound_credit(sid: str) -> dict | None:
    if not sid:
        return None
    page = f"https://freesound.org/s/{sid}/"
    from . import registry
    action, entry = registry.consult("freesound", page)
    if action == registry.USE:
        return registry.credit_of(entry)
    if action in (registry.SKIP, registry.DEFER):
        return None
    cached = creditcache.get("freesound:" + str(sid))
    if cached:
        registry.remember_ok("freesound", page, cached)
        return cached
    if registry.time_left() is not None and registry.time_left() < registry.MIN_CHECK_S:
        registry.enqueue("freesound", page, "new")
        return None
    credit, reason = _freesound_credit_fetch(sid)
    if credit:
        creditcache.put("freesound:" + str(sid), credit)
        registry.remember_ok("freesound", page, credit)
        return credit
    registry.remember_rejected("freesound", page, reason or "unreachable", page=page)
    return None


def _freesound_credit_fetch(sid: str) -> tuple[dict | None, str]:
    try:
        raw = _get(f"https://freesound.org/s/{sid}/")
    except Exception as exc:
        print(json.dumps({"event": "discover_license", "origin": "freesound", "error": str(exc)[:160]}), flush=True)
        return None, "unreachable"
    page = raw.decode("utf-8", "replace")
    found = LIC.page_licenses(page)
    if len(found) != 1 or not LIC.allowed(found[0]):
        return None, "bad_license"
    title = re.search(r'og:audio:title" content="([^"]*)"', page)
    artist = re.search(r'og:audio:artist" content="([^"]*)"', page)
    og_type = re.search(r'og:type" content="([^"]*)"', page)
    if not title or not artist:
        return None, "missing_credit"
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
        return None, "missing_credit"
    lq = re.search(r'https://cdn\.freesound\.org/previews/\d+/[\w-]+-lq\.ogg', page)
    credit = {
        "title": parts[0].strip(),
        "author": parts[1].strip(),
        "license": found[0],
        "page": f"https://freesound.org/s/{sid}/",
        "text": text,
        "og_type": (og_type.group(1).strip().lower() if og_type else ""),
    }
    if lq:
        credit["audio_url"] = lq.group(0).replace("-lq.ogg", "-hq.ogg")
    return credit, ""


def _ensure_credit(cand: dict, verify_live: bool, bass=None) -> dict | None:
    """Copy title, author, license, and page onto the source. Drop the clip when any of them is missing."""
    cand = dict(cand)
    src = dict(cand.get("source") or {})
    kind = src.get("kind")
    if verify_live and kind == "supplier":
        credit = _manifest_credit(src.get("manifest") or "", supplier=str(cand.get("origin") or "musicmatter"))
        if not credit:
            return None
        if cand.get("chord_symbol") and credit.get("chord_symbol") and credit["chord_symbol"] != cand["chord_symbol"]:
            return None
        if credit.get("chord_symbol"):
            cand["chord_symbol"] = credit["chord_symbol"]
        if credit.get("bass_pc") is not None:
            cand["bass_pc"] = credit["bass_pc"]
        src.update({k: credit[k] for k in _CREDIT_KEYS})
        if credit.get("audio_url"):
            src["audio_url"] = credit["audio_url"]
    elif verify_live and kind == "freesound":
        credit = _freesound_credit(str(src.get("id") or ""))
        if not credit:
            return None
        src.update({k: credit[k] for k in _CREDIT_KEYS})
        if credit.get("audio_url"):
            src["audio_url"] = credit["audio_url"]
        cand["text"] = credit.get("text") or ""
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
    if cand.get("voicing_kind") == "alt":
        return "alt voicing"
    return "exact"


def _tier(cand: dict) -> int:
    """An inversion ranks after the root voicing. Piano does not rank after the other families."""
    if cand.get("voicing_kind") == "alt":
        return 1
    return 0


def _strike_order(cands: list[dict], hint: str | None, seed: int, index: int, named: str | None) -> list[dict]:
    pool = [c for c in cands if c.get("origin") != "uiowa" and _rights_ok(_blob(c))]
    for word in _EXCLUDE:
        pool = [c for c in pool if not _hint_ok(c.get("instrument") or "", word)]
    if named:
        named_pool = [c for c in pool if c.get("origin") == named]
        if named_pool:
            pool = named_pool
    # The instrument hint does not narrow this pool. Piano is one family among the rest.
    del hint
    ordered: list[dict] = []
    for tier_n in range(4):
        tier = [c for c in pool if _tier(c) == tier_n]
        def rotate(names: list[str]) -> list[str]:
            if not names:
                return []
            start = (seed + index) % len(names)
            return names[start:] + names[:start]

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
        # One-shot keeps two files. Gather also keeps a root and each inversion, so a
        # written bass (Dm7/C) is in the pool when the note files did not finish.
        # Main-catalog files come first. A filename like G7sharp9 still spells as G,
        # and that must not take the inversion slot from the plain G voicing.
        if _plan_until.get() is None:
            kept = group[:2]
        else:
            group = sorted(group, key=lambda o: (
                0 if ("00a_main" in (o.get("manifest") or "") or "00b_main" in (o.get("manifest") or "")) else 1,
                0 if o.get("default_voicing") else 1,
                rng.random(),
            ))
            kept = []
            seen_inv = set()
            for off in group:
                path = f"{off.get('audio') or ''} {off.get('manifest') or ''}".lower()
                if "inv3" in path:
                    inv = "inv3"
                elif "inv2" in path:
                    inv = "inv2"
                elif "inv1" in path:
                    inv = "inv1"
                elif "root" in path:
                    inv = "root"
                else:
                    inv = "other"
                if inv in seen_inv:
                    continue
                seen_inv.add(inv)
                kept.append(off)
                if len(kept) >= 4:
                    break
            if not kept:
                kept = group[:2]
        for off in kept:
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
            if off.get("audio"):
                src["url"] = off["audio"]
                src["audio_url"] = off["audio"]
            cand = _cand("musicmatter", title, author, "CC-BY-SA-4.0", f"{title} {blob}", src, terms, hint, chords)
            if not cand:
                continue
            cand["chord_symbol"] = sym
            cand["voicing_kind"] = kind
            cand["pitched"] = True
            cand["instrument"] = inst
            out.append(cand)
    return out


def _freesound_from_api(doc: dict, terms, hint, chords, seed, host_ok_fn) -> list[dict]:
    rows = list(doc.get("results") or [])
    rng = random.Random(seed + 5)
    rng.shuffle(rows)
    out = []
    for row in rows[:8]:
        sid = str(row.get("id") or "").strip()
        if not sid:
            continue
        name = str(row.get("name") or f"freesound {sid}").strip()
        author = str(row.get("username") or "Freesound").strip() or "Freesound"
        page = str(row.get("url") or f"https://freesound.org/s/{sid}/")
        src = {
            "kind": "freesound",
            "id": sid,
            "pitched": False,
            "title": name,
            "author": author,
            "license": "CC0-1.0",
            "page": page,
            "_host_ok": host_ok_fn,
        }
        cand = _cand("freesound", name, author, "CC0-1.0", f"{name} {author}", src, terms, hint, chords)
        if cand:
            out.append(cand)
    return out


def search_freesound(terms, hint, chords, seed, host_ok_fn=None, registry_url=None) -> list[dict]:
    """Official API when FREESOUND_API_KEY is set. The HTML search is the fallback."""
    key = os.environ.get("FREESOUND_API_KEY", "").strip()
    q = urllib.parse.quote(" ".join(terms))
    if key:
        url = (
            "https://freesound.org/apiv2/search/text/"
            f"?query={q}&filter=license:%22Creative%20Commons%200%22"
            "&fields=id,name,username,license,previews,duration,url&page_size=8"
        )
        try:
            doc = _json_auth(url, key)
            hits = _freesound_from_api(doc, terms, hint, chords, seed, host_ok_fn)
            if hits:
                return hits
        except deadline.SearchTimeout:
            raise
        except Exception as exc:
            print(json.dumps({
                "event": "freesound_api",
                "error": str(exc)[:160],
                "fallback": "html",
            }), flush=True)
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
    except deadline.SearchTimeout:
        raise
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
    # A timbre hint does not take the first slot.
    del hint
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


_plan_until: contextvars.ContextVar[float | None] = contextvars.ContextVar("nc_plan_until", default=None)


def arm_wall(seconds: float):
    """Cap later file-reach checks to this many seconds from now. One-shot Perform does not arm it."""
    return _plan_until.set(time.monotonic() + max(0.0, float(seconds)))


def reset_wall(token) -> None:
    _plan_until.reset(token)


def _audio_reachable(cand: dict) -> bool:
    """Drop a file the renderer would not be allowed to download."""
    src = cand.get("source") or {}
    url = src.get("url") or ""
    if not url or src.get("kind") in ("freesound", "supplier"):
        return True
    budget = 25.0
    until = _plan_until.get()
    if until is not None:
        budget = min(budget, until - time.monotonic())
        if budget <= 0.05:
            return False
    try:
        with deadline.limit(budget):
            wait = deadline.socket_timeout(25)
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-2047"})
            with urllib.request.urlopen(req, timeout=wait) as r:
                if r.status >= 400:
                    return False
                if not deadline.read_response(r):
                    return False
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
    """One ensemble for these chords. ``cap`` is ignored.

    A take is not refused because one origin played more than half the chords.
    """
    del cap
    chosen, _primary = take.choose(groups, seed, mentioned, hint, len(groups) >= 12)
    if any(row is None for row in chosen):
        return None
    return chosen


def _start_prefetch(src: dict):
    """Download a chosen bed while planning continues. The renderer then finds it cached."""
    ctx = contextvars.copy_context()
    box: dict = {}

    def run() -> None:
        try:
            ctx.run(fetch.resolve, dict(src))
        except Exception as exc:
            box["error"] = exc
            print(json.dumps({"event": "prefetch", "error": str(exc)[:160]}), flush=True)
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=run, name="nc-prefetch", daemon=True)
    thread.start()
    return thread, box


def _drop_avoided_notes(maps, blocked: set[str]):
    """Copy note maps without samples whose id or url was kept by an earlier Gather."""
    if not maps or not blocked:
        return maps
    out = []
    for mp in maps:
        copy = dict(mp)
        notes = {}
        for midi, sample in (mp.get("notes") or {}).items():
            ident = str(sample.get("id") or "")
            url = str(sample.get("url") or "")
            if ident in blocked or url in blocked:
                continue
            notes[midi] = sample
        copy["notes"] = notes
        out.append(copy)
    return out


def _foreign_bass(sym: str, bass) -> bool:
    """The slash bass is not a tone of this chord. Gather may play the chord anyway.

    One-shot Perform does not arm the plan wall, so a mismatched bass still drops the file.
    """
    if bass is None or _plan_until.get() is None:
        return False
    tones = {int(n) % 12 for _name, notes in CH.voicings(sym) for n in notes}
    if not tones:
        return False
    return int(bass) % 12 not in tones


def plan_for(comp: dict, seed: int = 1, instrument: str | None = None, host_ok_fn=None, searchers=None, registry_url=None, verify_live: bool = True, note_maps=None, progress_cb=None, origin_budget_s: float = 20, texture_budget_s: float = 20, avoid_keys=None, avoid_sample_ids=None, note_budget_s: float | None = None) -> dict:
    """Build a plan from an even-handed search. Raises ValueError if nothing usable came back."""
    comp_text = comp.get("composition") or ""
    found = take.composition_chords(comp_text) or CH.find_full(
        f"{comp_text} {comp.get('performance') or ''}"
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
    heard = set()

    def tell(line: str | None = None, sound: dict | None = None) -> None:
        if not progress_cb:
            return
        if sound:
            row = progress.sound_row(sound.get("title"), sound.get("supplier"), sound.get("license"))
            if not row:
                sound = None
            else:
                key = (row["title"], row["supplier"], row["license"])
                if key in heard:
                    sound = None
                else:
                    heard.add(key)
                    sound = row
        if line or sound:
            progress_cb(line, sound)

    def note_hit(hit: dict | None, origin: str) -> None:
        if not hit:
            return
        tell(None, {
            "title": hit.get("title"),
            "supplier": progress.origin_label(hit.get("origin") or origin),
            "license": hit.get("license"),
        })

    def query_origin(fn, terms, hint_arg, origin_seed, budget_s):
        try:
            with deadline.limit(budget_s):
                hits = fn(terms, hint_arg, syms, origin_seed, host_ok_fn, registry_url) or []
        except deadline.SearchTimeout:
            return [], "timed out"
        except Exception as exc:
            return [], str(exc)[:180]
        return [h for h in hits if h], None

    # Note files do not depend on which supplier answered. Start them before the
    # searches so a cold library overlaps the supplier replies.
    note_pool = None
    note_future = None
    if note_maps is None and verify_live:
        # Copy the deadline into the note workers, then clear it on this thread
        # so a later one-shot Perform in the same process is not capped.
        budget_token = notemap.arm_budget(note_budget_s) if note_budget_s is not None else None
        try:
            note_pool, note_future = submit(notemap.ensure, syms, False, hint)
        finally:
            if budget_token is not None:
                notemap.reset_budget(budget_token)

    groups: dict[str, list] = {}
    errors = {}
    jobs = []
    texture_terms = ["field recording", "room tone", "ambience"]
    wants_bed = bool(take.phrase(words).get("bed"))
    try:
        for origin in order:
            fn = fns.get(origin)
            if not fn:
                continue
            tell(progress.finding_line(origin, query))
            jobs.append((
                "chord",
                origin,
                lambda fn=fn: query_origin(fn, query, hint, seed, origin_budget_s),
            ))
        # The bed is optional. Skip the room-tone pass unless the words asked for air or space.
        # Each origin keeps its own budget. One stall does not skip the others.
        if wants_bed:
            for origin in order:
                if origin in ("musicmatter", "uiowa"):
                    continue
                fn = fns.get(origin)
                if not fn:
                    continue
                tell(progress.finding_line(origin, texture_terms, texture=True))
                jobs.append((
                    "texture",
                    origin,
                    lambda fn=fn: query_origin(fn, texture_terms, None, seed + 17, texture_budget_s),
                ))
        results = run_all([job for _kind, _origin, job in jobs], workers=max(1, len(jobs)))
    except BaseException:
        if note_pool is not None:
            note_pool.shutdown(wait=True)
        raise
    for (kind, origin, _job), (hits, err) in zip(jobs, results):
        if kind != "chord":
            continue
        if err:
            errors[origin] = err
        hits.sort(key=lambda c: (-c["fit"], c["title"]))
        for hit in hits:
            groups.setdefault(hit.get("origin") or origin, []).append(hit)
            note_hit(hit, origin)
    seen = {hit["key"] for hits in groups.values() for hit in hits if hit.get("key")}
    for (kind, origin, _job), (hits, err) in zip(jobs, results):
        if kind != "texture":
            continue
        if err:
            errors[origin + ":texture"] = err
        for hit in hits:
            if not hit or hit.get("key") in seen:
                continue
            seen.add(hit["key"])
            groups.setdefault(hit.get("origin") or origin, []).append(hit)
            note_hit(hit, origin)
    for origin in ORIGINS:
        groups.setdefault(origin, [])
    blocked_keys = {str(k) for k in (avoid_keys or []) if k}
    if blocked_keys:
        for origin in list(groups):
            groups[origin] = [h for h in groups[origin] if str(h.get("key") or "") not in blocked_keys]
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
        tell(None, {
            "title": got.get("title"),
            "supplier": progress.origin_label(got.get("origin")),
            "license": got.get("license"),
        })
        return got

    bed = None
    prefetch_thread = None
    prefetch_box: dict = {}
    # The bed file is chosen from the search hits, before the note maps are
    # joined, so its download overlaps the rest of planning.
    if wants_bed:
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
        tried_late = False
        for cand in textures:
            until = _plan_until.get()
            late = until is not None and time.monotonic() >= until - 0.05
            if late and tried_late:
                break
            if late:
                # The supplier clock is spent. One live check, then stop. No 25 second hang.
                tried_late = True
                try:
                    with deadline.limit(1.5):
                        got = accept(cand, None, None)
                except deadline.SearchTimeout:
                    got = None
            else:
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
        if bed and verify_live and (bed.get("source") or {}).get("kind"):
            prefetch_thread, prefetch_box = _start_prefetch(bed["source"])

    sources = {}
    events = []
    banks: dict = {}
    note_layers = []
    assembled: list[dict] = []
    used: dict[str, str] = {}
    sample_ids: dict[str, str] = {}
    kept_picks: list[str] = []

    def sid_for(cand: dict, suffix: str) -> str:
        if cand["key"] in used:
            return used[cand["key"]]
        sid = f"{cand['origin'][:2]}-{suffix}"
        used[cand["key"]] = sid
        sources[sid] = cand["source"]
        if cand.get("key"):
            kept_picks.append(str(cand["key"]))
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
        for field in ("kind", "url", "id", "audio_url", "page", "title", "author", "license", "pitched", "expect_midi", "segment", "onset_s", "length_s", "credit_checked"):
            if sample.get(field) not in (None, ""):
                keep[field] = sample[field]
        if sample.get("cents") is not None:
            keep["cents"] = sample["cents"]
            keep["pitch_checked"] = True
        sources[sid] = keep
        if sample.get("id"):
            kept_picks.append(str(sample["id"]))
        if sample.get("url"):
            kept_picks.append(str(sample["url"]))
        tell(None, {
            "title": keep.get("title"),
            "supplier": progress.origin_label(keep.get("kind")),
            "license": keep.get("license"),
        })
        return sid

    tell(progress.NOTE_LINE)
    if note_future is not None:
        try:
            note_maps, library_meta = note_future.result()
        except Exception as exc:
            note_maps, library_meta = [], {"skipped": [], "error": str(exc)[:180]}
        finally:
            if note_pool is not None:
                note_pool.shutdown(wait=True)
    else:
        library_meta = {}
        if note_maps is None:
            note_maps = []
    note_maps = _drop_avoided_notes(note_maps or [], {str(s) for s in (avoid_sample_ids or []) if s})
    assembly = notemap.assemblies_for(syms, note_maps or [], _EXCLUDE)

    # A written bar or beat length is the clock. Otherwise a "second half" is a time boundary,
    # and a bar table keeps its own order. Prose chords are not added on top of a table.
    written = take.written_durations(comp_text, found)
    halves = None if written or take.bar_chords(comp_text) else take.half_sections(comp_text)
    section_spec: list[tuple[int, float]] = []
    section_harmonies: list[str] = []
    slice_bits: list[str] = []
    written_clock = False
    if written:
        seq = list(found)
        if len(seq) > take.CHORD_LIMIT:
            slice_bits.append(
                f"The piece had {len(seq)} chords; it was shortened to {take.CHORD_LIMIT} "
                "so the Performance stays within the chord limit."
            )
            seq = seq[: take.CHORD_LIMIT]
            written = written[: take.CHORD_LIMIT]
        length = float(sum(written))
        section_spec = [(len(seq), length)]
        section_harmonies = [" ".join(sym for sym, _bass in seq)]
        written_clock = True
        pulse = True
        broaden = 0
        slots = take.slots_from_durations(written)
    elif halves:
        left, right = halves
        half_len = float(length) / 2.0
        seq = []
        for group, seconds in ((left, half_len), (right, half_len)):
            part, part_note = take.expand_sequence(group, seconds)
            seq.extend(part)
            section_spec.append((len(part), seconds))
            section_harmonies.append(" ".join(sym for sym, _bass in group))
            if part_note:
                slice_bits.append(part_note)
        pulse = take.stated_pulse(words)
        broaden = take.broaden_bars(words) if pulse else 0
        slots = take.chord_slots(section_spec, int(seed), pulse, broaden)
    else:
        seq, one_note = take.expand_sequence(found, length)
        section_spec = [(len(seq), float(length))]
        section_harmonies = [" ".join(syms)]
        if one_note:
            slice_bits.append(one_note)
        pulse = take.stated_pulse(words)
        broaden = take.broaden_bars(words) if pulse else 0
        slots = take.chord_slots(section_spec, int(seed), pulse, broaden)
    slice_note = " ".join(slice_bits) or None
    if not seq:
        raise ValueError('no chord symbols found in the Composition (e.g. "Cm, Am, F")')
    kn = take.phrase(words)
    if not written_clock and len(slots) != len(seq):
        slots = take.chord_slots([(len(seq), float(length))], int(seed), pulse, broaden)
        section_spec = [(len(seq), float(length))]
    mean_dur = float(length) / len(seq)
    opening = take.ring(slots[0]["dur"], kn["gap"])
    section_of: list[str] = []
    for idx, (count, _seconds) in enumerate(section_spec):
        label = "A" if idx == 0 else ("B" if idx == 1 else chr(ord("A") + idx))
        section_of.extend([label] * count)
    breath_at = None
    send_lu, decay_s = take.reverb_for(kn)
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
            opts.append({
                "built": "assembled notes",
                "origin": row.get("origin"),
                "instrument": row.get("instrument"),
                "pluck": bool(row.get("pluck")) or _pluckish(row.get("instrument") or ""),
                "tier": _tier({"instrument": row.get("instrument"), "voicing_kind": "exact"}),
                "voicing": row.get("voicing"),
                "samples": row.get("samples"),
                "title": row.get("title"),
                "author": row.get("author"),
                "license": row.get("license"),
                "licenses": row.get("licenses"),
                "page": row.get("page"),
            })
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

    def _accept_rows(rows: list, symbols: list, dead: list[set]) -> bool:
        """Return False when a whole-chord file failed the rights check and should be retried."""
        for i, opt in enumerate(rows):
            if not opt or opt.get("built") != "whole-chord file":
                continue
            if opt.get("accepted"):
                continue
            sym, bass = symbols[i]
            got = accept(opt["cand"], sym, bass)
            if not got and _foreign_bass(sym, bass):
                got = accept(opt["cand"], sym, None)
            if not got:
                dead[i].add(id(opt["cand"]))
                return False
            opt["accepted"] = got
        return True

    def _pick(groups: list[list[dict]], symbols: list, prefer: list[str], use_hint: str | None, salt: int, avoid=None):
        dead = [set() for _ in groups]
        missing_syms: list[str] = []
        chosen = None
        primary = None
        for _attempt in range(24):
            current = []
            for i, group in enumerate(groups):
                current.append([
                    row for row in group
                    if not (row.get("built") == "whole-chord file" and id(row.get("cand")) in dead[i])
                ])
            missing_syms = [symbols[i][0] for i, group in enumerate(current) if not group]
            if missing_syms and not any(current):
                chosen = None
                break
            chosen, primary = take.choose(current, seed + salt, prefer, use_hint, len(current) >= 12, avoid)
            if any(row is None for row in chosen):
                missing_syms = [symbols[i][0] for i, row in enumerate(chosen) if row is None]
                chosen = None
                break
            if _accept_rows(chosen, symbols, dead):
                return chosen, primary, []
        return None, primary, missing_syms

    mentioned = _mentioned(words)
    lead_words = mentioned
    partner_words: list[str] | None = None
    section_cut = None
    if kn["layer"]:
        lead_words = take.prefer_from(kn["layer"][0]) or mentioned
        partner_words = take.prefer_from(kn["layer"][1]) or []
    elif kn["then_at"] is not None:
        section_cut = max(1, len(seq) // 2)
        if section_cut >= len(seq):
            section_cut = None

    mode = take.arrangement_mode(words, kn)
    # A ring or legato line has no deliberate silence. A breath there is a hole.
    if mode == "free" and not pulse and not kn["gap"] and not kn["ring"] and len(seq) >= 4:
        breath_at = 1 + (int(seed) % (len(seq) - 2))
    long_piece = len(seq) >= 12
    required = [lead_words, partner_words] if partner_words else None

    def _fail(missing):
        global _EXCLUDE
        _EXCLUDE = previous_exclude
        raise ValueError(
            "No exact chord recording or native-pitch notes for: "
            + ", ".join(dict.fromkeys(missing or [sym for sym, _bass in seq]))
            + ". A pitched clip in another key was not used."
        )

    def _pack_chosen(chosen: list) -> dict:
        anchor_opt = next(opt for opt in chosen if opt)
        return {
            "key": (anchor_opt.get("origin"), anchor_opt.get("instrument"), anchor_opt.get("built")),
            "origin": anchor_opt.get("origin"),
            "instrument": anchor_opt.get("instrument"),
            "built": anchor_opt.get("built"),
            "opts": chosen,
            "label": f"{anchor_opt.get('instrument')} ({anchor_opt.get('origin')})",
        }

    def _settle(member: dict, symbols: list) -> bool:
        """Keep the chords this voice can actually play. A failed file drops that chord only."""
        for i, opt in enumerate(list(member["opts"])):
            if not opt or opt.get("built") != "whole-chord file" or opt.get("accepted"):
                continue
            got = accept(opt["cand"], symbols[i][0], symbols[i][1])
            if not got and _foreign_bass(symbols[i][0], symbols[i][1]):
                got = accept(opt["cand"], symbols[i][0], None)
            if not got:
                member["opts"][i] = None
            else:
                opt["accepted"] = got
        return any(member["opts"])

    def _cast(groups, symbols, prefer, salt, use_hint, required_words=None):
        if required_words:
            cast = take.palette(
                groups, seed + salt, prefer, use_hint, long_piece, mode, required_words,
            )
            members = []
            for member in cast:
                if _settle(member, symbols):
                    members.append(member)
                if len(members) >= 3:
                    break
            if members:
                bare = [i for i in range(len(symbols)) if not any(m["opts"][i] for m in members)]
                if bare:
                    chosen, _primary, missing = _pick(groups, symbols, prefer, use_hint, salt)
                    if not chosen:
                        return None, missing or [symbols[i][0] for i in bare]
                    anchor = _pack_chosen(chosen)
                    if any(anchor["opts"][i] for i in bare):
                        members.append(anchor)
                    bare = [i for i in range(len(symbols)) if not any(m["opts"][i] for m in members)]
                    if bare:
                        return None, [symbols[i][0] for i in bare]
                return members, []
        chosen, _primary, missing = _pick(groups, symbols, prefer, use_hint, salt)
        if not chosen:
            return None, missing
        anchor = _pack_chosen(chosen)
        members = [anchor]
        if mode == "orderly" and not required_words:
            return members, []
        cast = take.palette(
            groups, seed + salt, prefer, use_hint, long_piece, mode, None,
        )
        for member in cast:
            if member["key"] == anchor["key"]:
                continue
            if any(opt is None for opt in member["opts"]):
                continue
            if not _settle(member, symbols):
                continue
            members.append(member)
            if len(members) >= 3:
                break
        return members, []

    coverage_notes: list[str] = []
    if section_cut:
        left_words = take.prefer_from(words[: kn["then_at"]]) or mentioned
        right_words = take.prefer_from(words[kn["then_at"] :]) or mentioned
        left, miss_l = _cast(option_groups[:section_cut], seq[:section_cut], left_words, 0, None)
        right, miss_r = _cast(option_groups[section_cut:], seq[section_cut:], right_words, 17, None)
        if not left or not right:
            _fail((miss_l or []) + (miss_r or []))
        segments = [(left, seq[:section_cut], 0), (right, seq[section_cut:], section_cut)]
    else:
        members, missing_syms = _cast(option_groups, seq, lead_words, 0, hint, required)
        if not members:
            _fail(missing_syms)
        if required:
            coverage_notes.extend(take.named_coverage(members, seq, required))
        segments = [(members, seq, 0)]

    events = []
    partner_events = []
    note_rows = []
    pads: dict[str, dict] = {}
    arrangement: list[dict] = []
    partner = []

    def ensure_bank(bank_id: str, samples: list, origin: str | None = None) -> None:
        bucket = banks.setdefault(bank_id, {"notes": {}})
        for sample in samples or []:
            sid = sid_for_sample(sample)
            midi = str(int(sample["midi"]))
            have = bucket["notes"].setdefault(midi, [])
            if sid not in have:
                have.append(sid)
                tell(None, {
                    "title": sample.get("title"),
                    "supplier": progress.origin_label(origin or sample.get("origin")),
                    "license": sample.get("license"),
                })

    def member_bank(member: dict) -> str:
        raw = f"{member['origin']}-{member['instrument']}".lower()
        return "e-" + re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:28]

    def _tones(opt: dict) -> list[dict]:
        tones = []
        for sample in opt.get("samples") or []:
            tones.append({
                "midi": int(sample["midi"]),
                "note": pitch.NOTE(int(sample["midi"])),
                "title": sample.get("title"),
                "author": sample.get("author"),
                "license": sample.get("license"),
                "page": sample.get("page"),
                "cents": sample.get("cents"),
            })
        return tones

    lead_pluck = False
    for members, symbols, offset in segments:
        roles = take.arrange(members, seed + offset * 17, mode, kn["twice"], mean_dur)
        for i, role in enumerate(roles):
            sym, bass = symbols[i]
            global_i = offset + i
            slot = slots[global_i]
            slot_t = float(slot["t"])
            slot_dur = float(slot["dur"])
            role["strikes_at"] = take.shape_strikes(
                role.get("strikes_at") or [0.0],
                slot_dur,
                int(seed),
                global_i,
                pulse,
                mode == "orderly",
                bool(kn["twice"]),
            )
            ring_i = take.ring(slot_dur, kn["gap"])
            if breath_at is not None and global_i == breath_at:
                # A short silence before the next chord. The chord itself is still heard.
                ring_i = dict(ring_i)
                gap_s = min(1.6, max(0.55, slot_dur * 0.08))
                ring_i["cover"] = round(max(1.3, slot_dur - gap_s), 3)
                ring_i["note_s"] = ring_i["cover"]
                ring_i["fade_out_s"] = round(min(float(ring_i["fade_out_s"]), max(0.25, gap_s)), 3)
            anchor = members[0]
            anchor_opt = anchor["opts"][i]
            if anchor_opt and anchor_opt.get("built") == "assembled notes":
                full = [int(n) for n in anchor_opt["voicing"]]
            else:
                full = _voicing(sym)
            playing = [] if role["rest"] else list(role["playing"])
            lead_k = role["lead"] if role["lead"] in playing else (playing[0] if playing else 0)
            lead = members[min(lead_k, len(members) - 1)]
            lead_opt = lead["opts"][i] if playing else anchor_opt
            if lead_opt and (lead_opt.get("pluck") or _pluckish(lead_opt.get("instrument") or "")):
                lead_pluck = True
            assembled = []
            for k in playing:
                opt = members[k]["opts"][i]
                if opt and opt.get("built") == "assembled notes":
                    assembled.append(k)
            notes_k = lead_k if lead_k in assembled else (assembled[0] if assembled else None)
            row_members = [members[k]["label"] for k in playing]
            arrangement.append({
                "chord": global_i,
                "symbol": sym,
                "members": row_members,
                "lead": None if role["rest"] else members[lead_k]["label"],
                "density": "full" if role["rest"] else role["density"],
                "register": "full" if role["rest"] else role["register"],
                "entry": role["entry"],
                "gain": role["gain"],
                "strikes_at": list(role["strikes_at"]),
                "rest": bool(role["rest"]),
                "start_s": round(slot_t, 3),
                "end_s": round(slot_t + slot_dur, 3),
                "breath": bool(breath_at is not None and global_i == breath_at),
            })
            credit = lead_opt or anchor_opt or {}
            if credit.get("built") == "whole-chord file":
                credit = credit.get("accepted") or credit
            chords.append({
                "t": round(slot_t, 3),
                "dur": round(slot_dur, 3),
                "symbol": sym,
                "voicing": full,
                "section": section_of[global_i] if global_i < len(section_of) else "A",
            })
            per_chord.append({
                "symbol": sym,
                "bass": bass,
                "origin": (lead_opt or anchor_opt or {}).get("origin") or anchor["origin"],
                "instrument": (lead_opt or anchor_opt or {}).get("instrument") or anchor["instrument"],
                "built": (lead_opt or anchor_opt or {}).get("built") or anchor["built"],
                "match": "native pitch" if (lead_opt or {}).get("built") == "assembled notes" else _match_label(credit) if credit else "exact",
                "title": (credit or {}).get("title"),
                "author": (credit or {}).get("author"),
                "license": (credit or {}).get("license"),
                "licenses": list((lead_opt or {}).get("licenses") or ([(credit or {}).get("license")] if (credit or {}).get("license") else [])),
                "page": (credit or {}).get("page") or (lead_opt or {}).get("page"),
                "voicing": full,
                "tones": _tones(lead_opt) if lead_opt and lead_opt.get("built") == "assembled notes" else [],
                "members": row_members,
                "rest": bool(role["rest"]),
            })
            if role["rest"]:
                continue
            for k in playing:
                opt = members[k]["opts"][i]
                gain = role["gain"] if k == lead_k else round(role["gain"] * 0.62, 3)
                is_partner = bool(partner_words) and take.matches_any(opt, partner_words)
                if opt.get("built") == "assembled notes":
                    bank_id = member_bank(members[k])
                    ensure_bank(bank_id, opt.get("samples") or [], opt.get("origin"))
                    if k == notes_k and not is_partner:
                        midis = take.sounding_midis(opt.get("voicing") or full, take.root_pc(sym, bass), role["density"], role["register"])
                        note_rows.append({
                            "chord": global_i,
                            "bank": bank_id,
                            "strikes_at": list(role["strikes_at"]),
                            "midi": midis,
                            "gain": gain,
                            "style": role["style"] if mode == "free" else ("roll" if kn["pluck_words"] else "strike"),
                            "note_s": ring_i["note_s"],
                            "fade_out_s": ring_i["fade_out_s"],
                            "last_note_s": ring_i["last_note_s"],
                            "sustain": bool(kn["ring"] and not kn["gap"]),
                        })
                    else:
                        spec = pads.setdefault(bank_id, {"chords": [], "instrument": opt.get("instrument"), "origin": opt.get("origin")})
                        spec["chords"].append(global_i)
                else:
                    picked = opt.get("accepted")
                    if not picked:
                        continue
                    sid = sid_for(picked, str(len(sources)))
                    match = _match_label(picked)
                    blob = take.chord_overlaps(
                        slot_t, ring_i["cover"], sid,
                        f"{picked['instrument']} ({picked['origin']}) {sym} {match}: {picked['title'][:80]}",
                        seed, global_i, 0.72 * gain, role["strikes_at"][0], mode == "orderly",
                        continuous=bool(kn["ring"] and not kn["gap"]),
                    )
                    for ev in blob:
                        ev["chord_index"] = global_i
                    if is_partner:
                        partner_events.extend(blob)
                    else:
                        events.extend(blob)

    style = "roll" if lead_pluck or kn["roll_words"] or kn["pluck_words"] else "strike"
    spread = [0.08, 0.35] if (lead_pluck or kn["pluck_words"]) else [0.05, 0.22]
    if mode == "free":
        jitter = [0.0, 0.14]
        gain_jitter = [0.42, 1.18]
    else:
        jitter = [0.0, 0.02]
        gain_jitter = [0.94, 1.02]
    surprise_note = None
    high_midi = None
    if arrangement:
        odd = max(arrangement, key=lambda row: (bool(row["rest"]), row["entry"] not in ("downbeat",), len(row["members"]) != 1, row["chord"]))
        if odd["rest"]:
            surprise_note = f"Surprise: chord {odd['chord'] + 1} ({odd['symbol']}) rests under the chord still ringing."
        elif mode == "free":
            who = ", ".join(odd["members"]) or "the ensemble"
            surprise_note = f"Surprise: chord {odd['chord'] + 1} ({odd['symbol']}) enters {odd['entry']} on {who}."
        else:
            surprise_note = f"Surprise: the ensemble stays {arrangement[0]['lead']} on every chord, as the words asked."

    layers = []
    if events:
        layers.append({
            "id": "strikes",
            "type": "clips",
            "bus": "keys",
            "role": "whole-chord strikes matched to the composition, overlapped and crossfaded, not time-stretched",
            "events": events,
        })
    if note_rows:
        layers.append({
            "id": "notes",
            "type": "chord_notes",
            "bus": "keys",
            "bank": note_rows[0]["bank"],
            "style": style,
            "spread_s": spread,
            "jitter_s": jitter[1],
            "gain_jitter": gain_jitter,
            "offset_s": 0.0,
            "gain": 0.78,
            "first_gain": 0.9,
            "restrike_gain": 0.55,
            "note_s": opening["note_s"],
            "fade_out_s": opening["fade_out_s"],
            "last_note_s": opening["last_note_s"],
            "last_fade_out_s": opening["last_fade_out_s"],
            "role": "ensemble notes at their recorded pitch; the arrangement decides which of them play",
            "per_chord": [
                {
                    "chord": row["chord"],
                    "strikes_at": row["strikes_at"],
                    "bank": row["bank"],
                    "midi": row["midi"],
                    "gain": row["gain"],
                    "style": row["style"],
                    "note_s": row["note_s"],
                    "fade_out_s": row["fade_out_s"],
                    "last_note_s": row["last_note_s"],
                }
                for row in note_rows
            ],
        })
    if partner_events:
        layers.append({
            "id": "partner",
            "type": "clips",
            "bus": "keys",
            "role": "the other named voice in this ensemble, not a new instrument from outside the cast",
            "events": partner_events,
        })
    for bank_id, spec in pads.items():
        layers.append({
            "id": "with-" + bank_id[:18],
            "type": "chord_pad",
            "bus": "keys",
            "bank": bank_id,
            "chords": spec["chords"],
            "gain_total": 0.34,
            "overlap_s": round(min(4.0, max(1.5, mean_dur * 0.45)), 3),
            "lead_s": 0.0,
            "lead_by_chord": take.pad_leads(len(chords), int(seed) + 5, pulse, mode == "orderly"),
            "fade_in_s": 0.3,
            "fade_out_s": opening["fade_out_s"],
            "role": f"{spec.get('instrument')} ({spec.get('origin')}) joins some chords, unaltered, not time-stretched",
        })

    bass_notes = {}
    bass_bank = None
    if note_rows:
        bank_counts = Counter(row["bank"] for row in note_rows)
        bass_bank = bank_counts.most_common(1)[0][0]
        have = {int(m) for m in (banks.get(bass_bank, {}).get("notes") or {})}
        for row in note_rows:
            if row["bank"] != bass_bank:
                continue
            i = row["chord"]
            midi = take.root_midi(chords[i]["symbol"], seq[i][1], have)
            if midi is None:
                continue
            notes = [midi]
            if high_midi is not None and i == surprise_i and high_midi != midi and high_midi in have and row["bank"] == bass_bank:
                notes.append(high_midi)
            bass_notes[str(i)] = notes
        covered = len(bass_notes)
        if covered < max(1, int(0.6 * len(note_rows))):
            bass_notes = {}
            bass_bank = None
        elif high_midi is not None and surprise_i not in {int(k) for k in bass_notes} :
            pass
    if bass_bank and bass_notes:
        layers.append({
            "id": "bass",
            "type": "chord_under",
            "bus": "keys",
            "bank": bass_bank,
            "gain": 0.22,
            "gain_low": 0.30,
            "overlap_s": round(min(4.0, max(1.2, opening["cover"] - slots[0]["dur"])), 3) if not kn["gap"] else 0.4,
            "lead_s": 0.0,
            "lead_by_chord": take.pad_leads(len(chords), int(seed) + 11, pulse, mode == "orderly"),
            "fade_in_s": 0.25,
            "fade_out_s": 1.1,
            "last_fade_out_s": 2.2,
            "role": "root in the lead ensemble, where that pitch was already recorded",
            "notes_by_chord": bass_notes,
        })
    elif high_midi is not None:
        row = next((row for row in note_rows if row["chord"] == surprise_i), None)
        if row and str(high_midi) in (banks.get(row["bank"], {}).get("notes") or {}):
            layers.append({
                "id": "surprise",
                "type": "chord_under",
                "bus": "keys",
                "bank": row["bank"],
                "gain": 0.24,
                "overlap_s": round(min(3.0, mean_dur * 0.5), 3),
                "lead_s": 0.0,
                "lead_by_chord": take.pad_leads(len(chords), int(seed) + 19, pulse, mode == "orderly"),
                "fade_in_s": 0.4,
                "fade_out_s": 1.4,
                "role": "one held high tone, once in the take",
                "notes_by_chord": {str(surprise_i): [high_midi]},
            })

    bed_note = "Texture: none. The words did not ask for air or space." if not kn["bed"] else "Texture: none. No unpitched field recording, room tone, or pad passed the rights check."
    if bed:
        sid = sid_for(bed, "bed")
        bed_events = []
        for ev in take.bed_events(length, seed):
            ev = dict(ev)
            ev["source"] = sid
            ev["flatten"] = False
            ev["role"] = f"texture ({bed['origin']}) {bed['title'][:80]}"
            bed_events.append(ev)
        layers.append({
            "id": "bed",
            "type": "clips",
            "bus": "pad",
            "role": f"texture {bed['instrument']} ({bed['origin']}), long crossfaded clips, not an 8 second loop",
            "events": bed_events,
        })
        bed_note = f"Texture: {bed['title']} ({bed['origin']}, {bed['license']})."

    mix_counts: Counter = Counter(row["origin"] for row in per_chord)
    instruments = []
    for row in per_chord:
        if row["instrument"] not in instruments:
            instruments.append(row["instrument"])
    if partner:
        for opt in partner:
            if opt and opt.get("instrument") and opt["instrument"] not in instruments:
                instruments.append(opt["instrument"])
                break
    if bed and bed["instrument"] not in instruments:
        instruments.append(bed["instrument"])
    total = sum(mix_counts.values()) or 1
    seen_ens = []
    for row in per_chord:
        label = f"{row['instrument']} ({row['origin']})"
        if not seen_ens or seen_ens[-1] != label:
            seen_ens.append(label)
    cast_labels = []
    for row in arrangement:
        for label in row.get("members") or []:
            if label not in cast_labels:
                cast_labels.append(label)
    if not cast_labels:
        cast_labels = seen_ens or ["the ensemble"]
    how = "the same way on every chord" if mode == "orderly" else "a different shape on each chord, seeded"
    duration_cv = take.duration_cv([float(c["dur"]) for c in chords])
    if written_clock:
        pacing = "Pacing: each chord lasts as long as the Composition wrote."
    elif pulse and broaden:
        pacing = f"Pacing: a steady pulse, with the last {broaden} bars let to broaden, as the words asked."
    elif pulse:
        pacing = "Pacing: a steady pulse, as the words asked."
    else:
        pacing = f"Pacing: chord lengths vary (duration CV {duration_cv})."
    ensemble_note = "Ensemble: " + ", ".join(cast_labels) + f". Arrangement: {how}. {pacing}"
    if len(per_chord) > 12:
        chord_note = f"{len(per_chord)} chords on {per_chord[0]['instrument']} ({per_chord[0]['origin']})"
        if len(seen_ens) > 1:
            chord_note = f"{len(per_chord)} chords. " + ensemble_note
    else:
        chord_note = "; ".join(
            f"{row['symbol']} {row.get('built', row['match'])} via {row['instrument']} ({row['origin']})"
            for row in per_chord
        )
    share_note = ", ".join(f"{name} {count}/{total}" for name, count in mix_counts.items())
    note = (
        ensemble_note + " Per chord: " + chord_note
        + ". Strike share: " + (share_note or "none")
        + ". " + bed_note
        + " No pitch-shift and no time-stretch. Chords ring across the bar line unless the words ask for space."
    )
    if coverage_notes:
        note = note + " " + " ".join(coverage_notes)
    if surprise_note:
        note = note + " " + surprise_note
    title = (comp.get("title") or "Untitled")
    soft = bool(kn["soft"])
    plan_sections = []
    cursor = 0
    for idx, (count, _seconds) in enumerate(section_spec):
        group = slots[cursor:cursor + count]
        cursor += count
        if not group:
            continue
        label = "A" if idx == 0 else ("B" if idx == 1 else chr(ord("A") + idx))
        harmony = section_harmonies[idx] if idx < len(section_harmonies) else " ".join(syms)
        plan_sections.append({
            "id": label,
            "start_s": round(float(group[0]["t"]), 3),
            "end_s": round(float(group[-1]["t"]) + float(group[-1]["dur"]), 3),
            "harmony": harmony,
        })
    _EXCLUDE = previous_exclude
    plan_notes = [note]
    plan_notes.extend(coverage_notes)
    if surprise_note:
        plan_notes.append(surprise_note)
    if slice_note:
        plan_notes.append(slice_note)
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
            "note": "One ensemble for the take. Each chord is a whole-chord file or notes at their recorded pitch. Sources are reused across chords. A take is not refused because only one origin answered. Texture is an unpitched field recording, room tone, or pad, and only when the words ask for air or space.",
        },
        "seed": int(seed),
        "length_s": round(length, 3),
        "tail_s": 6.0,
        "pulse_bpm": 60,
        "pitch_tolerance_cents": 15,
        "sections": plan_sections,
        "chords": chords,
        "sources": sources,
        "gather_picks": list(dict.fromkeys(kept_picks)),
        "banks": banks,
        "layers": layers,
        "master": {
            "buses": {
                "keys": {
                    "filters": [{"type": "lp", "hz": 5000 if soft else 8000, "order": 2}],
                    "lufs": -20.0,
                    "pan": -0.05,
                    "gain_db_curve": take.arc(length, mode == "orderly", seed),
                },
                "pad": {"filters": [{"type": "lp", "hz": 2500 if soft else 6000, "order": 2}], "lufs": -36.0, "pan": 0.08},
            },
            "reverb": {"engine": "dragonfly", "send_lu": send_lu, "params": {"decay_s": decay_s}},
            "hp_hz": 30,
            "high_shelf": {"hz": 9000, "db": -3.0 if soft else -1.0},
            "loudness": {"lufs": -17, "tp": -1.5, "method": "linear_tp_limiter"},
        },
        "source_mix": {
            "by_origin": dict(mix_counts),
            "share": {k: round(v / total, 2) for k, v in mix_counts.items()},
            "strike_share": {k: round(v / total, 2) for k, v in mix_counts.items()},
            "instruments": instruments,
            "ensemble": ensemble_note,
            "queried": order,
            "returned": {k: len(v) for k, v in groups.items()},
            "terms": query,
            "instrument_hint": hint,
            "per_chord": per_chord,
            "arrangement": arrangement,
            "coverage": coverage_notes,
            "predictability": {
                **take.predictability(arrangement),
                "mode": mode,
                "duration_cv": duration_cv,
                "pulse": pulse,
                "written_clock": written_clock,
            },
            "ensemble": cast_labels,
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
                "One ensemble plays the take. The next take may choose another ensemble. "
                "Share counts the lead strikes only; a partner and the texture bed are separate. "
                "Nothing is pitch-shifted or time-stretched. Every source has a title, author, license, and page."
            ),
        },
        "notes": plan_notes,
    }
    if slice_note:
        plan["trimmed"] = slice_note
    fit_plan(plan)
    if prefetch_thread is not None:
        until = _plan_until.get()
        # Gather has already spent its search clock. Do not sit here for the bed file;
        # the chosen-file download has its own short clock and finds a finished prefetch.
        timeout = None if until is None else max(0.0, until - time.monotonic())
        prefetch_thread.join(timeout=timeout)
        if not prefetch_thread.is_alive():
            err = prefetch_box.get("error")
            if isinstance(err, job_cancel.Cancelled):
                raise err
    return plan
