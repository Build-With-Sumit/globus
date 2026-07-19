"""SQLite persistence for receipts and immutable evaluation history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping


class ReceiptConflict(ValueError):
    """Raised when a receipt ID is reused with different content."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class TruthRepository:
    """Small, thread-safe repository with parameterized SQL only."""

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        self._lock = threading.RLock()
        self._uri = self.database == ":memory:"
        self._target = (
            f"file:globus_truth_{id(self)}?mode=memory&cache=shared"
            if self._uri
            else self.database
        )
        self._keeper: sqlite3.Connection | None = None
        if self._uri:
            self._keeper = sqlite3.connect(self._target, uri=True, timeout=5.0)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._target, uri=self._uri, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        if self.database != ":memory:":
            path = Path(self.database)
            path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(self._connect()) as connection, connection:
            if self.database != ":memory:":
                connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    storage_id TEXT PRIMARY KEY,
                    receipt_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_receipts_received
                    ON receipts(received_at DESC, storage_id DESC);
                CREATE INDEX IF NOT EXISTS idx_receipts_agent
                    ON receipts(agent_id, received_at DESC);

                CREATE TABLE IF NOT EXISTS verdicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    storage_id TEXT NOT NULL REFERENCES receipts(storage_id) ON DELETE CASCADE,
                    verdict TEXT NOT NULL CHECK (
                        verdict IN (
                            'healthy',
                            'verified_no_work',
                            'degraded_contradictory',
                            'failed',
                            'stale'
                        )
                    ),
                    evaluated_at TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    checks_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_verdicts_storage
                    ON verdicts(storage_id, id DESC);
                """
            )

    @staticmethod
    def storage_id(receipt: Mapping[str, Any], payload_json: str | None = None) -> str:
        candidate = receipt.get("receipt_id")
        if isinstance(candidate, str) and candidate:
            return candidate
        raw = payload_json or _canonical_json(receipt)
        return "invalid-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def save(
        self,
        receipt: Mapping[str, Any],
        evaluation: Mapping[str, Any],
        *,
        received_at: str,
    ) -> tuple[str, bool]:
        """Persist an immutable receipt and one evaluation.

        Exact retries are idempotent. Reusing a receipt ID with different bytes is
        rejected instead of silently rewriting audit history.
        """

        payload_json = _canonical_json(receipt)
        storage_id = self.storage_id(receipt, payload_json)
        receipt_id = receipt.get("receipt_id")
        agent_id = receipt.get("agent_id")
        run_id = receipt.get("run_id")
        safe_receipt_id = receipt_id if isinstance(receipt_id, str) else storage_id
        safe_agent_id = agent_id if isinstance(agent_id, str) else "(invalid)"
        safe_run_id = run_id if isinstance(run_id, str) else "(invalid)"
        with self._lock, closing(self._connect()) as connection, connection:
            existing = connection.execute(
                "SELECT payload_json FROM receipts WHERE storage_id = ?",
                (storage_id,),
            ).fetchone()
            created = existing is None
            if existing is not None and existing["payload_json"] != payload_json:
                raise ReceiptConflict(
                    f"receipt_id {storage_id!r} already exists with different content"
                )
            if created:
                connection.execute(
                    """
                    INSERT INTO receipts
                        (storage_id, receipt_id, agent_id, run_id, received_at, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        storage_id,
                        safe_receipt_id,
                        safe_agent_id,
                        safe_run_id,
                        received_at,
                        payload_json,
                    ),
                )
            connection.execute(
                """
                INSERT INTO verdicts
                    (storage_id, verdict, evaluated_at, reason_codes_json, checks_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    storage_id,
                    evaluation["verdict"],
                    evaluation["evaluated_at"],
                    _canonical_json(evaluation["reason_codes"]),
                    _canonical_json(evaluation["checks"]),
                ),
            )
        return storage_id, created

    def list_runs(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
            raise ValueError("offset must be between 0 and 1,000,000")
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT r.storage_id, r.received_at, r.payload_json,
                       v.verdict, v.evaluated_at,
                       v.reason_codes_json, v.checks_json
                  FROM receipts AS r
                  JOIN verdicts AS v ON v.id = (
                      SELECT MAX(v2.id) FROM verdicts AS v2
                       WHERE v2.storage_id = r.storage_id
                  )
                 ORDER BY r.received_at DESC, r.storage_id DESC
                 LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def get_run(self, storage_id: str) -> dict[str, Any] | None:
        if not isinstance(storage_id, str) or not storage_id or len(storage_id) > 200:
            return None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT r.storage_id, r.received_at, r.payload_json,
                       v.verdict, v.evaluated_at,
                       v.reason_codes_json, v.checks_json
                  FROM receipts AS r
                  JOIN verdicts AS v ON v.id = (
                      SELECT MAX(v2.id) FROM verdicts AS v2
                       WHERE v2.storage_id = r.storage_id
                  )
                 WHERE r.storage_id = ?
                """,
                (storage_id,),
            ).fetchone()
        return self._decode_row(row) if row is not None else None

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        evaluation = {
            "verdict": row["verdict"],
            "valid": row["verdict"] in {"healthy", "verified_no_work"},
            "evaluated_at": row["evaluated_at"],
            "reason_codes": json.loads(row["reason_codes_json"]),
            "checks": json.loads(row["checks_json"]),
        }
        return {
            "storage_id": row["storage_id"],
            "received_at": row["received_at"],
            "receipt": json.loads(row["payload_json"]),
            "evaluation": evaluation,
        }

    def summary(self) -> dict[str, Any]:
        counts = {
            "healthy": 0,
            "verified_no_work": 0,
            "degraded_contradictory": 0,
            "failed": 0,
            "stale": 0,
        }
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT v.verdict, COUNT(*) AS count
                  FROM receipts AS r
                  JOIN verdicts AS v ON v.id = (
                      SELECT MAX(v2.id) FROM verdicts AS v2
                       WHERE v2.storage_id = r.storage_id
                  )
                 GROUP BY v.verdict
                """
            ).fetchall()
        for row in rows:
            counts[row["verdict"]] = row["count"]
        return {
            "total": sum(counts.values()),
            "trusted": counts["healthy"] + counts["verified_no_work"],
            "attention": (
                counts["degraded_contradictory"]
                + counts["failed"]
                + counts["stale"]
            ),
            "verdicts": counts,
        }

    def verdict_history(self, storage_id: str) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT verdict, evaluated_at, reason_codes_json, checks_json
                  FROM verdicts WHERE storage_id = ? ORDER BY id
                """,
                (storage_id,),
            ).fetchall()
        return [
            {
                "verdict": row["verdict"],
                "evaluated_at": row["evaluated_at"],
                "reason_codes": json.loads(row["reason_codes_json"]),
                "checks": json.loads(row["checks_json"]),
            }
            for row in rows
        ]
