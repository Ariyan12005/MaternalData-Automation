"""Encryption at rest: AES-256-GCM with one data key, split into per-purpose keys.

The data key comes from ``DAYONE_DATA_KEY`` (32 bytes, URL-safe base64). Without
it, a key file is created next to the data with owner-only permissions, which
protects a copied database file but not a stolen disk: in production the key
must live outside ``var/`` (environment, secret manager).
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger("dayone.crypto")

MAGIC = b"DAY1"
NONCE_BYTES = 12
KEY_ENV = "DAYONE_DATA_KEY"


class DecryptionError(Exception):
    """Ciphertext was altered, truncated, or encrypted with another key."""


class Cipher:
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("AES-256 key must be 32 bytes")
        self._aead = AESGCM(key)

    def encrypt(self, data: bytes, context: bytes = b"") -> bytes:
        nonce = secrets.token_bytes(NONCE_BYTES)
        return MAGIC + nonce + self._aead.encrypt(nonce, data, context)

    def decrypt(self, blob: bytes, context: bytes = b"") -> bytes:
        if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + NONCE_BYTES + 16:
            raise DecryptionError("not an encrypted DayOne file")
        nonce = blob[len(MAGIC):len(MAGIC) + NONCE_BYTES]
        try:
            return self._aead.decrypt(nonce, blob[len(MAGIC) + NONCE_BYTES:], context)
        except InvalidTag as exc:
            raise DecryptionError("wrong key or corrupted file") from exc


def _derive(master: bytes, purpose: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=f"dayone/{purpose}".encode()).derive(master)


def load_master_key(var_dir: Path) -> bytes:
    """Data key from the environment, or from an owner-only key file created on first use."""
    encoded = os.environ.get(KEY_ENV)
    if encoded:
        key = base64.urlsafe_b64decode(encoded.encode() + b"=" * (-len(encoded) % 4))
        if len(key) != 32:
            raise ValueError(f"{KEY_ENV} must be 32 bytes in URL-safe base64")
        return key
    path = Path(var_dir) / "dayone.key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
        log.warning("created %s; set %s and keep the key outside var/ in production", path, KEY_ENV)
    encoded = path.read_text().strip()
    return base64.urlsafe_b64decode(encoded.encode() + b"=" * (-len(encoded) % 4))


def ciphers(master: bytes) -> dict[str, Cipher]:
    return {purpose: Cipher(_derive(master, purpose)) for purpose in ("database", "media", "outbox")}
