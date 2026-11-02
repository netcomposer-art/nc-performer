"""
Library qualify step: one harvested candidate in, a measured one-shot (or a rejection with its reason) out.

The Worker harvests candidates from Freesound and posts each file here (POST /internal/library/qualify). This module
decodes it to 48 kHz, trims it, measures it exactly as musicmatter.art's seed does (library_measures.py is a verbatim
copy of musicmatter.art tools/library/measures.py), fingerprints it, picks a role and tags, and returns the 24-bit
FLAC and a level-matched 128 kbps MP3 preview for the review page. Nothing here pitch-shifts, time-stretches or
normalises the audio; the level match is a playback gain in the manifest.

Design: "NYX Library: a curated commons for samples, design v1" (Peter, 2026-10-31), step 3.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from . import library_measures as M

ROLES = ("kick", "snare", "clap", "hat", "tom", "perc", "cymbal", "fx", "tonal")
MAX_SOURCE_BYTES = 25 * 1024 * 1024
EXTENSIONS = {"wav", "wave", "aif", "aiff", "flac", "ogg", "mp3", "m4a"}
MIN_LENGTH_S = 0.02
QUIET_DBFS = -50.0
# Longest one-shot after the trim, per role. Search already asks Freesound for at most 3 s.
MAX_LENGTH_S = {"fx": 6.0, "cymbal": 6.0, "tonal": 6.0}
DEFAULT_MAX_LENGTH_S = 3.0
LOOP = re.compile(r"\b(loop|loops|bpm|groove|beat ?\d|pattern|break ?beat|fill)\b", re.I)

# Words in a sound's name that name its role. The first role that matches wins unless the query's role also matches.
ROLE_WORDS: list[tuple[str, re.Pattern]] = [
    ("clap", re.compile(r"\b(hand ?)?claps?\b|\bhandclaps?\b|\bcp\b", re.I)),
    ("rim", re.compile(r"\brim ?(shot)?s?\b|\bside ?stick\b|\brs\b", re.I)),
    ("open-hat", re.compile(r"\bopen ?(hi[- ]?)?hat|\b(hi[- ]?)?hat ?open\b|\bhh ?open\b|\bopen ?hh\b|\boh\b", re.I)),
    ("hat", re.compile(r"\bhi[- ]?hats?\b|\bhihats?\b|\bhh\b|\bch\b|\bclosed ?hat|\bhats?\b", re.I)),
    ("cymbal", re.compile(r"\bcrash|\bride\b|\bcymbals?\b|\bsplash\b|\bchina\b", re.I)),
    ("tom", re.compile(r"\btoms?\b|\b(lt|mt|ht|low ?tom|hi ?tom)\b", re.I)),
    ("snare", re.compile(r"\bsnares?\b|\bsnr\b|\bsd\b", re.I)),
    ("kick", re.compile(r"\bkicks?\b|\bbd\b|\bbass ?drum\b|\bbassdrum\b|\bkck\b", re.I)),
    ("perc", re.compile(r"\bcow ?bell|\bcongas?\b|\bbongos?\b|\bclaves?\b|\bmaracas?\b|\bshakers?\b|\btambourine\b|"
                        r"\bperc(ussion)?\b|\bblock\b|\bclick\b|\btick\b", re.I)),
]
MACHINES = re.compile(r"\b(808|909|707|606|727|626|505|linn ?drum|lm[- ]?1|cr[- ]?78|dmx|sp[- ]?1200|mpc)\b", re.I)
STYLE_TAGS = {
    "analog", "analogue", "digital", "drum-machine", "drummachine", "lofi", "lo-fi", "vintage", "distorted", "dirty",
    "clean", "dry", "reverb", "gated", "punchy", "tight", "deep", "sub", "boomy", "crisp", "snappy", "noisy",
    "synthetic", "synth", "electro", "techno", "house", "trap", "hip-hop", "hiphop", "acoustic", "metallic", "short",
}


def clean_name(name: str) -> str:
    """A Freesound name as a title: no file extension, underscores as spaces, at most 80 characters."""
    stem = re.sub(r"\.(wav|wave|aif|aiff|flac|ogg|mp3|m4a)$", "", (name or "").strip(), flags=re.I)
    stem = re.sub(r"[_]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" -")
    return (stem or "Untitled")[:80]


def words(text: str) -> str:
    """Text for word matching: underscores and dots as spaces (they are word characters to a regex)."""
    return re.sub(r"[_.]+", " ", text or "")


def looks_like_loop(name: str, tags: list[str]) -> bool:
    return bool(LOOP.search(words(name))) or any(t.lower() in {"loop", "loops", "drum-loop", "beat"} for t in tags or [])


def assign_role(name: str, tags: list[str], hint: str | None, m: dict) -> tuple[str, set[str], str]:
    """(role, extra tags, basis). Name words first, the query's role next, Freesound tags last; measures settle the
    hat's open or closed and flag a role the measures disagree with."""
    name = words(name)
    found: list[str] = [label for label, rx in ROLE_WORDS if rx.search(name)]
    basis = "name"
    if hint and (hint in found or (hint == "hat" and "open-hat" in found) or (hint == "perc" and "rim" in found)):
        label = "open-hat" if hint == "hat" and "open-hat" in found else ("rim" if hint == "perc" and "rim" in found else hint)
    elif found:
        label = found[0]
    elif hint:
        label, basis = hint, "query"
    else:
        tag_text = " ".join(tags or [])
        found = [lab for lab, rx in ROLE_WORDS if rx.search(tag_text)]
        label, basis = (found[0], "tags") if found else ("perc", "default")
    extra: set[str] = set()
    if label == "rim":
        role = "perc"
        extra.add("rim")
    elif label == "open-hat":
        role = "hat"
        extra.add("open")
    else:
        role = label if label in ROLES else "perc"
    if role == "hat" and "open" not in extra:
        lower = (name or "").lower()
        if re.search(r"\bclosed\b|\bch\b|\bclosed ?hh\b", lower):
            extra.add("closed")
        else:
            extra.add("open" if m["decay_s"] > 0.35 else "closed")
    return role, extra, basis


