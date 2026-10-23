"""Play sliced notes in the order a chord writes them.

Each note is the recording of that MIDI pitch. Playback stays at the file's
own rate: this mixer does not resample. A written pitch that the instrument
does not have is left out and named. Another octave of the same pitch class
is not played in its place. When the next chord arrives, the ring fades across
120 ms. The slice itself is already capped at 8 seconds.
"""
from __future__ import annotations

import json
import os
import subprocess

import numpy as np

from .slicer import CHORD_FADE_S, note_name

MIX_SR = 48000
RING_TAIL = 8.0
# The three listen checks. The guitar and piano pitches are the ones the
# prototype check wrote down. The cello pitches are the catalog's root
# voicing. None of these lists is rewritten when a pitch is missing.
CHECKS = (
    {
        "slug": "guitar",
        "bpm": 96,
        "spread": 0.018,
        "name": "guitar-em7-a-x4",
        "line": "[Em7 A]x4",
        "events": [("Em7", [64, 67, 71, 74], 4), ("A", [57, 61, 64], 4)] * 4,
    },
    {
        "slug": "piano",
        "bpm": 96,
        "spread": 0.008,
        "name": "piano-cm2-fm9-gm7-8-x2",
        "line": "[Cm(2) Fm9 Gm7(8)]x2",
        "events": [("Cm", [60, 63, 67], 2), ("Fm9", [53, 56, 60, 63, 67], 4), ("Gm7", [55, 58, 62, 65], 8)] * 2,
    },
    {
        "slug": "cello-pizzicato",
        "bpm": 96,
        "spread": 0.015,
        "name": "cello-cm-fm-gm-x2",
        "line": "[Cm Fm Gm]x2",
        "events": [("Cm", [48, 51, 55], 4), ("Fm", [53, 56, 60], 4), ("Gm", [43, 46, 50], 4)] * 2,
    },
)


def select_written(written, available):
    """Play the written midi notes that exist. Do not look an octave away."""
    have = {int(midi) for midi in available}
    play, missing = [], []
    for midi in written:
        midi = int(midi)
        if midi in have:
            play.append(midi)
        else:
            missing.append(midi)
    return play, missing


def _join_names(midis) -> str:
    names = [note_name(midi) for midi in midis]
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + ", and " + names[-1]


def plain_missing(instrument: str, chord: str, missing, played) -> str:
    """One sentence a page can show when a written note is not on the instrument."""
    if not missing:
        return ""
    miss = _join_names(missing)
    if played:
        return f"{instrument} has no {miss}; {chord} plays {_join_names(played)}"
    return f"{instrument} has no {miss}; {chord} plays without them"


def level_gains(notes: dict) -> dict[int, float]:
    values = [float(note["lufs"]) for note in notes.values() if note.get("lufs") is not None]
    if not values:
        return {int(midi): 1.0 for midi in notes}
    median = float(np.median(np.asarray(values, dtype=np.float64)))
    gains = {}
    for midi, note in notes.items():
        if note.get("lufs") is None:
            gains[int(midi)] = 1.0
            continue
        db = float(np.clip(median - float(note["lufs"]), -3.0, 3.0))
        gains[int(midi)] = float(10 ** (db / 20.0))
    return gains


