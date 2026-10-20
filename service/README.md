# Perform (the hosted wrapper)

This folder is a small website in front of the NetComposer Performer kit in the parent directory.

You write a Composition. Perform searches openly licensed sounds across registered Suppliers and the open commons with no supplier preferred, checks the license, and renders a Performance. Pitch is never shifted, and this search does not time-stretch. The sound is shaped by **hacks** (trim, fade, reverb, a loudness limiter). The output is [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). The code is MIT, Copyright (c) 2026 Peter Semmelhack.

An optional `instrument` field on `POST /api/perform` is a timbre hint (`choir`, `synth`, `piano`). It is matched against every source. It does not select musicmatter. A chord strike is a whole-chord file of that chord, or the same chord assembled from single notes at their recorded pitch. One take keeps one ensemble and reuses those sounds across the chords. The next take, or Perform again, can choose a different ensemble. A Performance is not refused because only one or two origins answered. The record's `summary.source_mix` lists that strike share.

You can run it on your own computer. You do not need an account on netcomposer.art. A record made here, or with the kit, can move to any other instance with `nc submit`.

## What you are running

One program does the music. It is the `nc-perform` image. It calls the public kit (`nc compose`, `nc interpret`, `nc render`, `nc verify-record`).

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
- No pitch-shifting and no time-stretching. Clips are trimmed, layered, and crossfaded. Assembled chord tones keep the pitch they were recorded at and share one layer. A Performance can have at most 16 layers (`NC_MAX_LAYERS`); a plan past that is trimmed and the record says what was left out. A plan that sets `stretch_to_s` is refused.
- No `kind=url` sources. The service will not fetch an arbitrary URL.
- The transforms are called hacks.

AI planning runs only when the words ask for a line, triplets, or sections. Set `NC_LLM_EVERY_PERFORM=1` to send every Perform through the model. That flag defaults off, so the current behaviour stays until you turn it on. When the model is not used, phrase mapping still changes the plan: ring, space, distance, a plucked string, a fat synth, and "then" are read from the words. One take keeps one ensemble. Inside that ensemble the arrangement changes from chord to chord (who plays, when, how thick, how loud) unless the words ask for something steady, such as "repeat the same sounds over and over". Chord lengths vary too: some linger, some pass, some arrive early or late, unless the words lock a pulse ("steady pulse", "one chord per bar", "10 seconds per chord"). A written proportion, such as "second half", keeps that boundary and the total length, and the chords inside it still vary. The record's timeline and each sound's spans carry those real start and end times. A named voice that cannot play a chord is left out of that chord, and the record says which chords it missed, instead of swapping in a different instrument. Those choices are seeded and stored on `source_mix.arrangement` in the record. `summary.source_mix` on the record lists how many clips came from each origin. The hosted default model is `grok-4.3` at `https://api.x.ai/v1`. Set `XAI_API_KEY` and nothing else, and the kit posts to `{base}/chat/completions` with that model. If you set `NC_LLM_BASE_URL` yourself, that host is used instead. Tests point that URL at a local mock. They never call api.x.ai.

`GET /api/config` includes `llm_price`: the model name, USD per million tokens in, cached, and out, and the page those prices came from (`https://docs.x.ai/docs/models`, grok-4.3 list prices recorded in `service/nc_perform/config.py`: $1.25 / $0.20 cached / $2.50 per million). A finished job's status adds `cost_estimate` with that card. When the model actually ran, it also includes the token counts and `usd` (tokens times those prices). No typical token count is invented.

`GET /api/jobs/:id` always includes four fields the site reads while the job is running:

- `trimmed`: a plain-English note when the plan was shortened to fit a limit, or `null` when nothing was cut. The same note is copied into the record's known compromises. A musical surprise is not a trim.
- `seed`: the seed used for this take. An omitted seed is random. Pass `seed` when you want the same take again.
- `found_sounds`: `[{title, supplier, license}]`, filled as sources are actually found. Rows without a title, supplier, and license are left out. Nothing is invented.
- `stage_line`: a short sentence for the current stage, such as `Finding a choir on Freesound…`.

The signed record still carries `performance.timeline` and `sounds_used[].spans` as kit 0.2.0 writes them.

Freesound is queried through its official API when the Worker secret `FREESOUND_API_KEY` is set. If the secret is missing, or the API fails, the HTML search is used. The deploy workflow writes the secret only when it is non-empty.

A soft daily cap of $3 switches new jobs to the stub. A hard cap of $5 answers "Perform is resting until tomorrow."

Unlisted is the default. Unlisted audio is kept 90 days unless someone presses Keep. Public audio is kept, and it appears in the feed only after 24 hours. A takedown returns 410 and leaves a tombstone. The sealed copy of the record is under `sealed/<id>/`.

## API

