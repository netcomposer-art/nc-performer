"""INTERPRET: plain-language Composition + Performance words -> plan.json.

Three ways in, all producing the same schema (ncperf/schema/plan.schema.json):
  1. LLM: any OpenAI-compatible chat API (env NC_LLM_BASE_URL, NC_LLM_MODEL, NC_LLM_API_KEY).
  2. --plan: a plan a human or an agent wrote. No LLM at all.
  3. --stub: a rule-based draft (chord symbols found in the words, voiced from the full chord set). No LLM.
"""
import datetime, json, os, re, urllib.request, urllib.error
from . import chords as CH
from .schema import SCHEMA, validate

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)

def _data(name):
    """Bundled data (ncperf/data/): library.json (sources + banks the published takes used), example.plan.json (perf03)."""
    return json.load(open(os.path.join(HERE, 'data', name), encoding='utf-8'))

def read_composition(path_or_text, performance=None):
    """Return dict(title, composition, performance, file). Accepts a composition.md (NetComposer layout or loose text)."""
    if path_or_text and os.path.exists(path_or_text): txt = open(path_or_text, encoding='utf-8').read(); f = path_or_text
    else: txt = path_or_text or ''; f = None
    title = (re.search(r'^#\s+(.+)$', txt, re.M) or [None, 'Untitled'])[1].strip()
    def sec(name):
        m = re.search(rf'^##\s+{name}[^\n]*\n(.*?)(?=^##\s|\Z)', txt, re.M | re.S | re.I)
        return m.group(1).strip() if m else None
    comp, perf = sec('Composition'), sec('Performance')
    anchors = sec('Chord anchors')
    if comp is None and anchors and not re.search(r'^\s*>', txt, re.M):
        # the published bar-by-bar layout (e.g. BWV 846): meta line + chord table + prose
        cells = [c.strip() for row in anchors.splitlines() if row.strip().startswith('|') for c in row.strip().strip('|').split('|')]
        pairs = sorted((int(cells[k]), re.sub(r'\s*\(.*?\)', '', cells[k + 1]).strip()) for k in range(0, len(cells) - 1, 2) if cells[k].isdigit() and cells[k + 1])
        meta = ' '.join(re.sub(r'\*\*', '', l) for l in txt.splitlines() if l.startswith('**Meter') or l.startswith('**Tempo'))
        comp = (meta + '. One chord per bar: ' if meta else 'One chord per bar: ') + ' | '.join(c for _, c in pairs)
        perf = perf or sec('Prose') or ''
    if comp is None:
        words = sec("Composer's words") or txt
        words = ' '.join(l.lstrip('> ').strip() for l in words.splitlines() if l.strip().startswith('>')) or words
        m = re.split(r'\bPerformance\s*[-:\u2013\u2014]', words, maxsplit=1)
        comp = re.sub(r'^\s*Composition\s*[-:\u2013\u2014]\s*', '', m[0]).strip()
        perf = perf or (m[1].strip() if len(m) > 1 else '')
    composer = (re.search(r'\*\*Composer[^*]*:\*\*\s*([^·\n]+)', txt) or [None, None])[1]
    return dict(title=title, composition=comp, performance=performance if performance is not None else (perf or ''), file=f, composer=(composer or '').strip() or None)

def _bars(text):
    b = re.search(r'(\d+)\s*bars?', text, re.I); t = re.search(r'(\d+(?:\.\d+)?)\s*BPM', text, re.I); ts = re.search(r'\b(\d)/4\b', text)
    if b and t: return int(b.group(1)), (int(ts.group(1)) if ts else 4) * 60.0 / float(t.group(1))
    return None, None

def _length(text):
    nb, bs = _bars(text)
    if nb: return nb * bs
    m = re.search(r'(\d+(?:\.\d+)?)\s*-?\s*(?:minutes?|mins?|m)\b(?:\s*(\d+)\s*(?:seconds?|secs?|s)\b)?', text, re.I)
    if m: return max(20.0, min(600.0, float(m.group(1)) * 60 + float(m.group(2) or 0)))
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b', text, re.I)
    return max(20.0, min(600.0, float(m.group(1)))) if m else 120.0

