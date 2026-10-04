"""Storage for photos received from phones (camera captures, WhatsApp media).

Each photo is checked to be a JPEG or PNG, stored once under its SHA-256, and
encrypted when a cipher is given. References look like ``upload/<sha256>.jpg``;
the read-only dataset under ``data/Paper Registry/`` is never written to.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .crypto import Cipher

MAX_PHOTO_BYTES = 12 * 1024 * 1024
UPLOAD_PREFIX = "upload/"
_REF = re.compile(r"upload/([0-9a-f]{64})\.(jpg|png)")


class MediaError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def sniff(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return None


class MediaStore:
    def __init__(self, root: Path, cipher: Cipher | None = None):
        self.root = Path(root)
        self.cipher = cipher

    def _path(self, ref: str) -> Path:
        match = _REF.fullmatch(ref)
        if not match:
            raise MediaError("MEDIA_NOT_FOUND", "Image introuvable.")
        return self.root / f"{match.group(1)}.{match.group(2)}{'.enc' if self.cipher else ''}"

    def put(self, data: bytes) -> str:
        if not data:
            raise MediaError("MEDIA_EMPTY", "Photo vide.")
        if len(data) > MAX_PHOTO_BYTES:
            raise MediaError("MEDIA_TOO_LARGE", "Photo trop volumineuse (12 Mo maximum).")
        extension = sniff(data)
        if extension is None:
            raise MediaError("MEDIA_TYPE", "Seules les photos JPEG ou PNG sont acceptées.")
        ref = f"{UPLOAD_PREFIX}{hashlib.sha256(data).hexdigest()}.{extension}"
        path = self._path(ref)
        if not path.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            blob = self.cipher.encrypt(data, ref.encode()) if self.cipher else data
            temporary = path.with_suffix(path.suffix + ".tmp")
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as stream:
                stream.write(blob)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        return ref

    def get(self, ref: str) -> bytes:
        path = self._path(ref)
        if not path.is_file():
            raise MediaError("MEDIA_NOT_FOUND", "Image introuvable.")
        blob = path.read_bytes()
        return self.cipher.decrypt(blob, ref.encode()) if self.cipher else blob

    @staticmethod
    def is_upload(ref: object) -> bool:
        return isinstance(ref, str) and _REF.fullmatch(ref) is not None
