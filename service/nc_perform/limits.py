"""Request and plan limits. The kit checks licenses again at fetch time.
This module rejects plans the kit should never see: kind=url (SSRF), sources
outside the curated library, and anything over the size caps.
"""
from __future__ import annotations

import json
import os
import urllib.request
from urllib.parse import urlparse

from ncperf.schema import validate as schema_validate

from .config import Settings

_LIB = None


class Reject(Exception):
    """The job is not started. ``code`` is a short machine string."""

    def __init__(self, message: str, code: str = "rejected"):
        super().__init__(message)
        self.code = code


def library() -> dict:
    global _LIB
    if _LIB is None:
        import ncperf
        path = os.path.join(os.path.dirname(ncperf.__file__), "data", "library.json")
        _LIB = json.load(open(path, encoding="utf-8"))
    return _LIB


def words_of(comp: dict) -> str:
    return ((comp.get("composition") or "") + "\n" + (comp.get("performance") or "")).strip()


def check_words(comp: dict, settings: Settings) -> None:
    text = words_of(comp)
    if not (comp.get("composition") or "").strip() and not comp.get("title"):
        raise Reject("Give the Composition words (the chords, the form, the length).")
    if len(text) > settings.max_words:
        raise Reject(f"Those words are {len(text)} characters. The limit is {settings.max_words}.")


def check_plan(plan: dict, settings: Settings, suppliers: dict | None) -> None:
    raw = json.dumps(plan, ensure_ascii=False).encode()
    if len(raw) > settings.max_plan_bytes:
        raise Reject(f"The plan is {len(raw)} bytes. The limit is {settings.max_plan_bytes}.")
    errs = schema_validate(plan)
    if errs:
        raise Reject("The plan does not match the schema:\n" + "\n".join(errs[:12]))
    if len(plan.get("sources") or {}) > settings.max_sources:
        raise Reject(f"A Performance can use at most {settings.max_sources} sources.")
    if len(plan.get("chords") or []) > settings.max_chords:
        raise Reject(f"A Performance can have at most {settings.max_chords} chords.")
    if len(plan.get("layers") or []) > settings.max_layers:
        raise Reject(f"A Performance can have at most {settings.max_layers} layers.")
    length = float(plan.get("length_s") or 0) + float(plan.get("tail_s") or 0)
    if length > settings.max_length_s:
        raise Reject(f"The music plus its tail is {length:.0f}s. The limit is {settings.max_length_s:.0f}s.")
    _check_sources(plan, settings, suppliers)


def _check_sources(plan: dict, settings: Settings, suppliers: dict | None) -> None:
    lib = library()
    freesound = set()
    uiowa = set()
    for src in lib.get("sources", {}).values():
        if src.get("kind") == "freesound" and src.get("id"):
            freesound.add(str(src["id"]))
        if src.get("kind") == "uiowa_mis" and src.get("url"):
            uiowa.add(src["url"])
    listed = _listed_manifests(suppliers)
    for sid, src in (plan.get("sources") or {}).items():
        kind = src.get("kind")
        if kind == "url":
            raise Reject(f"Source {sid} is kind=url. Hosted Perform does not fetch arbitrary URLs.")
        if kind == "freesound":
            if str(src.get("id")) not in freesound:
                raise Reject(f"Freesound id {src.get('id')} is not in the curated library.")
        elif kind == "uiowa_mis":
            if src.get("url") not in uiowa:
                raise Reject(f"Source {sid} is not a UIowa file from the curated library.")
            _need_host(src.get("url"), settings)
        elif kind == "supplier":
            manifest = src.get("manifest") or ""
            if not manifest:
                raise Reject(f"Source {sid} needs a Supplier manifest.")
            _need_host(manifest, settings)
            if listed is not None and manifest not in listed:
                raise Reject(f"Manifest is not listed by an active Supplier: {manifest}")
        else:
            raise Reject(f"Source {sid} has an unknown kind {kind!r}.")


def _need_host(url: str, settings: Settings) -> None:
    host = (urlparse(url).hostname or "").lower()
    if not settings.host_allowed(host):
        raise Reject(f"Host is not on the allowlist: {host}")


def _listed_manifests(suppliers: dict | None) -> set[str] | None:
    """Manifest URLs listed by active Suppliers. None means the registry was
    not loaded and the kit's own live check is the backstop."""
    if not suppliers:
        return None
    out = set()
    for s in suppliers.get("suppliers", []):
        if s.get("status") != "active":
            continue
        for o in (s.get("_offerings") or []):
            if o.get("manifest"):
                out.add(o["manifest"])
    return out


def load_suppliers(settings: Settings) -> dict:
    """Registry plus each active Supplier's offerings. Hosts are checked first."""
    url = settings.suppliers_json
    _need_host(url, settings)
    doc = json.loads(_get(url))
    for s in doc.get("suppliers", []):
        if s.get("status") != "active" or not s.get("index"):
            continue
        _need_host(s["index"], settings)
        idx = json.loads(_get(s["index"]))
        s["_offerings"] = idx.get("offerings") or []
    return doc


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "nc-perform/0.2 (+https://github.com/netcomposer-art/nc-performer)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")
