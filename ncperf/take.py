"""Shape one discovered take: ensemble, ring, phrase, bed, and a seeded surprise.

Nothing here pitch-shifts or time-stretches. Durations, overlaps, rolls, and
which already-legal samples play together are the only choices.
"""
from __future__ import annotations

import random
import re

from . import chords as CH
from . import language

# Same ceiling as max_chords. The old plan stopped at 12.
CHORD_LIMIT = 40

_SPACE_BED = re.compile(
    r"\b(space|air|rooms?|ambience|ambient|atmosphere|field|rain|wind|wash|pads?)\b"
    r"|\bin the distance\b|\bdistant\b",
    re.I,
)
_GAP = re.compile(r"\b(space|air between|breath|pause between)\b", re.I)
_RING = re.compile(r"\b(ring(?:ing|s)?|legato)\b|\binto the next\b", re.I)
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


def composition_chords(text: str) -> list:
    """Chords in the order the Composition means.

    A bar table is sorted by bar number. Prose is not scanned once that table
    is there, so a three-column prelude does not come out row by row.
    """
    return bar_chords(text) or CH.find_full(language.uncomment(text))


_PULSE = re.compile(
    r"steady pulse|keep it even|\bsteady\b|\buniform\b|every downbeat|"
    r"one chord per bar|even changes|"
    r"\d+(?:\.\d+)?\s*(?:s|sec|seconds)\s+per\s+(?:chord|bar)|"
    r"(?:each|every)\s+bar\s+(?:is\s+|lasts\s+)?\d",
    re.I,
)
_BROADEN = re.compile(r"broadening over the last\s+(\w+|\d+)\s+bars", re.I)
_HALF_SPLIT = re.compile(r"\bsecond half\b", re.I)
_BAR_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5}


def stated_pulse(words: str) -> bool:
    """True when the words fix the clock: a steady pulse, or a duration per bar."""
    return bool(_PULSE.search(language.uncomment(words or "")))


def broaden_bars(words: str) -> int:
    """How many final bars the words ask to let breathe. Zero when they do not."""
    text = language.uncomment(words or "")
    match = _BROADEN.search(text)
    if match:
        raw = match.group(1).lower()
        if raw.isdigit():
            return int(raw)
        return _BAR_WORDS.get(raw, 3)
    if re.search(r"last bar\b.{0,40}\b(held|ring)", text, re.I):
        return 1
    return 0


def half_sections(text: str) -> tuple[list, list] | None:
    """Chords before and after 'second half', when the Composition splits the piece that way."""
    clean = language.uncomment(text or "")
    match = _HALF_SPLIT.search(clean)
    if not match:
        return None
    left = CH.find_full(clean[: match.start()])
    right = CH.find_full(clean[match.end() :])
    if not left or not right:
        return None
    return left, right


# A written clock. Organic rubato is not used once one of these matches every chord.
#   [2 bars Cm]  [4 beats G7]  [1.5 bars Am7]
#   Cm (2 bars)  G7 (4 beats)  Cm for 2 bars
# Tempo: "60 BPM", "tempo 90", "q90", or "quarter = 60" (a beat is a quarter). A BPM word wins.
# Meter: "4/4", "3/4", "6/8", "2/2" (default 4/4).
# A change: "at bar 12, 80 BPM", "from bar 12, 80 BPM", or "bar 12: 80 BPM" (bar 1 is the opening).
# "one chord per bar" plus a tempo change gives every chord one bar on that map.
_BRACKET_LEN = re.compile(r"\[\s*(\d+(?:\.\d+)?)\s*(bars?|beats?)\s+([^\]]+?)\]", re.I)
_LEN_AFTER = re.compile(
    r"^\s*(?:\(\s*(\d+(?:\.\d+)?)\s*(bars?|beats?)\s*\)|for\s+(\d+(?:\.\d+)?)\s*(bars?|beats?))",
    re.I,
)
_TEMPO_CHANGE = re.compile(
    r"\b(?:at|from)\s+bar\s+(\d+)\b[^.\n]{0,40}?(\d+(?:\.\d+)?)\s*BPM\b"
    r"|\bbar\s+(\d+)\s*:\s*(\d+(?:\.\d+)?)\s*BPM\b",
    re.I,
)
_BPM = re.compile(r"(\d+(?:\.\d+)?)\s*BPM\b", re.I)
_QUARTER_TEMPO = re.compile(r"quarter(?:\s+note)?\s*[=≈]\s*(\d+(?:\.\d+)?)", re.I)
_METER = re.compile(r"\b(\d+)\s*/\s*(2|4|8)\b")


