"""Hermetic checks for the Drive sync's credential-filename filter (no network, no DB)."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

# google_drive imports db_helpers at module level; classification never touches the DB.
if "db_helpers" not in sys.modules:
    stub = types.ModuleType("db_helpers")
    stub.db_write = lambda *a, **k: None
    sys.modules["db_helpers"] = stub

import google_drive as gd  # noqa: E402

DOC = "application/vnd.google-apps.document"


class CredentialFilenameTests(unittest.TestCase):
    def test_credential_looking_names_are_skipped_whatever_the_mime(self):
        for name in (
            "Credentials 2026.xlsx", "creds.csv", "team passwords", "prod_secrets",
            "API Key list", "api-keys", "access token backup", "id_rsa private key",
            "SSH key", "server.pem", "cert.p12", "deploy.env", "wallet backup",
            "seed phrase", "kubeconfig", "KeyStore", "metamask export",
        ):
            with self.subTest(name=name):
                method, ext, export_mime, reason = gd.drive_classify({"name": name, "mimeType": DOC})
                self.assertIsNone(method)
                self.assertEqual(reason, "credential-filename-pattern")

    def test_ordinary_names_still_classify(self):
        for name in ("Q3 plan", "Meeting notes", "Roadmap.docx", "Budget 2027", "Keynote deck", "Passwordless login spec"):
            with self.subTest(name=name):
                # "Passwordless" has no word boundary after "password", so it must NOT be caught.
                method, _ext, _mime, reason = gd.drive_classify({"name": name, "mimeType": DOC})
                self.assertEqual(method, "export")
                self.assertIsNone(reason)

    def test_missing_or_empty_name_does_not_raise_and_classifies_by_mime(self):
        for f in ({"mimeType": DOC}, {"name": None, "mimeType": DOC}, {"name": "", "mimeType": DOC}):
            method, _e, _m, reason = gd.drive_classify(f)
            self.assertEqual(method, "export")
            self.assertIsNone(reason)

    def test_filter_runs_before_the_mime_check(self):
        # An image named like a credential reports the credential reason, not the mime one.
        _m, _e, _x, reason = gd.drive_classify({"name": "passwords.png", "mimeType": "image/png"})
        self.assertEqual(reason, "credential-filename-pattern")


if __name__ == "__main__":
    unittest.main()
