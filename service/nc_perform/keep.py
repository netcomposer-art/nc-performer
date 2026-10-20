"""Processed Keep: a browser-rendered WAV with the Live dials.

A JSON ``{claim_token}`` keep still stores nothing new and leaves the original
audio in place. A multipart keep checks this WAV, stores it as ``kept.wav``,
and records the dials on ``meta.json``. The signed ``record.json`` is never
rewritten.

Size cap: 40 MiB (41,943,040 bytes). The longest hosted piece is 180 s of
music plus the 30 s tail allowed here, which is 210 s. Stereo 16-bit PCM at
48 kHz is 192,000 bytes/s, so 210 s is 40,320,000 bytes plus a 44-byte header
and fits in 40 MiB. The create page sends that encoding. Stereo 24-bit at the
same length is about 60.5 MB and does not fit; a short 24-bit file under the
cap is still accepted.
"""
from __future__ import annotations

import json
import os
import re
import struct

from .limits import Reject

KEPT_WAV = "kept.wav"
KEPT_MP3 = "kept.mp3"
# Original render, plus the dialed files a processed keep may add.
AUDIO_FILES = ("performance.flac", "preview.mp3", KEPT_WAV, KEPT_MP3)
KEPT_TAIL_S = 30.0
# 40 MiB. See the module docstring. Confirm with Petrus before raising it.
KEPT_WAV_MAX_BYTES = 40 * 1024 * 1024
# claim_token, dials JSON, and multipart headers around the WAV.
KEPT_FORM_OVERHEAD = 64 * 1024
# Hosted music cap, used when the record does not say how long the piece is.
DEFAULT_JOB_LENGTH_S = 180.0
# Frame-count rounding. A file that lands within this of the limit is kept.
DURATION_EPS_S = 0.05
AFTER_SIGNING = (
    "Live dials were applied in the browser after the record was signed. "
    "The signed record is the original render."
)


class WavInfo:
    def __init__(self, sample_rate: int, channels: int, bits: int, frames: int, duration_s: float):
        self.sample_rate = sample_rate
        self.channels = channels
        self.bits = bits
        self.frames = frames
        self.duration_s = duration_s


def too_large_message(n: int) -> str:
    cap = KEPT_WAV_MAX_BYTES
    label = f"{cap // (1024 * 1024)} MiB" if cap % (1024 * 1024) == 0 else f"{cap} bytes"
    return f"The dialed audio is too large ({n} bytes). The limit is {cap} bytes ({label})."


def job_length_s(record: dict | None) -> float:
    """Music length from the signed record, else the first output duration, else 180 s."""
    perf = (record or {}).get("performance") or {}
    music = perf.get("music_ends_at_s")
    if _positive(music):
        return float(music)
    for output in perf.get("outputs") or []:
        if isinstance(output, dict) and _positive(output.get("duration_s")):
            return float(output["duration_s"])
    return DEFAULT_JOB_LENGTH_S


