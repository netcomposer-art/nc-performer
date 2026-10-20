"""Job ids. ``h-`` is a hosted render, ``k-`` is a kit record checked and stored
here, ``g-`` is a Gather collection. 12 Crockford base32 characters is 60 bits:
unguessable, and safe in a URL.
"""
from __future__ import annotations

import hashlib
import re
import secrets

# Crockford base32, lowercase, no I L O U.
CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
ID_RE = re.compile(r"^[hkg]-[0-9abcdefghjkmnpqrstvwxyz]{12}$")


def new_id(prefix: str = "h") -> str:
    if prefix not in ("h", "k", "g"):
        raise ValueError("prefix must be h, k, or g")
    n = secrets.randbits(60)
    chars = []
    for _ in range(12):
        chars.append(CROCKFORD[n & 31])
        n >>= 5
    return prefix + "-" + "".join(reversed(chars))


def claim_token() -> str:
    return secrets.token_urlsafe(32)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()
