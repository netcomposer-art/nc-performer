"""Choose stub or LLM. The LLM runs only when the words ask for something the
stub cannot do: a line, triplets, or sections. Otherwise the rule-based stub
runs, including when the LLM is down, over the daily soft budget, or unset.
"""
from __future__ import annotations

import hashlib
import json
import re

from .config import Settings

# The stub voices chords and a pad. It does not write a melody, triplets, or
# more than one section. These are the words that mean "the stub is not enough".
_LINE = re.compile(
    r"\b(melod(?:y|ic|ies)|motif|leitmotif|a line|the line|lead line|sing(?:ing)?|sung)\b",
    re.I,
)
_TRIPLET = re.compile(r"\btriplets?\b", re.I)
_SECTIONS = re.compile(
    r"\b(sections?|verses?|choruses?|bridges?|intros?|codas?|part [a-d]|movement)\b",
    re.I,
)


def needs_llm(text: str) -> bool:
    return bool(_LINE.search(text or "") or _TRIPLET.search(text or "") or _SECTIONS.search(text or ""))


def cache_key(words: str, seed: int, model: str) -> str:
    blob = json.dumps({"words": words, "seed": seed, "model": model}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def llm_cost(usage: dict, settings: Settings) -> float:
    """USD from an OpenAI-style usage object. Missing fields count as zero."""
    if not usage:
        return 0.0
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or usage.get("cached_tokens") or 0)
    cached = min(cached, prompt)
    fresh = prompt - cached
    return fresh * settings.llm_usd_in + cached * settings.llm_usd_cached + completion * settings.llm_usd_out


def compute_cost(cpu_s: float, wall_s: float, settings: Settings) -> float:
    """Estimate from the standard-3 rates in the hosted plan. Memory is billed
    on the provisioned size, so wall-clock counts, not the measured peak."""
    return cpu_s * settings.cpu_usd_s + settings.provisioned_gib * wall_s * settings.gib_usd_s
