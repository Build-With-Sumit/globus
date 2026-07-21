from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from globus_truth.approval_center import ApprovalAuditError, ApprovalCenter
from globus_truth.fixtures import demo_receipts
from globus_truth.reference_actions import CRMNoteAdapter, EmailDraftAdapter
from globus_truth.service import TruthService
from globus_truth.storage import (
    TruthRepository,
    VerifiedActionVerificationConflict,
)
from globus_truth.verified_actions import canonical_json_bytes


NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)


def record_hash(record: dict) -> str:
    import hashlib

    fields = {
        key: value
        for key, value in record.items()
        if key != "verification_sha256"
    }
    return hashlib.sha256(canonical_json_bytes(fields)).hexdigest()


class VerifiedActionLabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = TruthRepository(self.root / "truth.sqlite")
        self.service = TruthService(self.repository, clock=lambda: NOW)
        self.artifacts = self.root / "lab"

    def tearDown(self) -> None:
        self.repository.close()
        self.temp.cleanup()

    def test_manifest_inventory_is_fixed_local_and_payload_free(self) -> None:
        inventory = self.service.verified_action_manifests()
        self.assertEqual(
            inventory["schema_version"],
            "globus.verified-action.manifests/v1",
        )
        self.assertEqual(inventory["external_calls"], 0)
        manifests = inventory["manifests"]
        self.assertEqual(
            {item["id"] for item in manifests},
            {
                EmailDraftAdapter.manifest.id,
                CRMNoteAdapter.manifest.id,
            },
        )
        self.assertTrue(
            all(item["action_kind"].startswith("verified.") for item in manifests)
        )
        serialized = json.dumps(inventory, sort_keys=True)
        self.assertNotIn("api_key", serialized.lower())
        self.assertNotIn("connected\": true", serialized.lower())

    def test_both_reference_adapters_pause_execute_verify_and_replay_block(
        self,
    ) -> None:
        for adapter_id in (
            EmailDraftAdapter.manifest.id,
            CRMNoteAdapter.manifest.id,
        ):
            with self.subTest(adapter_id=adapter_id):
                staged = self.service.stage_verified_action_lab(
                    adapter_id=adapter_id,
                    artifact_root=self.artifacts,
                )
                self.assertEqual(staged["status"], "pending")
                self.assertEqual(staged["external_calls"], 0)
                self.assertEqual(
                    staged["destination"]["observed_records"],
                    0,
                )
                self.assertEqual(len(staged["timeline"]["events"]), 6)
                self.assertEqual(
                    [event["event_type"] for event in staged["timeline"]["events"]],
                    [
                        "proposed",
                        "human_decision",
                        "truth_gate",
                        "execution_claimed",
                        "destination_verification",
                        "completed",
                    ],
                )

                result = self.service.resolve_verified_action_lab(
                    staged["proposal_id"],
                    disposition="approved",
                    artifact_root=self.artifacts,
                )
                self.assertTrue(result["expectations_met"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["external_calls"], 0)
                self.assertEqual(
                    result["destination"]["observed_records"],
                    1,
                )
                self.assertTrue(result["destination"]["verified"])
                self.assertTrue(result["verification"]["verified"])
                self.assertEqual(
                    result["verification"]["reason_code"],
                    "destination_readback_verified",
                )
                self.assertEqual(result["timeline"]["state"], "succeeded")
                self.assertTrue(result["timeline"]["terminal"])
                self.assertTrue(result["timeline"]["integrity_complete"])
                self.assertEqual(result["timeline"]["missing_stages"], [])
                self.assertEqual(
                    result["replay"]["reason_code"],
                    "approval_already_consumed",
                )

                replay = self.service.resolve_verified_action_lab(
                    staged["proposal_id"],
                    disposition="approved",
                    artifact_root=self.artifacts,
                )
                self.assertTrue(replay["expectations_met"])
                self.assertEqual(
                    replay["destination"]["observed_records"],
                    1,
                )
                self.assertIsNone(replay["execution"])

                serialized = json.dumps(result, sort_keys=True)
                for raw in (
                    "This is a generated local draft",
                    "Generated local CRM note",
                    "@example.test",
                ):
                    self.assertNotIn(raw, serialized)

    def test_human_rejection_is_terminal_with_zero_destination_effects(self) -> None:
        staged = self.service.stage_verified_action_lab(
            adapter_id=EmailDraftAdapter.manifest.id,
            artifact_root=self.artifacts,
        )
        result = self.service.resolve_verified_action_lab(
            staged["proposal_id"],
            disposition="rejected",
            artifact_root=self.artifacts,
        )
        self.assertTrue(result["expectations_met"])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["destination"]["observed_records"], 0)
        self.assertEqual(result["timeline"]["state"], "rejected")
        self.assertTrue(result["timeline"]["terminal"])
        self.assertTrue(result["timeline"]["integrity_complete"])
        self.assertTrue(
            all(
                event["outcome"] == "not_applicable"
                for event in result["timeline"]["events"][2:]
            )
        )

    def test_verification_is_immutable_hashed_bound_and_concurrent_safe(self) -> None:
        staged = self.service.stage_verified_action_lab(
            adapter_id=CRMNoteAdapter.manifest.id,
            artifact_root=self.artifacts,
        )
        self.service.resolve_verified_action_lab(
            staged["proposal_id"],
            disposition="approved",
            artifact_root=self.artifacts,
        )
        stored = self.repository.get_verified_action_verification(
            staged["proposal_id"]
        )
        self.assertIsNotNone(stored)
        assert stored is not None

        def exact_retry(_: int) -> bool:
            _, created = self.repository.save_verified_action_verification(
                deepcopy(stored)
            )
            return created

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(exact_retry, range(16)))
        self.assertEqual(results, [False] * 16)

        changed = deepcopy(stored)
        changed["observation_sha256"] = "a" * 64
        changed["verification_sha256"] = record_hash(changed)
        with self.assertRaises(VerifiedActionVerificationConflict):
            self.repository.save_verified_action_verification(changed)

        empty_verified = deepcopy(stored)
        empty_verified["observed_count"] = 0
        empty_verified["verification_sha256"] = record_hash(empty_verified)
        with self.assertRaises(ValueError):
            self.repository.save_verified_action_verification(empty_verified)

        with closing(sqlite3.connect(self.repository.database)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE verified_action_verifications
                       SET reason_code = 'forged'
                     WHERE proposal_id = ?
                    """,
                    (staged["proposal_id"],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    DELETE FROM verified_action_verifications
                     WHERE proposal_id = ?
                    """,
                    (staged["proposal_id"],),
                )

    def test_verified_prefix_cannot_complete_without_destination_proof(self) -> None:
        receipt = deepcopy(demo_receipts(NOW)[0])
        receipt["receipt_id"] = "verified-proof-required-truth"
        stored = self.service.ingest(receipt)
        center = ApprovalCenter(self.service, clock=self.service._now)
        center.submit(
            proposal_id="verified-proof-required",
            storage_id=stored["storage_id"],
            action_id="verified-proof-required-action",
            policy_id="healthy_only",
            action_kind="verified.test.effect",
            payload_sha256="b" * 64,
            requested_by="test-agent",
            risk="medium",
            expires_at=(
                NOW + timedelta(minutes=5)
            ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )
        center.decide(
            "verified-proof-required",
            outcome="approved",
            decided_by="test-human",
            reason_code="reviewed",
        )
        calls = 0

        def effect() -> None:
            nonlocal calls
            calls += 1

        with self.assertRaises(ApprovalAuditError):
            center.execute(
                "verified-proof-required",
                effect,
                payload_sha256="b" * 64,
            )
        self.assertEqual(calls, 1)
        self.assertIsNone(
            self.repository.get_approval_execution_completion(
                self.repository.get_approval_execution_claim(
                    "verified-proof-required"
                )["claim_id"]
            )
        )
        replay = center.execute(
            "verified-proof-required",
            effect,
            payload_sha256="b" * 64,
        )
        self.assertEqual(calls, 1)
        self.assertEqual(replay["reason_code"], "approval_already_consumed")
        timeline = self.service.get_verified_action_timeline(
            "verified-proof-required"
        )
        self.assertEqual(timeline["state"], "indeterminate")
        self.assertFalse(timeline["integrity_complete"])

    def test_expired_unclaimed_proposal_is_terminal_without_an_effect(self) -> None:
        current = [NOW]
        service = TruthService(
            self.repository,
            clock=lambda: current[0],
        )
        staged = service.stage_verified_action_lab(
            adapter_id=EmailDraftAdapter.manifest.id,
            artifact_root=self.artifacts,
        )
        current[0] = NOW + timedelta(minutes=11)
        timeline = service.get_verified_action_timeline(staged["proposal_id"])
        self.assertEqual(timeline["state"], "expired")
        self.assertTrue(timeline["terminal"])
        self.assertTrue(timeline["integrity_complete"])
        self.assertEqual(timeline["missing_stages"], [])
        self.assertEqual(
            [event["outcome"] for event in timeline["events"][1:]],
            ["not_applicable"] * 5,
        )
        self.assertTrue(
            all(
                event["reason_codes"] == ["proposal_expired"]
                for event in timeline["events"][1:]
            )
        )
        self.assertEqual(staged["destination"]["observed_records"], 0)

    def test_legacy_completion_is_not_retroactively_called_verified(self) -> None:
        receipt = deepcopy(demo_receipts(NOW)[0])
        receipt["receipt_id"] = "legacy-action-truth"
        stored = self.service.ingest(receipt)
        center = ApprovalCenter(self.service, clock=self.service._now)
        center.submit(
            proposal_id="legacy-action-proposal",
            storage_id=stored["storage_id"],
            action_id="legacy-action",
            policy_id="healthy_only",
            action_kind="legacy.local.effect",
            payload_sha256="c" * 64,
            requested_by="legacy-agent",
            risk="medium",
            expires_at=(
                NOW + timedelta(minutes=5)
            ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )
        center.decide(
            "legacy-action-proposal",
            outcome="approved",
            decided_by="legacy-human",
            reason_code="reviewed",
        )
        result = center.execute(
            "legacy-action-proposal",
            lambda: None,
            payload_sha256="c" * 64,
        )
        self.assertEqual(result["status"], "succeeded")
        timeline = self.service.get_verified_action_timeline(
            "legacy-action-proposal"
        )
        self.assertTrue(timeline["terminal"])
        self.assertFalse(timeline["integrity_complete"])
        self.assertTrue(timeline["legacy_unverified"])
        self.assertIn(
            "destination_verification",
            timeline["missing_stages"],
        )


if __name__ == "__main__":
    unittest.main()