def _unit(name: str) -> str:
    return "bar" if name.lower().startswith("bar") else "beat"


def _tempo_changes(text: str) -> list[tuple[int, float]]:
    found: dict[int, float] = {}
    for match in _TEMPO_CHANGE.finditer(text or ""):
        if match.group(1):
            bar, bpm = int(match.group(1)), float(match.group(2))
        else:
            bar, bpm = int(match.group(3)), float(match.group(4))
        if bar >= 1 and bpm > 0:
            found[bar] = bpm
    return sorted(found.items())


def _opening_bpm(text: str, changes: list[tuple[int, float]]) -> float | None:
    # ``tempo`` and ``q`` count, and a BPM word still beats an earlier alias.
    # A change at a later bar is blanked inside opening_tempo, so it stays a change.
    found = language.opening_tempo(text)
    if found and found.get("bpm"):
        return float(found["bpm"])
    for bar, bpm in changes:
        if bar <= 1:
            return bpm
    return None


def _quarters_per_bar(text: str) -> float:
    match = _METER.search(text or "")
    if not match:
        return 4.0
    return int(match.group(1)) * (4.0 / int(match.group(2)))


def _bracket_lengths(text: str, chords: list) -> list[tuple[float, str]] | None:
    symbols: list[str] = []
    specs: list[tuple[float, str]] = []
    for match in _BRACKET_LEN.finditer(text or ""):
        hit = CH.find_full(match.group(3))
        if not hit:
            continue
        symbols.append(hit[0][0])
        specs.append((float(match.group(1)), _unit(match.group(2))))
    wanted = [sym for sym, _bass in chords]
    if symbols == wanted and specs:
        return specs
    return None


def _annotated_lengths(text: str, chords: list) -> list[tuple[float, str]] | None:
    rows: list[tuple[str, tuple[float, str] | None]] = []
    body = text or ""
    for match in CH.TOKEN.finditer(body):
        if (
            match.group(1) == "A"
            and not match.group(2)
            and not match.group(3)
            and re.match(r" [a-z]", body[match.end() : match.end() + 2])
        ):
            continue
        sym = CH.spell(match.group(1) + match.group(2).rstrip("(").rstrip(")"))
        if not sym:
            continue
        tail = _LEN_AFTER.match(body[match.end() :])
        if not tail:
            rows.append((sym, None))
            continue
        amount = float(tail.group(1) or tail.group(3))
        unit = _unit(tail.group(2) or tail.group(4))
        rows.append((sym, (amount, unit)))
    wanted = [sym for sym, _bass in chords]
    if [sym for sym, _spec in rows] != wanted or any(spec is None for _sym, spec in rows):
        return None
    return [spec for _sym, spec in rows if spec is not None]


def _seconds_for(
    start_bar: float,
    amount: float,
    unit: str,
    quarters_per_bar: float,
    opening: float,
    changes: list[tuple[int, float]],
) -> float:
    quarters = amount * quarters_per_bar if unit == "bar" else amount
    pos = (start_bar - 1.0) * quarters_per_bar
    end = pos + quarters

    def bpm_at(q: float) -> float:
        bar = q / quarters_per_bar + 1.0
        bpm = opening
        for change_bar, change_bpm in changes:
            if change_bar <= bar + 1e-6:
                bpm = change_bpm
        return bpm

    points = [pos]
    for change_bar, _bpm in changes:
        boundary = (change_bar - 1.0) * quarters_per_bar
        if pos + 1e-9 < boundary < end - 1e-9:
            points.append(boundary)
    points.append(end)
    total = 0.0
    for left, right in zip(points, points[1:]):
        total += (right - left) * (60.0 / bpm_at(left))
    return total


