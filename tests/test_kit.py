"""Offline tests: schema, chord parsing, stub, the LLM path against a mock server, verify-record on the published records,
the reverb engines (Dragonfly checks are skipped if it is not installed), the send rule, the true-peak limiter, the tone shelf, the time-stretch refusal and resampling.
Run: python3 tests/test_kit.py"""
import json, os, subprocess, sys, time, urllib.request, glob
K = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, K)
from ncperf import interpret as I, chords as C
from ncperf.schema import validate
ok = True
def t(c, m):
    global ok; ok &= bool(c); print(('PASS ' if c else 'FAIL ') + m)
for f in glob.glob(os.path.join(K, 'examples', '*.plan.json')) + glob.glob(os.path.join(K, 'examples', '*', '*.plan.json')) + [os.path.join(K, 'ncperf/data/example.plan.json')]: t(validate(json.load(open(f))) == [], f'schema: {os.path.relpath(f, K)} validates')
bad = json.load(open(os.path.join(K, 'examples/ambient-cm/perf03.plan.json'))); bad['banks']['piano']['notes']['60'] = ['nope']; t(validate(bad), 'schema: unknown source id is caught')
t(C.find('Cm - Cm9 | C#m7b5, Gbmaj7, Bø, Eb6/9, D/F#') == ['Cm', 'Cm9', 'Dbm7b5', 'F#maj7', 'Bm7b5', 'Eb6-9', 'D'], 'chords: symbols found and spelled for the full chord set')
t(len(C.CAT['chords']) == 420 and sum(map(len, C.CAT['chords'].values())) == 2256, 'chords: catalog has 420 symbols / 2256 voicings')
comp = I.read_composition(os.path.join(K, 'examples/ambient-cm/composition.md'))
t(comp['composition'].startswith('2 mins') and 'triplet' in comp['performance'], 'read_composition: verbatim words split into Composition / Performance')
bw = I.read_composition(os.path.join(K, 'examples/bwv846/composition.md'))
if bw: b = I.stub(bw); t(len(b['chords']) == 35 and abs(b['length_s'] - 121.739) < 0.01 and min(b['chords'][2]['voicing']) % 12 == 11, 'read_composition + stub: BWV 846 bar table -> 35 chords at 69 BPM, slash bass kept (G7/B)')
st = I.stub(comp, seed=3); t(validate(st) == [] and st['interpreter']['kind'] == 'stub', f"stub: {len(st['chords'])} chords, valid")
plan_file = os.path.join(K, 'examples/ambient-cm/perf03.plan.json')
srv = subprocess.Popen([sys.executable, os.path.join(K, 'tests/mock_llm.py'), plan_file, '8799']); time.sleep(0.8)
try:
    p = I.llm(comp, seed=5, base_url='http://127.0.0.1:8799/v1', model='any-model', api_key='test-key', log=lambda *a: print('     ', *a))
    calls = json.loads(urllib.request.urlopen('http://127.0.0.1:8799/').read())
    t(validate(p) == [], 'llm: returned plan validates (after one repair round)')
    t(p['interpreter']['kind'] == 'llm' and p['interpreter']['model'] == 'any-model-mock', f"llm: record will name the model: {p['interpreter']['model']}")
    t(len(calls) == 2 and calls[0]['path'] == '/v1/chat/completions' and calls[0]['auth'] == 'Bearer test-key', f"llm: OpenAI-compatible calls {[(c['path'], c['n_messages'], c['prompt_chars']) for c in calls]}")
finally: srv.terminate()
for f in sorted(glob.glob(os.path.join(K, 'examples', '*', 'records', '*.json'))):
    r = subprocess.run([sys.executable, os.path.join(K, 'nc'), 'verify-record', f, '--no-files'], capture_output=True, text=True); t(r.returncode == 0, f'verify-record: {os.path.basename(f)}')
# ---------- kit 0.2: reverb engines, mastering, quality hacks ----------
import numpy as np, soundfile as sf, tempfile, copy
from ncperf import dsp, reverb as RV, util as U, record as RC
from ncperf.util import SR
rng = np.random.default_rng(0)
x = np.zeros((12 * SR, 2)); 
for k in range(6): i = k * 2 * SR; n = int(0.3 * SR); x[i:i + n] = rng.standard_normal((n, 2)) * np.exp(-np.arange(n) / (0.05 * SR))[:, None] * 0.3
imp = np.zeros((SR, 2)); imp[0] = 1.0; f = np.fft.rfftfreq(SR, 1 / SR); H = lambda y: 20 * np.log10(np.abs(np.fft.rfft(y[:, 0])) + 1e-12)
h = H(dsp.high_shelf(imp, 9000, -4.4)); t(abs(h[np.argmin(abs(f - 18000))] + 4.4) < 0.5 and abs(h[np.argmin(abs(f - 200))]) < 0.1, 'tone: high shelf -4.4 dB at 9 kHz (replaces the lowpass blend)')
y = np.sin(2 * np.pi * 997 * np.arange(2 * SR) / SR)[:, None] * np.ones((1, 2)) * 1.3; z, gr = dsp.tp_limit(y, -1.5)
t(dsp.true_peak_db(z) <= -1.45 and gr < 0, f'limiter: +2.3 dBTP in, {dsp.true_peak_db(z):.2f} dBTP out (ceiling -1.5), max GR {gr:.2f} dB')
q = y * 0.5; t(dsp.tp_limit(q, -1.5)[0] is q, 'limiter: no-op when already under the ceiling')
w, inf, _ = RV.process(x, dict(engine='synthetic', send_lu=4.0, rt60_s=3.0), 12.0, log=lambda *a: None)
t(inf['engine'] == 'synthetic' and abs(inf['send_rule']['wet_lufs_after'] - inf['send_rule']['dry_lufs'] - 4.0) < 0.05, f"send rule: wet = dry + {inf['send_rule']['wet_lufs_after'] - inf['send_rule']['dry_lufs']:.2f} LU (asked 4.0)")
t(abs((inf['measured_wet']['T30_s']['1000'] or 0) - 3.0) < 0.4, f"synthetic: measured T30 at 1 kHz {inf['measured_wet']['T30_s']['1000']} s (rt60_s 3.0)")
vst, why = RV.find_dragonfly()
if vst:
    a = RV.process(x, dict(engine='dragonfly'), 12.0, log=lambda *a: None)[1]; b = RV.process(x, dict(engine='dragonfly'), 12.0, log=lambda *a: None)[1]
    t(a['engine'] == 'dragonfly' and a['wet_sha256'] == b['wet_sha256'], f"dragonfly: {a['implementation']} via {a['host']}, deterministic (wet sha256 {a['wet_sha256'][:12]}... twice)")
    t(5.6 <= (a['measured_wet']['T30_s']['500'] or 0) <= 7.0 and a['params']['decay_s'] == 6.2, f"dragonfly: kit defaults, measured T30 500 Hz {a['measured_wet']['T30_s']['500']} s, 4 kHz {a['measured_wet']['T30_s']['4000']} s")
