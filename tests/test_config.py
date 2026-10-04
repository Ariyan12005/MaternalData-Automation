import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dayone.config import read_config, load_config


class ConfigTest(unittest.TestCase):
    def test_private_config_keeps_literal_password_and_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('# comment\nDAYONE_MONGODB_URI="mongodb+srv://user:pa#ss@host/"\nDAYONE_STORAGE=mongodb\nDAYONE_API_TOKEN=\nOTHER=ignored\n', encoding="utf-8")
            with patch.dict(os.environ, {"DAYONE_STORAGE": "sqlite"}, clear=True):
                sources = load_config(path)
                self.assertEqual(sources["DAYONE_STORAGE"], "environment")
                self.assertEqual(sources["DAYONE_MONGODB_URI"], ".env")
                self.assertEqual(os.environ["DAYONE_STORAGE"], "sqlite")
                self.assertEqual(os.environ["DAYONE_MONGODB_URI"], "mongodb+srv://user:pa#ss@host/")
                self.assertNotIn("DAYONE_API_TOKEN", os.environ)
                self.assertNotIn("OTHER", os.environ)
            self.assertEqual(read_config(Path(directory) / "absent"), {})