# ---------------- stub (rule-based, no LLM) ----------------
def stub(comp, seed=1, length_s=None, plan_id=None):
    words = comp['composition'] + ' ' + comp['performance']
    found = CH.find_full(comp['composition']) or CH.find_full(words); syms = [s for s, _ in found]
    if not syms: raise ValueError('no chord symbols found in the Composition (e.g. "Cm, Am, F"); write a plan by hand or use an LLM')
    L = length_s or _length(words)
    n = len(syms); nb, bs = _bars(words)
    reps = 1 if (nb and nb == n) else max(1, round(L / (n * 9.0))); seq = found * reps; dur = L / len(seq)
    prev = None; chords = []; sources = {}; mm = []
    for i, (s, bass) in enumerate(seq):
        path, notes = CH.pick(s, prev, bass=bass); prev = notes
        sid = 'mm-' + path.split('/')[1]
        sources[sid] = dict(kind='supplier', manifest=CH.manifest_url(path), pitched=False)
        chords.append(dict(t=round(i * dur, 3), dur=round(dur, 3), symbol=s, voicing=notes, section='A'))
        mm.append(dict(t=round(i * dur + 0.3, 3), source=sid, offset_s=0.0, dur_s=4.3, fade_in_s=0.02, fade_out_s=2.0, gain=0.8, role='chord strike (full chord set)'))
        mm.append(dict(t=round(i * dur + dur / 2 + 0.3, 3), source=sid, offset_s=0.0, dur_s=4.3, fade_in_s=0.02, fade_out_s=2.0, gain=0.45, role='chord restrike (full chord set)'))
    pad = [dict(t=max(0.0, round(c['t'] - 1.5, 3)) if i else 0.0, source=mm[2 * i]['source'], offset_s=0.0, src_window_s=[0.05, 3.6], stretch_to_s=round(c['dur'] + 4.0, 3),
                fade_in_s=3.0 if i else 4.5, fade_out_s=4.0, gain=0.6, role='stretched chord pad (time-stretch, no pitch-shift)') for i, c in enumerate(chords)]
    lo = min(min(c['voicing']) for c in chords)
    lib = _data('library.json')
    bass = [dict(chord=i) for i in range(len(chords))]
    roots = {i: [m for m in [CH.PC.get(c['symbol'][0], 0) + 36 + (1 if c['symbol'][1:2] == '#' else -1 if c['symbol'][1:2] == 'b' else 0)]] for i, c in enumerate(chords)}
    under = {str(i): [r for r in rs if str(r) in lib['banks'].get('cello', {}).get('notes', {})] for i, rs in roots.items()}
    under = {k: v for k, v in under.items() if v}
    used = {sid for v in lib['banks'].get('cello', {}).get('notes', {}).values() for sid in v} if under else set()
    for sid in used: sources[sid] = lib['sources'][sid]
    layers = [dict(id='pad', type='clips', bus='pad', role='stretched chord pad', events=pad), dict(id='keys', type='clips', bus='keys', role='chord strikes', events=mm)]
    banks = {}
    if under:
        banks['cello'] = lib['banks']['cello']; layers.append(dict(id='cello', type='chord_under', bus='low', bank='cello', notes_by_chord=under, gain=0.35, lead_s=1.0, role='cello root (where a measured cello note exists)'))
    soft = bool(re.search(r'ambient|soft|calm|slow|gentle|dark|no harsh', words, re.I))
    plan = dict(nc_plan_version='0.1', id=plan_id or re.sub(r'[^a-z0-9]+', '-', comp['title'].lower()).strip('-')[:40] or 'untitled', title=comp['title'] + ' (stub performance)',
        composition=dict(title=comp['title'], composer=comp.get('composer'), words_verbatim=dict(composition=comp['composition'], performance=comp['performance']), **({'file': os.path.basename(comp['file'])} if comp.get('file') else {})),
        interpreter=dict(kind='stub', at=datetime.datetime.now().astimezone().isoformat(timespec='seconds'), note='Rule-based draft: chord symbols found in the words, each voiced from the full chord set by voice leading, chord strikes plus a time-stretched pad. Performance words only set the tone (soft/bright). Edit this plan or use an LLM for more.'),
        seed=seed, length_s=round(L, 3), tail_s=4.0, pulse_bpm=60, pitch_tolerance_cents=15,
        sections=[dict(id='A', start_s=0, end_s=round(L, 3), harmony=' '.join(syms))], chords=chords, sources=sources, banks=banks, layers=layers,
        master=dict(buses=dict(pad=dict(filters=[dict(type='lp', hz=2500 if soft else 6000, order=2)], lufs=-21.0, pan=0.05), keys=dict(filters=[dict(type='lp', hz=3000 if soft else 8000, order=2)], lufs=-23.0, pan=-0.08),
                                low=dict(filters=[dict(type='hp', hz=40, order=2), dict(type='lp', hz=400, order=2)], lufs=-27.0)),
                    reverb=dict(engine='dragonfly', send_lu=5.3 if soft else 1.7, params=dict(decay_s=6.2 if soft else 2.8)), hp_hz=30, high_shelf=dict(hz=9000, db=-4.4 if soft else -0.9), loudness=dict(lufs=-17, tp=-1.5)),
        notes=['Stub plan from `nc interpret --stub`: no melody line and no triplets were read from the words; add a "notes" layer by hand or use an LLM interpreter.'])
    return plan

