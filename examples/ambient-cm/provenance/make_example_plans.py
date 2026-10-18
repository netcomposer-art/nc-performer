# Writes perf02.plan.json and perf03.plan.json from the original perf02/perf03 renders (nc_ambient_cm/work/uses_0{2,3}.json
# and the two scripts' constants). Kept for provenance: the plans are the generalized form of those two scripts.
import json, os, re
import sys
W = os.environ.get('NC_PERF_WORKDIR') or (sys.argv[1] if len(sys.argv) > 1 else sys.exit('usage: make_example_plans.py <dir with work/uses_02.json and work/uses_03.json from the original renders>')); OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMP = dict(title='Cm into Am and F, with a slow C\u2013F\u2013G line (ambient)', composer='Peter',
            words_verbatim=dict(composition='2 mins - half Cm, Cm7, Cm9. Second half Cm7, Am, F.',
                                performance="ambient vibe, no harsh tones, randomly alt between Cm chord structure in first half, build to second half and follow Cm, Am, F pattern. Don't rush it. Over the top of it all play a slow C, F, G pattern. Include an occasional triplet"),
            file='composition.md')
IOWA = 'https://theremin.music.uiowa.edu/sound%20files/MIS/'
PIANO = {36:'C2',41:'F2',45:'A2',48:'C3',50:'D3',51:'Eb3',52:'E3',53:'F3',55:'G3',57:'A3',58:'Bb3',60:'C4',62:'D4',63:'Eb4',64:'E4',65:'F4',67:'G4',69:'A4',70:'Bb4',72:'C5',75:'Eb5',76:'E5',77:'F5',79:'G5'}
RHODES = {48:'65719',51:'65721',52:'65725',53:'65728',55:'65730',57:'65715',58:'65714',60:'65720',62:'65724',63:'65722',64:'65726'}
CELLO = {36:('C2B2',0),40:('C2B2',4),53:('C3C4',4),55:('C3C4',6),58:('C3C4',9)}
VIOLIN = {72:0,77:5,79:7}
def sources():
    S = {}
    for m, n in PIANO.items(): S[f'piano-{n}'] = dict(kind='uiowa_mis', url=IOWA + f'Piano_Other/piano/Piano.pp.{n}.aiff', page='https://theremin.music.uiowa.edu/MISpiano.html')
    for m, i in RHODES.items(): S[f'rhodes-{i}'] = dict(kind='freesound', id=i)
    for m, (r, i) in CELLO.items(): S[f'cello-{r}-{i}'] = dict(kind='uiowa_mis', url=IOWA + f'Strings/cello2012/Cello.arco.pp.sulC.{r}.stereo.aif', page='https://theremin.music.uiowa.edu/MIScello2012.html', segment=dict(index=i))
    for m, i in VIOLIN.items(): S[f'violin-C5B5-{i}'] = dict(kind='uiowa_mis', url=IOWA + 'Strings/violin2012/Violin.arco.pp.sulD.C5B5.stereo.aif', page='https://theremin.music.uiowa.edu/MISviolin2012.html', segment=dict(index=i))
    S['altoflute-C5'] = dict(kind='uiowa_mis', url='https://theremin.music.uiowa.edu/sound%20files/MIS%20Pitches%20-%202014/Woodwinds/Alto%20Flute/AltoFlute.vib.ff.C5.stereo.aif', page='https://theremin.music.uiowa.edu/MIS-Pitches-2012/MISaltoflute2012.html')
    for i in ['467667','467684','467689','399493','399505','399476','373563','373573']: S[f'fs-{i}'] = dict(kind='freesound', id=i)
    S['harp-C5'] = S.pop('fs-373563'); S['harp-G5'] = S.pop('fs-373573'); S['harp-C5']['expect_midi'] = 72; S['harp-G5']['expect_midi'] = 79
    return S
def banks(guitar):
    B = dict(piano=dict(notes={str(m): [f'piano-{n}'] for m, n in PIANO.items()}),
             rhodes=dict(notes={str(m): [f'rhodes-{i}'] for m, i in RHODES.items()}),
             cello=dict(notes={str(m): [f'cello-{r}-{i}'] for m, (r, i) in CELLO.items()}),
             violin=dict(notes={str(m): [f'violin-C5B5-{i}'] for m, i in VIOLIN.items()}),
             harp=dict(notes={'72': ['harp-C5'], '79': ['harp-G5']}),
             altoflute=dict(notes={'72': ['altoflute-C5']}),
             guitar=dict(notes=guitar))
    return B
