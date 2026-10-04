"""Hybrid media test: real Meta media API, locally simulated inbound webhook, nothing sent to any phone.

Works while the Meta app is unpublished. For each synthetic specimen image:

  [REAL META]  upload it to the test number (POST /{phone-number-id}/media) and read its metadata back;
  [SIMULATED]  sign a webhook in Meta's documented shape with the app secret and POST it to the local
               listener, as if the registered sender had sent that photo;
  [LOCAL]      wait for DayOne's worker to download the photo through the real Graph API and store the page.

--replay posts the identical signed body a second time (a duplicate delivery). The target database must have
been started with --hold-outbound, so the acknowledgments are queued but never sent. Output holds internal IDs,
status codes and booleans only: no token, signature, phone number or media URL. The webhook body is not saved.

    python -m dayone --env-file .env --whatsapp-mode cloud --db var/sandbox-hybrid.sqlite3 --port 8010 --webhook-port 8011 --hold-outbound
    python tools/hybrid_media_test.py --image "data/Paper Registry/dossiers_specimen_10_patientes-01.png" --replay
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dayone import whatsapp  # noqa: E402


def build_webhook(phone_number_id: str, sender_digits: str, media_id: str, mime: str, data: bytes,
                  wamid: str, timestamp: int) -> bytes:
    message = {"from": sender_digits, "id": wamid, "timestamp": str(timestamp), "type": "image",
               "image": {"mime_type": mime, "sha256": base64.b64encode(hashlib.sha256(data).digest()).decode(),
                         "id": media_id}}
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": phone_number_id},
             "contacts": [{"wa_id": sender_digits}], "messages": [message]}
    payload = {"object": "whatsapp_business_account",
               "entry": [{"id": "SIMULATED", "changes": [{"field": "messages", "value": value}]}]}
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def sign(app_secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _graph(config: whatsapp.CloudConfig, method: str, path: str, *, query: dict | None = None,
           body: bytes | None = None, content_type: str | None = None) -> dict:
    url = f"{config.graph_base_url.rstrip('/')}/{config.api_version}/{path}" + (f"?{urlencode(query)}" if query else "")
    request = urllib.request.Request(url, data=body, method=method,
                                     headers={"Authorization": f"Bearer {config.access_token}"})
    if content_type:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=config.http_timeout * 3) as response:
            return json.loads(response.read(whatsapp.MAX_API_RESPONSE_BYTES))
    except urllib.error.HTTPError as exc:
        error = json.loads(exc.read(whatsapp.MAX_API_RESPONSE_BYTES) or b"{}").get("error", {})
        raise SystemExit(f"[REAL META] {method} {path.split('/')[-1]} failed: HTTP {exc.code}, "
                         f"code {error.get('code')}, {error.get('type')}") from None


def upload(config: whatsapp.CloudConfig, image: Path, mime: str) -> str:
    boundary = secrets.token_hex(16)
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="messaging_product"\r\n\r\nwhatsapp\r\n'.encode(),
             f'--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\n{mime}\r\n'.encode(),
             f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="specimen{image.suffix}"\r\n'
             f'Content-Type: {mime}\r\n\r\n'.encode() + image.read_bytes() + b"\r\n",
             f"--{boundary}--\r\n".encode()]
    return _graph(config, "POST", f"{config.phone_number_id}/media", body=b"".join(parts),
                  content_type=f"multipart/form-data; boundary={boundary}")["id"]


@contextmanager
def _db(path: Path):
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--image", action="append", required=True, type=Path,
                        help="synthetic specimen page (JPEG/PNG); repeat for several pages")
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "var" / "sandbox-hybrid.sqlite3",
                        help="database of the DayOne instance that owns the listener (must be held)")
    parser.add_argument("--webhook-url", default="http://127.0.0.1:8011" + whatsapp.WEBHOOK_PATH)
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--sender-label", help="registered WHATSAPP sender to impersonate when there are several")
    parser.add_argument("--replay", action="store_true", help="post each signed body twice (duplicate delivery)")
    parser.add_argument("--wait", type=float, default=60.0, help="seconds to wait for the worker per image")
    args = parser.parse_args(argv)

    target = urlparse(args.webhook_url)
    if target.scheme != "http" or not whatsapp.is_loopback(target.hostname or "") or target.path != whatsapp.WEBHOOK_PATH:
        parser.error(f"--webhook-url must be http://127.0.0.1:<port>{whatsapp.WEBHOOK_PATH}: the simulated "
                     "webhook goes to the local listener only")
    if not args.db.is_file():
        parser.error(f"database not found: {args.db}")
    for image in args.image:
        if not image.is_file() or mimetypes.guess_type(image.name)[0] not in ("image/jpeg", "image/png"):
            parser.error(f"not a JPEG/PNG file: {image}")
    whatsapp.load_env_file(args.env_file)
    try:
        config = whatsapp.load_config(os.environ, REPO_ROOT)
    except whatsapp.ConfigError as exc:
        parser.error(str(exc))

    with _db(args.db) as db:
        if db.execute("SELECT 1 FROM settings WHERE key = ?", (whatsapp.OUTBOUND_HOLD_KEY,)).fetchone() is None:
            parser.error("this database is not held: start its DayOne instance with --hold-outbound first, "
                         "so no acknowledgment can reach a phone")
        senders = db.execute("SELECT sender_id, label FROM senders WHERE channel = 'WHATSAPP' AND (? IS NULL OR label = ?)",
                             (args.sender_label, args.sender_label)).fetchall()
    if len(senders) != 1:
        parser.error(f"{len(senders)} matching WHATSAPP sender(s); register one with add-sender or pass --sender-label")
    sender_digits = whatsapp.recipient_for(senders[0]["sender_id"]).lstrip("+")
    print(f"Database held: yes. Simulated sender: '{senders[0]['label']}' (number not shown).")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for image in args.image:
        data, mime = image.read_bytes(), mimetypes.guess_type(image.name)[0]
        media_id = upload(config, image, mime)
        meta = _graph(config, "GET", media_id, query={"phone_number_id": config.phone_number_id})
        print(f"[REAL META] {image.name}: uploaded (media id ...{media_id[-4:]}); metadata {meta.get('mime_type')}, "
              f"size matches: {meta.get('file_size') == len(data)}, "
              f"sha256 matches: {str(meta.get('sha256', '')).lower() == hashlib.sha256(data).hexdigest()}")

        wamid = "wamid.SIMULATED-" + secrets.token_hex(12)
        body = build_webhook(config.phone_number_id, sender_digits, media_id, mime, data, wamid, int(time.time()))
        headers = {"Content-Type": "application/json", "X-Hub-Signature-256": sign(config.app_secret, body)}
        for attempt in range(2 if args.replay else 1):
            request = urllib.request.Request(args.webhook_url, data=body, headers=headers, method="POST")
            try:
                with opener.open(request, timeout=15) as response:
                    status = response.status
            except urllib.error.HTTPError as exc:
                status = exc.code
            print(f"[SIMULATED] signed webhook {'replayed' if attempt else 'posted'} to the local listener: HTTP {status}")

        deadline = time.monotonic() + args.wait
        while True:
            with _db(args.db) as db:
                jobs = db.execute("SELECT inbound_id, status, attempts, last_error, page_id FROM whatsapp_inbound "
                                  "WHERE wa_message_id = ?", (wamid,)).fetchall()
                page = db.execute("SELECT document_id, media_sha256 FROM pages WHERE source_message_id = ?",
                                  (wamid,)).fetchone()
            if (jobs and jobs[0]["status"] in ("DONE", "FAILED", "REJECTED", "IGNORED")) or time.monotonic() > deadline:
                break
            time.sleep(1)
        if not jobs:
            print("[LOCAL] no inbound job recorded: check the listener log")
            continue
        job = jobs[0]
        print(f"[LOCAL] inbound jobs for this message: {len(jobs)}; {job['inbound_id']} {job['status']} "
              f"after {job['attempts']} attempt(s){', error ' + job['last_error'] if job['last_error'] else ''}"
              + (f"; page {job['page_id']} in {page['document_id']}, stored bytes identical: "
                 f"{page['media_sha256'] == hashlib.sha256(data).hexdigest()}" if page else ""))

    with _db(args.db) as db:
        held = db.execute("SELECT COUNT(*) AS n, SUM(attempts) AS tries, COUNT(wa_message_id) AS sent "
                          "FROM whatsapp_outbound WHERE status = 'PENDING'").fetchone()
        other = db.execute("SELECT COUNT(*) FROM whatsapp_outbound WHERE status != 'PENDING'").fetchone()[0]
    nothing_sent = not held["tries"] and not held["sent"] and not other
    print(f"[LOCAL] outgoing messages: {held['n']} pending, {held['tries'] or 0} send attempts, "
          f"{held['sent']} WhatsApp message IDs; {other} in any other state. "
          + ("Nothing was sent." if nothing_sent else "WARNING: some messages left the queue, check the database."))
    return 0 if nothing_sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
