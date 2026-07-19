from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from globus_truth.fixtures import demo_receipts
from globus_truth.service import TruthService
from globus_truth.storage import ReceiptConflict, TruthRepository


NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "truth.db"
        self.repository = TruthRepository(self.database)
        self.service = TruthService(self.repository, clock=lambda: NOW)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_round_trip_and_summary(self) -> None:
        result = self.service.ingest(demo_receipts(NOW)[0])
        self.assertTrue(result["created"])
        runs = self.repository.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["receipt"]["agent_id"], "demo-indexer")
        self.assertEqual(runs[0]["evaluation"]["verdict"], "healthy")
        self.assertEqual(
            self.repository.summary(),
            {
                "total": 1,
                "trusted": 1,
                "attention": 0,
                "verdicts": {
                    "healthy": 1,
                    "verified_no_work": 0,
                    "degraded_contradictory": 0,
                    "failed": 0,
                    "stale": 0,
                },
            },
        )

    def test_exact_retry_is_idempotent_but_keeps_evaluation_history(self) -> None:
        receipt = demo_receipts(NOW)[0]
        self.assertTrue(self.service.ingest(receipt)["created"])
        self.assertFalse(self.service.ingest(receipt)["created"])
        self.assertEqual(len(self.repository.list_runs()), 1)
        self.assertEqual(
            len(self.repository.verdict_history(receipt["receipt_id"])),
            2,
        )

    def test_receipt_id_reuse_with_changed_content_is_rejected(self) -> None:
        receipt = demo_receipts(NOW)[0]
        self.service.ingest(receipt)
        changed = deepcopy(receipt)
        changed["summary"] = "Different content under the same audit identity."
        with self.assertRaises(ReceiptConflict):
            self.service.ingest(changed)

    def test_sql_metacharacters_remain_data(self) -> None:
        receipt = demo_receipts(NOW)[0]
        receipt["receipt_id"] = "safe:id"
        receipt["summary"] = "Robert'); DROP TABLE receipts;--"
        self.service.ingest(receipt)
        self.assertEqual(self.repository.summary()["total"], 1)
        self.assertIn("DROP TABLE", self.repository.get_run("safe:id")["receipt"]["summary"])

    def test_load_demo_never_deletes_existing_records(self) -> None:
        external = demo_receipts(NOW)[0]
        external["receipt_id"] = "external-001"
        external["agent_id"] = "demo-real-production-agent"
        self.service.ingest(external)
        first = self.service.load_demo()
        second = self.service.load_demo()
        self.assertEqual(first["loaded"], 5)
        self.assertEqual(first["created"], 5)
        self.assertEqual(second["created"], 0)
        self.assertEqual(self.repository.summary()["total"], 6)
        self.assertIsNotNone(self.repository.get_run("external-001"))

    def test_non_finite_metadata_is_not_persisted(self) -> None:
        receipt = demo_receipts(NOW)[0]
        receipt["metadata"]["latency_seconds"] = float("inf")
        with self.assertRaises(ValueError):
            self.service.ingest(receipt)
        self.assertEqual(self.repository.summary()["total"], 0)

    def test_in_memory_database_is_supported(self) -> None:
        repository = TruthRepository(":memory:")
        service = TruthService(repository, clock=lambda: NOW)
        service.ingest(demo_receipts(NOW)[0])
        self.assertEqual(repository.summary()["total"], 1)

    def test_limit_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.repository.list_runs(limit=0)
        with self.assertRaises(ValueError):
            self.repository.list_runs(limit=501)


if __name__ == "__main__":
    unittest.main()
