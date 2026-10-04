"""Delivery of registered visits to a central registry (REGISTERED -> SYNCED / SYNC_FAILED).

The payload is anonymized: internal patient and visit IDs, normalized values,
status and verification per field. No raw OCR text, no link keys, no names.

Two registries: ``HttpCentralRegistry`` posts JSON signed with HMAC-SHA256 to
``DAYONE_CENTRAL_URL``; ``LocalCentralRegistry`` stands in for it in the demo
(an encrypted append-only file under ``var/central/``) and can be switched off
to show a sync failure and its recovery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

RETRY_BASE_SECONDS = 5
RETRY_MAX_SECONDS = 300


class CentralUnavailable(Exception):
    """The central registry could not be reached or refused the delivery; it will be retried."""


def retry_delay(attempts: int) -> int:
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** max(0, attempts - 1))


def build_payload(document: dict, registration: dict, visits: list[dict]) -> dict:
    return {
        "idempotency_key": f"{registration['document_id']}:{registration['registered_at']}",
        "document_id": registration["document_id"],
        "facility_id": document["facility_id"],
        "patient_id": registration["patient_id"],
        "registered_at": registration["registered_at"],
        "visits": [
            {
                "visit_id": visit["visit_id"], "encounter_type": visit["encounter_type"],
                "visit_date": visit["visit_date"], "slot": visit["slot"],
                "fields": {name: {"value": field["value"], "field_status": field["field_status"],
                                  "verification": field["verification"]["state"]}
                           for name, field in visit["fields"].items()},
            }
            for visit in visits
        ],
    }


class LocalCentralRegistry:
    """Simulated central registry: encrypted JSON lines, deduplicated by idempotency key."""

    mode = "local"

    def __init__(self, path: Path, cipher=None, available=lambda: True):
        self.path = Path(path)
        self.cipher = cipher
        self.available = available
        self._lock = threading.Lock()

    def _records(self) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_bytes().splitlines():
            if not line:
                continue
            data = bytes.fromhex(line.decode()) if self.cipher else line
            records.append(json.loads(self.cipher.decrypt(data, b"central") if self.cipher else data))
        return records

    def deliver(self, payload: dict) -> dict:
        if not self.available():
            raise CentralUnavailable("registre central injoignable (simulation)")
        with self._lock:
            for record in self._records():
                if record["payload"]["idempotency_key"] == payload["idempotency_key"]:
                    return {**record["receipt"], "duplicate": True}
            receipt = {"receipt_id": "CEN-" + hashlib.sha256(payload["idempotency_key"].encode()).hexdigest()[:12],
                       "visits": len(payload["visits"])}
            line = json.dumps({"payload": payload, "receipt": receipt}, ensure_ascii=False).encode()
            if self.cipher:
                line = self.cipher.encrypt(line, b"central").hex().encode()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab") as stream:
                stream.write(line + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        return {**receipt, "duplicate": False}

    def count(self) -> int:
        return len(self._records())


class HttpCentralRegistry:
    """Real endpoint: POST JSON with an HMAC-SHA256 signature and an idempotency key."""

    mode = "http"

    def __init__(self, url: str, secret: str, timeout: float = 10):
        self.url = url
        self.secret = secret.encode()
        self.timeout = timeout

    def deliver(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        signature = hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        request = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Idempotency-Key": payload["idempotency_key"],
            "X-DayOne-Signature": f"sha256={signature}",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise CentralUnavailable(str(exc)) from exc
