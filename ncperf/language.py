"""The Composition language, first slice.

Python port of the page parser (netcomposer.art ``language.js``). The hosted
render image is Python and does not run Node, so Gather cannot load the page
file. ``ncperf/data/language-vectors.json`` is the shared contract: the page
and this module are pinned to the same rows.

Words this module does not recognise stay words. Nothing here calls a model.
A hack is Live's starting value for the whole piece. A ramp is not rendered.
"""
from __future__ import annotations

import math
import re

from . import chords as CH

PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
SPELL = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
ALIAS = {
    "maj": "", "M": "", "min": "m", "-": "m", "mi": "m", "Δ7": "maj7", "Δ": "maj7", "M7": "maj7", "ma7": "maj7", "M9": "maj9",
    "ø7": "m7b5", "ø": "m7b5", "°7": "dim7", "o7": "dim7", "°": "dim", "o": "dim", "+": "aug", "#5": "aug",
    "mMaj7": "m-maj7", "mmaj7": "m-maj7", "m(maj7)": "m-maj7", "madd9": "m-add9", "m(add9)": "m-add9",
    "6/9": "6-9", "69": "6-9", "m6/9": "m6-9", "m69": "m6-9", "sus": "sus4", "7sus": "7sus4", "9sus": "9sus4",
    "add2": "add9", "2": "sus2", "7+5": "maj7#5",
}
PRETTY = {"6-9": "6/9", "m6-9": "m6/9", "m-add9": "madd9"}
QCHAR = re.compile(r"^(?:maj|min|mi|m|M|dim|aug|sus|add|alt|ø|°|Δ|o|\+|-|[0-9]|#|b|\(|\)|/(?=9))")
HACK = {
    "lowpass": "lp", "lp": "lp", "highpass": "hp", "hp": "hp", "bandpass": "bp", "bp": "bp", "eq": "eq",
    "delay": "dl", "dl": "dl", "reverb": "rv", "rv": "rv", "drive": "dr", "dr": "dr", "comp": "cp", "cp": "cp", "width": "wd", "wd": "wd",
}
HACK_TYPE = {"lp": "lowpass", "hp": "highpass", "bp": "bandpass", "eq": "eq", "dl": "delay", "rv": "plate", "dr": "drive", "cp": "compressor", "wd": "width"}
HACK_WORD = re.compile(r"^(?:lowpass|highpass|bandpass|reverb|delay|drive|width|comp|eq|lp|hp|bp|dl|rv|dr|cp|wd)$", re.I)
CHANGE = re.compile(
    r"\b(?:at|from)\s+bar\s+\d+\b[^.\n]{0,40}?\d+(?:\.\d+)?\s*BPM\b|\bbar\s+\d+\s*:\s*\d+(?:\.\d+)?\s*BPM\b",
    re.I,
)
_BREAK = set(" \n\t\r,;[]|\"·.!:=≈–—→←×{}~*•")
_SKIP = set(" \n\t\r,;·.!=≈–—→←×{}~*•")
_TEMPO = re.compile(r"(?:^|[\s;,.(])tempo\s*(\d+(?:\.\d+)?)", re.I)
_Q = re.compile(r"(?:^|[\s;,.(])q\s*(\d+(?:\.\d+)?)", re.I)
_QUARTER = re.compile(r"quarter(?:\s*note)?\s*[=≈]\s*(\d+(?:\.\d+)?)", re.I)
_BPM = re.compile(r"(\d+(?:\.\d+)?)\s*BPM\b", re.I)


def known_catalog(qual: str) -> bool:
    """True when ``qual`` is a quality in the full chord set. ``''`` is major."""
    return qual in CH.QUALS


def uncomment(text) -> str:
    return _uncomment(str(text or ""))


def _uncomment(text: str) -> str:
    # Quotes go first, then a comment that starts a line or follows whitespace.
    # ``F#`` is not a comment: ``#`` has to be the start of a token.
    blanked = re.sub(r'"(?:[^"\\]|\\.)*"', " ", text)
    return re.sub(r"(?:^|\n|\s)#[^\n]*", " ", blanked)


def trim_num(n) -> str:
    if not (isinstance(n, (int, float)) and not isinstance(n, bool)) or (not (n > 0) and n != 0):
        return ""
    if abs(n - js_round(n)) < 1e-9:
        return str(js_round(n))
    text = f"{round3(n):.3f}".rstrip("0").rstrip(".")
    return text


def round3(n) -> float:
    return float(f"{float(n):.3f}")


def num(n):
    """A JSON number the way the page emits one: integers stay integers."""
    if n is None or isinstance(n, bool):
        return None
    value = round3(n) if isinstance(n, float) else n
    if isinstance(value, float) and abs(value - js_round(value)) < 1e-9:
        return js_round(value)
    if isinstance(value, float):
        return value
    return int(value) if isinstance(n, int) else value


def js_round(n) -> int:
    """``Math.round``: half goes toward +infinity."""
    return int(math.floor(float(n) + 0.5))


def pc_of(name: str) -> int:
    root = name[0].upper()
    acc = 1 if len(name) > 1 and name[1] == "#" else -1 if len(name) > 1 and name[1] == "b" else 0
    return (PC[root] + acc + 12) % 12


