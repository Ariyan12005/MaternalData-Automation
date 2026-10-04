"""The real HTTP server: sessions, roles, CSRF header, security headers."""

import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from dayone.server import Api, build_service, make_handler

SPECIMEN = "data/Paper Registry/dossiers_specimen_10_patientes-{:02d}.png"


class HttpTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        key = base64.urlsafe_b64encode(bytes(range(32))).decode()
        with patch.dict(os.environ, {"DAYONE_DATA_KEY": key}):
            self.service = build_service(Path(self.tmp.name) / "db.sqlite3", grouping_window_seconds=0)
        self.service.reset_demo()
        self.service.auth.create_user("admin", "admin-password-1", "admin")
        self.service.auth.create_user("amina", "reviewer-pass-1", "reviewer")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Api(self.service)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.service.store.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, *, cookie=None, csrf=True):
        headers = {"Content-Type": "application/json"}
        if csrf:
            headers["X-Requested-With"] = "dayone"
        if cookie:
            headers["Cookie"] = cookie
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read() or b"null"), response.headers
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"null"), error.headers

    def login(self, username, password):
        status, user, headers = self.call("POST", "/api/login", {"username": username, "password": password})
        self.assertEqual(status, 200, user)
        return headers["Set-Cookie"].split(";")[0]


class AuthHttpTest(HttpTestCase):
    def test_api_requires_a_session(self):
        self.assertEqual(self.call("GET", "/api/documents")[0], 401)
        self.assertEqual(self.call("GET", "/api/documents", cookie="dayone_session=forged")[0], 401)

    def test_session_cookie_is_http_only_and_strict(self):
        status, user, headers = self.call("POST", "/api/login", {"username": "amina", "password": "reviewer-pass-1"})
        self.assertEqual((status, user["role"]), (200, "reviewer"))
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_post_without_csrf_header_is_refused(self):
        cookie = self.login("admin", "admin-password-1")
        status, body, _ = self.call("POST", "/api/demo/reset", {}, cookie=cookie, csrf=False)
        self.assertEqual((status, body["error"]["code"]), (403, "CSRF"))

    def test_reviewer_cannot_do_admin_actions(self):
        reviewer = self.login("amina", "reviewer-pass-1")
        for path in ("/api/demo/reset", "/api/system/ai", "/api/users"):
            with self.subTest(path=path):
                self.assertEqual(self.call("POST", path, {}, cookie=reviewer)[0], 403)
        admin = self.login("admin", "admin-password-1")
        self.assertEqual(self.call("POST", "/api/demo/reset", {}, cookie=admin)[0], 200)

    def test_reviewer_identity_comes_from_the_session(self):
        cookie = self.login("amina", "reviewer-pass-1")
        sender = self.call("GET", "/api/senders", cookie=cookie)[1][0]["sender_id"]
        for number, page in enumerate((1, 2, 3), start=1):
            status, result, _ = self.call("POST", "/api/whatsapp/messages", {
                "sender_id": sender, "message_id": f"wamid.http-{number}", "media_ref": SPECIMEN.format(page)}, cookie=cookie)
            self.assertEqual(status, 200, result)
        self.service.close_capture(result["document_id"])
        self.service.tick()
        status, detail, _ = self.call("POST", f"/api/documents/{result['document_id']}/fields", {
            "scope": "encounter", "encounter_index": 4, "field": "fundal_height_cm", "action": "CORRECT", "value": "31"},
            cookie=cookie)
        self.assertEqual(status, 200, detail)
        reviewed = [event for event in detail["events"] if event["type"] == "FIELD_REVIEWED"]
        self.assertEqual([event["actor"] for event in reviewed], ["amina"])

    def test_media_needs_a_session_and_pages_get_security_headers(self):
        path = "/media/" + urllib.request.quote(SPECIMEN.format(1))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + path)
        caught.exception.close()
        self.assertEqual(caught.exception.code, 401)
        with urllib.request.urlopen(self.base + "/") as response:
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_repeated_bad_passwords_lock_the_account(self):
        for _ in range(5):
            self.assertEqual(self.call("POST", "/api/login", {"username": "amina", "password": "wrong-password"})[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {"username": "amina", "password": "reviewer-pass-1"})[0], 429)

    def test_admin_registers_a_sender_and_a_user(self):
        admin = self.login("admin", "admin-password-1")
        status, sender, _ = self.call("POST", "/api/senders", {
            "sender_id": "whatsapp:+212611111111", "label": "Sage-femme Ennahda", "facility_name": "CSU Ennahda"},
            cookie=admin)
        self.assertEqual(status, 200, sender)
        self.assertEqual(self.call("POST", "/api/senders", {"sender_id": "+2126", "label": "x"}, cookie=admin)[0], 422)
        status, user, _ = self.call("POST", "/api/users", {"username": "karim", "password": "short", "role": "reviewer"},
                                    cookie=admin)
        self.assertEqual((status, user["error"]["code"]), (422, "WEAK_PASSWORD"))

    def test_logout_ends_the_session(self):
        cookie = self.login("amina", "reviewer-pass-1")
        self.assertEqual(self.call("POST", "/api/logout", {}, cookie=cookie)[0], 200)
        self.assertEqual(self.call("GET", "/api/me", cookie=cookie)[0], 401)


if __name__ == "__main__":
    unittest.main()
