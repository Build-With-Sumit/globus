from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from globus_truth.approval_center import (
    ApprovalAuditError,
    ApprovalCenter,
    ApprovalCenterError,
)
from globus_truth.fixtures import demo_receipts
from globus_truth.service import TruthService
from globus_truth.storage import (
    ActionProposalConflict,
    TruthRepository,
)


NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)
PAYLOAD_SHA256 = "a" * 64


def iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class MismatchingReadbackService:
    def __init__(self, service: TruthService) -> None:
        self.service = service
        self.repository = service.repository

    def authorize_action(self, *args: object, **kwargs: object) -> dict:
        return self.service.authorize_action(*args, **kwargs)

    def get_action_decision(self, decision_id: str) -> dict | None:
        decision = self.service.get_action_decision(decision_id)
        if decision is None:
            return None
        changed = dict(decision)
        changed["action_id"] = "forged-action"
        return changed


class DriftAfterReadbackService:
    def __init__(
        self,
        service: TruthService,
        clock: Clock,
        storage_id: str,
    ) -> None:
        self.service = service
        self.repository = service.repository
        self.clock = clock
        self.storage_id = storage_id

    def authorize_action(self, *args: object, **kwargs: object) -> dict:
        return self.service.authorize_action(*args, **kwargs)

    def get_action_decision(self, decision_id: str) -> dict | None:
        decision = self.service.get_action_decision(decision_id)
        self.clock.now = NOW + timedelta(days=2)
        refreshed = self.service.get_run(self.storage_id)
        assert refreshed is not None
        assert refreshed["evaluation"]["verdict"] == "stale"
        return decision


class ApprovalCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "truth.db"
        self.clock = Clock()
        self.repository = TruthRepository(self.database)
        self.service = TruthService(self.repository, clock=self.clock)
        healthy = self.service.ingest(demo_receipts(NOW)[0])
        self.storage_id = healthy["storage_id"]
        self.center = ApprovalCenter(self.service, clock=self.clock)

    def tearDown(self) -> None:
        self.repository.close()
        self.temp.cleanup()

    def submit(
        self,
        *,
        proposal_id: str = "proposal-001",
        action_id: str = "send-follow-up-001",
        expires_at: datetime | None = None,
    ) -> dict:
        return self.center.submit(
            proposal_id=proposal_id,
            storage_id=self.storage_id,
            action_id=action_id,
            policy_id="healthy_only",
            action_kind="local-outbox",
            payload_sha256=PAYLOAD_SHA256,
            requested_by="agent.sales-desk",
            risk="high",
            expires_at=iso(expires_at or (NOW + timedelta(hours=1))),
        )

    def approve(self, proposal_id: str = "proposal-001") -> dict:
        return self.center.decide(
            proposal_id,
            outcome="approved",
            decided_by="operator.sumit",
            reason_code="reviewed",
        )

    def test_proposal_is_privacy_safe_hashed_immutable_and_idempotent(self) -> None:
        created = self.submit()
        replayed = self.submit()

        self.assertTrue(created["created"])
        self.assertFalse(replayed["created"])
        self.assertEqual(
            created["proposal_sha256"],
            replayed["proposal_sha256"],
        )
        self.assertEqual(len(created["proposal_sha256"]), 64)
        self.assertNotIn("payload", str(created).replace("payload_sha256", ""))

        with self.assertRaises(ActionProposalConflict):
            self.center.submit(
                proposal_id="proposal-001",
                storage_id=self.storage_id,
                action_id="different-action",
                policy_id="healthy_only",
                action_kind="local-outbox",
                payload_sha256=PAYLOAD_SHA256,
                requested_by="agent.sales-desk",
                risk="high",
                expires_at=iso(NOW + timedelta(hours=1)),
            )
        with closing(sqlite3.connect(self.database)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE action_proposals SET risk='low' WHERE proposal_id=?",
                    ("proposal-001",),
                )

    def test_proposal_rejects_unsafe_fields_hashes_and_expiry(self) -> None:
        common = {
            "proposal_id": "proposal-safe",
            "storage_id": self.storage_id,
            "action_id": "action-safe",
            "policy_id": "healthy_only",
            "action_kind": "local-outbox",
            "payload_sha256": PAYLOAD_SHA256,
            "requested_by": "agent.sales-desk",
            "risk": "high",
            "expires_at": iso(NOW + timedelta(hours=1)),
        }
        for changed in (
            {"proposal_id": "../escape"},
            {"action_kind": "send private body"},
            {"payload_sha256": "not-a-hash"},
            {"risk": "harmless"},
            {"expires_at": iso(NOW)},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.center.submit(**{**common, **changed})

    def test_human_decision_is_single_hashed_and_irreversible(self) -> None:
        proposal = self.submit()
        approved = self.approve()
        replayed = self.approve()

        self.assertTrue(approved["created"])
        self.assertFalse(replayed["created"])
        self.assertEqual(approved["proposal_sha256"], proposal["proposal_sha256"])
        with self.assertRaises(ApprovalCenterError):
            self.center.decide(
                "proposal-001",
                outcome="rejected",
                decided_by="operator.sumit",
                reason_code="changed-mind",
            )
        with closing(sqlite3.connect(self.database)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE human_approval_decisions
                       SET outcome='rejected'
                     WHERE proposal_id='proposal-001'
                    """
                )

    def test_expired_proposal_cannot_be_approved_or_executed(self) -> None:
        self.submit(expires_at=NOW + timedelta(seconds=1))
        self.clock.now = NOW + timedelta(seconds=2)
        with self.assertRaises(ApprovalCenterError):
            self.approve()

        called = 0

        def callback() -> None:
            nonlocal called
            called += 1

        result = self.center.execute(
            "proposal-001",
            callback,
            payload_sha256=PAYLOAD_SHA256,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason_code"], "human_approval_missing")
        self.assertEqual(called, 0)

    def test_human_approval_alone_does_not_override_blocked_truth(self) -> None:
        failed = self.service.ingest(demo_receipts(NOW)[3])
        self.center.submit(
            proposal_id="proposal-failed",
            storage_id=failed["storage_id"],
            action_id="unsafe-send",
            policy_id="healthy_only",
            action_kind="local-outbox",
            payload_sha256=PAYLOAD_SHA256,
            requested_by="agent.sales-desk",
            risk="critical",
            expires_at=iso(NOW + timedelta(hours=1)),
        )
        self.center.decide(
            "proposal-failed",
            outcome="approved",
            decided_by="operator.sumit",
            reason_code="reviewed",
        )
        called = 0

        def callback() -> None:
            nonlocal called
            called += 1

        result = self.center.execute(
            "proposal-failed",
            callback,
            payload_sha256=PAYLOAD_SHA256,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason_code"], "truth_gate_blocked")
        self.assertIsNotNone(result["gate_decision_id"])
        self.assertEqual(called, 0)
        self.assertIsNone(
            self.repository.get_approval_execution_claim("proposal-failed")
        )

    def test_exact_gate_readback_then_unique_claim_and_success(self) -> None:
        self.submit()
        self.approve()
        called = 0

        def callback() -> None:
            nonlocal called
            called += 1

        first = self.center.execute(
            "proposal-001",
            callback,
            payload_sha256=PAYLOAD_SHA256,
        )
        second = self.center.execute(
            "proposal-001",
            callback,
            payload_sha256=PAYLOAD_SHA256,
        )

        self.assertEqual(first["status"], "succeeded")
        self.assertTrue(first["callback_invoked"])
        self.assertEqual(second["status"], "already_consumed")
        self.assertEqual(second["completion_status"], "succeeded")
        self.assertEqual(second["reason_code"], "approval_already_consumed")
        self.assertFalse(second["callback_invoked"])
        self.assertEqual(first["claim_id"], second["claim_id"])
        self.assertEqual(called, 1)
        gate = self.service.get_action_decision(first["gate_decision_id"])
        self.assertIsNotNone(gate)
        claim = self.repository.get_approval_execution_claim("proposal-001")
        self.assertEqual(claim["gate_decision_id"], gate["decision_id"])
        completion = self.repository.get_approval_execution_completion(
            claim["claim_id"]
        )
        self.assertEqual(completion["outcome"], "succeeded")
        with closing(sqlite3.connect(self.database)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE approval_execution_claims
                       SET action_id='changed'
                     WHERE claim_id=?
                    """,
                    (claim["claim_id"],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    DELETE FROM approval_execution_completions
                     WHERE claim_id=?
                    """,
                    (claim["claim_id"],),
                )

    def test_changed_payload_is_blocked_before_gate_or_callback(self) -> None:
        self.submit()
        self.approve()
        called = 0

        def callback() -> None:
            nonlocal called
            called += 1

        for changed_digest in ("b" * 64, "not-a-digest"):
            result = self.center.execute(
                "proposal-001",
                callback,
                payload_sha256=changed_digest,
            )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason_code"], "approval_scope_mismatch")
        self.assertEqual(called, 0)
        self.assertEqual(self.repository.list_action_decisions(), [])
        self.assertIsNone(
            self.repository.get_approval_execution_claim("proposal-001")
        )

    def test_consumed_approval_cannot_be_replayed_with_changed_payload(self) -> None:
        self.submit()
        self.approve()
        called = 0

        def callback() -> None:
            nonlocal called
            called += 1

        first = self.center.execute(
            "proposal-001",
            callback,
            payload_sha256=PAYLOAD_SHA256,
        )
        changed = self.center.execute(
            "proposal-001",
            callback,
            payload_sha256="b" * 64,
        )
        exact = self.center.execute(
            "proposal-001",
            callback,
            payload_sha256=PAYLOAD_SHA256,
        )

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(changed["status"], "blocked")
        self.assertEqual(changed["reason_code"], "approval_scope_mismatch")
        self.assertEqual(exact["status"], "already_consumed")
        self.assertEqual(exact["reason_code"], "approval_already_consumed")
        self.assertEqual(exact["completion_status"], "succeeded")
        self.assertEqual(called, 1)

    def test_callback_failure_is_generic_immutable_and_never_retried(self) -> None:
        self.submit()
        self.approve()
        secret = "password=customer-secret"
        called = 0

        def callback() -> None:
            nonlocal called
            called += 1
            raise RuntimeError(secret)

        first = self.center.execute(
            "proposal-001",
            callback,
            payload_sha256=PAYLOAD_SHA256,
        )
        second = self.center.execute(
            "proposal-001",
            callback,
            payload_sha256=PAYLOAD_SHA256,
        )

        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["reason_code"], "callback_failed")
        self.assertEqual(second["status"], "already_consumed")
        self.assertEqual(second["completion_status"], "failed")
        self.assertEqual(called, 1)
        self.assertNotIn(secret, str(first))
        with closing(sqlite3.connect(self.database)) as connection:
            stored = str(
                connection.execute(
                    "SELECT * FROM approval_execution_completions"
                ).fetchall()
            )
        self.assertNotIn(secret, stored)

    def test_mismatched_gate_readback_fails_closed(self) -> None:
        self.submit()
        self.approve()
        center = ApprovalCenter(
            MismatchingReadbackService(self.service),
            clock=self.clock,
        )
        called = 0

        def callback() -> None:
            nonlocal called
            called += 1

        with self.assertRaises(ApprovalAuditError):
            center.execute(
                "proposal-001",
                callback,
                payload_sha256=PAYLOAD_SHA256,
            )
        self.assertEqual(called, 0)
        self.assertIsNone(
            self.repository.get_approval_execution_claim("proposal-001")
        )

    def test_claim_audit_failure_is_generic_and_never_calls_action(self) -> None:
        self.submit()
        self.approve()
        called = 0
        original = self.repository.claim_approved_execution

        def fail_claim(*args: object, **kwargs: object) -> None:
            raise sqlite3.OperationalError("token=private-db-detail")

        self.repository.claim_approved_execution = fail_claim

        def callback() -> None:
            nonlocal called
            called += 1

        try:
            with self.assertRaises(ApprovalAuditError) as caught:
                self.center.execute(
                    "proposal-001",
                    callback,
                    payload_sha256=PAYLOAD_SHA256,
                )
        finally:
            self.repository.claim_approved_execution = original

        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(called, 0)
        self.assertIsNone(
            self.repository.get_approval_execution_claim("proposal-001")
        )

    def test_truth_drift_between_gate_and_claim_is_rechecked_atomically(self) -> None:
        self.submit(expires_at=NOW + timedelta(days=3))
        self.approve()
        center = ApprovalCenter(
            DriftAfterReadbackService(
                self.service,
                self.clock,
                self.storage_id,
            ),
            clock=self.clock,
        )
        called = 0

        def callback() -> None:
            nonlocal called
            called += 1

        result = center.execute(
            "proposal-001",
            callback,
            payload_sha256=PAYLOAD_SHA256,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason_code"], "claim_preconditions_failed")
        self.assertEqual(called, 0)
        self.assertIsNone(
            self.repository.get_approval_execution_claim("proposal-001")
        )

    def test_concurrent_execution_invokes_callback_at_most_once(self) -> None:
        self.submit()
        self.approve()
        lock = threading.Lock()
        callback_count = 0
        repositories = [TruthRepository(self.database) for _ in range(8)]
        centers = [
            ApprovalCenter(
                TruthService(repository, clock=self.clock),
                clock=self.clock,
            )
            for repository in repositories
        ]

        def callback() -> None:
            nonlocal callback_count
            with lock:
                callback_count += 1

        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(
                    pool.map(
                        lambda index: centers[index].execute(
                            "proposal-001",
                            callback,
                            payload_sha256=PAYLOAD_SHA256,
                        ),
                        range(8),
                    )
                )
        finally:
            for repository in repositories:
                repository.close()

        self.assertEqual(callback_count, 1)
        self.assertEqual(
            len({item["claim_id"] for item in results}),
            1,
        )
        self.assertEqual(
            sum(1 for item in results if item["callback_invoked"]),
            1,
        )
        final = self.center.get("proposal-001")
        self.assertEqual(final["state"], "succeeded")

    def test_concurrent_approve_reject_race_has_one_terminal_outcome(self) -> None:
        self.submit()
        repositories = [TruthRepository(self.database) for _ in range(8)]
        centers = [
            ApprovalCenter(
                TruthService(repository, clock=self.clock),
                clock=self.clock,
            )
            for repository in repositories
        ]
        barrier = threading.Barrier(8)

        def decide(index: int) -> str:
            barrier.wait()
            outcome = "approved" if index % 2 == 0 else "rejected"
            try:
                result = centers[index].decide(
                    "proposal-001",
                    outcome=outcome,
                    decided_by=f"operator.{index}",
                    reason_code="reviewed",
                )
                return result["outcome"]
            except ApprovalCenterError:
                return "conflict"

        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                outcomes = list(pool.map(decide, range(8)))
        finally:
            for repository in repositories:
                repository.close()

        stored = self.repository.get_human_approval("proposal-001")
        self.assertIn(stored["outcome"], {"approved", "rejected"})
        self.assertEqual(
            sum(1 for outcome in outcomes if outcome != "conflict"),
            1,
        )

    def test_completion_audit_failure_leaves_claim_and_never_retries_callback(
        self,
    ) -> None:
        self.submit()
        self.approve()
        callback_count = 0
        original = self.repository.save_approval_execution_completion

        def fail_completion(completion: object) -> None:
            raise sqlite3.OperationalError("private database path")

        self.repository.save_approval_execution_completion = fail_completion

        def callback() -> None:
            nonlocal callback_count
            callback_count += 1

        try:
            with self.assertRaises(ApprovalAuditError) as caught:
                self.center.execute(
                    "proposal-001",
                    callback,
                    payload_sha256=PAYLOAD_SHA256,
                )
        finally:
            self.repository.save_approval_execution_completion = original

        self.assertNotIn("private", str(caught.exception))
        replay = self.center.execute(
            "proposal-001",
            callback,
            payload_sha256=PAYLOAD_SHA256,
        )
        self.assertEqual(replay["status"], "already_consumed")
        self.assertEqual(replay["completion_status"], "claimed")
        self.assertEqual(callback_count, 1)


if __name__ == "__main__":
    unittest.main()
