"""Performance record: every source (URL, license, author, sha256, uses), every setting, the seed, the interpreter, the outputs.
record_hash = sha256 of the canonical JSON (sorted keys, no whitespace) of the record without record_hash and signature."""
import collections, datetime, hashlib, json, os, subprocess
from . import licenses as LIC, __version__
from .util import sha256_file, canon

FORMAT_NOTE = 'Draft format (netcomposer.art/record/), same shape as the published ambient-cm and BWV 846 records.'

def loud(p):
    r = subprocess.run(['ffmpeg', '-hide_banner', '-i', p, '-af', 'ebur128=peak=true:framelog=quiet', '-f', 'null', '-'], capture_output=True, text=True).stderr
    s = r[r.rindex('Summary'):]
    return float(s.split('I:')[1].split('LUFS')[0]), float(s.split('Peak:')[1].split('dBFS')[0]), float(s.split('LRA:')[1].split('LU')[0])

def dur(p): return float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', p], capture_output=True, text=True).stdout)

def rhash(r): return hashlib.sha256(canon({k: v for k, v in r.items() if k not in ('record_hash', 'signature')}).encode()).hexdigest()

def interpreter_label(it):
    if not it: return 'human/agent-written plan'
    if it.get('kind') == 'llm': return it.get('model') or 'llm (model not named)'
    if it.get('kind') == 'discover' and str(it.get('note') or '').startswith('raw'):
        return 'raw assembly (one clip per chord, no processing)'
    if it.get('kind') == 'discover': return 'even-handed source discovery (no supplier preferred)'
    if it.get('kind') == 'stub': return 'nc interpret --stub (rule-based draft, no LLM)'
    return 'human/agent-written plan'

def hacks_chain(plan, log=None):
    """The hacks in the order they ran. With a render log, the reverb, tone and loudness entries say what actually ran.

    Time-stretch is not a hack this kit runs. A log that still lists a stretch engine records that fact;
    a new render leaves that list empty. Raw assembly records only the trim, the fades,
    the per-clip loudness match, and the peak guard.
    """
    if plan.get('assembly') == 'raw':
        if plan.get('assembly_plan'):
            fade = 'equal-power fades from the assembly plan, each at most 50 ms, placed at the plan\'s start time'
        else:
            fade = '12 ms equal-power fade in and out, overlapped 12 ms at each join'
        return [
            dict(n=1, tool='trim', type='each clip cut to its chord slot; a shorter clip ends, and the rest of the slot is silence'),
            dict(n=2, tool='fade', type=fade),
            dict(n=3, tool='loudness', type='each clip matched to the same integrated loudness', target_lufs=-20, mode_actual='per-clip'),
            dict(n=4, tool='peak', type='one gain on the sum so the peak stays under full scale', ceiling=0.98),
        ]
    log = log or {}; M = plan['master']; Q = log.get('quality') or {}
    H = []
    st = Q.get('stretch') or []
    if st:
        H.append(dict(n=len(H) + 1, tool='time_stretch', engine='; '.join(st), engine_requested=(plan.get('quality') or {}).get('stretch_engine', 'auto'), pitch_shift=False,
                      options='recorded from an older log; the current renderer refuses time-stretch'))
    if Q.get('resampler'): H.append(dict(n=len(H) + 1, tool='sample-rate conversion (decode to 48 kHz)', engine='; '.join(Q['resampler']), pitch_shift=False))
    H.append(dict(n=len(H) + 1, tool='gain', type='decay flatten (300 ms RMS follower, max +20 dB) where the plan asks for it'))
    if any(ly.get('level_match_rms') for ly in plan['layers']): H.append(dict(n=len(H) + 1, tool='gain', type='level match: every line source to the same 1.5 s RMS'))
    H.append(dict(n=len(H) + 1, tool='fade', type='equal-power fades; seeded rolls/strikes', seed=plan['seed']))
    H.append(dict(n=len(H) + 1, tool='filters/automation/lufs_match/pan/delay', buses=M['buses']))
    rv = log.get('reverb')
    if rv: H.append(dict(n=len(H) + 1, tool='reverb', **rv))
    else: H.append(dict(n=len(H) + 1, tool='reverb', type='synthetic hall generated from the seed', **M.get('reverb', {})))
    H.append(dict(n=len(H) + 1, tool='highpass', cutoff_hz=M.get('hp_hz', 30)))
    H.append(dict(n=len(H) + 1, **(log.get('tone') or dict(tool='lowpass blend', **M.get('lp_blend', {})))))
    H.append(dict(n=len(H) + 1, tool='tail', rule=f"music ends {plan['length_s']} s, {plan.get('tail_s', 4.0)} s fade"))
    L = M.get('loudness', {}); mi = log.get('master')
    if mi: H.append(dict(n=len(H) + 1, tool='loudness', **mi))
    else: H.append(dict(n=len(H) + 1, tool='loudness', method='ffmpeg loudnorm two-pass, linear', target_lufs=L.get('lufs', -17), ceiling_dbtp=L.get('tp', -1.5)))
    return H

