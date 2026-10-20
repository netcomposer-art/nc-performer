# Perform (the hosted wrapper)

This folder is a small website in front of the NetComposer Performer kit in the parent directory.

This release is raw assembly. You write a Composition. Gather finds, for each chord, openly licensed net audio that is that chord: a full-chord recording from the registered suppliers and the open commons (musicmatter, Freesound, ccMixter, Openverse, and the rest), with no supplier preferred. The seed chooses which recording. The words can steer that choice (`choir`). Gather again chooses different files. A single-note library is used only when that chord has no full-chord file, and only by stacking its indexed notes at the same moment, with no effects. If a note would need a fresh pitch measurement, Gather uses the precomputed index or skips it. Perform places the clips in harmonic order, one after another. Written lengths and tempo are the slots when the Composition gives them; otherwise the slots are the seeded organic durations. Each clip is trimmed to its slot. A shorter clip ends, and the rest of the slot is silence. A 12 ms fade in and out, overlapped 12 ms at each join, is the only overlap. Each clip is level-matched, and one gain on the sum keeps the peak under full scale. Nothing else runs: no extra layers, no reverb, no room tone, no mastering, and no model call. Pitch is never shifted and audio is never time-stretched. The output is [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). The code is MIT, Copyright (c) 2026 Peter Semmelhack.

On the site, Perform and Perform again assemble in the browser. Gather copies each chosen clip into the R2 cache and returns the URL, the level-match gain, the usable region, and the written clock when the Composition has one. The page caches those bytes (`Cache-Control: public, max-age=31536000, immutable`, CORS for netcomposer.art). Filtering, modulation, and the dials run in the browser on that cache and do not call the server. Save is the only later server step: `POST /api/keep-take` checks the plan, stores the uploaded WAV or FLAC, and signs that file. One-shot `POST /api/perform` still gathers and assembles on the server for an older client.

The earlier layered engine stays in the tree and is off. `POST /api/perform-processed` answers 404 unless `NC_PROCESSED` is `1`, `true`, `yes`, or `on`. Gather and `POST /api/perform` stay raw either way. The deploy workflow does not set `NC_PROCESSED`.

An optional `instrument` field is a weak hint (`choir`, `synth`, `piano`). The ensemble comes from the words. When the Composition or the Performance names instruments, those words are the ensemble. When they name none, the take picks one family that can play every chord, and each such family is equally likely, piano included. The hint does not select a supplier, does not enter the search, and does not put piano first. One take is one clip per chord and one ensemble. The next take, or Gather again, can choose a different ensemble. A Performance is not refused because only one or two origins answered.

You can run it on your own computer. You do not need an account on netcomposer.art. A record made here, or with the kit, can move to any other instance with `nc submit`.

## What you are running

One program does the music. It is the `nc-perform` image. This release gathers and assembles inside that image. The processed path, when `NC_PROCESSED` is on, still calls the public kit (`nc compose`, `nc interpret`, `nc render`, `nc verify-record`).

Three ways to start that same image:

| Way | When to use it |
|---|---|
| Docker Compose | You want it on a computer you control. This is the self-host path. |
| Cloudflare Workers + Containers + Queues + R2 | The hosted site. A Worker takes the request, a queue starts the image, R2 keeps the files. |
| Modal | Optional. Same image, different host. See `modal_app.py`. |

The record format is the same in all three. Kit records stay unsigned. A hosted render is signed with Ed25519 when you give the instance a signing key. The signature sits outside `record_hash`, so signing does not change the hash.

There are no user accounts in v1. Each Performance comes with a `claim_token`, shown once. A later account system can adopt a Performance with that token. The record itself is not rewritten: `meta.json` keeps `owner: null` until then. Visibility and retention live in `meta.json`, not inside the hashed record.

## Rules the service will not break

- Inputs: CC0, CC BY, CC BY-SA, or public domain.
- Output: CC BY-SA 4.0.
- No pitch-shifting and no time-stretching. On this release each clip is trimmed to its slot, faded 12 ms, level-matched, and the sum is peak-guarded (ceiling 0.98). A plan that sets `stretch_to_s` is refused. A posted plan that is not raw assembly is refused on `POST /api/perform` with `This release assembles the recordings in order. Processing is off.`
- The layered engine, when `NC_PROCESSED` is on, still trims, layers, and crossfades. Assembled chord tones keep the pitch they were recorded at and share one layer. A Performance can have at most 16 layers (`NC_MAX_LAYERS`); a plan past that is trimmed and the record says what was left out. That path is not what Gather or Perform run.
- A raw plan may keep one file per chord, and a stack's note files. Its source cap is the larger of the layered cap (24) and eight files per chord, still inside the 64 KiB plan cap. The layered cap stays 24.
- No `kind=url` sources. The service will not fetch an arbitrary URL.
- The transforms are called hacks. A one-shot raw record lists trim, fade, per-clip loudness (−20 LUFS), and the peak guard. A browser save lists the page's processing chain. The server does not re-run those effects. The signed record carries the upload hash.

On the processed path only, AI planning runs when the words ask for a line, triplets, or sections. Set `NC_LLM_EVERY_PERFORM=1` to send every processed Perform through the model. That flag defaults off. Raw assembly never calls the model. When the model is not used, phrase mapping still changes the plan: ring, space, distance, a plucked string, a fat synth, and "then" are read from the words. One take keeps one ensemble. Inside that ensemble the arrangement changes from chord to chord (who plays, when, how thick, how loud) unless the words ask for something steady, such as "repeat the same sounds over and over". Chord lengths vary too: some linger, some pass, some arrive early or late, unless the words lock a pulse ("steady pulse", "one chord per bar", "10 seconds per chord"). Words that ask a chord to ring or to be legato keep that sound going for the whole written length: the recording is started again, overlapped, and never time-stretched, so a long chord does not open a silence. A bar table in several columns is read in bar order, and chord words in the prose are not added. When every chord has a written length — `[2 bars Cm]`, `Cm (2 bars)`, or `Cm for 4 beats` — with a tempo (`60 BPM` or `quarter = 60`) and a meter (`4/4` by default, also `3/4`, `6/8`, `2/2`), that clock is kept, including `at bar 12, 80 BPM`. Uneven lengths are only used when the Composition does not set them. A written proportion, such as "second half", keeps that boundary and the total length, and the chords inside it still vary. The record's timeline and each sound's spans carry those real start and end times. A named voice that cannot play a chord is left out of that chord, and the record says which chords it missed, instead of swapping in a different instrument. Those choices are seeded and stored on `source_mix.arrangement` in the record. `summary.source_mix` on the record lists how many clips came from each origin. The hosted default model is `grok-4.3` at `https://api.x.ai/v1`. Set `XAI_API_KEY` and nothing else, and the kit posts to `{base}/chat/completions` with that model. If you set `NC_LLM_BASE_URL` yourself, that host is used instead. Tests point that URL at a local mock. They never call api.x.ai.

