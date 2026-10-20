"""Shape one discovered take: ensemble, ring, phrase, bed, and a seeded surprise.

Nothing here pitch-shifts or time-stretches. Durations, overlaps, rolls, and
which already-legal samples play together are the only choices.
"""
from __future__ import annotations

import random
import re
import zlib

from . import chords as CH

# Same ceiling as max_chords. The old plan stopped at 12.
CHORD_LIMIT = 40

_SPACE_BED = re.compile(
    r"\b(space|air|rooms?|ambience|ambient|atmosphere|field|rain|wind|wash|pads?)\b"
    r"|\bin the distance\b|\bdistant\b",
    re.I,
)
_GAP = re.compile(r"\b(space|air between|breath|pause between)\b", re.I)
_DISTANCE = re.compile(r"\b(distance|distant|far away)\b", re.I)
_SOFT = re.compile(r"ambient|soft|calm|slow|gentle|dark|no harsh", re.I)
_EVEN = re.compile(r"\b(no swells|evenly lit|keep it even|steady|uniform)\b", re.I)
_REPEAT = re.compile(
    r"repeat the same|same sounds|over and over|one instrument|the same way|don'?t vary|do not vary",
    re.I,
)
_VARY = re.compile(r"\bvary\b|different each|each chord different|contrast", re.I)
_TWICE = re.compile(
    r"played twice|twice per bar|every figure twice|gesture twice|states every figure twice",
    re.I,
)
_ROLL = re.compile(r"\b(rolls?|strums?|arpeggios?|arpeggiated)\b", re.I)
_PLUCK = re.compile(r"\b(pluck(?:ed|ing)?|pizz(?:icato)?|guitar|harp)\b", re.I)
_LAYER = re.compile(
    r"\blayers?\s+(?:a\s+|an\s+)?(.+?)\s+with\s+(?:a\s+|an\s+)?(.+?)(?:[.]|$)",
    re.I,
)
_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def bar_chords(text: str) -> list[tuple] | None:
    """Chords from a bar table, in bar order. Prose mentions are left out."""
    found: dict[int, tuple] = {}
    for line in (text or "").splitlines():
        raw = line.strip()
        if not raw.startswith("|"):
            continue
        cells = [c.strip() for c in raw.strip("|").split("|")]
        i = 0
        while i + 1 < len(cells):
            bar, chord = cells[i], cells[i + 1]
            i += 2
            if not bar.isdigit():
                continue
            hit = CH.find_full(chord)
            if not hit:
                continue
            found[int(bar)] = hit[0]
    if len(found) < 8 or 1 not in found:
        return None
    bars = sorted(found)
    if bars[-1] < 8 or len(bars) < bars[-1] * 0.9:
        return None
    return [found[b] for b in bars]


def expand_sequence(found: list, length: float) -> tuple[list, str | None]:
    """Repeat a progression so the piece fills its length, then stop at 40."""
    if not found:
        return [], None
    reps = max(1, round(float(length) / (len(found) * 9.0)))
    full = list(found) * reps
    note = None
    if len(full) > CHORD_LIMIT:
        note = (
            f"The piece had {len(full)} chords; it was shortened to {CHORD_LIMIT} "
            "so the Performance stays within the chord limit."
        )
        full = full[:CHORD_LIMIT]
    return full, note


def arrangement_mode(words: str, kn: dict | None = None) -> str:
    """'orderly' when the words demand a steady repeat. Otherwise 'free'."""
    text = words or ""
    kn = kn or {}
    if _REPEAT.search(text):
        return "orderly"
    if _VARY.search(text):
        return "free"
    if kn.get("even") or _EVEN.search(text):
        return "orderly"
    return "free"


def phrase(words: str) -> dict:
    """What the words change. 'Open' alone is reverb, not a bed and not a gap."""
    text = words or ""
    then = re.search(r"\bthen\b", text, re.I)
    layer = _LAYER.search(text)
    return {
        "bed": bool(_SPACE_BED.search(text)),
        "gap": bool(_GAP.search(text)),
        "distance": bool(_DISTANCE.search(text)),
        "soft": bool(_SOFT.search(text)),
        "even": bool(_EVEN.search(text)),
        "twice": bool(_TWICE.search(text)),
        "roll_words": bool(_ROLL.search(text)),
        "pluck_words": bool(_PLUCK.search(text)),
        "then_at": then.start() if then else None,
        "layer": (layer.group(1).strip(" .,"), layer.group(2).strip(" .,")) if layer else None,
    }


