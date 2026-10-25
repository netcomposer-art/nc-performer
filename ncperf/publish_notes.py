"""Upload sliced notes to the nc-performances bucket.

The GitHub workflow runs this. It uses the same Cloudflare token and account
id as Deploy Perform, through wrangler, and it does not print those values.
Uploading a key again replaces that object. Keys this run does not write are
left in place. FLAC masters are stored and are not given a public Worker route.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile

from .slicer import validate_manifest

BUCKET = "nc-performances"
OPUS_TYPE = "audio/ogg"
FLAC_TYPE = "audio/flac"
MANIFEST_TYPE = "application/json"
OPUS_CACHE = "public, max-age=31536000, immutable"
MANIFEST_CACHE = "public, max-age=300"
# The workflow's instrument names. Cello is the Iowa pizzicato library.
# bass is the fingered hollowbody. Publish keeps B1–A#4, MIDI 35–70.
LIBRARIES = {
    "guitar": "uiowa-guitar",
    "piano": "uiowa-piano",
    "cello": "uiowa-cello-pizz",
    "bass": "karoryfer-darkblack",
}
# Worker GET /api/notes/bass/{midi}.opus. B1 is 35 and A#4 is 70: 36 notes.
BASS_SLUG = "bass"
BASS_LO = 35
BASS_HI = 70
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRANGLER_CWD = os.path.join(ROOT, "service", "worker")


class Upload:
    def __init__(self, local: str, key: str, content_type: str, cache_control: str, role: str):
        self.local = local
        self.key = key
        self.content_type = content_type
        self.cache_control = cache_control
        self.role = role

    def size(self) -> int:
        return os.path.getsize(self.local)


def parse_instruments(text: str) -> list[str]:
    names = []
    for part in (text or "").split(","):
        name = part.strip().lower()
        if name and name not in names:
            names.append(name)
    if not names:
        known = ", ".join(LIBRARIES)
        raise SystemExit(f"instruments is empty. Known names: {known}.")
    unknown = [name for name in names if name not in LIBRARIES]
    if unknown:
        known = ", ".join(LIBRARIES)
        raise SystemExit(f"unknown instrument: {', '.join(unknown)}. Known names: {known}.")
    return names


def library_ids(names: list[str]) -> list[str]:
    return [LIBRARIES[name] for name in names]


def bass_midis() -> list[int]:
    return list(range(BASS_LO, BASS_HI + 1))


def check_bass_slug(notes: dict) -> None:
    """The bass publish is the 36 notes GET /api/notes/bass/{midi}.opus serves."""
    want = set(bass_midis())
    got = set()
    for key in notes:
        try:
            midi = int(key)
        except (TypeError, ValueError):
            raise SystemExit(f"bass note {key} is not a MIDI number") from None
        got.add(midi)
    if got == want:
        return
    missing = sorted(want - got)
    extra = sorted(got - want)
    parts = [f"bass must be the {len(want)} notes B1–A#4 (MIDI {BASS_LO}–{BASS_HI}) at /api/notes/bass/"]
    if missing:
        parts.append("missing " + ", ".join(str(midi) for midi in missing))
    if extra:
        parts.append("extra " + ", ".join(str(midi) for midi in extra))
    raise SystemExit("; ".join(parts))


def plan_uploads(out_dir: str) -> list[Upload]:
    """Opus, FLAC, and one manifest per instrument. Nothing else."""
    manifest_dir = os.path.join(out_dir, "manifests")
    if not os.path.isdir(manifest_dir):
        raise SystemExit(f"no manifests in {out_dir}")
    names = sorted(name for name in os.listdir(manifest_dir) if name.endswith(".json"))
    if not names:
        raise SystemExit(f"no manifests in {out_dir}")
    items: list[Upload] = []
    claimed: set[str] = set()
    for name in names:
        path = os.path.join(manifest_dir, name)
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        errs = validate_manifest(doc)
        if errs:
            raise SystemExit(f"{name} does not validate: " + "; ".join(errs))
        if doc.get("kind") != "notes":
            raise SystemExit(f"{name} is kind {doc.get('kind')}. This workflow publishes note files.")
        slug = str(doc.get("slug") or "")
        if name != slug + ".json":
            raise SystemExit(f"{name} does not match slug {slug}")
        notes = doc.get("notes") or {}
        if not notes:
            raise SystemExit(f"{name} has no notes")
        if slug == BASS_SLUG:
            check_bass_slug(notes)
        for midi, note in notes.items():
            for field, role, content_type in (("file", "opus", OPUS_TYPE), ("master", "flac", FLAC_TYPE)):
                key = str(note.get(field) or "")
                _require_key(slug, midi, key, role)
                local = os.path.join(out_dir, key)
                if not os.path.isfile(local):
                    raise SystemExit(f"missing {key}")
                if key in claimed:
                    raise SystemExit(f"duplicate key {key}")
                claimed.add(key)
                items.append(Upload(local, key, content_type, OPUS_CACHE, role))
        manifest_key = f"notes/{slug}/manifest.json"
        items.append(Upload(path, manifest_key, MANIFEST_TYPE, MANIFEST_CACHE, "manifest"))
    extras = _audio_on_disk(out_dir) - claimed
    if extras:
        raise SystemExit("files on disk are not in the manifest: " + ", ".join(sorted(extras)))
    return _ordered(items)


def _require_key(slug: str, midi, key: str, role: str) -> None:
    suffix = ".opus" if role == "opus" else ".flac"
    expected = f"notes/{slug}/{int(midi)}{suffix}"
    if key != expected:
        raise SystemExit(f"refusing key {key}; expected {expected}")


def _audio_on_disk(out_dir: str) -> set[str]:
    found = set()
    root = os.path.join(out_dir, "notes")
    if not os.path.isdir(root):
        return found
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".opus") or name.endswith(".flac"):
                full = os.path.join(dirpath, name)
                found.add(os.path.relpath(full, out_dir).replace(os.sep, "/"))
    return found


def _ordered(items: list[Upload]) -> list[Upload]:
    """Audio first, so a manifest never points at a file this run has not written yet."""
    return sorted(items, key=lambda item: (item.role == "manifest", item.key))


def put_argv(wrangler: list[str], item: Upload, bucket: str = BUCKET) -> list[str]:
    return [
        *wrangler,
        "r2", "object", "put", f"{bucket}/{item.key}",
        "--file", item.local,
        "--content-type", item.content_type,
        "--cache-control", item.cache_control,
        "--remote",
        "--force",
    ]


def get_argv(wrangler: list[str], key: str, dest: str, bucket: str = BUCKET) -> list[str]:
    return [*wrangler, "r2", "object", "get", f"{bucket}/{key}", "--file", dest, "--remote"]


def check_remote(expected: list[tuple[str, int]], found: list[tuple[str, int]]) -> list[str]:
    """Compare the objects just written with the files that were uploaded."""
    want = dict(expected)
    got = dict(found)
    errors = []
    if len(got) != len(want) or len(found) != len(expected):
        errors.append(f"count: expected {len(want)} objects, read back {len(got)}")
    for key, size in want.items():
        if key not in got:
            errors.append(f"missing: {key}")
        elif got[key] != size:
            errors.append(f"size: {key} local {size} bytes, remote {got[key]} bytes")
    for key in sorted(set(got) - set(want)):
        errors.append(f"extra: {key}")
    return errors


def summary_lines(items: list[Upload]) -> list[str]:
    by: dict[str, dict[str, int]] = {}
    order: list[str] = []
    for item in items:
        slug = item.key.split("/")[1]
        if slug not in by:
            by[slug] = {"opus_n": 0, "opus_b": 0, "flac_n": 0, "flac_b": 0, "manifest_b": 0}
            order.append(slug)
        row = by[slug]
        size = item.size()
        if item.role == "opus":
            row["opus_n"] += 1
            row["opus_b"] += size
        elif item.role == "flac":
            row["flac_n"] += 1
            row["flac_b"] += size
        else:
            row["manifest_b"] += size
    lines = []
    for slug in order:
        row = by[slug]
        lines.append(
            f"{slug}: {row['opus_n']} opus ({row['opus_b']} bytes), "
            f"{row['flac_n']} flac ({row['flac_b']} bytes), manifest {row['manifest_b']} bytes"
        )
    lines.append(f"verified {len(items)} objects, sizes match")
    return lines


def redact(text: str) -> str:
    out = text or ""
    for name in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        secret = os.environ.get(name) or ""
        if len(secret) >= 6:
            out = out.replace(secret, "[redacted]")
    return out


def publish(items: list[Upload], wrangler: list[str], run, work: str) -> list[str]:
    """Put every object, read it back, and require the same count and sizes.

    `run` takes an argument list and returns an object with returncode, stdout, stderr.
    A second run writes the same keys again.
    """
    if not items:
        raise SystemExit("nothing to upload")
    for item in items:
        result = run(put_argv(wrangler, item))
        if result.returncode != 0:
            raise SystemExit(f"upload failed: {item.key}\n{redact(result.stderr or result.stdout or '')}")
        print(f"uploaded {item.key} ({item.size()} bytes)", flush=True)
    found = []
    for item in items:
        dest = os.path.join(work, item.key.replace("/", "__"))
        result = run(get_argv(wrangler, item.key, dest))
        if result.returncode != 0 or not os.path.isfile(dest):
            raise SystemExit(f"read-back failed: {item.key}\n{redact(result.stderr or result.stdout or '')}")
        found.append((item.key, os.path.getsize(dest)))
    errors = check_remote([(item.key, item.size()) for item in items], found)
    if errors:
        raise SystemExit("verify failed:\n" + "\n".join(errors))
    lines = summary_lines(items)
    for line in lines:
        print(line, flush=True)
    return lines


def _run(argv: list[str]):
    return subprocess.run(argv, cwd=WRANGLER_CWD, capture_output=True, text=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish sliced notes to R2. Does not print Cloudflare secrets.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    libs = sub.add_parser("libraries", help="print the slicer library ids for an instrument list")
    libs.add_argument("--instruments", required=True)
    up = sub.add_parser("upload", help="upload notes/{slug} opus, flac, and manifest.json")
    up.add_argument("--out", required=True, help="directory written by nc slice")
    up.add_argument("--wrangler", default="npx wrangler", help="wrangler command, split on spaces")
    args = parser.parse_args(argv)
    if args.cmd == "libraries":
        for library_id in library_ids(parse_instruments(args.instruments)):
            print(library_id)
        return 0
    items = plan_uploads(args.out)
    wrangler = args.wrangler.split()
    with tempfile.TemporaryDirectory(prefix="nc-notes-") as work:
        publish(items, wrangler, _run, work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
