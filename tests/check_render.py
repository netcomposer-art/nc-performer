"""Check a render: loudness, true peak, clipping, and the chords.
Chords: chroma (CQT) of the harmony stems over each chord's window, compared with the plan's voicing.
Usage: python3 tests/check_render.py OUT_DIR [--stems pad,keys,mm]"""
import json, os, sys, glob, subprocess
import numpy as np, soundfile as sf, librosa
K = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, K)
from ncperf.record import loud
NAMES = 'C Db D Eb E F F# G Ab A Bb B'.split()
d = sys.argv[1]; stems = (sys.argv[sys.argv.index('--stems') + 1] if '--stems' in sys.argv else 'pad,keys,mm').split(',')
plan = json.load(open(os.path.join(d, 'plan.json'))); ok = True
fl = [f for f in glob.glob(os.path.join(d, '*.flac'))][0]; mp3 = glob.glob(os.path.join(d, '*_preview.mp3'))[0]
for f in (fl, mp3):
    I, TP, LRA = loud(f); y, sr = sf.read(f); clip = int(np.sum(np.abs(y) >= 0.99997))
    good = -18 <= I <= -16 and TP <= -1.0 and clip == 0; ok &= good
    print(f"{'PASS' if good else 'FAIL'} {os.path.basename(f)}: {I} LUFS, {TP} dBTP, LRA {LRA}, {len(y)/sr:.1f} s, clipped samples {clip}")
x = None
for s in stems:
    p = os.path.join(d, 'stems', s + '.wav')
    if os.path.exists(p):
        z, sr = sf.read(p); z = z.mean(1); x = z if x is None else x[:len(z)] + z[:len(x)]
y = librosa.resample(x, orig_sr=sr, target_sr=22050); sr = 22050
ch = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=2048, fmin=librosa.note_to_hz('C2'), n_octaves=5)
hit = 0
for i, c in enumerate(plan['chords']):
    a, b = int((c['t'] + 1.5) * sr / 2048), int((c['t'] + c['dur'] - 0.5) * sr / 2048)
    v = ch[:, a:b].mean(1); pcs = sorted({m % 12 for m in c['voicing']}); share = v[pcs].sum() / v.sum()
    top = list(np.argsort(v)[::-1][:len(pcs)]); good = set(top) == set(pcs) or (share >= 0.75 and set(top[:2]) <= set(pcs)); hit += good  # the N strongest pitch classes are exactly the chord, or most energy is in the chord and the two strongest are chord tones
    print(f"{'PASS' if good else 'FAIL'} chord {i:2d} {c['t']:6.1f}s {c['symbol']:5s} voicing {'/'.join(NAMES[p] for p in pcs):14s} heard top {'/'.join(NAMES[p] for p in top):14s} in-chord energy {share:.0%}")
ok &= hit == len(plan['chords'])
print(f"chords: {hit}/{len(plan['chords'])} match"); print('ALL PASS' if ok else 'SOME FAILED'); sys.exit(0 if ok else 1)
