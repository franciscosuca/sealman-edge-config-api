"""Key generation/hashing helpers for the extension system's internal (service-to-
service) key auth channel. The per-device field-ingress key channel is deliberately
not implemented (see IMPLEMENTATION-LOG.md), so these are only used for the internal
key today. Pure functions only — no FastAPI, no DB access.
"""

import hashlib
import hmac
import secrets


def generate_key() -> str:
    return secrets.token_urlsafe(32)


def hash_key(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def keys_match(raw: str, stored_hash: str) -> bool:
    if not raw or not stored_hash:
        return False
    return hmac.compare_digest(hash_key(raw), stored_hash)
