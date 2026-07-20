from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
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


if __name__ == "__main__":
    unittest.main()
