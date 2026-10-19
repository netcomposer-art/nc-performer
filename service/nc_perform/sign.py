"""Ed25519 signatures over ``record_hash``.

The signature sits outside the hashed body, so signing does not change
``record_hash``. It says this instance rendered this exact record. It does
not say anything about a different instance.

``NC_SIGNING_KEY`` is a PKCS#8 PEM (``openssl genpkey -algorithm ED25519``)
or ``base64:`` plus the raw 32-byte seed. Unset means the record stays
UNSIGNED, which is also how kit-submitted records are stored.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


@dataclass
class Signer:
    key: Ed25519PrivateKey
    public: bytes
    key_id: str

    @classmethod
    def from_text(cls, text: str) -> "Signer":
        seed = seed_from_text(text)
        key = Ed25519PrivateKey.from_private_bytes(seed)
        public = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return cls(key=key, public=public, key_id=hashlib.sha256(public).hexdigest()[:16])

    def sign_hash(self, record_hash: str) -> str:
        sig = self.key.sign(record_hash.encode())
        return base64.b64encode(sig).decode()

    def jwk(self) -> dict:
        return {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": base64.urlsafe_b64encode(self.public).decode().rstrip("="),
            "kid": self.key_id,
            "use": "sig",
            "alg": "Ed25519",
        }


def seed_from_text(text: str) -> bytes:
    t = text.strip()
    if not t:
        raise ValueError("empty signing key")
    if t.startswith("base64:"):
        raw = base64.b64decode(t[len("base64:"):])
        if len(raw) != 32:
            raise ValueError("Ed25519 seed must be 32 bytes")
        return raw
    if "BEGIN" in t:
        key = serialization.load_pem_private_key(t.encode(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("NC_SIGNING_KEY is not an Ed25519 key")
        return key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    raw = base64.b64decode(t)
    if len(raw) == 32:
        return raw
    raise ValueError("NC_SIGNING_KEY must be PEM or base64: of a 32-byte seed")


def apply_signature(record: dict, signer: Signer | None, signed_by: str) -> dict:
    """Set ``signature``. Does not recompute or change ``record_hash``."""
    if signer is None:
        record["signature"] = {
            "status": "UNSIGNED",
            "signed_by": None,
            "algorithm": None,
            "key_id": None,
            "value": None,
            "note": "This instance has no NC_SIGNING_KEY. The record_hash still identifies the bytes.",
        }
        return record
    value = record.get("record_hash", {}).get("value")
    if not value:
        raise ValueError("record has no record_hash")
    record["signature"] = {
        "status": "SIGNED",
        "signed_by": signed_by,
        "algorithm": "Ed25519",
        "key_id": signer.key_id,
        "value": signer.sign_hash(value),
        "note": "Signature covers record_hash (hex, UTF-8). It says this instance rendered this exact record.",
    }
    return record


def verify_signature(record: dict, public: bytes) -> bool:
    sig = record.get("signature") or {}
    if sig.get("status") != "SIGNED" or sig.get("algorithm") != "Ed25519":
        return False
    value = (record.get("record_hash") or {}).get("value")
    if not value or not sig.get("value"):
        return False
    key = Ed25519PublicKey.from_public_bytes(public)
    try:
        key.verify(base64.b64decode(sig["value"]), value.encode())
        return True
    except Exception:
        return False
