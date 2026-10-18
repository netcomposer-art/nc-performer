"""Reverb hack. master.reverb.engine picks one of:
  dragonfly    (default) Dragonfly Hall Reverb (GPL-3, open source), run headless and offline through pedalboard. Deterministic.
  convolution  a measured space from data/irs.json (CC BY, credited in the record), B-format decoded to a true-stereo 2x2 matrix.
  synthetic    the legacy hall made from seeded filtered noise (kit 0.1).
The wet return is level-set by a loudness send rule: integrated loudness (BS.1770) of the wet = dry + send_lu.
If Dragonfly is asked for but is not installed, the render falls back to synthetic with a warning, and the record says so."""
import hashlib, json, os, sys, urllib.request
import numpy as np, soundfile as sf, pyloudnorm as pyln
from scipy.signal import fftconvolve, resample_poly
from .util import SR, lp, hp
from . import dsp

DRAGONFLY_DEFAULTS = dict(dry_level=0.0, early_level=14.0, late_level=100.0, size_m=48.0, width=120.0, predelay_ms=24.0, diffuse=85.0, low_cut_hz=60.0,
                          low_cross_hz=400.0, low_mult_x=1.15, high_cut_hz=9000.0, high_cross_hz=3800.0, high_mult_x=0.45, spin_hz=1.2, wander_ms=22.0,
                          decay_s=6.2, early_send=25.0, modulation=40.0)
RETURN_EQ = {
    'dragonfly': dict(hp_hz=120, lp_hz=14000, octave_db={'63': -0.2, '125': -2.0, '250': -0.9, '500': 0.0, '1000': 1.8, '2000': 2.3, '4000': 2.6, '8000': -1.0, '16000': -4.1}),
    'convolution': dict(hp_hz=120, lp_hz=14000, octave_db={}),
    'synthetic': dict(hp_hz=None, lp_hz=None, octave_db={}),
}
DEFAULT_SEND_LU = 5.3      # perf02's measured wet-to-dry ratio
LEGACY_WET_REF = 0.38      # legacy 'wet' maps to send_lu = DEFAULT_SEND_LU + 20*log10(wet/0.38)
VST3_NAME = 'DragonflyHallReverb.vst3'
VST3_DIRS = ['/usr/lib/vst3', '/usr/local/lib/vst3', os.path.expanduser('~/.vst3'), '/Library/Audio/Plug-Ins/VST3',
             os.path.expanduser('~/Library/Audio/Plug-Ins/VST3'), r'C:\Program Files\Common Files\VST3']

def find_dragonfly(path=None):
    """(vst3_path, None) or (None, reason). Order: plan path, NC_DRAGONFLY_VST3, standard VST3 folders."""
    try: import pedalboard  # noqa: F401
    except ImportError: return None, 'pedalboard is not installed (pip install pedalboard)'
    for p in [path, os.environ.get('NC_DRAGONFLY_VST3')] + [os.path.join(d, VST3_NAME) for d in VST3_DIRS]:
        if p and os.path.exists(p): return p, None
    return None, f'{VST3_NAME} not found (apt install dragonfly-reverb, or set master.reverb.vst3_path / NC_DRAGONFLY_VST3)'

def _dragonfly(x, params, vst3):
    import pedalboard
    p = pedalboard.load_plugin(vst3)
    for k, v in params.items(): setattr(p, k, float(v))
    p.reset()
    y = p.process(np.ascontiguousarray(x.T, dtype=np.float32), SR, buffer_size=512, reset=True).T.astype(np.float64)
    return y, dict(implementation=f'{p.name} {p.version}', implementation_vendor=p.manufacturer_name, implementation_license='GPL-3.0', implementation_url='https://github.com/michaelwillis/dragonfly-reverb',
                   host=f'pedalboard {pedalboard.__version__}', host_license='GPL-3.0', host_url='https://github.com/spotify/pedalboard', vst3_path=vst3, buffer_size=512)

# ---------- convolution ----------
def ir_registry():
    return {k: v for k, v in json.load(open(os.path.join(os.path.dirname(__file__), 'data', 'irs.json'))).items() if not k.startswith('_')}

def _fetch_pinned(url, sha):
    from .fetch import cache_dir
    p = os.path.join(cache_dir(), 'ir_' + sha[:16] + '.wav')
    if not (os.path.exists(p) and hashlib.sha256(open(p, 'rb').read()).hexdigest() == sha):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        data = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'nc-performer'}), timeout=120).read()
        got = hashlib.sha256(data).hexdigest()
        if got != sha: raise ValueError(f'impulse response {url}: sha256 {got} does not match the pinned {sha}')
        open(p, 'wb').write(data)
    return p