`GET /api/config` includes `llm_price`: the model name, USD per million tokens in, cached, and out, and the page those prices came from (`https://docs.x.ai/docs/models`, grok-4.3 list prices recorded in `service/nc_perform/config.py`: $1.25 / $0.20 cached / $2.50 per million). A finished job's status adds `cost_estimate` with that card. When the model actually ran, it also includes the token counts and `usd` (tokens times those prices). No typical token count is invented.

`GET /api/jobs/:id` always includes these fields the site reads while the job is running:

- `trimmed`: a plain-English note when the plan was shortened to fit a limit, or `null` when nothing was cut. The same note is copied into the record's known compromises. A musical surprise is not a trim.
- `seed`: the seed used for this take. An omitted seed is random. Pass `seed` when you want the same take again.
- `found_sounds`: one row per chosen chord as soon as that chord is picked, `{chord, id, title, supplier, license, preview_url, symbol}`. `chord` is the position starting at 0. `id` is `c0`, `c1`, …. `preview_url` is `GET /api/jobs/{id}/audio/{id}` (playback answers 404 until the collection is stored). A later pick for the same chord replaces that row. A layered job still lists `{title, supplier, license}` only. Rows without a title, supplier, and license are left out. Nothing is invented.
- `stage_line`: a short sentence for the current stage, such as `Finding a choir on Freesound…`.
- `tester`: `true` only when the request sent a matching `X-NC-Tester` header, otherwise `false`.

`preview_ready` is `true` once `preview.mp3` has been stored and one byte has been read back, which happens before the record is signed. The status then includes `files.mp3`. `state` stays `rendering`, `verifying`, or `storing` until every required file, including the record, has been read back. `done` is sent only after `preview.mp3`, `meta.json`, the record, and the other output files have been read back from storage. Until that read succeeds the status stays `storing` (`Storing the Performance…`).

`POST /api/jobs/:id/cancel` with the job's `claim_token` (or the same secret as `keep_token`) stops a queued or running Perform. The shared warm container is left running; that job is stopped inside it. The queue slot is freed, and files under that id are removed. The status becomes `cancelled` with `error` and `stage_line` `Stopped.`. Calling it again is the same. A job that is already `done`, `failed`, or `rejected` is not changed and its files stay.

Raw Gather does not search for room tone, texture, or a bed, even when the words mention a room. On the processed path, a room-tone search runs only when the words ask for air or space. Each supplier search on that path, including each room-tone search, stops after 20 seconds and is skipped. Those searches run together, so one slow supplier does not hold up the others.

Recorded notes come from a precomputed index, `ncperf/data/note-index.json`. Each row is one file: its URL, instrument, measured pitch and cents, onset, length, license, and credit. Perform reads that index, picks the notes, and downloads only the files the plan kept, in parallel. It does not measure those notes again. A pitch the index does not list is left unavailable. Whole-chord files that are not in the index are still measured when the processed renderer places them. Raw assembly trims the published file and does not measure it. Parsed supplier credits are saved under `NC_CACHE/credits`, so a manifest or a Freesound page is not fetched twice. The index is metadata, not the audio.

The index ships with the kit. When R2 has `note-index/v1.json`, the Worker copies it into the container before the job, and that copy wins. `NC_NOTE_INDEX` (a file) or `NC_NOTE_INDEX_URL` also overrides the bundled file. Rebuild it with `python -m ncperf.noteindex`. The manual workflow "Refresh note index" runs that command and uploads the JSON as an artifact. It does not commit and it does not deploy. A weekly schedule can be added later; it is not turned on.

### Checked sounds

The note index and the musicmatter catalog are one registry: `ncperf/data/checked-sounds.json`. Each row is one sound that has already been checked. The key is the supplier id plus the canonical URL. A file that holds several notes adds the MIDI note to that key. Freesound credit lookups use the sound's page; that page is an alias of the audio row, not a second sound.

A row holds `verdict` (`ok` or `rejected`), `reason` (`bad_license`, `missing_credit`, `wrong_pitch`, `wrong_chord`, `unreachable`, `silent`, `clipped`, `too_short`, or empty), `pitch_midi`, `cents`, `chord` or `note`, `duration_s`, `loudness_lufs`, `license`, `title`, `author`, `page`, `fingerprint` (`etag`, `last_modified`, `size`, `sha256`), `checked_at`, `license_checked_at`, and `checker_version`.

The registry lives in R2 as one object, `registry/v1.json`, in the same bucket as the performances. The Worker copies it into the container before the job, the same way it copies `note-index/v1.json`, and copies a changed registry back when the job ends. A small `registry/v1-stats.json` is the public count. R2 fits this because Gather reads the catalog once and then looks up rows in memory. D1 would add a database binding the container does not have. KV bills per key and caps a value at 25 MiB, which is a poor fit for one multi-megabyte catalog read once per job. No new binding is required.

On a hit, Gather does not download the file to check it, does not measure pitch, and does not open the credit page. A rejected row is skipped and is not tried again in that take. A sound the registry has never seen is checked once and written back. If the Gather deadline is too close (under 0.35 s), that newcomer is left out of this take and queued. A background check on the container runs it so the next take can use it. The queued work does not hold the take past the 8 second Gather stop.

A row is checked again only when its fingerprint changes, `checker_version` changes, or the license row is older than `NC_REGISTRY_RECHECK_DAYS`. The default is 90. That variable is the interval. It is not a cap: set it to 400 and the background license check waits 400 days. A license that is due still counts as ok for this take. The recheck is the background queue, not a download during Gather. The manual workflow "Refresh checked-sounds registry" rebuilds the file and uploads it as an artifact. It does not commit, deploy, or run on a schedule.

`loudness_lufs` is null in the seed. The backfill did not decode the audio, so it does not invent a loudness. Raw render still matches the level of the clip it plays. Duration and cents are filled for the note-index rows, which were already measured. They are empty for musicmatter until a check decodes the file.