def pretty(sym) -> str:
    match = re.match(r"^([A-G](?:#|b)?)(.*)$", sym or "")
    if not match:
        return sym or ""
    return match.group(1) + (PRETTY.get(match.group(2), match.group(2)))


def qpb_of(meter) -> float:
    match = re.match(r"^(\d+)/(2|4|8)$", meter or "")
    if not match:
        return 4
    return int(match.group(1)) * (4 / int(match.group(2)))


def lp_dial(hz) -> int:
    if not (isinstance(hz, (int, float)) and hz > 0) or hz >= 20000:
        return 0
    value = js_round(100 * math.log(hz / 20000) / math.log(150 / 20000))
    return min(100, max(1, value))


def _hp_dial(hz) -> int:
    if not (isinstance(hz, (int, float)) and hz > 20):
        return 0
    value = js_round(100 * math.log(hz / 20) / math.log(100))
    return min(100, max(1, value))


def _clamp(n, lo, hi):
    return lo if n < lo else hi if n > hi else n


def hack_params(abbr, value) -> dict:
    if abbr == "lp":
        return {"cutoff": lp_dial(value)}
    if abbr == "hp":
        return {"cutoff": _hp_dial(value)}
    if abbr == "bp":
        return {"centre_hz": _clamp(value, 100, 8000)}
    if abbr == "dl":
        return {"wet": _clamp(value, 0, 100) / 100}
    if abbr == "rv":
        return {"mix": _clamp(value, 0, 100) / 100}
    if abbr == "dr":
        return {"drive_db": _clamp(value, 0, 24)}
    if abbr == "cp":
        return {"threshold_db": _clamp(value, -60, 0)}
    if abbr == "wd":
        return {"width": _clamp(value, 0, 200) / 100}
    if abbr == "eq":
        return {"mid_db": _clamp(js_round(value), -18, 18)}
    return {}


def opening_tempo(text) -> dict | None:
    """The opening quarter-note BPM.

    A later ``at bar N, BPM`` is blanked first, so it cannot become the opening.
    A ``BPM`` word wins over an earlier ``tempo``, ``q``, or ``quarter =``.
    """
    blank = CHANGE.sub(" ", _uncomment(str(text or "")))
    bpm = _BPM.search(blank)
    if bpm:
        return {"bpm": num(float(bpm.group(1))), "source": "bpm"}
    cands = []
    tempo = _TEMPO.search(blank)
    if tempo:
        cands.append((tempo.start(), float(tempo.group(1)), "tempo"))
    q = _Q.search(blank)
    if q:
        cands.append((q.start(), float(q.group(1)), "q"))
    quarter = _QUARTER.search(blank)
    if quarter:
        cands.append((quarter.start(), float(quarter.group(1)), "quarter"))
    if not cands:
        return None
    cands.sort(key=lambda row: row[0])
    return {"bpm": num(cands[0][1]), "source": cands[0][2]}


def tokenize(text) -> list[dict]:
    src = str(text or "")
    n = len(src)
    i = 0
    out = []
    while i < n:
        c = src[i]
        if c in _SKIP:
            i += 1
            continue
        if c == "#":
            # A ``#`` that starts a token is a comment. ``F#`` keeps the
            # accidental, because that ``#`` is in the middle of a word.
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == '"':
            i += 1
            q = []
            while i < n and src[i] != '"':
                q.append(src[i])
                i += 1
            closed = i < n and src[i] == '"'
            if closed:
                i += 1
            out.append({"type": "quote", "text": "".join(q), "end": i, "closed": closed})
            continue
        if c == "[":
            i += 1
            out.append({"type": "groupStart", "end": i})
            continue
        if c == "]":
            i += 1
            out.append({"type": "groupEnd", "end": i})
            continue
        if c == "|":
            i += 1
            out.append({"type": "bar", "end": i})
            continue
        if c == ":":
            i += 1
            out.append({"type": "word", "text": ":", "end": i})
            continue
        start = i
        word = []
        while i < n and not _brk(src, i):
            # ``(8|)`` is one duration. The bar stays inside the parentheses.
            if src[i] == "(":
                word.append(src[i])
                i += 1
                while i < n and src[i] != ")":
                    word.append(src[i])
                    i += 1
                if i < n and src[i] == ")":
                    word.append(src[i])
                    i += 1
                continue
            word.append(src[i])
            i += 1
        if word:
            out.append({"type": "word", "text": "".join(word), "end": i, "at": start})
    return out


def _brk(src: str, i: int) -> bool:
    return src[i] in _BREAK