def written_durations(text: str, chords: list) -> list[float] | None:
    """Seconds for each chord, when the Composition writes the clock.

    None means it did not: the take may use a pulse or organic lengths.
    A tempo is required (`60 BPM` or `quarter = 60`). A beat is a quarter note.
    A bar follows the meter (`4/4` by default; `6/8` is six eighths).
    `at bar 12, 80 BPM` changes the tempo from that bar, including inside a chord.
    Lengths are kept to the millisecond. The 1.25 s arrangement floor is not applied.
    plan.json still refuses a chord shorter than 0.1 s; that minimum is the schema.
    """
    if not chords:
        return None
    text = language.uncomment(text or "")
    changes = [(bar, bpm) for bar, bpm in _tempo_changes(text) if bar > 1]
    opening = _opening_bpm(text, _tempo_changes(text))
    specs = _bracket_lengths(text, chords) or _annotated_lengths(text, chords)
    if specs is None and changes and re.search(r"one chord per bar", text or "", re.I):
        specs = [(1.0, "bar")] * len(chords)
    if specs is None or opening is None or opening <= 0:
        return None
    quarters = _quarters_per_bar(text) or 4.0
    exact: list[float] = []
    start = 1.0
    for amount, unit in specs:
        if amount <= 0:
            return None
        exact.append(_seconds_for(start, amount, unit, quarters, opening, changes))
        start += amount if unit == "bar" else amount / quarters
    if any(value <= 0 for value in exact):
        return None
    rounded = [round(value, 3) for value in exact]
    target = round(sum(exact), 3)
    rounded[-1] = round(rounded[-1] + (target - sum(rounded)), 3)
    return rounded


def slots_from_durations(durs: list[float]) -> list[dict]:
    """Place written durations back to back. Nothing is scaled or rebreathed."""
    slots = []
    t = 0.0
    for dur in durs:
        slots.append({"t": round(t, 3), "dur": round(float(dur), 3)})
        t = round(t + float(dur), 3)
    return slots


def duration_cv(durs: list[float]) -> float:
    """Coefficient of variation. 0 is a perfect grid."""
    if len(durs) < 2:
        return 0.0
    mean = sum(durs) / len(durs)
    if mean <= 0:
        return 0.0
    var = sum((float(d) - mean) ** 2 for d in durs) / len(durs)
    return round((var ** 0.5) / mean, 3)


def looks_alternating(durs: list[float]) -> bool:
    """True for a long/short grid, including a two-value alternation."""
    if len(durs) < 4:
        return False
    evens = durs[0::2]
    odds = durs[1::2]
    def tight(group: list[float]) -> bool:
        mid = sum(group) / len(group)
        return mid > 0 and (max(group) - min(group)) / mid < 0.12
    if not (tight(evens) and tight(odds)):
        return False
    return abs(sum(evens) / len(evens) - sum(odds) / len(odds)) > 0.2 * (sum(durs) / len(durs))