`GET /api/config` and `GET /health` include `registry`: `size`, `ok`, `rejected`, `checker_version`, and `recheck_days`. No URLs and no secrets. The Worker reads those counts from `registry/v1-stats.json` when that object exists. Until a job copies the registry up, it reports the bundled seed (10,334 rows, 8,977 ok, 1,357 rejected). The container reads the file in the image. `GET /api/config` also sets `browser_assembly` to true, which is the signal the site uses to assemble in the browser and Save with `POST /api/keep-take`.

The seed is the note index plus the musicmatter catalog already on disk (10,195 offerings). It did not download the audio. Counts: 10,334 rows, 8,977 ok, 1,357 rejected. By supplier: musicmatter 8,839 ok and 1,356 rejected (`wrong_chord`, a catalog file whose name is not a chord), University of Iowa 104 ok, Freesound 34 ok (piano, cello, and flute), Philharmonia 1 rejected (`bad_license`, the custom licence already recorded on the note index). No row was rejected for a missing credit, a wrong pitch, an unreachable file, silence, clipping, or length, because those checks need the audio and this seed did not fetch it. On this machine a 5-chord Gather with the disk index and an empty registry downloaded 5 manifests and finished in 0.232 s (hit rate 0). The same Gather with the registry loaded made no check download and finished in 0.042 s, hit rate 1.0 (5 of 5). Loading the 5.5 MB file took 0.035 s. A rejected lookup returned in under a millisecond and did not fetch. Queue wait and the R2 copy are not in these numbers.

Gather, on this release, runs the supplier searches together and stops at 8 seconds (`GATHER_S`). Each supplier still has a 4 second budget. A chord with no live hit uses the musicmatter index already on disk, then indexed notes. If neither has that chord, Gather fails; it does not hang. It does not download the note libraries, does not measure pitch, and does not call the model. One-shot `POST /api/perform` is that Gather plus the raw assembly. Supplier indexes are cached on disk under `NC_CACHE/indexes`. Whole-chord files come first. A written slash bass prefers the inversion whose bass matches. When none does, the chord recording and one UIowa or other indexed bass note are sounded together, with no effects and no pitch-shift. When that note is not indexed, the chord is kept alone. A bass never drops the chord. Indexed notes are stacked on their own only for a chord that has no whole-chord file. A preview plays one clip the first time it is requested. The processed Gather (`processed` on the kit call, or `POST /api/perform-processed` when the flag is on) keeps the older 4 second origin and note budgets and can still search room tone.

### This release, BWV 846

Seed 1, no instrument field, no Freesound API key, Dragonfly present and unused. These clocks are search, pick, download, and render only. Queue wait, container start, and the R2 copy are not included.

| Path | Seconds |
|---|---|
| Gather, fresh cache, live suppliers | 4.7 |
| Perform from that Gather (downloads the clips) | 2.6 |
| Same Perform, those files already local | 2.0 |
| Gather, supplier indexes and credits already local | 0.8 |
| Earlier cold pass, empty cache | 6.5 gather, 3.8 perform |

Both passes are inside the targets (Gather under about 10 seconds, Perform-from-gather under about 5–10 seconds). The piece is 35 chords, 35 whole-chord files, fat synth, musicmatter, length 121.739 s. The prose mentions a room; this path did not search for one.

Canon in D from that Gather, seed 1, rendered in 1.0 s. The 12-bar blues in A gathered in 4.2 s (Openverse used the deadline) and rendered in 1.1 s.

### While the page is waiting

Clocks on this machine. Queue wait and the R2 copy are not included, except the local process start.

| What | Seconds |
|---|---|
| Gather, 5 chords, disk index, no registry, 5 manifest downloads | 0.232 |
| Same Gather after the registry is loaded, 0 downloads, hit rate 1.0 | 0.042 |
| Loading the registry (10,334 rows, 5.5 MB) | 0.035 |
| A rejected row, looked up and skipped | 0.001 |
| First chord on the status list, 5 chords, warm musicmatter index | 0.85 |
| That Gather, searches finished inside the 1 s settle | 0.85 |
| MP3 encode for those 5 chords (files already local) | 1.36 |
| Record written after that MP3 | 2.45 |
| Local process start until `GET /health` | 0.61 |
| `GET /api/warm` once the process is up | 0.001 |
| Gather hard stop | 8 |
| Each supplier search | 4 |
| Canon in D, seed 1, gather (earlier pass, warm index) | 0.8 |
| Canon in D, seed 1, render | 1.0 |
| 12-bar blues, gather / render | 4.2 / 1.1 |
| BWV 846, earlier pass, warm index gather / render | 0.8 / 2.0 |

Hosted container cold start was not measured here (no Cloudflare credentials). The Worker allows 300 seconds for the port, because a cold instance imports Dragonfly and the Python audio stack. `GET /api/warm` starts the shared container `nc-warm` when the Composition field is focused, and later jobs use that same instance. This process, already running, answers `{ok: true, warming: false}`. The Worker answers `{ok: true, warming: true}` and starts the container in the background. The ping is not a Perform.

Slash bass, this machine, disk index and the checked-sounds registry already loaded. Queue wait, container start, and the R2 copy are not included. Every seed gathered and rendered. The prelude is 35 chords, 121.739 s. Thirty-three use a matching inversion. `F#dim7/G` and `G7/C` have no such voicing, so each is the chord file plus one UIowa piano bass note (G2, C3). Canon and blues have no slash bass.

The word "Piano." in the Canon and blues Performance lines does not lock the family. The draw is the families that can play every chord: a whole-chord file for each one, or a stack of indexed notes for each one. Piano files and piano notes are one family. On this index, choir, fat synth, piano, and synth arp each have the files. UIowa guitar, strings, and winds do not contain every tone of these chords, so they are not in the draw. A named choir still wins when the words ask for one.

