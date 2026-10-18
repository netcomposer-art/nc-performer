"""License rules. Inputs: CC0, CC BY, CC BY-SA or public domain. Never NC, ND, Sampling+ or custom. Output: CC BY-SA 4.0."""
import re

OUT = dict(id='CC-BY-SA-4.0', name='CC BY-SA 4.0', uri='https://creativecommons.org/licenses/by-sa/4.0/')
_CC = re.compile(r'creativecommons\.org/(licenses/([a-z\-+]+)/(\d\.\d)|publicdomain/(zero|mark)/(1\.0))')

def from_url(u):
    """Map a Creative Commons URL to (id, name, uri) or None."""
    m = _CC.search(u or '')
    if not m: return None
    if m.group(4) == 'zero': return ('CC0-1.0', 'CC0 1.0', 'https://creativecommons.org/publicdomain/zero/1.0/')
    if m.group(4) == 'mark': return ('PDM-1.0', 'Public Domain Mark 1.0', 'https://creativecommons.org/publicdomain/mark/1.0/')
    kind, ver = m.group(2), m.group(3)
    return ('CC-' + kind.upper() + '-' + ver, 'CC ' + kind.upper().replace('-', '-') + ' ' + ver, f'https://creativecommons.org/licenses/{kind}/{ver}/')

def from_id(i):
    i = (i or '').strip()
    if i.upper() in ('CC0', 'CC0-1.0'): return ('CC0-1.0', 'CC0 1.0', 'https://creativecommons.org/publicdomain/zero/1.0/')
    m = re.fullmatch(r'CC-(BY(?:-SA)?)-(\d\.\d)', i, re.I)
    if m: return (f'CC-{m.group(1).upper()}-{m.group(2)}', f'CC {m.group(1).upper()} {m.group(2)}', f'https://creativecommons.org/licenses/{m.group(1).lower()}/{m.group(2)}/')
    if i.upper() in ('PD', 'PUBLIC-DOMAIN', 'PDM-1.0'): return ('PD', 'Public domain', None)
    return (i, i, None)

def allowed(lid):
    """True for CC0, PD/PDM, CC BY, CC BY-SA (any version). False for NC, ND, Sampling+ and anything else."""
    l = (lid or '').upper()
    if any(x in l for x in ('-NC', '-ND', 'SAMPLING')): return False
    return l in ('CC0-1.0', 'PD', 'PDM-1.0', 'PD-EQUIVALENT') or bool(re.fullmatch(r'CC-BY(-SA)?-\d\.\d', l))

def needs_credit(lid): return (lid or '').upper().startswith('CC-BY')

def page_licenses(html):
    """All CC licenses linked from a page (as ids)."""
    return sorted({from_url(m.group(0))[0] for m in _CC.finditer(html or '')})
