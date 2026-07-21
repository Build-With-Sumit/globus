from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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

    def test_server_refuses_non_loopback_bind(self) -> None:
        with self.assertRaisesRegex(ValueError, "local-only"):
            TruthHTTPServer(("0.0.0.0", 0), self.server.service)

    def test_dashboard_and_security_headers(self) -> None:
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Globus Truth Layer", body)
        decoded = body.decode("utf-8")
        self.assertIn("✓", decoded)
        self.assertIn("—", decoded)
        self.assertIn("Run live tamper challenge", decoded)
        self.assertIn("Globus Mission Control", decoded)
        self.assertIn("Verified AgentOS for organizations", decoded)
        self.assertIn("4</strong><span>built-in agents", decoded)
        self.assertIn("20</strong><span>LLM-facing tools", decoded)
        self.assertIn("33</strong><span>implemented provider adapters", decoded)
        self.assertIn("Implemented/setup required does not mean", decoded)
        self.assertIn("Run verified business workflow", decoded)
        self.assertIn("/api/v1/judge/outcome-gate", decoded)
        self.assertIn("Inspect gate decision", decoded)
        self.assertIn("expected mismatch was not proven", decoded)
        self.assertIn("Consequence Firewall", decoded)
        self.assertIn("Agents can ask. Humans decide what leaves.", decoded)
        self.assertIn("Stage generated approval request", decoded)
        self.assertIn("Approve this exact action", decoded)
        self.assertIn("/api/v1/judge/approval-center/stage", decoded)
        self.assertIn("/api/v1/approvals?limit=100", decoded)
        self.assertIn("v0.15 · Verified Action SDK", decoded)
        self.assertIn("Create local email draft", decoded)
        self.assertIn("Append local CRM note", decoded)
        self.assertIn("Authorization boundary", decoded)
        self.assertIn("Effect observed", decoded)
        self.assertIn("/api/v1/judge/verified-actions/stage", decoded)
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertNotIn("innerHTML", DASHBOARD_HTML)
        icon_status, icon_headers, icon_body = self.request("GET", "/favicon.svg")
        self.assertEqual(icon_status, 200)
        self.assertTrue(icon_headers["content-type"].startswith("image/svg+xml"))
        self.assertIn(b"<svg", icon_body)
        self.assertEqual(self.request("GET", "/favicon.ico")[0], 204)

    def test_platform_capability_inventory_is_honest_and_safe(self) -> None:
        status, _, body = self.request("GET", "/api/v1/platform/capabilities")
        self.assertEqual(status, 200)
        result = json.loads(body)
        headline = result["summary"]["headline"]
        self.assertEqual(headline["built_in_agents"], 4)
        self.assertEqual(headline["llm_tools"], 20)
        self.assertEqual(headline["implemented_provider_adapters"], 33)
        self.assertIn("not claimed as connected", result["summary"]["disclosure"])
        self.assertEqual(len(result["capabilities"]), 71)
        self.assertGreater(len(result["graph"]["nodes"]), 71)

        original = self.server.service.platform_capabilities

        def fail_with_private_detail() -> dict:
            raise RuntimeError("secret connection detail")

        self.server.service.platform_capabilities = fail_with_private_detail
        try:
            failed_status, _, failed_body = self.request(
                "GET",
                "/api/v1/platform/capabilities",
            )
        finally:
            self.server.service.platform_capabilities = original
        self.assertEqual(failed_status, 500)
        self.assertEqual(
            json.loads(failed_body),
            {"error": "platform capabilities unavailable safely"},
        )
        self.assertNotIn(b"secret connection detail", failed_body)

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

    def test_live_judge_challenge_catches_one_byte_change(self) -> None:
        status, _, body = self.request(
            "POST",
            "/api/v1/judge/challenge",
            "{}",
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 201)
        result = json.loads(body)
        self.assertTrue(result["expectations_met"])
        self.assertEqual(result["external_calls"], 0)
        self.assertEqual(
            [phase["verdict"] for phase in result["phases"]],
            ["healthy", "degraded_contradictory"],
        )
        self.assertEqual(
            result["artifact"]["final_bytes"],
            result["artifact"]["expected_bytes"] + 1,
        )
        self.assertNotEqual(
            result["artifact"]["expected_sha256"],
            result["artifact"]["final_sha256"],
        )
        for phase in result["phases"]:
            run_status, _, run_body = self.request(
                "GET",
                f"/api/v1/runs/{phase['storage_id']}",
            )
            self.assertEqual(run_status, 200)
            self.assertEqual(
                json.loads(run_body)["evaluation"]["verdict"],
                phase["verdict"],
            )

        invalid_status, _, _ = self.request(
            "POST",
            "/api/v1/judge/challenge",
            '{"unexpected":true}',
            {"Content-Type": "application/json"},
        )
        self.assertEqual(invalid_status, 400)

        null_status, _, null_body = self.request(
            "POST",
            "/api/v1/judge/challenge",
            "null",
            {"Content-Type": "application/json"},
        )
        self.assertEqual(null_status, 400)
        self.assertIn("accepts only", json.loads(null_body)["error"])

        original = self.server.service.run_judge_challenge

        def fail_with_storage_error() -> dict:
            raise sqlite3.OperationalError("database is locked")

        self.server.service.run_judge_challenge = fail_with_storage_error
        try:
            failed_status, _, failed_body = self.request(
                "POST",
                "/api/v1/judge/challenge",
                "{}",
                {"Content-Type": "application/json"},
            )
        finally:
            self.server.service.run_judge_challenge = original
        self.assertEqual(failed_status, 500)
        self.assertEqual(
            json.loads(failed_body),
            {"error": "judge challenge failed safely"},
        )

    def test_outcome_gate_authorizes_then_blocks_from_real_readback(self) -> None:
        status, _, body = self.request(
            "POST",
            "/api/v1/judge/outcome-gate",
            "{}",
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 201)
        result = json.loads(body)
        self.assertTrue(result["credential_free"])
        self.assertEqual(result["external_calls"], 0)
        self.assertTrue(result["expectations_met"])
        self.assertEqual(result["destination"]["claimed_follow_ups"], 3)
        self.assertEqual(result["destination"]["before_observed"], 3)
        self.assertEqual(result["destination"]["after_observed"], 2)

        before, after = result["phases"]
        self.assertEqual(before["name"], "before_change")
        self.assertEqual(before["claimed_count"], 3)
        self.assertEqual(before["observed_count"], 3)
        self.assertEqual(before["verdict"], "healthy")
        self.assertTrue(before["gate"]["authorized"])
        self.assertTrue(before["gate"]["binding_valid"])
        self.assertTrue(before["gate"]["audit_verified"])

        self.assertEqual(after["name"], "after_change")
        self.assertEqual(after["claimed_count"], 3)
        self.assertEqual(after["observed_count"], 2)
        self.assertEqual(after["verdict"], "degraded_contradictory")
        self.assertFalse(after["gate"]["authorized"])
        self.assertTrue(after["gate"]["binding_valid"])
        self.assertTrue(after["gate"]["audit_verified"])

        action = result["action"]
        self.assertTrue(action["first_executed"])
        self.assertFalse(action["second_executed"])
        self.assertTrue(action["second_prevented"])
        self.assertEqual(action["final_outbox_rows"], 1)

        for phase, expected_authorized in ((before, True), (after, False)):
            run_status, _, run_body = self.request(
                "GET",
                f"/api/v1/runs/{phase['storage_id']}",
            )
            self.assertEqual(run_status, 200)
            self.assertEqual(
                json.loads(run_body)["evaluation"]["verdict"],
                phase["verdict"],
            )
            decision_status, _, decision_body = self.request(
                "GET",
                f"/api/v1/gate/decisions/{phase['gate']['decision_id']}",
            )
            self.assertEqual(decision_status, 200)
            decision = json.loads(decision_body)
            self.assertEqual(decision["decision_id"], phase["gate"]["decision_id"])
            self.assertEqual(decision["storage_id"], phase["storage_id"])
            self.assertIs(decision["authorized"], expected_authorized)

        self.assertEqual(
            self.request("GET", "/api/v1/gate/decisions/missing")[0],
            404,
        )
        self.assertEqual(
            self.request("GET", "/api/v1/gate/decisions/bad%2Fid")[0],
            400,
        )

    def test_outcome_gate_strict_body_and_safe_failure(self) -> None:
        headers = {"Content-Type": "application/json"}
        invalid_status, _, invalid_body = self.request(
            "POST",
            "/api/v1/judge/outcome-gate",
            '{"unexpected":true}',
            headers,
        )
        self.assertEqual(invalid_status, 400)
        self.assertIn("accepts only", json.loads(invalid_body)["error"])

        null_status, _, null_body = self.request(
            "POST",
            "/api/v1/judge/outcome-gate",
            "null",
            headers,
        )
        self.assertEqual(null_status, 400)
        self.assertIn("accepts only", json.loads(null_body)["error"])

        original = self.server.service.run_outcome_gate_challenge

        def fail_with_storage_error() -> dict:
            raise sqlite3.OperationalError("private destination path")

        self.server.service.run_outcome_gate_challenge = fail_with_storage_error
        try:
            failed_status, _, failed_body = self.request(
                "POST",
                "/api/v1/judge/outcome-gate",
                "{}",
                headers,
            )
        finally:
            self.server.service.run_outcome_gate_challenge = original
        self.assertEqual(failed_status, 500)
        self.assertEqual(
            json.loads(failed_body),
            {"error": "outcome gate failed safely"},
        )
        self.assertNotIn(b"private destination path", failed_body)

    def test_approval_center_pauses_then_approves_exactly_once(self) -> None:
        headers = {"Content-Type": "application/json"}
        status, _, body = self.request(
            "POST",
            "/api/v1/judge/approval-center/stage",
            "{}",
            headers,
        )
        self.assertEqual(status, 201)
        pending = json.loads(body)
        self.assertEqual(pending["status"], "pending")
        self.assertTrue(pending["credential_free"])
        self.assertEqual(pending["external_calls"], 0)
        self.assertEqual(pending["action"]["executions"], 0)
        self.assertEqual(pending["proposal"]["truth_verdict"], "healthy")
        self.assertEqual(pending["proposal"]["risk"], "high")
        self.assertEqual(pending["proposal"]["approval_mode"], "explicit")

        proposal_id = pending["proposal_id"]
        list_status, _, list_body = self.request(
            "GET",
            "/api/v1/approvals?limit=100",
        )
        self.assertEqual(list_status, 200)
        proposals = json.loads(list_body)["proposals"]
        self.assertTrue(
            any(
                item["proposal"]["proposal_id"] == proposal_id
                and item["state"] == "pending"
                for item in proposals
            )
        )

        resolved_status, _, resolved_body = self.request(
            "POST",
            f"/api/v1/judge/approval-center/{proposal_id}/approve",
            "{}",
            headers,
        )
        self.assertEqual(resolved_status, 200)
        resolved = json.loads(resolved_body)
        self.assertTrue(resolved["expectations_met"])
        self.assertEqual(resolved["status"], "completed")
        self.assertEqual(resolved["action"]["final_outbox_rows"], 1)
        attempts = {item["name"]: item for item in resolved["attempts"]}
        self.assertFalse(attempts["changed_payload"]["executed"])
        self.assertEqual(
            attempts["changed_payload"]["reason_codes"],
            ["approval_scope_mismatch"],
        )
        self.assertTrue(attempts["exact_payload"]["executed"])
        self.assertFalse(attempts["replay"]["executed"])
        self.assertEqual(
            attempts["replay"]["reason_codes"],
            ["approval_already_consumed"],
        )
        self.assertEqual(resolved["audit"]["state"], "succeeded")

    def test_general_approval_api_is_strict_payload_free_and_durable(self) -> None:
        receipt = deepcopy(demo_receipts(NOW)[0])
        receipt["receipt_id"] = "http-approval-truth-001"
        stored = self.server.service.ingest(receipt)
        expires_at = (
            NOW + timedelta(hours=1)
        ).isoformat().replace("+00:00", "Z")
        proposal = {
            "proposal_id": "http-proposal-001",
            "storage_id": stored["storage_id"],
            "action_id": "http-action-001",
            "policy_id": "healthy_only",
            "action_kind": "local-outbox",
            "payload_sha256": "b" * 64,
            "requested_by": "agent.sales-desk",
            "risk": "high",
            "expires_at": expires_at,
        }
        headers = {"Content-Type": "application/json"}
        status, _, body = self.request(
            "POST",
            "/api/v1/approvals",
            json.dumps(proposal),
            headers,
        )
        self.assertEqual(status, 201)
        created = json.loads(body)
        self.assertTrue(created["created"])
        self.assertNotIn("payload", {
            key: value
            for key, value in created.items()
            if key != "payload_sha256"
        })

        retry_status, _, retry_body = self.request(
            "POST",
            "/api/v1/approvals",
            json.dumps(proposal),
            headers,
        )
        self.assertEqual(retry_status, 200)
        self.assertFalse(json.loads(retry_body)["created"])

        decision_status, _, decision_body = self.request(
            "POST",
            "/api/v1/approvals/http-proposal-001/decision",
            json.dumps({
                "outcome": "approved",
                "decided_by": "operator.http",
                "reason_code": "reviewed",
            }),
            headers,
        )
        self.assertEqual(decision_status, 201)
        self.assertEqual(json.loads(decision_body)["outcome"], "approved")

        get_status, _, get_body = self.request(
            "GET",
            "/api/v1/approvals/http-proposal-001",
        )
        self.assertEqual(get_status, 200)
        state = json.loads(get_body)
        self.assertEqual(state["state"], "approved")
        self.assertIsNone(state["claim"])
        self.assertNotIn('"payload":', get_body.decode("utf-8"))

        invalid = {**proposal, "raw_payload": "must-not-enter-audit"}
        invalid_status, _, _ = self.request(
            "POST",
            "/api/v1/approvals",
            json.dumps(invalid),
            headers,
        )
        self.assertEqual(invalid_status, 400)
        self.assertEqual(
            self.request("GET", "/api/v1/approvals/missing-proposal")[0],
            404,
        )

    def test_approval_center_rejection_and_http_fail_closed_contract(self) -> None:
        headers = {"Content-Type": "application/json"}
        stage_status, _, stage_body = self.request(
            "POST",
            "/api/v1/judge/approval-center/stage",
            "{}",
            headers,
        )
        self.assertEqual(stage_status, 201)
        proposal_id = json.loads(stage_body)["proposal_id"]
        reject_status, _, reject_body = self.request(
            "POST",
            f"/api/v1/judge/approval-center/{proposal_id}/reject",
            "{}",
            headers,
        )
        self.assertEqual(reject_status, 200)
        rejected = json.loads(reject_body)
        self.assertTrue(rejected["expectations_met"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["action"]["final_outbox_rows"], 0)
        self.assertEqual(rejected["attempts"], [])

        for path in (
            "/api/v1/judge/approval-center/stage",
            f"/api/v1/judge/approval-center/{proposal_id}/approve",
        ):
            invalid_status, _, invalid_body = self.request(
                "POST",
                path,
                '{"unexpected":true}',
                headers,
            )
            self.assertEqual(invalid_status, 400)
            self.assertIn("accepts only", json.loads(invalid_body)["error"])
        self.assertEqual(
            self.request(
                "POST",
                "/api/v1/judge/approval-center/bad%2Fid/approve",
                "{}",
                headers,
            )[0],
            400,
        )
        self.assertEqual(self.request("GET", "/api/v1/approvals?limit=0")[0], 400)

        original = self.server.service.stage_approval_challenge

        def fail_with_private_detail() -> dict:
            raise RuntimeError("password=private-provider-secret")

        self.server.service.stage_approval_challenge = fail_with_private_detail
        try:
            failed_status, _, failed_body = self.request(
                "POST",
                "/api/v1/judge/approval-center/stage",
                "{}",
                headers,
            )
        finally:
            self.server.service.stage_approval_challenge = original
        self.assertEqual(failed_status, 500)
        self.assertEqual(
            json.loads(failed_body),
            {"error": "approval challenge failed safely"},
        )
        self.assertNotIn(b"private-provider-secret", failed_body)

    def test_verified_action_sdk_http_lifecycle_and_timeline(self) -> None:
        headers = {"Content-Type": "application/json"}
        manifests_status, _, manifests_body = self.request(
            "GET",
            "/api/v1/verified-actions/manifests",
        )
        self.assertEqual(manifests_status, 200)
        manifests = json.loads(manifests_body)
        self.assertEqual(manifests["external_calls"], 0)
        self.assertEqual(len(manifests["manifests"]), 2)
        adapter_id = "globus.local.email-draft"

        stage_status, _, stage_body = self.request(
            "POST",
            "/api/v1/judge/verified-actions/stage",
            json.dumps({"adapter_id": adapter_id}),
            headers,
        )
        self.assertEqual(stage_status, 201)
        staged = json.loads(stage_body)
        self.assertEqual(staged["status"], "pending")
        self.assertEqual(staged["destination"]["observed_records"], 0)
        self.assertEqual(staged["external_calls"], 0)
        proposal_id = staged["proposal_id"]

        pending_status, _, pending_body = self.request(
            "GET",
            f"/api/v1/verified-actions/{proposal_id}/timeline",
        )
        self.assertEqual(pending_status, 200)
        pending = json.loads(pending_body)
        self.assertEqual(len(pending["events"]), 6)
        self.assertEqual(pending["state"], "pending")
        self.assertFalse(pending["terminal"])

        resolve_status, _, resolve_body = self.request(
            "POST",
            f"/api/v1/judge/verified-actions/{proposal_id}/approve",
            "{}",
            headers,
        )
        self.assertEqual(resolve_status, 200)
        resolved = json.loads(resolve_body)
        self.assertTrue(resolved["expectations_met"])
        self.assertEqual(resolved["status"], "completed")
        self.assertEqual(resolved["destination"]["observed_records"], 1)
        self.assertTrue(resolved["destination"]["verified"])
        self.assertEqual(resolved["external_calls"], 0)
        self.assertTrue(resolved["timeline"]["integrity_complete"])
        self.assertEqual(
            [event["event_type"] for event in resolved["timeline"]["events"]],
            [
                "proposed",
                "human_decision",
                "truth_gate",
                "execution_claimed",
                "destination_verification",
                "completed",
            ],
        )
        self.assertEqual(
            [event["outcome"] for event in resolved["timeline"]["events"]],
            [
                "recorded",
                "approved",
                "authorized",
                "claimed",
                "verified",
                "succeeded",
            ],
        )
        serialized = resolve_body.decode("utf-8")
        self.assertNotIn("@example.test", serialized)
        self.assertNotIn("This is a generated local draft", serialized)
        self.assertNotIn(str(self.temp.name), serialized)

    def test_verified_action_http_rejects_untrusted_shapes_and_fails_safely(
        self,
    ) -> None:
        headers = {"Content-Type": "application/json"}
        self.assertEqual(
            self.request(
                "POST",
                "/api/v1/judge/verified-actions/stage",
                "{}",
                headers,
            )[0],
            400,
        )
        self.assertEqual(
            self.request(
                "POST",
                "/api/v1/judge/verified-actions/stage",
                json.dumps({"adapter_id": "network.email.send"}),
                headers,
            )[0],
            400,
        )
        self.assertEqual(
            self.request(
                "GET",
                "/api/v1/verified-actions/missing/timeline",
            )[0],
            404,
        )
        self.assertEqual(
            self.request(
                "GET",
                "/api/v1/verified-actions/bad%2Fid/timeline",
            )[0],
            400,
        )
        self.assertEqual(
            self.request(
                "POST",
                "/api/v1/judge/verified-actions/bad%2Fid/approve",
                "{}",
                headers,
            )[0],
            400,
        )

        original = self.server.service.stage_verified_action_lab

        def fail_with_private_detail(*, adapter_id: str) -> dict:
            del adapter_id
            raise RuntimeError("password=private-provider-secret")

        self.server.service.stage_verified_action_lab = fail_with_private_detail
        try:
            status, _, body = self.request(
                "POST",
                "/api/v1/judge/verified-actions/stage",
                json.dumps({"adapter_id": "globus.local.crm-note"}),
                headers,
            )
        finally:
            self.server.service.stage_verified_action_lab = original
        self.assertEqual(status, 500)
        self.assertEqual(
            json.loads(body),
            {"error": "verified action stage failed safely"},
        )
        self.assertNotIn(b"private-provider-secret", body)

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
