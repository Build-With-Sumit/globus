"""SQLite persistence for receipts and immutable evaluation history."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
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
_ACTION_POLICIES = {"healthy_only", "trusted_completion"}
_ACTION_OBSERVED_VERDICTS = _VERDICTS | {
    "missing",
    "malformed",
    "unavailable",
}
_ACTION_DECISION_FIELDS = {
    "decision_id",
    "storage_id",
    "action_id",
    "policy_id",
    "observed_verdict",
    "authorized",
    "reason_codes",
    "decided_at",
}
_ACTION_BLOCK_REASONS = {
    "verified_no_work": "policy_requires_healthy",
    "degraded_contradictory": "verdict_contradictory",
    "failed": "verdict_failed",
    "stale": "verdict_stale",
    "missing": "truth_record_missing",
    "malformed": "truth_record_malformed",
    "unavailable": "truth_lookup_failed",
}
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_Reevaluator = Callable[
    [Mapping[str, Any], str],
    tuple[Mapping[str, Any], str | None],
]


class ReceiptConflict(ValueError):
    """Raised when a receipt ID is reused with different content."""


class ActionDecisionConflict(ValueError):
    """Raised when an action decision ID is reused with different content."""


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

                CREATE TABLE IF NOT EXISTS action_decisions (
                    decision_id TEXT PRIMARY KEY,
                    storage_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    policy_id TEXT NOT NULL CHECK (
                        policy_id IN ('healthy_only', 'trusted_completion')
                    ),
                    observed_verdict TEXT NOT NULL CHECK (
                        observed_verdict IN (
                            'healthy',
                            'verified_no_work',
                            'degraded_contradictory',
                            'failed',
                            'stale',
                            'missing',
                            'malformed',
                            'unavailable'
                        )
                    ),
                    authorized INTEGER NOT NULL CHECK (authorized IN (0, 1)),
                    reason_codes_json TEXT NOT NULL,
                    decided_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_action_decisions_storage
                    ON action_decisions(storage_id, decided_at DESC);
                CREATE INDEX IF NOT EXISTS idx_action_decisions_recent
                    ON action_decisions(decided_at DESC, decision_id DESC);
                CREATE TRIGGER IF NOT EXISTS action_decisions_no_update
                    BEFORE UPDATE ON action_decisions
                    BEGIN
                        SELECT RAISE(ABORT, 'action decisions are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS action_decisions_no_delete
                    BEFORE DELETE ON action_decisions
                    BEGIN
                        SELECT RAISE(ABORT, 'action decisions are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS action_decisions_consistent_insert
                    BEFORE INSERT ON action_decisions
                    WHEN
                        NEW.authorized != CASE
                            WHEN NEW.observed_verdict = 'healthy' THEN 1
                            WHEN NEW.observed_verdict = 'verified_no_work'
                                 AND NEW.policy_id = 'trusted_completion' THEN 1
                            ELSE 0
                        END
                        OR NEW.reason_codes_json != CASE
                            WHEN NEW.observed_verdict = 'healthy'
                                THEN '["policy_satisfied"]'
                            WHEN NEW.observed_verdict = 'verified_no_work'
                                 AND NEW.policy_id = 'trusted_completion'
                                THEN '["policy_satisfied"]'
                            WHEN NEW.observed_verdict = 'verified_no_work'
                                THEN '["policy_requires_healthy"]'
                            WHEN NEW.observed_verdict = 'degraded_contradictory'
                                THEN '["verdict_contradictory"]'
                            WHEN NEW.observed_verdict = 'failed'
                                THEN '["verdict_failed"]'
                            WHEN NEW.observed_verdict = 'stale'
                                THEN '["verdict_stale"]'
                            WHEN NEW.observed_verdict = 'missing'
                                THEN '["truth_record_missing"]'
                            WHEN NEW.observed_verdict = 'malformed'
                                THEN '["truth_record_malformed"]'
                            WHEN NEW.observed_verdict = 'unavailable'
                                THEN '["truth_lookup_failed"]'
                            ELSE ''
                        END
                    BEGIN
                        SELECT RAISE(ABORT, 'inconsistent action decision');
                    END;
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

    @staticmethod
    def _validate_action_decision(
        decision: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return a normalized, privacy-safe action decision.

        The exact field allowlist prevents callers from accidentally placing
        receipt bodies, prompts, credentials, or destination payloads in the
        authorization audit log.
        """

        if not isinstance(decision, Mapping) or set(decision) != _ACTION_DECISION_FIELDS:
            raise ValueError("action decision contains unsupported fields")
        normalized: dict[str, Any] = {}
        for field in ("decision_id", "storage_id", "action_id"):
            value = decision.get(field)
            if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
                raise ValueError(f"{field} must be a safe 1-128 character identifier")
            normalized[field] = value

        policy_id = decision.get("policy_id")
        if policy_id not in _ACTION_POLICIES:
            raise ValueError("unsupported action policy")
        normalized["policy_id"] = policy_id

        observed_verdict = decision.get("observed_verdict")
        if observed_verdict not in _ACTION_OBSERVED_VERDICTS:
            raise ValueError("unsupported observed verdict")
        normalized["observed_verdict"] = observed_verdict

        authorized = decision.get("authorized")
        if not isinstance(authorized, bool):
            raise ValueError("authorized must be a boolean")
        normalized["authorized"] = authorized

        reason_codes = decision.get("reason_codes")
        if (
            not isinstance(reason_codes, (list, tuple))
            or not 1 <= len(reason_codes) <= 16
            or any(
                not isinstance(code, str) or not _SAFE_ID_RE.fullmatch(code)
                for code in reason_codes
            )
        ):
            raise ValueError("reason_codes must contain 1-16 safe identifiers")
        normalized["reason_codes"] = list(reason_codes)

        expected_authorized = (
            observed_verdict == "healthy"
            or (
                observed_verdict == "verified_no_work"
                and policy_id == "trusted_completion"
            )
        )
        expected_reason = (
            "policy_satisfied"
            if expected_authorized
            else _ACTION_BLOCK_REASONS[observed_verdict]
        )
        if authorized is not expected_authorized:
            raise ValueError("action decision authorization is inconsistent")
        if normalized["reason_codes"] != [expected_reason]:
            raise ValueError("action decision reason code is inconsistent")

        decided_at = decision.get("decided_at")
        if not isinstance(decided_at, str) or not decided_at or len(decided_at) > 40:
            raise ValueError("decided_at must be an RFC 3339 timestamp")
        timestamp = decided_at[:-1] + "+00:00" if decided_at.endswith("Z") else decided_at
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise ValueError("decided_at must be an RFC 3339 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("decided_at must include a timezone")
        normalized["decided_at"] = (
            parsed.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        return normalized

    def save_action_decision(self, decision: Mapping[str, Any]) -> bool:
        """Persist one immutable, privacy-safe authorization decision.

        Exact retries are idempotent. Reusing a decision ID for any different
        content raises :class:`ActionDecisionConflict`.
        """

        item = self._validate_action_decision(decision)
        reason_codes_json = _canonical_json(item["reason_codes"])
        candidate = (
            item["decision_id"],
            item["storage_id"],
            item["action_id"],
            item["policy_id"],
            item["observed_verdict"],
            int(item["authorized"]),
            reason_codes_json,
            item["decided_at"],
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT decision_id, storage_id, action_id, policy_id,
                           observed_verdict, authorized, reason_codes_json,
                           decided_at
                      FROM action_decisions
                     WHERE decision_id = ?
                    """,
                    (item["decision_id"],),
                ).fetchone()
                if row is not None:
                    existing = tuple(row)
                    if existing != candidate:
                        raise ActionDecisionConflict(
                            "decision_id already exists with different content"
                        )
                    connection.commit()
                    return False
                if item["authorized"]:
                    truth = connection.execute(
                        """
                        SELECT verdict, fresh_until
                          FROM verdicts
                         WHERE storage_id = ?
                         ORDER BY id DESC
                         LIMIT 1
                        """,
                        (item["storage_id"],),
                    ).fetchone()
                    if (
                        truth is None
                        or truth["verdict"] != item["observed_verdict"]
                        or truth["fresh_until"] is None
                    ):
                        raise ActionDecisionConflict(
                            "authorized decision no longer matches current truth"
                        )
                    deadline_text = truth["fresh_until"]
                    deadline_candidate = (
                        deadline_text[:-1] + "+00:00"
                        if deadline_text.endswith("Z")
                        else deadline_text
                    )
                    decided_candidate = (
                        item["decided_at"][:-1] + "+00:00"
                        if item["decided_at"].endswith("Z")
                        else item["decided_at"]
                    )
                    try:
                        deadline = datetime.fromisoformat(deadline_candidate)
                        decided = datetime.fromisoformat(decided_candidate)
                    except (TypeError, ValueError) as exc:
                        raise ActionDecisionConflict(
                            "authorized decision has no current freshness proof"
                        ) from exc
                    if deadline.tzinfo is None or deadline < decided:
                        raise ActionDecisionConflict(
                            "authorized decision has expired truth"
                        )
                connection.execute(
                    """
                    INSERT INTO action_decisions
                        (
                            decision_id,
                            storage_id,
                            action_id,
                            policy_id,
                            observed_verdict,
                            authorized,
                            reason_codes_json,
                            decided_at
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    candidate,
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _decode_action_decision(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "decision_id": row["decision_id"],
            "storage_id": row["storage_id"],
            "action_id": row["action_id"],
            "policy_id": row["policy_id"],
            "observed_verdict": row["observed_verdict"],
            "authorized": bool(row["authorized"]),
            "reason_codes": json.loads(row["reason_codes_json"]),
            "decided_at": row["decided_at"],
        }

    def get_action_decision(self, decision_id: str) -> dict[str, Any] | None:
        if not isinstance(decision_id, str) or not _SAFE_ID_RE.fullmatch(decision_id):
            return None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT decision_id, storage_id, action_id, policy_id,
                       observed_verdict, authorized, reason_codes_json,
                       decided_at
                  FROM action_decisions
                 WHERE decision_id = ?
                """,
                (decision_id,),
            ).fetchone()
        return self._decode_action_decision(row) if row is not None else None

    def list_action_decisions(
        self,
        *,
        storage_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if storage_id is not None and (
            not isinstance(storage_id, str) or not _SAFE_ID_RE.fullmatch(storage_id)
        ):
            raise ValueError("storage_id must be a safe 1-128 character identifier")
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
            raise ValueError("offset must be between 0 and 1,000,000")
        filters = ""
        parameters: list[Any] = []
        if storage_id is not None:
            filters = "WHERE storage_id = ?"
            parameters.append(storage_id)
        parameters.extend((limit, offset))
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT decision_id, storage_id, action_id, policy_id,
                       observed_verdict, authorized, reason_codes_json,
                       decided_at
                  FROM action_decisions
                  {filters}
                 ORDER BY decided_at DESC, decision_id DESC
                 LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [self._decode_action_decision(row) for row in rows]

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
