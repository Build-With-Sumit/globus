from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from globus_truth import judge_mode
from globus_truth.judge_mode import run_artifact_tamper_challenge
from globus_truth.service import TruthService
from globus_truth.storage import TruthRepository


NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)


class JudgeModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.artifact_root = self.root / "judge-artifacts"
        self.service = TruthService(
            TruthRepository(self.root / "truth.db"),
            clock=lambda: NOW,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_challenge(self) -> dict:
        return run_artifact_tamper_challenge(
            self.service,
            artifact_root=self.artifact_root,
        )

    def artifact_path(self, result: dict) -> Path:
        return self.artifact_root / Path(result["artifact"]["relative_path"])

    def test_real_one_byte_append_changes_hash_and_flips_verdict(self) -> None:
        result = self.run_challenge()
        artifact = self.artifact_path(result)
        actual_bytes = artifact.read_bytes()

        self.assertTrue(result["expectations_met"])
        self.assertEqual(
            [phase["verdict"] for phase in result["phases"]],
            ["healthy", "degraded_contradictory"],
        )
        self.assertEqual(
            [phase["valid"] for phase in result["phases"]],
            [True, False],
        )
        self.assertTrue(result["phases"][0]["size_matches"])
        self.assertTrue(result["phases"][0]["sha256_matches"])
        self.assertFalse(result["phases"][1]["size_matches"])
        self.assertFalse(result["phases"][1]["sha256_matches"])
        self.assertEqual(
            result["artifact"]["final_bytes"],
            result["artifact"]["expected_bytes"] + 1,
        )
        self.assertEqual(len(actual_bytes), result["artifact"]["final_bytes"])
        self.assertEqual(
            hashlib.sha256(actual_bytes).hexdigest(),
            result["artifact"]["final_sha256"],
        )
        self.assertNotEqual(
            result["artifact"]["expected_sha256"],
            result["artifact"]["final_sha256"],
        )
        self.assertEqual(actual_bytes[-1:], b"!")

        real_verify = judge_mode.verify_artifact_readback
        calls = 0

        def contaminate_before_second_readback(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                with Path(args[0]).open("ab") as handle:
                    handle.write(b"?")
            return real_verify(*args, **kwargs)

        with patch.object(
            judge_mode,
            "verify_artifact_readback",
            side_effect=contaminate_before_second_readback,
        ):
            contaminated = self.run_challenge()
        self.assertFalse(contaminated["expectations_met"])
        self.assertEqual(
            contaminated["artifact"]["final_bytes"],
            contaminated["artifact"]["expected_bytes"] + 2,
        )

    def test_both_immutable_receipts_are_persisted_with_real_measurements(
        self,
    ) -> None:
        result = self.run_challenge()
        expected_verdicts = {
            "before_tamper": "healthy",
            "after_tamper": "degraded_contradictory",
        }

        for phase in result["phases"]:
            stored = self.service.get_run(phase["storage_id"])
            self.assertIsNotNone(stored)
            self.assertEqual(
                stored["evaluation"]["verdict"],
                expected_verdicts[phase["name"]],
            )
            self.assertEqual(
                stored["receipt"]["metadata"]["challenge_id"],
                result["challenge_id"],
            )
            self.assertEqual(
                stored["receipt"]["metadata"]["phase"],
                phase["name"],
            )
            self.assertEqual(
                stored["receipt"]["evidence"][0]["sha256"],
                phase["observed_sha256"],
            )
            self.assertEqual(
                len(self.service.verdict_history(phase["storage_id"])),
                1,
            )

        after = self.service.get_run(result["phases"][1]["storage_id"])
        failed_receipt_checks = {
            check["name"]
            for check in after["receipt"]["checks"]
            if not check["passed"]
        }
        self.assertEqual(
            failed_receipt_checks,
            {"artifact_size_matches", "artifact_sha256_matches"},
        )
        failed_evaluator_checks = {
            check["name"]
            for check in after["evaluation"]["checks"]
            if not check["passed"]
        }
        self.assertIn("agent_checks_passed", failed_evaluator_checks)
        self.assertEqual(
            self.service.summary(),
            {
                "total": 2,
                "trusted": 1,
                "attention": 1,
                "verdicts": {
                    "healthy": 1,
                    "verified_no_work": 0,
                    "degraded_contradictory": 1,
                    "failed": 0,
                    "stale": 0,
                },
            },
        )

    def test_repeated_challenges_are_unique_and_confined_to_the_root(
        self,
    ) -> None:
        first = self.run_challenge()
        second = self.run_challenge()

        self.assertNotEqual(first["challenge_id"], second["challenge_id"])
        self.assertNotEqual(
            first["artifact"]["relative_path"],
            second["artifact"]["relative_path"],
        )
        self.assertTrue(self.artifact_path(first).is_file())
        self.assertTrue(self.artifact_path(second).is_file())
        resolved_root = self.artifact_root.resolve()
        for result in (first, second):
            artifact = self.artifact_path(result).resolve()
            self.assertIn(resolved_root, artifact.parents)
            relative = Path(result["artifact"]["relative_path"])
            self.assertFalse(relative.is_absolute())
            self.assertNotIn("..", relative.parts)
        self.assertEqual(
            len(list(self.artifact_root.glob("*/manifest.json"))),
            2,
        )
        self.assertEqual(self.service.summary()["total"], 4)

    def test_result_exposes_no_absolute_or_private_filesystem_data(self) -> None:
        result = self.run_challenge()
        serialized = json.dumps(result, sort_keys=True)
        relative_path = result["artifact"]["relative_path"]

        self.assertFalse(Path(relative_path).is_absolute())
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(str(self.root).replace("\\", "\\\\"), serialized)
        self.assertNotIn("\\", relative_path)
        self.assertNotIn("..", Path(relative_path).parts)
        self.assertEqual(result["credential_free"], True)
        self.assertEqual(result["external_calls"], 0)
        for phase in result["phases"]:
            self.assertEqual(
                set(phase),
                {
                    "name",
                    "action",
                    "storage_id",
                    "verdict",
                    "valid",
                    "observed_bytes",
                    "observed_sha256",
                    "size_matches",
                    "sha256_matches",
                },
            )


if __name__ == "__main__":
    unittest.main()
