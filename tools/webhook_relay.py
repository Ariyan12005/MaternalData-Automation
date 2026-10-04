"""Local relay between an HTTPS tunnel and the DayOne webhook listener, for Meta sandbox checks.

Only /webhooks/whatsapp is forwarded, byte for byte with its signature header, to the listener on
loopback. With --fail-first N, the first N deliveries that carry inbound messages are forwarded (and
stored by the listener) but Meta is answered 503, so Meta redelivers the same signed payload: a real
duplicate delivery. Logs hold status codes and a short body hash, never bodies or query strings.

The tunnel keeps pointing at port 8001; for the check, start DayOne with --webhook-port 8003 and run:

    python tools/webhook_relay.py --fail-first 1   # tunnel -> relay 127.0.0.1:8001 -> listener 127.0.0.1:8003
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

WEBHOOK_PATH = "/webhooks/whatsapp"
MAX_BODY_BYTES = 3 * 1024 * 1024
MAX_REPLY_BYTES = 64 * 1024
FORWARDED_HEADERS = ("Content-Type", "X-Hub-Signature-256")

log = logging.getLogger("webhook_relay")


def carries_inbound_messages(body: bytes) -> bool:
    try:
        return any(change["value"].get("messages")
                   for entry in json.loads(body)["entry"] for change in entry["changes"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def make_handler(target: str, fail_first: int):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    lock = threading.Lock()
    remaining = [fail_first]

    class Relay(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self._relay(None)

        def do_POST(self):
            length = self.headers.get("Content-Length", "")
            if not length.isdigit():
                return self._reply(411)
            if int(length) > MAX_BODY_BYTES:
                self.close_connection = True
                return self._reply(413)
            self._relay(self.rfile.read(int(length)))

        def _relay(self, body: bytes | None):
            if urlparse(self.path).path != WEBHOOK_PATH:
                return self._reply(404)
            headers = {name: self.headers[name] for name in FORWARDED_HEADERS if self.headers.get(name)}
            request = urllib.request.Request(target + self.path, data=body, headers=headers, method=self.command)
            try:
                with opener.open(request, timeout=15) as response:
                    status, data = response.status, response.read(MAX_REPLY_BYTES)
            except urllib.error.HTTPError as exc:
                status, data = exc.code, b""
                exc.close()
            except OSError:
                log.warning("%s %s: listener unreachable -> 502", self.command, WEBHOOK_PATH)
                return self._reply(502)
            answer = status
            if body is not None and status == 200 and carries_inbound_messages(body):
                with lock:
                    if remaining[0] > 0:
                        remaining[0] -= 1
                        answer = 503
            digest = hashlib.sha256(body).hexdigest()[:12] if body else "-"
            log.info("%s %s body=%s listener=%s answered=%s%s", self.command, WEBHOOK_PATH, digest, status, answer,
                     " (forced failure: Meta should redeliver)" if answer != status else "")
            self._reply(answer, data if answer == status else b"")

        def _reply(self, status: int, data: bytes = b""):
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_request(self, code="-", size="-"):
            pass

        def log_message(self, format, *args):
            # args can hold the request line, whose query string carries hub.verify_token.
            pass

    return Relay


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8001, help="loopback port the tunnel points to")
    parser.add_argument("--listener-port", type=int, default=8003, help="DayOne --webhook-port")
    parser.add_argument("--fail-first", type=int, default=0, metavar="N",
                        help="answer 503 to the first N stored inbound deliveries so Meta redelivers them")
    args = parser.parse_args(argv)
    if args.port == args.listener_port:
        parser.error("--port must differ from --listener-port")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                 make_handler(f"http://127.0.0.1:{args.listener_port}", args.fail_first))
    print(f"Relay http://127.0.0.1:{args.port}{WEBHOOK_PATH} -> listener port {args.listener_port}; "
          f"forced failures: {args.fail_first}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