else: print(f'SKIP dragonfly tests ({why})')
_fd = RV.find_dragonfly; RV.find_dragonfly = lambda p=None: (None, 'test: not installed')
try:
    w2, fb, _ = RV.process(x, dict(engine='dragonfly'), 12.0, log=lambda *a: None)
    t(fb['engine_requested'] == 'dragonfly' and fb['engine'] == 'synthetic' and 'not installed' in fb['fallback_reason'], 'fallback: no Dragonfly -> synthetic, with a warning, and the record says which engine ran')
finally: RV.find_dragonfly = _fd
pl = json.load(open(os.path.join(K, 'examples/ambient-cm/perf03.plan.json')))
g = copy.deepcopy(pl); g['master']['reverb'] = dict(engine='dragonfly', send_lu=4.5, params=dict(decay_s=5.0, size_m=40)); g['quality'] = dict(stretch_engine='auto', resampler='soxr'); g['master']['loudness'] = dict(lufs=-17, tp=-1.5, method='linear_tp_limiter')
t(validate(g) == [], 'schema: dragonfly reverb with params, quality and loudness.method validate')
g['master']['reverb'] = dict(engine='convolution', ir='openair-stpauls-choir', send_lu=3.0); t(validate(g) == [], 'schema: convolution reverb by IR id validates')
g['master']['reverb'] = dict(engine='plate-o-matic'); t(validate(g) != [], 'schema: unknown reverb engine is caught')
REG = RV.ir_registry()
t(len(REG) >= 1 and all(e['license']['id'].startswith('CC-BY') and e.get('attribution') and e.get('page') and e.get('verified') and all(len(p['sha256']) == 64 for p in e['positions']) for e in REG.values()),
  f'impulse responses: {len(REG)} listed, all CC BY with attribution, verified page and pinned sha256 (no audio shipped)')
tone = np.sin(2 * np.pi * 440 * np.arange(SR // 2) / SR)[:, None] * np.ones((1, 2)) * 0.3
try:
    U.stretch(tone, 2.0); stretched = False
except RuntimeError as exc:
    stretched = "time-stretch is not allowed" in str(exc)
t(stretched, "time-stretch is refused")
render_src = open(os.path.join(K, "ncperf/render.py"), encoding="utf-8").read()
t("stretch(" not in render_src and "loop_unaltered(" in render_src, "renderer does not call a stretch or pitch op")
looped = U.loop_unaltered(tone, 1.0)
t(abs(len(looped) / SR - 1.0) < 0.01, "a pad is made by repeating the unaltered clip")
try:
    from ncperf.render import Renderer
    Renderer({"seed": 1, "length_s": 1, "layers": [{"events": [{"stretch_to_s": 2.0}]}]}, "/tmp")
    refused = False
except RuntimeError as exc:
    refused = "time-stretch is not allowed" in str(exc)
t(refused, "a plan that asks for stretch_to_s is refused")
st = I.stub(comp, seed=3)
t("stretch_to_s" not in json.dumps(st), "stub plan does not time-stretch")
with tempfile.TemporaryDirectory() as d:
    sf.write(f'{d}/a.wav', tone[:22050] if False else np.zeros((44100, 2)), 44100); U.USED['resampler'].clear(); dd = U.decode(f'{d}/a.wav')
    t(abs(len(dd) - SR) <= 2 and U.USED['resampler'] == ({'soxr precision 28'} if U.has_soxr() else {'ffmpeg swr (default)'}), f"decode: 44.1 kHz -> 48 kHz with {sorted(U.USED['resampler'])[0]}")
hc = RC.hacks_chain(pl, dict(reverb=dict(engine='dragonfly', engine_requested='dragonfly', implementation='Dragonfly Hall Reverb 3.2.10'), master=dict(method='linear+limiter', mode_actual='linear'), tone=dict(tool='high shelf (RBJ biquad)', hz=9000, db=-4.4)))
t([h['engine'] for h in hc if h['tool'] == 'reverb'] == ['dragonfly'] and [h['mode_actual'] for h in hc if h['tool'] == 'loudness'] == ['linear'], 'record: hacks chain carries the reverb engine and the loudness mode that ran')
print('ALL PASS' if ok else 'SOME FAILED'); sys.exit(0 if ok else 1)