def _free_weights(n: int, rng: random.Random) -> list[float]:
    """Phrase-sized push and pull. Not a long/short alternation and not one ratio repeated."""
    if n <= 1:
        return [1.0]
    weights: list[float] = []
    i = 0
    previous = ""
    kinds = ["rush", "hold", "unwind", "swell", "plain"]
    while i < n:
        left = n - i
        if left == 1:
            weights.append(rng.uniform(0.55, 1.9))
            break
        choices = [kind for kind in kinds if kind != previous] or kinds
        kind = rng.choice(choices)
        previous = kind
        span = min(left, rng.choice([2, 3, 3, 4, 5][: left]))
        if kind == "hold":
            hold_at = rng.randrange(span)
            for j in range(span):
                weights.append(rng.uniform(1.85, 2.7) if j == hold_at else rng.uniform(0.36, 0.72))
        elif kind == "rush":
            start = rng.uniform(1.25, 1.8)
            for j in range(span):
                weights.append(start * (0.74 ** j) * rng.uniform(0.9, 1.08))
        elif kind == "unwind":
            start = rng.uniform(0.4, 0.68)
            for j in range(span):
                weights.append(start * (1.34 ** j) * rng.uniform(0.92, 1.08))
        elif kind == "swell":
            mid = span // 2
            for j in range(span):
                weights.append(rng.uniform(1.45, 2.15) if j == mid else rng.uniform(0.48, 0.86))
        else:
            bag = [0.4, 0.58, 0.77, 1.05, 1.38, 1.72, 2.15, 2.55]
            for _j in range(span):
                weights.append(rng.choice(bag) * rng.uniform(0.88, 1.12))
        i += span
    weights = weights[:n]
    if looks_alternating(weights) or duration_cv(weights) < 0.22:
        weights[rng.randrange(n)] *= rng.uniform(1.7, 2.4)
        other = rng.randrange(n)
        weights[other] *= rng.uniform(0.35, 0.6)
    return weights


def _unstick(durs: list[float], total: float, rng: random.Random) -> list[float]:
    """Break a grid that survived scaling: one chord lingers, another passes."""
    if len(durs) < 2 or total <= 0:
        return durs
    out = [max(0.2, float(d)) for d in durs]
    i = rng.randrange(len(out))
    j = (i + 1 + rng.randrange(len(out) - 1)) % len(out)
    out[i] *= rng.uniform(1.65, 2.3)
    out[j] *= rng.uniform(0.38, 0.62)
    scale = total / sum(out)
    out = [w * scale for w in out]
    out[-1] += total - sum(out)
    if out[-1] < 0.4 and len(out) > 1:
        out[-1] = 0.4
        head = sum(out[:-1]) or 1.0
        scale = (total - 0.4) / head
        out = [w * scale for w in out[:-1]] + [0.4]
    return [round(max(0.1, w), 3) for w in out]


def _fit_lengths(weights: list[float], total: float) -> list[float]:
    """Scale phrase weights onto `total` seconds. Every chord stays long enough to hear.

    The floor is max(1.25 s, 0.28 of the mean), and never more than 0.92 of the mean.
    A section's last chord absorbs the rounding so the section still ends on time.
    """
    n = len(weights)
    if n == 0:
        return []
    if n == 1:
        return [round(total, 3)]
    mean = total / n
    # 1.25 s is an arrangement floor so a passing chord is still a chord, not a click.
    lo = min(mean * 0.92, max(1.25, mean * 0.28))
    hi = max(mean * 1.08, min(total * 0.58, mean * 2.85))
    if n * lo > total:
        lo = total / n * 0.9
    if n * hi < total:
        hi = total / n * 1.05
    raw = [max(weights) and (w / (sum(weights) or 1)) * total for w in weights]
    out = [min(hi, max(lo, w)) for w in raw]
    for _ in range(8):
        spare = total - sum(out)
        if abs(spare) < 0.02:
            break
        if spare > 0:
            room = [i for i, w in enumerate(out) if w < hi - 0.01]
        else:
            room = [i for i, w in enumerate(out) if w > lo + 0.01]
        if not room:
            break
        share = spare / len(room)
        for i in room:
            out[i] = min(hi, max(lo, out[i] + share))
    out[-1] = max(0.4, out[-1] + (total - sum(out)))
    return [round(w, 3) for w in out]


