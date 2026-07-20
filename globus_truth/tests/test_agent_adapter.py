from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from globus_truth import agent_adapter


class FakeRunDatabase:
    """Small in-memory stand-in for the runner's MySQL helper surface."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.next_id = 1

    def insert(self, sql: str, params: tuple) -> int:
        normalized = " ".join(sql.split()).lower()
        if not normalized.startswith("insert into globus_agent_runs"):
            raise AssertionError(f"unexpected insert SQL: {sql}")
        run_id = self.next_id
        self.write(sql, params)
        return run_id

    def write(self, sql: str, params: tuple) -> bool:
        normalized = " ".join(sql.split()).lower()
        now = datetime.now(timezone.utc)
        if normalized.startswith("insert into globus_agent_runs"):
            email, agent_name = params
            self.rows.append(
                {
                    "id": self.next_id,
                    "member_email": email,
                    "agent_name": agent_name,
                    "status": "running",
                    "started_at": now,
                    "finished_at": None,
                    "brief_path": "",
                    "bytes_written": 0,
                    "error_message": "",
                }
            )
            self.next_id += 1
            return True

        if "set status='ok'" in normalized:
            brief_path, bytes_written, run_id = params
            row = self._row(run_id)
            row.update(
                status="ok",
                brief_path=brief_path,
                bytes_written=bytes_written,
                finished_at=now,
            )
            return True

        if "set status='error'" in normalized:
            error_message, run_id = params
            row = self._row(run_id)
            row.update(
                status="error",
                error_message=error_message,
                finished_at=now,
            )
            return True

        raise AssertionError(f"unexpected write SQL: {sql}")

    def read(self, sql: str, params: tuple) -> list[dict]:
        normalized = " ".join(sql.split()).lower()

        if normalized.startswith(
            "select status, brief_path, bytes_written from globus_agent_runs"
        ):
            row = self._row(params[0])
            return [
                {
                    "status": row["status"],
                    "brief_path": row["brief_path"],
                    "bytes_written": row["bytes_written"],
                }
            ]

        if (
            normalized.startswith("select id from globus_agent_runs")
            and "status='running'" not in normalized
        ):
            email, agent_name = params
            matches = [
                row
                for row in self.rows
                if row["member_email"] == email
                and row["agent_name"] == agent_name
            ]
            matches.sort(key=lambda row: row["id"], reverse=True)
            return [{"id": matches[0]["id"]}] if matches else []

        if "timestampdiff(second" in normalized:
            rows = self._scope(params)
            now = datetime.now(timezone.utc)
            return [
                {
                    "id": row["id"],
                    "agent_name": row["agent_name"],
                    "member_email": row["member_email"],
                    "started_at": row["started_at"],
                    "runtime_sec": int((now - row["started_at"]).total_seconds()),
                }
                for row in rows
                if row["status"] == "running"
            ][:20]

        if "from globus_agent_runs r1" in normalized:
            rows = [row for row in self._scope(params) if row["status"] == "ok"]
            latest: dict[tuple[str, str], dict] = {}
            for row in sorted(rows, key=lambda item: item["id"], reverse=True):
                key = (row["member_email"], row["agent_name"])
                latest.setdefault(key, row)
            return [
                {
                    "id": row["id"],
                    "agent_name": row["agent_name"],
                    "member_email": row["member_email"],
                    "brief_path": row["brief_path"],
                    "bytes_written": row["bytes_written"],
                    "ts": row["finished_at"],
                }
                for row in latest.values()
            ]

        if "finished_at as ts" in normalized:
            rows = [
                row
                for row in self._scope(params)
                if row["finished_at"] is not None
            ]
            rows.sort(key=lambda row: row["id"], reverse=True)
            return [
                {
                    "id": row["id"],
                    "agent_name": row["agent_name"],
                    "member_email": row["member_email"],
                    "status": row["status"],
                    "brief_path": row["brief_path"],
                    "bytes_written": row["bytes_written"],
                    "ts": row["finished_at"],
                }
                for row in rows[:15]
            ]

        if "status='running'" in normalized:
            email, agent_name = params
            rows = [
                row
                for row in self.rows
                if row["member_email"] == email
                and row["agent_name"] == agent_name
                and row["status"] == "running"
            ]
            rows.sort(key=lambda row: row["id"], reverse=True)
            return [
                {"id": row["id"], "started_at": row["started_at"]}
                for row in rows[:1]
            ]

        raise AssertionError(f"unexpected read SQL: {sql}")

    def _scope(self, params: tuple) -> list[dict]:
        if not params:
            return list(self.rows)
        email = params[0]
        return [row for row in self.rows if row["member_email"] == email]

    def _row(self, run_id: int) -> dict:
        return next(row for row in self.rows if row["id"] == run_id)


class PointReadOnlyService:
    """Proves status enrichment uses exact reads, never a global page."""

    def __init__(self, service) -> None:
        self.service = service
        self.requested: list[str] = []

    def get_run(self, storage_id: str):
        self.requested.append(storage_id)
        return self.service.get_run(storage_id)

    def list_runs(self, *args, **kwargs):
        raise AssertionError("tenant status must not scan list_runs")


class AgentRunnerTruthIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.temp.name) / "briefs"
        self.database = Path(self.temp.name) / "truth.db"
        self.old_env = {
            key: os.environ.get(key)
            for key in (
                "GLOBUS_AGENTS_WORK_DIR",
                "GLOBUS_TRUTH_DB",
                "GLOBUS_TRUTH_SCOPE_SECRET",
            )
        }
        os.environ["GLOBUS_AGENTS_WORK_DIR"] = str(self.work_dir)
        os.environ["GLOBUS_TRUTH_DB"] = str(self.database)
        os.environ["GLOBUS_TRUTH_SCOPE_SECRET"] = "test-scope-secret-" + ("a" * 48)
        agent_adapter.clear_truth_service_cache()

        self.fake_db = FakeRunDatabase()
        db_helpers = types.ModuleType("db_helpers")
        db_helpers.db_read = self.fake_db.read
        db_helpers.db_write = self.fake_db.write
        db_helpers.db_insert = self.fake_db.insert

        catalog = types.ModuleType("globus_agents_catalog")
        catalog.GLOBUS_AGENTS_CATALOG = [
            {
                "name": "research",
                "role": "Research Agent",
                "task_prompt": "Produce a grounded research brief.",
            }
        ]

        self.send = Mock(
            return_value=(
                "A substantive research brief with concrete findings and next steps.",
                {"input_tokens": 3, "output_tokens": 9},
            )
        )
        orchestrator = types.ModuleType("globus_orchestrator")
        orchestrator.globus_chat_send = self.send

        self.module_names = (
            "db_helpers",
            "globus_agents_catalog",
            "globus_orchestrator",
        )
        self.old_modules = {
            name: sys.modules.get(name) for name in self.module_names
        }
        sys.modules["db_helpers"] = db_helpers
        sys.modules["globus_agents_catalog"] = catalog
        sys.modules["globus_orchestrator"] = orchestrator

        runner_path = (
            Path(__file__).resolve().parents[2] / "server" / "agent_runner.py"
        )
        spec = importlib.util.spec_from_file_location(
            f"_agent_runner_under_test_{id(self)}", runner_path
        )
        assert spec is not None and spec.loader is not None
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)

    def tearDown(self) -> None:
        agent_adapter.clear_truth_service_cache()
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name, module in self.old_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        self.temp.cleanup()

    def test_healthy_run_is_green_only_after_readback_and_sha_verification(
        self,
    ) -> None:
        email = "Member.One@example.test"

        result = self.runner.run_agent_for_member("research", email)

        self.assertTrue(result["ok"])
        self.assertEqual(
            set(result["truth"]),
            {"storage_id", "verdict", "valid", "reason_codes"},
        )
        self.assertEqual(result["truth"]["verdict"], "healthy")
        self.assertTrue(result["truth"]["valid"])
        artifact = Path(result["brief_path"])
        self.assertIn("-run-1.md", artifact.name)
        actual_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()

        stored = self.runner._truth_service().get_run(
            result["truth"]["storage_id"]
        )
        self.assertIsNotNone(stored)
        receipt = stored["receipt"]
        self.assertEqual(
            receipt["agent_id"],
            agent_adapter.member_agent_id(email, "research"),
        )
        self.assertEqual(receipt["evidence"][0]["sha256"], actual_sha)
        self.assertTrue(
            next(
                check
                for check in receipt["checks"]
                if check["name"] == "artifact_readback"
            )["passed"]
        )
        self.assertTrue(
            next(
                check
                for check in receipt["checks"]
                if check["name"] == "artifact_sha256_matches"
            )["passed"]
        )
        self.assertNotIn(email.lower(), json.dumps(receipt).lower())
        self.assertEqual(self.fake_db.rows[0]["status"], "ok")

        status = self.runner.agent_status(email)
        self.assertEqual(status["recent_runs"][0]["truth"], result["truth"])
        self.assertEqual(
            status["latest_per_agent"]["research"]["truth"],
            result["truth"],
        )

    def test_empty_model_output_is_contradictory_not_ok(self) -> None:
        self.send.return_value = ("", {})

        result = self.runner.run_agent_for_member(
            "research", "empty@example.test"
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["truth"]["verdict"], "degraded_contradictory")
        self.assertFalse(result["truth"]["valid"])
        self.assertEqual(self.fake_db.rows[0]["status"], "error")
        stored = self.runner._truth_service().get_run(
            result["truth"]["storage_id"]
        )
        meaningful = next(
            check
            for check in stored["receipt"]["checks"]
            if check["name"] == "model_reply_meaningful"
        )
        self.assertFalse(meaningful["passed"])
        self.assertTrue(Path(result["brief_path"]).is_file())

    def test_actual_refusal_reply_is_scanned_but_not_persisted(self) -> None:
        refusal = (
            "No source material was included. "
            "Please provide the source material."
        )
        self.send.return_value = (refusal, {})

        result = self.runner.run_agent_for_member(
            "research", "refusal@example.test"
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["truth"]["verdict"], "degraded_contradictory")
        stored = self.runner._truth_service().get_run(
            result["truth"]["storage_id"]
        )
        refusal_check = next(
            check
            for check in stored["receipt"]["checks"]
            if check["name"] == "model_reply_not_error_prose"
        )
        self.assertFalse(refusal_check["passed"])
        self.assertNotIn(refusal, json.dumps(stored["receipt"]))
        self.assertEqual(self.fake_db.rows[0]["status"], "error")

    def test_sha_mismatch_cannot_produce_a_healthy_receipt(self) -> None:
        artifact = Path(self.temp.name) / "tampered.md"
        artifact.write_bytes(b"actual artifact bytes")
        now = datetime.now(timezone.utc)

        truth = agent_adapter.record_successful_agent_run(
            email="checksum@example.test",
            agent_name="research",
            runner_run_id=77,
            run_key="runner-77",
            started_at=now,
            finished_at=now,
            model_reply=(
                "A substantive model response that would otherwise be valid."
            ),
            artifact_path=artifact,
            expected_sha256=hashlib.sha256(b"different bytes").hexdigest(),
            expected_bytes=len(artifact.read_bytes()),
            service=self.runner._truth_service(),
        )

        self.assertEqual(truth["verdict"], "degraded_contradictory")
        self.assertFalse(truth["valid"])
        stored = self.runner._truth_service().get_run(truth["storage_id"])
        sha_check = next(
            check
            for check in stored["receipt"]["checks"]
            if check["name"] == "artifact_sha256_matches"
        )
        self.assertFalse(sha_check["passed"])

    def test_exception_emits_failed_privacy_safe_receipt(self) -> None:
        email = "private.member@example.test"
        self.send.side_effect = RuntimeError(
            f"provider failed while handling {email}"
        )

        result = self.runner.run_agent_for_member("research", email)

        self.assertFalse(result["ok"])
        self.assertEqual(result["truth"]["verdict"], "failed")
        self.assertFalse(result["truth"]["valid"])
        stored = self.runner._truth_service().get_run(
            result["truth"]["storage_id"]
        )
        receipt = stored["receipt"]
        self.assertEqual(receipt["declared_status"], "failed")
        self.assertEqual(
            receipt["agent_id"],
            agent_adapter.member_agent_id(email, "research"),
        )
        self.assertNotIn(email.lower(), json.dumps(receipt).lower())
        self.assertNotIn("provider failed", receipt["error"]["message"])
        self.assertIn("Agent execution failed", receipt["error"]["message"])
        self.assertEqual(self.fake_db.rows[0]["status"], "error")

    def test_truth_persistence_unavailable_fails_closed_before_model_call(
        self,
    ) -> None:
        self.runner._truth_service = Mock(
            side_effect=OSError("truth database unavailable")
        )

        result = self.runner.run_agent_for_member(
            "research", "closed@example.test"
        )

        self.assertFalse(result["ok"])
        self.assertIn("truth database unavailable", result["error"])
        self.send.assert_not_called()
        self.assertEqual(self.fake_db.rows[0]["status"], "error")

    def test_missing_durable_runner_id_fails_before_model_or_receipt(
        self,
    ) -> None:
        # An older matching row must never be reused after an INSERT failure.
        self.fake_db.insert(
            "INSERT INTO globus_agent_runs "
            "(member_email, agent_name, status, started_at) "
            "VALUES (%s, %s, 'running', NOW())",
            ("rowless@example.test", "research"),
        )
        self.runner.db_helpers.db_insert = Mock(return_value=None)

        result = self.runner.run_agent_for_member(
            "research", "rowless@example.test"
        )

        self.assertFalse(result["ok"])
        self.assertIn("did not return a durable run ID", result["error"])
        self.send.assert_not_called()
        self.assertFalse(self.database.exists())

    def test_two_runs_cannot_overwrite_the_same_evidence_file(self) -> None:
        email = "durable@example.test"
        first = self.runner.run_agent_for_member("research", email)
        first_bytes = Path(first["brief_path"]).read_bytes()
        second = self.runner.run_agent_for_member("research", email)

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertNotEqual(first["brief_path"], second["brief_path"])
        self.assertEqual(Path(first["brief_path"]).read_bytes(), first_bytes)
        self.assertIn("-run-1.md", first["brief_path"])
        self.assertIn("-run-2.md", second["brief_path"])

    def test_runner_ledger_failure_cannot_return_overall_success(self) -> None:
        self.runner._update_run_ok = Mock(return_value=False)

        result = self.runner.run_agent_for_member(
            "research", "ledger@example.test"
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["truth"]["verdict"], "healthy")
        self.assertIn("could not be committed", result["error"])
        self.assertEqual(self.fake_db.rows[0]["status"], "error")

    def test_member_pseudonym_is_keyed_to_each_install(self) -> None:
        email = "guessable@example.test"
        first = agent_adapter.member_scope_hash(email)
        os.environ["GLOBUS_TRUTH_SCOPE_SECRET"] = "second-install-" + ("b" * 48)
        second = agent_adapter.member_scope_hash(email)

        self.assertNotEqual(first, second)
        self.assertNotEqual(
            first,
            hashlib.sha256(email.encode("utf-8")).hexdigest()[: len(first)],
        )

    def test_status_point_reads_are_tenant_isolated_and_page_independent(
        self,
    ) -> None:
        member_a = "a@example.test"
        member_b = "b@example.test"
        result_a = self.runner.run_agent_for_member("research", member_a)
        result_b = self.runner.run_agent_for_member("research", member_b)
        self.assertTrue(result_a["ok"])
        self.assertTrue(result_b["ok"])
        service = PointReadOnlyService(self.runner._truth_service())

        indexes = agent_adapter.truth_status_for_member(
            member_a,
            [
                {"id": 1, "agent_name": "research"},
                # Even if a caller accidentally includes B's globally unique
                # runner ID, A's scoped receipt identity cannot resolve it.
                {"id": 2, "agent_name": "research"},
            ],
            service=service,
        )

        self.assertEqual(
            indexes["by_runner_run_id"],
            {"1": result_a["truth"]},
        )
        self.assertEqual(
            indexes["latest_per_agent"],
            {"research": result_a["truth"]},
        )
        self.assertEqual(
            service.requested,
            [
                agent_adapter.receipt_storage_id(
                    member_a, "research", runner_run_id
                )
                for runner_run_id in (1, 2)
            ],
        )
        self.assertNotIn(result_b["truth"], indexes["by_runner_run_id"].values())


if __name__ == "__main__":
    unittest.main()
