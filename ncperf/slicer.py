"""Cut native-pitch notes out of a source recording.

One note becomes two files: Opus at 96 kbps for the page, and a FLAC master.
The cut starts 5 ms before the onset, keeps the ring until it falls 60 dB
below the attack or 8 seconds, and fades the last 80 ms. Nothing in this
module pitch-shifts or time-stretches. A missing pitch stays missing.

The bytes are written only on the machine that runs the command. Publishing
them to R2 is a later step and needs an explicit yes. See PUBLISH for the
key layout that step would use.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

FORMAT = "nc-sampler/1"
PRE_ROLL_S = 0.005
RING_DB = -60.0
RING_CAP_S = 8.0
SLICE_FADE_S = 0.080
ATTACK_FADE_S = 0.003
CHORD_FADE_S = 0.120
OPUS_BITRATE = "96k"
# Search window for one chromatic file. Wider than this and the pitch
# measurement is run without a centre, so a harmonic an octave away is not
# pulled into the file's own octave.
SPAN_WINDOW = 14
# pYIN within 15 cents is the note. The FFT peak on a guitar often sits
# 20–30 cents flat of that fundamental, so it is only a disagreement check.
FFT_DISAGREE_CENTS = 40.0
SPREAD_CENTS = 45.0

# Proposed layout in the Worker's PERFORMANCES bucket (nc-performances).
# The page asks the Worker for these keys. It does not read R2 itself, and
# this module does not write them. FLAC masters stay off the public route.
# A dedicated bucket is optional; the same bucket with this prefix is enough
# if the notes/ prefix is left out of the performance-expiry lifecycle.
PUBLISH = {
    "bucket_binding": "PERFORMANCES",
    "bucket_name": "nc-performances",
    "note_key": "notes/{slug}/{midi}.opus",
    "master_key": "notes/{slug}/{midi}.flac",
    "manifest_key": "notes/{slug}/manifest.json",
    "chord_key": "notes/{slug}/chords/{symbol}.opus",
    "chord_master_key": "notes/{slug}/chords/{symbol}.flac",
    "chord_manifest_key": "notes/{slug}/chords/manifest.json",
    "route_manifest": "GET /api/notes/{slug}/manifest.json",
    "route_note": "GET /api/notes/{slug}/{midi}.opus",
    "cache_control": "public, max-age=31536000, immutable",
    "content_type_opus": "audio/ogg",
    "content_type_flac": "audio/flac",
    "cors": "echo https://netcomposer.art and https://*.netcomposer.art, same as gathered clips",
    "masters_on_public_route": False,
    "page_fetch": (
        "The page loads GET /api/notes/{slug}/manifest.json once, then fetches "
        "only the Opus keys the written voicing needs and decodes them in parallel. "
        "Notes are not copied into gathers/{id}/audio/."
    ),
    "lifecycle": "The notes/ prefix is not part of the performance-expiry lifecycle.",
}

DEFAULT_LIBRARIES = ("uiowa-guitar", "uiowa-piano", "uiowa-cello-pizz")
_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_SCHEMA = os.path.join(os.path.dirname(__file__), "schema", "sampler.schema.json")
_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def note_name(midi: int) -> str:
    midi = int(midi)
    return _NAMES[midi % 12] + str(midi // 12 - 1)


def instrument_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "instrument"


def chord_file_stem(symbol: str) -> str:
    return symbol.replace("#", "s").replace("/", "-").replace(" ", "")


def note_key(slug: str, midi: int) -> str:
    return f"notes/{slug}/{int(midi)}.opus"


def master_key(slug: str, midi: int) -> str:
    return f"notes/{slug}/{int(midi)}.flac"


def chord_key(slug: str, symbol: str) -> str:
    return f"notes/{slug}/chords/{chord_file_stem(symbol)}.opus"


def chord_master_key(slug: str, symbol: str) -> str:
    return f"notes/{slug}/chords/{chord_file_stem(symbol)}.flac"


def credit_line(author: str, page: str, license_id: str) -> str:
    return f"{author}, {page}, {license_id}"


def default_playback() -> dict:
    return {
        "pitch_shift": False,
        "time_stretch": False,
        "octave_substitution": False,
        "missing": "omit",
        "ring_cap_s": RING_CAP_S,
        "slice_fade_s": SLICE_FADE_S,
        "chord_change_fade_s": CHORD_FADE_S,
    }


def require_license(license_id: str) -> str:
    """Reject anything other than CC0, CC BY, CC BY-SA, or public domain."""
    from . import licenses as LIC
    parsed = LIC.from_id(license_id)[0]
    if not LIC.allowed(parsed):
        raise ValueError(f"license not allowed: {license_id}")
    return parsed


def slice_note(audio, sr, onset_s, region_end_s=None, pre_roll_s=PRE_ROLL_S,
               ring_db=RING_DB, ring_cap_s=RING_CAP_S, fade_s=SLICE_FADE_S,
               attack_s=ATTACK_FADE_S):
    """Cut one note. The sample rate of `audio` is the sample rate returned.

    `region_end_s` is a hard stop, in the same clock as `onset_s`, so a
    chromatic run does not keep ringing into the next pitch. The ring inside
    that window ends at `ring_db` below the peak, or at `ring_cap_s`.
    """
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim == 1:
        audio = audio[:, None]
    n = int(audio.shape[0])
    sr = int(sr)
    t0 = max(0.0, float(onset_s) - float(pre_roll_s))
    i0 = min(n, max(0, int(round(t0 * sr))))
    hard = n / sr if n else t0
    if region_end_s is not None:
        hard = min(hard, float(region_end_s))
    hard = min(hard, t0 + float(ring_cap_s))
    i1 = min(n, max(i0, int(round(hard * sr))))
    seg = np.array(audio[i0:i1], copy=True)
    if len(seg) == 0:
        return seg, t0, t0
    env = np.max(np.abs(seg), axis=1)
    peak = float(env.max()) if len(env) else 0.0
    end = len(seg)
    if peak > 0:
        win = max(1, int(round(0.05 * sr)))
        blocks = len(env) // win
        if blocks:
            levels = env[: blocks * win].reshape(blocks, win).max(axis=1)
            thresh = peak * (10 ** (float(ring_db) / 20.0))
            below = np.flatnonzero(levels < thresh)
            if len(below):
                end = max(1, int(below[0] * win))
    seg = seg[: min(end, len(seg))]
    _apply_fade(seg, sr, fade_s, attack_s)
    t1 = t0 + (len(seg) / sr if sr else 0.0)
    return seg, t0, t1


def _apply_fade(seg, sr, fade_s, attack_s) -> None:
    n = len(seg)
    if n == 0:
        return
    fade = min(n, int(round(float(fade_s) * sr)))
    if fade > 1:
        seg[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)[:, None]
    attack = min(n, int(round(float(attack_s) * sr)))
    if attack > 1:
        seg[:attack] *= np.linspace(0.0, 1.0, attack, dtype=np.float32)[:, None]


def strict_regions(mono, sr, drop_db=55.0, min_note_s=0.30, rise_db=12.0, hop_s=0.010):
    """Onsets for a chromatic run, including a quiet note after a loud one.

    The index splits on silence 40 dB below the file peak and glues any gap
    shorter than 0.35 s, so a quiet pitch disappears or two pitches become
    one. A new note here is a rise of `rise_db` above the quietest moment in
    the previous 0.30 s. A bump during a ringing note does not clear that
    bar, because the ring itself is the recent minimum. The pitch of each
    region is decided later, and a neighbour is never moved into a hole.
    """
    mono = np.asarray(mono, dtype=np.float64).reshape(-1)
    hop = max(1, int(round(hop_s * sr)))
    n = len(mono) // hop
    if n < 8:
        return []
    frames = mono[: n * hop].reshape(n, hop)
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-18)
    db = 20.0 * np.log10(rms)
    # Smooth over 30 ms so a single waveform cycle is not an onset.
    smooth = max(1, int(round(0.030 / hop_s)))
    kernel = np.ones(smooth) / smooth
    level = np.convolve(db, kernel, mode="same")
    look = max(2, int(round(0.30 / hop_s)))
    debounce = max(1, int(round(0.25 / hop_s)))
    onsets = []
    for i in range(look, n):
        floor = float(np.min(level[i - look:i]))
        if float(level[i]) < floor + rise_db:
            continue
        if onsets and i - onsets[-1] < debounce:
            continue
        j = i
        limit = onsets[-1] if onsets else 0
        while j > limit and float(level[j - 1]) > floor + rise_db * 0.4:
            j -= 1
        if onsets and j <= onsets[-1]:
            continue
        onsets.append(int(j))
    head = float(np.max(level[:look]))
    if head > -45.0 and (not onsets or onsets[0] > debounce):
        onsets.insert(0, 0)
    if not onsets:
        return []
    persist = max(2, int(round(0.040 / hop_s)))
    regions = []
    for k, start_i in enumerate(onsets):
        stop_i = onsets[k + 1] if k + 1 < len(onsets) else n
        seg = level[start_i:stop_i]
        peak = float(np.max(seg))
        end_i = stop_i
        run = 0
        for t, val in enumerate(seg):
            if float(val) < peak - drop_db:
                run += 1
                if run >= persist:
                    end_i = start_i + t - persist + 1
                    break
            else:
                run = 0
        end_i = min(stop_i, max(end_i, start_i + 1))
        t0 = start_i * hop / float(sr)
        t1 = end_i * hop / float(sr)
        if t1 - t0 >= min_note_s:
            regions.append((t0, t1))
    return regions


def strict_accept(meas: dict, lo: int, hi: int) -> bool:
    """A recovery needs a confident pYIN reading of this exact note.

    `pitch.accept` can pass on the FFT peak alone, and it allows 60 cents of
    spread. Recovery does not take that FFT-only rescue. pYIN has to name the
    measured midi within 15 cents, and the spread has to stay inside 45 cents.
    The midi it returns is the one that was measured. The caller must not
    point that midi at a different hole.

    Guitar partials pull `pitch.fft_peak` (a window of ±0.6 semitone) about
    20–30 cents off a fundamental pYIN has already named. Requiring that peak
    inside 15 cents rejected notes the index would keep, including an E3 at
    +2 cents. The FFT peak rejects a reading only when it disagrees by more
    than 40 cents. A missing FFT peak does not reject a confident pYIN reading.
    """
    midi = meas.get("midi") if isinstance(meas, dict) else None
    if midi is None or not (int(lo) <= int(midi) <= int(hi)):
        return False
    cents = meas.get("cents")
    spread = meas.get("spread_cents")
    if cents is None or spread is None:
        return False
    if abs(float(cents)) > 15 or float(spread) > SPREAD_CENTS:
        return False
    fft_cents = meas.get("fft_cents")
    if fft_cents is not None and abs(float(fft_cents)) > FFT_DISAGREE_CENTS:
        return False
    return True


def identify_region(audio, sr, lo: int, hi: int):
    """The pitch in this region. `accepted` is the strict gate.

    A reading that names the right note but fails the gate comes back with
    `accepted` false, so the gap report can say whether it was the cents,
    the spread, or the FFT peak. It is not stored as a note, and it is not
    moved onto a neighbour.
    """
    from . import pitch
    y = np.asarray(audio, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    if len(y) < int(0.30 * sr):
        return None
    if hi - lo <= SPAN_WINDOW:
        centre = int(round((int(lo) + int(hi)) / 2))
        meas = pitch.measure(y, expect_midi=centre, sr=int(sr))
    else:
        meas = pitch.measure(y, expect_midi=None, sr=int(sr))
    if meas.get("midi") is None or meas.get("cents") is None:
        return None
    midi = int(meas["midi"])
    if not (int(lo) <= midi <= int(hi)):
        return None
    return {
        "midi": midi,
        "cents": float(meas["cents"]),
        "fft_cents": None if meas.get("fft_cents") is None else float(meas["fft_cents"]),
        "spread_cents": None if meas.get("spread_cents") is None else float(meas["spread_cents"]),
        "accepted": strict_accept(meas, lo, hi),
    }


def choose_segments(options, expected: int):
    """Pick the silence split closest to the number of pitches in the filename.

    `options` is `(drop_db, segments)`. The index always used 40 dB. A run
    whose count matches the filename is preferred, whichever depth produced
    it. Segments shorter than 0.30 s are dropped before the count.
    """
    ranked = []
    for drop, segs in options:
        kept = [(float(a), float(b)) for a, b in segs if float(b) - float(a) >= 0.30]
        ranked.append((abs(len(kept) - int(expected)), abs(float(drop) - 50.0), float(drop), kept))
    ranked.sort()
    return ranked[0][3], ranked[0][2]


def assign_recoveries(measurements, holes):
    """Keep a measurement only when its own midi is a hole and the strict gate passed.

    Two readings of the same midi keep the one closer to the note. A reading
    of F3 is never stored as E3. A reading with `accepted` false is a near
    miss, not a note.
    """
    wanted = {int(m) for m in holes}
    best = {}
    for row in measurements:
        if row.get("accepted") is False:
            continue
        midi = int(row["midi"])
        if midi not in wanted:
            continue
        prev = best.get(midi)
        if prev is None or abs(float(row["cents"])) < abs(float(prev["cents"])):
            best[midi] = dict(row)
    return best


def absent_reason(midi: int, near: dict | None, where: str = "the Iowa file") -> str:
    tail = "A neighbour was not moved into the hole."
    if not near or int(near.get("midi", -1)) != int(midi) or near.get("cents") is None:
        return (
            f"Scanned {where} that covers this pitch. The strict onset and pitch check "
            "did not accept it, and a neighbour was not moved into the hole."
        )
    name = note_name(midi)
    cents = float(near["cents"])
    spread = near.get("spread_cents")
    fft_cents = near.get("fft_cents")
    if abs(cents) > 15:
        return (
            f"Scanned {where}. The note measures as {name} "
            f"at {cents:+.0f} cents, outside the 15 cent gate, so it was not kept. {tail}"
        )
    if spread is not None and float(spread) > SPREAD_CENTS:
        return (
            f"Scanned {where}. The note measures as {name} at {cents:+.0f} cents, "
            f"but the pitch wanders by {float(spread):.0f} cents, past the {SPREAD_CENTS:.0f} cent spread gate, "
            f"so it was not kept. {tail}"
        )
    if fft_cents is not None and abs(float(fft_cents)) > FFT_DISAGREE_CENTS:
        return (
            f"Scanned {where}. pYIN measures {name} at {cents:+.0f} cents, "
            f"but the FFT peak is {float(fft_cents):+.0f} cents from that note, so it was not kept. {tail}"
        )
    return (
        f"Scanned {where} that covers this pitch. The strict onset and pitch check "
        "did not accept it, and a neighbour was not moved into the hole."
    )


def classify_gaps(spec_lo, spec_hi, page_covered, scanned_covered, sliced):
    """Split the spec range into absent, not on the page, and not opened.

    `absent` means a file we opened spans that pitch and no region measured
    as it. `not_in_sources` means no published filename spans it. A pitch
    that is on the page but in a file we did not open is `unscanned`, so the
    report does not call it missing.
    """
    page = {int(m) for m in page_covered}
    scanned = {int(m) for m in scanned_covered}
    have = {int(m) for m in sliced}
    absent, missing_files, unscanned = [], [], []
    for midi in range(int(spec_lo), int(spec_hi) + 1):
        if midi in have:
            continue
        if midi in scanned:
            absent.append(midi)
        elif midi not in page:
            missing_files.append(midi)
        else:
            unscanned.append(midi)
    return absent, missing_files, unscanned


def tuning_cents(audio, sr, midi: int):
    """FFT offset of this slice from equal temperament, in cents. No resample."""
    from . import pitch
    y = np.asarray(audio, dtype=np.float64)
    if y.ndim == 2:
        y = y.mean(axis=1)
    start = int(0.03 * sr)
    frame = y[start:start + int(1.2 * sr)]
    if len(frame) < 2048:
        frame = y
    if len(frame) < 1024:
        return None
    fc = pitch.fft_peak(frame, int(sr), int(midi))
    if fc is None:
        return None
    return round(float(fc), 1)


def encode_commands(wav: str, flac: str, opus: str) -> tuple[list[str], list[str]]:
    """FLAC and Opus commands. No sample-rate flag, no stretch, no pitch filter.

    libopus stores 48 kHz frames. That conversion keeps the pitch and the
    duration. The FLAC master stays at the source rate.
    """
    flac_cmd = ["ffmpeg", "-v", "error", "-y", "-i", wav, "-c:a", "flac", "-compression_level", "8", flac]
    opus_cmd = ["ffmpeg", "-v", "error", "-y", "-i", wav, "-c:a", "libopus", "-b:a", OPUS_BITRATE, "-application", "audio", opus]
    return flac_cmd, opus_cmd


def encode_pair(audio, sr, flac_path: str, opus_path: str) -> None:
    import soundfile as sf
    os.makedirs(os.path.dirname(flac_path), exist_ok=True)
    wav = flac_path + ".wav"
    sf.write(wav, np.asarray(audio, dtype=np.float32), int(sr), subtype="PCM_16")
    flac_cmd, opus_cmd = encode_commands(wav, flac_path, opus_path)
    try:
        subprocess.run(flac_cmd, check=True)
        subprocess.run(opus_cmd, check=True)
    finally:
        if os.path.exists(wav):
            os.remove(wav)


def measure_loudness(audio, sr):
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    peak_db = round(20.0 * np.log10(peak + 1e-12), 1)
    lufs = None
    if len(audio) >= int(0.40 * sr):
        try:
            import pyloudnorm as pyln
            meter = pyln.Meter(int(sr))
            value = float(meter.integrated_loudness(np.asarray(audio, dtype=np.float64)))
            if np.isfinite(value):
                lufs = round(value, 1)
        except Exception:
            lufs = None
    return lufs, peak_db


def read_native(path: str):
    """Decode at the file's own rate. Sample-rate flags are not passed."""
    import soundfile as sf
    try:
        audio, sr = sf.read(path, always_2d=True, dtype="float32")
        return audio, int(sr)
    except Exception:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate,channels", "-of", "json", path],
            capture_output=True, text=True, check=True,
        )
        stream = json.loads(probe.stdout)["streams"][0]
        sr = int(stream["sample_rate"])
        channels = int(stream.get("channels") or 1)
        cmd = ["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-ac", str(channels), "-"]
        raw = subprocess.run(cmd, capture_output=True, check=True).stdout
        audio = np.frombuffer(raw, dtype=np.float32)
        if channels > 1:
            audio = audio.reshape(-1, channels)
        else:
            audio = audio.reshape(-1, 1)
        return audio, sr


