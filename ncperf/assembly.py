"""Assembly plan v1: the page places the clips, processes them, and uploads the take.

Gather publishes one playable file per chord, the level-match gain, and the
written clock when the Composition has one. The page caches those bytes,
builds an assembly plan, and runs filtering, modulation, and dials itself.
``POST /api/keep-take`` checks that plan, checks the uploaded WAV or FLAC, and
signs that file. It rebuilds the unprocessed plan only to confirm the clips.
That mix is not the take. Nothing in the plan is time-stretched or pitch-shifted.

The organic durations are ``ncperf.take.chord_slots``. This module does not
keep a second clock. ``reference_vectors`` is the lock for a page that ports
that function.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import re
import subprocess
import tempfile

import numpy as np
import soundfile as sf

from . import assemble
from . import licenses as LIC
from . import take
from .util import SR, decode

VERSION = "1"
# The page should copy gain_db. This covers rounding, not a different mix.
GAIN_TOLERANCE_DB = 0.5
FADE_MAX_MS = 50.0
# Written starts are millisecond values. 20 ms is the acceptance window.
WRITTEN_TOLERANCE_S = 0.02
# len_s may exceed the published duration by this much, and no further.
STRETCH_TOLERANCE_S = 0.001
MAX_TOTAL_S = 180.0
# Codec framing and a short browser tail. The page puts a longer tail into total_s.
DURATION_TOLERANCE_S = 0.5
# 0.001 is about -60 dBFS. 1.02 allows a full-scale file and rejects a hot float.
PEAK_FLOOR = 1e-3
PEAK_CEILING = 1.02
LOUDNESS_MIN_LUFS = -45.0
LOUDNESS_MAX_LUFS = -5.0
# 40 MiB. Stereo 16-bit PCM at 48 kHz for 180 s is about 34.6 MB and fits.
TAKE_AUDIO_MAX = 40 * 1024 * 1024
TAKE_V2 = "2"
# Add a hack here. The limiter is not movable: it is the fixed last item.
HACK_TYPES = ("lowpass", "delay", "plate", "eq", "drive", "compressor", "width", "limiter")
# The same type may appear more than once (two delays). Each hack keeps its own id.
MOVABLE_HACKS = 10
HACK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _./:+-]{0,63}")
PARAM_STR_MAX = 64
PARAM_KEYS_MAX = 32
PARAMS_BYTES_MAX = 2048
CACHE_CONTROL = "public, max-age=31536000, immutable"
_ORIGIN = re.compile(r"^https://([a-z0-9-]+\.)*netcomposer\.art$", re.I)
_SUPPLIERS = ("musicmatter", "freesound", "uiowa", "uiowa_mis", "philharmonia")


def allow_origin(origin: str | None) -> tuple[str, bool]:
    """Access-Control-Allow-Origin for a clip fetch.

    https://netcomposer.art and its subdomains are echoed. Anything else,
    including a missing Origin, gets ``*``. The second value is whether the
    response should send ``Vary: Origin``.
    """
    text = (origin or "").strip()
    if text and _ORIGIN.match(text):
        return text, True
    return "*", False


def written_clock(comp: dict | None, plan: dict | None) -> dict | None:
    """The Composition's durations and tempo map, or None when it did not write a clock.

    None means the page uses the organic rule (``take.chord_slots``). The
    1.25 s floor is not part of a written clock.
    """
    comp = comp or {}
    plan = plan or {}
    text = f"{comp.get('composition') or ''}\n{comp.get('performance') or ''}"
    symbols = [str(chord.get("symbol") or "") for chord in (plan.get("chords") or []) if isinstance(chord, dict)]
    if not symbols:
        symbols = [str(clip.get("symbol") or "") for clip in (plan.get("clips") or []) if isinstance(clip, dict)]
    durations = take.written_durations(text, [(symbol, None) for symbol in symbols])
    if not durations:
        return None
    changes = take._tempo_changes(text)
    opening = take._opening_bpm(text, changes)
    tempo = [{"bar": int(bar), "bpm": float(bpm)} for bar, bpm in changes]
    if opening and not any(row["bar"] <= 1 for row in tempo):
        tempo.insert(0, {"bar": 1, "bpm": float(opening)})
    return {
        "written": True,
        "durations_s": durations,
        "slots": take.slots_from_durations(durations),
        "tempo": tempo,
        "quarters_per_bar": take._quarters_per_bar(text),
    }


def reference_vectors() -> list[dict]:
    """Fixed calls of the organic rule, plus one written clock.

    A page port of ``take.chord_slots`` matches every ``slots`` list here.
    """
    cases = [
        ("five-chords-seed-1", 1, [(5, 40.0)], False, 0),
        ("five-chords-seed-2", 2, [(5, 40.0)], False, 0),
        ("floor-eight-seed-3", 3, [(8, 12.0)], False, 0),
        ("pulse-four-seed-1", 1, [(4, 16.0)], True, 0),
        ("pulse-broaden-seed-4", 4, [(8, 20.0)], True, 2),
        ("two-sections-seed-7", 7, [(3, 12.0), (2, 10.0)], False, 0),
    ]
    out = []
    for name, seed, sections, pulse, broaden in cases:
        slots = take.chord_slots(sections, seed, pulse, broaden)
        durs = [slot["dur"] for slot in slots]
        out.append({
            "name": name,
            "rule": "organic",
            "seed": seed,
            "sections": [{"count": count, "seconds": seconds} for count, seconds in sections],
            "pulse": pulse,
            "broaden": broaden,
            "slots": slots,
            "duration_cv": take.duration_cv(durs),
            "looks_alternating": take.looks_alternating(durs),
            "sum_s": round(sum(durs), 3),
            "min_s": round(min(durs), 3) if durs else 0,
        })
    text = "4/4\n60 BPM\n[1 bar Cm] [2 bars F] [1 bar G]"
    durations = take.written_durations(text, [("Cm", None), ("F", None), ("G", None)])
    out.append({
        "name": "written-60bpm-bars",
        "rule": "written",
        "text": text,
        "chords": ["Cm", "F", "G"],
        "floor_applied": False,
        "durations_s": durations,
        "slots": take.slots_from_durations(durations or []),
    })
    return out


def materialize(plan: dict, files: list[dict] | None = None, budget_s: float = 5.0) -> tuple[list[dict], dict]:
    """Copy each chosen clip into memory and measure the usable region.

    Whole-chord files keep their bytes. A note stack is mixed, without fade
    or gain, into one wav under the clip id. The returned meta is what the
    page needs besides the URL.
    """
    from . import gather

    rows = [dict(row) for row in (files or []) if isinstance(row, dict) and row.get("sid")]
    by_sid = {str(row["sid"]): row for row in rows}
    sources = plan.get("sources") or {}
    missing = [
        sid for sid in sources
        if not (isinstance(by_sid.get(sid), dict) and isinstance(by_sid[sid].get("data"), (bytes, bytearray)) and by_sid[sid]["data"])
    ]
    if missing:
        for row in gather.cache_sources(plan, budget_s):
            by_sid[str(row["sid"])] = row
    meta: dict[str, dict] = {}
    for clip in plan.get("clips") or []:
        if not isinstance(clip, dict):
            continue
        cid = str(clip.get("id") or "")
        sids = [str(sid) for sid in (clip.get("sids") or []) if sid]
        if not cid or not sids:
            raise ValueError("A chord has no recording to copy.")
        parts = []
        for sid in sids:
            row = by_sid.get(sid)
            data = (row or {}).get("data")
            if not isinstance(data, (bytes, bytearray)) or not data:
                raise ValueError(f"Could not copy the recording for {clip.get('symbol') or cid}.")
            src = sources.get(sid) if isinstance(sources.get(sid), dict) else {}
            parts.append(_usable_audio(bytes(data), (row or {}).get("ext"), src))
        if len(parts) == 1:
            full, usable, start_s, end_s = parts[0]
            del full
            info = _measure(usable, sources.get(sids[0]) if isinstance(sources.get(sids[0]), dict) else {})
            info["start_s"] = start_s
            info["end_s"] = end_s
        else:
            stacked = _mix_parts([part[1] for part in parts])
            info = _measure(stacked, {})
            info["start_s"] = 0.0
            info["end_s"] = info["duration_s"]
            wav = _wav_bytes(stacked)
            by_sid[cid] = {"sid": cid, "ext": ".wav", "data": wav, "url": None, "range_bytes": None}
        info["author"] = str(clip.get("author") or "")
        info["duration_s"] = round(max(0.0, info["end_s"] - info["start_s"]), 3)
        meta[cid] = info
    out = list(by_sid.values())
    return out, meta


def annotate_sounds(sounds: list[dict], meta: dict) -> list[dict]:
    """Add the fields the page assembles with. Existing keys stay."""
    for index, row in enumerate(sounds):
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "")
        info = meta.get(cid) or {}
        row["clip_id"] = cid
        row["pos"] = index
        if row.get("preview_url"):
            row["url"] = row["preview_url"]
        if info.get("author") and not row.get("author"):
            row["author"] = info["author"]
        for key in ("start_s", "end_s", "duration_s", "loudness_lufs", "gain_db"):
            if key in info and info[key] is not None:
                row[key] = info[key]
    return sounds


def validate(plan, doc: dict, max_length_s: float = MAX_TOTAL_S) -> list[str]:
    """Reasons this plan cannot be kept. An empty list is a plan Keep will render."""
    if not isinstance(plan, dict):
        return ["plan must be a JSON object."]
    if not isinstance(doc, dict) or not isinstance(doc.get("plan"), dict):
        return ["That Gather is not ready."]
    raw = doc["plan"]
    if raw.get("assembly") != "raw":
        return ["Keep renders a raw assembly plan. This Gather is not one."]
    errors: list[str] = []
    version = plan.get("nc_assembly_version", VERSION)
    if str(version) != VERSION:
        errors.append("This server reads assembly plan v1.")
    gather_id = str(doc.get("id") or "")
    if plan.get("gather_id") != gather_id:
        errors.append("gather_id does not match this Gather.")
    seed = plan.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, (int, float)) or int(seed) != seed or int(seed) < 1:
        errors.append("seed must be a positive integer.")
    entries = plan.get("entries")
    if not isinstance(entries, list) or not entries:
        return errors + ["entries must list every chord."]
    total = plan.get("total_s")
    if isinstance(total, bool) or not isinstance(total, (int, float)) or not np.isfinite(total) or float(total) <= 0:
        errors.append("total_s must be a positive number of seconds.")
        total_s = 0.0
    else:
        total_s = float(total)
        if total_s > float(max_length_s) + 1e-9:
            errors.append(f"The performance is longer than {int(max_length_s)} seconds.")
    clips = [clip for clip in (raw.get("clips") or []) if isinstance(clip, dict)]
    sounds = {}
    for row in doc.get("sounds") or []:
        if isinstance(row, dict):
            sounds[str(row.get("clip_id") or row.get("id") or "")] = row
    if len(entries) != len(clips):
        errors.append("The plan must include every chord, in order.")
    clock = doc.get("clock") if isinstance(doc.get("clock"), dict) else None
    written = list(clock.get("durations_s") or []) if clock and clock.get("written") else None
    slots = take.slots_from_durations(written) if written else None
    if written is not None and len(written) != len(clips):
        errors.append("The written lengths do not match this Gather.")
    seen_pos = set()
    last_at = -1.0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append("Each entry must be an object.")
            continue
        prefix = f"Chord {index + 1}"
        pos = entry.get("pos")
        if pos != index:
            errors.append(f"{prefix} is out of order.")
        elif pos in seen_pos:
            errors.append(f"{prefix} is listed twice.")
        else:
            seen_pos.add(pos)
        clip = clips[index] if index < len(clips) else {}
        cid = str(entry.get("clip_id") or "")
        if not clip or cid != str(clip.get("id") or "") or cid not in sounds:
            errors.append(f"{prefix} uses a clip that is not in this Gather.")
            continue
        symbol = str(clip.get("symbol") or "")
        if str(entry.get("chord") or "") != symbol:
            errors.append(f"{prefix} is not {symbol}.")
        sound = sounds[cid]
        at = _num(entry.get("at_s"))
        length = _num(entry.get("len_s"))
        gain = _num(entry.get("gain_db"))
        fade_in = _num(entry.get("fade_in_ms"))
        fade_out = _num(entry.get("fade_out_ms"))
        if at is None or length is None or length <= 0:
            errors.append(f"{prefix} needs a start and a length.")
            continue
        if at < -STRETCH_TOLERANCE_S or at + 1e-9 < last_at:
            errors.append("The chords must stay in harmonic order.")
        last_at = at
        usable = _num(sound.get("duration_s"))
        if usable is None:
            errors.append(f"{prefix} has no duration yet.")
        elif length > usable + STRETCH_TOLERANCE_S:
            errors.append(f"{prefix} is longer than the recording. Nothing is time-stretched.")
        expected = _num(sound.get("gain_db"))
        if gain is None or expected is None or abs(gain - expected) > GAIN_TOLERANCE_DB + 1e-9:
            errors.append(f"{prefix} is outside the level match.")
        if fade_in is None or fade_out is None or fade_in < 0 or fade_out < 0 or fade_in > FADE_MAX_MS or fade_out > FADE_MAX_MS:
            errors.append(f"Fades must be {int(FADE_MAX_MS)} ms or shorter.")
        if slots is not None and index < len(slots):
            if abs(at - float(slots[index]["t"])) > WRITTEN_TOLERANCE_S:
                errors.append("The written lengths are not the ones in the Composition.")
            if length > float(written[index]) + WRITTEN_TOLERANCE_S:
                errors.append("A chord runs longer than its written length.")
        if total_s and at + length > total_s + WRITTEN_TOLERANCE_S:
            errors.append(f"{prefix} ends after the performance.")
    if slots is not None and total_s and abs(total_s - sum(written)) > WRITTEN_TOLERANCE_S:
        errors.append("The written lengths are not the ones in the Composition.")
    return errors


def plan_for_record(raw: dict, assembly: dict, title: str | None = None) -> dict:
    """A raw plan whose timeline is the assembly plan, for the signed record."""
    plan = copy.deepcopy(raw)
    entries = list(assembly.get("entries") or [])
    total = round(float(assembly.get("total_s") or 0), 3)
    chords = list(plan.get("chords") or [])
    clips = list(plan.get("clips") or [])
    for index, entry in enumerate(entries):
        if index >= len(chords):
            break
        at = round(float(entry.get("at_s") or 0), 3)
        if index + 1 < len(entries):
            window = round(float(entries[index + 1].get("at_s") or 0) - at, 3)
        else:
            window = round(total - at, 3)
        played = round(float(entry.get("len_s") or 0), 3)
        chords[index]["t"] = at
        chords[index]["dur"] = round(max(played, window), 3)
        if index < len(clips) and isinstance(clips[index], dict):
            clips[index]["t"] = at
            clips[index]["dur"] = played
    plan["chords"] = chords
    plan["clips"] = clips
    plan["length_s"] = total
    try:
        plan["seed"] = int(assembly.get("seed") or plan.get("seed") or 1)
    except (TypeError, ValueError):
        pass
    if title:
        plan["title"] = title
        if isinstance(plan.get("composition"), dict):
            plan["composition"]["title"] = title
    plan["assembly_plan"] = assembly
    plan["assembly"] = "raw"
    return plan


def render_take(storage, gather_id: str, plan: dict, assembly: dict, out_dir: str, name: str = "performance", log=print, encode: bool = True):
    """Render the assembly plan from the gathered clip bytes. No stretch, no pitch-shift.

    ``encode=False`` rebuilds the mix only to confirm every clip is present and
    no entry is longer than its source. The mix is not written. Save stores the
    browser upload instead.
    """
    from . import util

    os.makedirs(out_dir, exist_ok=True)
    util.USED["resampler"].clear()
    util.USED["stretch"].clear()
    entries = list(assembly.get("entries") or [])
    total_s = float(assembly.get("total_s") or 0)
    total_n = max(1, int(round(total_s * SR)))
    mix = np.zeros((total_n, 2))
    uses = []
    resolved: dict[str, dict] = {}
    clips = {str(clip.get("id")): clip for clip in (plan.get("clips") or []) if isinstance(clip, dict)}
    sources = plan.get("sources") or {}
    for entry in entries:
        cid = str(entry.get("clip_id") or "")
        clip = clips.get(cid) or {}
        audio = _load_clip(storage, gather_id, cid, clip, sources)
        if audio is None:
            raise RuntimeError(f"could not read gathered clip {cid}")
        y, start_s = audio
        play_n = max(1, int(round(float(entry.get("len_s") or 0) * SR)))
        start_n = max(0, int(round(start_s * SR)))
        piece = _stereo(y[start_n:start_n + play_n])
        short = play_n - len(piece)
        if short > int(STRETCH_TOLERANCE_S * SR) + 1:
            raise RuntimeError(f"clip {cid} is shorter than the plan. Nothing is time-stretched.")
        if short > 0:
            piece = np.vstack([piece, np.zeros((short, 2))])
        gain_db = float(entry.get("gain_db") or 0)
        piece = piece * (10 ** (gain_db / 20.0))
        fade_in = int(round(float(entry.get("fade_in_ms") or 0) / 1000.0 * SR))
        fade_out = int(round(float(entry.get("fade_out_ms") or 0) / 1000.0 * SR))
        piece = _fade(piece, fade_in, fade_out)
        at_n = max(0, int(round(float(entry.get("at_s") or 0) * SR)))
        end = min(total_n, at_n + len(piece))
        if end > at_n:
            mix[at_n:end] += piece[: end - at_n]
        at_s = round(at_n / SR, 3)
        index = int(entry.get("pos") or 0)
        for sid in clip.get("sids") or [cid]:
            sid = str(sid)
            if sid not in resolved:
                resolved[sid] = _credit_source(storage, gather_id, sid, sources.get(sid), clip)
            uses.append({
                "key": sid,
                "layer": "chords",
                "role": "raw chord",
                "at_s": at_s,
                "len_s": round(float(entry.get("len_s") or 0), 3),
                "gain_db": round(gain_db, 2),
                "pitch_shift": False,
                "time_stretch": 1.0,
                "chord_index": index,
            })
        log(f"  placed {cid} at {at_s}s for {entry.get('len_s')}s")
    guarded, before = assemble._peak_guard(mix, assemble.PEAK)
    body = {
        "seed": assembly.get("seed"),
        "uses": uses,
        "selections": {},
        "measurements": [],
        "rejected": [],
        "reverb": None,
        "ir_inputs": None,
        "master": {
            "method": "peak guard on the sum",
            "mode_actual": "peak" if before > assemble.PEAK else "unity",
            "ceiling": assemble.PEAK,
            "peak_before": round(float(before), 4),
        },
        "tone": None,
        "quality": {
            "resampler": sorted(util.USED["resampler"]),
            "stretch": sorted(util.USED["stretch"]),
        },
        "sources": resolved,
        "assembly_plan": assembly,
    }
    with open(os.path.join(out_dir, "uses.json"), "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=1, default=float)
    if not encode:
        return None, None, body
    flac = os.path.join(out_dir, name + ".flac")
    mp3 = os.path.join(out_dir, name + ".mp3")
    sf.write(flac, guarded, SR, subtype="PCM_24")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", flac, "-ar", "48000", "-c:a", "libmp3lame", "-b:a", "320k", mp3],
        check=True,
    )
    return flac, mp3, body


def parse_take_state(raw, gather_id: str) -> dict:
    """The page's plan plus the processing it applied. Raises ValueError when the shape is wrong.

    ``plan`` is assembly plan v1. v1 ``processing`` may be omitted. Its chain is
    the effect names in the order the page ran them, and ``params`` is whatever
    JSON the page used. v2 is the hack stack: ``schema`` 2 and a chain of hack
    objects, with the fixed limiter last. This function does not re-run either.
    """
    if not isinstance(raw, dict):
        raise ValueError("take_state must be a JSON object.")
    version = raw.get("nc_take_version", VERSION)
    plan = raw.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("take_state.plan is the assembly plan.")
    if plan.get("gather_id") != gather_id:
        raise ValueError("gather_id does not match this Gather.")
    text = str(version)
    if text == VERSION:
        processing = _processing_v1(raw.get("processing", {}))
        return {"nc_take_version": VERSION, "plan": plan, "processing": processing}
    if text == TAKE_V2:
        processing = _processing_v2(raw.get("processing", {}))
        return {"nc_take_version": TAKE_V2, "plan": plan, "processing": processing}
    raise ValueError("This server reads take_state v1 or v2.")


def _processing_v1(processing) -> dict:
    """v1 stores the names and the one params object. Other keys are dropped."""
    if processing is None:
        processing = {}
    if not isinstance(processing, dict):
        raise ValueError("processing must be a JSON object.")
    chain = processing.get("chain", [])
    if chain is None:
        chain = []
    if not isinstance(chain, list) or len(chain) > 32:
        raise ValueError("processing.chain must be a list of short names.")
    names = []
    for item in chain:
        if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _./:+-]{0,63}", item.strip()):
            raise ValueError("processing.chain must be a list of short names.")
        names.append(item.strip())
    params = processing.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("processing.params must be a JSON object.")
    try:
        params = json.loads(json.dumps(params))
    except (TypeError, ValueError) as exc:
        raise ValueError("processing.params must be a JSON object.") from exc
    return {"chain": names, "params": params}


def _processing_v2(processing) -> dict:
    """v2 keeps each hack object. The limiter is fixed and last, and is not one of the ten."""
    if not isinstance(processing, dict):
        raise ValueError("processing must be a JSON object.")
    schema = processing.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != 2:
        raise ValueError("processing.schema must be 2.")
    chain = processing.get("chain")
    if not isinstance(chain, list) or not chain:
        raise ValueError("The limiter stays last.")
    if len(chain) > MOVABLE_HACKS + 1:
        raise ValueError("A hack stack has at most 10 hacks, plus the limiter.")
    items = [_hack(item, last=index == len(chain) - 1) for index, item in enumerate(chain)]
    return {"schema": 2, "chain": items}


def _hack(item, last: bool) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Each hack is an object.")
    type_name = item.get("type")
    if not isinstance(type_name, str) or type_name not in HACK_TYPES:
        raise ValueError("Unknown hack type.")
    fixed = item.get("fixed", False)
    if fixed is not True and fixed is not False:
        raise ValueError("fixed must be true or false.")
    if last:
        if type_name != "limiter" or fixed is not True:
            raise ValueError("The limiter stays last.")
    elif type_name == "limiter" or fixed is True:
        raise ValueError("The limiter stays last.")
    ident = item.get("id")
    if not isinstance(ident, str) or not HACK_NAME.fullmatch(ident):
        raise ValueError("Each hack needs a short id.")
    version = item.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("Each hack version must be an integer.")
    bypass = item.get("bypass")
    if bypass is not True and bypass is not False:
        raise ValueError("Each hack bypass must be true or false.")
    kept = {
        "id": ident,
        "type": type_name,
        "version": version,
        "bypass": bypass,
        "params": _hack_params(item.get("params", {})),
    }
    if fixed is True:
        kept["fixed"] = True
    return kept


def _hack_params(params) -> dict:
    if not isinstance(params, dict):
        raise ValueError("Hack params must be a flat object.")
    if len(params) > PARAM_KEYS_MAX:
        raise ValueError("Hack params are too large.")
    out = {}
    for key, value in params.items():
        if not isinstance(key, str) or not HACK_NAME.fullmatch(key):
            raise ValueError("Hack params must be a flat object.")
        out[key] = _param_value(value)
    raw = json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode()
    if len(raw) > PARAMS_BYTES_MAX:
        raise ValueError("Hack params are too large.")
    return out


def _param_value(value):
    """A finite number, a short string, or null. Null is the open filter. NaN is not a number here."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Hack params must be finite numbers, short strings, or null.")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Hack params must be finite numbers, short strings, or null.")
        return value
    if isinstance(value, str) and len(value) <= PARAM_STR_MAX:
        return value
    raise ValueError("Hack params must be finite numbers, short strings, or null.")


