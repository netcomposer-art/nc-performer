"""Audio helpers. Every transform here is a hack in NetComposer's sense: trim, gain, fade, filter, time-stretch.
Decoding resamples to 48 kHz (sample-rate conversion only). Nothing here changes pitch."""
import hashlib, json, os, subprocess, tempfile
import numpy as np, soundfile as sf
from scipy.signal import butter, sosfilt

SR = 48000

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''): h.update(b)
    return h.hexdigest()

def canon(o):
    return json.dumps(o, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=float)

def lp(x, f, o=2): return sosfilt(butter(o, f, 'low', fs=SR, output='sos'), x, axis=0)
def hp(x, f, o=2): return sosfilt(butter(o, f, 'high', fs=SR, output='sos'), x, axis=0)

# Quality options, set per render from plan['quality'] (see Renderer). USED records what actually ran.
Q = dict(resampler='soxr', stretch_engine='auto'); USED = dict(resampler=set(), stretch=set())
_caps = {}
def has_soxr():
    if 'soxr' not in _caps:
        r = subprocess.run(['ffmpeg', '-hide_banner', '-h', 'filter=aresample'], capture_output=True, text=True); _caps['soxr'] = 'soxr' in r.stdout
    return _caps['soxr']
def rubberband_cli():
    """Path and version of the rubberband command-line tool (R3 'finer' engine needs 3.0+), or None."""
    if 'rb' not in _caps:
        import shutil; p = shutil.which('rubberband'); v = None
        if p:
            r = subprocess.run([p, '--version'], capture_output=True, text=True); v = (r.stdout + r.stderr).strip().splitlines()[0] if (r.stdout + r.stderr).strip() else None
        _caps['rb'] = (p, v) if p and v and int(v.split('.')[0]) >= 3 else None
    return _caps['rb']

_dec = {}
def decode(path, t0=0.0, dur=None):
    """Decode to 48 kHz stereo float64 with ffmpeg (resampling only, pitch unchanged). Resampler: soxr (precision 28) when ffmpeg has it."""
    rs = 'soxr' if Q['resampler'] == 'soxr' and has_soxr() else 'swr'
    k = (path, round(t0, 4), None if dur is None else round(dur, 4), rs)
    if k in _dec: return _dec[k]
    af = ['-af', f'aresample={SR}:resampler=soxr:precision=28'] if rs == 'soxr' else []
    cmd = ['ffmpeg', '-v', 'quiet', '-ss', f'{t0:.4f}', '-i', path] + (['-t', f'{dur:.4f}'] if dur else []) + ['-ac', '2'] + af + ['-ar', str(SR), '-f', 'f32le', '-']
    USED['resampler'].add('soxr precision 28' if rs == 'soxr' else 'ffmpeg swr (default)')
    y = np.frombuffer(subprocess.run(cmd, capture_output=True, check=True).stdout, dtype=np.float32).reshape(-1, 2).astype(np.float64)
    _dec[k] = y
    return y

_st = {}
def stretch_engine():
    e = Q['stretch_engine']; rb = rubberband_cli()
    if e in ('auto', 'rubberband-r3') and rb: return 'rubberband-r3'
    if e == 'rubberband-r3' and not rb: print('WARNING: rubberband CLI 3.x not found; time-stretch falls back to the ffmpeg rubberband filter (R2).', file=__import__('sys').stderr)
    return 'ffmpeg-rubberband'

def stretch(y, ratio, tag=None):
    """Time-stretch hack, duration changes, pitch does not. Engine: rubberband CLI R3 ('finer', --fine) when installed, else ffmpeg's
    rubberband filter (R2: transients=smooth, window=long, phase=independent, pitch=1)."""
    eng = stretch_engine(); k = (tag, round(ratio, 5), eng)
    if tag and k in _st: return _st[k]
    with tempfile.TemporaryDirectory() as d:
        sf.write(f'{d}/i.wav', y, SR, subtype='FLOAT')
        if eng == 'rubberband-r3':
            subprocess.run([rubberband_cli()[0], '-q', '--fine', '--time', f'{ratio:.6f}', f'{d}/i.wav', f'{d}/o.wav'], check=True, capture_output=True)
            USED['stretch'].add(f'rubberband CLI {rubberband_cli()[1]} R3 (--fine), pitch unchanged')
        else:
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', f'{d}/i.wav', '-af',
                            f'rubberband=tempo={1/ratio:.6f}:pitch=1:transients=smooth:window=long:phase=independent',
                            '-c:a', 'pcm_f32le', f'{d}/o.wav'], check=True)
            USED['stretch'].add('ffmpeg rubberband filter (librubberband R2): transients=smooth, window=long, phase=independent, pitch=1')
        z = sf.read(f'{d}/o.wav', always_2d=True)[0]
    if tag: _st[k] = z
    return z

def fade(z, fi, fo):
    """Equal-power fade hack."""
    z = z.copy(); a = min(len(z), int(fi * SR)); b = min(len(z), int(fo * SR))
    if a: z[:a] *= (np.sin(np.linspace(0, np.pi / 2, a)) ** 2)[:, None]
    if b: z[-b:] *= (np.cos(np.linspace(0, np.pi / 2, b)) ** 2)[:, None]
    return z

def flatten(z, win=0.3, maxgain_db=20):
    """Decay-flatten gain hack (RMS follower)."""
    n = int(win * SR); m = np.sqrt(np.convolve(np.mean(z ** 2, 1), np.ones(n) / n, 'same')) + 1e-9
    ref = np.percentile(m, 85); return z * np.minimum(ref / m, 10 ** (maxgain_db / 20))[:, None]

def onset(y, thr=0.05):
    a = np.abs(y).max(1); return int(np.argmax(a > thr * a.max())) if a.max() > 0 else 0

def rms_db(z): return 20 * np.log10(np.sqrt(np.mean(z ** 2)) + 1e-12)