def _pulse_lengths(n: int, total: float, broaden: int, rng: random.Random) -> list[float]:
    """Equal bars, except a written broadening of the final bars."""
    if n <= 0:
        return []
    if broaden <= 0 or n < 3:
        each = total / n
        durs = [each] * n
    else:
        k = min(broaden, n - 1)
        factors = []
        for j in range(k):
            factors.append(1.12 + 0.16 * j)
        head = n - k
        # head * 1 + sum(factors) = scale; durs sum to total
        weight = [1.0] * head + factors
        scale = total / sum(weight)
        durs = [w * scale for w in weight]
        # A hair of in-bar difference would fight the pulse. Keep the head identical.
        head_dur = durs[0]
        durs = [head_dur] * head + durs[head:]
        durs[-1] += total - sum(durs)
    return [round(d, 3) for d in durs]


def chord_slots(sections: list[tuple[int, float]], seed: int, pulse: bool, broaden: int = 0) -> list[dict]:
    """Start and duration of each chord. Sections are (count, seconds) and keep their boundaries.

    Free pacing is phrase rubato. A stated pulse stays even, with a written broadening on the last bars.
    """
    slots = []
    t = 0.0
    last = len(sections) - 1
    for si, (count, seconds) in enumerate(sections):
        if count <= 0 or seconds <= 0:
            continue
        rng = random.Random(int(seed) + 211 + si * 29)
        if pulse:
            durs = _pulse_lengths(count, seconds, broaden if si == last else 0, rng)
        else:
            durs = _fit_lengths(_free_weights(count, rng), seconds)
            for _ in range(3):
                if len(durs) < 2 or not (looks_alternating(durs) or duration_cv(durs) < 0.22):
                    break
                durs = _unstick(durs, seconds, rng)
        if durs:
            durs[-1] = round(durs[-1] + (seconds - sum(durs)), 3)
        for dur in durs:
            slots.append({"t": round(t, 3), "dur": round(dur, 3)})
            t = round(t + dur, 3)
    return slots


def shape_strikes(strikes: list[float], slot_dur: float, seed: int, index: int, pulse: bool, orderly: bool, twice: bool) -> list[float]:
    """Where the attacks sit inside this chord. A free take does not share one offset from every boundary."""
    dur = max(0.4, float(slot_dur))
    if pulse:
        if twice:
            return [0.0, round(dur / 2.0, 3)]
        return [0.0]
    if orderly:
        first = 0.02
        if twice:
            return [first, round(min(dur - 0.12, max(first + 0.15, dur / 2.0)), 3)]
        if len(strikes) > 1:
            return [first, round(min(dur * 0.45, first + 0.22), 3)]
        return [first]
    rng = random.Random(int(seed) * 17 + index * 5 + 3)
    roll = rng.random()
    if index == 0:
        first = rng.uniform(0.02, 0.09)
    elif roll < 0.3:
        first = rng.uniform(-0.34, -0.07)
    elif roll < 0.62:
        first = rng.uniform(0.16, 0.48)
    else:
        first = rng.uniform(0.04, 0.14)
    first = max(-0.4, min(first, dur * 0.42))
    out = [round(first, 3)]
    if twice:
        out.append(round(min(dur - 0.12, max(first + 0.18, dur / 2.0 + rng.uniform(-0.1, 0.12))), 3))
    elif len(strikes) > 1 or rng.random() < 0.5:
        out.append(round(min(dur * 0.7, max(first + 0.08, first + rng.uniform(0.14, 0.42))), 3))
    if dur > 7.2 and rng.random() < 0.75:
        out.append(round(dur * rng.uniform(0.34, 0.71), 3))
    cleaned = []
    for t in sorted(out):
        if t < dur - 0.05 and (not cleaned or abs(t - cleaned[-1]) > 0.04):
            cleaned.append(round(t, 3))
    return cleaned or [0.04]


def pad_leads(n: int, seed: int, pulse: bool, orderly: bool) -> dict[str, float]:
    """How far before each chord a joining layer enters. Free takes do not use one lead."""
    if n <= 0:
        return {}
    if pulse or orderly:
        return {str(i): 0.0 for i in range(n)}
    rng = random.Random(int(seed) + 83)
    bag = [0.05, 0.12, 0.19, 0.28, 0.37, 0.46, 0.09, 0.23]
    leads = {str(i): round(rng.choice(bag), 3) for i in range(n)}
    if n > 1 and len(set(leads.values())) == 1:
        leads["1"] = 0.33 if leads["0"] != 0.33 else 0.14
    return leads


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
        "ring": bool(_RING.search(text)),
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


