from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import globus_truth.approval_challenge as approval_challenge_module
from globus_truth.approval_challenge import (
    resolve_approval_center_challenge,
    stage_approval_center_challenge,
)
from globus_truth.service import TruthService
from globus_truth.storage import TruthRepository


NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ApprovalChallengeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.challenge_root = self.root / "approval-challenges"
        self.repository = TruthRepository(self.root / "truth.db")
        self.service = TruthService(self.repository, clock=lambda: NOW)

    def tearDown(self) -> None:
        self.repository.close()
        self.temp.cleanup()

    def destination(self, report: dict) -> Path:
        return (
            self.challenge_root
            / report["proposal_id"]
            / "approval-destination.sqlite"
        )

    @staticmethod
    def counts(database: Path) -> tuple[int, int]:
        with closing(sqlite3.connect(database)) as connection:
            targets = connection.execute(
                "SELECT COUNT(*) FROM approval_targets"
            ).fetchone()[0]
            outbox = connection.execute(
                "SELECT COUNT(*) FROM local_outbox"
            ).fetchone()[0]
        return int(targets), int(outbox)

    def stage(self) -> dict:
        return stage_approval_center_challenge(
            self.service,
            artifact_root=self.challenge_root,
        )

    def resolve(self, proposal_id: str, disposition: str) -> dict:
        return resolve_approval_center_challenge(
            self.service,
            proposal_id,
            disposition=disposition,
            artifact_root=self.challenge_root,
        )

    def test_stage_pauses_with_healthy_evidence_and_zero_actions(self) -> None:
        report = self.stage()
        database = self.destination(report)

        self.assertEqual(report["status"], "pending")
        self.assertTrue(report["credential_free"])
        self.assertEqual(report["external_calls"], 0)
        self.assertEqual(report["action"]["executions"], 0)
        self.assertTrue(report["action"]["bounded_local_only"])
        self.assertEqual(report["proposal"]["truth_verdict"], "healthy")
        self.assertEqual(report["proposal"]["risk"], "high")
        self.assertEqual(report["proposal"]["approval_mode"], "explicit")
        self.assertEqual(report["proposal"]["max_uses"], 1)
        self.assertEqual(len(report["proposal"]["payload_sha256"]), 64)
        self.assertEqual(self.counts(database), (1, 0))

        state = self.service.approval_center().get(report["proposal_id"])
        self.assertEqual(state["state"], "pending")
        stored = self.service.get_run(
            report["proposal"]["truth_storage_id"]
        )
        self.assertEqual(stored["evaluation"]["verdict"], "healthy")

    def test_human_approve_proves_scope_exact_once_and_replay_block(self) -> None:
        pending = self.stage()
        result = self.resolve(pending["proposal_id"], "approved")
        database = self.destination(pending)

        self.assertTrue(result["expectations_met"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["disposition"], "approved")
        self.assertEqual(result["external_calls"], 0)
        self.assertEqual(result["action"]["execution_count"], 1)
        self.assertEqual(result["action"]["final_outbox_rows"], 1)
        self.assertEqual(self.counts(database), (1, 1))

        attempts = {item["name"]: item for item in result["attempts"]}
        self.assertFalse(attempts["changed_payload"]["authorized"])
        self.assertFalse(attempts["changed_payload"]["executed"])
        self.assertEqual(
            attempts["changed_payload"]["reason_codes"],
            ["approval_scope_mismatch"],
        )
        self.assertTrue(attempts["exact_payload"]["authorized"])
        self.assertTrue(attempts["exact_payload"]["executed"])
        self.assertFalse(attempts["replay"]["authorized"])
        self.assertFalse(attempts["replay"]["executed"])
        self.assertEqual(
            attempts["replay"]["reason_codes"],
            ["approval_already_consumed"],
        )
        self.assertNotEqual(
            attempts["changed_payload"]["payload_sha256"],
            attempts["exact_payload"]["payload_sha256"],
        )

        audit = result["audit"]
        self.assertEqual(audit["state"], "succeeded")
        self.assertEqual(audit["approval"]["outcome"], "approved")
        self.assertEqual(audit["completion"]["outcome"], "succeeded")
        self.assertIsNotNone(
            self.service.get_action_decision(
                audit["claim"]["gate_decision_id"]
            )
        )

    def test_human_rejection_keeps_local_outbox_empty(self) -> None:
        pending = self.stage()
        result = self.resolve(pending["proposal_id"], "rejected")

        self.assertTrue(result["expectations_met"])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["disposition"], "rejected")
        self.assertEqual(result["attempts"], [])
        self.assertEqual(result["action"]["final_outbox_rows"], 0)
        self.assertEqual(self.counts(self.destination(pending)), (1, 0))
        self.assertEqual(result["audit"]["state"], "rejected")
        self.assertEqual(self.repository.list_action_decisions(), [])

    def test_changed_destination_evidence_fails_closed_at_resolution(self) -> None:
        pending = self.stage()
        database = self.destination(pending)
        with closing(sqlite3.connect(database)) as connection:
            with connection:
                connection.execute(
                    "UPDATE approval_targets SET state = 'compromised'"
                )

        result = self.resolve(pending["proposal_id"], "approved")

        self.assertFalse(result["expectations_met"])
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["action"]["final_outbox_rows"], 0)
        self.assertEqual(self.counts(database), (1, 0))
        self.assertEqual(len(result["attempts"]), 1)
        self.assertFalse(result["attempts"][0]["authorized"])
        self.assertFalse(result["attempts"][0]["executed"])
        self.assertEqual(
            result["attempts"][0]["reason_codes"],
            ["destination_evidence_changed"],
        )
        self.assertEqual(result["audit"]["state"], "approved")
        self.assertIsNone(result["audit"]["claim"])

    def test_destination_drift_after_readback_rolls_back_local_effect(self) -> None:
        pending = self.stage()
        database = self.destination(pending)
        original = approval_challenge_module._resolution_evidence

        def drift_after_readback(*args: object, **kwargs: object):
            evidence = original(*args, **kwargs)
            with closing(sqlite3.connect(database)) as connection:
                with connection:
                    connection.execute(
                        "UPDATE approval_targets SET state = 'raced'"
                    )
            return evidence

        with patch.object(
            approval_challenge_module,
            "_resolution_evidence",
            side_effect=drift_after_readback,
        ):
            result = self.resolve(pending["proposal_id"], "approved")

        attempts = {item["name"]: item for item in result["attempts"]}
        self.assertFalse(result["expectations_met"])
        self.assertEqual(result["status"], "needs_attention")
        self.assertFalse(result["action"]["first_executed"])
        self.assertEqual(result["action"]["final_outbox_rows"], 0)
        self.assertEqual(self.counts(database), (1, 0))
        self.assertTrue(attempts["exact_payload"]["authorized"])
        self.assertFalse(attempts["exact_payload"]["executed"])
        self.assertEqual(
            attempts["exact_payload"]["reason_codes"],
            ["callback_failed"],
        )
        self.assertFalse(attempts["replay"]["authorized"])
        # The Truth claim is the authorization linearization point and remains
        # immutable.  The later destination transaction rejected the changed
        # evidence, so no local effect was committed and replay stays blocked.
        self.assertEqual(result["audit"]["state"], "failed")
        self.assertIsNotNone(result["audit"]["claim"])

    def test_stale_truth_evidence_fails_closed_at_resolution(self) -> None:
        clock = Clock(NOW)
        self.service = TruthService(
            self.repository,
            clock=clock,
            stale_after=timedelta(minutes=1),
        )
        pending = self.stage()
        clock.now = NOW + timedelta(minutes=2)

        result = self.resolve(pending["proposal_id"], "approved")

        self.assertFalse(result["expectations_met"])
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["action"]["final_outbox_rows"], 0)
        self.assertEqual(self.counts(self.destination(pending)), (1, 0))
        self.assertEqual(len(result["attempts"]), 1)
        self.assertFalse(result["attempts"][0]["authorized"])
        self.assertFalse(result["attempts"][0]["executed"])
        self.assertEqual(
            result["attempts"][0]["reason_codes"],
            ["truth_evidence_not_current"],
        )
        self.assertEqual(result["proposal"]["truth_verdict"], "stale")
        self.assertEqual(result["audit"]["state"], "approved")
        self.assertIsNone(result["audit"]["claim"])

    def test_reports_are_deidentified_payload_free_and_path_safe(self) -> None:
        pending = self.stage()
        result = self.resolve(pending["proposal_id"], "approved")
        serialized = json.dumps(
            {"pending": pending, "result": result},
            sort_keys=True,
        )

        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(str(self.root).replace("\\", "\\\\"), serialized)
        self.assertNotIn("@", serialized)
        self.assertNotIn("target_id", serialized)
        self.assertNotIn("recipient", serialized.lower().replace(
            "generated recipient", ""
        ))
        self.assertNotIn('"payload":', serialized)
        self.assertTrue(
            self.destination(pending).resolve().is_relative_to(
                self.challenge_root.resolve()
            )
        )

    def test_invalid_or_cross_root_identifiers_are_rejected(self) -> None:
        for proposal_id in ("../escape", "not-a-challenge", "approval-missing"):
            with self.subTest(proposal_id=proposal_id), self.assertRaises(
                (ValueError, FileNotFoundError)
            ):
                self.resolve(proposal_id, "approved")
        with self.assertRaises(ValueError):
            self.resolve(self.stage()["proposal_id"], "maybe")


if __name__ == "__main__":
    unittest.main()
