"""HTML for the record page and the small admin page. The create page is
service/web/index.html so the Worker and this process serve the same file.
"""
from __future__ import annotations

import html
import json

from . import keep as K


def record_page(rec: dict, meta: dict, job_id: str) -> str:
    p = rec.get("performance") or {}
    title = html.escape(p.get("title") or job_id)
    composer = html.escape(((p.get("composition_ref") or {}).get("composer") or {}).get("display_name") or "anonymous")
    license_uri = "https://creativecommons.org/licenses/by-sa/4.0/"
    attr = html.escape(rec.get("attribution") or "")
    rh = html.escape((rec.get("record_hash") or {}).get("value") or "")
    it = p.get("interpreter") or {}
    interp = html.escape(it.get("label") or it.get("model") or it.get("kind") or "")
    outs = p.get("outputs") or []
    loud = ""
    if outs:
        loud = html.escape(f"{outs[0].get('loudness_lufs_integrated')} LUFS, true peak {outs[0].get('true_peak_dbtp')} dBTP")
    sounds = []
    for s in rec.get("sounds_used") or []:
        sounds.append(
            f"<li>{html.escape(s.get('credit_line') or s.get('title') or s.get('key') or '')}</li>"
        )
    hacks = []
    for h in rec.get("hacks_chain") or []:
        tool = html.escape(str(h.get("tool") or h.get("type") or "hack"))
        engine = html.escape(str(h.get("engine") or ""))
        hacks.append(f"<li>{tool}{(' · ' + engine) if engine else ''}</li>")
    sig = rec.get("signature") or {}
    sig_line = html.escape(f"{sig.get('status')} {sig.get('algorithm') or ''} {sig.get('key_id') or ''} {sig.get('signed_by') or ''}")
    origin = html.escape(meta.get("origin") or "hosted")
    note = html.escape(meta.get("note") or "")
    noindex = meta.get("visibility") != "public" or meta.get("hidden")
    robots = '<meta name="robots" content="noindex">' if noindex else ""
    full = html.escape(json.dumps(rec, indent=1, ensure_ascii=False))
    jid = html.escape(job_id)
    dials = meta.get("live_dials") if isinstance(meta.get("live_dials"), dict) else None
    if dials:
        live = meta.get("live_audio") or {}
        player = f"/api/jobs/{jid}/files/kept.mp3" if live.get("mp3") else f"/api/jobs/{jid}/files/kept.wav"
        intro = (
            "<p>This is the kept sound with the Live dials. "
            + html.escape(K.AFTER_SIGNING)
            + "</p><p>Live dials: "
            + html.escape(K.dials_line(dials))
            + "</p>"
        )
        mp3_link = f'<a href="/api/jobs/{jid}/files/kept.mp3">Download dialed MP3</a>' if live.get("mp3") else ""
        downloads = (
            f'<a href="/api/jobs/{jid}/files/kept.wav">Download dialed WAV</a>'
            + mp3_link
            + f'<a href="/api/jobs/{jid}/files/performance.flac">Download original FLAC (signed render)</a>'
            + f'<a href="/api/jobs/{jid}/files/preview.mp3">Download original MP3 (signed render)</a>'
        )
    else:
        player = f"/api/jobs/{jid}/files/preview.mp3"
        intro = ""
        downloads = (
            f'<a href="/api/jobs/{jid}/files/performance.flac">Download FLAC</a>'
            f'<a href="/api/jobs/{jid}/files/preview.mp3">Download MP3</a>'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{robots}
<title>{title}</title>
<style>
  body {{ font: 16px/1.45 Georgia, serif; margin: 0; background: #f6f3ec; color: #1c1915; }}
  main {{ max-width: 42rem; margin: 0 auto; padding: 1.5rem 1rem 4rem; }}
  audio {{ width: 100%; margin: 1rem 0; }}
  a {{ color: #6b3a1f; }}
  .row {{ display: flex; flex-wrap: wrap; gap: .5rem; margin: .8rem 0; }}
  .row a, button {{ background: #1c1915; color: #f6f3ec; text-decoration: none; border: 0; border-radius: 999px; padding: .45rem .8rem; font: 14px sans-serif; cursor: pointer; }}
  .badge {{ font: 13px sans-serif; letter-spacing: .04em; text-transform: uppercase; }}
  details {{ margin-top: 1rem; }}
  pre {{ white-space: pre-wrap; font: 12px/1.4 ui-monospace, monospace; background: #fff; padding: .8rem; border-radius: 8px; }}
</style>
</head>
<body>
<main>
  <p class="badge"><a href="{license_uri}">CC BY-SA 4.0</a> · {origin}</p>
  <h1>{title}</h1>
  <p>Composed by {composer}. Interpreter: {interp}.</p>
  {intro}
  <audio controls src="{player}"></audio>
  <div class="row">
    {downloads}
    <a href="/records/{jid}.json">Download record</a>
    <a href="/api/jobs/{jid}/files/plan.json">Download plan</a>
    <button type="button" id="copy">Copy link</button>
  </div>
  <p>{loud}</p>
  <p>{attr}</p>
  {('<p>' + note + '</p>') if note else ''}
  <h2>Sounds</h2>
  <ul>{''.join(sounds)}</ul>
  <h2>Hacks</h2>
  <ul>{''.join(hacks)}</ul>
  <p>record_hash <code>{rh}</code></p>
  <p>Signature: {sig_line}</p>
  <p>Anyone can check this record with <code>nc verify-record</code>. The signature says which instance rendered it.</p>
  <details><summary>Full record</summary><pre>{full}</pre></details>
  <p>Peter Semmelhack · NetComposer</p>
</main>
<script>
document.getElementById("copy").onclick = () => navigator.clipboard.writeText(location.href);
</script>
</body>
</html>
"""


def admin_page(summary: dict, enabled: bool) -> str:
    body = html.escape(json.dumps(summary, indent=1))
    state = "on" if enabled else "off"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex">
<title>Perform admin</title>
<style>body{{font:15px/1.4 sans-serif;margin:1.5rem}} pre{{white-space:pre-wrap}}</style></head>
<body>
<h1>Perform admin</h1>
<p>Perform is <strong>{state}</strong>. Today's spend is against the soft and hard caps in the summary.</p>
<pre>{body}</pre>
</body></html>
"""
