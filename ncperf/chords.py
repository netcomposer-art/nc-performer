"""Chord symbols: parse, spell for the full chord set, and pick voicings by voice leading.
The musicmatter full chord set (CC BY-SA 4.0, Peter Semmelhack) has 35 qualities x 12 roots = 420 symbols, 2256 recorded voicings."""
import json, os, re
CAT = json.load(open(os.path.join(os.path.dirname(__file__), 'data', 'fullchords_catalog.json')))
PC = dict(C=0, D=2, E=4, F=5, G=7, A=9, B=11)
ROOT_SPELL = {0: 'C', 1: 'Db', 2: 'D', 3: 'Eb', 4: 'E', 5: 'F', 6: 'F#', 7: 'G', 8: 'Ab', 9: 'A', 10: 'Bb', 11: 'B'}
ALIASES = [('maj', ''), ('M', ''), ('min', 'm'), ('-', 'm'), ('mi', 'm'), ('Δ7', 'maj7'), ('Δ', 'maj7'), ('M7', 'maj7'), ('ma7', 'maj7'), ('M9', 'maj9'),
           ('ø7', 'm7b5'), ('ø', 'm7b5'), ('°7', 'dim7'), ('o7', 'dim7'), ('°', 'dim'), ('o', 'dim'), ('+', 'aug'), ('#5', 'aug'),
           ('mMaj7', 'm-maj7'), ('mmaj7', 'm-maj7'), ('m(maj7)', 'm-maj7'), ('madd9', 'm-add9'), ('m(add9)', 'm-add9'), ('6/9', '6-9'), ('69', '6-9'), ('m6/9', 'm6-9'), ('m69', 'm6-9'),
           ('sus', 'sus4'), ('7sus', '7sus4'), ('9sus', '9sus4'), ('add2', 'add9'), ('2', 'sus2'), ('7+5', 'maj7#5')]
QUALS = sorted({re.sub(r'^[A-G](#|b)?', '', k) for k in CAT['chords']}, key=len, reverse=True)
TOKEN = re.compile(r'(?<![A-Za-z#])([A-G](?:#|b)?)((?:maj|min|mi|m|M|dim|aug|sus|add|alt|ø|°|Δ|o|\+|-|[0-9]|#|b|\(|\)|/(?=9))*)(/[A-G](?:#|b)?)?(?![A-Za-z])')

def spell(sym):
    """'C#m7' -> 'Dbm7' (the spelling the full chord set uses), or None if the quality is not in the set. Slash bass is dropped."""
    m = re.fullmatch(r'\s*([A-G])(#|b)?(.*?)(?:/[A-G](?:#|b)?)?\s*', sym)
    if not m: return None
    pc = (PC[m.group(1)] + {'#': 1, 'b': -1, None: 0}[m.group(2)]) % 12; q = m.group(3)
    if q not in QUALS:
        for a, b in ALIASES:
            if q == a: q = b; break
    if q not in QUALS: return None
    return ROOT_SPELL[pc] + q

def find(text): return [s for s, _ in find_full(text)]

def _pc(name): return (PC[name[0]] + {'#': 1, 'b': -1}.get(name[1:2], 0)) % 12

def find_full(text):
    """Chord symbols in plain text, in order, with the slash bass as a pitch class (or None)."""
    out = []
    for m in TOKEN.finditer(text):
        if m.group(1) == 'A' and not m.group(2) and not m.group(3) and re.match(r' [a-z]', text[m.end():m.end() + 2]): continue  # the article "A"
        s = spell(m.group(1) + m.group(2).rstrip('(').rstrip(')') )
        if s: out.append((s, _pc(m.group(3)[1:]) if m.group(3) else None))
    return out

def voicings(sym): return CAT['chords'].get(spell(sym) or '', [])

def pick(sym, prev=None, center=60, lo=40, hi=84, bass=None):
    """Choose a recorded voicing: closest to the previous one (voice leading), else nearest the center.
    With a slash bass, prefer voicings whose lowest note is that bass."""
    vs = [v for v in voicings(sym) if lo <= min(v[1]) and max(v[1]) <= hi] or voicings(sym)
    if bass is not None: vs = [v for v in vs if min(v[1]) % 12 == bass] or vs
    if not vs: return None
    def cost(v):
        n = v[1]; c = abs(sum(n) / len(n) - center) * 0.5
        if prev: c += sum(min(abs(a - b) for b in prev) for a in n) / len(n) + sum(min(abs(a - b) for a in n) for b in prev) / len(prev)
        return c
    return min(vs, key=cost)

def manifest_url(path): return CAT['manifest_base'] + path + '.nc.json'
def audio_url(path): return CAT['audio_base'] + path + '.flac'
