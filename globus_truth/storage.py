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
_APPROVAL_POLICIES = _ACTION_POLICIES
_APPROVAL_RISKS = {"low", "medium", "high", "critical"}
_APPROVAL_OUTCOMES = {"approved", "rejected"}
_APPROVAL_COMPLETION_OUTCOMES = {"succeeded", "failed"}
_APPROVAL_PROPOSAL_FIELDS = {
    "proposal_id",
    "storage_id",
    "action_id",
    "policy_id",
    "action_kind",
    "payload_sha256",
    "requested_by",
    "risk",
    "created_at",
    "expires_at",
    "proposal_sha256",
}
_APPROVAL_DECISION_FIELDS = {
    "approval_id",
    "proposal_id",
    "proposal_sha256",
    "outcome",
    "decided_by",
    "reason_code",
    "decided_at",
}
_APPROVAL_CLAIM_FIELDS = {
    "claim_id",
    "proposal_id",
    "approval_id",
    "action_id",
    "gate_decision_id",
    "gate_decision_sha256",
    "claimed_at",
}
_APPROVAL_COMPLETION_FIELDS = {
    "completion_id",
    "claim_id",
    "outcome",
    "reason_code",
    "completed_at",
}
_VERIFIED_ACTION_VERIFICATION_FIELDS = {
    "verification_id",
    "proposal_id",
    "claim_id",
    "adapter_id",
    "adapter_version",
    "action_kind",
    "request_sha256",
    "idempotency_key_sha256",
    "observation_sha256",
    "observed_count",
    "verified",
    "reason_code",
    "verified_at",
    "verification_sha256",
}
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_Reevaluator = Callable[
    [Mapping[str, Any], str],
    tuple[Mapping[str, Any], str | None],
]


class ReceiptConflict(ValueError):
    """Raised when a receipt ID is reused with different content."""


class ActionDecisionConflict(ValueError):
    """Raised when an action decision ID is reused with different content."""


class ActionProposalConflict(ValueError):
    """Raised when a proposal or action ID is reused with different content."""


class HumanApprovalConflict(ValueError):
    """Raised when an immutable human decision is contradicted."""


class ApprovalExecutionConflict(ValueError):
    """Raised when an execution audit record conflicts with persisted state."""