# ---------------- LLM (any OpenAI-compatible chat API) ----------------
SYSTEM = """You are the INTERPRETER of a NetComposer Performer. You turn a Composition (the composer's words) and a Performance brief into ONE plan.json object that follows the JSON Schema below. A deterministic renderer plays the plan; it will not improvise.
Rules you must keep:
- Keep the composer's chords, line and form; choose voicings, orchestration, pacing, hacks and seed yourself.
- Voicings are MIDI note lists (60 = C4). Prefer voicings that exist in the full chord set (given) so the chord strikes can come from it.
- Sounds: use only ids from the LIBRARY below, or add supplier manifests (kind "supplier") from the full chord set, or Freesound ids (kind "freesound") you are sure are CC0/CC BY/CC BY-SA. Licenses are checked live; anything else is rejected.
- Never ask for pitch-shifting; there is no such field. Time-stretch is allowed.
- Reverb: master.reverb.engine "dragonfly" (default) with send_lu (wet loudness vs dry, LU; about 5 for ambient, 1-2 for drier) and optional params (decay_s etc.). "convolution" only with an ir id from the kit's list.
- Loudness: master.loudness.lufs -17, tp -1.5.
- Reply with the JSON object only."""

def _post(base, key, body, timeout=180):
    req = urllib.request.Request(base.rstrip('/') + '/chat/completions', data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', **({'Authorization': f'Bearer {key}'} if key else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read())

def _json_from(text):
    text = text.strip()
    m = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.S)
    if m: text = m.group(1)
    i, j = text.find('{'), text.rfind('}')
    return json.loads(text[i:j + 1])

def llm(comp, seed=1, base_url=None, model=None, api_key=None, example=None, repair_rounds=2, log=print):
    base = base_url or os.environ.get('NC_LLM_BASE_URL') or os.environ.get('OPENAI_BASE_URL')
    model = model or os.environ.get('NC_LLM_MODEL')
    key = api_key if api_key is not None else os.environ.get('NC_LLM_API_KEY', os.environ.get('OPENAI_API_KEY'))
    if not base or not model: raise SystemExit('LLM interpreter needs NC_LLM_BASE_URL and NC_LLM_MODEL (and usually NC_LLM_API_KEY). Or use --plan FILE or --stub.')
    lib = _data('library.json')
    example = example or _data('example.plan.json')
    draft = None
    try: draft = stub(comp, seed=seed)
    except ValueError: pass
    user = '\n\n'.join([
        'JSON SCHEMA (plan.json):\n' + json.dumps(SCHEMA, separators=(',', ':')),
        'LIBRARY (sources and banks you may use; each bank note lists candidates, the renderer keeps the first that measures right):\n' + json.dumps(dict(sources=lib['sources'], banks=lib['banks']), separators=(',', ':')),
        'FULL CHORD SET symbols available (35 qualities x 12 roots; manifest https://musicmatter.art/sounds/fullchords-v4/<ROOT>/<file>.nc.json):\n' + ' '.join(CH.CAT['chords'].keys()),
        'EXAMPLE PLAN (a finished performance of another composition):\n' + json.dumps(example, separators=(',', ':')),
        *(['RULE-BASED DRAFT for this composition (chords found and voiced; improve it):\n' + json.dumps(draft, separators=(',', ':'))] if draft else []),
        f"COMPOSITION title: {comp['title']}\nComposer: {comp.get('composer') or 'unknown'}\nComposition (verbatim): {comp['composition']}\nPerformance (verbatim): {comp['performance']}\nUse seed {seed}."])
    msgs = [dict(role='system', content=SYSTEM), dict(role='user', content=user)]
    for rnd in range(repair_rounds + 1):
        body = dict(model=model, messages=msgs, temperature=0.2, response_format={'type': 'json_object'})
        try: r = _post(base, key, body)
        except urllib.error.HTTPError as e:
            if e.code in (400, 422): body.pop('response_format'); r = _post(base, key, body)  # server without JSON mode
            else: raise
        text = r['choices'][0]['message']['content']
        try: plan = _json_from(text); errs = validate(plan)
        except Exception as e: plan, errs = None, [f'not JSON: {e}']
        if not errs: break
        log(f'  interpreter round {rnd + 1}: {len(errs)} schema problems; asking for a fix')
        msgs += [dict(role='assistant', content=text), dict(role='user', content='The plan does not validate:\n' + '\n'.join(errs[:40]) + '\nReturn the corrected full JSON object only.')]
    else:
        raise SystemExit('LLM plan still invalid after repair rounds:\n' + '\n'.join(errs[:40]))
    plan['interpreter'] = dict(kind='llm', model=r.get('model') or model, requested_model=model, base_url=re.sub(r'//[^/@]*@', '//', base),
                               at=datetime.datetime.now().astimezone().isoformat(timespec='seconds'), note='plan.json written by an LLM from the verbatim words; rendered deterministically.')
    plan.setdefault('composition', {}).setdefault('words_verbatim', dict(composition=comp['composition'], performance=comp['performance']))
    return plan

def from_plan(path):
    plan = json.load(open(path, encoding='utf-8'))
    errs = validate(plan)
    if errs: raise SystemExit(f'{path} does not validate:\n' + '\n'.join(errs[:40]))
    it = plan.get('interpreter') or {}
    if it.get('kind') not in ('llm', 'stub'): plan['interpreter'] = dict(it, kind='human/agent-written plan')
    return plan
