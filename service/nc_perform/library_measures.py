"""
The Library's measurements, in one place: decode, trim, measure, hit level, playback gain and the waveform outline.

seed_vcsl.py, level_match.py and publish.py import these, and nc-performer's harvester container runs a verbatim copy
(service/nc_perform/library_measures.py) so a harvested sound and a VCSL sound are measured the same way and library.json
stays one format. Change this file and that copy together; MEASURES_VERSION and the golden test
(tools/library/test_measures.py, service/tests/test_library.py) catch a drift.

Needs: ffmpeg, numpy, soundfile, pyloudnorm.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pyloudnorm
import soundfile as sf

MEASURES_VERSION = "1"
SR = 48000
CEILING_DB = -1.0
WAVE_COLUMNS = 48
DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _channels(path: Path) -> int:
    """Channel count: libsndfile first (WAV, FLAC, AIFF, OGG, MP3), ffprobe for anything it can't open (M4A)."""
    try:
        return int(sf.info(str(path)).channels)
    except Exception:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels",
             "-of", "default=nw=1:nk=1", str(path)],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        return int(out.splitlines()[0])


def decode(path: Path) -> np.ndarray:
    """Decode to 48 kHz float32 stereo-or-mono (frames x channels) with ffmpeg's soxr resampler."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-af", "aresample=resampler=soxr:precision=28",
         "-ar", str(SR), "-f", "f32le", "-acodec", "pcm_f32le", "-"],
        check=True, capture_output=True,
    ).stdout
    channels = _channels(Path(path))
    ch = min(2, channels)
    data = np.frombuffer(out, dtype=np.float32).reshape(-1, channels)[:, :ch]
    return data.copy()


def envelope_db(mono: np.ndarray, hop: int = 48, window: int = 480) -> np.ndarray:
    """RMS level in dB over a 10 ms window, one value per 1 ms (smooth enough for low drums' long cycles)."""
    sq = np.concatenate([np.zeros(window), mono.astype(np.float64) ** 2])
    csum = np.cumsum(sq)
    idx = np.arange(window, len(sq), hop)
    rms = np.sqrt(np.maximum((csum[idx] - csum[idx - window]) / window, 1e-18))
    return 20 * np.log10(np.maximum(rms, 1e-9))


def trim(data: np.ndarray) -> tuple[np.ndarray, int]:
    mono = np.abs(data).max(axis=1)
    peak = float(mono.max())
    if peak <= 0:
        return data[:0], 0
    above = np.nonzero(mono > peak * 10 ** (-40 / 20))[0]
    start = max(0, int(above[0]) - int(0.002 * SR))
    tail = np.nonzero(mono > peak * 10 ** (-60 / 20))[0]
    end = min(len(data), int(tail[-1]) + int(0.02 * SR))
    out = data[start:end].copy()
    fade = min(len(out), int(0.01 * SR))
    if fade > 0:
        out[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)[:, None]
    return out, start


def measure(data: np.ndarray, note: str | None) -> dict:
    mono = data.mean(axis=1)
    n = len(mono)
    length = n / SR
    peak = float(np.abs(data).max())
    env = envelope_db(mono)  # 1 ms frames
    ipk = int(np.argmax(env)) if len(env) else 0
    # Attack: from the start (2 ms before the onset) to the first 1 ms frame within 3 dB of the peak sample.
    hop = 48
    frames = np.abs(mono[: (n // hop) * hop]).reshape(-1, hop).max(axis=1) if n >= hop else np.abs(mono)
    reach = np.nonzero(frames >= frames.max() * 10 ** (-3 / 20))[0] if len(frames) else np.array([0])
    attack_ms = float(reach[0])  # frames are 1 ms
    after = np.nonzero(env[ipk:] < env[ipk] - 30)[0]
    decay_s = float(after[0]) / 1000 if len(after) else length - ipk / 1000
    meter = pyloudnorm.Meter(SR, block_size=min(0.4, max(0.05, length)))
    try:
        lufs = float(meter.integrated_loudness(data if data.shape[1] > 1 else mono))
    except Exception:
        lufs = float("nan")
    head = mono[: min(n, int(0.3 * SR))]
    spec = np.abs(np.fft.rfft(head * np.hanning(len(head)))) if len(head) > 16 else np.zeros(2)
    freqs = np.fft.rfftfreq(len(head), 1 / SR) if len(head) > 16 else np.zeros(2)
    power = spec**2
    total = float(power.sum()) or 1.0
    centroid = float((freqs * power).sum() / total)
    low_share = float(power[freqs < 150].sum() / total)
    out = {
        "length_s": round(length, 3),
        "lufs": None if not np.isfinite(lufs) else round(lufs, 1),
        "peak_dbfs": round(20 * np.log10(max(peak, 1e-9)), 1),
        "attack_ms": round(attack_ms, 1),
        "decay_s": round(decay_s, 3),
        "centroid_hz": round(centroid),
        "low_share": round(low_share, 3),
    }
    if note:
        out["note"] = note
    else:
        band = (freqs > 30) & (freqs < 400)
        if band.any() and low_share > 0.05:
            out["pitch_hz"] = round(float(freqs[band][np.argmax(spec[band])]), 1)
    return out


def measure_tags(role: str, m: dict) -> set[str]:
    """The tags a sound earns from its measures (length, brightness, punch, tightness), the same for every source."""
    t: set[str] = set()
    if m["length_s"] < 0.3:
        t.add("short")
    if m["length_s"] > 1.5:
        t.add("long")
    if m["centroid_hz"] > 4000:
        t.add("bright")
    elif m["centroid_hz"] < 800:
        t.add("dark")
    if role in {"kick", "tom"}:
        if m["attack_ms"] <= 10 and m["decay_s"] < 0.6:
            t.add("punchy")
        if m["decay_s"] >= 0.9:
            t.add("boomy")
        if m.get("pitch_hz", 999) < 70:
            t.add("deep")
    if role in {"snare", "clap", "hat", "perc"}:
        if m["decay_s"] < 0.2:
            t.add("tight")
        if m["attack_ms"] <= 4 and m["centroid_hz"] > 2500:
            t.add("snappy")
    return t


def hit_db_data(data: np.ndarray, sr: int) -> tuple[float, float]:
    """(loudest 50 ms RMS in dBFS, sample peak in dBFS) of float samples (frames x channels)."""
    data = np.asarray(data, dtype=np.float64)
    mono = data.mean(axis=1)
    win = max(1, int(0.05 * sr))
    sq = np.concatenate([np.zeros(win), mono**2])
    csum = np.cumsum(sq)
    rms = np.sqrt(np.maximum((csum[win:] - csum[:-win]) / win, 1e-20))
    peak = float(np.abs(data).max())
    return 20 * np.log10(max(float(rms.max()), 1e-10)), 20 * np.log10(max(peak, 1e-10))


def hit_db(path: Path) -> tuple[float, float]:
    """(loudest 50 ms RMS in dBFS, sample peak in dBFS) of an audio file."""
    data, sr = sf.read(str(path), dtype="float64", always_2d=True)
    return hit_db_data(data, sr)


def playback_gain(family_loudest_hit_db: float, peak_db: float, target: float = -14.0) -> tuple[float, bool]:
    """(gain_db, limited_by_peak): the family's loudest hit to the target, never pushing this peak past CEILING_DB."""
    family_gain = target - family_loudest_hit_db
    gain = min(family_gain, CEILING_DB - peak_db)
    limited = gain < family_gain - 0.05
    return round(gain, 1), limited


def playback_method(target: float = -14.0) -> str:
    return ("family level match: loudest 50 ms RMS of the family's loudest sound to "
            f"{target:g} dBFS; peak kept under {CEILING_DB:g} dBFS")


def wave_data(data: np.ndarray) -> str:
    """The sound's outline for a mini waveform: WAVE_COLUMNS columns of the loudest |sample| (channels mixed), scaled to
    the sound's own peak, one base-36 digit each (0 silent .. z the peak)."""
    mono = np.abs(np.asarray(data, dtype=np.float32).mean(axis=1))
    if not len(mono):
        return "0" * WAVE_COLUMNS
    cols = np.array_split(mono, WAVE_COLUMNS)
    top = max(float(mono.max()), 1e-9)
    return "".join(DIGITS[min(35, round(float(c.max() if len(c) else 0) / top * 35))] for c in cols)


def wave(path: Path) -> str:
    data, _ = sf.read(str(path), always_2d=True, dtype="float32")
    return wave_data(data)