def chord_of(token: str, in_group: bool, known) -> dict | None:
    match = re.match(r"^([A-Ga-g])([#b]?)([\s\S]*)$", token)
    if not match:
        return None
    rest = match.group(3)
    qual = []
    k = 0
    while k < len(rest):
        if rest[k] == "(" and re.match(r"^(\d+(?:\.\d+)?\|?|\?)", rest[k + 1:]):
            break
        if rest[k] == "/" and (k + 1 >= len(rest) or rest[k + 1] != "9") and re.match(r"^/[A-Ga-g][#b]?", rest[k:]):
            break
        qm = QCHAR.match(rest[k:])
        if not qm:
            break
        qual.append(qm.group(0))
        k += len(qm.group(0))
    dur = None
    open_dur = False
    paren = False

    def take_dur(at: int):
        d = re.match(r"^\((\d+(?:\.\d+)?)(\|)?\)", rest[at:])
        o = re.match(r"^\(\?\)", rest[at:])
        if d:
            return len(d.group(0)), {"n": float(d.group(1)), "bars": bool(d.group(2))}, False
        if o:
            return len(o.group(0)), None, True
        return None

    got = take_dur(k)
    if got:
        k += got[0]
        dur = got[1]
        open_dur = got[2]
        paren = True
    bass = None
    if k < len(rest) and rest[k] == "/" and (k + 1 >= len(rest) or rest[k + 1] != "9"):
        bm = re.match(r"^/([A-Ga-g][#b]?)", rest[k:])
        if bm:
            bass = bm.group(1)
            k += len(bm.group(0))
            if not paren:
                got = take_dur(k)
                if got:
                    k += got[0]
                    dur = got[1]
                    open_dur = got[2]
                    paren = True
    if k != len(rest):
        return None
    letter = match.group(1)
    q = "".join(qual)
    bare_lower = letter == letter.lower() and letter != letter.upper() and not q and dur is None and not open_dur and not bass
    if bare_lower and not in_group:
        return {"guard": letter}
    quality = q
    if not known(quality) and quality in ALIAS:
        quality = ALIAS[quality]
    if not known(quality):
        shown = letter.upper() + (match.group(2) or "") + q + ("/" + bass if bass else "")
        return {"unknown": shown}
    root = letter.upper() + (match.group(2) or "")
    return {
        "kind": "chord",
        "symbol": SPELL[pc_of(root)] + quality,
        "bass": pc_of(bass) if bass else None,
        "beats": None if dur is None or dur["bars"] else num(dur["n"]),
        "bars": num(dur["n"]) if dur is not None and dur["bars"] else None,
        "openDur": open_dur,
        "paren": paren,
        "lower": letter == letter.lower() and letter != letter.upper(),
        "bareA": letter == "A" and not quality and not bass and dur is None and not open_dur,
    }


def _render_chord(ev) -> str:
    s = pretty(ev.get("symbol"))
    if ev.get("bass") is not None:
        s += "/" + SPELL[ev["bass"]]
    if ev.get("openDur"):
        s += "(?)"
    elif ev.get("bars") is not None:
        s += "(" + trim_num(ev["bars"]) + "|)"
    elif ev.get("beats") is not None:
        s += "(" + trim_num(ev["beats"]) + ")"
    return s


def _render_rest(ev) -> str:
    if ev.get("openDur"):
        return "_(?)"
    if ev.get("bars") is not None:
        return "_(" + trim_num(ev["bars"]) + "|)"
    return "_(" + trim_num(ev.get("beats") or 0) + ")"


def _join_syntax(items) -> str:
    out = ""
    prev = ""
    for it in items:
        if not it or not it.get("text"):
            continue
        if out:
            mus = it.get("kind") in ("chord", "rest", "group")
            was = prev in ("chord", "rest", "group")
            out += " " if mus and was else "; "
        out += it["text"]
        prev = it.get("kind") or ""
    return out


