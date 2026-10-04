import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch
from pymongo.errors import OperationFailure
from dayone.check import main


class CheckTest(unittest.TestCase):
    def run_check(self, client):
        output = io.StringIO()
        settings = {"DAYONE_MONGODB_URI": "mongodb+srv://hidden:private@host.invalid/",
                    "DAYONE_ENCRYPTION_KEY": "secret-key", "DAYONE_API_TOKEN": "secret-token"}
        with patch.dict(os.environ, settings, clear=True), patch("dayone.check.load_config", return_value={}), patch("dayone.security.Cipher"), patch("pymongo.MongoClient", return_value=client), redirect_stdout(output):
            status = main()
        text = output.getvalue()
        for secret in ("private", "host.invalid", "secret-key", "secret-token", "server-secret"):
            self.assertNotIn(secret, text)
        client.close.assert_called_once()
        return status, text

    def test_auth_failure_reports_numeric_code_and_ping(self):
        client = MagicMock()
        client.admin.command.side_effect = OperationFailure("bad auth: authentication failed server-secret", code=8000)
        status, text = self.run_check(client)
        self.assertEqual(status, 1)
        self.assertIn('admin.command("ping")', text)
        self.assertIn("8000", text)
        self.assertIn("authentication", text)
        client.__getitem__.assert_not_called()

    def test_read_permission_failure_identifies_collection(self):
        client = MagicMock()
        client.__getitem__.return_value.__getitem__.return_value.find_one.side_effect = OperationFailure("server-secret", code=13)
        status, text = self.run_check(client)
        self.assertEqual(status, 1)
        self.assertIn("patients.find_one({})", text)
        self.assertIn("13", text)
        self.assertIn("permission", text)

    def test_other_atlas_error_not_misreported_as_auth(self):
        client = MagicMock()
        client.admin.command.side_effect = OperationFailure("server-secret", code=8000)
        status, text = self.run_check(client)
        self.assertEqual(status, 1)
        self.assertIn("Atlas rejected this operation", text)
