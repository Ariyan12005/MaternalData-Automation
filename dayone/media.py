"""Encrypted originals with opaque refs; organizer assets are never overwritten."""
import hashlib
from pathlib import Path
from .security import Cipher

class MediaStore:
    def __init__(self, directory, cipher=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.cipher = cipher or Cipher()

    def put(self, data, suffix):
        if suffix not in (".jpg", ".jpeg", ".png"):
            raise ValueError("Only JPEG and PNG images are accepted")
        if not data or len(data) > 8 * 1024 * 1024:
            raise ValueError("Image must contain 1 byte to 8 MiB")
        if not (data.startswith(b"\xff\xd8\xff") if suffix != ".png" else data.startswith(b"\x89PNG\r\n\x1a\n")):
            raise ValueError("Image signature mismatch")
        digest = hashlib.sha256(data).hexdigest()
        ref = "secure/" + self.cipher.index(digest) + suffix
        destination = self.directory / (ref.split("/")[1] + ".enc")
        if not destination.exists():
            import os
            import tempfile
            fd, temporary = tempfile.mkstemp(dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(self.cipher.encrypt(data))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return ref

    def get(self, ref):
        import re
        if not re.fullmatch(r"secure/[0-9a-f]{64}\.(jpg|jpeg|png)", ref):
            raise ValueError("Invalid encrypted media reference")
        return self.cipher.decrypt((self.directory / (ref.split("/")[1] + ".enc")).read_bytes())


class MongoMediaStore:
    """Encrypted originals in Atlas, below BSON's document-size limit."""
    def __init__(self, database, cipher=None):
        self.collection = database["media_blobs"]
        self.cipher = cipher or Cipher()

    def put(self, data, suffix):
        if suffix not in (".jpg", ".jpeg", ".png") or not data or len(data) > 8 * 1024 * 1024:
            raise ValueError("JPEG/PNG image must be at most 8 MiB")
        if not (data.startswith(b"\xff\xd8\xff") if suffix != ".png" else data.startswith(b"\x89PNG\r\n\x1a\n")):
            raise ValueError("Image signature mismatch")
        ref = "secure/" + self.cipher.index(hashlib.sha256(data).hexdigest()) + suffix
        # Atomic insertion and majority acknowledgment precede the capture receipt.
        from pymongo.write_concern import WriteConcern
        self.collection.with_options(write_concern=WriteConcern("majority")).update_one(
            {"_id": ref}, {"$setOnInsert": {"payload": self.cipher.encrypt(data)}}, upsert=True)
        return ref

    def get(self, ref):
        import re
        if not re.fullmatch(r"secure/[0-9a-f]{64}\.(jpg|jpeg|png)", ref):
            raise ValueError("Invalid encrypted media reference")
        item = self.collection.find_one({"_id": ref})
        if item is None:
            raise FileNotFoundError("Encrypted image unavailable")
        return self.cipher.decrypt(item["payload"])
