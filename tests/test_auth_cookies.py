"""Hermetic checks for host-bound member sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import auth_cookies  # noqa: E402


def _token(cookie: str) -> str:
    return cookie.split(";", 1)[0].split("=", 1)[1]


class HostBoundSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        auth_cookies.configure(session_secret=b"s" * 32, session_ttl=60)

    def test_same_host_works_and_cross_host_replay_fails(self) -> None:
        with patch.object(auth_cookies.time, "time", return_value=1_000):
            token = _token(
                auth_cookies.make_cookie(
                    "member@example.test",
                    audience="Globus.Acme.Com",
                )
            )
        with patch.object(auth_cookies.time, "time", return_value=1_001):
            self.assertEqual(
                auth_cookies.verify_token(
                    token,
                    audience="globus.acme.com",
                ),
                "member@example.test",
            )
            self.assertIsNone(
                auth_cookies.verify_token(token, audience="localhost")
            )

    def test_valid_hmac_legacy_unbound_payload_is_rejected(self) -> None:
        payload = "member@example.test|1060"
        mac = hmac.new(
            b"s" * 32,
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        token = (
            base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
            + "."
            + mac
        )
        with patch.object(auth_cookies.time, "time", return_value=1_001):
            self.assertIsNone(
                auth_cookies.verify_token(
                    token,
                    audience="globus.acme.com",
                )
            )

    def test_expired_tampered_and_invalid_audiences_fail_closed(self) -> None:
        with patch.object(auth_cookies.time, "time", return_value=1_000):
            token = _token(
                auth_cookies.make_cookie(
                    "member@example.test",
                    audience="[::1]",
                )
            )
        with patch.object(auth_cookies.time, "time", return_value=1_061):
            self.assertIsNone(
                auth_cookies.verify_token(token, audience="[::1]")
            )
        self.assertIsNone(
            auth_cookies.verify_token(token + "0", audience="[::1]")
        )
        self.assertIsNone(auth_cookies.verify_token(token, audience=""))
        with self.assertRaises(ValueError):
            auth_cookies.make_cookie(
                "member@example.test",
                audience="[::1]|localhost",
            )


if __name__ == "__main__":
    unittest.main()