def collection_of(opt: dict) -> str:
    """The catalog pack inside a source. A bigger pack is not a heavier pack."""
    named = opt.get("collection")
    if named:
        return str(named)
    cand = opt.get("cand") or {}
    if isinstance(cand, dict) and cand.get("collection"):
        return str(cand["collection"])
    return str(opt.get("instrument") or "catalog")


def family_of(name: str) -> str:
    """The ensemble family of a recording. Piano is one family, wherever it was recorded."""
    inst = (name or "").lower()
    if "piano" in inst or inst == "rhodes":
        return "piano"
    if "fat" in inst and "synth" in inst:
        return "fat synth"
    if "arp" in inst:
        return "synth arp"
    if "synth" in inst:
        return "synth"
    if any(part in inst for part in ("cello", "violin", "viola", "string")):
        return "strings"
    if any(part in inst for part in ("flute", "oboe", "clarinet", "bassoon", "trumpet", "horn", "sax")):
        return "winds"
    if any(part in inst for part in ("marimba", "vibraphone", "xylophone", "glock", "bell")):
        return "mallets"
    if "guitar" in inst:
        return "guitar"
    if "harp" in inst:
        return "harp"
    if "choir" in inst or inst in ("voice", "chorus"):
        return "choir"
    if "organ" in inst:
        return "organ"
    return inst or "other"


def _tier_value(opt: dict) -> int:
    """0 is a real rank. A missing tier sorts last."""
    raw = opt.get("tier")
    if raw is None:
        return 9
    return int(raw)


def _coverage(groups: list[list[dict]], avoid: tuple | None = None) -> dict[tuple, dict[int, dict]]:
    by_key: dict[tuple, dict[int, dict]] = {}
    for i, group in enumerate(groups):
        best: dict[tuple, dict] = {}
        for opt in group:
            key = _key(opt)
            if avoid and key == avoid:
                continue
            prev = best.get(key)
            if prev is None or _tier_value(opt) < _tier_value(prev):
                best[key] = opt
        for key, opt in best.items():
            by_key.setdefault(key, {})[i] = opt
    return by_key


def pick_recording(by_key: dict, n: int, seed: int, prefer: list[str] | None) -> tuple | None:
    """One recording that can play every chord.

    A family is in the draw when one of its recordings covers the piece: a
    whole-chord file for each chord, or a stack of indexed notes for each
    chord. Families draw with equal weight. Piano files and piano notes are
    one family, the same size as choir, guitar, or a synth. The word "piano"
    does not shrink that draw. Another named instrument does, when that
    family can cover. The seed picks the family, then a recording inside it.
    A whole-chord file ranks ahead of a note stack. An exact voicing ranks
    ahead of an inversion. A family that cannot cover the piece is not used
    to fill the gaps.
    """
    if not by_key:
        return None
    full = [key for key, rows in by_key.items() if len(rows) == n and n]
    if full:
        pool = full
    else:
        best = max(len(rows) for rows in by_key.values())
        pool = [key for key, rows in by_key.items() if len(rows) == best]
    # Piano in the words is not a default. Choir, guitar, strings, and the
    # other named families still keep the draw when they can play the chords.
    asked = [word for word in (prefer or []) if word != "piano"]
    if asked:
        named = []
        for key in pool:
            sample = next(iter(by_key[key].values()))
            if matches_any(sample, asked):
                named.append(key)
        if named:
            pool = named
    families: dict[str, list] = {}
    for key in pool:
        families.setdefault(family_of(key[1]), []).append(key)
    rng = random.Random(int(seed) + 17)
    names = sorted(families)
    rng.shuffle(names)
    options = list(families[names[0]])
    rng.shuffle(options)
    options.sort(key=lambda key: (
        0 if key[2] == "whole-chord file" else 1,
        min(_tier_value(opt) for opt in by_key[key].values()),
    ))
    return options[0]