| Method and path | What it does |
|---|---|
| `POST /api/perform` | Start a Performance. Body: `title`, `composer`, `composition`, `performance`, optional `plan`, `visibility` (`unlisted` or `public`), `seed`, `perform_again`, `turnstile_token`, `keep`. Returns `202` with `id`, `claim_token` (once), and `record_url`. |
| `GET /api/jobs/:id` | Status. `state` moves through queued, planning, fetching, measuring, rendering, reverb, mastering, verifying, storing, then done, failed, or rejected. The body always includes `trimmed` (string or null), `seed`, `found_sounds`, and `stage_line`. |
| `GET /api/jobs/:id/events` | The same status as a stream. |
| `GET /api/jobs/:id/files/:name` | FLAC, MP3, record, plan, and the other stored files. Expired or taken-down audio is `410`. |
| `POST /api/jobs/:id/keep` | Body `{claim_token}`. Keeps the audio. |
| `POST /api/jobs/:id/visibility` | Body `{claim_token, visibility}`. |
| `GET /records/:id` | The HTML page. |
| `GET /records/:id.json` | The record bytes. |
| `GET /records/sha256/:hash` | Redirect to the page for that `record_hash`. |
| `GET /api/verify/:id` | Hash, license, pitch, output checksums, and whether the signature matches. |
| `GET /.well-known/nc-record-keys.json` | The public Ed25519 key, when a signing key is set. |
| `POST /api/submit` | A kit record plus its audio. Checked, stored under a `k-` id, not re-rendered, not signed. |
| `GET /api/feed` | Public Performances whose 24 hour wait has passed. |
| `GET /api/config` | Site key, limits, whether a model key is configured. |
| `GET /health` | Liveness. |
| `GET /` | The Perform page. |
| `GET /admin` | Today's spend. Needs `Authorization: Bearer <NC_ADMIN_TOKEN>`. |
| `POST /admin/enabled` | Body `{enabled: true\|false}`. |
| `POST /admin/hide` | Body `{id}`. Removes it from the feed. |
| `POST /admin/takedown` | Body `{id, reason}`. `410` and a tombstone. |

`503` means resting or the queue is full. `429` means this network sent a short burst (more than 3 Performs in 10 seconds), or has hit 3 Performs in 10 minutes or 10 in a day. The 10-minute and daily windows are counted in the service's own state. `403` means the Turnstile check failed. Turnstile is off when `TURNSTILE_SECRET_KEY` is unset.

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

   Never paste those values into a chat, an issue, or a commit.
6. Deploy by hand: Actions → **Deploy Perform** → **Run workflow**. The workflow checks out the repo, refuses to continue when `CLOUDFLARE_API_TOKEN` or `CLOUDFLARE_ACCOUNT_ID` is missing, runs `wrangler deploy` from `service/worker`, then sets `XAI_API_KEY`, `FREESOUND_API_KEY`, `TURNSTILE_SECRET_KEY`, `NC_SIGNING_KEY`, and `NC_ADMIN_TOKEN` only when that GitHub secret has a value. It has no push trigger.
7. When the run finishes, the log prints a `*.workers.dev` URL. That is the site. Open it and run one short Composition.
8. Leave the DNS for netcomposer.art alone. The Worker does not require that domain. A custom hostname can be attached later if the DNS moves to Cloudflare.

`NC_INSTANCE_NAME` is already set in `service/worker/wrangler.jsonc` to `netcomposer.art hosted Performer (nc-performer 0.2.0)`. Change that variable in a later deploy if you want a different public name. The Turnstile site key in that file is public by design.

The container type is `standard-3` (2 vCPU, 8 GiB, 16 GB disk). That is the smallest type that can load Dragonfly and the Python audio stack. `basic` is 1 GiB and `standard-1` is 4 GiB. While the instance is awake the active rate is about **$0.00006 per second** (about $0.22 per hour): 2 × $0.000020 per vCPU-second plus 8 × $0.0000025 per GiB-second. It sleeps 15 minutes after the last request, and that idle time is billed too.

A failed Perform should say `Something went wrong while performing. Please try again. (job id)`. The page reads that sentence from the job JSON. To capture the live error, open Actions → **Diagnose Perform** → **Run workflow**, then press Perform on the site. The workflow prints container status and tails `nc-perform-api` for 5 minutes. It uses the same Cloudflare secrets as deploy and does not print them. It does not deploy. Wrangler has no stored container log, so the tail is the log.

## How a model call works

Only the container calls the model, and only when the words need it and the soft budget still has room.

`XAI_API_KEY` selects `https://api.x.ai/v1` and `grok-4.3`. The kit function is `ncperf.interpret.llm`. It sends `POST {NC_LLM_BASE_URL}/chat/completions` with `Authorization: Bearer <key>`. The service wraps that post only to read `usage` for the cost log. Prices used for the estimate are $1.25 / $0.20 cached / $2.50 per million tokens, from the xAI model docs (https://docs.x.ai/docs/models), overridable with `NC_LLM_USD_PER_TOKEN_IN`, `NC_LLM_USD_PER_TOKEN_CACHED`, and `NC_LLM_USD_PER_TOKEN_OUT`. An explicit `NC_LLM_BASE_URL` wins over the xAI default, which is how tests substitute a mock. `NC_LLM_EVERY_PERFORM` defaults off.