def prefer_from(text: str) -> list[str]:
    low = (text or "").lower()
    out: list[str] = []
    if _PLUCK.search(low):
        out.append("pluck")
    if "fat" in low and "synth" in low:
        out.append("fat synth")
    for word in ("choir", "piano", "organ", "flute", "strings", "violin", "cello", "guitar", "harp", "pad", "synth"):
        if word not in out and re.search(rf"\b{word}", low):
            out.append(word)
    return out


def matches(opt: dict, word: str) -> bool:
    inst = (opt.get("instrument") or "").lower()
    if word in ("pluck", "pizz", "plucked"):
        return bool(opt.get("pluck")) or any(part in inst for part in ("pizz", "guitar", "harp", "pluck"))
    if word == "fat synth":
        return "fat" in inst and "synth" in inst
    if word in ("synth", "arp"):
        return "synth" in inst or inst == "arp"
    if word in ("choir", "voice"):
        return "choir" in inst or inst in ("choir", "voice", "chorus")
    if word == "piano":
        return inst == "piano"
    return word == inst or (word in inst and word != "")


def matches_any(opt: dict, words: list[str] | None) -> bool:
    return any(matches(opt, word) for word in (words or []))


def _key(opt: dict) -> tuple:
    return (opt.get("origin") or "", opt.get("instrument") or "", opt.get("built") or "")


def choose(groups: list[list[dict]], seed: int, prefer: list[str] | None, hint: str | None, long: bool, avoid: tuple | None = None) -> tuple[list, tuple | None]:
    """One ensemble for these chords. Seed breaks ties so the next take can differ.

    An ensemble is one origin, one instrument, and one way of building the chord.
    Full coverage wins. Words and the instrument hint rank next. A long piece
    prefers assembled notes so pitches are reused instead of one file per chord.
    """
    n = len(groups)
    by_key: dict[tuple, dict[int, dict]] = {}
    for i, group in enumerate(groups):
        best: dict[tuple, dict] = {}
        for opt in group:
            key = _key(opt)
            if avoid and key == avoid:
                continue
            prev = best.get(key)
            if prev is None or int(opt.get("tier") or 9) < int(prev.get("tier") or 9):
                best[key] = opt
        for key, opt in best.items():
            by_key.setdefault(key, {})[i] = opt
    if not by_key:
        return [None] * n, None

    def rank(key: tuple):
        covered = by_key[key]
        sample = next(iter(covered.values()))
        origin, inst, built = key
        missing = n - len(covered)
        word = 0 if matches_any(sample, prefer) else 1
        if hint:
            hint_rank = 0 if matches(sample, hint) else 1
        else:
            hint_rank = 0
        if long:
            kind_rank = 0 if built == "assembled notes" else 1
        else:
            kind_rank = 0
        tier = min(int(opt.get("tier") or 9) for opt in covered.values())
        tie = zlib.adler32(f"{seed}|{origin}|{inst}|{built}".encode()) % 100000
        return (0 if missing == 0 else 1, missing, word, hint_rank, kind_rank, tier, tie)

    keys = sorted(by_key, key=rank)
    primary = keys[0]
    chosen: list = [by_key[primary].get(i) for i in range(n)]
    if any(row is None for row in chosen):
        for key in keys[1:]:
            for i in range(n):
                if chosen[i] is None and i in by_key[key]:
                    chosen[i] = by_key[key][i]
            if all(row is not None for row in chosen):
                break
    return chosen, primary


def _coverage(groups: list[list[dict]], avoid: tuple | None = None) -> dict[tuple, dict[int, dict]]:
    by_key: dict[tuple, dict[int, dict]] = {}
    for i, group in enumerate(groups):
        best: dict[tuple, dict] = {}
        for opt in group:
            key = _key(opt)
            if avoid and key == avoid:
                continue
            prev = best.get(key)
            if prev is None or int(opt.get("tier") or 9) < int(prev.get("tier") or 9):
                best[key] = opt
        for key, opt in best.items():
            by_key.setdefault(key, {})[i] = opt
    return by_key


