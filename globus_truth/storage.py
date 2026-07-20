"""SQLite persistence for receipts and immutable evaluation history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Mapping


_TRUSTED_VERDICTS = ("healthy", "verified_no_work")
_VERDICTS = {
    "healthy",
    "verified_no_work",
    "degraded_contradictory",
    "failed",
    "stale",
}
_MAX_REEVALUATIONS_PER_READ = 500
_Reevaluator = Callable[
    [Mapping[str, Any], str],
    tuple[Mapping[str, Any], str | None],
]


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
        self._reevaluation_clock: Callable[[], str] | None = None
        self._reevaluator: _Reevaluator | None = None
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

    def close(self) -> None:
        """Release the anchor used to keep a shared in-memory database alive."""
        with self._lock:
            keeper, self._keeper = self._keeper, None
        if keeper is not None:
            keeper.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

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
                    fresh_until TEXT,
                    reason_codes_json TEXT NOT NULL,
                    checks_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_verdicts_storage
                    ON verdicts(storage_id, id DESC);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(verdicts)").fetchall()
            }
            if "fresh_until" not in columns:
                connection.execute("ALTER TABLE verdicts ADD COLUMN fresh_until TEXT")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_verdicts_freshness
                    ON verdicts(verdict, fresh_until)
                """
            )

    def configure_stale_reevaluation(
        self,
        *,
        clock: Callable[[], str],
        reevaluator: _Reevaluator,
    ) -> None:
        """Install deterministic callbacks used to age persisted trusted verdicts."""

        with self._lock:
            self._reevaluation_clock = clock
            self._reevaluator = reevaluator

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
        fresh_until: str | None = None,
    ) -> tuple[str, bool]:
        """Persist an immutable receipt and one evaluation.

        Exact retries are idempotent. Reusing a receipt ID with different bytes is
        rejected instead of silently rewriting audit history.
        """
        return self.save_many(
            [
                {
                    "receipt": receipt,
                    "evaluation": evaluation,
                    "received_at": received_at,
                    "fresh_until": fresh_until,
                }
            ]
        )[0]

    def save_many(
        self,
        entries: list[Mapping[str, Any]],
    ) -> list[tuple[str, bool]]:
        """Persist several receipt/evaluation pairs in one SQLite transaction.

        This powers multi-phase verification stories whose receipts must appear
        together or not at all. Exact duplicate IDs inside or outside the batch
        retain the same immutable-content and evaluation-history semantics as
        :meth:`save`.
        """
        prepared: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError("invalid atomic receipt entry")
            receipt = entry.get("receipt")
            evaluation = entry.get("evaluation")
            received_at = entry.get("received_at")
            fresh_until = entry.get("fresh_until")
            if (
                not isinstance(receipt, Mapping)
                or not isinstance(evaluation, Mapping)
                or not isinstance(received_at, str)
                or not received_at
            ):
                raise ValueError("invalid atomic receipt entry")
            payload_json = _canonical_json(receipt)
            storage_id = self.storage_id(receipt, payload_json)
            receipt_id = receipt.get("receipt_id")
            agent_id = receipt.get("agent_id")
            run_id = receipt.get("run_id")
            prepared.append(
                {
                    "storage_id": storage_id,
                    "payload_json": payload_json,
                    "receipt_id": (
                        receipt_id if isinstance(receipt_id, str) else storage_id
                    ),
                    "agent_id": (
                        agent_id if isinstance(agent_id, str) else "(invalid)"
                    ),
                    "run_id": run_id if isinstance(run_id, str) else "(invalid)",
                    "received_at": received_at,
                    "evaluation": evaluation,
                    "fresh_until": fresh_until,
                }
            )

        results: list[tuple[str, bool]] = []
        with self._lock, closing(self._connect()) as connection, connection:
            for item in prepared:
                storage_id = item["storage_id"]
                payload_json = item["payload_json"]
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
                            (
                                storage_id,
                                receipt_id,
                                agent_id,
                                run_id,
                                received_at,
                                payload_json
                            )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            storage_id,
                            item["receipt_id"],
                            item["agent_id"],
                            item["run_id"],
                            item["received_at"],
                            payload_json,
                        ),
                    )
                evaluation = item["evaluation"]
                connection.execute(
                    """
                    INSERT INTO verdicts
                        (
                            storage_id,
                            verdict,
                            evaluated_at,
                            fresh_until,
                            reason_codes_json,
                            checks_json
                        )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        storage_id,
                        evaluation["verdict"],
                        evaluation["evaluated_at"],
                        (
                            item["fresh_until"]
                            if evaluation["verdict"] in _TRUSTED_VERDICTS
                            else None
                        ),
                        _canonical_json(evaluation["reason_codes"]),
                        _canonical_json(evaluation["checks"]),
                    ),
                )
                results.append((storage_id, created))
        return results

    def _refresh_due_verdicts(
        self,
        *,
        limit: int = _MAX_REEVALUATIONS_PER_READ,
        storage_ids: list[str] | None = None,
    ) -> int:
        """Reevaluate a bounded set of latest trusted verdicts whose signal expired."""

        with self._lock:
            clock = self._reevaluation_clock
            reevaluator = self._reevaluator
        if clock is None or reevaluator is None or limit < 1:
            return 0
        evaluated_at = clock()
        if not isinstance(evaluated_at, str) or not evaluated_at:
            raise ValueError("reevaluation clock must return an ISO timestamp")
        if storage_ids == []:
            return 0

        filters = [
            "v.verdict IN (?, ?)",
            "(v.fresh_until IS NULL OR v.fresh_until < ?)",
        ]
        parameters: list[Any] = [
            *_TRUSTED_VERDICTS,
            evaluated_at,
        ]
        if storage_ids is not None:
            filters.append(
                "r.storage_id IN (" + ", ".join("?" for _ in storage_ids) + ")"
            )
            parameters.extend(storage_ids)
        batch_limit = min(limit, _MAX_REEVALUATIONS_PER_READ)
        parameters.append(batch_limit)
        with self._lock, closing(self._connect()) as connection:
            candidates = connection.execute(
                f"""
                SELECT r.storage_id, r.payload_json,
                       v.id AS verdict_id, v.verdict
                  FROM receipts AS r
                  JOIN verdicts AS v ON v.id = (
                      SELECT MAX(v2.id) FROM verdicts AS v2
                       WHERE v2.storage_id = r.storage_id
                  )
                 WHERE {" AND ".join(filters)}
                 ORDER BY
                       CASE WHEN v.fresh_until IS NULL THEN 0 ELSE 1 END,
                       v.fresh_until,
                       v.id
                 LIMIT ?
                """,
                parameters,
            ).fetchall()

        for candidate in candidates:
            receipt = json.loads(candidate["payload_json"])
            evaluation, fresh_until = reevaluator(receipt, evaluated_at)
            verdict = evaluation.get("verdict")
            if verdict not in _VERDICTS:
                raise ValueError("reevaluator returned an unsupported verdict")

            with self._lock, closing(self._connect()) as connection:
                # Repository instances have separate Python locks. A SQLite
                # write lock plus an in-transaction recheck makes one verdict
                # transition globally atomic across threads and processes.
                connection.execute("BEGIN IMMEDIATE")
                try:
                    latest = connection.execute(
                        """
                        SELECT id, verdict, fresh_until
                          FROM verdicts
                         WHERE storage_id = ?
                         ORDER BY id DESC
                         LIMIT 1
                        """,
                        (candidate["storage_id"],),
                    ).fetchone()
                    if (
                        latest is None
                        or latest["id"] != candidate["verdict_id"]
                        or latest["verdict"] not in _TRUSTED_VERDICTS
                        or (
                            latest["fresh_until"] is not None
                            and latest["fresh_until"] >= evaluated_at
                        )
                    ):
                        connection.commit()
                        continue
                    if verdict == latest["verdict"]:
                        # Legacy trusted rows have no deadline. Backfill it
                        # without manufacturing an unchanged audit event.
                        connection.execute(
                            "UPDATE verdicts SET fresh_until = ? WHERE id = ?",
                            (fresh_until, latest["id"]),
                        )
                        connection.commit()
                        continue
                    connection.execute(
                        """
                        INSERT INTO verdicts
                            (
                                storage_id,
                                verdict,
                                evaluated_at,
                                fresh_until,
                                reason_codes_json,
                                checks_json
                            )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            candidate["storage_id"],
                            verdict,
                            evaluation["evaluated_at"],
                            (
                                fresh_until
                                if verdict in _TRUSTED_VERDICTS
                                else None
                            ),
                            _canonical_json(evaluation["reason_codes"]),
                            _canonical_json(evaluation["checks"]),
                        ),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        return len(candidates)

    def list_runs(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
            raise ValueError("offset must be between 0 and 1,000,000")
        with self._lock, closing(self._connect()) as connection:
            page_ids = [
                row["storage_id"]
                for row in connection.execute(
                    """
                    SELECT storage_id
                      FROM receipts
                     ORDER BY received_at DESC, storage_id DESC
                     LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
            ]
        self._refresh_due_verdicts(limit=limit, storage_ids=page_ids)
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
        self._refresh_due_verdicts(limit=1, storage_ids=[storage_id])
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
        while (
            self._refresh_due_verdicts()
            == _MAX_REEVALUATIONS_PER_READ
        ):
            # Summary counts the whole fleet, so age every due batch before
            # returning rather than mixing fresh and expired trusted rows.
            pass
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
        if isinstance(storage_id, str) and storage_id and len(storage_id) <= 200:
            self._refresh_due_verdicts(limit=1, storage_ids=[storage_id])
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
