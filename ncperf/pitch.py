"""Pitch measurement. NetComposer picks pitched sounds by measuring them, never by their labels."""
import numpy as np, warnings
import librosa
from .util import SR

warnings.filterwarnings('ignore')
NOTE = lambda m: librosa.midi_to_note(int(m), unicode=False)

def segment_run(m, sr, drop_db=40.0):
    """Split a multi-note run (e.g. a UIowa MIS chromatic file) into notes by silence. Same rule as perf02/perf03."""
    hop = 512
    rms = np.sqrt(np.convolve(m ** 2, np.ones(hop * 4) / (hop * 4), 'same')[::hop]); db = 20 * np.log10(rms + 1e-9)
    act = db > db.max() - drop_db; segs = []; i = 0; n = len(act); g = int(0.35 * sr / hop)
    while i < n:
        if act[i]:
            j = i
            while j < n and np.any(act[j:j + g]): j += 1
            if (j - i) * hop / sr > 0.4: segs.append((i * hop / sr, j * hop / sr))
            i = j
        else: i += 1
    return segs

def fft_peak(a, sr, midi):
    """Refined FFT peak near the expected fundamental, in cents from midi."""
    n = 1 << 18; S = np.abs(np.fft.rfft(a * np.hanning(len(a)), n)); fr = np.fft.rfftfreq(n, 1 / sr)
    f0 = librosa.midi_to_hz(midi); w = (fr > f0 * 2 ** (-0.6 / 12)) & (fr < f0 * 2 ** (0.6 / 12))
    if not w.any(): return None
    pk = fr[np.argmax(S * w)]; return float((librosa.hz_to_midi(pk) - midi) * 100)

def measure(y, expect_midi=None, sr=SR, windows=((0.4, 2.4), (0.15, 1.5), (0.05, 0.8))):
    """Measure the sounding pitch of a mono or stereo array (from its onset). Returns dict(midi, note, cents, spread_cents, fft_cents, voiced, method)."""
    m = y.mean(1) if y.ndim == 2 else y
    a0 = np.abs(m); o = int(np.argmax(a0 > 0.05 * a0.max())) if a0.max() > 0 else 0
    lo, hi = 50.0, 2100.0
    if expect_midi is not None:
        lo = float(librosa.midi_to_hz(expect_midi - 7)); hi = float(librosa.midi_to_hz(expect_midi + 7))
    g = np.array([]); used = None; v = np.array([0.0])
    for w0, w1 in windows:
        a = m[o + int(w0 * sr): o + int(w1 * sr)]
        if len(a) < 4096: continue
        f0, v, _ = librosa.pyin(a.astype(np.float32), fmin=lo, fmax=hi, sr=sr, frame_length=4096, hop_length=1024, resolution=0.02)
        g = f0[v]; used = (w0, w1)
        if len(g) >= 5: break
    if len(g) < 5: return dict(midi=None, note=None, cents=None, voiced=0.0, method='pYIN: no stable voiced frames')
    mm = float(librosa.hz_to_midi(np.median(g))); mr = int(round(mm))
    a = m[o + int(used[0] * sr): o + int(used[1] * sr)]
    return dict(midi=mr, note=NOTE(mr), cents=round((mm - mr) * 100, 1), spread_cents=round(float(np.std(librosa.hz_to_midi(g))) * 100, 1),
                fft_cents=None if (fc := fft_peak(a, sr, mr)) is None else round(fc, 1), voiced=round(float(np.mean(v)), 2),
                method=f'NC pYIN 0.02-semitone resolution, window {used[0]}-{used[1]} s after onset, cross-checked with an FFT peak')

def accept(meas, expect_midi, tol=15.0):
    """Accept when pYIN says the expected note within tol cents, or pYIN is unstable but the FFT peak is within tol."""
    if meas.get('midi') != expect_midi: return False
    if abs(meas['cents']) <= tol and meas.get('spread_cents', 999) <= 60: return True
    return meas.get('fft_cents') is not None and abs(meas['fft_cents']) <= tol and abs(meas['cents']) <= 2 * tol

def measure_fft(y, expect_midi, sr=SR, lo=50.0, hi=2000.0):
    """For inharmonic sounds (bells): the strongest spectral peak in lo-hi Hz, as midi + cents."""
    m = y.mean(1) if y.ndim == 2 else y; o = int(np.argmax(np.abs(m) > 0.05 * np.abs(m).max())); a = m[o:o + int(3 * sr)]
    n = 1 << 19; S = np.abs(np.fft.rfft(a * np.hanning(len(a)), n)); fr = np.fft.rfftfreq(n, 1 / sr); w = (fr > lo) & (fr < hi)
    pk = fr[w][np.argmax(S[w])]; mm = float(librosa.hz_to_midi(pk)); mr = int(round(mm))
    return dict(midi=mr, note=NOTE(mr), cents=round((mm - mr) * 100, 1), spread_cents=0.0, fft_cents=round((mm - mr) * 100, 1), voiced=None,
                method=f'NC FFT: strongest peak {lo:.0f}-{hi:.0f} Hz in the first 3 s ({pk:.1f} Hz)')
