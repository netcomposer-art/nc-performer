"""Send a kit record to a Perform instance.

``nc submit out/name.record.json --audio-dir out/ --api http://localhost:8080``

The instance runs the same checks as ``nc verify-record``. If they pass, the
record gets a ``k-`` id and a page marked as a kit render. This instance does
not sign it. There is no default server: set ``--api`` or ``NC_PERFORM_API``
so a self-hosted record can move to any instance, including one that is not
netcomposer.art.
"""
import json, os, sys, urllib.request


def submit(record_path, audio_dir, api, plan_path=None, timeout=120):
    api = api.rstrip("/")
    rec = json.load(open(record_path, encoding="utf-8"))
    outputs = (rec.get("performance") or {}).get("outputs") or []
    boundary = "nc-submit-boundary"
    chunks = []

    def add(name, filename, data, content_type):
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n".encode() + data + b"\r\n"
        )

    add("record", "record.json", open(record_path, "rb").read(), "application/json")
    plan = plan_path or os.path.join(audio_dir or ".", "plan.json")
    if plan and os.path.exists(plan):
        add("plan", "plan.json", open(plan, "rb").read(), "application/json")
    if not audio_dir:
        audio_dir = os.path.dirname(os.path.abspath(record_path))
    for o in outputs:
        name = os.path.basename(o.get("file") or "")
        path = os.path.join(audio_dir, name)
        if not os.path.exists(path):
            sys.exit(f"missing {path} (the record lists it)")
        ctype = "audio/flac" if name.endswith(".flac") else "audio/mpeg" if name.endswith(".mp3") else "application/octet-stream"
        add(name, name, open(path, "rb").read(), ctype)
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    req = urllib.request.Request(
        api + "/api/submit", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())
