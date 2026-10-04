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
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS item_status ("
            "id TEXT PRIMARY KEY, state TEXT NOT NULL, attempts INTEGER NOT NULL, "
            "info BLOB NOT NULL, created_at TEXT, updated_at TEXT)"
        )
        self.db.commit()

    def capture(self, sender_id, message_id, data, *, group_id=None, suffix=".jpg"):
        body = {"sender_id": sender_id, "message_id": message_id, "image_base64": base64.b64encode(data).decode(), "suffix": suffix, "group_id": group_id}
        key = self.cipher.index(message_id)
        self.db.execute("INSERT OR IGNORE INTO outbox VALUES (?, ?)", (key, self.cipher.seal(body)))
        info = {
            "message_id": message_id,
            "sender_id": sender_id,
            "page_id": None,
            "document_id": None,
            "last_error": None,
        }
        self.db.execute(
            "INSERT OR REPLACE INTO item_status (id, state, attempts, info, created_at, updated_at) "
            "VALUES (?, 'PENDING', 0, ?, datetime('now'), datetime('now'))",
            (key, self.cipher.seal(info)),
        )
        self.db.commit()
        return key

    def status(self, message_id_or_key: str) -> dict | None:
        key = message_id_or_key if message_id_or_key.startswith("idx_") else self.cipher.index(message_id_or_key)
        row = self.db.execute(
            "SELECT id, state, attempts, info, created_at, updated_at "
            "FROM item_status WHERE id = ?",
            (key,),
        ).fetchone()
        if not row:
            return None
        info = self.cipher.open(row[3])
        return {
            "id": row[0],
            "message_id": info.get("message_id"),
            "sender_id": info.get("sender_id"),
            "state": row[1],
            "attempts": row[2],
            "last_error": info.get("last_error"),
            "page_id": info.get("page_id"),
            "document_id": info.get("document_id"),
            "created_at": row[4],
            "updated_at": row[5],
        }

    def list_items(self, state: str | None = None) -> list[dict]:
        query = "SELECT id, state, attempts, info, created_at, updated_at FROM item_status"
        params = ()
        if state:
            query += " WHERE state = ?"
            params = (state,)
        query += " ORDER BY rowid"
        rows = self.db.execute(query, params).fetchall()
        items = []
        for r in rows:
            info = self.cipher.open(r[3])
            items.append({
                "id": r[0],
                "message_id": info.get("message_id"),
                "sender_id": info.get("sender_id"),
                "state": r[1],
                "attempts": r[2],
                "last_error": info.get("last_error"),
                "page_id": info.get("page_id"),
                "document_id": info.get("document_id"),
                "created_at": r[4],
                "updated_at": r[5],
            })
        return items

    def flush(self, send, close_group=None):
        delivered = 0
        for key, payload in self.db.execute("SELECT id, payload FROM outbox ORDER BY rowid").fetchall():
            row = self.db.execute("SELECT info FROM item_status WHERE id = ?", (key,)).fetchone()
            info = self.cipher.open(row[0]) if row else {}
            try:
                response = send(self.cipher.open(payload))
            except Exception as exc:
                info["last_error"] = f"{type(exc).__name__}: {str(exc)}"
                self.db.execute(
                    "UPDATE item_status SET state = 'FAILED', attempts = attempts + 1, "
                    "info = ?, updated_at = datetime('now') WHERE id = ?",
                    (self.cipher.seal(info), key),
                )
                self.db.commit()
                raise
            if not isinstance(response, dict) or not response.get("page_id"):
                info["last_error"] = "Backend did not acknowledge a persisted page"
                self.db.execute(
                    "UPDATE item_status SET state = 'FAILED', attempts = attempts + 1, "
                    "info = ?, updated_at = datetime('now') WHERE id = ?",
                    (self.cipher.seal(info), key),
                )
                self.db.commit()
                raise RuntimeError("Backend did not acknowledge a persisted page")
            body = self.cipher.open(payload)
            if close_group and body.get("group_id"):
                if not response.get("document_id"):
                    raise RuntimeError("Grouped capture response requires document_id")
                document_id = response["document_id"]
                self.db.execute("INSERT OR IGNORE INTO closures VALUES (?, ?)", (self.cipher.index(document_id), self.cipher.seal(document_id)))
            info["page_id"] = response.get("page_id")
            info["document_id"] = response.get("document_id")
            info["last_error"] = None
            self.db.execute(
                "UPDATE item_status SET state = 'SYNCED', "
                "attempts = attempts + 1, info = ?, updated_at = datetime('now') WHERE id = ?",
                (self.cipher.seal(info), key),
            )
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