def _rank_key(key, covered, n, seed, prefer, hint, long):
    sample = next(iter(covered.values()))
    origin, inst, built = key
    missing = n - len(covered)
    word = 0 if matches_any(sample, prefer) else 1
    hint_rank = 0 if not hint or matches(sample, hint) else 1
    if long:
        kind_rank = 0 if built == "assembled notes" else 1
    else:
        kind_rank = 0
    tier = min(int(opt.get("tier") or 9) for opt in covered.values())
    tie = zlib.adler32(f"{seed}|{origin}|{inst}|{built}".encode()) % 100000
    return (0 if missing == 0 else 1, missing, word, hint_rank, kind_rank, tier, tie)


def _best_named(by_key: dict, words: list[str], n: int, seed: int, prefer, hint, long):
    """The recording of this named voice that covers the most chords. Partial cover is allowed."""
    hits = []
    for key, covered in by_key.items():
        sample = next(iter(covered.values()))
        if matches_any(sample, words):
            hits.append(key)
    if not hits:
        return None

    def sort_key(key: tuple):
        covered = by_key[key]
        missing = n - len(covered)
        return (0 if missing == 0 else 1, missing, _rank_key(key, covered, n, seed, prefer, hint, long))

    hits.sort(key=sort_key)
    return hits[0]


def named_coverage(members: list[dict], symbols: list, word_lists: list[list[str]] | None) -> list[str]:
    """Plain sentences for a named voice that could not play every chord. No invented pitches."""
    notes = []
    lists = [words for words in (word_lists or []) if words]
    for words in lists:
        label = " ".join(words)
        owners = [
            member for member in members
            if any(opt and matches_any(opt, words) for opt in (member.get("opts") or []))
        ]
        if not owners:
            notes.append(f"No {label} recording covered these chords, so that voice is not in the ensemble.")
            continue
        missing = []
        for i, pair in enumerate(symbols):
            if not any(
                i < len(member.get("opts") or []) and member["opts"][i] and matches_any(member["opts"][i], words)
                for member in owners
            ):
                missing.append(pair[0])
        if missing:
            who = owners[0].get("label") or owners[0].get("instrument") or label
            notes.append(
                f"{who} did not cover {', '.join(missing)}. "
                "Those chords stay with the rest of the ensemble. Nothing was pitch-shifted to fill them."
            )
    extras = []
    for member in members:
        opts = [opt for opt in (member.get("opts") or []) if opt]
        if opts and not any(matches_any(opt, words) for opt in opts for words in lists):
            extras.append(member.get("label") or member.get("instrument") or "another recording")
    if extras:
        notes.append(f"{', '.join(extras)} was added only where the named voices had no recording.")
    return notes


def palette(groups: list[list[dict]], seed: int, prefer: list[str] | None, hint: str | None, long: bool, mode: str, required: list[list[str]] | None = None) -> list[dict]:
    """The cast for this take. Orderly keeps one member. Free keeps a few who can play the chords.

    Extras stay with the anchor's origin when that origin has more than one instrument.
    Named timbres ('layer a pluck with a synth') are the cast, even when one of them
    cannot play every chord. A chord that voice cannot play is left to the other voice.
    """
    n = len(groups)
    by_key = _coverage(groups)
    if not by_key:
        return []
    keys = sorted(by_key, key=lambda key: _rank_key(key, by_key[key], n, seed, prefer, hint, long))

    def full(key: tuple) -> bool:
        return len(by_key[key]) == n

    def pack(key: tuple) -> dict:
        opts = [by_key[key].get(i) for i in range(n)]
        sample = next(opt for opt in opts if opt)
        return {
            "key": key,
            "origin": key[0],
            "instrument": key[1],
            "built": key[2],
            "opts": opts,
            "label": f"{key[1]} ({key[0]})",
        }

    if required:
        picked = []
        for words in required:
            if not words:
                continue
            hit = _best_named(by_key, words, n, seed, prefer, hint, long)
            if hit and hit not in picked:
                picked.append(hit)
        if picked:
            return [pack(key) for key in picked[:3]]
        return []

    anchor = keys[0]
    if prefer:
        named = [key for key in keys if full(key) and matches_any(next(iter(by_key[key].values())), prefer)]
        if named:
            anchor = named[0]
    members = [pack(anchor)]
    if mode == "orderly":
        return members

    same = [
        key for key in keys
        if full(key) and key[0] == anchor[0] and key[1] != anchor[1]
        and (not hint or matches(next(iter(by_key[key].values())), hint))
        and (not prefer or matches_any(next(iter(by_key[key].values())), prefer))
    ]
    others = [
        key for key in keys
        if full(key) and key not in same and key != anchor and (not long or key[2] == "assembled notes")
        and (not hint or matches(next(iter(by_key[key].values())), hint))
        and (not prefer or matches_any(next(iter(by_key[key].values())), prefer))
    ]
    # A timbre hint stays on one origin. The next seed can pick a different origin.
    pool = same if hint else (same or others)
    rng = random.Random(seed + 91)
    rng.shuffle(pool)
    # Not the rank order, so the next seed can keep the anchor and change the company.
    for key in pool[:2]:
        members.append(pack(key))
    return members


