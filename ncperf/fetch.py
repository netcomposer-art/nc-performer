"""Fetch one planned sound and check its license on the live page or manifest.
Which sounds to try is decided by ncperf.discover. This module does not rank suppliers.
Downloads are cached by URL (and byte range)."""
import hashlib, html as _html, json, os, re, time, urllib.request
from . import licenses as LIC
from .util import sha256_file

UA = 'nc-performer-kit/0.1 (+https://netcomposer.art/agents/)'
REGISTRY = os.environ.get('NC_SUPPLIERS_JSON', 'https://netcomposer.art/suppliers.json')
IOWA_TERMS_URL = 'https://theremin.music.uiowa.edu/MIS.html'
IOWA_TERMS = 'Since 1997, these recordings have been freely available on this website and may be downloaded and used for any projects, without restrictions.'

def cache_dir():
    d = os.environ.get('NC_CACHE') or os.path.join(os.path.expanduser('~'), '.cache', 'nc_kit')
    os.makedirs(d, exist_ok=True); return d

def http(url, range_bytes=None, timeout=120, tries=3):
    h = {'User-Agent': UA}
    if range_bytes: h['Range'] = f'bytes=0-{int(range_bytes) - 1}'
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                return r.read(), r.geturl()
        except Exception as e:
            if k == tries - 1: raise
            time.sleep(2 * (k + 1))

_text = {}
def text(url):
    if url not in _text:
        b, final = http(url); _text[url] = (b.decode('utf-8', 'replace'), final)
    return _text[url]

def download(url, range_bytes=None, ext=None):
    key = hashlib.sha1(f'{url}|{range_bytes}'.encode()).hexdigest()[:20]
    ext = ext or os.path.splitext(url.split('?')[0])[1] or '.bin'
    p = os.path.join(cache_dir(), key + ext.replace('%20', ''))
    if not os.path.exists(p):
        b, _ = http(url, range_bytes=range_bytes, timeout=600)
        if range_bytes and len(b) > range_bytes: b = b[:range_bytes]
        tmp = p + '.part'; open(tmp, 'wb').write(b); os.replace(tmp, p)
    return p

# ---------------- registered Suppliers ----------------
_reg = {}
def registry():
    if 'r' not in _reg: _reg['r'] = json.loads(text(REGISTRY)[0])
    return _reg['r']

_idx = {}
def supplier_for_manifest(manifest_url):
    """Find the active registered Supplier whose index lists this manifest."""
    for s in registry().get('suppliers', []):
        if s.get('status') != 'active': continue
        if s['index'] not in _idx: _idx[s['index']] = json.loads(text(s['index'])[0])
        for o in _idx[s['index']].get('offerings', []):
            if o.get('manifest') == manifest_url: return s, _idx[s['index']], o
    return None, None, None