def choose(groups: list[list[dict]], seed: int, prefer: list[str] | None, hint: str | None, long: bool, avoid: tuple | None = None) -> tuple[list, tuple | None]:
    """One ensemble for these chords. The next seed can choose another family.

    ``hint`` and ``long`` do not choose the ensemble. A named choir, guitar,
    or synth does, via ``prefer``, when that family can cover. The word
    "piano" does not. A recording that cannot play a chord is not used to
    fill one. Nothing is pitch-shifted to fill a gap.
    """
    del hint, long
    n = len(groups)
    by_key = _coverage(groups, avoid)
    if not by_key:
        return [None] * n, None
    primary = pick_recording(by_key, n, seed, prefer)
    chosen: list = [by_key[primary].get(i) for i in range(n)]
    if any(row is None for row in chosen):
        rest = [key for key in by_key if key != primary]
        fam = family_of(primary[1])
        rng = random.Random(int(seed) + 29)
        rng.shuffle(rest)
        rest.sort(key=lambda key: (0 if family_of(key[1]) == fam else 1, -len(by_key[key])))
        for key in rest:
            for i in range(n):
                if chosen[i] is None and i in by_key[key]:
                    chosen[i] = by_key[key][i]
            if all(row is not None for row in chosen):
                break
    return chosen, primary


def _pick_name(rng: random.Random, names: list[str]) -> str:
    """Equal weight. Sorting first makes the shuffle reproducible."""
    ordered = sorted(names)
    rng.shuffle(ordered)
    return ordered[0]


def draw_each(groups: list[list[dict]], seed: int, prefer: list[str] | None, avoid: tuple | None = None) -> tuple[list, tuple | None]:
    """Each chord draws on its own. The same seed draws the same take.

    Families that can play that chord are equal, and piano is not the default.
    A named choir, guitar, or synth narrows that chord when the family can play
    it. The word "piano" does not. Inside the family, each source is equal, and
    inside the source each collection is equal, so a library with more files is
    not heavier. An exact voicing still ranks ahead of an inversion. A note
    stack is used for a family that has no whole-chord file of that chord.
    Nothing is pitch-shifted to fill a gap.
    """
    chosen = [_draw_chord(group, int(seed), index, prefer, avoid) for index, group in enumerate(groups)]
    primary = next((row for row in chosen if row), None)
    return chosen, (_key(primary) if primary else None)


def _draw_chord(group: list[dict], seed: int, index: int, prefer: list[str] | None, avoid: tuple | None) -> dict | None:
    rows = []
    for opt in group or []:
        if not opt:
            continue
        if avoid and _key(opt) == avoid:
            continue
        rows.append(opt)
    if not rows:
        return None
    asked = [word for word in (prefer or []) if word != "piano"]
    if asked:
        named = [row for row in rows if matches_any(row, asked)]
        if named:
            rows = named
    # A whole-chord file of this family ranks ahead of stacking that same family.
    # A different family that can only stack notes stays in the draw.
    file_families = {
        family_of(row.get("instrument") or "")
        for row in rows
        if row.get("built") == "whole-chord file"
    }
    kept = []
    for row in rows:
        fam = family_of(row.get("instrument") or "")
        if row.get("built") != "whole-chord file" and fam in file_families:
            continue
        kept.append(row)
    rows = kept or rows
    rng = random.Random(int(seed) + 17 + int(index) * 1009)
    by_fam: dict[str, list] = {}
    for row in rows:
        by_fam.setdefault(family_of(row.get("instrument") or ""), []).append(row)
    fam_rows = by_fam[_pick_name(rng, list(by_fam))]
    by_src: dict[str, list] = {}
    for row in fam_rows:
        by_src.setdefault(str(row.get("origin") or ""), []).append(row)
    src_rows = by_src[_pick_name(rng, list(by_src))]
    best = min(_tier_value(row) for row in src_rows)
    tier_rows = [row for row in src_rows if _tier_value(row) == best]
    by_col: dict[str, list] = {}
    for row in tier_rows:
        by_col.setdefault(collection_of(row), []).append(row)
    files = by_col[_pick_name(rng, list(by_col))]
    files = sorted(files, key=lambda row: (
        str(row.get("title") or ""),
        str((row.get("cand") or {}).get("key") or ""),
    ))
    rng.shuffle(files)
    return files[0]