def role_doubt(role: str, m: dict) -> str | None:
    """A short note for the review page when the measures argue with the role."""
    if role == "kick" and m["centroid_hz"] > 2500 and m["low_share"] < 0.05:
        return "Measures sound bright for a kick: check the role."
    if role == "hat" and m["centroid_hz"] < 1500:
        return "Measures sound dark for a hat: check the role."
    if role in {"snare", "clap"} and m["low_share"] > 0.6:
        return f"Mostly low end for a {role}: check the role."
    return None


def harvest_tags(role: str, extra: set[str], name: str, tags: list[str], query: str, m: dict) -> list[str]:
    t: set[str] = {"electronic", role} | extra | M.measure_tags(role, m)
    text = words(" ".join([name or "", query or "", " ".join(tags or [])]))
    for hit in MACHINES.findall(text):
        t.add(re.sub(r"[\s-]+", "", hit.lower()))
    for tag in tags or []:
        low = tag.lower()
        if low in STYLE_TAGS:
            t.add({"analogue": "analog", "drummachine": "drum-machine", "lo-fi": "lofi", "hiphop": "hip-hop"}.get(low, low))
    if "acoustic" in t:
        t.discard("electronic")
    return sorted(t)


def run_clipped(data: np.ndarray, level: float = 0.999, run: int = 4, runs: int = 2) -> bool:
    """True when more than `runs` stretches of `run`+ consecutive samples sit at full scale (flat-topped, i.e. clipped).
    A sound normalised to 0 dBFS touches full scale once or twice; that alone is not clipping."""
    hot = (np.abs(data) >= level).any(axis=1).astype(np.int8)
    if not hot.any():
        return False
    edges = np.diff(np.concatenate([[0], hot, [0]]))
    starts, ends = np.nonzero(edges == 1)[0], np.nonzero(edges == -1)[0]
    return int(((ends - starts) >= run).sum()) > runs


def reject(reason: str, **extra) -> dict:
    return {"ok": False, "reason": reason, **extra}