def parse(text, known=None) -> dict:
    """Read one Composition the way the page does.

    ``known`` defaults to the full chord set. Pass a predicate to pin a test
    to a smaller catalog. The return matches the page's parse object.
    """
    if known is None:
        known = known_catalog
    src = str(text or "")
    tokens = tokenize(src)
    syntax = []
    words = []
    notes = []
    played = []
    hacks = []
    seen = {}
    flags = {"paren": False, "rest": False, "loop": False, "lower": False, "quoteChord": False}
    meter = "4/4"
    seed = None
    tempos = []
    next_rec = 0
    state = {"i": 0}

    def note(s):
        if s and s not in seen:
            seen[s] = 1
            notes.append(s)

    def add_word(w):
        if w:
            words.append(w)

    def stamp(ev):
        copy = {
            "kind": ev.get("kind"),
            "symbol": ev.get("symbol"),
            "bass": None if ev.get("bass") is None else ev.get("bass"),
            "beats": ev.get("beats"),
            "bars": ev.get("bars"),
            "openDur": bool(ev.get("openDur")),
            "recording": ev.get("recording"),
        }
        played.append(copy)
        return copy

    def assign(ev):
        nonlocal next_rec
        if ev.get("kind") == "chord" and ev.get("recording") is None:
            ev["recording"] = next_rec
            next_rec += 1
        return ev

    def try_chord(in_group):
        t = tokens[state["i"]] if state["i"] < len(tokens) else None
        if not t or t["type"] != "word":
            return None
        c = chord_of(t["text"], in_group, known)
        if not c:
            return None
        if c.get("guard"):
            note('"' + c["guard"] + '" is a word here, not ' + c["guard"].upper() + " major.")
            return None
        if c.get("unknown"):
            note(c["unknown"] + " is not a chord.")
            return None
        if c.get("bareA") and re.match(r" [a-z]", src[t["end"]:t["end"] + 2] or ""):
            return None
        state["i"] += 1
        if c.get("paren"):
            flags["paren"] = True
        if c.get("lower"):
            flags["lower"] = True
        return c

    def try_rest():
        t = tokens[state["i"]] if state["i"] < len(tokens) else None
        if not t or t["type"] != "word":
            return None
        m = re.match(r"^_\((\d+(?:\.\d+)?)(\|)?\)$", t["text"])
        o = re.match(r"^_\(\?\)$", t["text"])
        if m or o:
            state["i"] += 1
            flags["rest"] = True
            return {
                "kind": "rest",
                "beats": num(float(m.group(1))) if m and not m.group(2) else None,
                "bars": num(float(m.group(1))) if m and m.group(2) else None,
                "openDur": bool(o),
            }
        if not re.match(r"^rest$", t["text"], re.I):
            return None
        n = tokens[state["i"] + 1] if state["i"] + 1 < len(tokens) else None
        if not n or n["type"] != "word" or not re.match(r"^\d+(?:\.\d+)?$", n["text"]):
            return None
        bars = False
        eat = 2
        u = tokens[state["i"] + 2] if state["i"] + 2 < len(tokens) else None
        if u and u["type"] == "word" and re.match(r"^(?:bars?|beats?)$", u["text"], re.I):
            bars = bool(re.match(r"^bar", u["text"], re.I))
            eat = 3
        state["i"] += eat
        flags["rest"] = True
        return {
            "kind": "rest",
            "beats": None if bars else num(float(n["text"])),
            "bars": num(float(n["text"])) if bars else None,
            "openDur": False,
        }

    def try_hack():
        t = tokens[state["i"]] if state["i"] < len(tokens) else None
        if not t or t["type"] != "word":
            return False
        name = value = ramp = None
        eat = 0
        glued = re.match(
            r"^(lowpass|highpass|bandpass|reverb|delay|drive|width|comp|eq|lp|hp|bp|dl|rv|dr|cp|wd)(\d+(?:\.\d+)?)(?:->(\d+(?:\.\d+)?))?$",
            t["text"], re.I,
        )
        if glued:
            name = glued.group(1)
            value = float(glued.group(2))
            ramp = float(glued.group(3)) if glued.group(3) is not None else None
            eat = 1
        elif HACK_WORD.match(t["text"]):
            n = tokens[state["i"] + 1] if state["i"] + 1 < len(tokens) else None
            val = re.match(r"^(\d+(?:\.\d+)?)(?:->(\d+(?:\.\d+)?))?$", n["text"]) if n and n["type"] == "word" else None
            if not val:
                return False
            name = t["text"]
            value = float(val.group(1))
            ramp = float(val.group(2)) if val.group(2) is not None else None
            eat = 2
        else:
            return False
        state["i"] += eat
        abbr = HACK[name.lower()]
        prev = None
        for h in hacks:
            if h["abbr"] == abbr:
                prev = h
        value = num(value)
        ramp = None if ramp is None else num(ramp)
        if prev and prev["value"] != value:
            note(abbr + " was written twice. The last value is the start.")
        row = {"abbr": abbr, "type": HACK_TYPE[abbr], "value": value, "ramp": ramp, "params": hack_params(abbr, value)}
        if prev:
            prev["value"] = value
            prev["ramp"] = ramp
            prev["params"] = row["params"]
            shown = None
            for si in range(len(syntax) - 1, -1, -1):
                if syntax[si].get("kind") == "hack" and str(syntax[si].get("text") or "").startswith(abbr):
                    shown = syntax[si]
                    break
            if shown:
                shown["text"] = abbr + trim_num(value)
        else:
            hacks.append(row)
            syntax.append({"kind": "hack", "text": abbr + trim_num(value)})
        if ramp is not None:
            note("->" + trim_num(ramp) + " is a ramp. A person plays ramps in Live.")
        return True

    def remember_tempo(n):
        tempos.append(num(n))
        if len(tempos) == 1:
            syntax.append({"kind": "tempo", "text": "q" + trim_num(n)})

    def try_tempo():
        t = tokens[state["i"]] if state["i"] < len(tokens) else None
        if not t or t["type"] != "word":
            return False
        g = re.match(r"^(tempo|q)(\d+(?:\.\d+)?)$", t["text"], re.I)
        if g:
            state["i"] += 1
            remember_tempo(float(g.group(2)))
            return True
        nxt = tokens[state["i"] + 1] if state["i"] + 1 < len(tokens) else None
        if re.match(r"^tempo$", t["text"], re.I) and nxt and re.match(r"^\d+(?:\.\d+)?$", nxt["text"]):
            remember_tempo(float(nxt["text"]))
            state["i"] += 2
            return True
        if re.match(r"^q$", t["text"], re.I) and nxt and re.match(r"^\d+(?:\.\d+)?$", nxt["text"]):
            remember_tempo(float(nxt["text"]))
            state["i"] += 2
            return True
        if re.match(r"^quarter$", t["text"], re.I):
            j = state["i"] + 1
            if j < len(tokens) and re.match(r"^notes?$", tokens[j]["text"], re.I):
                j += 1
            if j < len(tokens) and tokens[j]["text"] == "=":
                j += 1
            elif j < len(tokens) and re.match(r"^=(\d+(?:\.\d+)?)$", tokens[j]["text"]):
                remember_tempo(float(tokens[j]["text"][1:]))
                state["i"] = j + 1
                return True
            if j < len(tokens) and re.match(r"^\d+(?:\.\d+)?$", tokens[j]["text"]):
                remember_tempo(float(tokens[j]["text"]))
                state["i"] = j + 1
                return True
        glued_bpm = re.match(r"^(\d+(?:\.\d+)?)BPM$", t["text"], re.I)
        if glued_bpm:
            remember_tempo(float(glued_bpm.group(1)))
            state["i"] += 1
            return True
        if re.match(r"^\d+(?:\.\d+)?$", t["text"]) and nxt and re.match(r"^BPM$", nxt["text"], re.I):
            remember_tempo(float(t["text"]))
            state["i"] += 2
            return True
        return False

    def try_seed():
        nonlocal seed
        t = tokens[state["i"]] if state["i"] < len(tokens) else None
        if not t or t["type"] != "word":
            return False
        g = re.match(r"^(?:seed|sd)(\d+)$", t["text"], re.I)
        n = None
        eat = 0
        nxt = tokens[state["i"] + 1] if state["i"] + 1 < len(tokens) else None
        if g:
            n = int(g.group(1))
            eat = 1
        elif re.match(r"^(?:seed|sd)$", t["text"], re.I) and nxt and re.match(r"^\d+$", nxt["text"]):
            n = int(nxt["text"])
            eat = 2
        else:
            return False
        state["i"] += eat
        if seed is None:
            seed = n
            syntax.append({"kind": "seed", "text": "sd" + str(n)})
        elif seed != n:
            note("Another seed was written. The first is kept.")
        return True

    def try_length():
        t = tokens[state["i"]] if state["i"] < len(tokens) else None
        if not t or t["type"] != "word" or not re.match(r"^\d+(?:\.\d+)?$", t["text"]):
            return False
        u = tokens[state["i"] + 1] if state["i"] + 1 < len(tokens) else None
        if not u or u["type"] != "word":
            return False
        amount = float(t["text"])
        label = None
        if re.match(r"^bars?$", u["text"], re.I):
            label = trim_num(amount) + (" bar" if amount == 1 else " bars")
        elif re.match(r"^(?:minutes?|mins?|m)$", u["text"], re.I):
            label = trim_num(amount) + (" minute" if amount == 1 else " minutes")
        elif re.match(r"^(?:seconds?|secs?|s)$", u["text"], re.I):
            label = trim_num(amount) + (" second" if amount == 1 else " seconds")
        if not label:
            return False
        state["i"] += 2
        syntax.append({"kind": "length", "text": label})
        return True

    def try_meter():
        nonlocal meter
        t = tokens[state["i"]] if state["i"] < len(tokens) else None
        if not t or t["type"] != "word" or not re.match(r"^\d+/(?:2|4|8)$", t["text"]):
            return False
        meter = t["text"]
        state["i"] += 1
        if meter != "4/4":
            syntax.append({"kind": "meter", "text": meter})
        return True

    def try_loop():
        t = tokens[state["i"]] if state["i"] < len(tokens) else None
        if not t or t["type"] != "word":
            return 0
        m = (
            re.match(r"^loop\((\d+(?:\.\d+)?)\)?$", t["text"], re.I)
            or re.match(r"^x\((\d+(?:\.\d+)?)\)?$", t["text"], re.I)
            or re.match(r"^x(\d+(?:\.\d+)?)$", t["text"], re.I)
        )
        n = None
        eat = 0
        nxt = tokens[state["i"] + 1] if state["i"] + 1 < len(tokens) else None
        if m:
            n = float(m.group(1))
            eat = 1
        elif re.match(r"^loop$", t["text"], re.I) and nxt and re.match(r"^\(?(\d+(?:\.\d+)?)\)?$", nxt["text"]):
            n = float(re.sub(r"[()]", "", nxt["text"]))
            eat = 2
        elif re.match(r"^x$", t["text"], re.I) and nxt and re.match(r"^\d+(?:\.\d+)?$", nxt["text"]):
            n = float(nxt["text"])
            eat = 2
        else:
            return 0
        # A fractional loop is consumed here. The caller still sees 0, and the
        # page's outer loop then keeps the original word. Both sides do that.
        if not (n > 0) or abs(n - js_round(n)) > 1e-9:
            state["i"] += eat
            note("loop needs a whole number.")
            return 0
        state["i"] += eat
        return js_round(n)

    def check_bars(items):
        qpb = qpb_of(meter)
        seg = []
        bad = False

        def flush():
            if not seg:
                return
            beats = 0
            open_ = False
            for it in seg:
                if it.get("openDur") or (it.get("beats") is None and it.get("bars") is None):
                    open_ = True
                else:
                    beats += it["bars"] * qpb if it.get("bars") is not None else it["beats"]
            if open_:
                note("A bar line needs a duration on each chord.")
            elif abs(beats - qpb) > 1e-6:
                note("The beats before | add up to " + trim_num(beats) + ", not " + trim_num(qpb) + ".")
            seg.clear()

        for it in items:
            if it.get("kind") == "bar":
                flush()
                bad = True
            else:
                seg.append(it)
        if bad:
            flush()

    def expand(items, times):
        for pass_n in range(times):
            for it in items:
                if it.get("kind") == "bar":
                    continue
                if pass_n == 0:
                    assign(it)
                stamp(it)

    def render_items(items, times):
        parts = []
        for it in items:
            if it.get("kind") == "bar":
                parts.append("|")
            elif it.get("kind") == "rest":
                parts.append(_render_rest(it))
            elif it.get("kind") == "chord":
                parts.append(_render_chord(it))
        s = "[" + " ".join(parts) + "]"
        if times > 1:
            s += "x" + str(times)
        return s

    def at_statement():
        if try_hack():
            return True
        if try_tempo():
            return True
        if try_seed():
            return True
        if try_meter():
            return True
        if try_length():
            return True
        rest = try_rest()
        if rest:
            assign(rest)
            stamp(rest)
            syntax.append({"kind": "rest", "text": _render_rest(rest)})
            return True
        ch = try_chord(False)
        if ch:
            assign(ch)
            stamp(ch)
            syntax.append({"kind": "chord", "text": _render_chord(ch)})
            return True
        times = try_loop()
        if times:
            note("loop needs a group.")
            flags["loop"] = True
            return True
        return False

    def take_quote():
        t = tokens[state["i"]]
        state["i"] += 1
        add_word('"' + t["text"] + '"')
        if not t.get("closed"):
            note("A quote was not closed.")
        note("Quoted words stay words.")
        if _quote_has_chord(t["text"]):
            flags["quoteChord"] = True

    def _quote_has_chord(q):
        for inner in tokenize(q):
            if inner["type"] != "word":
                continue
            c = chord_of(inner["text"], False, known)
            if c and not c.get("guard") and not c.get("unknown") and not c.get("lower") and not c.get("bareA"):
                return True
        return False

    def is_legacy():
        a = tokens[state["i"]] if state["i"] < len(tokens) else None
        b = tokens[state["i"] + 1] if state["i"] + 1 < len(tokens) else None
        return bool(
            a and a["type"] == "word" and re.match(r"^\d+(?:\.\d+)?$", a["text"])
            and b and b["type"] == "word" and re.match(r"^(?:bars?|beats?)$", b["text"], re.I)
        )

    def parse_legacy():
        n = float(tokens[state["i"]]["text"])
        bars = bool(re.match(r"^bar", tokens[state["i"] + 1]["text"], re.I))
        state["i"] += 2
        chord = None
        while state["i"] < len(tokens) and tokens[state["i"]]["type"] != "groupEnd":
            if tokens[state["i"]]["type"] == "quote":
                take_quote()
                continue
            if not chord:
                ch = try_chord(True)
                if ch:
                    chord = ch
                    continue
            if state["i"] < len(tokens) and tokens[state["i"]]["type"] == "word":
                add_word(tokens[state["i"]]["text"])
                state["i"] += 1
                continue
            if state["i"] < len(tokens) and tokens[state["i"]]["type"] != "groupEnd":
                state["i"] += 1
            else:
                break
        if state["i"] < len(tokens) and tokens[state["i"]]["type"] == "groupEnd":
            state["i"] += 1
        if not chord:
            return
        chord["beats"] = None if bars else num(n)
        chord["bars"] = num(n) if bars else None
        chord["paren"] = False
        assign(chord)
        stamp(chord)
        syntax.append({"kind": "chord", "text": _render_chord(chord)})

    def parse_group():
        state["i"] += 1
        if is_legacy():
            parse_legacy()
            return
        items = []
        while state["i"] < len(tokens) and tokens[state["i"]]["type"] != "groupEnd":
            t = tokens[state["i"]]
            if t["type"] == "groupStart":
                note("Nested groups are not read.")
                state["i"] += 1
                continue
            if t["type"] == "bar":
                items.append({"kind": "bar"})
                state["i"] += 1
                continue
            if t["type"] == "quote":
                take_quote()
                continue
            if try_hack():
                note("Hacks are for the whole piece.")
                continue
            if try_tempo() or try_seed() or try_meter() or try_length():
                continue
            rest = try_rest()
            if rest:
                items.append(rest)
                continue
            ch = try_chord(True)
            if ch:
                items.append(ch)
                continue
            if t["type"] == "word":
                add_word(t["text"])
                state["i"] += 1
                continue
            state["i"] += 1
        if state["i"] < len(tokens) and tokens[state["i"]]["type"] == "groupEnd":
            state["i"] += 1
        times = try_loop()
        if not (times > 0):
            times = 1
        if times > 1:
            flags["loop"] = True
        check_bars(items)
        expand(items, times)
        syntax.append({"kind": "group", "text": render_items(items, times)})

    while state["i"] < len(tokens):
        t = tokens[state["i"]]
        if t["type"] == "quote":
            take_quote()
            continue
        if t["type"] == "groupStart":
            parse_group()
            continue
        if t["type"] in ("groupEnd", "bar"):
            state["i"] += 1
            continue
        if at_statement():
            continue
        if t["type"] == "word":
            add_word(t["text"])
            state["i"] += 1
            continue
        state["i"] += 1

    ot = opening_tempo(src)
    bpm = ot["bpm"] if ot else (tempos[0] if tempos else None)
    if len(tempos) > 1 and any(n != bpm for n in tempos):
        note("Another tempo was written. " + ("The BPM is kept." if ot and ot.get("source") == "bpm" else "The first tempo is kept."))
    tempo_item = None
    for s in syntax:
        if s.get("kind") == "tempo" and tempo_item is None:
            tempo_item = s
    if bpm is not None:
        shown = "q" + trim_num(bpm)
        if tempo_item:
            tempo_item["text"] = shown
        else:
            syntax.insert(0, {"kind": "tempo", "text": shown})
    qpb = qpb_of(meter)
    needs_tempo = False
    for ev in played:
        beats = ev.get("beats")
        if ev.get("bars") is not None:
            beats = ev["bars"] * qpb
        if beats is not None and not (isinstance(bpm, (int, float)) and bpm > 0):
            needs_tempo = True
            ev["seconds"] = None
        elif beats is not None and isinstance(bpm, (int, float)) and bpm > 0:
            ev["seconds"] = num(round3(beats * 60 / bpm))
        else:
            ev["seconds"] = None
    if needs_tempo:
        note("A duration needs a tempo.")
    chords = [e for e in played if e.get("kind") == "chord"]
    timed = [e for e in chords if e.get("beats") is not None or e.get("bars") is not None]
    loose = [e for e in chords if e.get("beats") is None and e.get("bars") is None]
    if timed and loose:
        note("A duration stays on its own chord. The others play their whole recordings.")
    if any(e.get("openDur") for e in chords):
        note("An open duration plays the whole recording.")
    if flags["loop"]:
        note("The loop uses the same recordings.")
    clock = "language" if flags["paren"] or flags["rest"] or flags["loop"] or flags["lower"] or flags["quoteChord"] else "legacy"
    if clock == "language" and CHANGE.search(_uncomment(src)):
        note("A later tempo is not read yet. The opening tempo is kept.")
    if words:
        note("Words are left for the AI.")
    gather = []
    gathered = set()
    for c in chords:
        if c.get("recording") in gathered:
            continue
        gathered.add(c.get("recording"))
        gather.append(c.get("symbol"))
    line = _join_syntax(syntax)
    iread = line
    if words:
        iread += ("\n" if iread else "") + " ".join(words)
    for s in notes:
        iread += ("\n" if iread else "") + s
    return {
        "bpm": bpm, "meter": meter, "seed": seed, "clock": clock, "hacks": hacks,
        "played": played, "gather": gather, "words": words, "notes": notes,
        "iread": iread,
    }


