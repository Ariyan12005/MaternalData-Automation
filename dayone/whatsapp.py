"""WhatsApp Cloud API adapter: webhook checks, durable inbound/outbound jobs and the Graph API client.

Simulator mode does not use this module. In cloud mode the webhook only records work; `WhatsAppCloud.tick()` does the
network calls (media download, sending) outside the HTTP request.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode, urlparse

from .service import INBOUND_MEDIA_PREFIX, Conflict, DayOneService, NotFound, ServiceError
from .store import Store, next_id

log = logging.getLogger("dayone.whatsapp")

WEBHOOK_PATH = "/webhooks/whatsapp"
MAX_WEBHOOK_BYTES = 3 * 1024 * 1024  # Meta: webhook payloads can be up to 3 MB
DEFAULT_MAX_MEDIA_BYTES = 5 * 1024 * 1024  # Meta: JPEG/PNG images up to 5 MB
MAX_API_RESPONSE_BYTES = 64 * 1024
IMAGE_TYPES = {"image/jpeg": (".jpg", b"\xff\xd8\xff"), "image/png": (".png", b"\x89PNG\r\n\x1a\n")}

# Meta error codes documented as "try again later"; 0/190 (token) recover once an admin renews the token.
TRANSIENT_GRAPH_CODES = {0, 2, 4, 190, 80007, 130429, 131000, 131016, 131056, 131057, 133004, 2494100}
MAX_INBOUND_ATTEMPTS = 10
MAX_OUTBOUND_ATTEMPTS = 10
RETRY_BASE_SECONDS = 30
RETRY_CAP_SECONDS = 3600

RESEND_PHOTO_TEXT = "Nous n'avons pas pu recevoir votre photo. Merci de la renvoyer."

REQUIRED_ENV = ("WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_ACCESS_TOKEN",
                "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_API_VERSION")
API_VERSION = re.compile(r"v\d+\.\d+")
MEDIA_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}")
CHALLENGE = re.compile(r"[A-Za-z0-9_.-]{1,256}")
ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
OUTBOUND_RANK = {"SENT": 1, "DELIVERED": 2, "READ": 3}
# Set by --hold-outbound and kept in the database: nothing queued in it is ever sent until discard-held clears it.
OUTBOUND_HOLD_KEY = "whatsapp_outbound_hold"


class ConfigError(Exception):
    pass


class CloudApiError(Exception):
    """A failed Graph API call. `code` is safe to store and log: it never contains URLs, tokens or payloads."""

    def __init__(self, code: str, *, transient: bool):
        super().__init__(code)
        self.code = code
        self.transient = transient


# ---------------------------------------------------------------------------------------------------------- config

@dataclass(frozen=True)
class CloudConfig:
    verify_token: str = field(repr=False)
    app_secret: str = field(repr=False)
    access_token: str = field(repr=False)
    phone_number_id: str
    api_version: str
    media_dir: Path
    graph_base_url: str = "https://graph.facebook.com"
    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES
    http_timeout: float = 10.0


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def media_dir_from_env(environ, repo_root: Path) -> Path:
    path = Path(environ.get("WHATSAPP_MEDIA_DIR", "").strip() or Path("var") / "media" / "whatsapp")
    return (path if path.is_absolute() else Path(repo_root) / path).resolve()


def load_config(environ, repo_root: Path) -> CloudConfig:
    missing = [name for name in REQUIRED_ENV if not environ.get(name, "").strip()]
    if missing:
        raise ConfigError("Variables d'environnement manquantes : " + ", ".join(missing))
    placeholders = [name for name in REQUIRED_ENV
                    if environ[name].strip().startswith("replace-with-") or set(environ[name].strip()) == {"0"}]
    if placeholders:
        raise ConfigError("Valeurs d'exemple de .env.example encore présentes : " + ", ".join(placeholders))
    version = environ["WHATSAPP_API_VERSION"].strip()
    if not API_VERSION.fullmatch(version):
        raise ConfigError("WHATSAPP_API_VERSION doit avoir la forme vNN.N (par exemple v26.0).")
    phone_number_id = environ["WHATSAPP_PHONE_NUMBER_ID"].strip()
    if not phone_number_id.isdigit():
        raise ConfigError("WHATSAPP_PHONE_NUMBER_ID doit être l'identifiant numérique du numéro (pas le numéro).")
    base = environ.get("WHATSAPP_GRAPH_BASE_URL", "").strip().rstrip("/") or "https://graph.facebook.com"
    parsed = urlparse(base)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback(parsed.hostname or "")):
        raise ConfigError("WHATSAPP_GRAPH_BASE_URL doit être en https (http seulement vers 127.0.0.1 pour les tests).")
    try:
        max_bytes = int(environ.get("WHATSAPP_MAX_MEDIA_BYTES", "").strip() or DEFAULT_MAX_MEDIA_BYTES)
        timeout = float(environ.get("WHATSAPP_HTTP_TIMEOUT_SECONDS", "").strip() or 10)
    except ValueError:
        raise ConfigError("WHATSAPP_MAX_MEDIA_BYTES et WHATSAPP_HTTP_TIMEOUT_SECONDS doivent être des nombres.") from None
    if not 1 <= max_bytes <= 100 * 1024 * 1024 or not 1 <= timeout <= 120:
        raise ConfigError("WHATSAPP_MAX_MEDIA_BYTES ou WHATSAPP_HTTP_TIMEOUT_SECONDS hors limites.")
    return CloudConfig(
        verify_token=environ["WHATSAPP_VERIFY_TOKEN"].strip(), app_secret=environ["WHATSAPP_APP_SECRET"].strip(),
        access_token=environ["WHATSAPP_ACCESS_TOKEN"].strip(), phone_number_id=phone_number_id,
        api_version=version, media_dir=media_dir_from_env(environ, repo_root), graph_base_url=base,
        max_media_bytes=max_bytes, http_timeout=timeout,
    )


def load_env_file(path: str | Path, environ=os.environ) -> list[str]:
    """KEY=VALUE lines; variables already set in the environment win. Returns the names loaded, never values."""
    loaded = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if ENV_KEY.fullmatch(key) and key not in environ:
            environ[key] = value
            loaded.append(key)
    return loaded


# ------------------------------------------------------------------------------------------------- webhook checks

def signature_valid(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):].strip().lower())


def subscription_challenge(verify_token: str, query: dict) -> str | None:
    def first(name):
        values = query.get(name) or [None]
        return values[0]

    mode, token, challenge = first("hub.mode"), first("hub.verify_token"), first("hub.challenge")
    if mode != "subscribe":
        reason = "hub.mode is not subscribe"
    elif challenge is None or not CHALLENGE.fullmatch(challenge):
        reason = "hub.challenge missing or malformed"
    elif token is None or not hmac.compare_digest(token.encode("utf-8"), verify_token.encode("utf-8")):
        reason = "hub.verify_token does not match WHATSAPP_VERIFY_TOKEN"
    else:
        return challenge
    log.warning("webhook verification refused: %s", reason)
    return None


def sender_id_for(phone: str) -> str | None:
    digits = re.sub(r"[\s+()-]", "", phone or "")
    return f"whatsapp:+{digits}" if digits.isdigit() and 6 <= len(digits) <= 15 else None


def recipient_for(sender_id: str) -> str:
    return sender_id.removeprefix("whatsapp:")


@dataclass
class InboundMessage:
    wa_message_id: str
    message_type: str
    phone: str
    media_id: str | None = None
    mime_type: str | None = None
    sha256: str | None = None
    context_id: str | None = None
    timestamp: str | None = None
    error_code: str | None = None


@dataclass
class StatusUpdate:
    wa_message_id: str
    status: str
    error_code: int | None = None


def parse_webhook(payload: dict, phone_number_id: str) -> tuple[list[InboundMessage], list[StatusUpdate], int]:
    """Messages and statuses addressed to our business number; returns (messages, statuses, skipped changes)."""
    messages, statuses, skipped = [], [], 0
    if payload.get("object") != "whatsapp_business_account":
        return messages, statuses, 1
    for entry in _list(payload.get("entry")):
        for change in _list(entry.get("changes") if isinstance(entry, dict) else None):
            value = change.get("value") if isinstance(change, dict) else None
            metadata = value.get("metadata") if isinstance(value, dict) else None
            if (change.get("field") != "messages" or not isinstance(metadata, dict)
                    or str(metadata.get("phone_number_id")) != phone_number_id):
                skipped += 1
                continue
            for error in _list(value.get("errors")):
                log.warning("webhook carried a platform error (code %s)", _int(error.get("code")) if isinstance(error, dict) else None)
            for item in _list(value.get("messages")):
                message = _parse_message(item)
                if message:
                    messages.append(message)
            for item in _list(value.get("statuses")):
                if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("status"), str):
                    errors = _list(item.get("errors"))
                    code = _int(errors[0].get("code")) if errors and isinstance(errors[0], dict) else None
                    statuses.append(StatusUpdate(item["id"], item["status"].upper(), code))
    return messages, statuses, skipped


def _parse_message(item) -> InboundMessage | None:
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("from"), str):
        return None
    message = InboundMessage(item["id"][:200], str(item.get("type") or "unknown")[:40], item["from"],
                             timestamp=item.get("timestamp") if isinstance(item.get("timestamp"), str) else None)
    context = item.get("context")
    if isinstance(context, dict) and isinstance(context.get("id"), str):
        message.context_id = context["id"][:200]
    errors = _list(item.get("errors"))
    if errors and isinstance(errors[0], dict):
        message.error_code = str(_int(errors[0].get("code")))
    image = item.get("image")
    if message.message_type == "image" and isinstance(image, dict) and isinstance(image.get("id"), str):
        message.media_id = image["id"]
        message.mime_type = image.get("mime_type") if isinstance(image.get("mime_type"), str) else None
        message.sha256 = image.get("sha256") if isinstance(image.get("sha256"), str) else None
    return message


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------------------------------- transport

class Transport(Protocol):
    def send_text(self, to: str, body: str, *, context_message_id: str | None = None) -> str:
        """Returns the WhatsApp message ID; raises CloudApiError."""


class MediaSource(Protocol):
    def fetch_image(self, media_id: str) -> tuple[bytes, str]:
        """Returns (bytes, mime type); raises CloudApiError."""


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old, target = urlparse(req.full_url), urlparse(new.full_url)
        if target.scheme != "https" and target.scheme != old.scheme:
            return None
        if (target.scheme, target.netloc) != (old.scheme, old.netloc):
            new.remove_header("Authorization")
        return new


class GraphClient:
    """Graph API over urllib: bounded reads, timeouts, no token on cross-host redirects."""

    def __init__(self, config: CloudConfig):
        self.config = config
        self._opener = urllib.request.build_opener(_StripAuthOnRedirect())

    def _url(self, *parts: str) -> str:
        return "/".join([self.config.graph_base_url, self.config.api_version, *(quote(p, safe="") for p in parts)])

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.config.access_token}"}

    def _open(self, request: urllib.request.Request, max_bytes: int, *, retry_not_found: bool = False):
        try:
            with self._opener.open(request, timeout=self.config.http_timeout) as response:
                length = _int(response.headers.get("Content-Length"))
                if length is not None and length > max_bytes:
                    raise CloudApiError("TOO_LARGE", transient=False)
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise CloudApiError("TOO_LARGE", transient=False)
                return response.headers, body
        except urllib.error.HTTPError as exc:
            graph_code = None
            try:
                graph_code = _int(json.loads(exc.read(MAX_API_RESPONSE_BYTES)).get("error", {}).get("code"))
            except (ValueError, AttributeError, OSError):
                pass
            finally:
                exc.close()
            transient = (exc.code >= 500 or exc.code == 429 or graph_code in TRANSIENT_GRAPH_CODES
                         or (retry_not_found and exc.code == 404))
            code = f"HTTP_{exc.code}" + (f"_{graph_code}" if graph_code is not None else "")
            raise CloudApiError(code, transient=transient) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, http.client.HTTPException):
            raise CloudApiError("NETWORK", transient=True) from None

    def fetch_image(self, media_id: str) -> tuple[bytes, str]:
        if not MEDIA_ID.fullmatch(media_id or ""):
            raise CloudApiError("BAD_MEDIA_ID", transient=False)
        url = self._url(media_id) + "?" + urlencode({"phone_number_id": self.config.phone_number_id})
        _, body = self._open(urllib.request.Request(url, headers=self._auth()), MAX_API_RESPONSE_BYTES)
        try:
            meta = json.loads(body)
            media_url, mime = meta["url"], str(meta.get("mime_type", "")).split(";")[0].strip().lower()
        except (ValueError, KeyError, TypeError):
            raise CloudApiError("BAD_MEDIA_RESPONSE", transient=False) from None
        if mime not in IMAGE_TYPES:
            raise CloudApiError("UNSUPPORTED_MEDIA_TYPE", transient=False)
        declared = _int(meta.get("file_size"))
        if declared is not None and declared > self.config.max_media_bytes:
            raise CloudApiError("TOO_LARGE", transient=False)
        if not self._trusted_download_url(media_url):
            raise CloudApiError("UNTRUSTED_MEDIA_URL", transient=False)
        # Meta: a failed download returns 404; query the media ID again for a fresh URL.
        _, data = self._open(urllib.request.Request(media_url, headers=self._auth()), self.config.max_media_bytes,
                             retry_not_found=True)
        return data, mime

    def _trusted_download_url(self, url) -> bool:
        if not isinstance(url, str):
            return False
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.netloc:
            return True
        return url.startswith(self.config.graph_base_url + "/") and urlparse(self.config.graph_base_url).scheme == "http"

    def send_text(self, to: str, body: str, *, context_message_id: str | None = None) -> str:
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "text",
                   "text": {"body": body}}
        if context_message_id:
            payload["context"] = {"message_id": context_message_id}
        request = urllib.request.Request(
            self._url(self.config.phone_number_id, "messages"), data=json.dumps(payload).encode("utf-8"),
            headers={**self._auth(), "Content-Type": "application/json"}, method="POST",
        )
        _, response = self._open(request, MAX_API_RESPONSE_BYTES)
        try:
            return str(json.loads(response)["messages"][0]["id"])
        except (ValueError, KeyError, IndexError, TypeError):
            raise CloudApiError("BAD_SEND_RESPONSE", transient=False) from None


# --------------------------------------------------------------------------------------------------------- adapter

def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(RETRY_BASE_SECONDS * 2 ** max(attempts - 1, 0), RETRY_CAP_SECONDS))


def _sha256_matches(declared: str | None, data: bytes) -> bool:
    """Meta sends a base64 SHA-256; anything that is not a 32-byte digest cannot be checked and is not held against us."""
    if not declared:
        return True
    digest = hashlib.sha256(data).digest()
    try:
        raw = base64.b64decode(declared, validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) == 32:
        return hmac.compare_digest(raw, digest)
    if re.fullmatch(r"[0-9a-fA-F]{64}", declared):
        return hmac.compare_digest(declared.lower(), digest.hex())
    return True


class WhatsAppCloud:
    def __init__(self, service: DayOneService, config: CloudConfig, *, transport: Transport | None = None,
                 media: MediaSource | None = None):
        self.service = service
        self.store: Store = service.store
        self.config = config
        graph = GraphClient(config) if transport is None or media is None else None
        self.transport = transport or graph
        self.media = media or graph

    # -- webhook (runs inside the HTTP request: database only, no network)

    def signature_valid(self, raw_body: bytes, header: str | None) -> bool:
        return signature_valid(self.config.app_secret, raw_body, header)

    def subscription_challenge(self, query: dict) -> str | None:
        return subscription_challenge(self.config.verify_token, query)

    def record_webhook(self, payload: dict) -> dict:
        messages, statuses, skipped = parse_webhook(payload, self.config.phone_number_id)
        now = self.service.clock()
        summary = {"queued": 0, "duplicates": 0, "ignored": 0, "rejected": 0, "statuses": 0, "skipped": skipped}
        with self.store.tx() as db:
            for message in messages:
                if db.execute("SELECT 1 FROM whatsapp_inbound WHERE wa_message_id = ?",
                              (message.wa_message_id,)).fetchone():
                    summary["duplicates"] += 1
                    continue
                sender_id = sender_id_for(message.phone)
                known = sender_id and db.execute(
                    "SELECT 1 FROM senders WHERE sender_id = ? AND channel = 'WHATSAPP'", (sender_id,)).fetchone()
                if not known:
                    status, sender_id = "REJECTED", None
                elif message.message_type != "image" or message.media_id is None:
                    status = "IGNORED"
                else:
                    status = "PENDING"
                summary[{"PENDING": "queued", "IGNORED": "ignored", "REJECTED": "rejected"}[status]] += 1
                pending = status == "PENDING"
                db.execute(
                    "INSERT INTO whatsapp_inbound(inbound_id, wa_message_id, message_type, sender_id, media_id, "
                    "mime_type, media_sha256, context_message_id, sent_at, received_at, status, next_attempt_at, "
                    "last_error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (next_id(db, "WIN"), message.wa_message_id, message.message_type, sender_id,
                     message.media_id if pending else None, message.mime_type if pending else None,
                     message.sha256 if pending else None, message.context_id if pending else None,
                     _unix_to_iso(message.timestamp), _iso(now), status, _iso(now) if pending else None,
                     message.error_code, _iso(now)),
                )
            for update in statuses:
                summary["statuses"] += self._apply_status(db, update, now)
        return summary

    def _apply_status(self, db, update: StatusUpdate, now: datetime) -> int:
        row = db.execute("SELECT * FROM whatsapp_outbound WHERE wa_message_id = ?", (update.wa_message_id,)).fetchone()
        if row is None or row["status"] == "FAILED":
            return 0
        if update.status == "FAILED" and row["status"] in ("DELIVERED", "READ"):
            return 0
        if update.status == "FAILED":
            code = f"STATUS_FAILED_{update.error_code}"
            if update.error_code in TRANSIENT_GRAPH_CODES and row["attempts"] < MAX_OUTBOUND_ATTEMPTS:
                db.execute("UPDATE whatsapp_outbound SET status = 'PENDING', wa_message_id = NULL, last_error = ?, "
                           "next_attempt_at = ?, updated_at = ? WHERE message_id = ?",
                           (code, _iso(now + _backoff(row["attempts"])), _iso(now), row["message_id"]))
            else:
                db.execute("UPDATE whatsapp_outbound SET status = 'FAILED', last_error = ?, updated_at = ? "
                           "WHERE message_id = ?", (code, _iso(now), row["message_id"]))
            return 1
        if OUTBOUND_RANK.get(update.status, 0) > OUTBOUND_RANK.get(row["status"], 0):
            db.execute("UPDATE whatsapp_outbound SET status = ?, updated_at = ? WHERE message_id = ?",
                       (update.status, _iso(now), row["message_id"]))
            return 1
        return 0

    # -- worker (network calls happen here, never inside the webhook request)

    def tick(self) -> dict:
        return {"inbound": self.process_inbound(), "outbound": self.deliver_outbound()}

    def process_inbound(self, limit: int = 10) -> int:
        now = self.service.clock()
        with self.store.read() as db:
            rows = db.execute(
                "SELECT * FROM whatsapp_inbound WHERE status = 'PENDING' AND next_attempt_at <= ? "
                "ORDER BY COALESCE(sent_at, received_at), inbound_id LIMIT ?", (_iso(now), limit)).fetchall()
        for row in rows:
            self._process_inbound(row)
        return len(rows)

    def _process_inbound(self, row) -> None:
        try:
            data, mime = self.media.fetch_image(row["media_id"])
            extension, magic = IMAGE_TYPES.get(mime, (None, None))
            if extension is None or not data.startswith(magic):
                raise CloudApiError("NOT_AN_IMAGE", transient=False)
            if not _sha256_matches(row["media_sha256"], data):
                raise CloudApiError("SHA256_MISMATCH", transient=True)
            media_ref = self._store_media(data, extension)
        except CloudApiError as exc:
            self._inbound_failed(row, exc)
            return

        retake_request_id = self._pending_retake_for(row["context_message_id"])
        try:
            try:
                result = self.service.ingest_photo(sender_id=row["sender_id"], message_id=row["wa_message_id"],
                                                   media_ref=media_ref, retake_request_id=retake_request_id)
            except (NotFound, Conflict):
                if retake_request_id is None:
                    raise
                # The request closed in the meantime: keep the photo as a normal page rather than drop it.
                result = self.service.ingest_photo(sender_id=row["sender_id"], message_id=row["wa_message_id"],
                                                   media_ref=media_ref)
        except ServiceError as exc:
            self._inbound_failed(row, CloudApiError(f"INGEST_{exc.code}", transient=False), notify=False)
            return
        now = self.service.clock()
        with self.store.tx() as db:
            db.execute("UPDATE whatsapp_inbound SET status = 'DONE', attempts = attempts + 1, media_ref = ?, "
                       "page_id = ?, last_error = NULL, next_attempt_at = NULL, updated_at = ? WHERE inbound_id = ?",
                       (media_ref, result["page_id"], _iso(now), row["inbound_id"]))
        log.info("inbound %s stored as %s", row["inbound_id"], result["page_id"])

    def _store_media(self, data: bytes, extension: str) -> str:
        if self.service.media_store is not None:
            return self.service.media_store.put(data, extension)
        name = hashlib.sha256(data).hexdigest() + extension
        target = self.config.media_dir / name
        try:
            if not target.is_file():
                self.config.media_dir.mkdir(parents=True, exist_ok=True)
                temporary = self.config.media_dir / f".{name}.{secrets.token_hex(4)}.tmp"
                with open(temporary, "xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
        except OSError:
            raise CloudApiError("MEDIA_STORAGE", transient=True) from None
        return INBOUND_MEDIA_PREFIX + name

    def _pending_retake_for(self, context_message_id: str | None) -> str | None:
        if not context_message_id:
            return None
        with self.store.read() as db:
            row = db.execute(
                "SELECT r.request_id FROM whatsapp_outbound o JOIN retake_requests r ON r.request_message_id = o.message_id "
                "WHERE o.wa_message_id = ? AND r.status = 'PENDING'", (context_message_id,)).fetchone()
        return row["request_id"] if row else None

    def _inbound_failed(self, row, error: CloudApiError, *, notify: bool = True) -> None:
        now = self.service.clock()
        attempts = row["attempts"] + 1
        retry = error.transient and attempts < MAX_INBOUND_ATTEMPTS
        with self.store.tx() as db:
            db.execute(
                "UPDATE whatsapp_inbound SET status = ?, attempts = ?, last_error = ?, next_attempt_at = ?, "
                "updated_at = ? WHERE inbound_id = ?",
                ("PENDING" if retry else "FAILED", attempts, error.code,
                 _iso(now + _backoff(attempts)) if retry else None, _iso(now), row["inbound_id"]),
            )
            if not retry and notify:
                db.execute("INSERT INTO messages(sender_id, direction, body, media_ref, created_at) "
                           "VALUES (?, 'OUT', ?, NULL, ?)", (row["sender_id"], RESEND_PHOTO_TEXT, _iso(now)))
        log.warning("inbound %s %s (%s, attempt %d)", row["inbound_id"], "will retry" if retry else "failed",
                    error.code, attempts)

    def deliver_outbound(self, limit: int = 20) -> int:
        if outbound_hold_active(self.store):
            return 0
        now = self.service.clock()
        with self.store.read() as db:
            rows = db.execute(
                "SELECT o.message_id, o.sender_id, o.attempts, m.body, wi.wa_message_id AS quoted "
                "FROM whatsapp_outbound o JOIN messages m ON m.id = o.message_id "
                "LEFT JOIN retake_requests r ON r.request_message_id = o.message_id "
                "LEFT JOIN pages p ON p.page_id = r.page_id "
                "LEFT JOIN whatsapp_inbound wi ON wi.wa_message_id = p.source_message_id "
                "WHERE o.status = 'PENDING' AND o.next_attempt_at <= ? ORDER BY o.message_id LIMIT ?",
                (_iso(now), limit)).fetchall()
        for row in rows:
            self._deliver(row)
        return len(rows)

    def _deliver(self, row) -> None:
        attempts = row["attempts"] + 1
        try:
            wa_message_id = self.transport.send_text(recipient_for(row["sender_id"]), row["body"],
                                                     context_message_id=row["quoted"])
        except CloudApiError as exc:
            now = self.service.clock()
            retry = exc.transient and attempts < MAX_OUTBOUND_ATTEMPTS
            with self.store.tx() as db:
                db.execute(
                    "UPDATE whatsapp_outbound SET status = ?, attempts = ?, last_error = ?, next_attempt_at = ?, "
                    "updated_at = ? WHERE message_id = ?",
                    ("PENDING" if retry else "FAILED", attempts, exc.code,
                     _iso(now + _backoff(attempts)) if retry else _iso(now), _iso(now), row["message_id"]),
                )
            log.warning("outbound message %s %s (%s, attempt %d)", row["message_id"],
                        "will retry" if retry else "failed", exc.code, attempts)
            return
        now = self.service.clock()
        with self.store.tx() as db:
            db.execute("UPDATE whatsapp_outbound SET status = 'SENT', attempts = ?, wa_message_id = ?, last_error = NULL, "
                       "updated_at = ? WHERE message_id = ?", (attempts, wa_message_id, _iso(now), row["message_id"]))
        log.info("outbound message %s accepted by the Cloud API", row["message_id"])


def _unix_to_iso(timestamp: str | None) -> str | None:
    seconds = _int(timestamp)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


# ------------------------------------------------------------------------------------------------ back-office reads

def delivery_overview(store: Store, limit: int = 50) -> dict:
    with store.read() as db:
        outbound = [dict(row) for row in db.execute(
            "SELECT o.message_id, s.label AS sender_label, m.body, o.status, o.attempts, o.last_error, "
            "o.next_attempt_at, o.updated_at FROM whatsapp_outbound o JOIN messages m ON m.id = o.message_id "
            "JOIN senders s ON s.sender_id = o.sender_id ORDER BY o.message_id DESC LIMIT ?", (limit,))]
        inbound = [dict(row) for row in db.execute(
            "SELECT inbound_id, message_type, status, attempts, last_error, page_id, received_at, updated_at "
            "FROM whatsapp_inbound ORDER BY inbound_id DESC LIMIT ?", (limit,))]
    return {"outbound": outbound, "inbound": inbound}


def hold_outbound(store: Store) -> None:
    with store.tx() as db:
        db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                   (OUTBOUND_HOLD_KEY, _iso(datetime.now(timezone.utc))))


def outbound_hold_active(store: Store) -> bool:
    with store.read() as db:
        return db.execute("SELECT 1 FROM settings WHERE key = ?", (OUTBOUND_HOLD_KEY,)).fetchone() is not None


def discard_held(store: Store) -> int:
    """Marks every unsent message FAILED and lifts the hold. There is deliberately no command that sends them."""
    now = _iso(datetime.now(timezone.utc))
    with store.tx() as db:
        discarded = db.execute("UPDATE whatsapp_outbound SET status = 'FAILED', last_error = 'DISCARDED_HELD', "
                               "updated_at = ? WHERE status = 'PENDING'", (now,)).rowcount
        db.execute("DELETE FROM settings WHERE key = ?", (OUTBOUND_HOLD_KEY,))
    return discarded


def register_sender(store: Store, *, phone: str, facility_id: str, label: str) -> str:
    sender_id = sender_id_for(phone)
    if sender_id is None:
        raise ValueError("Numéro invalide : indiquez le numéro international, par exemple +2126XXXXXXXX.")
    if not label.strip():
        raise ValueError("Libellé obligatoire.")
    with store.tx() as db:
        if db.execute("SELECT 1 FROM facilities WHERE facility_id = ?", (facility_id,)).fetchone() is None:
            raise ValueError(f"Établissement inconnu : {facility_id}")
        db.execute(
            "INSERT INTO senders(sender_id, facility_id, label, channel) VALUES (?, ?, ?, 'WHATSAPP') "
            "ON CONFLICT(sender_id) DO UPDATE SET facility_id = excluded.facility_id, label = excluded.label, "
            "channel = 'WHATSAPP'", (sender_id, facility_id, label.strip()))
    return sender_id


def _masked(sender_id: str) -> str:
    return sender_id[:len("whatsapp:+") + 3] + "•" * max(len(sender_id) - len("whatsapp:+") - 5, 0) + sender_id[-2:]


def main(argv: list[str] | None = None) -> int:
    from .server import REPO_ROOT, build_service

    parser = argparse.ArgumentParser(prog="python -m dayone.whatsapp", description="WhatsApp Cloud API utilities")
    parser.add_argument("--env-file", help="read KEY=VALUE settings from this file (existing variables win)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-config", help="validate the cloud settings without printing secrets")
    add = commands.add_parser("add-sender", help="allow a midwife's WhatsApp number to send photos")
    add.add_argument("--phone", required=True, help="international format, e.g. +2126XXXXXXXX")
    add.add_argument("--label", required=True)
    add.add_argument("--facility", default="FAC-SIDI-SMAIL")
    add.add_argument("--db", default=str(REPO_ROOT / "var" / "dayone.sqlite3"))
    discard = commands.add_parser("discard-held", help="never send the messages queued under --hold-outbound; "
                                                       "lift the hold for future messages")
    discard.add_argument("--db", default=str(REPO_ROOT / "var" / "dayone.sqlite3"))
    args = parser.parse_args(argv)
    if args.env_file:
        load_env_file(args.env_file)

    if args.command == "check-config":
        try:
            config = load_config(os.environ, REPO_ROOT)
        except ConfigError as exc:
            print(f"Configuration incomplète : {exc}", file=sys.stderr)
            return 1
        print(f"Configuration OK : API {config.api_version}, base {config.graph_base_url}, "
              f"médias dans {config.media_dir}, limite {config.max_media_bytes} octets, "
              f"délai {config.http_timeout:g} s. Les secrets ne sont pas affichés.")
        return 0

    service = build_service(args.db)
    service.ensure_seed()
    if args.command == "discard-held":
        try:
            discarded = discard_held(service.store)
        finally:
            service.store.close()
        print(f"{discarded} message(s) en attente marqué(s) FAILED (jamais envoyés) ; blocage des envois levé.")
        return 0
    try:
        sender_id = register_sender(service.store, phone=args.phone, facility_id=args.facility, label=args.label)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        service.store.close()
    print(f"Expéditeur WhatsApp enregistré : {_masked(sender_id)} (établissement {args.facility})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
