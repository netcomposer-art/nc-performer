"""plan.json validation: jsonschema if installed, otherwise a small built-in checker (types, required, enums, ranges, refs), plus cross-reference checks."""
import json, os
SCHEMA = json.load(open(os.path.join(os.path.dirname(__file__), 'plan.schema.json')))
_T = dict(object=dict, array=list, string=str, integer=int, number=(int, float), boolean=bool, null=type(None))

def _check(v, s, path, errs, root):
    if '$ref' in s: s = root['$defs'][s['$ref'].split('/')[-1]]
    t = s.get('type')
    if t:
        ts = t if isinstance(t, list) else [t]
        if not any(isinstance(v, _T[x]) and not (x in ('integer', 'number') and isinstance(v, bool)) for x in ts): errs.append(f'{path}: expected {t}'); return
    if 'enum' in s and v not in s['enum']: errs.append(f'{path}: {v!r} not in {s["enum"]}')
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if 'minimum' in s and v < s['minimum']: errs.append(f'{path}: {v} < {s["minimum"]}')
        if 'maximum' in s and v > s['maximum']: errs.append(f'{path}: {v} > {s["maximum"]}')
    if isinstance(v, dict):
        for k in s.get('required', []):
            if k not in v: errs.append(f'{path}: missing {k}')
        for k, x in v.items():
            if k in s.get('properties', {}): _check(x, s['properties'][k], f'{path}.{k}', errs, root)
            elif isinstance(s.get('additionalProperties'), dict): _check(x, s['additionalProperties'], f'{path}.{k}', errs, root)
    if isinstance(v, list):
        if 'minItems' in s and len(v) < s['minItems']: errs.append(f'{path}: fewer than {s["minItems"]} items')
        if 'maxItems' in s and len(v) > s['maxItems']: errs.append(f'{path}: more than {s["maxItems"]} items')
        if 'items' in s:
            for i, x in enumerate(v): _check(x, s['items'], f'{path}[{i}]', errs, root)

def validate(plan):
    errs = []
    try:
        import jsonschema
        errs += [f"{'.'.join(map(str, e.path)) or '$'}: {e.message}" for e in jsonschema.Draft202012Validator(SCHEMA).iter_errors(plan)]
    except ImportError:
        _check(plan, SCHEMA, '$', errs, SCHEMA)
    if errs: return errs
    S, B = plan['sources'], plan['banks']; n = len(plan['chords'])
    for b, v in B.items():
        for m, ids in v['notes'].items():
            for i in ids:
                if i not in S: errs.append(f'banks.{b}.{m}: unknown source {i}')
    for ly in plan['layers']:
        for k in ['bank'] + ['fallback_banks']:
            for b in ([ly[k]] if isinstance(ly.get(k), str) else ly.get(k, [])):
                if b not in B: errs.append(f"layers.{ly['id']}: unknown bank {b}")
        for pc in ly.get('per_chord', []):
            if pc['chord'] >= n: errs.append(f"layers.{ly['id']}: chord {pc['chord']} out of range")
        for e in ly.get('events', []):
            if 'source' in e and e['source'] not in S: errs.append(f"layers.{ly['id']}: unknown source {e['source']}")
            for tb in e.get('timbres', []):
                if tb['bank'] not in B: errs.append(f"layers.{ly['id']}: unknown bank {tb['bank']}")
        if ly['type'] in ('chord_notes', 'chord_pad', 'chord_under', 'drone') and 'bank' not in ly: errs.append(f"layers.{ly['id']}: needs a bank")
    for s, v in S.items():
        if v['kind'] == 'url' and 'license' not in v: errs.append(f'sources.{s}: kind=url needs a declared license')
        if v['kind'] == 'supplier' and 'manifest' not in v: errs.append(f'sources.{s}: kind=supplier needs a manifest URL')
        if v['kind'] == 'freesound' and 'id' not in v: errs.append(f'sources.{s}: kind=freesound needs an id')
        if v['kind'] == 'uiowa_mis' and 'url' not in v: errs.append(f'sources.{s}: kind=uiowa_mis needs a url')
    return errs
