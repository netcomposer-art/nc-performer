"""nc: the NetComposer Performer kit.

  nc compose        write a composition.md from plain words
  nc interpret      composition.md -> plan.json   (LLM via env, --plan FILE, or --stub)
  nc render         plan.json -> FLAC + MP3 + record (deterministic)
  nc perform        interpret + render in one go
  nc verify-record  check a performance record
  nc submit         send a record to a Perform instance (any host)
"""
import argparse, json, os, sys, datetime
from . import __version__

def _comp_md(title, composer, comp, perf, minutes=None):
    now = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    return f"""# {title}

**Composer of this Composition:** {composer} · **License out:** CC BY-SA 4.0
**Allowed transforms:** trim, gain, fade, filter, reverb, delay, time_stretch (no pitch-shift)

## Composition

{comp.strip()}

## Performance

{perf.strip()}

## Material rules

- Inputs: CC0, CC BY, CC BY-SA or public domain only.
- No pitch-shifting. Time-stretch is fine.
- Output license: CC BY-SA 4.0.

## Credits

- **Composition:** {composer} ({now[:10]}). CC BY-SA 4.0.
"""

def cmd_compose(a):
    comp = a.composition or input('Composition (chords, form, length): ')
    perf = a.performance if a.performance is not None else input('Performance (how it should sound): ')
    md = _comp_md(a.title, a.composer, comp, perf)
    open(a.out, 'w', encoding='utf-8').write(md); print(f'wrote {a.out}')

def _interpret(a):
    from . import interpret as I
    if a.plan:
        plan = I.from_plan(a.plan); print(f"plan {a.plan}: valid; interpreter: {plan['interpreter']['kind']}"); return plan
    comp = I.read_composition(a.composition, a.performance)
    if a.stub: plan = I.stub(comp, seed=a.seed)
    else: plan = I.llm(comp, seed=a.seed, base_url=a.base_url, model=a.model)
    return plan

def cmd_interpret(a):
    plan = _interpret(a)
    from .schema import validate
    errs = validate(plan)
    if errs: sys.exit('plan does not validate:\n' + '\n'.join(errs))
    json.dump(plan, open(a.out, 'w', encoding='utf-8'), indent=1, ensure_ascii=False); print(f"wrote {a.out} ({len(plan['chords'])} chords, {len(plan['layers'])} layers, interpreter {plan['interpreter'].get('model') or plan['interpreter']['kind']})")

def _render(plan, plan_path, out, name, composition=None, performer=None):
    from .render import Renderer
    from . import record as R
    os.makedirs(out, exist_ok=True)
    fl, mp3, log = Renderer(plan, out).run(name)
    rp = os.path.join(out, name + '.record.json')
    R.build(plan, plan_path, log, fl, mp3, rp, performer=performer or os.environ.get('NC_PERFORMER', 'nc-performer kit'), composition_path=composition)
    ok, lines = R.verify(rp, audio_dir=out)
    print('\n'.join(lines)); print(f"\n{'OK' if ok else 'PROBLEMS'}: {fl}\n    {mp3}\n    {rp}")
    return ok