def chords(U, n):
    return [dict(t=p['t0'], dur=10.0, symbol=p['chord'], voicing=p['midi'], section=('A' if p['i'] < 4 else 'build') if p['section'] == 'A' else 'B') for p in U['plan']]
SECTIONS = [dict(id='A', start_s=0, end_s=60, harmony='random alternation of Cm / Cm7 / Cm9', notes='slow changes, long crossfades'),
            dict(id='build', start_s=38, end_s=62, harmony='(continues A)', notes='rising level, opening tone, more motion'),
            dict(id='B', start_s=60, end_s=120, harmony='Cm7 -> Am -> F, twice', notes='slow, even changes'),
            dict(id='line', start_s=5, end_s=120, harmony='single notes C5, F5, G5', notes='slow; an occasional triplet')]
def line_events(U, mapper):
    ev = []
    for e in U['line_events']:
        m = {'C': 72, 'F': 77, 'G': 79}[e['pitch']]
        tb = [mapper(w, m, e['kind']) for w in e['timbre'].split('+')]
        tb = [x for x in tb if x]
        ev.append(dict(t=e['t'], midi=m, dur_s=e['len'], kind=e['kind'], timbres=tb))
    return ev
def plan03():
    U = json.load(open(f'{W}/work/uses_03.json')); S = sources()
    S['piano-C3']['expect_midi'] = 48
    S['bed-pineforest'] = dict(kind='freesound', id='173031', range_bytes=6000001, pitched=False)
    S['bed-rain'] = dict(kind='freesound', id='398425', range_bytes=6000001, pitched=False)
    S['mm-C-Cm7_5_root_8va'] = dict(kind='supplier', manifest='https://musicmatter.art/sounds/fullchords-v4/C/00a_main_Cm7_5_root_8va.nc.json', pitched=False)
    S['mm-A-Am_6_inv2_8va'] = dict(kind='supplier', manifest='https://musicmatter.art/sounds/fullchords-v4/A/00a_main_Am_6_inv2_8va.nc.json', pitched=False)
    C = chords(U, 3); P = U['plan']
    def mp(w, m, kind):
        if w == 'harp' and m not in (72, 79): return None
        if w == 'altoflute' and m != 72: return None
        return {'guitar': dict(bank='guitar', gain=0.8), 'piano': dict(bank='piano', gain=1.0), 'violin': dict(bank='violin', gain=0.8, fade_in_s=0.25),
                'harp': dict(bank='harp', gain=0.8), 'altoflute': dict(bank='altoflute', gain=0.45, fade_in_s=0.3, fade_out_s=1.6, max_s=3.0, pre_s=0.02)}[w]
    layers = [
      dict(id='keys-piano', type='chord_notes', bus='keys', bank='piano', style='roll', spread_s=[0.15, 0.45], first_gain=0.85, restrike_gain=0.5, gain=1.0, offset_s=0.4,
           per_chord=[dict(chord=p['i'], strikes_at=[0.0] + p['restrikes']) for p in P if 'piano' in p['keys']], role='piano roll'),
      dict(id='keys-rhodes', type='chord_notes', bus='keys', bank='rhodes', style='strike', jitter_s=0.05, first_gain=0.85, restrike_gain=0.5, gain=0.8, offset_s=0.4,
           per_chord=[dict(chord=p['i'], strikes_at=[0.0] + p['restrikes']) for p in P if 'rhodes' in p['keys']], role='rhodes strike'),
      dict(id='pad', type='chord_pad', bus='pad', bank='rhodes', fallback_banks=['piano'], src_window_s=dict(rhodes=[0.2, 6.2], piano=[0.25, 4.0]), overlap_s=4.0, lead_s=1.5, gain_total=0.7, flatten=True, role='stretched pad'),
      dict(id='cello', type='chord_under', bus='pad', bank='cello', notes_by_chord={str(p['i']): p['cello'] for p in P if p['cello']}, gain=0.4, lead_s=1.0, role='cello'),
      dict(id='drone', type='drone', bus='drone', bank='cello', piece_s=15.0, hop_s=11.0, src_window_s=[0.5, None], flatten=False,
           spans=[dict(start_s=0.0, dur_s=72.0, midi=[36]), dict(start_s=88.0, dur_s=14.0, midi=[36]), dict(start_s=78.0, dur_s=12.5, midi=[53]), dict(start_s=108.0, dur_s=16.0, midi=[53])], role='cello drone'),
      dict(id='bed', type='clips', bus='bed', events=[dict(t=0.0, source='bed-pineforest', offset_s=40.0, dur_s=68.0, fade_in_s=8.0, fade_out_s=12.0, gain=1.0, role='field recording bed (A)'),
                                                      dict(t=56.0, source='bed-rain', offset_s=60.0, dur_s=66.0, fade_in_s=12.0, fade_out_s=10.0, gain=1.0, role='field recording bed (B)')]),
      dict(id='plucks', type='clips', bus='pluck', events=[dict(t=t, source=s, from_onset=True, dur_s=7.0, fade_in_s=0.01, fade_out_s=3.0, gain=g, role=r) for t, s, g, r in
           [(44.6, 'harp-C5', 0.6, 'harp pluck'), (49.6, 'harp-G5', 0.55, 'harp pluck'), (54.6, 'piano-C3', 0.8, 'piano pluck'), (58.3, 'harp-C5', 0.6, 'harp pluck'), (90.4, 'piano-C3', 0.7, 'piano pluck')]]),
      dict(id='musicmatter', type='clips', bus='mm', events=[dict(t=46.2, source='mm-C-Cm7_5_root_8va', fade_in_s=0.05, fade_out_s=1.0, gain=1.0, role='musicmatter chord, faint'),
                                                             dict(t=76.2, source='mm-A-Am_6_inv2_8va', fade_in_s=0.05, fade_out_s=1.0, gain=1.0, role='musicmatter chord, faint')]),
      dict(id='line', type='notes', bus='line', level_match_rms=0.1, first_half_db=-1.5, half_s=60.0, fade_out_s=2.5, triplet_fade_out_s=0.6, triplet_gain=0.7, events=line_events(U, mp), role='line')]
    br = lambda d, b: dict(dark_hz=d, bright_hz=b, t=[0, 40, 60, 105, 130], mix=[0, 0, 1, 1, 0.3])
    build = dict(t=[0, 38, 62], db=[-6.0, -6.0, 0.0])
    master = dict(buses=dict(
        pad=dict(brighten=br(1100, 3000), lufs=-21.5, pan=0.06, gain_db_curve=build), keys=dict(brighten=br(1700, 4000), lufs=-22.0, pan=-0.1, gain_db_curve=build),
        mm=dict(brighten=br(1500, 3000), lufs=-32.0, pan=0.15), drone=dict(filters=[dict(type='hp', hz=45, order=2), dict(type='lp', hz=300, order=4)], lufs=-27.0, pan=0.0, gain_db_curve=build),
        bed=dict(filters=[dict(type='hp', hz=150, order=2), dict(type='lp', hz=3000, order=2)], lufs=-30.0, pan=0.0), pluck=dict(filters=[dict(type='lp', hz=2500, order=2)], lufs=-30.0, pan=0.22),
        line=dict(filters=[dict(type='hp', hz=180, order=2), dict(type='lp', hz=5200, order=2)], delay=dict(time_s=1.0, feedback=0.32, taps=4, tone_hz=2000, mix=0.5), lufs=-22.0, pan=0.12)),
        reverb=dict(engine='dragonfly', send_lu=5.3), hp_hz=30, high_shelf=dict(hz=9000, db=-4.4), loudness=dict(lufs=-17, tp=-1.5))
    guitar = {'72': ['fs-467667', 'fs-399493'], '77': ['fs-467684', 'fs-399505'], '79': ['fs-467689', 'fs-399476']}
    return dict(nc_plan_version='0.1', title='Cm into Am and F, with a slow C\u2013F\u2013G line (ambient): take 3, rolled piano and forest air', id='ambient-cm-03',
                composition=COMP, interpreter=dict(kind='human/agent-written plan', note='written by Astrid (NetComposer Performer agent) for perf03; generalized from render_ambient_cm_03.py'),
                seed=2026, length_s=120.0, tail_s=4.0, pulse_bpm=60, pitch_tolerance_cents=15.0, sections=SECTIONS, chords=C, sources=S, banks=banks(guitar), layers=layers, master=master,
                notes=['Guitar bank lists allan764 first and Skamos66 as the fallback: the render keeps whichever measures as the wanted note (perf03 rejected allan764 G5).'])