def place(played, caps) -> dict:
    """Lay events on a clock. A written duration keeps its slot.

    The next event starts on time even when the file is shorter. A longer
    file is trimmed. An open chord plays its whole cap, and the next event
    follows it. ``caps`` line up with chord events; rests are skipped.
    """
    cursor = 0.0
    n = 0
    entries = []
    played = played or []
    caps = list(caps) if caps else []
    for ev in played:
        if ev.get("kind") == "rest":
            sec = ev.get("seconds")
            if isinstance(sec, (int, float)) and not isinstance(sec, bool) and sec > 0:
                cursor = round3(cursor + sec)
            continue
        raw = caps[n] if n < len(caps) else 0
        n += 1
        cap = raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0 else 0
        slot = ev.get("seconds")
        length = cap if slot is None else slot
        if not (isinstance(length, (int, float)) and not isinstance(length, bool) and length > 0):
            if cap > 0:
                length = cap
            elif slot is None:
                length = 0
            else:
                length = min(slot, 0.1)
        if cap > 0 and length > cap + 0.001:
            length = math.floor(cap * 1000) / 1000
        if not (isinstance(length, (int, float)) and not isinstance(length, bool) and length > 0):
            length = 0.1
        length = round3(length)
        entries.append({
            "at_s": num(round3(cursor)),
            "len_s": num(length),
            "recording": ev.get("recording"),
            "symbol": ev.get("symbol") or "",
        })
        cursor = round3(cursor + (length if slot is None else slot))
    return {"entries": entries, "total_s": num(round3(cursor))}


