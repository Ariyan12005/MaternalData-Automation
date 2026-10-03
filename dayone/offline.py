"""Encrypted edge outbox, independent of Atlas connectivity."""
import argparse
import base64
import json
import sqlite3
import uuid
from pathlib import Path
from urllib.request import Request, urlopen
from .security import Cipher

class OfflineQueue:
    def __init__(self, path, cipher=None):
        self.cipher = cipher or Cipher()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, payload BLOB NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS closures (id TEXT PRIMARY KEY, payload BLOB NOT NULL)")
        self.db.commit()

    def capture(self, sender_id, message_id, data, *, group_id=None, suffix=".jpg"):
        body = {"sender_id": sender_id, "message_id": message_id, "image_base64": base64.b64encode(data).decode(), "suffix": suffix, "group_id": group_id}
        key = self.cipher.index(message_id)
        self.db.execute("INSERT OR IGNORE INTO outbox VALUES (?, ?)", (key, self.cipher.seal(body)))
        self.db.commit()
        return key

    def flush(self, send, close_group=None):
        delivered = 0
        for key, payload in self.db.execute("SELECT id, payload FROM outbox ORDER BY rowid").fetchall():
            response = send(self.cipher.open(payload))
            if not isinstance(response, dict) or not response.get("page_id"):
                raise RuntimeError("Backend did not acknowledge a persisted page")
            body = self.cipher.open(payload)
            if close_group and body.get("group_id"):
                if not response.get("document_id"):
                    raise RuntimeError("Grouped capture response requires document_id")
                document_id = response["document_id"]
                self.db.execute("INSERT OR IGNORE INTO closures VALUES (?, ?)", (self.cipher.index(document_id), self.cipher.seal(document_id)))
            self.db.execute("DELETE FROM outbox WHERE id=?", (key,))
            self.db.commit()
            delivered += 1
        if close_group:
            for key, payload in self.db.execute("SELECT id, payload FROM closures").fetchall():
                response = close_group(self.cipher.open(payload))
                if not isinstance(response, dict) or not response.get("ok"):
                    raise RuntimeError("Capture closure was not acknowledged")
                self.db.execute("DELETE FROM closures WHERE id=?", (key,))
                self.db.commit()
        return delivered

    def close(self):
        self.db.close()

def main():
    from .config import load_config
    load_config()
    import os
    parser = argparse.ArgumentParser(description="Encrypted bridge capture and reconnect retry")
    parser.add_argument("--queue", default="var/offline.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("image")
    capture.add_argument("--sender", required=True)
    capture.add_argument("--message-id", default=None)
    capture.add_argument("--group", default=None)
    sync = sub.add_parser("sync")
    sync.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    queue = OfflineQueue(args.queue)
    try:
        if args.command == "capture":
            image = Path(args.image)
            queue.capture(args.sender, args.message_id or "edge." + uuid.uuid4().hex, image.read_bytes(), group_id=args.group, suffix=image.suffix.lower())
            print("Photo encrypted and queued locally; not yet delivered to platform")
        else:
            def send(body):
                request = Request(args.url.rstrip("/") + "/api/whatsapp/uploads", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ.get("DAYONE_API_TOKEN", "")})
                with urlopen(request, timeout=30) as response:
                    return json.load(response)
            def close_group(document_id):
                request = Request(args.url.rstrip("/") + "/api/documents/" + document_id + "/close", data=b"{}", headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ.get("DAYONE_API_TOKEN", "")})
                with urlopen(request, timeout=30) as response:
                    return json.load(response)
            print("Delivered pages:", queue.flush(send, close_group))
    finally:
        queue.close()

if __name__ == "__main__":
    main()