def ir_sounds(log):
    """Impulse responses heard through the convolution reverb, as sounds_used entries (credited like any other input)."""
    x = log.get('ir_inputs')
    if not x: return []
    e = x['entry']; out = []
    for i in x['inputs']:
        out.append(dict(key=i['key'], source='OpenAIR' if 'openair' in i['key'] else e.get('source', 'url'), title=f"{e.get('title')}: {i['label']}", source_url=e['page'], source_url_archive=e.get('page_archive'),
                        author=e['author'], audio_used=dict(url=i['url'], what='impulse response (B-format WAV), full file', sha256=i['sha256']), supplier=None, license=e['license'],
                        credit_line=e['attribution'], license_verified=e.get('verified'), pitch_measured=None, roles=['reverb impulse response'], uses=1, time_stretch_ratios=[1.0],
                        pitch_shift=False, spans=[], modifications='decoded to virtual microphones, resampled to 48 kHz, direct sound removed, truncated at the noise floor, predelay added' + (', decay reshaped' if log['reverb'].get('decay_reshape_s') else '')))
    return out

def with_chord_span(use, chords):
    """Copy the chord's real start and end onto a use, so the strip can show uneven slots."""
    span = dict(use)
    idx = span.get('chord_index')
    if isinstance(idx, int) and 0 <= idx < len(chords or []):
        chord = chords[idx]
        start = float(chord.get('t') or 0)
        span['chord_start_s'] = round(start, 3)
        span['chord_end_s'] = round(start + float(chord.get('dur') or 0), 3)
    return span

