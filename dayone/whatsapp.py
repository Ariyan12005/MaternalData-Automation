"""WhatsApp Cloud API adapter (Meta): inbound webhook, media download, outbound messages.

Configured from the environment; without ``WHATSAPP_APP_SECRET`` the webhook is
closed and the browser simulator stays the only channel.

    WHATSAPP_VERIFY_TOKEN     token typed in the Meta app's webhook settings
    WHATSAPP_APP_SECRET       app secret: signs every webhook call (X-Hub-Signature-256)
    WHATSAPP_ACCESS_TOKEN     system-user token for the Graph API
    WHATSAPP_PHONE_NUMBER_ID  the business number that sends acknowledgments
    WHATSAPP_GRAPH_URL        default https://graph.facebook.com/v21.0
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("dayone.whatsapp")

MAX_SEND_ATTEMPTS = 5
NOT_A_PHOTO_REPLY = "Merci d'envoyer uniquement des photos des pages de la fiche."


@dataclass(frozen=True)
class WhatsAppConfig:
    verify_token: str
    app_secret: str
    access_token: str
    phone_number_id: str
    graph_url: str = "https://graph.facebook.com/v21.0"

    @classmethod
    def from_env(cls) -> "WhatsAppConfig | None":
        if not os.environ.get("WHATSAPP_APP_SECRET"):
            return None
        return cls(
            verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN", ""),
            app_secret=os.environ["WHATSAPP_APP_SECRET"],
            access_token=os.environ.get("WHATSAPP_ACCESS_TOKEN", ""),
            phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
            graph_url=os.environ.get("WHATSAPP_GRAPH_URL", cls.graph_url).rstrip("/"),
        )


class GraphError(Exception):
    pass


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float = 15):
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read()
    except (urllib.error.URLError, OSError) as exc:
        raise GraphError(str(exc)) from exc


class WhatsAppChannel:
    def __init__(self, config: WhatsAppConfig, service, transport=urllib_transport):
        self.config = config
        self.service = service
        self.transport = transport

    # ------------------------------------------------------------ webhook

    def verify_subscription(self, mode: str | None, token: str | None, challenge: str | None) -> str | None:
        """Meta's GET handshake: echo the challenge only for the right verify token."""
        if mode == "subscribe" and token and challenge and self.config.verify_token and \
                hmac.compare_digest(token, self.config.verify_token):
            return challenge
        return None

    def signature_valid(self, body: bytes, header: str | None) -> bool:
        expected = "sha256=" + hmac.new(self.config.app_secret.encode(), body, hashlib.sha256).hexdigest()
        return bool(header) and hmac.compare_digest(header, expected)

    def handle_webhook(self, payload: dict) -> list[dict]:
        """Process every message of one webhook call; returns one result per message (for logs and tests)."""
        results = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                for message in change.get("value", {}).get("messages", []):
                    results.append(self._handle_message(message))
        return results

    def _handle_message(self, message: dict) -> dict:
        from .service import ServiceError

        sender_id = f"whatsapp:+{message.get('from', '')}"
        message_id = message.get("id")
        kind = message.get("type")
        try:
            if kind == "image":
                data = self._download(message["image"]["id"])
                media_ref = self.service.upload_photo(data)["media_ref"]
                captured_at = None
                if str(message.get("timestamp", "")).isdigit():
                    captured_at = datetime.fromtimestamp(int(message["timestamp"]), timezone.utc).isoformat()
                try:
                    result = self.service.ingest_photo(sender_id=sender_id, message_id=message_id, media_ref=media_ref,
                                                       captured_at=captured_at)
                except ServiceError as exc:
                    if exc.code != "INVALID_CAPTURE_TIME":
                        raise
                    # A photo is never refused for its timestamp (phone clock wrong); it is kept without one.
                    result = self.service.ingest_photo(sender_id=sender_id, message_id=message_id, media_ref=media_ref)
                return {"message_id": message_id, "outcome": "DUPLICATE" if result["duplicate"] else "PAGE",
                        "document_id": result["document_id"]}
            if kind == "text" and message.get("text", {}).get("body", "").strip().lower() == "fin":
                closed = self.service.close_sender_capture(sender_id)
                return {"message_id": message_id, "outcome": "CLOSED", "document_id": closed}
            self.service.queue_message(sender_id, NOT_A_PHOTO_REPLY)
            return {"message_id": message_id, "outcome": "IGNORED"}
        except ServiceError as exc:
            log.warning("whatsapp message %s refused: %s", message_id, exc.code)
            return {"message_id": message_id, "outcome": "REFUSED", "code": exc.code}
        except (GraphError, KeyError) as exc:
            log.warning("whatsapp media for %s not downloaded: %s", message_id, exc)
            return {"message_id": message_id, "outcome": "MEDIA_FAILED"}

    def _download(self, media_id: str) -> bytes:
        auth = {"Authorization": f"Bearer {self.config.access_token}"}
        status, body = self.transport("GET", f"{self.config.graph_url}/{media_id}", auth, None)
        if status != 200:
            raise GraphError(f"media lookup {status}")
        url = json.loads(body)["url"]
        status, data = self.transport("GET", url, auth, None)
        if status != 200:
            raise GraphError(f"media download {status}")
        return data

    # ------------------------------------------------------------ outbound

    def send_text(self, sender_id: str, body: str) -> str:
        number = sender_id.removeprefix("whatsapp:+")
        payload = json.dumps({"messaging_product": "whatsapp", "to": number, "type": "text",
                              "text": {"body": body}}).encode()
        status, response = self.transport(
            "POST", f"{self.config.graph_url}/{self.config.phone_number_id}/messages",
            {"Authorization": f"Bearer {self.config.access_token}", "Content-Type": "application/json"}, payload)
        if status != 200:
            raise GraphError(f"send {status}")
        return json.loads(response)["messages"][0]["id"]

    def deliver_pending(self) -> int:
        """Send queued outbound messages (acknowledgments, retake requests); failures are retried."""
        sent = 0
        for message in self.service.pending_outbound(MAX_SEND_ATTEMPTS):
            try:
                provider_id = self.send_text(message["sender_id"], message["body"])
            except (GraphError, KeyError, ValueError) as exc:
                self.service.mark_outbound(message["id"], failed=True, error=str(exc))
                continue
            self.service.mark_outbound(message["id"], provider_id=provider_id)
            sent += 1
        return sent