| Piece | Seed | Gather | Render | Ensemble |
|---|---|---|---|---|
| C major Prelude | 1 | 0.893 | 2.706 | piano |
| C major Prelude | 2 | 0.865 | 2.293 | fat synth |
| C major Prelude | 3 | 0.799 | 2.103 | piano |
| C major Prelude | 4 | 0.825 | 2.609 | choir |
| C major Prelude | 5 | 0.800 | 2.616 | synth arp |
| Canon in D | 1 | 1.003 | 1.006 | piano |
| Canon in D | 2 | 1.028 | 1.627 | fat synth |
| Canon in D | 3 | 1.031 | 0.874 | piano |
| Canon in D | 4 | 1.013 | 1.523 | choir |
| Canon in D | 5 | 1.032 | 1.499 | synth arp |
| 12-bar blues in A | 1 | 0.961 | 0.665 | piano |
| 12-bar blues in A | 2 | 1.021 | 1.288 | fat synth |
| 12-bar blues in A | 3 | 0.962 | 0.663 | piano |
| 12-bar blues in A | 4 | 1.026 | 1.385 | choir |
| 12-bar blues in A | 5 | 0.963 | 1.591 | synth arp |

### Canon in D, 10 seeds, no instrument named

The published Performance line says "Piano." This run used the 16-bar harmony and the words "Let each chord ring into the next. One ensemble, clear and unhurried." Live suppliers were queried inside the 4 second deadline. Musicmatter covered every chord, so no note stack was used. Each take is 16 whole-chord files, one family, length 53.333 s (one chord per bar).

| Seed | Family | Origin | Built | Chord 1 |
|---|---|---|---|---|
| 1 | piano | musicmatter | whole-chord file | D major chord, position 4, root 8va |
| 2 | fat synth | musicmatter | whole-chord file | Fat Synth |
| 3 | piano | musicmatter | whole-chord file | D major chord, position 4, root 8va |
| 4 | choir | musicmatter | whole-chord file | Choir |
| 5 | synth arp | musicmatter | whole-chord file | synth arp |
| 6 | fat synth | musicmatter | whole-chord file | Fat Synth |
| 7 | fat synth | musicmatter | whole-chord file | Fat Synth |
| 8 | fat synth | musicmatter | whole-chord file | Fat Synth |
| 9 | synth arp | musicmatter | whole-chord file | synth arp |
| 10 | choir | musicmatter | whole-chord file | Choir |

Piano is in 2 of these 10 takes. An `instrument: "Piano"` hint on the same seed does not change the family. Words that say "choir" pick choir. The word "Piano." does not. It is one family in the draw, the same size as the others that can cover the piece.

### Previous layered engine (not what this release runs)

Cold BWV 846 on this machine (seed 1393160823, instrument Piano, no Freesound API key, Dragonfly loaded, fresh cache). These clocks are the older layered render. Queue wait, container start, and the R2 copy are not included.

| Path | Seconds |
|---|---|
| One-shot, before parallel fetch | 415.5 |
| One-shot, parallel fetch, cold disk | 110.7 |
| One-shot, parallel fetch, files already local | 64.6 |
| Gather, whole-chord files, notes not in an index | 6.7 |
| Perform from that Gather | 10.5 |
| Gather + Perform, whole-chord files | 17.3 |
| Gather, note index, cold disk | 5.3 |
| Perform from that Gather (17 UIowa piano notes) | 15.0 |
| Gather + Perform, note index | 20.3 |
| Same Perform, those files already local | 12.9 |
| One-shot, note index, cold disk | 151.6 |

The note-index rows are the same piece, seed, instrument, and machine. Gather read the index and did not measure pitch (0 measurements). It kept 17 University of Iowa piano notes, all already pitch-checked. Perform downloaded those files and rendered them, Dragonfly included. The one-shot still uses the 20 second supplier budget, so its plan was 44.6 seconds (Openverse timed out) and the render was 107.0 seconds. That render also measured nothing; it fetched the same kind of piano files a second time, plus one room-tone clip. Queue wait, container start, and the R2 copy are not in any of these numbers.

The signed record still carries `performance.timeline` and `sounds_used[].spans` as kit 0.2.0 writes them.

Freesound is queried through its official API when the Worker secret `FREESOUND_API_KEY` is set. If the secret is missing, or the API fails, the HTML search is used. The deploy workflow writes the secret only when it is non-empty.

A soft daily cap of $3 switches new jobs to the stub. A hard cap of $5 answers "Perform is resting until tomorrow."

Unlisted is the default. Unlisted audio is kept 90 days unless someone presses Keep. Public audio is kept, and it appears in the feed only after 24 hours. A takedown returns 410 and leaves a tombstone. The sealed copy of the record is under `sealed/<id>/`.

Keep with a JSON `{claim_token}` body still keeps the original render. When the Live dials have moved, the create page sends multipart `claim_token`, `dials` (JSON), and `audio` (a WAV the browser rendered through the filter and delay). That file is stored as `kept.wav` next to the original. The signed record is not rewritten: `meta.json` carries `live_dials`, and the record page says the dials were applied after signing. The WAV must be PCM, 44.1 or 48 kHz, mono or stereo, 16- or 24-bit, no longer than the job length plus 30 seconds, and at most 40 MiB. The Worker stores the WAV only. This Python service also writes `kept.mp3` (192 kbps) when ffmpeg is available.

## API