def render(manifest: dict, events, bpm: float, load, sr: int = MIX_SR, spread: float = 0.0, fade_s: float = CHORD_FADE_S):
    """Mix `events` of (symbol, written midis, beats). `load(midi)` returns (n, 2) at `sr`.

    `load` is called only for written pitches the manifest has. The buffer it
    returns is placed sample for sample. A longer ring is faded across
    `fade_s` at the chord change; a shorter recording ends on its own.
    """
    if int(sr) != MIX_SR:
        raise ValueError("the mix rate is 48000 Hz")
    sr = int(sr)
    notes = manifest.get("notes") or {}
    gains = level_gains(notes)
    beat = 60.0 / float(bpm)
    total = sum(float(beats) for _, _, beats in events) * beat + RING_TAIL
    buf = np.zeros((int(total * sr) + 8, 2), dtype=np.float64)
    messages = []
    seen = set()
    t = 0.0
    cache = {}
    for symbol, written, beats in events:
        play, missing = select_written(written, notes)
        sentence = plain_missing(manifest.get("instrument") or manifest.get("slug") or "instrument", symbol, missing, play)
        if sentence and sentence not in seen:
            seen.add(sentence)
            messages.append(sentence)
        hold = float(beats) * beat
        for i, midi in enumerate(play):
            if midi not in cache:
                audio = np.asarray(load(midi), dtype=np.float64)
                if audio.ndim != 2 or audio.shape[1] != 2:
                    raise ValueError(f"midi {midi}: expected stereo audio at the mix rate")
                cache[midi] = audio
            audio = cache[midi]
            start = t + i * float(spread)
            keep = int(round((hold - i * float(spread) + float(fade_s)) * sr))
            if keep <= 8:
                continue
            piece = np.array(audio[:keep], copy=True)
            if len(piece) == keep:
                fade_n = min(len(piece), int(round(float(fade_s) * sr)))
                if fade_n > 1:
                    piece[-fade_n:] *= np.linspace(1.0, 0.0, fade_n)[:, None]
            at = int(round(start * sr))
            end = min(len(buf), at + len(piece))
            if at < len(buf) and end > at:
                buf[at:end] += piece[: end - at] * gains.get(midi, 1.0)
        t += hold
    peak = float(np.max(np.abs(buf))) if len(buf) else 0.0
    if peak > 0:
        buf *= (10 ** (-1.0 / 20.0)) / peak
    last = int(round((t + 0.2) * sr))
    nz = np.flatnonzero(np.max(np.abs(buf), axis=1) > 1e-5)
    end = max(last, int(nz[-1]) + 1 if len(nz) else last)
    return buf[:end].astype(np.float32), messages


def load_opus(path: str, sr: int = MIX_SR):
    """Decode an Opus note to 48 kHz stereo. Opus frames are already 48 kHz.

    Asking the decoder for another rate would still be sample-rate conversion,
    not a pitch change, but this path only asks for the codec rate.
    """
    if int(sr) != MIX_SR:
        raise ValueError("opus notes are decoded at 48000 Hz")
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-ac", "2", "-ar", str(MIX_SR), "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    if not raw:
        raise RuntimeError(f"no audio in {path}")
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).copy()


def load_manifest(notes_dir: str, slug: str) -> dict:
    path = os.path.join(notes_dir, "manifests", slug + ".json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def render_checks(notes_dir: str, out_dir: str, log=print) -> list[dict]:
    """Write the three 96 BPM listen checks. Missing notes are named beside the wav."""
    import soundfile as sf
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for check in CHECKS:
        manifest = load_manifest(notes_dir, check["slug"])
        cache = {}

        def load(midi, manifest=manifest, cache=cache, notes_dir=notes_dir):
            if midi not in cache:
                rel = manifest["notes"][str(midi)]["file"]
                cache[midi] = load_opus(os.path.join(notes_dir, rel))
            return cache[midi]

        audio, messages = render(manifest, check["events"], check["bpm"], load, spread=check["spread"])
        wav = os.path.join(out_dir, check["name"] + ".wav")
        sf.write(wav, audio, MIX_SR, subtype="PCM_24")
        side = {
            "line": check["line"],
            "bpm": check["bpm"],
            "instrument": manifest.get("instrument"),
            "slug": check["slug"],
            "wav": wav,
            "seconds": round(len(audio) / MIX_SR, 3),
            "messages": messages,
        }
        with open(os.path.join(out_dir, check["name"] + ".json"), "w", encoding="utf-8") as fh:
            json.dump(side, fh, indent=1)
            fh.write("\n")
        log(json.dumps({"event": "check", **{k: side[k] for k in ("line", "slug", "seconds", "messages")}}))
        written.append(side)
    return written


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Render the three listen checks from sliced notes. No pitch shift.")
    parser.add_argument("--notes", required=True, help="Directory written by nc slice")
    parser.add_argument("-o", "--out", required=True)
    args = parser.parse_args(argv)
    render_checks(args.notes, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