def _vmic(b, deg, p):   # FuMa W,X,Y,Z -> first-order virtual microphone at azimuth deg (+ = left)
    th = np.deg2rad(deg); return p * np.sqrt(2) * b[:, 0] + (1 - p) * (b[:, 1] * np.cos(th) + b[:, 2] * np.sin(th))

def _prep(pair, sr, reshape, direct_ms, pre_ms):
    y = resample_poly(pair, SR, sr, axis=0) if sr != SR else pair.copy()
    a = np.abs(y).max(1); on = int(np.argmax(a > 0.1 * a.max())); y = y[on + int(direct_ms / 1000 * SR):].copy()
    f = int(0.001 * SR); y[:f] *= np.sin(np.linspace(0, np.pi / 2, f))[:, None] ** 2
    e = np.mean(y ** 2, 1); w = int(0.05 * SR); sm = np.convolve(e, np.ones(w) / w, 'same'); nf = np.median(sm[-int(0.5 * SR):])
    cut = min(len(y), len(y) - int(np.argmax(sm[::-1] > 4 * nf))); t = np.arange(len(y)) / SR
    if reshape: y *= np.exp(-6.91 * t * (1 / reshape[1] - 1 / reshape[0]))[:, None]
    y = y[:cut]; fo = int(0.25 * cut); y[-fo:] *= np.cos(np.linspace(0, np.pi / 2, fo))[:, None] ** 2
    return np.concatenate([np.zeros((int(pre_ms / 1000 * SR), 2)), y]), dict(onset_s=round(on / SR, 4), truncated_at_s=round(cut / SR, 3))

def build_ir(rv):
    """4-channel true-stereo matrix [LL, LR, RL, RR] plus the input entries for the record."""
    spec = rv.get('ir'); reg = ir_registry()
    e = dict(reg[spec]) if isinstance(spec, str) else dict(spec or {})
    if isinstance(spec, str) and spec not in reg: raise ValueError(f'unknown impulse response id {spec}; known: {sorted(reg)}')
    from . import licenses as LIC
    if not LIC.allowed(e['license']['id']): raise ValueError(f"impulse response license {e['license']['id']} is not allowed")
    d = dict(e.get('defaults', {})); d.update({k: rv[k] for k in ('decode', 'decay_reshape_s', 'direct_remove_ms', 'predelay_ms') if k in rv})
    dec = dict(pattern=0.27, az_deg=60, lf_dipole_shelf_db=6.0, lf_dipole_shelf_hz=500); dec.update(d.get('decode') or {})
    pos = e['positions'] if len(e['positions']) > 1 else e['positions'] * 2; mats = []; infos = []
    for ps in pos:
        b, sr = sf.read(_fetch_pinned(ps['url'], ps['sha256']), always_2d=True)
        if e.get('format', 'bformat_fuma') == 'bformat_fuma':
            if dec['lf_dipole_shelf_db']: b = b.copy(); b[:, 1:] = dsp.low_shelf(b[:, 1:], dec['lf_dipole_shelf_hz'], dec['lf_dipole_shelf_db'], sr=sr)
            pair = np.stack([_vmic(b, dec['az_deg'], dec['pattern']), _vmic(b, -dec['az_deg'], dec['pattern'])], 1)
        else: pair = b[:, :2]
        m, info = _prep(pair, sr, d.get('decay_reshape_s'), d.get('direct_remove_ms', 3.0), d.get('predelay_ms', 20)); mats.append(m); infos.append(dict(label=ps['label'], **info))
    n = min(len(m) for m in mats); M = np.concatenate([m[:n] for m in mats], 1); M /= np.sqrt(np.sum(M ** 2) / 4)
    inputs = [dict(key=f"ir:{spec if isinstance(spec, str) else e.get('id', 'custom')}:{i}", url=ps['url'], sha256=ps['sha256'], label=ps['label']) for i, ps in enumerate(e['positions'])]
    return M, dict(ir_id=spec if isinstance(spec, str) else e.get('id', 'custom'), ir_title=e.get('title'), decode=dec, decay_reshape_s=d.get('decay_reshape_s'),
                   direct_remove_ms=d.get('direct_remove_ms', 3.0), predelay_ms=d.get('predelay_ms', 20), matrix='true stereo 2x2 (L in = position 1, R in = position 2)',
                   positions=infos, ir_len_s=round(n / SR, 3)), dict(entry=e, inputs=inputs)

def _conv(x, M):
    L_ = fftconvolve(x[:, 0], M[:, 0]) + fftconvolve(x[:, 1], M[:, 2]); R_ = fftconvolve(x[:, 0], M[:, 1]) + fftconvolve(x[:, 1], M[:, 3])
    return np.stack([L_, R_], 1)[:len(x)]

