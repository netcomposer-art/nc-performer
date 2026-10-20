"""Audio helpers. Every transform here is a hack in NetComposer's sense: trim, gain, fade, filter.
Decoding resamples to 48 kHz (sample-rate conversion only). Nothing here changes pitch or duration."""
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
    """Time-stretch is not allowed. Duration and pitch of a recording stay as published."""
    raise RuntimeError("time-stretch is not allowed")


def loop_unaltered(y, seconds, crossfade_s=0.12):
    """Repeat a clip unchanged until it covers `seconds`, with an equal-power crossfade at each join.

    Each copy keeps the recording's duration and pitch. This is how a pad is held out.
    """
    y = np.atleast_2d(np.asarray(y))
    if y.shape[1] != 2 and y.shape[0] == 2:
        y = y.T
    need = int(round(max(0.0, seconds) * SR))
    if need <= 0 or len(y) == 0:
        return np.zeros((need, 2))
    if len(y) >= need:
        return np.array(y[:need], copy=True)
    fade_n = min(int(crossfade_s * SR), max(0, len(y) // 4))
    out = np.zeros((need, y.shape[1]))
    pos = 0
    while pos < need:
        n = min(len(y), need - pos)
        chunk = np.array(y[:n], copy=True)
        if pos > 0 and fade_n:
            f = min(fade_n, n)
            w = np.sin(np.linspace(0, np.pi / 2, f)) ** 2
            out[pos:pos + f] *= (1.0 - w)[:, None]
            chunk[:f] *= w[:, None]
        out[pos:pos + n] += chunk
        if n < len(y):
            break
        step = n - fade_n if fade_n and n > fade_n else n
        pos += max(1, step)
    return out

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