def cmd_render(a):
    from . import interpret as I
    plan = I.from_plan(a.plan)
    if a.seed is not None: plan['seed'] = a.seed
    pp = os.path.join(a.out, 'plan.json'); os.makedirs(a.out, exist_ok=True)
    json.dump(plan, open(pp, 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    sys.exit(0 if _render(plan, pp, a.out, a.name or plan.get('id', 'performance'), a.composition_file, a.performer) else 1)

def cmd_perform(a):
    plan = _interpret(a); os.makedirs(a.out, exist_ok=True); pp = os.path.join(a.out, 'plan.json')
    json.dump(plan, open(pp, 'w', encoding='utf-8'), indent=1, ensure_ascii=False); print(f'wrote {pp}')
    cf = a.composition if a.composition and os.path.exists(a.composition) else None
    sys.exit(0 if _render(plan, pp, a.out, a.name or plan.get('id', 'performance'), cf, a.performer) else 1)

def cmd_submit(a):
    api = a.api or os.environ.get('NC_PERFORM_API')
    if not api:
        sys.exit('nc submit needs --api or NC_PERFORM_API (the Perform instance). There is no default host.')
    from . import record as R
    ok, lines = R.verify(a.record, audio_dir=a.audio_dir, check_files=not a.no_files)
    print('\n'.join(lines))
    if not ok:
        sys.exit('FAILED locally; not submitted')
    from .submit import submit
    out = submit(a.record, a.audio_dir, api, a.plan)
    print(json.dumps(out, indent=1))

def cmd_verify(a):
    from . import record as R
    ok, lines = R.verify(a.record, audio_dir=a.audio_dir, check_files=not a.no_files, strict_loudness=a.strict_loudness)
    print('\n'.join(lines)); print('OK' if ok else 'FAILED'); sys.exit(0 if ok else 1)

def main(argv=None):
    p = argparse.ArgumentParser(prog='nc', description='NetComposer Performer kit: Composition words -> plan.json -> rendered Performance + record. No pitch-shifting; inputs CC0 / CC BY / CC BY-SA / public domain; output CC BY-SA 4.0.')
    p.add_argument('--version', action='version', version=f'nc-performer {__version__}')
    s = p.add_subparsers(dest='cmd', required=True)
    c = s.add_parser('compose', help='write a composition.md'); c.add_argument('-t', '--title', required=True); c.add_argument('--composer', default='anonymous')
    c.add_argument('-c', '--composition'); c.add_argument('-p', '--performance'); c.add_argument('-o', '--out', default='composition.md'); c.set_defaults(f=cmd_compose)
    def interp_args(x):
        x.add_argument('composition', nargs='?', help='composition.md (or the words in quotes)')
        x.add_argument('-p', '--performance', help='Performance words (overrides the file)')
        g = x.add_mutually_exclusive_group(); g.add_argument('--plan', help='use this plan.json (hand- or agent-written); no LLM'); g.add_argument('--stub', action='store_true', help='rule-based draft; no LLM')
        x.add_argument('--model', help='overrides NC_LLM_MODEL'); x.add_argument('--base-url', help='overrides NC_LLM_BASE_URL'); x.add_argument('--seed', type=int, default=1)
    i = s.add_parser('interpret', help='composition -> plan.json'); interp_args(i); i.add_argument('-o', '--out', default='plan.json'); i.set_defaults(f=cmd_interpret)
    r = s.add_parser('render', help='plan.json -> audio + record'); r.add_argument('--plan', required=True); r.add_argument('-o', '--out', default='out'); r.add_argument('--name')
    r.add_argument('--seed', type=int, help='override the plan seed'); r.add_argument('--composition-file', help='composition.md to hash into the record'); r.add_argument('--performer'); r.set_defaults(f=cmd_render)
    f = s.add_parser('perform', help='interpret + render'); interp_args(f); f.add_argument('-o', '--out', default='out'); f.add_argument('--name'); f.add_argument('--performer'); f.set_defaults(f=cmd_perform)
    v = s.add_parser('verify-record', help='check a record'); v.add_argument('record'); v.add_argument('--audio-dir'); v.add_argument('--no-files', action='store_true', help='skip output file checks')
    v.add_argument('--strict-loudness', action='store_true', help='fail (not warn) outside -16..-18 LUFS / -1 dBTP'); v.set_defaults(f=cmd_verify)
    u = s.add_parser('submit', help='send a verified record to a Perform instance')
    u.add_argument('record'); u.add_argument('--audio-dir'); u.add_argument('--plan', help='plan.json (default: <audio-dir>/plan.json)')
    u.add_argument('--api', help='Perform base URL (or set NC_PERFORM_API). No default host.')
    u.add_argument('--no-files', action='store_true'); u.set_defaults(f=cmd_submit)
    a = p.parse_args(argv)
    if a.cmd == 'interpret' and not (a.plan or a.composition): p.error('give a composition.md, or --plan FILE')
    a.f(a)

if __name__ == '__main__': main()
