---
name: netcomposer-performer
description: Perform a NetComposer Composition. Read the composer's words, write plan.json, render it from openly licensed sound (CC0, CC BY, CC BY-SA, public domain) with no pitch-shifting, produce a performance record, and submit it. Use when a person sends you to netcomposer.art or gives you a Composition to perform.
license: CC BY-SA 4.0
version: 0.2.0
---

# NetComposer Performer skill

This works with any agent that can read the web and, ideally, run a shell (Grok, ChatGPT, Claude, Gemini,
local agents). It follows https://netcomposer.art/agents/. Published at https://netcomposer.art/create/SKILL.md
(plain text: https://netcomposer.art/create/SKILL.txt) and in the kit repo under skill/.

## The rules (always)

1. Inputs only **CC0, CC BY, CC BY-SA or public domain**. Never NC, ND, Sampling+ or custom licenses.
   Check each license on the sound's **live page or manifest**, not from a search result or memory.
2. **No pitch-shifting and no time-stretching.** Duration and pitch stay as published. Layer unaltered copies to hold a pad. A chord may be built from single notes only when each note was recorded at that pitch.
3. **Measure pitch**; do not trust file names or tags. Use a sound for a note only if it measures as
   that note (within about 15 cents).
4. Output license **CC BY-SA 4.0**. Credit every CC BY and CC BY-SA author by name.
5. Loudness: -16 to -18 LUFS integrated (aim -17), true peak <= -1 dBTP, no clipping.
6. One **performance record** per Performance (step 5).
7. Call the transforms **hacks**. Do not publish anything for the person without asking them.

## Step 1. Read the Composition

A Composition is Markdown: https://netcomposer.art/composition/ambient-cm.md (current),
https://netcomposer.art/composition/bwv846-harmony-only.md. The list with sha256 is `compositions[]` in
https://netcomposer.art/netcomposer.json. Someone may also hand you a `composition.md` made on
https://netcomposer.art/create/ (sections `## Composition` and `## Performance`).

- The **Composition** words fix harmony, length and shape. Keep them: the chords, any line the composer
  names, the form. Quote them verbatim in the plan.
- The **Performance** words are the brief (mood, pacing, density). The rest is your choice: voicings,
  sounds, orchestration, hacks, seed.

## Step 2. Write plan.json

Schema: `ncperf/schema/plan.schema.json` in the kit. Examples: `examples/ambient-cm/perf02.plan.json`
and `perf03.plan.json`. The parts:

- `chords`: `[{t, dur, symbol, voicing:[MIDI]}]`. 60 = C4. Prefer voicings that exist in the musicmatter
  full chord set (420 symbols, 2256 voicings; https://fullchords.musicmatter.art/, manifests under
  https://musicmatter.art/sounds/fullchords-v4/<ROOT>/<file>.nc.json, roots A Ab B Bb C D Db E Eb F Fsharp G).
- `sources`: every sound by id: `{"kind":"supplier","manifest":URL}`, `{"kind":"freesound","id":"123"}`,
  `{"kind":"uiowa_mis","url":URL}` or `{"kind":"url","url":URL,"license":"CC-BY-4.0","page":URL}`.
- `banks`: instrument -> MIDI note -> candidate ids in order. List two when you can; the renderer keeps
  the first that measures right.
- `layers`: `chord_notes` (strike/roll the voicing), `chord_pad` (unaltered copies, overlapped), `chord_under`,
  `drone`, `clips` (beds, fixed clips; do not set `stretch_to_s`), `notes` (the line;
  `kind:"triplet"` for triplets).
- `master`: per-bus hacks (filters, brighten automation, delay, LUFS match, pan), reverb, tone, loudness -17.
  Reverb: `{"engine":"dragonfly","send_lu":5.3}` (Dragonfly Hall; `send_lu` = wet loudness above the dry mix in LU,
  about 5 for ambient, 1-2 for drier; optional `params` such as `decay_s`). Or `"engine":"convolution","ir":"openair-stpauls-nave"`
  for a measured space (CC BY, credited automatically). Tone: `high_shelf: {hz, db}`.
- `quality`: `stretch_engine` (`auto` = Rubber Band R3 when installed) and `resampler` (`soxr`).
- `seed`, `notes` (intent and compromises; they go into the record).

Validate: `nc interpret --plan plan.json -o checked.json`.

## Step 3. Render

**Getting the kit:** nc-performer is public (MIT) at https://github.com/netcomposer-art/nc-performer.
Install: `pip install git+https://github.com/netcomposer-art/nc-performer` (and ffmpeg with the rubberband filter).
For the default reverb also install pedalboard (`pip install pedalboard`) and Dragonfly Reverb (VST3; `apt install dragonfly-reverb`
on Debian/Ubuntu, or the release VST3 in the standard VST3 folder, or point `NC_DRAGONFLY_VST3` at it). Both are GPL-3 and optional:
without them the kit warns, uses its synthetic hall, and the record says which reverb ran.

**With the kit** (Python 3.10+, ffmpeg with rubberband, numpy scipy soundfile librosa pyloudnorm):

```
nc render --plan plan.json -o out/          # fetch, license check, measure, select, hacks, master, record, verify
nc perform composition.md --stub -o out/    # or let the kit draft the plan (no LLM)
nc perform composition.md -o out/           # or any OpenAI-compatible LLM via NC_LLM_BASE_URL / NC_LLM_MODEL / NC_LLM_API_KEY
```

**By hand** (any tools), in this order:

1. Fetch each sound from any license-compatible source. Registered Suppliers
   (https://netcomposer.art/suppliers.json -> the Supplier's index -> the manifest) are one origin,
   not a preferred one. Also search the open commons (Freesound, Wikimedia Commons, the Internet
   Archive, ccMixter, Openverse, public-domain collections). Read the license on the live page and
   check a Supplier sha256. Freesound originals need OAuth2; the public HQ previews are fine, say so
   in the record. Hosted Perform queries these origins together and does not start with a Supplier.
2. Measure every pitched sound (pYIN or similar; FFT for bells). Drop any that is not the note.
3. Place, fade, filter, add delay and reverb. Never pitch-shift and never time-stretch. Hold a pad by
   layering and crossfading unaltered copies. Name the reverb tool and its
   version in the record; a convolution impulse response is an input like any other (license, author, sha256).
4. Master to -17 LUFS integrated, true peak <= -1 dBTP: linear gain, then a true-peak limiter (4x oversampled)
   only where a peak would pass the ceiling. Avoid ffmpeg `loudnorm`: it can switch to dynamic mode on its own. Write FLAC
   24-bit/48 kHz and an MP3 preview.

## Step 4. Check

- Loudness and true peak: `ffmpeg -i out.flac -af ebur128=peak=true -f null -`.
- The chords sound as planned (listen; or `python3 tests/check_render.py out/`).

## Step 5. The performance record

JSON, format https://netcomposer.art/record/, example https://netcomposer.art/records/ambient-cm-perf03.json.
It must list every source (URL, license, author, sha256, uses), every setting and the seed, the
interpreter (the model name, or `human/agent-written plan`), the outputs with sha256 and loudness, credit
lines, and `record_hash` = sha256 of the canonical JSON (sorted keys, no whitespace) without
`record_hash` and `signature`. `signature` stays UNSIGNED for now. The kit writes it; check with
`nc verify-record out/<name>.record.json --audio-dir out/`.

## Step 6. Submit

Submissions go through issues on the site repository (or a pull request adding an entry to
suppliers.json). This channel is proposed and may change. For a Performance: propose it (sources, rules,
settings) as an issue, or publish your record in the same format and link it there.
Issues: https://github.com/netcomposer-art/netcomposer.art/issues

Ask the person who sent you before you open the issue or publish anything in their name.