def inspect_wav(blob: bytes, max_duration_s: float) -> WavInfo:
    """Accept PCM WAV at 44.1 or 48 kHz, mono or stereo, 16- or 24-bit.

    ``max_duration_s`` is the job length plus ``KEPT_TAIL_S``. The data chunk
    must be present and must fit in the file.
    """
    if len(blob) < 12 or blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise Reject("audio is not a PCM WAV.")
    offset = 12
    fmt = b""
    data_size = None
    while offset + 8 <= len(blob):
        size = struct.unpack_from("<I", blob, offset + 4)[0]
        start = offset + 8
        end = start + size
        if end > len(blob):
            raise Reject("WAV data is truncated.")
        chunk_id = blob[offset:offset + 4]
        if chunk_id == b"fmt " and not fmt:
            fmt = blob[start:end]
        elif chunk_id == b"data" and data_size is None:
            data_size = size
        offset = end + (size & 1)
        if offset > len(blob):
            # A chunk with an odd size may omit the pad byte at end of file.
            break
    if len(fmt) < 16 or data_size is None:
        raise Reject("audio is not a PCM WAV.")
    audio_format, channels, rate, byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", fmt)
    if audio_format != 1:
        raise Reject("WAV must be PCM (format 1).")
    if channels not in (1, 2):
        raise Reject("WAV must be mono or stereo.")
    if bits not in (16, 24):
        raise Reject("WAV must be 16-bit or 24-bit PCM.")
    if rate not in (44100, 48000):
        raise Reject("WAV sample rate must be 44100 or 48000 Hz.")
    expect_block = channels * (bits // 8)
    if block_align != expect_block or byte_rate != rate * expect_block:
        raise Reject("WAV header is inconsistent.")
    if data_size % block_align != 0:
        raise Reject("WAV data is truncated.")
    frames = data_size // block_align
    if frames < 1:
        raise Reject("WAV has no audio frames.")
    duration = frames / float(rate)
    if duration > max_duration_s + DURATION_EPS_S:
        raise Reject(
            f"The dialed audio is {duration:.1f}s. "
            f"This Performance allows at most {max_duration_s:.1f}s "
            f"(the job length plus {KEPT_TAIL_S:.0f}s of tail)."
        )
    return WavInfo(rate, channels, bits, frames, duration)


def parse_dials(raw) -> dict:
    """The Live dials object the create page puts in the ``dials`` field."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise Reject("dials must be JSON.") from e
    if isinstance(raw, str):
        try:
            obj = json.loads(raw or "")
        except json.JSONDecodeError as e:
            raise Reject("dials must be JSON.") from e
    elif isinstance(raw, dict):
        obj = raw
    else:
        raise Reject("dials must be a JSON object.")
    if not isinstance(obj, dict):
        raise Reject("dials must be a JSON object.")

    def num(name: str, lo: float, hi: float) -> float:
        if name not in obj:
            raise Reject(f"dials.{name} is required.")
        value = obj[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Reject(f"dials.{name} must be a number.")
        if value < lo or value > hi:
            raise Reject(f"dials.{name} must be between {lo:g} and {hi:g}.")
        return value

    cutoff_hz = obj.get("cutoff_hz", None)
    if cutoff_hz is not None:
        if isinstance(cutoff_hz, bool) or not isinstance(cutoff_hz, (int, float)):
            raise Reject("dials.cutoff_hz must be a number or null.")
        if cutoff_hz < 20 or cutoff_hz > 24000:
            raise Reject("dials.cutoff_hz must be between 20 and 24000, or null when the filter is open.")
    timing = obj.get("delay_timing")
    if not isinstance(timing, str) or not timing.strip() or len(timing) > 40:
        raise Reject("dials.delay_timing must be a short string.")
    if "loop" not in obj or not isinstance(obj["loop"], bool):
        raise Reject("dials.loop must be true or false.")
    return {
        "cutoff": num("cutoff", 0, 100),
        "cutoff_hz": None if cutoff_hz is None else float(cutoff_hz) if not isinstance(cutoff_hz, int) else cutoff_hz,
        "delay_timing": timing,
        "delay_beats": num("delay_beats", 0, 8),
        "delay_s": num("delay_s", 0, 5),
        "feedback": num("feedback", 0, 0.95),
        "wet": num("wet", 0, 1),
        "loop": obj["loop"],
    }


def dials_line(dials: dict) -> str:
    hz = dials.get("cutoff_hz")
    hz_s = "filter open" if hz is None else f"{hz:g} Hz"
    loop = "on" if dials.get("loop") else "off"
    return (
        f"cutoff {dials.get('cutoff'):g} ({hz_s}), "
        f"delay {dials.get('delay_timing')} ({dials.get('delay_beats'):g} beats, {dials.get('delay_s'):g} s), "
        f"feedback {dials.get('feedback'):g}, wet {dials.get('wet'):g}, loop {loop}"
    )


def live_audio_meta(info: WavInfo, has_mp3: bool) -> dict:
    return {
        "wav": KEPT_WAV,
        "mp3": KEPT_MP3 if has_mp3 else None,
        "sample_rate": info.sample_rate,
        "channels": info.channels,
        "bits_per_sample": info.bits,
        "duration_s": round(info.duration_s, 3),
        "note": AFTER_SIGNING,
    }


def kept_files(meta: dict, base: str, job_id: str) -> dict | None:
    """Extra job-status fields when this Performance has a dialed keep."""
    dials = meta.get("live_dials")
    if not isinstance(dials, dict):
        return None
    files = {"kept_wav": f"{base}/api/jobs/{job_id}/files/{KEPT_WAV}"}
    if (meta.get("live_audio") or {}).get("mp3"):
        files["kept_mp3"] = f"{base}/api/jobs/{job_id}/files/{KEPT_MP3}"
    return {"files": files, "live_dials": dials, "kept_note": AFTER_SIGNING}


def kept_feed(meta: dict, base: str, job_id: str) -> dict:
    extra = kept_files(meta, base, job_id)
    if not extra:
        return {}
    out = {
        "live_dials": extra["live_dials"],
        "kept_audio": extra["files"]["kept_wav"],
        "note": AFTER_SIGNING,
    }
    if "kept_mp3" in extra["files"]:
        out["kept_preview"] = extra["files"]["kept_mp3"]
    return out


def parse_multipart(body: bytes, content_type: str) -> dict[str, bytes]:
    """Read form fields without eating trailing bytes of the WAV.

    The CRLF before each boundary is the delimiter, so exactly one is removed.
    A sample that happens to end in newline bytes stays in the file.
    """
    found = re.search(r"boundary=([^;]+)", content_type, re.I)
    if not found:
        raise Reject("Keep the dialed audio as multipart/form-data.")
    boundary = found.group(1).strip().strip('"').encode()
    marker = b"--" + boundary
    positions = []
    start = 0
    while True:
        at = body.find(marker, start)
        if at < 0:
            break
        positions.append(at)
        start = at + len(marker)
    parts: dict[str, bytes] = {}
    for i, pos in enumerate(positions):
        chunk = body[pos + len(marker): positions[i + 1] if i + 1 < len(positions) else len(body)]
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith(b"\n"):
            chunk = chunk[1:]
        sep = chunk.find(b"\r\n\r\n")
        width = 4
        if sep < 0:
            sep = chunk.find(b"\n\n")
            width = 2
        if sep < 0:
            continue
        head, data = chunk[:sep], chunk[sep + width:]
        if data.endswith(b"\r\n"):
            data = data[:-2]
        elif data.endswith(b"\n"):
            data = data[:-1]
        if b"Content-Disposition" not in head:
            continue
        name_m = re.search(br'name="([^"]+)"', head)
        if not name_m:
            continue
        parts[name_m.group(1).decode()] = data
    return parts


def encode_kept_mp3(wav: bytes) -> bytes | None:
    """192 kbps preview. None when ffmpeg is missing or the encode fails.

    The Worker cannot run ffmpeg, so hosted Keep stores the WAV only. This
    mirror writes ``kept.mp3`` when the encode works.
    """
    import shutil
    import subprocess
    import tempfile

    if shutil.which("ffmpeg") is None:
        return None
    d = tempfile.mkdtemp(prefix="nc-kept-")
    try:
        src = os.path.join(d, "kept.wav")
        dst = os.path.join(d, "kept.mp3")
        with open(src, "wb") as f:
            f.write(wav)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src,
                 "-codec:a", "libmp3lame", "-b:a", "192k", dst],
                check=True, capture_output=True, timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None
        if not os.path.exists(dst) or os.path.getsize(dst) < 1:
            return None
        with open(dst, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
