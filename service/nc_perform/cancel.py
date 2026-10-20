"""Drop a queued Perform, or stop one that is still running.

The token is the claim_token from POST /api/perform. Keep uses that same
secret, so a body may send it as claim_token or keep_token.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from ncperf.cancel import MESSAGE

_HASH = re.compile(r"^[0-9a-f]{64}$")


def token_from(body: dict | None) -> str:
    if not isinstance(body, dict):
        return ""
    for key in ("claim_token", "keep_token"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def wipe_performance(storage, job_id: str) -> None:
    """Remove every stored output for this id, including a record alias."""
    raw = storage.get(f"performances/{job_id}/record.json")
    if raw:
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            rec = None
        if isinstance(rec, dict):
            rh = (rec.get("record_hash") or {}).get("value")
            if isinstance(rh, str) and _HASH.match(rh):
                storage.delete(f"aliases/sha256/{rh}")
    for key in storage.list(f"performances/{job_id}/"):
        storage.delete(key)


def write_cancelled_status(storage, job_id: str) -> None:
    body = {
        "id": job_id,
        "state": "cancelled",
        "error": MESSAGE,
        "stage_line": MESSAGE,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    storage.put(f"jobs/{job_id}/status.json", json.dumps(body).encode(), "application/json")
