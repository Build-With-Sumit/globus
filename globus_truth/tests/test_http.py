from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from globus_truth.fixtures import demo_receipts
from globus_truth.service import TruthService
from globus_truth.storage import TruthRepository
from globus_truth.web import DASHBOARD_HTML, MAX_REQUEST_BYTES, TruthHTTPServer


NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        repository = TruthRepository(Path(cls.temp.name) / "truth.db")
        service = TruthService(repository, clock=lambda: NOW)
        cls.server = TruthHTTPServer(("127.0.0.1", 0), service)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, response_headers, data

    def test_dashboard_and_security_headers(self) -> None:
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Globus Truth Layer", body)
        decoded = body.decode("utf-8")
        self.assertIn("✓", decoded)
        self.assertIn("—", decoded)
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertNotIn("innerHTML", DASHBOARD_HTML)

    def test_sample_load_populates_all_verdicts(self) -> None:
        status, _, body = self.request(
            "POST",
            "/api/v1/samples/load",
            "{}",
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        loaded = json.loads(body)
        self.assertEqual(loaded["loaded"], 5)
        status, _, body = self.request("GET", "/api/v1/summary")
        summary = json.loads(body)
        self.assertGreaterEqual(summary["total"], 5)
        for verdict in loaded["verdicts"]:
            self.assertGreaterEqual(summary["verdicts"][verdict], 1)

    def test_receipt_ingest_and_listing(self) -> None:
        receipt = deepcopy(demo_receipts(NOW)[0])
        receipt["receipt_id"] = "http-healthy-001"
        receipt["agent_id"] = "fleet-http-test"
        status, _, body = self.request(
            "POST",
            "/api/v1/receipts",
            json.dumps(receipt),
            {"Content-Type": "application/json; charset=utf-8"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["evaluation"]["verdict"], "healthy")
        status, _, body = self.request("GET", "/api/v1/runs/http-healthy-001")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["receipt"]["agent_id"], "fleet-http-test")

    def test_changed_retry_returns_conflict(self) -> None:
        receipt = deepcopy(demo_receipts(NOW)[0])
        receipt["receipt_id"] = "http-conflict-001"
        headers = {"Content-Type": "application/json"}
        self.assertEqual(
            self.request("POST", "/api/v1/receipts", json.dumps(receipt), headers)[0],
            201,
        )
        receipt["summary"] = "Changed claim."
        status, _, _ = self.request(
            "POST", "/api/v1/receipts", json.dumps(receipt), headers
        )
        self.assertEqual(status, 409)

    def test_strict_json_rejects_duplicate_keys(self) -> None:
        status, _, _ = self.request(
            "POST",
            "/api/v1/receipts",
            '{"receipt_id":"one","receipt_id":"two"}',
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)

    def test_strict_json_rejects_non_finite_numbers(self) -> None:
        status, _, _ = self.request(
            "POST",
            "/api/v1/receipts",
            '{"metadata":{"latency":Infinity}}',
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)

    def test_content_type_and_size_are_validated(self) -> None:
        status, _, _ = self.request(
            "POST", "/api/v1/receipts", "{}", {"Content-Type": "text/plain"}
        )
        self.assertEqual(status, 415)
        status, _, _ = self.request(
            "POST",
            "/api/v1/receipts",
            b"x" * (MAX_REQUEST_BYTES + 1),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 413)

    def test_pagination_bounds_and_not_found(self) -> None:
        self.assertEqual(self.request("GET", "/api/v1/runs?limit=0")[0], 400)
        self.assertEqual(self.request("GET", "/api/v1/runs/missing")[0], 404)
        self.assertEqual(self.request("GET", "/not-here")[0], 404)

    def test_bad_host_header_is_rejected(self) -> None:
        status, _, _ = self.request("GET", "/", headers={"Host": "malicious.example"})
        self.assertEqual(status, 421)

    def test_untrusted_text_is_data_not_dashboard_markup(self) -> None:
        receipt = deepcopy(demo_receipts(NOW)[0])
        receipt["receipt_id"] = "http-xss-001"
        receipt["agent_id"] = "fleet-xss-test"
        receipt["summary"] = "<img src=x onerror=alert(1)>"
        status, _, _ = self.request(
            "POST",
            "/api/v1/receipts",
            json.dumps(receipt),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 201)
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn(b"onerror=alert", body)


if __name__ == "__main__":
    unittest.main()
