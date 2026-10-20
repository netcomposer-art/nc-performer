"""RENDER: deterministic. plan.json in; FLAC + MP3 + uses log out. Same plan + same sources + same seed = same audio.
Fetch -> license check -> measure pitch -> select by measurement -> place -> hacks -> mix -> master. No pitch-shifting anywhere."""
import json, os, random, subprocess, threading
import numpy as np, soundfile as sf, pyloudnorm as pyln
from scipy.signal import fftconvolve
from . import fetch, pitch
from .parallel import run_all
from .util import SR, decode, fade, flatten, loop_unaltered, lp, hp, onset
from . import util, dsp, reverb

class Renderer:
    def __init__(self, plan, out_dir, log=print):
        self.P = plan; self.out = out_dir; self.log = log
        self.rng = random.Random(plan['seed']); np.random.seed(plan['seed'] % (2 ** 32))
        self.END = float(plan['length_s']); self.TAIL = float(plan.get('tail_s', 4.0))
        self.L = int((self.END + self.TAIL + 6) * SR)
        self.bus = {}; self.uses = []; self.res = {}; self.meas = {}; self.rejected = []; self.sel = {}
        q = plan.get('quality', {}); util.Q.update(resampler=q.get('resampler', 'soxr'), stretch_engine=q.get('stretch_engine', 'auto'))
        util.USED['resampler'].clear(); util.USED['stretch'].clear(); self.reverb_info = None; self.ir_inputs = None; self.master_info = None
        self._src_lock = threading.Lock()
        for ly in plan.get('layers') or []:
            if ly.get('stretch_to_s'):
                raise RuntimeError('time-stretch is not allowed')
            for ev in ly.get('events') or []:
                if ev.get('stretch_to_s'):
                    raise RuntimeError('time-stretch is not allowed')

    # ---------- sources ----------
    def source(self, sid):
        """Resolve (fetch + license check) once; for multi-note runs, cut the requested note by silence segmentation."""
        with self._src_lock:
            if sid in self.res: return self.res[sid]
        s = self.P['sources'][sid]; r = fetch.resolve(s); r['id'] = sid
        y = decode(r['path']); r['offset_s'] = 0.0
        if 'segment' in s:
            segs = pitch.segment_run(y.mean(1), SR, s['segment'].get('drop_db', 40.0)); i = s['segment']['index']
            t0, t1 = segs[i]; r['segment'] = dict(index=i, t0=round(t0, 3), t1=round(t1, 3), of=len(segs)); r['offset_s'] = t0
            y = y[int(t0 * SR):int(t1 * SR)]
        r['y'] = y
        with self._src_lock:
            if sid in self.res: return self.res[sid]
            self.res[sid] = r
        self.log(f"  source {sid}: {r['license']['name']} \u00b7 {r['author']} \u00b7 {r['title'][:60]}")
        return r

    def _warm(self):
        """Decode the kept files together. Gather already checked their pitch."""
        sids = [sid for sid in (self.P.get('sources') or {})]

        def one(sid):
            try:
                self.source(sid)
            except Exception as exc:
                self.log(f"  source {sid}: {exc}")

        if len(sids) <= 1:
            for sid in sids:
                one(sid)
            return
        run_all([lambda sid=sid: one(sid) for sid in sids], workers=min(8, len(sids)))

    def measured(self, sid, expect):
        if (sid, expect) not in self.meas:
            r = self.source(sid); s = self.P['sources'][sid]
            checked = s.get('pitch_checked') and s.get('expect_midi') is not None and int(s.get('expect_midi')) == int(expect)
            if checked:
                m = {'accepted': True, 'cents': s.get('cents'), 'note': pitch.NOTE(int(expect)), 'checked': 'gather'}
            else:
                m = pitch.measure_fft(r['y'], expect, hi=s.get('fft_hi_hz', 2000.0)) if s.get('pitch_method') == 'fft' else pitch.measure(r['y'], expect); m['accepted'] = pitch.accept(m, expect, self.P.get('pitch_tolerance_cents', 15.0))
            self.meas[(sid, expect)] = m
            if not m['accepted']: self.rejected.append(dict(source=sid, expected=pitch.NOTE(expect), measured=m))
        return self.meas[(sid, expect)]

    def pick(self, bank, midi):
        """Select by measurement: the first candidate in the bank whose measured pitch is the wanted note."""
        k = (bank, midi)
        if k in self.sel: return self.sel[k]
        cands = self.P['banks'].get(bank, {}).get('notes', {}).get(str(midi), [])
        for sid in cands:
            if self.measured(sid, midi)['accepted']: self.sel[k] = sid; return sid
        self.sel[k] = None; return None

    def note(self, sid, dur=None, pre=0.004):
        r = self.res[sid]; y = r['y']
        if self.P['sources'][sid].get('segment') is None:
            o = max(0, onset(y) - int(pre * SR)); z = y[o:] ; so = o / SR
        else: z = y; so = r['offset_s']
        if self.P['sources'][sid].get('segment') is None: so = so
        if dur: z = z[:int(dur * SR)]
        return z, (so if self.P['sources'][sid].get('segment') is None else r['offset_s'])

    def _lead(self, ly, i, default):
        table = ly.get('lead_by_chord') or {}
        if str(i) in table:
            return float(table[str(i)])
        return float(ly.get('lead_s', default))

    def put(self, bus, z, t0, g, sid, **meta):
        if t0 < 0:
            drop = int(round(-t0 * SR))
            if drop >= len(z):
                return
            z = z[drop:]
            t0 = 0.0
        if bus not in self.bus: self.bus[bus] = np.zeros((self.L, 2))
        i = int(round(t0 * SR)); n = min(len(z), self.L - i)
        if n > 0 and i < self.L: self.bus[bus][i:i + n] += z[:n] * g
        m = self.meas.get((sid, meta.get('midi'))) if meta.get('midi') is not None else None
        self.uses.append(dict(key=sid, layer=bus, at_s=round(t0, 3), len_s=round(len(z) / SR, 3), gain_db=round(20 * np.log10(max(g, 1e-9)), 2), pitch_shift=False, **meta))

    # ---------- layers ----------
    def chords(self): return self.P['chords']

    def L_chord_notes(self, ly):
        C = self.chords(); last_i = len(C) - 1
        for pc in ly['per_chord']:
            i = pc['chord']; c = C[i]; last = (i == last_i); notes = list(c['voicing'])
            if pc.get('midi'):
                allowed = {int(m) for m in c['voicing']}
                picked = [int(m) for m in pc['midi'] if int(m) in allowed]
                if picked:
                    notes = picked
            g_scale = float(pc.get('gain') or 1.0)
            style = pc.get('style') or ly.get('style', 'strike')
            for k, off in enumerate(pc.get('strikes_at', [0.0])):
                if style == 'roll':
                    lo, hi = ly.get('spread_s', [0.15, 0.45]); sp = self.rng.uniform(lo, hi); dts = [j * sp / max(1, len(notes) - 1) for j in range(len(notes))]
                else: dts = sorted(self.rng.uniform(0, ly.get('jitter_s', 0.05)) for _ in notes)
                g0 = ly.get('first_gain', 0.85) if k == 0 else ly.get('restrike_gain', 0.5)
                bank = pc.get('bank') or ly.get('bank')
                for m, dt in zip(notes, dts):
                    sid = self.pick(bank, m) if bank else None
                    gj = self.rng.uniform(*ly.get('gain_jitter', [0.8, 1.0]))
                    if sid is None: continue
                    if last and pc.get('last_note_s'):
                        sounding = float(pc['last_note_s'])
                    elif pc.get('note_s'):
                        sounding = float(pc['note_s'])
                    else:
                        sounding = ly.get('last_note_s', 12.0) if last else ly.get('note_s', 9.0)
                    fade_out = float(pc['fade_out_s']) if pc.get('fade_out_s') else (ly.get('last_fade_out_s', 6.0) if last else ly.get('fade_out_s', 3.5))
                    if pc.get("sustain"):
                        z, so = self.note(sid)
                        if len(z) + int(0.05 * SR) < int(sounding * SR):
                            # The recording is shorter than this lingering chord. Repeat it unaltered.
                            z = loop_unaltered(z, sounding)
                        else:
                            z = z[:int(sounding * SR)]
                    else:
                        z, so = self.note(sid, sounding)
                    z = fade(z, 0.02, fade_out)
                    self.put(ly.get('bus', 'keys'), z, c['t'] + ly.get('offset_s', 0.4) + off + dt, g0 * gj * ly.get('gain', 1.0) * g_scale, sid, chord_index=i, midi=m,
                             source_offset_s=round(so, 3), time_stretch=1.0, role=ly.get('role', ly['id']))

    def L_chord_pad(self, ly):
        C = self.chords(); last_i = len(C) - 1
        for i in ly.get('chords', range(len(C))):
            c = C[i]; last = (i == last_i); plen = ly.get('last_len_s', 14.0) if last else c['dur'] + ly.get('overlap_s', 4.0)
            for m in c['voicing']:
                for bank in [ly['bank']] + ly.get('fallback_banks', []):
                    sid = self.pick(bank, m)
                    if sid: break
                if not sid: continue
                a, b = ly.get('src_window_s', {}).get(bank, [0.25, 4.0])
                z, so = self.note(sid, b + 0.2); seg = z[int(a * SR):int(b * SR)]
                zz = loop_unaltered(seg, plen)
                zz = fade(zz[:int(plen * SR)], ly.get('fade_in_s', 3.0) if i else ly.get('first_fade_in_s', 4.5), ly.get('last_fade_out_s', 8.0) if last else ly.get('fade_out_s', 4.5))
                lead = self._lead(ly, i, 1.5)
                t0 = max(0.0, c['t'] - lead) if i else 0.0
                self.put(ly.get('bus', 'pad'), zz, t0, ly.get('gain_total', 0.7) / np.sqrt(len(c['voicing'])), sid, chord_index=i, midi=m, source_offset_s=round(so + a, 3),
                         source_len_s=round(b - a, 3), time_stretch=1.0, role=ly.get('role', ly['id']) + f' ({bank})')

    def L_chord_under(self, ly):
        C = self.chords(); last_i = len(C) - 1
        for si, ms in ly['notes_by_chord'].items():
            i = int(si); c = C[i]; last = (i == last_i); plen = ly.get('last_len_s', 14.0) if last else c['dur'] + ly.get('overlap_s', 4.0)
            for m in ms:
                sid = self.pick(ly['bank'], m)
                if not sid: continue
                z, so = self.note(sid); a = ly.get('trim_start_s', 0.3); seg = z[int(a * SR):int((len(z) / SR - ly.get('trim_end_s', 0.4)) * SR)]
                zz = fade(loop_unaltered(seg, plen)[:int(plen * SR)], ly.get('fade_in_s', 2.5), ly.get('last_fade_out_s', 8.0) if last else ly.get('fade_out_s', 4.0))
                g = ly.get('gain_low', ly.get('gain', 0.4)) if m < 48 else ly.get('gain', 0.4)
                lead = self._lead(ly, i, 1.0)
                self.put(ly.get('bus', 'pad'), zz, max(0.0, c['t'] - lead) if i else ly.get('first_t_s', 0.0), g, sid, chord_index=i, midi=m,
                         source_offset_s=round(so + a, 3), source_len_s=round(len(seg) / SR, 3), time_stretch=1.0, role=ly.get('role', ly['id']))

    def L_drone(self, ly):
        P = ly.get('piece_s', 15.0); H = ly.get('hop_s', 11.0); pieces = {}
        for sp in ly['spans']:
            for m in sp['midi']:
                if m not in pieces:
                    sid = self.pick(ly['bank'], m)
                    if not sid: continue
                    z, so = self.note(sid); a, b = ly.get('src_window_s', [0.5, None])
                    b = (len(z) / SR - 0.5) if b is None else b; b = min(b, len(z) / SR)
                    seg = z[int(a * SR):int(b * SR)]; x = loop_unaltered(seg, P)
                    if ly.get('flatten'): x = flatten(x)
                    pieces[m] = (sid, so + a, len(seg) / SR, 1.0, fade(x[:int(P * SR)], ly.get('fade_s', 4.0), ly.get('fade_s', 4.0)))
                if m not in pieces: continue
                sid, so, sl, r, pc = pieces[m]; t = sp['start_s']; end = sp['start_s'] + sp['dur_s']
                while t < end - 2.0:
                    z = fade(pc[:int(min(P, end - t + 2.0) * SR)], ly.get('first_fade_in_s', 6.0) if t == 0 else ly.get('fade_s', 4.0), ly.get('fade_s', 4.0))
                    self.put(ly.get('bus', 'drone'), z, t, ly.get('gain_by_midi', {}).get(str(m), 1.0), sid, midi=m, source_offset_s=round(so, 3), source_len_s=round(sl, 3), time_stretch=round(r, 4), role=ly.get('role', ly['id']))
                    t += H

    def L_clips(self, ly):
        for e in ly['events']:
            sid = e['source']; r = self.source(sid); s = self.P['sources'][sid]
            if s.get('expect_midi') is not None and not self.measured(sid, s['expect_midi'])['accepted']:
                self.log(f'  skip {sid}: measured pitch is not {pitch.NOTE(s["expect_midi"])}'); continue
            if e.get('from_onset'):
                z, so = self.note(sid, e.get('dur_s'))
            else:
                y = r['y']; o = int(e.get('offset_s', 0.0) * SR); z = y[o:o + int(e['dur_s'] * SR)] if e.get('dur_s') else y[o:]; so = r['offset_s'] + e.get('offset_s', 0.0)
            if e.get('stretch_to_s'):
                raise RuntimeError('time-stretch is not allowed')
            z = fade(z, e.get('fade_in_s', 0.01), e.get('fade_out_s', 3.0))
            meta = {}
            if e.get('chord_index') is not None:
                meta['chord_index'] = e['chord_index']
            self.put(ly.get('bus', ly['id']), z, e['t'], e.get('gain', 1.0), sid, midi=s.get('expect_midi'), source_offset_s=round(so, 3), time_stretch=1.0, role=e.get('role', ly.get('role', ly['id'])), **meta)

    def L_notes(self, ly):
        half = ly.get('half_s', self.END / 2); first_db = ly.get('first_half_db', 0.0)
        for e in sorted(ly['events'], key=lambda e: e['t']):
            m = e['midi']; base = 10 ** ((first_db if e['t'] < half else 0.0) / 20); tri = e.get('kind') == 'triplet'
            for tb in e['timbres']:
                sid = self.pick(tb['bank'], m)
                if not sid: self.log(f"  line {pitch.NOTE(m)} at {e['t']}: no measured match in bank {tb['bank']}"); continue
                z, so = self.note(sid, min(e['dur_s'], tb.get('max_s', 1e9)), pre=tb.get('pre_s', 0.004))
                if ly.get('level_match_rms'): z = z * (ly['level_match_rms'] / (np.sqrt(np.mean(z[:int(1.5 * SR)] ** 2)) + 1e-9))
                z = fade(z, tb.get('fade_in_s', 0.03), ly.get('triplet_fade_out_s', 0.6) if tri else tb.get('fade_out_s', ly.get('fade_out_s', 2.5)))
                g = tb.get('gain', 0.8) * (ly.get('triplet_gain', 0.7) if tri else 1.0) * base * e.get('gain', 1.0)
                self.put(ly.get('bus', 'line'), z, e['t'], g, sid, midi=m, pitch=pitch.NOTE(m), kind=e.get('kind', 'line'), timbre=tb['bank'],
                         source_offset_s=round(so, 3), time_stretch=1.0, role=ly.get('role', 'line'))

    # ---------- mix + master ----------
    def mix(self):
        M = self.P['master']; t = np.arange(self.L) / SR; meter = pyln.Meter(SR); before = {}
        for name, x in self.bus.items():
            b = M['buses'].get(name, {})
            for f in b.get('filters', []): x = (lp if f['type'] == 'lp' else hp)(x, f['hz'], f.get('order', 2))
            if 'brighten' in b:
                br = b['brighten']; a = lp(x, br['dark_hz'], 2); c = lp(x, br['bright_hz'], 2); w = np.interp(t, br['t'] + [self.L / SR], br['mix'] + [br['mix'][-1]])[:, None]; x = a * (1 - w) + c * w
            if 'delay' in b:
                d = b['delay']; n = int(d['time_s'] * SR); dl = np.zeros_like(x); src = x.copy()
                for k in range(1, d.get('taps', 4) + 1):
                    sh = k * n; tap = lp(src[:self.L - sh], max(300, d.get('tone_hz', 2000) - 250 * k), 1) * (d['feedback'] ** k)
                    if k % 2: dl[sh:, 0] += tap[:, 1] * 0.9; dl[sh:, 1] += tap[:, 0] * 0.6
                    else: dl[sh:, 0] += tap[:, 0] * 0.6; dl[sh:, 1] += tap[:, 1] * 0.9
                x = x + d.get('mix', 0.5) * dl
            seg = x[:int(self.END * SR)]; l = meter.integrated_loudness(seg) if np.any(seg) else -99.0; before[name] = round(float(l), 2)
            if 'lufs' in b and l > -90: x = x * 10 ** ((b['lufs'] - l) / 20)
            p = b.get('pan', 0.0); x[:, 0] *= np.sqrt(1 - p); x[:, 1] *= np.sqrt(1 + p)
            if 'gain_db_curve' in b:
                gc = b['gain_db_curve']; x = x * (10 ** (np.interp(t, gc['t'] + [self.L / SR], gc['db'] + [gc['db'][-1]]) / 20))[:, None]
            self.bus[name] = x
        os.makedirs(os.path.join(self.out, 'stems'), exist_ok=True)
        for k, x in self.bus.items(): sf.write(os.path.join(self.out, 'stems', f'{k}.wav'), x, SR, subtype='FLOAT')
        x = sum(self.bus.values()); rv = M.get('reverb', {})
        if rv.get('engine', 'dragonfly') == 'synthetic' and rv.get('send_rule') == 'legacy_peak':
            # kit 0.1 law, kept so old plans can be reproduced exactly: dry and wet each peak-normalised, then mixed by 'wet'
            ir = reverb._synthetic_ir(rv); wet = np.stack([fftconvolve(x[:, c], ir[:, c])[:self.L] for c in range(2)], 1); W = rv.get('wet', 0.4)
            out = (1 - W) * x / np.max(np.abs(x)) + W * 1.6 * wet / np.max(np.abs(wet))
            self.reverb_info = dict(engine_requested='synthetic', engine='synthetic', implementation='synthetic hall: seeded filtered noise IR, fftconvolve', params=rv, send_rule=dict(rule='legacy peak law (kit 0.1)', wet=W), deterministic=True)
        else:
            ws, self.reverb_info, self.ir_inputs = reverb.process(x, rv, self.END, log=self.log); out = x + ws
        out = hp(out, M.get('hp_hz', 30))
        tf = M.get('tone_filter', 'shelf'); lb = M.get('lp_blend', {'hz': 9000, 'mix': 0.4})
        if tf == 'lp_blend':
            out = (1 - lb['mix']) * out + lb['mix'] * lp(out, lb['hz']); tone = dict(tool='lowpass blend (legacy)', **lb)
        else:
            hs = M.get('high_shelf') or dict(hz=lb['hz'], db=round(20 * np.log10(max(1 - lb['mix'], 1e-3)), 2))
            out = dsp.high_shelf(out, hs['hz'], hs['db'], hs.get('slope', 0.7)); tone = dict(tool='high shelf (RBJ biquad)', hz=hs['hz'], db=hs['db'], slope=hs.get('slope', 0.7),
                                                                                              derived_from_lp_blend=not M.get('high_shelf'))
        self.tone_info = tone
        endi = int((self.END + self.TAIL) * SR); out = out[:endi]; fin = int(self.TAIL * SR); out[-fin:] *= np.linspace(1, 0, fin)[:, None] ** 2; out /= np.max(np.abs(out)) * 1.05
        return out, before

    def master(self, out, name):
        Lm = self.P['master'].get('loudness', {}); I = Lm.get('lufs', -17); TP = Lm.get('tp', -1.5); LRA = Lm.get('lra', 11); method = Lm.get('method', 'linear_tp_limiter')
        fl = os.path.join(self.out, name + '.flac'); mp3 = os.path.join(self.out, name + '_preview.mp3')
        if method == 'loudnorm':   # legacy: ffmpeg loudnorm two-pass; it silently goes dynamic (AGC) when linear gain would break the TP target
            raw = os.path.join(self.out, 'premaster.wav'); sf.write(raw, out, SR, subtype='PCM_24')
            p1 = subprocess.run(['ffmpeg', '-hide_banner', '-i', raw, '-af', f'loudnorm=I={I}:TP={TP}:LRA={LRA}:print_format=json', '-f', 'null', '-'], capture_output=True, text=True).stderr
            m = json.loads(p1[p1.rindex('{'):p1.rindex('}') + 1])
            ln = f"loudnorm=I={I}:TP={TP}:LRA={LRA}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true:print_format=json"
            r = subprocess.run(['ffmpeg', '-y', '-hide_banner', '-i', raw, '-af', ln, '-ar', '48000', '-c:a', 'flac', '-sample_fmt', 's32', '-bits_per_raw_sample', '24', fl], capture_output=True, text=True, check=True).stderr
            m2 = json.loads(r[r.rindex('{'):r.rindex('}') + 1])
            self.master_info = dict(method='ffmpeg loudnorm two-pass (legacy)', mode_actual=m2.get('normalization_type'), target_lufs=I, ceiling_dbtp=TP, pass1=m)
        else:
            meter = pyln.Meter(SR); li = meter.integrated_loudness(out); g = I - li; gr = 0.0
            for _ in range(4):
                y, gr = dsp.tp_limit(out * 10 ** (g / 20), TP); d = I - meter.integrated_loudness(y); g += d
                if abs(d) < 0.02: break
            sf.write(fl, y, SR, subtype='PCM_24'); m = dict(input_i=round(li, 2))
            self.master_info = dict(method='linear gain to target + 4x-oversampled stereo-linked lookahead true-peak limiter (3 ms lookahead, 150 ms release)',
                                    mode_actual='linear' if gr == 0.0 else 'linear+tp_limiter', target_lufs=I, ceiling_dbtp=TP, premaster_lufs=round(li, 2), gain_db=round(g, 2),
                                    limiter_max_gain_reduction_db=round(gr, 2), true_peak_4x_dbtp=round(dsp.true_peak_db(y), 2))
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', fl, '-ar', '48000', '-c:a', 'libmp3lame', '-b:a', '320k', mp3], check=True)
        return fl, mp3, m

    def run(self, name='performance'):
        os.makedirs(self.out, exist_ok=True)
        if self.P.get('from_gather'):
            self._warm()
        for ly in self.P['layers']:
            self.log(f"layer {ly['id']} ({ly['type']})"); getattr(self, 'L_' + ly['type'])(ly)
        out, before = self.mix(); fl, mp3, p1 = self.master(out, name)
        sel = {f'{b}:{pitch.NOTE(m)}': s for (b, m), s in self.sel.items()}
        meas = [dict(source=s, expected=pitch.NOTE(m), **v) for (s, m), v in self.meas.items()]
        log = dict(seed=self.P['seed'], uses=self.uses, selections=sel, measurements=meas, rejected=self.rejected, bus_lufs_before=before, pass1=p1,
                   reverb=self.reverb_info, ir_inputs=self.ir_inputs, master=self.master_info, tone=self.tone_info,
                   quality=dict(resampler=sorted(util.USED['resampler']), stretch=sorted(util.USED['stretch'])),
                   sources={k: {kk: vv for kk, vv in v.items() if kk not in ('y',)} for k, v in self.res.items()})
        json.dump(log, open(os.path.join(self.out, 'uses.json'), 'w'), indent=1, default=float)
        return fl, mp3, log