def _schema_doc() -> dict:
    with open(_SCHEMA, encoding="utf-8") as fh:
        return json.load(fh)


def _type_ok(value, kind: str) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    return isinstance(value, _TYPES[kind])


def _check(value, spec, path, errs, root) -> None:
    if "$ref" in spec:
        spec = root["$defs"][spec["$ref"].split("/")[-1]]
    if "const" in spec and value != spec["const"]:
        errs.append(f"{path}: expected {spec['const']!r}")
        return
    kinds = spec.get("type")
    if kinds:
        options = kinds if isinstance(kinds, list) else [kinds]
        if not any(_type_ok(value, kind) for kind in options):
            errs.append(f"{path}: expected {kinds}")
            return
    if "enum" in spec and value not in spec["enum"]:
        errs.append(f"{path}: {value!r} is not allowed")
    if isinstance(value, str):
        if "minLength" in spec and len(value) < spec["minLength"]:
            errs.append(f"{path}: too short")
        if "pattern" in spec and re.search(spec["pattern"], value) is None:
            errs.append(f"{path}: does not match {spec['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in spec and value < spec["minimum"]:
            errs.append(f"{path}: {value} < {spec['minimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            errs.append(f"{path}: {value} > {spec['maximum']}")
    if isinstance(value, dict):
        for key in spec.get("required", []):
            if key not in value:
                errs.append(f"{path}: missing {key}")
        props = spec.get("properties") or {}
        extra = spec.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                _check(item, props[key], f"{path}.{key}", errs, root)
            elif extra is False:
                errs.append(f"{path}: unexpected {key}")
            elif isinstance(extra, dict):
                _check(item, extra, f"{path}.{key}", errs, root)
    if isinstance(value, list):
        if "minItems" in spec and len(value) < spec["minItems"]:
            errs.append(f"{path}: fewer than {spec['minItems']} items")
        item_spec = spec.get("items")
        if item_spec:
            for i, item in enumerate(value):
                _check(item, item_spec, f"{path}[{i}]", errs, root)


def validate_manifest(doc: dict) -> list[str]:
    """Structural checks from the schema, then the license and pitch rules."""
    from . import licenses as LIC
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["manifest is not an object"]
    _check(doc, _schema_doc(), "$", errs, _schema_doc())
    if errs:
        return errs
    kind = doc.get("kind")
    if kind == "notes":
        for field in ("notes", "range", "gaps", "recovered"):
            if field not in doc:
                errs.append(f"$: missing {field}")
        for key, note in (doc.get("notes") or {}).items():
            if str(note.get("midi")) != str(key):
                errs.append(f"notes.{key}: key does not match midi")
            if note.get("source", {}).get("t1", 0) < note.get("source", {}).get("t0", 0):
                errs.append(f"notes.{key}: source ends before it starts")
            if float(note.get("dur_s") or 0) > RING_CAP_S + 1e-3:
                errs.append(f"notes.{key}: longer than the {RING_CAP_S:.0f} s cap")
    elif kind == "chords":
        if "chords" not in doc:
            errs.append("$: missing chords")
        for key, chord in (doc.get("chords") or {}).items():
            if chord.get("name") != key:
                errs.append(f"chords.{key}: key does not match name")
    else:
        errs.append(f"kind: {kind}")
    license_in = doc.get("license_in") or ""
    parsed = LIC.from_id(license_in)[0]
    if not LIC.allowed(parsed):
        errs.append(f"license_in: {license_in} is not CC0, CC BY, CC BY-SA, or public domain")
    if doc.get("license_out") != LIC.OUT["id"]:
        errs.append("license_out: output is CC BY-SA 4.0")
    play = doc.get("playback") or {}
    if play.get("octave_substitution") is not False or play.get("missing") != "omit":
        errs.append("playback: a missing note is left out, not replaced")
    if play.get("pitch_shift") is not False or play.get("time_stretch") is not False:
        errs.append("playback: pitch and duration stay as recorded")
    return errs


def _source(url: str, t0: float, t1: float, license_id: str) -> dict:
    return {
        "url": url,
        "t0": round(float(t0), 4),
        "t1": round(float(t1), 4),
        "license": license_id,
    }


def make_note(slug: str, midi: int, audio, sr: int, source: dict, cents: float, method: str, recovered: bool = False) -> dict:
    lufs, peak_db = measure_loudness(audio, sr)
    channels = int(audio.shape[1]) if np.asarray(audio).ndim == 2 else 1
    note = {
        "midi": int(midi),
        "name": note_name(midi),
        "file": note_key(slug, midi),
        "master": master_key(slug, midi),
        "cents": round(float(cents), 1),
        "cents_method": method,
        "dur_s": round(len(audio) / float(sr), 3),
        "lufs": lufs,
        "peak_db": peak_db,
        "sr": int(sr),
        "channels": channels,
        "source": source,
    }
    if recovered:
        note["recovered"] = True
    return note


def make_chord(slug: str, symbol: str, audio, sr: int, source: dict) -> dict:
    lufs, peak_db = measure_loudness(audio, sr)
    return {
        "name": symbol,
        "file": chord_key(slug, symbol),
        "master": chord_master_key(slug, symbol),
        "dur_s": round(len(audio) / float(sr), 3),
        "lufs": lufs,
        "peak_db": peak_db,
        "sr": int(sr),
        "source": source,
    }


def _gap(midi: int, status: str, reason: str, files: list[str] | None = None) -> dict:
    row = {"midi": int(midi), "name": note_name(midi), "status": status, "reason": reason}
    if files:
        row["files"] = files
    return row


def build_note_manifest(spec: dict, notes: dict, gaps: list, recovered: list) -> dict:
    require_license(spec["license"])
    slug = instrument_slug(spec["instrument"])
    mids = sorted(int(m) for m in notes)
    doc = {
        "format": FORMAT,
        "kind": "notes",
        "id": spec["id"],
        "instrument": spec["instrument"],
        "slug": slug,
        "sustain": "plucked" if spec.get("pluck") else "rings",
        "velocity": list(spec.get("velocity") or ["mf"]),
        "range": {
            "lo": mids[0] if mids else int(spec["lo"]),
            "hi": mids[-1] if mids else int(spec["hi"]),
        },
        "credit": credit_line(spec.get("author") or "", spec.get("page") or "", spec["license"]),
        "license_in": spec["license"],
        "license_out": "CC-BY-SA-4.0",
        "page": spec.get("page") or "",
        "author": spec.get("author") or "",
        "playback": default_playback(),
        "notes": {str(midi): notes[midi] for midi in mids},
        "gaps": gaps,
        "recovered": recovered,
    }
    errs = validate_manifest(doc)
    if errs:
        raise ValueError("manifest does not validate: " + "; ".join(errs))
    return doc


def build_chord_manifest(spec: dict, chords: dict) -> dict:
    """A chord instrument: whole recordings keyed by chord symbol, played as-is."""
    require_license(spec["license"])
    slug = instrument_slug(spec["instrument"])
    doc = {
        "format": FORMAT,
        "kind": "chords",
        "id": spec["id"],
        "instrument": spec["instrument"],
        "slug": slug,
        "sustain": "plucked" if spec.get("pluck") else "rings",
        "velocity": list(spec.get("velocity") or ["mf"]),
        "credit": credit_line(spec.get("author") or "", spec.get("page") or "", spec["license"]),
        "license_in": spec["license"],
        "license_out": "CC-BY-SA-4.0",
        "page": spec.get("page") or "",
        "author": spec.get("author") or "",
        "playback": default_playback(),
        "chords": chords,
    }
    errs = validate_manifest(doc)
    if errs:
        raise ValueError("manifest does not validate: " + "; ".join(errs))
    return doc


def parse_sfz_regions(text: str, sample_base: str) -> list[dict]:
    """One native-pitch sample per MIDI from an SFZ map.

    A zone whose key is not the recorded pitch is dropped. Round robins after
    the first are dropped, because the manifest stores one file per MIDI.
    Sample paths are resolved from `sample_base` (the program directory, for
    a map that is included from that program). Nothing is transposed.
    """
    found = []
    seen = set()
    for region in re.split(r"(?i)<region>", text or "")[1:]:
        def grab(key: str, region: str = region):
            match = re.search(rf"(?i)\b{key}=(-?\d+)", region)
            return int(match.group(1)) if match else None
        lo, hi = grab("lokey"), grab("hikey")
        centre, seq = grab("pitch_keycenter"), grab("seq_position")
        sample_m = re.search(r"(?i)sample=([^\r\n]+)", region)
        if lo is None or hi is None or lo != hi or not sample_m:
            continue
        if centre is not None and centre != lo:
            continue
        if seq not in (None, 1):
            continue
        sample = sample_m.group(1).strip().replace("\\", "/")
        url = urllib.parse.urljoin(sample_base, sample)
        if url in seen:
            continue
        seen.add(url)
        found.append({"url": url, "name": sample.rsplit("/", 1)[-1], "lo": int(lo), "hi": int(hi)})
    return found


def sfz_note_sources(url: str, sample_base: str) -> list[dict]:
    from . import fetch
    text, _ = fetch.text(url)
    return parse_sfz_regions(text, sample_base)


_NAMED_NOTE = re.compile(r"_([A-G](?:#|b)?)(-?\d)(?:_|$)")


def named_note_sources(spec: dict) -> list[dict]:
    """Wav files whose names carry the MIDI pitch. No pitch is invented."""
    from . import fetch, notemap
    text, _ = fetch.text(spec["github_contents"])
    doc = json.loads(text)
    if not isinstance(doc, list):
        raise RuntimeError("note listing was not a file list")
    rx = re.compile(spec.get("file_re") or ".")
    found = []
    seen = set()
    for item in doc:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        url = str(item.get("download_url") or "")
        if not url or not name.lower().endswith((".wav", ".aif", ".aiff", ".flac")):
            continue
        if not rx.search(name):
            continue
        match = _NAMED_NOTE.search(name)
        if not match:
            continue
        midi = notemap._midi(match.group(1), match.group(2))
        if url in seen:
            continue
        seen.add(url)
        found.append({"url": url, "name": name, "lo": int(midi), "hi": int(midi)})
    return found


def list_sources(spec: dict) -> list[dict]:
    """Files whose names give a pitch span. One note per file stays one note."""
    kind = spec.get("kind")
    if kind == "sfz_note":
        return sfz_note_sources(spec["sfz_url"], spec["sample_base"])
    if kind == "named_note":
        return named_note_sources(spec)
    from . import notemap
    found = []
    seen = set()
    rx = re.compile(spec["file_re"])
    for url in notemap._uiowa_files(spec["page"]):
        name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        if "mono" in name.lower() or not rx.search(name):
            continue
        span = notemap._span(url)
        if not span or url in seen:
            continue
        seen.add(url)
        found.append({"url": url, "name": name, "lo": int(span[0]), "hi": int(span[1])})
    return found


def _norm(url: str) -> str:
    return urllib.parse.unquote(url or "").split("#", 1)[0]


def _region(audio, sr, t0, t1):
    i0 = max(0, int(float(t0) * sr))
    i1 = min(len(audio), max(i0 + 1, int(float(t1) * sr)))
    return audio[i0:i1]


def _cents_for(seg, sr, midi, index_cents):
    measured = tuning_cents(seg, sr, midi)
    if measured is None:
        if index_cents is None:
            return 0.0, "index"
        return round(float(index_cents), 1), "index"
    return measured, "fft"


def _write_audio(out_dir: str, note: dict, seg, sr) -> dict:
    flac = os.path.join(out_dir, note["master"])
    opus = os.path.join(out_dir, note["file"])
    encode_pair(seg, sr, flac, opus)
    return {"opus": os.path.getsize(opus), "flac": os.path.getsize(flac), "sr": int(sr), "dur_s": note["dur_s"]}


def native_decision(audio, sr, midi: int) -> dict:
    """Pitch check for a file that is already one note.

    pYIN has to pass the slicer's strict gate. When pYIN does not name this
    note, the FFT cents `tuning_cents` records for an indexed note can keep
    it, still within 15 cents. The audio is not resampled or retuned, and a
    reading of a different MIDI is not moved onto this one.
    """
    midi = int(midi)
    found = identify_region(audio, sr, midi, midi)
    if found and found.get("accepted") is True and int(found["midi"]) == midi:
        return {"ok": True, "midi": midi, "cents": round(float(found["cents"]), 1), "method": "pyin", "near": found}
    cents = tuning_cents(audio, sr, midi)
    if cents is not None and abs(float(cents)) <= 15:
        return {"ok": True, "midi": midi, "cents": round(float(cents), 1), "method": "fft", "near": found}
    return {"ok": False, "midi": midi, "cents": None, "method": None, "near": found}


def accept_native(audio, sr, midi: int):
    row = native_decision(audio, sr, midi)
    if not row["ok"]:
        return None
    return row


def _onset_s(audio, sr) -> float:
    """Where the note starts. A quiet lead-in is not the note.

    Same threshold the pitch check uses: the first sample above 5% of the
    peak. The slice then keeps the usual 5 ms before that.
    """
    y = np.asarray(audio)
    mono = y.mean(axis=1) if y.ndim == 2 else y
    if len(mono) == 0:
        return 0.0
    level = np.abs(mono)
    peak = float(level.max())
    if peak <= 0:
        return 0.0
    return float(int(np.argmax(level > 0.05 * peak)) / float(sr))


def _take_native(spec, item, audio, sr, slug, out_dir, notes, near_miss, sizes) -> None:
    """Keep this file only when `accept_native` names its own MIDI.

    Two files of the same MIDI keep the one closer to the note.
    The other file is not moved onto a missing pitch.
    """
    midi = int(item["lo"])
    reading = native_decision(audio, sr, midi)
    found = reading.get("near")
    if not reading["ok"]:
        if midi not in notes and found and int(found.get("midi", -1)) == midi:
            prev = near_miss.get(midi)
            if prev is None or abs(float(found["cents"])) < abs(float(prev.get("cents") or 99)):
                near_miss[midi] = found
        return
    cents = round(float(reading["cents"]), 1)
    prev = notes.get(midi)
    if prev is not None and abs(float(prev["cents"])) <= abs(cents):
        return
    seg, t0, t1 = slice_note(audio, sr, _onset_s(audio, sr), None)
    if len(seg) < int(0.05 * sr):
        return
    record = make_note(slug, midi, seg, sr, _source(item["url"], t0, t1, spec["license"]), cents, reading["method"])
    _write_audio(out_dir, record, seg, sr)
    notes[midi] = record
    row = {
        "midi": midi,
        "name": record["name"],
        "opus": os.path.getsize(os.path.join(out_dir, record["file"])),
        "flac": os.path.getsize(os.path.join(out_dir, record["master"])),
        "dur_s": record["dur_s"],
        "recovered": False,
    }
    for i, prev_row in enumerate(sizes):
        if prev_row["midi"] == midi:
            sizes[i] = row
            return
    sizes.append(row)


def slice_instrument(spec: dict, library: dict | None, out_dir: str, recover: bool = True, log=print) -> dict:
    """Slice one Iowa library. Holes that survive the rescan are recorded."""
    require_license(spec["license"])
    slug = instrument_slug(spec["instrument"])
    index_notes = {}
    for midi, note in ((library or {}).get("notes") or {}).items():
        if isinstance(note, dict) and note.get("cents") is not None:
            index_notes[int(midi)] = note
    files = list_sources(spec)
    known = {_norm(item["url"]) for item in files}
    for midi, note in index_notes.items():
        url = note.get("audio_url") or note.get("url") or ""
        if url and _norm(url) not in known:
            files.append({"url": url, "name": urllib.parse.unquote(url.rsplit("/", 1)[-1]), "lo": midi, "hi": midi})
            known.add(_norm(url))
    page_covered = set()
    spans: dict[int, list[str]] = {}
    for item in files:
        for midi in range(item["lo"], item["hi"] + 1):
            page_covered.add(midi)
            spans.setdefault(midi, []).append(item["name"])

    # A chromatic run is opened in full so a hole inside it can be recovered.
    # A one-note file (piano) is opened only when the index already uses it.
    # The rest of the keyboard stays unscanned and is not reported as absent.
    # `native` libraries are one file per note and have no index. Each file in
    # the spec range is opened and kept only when the strict pitch check names
    # that file's own MIDI.
    chromatic = any(item["hi"] > item["lo"] for item in files)
    native = bool(spec.get("native"))

    def covers_index(item: dict) -> bool:
        return any(item["lo"] <= midi <= item["hi"] for midi in index_notes)

    def in_spec(item: dict) -> bool:
        return int(spec["lo"]) <= int(item["lo"]) <= int(spec["hi"]) and item["lo"] == item["hi"]

    if native:
        wanted = [item for item in files if in_spec(item)]
    elif chromatic and recover:
        wanted = files
    else:
        wanted = [item for item in files if covers_index(item)]

    paths, errors = _prefetch([item["url"] for item in wanted], log, spec["id"])
    notes = {}
    recovered_rows = []
    near_miss = {}
    scanned_covered = set()
    source_bytes = {}
    sizes = []
    self_n = self_ok = 0

    for item in wanted:
        path = paths.get(item["url"])
        if not path:
            continue
        try:
            audio, sr = read_native(path)
        except Exception as exc:
            errors.append(f"{item['name']}: {exc}")
            continue
        source_bytes[item["url"]] = os.path.getsize(path)
        for midi in range(item["lo"], item["hi"] + 1):
            scanned_covered.add(midi)
        here = [(midi, note) for midi, note in index_notes.items() if _norm(note.get("audio_url") or note.get("url") or "") == _norm(item["url"])]
        for midi, note in here:
            if midi in notes:
                continue
            onset = float(note.get("onset_s") or 0.0)
            length = note.get("length_s")
            region_end = None if not length else onset + float(length)
            seg, t0, t1 = slice_note(audio, sr, onset, region_end)
            if len(seg) < int(0.05 * sr):
                errors.append(f"{item['name']} midi {midi}: slice too short")
                continue
            cents, method = _cents_for(seg, sr, midi, note.get("cents"))
            record = make_note(slug, midi, seg, sr, _source(item["url"], t0, t1, spec["license"]), cents, method)
            _write_audio(out_dir, record, seg, sr)
            notes[midi] = record
            sizes.append({"midi": midi, "name": record["name"], "opus": os.path.getsize(os.path.join(out_dir, record["file"])), "flac": os.path.getsize(os.path.join(out_dir, record["master"])), "dur_s": record["dur_s"], "recovered": False})
            if recover and self_n < 8:
                raw = _region(audio, sr, onset, region_end or onset + RING_CAP_S)
                found = identify_region(raw, sr, item["lo"], item["hi"])
                self_n += 1
                if found and found.get("accepted") and found["midi"] == midi:
                    self_ok += 1
        if native and item["lo"] == item["hi"]:
            _take_native(spec, item, audio, sr, slug, out_dir, notes, near_miss, sizes)
        if recover and not (native and item["lo"] == item["hi"]):
            holes = [midi for midi in range(item["lo"], item["hi"] + 1) if midi not in notes and midi not in index_notes]
            if holes:
                from . import pitch
                mono = audio.mean(axis=1) if audio.ndim == 2 else audio
                expected = item["hi"] - item["lo"] + 1
                options = [(drop, pitch.segment_run(mono, sr, drop_db=drop)) for drop in (35, 40, 45, 50, 55, 60)]
                regions, drop_used = choose_segments(options, expected)
                measurements = []
                for t0, t1 in regions:
                    found = identify_region(_region(audio, sr, t0, min(t1, t0 + 2.8)), sr, item["lo"], item["hi"])
                    if not found:
                        continue
                    found["t0"] = t0
                    found["t1"] = t1
                    found["file"] = item["name"]
                    found["drop_db"] = drop_used
                    measurements.append(found)
                    if not found["accepted"] and found["midi"] in holes:
                        prev = near_miss.get(found["midi"])
                        if prev is None or abs(found["cents"]) < abs(prev["cents"]):
                            near_miss[found["midi"]] = found
                chosen = assign_recoveries(measurements, holes)
                for midi, found in sorted(chosen.items()):
                    seg, t0, t1 = slice_note(audio, sr, found["t0"], found["t1"])
                    if len(seg) < int(0.05 * sr):
                        continue
                    cents = round(float(found["cents"]), 1)
                    record = make_note(slug, midi, seg, sr, _source(item["url"], t0, t1, spec["license"]), cents, "pyin", recovered=True)
                    _write_audio(out_dir, record, seg, sr)
                    notes[midi] = record
                    near_miss.pop(midi, None)
                    recovered_rows.append({
                        "midi": midi,
                        "name": note_name(midi),
                        "cents": round(float(found["cents"]), 1),
                        "file": item["name"],
                        "t0": round(float(found["t0"]), 4),
                    })
                    sizes.append({"midi": midi, "name": record["name"], "opus": os.path.getsize(os.path.join(out_dir, record["file"])), "flac": os.path.getsize(os.path.join(out_dir, record["master"])), "dur_s": record["dur_s"], "recovered": True})
        del audio
        log(json.dumps({"event": "sliced_file", "id": spec["id"], "file": item["name"], "notes": len(notes)}))

    absent, not_in, unscanned = classify_gaps(spec["lo"], spec["hi"], page_covered, scanned_covered, notes)
    where = "the Iowa file" if spec.get("kind") == "uiowa_mis" else "the source file"
    gaps = [_gap(midi, "absent", absent_reason(midi, near_miss.get(midi), where), spans.get(midi, [])[:6]) for midi in absent]
    if spec.get("kind") == "uiowa_mis":
        layer = (spec.get("velocity") or ["mf"])[0]
        missing_reason = f"No {layer} file on the Iowa page has this pitch in its name."
    else:
        missing_reason = "No source file is named as this pitch."
    gaps += [_gap(midi, "not-in-sources", missing_reason) for midi in not_in]
    manifest = build_note_manifest(spec, notes, gaps, recovered_rows)
    dest = os.path.join(out_dir, "manifests", slug + ".json")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    report = {
        "id": spec["id"],
        "instrument": spec["instrument"],
        "slug": slug,
        "manifest": dest,
        "notes": len(notes),
        "recovered": [row["name"] for row in recovered_rows],
        "absent": [note_name(midi) for midi in absent],
        "not_in_sources": [note_name(midi) for midi in not_in],
        "unscanned": [note_name(midi) for midi in unscanned],
        "self_check": {"indexed": self_n, "agreed": self_ok},
        "source_bytes": sum(source_bytes.values()),
        "source_files": len(source_bytes),
        "sizes": sizes,
        "warnings": errors,
    }
    log(json.dumps({"event": "instrument", "id": spec["id"], "notes": len(notes), "recovered": report["recovered"], "absent": report["absent"], "not_in_sources": report["not_in_sources"], "self_check": report["self_check"], "warnings": len(errors)}))
    return report


def _prefetch(urls: list[str], log, library_id: str) -> tuple[dict, list]:
    from . import fetch
    paths, errors = {}, []

    def one(url: str):
        ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or ".aiff"
        return url, fetch.download(url, ext=ext)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(one, url) for url in urls]
        for fut in as_completed(futures):
            try:
                url, path = fut.result()
            except Exception as exc:
                errors.append(str(exc)[:200])
                continue
            paths[url] = path
            log(json.dumps({"event": "fetched", "id": library_id, "file": urllib.parse.unquote(url.rsplit("/", 1)[-1]), "bytes": os.path.getsize(path)}))
    return paths, errors


def _assert_terms() -> None:
    from . import fetch
    import html as html_lib
    page, _ = fetch.text(fetch.IOWA_TERMS_URL)
    if fetch.IOWA_TERMS not in html_lib.unescape(page):
        raise RuntimeError("UIowa MIS terms not found on the live page; not using it")


def run_slice(out_dir: str, libraries: list[str] | None = None, index_path: str | None = None, recover: bool = True, log=print) -> dict:
    """Slice the named libraries. Remaining holes do not fail the run."""
    from . import noteindex, notemap
    path = index_path or os.environ.get("NC_NOTE_INDEX") or noteindex.BUNDLED
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    by_id = {lib.get("id"): lib for lib in doc.get("libraries") or [] if isinstance(lib, dict)}
    wanted = list(libraries or DEFAULT_LIBRARIES)
    kinds = {"uiowa_mis", "sfz_note", "named_note"}
    by_spec = {spec["id"]: spec for spec in notemap._specs() if spec.get("kind") in kinds}
    missing = [name for name in wanted if name not in by_spec]
    specs = [by_spec[name] for name in wanted if name in by_spec]
    if missing:
        raise SystemExit("unknown library: " + ", ".join(missing))
    if any(spec.get("kind") == "uiowa_mis" for spec in specs):
        _assert_terms()
    os.makedirs(out_dir, exist_ok=True)
    reports = []
    for spec in specs:
        spec = dict(spec)
        if not spec.get("velocity"):
            spec["velocity"] = ["mf"]
        reports.append(slice_instrument(spec, by_id.get(spec["id"]), out_dir, recover=recover, log=log))
    summary = _size_summary(reports)
    with open(os.path.join(out_dir, "gap-report.json"), "w", encoding="utf-8") as fh:
        json.dump(reports, fh, indent=1)
        fh.write("\n")
    with open(os.path.join(out_dir, "sizes.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
        fh.write("\n")
    log(json.dumps({"event": "sizes", "rows": summary}))
    return {"reports": reports, "sizes": summary}


def _size_summary(reports: list[dict]) -> list[dict]:
    rows = []
    for report in reports:
        sizes = report.get("sizes") or []
        opus = [row["opus"] for row in sizes]
        flac = [row["flac"] for row in sizes]
        rows.append({
            "instrument": report["instrument"],
            "slug": report["slug"],
            "notes": len(sizes),
            "recovered": len(report.get("recovered") or []),
            "absent": len(report.get("absent") or []),
            "not_in_sources": len(report.get("not_in_sources") or []),
            "opus_bytes": int(sum(opus)),
            "flac_bytes": int(sum(flac)),
            "opus_avg": int(sum(opus) / len(opus)) if opus else 0,
            "opus_min": int(min(opus)) if opus else 0,
            "opus_max": int(max(opus)) if opus else 0,
            "flac_avg": int(sum(flac) / len(flac)) if flac else 0,
            "source_bytes": int(report.get("source_bytes") or 0),
            "source_files": int(report.get("source_files") or 0),
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Slice native-pitch Iowa notes into Opus 96k and FLAC. Does not upload.")
    parser.add_argument("-o", "--out", required=True, help="Directory for manifests and notes/. Nothing is published.")
    parser.add_argument("--index", help="note-index.json (default: the bundled index)")
    parser.add_argument("--library", action="append", default=[], help="Library id. Repeatable. Default: guitar, piano, cello pizzicato.")
    parser.add_argument("--no-recover", action="store_true", help="Slice the index only; do not rescan for holes.")
    args = parser.parse_args(argv)
    run_slice(args.out, libraries=args.library or None, index_path=args.index, recover=not args.no_recover)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