def plan02():
    U = json.load(open(f'{W}/work/uses_02.json')); S = sources()
    S['bed-waves'] = dict(kind='freesound', id='587164', pitched=False)
    S['bell-C5'] = dict(kind='freesound', id='857877', expect_midi=72, pitch_method='fft', fft_hi_hz=900.0)
    mm = sorted({(u['at_s'], u['key'][3:]) for u in U['uses'] if u['key'].startswith('mm:')})
    for _, k in mm: S['mm-' + k.replace('/', '-')] = dict(kind='supplier', manifest=f'https://musicmatter.art/sounds/fullchords-v4/{k}.nc.json', pitched=False)
    C = chords(U, 2); P = U['plan']
    def mp(w, m, kind):
        if w == 'harp' and m not in (72, 79): return None
        return {'guitar': dict(bank='guitar', gain=0.8, fade_in_s=0.03 if kind == 'triplet' else 0.05), 'piano': dict(bank='piano', gain=0.9, fade_in_s=0.03 if kind == 'triplet' else 0.05),
                'violin': dict(bank='violin', gain=0.75 * (1.5 if m == 77 else 1.0), fade_in_s=0.25), 'harp': dict(bank='harp', gain=0.5, fade_in_s=0.05)}[w]
    layers = [
      dict(id='keys-rhodes', type='chord_notes', bus='keys', bank='rhodes', style='strike', jitter_s=0.09, first_gain=0.9, restrike_gain=0.55, gain=1.0, offset_s=0.3,
           per_chord=[dict(chord=p['i'], strikes_at=[0.0] + p['restrikes']) for p in P if 'rhodes' in p['keys']], role='rhodes strike'),
      dict(id='keys-piano', type='chord_notes', bus='keys', bank='piano', style='strike', jitter_s=0.09, first_gain=0.9, restrike_gain=0.55, gain=0.85, offset_s=0.3,
           per_chord=[dict(chord=p['i'], strikes_at=[0.0] + p['restrikes']) for p in P if 'piano' in p['keys']], role='piano strike'),
      dict(id='pad', type='chord_pad', bus='pad', bank='piano', src_window_s=dict(piano=[0.25, 4.0]), overlap_s=4.0, lead_s=1.5, gain_total=0.7, flatten=True, role='stretched pad'),
      dict(id='cello', type='chord_under', bus='pad', bank='cello', notes_by_chord={str(p['i']): p['cello'] for p in P if p['cello']}, gain=0.35, gain_low=0.45, lead_s=1.0, first_t_s=0.5, role='cello'),
      dict(id='drone', type='drone', bus='drone', bank='piano', piece_s=15.0, hop_s=11.0, src_window_s=[0.25, 4.0], flatten=True, gain_by_midi={'36': 1.0, '48': 0.6, '41': 1.0, '53': 0.6},
           spans=[dict(start_s=0.0, dur_s=72.0, midi=[36, 48]), dict(start_s=88.0, dur_s=14.0, midi=[36, 48]), dict(start_s=78.0, dur_s=12.5, midi=[41, 53]), dict(start_s=108.0, dur_s=16.0, midi=[41, 53])], role='piano drone'),
      dict(id='bed', type='clips', bus='bed', events=[dict(t=0.0, source='bed-waves', offset_s=30.0, dur_s=120.0, fade_in_s=8.0, fade_out_s=12.0, gain=1.0, role='field recording bed')]),
      dict(id='bells', type='clips', bus='bell', events=[dict(t=t, source='bell-C5', offset_s=0.0, dur_s=16.0, fade_in_s=0.01, fade_out_s=6.0, gain=g, role='bell accent') for t, g in [(45.0, 0.7), (52.5, 0.6), (58.0, 1.0), (90.2, 0.6)]]),
      dict(id='musicmatter', type='clips', bus='mm', events=[dict(t=t, source='mm-' + k.replace('/', '-'), fade_in_s=0.05, fade_out_s=1.0, gain=1.0, role='musicmatter chord, faint') for t, k in mm]),
      dict(id='line', type='notes', bus='line', level_match_rms=None, first_half_db=-1.5, half_s=60.0, fade_out_s=2.5, triplet_fade_out_s=0.6, triplet_gain=0.7, events=line_events(U, mp), role='line')]
    br = lambda d, b: dict(dark_hz=d, bright_hz=b, t=[0, 40, 60, 105, 130], mix=[0, 0, 1, 1, 0.3])
    build = dict(t=[0, 38, 62], db=[-6.0, -6.0, 0.0])
    master = dict(buses=dict(
        pad=dict(brighten=br(1200, 3200), lufs=-22.0, pan=-0.08, gain_db_curve=build), keys=dict(brighten=br(1800, 4200), lufs=-22.0, pan=0.1, gain_db_curve=build),
        mm=dict(brighten=br(1500, 3000), lufs=-31.0, pan=-0.15), drone=dict(filters=[dict(type='hp', hz=50, order=2), dict(type='lp', hz=280, order=4)], lufs=-28.0, pan=0.0, gain_db_curve=build),
        bed=dict(filters=[dict(type='hp', hz=120, order=2), dict(type='lp', hz=2600, order=2)], lufs=-29.0, pan=0.0, gain_db_curve=dict(t=[0, 40, 60, 100, 130], db=[0, 0, 2.5, 0, -3])),
        bell=dict(filters=[dict(type='lp', hz=750, order=4), dict(type='lp', hz=750, order=4)], lufs=-31.0, pan=-0.2),
        line=dict(filters=[dict(type='hp', hz=180, order=2), dict(type='lp', hz=5500, order=2)], delay=dict(time_s=0.75, feedback=0.3, taps=5, tone_hz=2200, mix=0.5), lufs=-22.0, pan=0.15)),
        reverb=dict(engine='dragonfly', send_lu=5.3), hp_hz=30, high_shelf=dict(hz=9000, db=-4.4), loudness=dict(lufs=-17, tp=-1.5))
    guitar = {'72': ['fs-399493'], '77': ['fs-399505'], '79': ['fs-399476']}
    return dict(nc_plan_version='0.1', title='Cm into Am and F, with a slow C\u2013F\u2013G line (ambient): take 2, open-commons palette', id='ambient-cm-02',
                composition=COMP, interpreter=dict(kind='human/agent-written plan', note='written by Astrid (NetComposer Performer agent) for perf02; generalized from render_ambient_cm_02.py'),
                seed=1018, length_s=120.0, tail_s=4.0, pulse_bpm=60, pitch_tolerance_cents=15.0, sections=SECTIONS, chords=C, sources=S, banks=banks(guitar), layers=layers, master=master, notes=[])
def prune(p):
    """Keep only the sources the plan can reach (via banks used by layers, or directly)."""
    used_banks = set()
    for ly in p['layers']:
        for k in ('bank',): used_banks.add(ly.get(k))
        used_banks |= set(ly.get('fallback_banks', []))
        for e in ly.get('events', []):
            for tb in e.get('timbres', []): used_banks.add(tb['bank'])
    p['banks'] = {k: v for k, v in p['banks'].items() if k in used_banks}
    ids = {s for b in p['banks'].values() for l in b['notes'].values() for s in l} | {e['source'] for ly in p['layers'] for e in ly.get('events', []) if 'source' in e}
    p['sources'] = {k: v for k, v in p['sources'].items() if k in ids}
    return p
for n, f in [('perf02', plan02), ('perf03', plan03)]:
    p = prune(f()); json.dump(p, open(f'{OUT}/{n}.plan.json', 'w'), indent=1, ensure_ascii=False); print(n, len(p['sources']), 'sources', len(p['layers']), 'layers')