def uses_language(text: str) -> bool:
    """True when Gather should follow the language clock.

    A bar table still wins, so a prelude keeps its bar order. A regather of
    one chord is decided by the caller, not here.
    """
    parsed = parse(text or "")
    if parsed.get("clock") != "language":
        return False
    from . import take
    return take.bar_chords(text or "") is None


def plan_doc(parsed: dict) -> dict:
    """Unique recordings, the played order, and provisional slots.

    Open chords are 0.1 s until the file is measured. A written duration
    already has its seconds. More than the chord limit is shortened.
    """
    from .take import CHORD_LIMIT

    played = [dict(ev) for ev in (parsed.get("played") or [])]
    slice_note = None
    chord_at = [i for i, ev in enumerate(played) if ev.get("kind") == "chord"]
    if len(chord_at) > CHORD_LIMIT:
        slice_note = (
            f"The piece had {len(chord_at)} chords; it was shortened to {CHORD_LIMIT} "
            "so the Performance stays within the chord limit."
        )
        played = played[: chord_at[CHORD_LIMIT - 1] + 1]
    unique = []
    seen = {}
    share = []
    expanded = []
    symbols = []
    plays = {}
    for ev in played:
        if ev.get("kind") != "chord":
            continue
        rec = int(ev["recording"])
        if rec not in seen:
            seen[rec] = len(unique)
            unique.append((ev.get("symbol"), ev.get("bass")))
        pos = seen[rec]
        plays.setdefault(pos, []).append(len(share))
        share.append(pos)
        expanded.append((ev.get("symbol"), ev.get("bass")))
        symbols.append(ev.get("symbol"))
    laid = place(played, None)
    slots = [{"t": row["at_s"], "dur": row["len_s"]} for row in laid["entries"]]
    total = float(laid["total_s"] or 0)
    hacks = []
    for hack in parsed.get("hacks") or []:
        hacks.append({
            "abbr": hack.get("abbr"),
            "type": hack.get("type"),
            "value": hack.get("value"),
            "ramp": hack.get("ramp"),
        })
    return {
        "unique": unique,
        "expanded": expanded,
        "share": share,
        "plays": plays,
        "symbols": symbols,
        "slots": slots,
        "section_spec": [(len(slots), total)],
        "slice_note": slice_note,
        "clock": {
            "played": played,
            "bpm": parsed.get("bpm"),
            "meter": parsed.get("meter") or "4/4",
            "hacks": hacks,
            "measured": False,
        },
    }


