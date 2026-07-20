from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from globus_truth.action_gate import ActionGate, ActionGateAuditError
from globus_truth.fixtures import demo_receipts
from globus_truth.service import TruthService
from globus_truth.storage import ActionDecisionConflict, TruthRepository


NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)
SAFE_FIELDS = {
    "decision_id",
    "storage_id",
    "action_id",
    "policy_id",
    "observed_verdict",
    "authorized",
    "reason_codes",
    "decided_at",
}


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FailingAuditRepository:
    def save_action_decision(self, decision: object) -> None:
        raise RuntimeError("database password=do-not-leak")


class ActionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "truth.db"
        self.repository = TruthRepository(self.database)
        self.clock = MutableClock(NOW)
        self.service = TruthService(self.repository, clock=self.clock)
        self.gate = ActionGate(
            self.service,
            clock=self.clock,
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temp.cleanup()

    def test_policy_verdict_matrix_reads_persisted_current_verdict(self) -> None:
        results = [self.service.ingest(receipt) for receipt in demo_receipts(NOW)]
        verdicts = [
            "healthy",
            "verified_no_work",
            "degraded_contradictory",
            "failed",
            "stale",
        ]
        expected = {
            "healthy_only": [True, False, False, False, False],
            "trusted_completion": [True, True, False, False, False],
        }

        for policy_id, policy_expected in expected.items():
            for index, (result, verdict, authorized) in enumerate(
                zip(results, verdicts, policy_expected)
            ):
                decision = self.gate.decide(
                    storage_id=result["storage_id"],
                    action_id=f"matrix-{policy_id}-{index}",
                    policy_id=policy_id,
                    decision_id=f"decision-{policy_id}-{index}",
                )
                self.assertEqual(set(decision), SAFE_FIELDS)
                self.assertEqual(decision["observed_verdict"], verdict)
                self.assertIs(decision["authorized"], authorized)
                self.assertEqual(
                    self.repository.get_action_decision(decision["decision_id"]),
                    decision,
                )

        self.assertEqual(len(self.repository.list_action_decisions()), 10)

    def test_healthy_receipt_is_refreshed_to_stale_before_gate_decision(self) -> None:
        result = self.service.ingest(demo_receipts(NOW)[0])
        self.clock.now = NOW + timedelta(days=2)

        decision = self.gate.decide(
            storage_id=result["storage_id"],
            action_id="publish-report",
            decision_id="decision-stale-after-read",
        )

        self.assertFalse(decision["authorized"])
        self.assertEqual(decision["observed_verdict"], "stale")
        self.assertEqual(decision["reason_codes"], ["verdict_stale"])
        self.assertEqual(
            [item["verdict"] for item in self.service.verdict_history(result["storage_id"])],
            ["healthy", "stale"],
        )

    def test_missing_malformed_and_unavailable_truth_fail_closed_and_are_audited(
        self,
    ) -> None:
        cases = [
            (
                ActionGate(self.service, clock=self.clock),
                "missing-receipt",
                "missing",
                "truth_record_missing",
            ),
            (
                ActionGate(
                    lambda storage_id: {
                        "storage_id": "different-receipt",
                        "evaluation": {"verdict": "healthy", "valid": True},
                    },
                    self.repository,
                    clock=self.clock,
                ),
                "requested-receipt",
                "malformed",
                "truth_record_malformed",
            ),
            (
                ActionGate(
                    lambda storage_id: (_ for _ in ()).throw(
                        RuntimeError("credential=do-not-leak")
                    ),
                    self.repository,
                    clock=self.clock,
                ),
                "unavailable-receipt",
                "unavailable",
                "truth_lookup_failed",
            ),
        ]

        for index, (gate, storage_id, observed, reason) in enumerate(cases):
            decision = gate.decide(
                storage_id=storage_id,
                action_id=f"blocked-action-{index}",
                decision_id=f"blocked-decision-{index}",
            )
            self.assertFalse(decision["authorized"])
            self.assertEqual(decision["observed_verdict"], observed)
            self.assertEqual(decision["reason_codes"], [reason])
            self.assertEqual(
                self.repository.get_action_decision(decision["decision_id"]),
                decision,
            )
            self.assertNotIn("credential", str(decision))

    def test_gate_has_no_caller_verdict_override(self) -> None:
        result = self.service.ingest(demo_receipts(NOW)[3])
        with self.assertRaises(TypeError):
            self.gate.decide(
                storage_id=result["storage_id"],
                action_id="unsafe-override-attempt",
                verdict="healthy",  # type: ignore[call-arg]
            )
        self.assertEqual(self.repository.list_action_decisions(), [])

    def test_decisions_are_idempotent_only_for_exact_content_and_immutable(self) -> None:
        stored = self.service.ingest(demo_receipts(NOW)[0])
        decision = {
            "decision_id": "immutable-decision",
            "storage_id": stored["storage_id"],
            "action_id": "release-001",
            "policy_id": "healthy_only",
            "observed_verdict": "healthy",
            "authorized": True,
            "reason_codes": ["policy_satisfied"],
            "decided_at": "2030-01-15T12:00:00.000000Z",
        }
        self.assertTrue(self.repository.save_action_decision(decision))
        self.assertFalse(self.repository.save_action_decision(dict(decision)))

        changed = dict(decision)
        changed["action_id"] = "release-002"
        with self.assertRaises(ActionDecisionConflict):
            self.repository.save_action_decision(changed)
        with closing(sqlite3.connect(self.database)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE action_decisions SET authorized=0 WHERE decision_id=?",
                    (decision["decision_id"],),
                )
        self.assertTrue(
            self.repository.get_action_decision(decision["decision_id"])[
                "authorized"
            ]
        )

    def test_authorized_decision_is_rechecked_atomically_against_latest_truth(
        self,
    ) -> None:
        stored = self.service.ingest(demo_receipts(NOW)[0])

        def race_to_stale(storage_id: str) -> dict:
            previously_healthy = self.service.get_run(storage_id)
            self.clock.now = NOW + timedelta(days=2)
            refreshed = self.service.get_run(storage_id)
            self.assertEqual(refreshed["evaluation"]["verdict"], "stale")
            return previously_healthy

        racing_gate = ActionGate(
            race_to_stale,
            self.repository,
            clock=self.clock,
        )
        with self.assertRaises(ActionGateAuditError):
            racing_gate.decide(
                storage_id=stored["storage_id"],
                action_id="race-sensitive-action",
                decision_id="race-sensitive-decision",
            )

        self.assertIsNone(
            self.repository.get_action_decision("race-sensitive-decision")
        )
        self.assertEqual(
            self.service.get_run(stored["storage_id"])["evaluation"]["verdict"],
            "stale",
        )

    def test_storage_and_sql_reject_inconsistent_allow_decisions(self) -> None:
        inconsistent = {
            "decision_id": "inconsistent-allow",
            "storage_id": "receipt-failed",
            "action_id": "unsafe-action",
            "policy_id": "healthy_only",
            "observed_verdict": "failed",
            "authorized": True,
            "reason_codes": ["policy_satisfied"],
            "decided_at": "2030-01-15T12:00:00.000000Z",
        }
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            self.repository.save_action_decision(inconsistent)

        with closing(sqlite3.connect(self.database)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO action_decisions
                        (
                            decision_id,
                            storage_id,
                            action_id,
                            policy_id,
                            observed_verdict,
                            authorized,
                            reason_codes_json,
                            decided_at
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "direct-inconsistent-allow",
                        "receipt-failed",
                        "unsafe-action",
                        "healthy_only",
                        "failed",
                        1,
                        '["policy_satisfied"]',
                        "2030-01-15T12:00:00.000000Z",
                    ),
                )

    def test_audit_failure_never_returns_an_allow_decision_or_leaks_details(self) -> None:
        result = self.service.ingest(demo_receipts(NOW)[0])
        gate = ActionGate(
            self.service,
            FailingAuditRepository(),
            clock=self.clock,
        )

        with self.assertRaises(ActionGateAuditError) as caught:
            gate.decide(
                storage_id=result["storage_id"],
                action_id="send-customer-message",
            )

        self.assertNotIn("password", str(caught.exception))
        self.assertIn("blocked", str(caught.exception).lower())

    def test_decision_storage_rejects_payloads_and_exposes_safe_fields_only(self) -> None:
        secret = "private-customer-prompt"
        receipt = demo_receipts(NOW)[0]
        receipt["summary"] = secret
        result = self.service.ingest(receipt)
        decision = self.gate.decide(
            storage_id=result["storage_id"],
            action_id="privacy-check",
            decision_id="privacy-decision",
        )

        self.assertEqual(set(decision), SAFE_FIELDS)
        self.assertNotIn(secret, str(decision))
        stored = self.repository.list_action_decisions(
            storage_id=result["storage_id"]
        )
        self.assertEqual(stored, [decision])
        self.assertNotIn(secret, str(stored))

        unsafe = dict(decision)
        unsafe["prompt"] = secret
        with self.assertRaises(ValueError):
            self.repository.save_action_decision(unsafe)

        with closing(sqlite3.connect(self.database)) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(action_decisions)"
                ).fetchall()
            }
        self.assertEqual(
            columns,
            {
                "decision_id",
                "storage_id",
                "action_id",
                "policy_id",
                "observed_verdict",
                "authorized",
                "reason_codes_json",
                "decided_at",
            },
        )

    def test_identifiers_and_policy_are_strictly_validated(self) -> None:
        for kwargs in (
            {"storage_id": "../receipt", "action_id": "safe"},
            {"storage_id": "safe", "action_id": "send customer data"},
            {"storage_id": "safe", "action_id": "safe", "policy_id": "allow_all"},
            {
                "storage_id": "safe",
                "action_id": "safe",
                "decision_id": "x" * 129,
            },
        ):
            with self.assertRaises(ValueError):
                self.gate.decide(**kwargs)
        self.assertEqual(self.repository.list_action_decisions(), [])

    def test_action_table_is_added_to_an_existing_database_without_data_loss(
        self,
    ) -> None:
        legacy_database = Path(self.temp.name) / "legacy-action.db"
        with closing(sqlite3.connect(legacy_database)) as connection, connection:
            connection.execute(
                "CREATE TABLE existing_data (id INTEGER PRIMARY KEY, value TEXT)"
            )
            connection.execute(
                "INSERT INTO existing_data (id, value) VALUES (1, 'preserved')"
            )

        migrated = TruthRepository(legacy_database)
        try:
            self.assertEqual(migrated.list_action_decisions(), [])
            with closing(sqlite3.connect(legacy_database)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT id, value FROM existing_data"
                    ).fetchall(),
                    [(1, "preserved")],
                )
                self.assertIsNotNone(
                    connection.execute(
                        """
                        SELECT name FROM sqlite_master
                         WHERE type='table' AND name='action_decisions'
                        """
                    ).fetchone()
                )
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
