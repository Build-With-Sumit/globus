from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from globus_truth.__main__ import _read_json, main
from globus_truth.fixtures import demo_receipts
from globus_truth.service import TruthService
from globus_truth.storage import TruthRepository


class CliTests(unittest.TestCase):
    def test_bare_module_command_launches_safe_demo(self) -> None:
        with patch("globus_truth.__main__._serve", return_value=0) as serve:
            self.assertEqual(main([]), 0)
        args, kwargs = serve.call_args
        self.assertEqual(args[0].command, "demo")
        self.assertTrue(kwargs["load_demo"])
        self.assertEqual(args[0].host, "127.0.0.1")

    def test_server_command_refuses_non_loopback_bind(self) -> None:
        errors = io.StringIO()
        with (
            patch("globus_truth.__main__._serve") as serve,
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["serve", "--host", "0.0.0.0"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("local-only", errors.getvalue())
        serve.assert_not_called()

    def test_cli_rejects_non_finite_json_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text('{"metadata":{"latency":NaN}}', encoding="utf-8")
            with self.assertRaises(ValueError):
                _read_json(str(path))

    def test_gate_command_allows_only_from_a_persisted_healthy_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "truth.db"
            service = TruthService(TruthRepository(database))
            stored = service.ingest(demo_receipts(datetime.now(timezone.utc))[0])
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main([
                    "gate",
                    "--db",
                    str(database),
                    stored["storage_id"],
                    "--action-id",
                    "cli-safe-action",
                ])

            self.assertEqual(exit_code, 0)
            decision = json.loads(output.getvalue())
            self.assertTrue(decision["authorized"])
            self.assertEqual(decision["observed_verdict"], "healthy")
            self.assertEqual(decision["reason_codes"], ["policy_satisfied"])

    def test_gate_command_audits_missing_truth_and_exits_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "truth.db"
            output = io.StringIO()
            errors = io.StringIO()

            with redirect_stdout(output), redirect_stderr(errors):
                exit_code = main([
                    "gate",
                    "--db",
                    str(database),
                    "missing-receipt",
                    "--action-id",
                    "cli-blocked-action",
                ])

            self.assertEqual(exit_code, 1)
            self.assertEqual(errors.getvalue(), "")
            decision = json.loads(output.getvalue())
            self.assertFalse(decision["authorized"])
            self.assertEqual(decision["observed_verdict"], "missing")
            self.assertEqual(decision["reason_codes"], ["truth_record_missing"])

    def test_outcome_challenge_command_proves_allow_then_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "truth.db"
            artifacts = Path(directory) / "artifacts"
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main([
                    "outcome-challenge",
                    "--db",
                    str(database),
                    "--artifact-root",
                    str(artifacts),
                ])

            self.assertEqual(exit_code, 0)
            report = json.loads(output.getvalue())
            self.assertTrue(report["expectations_met"])
            self.assertTrue(report["credential_free"])
            self.assertEqual(report["external_calls"], 0)
            self.assertEqual(
                [phase["verdict"] for phase in report["phases"]],
                ["healthy", "degraded_contradictory"],
            )
            self.assertTrue(report["phases"][0]["gate"]["authorized"])
            self.assertFalse(report["phases"][1]["gate"]["authorized"])
            self.assertEqual(report["action"]["final_outbox_rows"], 1)

    def test_outcome_challenge_command_exits_blocked_when_proof_is_incomplete(
        self,
    ) -> None:
        output = io.StringIO()
        incomplete = {
            "expectations_met": False,
            "credential_free": True,
            "external_calls": 0,
        }

        with (
            patch.object(
                TruthService,
                "run_outcome_gate_challenge",
                return_value=incomplete,
            ),
            redirect_stdout(output),
            tempfile.TemporaryDirectory() as directory,
        ):
            exit_code = main([
                "outcome-challenge",
                "--db",
                str(Path(directory) / "truth.db"),
            ])

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue()), incomplete)

    def test_approval_propose_decide_and_list_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "truth.db"
            service = TruthService(TruthRepository(database))
            stored = service.ingest(
                demo_receipts(datetime.now(timezone.utc))[0]
            )
            expiry = (
                datetime.now(timezone.utc)
                + timedelta(hours=1)
            ).isoformat().replace("+00:00", "Z")
            proposed_output = io.StringIO()

            with redirect_stdout(proposed_output):
                proposed_exit = main([
                    "approval-propose",
                    "--db",
                    str(database),
                    stored["storage_id"],
                    "--proposal-id",
                    "cli-proposal-001",
                    "--action-id",
                    "cli-action-001",
                    "--action-kind",
                    "local-outbox",
                    "--payload-sha256",
                    "a" * 64,
                    "--requested-by",
                    "agent.sales-desk",
                    "--expires-at",
                    expiry,
                    "--risk",
                    "high",
                ])
            self.assertEqual(proposed_exit, 0)
            proposed = json.loads(proposed_output.getvalue())
            self.assertTrue(proposed["created"])
            self.assertEqual(proposed["proposal_id"], "cli-proposal-001")

            decision_output = io.StringIO()
            with redirect_stdout(decision_output):
                decision_exit = main([
                    "approval-decide",
                    "--db",
                    str(database),
                    "cli-proposal-001",
                    "--outcome",
                    "approved",
                    "--decided-by",
                    "operator.cli",
                    "--reason-code",
                    "reviewed",
                ])
            self.assertEqual(decision_exit, 0)
            decision = json.loads(decision_output.getvalue())
            self.assertEqual(decision["outcome"], "approved")

            list_output = io.StringIO()
            with redirect_stdout(list_output):
                list_exit = main([
                    "approval-list",
                    "--db",
                    str(database),
                    "--limit",
                    "10",
                ])
            self.assertEqual(list_exit, 0)
            listed = json.loads(list_output.getvalue())["proposals"]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["state"], "approved")
            self.assertNotIn('"payload":', list_output.getvalue())

    def test_approval_challenge_cli_requires_real_human_resolution_step(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "truth.db"
            artifacts = Path(directory) / "approval-artifacts"
            staged_output = io.StringIO()

            with redirect_stdout(staged_output):
                staged_exit = main([
                    "approval-challenge",
                    "--db",
                    str(database),
                    "--artifact-root",
                    str(artifacts),
                ])
            self.assertEqual(staged_exit, 0)
            staged = json.loads(staged_output.getvalue())
            self.assertEqual(staged["status"], "pending")
            self.assertEqual(staged["action"]["executions"], 0)

            resolved_output = io.StringIO()
            with redirect_stdout(resolved_output):
                resolved_exit = main([
                    "approval-challenge",
                    "--db",
                    str(database),
                    "--artifact-root",
                    str(artifacts),
                    "--proposal-id",
                    staged["proposal_id"],
                    "--decision",
                    "approved",
                ])
            self.assertEqual(resolved_exit, 0)
            resolved = json.loads(resolved_output.getvalue())
            self.assertTrue(resolved["expectations_met"])
            self.assertEqual(resolved["action"]["final_outbox_rows"], 1)
            attempts = {item["name"]: item for item in resolved["attempts"]}
            self.assertEqual(
                attempts["changed_payload"]["reason_codes"],
                ["approval_scope_mismatch"],
            )
            self.assertEqual(
                attempts["replay"]["reason_codes"],
                ["approval_already_consumed"],
            )


if __name__ == "__main__":
    unittest.main()
