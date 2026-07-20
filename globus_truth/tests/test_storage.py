from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from globus_truth.fixtures import demo_receipts
from globus_truth.service import TruthService
from globus_truth.storage import ReceiptConflict, TruthRepository


NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class BarrierClock:
    def __init__(self, now: datetime, parties: int) -> None:
        self.now = now
        self.barrier = threading.Barrier(parties)

    def __call__(self) -> datetime:
        self.barrier.wait(timeout=5)
        return self.now


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "truth.db"
        self.repository = TruthRepository(self.database)
        self.clock = MutableClock(NOW)
        self.service = TruthService(self.repository, clock=self.clock)

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

    def test_trusted_receipt_automatically_transitions_to_stale_on_read(self) -> None:
        receipt = demo_receipts(NOW)[0]
        storage_id = self.service.ingest(receipt)["storage_id"]
        original = self.service.get_run(storage_id)
        self.assertEqual(original["evaluation"]["verdict"], "healthy")
        self.assertEqual(len(self.service.verdict_history(storage_id)), 1)

        self.clock.now = NOW + timedelta(days=2)
        aged = self.service.get_run(storage_id)

        self.assertEqual(aged["evaluation"]["verdict"], "stale")
        self.assertFalse(aged["evaluation"]["valid"])
        self.assertEqual(aged["evaluation"]["reason_codes"], ["heartbeat_stale"])
        self.assertEqual(aged["receipt"], original["receipt"])
        self.assertEqual(aged["received_at"], original["received_at"])
        self.assertEqual(
            [entry["verdict"] for entry in self.service.verdict_history(storage_id)],
            ["healthy", "stale"],
        )

        # Repeated dashboard reads do not append duplicate stale evaluations.
        self.service.summary()
        self.service.list_runs()
        self.service.get_run(storage_id)
        self.assertEqual(len(self.service.verdict_history(storage_id)), 2)

    def test_persisted_freshness_deadline_is_inclusive(self) -> None:
        receipt = demo_receipts(NOW)[0]
        storage_id = self.service.ingest(receipt)["storage_id"]
        finished_at = datetime.fromisoformat(
            receipt["finished_at"].replace("Z", "+00:00")
        )

        self.clock.now = finished_at + timedelta(hours=24)
        self.assertEqual(
            self.service.get_run(storage_id)["evaluation"]["verdict"],
            "healthy",
        )
        self.assertEqual(len(self.service.verdict_history(storage_id)), 1)

        self.clock.now += timedelta(microseconds=1)
        self.assertEqual(
            self.service.get_run(storage_id)["evaluation"]["verdict"],
            "stale",
        )
        self.assertEqual(len(self.service.verdict_history(storage_id)), 2)

    def test_concurrent_repositories_record_one_stale_transition(self) -> None:
        receipt = demo_receipts(NOW)[0]
        storage_id = self.service.ingest(receipt)["storage_id"]
        clock = BarrierClock(NOW + timedelta(days=2), parties=2)
        services = [
            TruthService(TruthRepository(self.database), clock=clock)
            for _ in range(2)
        ]

        with ThreadPoolExecutor(max_workers=2) as executor:
            summaries = list(executor.map(lambda service: service.summary(), services))

        self.assertTrue(
            all(summary["verdicts"]["stale"] == 1 for summary in summaries)
        )
        with closing(sqlite3.connect(self.database)) as connection:
            history = connection.execute(
                "SELECT verdict FROM verdicts WHERE storage_id=? ORDER BY id",
                (storage_id,),
            ).fetchall()
        self.assertEqual(history, [("healthy",), ("stale",)])

    def test_summary_ages_every_expired_receipt_beyond_one_batch(self) -> None:
        template = demo_receipts(NOW)[0]
        for index in range(501):
            receipt = deepcopy(template)
            receipt["receipt_id"] = f"batch-receipt-{index:04d}"
            receipt["run_id"] = f"batch-run-{index:04d}"
            self.service.ingest(receipt)

        self.clock.now = NOW + timedelta(days=2)
        summary = self.service.summary()

        self.assertEqual(summary["total"], 501)
        self.assertEqual(summary["verdicts"]["stale"], 501)
        self.assertEqual(summary["trusted"], 0)

    def test_automatic_staleness_preserves_failure_and_contradiction_precedence(
        self,
    ) -> None:
        receipts = demo_receipts(NOW)
        contradictory_id = self.service.ingest(receipts[2])["storage_id"]
        failed_id = self.service.ingest(receipts[3])["storage_id"]

        self.clock.now = NOW + timedelta(days=7)
        runs = {
            run["storage_id"]: run["evaluation"]["verdict"]
            for run in self.service.list_runs()
        }

        self.assertEqual(runs[contradictory_id], "degraded_contradictory")
        self.assertEqual(runs[failed_id], "failed")
        self.assertEqual(len(self.repository.verdict_history(contradictory_id)), 1)
        self.assertEqual(len(self.repository.verdict_history(failed_id)), 1)
        summary = self.service.summary()
        self.assertEqual(summary["verdicts"]["degraded_contradictory"], 1)
        self.assertEqual(summary["verdicts"]["failed"], 1)
        self.assertEqual(summary["verdicts"]["stale"], 0)

    def test_existing_database_is_migrated_without_rewriting_history(self) -> None:
        legacy_database = Path(self.temp.name) / "legacy.db"
        receipt = demo_receipts(NOW)[0]
        payload = json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with closing(sqlite3.connect(legacy_database)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE receipts (
                    storage_id TEXT PRIMARY KEY,
                    receipt_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE verdicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    storage_id TEXT NOT NULL REFERENCES receipts(storage_id),
                    verdict TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    checks_json TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO receipts
                    (
                        storage_id,
                        receipt_id,
                        agent_id,
                        run_id,
                        received_at,
                        payload_json
                    )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt["receipt_id"],
                    receipt["receipt_id"],
                    receipt["agent_id"],
                    receipt["run_id"],
                    "2030-01-15T12:00:00Z",
                    payload,
                ),
            )
            connection.execute(
                """
                INSERT INTO verdicts
                    (
                        storage_id,
                        verdict,
                        evaluated_at,
                        reason_codes_json,
                        checks_json
                    )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    receipt["receipt_id"],
                    "healthy",
                    "2030-01-15T12:00:00Z",
                    '["all_invariants_satisfied"]',
                    "[]",
                ),
            )

        repository = TruthRepository(legacy_database)
        clock = MutableClock(NOW)
        service = TruthService(repository, clock=clock)
        self.assertEqual(
            service.get_run(receipt["receipt_id"])["evaluation"]["verdict"],
            "healthy",
        )
        self.assertEqual(len(service.verdict_history(receipt["receipt_id"])), 1)

        clock.now = NOW + timedelta(days=2)
        self.assertEqual(
            service.get_run(receipt["receipt_id"])["evaluation"]["verdict"],
            "stale",
        )
        self.assertEqual(len(service.verdict_history(receipt["receipt_id"])), 2)


if __name__ == "__main__":
    unittest.main()