def qualify(raw: bytes, meta: dict) -> dict:
    """Qualify one candidate. meta: {id, name, tags, query, role (the query's role), quality, ext}."""
    source_sha = hashlib.sha256(raw).hexdigest()
    ext = re.sub(r"[^a-z0-9]", "", str(meta.get("ext") or "").lower()) or "wav"
    if ext not in EXTENSIONS:
        return reject(f"unsupported file type .{ext}", source_sha256=source_sha)
    name = str(meta.get("name") or "")
    tags = [str(t) for t in (meta.get("tags") or [])]
    if looks_like_loop(name, tags):
        return reject("looks like a loop, not a one-shot", source_sha256=source_sha)
    with tempfile.TemporaryDirectory(prefix="nc-lib-") as tmp:
        src = Path(tmp) / f"in.{ext}"
        src.write_bytes(raw)
        try:
            data = M.decode(src)
        except Exception as exc:  # ffmpeg or libsndfile could not read it
            return reject(f"could not decode ({type(exc).__name__})", source_sha256=source_sha)
        if len(data) < int(MIN_LENGTH_S * M.SR):
            return reject("too short", source_sha256=source_sha)
        data, _ = M.trim(data)
        if len(data) < int(MIN_LENGTH_S * M.SR):
            return reject("silent or too short after trim", source_sha256=source_sha)
        peak = float(np.abs(data).max())
        if 20 * np.log10(max(peak, 1e-9)) < QUIET_DBFS:
            return reject(f"too quiet (peak below {QUIET_DBFS:g} dBFS)", source_sha256=source_sha)
        if run_clipped(data):
            return reject("clipped (flat-topped at full scale)", source_sha256=source_sha)
        m = M.measure(data, None)
        hint = meta.get("role") if meta.get("role") in ROLES else None
        role, extra, basis = assign_role(name, tags, hint, m)
        limit = MAX_LENGTH_S.get(role, DEFAULT_MAX_LENGTH_S)
        if m["length_s"] > limit:
            return reject(f"longer than {limit:g} s after trim ({m['length_s']:.2f} s)", source_sha256=source_sha)
        hit, peak_db = M.hit_db_data(data, M.SR)
        gain, limited = M.playback_gain(hit, peak_db)
        m["hit_dbfs"] = round(hit, 1)
        out = Path(tmp) / "audio.flac"
        sf.write(str(out), data, M.SR, subtype="PCM_24", format="FLAC")
        flac = out.read_bytes()
        preview = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(out), "-af", f"volume={gain}dB", "-b:a", "128k", "-f", "mp3", "-"],
            check=True, capture_output=True,
        ).stdout
    processing = ["resample to 48 kHz (soxr)", "trim silence: 2 ms before onset, tail below -60 dB", "10 ms fade out"]
    if meta.get("quality") == "preview":
        processing.insert(0, "decoded from the source's HQ MP3 preview (lossy; upgrade to the original when available)")
    result = {
        "ok": True,
        "sha256": hashlib.sha256(flac).hexdigest(),
        "source_sha256": source_sha,
        "size_bytes": len(flac),
        "role": role,
        "role_basis": basis,
        "tags": harvest_tags(role, extra, name, tags, str(meta.get("query") or ""), m),
        "title": clean_name(name),
        "measures": m,
        "measures_version": M.MEASURES_VERSION,
        "playback": {"gain_db": gain, "method": M.playback_method(), **({"limited_by_peak": True} if limited else {})},
        "format": {"mime": "audio/flac", "lossless": meta.get("quality") != "preview", "sample_rate": M.SR,
                   "bit_depth": 24, "channels": int(data.shape[1]), "frames": int(len(data))},
        "processing": processing,
        "wave": M.wave_data(data),
        "flac": flac,
        "preview": preview,
    }
    doubt = role_doubt(role, m)
    if doubt:
        result["note"] = doubt
    return result


def to_json(result: dict) -> dict:
    """The response body: bytes as base64."""
    import base64
    out = {k: v for k, v in result.items() if not isinstance(v, (bytes, bytearray))}
    for key in ("flac", "preview"):
        if isinstance(result.get(key), (bytes, bytearray)):
            out[f"{key}_b64"] = base64.b64encode(result[key]).decode()
    return out


def read_meta(header: str | None) -> dict:
    """X-NC-Library-Meta: base64url JSON."""
    import base64
    import json
    if not header:
        return {}
    pad = "=" * (-len(header) % 4)
    try:
        doc = json.loads(base64.urlsafe_b64decode(header + pad).decode())
    except Exception:
        return {}
    return doc if isinstance(doc, dict) else {}