def apply_language_durations(plan: dict, caps) -> dict:
    """Replace provisional slots with ``place`` once each file's length is known."""
    lc = plan.get("language_clock") or {}
    laid = place(lc.get("played") or [], caps)
    chords = plan.get("chords") or []
    for index, row in enumerate(laid["entries"]):
        if index >= len(chords) or not isinstance(chords[index], dict):
            break
        chords[index]["t"] = row["at_s"]
        chords[index]["dur"] = row["len_s"]
    total = float(laid["total_s"] or 0)
    plan["length_s"] = max(5.0, round(total, 3))
    plan["sections"] = [{"id": "A", "start_s": 0.0, "end_s": round(total, 3)}]
    sync_language_rows(plan)
    return laid


def sync_language_rows(plan: dict) -> None:
    """Copy chord times onto clips and events after a place or a length clamp."""
    chords = plan.get("chords") or []
    clips = plan.get("clips") or []
    for index, chord in enumerate(chords):
        if not isinstance(chord, dict) or index >= len(clips) or not isinstance(clips[index], dict):
            continue
        clips[index]["t"] = chord.get("t")
        clips[index]["dur"] = chord.get("dur")
    for layer in plan.get("layers") or []:
        if not isinstance(layer, dict):
            continue
        for ev in layer.get("events") or []:
            if not isinstance(ev, dict):
                continue
            idx = ev.get("chord_index")
            if isinstance(idx, int) and 0 <= idx < len(chords):
                ev["t"] = chords[idx].get("t")
                ev["dur_s"] = chords[idx].get("dur")