# ---------- synthetic (legacy) ----------
def _synthetic_ir(rv):
    rt = rv.get('rt60_s', 6.0); n = int((rt + 0.5) * SR); tt = np.arange(n) / SR
    ir = np.random.randn(n, 2) * np.exp(-6.91 * tt / rt)[:, None]; ir = lp(ir, rv.get('lp_hz', 4000)); ir = hp(ir, rv.get('hp_hz', 170), 1)
    ir[:int(0.15 * SR)] *= np.linspace(0, 1, int(0.15 * SR))[:, None]; ir = np.concatenate([np.zeros((int(rv.get('predelay_ms', 45) / 1000 * SR), 2)), ir])
    return ir / np.sqrt(np.sum(ir ** 2))

# ---------- the hack ----------
def _return_eq(w, eq):
    if eq.get('hp_hz'): w = hp(w, eq['hp_hz'], 2)
    if eq.get('lp_hz'): w = lp(w, eq['lp_hz'], 2)
    return dsp.octave_eq(w, eq.get('octave_db') or {})

def process(x, rv, end_s, log=print):
    """x: dry stereo mix (sum of buses). Returns (wet_scaled, info, ir_inputs). info goes into the record's reverb hack entry."""
    req = rv.get('engine', 'dragonfly'); eng = req; info = dict(engine_requested=req); ir_inputs = None
    if req not in ('dragonfly', 'convolution', 'synthetic'): raise ValueError(f'unknown reverb engine {req}')
    vst3 = None
    if req == 'dragonfly':
        vst3, why = find_dragonfly(rv.get('vst3_path'))
        if not vst3:
            eng = 'synthetic'; info['fallback_reason'] = why
            msg = f'WARNING: Dragonfly reverb is not available ({why}). Falling back to the synthetic hall; the record will say so.'
            log(msg); print(msg, file=sys.stderr)
    info['engine'] = eng
    eq = dict(RETURN_EQ[eng]); eq.update(rv.get('return_eq') or {}); info['return_eq'] = eq
    imp = np.zeros((14 * SR, 2)); imp[0, 0] = 1.0
    if eng == 'dragonfly':
        params = dict(DRAGONFLY_DEFAULTS)
        if 'rt60_s' in rv and 'decay_s' not in (rv.get('params') or {}): params['decay_s'] = float(rv['rt60_s'])
        params.update(rv.get('params') or {}); info['params'] = params
        w, tinfo = _dragonfly(x, params, vst3); info.update(tinfo); wi, _ = _dragonfly(imp, params, vst3)
    elif eng == 'convolution':
        M, cinfo, ir_inputs = build_ir(rv); info.update(cinfo); info['implementation'] = 'scipy.signal.fftconvolve (true stereo 2x2)'
        w = _conv(x, M); wi = _conv(imp, M)
    else:
        ir = _synthetic_ir(rv); info['params'] = {k: rv.get(k, d) for k, d in dict(rt60_s=6.0, predelay_ms=45, lp_hz=4000, hp_hz=170).items()}
        info['implementation'] = 'synthetic hall: seeded filtered noise IR, fftconvolve'
        w = np.stack([fftconvolve(x[:, c], ir[:, c])[:len(x)] for c in range(2)], 1); wi = np.stack([ir[:, 0], ir[:, 1]], 1)[:14 * SR]
    w = _return_eq(w, eq); wi = _return_eq(wi, eq)
    # send rule
    if 'send_lu' in rv: send = float(rv['send_lu']); how = 'plan send_lu'
    elif 'wet' in rv: send = DEFAULT_SEND_LU + 20 * np.log10(max(float(rv['wet']), 1e-3) / LEGACY_WET_REF); how = f"legacy wet {rv['wet']} -> {DEFAULT_SEND_LU} + 20*log10(wet/{LEGACY_WET_REF})"
    else: send = DEFAULT_SEND_LU; how = 'default'
    m = pyln.Meter(SR); n = int(end_s * SR); ld = m.integrated_loudness(x[:n]); lw = m.integrated_loudness(w[:n])
    ws = w * 10 ** ((ld + send - lw) / 20)
    info['send_rule'] = dict(rule='integrated loudness (BS.1770) of the wet return = dry + send_lu', send_lu=round(send, 2), source=how,
                             dry_lufs=round(float(ld), 2), wet_lufs_after=round(float(m.integrated_loudness(ws[:n])), 2))
    T30, EDT = dsp.rt_bands(wi[:, 0] if wi.ndim > 1 else wi)
    info['measured_wet'] = dict(method='impulse into the left input through engine + return EQ; Schroeder integration per octave band', T30_s=T30, EDT_s=EDT)
    info['wet_sha256'] = hashlib.sha256(np.ascontiguousarray(ws, dtype='<f4').tobytes()).hexdigest()
    info['impulse_sha256'] = hashlib.sha256(np.ascontiguousarray(wi, dtype='<f4').tobytes()).hexdigest()
    info['deterministic'] = True
    return ws, info, ir_inputs