class VerifiedActionVerificationConflict(ValueError):
    """Raised when a destination-verification audit conflicts with state."""


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

                CREATE TABLE IF NOT EXISTS action_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    storage_id TEXT NOT NULL
                        REFERENCES receipts(storage_id),
                    action_id TEXT NOT NULL UNIQUE,
                    policy_id TEXT NOT NULL CHECK (
                        policy_id IN ('healthy_only', 'trusted_completion')
                    ),
                    action_kind TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL CHECK (
                        length(payload_sha256) = 64
                        AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
                    ),
                    requested_by TEXT NOT NULL,
                    risk TEXT NOT NULL CHECK (
                        risk IN ('low', 'medium', 'high', 'critical')
                    ),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    proposal_sha256 TEXT NOT NULL CHECK (
                        length(proposal_sha256) = 64
                        AND proposal_sha256 NOT GLOB '*[^0-9a-f]*'
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_action_proposals_recent
                    ON action_proposals(created_at DESC, proposal_id DESC);
                CREATE TRIGGER IF NOT EXISTS action_proposals_no_update
                    BEFORE UPDATE ON action_proposals
                    BEGIN
                        SELECT RAISE(ABORT, 'action proposals are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS action_proposals_no_delete
                    BEFORE DELETE ON action_proposals
                    BEGIN
                        SELECT RAISE(ABORT, 'action proposals are immutable');
                    END;

                CREATE TABLE IF NOT EXISTS human_approval_decisions (
                    approval_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL UNIQUE
                        REFERENCES action_proposals(proposal_id),
                    proposal_sha256 TEXT NOT NULL CHECK (
                        length(proposal_sha256) = 64
                        AND proposal_sha256 NOT GLOB '*[^0-9a-f]*'
                    ),
                    outcome TEXT NOT NULL CHECK (
                        outcome IN ('approved', 'rejected')
                    ),
                    decided_by TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    decided_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_human_approval_recent
                    ON human_approval_decisions(
                        decided_at DESC,
                        approval_id DESC
                    );
                CREATE TRIGGER IF NOT EXISTS human_approval_no_update
                    BEFORE UPDATE ON human_approval_decisions
                    BEGIN
                        SELECT RAISE(ABORT, 'human approval decisions are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS human_approval_no_delete
                    BEFORE DELETE ON human_approval_decisions
                    BEGIN
                        SELECT RAISE(ABORT, 'human approval decisions are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS human_approval_consistent_insert
                    BEFORE INSERT ON human_approval_decisions
                    WHEN NOT EXISTS (
                        SELECT 1
                          FROM action_proposals AS p
                         WHERE p.proposal_id = NEW.proposal_id
                           AND p.proposal_sha256 = NEW.proposal_sha256
                           AND NEW.decided_at >= p.created_at
                           AND NEW.decided_at <= p.expires_at
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'inconsistent human approval decision');
                    END;

                CREATE TABLE IF NOT EXISTS approval_execution_claims (
                    claim_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL UNIQUE
                        REFERENCES action_proposals(proposal_id),
                    approval_id TEXT NOT NULL UNIQUE
                        REFERENCES human_approval_decisions(approval_id),
                    action_id TEXT NOT NULL UNIQUE,
                    gate_decision_id TEXT NOT NULL UNIQUE
                        REFERENCES action_decisions(decision_id),
                    gate_decision_sha256 TEXT NOT NULL CHECK (
                        length(gate_decision_sha256) = 64
                        AND gate_decision_sha256 NOT GLOB '*[^0-9a-f]*'
                    ),
                    claimed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_approval_claims_recent
                    ON approval_execution_claims(
                        claimed_at DESC,
                        claim_id DESC
                    );
                CREATE TRIGGER IF NOT EXISTS approval_claims_no_update
                    BEFORE UPDATE ON approval_execution_claims
                    BEGIN
                        SELECT RAISE(ABORT, 'approval execution claims are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS approval_claims_no_delete
                    BEFORE DELETE ON approval_execution_claims
                    BEGIN
                        SELECT RAISE(ABORT, 'approval execution claims are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS approval_claims_consistent_insert
                    BEFORE INSERT ON approval_execution_claims
                    WHEN NOT EXISTS (
                        SELECT 1
                          FROM action_proposals AS p
                          JOIN human_approval_decisions AS a
                            ON a.proposal_id = p.proposal_id
                          JOIN action_decisions AS g
                            ON g.decision_id = NEW.gate_decision_id
                          JOIN verdicts AS v
                            ON v.id = (
                                SELECT MAX(v2.id)
                                  FROM verdicts AS v2
                                 WHERE v2.storage_id = p.storage_id
                            )
                         WHERE p.proposal_id = NEW.proposal_id
                           AND p.action_id = NEW.action_id
                           AND a.approval_id = NEW.approval_id
                           AND a.proposal_sha256 = p.proposal_sha256
                           AND a.outcome = 'approved'
                           AND NEW.claimed_at >= a.decided_at
                           AND NEW.claimed_at <= p.expires_at
                           AND g.storage_id = p.storage_id
                           AND g.action_id = p.action_id
                           AND g.policy_id = p.policy_id
                           AND g.authorized = 1
                           AND g.decided_at <= NEW.claimed_at
                           AND g.observed_verdict = v.verdict
                           AND v.fresh_until IS NOT NULL
                           AND v.fresh_until >= NEW.claimed_at
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'inconsistent approval execution claim');
                    END;

                CREATE TABLE IF NOT EXISTS approval_execution_completions (
                    completion_id TEXT PRIMARY KEY,
                    claim_id TEXT NOT NULL UNIQUE
                        REFERENCES approval_execution_claims(claim_id),
                    outcome TEXT NOT NULL CHECK (
                        outcome IN ('succeeded', 'failed')
                    ),
                    reason_code TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_approval_completions_recent
                    ON approval_execution_completions(
                        completed_at DESC,
                        completion_id DESC
                    );
                CREATE TRIGGER IF NOT EXISTS approval_completions_no_update
                    BEFORE UPDATE ON approval_execution_completions
                    BEGIN
                        SELECT RAISE(ABORT, 'approval completions are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS approval_completions_no_delete
                    BEFORE DELETE ON approval_execution_completions
                    BEGIN
                        SELECT RAISE(ABORT, 'approval completions are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS approval_completions_consistent_insert
                    BEFORE INSERT ON approval_execution_completions
                    WHEN NOT EXISTS (
                        SELECT 1
                          FROM approval_execution_claims AS c
                         WHERE c.claim_id = NEW.claim_id
                           AND NEW.completed_at >= c.claimed_at
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'inconsistent approval completion');
                    END;

                CREATE TABLE IF NOT EXISTS verified_action_verifications (
                    verification_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL UNIQUE
                        REFERENCES action_proposals(proposal_id),
                    claim_id TEXT NOT NULL UNIQUE
                        REFERENCES approval_execution_claims(claim_id),
                    adapter_id TEXT NOT NULL,
                    adapter_version TEXT NOT NULL,
                    action_kind TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL CHECK (
                        length(request_sha256) = 64
                        AND request_sha256 NOT GLOB '*[^0-9a-f]*'
                    ),
                    idempotency_key_sha256 TEXT NOT NULL CHECK (
                        length(idempotency_key_sha256) = 64
                        AND idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'
                    ),
                    observation_sha256 TEXT NOT NULL CHECK (
                        length(observation_sha256) = 64
                        AND observation_sha256 NOT GLOB '*[^0-9a-f]*'
                    ),
                    observed_count INTEGER NOT NULL CHECK (
                        observed_count >= 0
                        AND observed_count <= 1000000
                    ),
                    verified INTEGER NOT NULL CHECK (
                        verified IN (0, 1)
                        AND (verified = 0 OR observed_count >= 1)
                    ),
                    reason_code TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    verification_sha256 TEXT NOT NULL CHECK (
                        length(verification_sha256) = 64
                        AND verification_sha256 NOT GLOB '*[^0-9a-f]*'
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_verified_actions_recent
                    ON verified_action_verifications(
                        verified_at DESC,
                        verification_id DESC
                    );
                CREATE TRIGGER IF NOT EXISTS verified_actions_no_update
                    BEFORE UPDATE ON verified_action_verifications
                    BEGIN
                        SELECT RAISE(ABORT, 'verified action records are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS verified_actions_no_delete
                    BEFORE DELETE ON verified_action_verifications
                    BEGIN
                        SELECT RAISE(ABORT, 'verified action records are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS verified_actions_consistent_insert
                    BEFORE INSERT ON verified_action_verifications
                    WHEN NOT EXISTS (
                        SELECT 1
                          FROM approval_execution_claims AS c
                          JOIN action_proposals AS p
                            ON p.proposal_id = c.proposal_id
                         WHERE c.claim_id = NEW.claim_id
                           AND c.proposal_id = NEW.proposal_id
                           AND p.action_kind = NEW.action_kind
                           AND p.payload_sha256 = NEW.request_sha256
                           AND NEW.verified_at >= c.claimed_at
                           AND (
                                (
                                    NEW.verified = 1
                                    AND NEW.observed_count >= 1
                                    AND NEW.reason_code =
                                        'destination_readback_verified'
                                )
                                OR (
                                    NEW.verified = 0
                                    AND NEW.reason_code !=
                                        'destination_readback_verified'
                                )
                           )
                    )
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'inconsistent verified action record'
                        );
                    END;
                CREATE TRIGGER IF NOT EXISTS verified_action_completion_required
                    BEFORE INSERT ON approval_execution_completions
                    WHEN EXISTS (
                        SELECT 1
                          FROM approval_execution_claims AS c
                          JOIN action_proposals AS p
                            ON p.proposal_id = c.proposal_id
                         WHERE c.claim_id = NEW.claim_id
                           AND p.action_kind GLOB 'verified.*'
                    )
                    AND NOT EXISTS (
                        SELECT 1
                          FROM verified_action_verifications AS v
                         WHERE v.claim_id = NEW.claim_id
                           AND v.verified_at <= NEW.completed_at
                           AND (
                                (
                                    NEW.outcome = 'succeeded'
                                    AND v.verified = 1
                                )
                                OR (
                                    NEW.outcome = 'failed'
                                    AND v.verified = 0
                                )
                           )
                    )
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'verified action completion lacks destination proof'
                        );
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

    @staticmethod
    def _approval_timestamp(name: str, value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > 40:
            raise ValueError(f"{name} must be an RFC 3339 timestamp")
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError(f"{name} must be an RFC 3339 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{name} must include a timezone")
        return (
            parsed.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _approval_safe_id(name: str, value: Any) -> str:
        if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
            raise ValueError(f"{name} must be a safe 1-128 character identifier")
        return value

    @staticmethod
    def _approval_sha256(name: str, value: Any) -> str:
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        return value

    @staticmethod
    def _decode_action_proposal(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "proposal_id": row["proposal_id"],
            "storage_id": row["storage_id"],
            "action_id": row["action_id"],
            "policy_id": row["policy_id"],
            "action_kind": row["action_kind"],
            "payload_sha256": row["payload_sha256"],
            "requested_by": row["requested_by"],
            "risk": row["risk"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "proposal_sha256": row["proposal_sha256"],
        }

    @classmethod
    def _validate_action_proposal(
        cls,
        proposal: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(proposal, Mapping) or set(proposal) != _APPROVAL_PROPOSAL_FIELDS:
            raise ValueError("action proposal contains unsupported fields")
        item: dict[str, Any] = {}
        for field in (
            "proposal_id",
            "storage_id",
            "action_id",
            "action_kind",
            "requested_by",
        ):
            item[field] = cls._approval_safe_id(field, proposal.get(field))
        policy_id = proposal.get("policy_id")
        if policy_id not in _APPROVAL_POLICIES:
            raise ValueError("unsupported action policy")
        item["policy_id"] = policy_id
        item["payload_sha256"] = cls._approval_sha256(
            "payload_sha256",
            proposal.get("payload_sha256"),
        )
        risk = proposal.get("risk")
        if risk not in _APPROVAL_RISKS:
            raise ValueError("unsupported action risk")
        item["risk"] = risk
        item["created_at"] = cls._approval_timestamp(
            "created_at",
            proposal.get("created_at"),
        )
        item["expires_at"] = cls._approval_timestamp(
            "expires_at",
            proposal.get("expires_at"),
        )
        if item["created_at"] >= item["expires_at"]:
            raise ValueError("action proposal must expire after it is created")
        item["proposal_sha256"] = cls._approval_sha256(
            "proposal_sha256",
            proposal.get("proposal_sha256"),
        )
        expected_hash = hashlib.sha256(
            _canonical_json(
                {
                    field: item[field]
                    for field in sorted(_APPROVAL_PROPOSAL_FIELDS - {"proposal_sha256"})
                }
            ).encode("utf-8")
        ).hexdigest()
        if item["proposal_sha256"] != expected_hash:
            raise ValueError("action proposal hash does not match its fields")
        return item

    def save_action_proposal(
        self,
        proposal: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Insert one immutable proposal; semantic retries return the first row."""

        item = self._validate_action_proposal(proposal)
        columns = (
            "proposal_id, storage_id, action_id, policy_id, action_kind, "
            "payload_sha256, requested_by, risk, created_at, expires_at, "
            "proposal_sha256"
        )
        semantic_fields = (
            "proposal_id",
            "storage_id",
            "action_id",
            "policy_id",
            "action_kind",
            "payload_sha256",
            "requested_by",
            "risk",
            "expires_at",
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    f"""
                    SELECT {columns}
                      FROM action_proposals
                     WHERE proposal_id = ? OR action_id = ?
                    """,
                    (item["proposal_id"], item["action_id"]),
                ).fetchone()
                if existing is not None:
                    decoded = self._decode_action_proposal(existing)
                    if all(decoded[field] == item[field] for field in semantic_fields):
                        connection.commit()
                        return decoded, False
                    raise ActionProposalConflict(
                        "proposal_id or action_id already has different content"
                    )
                connection.execute(
                    """
                    INSERT INTO action_proposals
                        (
                            proposal_id, storage_id, action_id, policy_id,
                            action_kind, payload_sha256, requested_by, risk,
                            created_at, expires_at, proposal_sha256
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["proposal_id"],
                        item["storage_id"],
                        item["action_id"],
                        item["policy_id"],
                        item["action_kind"],
                        item["payload_sha256"],
                        item["requested_by"],
                        item["risk"],
                        item["created_at"],
                        item["expires_at"],
                        item["proposal_sha256"],
                    ),
                )
                connection.commit()
                return item, True
            except Exception:
                connection.rollback()
                raise

    def get_action_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        if not isinstance(proposal_id, str) or not _SAFE_ID_RE.fullmatch(proposal_id):
            return None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT proposal_id, storage_id, action_id, policy_id,
                       action_kind, payload_sha256, requested_by, risk,
                       created_at, expires_at, proposal_sha256
                  FROM action_proposals
                 WHERE proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
        return self._decode_action_proposal(row) if row is not None else None

    def list_action_proposals(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
            raise ValueError("offset must be between 0 and 1,000,000")
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT proposal_id, storage_id, action_id, policy_id,
                       action_kind, payload_sha256, requested_by, risk,
                       created_at, expires_at, proposal_sha256
                  FROM action_proposals
                 ORDER BY created_at DESC, proposal_id DESC
                 LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [self._decode_action_proposal(row) for row in rows]

    @staticmethod
    def _decode_human_approval(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "approval_id": row["approval_id"],
            "proposal_id": row["proposal_id"],
            "proposal_sha256": row["proposal_sha256"],
            "outcome": row["outcome"],
            "decided_by": row["decided_by"],
            "reason_code": row["reason_code"],
            "decided_at": row["decided_at"],
        }

    @classmethod
    def _validate_human_approval(
        cls,
        decision: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(decision, Mapping) or set(decision) != _APPROVAL_DECISION_FIELDS:
            raise ValueError("human approval decision contains unsupported fields")
        item: dict[str, Any] = {}
        for field in (
            "approval_id",
            "proposal_id",
            "decided_by",
            "reason_code",
        ):
            item[field] = cls._approval_safe_id(field, decision.get(field))
        item["proposal_sha256"] = cls._approval_sha256(
            "proposal_sha256",
            decision.get("proposal_sha256"),
        )
        outcome = decision.get("outcome")
        if outcome not in _APPROVAL_OUTCOMES:
            raise ValueError("human outcome must be approved or rejected")
        item["outcome"] = outcome
        item["decided_at"] = cls._approval_timestamp(
            "decided_at",
            decision.get("decided_at"),
        )
        return item

    def save_human_approval(
        self,
        decision: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Insert the one terminal human decision for a proposal."""

        item = self._validate_human_approval(decision)
        semantic_fields = (
            "proposal_id",
            "proposal_sha256",
            "outcome",
            "decided_by",
            "reason_code",
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT approval_id, proposal_id, proposal_sha256, outcome,
                           decided_by, reason_code, decided_at
                      FROM human_approval_decisions
                     WHERE proposal_id = ? OR approval_id = ?
                    """,
                    (item["proposal_id"], item["approval_id"]),
                ).fetchone()
                if existing is not None:
                    decoded = self._decode_human_approval(existing)
                    if all(decoded[field] == item[field] for field in semantic_fields):
                        connection.commit()
                        return decoded, False
                    raise HumanApprovalConflict(
                        "proposal already has a different human decision"
                    )
                proposal = connection.execute(
                    """
                    SELECT proposal_sha256, created_at, expires_at
                      FROM action_proposals
                     WHERE proposal_id = ?
                    """,
                    (item["proposal_id"],),
                ).fetchone()
                if proposal is None:
                    raise HumanApprovalConflict("action proposal does not exist")
                if (
                    proposal["proposal_sha256"] != item["proposal_sha256"]
                    or item["decided_at"] < proposal["created_at"]
                    or item["decided_at"] > proposal["expires_at"]
                ):
                    raise HumanApprovalConflict(
                        "human decision is not bound to a live proposal"
                    )
                connection.execute(
                    """
                    INSERT INTO human_approval_decisions
                        (
                            approval_id, proposal_id, proposal_sha256, outcome,
                            decided_by, reason_code, decided_at
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["approval_id"],
                        item["proposal_id"],
                        item["proposal_sha256"],
                        item["outcome"],
                        item["decided_by"],
                        item["reason_code"],
                        item["decided_at"],
                    ),
                )
                connection.commit()
                return item, True
            except Exception:
                connection.rollback()
                raise

    def get_human_approval(
        self,
        proposal_id: str,
    ) -> dict[str, Any] | None:
        if not isinstance(proposal_id, str) or not _SAFE_ID_RE.fullmatch(proposal_id):
            return None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT approval_id, proposal_id, proposal_sha256, outcome,
                       decided_by, reason_code, decided_at
                  FROM human_approval_decisions
                 WHERE proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
        return self._decode_human_approval(row) if row is not None else None

    @staticmethod
    def _decode_approval_claim(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "claim_id": row["claim_id"],
            "proposal_id": row["proposal_id"],
            "approval_id": row["approval_id"],
            "action_id": row["action_id"],
            "gate_decision_id": row["gate_decision_id"],
            "gate_decision_sha256": row["gate_decision_sha256"],
            "claimed_at": row["claimed_at"],
        }

    @classmethod
    def _validate_approval_claim(
        cls,
        claim: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(claim, Mapping) or set(claim) != _APPROVAL_CLAIM_FIELDS:
            raise ValueError("approval execution claim contains unsupported fields")
        item: dict[str, Any] = {}
        for field in (
            "claim_id",
            "proposal_id",
            "approval_id",
            "action_id",
            "gate_decision_id",
        ):
            item[field] = cls._approval_safe_id(field, claim.get(field))
        item["gate_decision_sha256"] = cls._approval_sha256(
            "gate_decision_sha256",
            claim.get("gate_decision_sha256"),
        )
        item["claimed_at"] = cls._approval_timestamp(
            "claimed_at",
            claim.get("claimed_at"),
        )
        return item

    def claim_approved_execution(
        self,
        claim: Mapping[str, Any],
        *,
        gate_decision: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Atomically bind one execution claim to approval and current Truth."""

        item = self._validate_approval_claim(claim)
        normalized_gate = self._validate_action_decision(gate_decision)
        gate_hash = hashlib.sha256(
            _canonical_json(normalized_gate).encode("utf-8")
        ).hexdigest()
        if (
            normalized_gate["decision_id"] != item["gate_decision_id"]
            or gate_hash != item["gate_decision_sha256"]
        ):
            raise ApprovalExecutionConflict(
                "execution claim does not match the audited gate decision"
            )

        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT claim_id, proposal_id, approval_id, action_id,
                           gate_decision_id, gate_decision_sha256, claimed_at
                      FROM approval_execution_claims
                     WHERE proposal_id = ? OR action_id = ? OR claim_id = ?
                    """,
                    (
                        item["proposal_id"],
                        item["action_id"],
                        item["claim_id"],
                    ),
                ).fetchone()
                if existing is not None:
                    decoded = self._decode_approval_claim(existing)
                    if (
                        decoded["proposal_id"] == item["proposal_id"]
                        and decoded["action_id"] == item["action_id"]
                    ):
                        connection.commit()
                        return decoded, False
                    raise ApprovalExecutionConflict(
                        "execution claim identifier already has different content"
                    )

                approval = connection.execute(
                    """
                    SELECT p.storage_id, p.action_id, p.policy_id,
                           p.proposal_sha256, p.created_at, p.expires_at,
                           a.approval_id, a.proposal_sha256 AS approved_hash,
                           a.outcome, a.decided_at
                      FROM action_proposals AS p
                      LEFT JOIN human_approval_decisions AS a
                        ON a.proposal_id = p.proposal_id
                     WHERE p.proposal_id = ?
                    """,
                    (item["proposal_id"],),
                ).fetchone()
                if (
                    approval is None
                    or approval["approval_id"] != item["approval_id"]
                    or approval["outcome"] != "approved"
                    or approval["approved_hash"] != approval["proposal_sha256"]
                    or approval["action_id"] != item["action_id"]
                    or item["claimed_at"] < approval["decided_at"]
                    or item["claimed_at"] > approval["expires_at"]
                ):
                    raise ApprovalExecutionConflict(
                        "execution is not backed by a current human approval"
                    )

                stored_gate = connection.execute(
                    """
                    SELECT decision_id, storage_id, action_id, policy_id,
                           observed_verdict, authorized, reason_codes_json,
                           decided_at
                      FROM action_decisions
                     WHERE decision_id = ?
                    """,
                    (item["gate_decision_id"],),
                ).fetchone()
                if stored_gate is None:
                    raise ApprovalExecutionConflict(
                        "audited gate decision is unavailable"
                    )
                decoded_gate = self._decode_action_decision(stored_gate)
                if (
                    decoded_gate != normalized_gate
                    or not decoded_gate["authorized"]
                    or decoded_gate["storage_id"] != approval["storage_id"]
                    or decoded_gate["action_id"] != approval["action_id"]
                    or decoded_gate["policy_id"] != approval["policy_id"]
                    or decoded_gate["decided_at"] > item["claimed_at"]
                ):
                    raise ApprovalExecutionConflict(
                        "gate decision is not bound to the approved action"
                    )

                latest = connection.execute(
                    """
                    SELECT verdict, fresh_until
                      FROM verdicts
                     WHERE storage_id = ?
                     ORDER BY id DESC
                     LIMIT 1
                    """,
                    (approval["storage_id"],),
                ).fetchone()
                policy_allows = (
                    latest is not None
                    and (
                        latest["verdict"] == "healthy"
                        or (
                            latest["verdict"] == "verified_no_work"
                            and approval["policy_id"] == "trusted_completion"
                        )
                    )
                )
                if (
                    not policy_allows
                    or latest["verdict"] != decoded_gate["observed_verdict"]
                    or latest["fresh_until"] is None
                    or latest["fresh_until"] < item["claimed_at"]
                ):
                    raise ApprovalExecutionConflict(
                        "Truth changed or expired before execution claim"
                    )
                connection.execute(
                    """
                    INSERT INTO approval_execution_claims
                        (
                            claim_id, proposal_id, approval_id, action_id,
                            gate_decision_id, gate_decision_sha256, claimed_at
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["claim_id"],
                        item["proposal_id"],
                        item["approval_id"],
                        item["action_id"],
                        item["gate_decision_id"],
                        item["gate_decision_sha256"],
                        item["claimed_at"],
                    ),
                )
                connection.commit()
                return item, True
            except Exception:
                connection.rollback()
                raise

    def get_approval_execution_claim(
        self,
        proposal_id: str,
    ) -> dict[str, Any] | None:
        if not isinstance(proposal_id, str) or not _SAFE_ID_RE.fullmatch(proposal_id):
            return None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT claim_id, proposal_id, approval_id, action_id,
                       gate_decision_id, gate_decision_sha256, claimed_at
                  FROM approval_execution_claims
                 WHERE proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
        return self._decode_approval_claim(row) if row is not None else None

    @staticmethod
    def _decode_approval_completion(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "completion_id": row["completion_id"],
            "claim_id": row["claim_id"],
            "outcome": row["outcome"],
            "reason_code": row["reason_code"],
            "completed_at": row["completed_at"],
        }

    @classmethod
    def _validate_approval_completion(
        cls,
        completion: Mapping[str, Any],
    ) -> dict[str, Any]:
        if (
            not isinstance(completion, Mapping)
            or set(completion) != _APPROVAL_COMPLETION_FIELDS
        ):
            raise ValueError("approval completion contains unsupported fields")
        item: dict[str, Any] = {}
        for field in ("completion_id", "claim_id", "reason_code"):
            item[field] = cls._approval_safe_id(field, completion.get(field))
        outcome = completion.get("outcome")
        if outcome not in _APPROVAL_COMPLETION_OUTCOMES:
            raise ValueError("unsupported approval completion outcome")
        item["outcome"] = outcome
        item["completed_at"] = cls._approval_timestamp(
            "completed_at",
            completion.get("completed_at"),
        )
        return item

    def save_approval_execution_completion(
        self,
        completion: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Persist the one immutable, payload-free completion for a claim."""

        item = self._validate_approval_completion(completion)
        semantic_fields = ("claim_id", "outcome", "reason_code")
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT completion_id, claim_id, outcome, reason_code,
                           completed_at
                      FROM approval_execution_completions
                     WHERE claim_id = ? OR completion_id = ?
                    """,
                    (item["claim_id"], item["completion_id"]),
                ).fetchone()
                if existing is not None:
                    decoded = self._decode_approval_completion(existing)
                    if all(decoded[field] == item[field] for field in semantic_fields):
                        connection.commit()
                        return decoded, False
                    raise ApprovalExecutionConflict(
                        "execution claim already has a different completion"
                    )
                claim = connection.execute(
                    """
                    SELECT claimed_at
                      FROM approval_execution_claims
                     WHERE claim_id = ?
                    """,
                    (item["claim_id"],),
                ).fetchone()
                if claim is None or item["completed_at"] < claim["claimed_at"]:
                    raise ApprovalExecutionConflict(
                        "completion is not bound to a valid execution claim"
                    )
                connection.execute(
                    """
                    INSERT INTO approval_execution_completions
                        (
                            completion_id, claim_id, outcome, reason_code,
                            completed_at
                        )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        item["completion_id"],
                        item["claim_id"],
                        item["outcome"],
                        item["reason_code"],
                        item["completed_at"],
                    ),
                )
                connection.commit()
                return item, True
            except Exception:
                connection.rollback()
                raise

    def get_approval_execution_completion(
        self,
        claim_id: str,
    ) -> dict[str, Any] | None:
        if not isinstance(claim_id, str) or not _SAFE_ID_RE.fullmatch(claim_id):
            return None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT completion_id, claim_id, outcome, reason_code,
                       completed_at
                  FROM approval_execution_completions
                 WHERE claim_id = ?
                """,
                (claim_id,),
            ).fetchone()
        return self._decode_approval_completion(row) if row is not None else None

    @staticmethod
    def _decode_verified_action_verification(
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        return {
            "verification_id": row["verification_id"],
            "proposal_id": row["proposal_id"],
            "claim_id": row["claim_id"],
            "adapter_id": row["adapter_id"],
            "adapter_version": row["adapter_version"],
            "action_kind": row["action_kind"],
            "request_sha256": row["request_sha256"],
            "idempotency_key_sha256": row["idempotency_key_sha256"],
            "observation_sha256": row["observation_sha256"],
            "observed_count": int(row["observed_count"]),
            "verified": bool(row["verified"]),
            "reason_code": row["reason_code"],
            "verified_at": row["verified_at"],
            "verification_sha256": row["verification_sha256"],
        }

    @classmethod
    def _validate_verified_action_verification(
        cls,
        verification: Mapping[str, Any],
    ) -> dict[str, Any]:
        if (
            not isinstance(verification, Mapping)
            or set(verification) != _VERIFIED_ACTION_VERIFICATION_FIELDS
        ):
            raise ValueError(
                "verified action record contains unsupported fields"
            )
        item: dict[str, Any] = {}
        for field in (
            "verification_id",
            "proposal_id",
            "claim_id",
            "adapter_id",
            "adapter_version",
            "action_kind",
            "reason_code",
        ):
            item[field] = cls._approval_safe_id(
                field,
                verification.get(field),
            )
        for field in (
            "request_sha256",
            "idempotency_key_sha256",
            "observation_sha256",
        ):
            item[field] = cls._approval_sha256(
                field,
                verification.get(field),
            )
        observed_count = verification.get("observed_count")
        if (
            type(observed_count) is not int
            or not 0 <= observed_count <= 1_000_000
        ):
            raise ValueError("observed_count must be an integer from 0 to 1000000")
        item["observed_count"] = observed_count
        verified = verification.get("verified")
        if not isinstance(verified, bool):
            raise ValueError("verified must be a boolean")
        item["verified"] = verified
        if verified and observed_count < 1:
            raise ValueError(
                "a verified action must include at least one observed record"
            )
        if (
            verified
            and item["reason_code"] != "destination_readback_verified"
        ) or (
            not verified
            and item["reason_code"] == "destination_readback_verified"
        ):
            raise ValueError(
                "verified action reason code is inconsistent with its result"
            )
        item["verified_at"] = cls._approval_timestamp(
            "verified_at",
            verification.get("verified_at"),
        )
        item["verification_sha256"] = cls._approval_sha256(
            "verification_sha256",
            verification.get("verification_sha256"),
        )
        expected_hash = hashlib.sha256(
            _canonical_json(
                {
                    field: item[field]
                    for field in sorted(
                        _VERIFIED_ACTION_VERIFICATION_FIELDS
                        - {"verification_sha256"}
                    )
                }
            ).encode("utf-8")
        ).hexdigest()
        if item["verification_sha256"] != expected_hash:
            raise ValueError(
                "verified action record hash does not match its fields"
            )
        return item

    def save_verified_action_verification(
        self,
        verification: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Persist one immutable, payload-free destination read-back result."""

        item = self._validate_verified_action_verification(verification)
        semantic_fields = tuple(
            sorted(
                _VERIFIED_ACTION_VERIFICATION_FIELDS
                - {"verification_id", "verified_at", "verification_sha256"}
            )
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT verification_id, proposal_id, claim_id, adapter_id,
                           adapter_version, action_kind, request_sha256,
                           idempotency_key_sha256, observation_sha256,
                           observed_count, verified, reason_code, verified_at,
                           verification_sha256
                      FROM verified_action_verifications
                     WHERE verification_id = ?
                        OR proposal_id = ?
                        OR claim_id = ?
                    """,
                    (
                        item["verification_id"],
                        item["proposal_id"],
                        item["claim_id"],
                    ),
                ).fetchone()
                if existing is not None:
                    decoded = self._decode_verified_action_verification(
                        existing
                    )
                    if all(
                        decoded[field] == item[field]
                        for field in semantic_fields
                    ):
                        connection.commit()
                        return decoded, False
                    raise VerifiedActionVerificationConflict(
                        "execution claim already has a different verification"
                    )

                binding = connection.execute(
                    """
                    SELECT c.proposal_id, c.claimed_at, p.action_kind,
                           p.payload_sha256
                      FROM approval_execution_claims AS c
                      JOIN action_proposals AS p
                        ON p.proposal_id = c.proposal_id
                     WHERE c.claim_id = ?
                    """,
                    (item["claim_id"],),
                ).fetchone()
                if (
                    binding is None
                    or binding["proposal_id"] != item["proposal_id"]
                    or binding["action_kind"] != item["action_kind"]
                    or binding["payload_sha256"] != item["request_sha256"]
                    or item["verified_at"] < binding["claimed_at"]
                ):
                    raise VerifiedActionVerificationConflict(
                        "verification is not bound to the claimed exact action"
                    )

                connection.execute(
                    """
                    INSERT INTO verified_action_verifications
                        (
                            verification_id, proposal_id, claim_id,
                            adapter_id, adapter_version, action_kind,
                            request_sha256, idempotency_key_sha256,
                            observation_sha256, observed_count, verified,
                            reason_code, verified_at, verification_sha256
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["verification_id"],
                        item["proposal_id"],
                        item["claim_id"],
                        item["adapter_id"],
                        item["adapter_version"],
                        item["action_kind"],
                        item["request_sha256"],
                        item["idempotency_key_sha256"],
                        item["observation_sha256"],
                        item["observed_count"],
                        int(item["verified"]),
                        item["reason_code"],
                        item["verified_at"],
                        item["verification_sha256"],
                    ),
                )
                connection.commit()
                return item, True
            except Exception:
                connection.rollback()
                raise

    def get_verified_action_verification(
        self,
        proposal_id: str,
    ) -> dict[str, Any] | None:
        """Return the immutable read-back result for one proposal, if any."""

        if not isinstance(proposal_id, str) or not _SAFE_ID_RE.fullmatch(
            proposal_id
        ):
            return None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT verification_id, proposal_id, claim_id, adapter_id,
                       adapter_version, action_kind, request_sha256,
                       idempotency_key_sha256, observation_sha256,
                       observed_count, verified, reason_code, verified_at,
                       verification_sha256
                  FROM verified_action_verifications
                 WHERE proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
        return (
            self._decode_verified_action_verification(row)
            if row is not None
            else None
        )

    def get_verified_action_timeline_snapshot(
        self,
        proposal_id: str,
    ) -> dict[str, Any] | None:
        """Read the immutable lifecycle sources in one consistent snapshot."""

        if not isinstance(proposal_id, str) or not _SAFE_ID_RE.fullmatch(
            proposal_id
        ):
            return None
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN")
            try:
                proposal_row = connection.execute(
                    """
                    SELECT proposal_id, storage_id, action_id, policy_id,
                           action_kind, payload_sha256, requested_by, risk,
                           created_at, expires_at, proposal_sha256
                      FROM action_proposals
                     WHERE proposal_id = ?
                    """,
                    (proposal_id,),
                ).fetchone()
                if proposal_row is None:
                    connection.commit()
                    return None
                proposal = self._decode_action_proposal(proposal_row)
                approval_row = connection.execute(
                    """
                    SELECT approval_id, proposal_id, proposal_sha256, outcome,
                           decided_by, reason_code, decided_at
                      FROM human_approval_decisions
                     WHERE proposal_id = ?
                    """,
                    (proposal_id,),
                ).fetchone()
                claim_row = connection.execute(
                    """
                    SELECT claim_id, proposal_id, approval_id, action_id,
                           gate_decision_id, gate_decision_sha256, claimed_at
                      FROM approval_execution_claims
                     WHERE proposal_id = ?
                    """,
                    (proposal_id,),
                ).fetchone()
                claim = (
                    self._decode_approval_claim(claim_row)
                    if claim_row is not None
                    else None
                )
                if claim is not None:
                    gate_row = connection.execute(
                        """
                        SELECT decision_id, storage_id, action_id, policy_id,
                               observed_verdict, authorized,
                               reason_codes_json, decided_at
                          FROM action_decisions
                         WHERE decision_id = ?
                        """,
                        (claim["gate_decision_id"],),
                    ).fetchone()
                else:
                    gate_row = connection.execute(
                        """
                        SELECT decision_id, storage_id, action_id, policy_id,
                               observed_verdict, authorized,
                               reason_codes_json, decided_at
                          FROM action_decisions
                         WHERE storage_id = ?
                           AND action_id = ?
                           AND decided_at >= ?
                         ORDER BY decided_at DESC, decision_id DESC
                         LIMIT 1
                        """,
                        (
                            proposal["storage_id"],
                            proposal["action_id"],
                            proposal["created_at"],
                        ),
                    ).fetchone()
                verification_row = connection.execute(
                    """
                    SELECT verification_id, proposal_id, claim_id, adapter_id,
                           adapter_version, action_kind, request_sha256,
                           idempotency_key_sha256, observation_sha256,
                           observed_count, verified, reason_code, verified_at,
                           verification_sha256
                      FROM verified_action_verifications
                     WHERE proposal_id = ?
                    """,
                    (proposal_id,),
                ).fetchone()
                completion_row = (
                    connection.execute(
                        """
                        SELECT completion_id, claim_id, outcome, reason_code,
                               completed_at
                          FROM approval_execution_completions
                         WHERE claim_id = ?
                        """,
                        (claim["claim_id"],),
                    ).fetchone()
                    if claim is not None
                    else None
                )
                snapshot = {
                    "proposal": proposal,
                    "approval": (
                        self._decode_human_approval(approval_row)
                        if approval_row is not None
                        else None
                    ),
                    "gate": (
                        self._decode_action_decision(gate_row)
                        if gate_row is not None
                        else None
                    ),
                    "claim": claim,
                    "verification": (
                        self._decode_verified_action_verification(
                            verification_row
                        )
                        if verification_row is not None
                        else None
                    ),
                    "completion": (
                        self._decode_approval_completion(completion_row)
                        if completion_row is not None
                        else None
                    ),
                }
                connection.commit()
                return snapshot
            except Exception:
                connection.rollback()
                raise

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