def build(plan, plan_path, log, flac, mp3, out_path, performer='nc-performer kit', composition_path=None):
    tz = datetime.datetime.now().astimezone().tzinfo; now = datetime.datetime.now(tz)
    by = collections.defaultdict(list)
    for u in log['uses']: by[u['key']].append(u)
    meas = collections.defaultdict(list)
    for m in log['measurements']: meas[m['source']].append(m)
    sounds = []
    for sid, us in by.items():
        r = log['sources'][sid]; acc = [m for m in meas.get(sid, []) if m.get('accepted')]
        pm = None if not acc else dict(sounding=acc[0]['note'], cents=acc[0]['cents'], spread_cents=acc[0].get('spread_cents'), fft_cents=acc[0].get('fft_cents'), method=acc[0]['method'])
        L = r['license']
        e = dict(key=sid, source=r['source'], title=r['title'] + (f" (note {r['segment']['index'] + 1} of {r['segment']['of']} in the run, {r['segment']['t0']}-{r['segment']['t1']} s)" if r.get('segment') else ''),
                 source_url=r['source_url'], author=r['author'], audio_used=dict(url=r['audio_url'], what=r['what'], sha256=r['sha256'], size_bytes=r['size_bytes']),
                 supplier=r['supplier'], license=L, credit_line=f"\u201c{r['title']}\u201d by {r['author']} ({r['source_url']}), {L['name']}",
                 pitch_measured=pm, roles=sorted({u.get('role', u['layer']) for u in us}), uses=len(us), time_stretch_ratios=sorted({u['time_stretch'] for u in us}), pitch_shift=False, spans=[with_chord_span(u, plan.get('chords') or []) for u in us])
        if r.get('chord'): e['chord'] = dict(symbol=r['chord'].get('symbol'), voicing=r['chord'].get('voicing'), notes=r['chord'].get('notes'))
        sounds.append(e)
    sounds += ir_sounds(log)
    bad = [s['key'] for s in sounds if not LIC.allowed(s['license']['id'])]
    if bad: raise ValueError(f'disallowed licenses in the mix: {bad}')
    fI, fP, fL = loud(flac); mI, mP, mL = loud(mp3)
    cnt = collections.Counter(s['source'] for s in sounds); lic = collections.Counter(s['license']['name'] for s in sounds); authors = sorted({s['author'] for s in sounds})
    comp = []
    if any('preview' in s['audio_used']['what'] for s in sounds): comp.append(dict(what='Freesound sounds are HQ previews (lossy Ogg Vorbis); the originals need OAuth, which the kit does not do.'))
    part = [s['key'] for s in sounds if 'HTTP Range' in s['audio_used']['what'] or 'first ' in s['audio_used']['what']]
    if part: comp.append(dict(what=f'Partial downloads (HTTP Range; hashes cover exactly the fetched bytes): {", ".join(part)}.'))
    for rj in log['rejected']:
        m = rj['measured']; comp.append(dict(what=f"Rejected by measurement: {rj['source']} wanted {rj['expected']}, measured {m.get('note')} {m.get('cents')} c (spread {m.get('spread_cents')}, FFT {m.get('fft_cents')}). Not in the audio."))
    if any(s['license']['id'] == 'PD-equivalent' for s in sounds): comp.append(dict(what='UIowa MIS terms ("freely available ... used for any projects, without restrictions") are not a formal CC0/PD dedication; treated as public-domain equivalent and credited anyway.'))
    rvi = log.get('reverb') or {}
    if rvi.get('fallback_reason'): comp.append(dict(what=f"Reverb: {rvi['engine_requested']} was asked for but not available ({rvi['fallback_reason']}); the synthetic hall ran instead."))
    for n in plan.get('notes', []): comp.append(dict(what=n))
    it = dict(plan.get('interpreter') or {}); it['label'] = interpreter_label(it)
    credit = '; '.join(f"{a} ({', '.join(sorted({s['license']['name'] for s in sounds if s['author'] == a}))})" for a in authors)
    C = plan['composition']
    rec = dict(record_type='netcomposer.performance_record', record_format_version=None, record_format_note=FORMAT_NOTE,
      performance=dict(id='perf_' + plan.get('id', 'untitled'), title=plan['title'], performed_at=now.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), performed_at_local=now.isoformat(timespec='seconds'),
        duration_s=round(dur(flac), 3), music_ends_at_s=plan['length_s'], performer_mode='algorithmic',
        performer=dict(name=performer, kit=dict(name='nc-performer', version=__version__, render_module_sha256=sha256_file(os.path.join(os.path.dirname(__file__), 'render.py')))),
        interpreter=it, plan_ref=dict(path=os.path.basename(plan_path), sha256=sha256_file(plan_path)),
        composition_ref=dict(title=C['title'], composer=dict(display_name=C.get('composer')), words_verbatim=C.get('words_verbatim'), **({'sha256': sha256_file(composition_path)} if composition_path and os.path.exists(composition_path) else {})),
        rule_set=dict(seed=plan['seed'], pulse_bpm=plan.get('pulse_bpm'), sections=plan.get('sections'), layers=[dict(id=l['id'], type=l['type'], bus=l.get('bus'), role=l.get('role')) for l in plan['layers']],
                      pitch_shift=False, transforms_enabled=(['trim', 'fade', 'gain'] if plan.get('assembly') == 'raw' else ['trim', 'gain', 'fade', 'filter', 'reverb', 'delay']),
                      pitch_policy=("Each clip is the recording as published, cut to its slot. Nothing was pitch-shifted or time-stretched." if plan.get('assembly') == 'raw' else f"Every pitched sound chosen by NC measurement (pYIN / FFT), not by label; accepted within {plan.get('pitch_tolerance_cents', 15)} cents."), selections=log['selections']),
        timeline=[dict(chord_index=i, start_s=c['t'], end_s=round(c['t'] + c['dur'], 3), section=c.get('section'), chord=c['symbol'], voicing_midi=c['voicing']) for i, c in enumerate(plan['chords'])],
        line_events=[e for ly in plan['layers'] if ly['type'] == 'notes' for e in ly['events']],
        outputs=[dict(file=os.path.basename(flac), format='FLAC 24-bit/48 kHz stereo', sha256=sha256_file(flac), size_bytes=os.path.getsize(flac), duration_s=round(dur(flac), 3), loudness_lufs_integrated=fI, true_peak_dbtp=fP, lra_lu=fL),
                 dict(file=os.path.basename(mp3), format='MP3 320 kbps, 48 kHz (preview)', sha256=sha256_file(mp3), size_bytes=os.path.getsize(mp3), duration_s=round(dur(mp3), 3), loudness_lufs_integrated=mI, true_peak_dbtp=mP, lra_lu=mL)],
        known_compromises=comp, license_out=dict(id=LIC.OUT['id'], uri=LIC.OUT['uri'])),
      hacks_chain=hacks_chain(plan, log), sounds_used=sounds,
      summary=dict(distinct_sounds=len(sounds), authors=authors, contributors=len(authors), sources=dict(cnt), licenses=dict(lic), total_events=len(log['uses']), **({'source_mix': plan['source_mix']} if plan.get('source_mix') else {})),
      derived_inputs=dict(accepted_input_licenses=['CC0-1.0', 'CC-BY (any version)', 'CC-BY-SA (any version)', 'Public domain (incl. UIowa MIS no-restriction terms)'], present=sorted(lic),
                          check='OK: no NC, ND, Sampling+ or custom-restrictive licenses. CC BY / BY-SA inputs credited; output CC BY-SA 4.0.'),
      attribution=f"\u201c{C['title']}\u201d, composed by {C.get('composer')}. Performed with the NetComposer Performer kit (interpreter: {it['label']}) using sounds by: {credit}. Licensed CC BY-SA 4.0, {LIC.OUT['uri']}",
      provenance_files=dict(plan=dict(path=os.path.basename(plan_path), sha256=sha256_file(plan_path)), uses_log=dict(path='uses.json', sha256=sha256_file(os.path.join(os.path.dirname(out_path), 'uses.json')))),
      created_at=now.isoformat(timespec='seconds'),
      signature=dict(status='UNSIGNED', signed_by=None, algorithm=None, key_id=None, value=None, note='Placeholder. Open decision #33 (signed usage records) is not decided.'), amendments=[])
    if plan.get('assembly_plan'):
        rec['assembly_plan'] = plan['assembly_plan']
    rec['record_hash'] = dict(algorithm='sha256', over='canonical JSON (sorted keys, no whitespace) of the record without record_hash and signature', value=rhash(rec))
    json.dump(rec, open(out_path, 'w'), indent=1, ensure_ascii=False, default=float)
    return rec

