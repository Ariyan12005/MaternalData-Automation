"""HTTP adapter for a live extractor service. Prepared but not connected: the request format is not agreed yet.

Implemented is only what the shared contract documents: a successful call returns one complete schema v1.0
draft (docs/schema.md). How pages are sent, authentication and error bodies are open questions
(docs/extractor-contract.md), so the request encoder must be supplied explicitly and there is no default.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .extraction import ExtractionError, ExtractorUnavailable, check_live_draft
from .service import ServiceError

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 300.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TEMPORARY_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
BACKOFF_BASE_SECONDS = 5.0
BACKOFF_CAP_SECONDS = 300.0

# (page_ref, file) pairs in page order -> (body, headers). To be agreed with Fatma before any live call.
RequestEncoder = Callable[[list[tuple[str, Path]]], tuple[bytes, dict[str, str]]]


class ExtractorConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ExtractorConfig:
    url: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_extractor_config(environ) -> ExtractorConfig:
    url = environ.get("DAYONE_EXTRACTOR_URL", "").strip()
    if not url:
        raise ExtractorConfigError("DAYONE_EXTRACTOR_URL manquant.")
    parsed = urlparse(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ExtractorConfigError("DAYONE_EXTRACTOR_URL doit être une URL http(s) sans identifiants, "
                                   "paramètres ni fragment.")
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise ExtractorConfigError("DAYONE_EXTRACTOR_URL : http n'est accepté que sur 127.0.0.1 ; utilisez https.")
    raw_timeout = environ.get("DAYONE_EXTRACTOR_TIMEOUT_SECONDS", "").strip()
    try:
        timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        raise ExtractorConfigError("DAYONE_EXTRACTOR_TIMEOUT_SECONDS doit être un nombre de secondes.") from None
    if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise ExtractorConfigError(f"DAYONE_EXTRACTOR_TIMEOUT_SECONDS doit être entre 0 et {MAX_TIMEOUT_SECONDS:g}.")
    return ExtractorConfig(url=url, timeout=timeout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpExtractor:
    """Same interface as FixtureExtractor: extract(page_refs) returns a draft or raises."""

    name = "http"

    def __init__(self, config: ExtractorConfig, *, resolve_media: Callable[[str], Path],
                 encode_request: RequestEncoder, monotonic: Callable[[], float] = time.monotonic):
        self.config = config
        self._resolve = resolve_media
        self._encode = encode_request
        self._monotonic = monotonic
        self._failures = 0
        self._retry_at = 0.0
        handlers = [_NoRedirect()]
        if _is_loopback(urlparse(config.url).hostname or ""):
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    def extract(self, page_refs: list[str]) -> dict:
        if self._monotonic() < self._retry_at:
            raise ExtractorUnavailable("EXTRACTOR_BACKING_OFF")
        try:
            pages = [(ref, self._resolve(ref)) for ref in page_refs]
        except ServiceError as exc:
            raise ExtractionError(exc.code, exc.message) from None
        body, headers = self._encode(pages)
        request = urllib.request.Request(self.config.url, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.config.timeout) as response:
                status, content = response.status, response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in TEMPORARY_HTTP_STATUSES:
                self._unavailable(f"EXTRACTOR_HTTP_{exc.code}")
            self._recovered()
            raise ExtractionError(f"EXTRACTOR_HTTP_{exc.code}",
                                  f"L'extracteur a refusé ces pages (HTTP {exc.code}).") from None
        except urllib.error.URLError as exc:
            timed_out = isinstance(exc.reason, (TimeoutError, socket.timeout))
            self._unavailable("EXTRACTOR_TIMEOUT" if timed_out else "EXTRACTOR_UNREACHABLE")
        except (TimeoutError, socket.timeout):
            self._unavailable("EXTRACTOR_TIMEOUT")
        except (OSError, http.client.HTTPException):
            self._unavailable("EXTRACTOR_UNREACHABLE")
        self._recovered()
        if status != 200:
            raise ExtractionError(f"EXTRACTOR_HTTP_{status}", f"Réponse inattendue de l'extracteur (HTTP {status}).")
        return self._draft(content, page_refs)

    def _unavailable(self, code: str):
        self._failures += 1
        self._retry_at = self._monotonic() + min(BACKOFF_BASE_SECONDS * 2 ** (self._failures - 1), BACKOFF_CAP_SECONDS)
        raise ExtractorUnavailable(code)

    def _recovered(self) -> None:
        self._failures, self._retry_at = 0, 0.0

    @staticmethod
    def _draft(content: bytes, page_refs: list[str]) -> dict:
        if len(content) > MAX_RESPONSE_BYTES:
            raise ExtractionError("EXTRACTOR_RESPONSE_TOO_LARGE", "Réponse de l'extracteur trop volumineuse.")
        try:
            draft = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            raise ExtractionError("EXTRACTOR_INVALID_JSON", "Réponse de l'extracteur illisible (JSON attendu).") from None
        return check_live_draft(draft, page_refs)
