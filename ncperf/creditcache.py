"""Parsed supplier credits, saved so the same page is not fetched twice.

What is stored is the title, the author, the license, and the page, plus the
audio URL when the page already named it. The HTML is not stored. A lookup
that failed is not stored.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading

_LOCK = threading.Lock()
_MEM: dict[str, dict] = {}
_BUNDLED = os.path.join(os.path.dirname(__file__), "data", "credit-cache.json")


def reset() -> None:
    """Drop the in-process copy. Tests use this between caches."""
    with _LOCK:
        _MEM.clear()


def _path(key: str) -> str:
    from . import fetch
    folder = os.path.join(fetch.cache_dir(), "credits")
    os.makedirs(folder, exist_ok=True)
    name = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".json"
    return os.path.join(folder, name)


def _bundled() -> dict[str, dict]:
    if not os.path.isfile(_BUNDLED):
        return {}
    try:
        with open(_BUNDLED, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    rows = doc.get("credits") if isinstance(doc, dict) else None
    if not isinstance(rows, dict):
        return {}
    return {str(k): v for k, v in rows.items() if isinstance(v, dict)}


def get(key: str) -> dict | None:
    """Return a stored credit, or None when this key has not been saved."""
    if not key:
        return None
    with _LOCK:
        hit = _MEM.get(key)
    if hit is not None:
        return dict(hit)
    path = _path(key)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            doc = None
        if isinstance(doc, dict) and doc.get("title") and doc.get("license"):
            with _LOCK:
                _MEM[key] = doc
            return dict(doc)
    bundled = _bundled().get(key)
    if bundled and bundled.get("title") and bundled.get("license"):
        with _LOCK:
            _MEM[key] = bundled
        return dict(bundled)
    return None


def put(key: str, credit: dict) -> None:
    """Save one successful credit. Empty and failed lookups are not saved."""
    if not key or not isinstance(credit, dict):
        return
    if not str(credit.get("title") or "").strip() or not str(credit.get("license") or "").strip():
        return
    body = {k: v for k, v in credit.items() if v not in (None, "")}
    with _LOCK:
        prev = dict(_MEM.get(key) or {})
        if not prev:
            path = _path(key)
            if os.path.isfile(path):
                try:
                    with open(path, encoding="utf-8") as fh:
                        disk = json.load(fh)
                    if isinstance(disk, dict):
                        prev = disk
                except (OSError, json.JSONDecodeError):
                    prev = {}
        merged = dict(prev)
        merged.update(body)
        body = merged
        _MEM[key] = body
        path = _path(key)
        tmp = path + ".part"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(body, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
