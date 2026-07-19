from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from globus_truth.evaluator import evaluate_receipt
from globus_truth.fixtures import demo_receipts


NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)


class EvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.receipts = demo_receipts(NOW)
        self.healthy = deepcopy(self.receipts[0])

    def verdict(self, receipt: dict) -> str:
        return evaluate_receipt(receipt, now=NOW).verdict

    def test_demo_fixtures_cover_every_verdict(self) -> None:
        self.assertEqual(
            [self.verdict(receipt) for receipt in self.receipts],
            [
                "healthy",
                "verified_no_work",
                "degraded_contradictory",
                "failed",
                "stale",
            ],
        )

    def test_declared_success_never_passes_without_evidence(self) -> None:
        self.healthy["evidence"] = []
        result = evaluate_receipt(self.healthy, now=NOW)
        self.assertEqual(result.verdict, "degraded_contradictory")
        self.assertIn("success_without_evidence", result.reason_codes)

    def test_success_requires_measured_work(self) -> None:
        self.healthy["input"] = {"items_seen": 0, "items_eligible": 0}
        self.healthy["output"] = {"items_processed": 0, "items_changed": 0}
        result = evaluate_receipt(self.healthy, now=NOW)
        self.assertEqual(result.verdict, "degraded_contradictory")
        self.assertIn("success_without_measured_work", result.reason_codes)

    def test_no_work_requires_explicit_reason(self) -> None:
        receipt = deepcopy(self.receipts[1])
        del receipt["no_work"]
        result = evaluate_receipt(receipt, now=NOW)
        self.assertEqual(result.verdict, "degraded_contradictory")
        self.assertIn("missing_no_work_reason", result.reason_codes)

    def test_no_work_counts_must_show_zero_eligible_work(self) -> None:
        receipt = deepcopy(self.receipts[1])
        receipt["input"]["items_eligible"] = 1
        result = evaluate_receipt(receipt, now=NOW)
        self.assertEqual(result.verdict, "degraded_contradictory")
        self.assertIn("no_work_count_contradiction", result.reason_codes)

    def test_fluent_refusal_is_not_output(self) -> None:
        self.healthy["summary"] = (
            "No source material was included. Please provide the source material."
        )
        result = evaluate_receipt(self.healthy, now=NOW)
        self.assertEqual(result.verdict, "degraded_contradictory")
        self.assertIn("error_prose_as_output", result.reason_codes)

    def test_failed_check_contradicts_success(self) -> None:
        self.healthy["checks"][0]["passed"] = False
        result = evaluate_receipt(self.healthy, now=NOW)
        self.assertEqual(result.verdict, "degraded_contradictory")
        self.assertIn("agent_check_failed", result.reason_codes)

    def test_failed_run_stays_failed_and_requires_error_details(self) -> None:
        receipt = deepcopy(self.receipts[3])
        del receipt["error"]
        result = evaluate_receipt(receipt, now=NOW)
        self.assertEqual(result.verdict, "failed")
        self.assertIn("missing_failure_detail", result.reason_codes)

    def test_stale_threshold_boundary_is_inclusive(self) -> None:
        receipt = deepcopy(self.healthy)
        boundary = NOW - timedelta(hours=24)
        receipt["started_at"] = (boundary - timedelta(minutes=2)).isoformat()
        receipt["finished_at"] = boundary.isoformat()
        receipt["heartbeat_at"] = boundary.isoformat()
        receipt["evidence"][0]["observed_at"] = boundary.isoformat()
        self.assertEqual(self.verdict(receipt), "healthy")
        receipt["finished_at"] = (boundary - timedelta(seconds=1)).isoformat()
        receipt["heartbeat_at"] = receipt["finished_at"]
        receipt["evidence"][0]["observed_at"] = receipt["finished_at"]
        self.assertEqual(self.verdict(receipt), "stale")

    def test_timezone_is_required(self) -> None:
        self.healthy["heartbeat_at"] = "2030-01-15T11:57:00"
        result = evaluate_receipt(self.healthy, now=NOW)
        self.assertEqual(result.verdict, "failed")
        self.assertIn("invalid_heartbeat_at", result.reason_codes)

    def test_rfc3339_requires_t_separator(self) -> None:
        self.healthy["heartbeat_at"] = "2030-01-15 11:57:00+00:00"
        result = evaluate_receipt(self.healthy, now=NOW)
        self.assertEqual(result.verdict, "failed")
        self.assertIn("invalid_heartbeat_at", result.reason_codes)

    def test_non_finite_metadata_is_rejected(self) -> None:
        self.healthy["metadata"]["latency_seconds"] = float("nan")
        result = evaluate_receipt(self.healthy, now=NOW)
        self.assertEqual(result.verdict, "failed")
        self.assertIn("invalid_metadata", result.reason_codes)

    def test_future_timestamp_is_contradictory(self) -> None:
        future = NOW + timedelta(minutes=6)
        self.healthy["finished_at"] = future.isoformat()
        self.healthy["heartbeat_at"] = future.isoformat()
        self.healthy["evidence"][0]["observed_at"] = future.isoformat()
        result = evaluate_receipt(self.healthy, now=NOW)
        self.assertEqual(result.verdict, "degraded_contradictory")
        self.assertIn("future_timestamp", result.reason_codes)

    def test_count_invariants_are_enforced(self) -> None:
        self.healthy["output"]["items_changed"] = 5
        result = evaluate_receipt(self.healthy, now=NOW)
        self.assertEqual(result.verdict, "degraded_contradictory")
        self.assertIn("count_invariant", result.reason_codes)

    def test_boolean_is_not_an_integer_count(self) -> None:
        self.healthy["input"]["items_seen"] = True
        result = evaluate_receipt(self.healthy, now=NOW)
        self.assertEqual(result.verdict, "failed")
        self.assertIn("invalid_counts", result.reason_codes)

    def test_unknown_fields_fail_schema(self) -> None:
        self.healthy["surprise"] = "ignored by permissive validators"
        result = evaluate_receipt(self.healthy, now=NOW)
        self.assertEqual(result.verdict, "failed")
        self.assertIn("unknown_fields", result.reason_codes)

    def test_evidence_must_belong_to_run_window(self) -> None:
        self.healthy["evidence"][0]["observed_at"] = (
            NOW - timedelta(days=1)
        ).isoformat()
        result = evaluate_receipt(self.healthy, now=NOW)
        self.assertEqual(result.verdict, "degraded_contradictory")
        self.assertIn("evidence_timestamp_invariant", result.reason_codes)

    def test_evaluation_is_deterministic_for_same_clock(self) -> None:
        first = evaluate_receipt(self.healthy, now=NOW).to_dict()
        second = evaluate_receipt(self.healthy, now=NOW).to_dict()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
