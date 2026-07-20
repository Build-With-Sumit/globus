from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from globus_truth import outcome_challenge
from globus_truth.service import TruthService
from globus_truth.storage import TruthRepository


NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)


class BusinessOutcomeChallengeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.challenge_root = self.root / "outcome-challenges"
        self.service = TruthService(
            TruthRepository(self.root / "truth.db"),
            clock=lambda: NOW,
        )

    def tearDown(self) -> None:
        self.service.repository.close()
        self.temp.cleanup()

    def run_challenge(self) -> dict:
        return self.service.run_outcome_gate_challenge(
            artifact_root=self.challenge_root,
        )

    def destination_path(self, result: dict) -> Path:
        return (
            self.challenge_root
            / Path(result["destination"]["relative_path"])
        )

    @staticmethod
    def database_counts(database: Path) -> tuple[int, int]:
        with closing(sqlite3.connect(database)) as connection:
            follow_ups = connection.execute(
                "SELECT COUNT(*) FROM destination_follow_ups"
            ).fetchone()[0]
            outbox = connection.execute(
                "SELECT COUNT(*) FROM local_outbox"
            ).fetchone()[0]
        return int(follow_ups), int(outbox)

    def test_real_destination_state_flips_verdict_and_blocks_second_action(
        self,
    ) -> None:
        real_insert = outcome_challenge._insert_local_outbox
        with patch.object(
            outcome_challenge,
            "_insert_local_outbox",
            wraps=real_insert,
        ) as action_callback:
            result = self.run_challenge()

        database = self.destination_path(result)
        self.assertTrue(result["expectations_met"])
        self.assertEqual(
            [phase["verdict"] for phase in result["phases"]],
            ["healthy", "degraded_contradictory"],
        )
        self.assertEqual(
            [phase["gate"]["authorized"] for phase in result["phases"]],
            [True, False],
        )
        self.assertEqual(
            [phase["observed_count"] for phase in result["phases"]],
            [3, 2],
        )
        self.assertNotEqual(
            result["destination"]["before_sha256"],
            result["destination"]["after_sha256"],
        )
        self.assertEqual(result["destination"]["rows_modified"], 1)
        self.assertTrue(result["action"]["first_executed"])
        self.assertFalse(result["action"]["second_executed"])
        self.assertTrue(result["action"]["second_prevented"])
        self.assertEqual(result["action"]["final_outbox_rows"], 1)
        self.assertEqual(action_callback.call_count, 1)
        self.assertEqual(self.database_counts(database), (2, 1))

        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT record_id, route, sequence, state
                      FROM destination_follow_ups
                     ORDER BY record_id
                    """
                ).fetchall()
            ]
            outbox = connection.execute(
                """
                SELECT action_id, action_kind
                  FROM local_outbox
                """
            ).fetchone()
        canonical = json.dumps(
            rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(canonical).hexdigest(),
            result["destination"]["after_sha256"],
        )
        self.assertEqual(outbox[0], result["action"]["action_id"])
        self.assertEqual(outbox[1], result["action"]["kind"])

        for phase in result["phases"]:
            stored = self.service.get_run(phase["storage_id"])
            self.assertIsNotNone(stored)
            self.assertEqual(
                stored["receipt"]["receipt_id"],
                phase["receipt_id"],
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
                stored["evaluation"]["verdict"],
                phase["verdict"],
            )

    def test_response_is_confined_payload_free_and_deidentified(self) -> None:
        result = self.run_challenge()
        database = self.destination_path(result)
        serialized = json.dumps(result, sort_keys=True)
        relative = Path(result["destination"]["relative_path"])

        self.assertFalse(relative.is_absolute())
        self.assertNotIn("..", relative.parts)
        self.assertNotIn("\\", result["destination"]["relative_path"])
        self.assertIn(self.challenge_root.resolve(), database.resolve().parents)
        self.assertNotEqual(database.resolve(), (self.root / "truth.db").resolve())
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(str(self.root).replace("\\", "\\\\"), serialized)
        self.assertNotIn("@", serialized)
        self.assertNotIn("payload", serialized.lower())
        self.assertNotIn("record_id", serialized)
        self.assertEqual(result["credential_free"], True)
        self.assertEqual(result["external_calls"], 0)

        allowed_gate_fields = {
            "decision_id",
            "storage_id",
            "action_id",
            "policy_id",
            "authorized",
            "observed_verdict",
            "verdict",
            "reason_codes",
            "decided_at",
            "binding_valid",
            "audit_verified",
        }
        for phase in result["phases"]:
            self.assertEqual(set(phase["gate"]), allowed_gate_fields)

        with closing(sqlite3.connect(database)) as connection:
            values = connection.execute(
                """
                SELECT record_id, route, state
                  FROM destination_follow_ups
                """
            ).fetchall()
        self.assertEqual(len(values), 2)
        self.assertTrue(
            all(
                "@" not in value
                for row in values
                for value in row
            )
        )

    def test_repeated_challenges_are_unique_and_isolated(self) -> None:
        first = self.run_challenge()
        second = self.run_challenge()

        self.assertTrue(first["expectations_met"])
        self.assertTrue(second["expectations_met"])
        self.assertNotEqual(first["challenge_id"], second["challenge_id"])
        self.assertNotEqual(
            first["destination"]["relative_path"],
            second["destination"]["relative_path"],
        )
        self.assertNotEqual(
            first["action"]["action_id"],
            second["action"]["action_id"],
        )
        self.assertEqual(
            self.database_counts(self.destination_path(first)),
            (2, 1),
        )
        self.assertEqual(
            self.database_counts(self.destination_path(second)),
            (2, 1),
        )
        self.assertEqual(
            len(list(self.challenge_root.glob("*/destination.sqlite"))),
            2,
        )
        self.assertEqual(self.service.summary()["total"], 4)

    def test_concurrent_challenges_remain_unique_and_consistent(self) -> None:
        challenge_count = 12
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(
                executor.map(
                    lambda _: self.run_challenge(),
                    range(challenge_count),
                )
            )

        self.assertTrue(all(result["expectations_met"] for result in results))
        self.assertEqual(
            len({result["challenge_id"] for result in results}),
            challenge_count,
        )
        self.assertEqual(
            len(
                {
                    result["destination"]["relative_path"]
                    for result in results
                }
            ),
            challenge_count,
        )
        self.assertTrue(
            all(
                self.database_counts(self.destination_path(result)) == (2, 1)
                for result in results
            )
        )
        self.assertEqual(self.service.summary()["total"], challenge_count * 2)

    def test_extra_mutation_is_detected_without_second_action_invocation(
        self,
    ) -> None:
        real_insert = outcome_challenge._insert_local_outbox

        def delete_two(database_path: Path, *, record_id: str) -> int:
            del record_id
            with closing(sqlite3.connect(database_path)) as connection:
                with connection:
                    rows = connection.execute(
                        """
                        SELECT record_id
                          FROM destination_follow_ups
                         ORDER BY record_id
                         LIMIT 2
                        """
                    ).fetchall()
                    cursor = connection.executemany(
                        """
                        DELETE FROM destination_follow_ups
                         WHERE record_id = ?
                        """,
                        rows,
                    )
            return max(int(cursor.rowcount), 0)

        with (
            patch.object(
                outcome_challenge,
                "_delete_one_follow_up",
                side_effect=delete_two,
            ),
            patch.object(
                outcome_challenge,
                "_insert_local_outbox",
                wraps=real_insert,
            ) as action_callback,
        ):
            result = self.run_challenge()

        self.assertFalse(result["expectations_met"])
        self.assertEqual(result["destination"]["rows_modified"], 2)
        self.assertEqual(result["destination"]["after_observed"], 1)
        self.assertEqual(result["phases"][1]["verdict"], "degraded_contradictory")
        self.assertFalse(result["phases"][1]["gate"]["authorized"])
        self.assertFalse(result["action"]["second_executed"])
        self.assertEqual(result["action"]["final_outbox_rows"], 1)
        self.assertEqual(action_callback.call_count, 1)

    def test_gate_failure_propagates_before_any_second_action(self) -> None:
        real_authorize = self.service.authorize_action
        real_insert = outcome_challenge._insert_local_outbox
        gate_calls = 0

        def fail_second_gate(*args, **kwargs):
            nonlocal gate_calls
            gate_calls += 1
            if gate_calls == 2:
                raise RuntimeError("simulated gate persistence failure")
            return real_authorize(*args, **kwargs)

        with (
            patch.object(
                self.service,
                "authorize_action",
                side_effect=fail_second_gate,
            ),
            patch.object(
                outcome_challenge,
                "_insert_local_outbox",
                wraps=real_insert,
            ) as action_callback,
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated gate"):
                self.run_challenge()

        databases = list(self.challenge_root.glob("*/destination.sqlite"))
        self.assertEqual(len(databases), 1)
        self.assertEqual(self.database_counts(databases[0]), (2, 1))
        self.assertEqual(action_callback.call_count, 1)

    def test_malformed_allow_decision_fails_closed(self) -> None:
        real_authorize = self.service.authorize_action
        real_insert = outcome_challenge._insert_local_outbox
        gate_calls = 0

        def malformed_first_gate(*args, **kwargs):
            nonlocal gate_calls
            gate_calls += 1
            decision = real_authorize(*args, **kwargs)
            if gate_calls == 1:
                decision["authorized"] = "yes"
            return decision

        with (
            patch.object(
                self.service,
                "authorize_action",
                side_effect=malformed_first_gate,
            ),
            patch.object(
                outcome_challenge,
                "_insert_local_outbox",
                wraps=real_insert,
            ) as action_callback,
        ):
            result = self.run_challenge()

        self.assertFalse(result["expectations_met"])
        self.assertFalse(result["phases"][0]["gate"]["authorized"])
        self.assertFalse(result["action"]["first_executed"])
        self.assertFalse(result["action"]["second_executed"])
        self.assertEqual(result["action"]["final_outbox_rows"], 0)
        self.assertEqual(action_callback.call_count, 0)

    def test_unpersisted_allow_decision_cannot_invoke_the_action(self) -> None:
        real_authorize = self.service.authorize_action
        real_insert = outcome_challenge._insert_local_outbox
        gate_calls = 0

        def unpersisted_first_allow(storage_id, action_id, **kwargs):
            nonlocal gate_calls
            gate_calls += 1
            if gate_calls == 1:
                return {
                    "decision_id": "not-in-the-audit-log",
                    "storage_id": storage_id,
                    "action_id": action_id,
                    "policy_id": kwargs["policy_id"],
                    "observed_verdict": "healthy",
                    "authorized": True,
                    "reason_codes": ["policy_satisfied"],
                    "decided_at": "2030-01-15T12:00:00.000000Z",
                }
            return real_authorize(storage_id, action_id, **kwargs)

        with (
            patch.object(
                self.service,
                "authorize_action",
                side_effect=unpersisted_first_allow,
            ),
            patch.object(
                outcome_challenge,
                "_insert_local_outbox",
                wraps=real_insert,
            ) as action_callback,
        ):
            result = self.run_challenge()

        self.assertFalse(result["expectations_met"])
        self.assertTrue(result["phases"][0]["gate"]["authorized"])
        self.assertFalse(result["phases"][0]["gate"]["audit_verified"])
        self.assertFalse(result["action"]["first_executed"])
        self.assertFalse(result["action"]["second_executed"])
        self.assertEqual(result["action"]["final_outbox_rows"], 0)
        self.assertEqual(action_callback.call_count, 0)


if __name__ == "__main__":
    unittest.main()