def _entry_times(rng: random.Random, kind: str, twice: bool, dur: float) -> list[float]:
    if twice:
        second = max(0.2, float(dur) / 2.0)
        if kind == "late":
            return [round(rng.uniform(0.04, 0.12), 3), round(second + rng.uniform(-0.04, 0.08), 3)]
        return [0.0, round(second, 3)]
    if kind == "downbeat":
        return [round(rng.uniform(0.0, 0.03), 3)]
    if kind == "late":
        return [round(rng.uniform(0.05, 0.18), 3)]
    if kind == "push":
        return [round(rng.uniform(0.0, 0.02), 3), round(rng.uniform(0.1, 0.22), 3)]
    if kind == "early":
        return [0.0]
    return [round(rng.uniform(0.0, 0.05), 3), round(rng.uniform(0.08, 0.18), 3)]


def arrange(members: list[dict], seed: int, mode: str, twice: bool, dur: float) -> list[dict]:
    """How this cast plays each chord.

    Orderly copies one bar: the same voices, the same time, the same loudness.
    Free varies who plays, when, and how loud. A combination may return.
    A chord is never left empty when somebody in the cast can play it.
    """
    n = len(members[0]["opts"]) if members else 0
    n_members = len(members)
    if n == 0:
        return []
    if mode == "orderly":
        strikes = [0.0, round(max(0.2, float(dur) / 2.0), 3)] if twice else [0.02]
        rows = []
        for i in range(n):
            playing = [k for k in range(n_members) if members[k]["opts"][i]]
            rows.append({
                "playing": playing,
                "lead": playing[0] if playing else 0,
                "density": "full",
                "register": "full",
                "entry": "downbeat",
                "gain": 1.0,
                "strikes_at": list(strikes),
                "rest": not playing,
                "style": "strike",
            })
        return rows

    rng = random.Random(int(seed) + 404)
    kinds = ["downbeat", "late", "push", "stagger", "early"]
    densities = ["full", "sparse", "thin", "full", "cluster"]
    registers = ["full", "low", "high", "mid"]
    rows = []
    seen_sets = []
    for i in range(n):
        available = [k for k in range(n_members) if members[k]["opts"][i]]
        if not available:
            rows.append({
                "playing": [], "lead": 0, "density": "full", "register": "full",
                "entry": "downbeat", "gain": 1.0, "strikes_at": [0.0], "rest": True, "style": "strike",
            })
            continue
        if n_members == 1:
            playing = [available[0]]
        else:
            playing = []
            if seen_sets and rng.random() < 0.42:
                playing = [k for k in seen_sets[-1] if k in available]
            if not playing:
                width = rng.choice([0.38, 0.62, 0.84])
                playing = [k for k in available if rng.random() < width]
            if not playing:
                playing = [available[rng.randrange(len(available))]]
            # A pure round-robin is a grid. Open the chord up instead.
            if len(seen_sets) >= n_members and seen_sets[-n_members:] == [tuple([k]) for k in range(n_members)]:
                playing = list(available)
        seen_sets.append(tuple(playing))
        lead = playing[rng.randrange(len(playing))]
        entry = "downbeat" if i == 0 else rng.choice(kinds)
        rows.append({
            "playing": playing,
            "lead": lead,
            "density": rng.choice(densities),
            "register": rng.choice(registers),
            "entry": entry,
            "gain": round(rng.uniform(0.72, 1.16), 3),
            "strikes_at": _entry_times(rng, entry, twice, dur),
            "rest": False,
            "style": "roll" if entry in ("push", "stagger") else "strike",
        })
    # Every member who was invited should be heard, unless the take is a single voice.
    if n_members > 1:
        used = {k for row in rows for k in row["playing"]}
        for k in range(n_members):
            if k in used:
                continue
            for i in range(n):
                if rows[i]["rest"]:
                    continue
                if members[k]["opts"][i]:
                    rows[i]["playing"] = sorted(set(rows[i]["playing"] + [k]))
                    rows[i]["rest"] = False
                    used.add(k)
                    break
    return rows