def lic_ok(L):
    if 'public domain' in (L.get('name') or '').lower() or L.get('id') in ('PD', 'PD-equivalent', 'PDM-1.0'): return True
    lid = L.get('id') or (LIC.from_url(L.get('uri') or '') or (None,))[0]
    if not lid and L.get('name'): lid = L['name'].replace('CC0 1.0', 'CC0-1.0').replace(' ', '-').replace('CC-BY-SA', 'CC-BY-SA')
    return LIC.allowed(lid)

def verify(path, audio_dir=None, check_files=True, strict_loudness=False):
    """Check a record: hash, license rules, no pitch-shift, credits, interpreter, and (if present) output checksums. Returns (ok, list of lines)."""
    r = json.load(open(path)); out = []; ok = True
    def chk(c, msg):
        nonlocal ok; out.append(('OK   ' if c else 'FAIL ') + msg); ok = ok and c
    chk(r.get('record_type') == 'netcomposer.performance_record', 'record_type is netcomposer.performance_record')
    chk(r.get('record_hash', {}).get('value') == rhash(json.loads(json.dumps(r))), 'record_hash matches the canonical JSON')
    S = r.get('sounds_used', [])
    chk(len(S) > 0, f'{len(S)} sounds listed')
    chk(all(lic_ok(s['license']) for s in S), 'every input license is CC0, CC BY, CC BY-SA or public domain')
    H = [s for s in S if s.get('audio_used')]
    chk(all(s['audio_used'].get('sha256') for s in H), f'every heard input has a sha256 ({len(H)} heard, {len(S) - len(H)} listed without audio, e.g. rhythm donors)')
    au = lambda s: s.get('author') or (s.get('supplier') or {}).get('display_name')
    chk(all(s.get('source_url') and au(s) for s in S), 'every input has a source URL and an author')
    chk(all(not s.get('pitch_shift') for s in S) and r['performance'].get('rule_set', {}).get('pitch_shift') is False, 'no pitch-shifting')
    by = [s for s in S if (s['license'].get('name', '').startswith('CC BY'))]
    chk(all(au(s) in json.dumps(r.get('attribution', ''), ensure_ascii=False) or s.get('credit_line') for s in by), f'{len(by)} CC BY / BY-SA inputs credited')
    chk(r['performance'].get('license_out', {}).get('id') == 'CC-BY-SA-4.0', 'output license CC BY-SA 4.0')
    it = r['performance'].get('interpreter')
    chk(it is None or bool(it.get('label') or it.get('model') or it.get('kind')), 'interpreter noted' + (f": {it.get('label') or it.get('model') or it.get('kind')}" if it else ' (absent: older record)'))
    rh = [h for h in r.get('hacks_chain', []) if h.get('tool') == 'reverb']
    if rh and 'engine' in rh[0]:
        h = rh[0]; chk(h['engine'] in ('dragonfly', 'convolution', 'synthetic') and (h['engine'] == h.get('engine_requested') or bool(h.get('fallback_reason'))),
                       f"reverb engine that ran is recorded: {h['engine']}" + (f" (asked for {h['engine_requested']}: {h['fallback_reason']})" if h.get('fallback_reason') else ''))
    else: out.append('NOTE reverb engine not recorded (record from kit 0.1: synthetic hall)')
    lh = [h for h in r.get('hacks_chain', []) if h.get('tool') == 'loudness']
    if lh and 'mode_actual' in lh[0]: out.append(f"OK   loudness mode that ran: {lh[0]['mode_actual']}")
    for o in r['performance'].get('outputs', []):
        p = os.path.join(audio_dir or os.path.dirname(os.path.abspath(path)), o['file'].split('/')[-1])
        if check_files and os.path.exists(p): chk(sha256_file(p) == o['sha256'], f"output {o['file']} sha256 matches")
        L = o.get('loudness_lufs_integrated'); TP = o.get('true_peak_dbtp')
        if L is not None:
            good = -18.0 <= L <= -16.0 and TP <= -1.0; msg = f"output {o['file'].split('/')[-1]}: {L} LUFS, {TP} dBTP (kit target -16..-18 LUFS, <= -1 dBTP)"
            if strict_loudness or good: chk(good, msg)
            else: out.append('WARN ' + msg)
    return ok, out
