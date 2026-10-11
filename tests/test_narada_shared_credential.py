"""Hermetic checks for the company-wide Narada credential fallback."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

# Stub the DB layer so the module imports without a database or pymysql.
_dbh = types.ModuleType("db_helpers")
_dbh.db_read = lambda *a, **k: []
_dbh.db_write = lambda *a, **k: True
_dbh.cfg = lambda *a, **k: ""
_oauth = types.ModuleType("oauth_db")
_oauth.encrypt_token = lambda s: s
_oauth.decrypt_token = lambda s: s
sys.modules["db_helpers"] = _dbh
sys.modules["oauth_db"] = _oauth

import narada_creds  # noqa: E402


class SharedCredentialTests(unittest.TestCase):
    def test_no_member_key_and_no_shared_key_is_none(self):
        with patch.object(narada_creds, "cfg", lambda n, d="": ""), \
                patch.object(narada_creds, "db_read", lambda *a, **k: []):
            self.assertIsNone(narada_creds.get_credential("a@x.test", "apollo"))
            self.assertFalse(narada_creds.has_credential("a@x.test", "apollo"))

    def test_shared_key_is_used_when_member_has_none(self):
        def fake_cfg(name, default=""):
            return "shared-key" if name == "NARADA_APOLLO_API_KEY" else ""
        with patch.object(narada_creds, "cfg", fake_cfg), \
                patch.object(narada_creds, "db_read", lambda *a, **k: []):
            self.assertEqual(
                narada_creds.get_credential("a@x.test", "apollo"),
                {"api_key": "shared-key"})
            self.assertTrue(narada_creds.has_credential("a@x.test", "apollo"))

    def test_member_key_wins_over_shared_key(self):
        rows = [{"credential_enc": '{"api_key": "mine"}', "status": "active"}]
        with patch.object(narada_creds, "cfg", lambda n, d="": "shared-key"), \
                patch.object(narada_creds, "db_read", lambda *a, **k: rows):
            self.assertEqual(
                narada_creds.get_credential("a@x.test", "apollo"),
                {"api_key": "mine"})


if __name__ == "__main__":
    unittest.main()