def resolve(src):
    """Resolve one plan source to a local file plus provenance. Raises on any license problem."""
    k = src['kind']; now = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    if k == 'supplier':
        s, idx, off = supplier_for_manifest(src['manifest'])
        if not s: raise ValueError(f"manifest not listed by any active registered Supplier: {src['manifest']}")
        m = json.loads(text(src['manifest'])[0]); lid = m.get('license') or idx.get('default_license')
        L = LIC.from_id(lid)
        if not LIC.allowed(L[0]): raise ValueError(f'license not allowed: {lid}')
        p = download(m['audio'])
        sha = sha256_file(p)
        if m.get('sha256') and sha != m['sha256']: raise ValueError(f"sha256 mismatch for {m['audio']}")
        cr = m.get('creator') or idx.get('creator') or {}
        return dict(path=p, audio_url=m['audio'], what='Supplier audio as listed in its manifest (sha256 matches the manifest)', source='Supplier: ' + s['name'],
                    source_url=src['manifest'], title=m.get('title', os.path.basename(m['audio'])), author=cr.get('name', s.get('display_name')),
                    supplier=dict(display_name=cr.get('name', s.get('display_name')), primary_link=cr.get('link', s.get('url')), registry_id=s.get('id')),
                    license=dict(id=L[0], name=L[1], uri=L[2], verified_live=f"{now}, manifest {src['manifest']}"), sha256=sha, size_bytes=os.path.getsize(p),
                    chord=m.get('chord'), partial=False)
    if k == 'freesound':
        page, final = text(f"https://freesound.org/s/{src['id']}/")
        lic = [l for l in LIC.page_licenses(page)]
        if len(lic) != 1: raise ValueError(f"freesound {src['id']}: could not read exactly one license from the live page ({lic})")
        if not LIC.allowed(lic[0]): raise ValueError(f"freesound {src['id']}: license not allowed: {lic[0]}")
        lq = re.search(r'https://cdn\.freesound\.org/previews/\d+/[\w-]+-lq\.ogg', page)
        if not lq: raise ValueError(f"freesound {src['id']}: no preview URL on the page")
        url = lq.group(0).replace('-lq.ogg', '-hq.ogg')
        artist = re.search(r'og:audio:artist" content="([^"]*)"', page); title = re.search(r'og:audio:title" content="([^"]*)"', page)
        rb = src.get('range_bytes'); p = download(url, rb, '.ogg'); L = LIC.from_id(lic[0])
        if L[2] is None: L = LIC.from_url(re.search(LIC._CC, page).group(0))
        a = _html.unescape(artist.group(1)) if artist else src.get('author', '?')
        what = 'Freesound HQ preview (lossy Ogg Vorbis); originals need OAuth, so the preview is used'
        if rb: what += f'; only the first {rb:,} bytes were fetched (HTTP Range) and hashed'
        return dict(path=p, audio_url=url, what=what, source='Freesound', source_url=final if '/sounds/' in final else f"https://freesound.org/s/{src['id']}/",
                    title=_html.unescape(title.group(1)) if title else src['id'], author=a, supplier=dict(display_name=a, primary_link=f'https://freesound.org/people/{a}/'),
                    license=dict(id=L[0], name=L[1], uri=L[2], verified_live=f'{now}, {final}'), sha256=sha256_file(p), size_bytes=os.path.getsize(p), partial=bool(rb))
    if k == 'uiowa_mis':
        page, _ = text(IOWA_TERMS_URL)
        if IOWA_TERMS not in _html.unescape(page): raise ValueError('UIowa MIS terms not found on the live page; not using it')
        p = download(src['url'], ext=os.path.splitext(src['url'])[1])
        f = urllib.request.unquote(src['url'].rsplit('/', 1)[1])
        return dict(path=p, audio_url=src['url'], what='UIowa MIS original (lossless AIFF), resampled to 48 kHz on decode, pitch unchanged', source='University of Iowa Musical Instrument Samples (MIS)',
                    source_url=src.get('page', IOWA_TERMS_URL), title=f, author='University of Iowa Electronic Music Studios (Lawrence Fritts, director)',
                    supplier=dict(display_name='University of Iowa Electronic Music Studios', primary_link=IOWA_TERMS_URL),
                    license=dict(id='PD-equivalent', name='Public domain equivalent (UIowa MIS: no restrictions)', uri=IOWA_TERMS_URL, terms_verbatim=IOWA_TERMS, verified_live=f'{now}, {IOWA_TERMS_URL}'),
                    sha256=sha256_file(p), size_bytes=os.path.getsize(p), partial=False)
    if k == 'commons':
        L = LIC.from_id(src.get('license'))
        if not LIC.allowed(L[0]): raise ValueError(f"license not allowed: {src.get('license')}")
        page = src.get('page') or ''
        if not page or not src.get('url'): raise ValueError('commons source needs a url and a page')
        html, final = text(page)
        found = LIC.page_licenses(html)
        uri = (L[2] or '').rstrip('/')
        page_l = html.lower()
        if L[0] not in found and (not uri or uri not in html) and not (L[0] == 'PD' and 'public domain' in page_l):
            raise ValueError(f"declared license {L[0]} not found on the live page {page} ({found})")
        rb = src.get('range_bytes'); p = download(src['url'], rb)
        what = 'open-commons file as published' + (f'; only the first {rb:,} bytes were fetched (HTTP Range) and hashed' if rb else '')
        return dict(path=p, audio_url=src['url'], what=what, source=src.get('source') or 'open commons', source_url=final or page,
                    title=src.get('title') or os.path.basename(src['url']), author=src.get('author') or 'unknown',
                    supplier=dict(display_name=src.get('author') or 'unknown', primary_link=page),
                    license=dict(id=L[0], name=L[1], uri=L[2], verified_live=f'{now}, {final or page}'),
                    sha256=sha256_file(p), size_bytes=os.path.getsize(p), partial=bool(rb))
    if k == 'url':
        L = LIC.from_id(src['license'])
        if not LIC.allowed(L[0]): raise ValueError(f"license not allowed: {src['license']}")
        ver = None
        if src.get('page'):
            found = LIC.page_licenses(text(src['page'])[0])
            if L[0] in found: ver = f"{now}, {src['page']}"
            elif L[0] != 'PD': raise ValueError(f"declared license {L[0]} not found on the live page {src['page']} ({found})")
        p = download(src['url'], src.get('range_bytes'))
        return dict(path=p, audio_url=src['url'], what='file as published' + (f"; first {src['range_bytes']:,} bytes" if src.get('range_bytes') else ''), source=src.get('source', 'open commons'),
                    source_url=src.get('page', src['url']), title=src.get('title', os.path.basename(src['url'])), author=src.get('author', 'unknown'),
                    supplier=dict(display_name=src.get('author', 'unknown'), primary_link=src.get('page', src['url'])),
                    license=dict(id=L[0], name=L[1], uri=L[2], verified_live=ver or 'declared in the plan; not verified on a live page'), sha256=sha256_file(p), size_bytes=os.path.getsize(p), partial=bool(src.get('range_bytes')))
    raise ValueError(f'unknown source kind {k}')