def _best_named(by_key: dict, words: list[str], n: int, seed: int, prefer, hint, long):
    """The recording of this named voice that covers the most chords. Partial cover is allowed."""
    del prefer, hint, long
    hits = []
    for key, covered in by_key.items():
        sample = next(iter(covered.values()))
        if matches_any(sample, words):
            hits.append(key)
    if not hits:
        return None
    rng = random.Random(int(seed) + 41)
    rng.shuffle(hits)

    def sort_key(key: tuple):
        covered = by_key[key]
        missing = n - len(covered)
        tier = min(_tier_value(opt) for opt in covered.values())
        return (0 if missing == 0 else 1, missing, tier)

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
    """The cast for this take. One family, unless the words name more than one voice.

    Named timbres ('layer a pluck with a synth') are the cast, even when one of them
    cannot play every chord. A chord that voice cannot play is left to the other voice.
    A timbre hint does not add a second family, and piano is not added beside the
    family the seed chose.
    """
    del hint, long, mode
    n = len(groups)
    by_key = _coverage(groups)
    if not by_key:
        return []

    def pack(key: tuple) -> dict:
        opts = [by_key[key].get(i) for i in range(n)]
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
            hit = _best_named(by_key, words, n, seed, prefer, None, False)
            if hit and hit not in picked:
                picked.append(hit)
        if picked:
            return [pack(key) for key in picked[:3]]
        return []

    anchor = pick_recording(by_key, n, seed, prefer)
    if anchor is None:
        return []
    return [pack(anchor)]


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


# A whole-chord file is about 4.4 s with audible energy to ~2.3 s. A ringing chord
# assumes 2.0 s, so a restart still overlaps if the tail is a little shorter.
SUSTAIN_AUDIBLE = 2.0


def chord_overlaps(chord_t: float, cover: float, sid: str, role: str, seed: int, index: int, gain: float, entry: float, orderly: bool, continuous: bool = False) -> list[dict]:
    """Unaltered copies. A whole-chord file goes quiet after about two seconds, so the next copy starts before that.

    The file is not time-stretched. Orderly spacing is one hop. Free spacing changes from copy to copy.
    `continuous` is a ring or legato chord: restarts stay close enough that the join is under 0.15 s,
    and a last copy is placed so the written length stays covered.
    """
    rng = random.Random(seed * 1009 + index * 13)
    start = max(0.0, float(chord_t) + float(entry))
    audible = SUSTAIN_AUDIBLE if continuous else 2.35
    target = float(chord_t) + float(cover)
    last = target - audible
    if last < start:
        last = start
    events = []
    t = start
    k = 0
    while t <= last + 1e-6 and k < 48:
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
        if continuous:
            t += rng.choice([0.95, 1.1, 1.2, 1.35])
        else:
            t += 1.55 if orderly else rng.choice([1.05, 1.28, 1.5, 1.72])
        k += 1
    if continuous and events and k < 48:
        sound_end = events[-1]["t"] + audible
        if sound_end < target - 0.12:
            t_close = round(max(start, target - audible), 3)
            if t_close > events[-1]["t"] + 0.15:
                events.append({
                    "t": t_close,
                    "source": sid,
                    "offset_s": 0.0,
                    "dur_s": round(max(2.0, audible + 0.2), 3),
                    "fade_in_s": 0.05,
                    "fade_out_s": 0.3,
                    "gain": round(gain * 0.9, 3),
                    "flatten": False,
                    "role": role,
                })
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