def sounding_midis(voicing, root_pc, density: str, register: str) -> list[int]:
    """A subset of this chord's own notes. Nothing outside the voicing is added."""
    notes = sorted({int(m) for m in voicing or []})
    if len(notes) <= 1 or (density == "full" and register == "full"):
        return notes

    def is_root(m: int) -> bool:
        return root_pc is not None and m % 12 == int(root_pc)

    if register == "low":
        pool = notes[: max(2, (len(notes) + 1) // 2)]
    elif register == "high":
        pool = notes[-max(2, (len(notes) + 1) // 2) :]
    elif register == "mid" and len(notes) > 3:
        mid = len(notes) // 2
        pool = notes[max(0, mid - 1) : mid + 2]
    else:
        pool = list(notes)
    roots = [m for m in notes if is_root(m)]
    chosen: list[int] = []
    if roots:
        anchor = min(roots, key=lambda m: min(abs(m - p) for p in pool))
        chosen.append(anchor)
    colors = [m for m in pool if m not in chosen]
    if not colors:
        colors = [m for m in notes if m not in chosen]
    if density in ("thin", "sparse") and colors:
        chosen.append(colors[-1] if register == "high" else colors[0])
    else:
        chosen.extend(colors)
    out = []
    for m in chosen:
        if m in notes and m not in out:
            out.append(m)
    if len(out) < min(2, len(notes)):
        for m in notes:
            if m not in out:
                out.append(m)
            if len(out) >= min(2, len(notes)):
                break
    return sorted(out)


def predictability(rows: list[dict]) -> dict:
    """1 is the same arrangement every chord. Source variation is how often the cast changes."""
    if not rows:
        return {"self_similarity": 1.0, "member_similarity": 1.0, "source_variation": 0.0}
    sigs = []
    members = []
    for row in rows:
        sigs.append((
            tuple(row.get("members") or []),
            row.get("entry"),
            row.get("density"),
            row.get("register"),
            round(float(row.get("gain") or 0), 1),
            tuple(round(float(t), 2) for t in (row.get("strikes_at") or [])),
            bool(row.get("rest")),
        ))
        members.append(tuple(row.get("members") or []))
    same = sum(1 for a, b in zip(sigs, sigs[1:]) if a == b)
    same_m = sum(1 for a, b in zip(members, members[1:]) if a == b)
    denom = max(1, len(rows) - 1)
    member_sim = same_m / denom
    return {
        "self_similarity": round(same / denom, 3),
        "member_similarity": round(member_sim, 3),
        "source_variation": round(1 - member_sim, 3),
    }


def chord_overlaps(chord_t: float, cover: float, sid: str, role: str, seed: int, index: int, gain: float, entry: float, orderly: bool) -> list[dict]:
    """Unaltered copies. A whole-chord file goes quiet after about two seconds, so the next copy starts before that.

    The file is not time-stretched. Orderly spacing is one hop. Free spacing changes from copy to copy.
    """
    rng = random.Random(seed * 1009 + index * 13)
    start = float(chord_t) + float(entry)
    audible = 2.35
    last = float(chord_t) + float(cover) - audible
    if last < start:
        last = start
    events = []
    t = start
    k = 0
    while t <= last + 1e-6 and k < 24:
        follow = gain * (0.94 if orderly else rng.uniform(0.86, 1.02))
        events.append({
            "t": round(t, 3),
            "source": sid,
            "offset_s": 0.0,
            "dur_s": round(max(2.0, float(cover) * (0.72 if k else 1.0)), 3),
            "fade_in_s": 0.02 if k == 0 else round(rng.uniform(0.03, 0.08), 3),
            "fade_out_s": 0.35 if orderly else round(rng.uniform(0.25, 0.5), 3),
            "gain": round(gain if k == 0 else follow, 3),
            "flatten": False,
            "role": role,
        })
        t += 1.55 if orderly else rng.choice([1.05, 1.28, 1.5, 1.72])
        k += 1
    return events


def ring(dur: float, gap: bool) -> dict:
    """How long a chord sounds. A gap leaves a short breath. Otherwise it overlaps the next."""
    dur = float(dur)
    if gap:
        note_s = max(1.2, dur * 0.92)
        fade = min(1.1, note_s * 0.22)
        return {
            "note_s": round(note_s, 3),
            "fade_out_s": round(fade, 3),
            "last_note_s": round(note_s + 1.2, 3),
            "last_fade_out_s": round(min(2.4, fade + 0.8), 3),
            "cover": note_s,
        }
    over = min(6.0, max(2.5, dur * 0.55))
    note_s = dur + over
    fade = max(0.9, over * 0.85)
    return {
        "note_s": round(note_s, 3),
        "fade_out_s": round(fade, 3),
        "last_note_s": round(note_s + 1.5, 3),
        "last_fade_out_s": round(min(6.0, fade + 1.2), 3),
        "cover": note_s,
    }


def entry_offset(seed: int, index: int) -> float:
    return round(random.Random(seed * 1009 + index * 17).uniform(0.0, 0.04), 3)


def clip_copies(chord_t: float, dur: float, cover: float, sid: str, role: str, seed: int, index: int, gain: float) -> list[dict]:
    """Unaltered copies, crossfaded, long enough to reach the next chord. Not cut at 4.2 s."""
    rng = random.Random(seed * 1009 + index)
    start = float(chord_t) + rng.uniform(0.0, 0.04)
    step = 3.25
    events = []
    t = start
    k = 0
    end = float(chord_t) + float(cover) - 0.35
    while t < end and k < 8:
        events.append({
            "t": round(t, 3),
            "source": sid,
            "offset_s": 0.0,
            "dur_s": round(max(2.0, float(cover)), 3),
            "fade_in_s": 0.02 if k == 0 else 0.85,
            "fade_out_s": 1.7,
            "gain": round(gain if k == 0 else gain * 0.62, 3),
            "flatten": False,
            "role": role,
        })
        t += step
        k += 1
    return events


def bed_events(length: float, seed: int) -> list[dict]:
    """Irregular hops and a small file offset, so the bed is not an 8 s loop from the top."""
    rng = random.Random(int(seed) + 29)
    hop0 = rng.uniform(10.8, 14.2)
    offset = rng.uniform(0.2, 1.7)
    events = []
    t = 0.0
    k = 0
    while t < float(length) - 0.4 and k < 40:
        hop = hop0 + rng.uniform(-1.6, 2.1)
        if abs(hop - 8.0) < 0.45:
            hop += 1.4
        events.append({
            "t": round(t, 3),
            "offset_s": round((offset + k * 0.81) % 2.4, 3),
            "dur_s": 18.0,
            "fade_in_s": 1.5 if k == 0 else 2.7,
            "fade_out_s": 3.0,
            "gain": 0.18,
        })
        t += hop
        k += 1
    return events


def arc(length: float, even: bool, seed: int = 0) -> dict:
    span = float(length)
    if even:
        db = [-1.2, -0.8, -0.3, -1.0]
        spots = [0, 0.2, 0.62, 1]
    else:
        rng = random.Random(int(seed) + 77)
        spots = [0, 0.11, 0.27, 0.46, 0.63, 0.81, 1]
        db = [round(rng.uniform(-6.0, 1.4), 2) for _ in spots]
        db[0] = min(db[0], -1.2)
        db[-1] = min(db[-1], -0.8)
    return {"t": [round(s * span, 3) for s in spots], "db": db}


def reverb_for(kn: dict) -> tuple[float, float]:
    send, decay = 3.4, 3.5
    if kn.get("soft"):
        send, decay = 4.0, 5.0
    if kn.get("distance"):
        send = max(send, 4.5)
        decay = max(decay, 4.2)
    return send, decay


def root_pc(symbol: str, bass) -> int | None:
    if bass is not None:
        return int(bass) % 12
    m = re.match(r"\s*([A-G])(#|b)?", symbol or "")
    if not m:
        return None
    return (_PC[m.group(1)] + {"#": 1, "b": -1}.get(m.group(2) or "", 0)) % 12


def root_midi(symbol: str, bass, midis: set[int]) -> int | None:
    pc = root_pc(symbol, bass)
    if pc is None or not midis:
        return None
    low = [m for m in midis if m % 12 == pc and m < 55]
    if low:
        return min(low)
    any_pc = [m for m in midis if m % 12 == pc]
    if not any_pc:
        return None
    return min(any_pc)


def surprise_index(n: int, seed: int) -> int:
    if n <= 1:
        return 0
    return 1 + (int(seed) % (n - 1))
