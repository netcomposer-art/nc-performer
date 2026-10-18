# nc-performer: the NetComposer Performer kit

Turn a NetComposer **Composition** (the composer's words) and a **Performance** brief into a rendered
Performance and its **performance record**, from openly licensed sound only.

```
words ──INTERPRET──▶ plan.json ──RENDER──▶ performance.flac + _preview.mp3 + .record.json
         (LLM, or a plan you         (deterministic: fetch, check licenses, measure pitch,
          or your agent wrote)        select by measurement, hacks, mix, master to -17 LUFS)
```

The rules are those of netcomposer.art and the kit enforces them:

- Inputs: **CC0, CC BY, CC BY-SA or public domain**. NC, ND, Sampling+ and custom licenses are rejected.
  Licenses are read **live** at render time (Freesound: the sound's own page; Suppliers: the manifest;
  UIowa MIS: the terms page).
- **No pitch-shifting.** There is no field for it. Time-stretch is allowed (Rubber Band, duration changes, pitch does not).
- Pitch is **measured** (pYIN, FFT for bells), never taken from a label. A bank lists candidates per note;
  the first one that measures as the note within the tolerance (default 15 cents) is used, and every
  rejection is written into the record.
- Output: **CC BY-SA 4.0**, -17 LUFS integrated (accepted -16..-18), true peak <= -1 dBTP, no clipping.
- Every Performance gets a full record: each source with URL, license, author, sha256 and uses; every
  setting and the seed; the interpreter; the outputs with sha256 and loudness; a `record_hash`.

The transforms are called **hacks**: time-stretch, decay flatten, level match, fades, filters, brighten
automation, ping-pong delay, reverb (Dragonfly Hall by default), high-pass, high shelf, loudness.

## Licenses

| Part | License |
|---|---|
| Code (`ncperf/`, `nc`, `tests/`) | **MIT** ([LICENSE](LICENSE)), Copyright (c) 2026 Peter Semmelhack |
| Docs, schema, skill, example plans and records | **CC BY-SA 4.0** ([LICENSE-docs.md](LICENSE-docs.md)) |
| Rendered Performances | CC BY-SA 4.0 (required by the input licenses and the project) |

Optional runtime dependencies, **not bundled**: [Dragonfly Reverb](https://github.com/michaelwillis/dragonfly-reverb)
(GPL-3.0) and [pedalboard](https://github.com/spotify/pedalboard) (GPL-3.0), used only to run the default reverb hack.
The kit calls them as separate installed tools; its own code stays MIT. Without them the kit still runs
(the reverb falls back to the synthetic hall and the record says so).

## Quickstart

Needs Python 3.10+ and `ffmpeg` with the `rubberband` filter (check: `ffmpeg -hide_banner -filters | grep rubberband`;
on Debian/Ubuntu `apt install ffmpeg`, on macOS `brew install ffmpeg rubberband` or a build with librubberband).

Recommended for the best sound (each is optional; the record says what actually ran):

| What | Install | Used for |
|---|---|---|
| Dragonfly Hall (VST3) | Debian/Ubuntu `apt install dragonfly-reverb`; macOS/Windows: the VST3 from the [releases](https://github.com/michaelwillis/dragonfly-reverb/releases) into the standard VST3 folder | default reverb hack |
| pedalboard | `pip install "nc-performer[reverb] @ git+https://github.com/netcomposer-art/nc-performer"` (or `pip install pedalboard`) | runs Dragonfly headless, offline |
| Rubber Band CLI 3.x | `apt install rubberband-cli` / `brew install rubberband` | R3 ("finer") time-stretch |

Dragonfly is looked up in `master.reverb.vst3_path`, then `NC_DRAGONFLY_VST3`, then the standard VST3 folders.

```
pip install git+https://github.com/netcomposer-art/nc-performer
nc --help

# a Composition, a rule-based plan (no LLM), a render with its record
nc compose -t "Glass garden" --composer "Your Name" -c "1 min. Ebsus2, Cm-add9, Ab6/9, Bb7sus4, Eb6." -p "clear and open" -o composition.md
nc perform composition.md --stub -o out/
nc verify-record out/glass-garden.record.json --audio-dir out/
```

Or from a clone, without installing: `git clone https://github.com/netcomposer-art/nc-performer && cd nc-performer &&
pip install numpy scipy soundfile librosa pyloudnorm && ./nc --help`. Re-render take 3 of the ambient Cm piece:
`./nc render --plan examples/ambient-cm/perf03.plan.json -o out/` (about 85 MB of downloads, a few minutes).

Downloads are cached in `~/.cache/nc_kit` (set `NC_CACHE` to move it).

## Commands

```
nc compose -t "Title" --composer "Name" -c "2 mins - Cm, Am, F" -p "ambient, slow" -o composition.md
nc interpret composition.md -o plan.json                 # LLM (env below)
nc interpret composition.md --stub -o plan.json          # rule-based draft, no LLM
nc interpret --plan my.plan.json -o plan.json            # validate a hand/agent-written plan
nc render --plan plan.json -o out/                       # deterministic render + record + verify
nc perform composition.md --plan my.plan.json -o out/    # interpret + render
nc verify-record out/<name>.record.json --audio-dir out/
```

### The interpreter is LLM-agnostic

Any OpenAI-compatible chat completions API works (hosted or local: OpenAI, xAI, OpenRouter, Together,
vLLM, llama.cpp server, Ollama's `/v1`, ...):

| env | meaning |
|---|---|
| `NC_LLM_BASE_URL` | e.g. `https://api.example.com/v1` (the kit POSTs to `<base>/chat/completions`) |
| `NC_LLM_MODEL` | model name; it is written into the plan and the record |
| `NC_LLM_API_KEY` | bearer key, if the server needs one |

`OPENAI_BASE_URL` / `OPENAI_API_KEY` are read as fallbacks. The prompt holds the plan schema, the source
library, the full chord set symbols, the perf03 plan as an example and a rule-based draft (about 53k
characters). The answer is validated against the schema; up to two repair rounds send the errors back.

**No LLM at all:** write `plan.json` yourself (or let your agent write it) and use `--plan`. The record
then says `interpreter: human/agent-written plan`. `--stub` gives a starting point: it finds the chord
symbols in the words, voices each one from the full chord set by voice leading, and plays chord strikes
plus a time-stretched pad (no melody or triplets; add a `notes` layer).

## plan.json

Schema: [`ncperf/schema/plan.schema.json`](ncperf/schema/plan.schema.json) (JSON Schema 2020-12; the kit
validates without the `jsonschema` package, and uses it when installed). Examples:

- [`examples/ambient-cm/perf02.plan.json`](examples/ambient-cm/perf02.plan.json): take 2 (open-commons palette, seed 1018)
- [`examples/ambient-cm/perf03.plan.json`](examples/ambient-cm/perf03.plan.json): take 3 (rolled piano and forest air, seed 2026)
- [`ncperf/data/library.json`](ncperf/data/library.json): every source and bank the two takes used (the interpreter's sound library)
- [`examples/glass-garden.create-stub.plan.json`](examples/glass-garden.create-stub.plan.json): a plan stub made on https://netcomposer.art/create/

A plan holds: `sections`; `chords` (time, length, symbol, voicing as MIDI notes); `sources` (by id:
`supplier` manifest, `freesound` id, `uiowa_mis` file, or `url` with a declared license); `banks`
(instrument -> note -> candidate source ids); `layers` (`chord_notes`, `chord_pad`, `chord_under`, `drone`,
`clips`, `notes` for the line and triplets); `master` (buses with the hacks, reverb, loudness); `quality`
(time-stretch and resampling engines); `seed`; `notes`.

## The master hacks (kit 0.2)

**Reverb** (`master.reverb`). `engine`:

- `dragonfly` (default): Dragonfly Hall Reverb, run headless and offline through pedalboard. Bit-identical
  from run to run. Kit defaults (tuned on the ambient Cm piece; any of them can be set in `params`):
  size 48 m, width 120, predelay 24 ms, diffuse 85, decay 6.2 s, early level 14 / late 100 / early send 25,
  low cut 60 Hz, low cross 400 Hz x1.15, high cut 9 kHz, high cross 3.8 kHz x0.45, spin 1.2 Hz, wander 22 ms,
  modulation 40, dry 0. Return EQ: high-pass 120 Hz, low-pass 14 kHz, a gentle linear-phase octave tilt.
  Measured on the wet: T30 about 6.3 s at 500 Hz, 4.0 s at 4 kHz, 2.9 s at 8 kHz.
- `convolution`: a measured space by `ir` id from [`ncperf/data/irs.json`](ncperf/data/irs.json)
  (St Paul's Cathedral nave or choir, OpenAIR, University of York, CC BY 4.0). No audio ships with the
  kit; the file is downloaded and pinned by sha256. B-format is decoded to a true-stereo 2x2 matrix
  (virtual hypercardioids at +/-60 deg, dual-band decode), direct sound removed, predelay added, optional
  decay reshape. The impulse response is credited in the record like any other sound.
- `synthetic`: the kit 0.1 hall from seeded filtered noise.

If `dragonfly` is asked for and Dragonfly or pedalboard is missing, the render prints a warning, uses
`synthetic`, and the record's reverb hack says `engine: synthetic`, `engine_requested: dragonfly` and why.

The wet level is a **loudness send**: integrated loudness of the wet return = dry + `send_lu` (default 5.3 LU,
the ambient Cm takes). A legacy `wet` maps to `send_lu = 5.3 + 20*log10(wet/0.38)`; a legacy `rt60_s` sets the
Dragonfly decay when `params.decay_s` is not given.

The record's reverb hack lists the engine that ran, the tool and version, the host and version, every
parameter, the return EQ, the send, the measured wet T30 and EDT per octave band, and sha256 hashes of the
wet return and of its impulse response (so a re-render can be checked bit for bit).

**Tone** (`master.tone_filter`, default `shelf`): a high shelf (`master.high_shelf: {hz, db}`) replaces the
kit 0.1 low-pass blend. A legacy `lp_blend {hz, mix}` becomes a shelf at `hz` with `20*log10(1-mix)` dB.

**Loudness** (`master.loudness`, default `method: linear_tp_limiter`): linear gain to `lufs`, then a
4x-oversampled, stereo-linked lookahead true-peak limiter at `tp` (3 ms lookahead, 150 ms release); it only
acts where a peak would pass the ceiling. The record gives `mode_actual` (`linear` or `linear+tp_limiter`
with the largest gain reduction). `method: loudnorm` is the kit 0.1 ffmpeg two-pass; ffmpeg can silently
switch it to dynamic (AGC) mode, and the record then says `mode_actual: dynamic`.

**Quality** (`quality`): `stretch_engine` `auto` (default: Rubber Band CLI R3 `--fine` when installed, else
the ffmpeg rubberband filter, R2) and `resampler` `soxr` (default, precision 28; ffmpeg's `swr` if soxr is
missing). Pitch never changes.

To reproduce a kit 0.1 render: `reverb: {engine: synthetic, send_rule: legacy_peak, ...}`,
`tone_filter: lp_blend`, `loudness.method: loudnorm`, `quality: {stretch_engine: ffmpeg-rubberband, resampler: swr}`.

## Sources

1. **Registered Suppliers** first: [`suppliers.json`](https://netcomposer.art/suppliers.json) -> each
   Supplier's `netcomposer.json` -> one manifest per sound with license, sha256 and audio URL. The kit
   checks that the manifest belongs to an active Supplier's index and that the file's sha256 matches.
   `ncperf/data/fullchords_catalog.json` indexes musicmatter.art's full chord set (420 symbols, 2256
   recorded voicings, CC BY-SA 4.0).
2. **The open commons.** Freesound: the license is read from the sound's live page (exactly one CC
   license link must be there). **Freesound originals need OAuth2, so the kit uses the public HQ
   previews (Ogg Vorbis, lossy)** and says so in the record. Long field recordings can be fetched in
   part (`range_bytes`); the sha256 then covers exactly the bytes used. UIowa MIS: the terms ("freely
   available ... used for any projects, without restrictions") are checked on the live page and treated
   as public-domain equivalent, credited anyway.

## Output

`<name>.flac` (24-bit/48 kHz), `<name>_preview.mp3` (320 kbps), `<name>.record.json`, `uses.json`
(every placement, measurement and rejection), `plan.json` (the exact plan rendered), `stems/`.
Same plan + same sources + same seed = same audio.

## Submitting a Performance

Submissions go through issues on the site repository (or a pull request adding an entry to
suppliers.json). This channel is proposed and may change. For a Performance: propose it (sources,
rules, settings) as an issue, or publish your record in the same format and link it there:
https://github.com/netcomposer-art/netcomposer.art/issues

## For agents

[`skill/SKILL.md`](skill/SKILL.md) (and a plain-text [`skill/SKILL.txt`](skill/SKILL.txt)) teaches any
agent to read a Composition, write `plan.json`, run the kit or do the steps by hand, keep the license
rules and submit.

## Tests

`python3 tests/test_kit.py`: schema, chord parsing, the stub, the LLM path against a local mock
OpenAI-compatible server (`tests/mock_llm.py`, including a repair round), BWV 846 parsing, `verify-record` on the
two published ambient-cm records in `examples/ambient-cm/records/`, and the kit 0.2 hacks: the reverb send rule,
Dragonfly determinism and decay (skipped if Dragonfly is not installed), the fallback, the IR list, the
true-peak limiter, the high shelf, time-stretch without pitch change, and soxr resampling. No network needed. `tests/check_render.py <out_dir>` checks a render: loudness, true peak, clipping and
the chords (chroma of the harmony stems against each chord's voicing).

---
Peter Semmelhack · https://netcomposer.art · https://github.com/netcomposer-art/nc-performer · code MIT · docs CC BY-SA 4.0