def inspect_upload(data: bytes, total_s: float) -> dict:
    """Decode a WAV or FLAC take and refuse a file that cannot be this plan.

    Duration must match ``total_s`` within ``DURATION_TOLERANCE_S``. The peak
    and the integrated loudness have to sit in the sane range. The bytes are
    not compared to a server render.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("Attach the take as the form field 'audio'.")
    blob = bytes(data)
    if len(blob) > TAKE_AUDIO_MAX:
        raise ValueError(
            f"The take is too large ({len(blob)} bytes). The limit is {TAKE_AUDIO_MAX} bytes (40 MiB)."
        )
    kind = _audio_kind(blob)
    if kind is None:
        raise ValueError("audio must be WAV or FLAC.")
    handle = tempfile.NamedTemporaryFile(suffix="." + kind, delete=False)
    try:
        handle.write(blob)
        handle.close()
        try:
            y, rate = sf.read(handle.name, always_2d=True, dtype="float64")
        except Exception as exc:
            raise ValueError("The take could not be decoded.") from exc
    finally:
        try:
            os.remove(handle.name)
        except OSError:
            pass
    rate = int(rate)
    if rate not in (44100, 48000):
        raise ValueError("WAV or FLAC sample rate must be 44100 or 48000 Hz.")
    if y.ndim != 2 or y.shape[1] not in (1, 2):
        raise ValueError("The take must be mono or stereo.")
    if len(y) < 1 or not np.isfinite(y).all():
        raise ValueError("The take could not be decoded.")
    duration = len(y) / float(rate)
    if abs(duration - float(total_s)) > DURATION_TOLERANCE_S:
        raise ValueError(f"The take is {duration:.2f}s. The plan is {float(total_s):.2f}s.")
    peak = float(np.max(np.abs(y)))
    if peak < PEAK_FLOOR:
        raise ValueError("The take is silent.")
    if peak > PEAK_CEILING:
        raise ValueError("The take peaks past full scale.")
    loud = _integrated_lufs(y, rate)
    if loud is not None and (loud < LOUDNESS_MIN_LUFS or loud > LOUDNESS_MAX_LUFS):
        which = "too quiet" if loud < LOUDNESS_MIN_LUFS else "too loud"
        raise ValueError(f"The take is {which} ({loud:.1f} LUFS).")
    return {
        "format": kind,
        "sample_rate": rate,
        "channels": int(y.shape[1]),
        "duration_s": round(duration, 3),
        "peak": round(peak, 4),
        "loudness_lufs": None if loud is None else round(float(loud), 2),
        "upload_sha256": hashlib.sha256(blob).hexdigest(),
        "upload_size_bytes": len(blob),
    }


def write_take_files(data: bytes, out_dir: str) -> tuple[str, str]:
    """Store the upload as performance.flac and encode preview.mp3 from it.

    A FLAC upload is kept byte for byte. A WAV upload is packed into FLAC
    without resampling. The preview is 320 kbps.
    """
    os.makedirs(out_dir, exist_ok=True)
    blob = bytes(data)
    flac = os.path.join(out_dir, "performance.flac")
    if _audio_kind(blob) == "flac":
        with open(flac, "wb") as fh:
            fh.write(blob)
    else:
        wav = os.path.join(out_dir, "upload.wav")
        with open(wav, "wb") as fh:
            fh.write(blob)
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", wav, "-c:a", "flac", flac],
            check=True, timeout=180,
        )
    if not os.path.isfile(flac) or os.path.getsize(flac) < 1:
        raise RuntimeError("The take could not be stored.")
    mp3 = os.path.join(out_dir, "preview.mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", flac, "-ar", "48000", "-c:a", "libmp3lame", "-b:a", "320k", mp3],
        check=True, timeout=180,
    )
    if not os.path.isfile(mp3) or os.path.getsize(mp3) < 1:
        raise RuntimeError("The preview could not be encoded.")
    return flac, mp3


def finish_browser_record(rec: dict, take_state: dict, info: dict, flac_path: str) -> dict:
    """Record the upload, the plan, and the processing. Recompute the hash.

    The signature is applied by the caller after this returns.
    """
    from . import record

    processing = json.loads(json.dumps(take_state.get("processing") or {"chain": [], "params": {}}))
    rec["take_state"] = {
        "nc_take_version": str(take_state.get("nc_take_version") or VERSION),
        "plan": take_state.get("plan"),
        "processing": processing,
    }
    rec["assembly_plan"] = take_state.get("plan")
    rec["hacks_chain"] = [
        {
            "n": 1,
            "tool": "browser-assembly",
            "type": "The page placed each gathered clip. The server rebuilt that plan only to confirm the clips and the lengths, and did not keep that mix.",
        },
        {
            "n": 2,
            "tool": "browser-processing",
            "chain": list(processing.get("chain") or []),
            "params": processing.get("params") or {},
            "type": "Filtering, modulation, and dials ran in the browser. The server did not re-run them.",
        },
        {"n": 3, "tool": "preview", "type": "MP3 preview encoded from the uploaded take"},
    ]
    compromises = rec.get("performance", {}).setdefault("known_compromises", [])
    compromises.append({
        "what": "The stored audio is the browser upload. Web Audio processing is not reproduced by the server.",
    })
    rec["performance"]["take_audio"] = {
        "source": "browser",
        "upload_sha256": info.get("upload_sha256"),
        "upload_size_bytes": info.get("upload_size_bytes"),
        "upload_format": info.get("format"),
        "sample_rate": info.get("sample_rate"),
        "channels": info.get("channels"),
        "duration_s": info.get("duration_s"),
        "peak": info.get("peak"),
        "loudness_lufs": info.get("loudness_lufs"),
    }
    try:
        meta = sf.info(flac_path)
        subtype = meta.subtype or "FLAC"
        rec["performance"]["outputs"][0]["format"] = f"FLAC {subtype}, {meta.samplerate} Hz, {meta.channels} ch"
    except Exception:
        pass
    rec["record_hash"] = {
        "algorithm": "sha256",
        "over": "canonical JSON (sorted keys, no whitespace) of the record without record_hash and signature",
        "value": record.rhash(rec),
    }
    return rec


def _audio_kind(data: bytes) -> str | None:
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if len(data) >= 4 and data[:4] == b"fLaC":
        return "flac"
    return None


def _integrated_lufs(y: np.ndarray, rate: int) -> float | None:
    if len(y) < int(0.4 * rate):
        return None
    try:
        import pyloudnorm as pyln
        meter = pyln.Meter(rate)
        heard = np.nan_to_num(y if y.shape[1] == 2 else np.column_stack([y[:, 0], y[:, 0]]))
        loud = float(meter.integrated_loudness(heard))
    except Exception:
        return None
    if not np.isfinite(loud):
        return None
    return loud


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not np.isfinite(number):
        return None
    return number


def _registry_lufs(src: dict) -> float | None:
    urls = []
    for field in ("audio_url", "url"):
        if src.get(field):
            urls.append(str(src[field]))
    if not urls:
        return None
    from . import registry
    names = []
    for field in ("origin", "kind"):
        val = src.get(field)
        if isinstance(val, str) and val.strip():
            names.append(val.strip())
    names.extend(_SUPPLIERS)
    seen = set()
    for name in names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        for url in urls:
            row = registry.peek(key, url)
            if not isinstance(row, dict) or row.get("loudness_lufs") in (None, ""):
                continue
            try:
                loud = float(row["loudness_lufs"])
            except (TypeError, ValueError):
                continue
            if np.isfinite(loud):
                return loud
    return None


def _decode_bytes(data: bytes, ext: str | None):
    suffix = ext if isinstance(ext, str) and ext.startswith(".") else ".bin"
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        handle.write(data)
        handle.close()
        return decode(handle.name)
    finally:
        try:
            os.remove(handle.name)
        except OSError:
            pass


def _usable_audio(data: bytes, ext, src: dict):
    """Full decode, the sliced usable region, and start/end in the file."""
    full = _stereo(_decode_bytes(data, ext))
    dur = len(full) / SR if len(full) else 0.0
    seg = src.get("segment") if isinstance(src, dict) else None
    if isinstance(seg, dict) and seg.get("t0") is not None and seg.get("t1") is not None:
        start = max(0.0, float(seg["t0"]))
        end = min(dur, max(start, float(seg["t1"])))
    else:
        start, end = 0.0, dur
    a = max(0, int(round(start * SR)))
    b = max(a, int(round(end * SR)))
    return full, full[a:b], round(start, 3), round(end, 3)


def _mix_parts(parts: list) -> np.ndarray:
    if not parts:
        return np.zeros((1, 2))
    slot_n = max(len(part) for part in parts)
    _audible, stacked = assemble._stack(parts, max(1, slot_n))
    heard = max((len(part) for part in parts), default=1)
    return _stereo(stacked[: max(1, heard)])


def _measure(usable: np.ndarray, src: dict) -> dict:
    reg = _registry_lufs(src) if src else None
    if reg is not None:
        loud = round(reg, 2)
        gain = round(assemble.CLIP_LUFS - loud, 2)
    else:
        _matched, gain = assemble._match_level(usable, len(usable))
        if len(usable) == 0 or float(np.max(np.abs(usable))) < 1e-8:
            loud = None
            gain = 0.0
        else:
            loud = round(assemble.CLIP_LUFS - gain, 2)
            gain = round(float(gain), 2)
    dur = round(len(usable) / SR, 3) if len(usable) else 0.0
    return {"loudness_lufs": loud, "gain_db": gain, "duration_s": dur}


def _wav_bytes(y: np.ndarray) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, _stereo(y), SR, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _stereo(y: np.ndarray) -> np.ndarray:
    y = np.asarray(y, dtype=np.float64)
    if y.ndim == 1:
        return np.column_stack([y, y])
    if y.shape[1] == 1:
        return np.column_stack([y[:, 0], y[:, 0]])
    return y


def _fade(y: np.ndarray, fade_in_n: int, fade_out_n: int) -> np.ndarray:
    z = np.array(y, copy=True)
    n = len(z)
    a = min(max(0, fade_in_n), n)
    b = min(max(0, fade_out_n), n)
    if a:
        z[:a] *= (np.sin(np.linspace(0, np.pi / 2, a)) ** 2)[:, None]
    if b:
        z[n - b:] *= (np.cos(np.linspace(0, np.pi / 2, b)) ** 2)[:, None]
    return z


def _load_clip(storage, gather_id: str, cid: str, clip: dict, sources: dict):
    """The bytes the page plays, sliced the same way Gather measured them.

    A stack is the mixed wav stored under the clip id (start 0). A single
    file is the original recording, and start_s is its usable start.
    """
    sids = [str(sid) for sid in (clip.get("sids") or []) if sid]
    if len(sids) > 1:
        data = storage.get(f"gathers/{gather_id}/audio/{cid}")
        if not data:
            return None
        y = _stereo(_decode_bytes(bytes(data), ".wav"))
        return y, 0.0
    sid = sids[0] if sids else cid
    data = storage.get(f"gathers/{gather_id}/audio/{sid}") or storage.get(f"gathers/{gather_id}/audio/{cid}")
    if not data:
        return None
    src = sources.get(sid) if isinstance(sources.get(sid), dict) else {}
    _full, _usable, start_s, _end = _usable_audio(bytes(data), _ext_of(storage, gather_id, sid), src)
    return _full, start_s


def _ext_of(storage, gather_id: str, sid: str) -> str:
    doc_raw = storage.get(f"gathers/{gather_id}/collection.json")
    if not doc_raw:
        return ".bin"
    try:
        doc = json.loads(doc_raw)
    except Exception:
        return ".bin"
    for row in doc.get("files") or []:
        if isinstance(row, dict) and str(row.get("sid") or "") == sid:
            return str(row.get("ext") or ".bin")
    return ".bin"


def _credit_source(storage, gather_id: str, sid: str, src, clip: dict) -> dict:
    src = src if isinstance(src, dict) else {}
    data = storage.get(f"gathers/{gather_id}/audio/{sid}") or b""
    if not isinstance(data, (bytes, bytearray)):
        data = b""
    license_id = src.get("license") or (clip or {}).get("license") or ""
    if isinstance(license_id, dict):
        license_id = license_id.get("id") or ""
    kind = LIC.from_id(str(license_id))
    author = str(src.get("author") or (clip or {}).get("author") or (clip or {}).get("supplier") or "unknown")
    title = str(src.get("title") or (clip or {}).get("title") or sid)
    page = str(src.get("page") or (clip or {}).get("page") or src.get("url") or src.get("audio_url") or "")
    audio_url = str(src.get("audio_url") or src.get("url") or page)
    return {
        "audio_url": audio_url,
        "what": "gathered clip, as copied for the page",
        "source": str(src.get("source") or (clip or {}).get("supplier") or "gathered"),
        "source_url": page,
        "title": title,
        "author": author,
        "supplier": {"display_name": str((clip or {}).get("supplier") or author)},
        "license": {
            "id": kind[0],
            "name": kind[1],
            "uri": kind[2],
            "verified_live": "checked when the clip was gathered",
        },
        "sha256": hashlib.sha256(bytes(data)).hexdigest(),
        "size_bytes": len(data),
    }
