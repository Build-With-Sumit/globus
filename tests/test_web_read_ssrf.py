"""Hermetic checks for the web_read URL safety validator (no network)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import globus_web_read as wr  # noqa: E402


def _resolves_to(*addrs):
    return lambda host, port, *a, **k: [
        (2, 1, 6, "", (addr, 0)) for addr in addrs]


class UrlIsPublicHttpTests(unittest.TestCase):
    def test_public_address_is_allowed(self):
        with patch.object(wr.socket, "getaddrinfo",
                          _resolves_to("93.184.216.34")):
            self.assertTrue(wr.url_is_public_http("https://example.com/x"))

    def test_loopback_private_and_metadata_are_refused(self):
        for addr in ("127.0.0.1", "10.0.0.5", "192.168.1.1",
                     "169.254.169.254", "0.0.0.0", "::1"):
            with patch.object(wr.socket, "getaddrinfo", _resolves_to(addr)):
                self.assertFalse(
                    wr.url_is_public_http("http://internal.example/"), addr)

    def test_one_private_answer_among_public_ones_is_refused(self):
        with patch.object(wr.socket, "getaddrinfo",
                          _resolves_to("93.184.216.34", "10.1.2.3")):
            self.assertFalse(wr.url_is_public_http("http://mixed.example/"))

    def test_non_http_schemes_are_refused_case_insensitively(self):
        for url in ("file:///etc/passwd", "FILE:///etc/passwd",
                    "ftp://example.com/", "javascript:alert(1)", ""):
            self.assertFalse(wr.url_is_public_http(url), url)

    def test_unresolvable_host_fails_closed(self):
        def boom(*a, **k):
            raise OSError("no such host")
        with patch.object(wr.socket, "getaddrinfo", boom):
            self.assertFalse(wr.url_is_public_http("http://nope.invalid/"))

    def test_web_read_refuses_before_any_reader_runs(self):
        out = wr.web_read("file:///etc/passwd")
        self.assertFalse(out["ok"])
        self.assertIn("public http(s)", out["error"])

    def test_redirect_to_private_address_is_stopped(self):
        handler = wr._NoPrivateRedirect()
        with patch.object(wr.socket, "getaddrinfo", _resolves_to("127.0.0.1")):
            self.assertIsNone(handler.redirect_request(
                None, None, 302, "Found", {}, "http://localhost/admin"))


if __name__ == "__main__":
    unittest.main()
