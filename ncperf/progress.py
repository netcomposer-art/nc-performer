"""What a running job tells the page. Only sounds that were actually chosen."""
from __future__ import annotations

STAGE_LINES = {
    "queued": "Queued",
    "gathering": "Gathering the sounds…",
    "gathered": "Gathered.",
    "planning": "Reading the notes…",
    "fetching": "Fetching the recordings…",
    "measuring": "Checking each note's pitch…",
    "rendering": "Playing the chords…",
    "reverb": "Adding the room…",
    "mastering": "Setting the loudness…",
    "verifying": "Checking the record…",
    "storing": "Storing the Performance…",
    "done": "Done",
    "failed": "Something went wrong.",
    "rejected": "This Performance was refused.",
    "cancelled": "Stopped.",
}

_ORIGIN = {
    "musicmatter": "Musicmatter",
    "freesound": "Freesound",
    "openverse": "Openverse",
    "internet_archive": "the Internet Archive",
    "wikimedia": "Wikimedia Commons",
    "commons": "Wikimedia Commons",
    "ccmixter": "ccMixter",
    "uiowa": "the University of Iowa",
    "uiowa_mis": "the University of Iowa",
    "supplier": "Musicmatter",
}


def origin_label(name: str | None) -> str:
    key = (name or "").strip().lower()
    return _ORIGIN.get(key, name or "")


NOTE_LINE = "Checking recorded notes…"


def finding_line(origin: str, words: list[str] | None, texture: bool = False) -> str:
    """A short line for the page, such as 'Finding a choir on Freesound…'."""
    where = origin_label(origin) or origin
    if texture:
        what = "a room tone"
    elif words:
        word = str(words[0]).strip()
        what = word if word.endswith("s") else f"a {word}"
    else:
        what = "sounds"
    return f"Finding {what} on {where}…"


def sound_row(title, supplier, license_id) -> dict | None:
    """One found sound. Missing title, supplier, or license means it is not listed."""
    if isinstance(license_id, dict):
        license_id = license_id.get("id") or license_id.get("name") or ""
    title = str(title or "").strip()
    supplier = str(supplier or "").strip()
    license_id = str(license_id or "").strip()
    if not title or not supplier or not license_id:
        return None
    return {"title": title, "supplier": supplier, "license": license_id}


def clip_row(sound) -> dict | None:
    """A chosen chord. Position and preview URL stay on the row the page fills."""
    if not isinstance(sound, dict):
        return None
    row = sound_row(sound.get("title"), sound.get("supplier"), sound.get("license"))
    if not row:
        return None
    if sound.get("chord") is not None and sound.get("chord") != "":
        try:
            row["chord"] = int(sound["chord"])
        except (TypeError, ValueError):
            pass
    for key in ("id", "preview_url", "symbol", "clip_id", "url", "author", "credit", "page"):
        value = sound.get(key)
        if value not in (None, ""):
            row[key] = str(value)
    for key in ("pos", "start_s", "end_s", "duration_s", "loudness_lufs", "gain_db"):
        value = sound.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        row[key] = int(value) if key == "pos" else float(value)
    return row


def with_preview(sound, job_id: str, base: str) -> dict | None:
    """Point a chosen clip at the preview route for this job."""
    row = clip_row(sound)
    if not row or not job_id:
        return row
    sid = row.get("id")
    if sid and not row.get("preview_url"):
        path = f"/api/jobs/{job_id}/audio/{sid}"
        root = (base or "").rstrip("/")
        row["preview_url"] = f"{root}{path}" if root else path
    return row


def _sound_key(row: dict):
    if "chord" in row:
        return ("chord", row["chord"])
    return ("hit", row["title"], row["supplier"], row["license"])


def merge_sounds(have: list | None, extra: list | None) -> list[dict]:
    order = []
    by = {}
    for row in list(have or []) + list(extra or []):
        if not isinstance(row, dict):
            continue
        clean = clip_row(row)
        if not clean:
            continue
        key = _sound_key(clean)
        if key not in by:
            order.append(key)
        # A later pick for the same chord replaces the one already on the list.
        by[key] = clean
    return [by[key] for key in order]


def sounds_from_sources(sources: dict | None) -> list[dict]:
    rows = []
    for src in (sources or {}).values():
        if not isinstance(src, dict):
            continue
        kind = src.get("kind") or src.get("origin")
        row = sound_row(src.get("title"), origin_label(kind), src.get("license"))
        if row:
            rows.append(row)
    return merge_sounds([], rows)


def _seed(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def public_fields(job: dict | None = None, status: dict | None = None, stage: str | None = None, position: int | None = None) -> dict:
    """The four fields the Create page reads. trimmed is a string or null."""
    job = job or {}
    status = status if isinstance(status, dict) else {}
    req = job.get("request") if isinstance(job.get("request"), dict) else {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    seed = _seed(status.get("seed", result.get("seed", req.get("seed"))))
    trimmed = status["trimmed"] if "trimmed" in status else result.get("trimmed")
    if not isinstance(trimmed, str) or not trimmed.strip():
        trimmed = None
    if isinstance(status.get("found_sounds"), list):
        sounds = merge_sounds(status.get("found_sounds"), [])
    else:
        sounds = merge_sounds(result.get("found_sounds"), [])
    stage_name = stage or job.get("stage") or status.get("state") or ""
    live = result.get("stage_line") or status.get("stage_line") or ""
    if stage_name == "queued" and position and not result.get("stage_line"):
        line = f"Queued (position {position})"
    elif isinstance(live, str) and live.strip():
        line = live.strip()
    else:
        line = STAGE_LINES.get(stage_name) or STAGE_LINES["planning"]
    return {
        "seed": seed,
        "trimmed": trimmed,
        "found_sounds": sounds,
        "stage_line": line,
    }


def view_from_job(job: dict | None) -> dict:
    """seed, trimmed (string or null), found_sounds, and stage_line for a status payload."""
    return public_fields(job, stage=(job or {}).get("stage"))
