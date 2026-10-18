"""DSP helpers for the master hacks: RBJ shelves, octave-point linear-phase EQ, the true-peak limiter, RT measurement.
Everything is deterministic (no randomness, no threads with order-dependent sums)."""
import numpy as np
from scipy.signal import butter, sosfilt, sosfiltfilt, fftconvolve, firwin2, resample_poly
from scipy.ndimage import minimum_filter1d, uniform_filter1d
from .util import SR

def _shelf_sos(f0, gdb, high=True, S=0.7, sr=SR):
    A = 10 ** (gdb / 40); w = 2 * np.pi * f0 / sr; al = np.sin(w) / 2 * np.sqrt((A + 1 / A) * (1 / S - 1) + 2); c = np.cos(w); q = 2 * np.sqrt(A) * al
    if high:
        b = [A * ((A + 1) + (A - 1) * c + q), -2 * A * ((A - 1) + (A + 1) * c), A * ((A + 1) + (A - 1) * c - q)]
        a = [(A + 1) - (A - 1) * c + q, 2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - q]
    else:
        b = [A * ((A + 1) - (A - 1) * c + q), 2 * A * ((A - 1) - (A + 1) * c), A * ((A + 1) - (A - 1) * c - q)]
        a = [(A + 1) + (A - 1) * c + q, -2 * ((A - 1) + (A + 1) * c), (A + 1) + (A - 1) * c - q]
    return np.array([b + a]) / a[0]

def high_shelf(x, f0, gdb, S=0.7): return x if gdb == 0 else sosfilt(_shelf_sos(f0, gdb, True, S), x, axis=0)
def low_shelf(x, f0, gdb, S=0.7, sr=SR): return x if gdb == 0 else sosfilt(_shelf_sos(f0, gdb, False, S, sr), x, axis=0)

OCT = (63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
def octave_eq(x, octave_db, taps=4097):
    """Linear-phase FIR through octave-point gains (dB, keys = band centre Hz as strings or ints), latency compensated."""
    g = np.array([float(octave_db.get(str(f), octave_db.get(f, 0.0))) for f in OCT])
    if not np.any(g): return x
    f = np.r_[0, OCT, SR / 2]; gg = np.r_[g[0], g, g[-1]]; h = firwin2(taps, f / (SR / 2), 10 ** (gg / 20)); d = taps // 2
    return np.stack([fftconvolve(x[:, c], h)[d:d + len(x)] for c in range(x.shape[1])], 1)

def true_peak_db(y, os_=4):
    return float(20 * np.log10(np.max(np.abs(resample_poly(y, os_, 1, axis=0))) + 1e-12))

def tp_limit(y, ceil_db=-1.5, la_ms=3.0, rel_ms=150.0, os_=4):
    """Lookahead true-peak limiter: detector on the 4x oversampled signal, stereo-linked, gain applied at the base rate.
    Returns (limited, max gain reduction dB). No-op when the signal is already under the ceiling."""
    c = 10 ** (ceil_db / 20); u = np.abs(resample_poly(y, os_, 1, axis=0)).max(1)
    pk = u[:len(y) * os_].reshape(len(y), os_).max(1); g = np.minimum(1.0, c / np.maximum(pk, 1e-12))
    if g.min() >= 1.0: return y, 0.0
    la = int(la_ms / 1000 * SR); gm = minimum_filter1d(g, 2 * la + 1)
    a = np.exp(-1 / (rel_ms / 1000 * SR)); gs = gm.copy(); idx = np.nonzero(gm < 1.0)[0]
    # release smoothing only matters after a gain dip; run the recursion from the first dip on
    i0 = int(idx[0]) if len(idx) else len(gs)
    for i in range(max(1, i0), len(gs)):
        if gs[i] > gs[i - 1]: gs[i] = a * gs[i - 1] + (1 - a) * gs[i]
    gs = np.minimum(uniform_filter1d(gs, la + 1), gm)
    return y * gs[:, None], float(20 * np.log10(gs.min()))

def rt_bands(ir, sr=SR, bands=(125, 250, 500, 1000, 2000, 4000, 8000)):
    """Schroeder backward integration per octave band: T30 (-5..-35 dB fit, T20 if the range is short) and EDT (0..-10 dB)."""
    ir = ir if ir.ndim == 1 else ir[:, 0]; T30, EDT = {}, {}
    for fc in bands:
        y = sosfiltfilt(butter(4, [fc / np.sqrt(2), min(fc * np.sqrt(2), sr / 2 * 0.95)], 'band', fs=sr, output='sos'), ir)
        e = y ** 2; e = e[np.argmax(e):]; nf = np.mean(e[-max(1, len(e) // 10):]); w = int(0.02 * sr)
        sm = np.convolve(e, np.ones(w) / w, 'same'); cut = int(np.argmax(sm < 2 * nf)) if np.any(sm < 2 * nf) else len(e)
        e = np.clip(e[:max(cut, w)] - nf, 1e-30, None); edc = np.cumsum(e[::-1])[::-1]; edc = 10 * np.log10(edc / edc[0]); t = np.arange(len(edc)) / sr
        def fit(a, b):
            m = (edc <= a) & (edc >= b)
            return None if m.sum() < 10 else round(float(-60 / np.polyfit(t[m], edc[m], 1)[0]), 2)
        T30[str(fc)] = fit(-5, -35) or fit(-5, -25); EDT[str(fc)] = fit(0, -10)
    return T30, EDT