| Method and path | What it does |
|---|---|
| `POST /api/gather` | Pick one clip per chord and return the collection. Body: `title`, `composer`, `composition`, `performance`, optional `instrument` (ignored as an ensemble), `seed` (random when omitted), `exclude_gather_id`, `claim_token` (required with `exclude_gather_id`), `turnstile_token`. This service answers `200` with `gather_id` (`g-` plus 12 Crockford characters), `id` (the same id), `claim_token` (once), `seed`, `sounds`, `poll`, and `excluded_gather_id` (`null` when this is the first collection). Each sound is one chord. The row always has `{id, clip_id, pos, title, supplier, license, credit, author, preview_url, url, page, chord, symbol, start_s, end_s, duration_s, loudness_lufs, gain_db}` and `source_url` when that clip is a single file. `id` and `clip_id` are `c0`, `c1`, …. `url` and `preview_url` are the same `GET /api/jobs/{gather_id}/audio/{id}`. `chord` is the position; `symbol` is the chord name (`Cm`). `start_s` and `end_s` are the usable region in that file. `duration_s` is `end_s - start_s`. `gain_db` is the level match toward −20 LUFS (`-20 - loudness_lufs`). A raw response also has `clock`: `null` when the Composition did not write a tempo and lengths, otherwise `{written, durations_s, slots:[{t, dur}], tempo:[{bar, bpm}], quarters_per_bar}`. The gathered state is the contract. A chord event before the copy finishes may not have the measurements yet. The Worker answers `202` while the container runs: the same fields, `sounds: []`, and `pending: true`. Poll `poll` until `state` is `gathered`, then read `sounds` there. A second Gather with `exclude_gather_id` and that collection's `claim_token` uses a new id, a new token, and a new seed unless `seed` is set. It skips the previous picks when another sound can play the chord. A missing token is `400` `{"error":"claim_token is required.","code":"rejected"}`. A wrong token is `403` `{"error":"claim_token does not match this Gather."}`. An unknown id is `404` `{"error":"not found"}`. |
| `POST /api/perform` | One-shot Gather plus raw assembly when `gather_id` and `plan` are omitted. It does not call the model. Body: `title`, `composer`, `composition`, `performance`, optional `plan` (only `assembly: "raw"`), `visibility` (`unlisted` or `public`), `seed`, `perform_again`, `turnstile_token`, `keep`. Returns `202` with `id` (`h-`), `claim_token` (once), and `record_url`. With `gather_id` and that Gather's `claim_token`, the body places that collection and does not search. Composition fields on that call are ignored; visibility and keep still apply. A posted plan that is not raw assembly is `400` `{"error":"This release assembles the recordings in order. Processing is off.","code":"rejected"}`. A Gather that is still running is `409` `{"error":"That Gather is not ready.","code":"rejected","gather_id","state"}`. A missing token is `400` `{"error":"claim_token is required.","code":"rejected"}`. A wrong token is `403` `{"error":"claim_token does not match this Gather."}`. An unknown id is `404` `{"error":"not found"}`. |
| `POST /api/keep-take` | Save. Multipart fields: `gather_id`, `claim_token` (the Gather's token), `take_state` (JSON, at most 64 KiB), `audio` (WAV or FLAC, at most 40 MiB), optional `title` (at most 200 characters), optional `listed` (`true` or `false`; omitted stays unlisted), and `turnstile_token` when the bot check is on. Turnstile, the tester pass, and the per-network limits apply, the same as Gather. This service checks the plan and the audio before it stores them and answers `400` `{"error","errors","code":"rejected"}`. On success it answers `200` with `{id, gather_id, status:"done", state, claim_token, record_url, page_url, poll}` after the files read back. The Worker checks the body size and that the audio is WAV or FLAC, answers `202` with `{id, gather_id, status:"queued", position, poll, claim_token, record_url, page_url}`, and the container checks the plan and the audio. A failed check becomes a rejected job whose `error` is the same sentence. `done` waits for the R2 read-back. The new `claim_token` belongs to the Performance. The stored take is the upload. The server does not replace it with its own mix. |
| `POST /api/perform-processed` | The earlier layered engine (search, layers, reverb, room tone when the words ask, mastering, and the model when the words ask). Answers `404` `{"error":"not found"}` unless `NC_PROCESSED` is on. The body is the same as `POST /api/perform`. |
| `GET /api/jobs/:id` | Status. `state` moves through queued, gathering, gathered, planning, fetching, rendering, verifying, storing, then done, failed, rejected, or cancelled. A raw job does not enter reverb or mastering. The body always includes `trimmed` (string or null), `seed`, `found_sounds`, `stage_line`, and `tester` (true only when that job was started with a matching `X-NC-Tester` header). A raw `found_sounds` row is `{chord, id, title, supplier, license, preview_url, symbol}` and arrives as each chord is chosen. After the copy, that row also carries the measurement fields (`clip_id`, `pos`, `url`, `start_s`, `end_s`, `duration_s`, `loudness_lufs`, `gain_db`, `credit`). A layered row stays `{title, supplier, license}`. Before `done`, `preview_ready: true` and `files.mp3` mean the MP3 was read back and the record may still be signing. A `g-` id also includes `gather_id`, `sounds` (the same sound objects as Gather, or `[]` until they are ready), and `clock` (`null` means the page uses the organic rule). The signed record lists every clip and its time span. |
| `GET /api/jobs/:id/audio/:sid` | One gathered clip, from `gathers/{gather_id}/audio/{sid}` in R2. `Cache-Control: public, max-age=31536000, immutable`. `Access-Control-Allow-Origin` echoes `https://netcomposer.art` and `https://*.netcomposer.art` (`Vary: Origin`); any other Origin gets `*`. `404` when that file was not stored. |
| `GET /api/jobs/:id/events` | The same status as a stream. A new or replaced chord is a new event. The stream also carries `preview_ready`. |
| `GET /api/warm` | Start the shared Perform container. The page calls it when the Composition field is focused. Worker: `200` `{ok: true, warming: true}` immediately, then `GET /health` on container `nc-warm`. This process: `200` `{ok: true, warming: false}`. Not counted as a Perform. |
| `GET /api/jobs/:id/files/:name` | FLAC, MP3, the dialed `kept.wav` (and `kept.mp3` when the mirror made one), record, plan, and the other stored files. Expired or taken-down audio is `410`. `flac` and `mp3` on the job stay the original render. |
| `POST /api/jobs/:id/cancel` | Stop a Performance or a Gather. JSON `{claim_token}` or `{keep_token}` (the secret from the call that created that id). Queued work is dropped. Running work is stopped, the slot is freed, and partial files are removed, including `gathers/{id}/`. The status is `200` with `state` `cancelled`, `stage` `cancelled`, `percent` `100`, `position` `null`, `error` `Stopped.`, and `stage_line` `Stopped.`. A second call returns that same status. `done`, `failed`, and `rejected` are left as they are and answer `200` with the current status (a finished Performance still has `files`). A finished Gather (`gathered`) can still be cancelled, and its files are removed. Missing token is `400` `{"error":"claim_token is required.","code":"rejected"}`. A wrong token is `403` `{"error":"claim_token does not match this Performance."}`. An unknown id is `404` `{"error":"not found"}`. |
| `POST /api/jobs/:id/keep` | JSON `{claim_token}` keeps the original audio. Multipart `claim_token`, `dials`, and `audio` (WAV) stores the dialed file, sets `live_dials`, and answers `{ok, processed:true, dials, audio_url}`. The signed record is unchanged. |
| `POST /api/jobs/:id/visibility` | Body `{claim_token, visibility}`. |
| `GET /records/:id` | The HTML page. |
| `GET /records/:id.json` | The record bytes. |
| `GET /records/sha256/:hash` | Redirect to the page for that `record_hash`. |
| `GET /api/verify/:id` | Hash, license, pitch, output checksums, and whether the signature matches. |
| `GET /.well-known/nc-record-keys.json` | The public Ed25519 key, when a signing key is set. |
| `POST /api/submit` | A kit record plus its audio. Checked, stored under a `k-` id, not re-rendered, not signed. |
| `GET /api/feed` | Public Performances whose 24 hour wait has passed. |
| `GET /api/config` | Site key, limits, whether a model key is configured, `browser_assembly` (true on this release), and `registry` counts (`size`, `ok`, `rejected`). |
| `GET /health` | Liveness, plus the same `registry` counts. |
| `GET /` | The Perform page. |
| `GET /admin` | Today's spend. Needs `Authorization: Bearer <NC_ADMIN_TOKEN>`. |
| `POST /admin/enabled` | Body `{enabled: true\|false}`. |
| `POST /admin/hide` | Body `{id}`. Removes it from the feed. |
| `POST /admin/takedown` | Body `{id, reason}`. `410` and a tombstone. |

`503` means resting or the queue is full. `429` means this network sent a short burst (more than 3 Performs in 10 seconds), or has hit 3 Performs in 10 minutes or 10 in a rolling 24 hours. Gather, Save (`POST /api/keep-take`), one-shot Perform, and Perform-from-gather each count as one Perform toward those windows. Assembling again in the browser does not, and neither do filtering, modulation, or the dials. The 10-minute and daily windows are counted in the service's own state. A `429` with `code` `rate` also returns `retry_after_s` (seconds) and `retry_at` (UTC ISO-8601, such as `2026-10-21T15:40:00Z`). The page should format `retry_at` in the visitor's local time. The `error` sentence names that same moment in UTC, for example `Daily limit reached (10 Performs from this network). Try again after 3:40 PM UTC.` `403` means the Turnstile check failed. Turnstile is off when `TURNSTILE_SECRET_KEY` is unset.

A request to `POST /api/perform`, `POST /api/gather`, or `POST /api/keep-take` may send the header `X-NC-Tester` with the value of the Worker secret `NC_TESTER_TOKEN`. When they match, that request skips the per-network 10-minute and daily limits and does not use a slot. Turnstile, the word and plan size limits, the 10-second burst guard, and the global $3 / $5 daily cost caps still apply. The job status then has `tester: true`. The token is not stored and not logged. `NC_ADMIN_TOKEN` does not skip these limits. Leave `NC_TESTER_TOKEN` empty and the header does nothing.

## Assembly plan v1

The page generates this document. The server does not. `nc_assembly_version` may be omitted; if it is present it must be `"1"`.

```json
{
  "nc_assembly_version": "1",
  "gather_id": "g-0123456789ab",
  "seed": 9,
  "entries": [
    {
      "pos": 0,
      "chord": "Cm",
      "clip_id": "c0",
      "at_s": 0,
      "len_s": 2.0,
      "gain_db": -2.45,
      "fade_in_ms": 12,
      "fade_out_ms": 12
    }
  ],
  "total_s": 10
}
```

`chord` is the symbol from the gather sound (`Cm`), not the index. `pos` is `0 … n-1` in that gather's order. There is one entry per chord, the same clip ids, in that order. `seed` is the page's seed. Perform again in the browser picks a new seed and does not ask the server. `gain_db` is copied from the gather sound. Keep accepts it within ±0.5 dB of that number. The match is −20 LUFS: `gain_db = -20 - loudness_lufs`, rounded to 2 decimals. A registry row's `loudness_lufs` wins when it is a number. The checked-sounds seed leaves loudness empty, so Gather measures the usable region: integrated loudness when that region is at least 0.4 s, otherwise an RMS match to the same target.

`len_s` must be ≤ `duration_s` (1 ms of rounding). A longer value is refused. Nothing is time-stretched and nothing is pitch-shifted. Fades are from 0 through 50 ms. Send 12 to match the one-shot fade. `at_s` does not go backwards. A start earlier than 0 by more than 1 ms is refused. Organic slots and a written clock both put the first chord at 0. `total_s` is at most 180. Each chord must end by `total_s` (20 ms).

When `clock` is an object, the written grid is the law. `at_s` matches `clock.slots[i].t` within 20 ms, `len_s` is at most that slot's `durations_s[i]`, and `total_s` is the sum of `durations_s` within 20 ms. If the recording is shorter than the written slot, set `len_s` to `min(written, duration_s)` and leave `at_s` on the written grid. The rest of the slot is silence. The 1.25 s floor is not applied to a written clock. A written chord can be as short as the plan schema allows (0.1 s).

When `clock` is `null`, the page places the chords with the organic rule. The reference is `ncperf.take.chord_slots`. A port has to match `ncperf/data/assembly-vectors.json` on every `slots` list, `duration_cv`, and `looks_alternating`.

Organic rule, in the same terms as that function:

- Sections are `(count, seconds)` and keep their boundaries. A free take with no written split is one section, `(chord count, the take's seconds)`.
- Each section draws from Python `random.Random(int(seed) + 211 + section_index * 29)`.
- A stated pulse (`steady pulse`, `one chord per bar`, and the other phrases `take.stated_pulse` knows) uses equal bars. `broaden` lengthens only the last section's final bars, by 1.12, then 1.28, and so on, adding 0.16 each bar. The bars before those stay identical.
- Otherwise the section is phrase rubato. Weights come from `take._free_weights`. `_fit_lengths` scales them onto the section. The floor is `lo = min(mean * 0.92, max(1.25, mean * 0.28))` and the ceiling is `hi = max(mean * 1.08, min(total * 0.58, mean * 2.85))`. If `n * lo` is longer than the section, `lo` becomes `total / n * 0.9`. If the lengths look alternating, or `duration_cv` is under 0.22, `_unstick` runs up to three times.
- `duration_cv` is the population standard deviation divided by the mean, rounded to 3 decimals. `looks_alternating` is true when there are at least four chords, the even and the odd groups are each tight (range / midpoint under 0.12), and the two group means differ by more than 0.2 times the overall mean.

Reference rows (the file has the full slots):

| Name | What it locks |
|---|---|
| `five-chords-seed-1` | 5 chords, 40 s, seed 1, free. Durations 7.893, 6.919, 13.797, 6.403, 4.988. CV 0.381. |
| `five-chords-seed-2` | Same shape, seed 2. Durations 4.125, 17.94, 4.954, 6.143, 6.838. CV 0.632. |
| `floor-eight-seed-3` | 8 chords, 12 s, seed 3. The floor holds: 1.523, 2.206, 1.403, 1.867, 1.25, 1.25, 1.25, 1.251. CV 0.223. |
| `pulse-four-seed-1` | 4 chords, 16 s, pulse, seed 1. Four bars of 4.0 s. CV 0. |
| `pulse-broaden-seed-4` | 8 chords, 20 s, pulse, broaden 2, seed 4. Six bars of 2.381, then 2.667 and 3.047. |
| `two-sections-seed-7` | Sections (3, 12 s) and (2, 10 s), seed 7, free. Durations 2.989, 3.647, 5.364, 7.673, 2.327. |
| `written-60bpm-bars` | `4/4`, `60 BPM`, `[1 bar Cm] [2 bars F] [1 bar G]`. Durations 4, 8, 4. The floor is not applied. |

Save does not render this plan as the take. Web Audio will not match a server mix. The page exports the audio it played and uploads that file.

`take_state` is the plan plus the processing the page applied:

```json
{
  "nc_take_version": "1",
  "plan": {
    "nc_assembly_version": "1",
    "gather_id": "g-0123456789ab",
    "seed": 9,
    "entries": [],
    "total_s": 10
  },
  "processing": {
    "chain": ["filter", "modulation", "dials"],
    "params": { "cutoff_hz": 1200, "wet": 0.2 }
  }
}
```

`nc_take_version` may be omitted; if it is present it must be `"1"`. `processing` may be omitted. `chain` is up to 32 short names, in the order the page ran them. `params` is a JSON object. The whole `take_state` is at most 64 KiB. The server stores the chain and the params. It does not re-run them.

The upload is WAV or FLAC, mono or stereo, 44100 or 48000 Hz, at most 40 MiB. Its duration must match `total_s` within 0.5 s. A longer effect tail belongs in `total_s` (still at most 180). The sample peak must be at least about −60 dBFS and at most about +0.2 dB (1.02). Integrated loudness, when the file is at least 0.4 s, must sit between −45 and −5 LUFS. A file that is not decodable is refused.

The earlier plan checks still apply: clips from that Gather, harmonic order, no stretch, gains within ±0.5 dB, fades at most 50 ms, written lengths when `clock` is set, `total_s` at most 180. The server then rebuilds the unprocessed placements from the gathered bytes, only to confirm every clip is present and no entry is longer than its source. That mix is discarded. The stored take is the upload: a FLAC upload is kept byte for byte as `performance.flac`; a WAV upload is packed into FLAC without resampling. `preview.mp3` is encoded from that file. The signed record contains `take_state`, `assembly_plan`, the timeline, a credit and license for each clip, and `performance.take_audio.upload_sha256` (the exact uploaded bytes) plus the stored file's sha256. `done` is not published until `performance.flac`, `preview.mp3`, `record.json`, and the other required outputs read back.

On this machine, with the files already in memory and no supplier network: measuring five 2-second clips took 0.277 s (0.239 s on the repeat). A 1.6 s Save of an uploaded WAV, including the confirm mix, the FLAC pack, the MP3, the loudness scan, and the read-back, was 0.516 s of wall time (confirm and encode 0.317 s, verify 0.184 s). The download of a live supplier file is still outside these numbers. Gather's search still stops at 8 seconds; the copy and the measure run after that, under the 5-second fetch budget.

## Self-host with Docker

You need Docker. You do not need a Cloudflare account, an xAI key, or a domain.

1. Install Docker Desktop, or Docker Engine with Compose.
2. Clone this repository.
3. From the `service` directory, start it:

   ```
   cd service
   docker compose up --build
   ```

4. Open http://localhost:8080
5. Type a short Composition and press Perform. The first build takes a few minutes. Later starts are faster.
6. When it finishes, play the MP3, download the FLAC, and download the record.

That is a working instance. Audio is stored in a Docker volume named `perform-data`. The page says the instance is "nc-perform (self-hosted)". Records are unsigned until you add a key.

### Optional keys

Copy the example and edit it. Compose picks up `service/.env` when the file exists.

```
cp .env.example .env
```

- `XAI_API_KEY` turns on model planning for words that ask for a line, triplets, or sections. Leave it empty to stay on the stub.
- `NC_SIGNING_KEY` signs hosted records. Make a key with the command in the owner checklist below. You can also put one line `base64:` plus the raw 32-byte seed.
- `TURNSTILE_SITEKEY` and `TURNSTILE_SECRET_KEY` turn on the bot check. Leave both empty and the check stays off.
- `NC_ADMIN_TOKEN` turns on `/admin`. One line is enough: `openssl rand -hex 32`. Leave it empty and `/admin` stays off.
- `NC_TESTER_TOKEN` is the value for the `X-NC-Tester` header. It skips only the per-network 10-minute and daily limits on `/api/perform`, `/api/gather`, and `/api/keep-take`. Generate it with `openssl rand -hex 32`. Leave it empty and every request stays on the public limits.
- `NC_PROCESSED` opens `POST /api/perform-processed`. Leave it unset. Gather and Perform stay raw assembly.

Stop the server with Ctrl-C, or `docker compose down` from the `service` directory. `docker compose down -v` also deletes the audio volume.

### Point storage at S3 or R2

Set these in `.env` if you do not want the Docker volume:

```
NC_STORAGE=s3://your-bucket
NC_S3_ENDPOINT=https://ACCOUNT.r2.cloudflarestorage.com
NC_S3_ACCESS_KEY_ID=...
NC_S3_SECRET_ACCESS_KEY=...
NC_S3_REGION=auto
```

The bucket name in `NC_STORAGE` is the bucket. The endpoint is the account endpoint, without the bucket in the path.

### Run the Python process without Docker

From the repository root, with Python 3.10+, ffmpeg, and the rubberband filter:

```
pip install -e .
pip install -e service
python3 -m nc_perform
```

Same environment variables. Files land in `/data` unless you set `NC_STORAGE=file:///some/folder` and `NC_DATA_DIR` to that folder. This path is how the tests run. The image adds Dragonfly Reverb and pedalboard, which are separate GPL-3.0 tools (see `NOTICE`). Without them the reverb hack uses the synthetic hall and the record says so.

### Move a kit record onto your instance

Render with the kit, then send the record to whatever host you choose. There is no default host.

```
nc perform composition.md --stub -o out/
nc submit out/performance.record.json --audio-dir out/ --api http://localhost:8080
```

Or set `NC_PERFORM_API`. The instance runs `nc verify-record`. If the check passes, you get a `k-` id. The note on the page says the render was verified by hash and not re-rendered. This instance does not sign it.

## R2 retention

Do not add a bucket lifecycle rule that expires every object under `performances/`. That prefix holds records as well as audio, and a lifecycle rule cannot see which objects are unlisted.

The service does that itself. A read of expired audio returns 410, and a daily job deletes expired FLAC and MP3 objects. Records stay. Failed job notes under `jobs/` are deleted after 7 days.

Object lock is optional and has to be turned on when the bucket is created. This repository does not turn it on.

## Owner checklist: go live on Cloudflare

The code is already in this repo. The workflow does not run on push. Nothing here deploys by itself.

Do these steps once, in order.

1. Use a Cloudflare account that can run Workers Paid features (Containers, Queues, R2). In the Cloudflare dashboard, add budget notifications at **$20** and **$50**, email only.
2. Confirm the R2 bucket **`nc-performances`** exists. Do not attach a lifecycle rule that deletes the whole bucket or the `performances/` prefix.
3. In the xAI console, create an API key and set a monthly limit of **$20**. You will paste the key into GitHub, not into the repo.
4. Generate the signing key on your own machine. Do not commit the file. `*.pem` is gitignored.

   ```
   openssl genpkey -algorithm ED25519 -out nc-signing-private.pem
   ```

   The Worker secret is the PEM text, including the BEGIN and END lines. The public key is derived from it and is published at `/.well-known/nc-record-keys.json` after deploy. You do not upload the public key separately.
5. In this GitHub repository, open Settings → Secrets and variables → Actions. Add these repository secrets if they are not already there:

   | Secret | What to do |
   |---|---|
   | `CLOUDFLARE_API_TOKEN` | Already created. Needs permission to deploy Workers, Containers, Queues, and R2. The workflow stops with an error if this is missing. |
   | `CLOUDFLARE_ACCOUNT_ID` | Already created. The workflow stops with an error if this is missing. |
   | `TURNSTILE_SECRET_KEY` | Add this. It is the secret half of the widget whose site key is `0x4AAAAAAFbvFabFcwn6LUki`. If you leave it empty, the workflow skips it and the bot check stays off. |
   | `XAI_API_KEY` | Add this. The key from step 3. If you leave it empty, the workflow skips it and planning stays on the stub. |
   | `FREESOUND_API_KEY` | Add this when you want the official Freesound API. If you leave it empty, the workflow skips it and search falls back to the Freesound HTML page. |
   | `NC_SIGNING_KEY` | Add this. The PEM from step 4. If you leave it empty, the workflow skips it and hosted records stay unsigned. |
   | `NC_ADMIN_TOKEN` | Add this. Generate it with `openssl rand -hex 32` and paste that one line. It is the Bearer token for `/admin`. If you leave it empty, the workflow skips it and `/admin` stays off. |
   | `NC_TESTER_TOKEN` | Add this yourself. Generate it with `openssl rand -hex 32` and paste that one line into the GitHub secret only. It is the value of the `X-NC-Tester` header, and it skips only the per-network 10-minute and daily limits. If you leave it empty, the workflow skips it and every visitor stays on the public limits. Do not paste it into chat. |

   Never paste those values into a chat, an issue, or a commit.
6. Deploy by hand: Actions → **Deploy Perform** → **Run workflow**. The workflow checks out the repo, refuses to continue when `CLOUDFLARE_API_TOKEN` or `CLOUDFLARE_ACCOUNT_ID` is missing, runs `wrangler deploy` from `service/worker`, then sets `XAI_API_KEY`, `FREESOUND_API_KEY`, `TURNSTILE_SECRET_KEY`, `NC_SIGNING_KEY`, `NC_ADMIN_TOKEN`, and `NC_TESTER_TOKEN` only when that GitHub secret has a value. It has no push trigger.
7. When the run finishes, the log prints a `*.workers.dev` URL. That is the site. Open it and run one short Composition.
8. Leave the DNS for netcomposer.art alone. The Worker does not require that domain. A custom hostname can be attached later if the DNS moves to Cloudflare.

`NC_INSTANCE_NAME` is already set in `service/worker/wrangler.jsonc` to `netcomposer.art hosted Performer (nc-performer 0.2.0)`. Change that variable in a later deploy if you want a different public name. The Turnstile site key in that file is public by design.

The container type is `standard-3` (2 vCPU, 8 GiB, 16 GB disk). That is the smallest type that can load Dragonfly and the Python audio stack. `basic` is 1 GiB and `standard-1` is 4 GiB. While the instance is awake the active rate is about **$0.00006 per second** (about $0.22 per hour): 2 × $0.000020 per vCPU-second plus 8 × $0.0000025 per GiB-second. It sleeps 15 minutes after the last request, and that idle time is billed too.

A failed Perform should say `Something went wrong while performing. Please try again. (job id)`. The page reads that sentence from the job JSON. To capture the live error, open Actions → **Diagnose Perform** → **Run workflow**, then press Perform on the site. The workflow prints container status and tails `nc-perform-api` for 5 minutes. It uses the same Cloudflare secrets as deploy and does not print them. It does not deploy. Wrangler has no stored container log, so the tail is the log.

## How a model call works

Raw assembly does not call the model. On the processed path, only the container calls the model, and only when the words need it and the soft budget still has room.

`XAI_API_KEY` selects `https://api.x.ai/v1` and `grok-4.3`. The kit function is `ncperf.interpret.llm`. It sends `POST {NC_LLM_BASE_URL}/chat/completions` with `Authorization: Bearer <key>`. The service wraps that post only to read `usage` for the cost log. Prices used for the estimate are $1.25 / $0.20 cached / $2.50 per million tokens, from the xAI model docs (https://docs.x.ai/docs/models), overridable with `NC_LLM_USD_PER_TOKEN_IN`, `NC_LLM_USD_PER_TOKEN_CACHED`, and `NC_LLM_USD_PER_TOKEN_OUT`. An explicit `NC_LLM_BASE_URL` wins over the xAI default, which is how tests substitute a mock. `NC_LLM_EVERY_PERFORM` defaults off.