def settle(plan: dict, caps, *, max_length_s: float = 180.0, max_sources: int = 10000, max_chords: int = 40, max_layers: int = 64, max_plan_bytes: int = 8 * 1024 * 1024) -> None:
    """Place measured files, then keep the existing length cap. Idempotent."""
    lc = plan.get("language_clock")
    if not isinstance(lc, dict) or lc.get("measured"):
        return
    apply_language_durations(plan, caps)
    from .fit import fit_plan
    fit_plan(
        plan,
        max_layers=max_layers,
        max_sources=max_sources,
        max_chords=max_chords,
        max_length_s=max_length_s,
        max_plan_bytes=max_plan_bytes,
    )
    sync_language_rows(plan)
    lc["measured"] = True


def clock_view(plan: dict) -> dict | None:
    """The clock a fallback page can read. The create page places the files itself.

    Written when any played chord has a duration. An all-open piece returns
    None, which is the organic rule: each recording follows the one before it.
    """
    lc = plan.get("language_clock") or {}
    if not isinstance(lc, dict):
        return None
    played = [ev for ev in (lc.get("played") or []) if isinstance(ev, dict) and ev.get("kind") == "chord"]
    if not any(ev.get("seconds") is not None for ev in played):
        return None
    chords = [c for c in (plan.get("chords") or []) if isinstance(c, dict)]
    if not chords:
        return None
    bpm = lc.get("bpm")
    tempo = []
    if isinstance(bpm, (int, float)) and not isinstance(bpm, bool) and bpm > 0:
        tempo.append({"bar": 1, "bpm": float(bpm)})
    return {
        "written": True,
        "durations_s": [float(c.get("dur") or 0) for c in chords],
        "slots": [{"t": float(c.get("t") or 0), "dur": float(c.get("dur") or 0)} for c in chords],
        "tempo": tempo,
        "quarters_per_bar": qpb_of(lc.get("meter") or "4/4"),
    }
