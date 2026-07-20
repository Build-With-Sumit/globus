"""Credential-free proof that Truth verdicts can gate a business action.

The challenge uses only generated, deidentified rows in a per-run SQLite
destination.  It independently reopens that destination, canonicalizes the
observed rows, and hashes them before producing each Truth receipt.  A healthy
receipt may authorize one bounded local outbox insert.  After exactly one
destination row is deleted, the contradictory receipt must deny the same
action, so its callback is never invoked a second time.

Only generated identifiers, relative paths, measurements, verdicts, and safe
gate decisions are returned.  Destination payloads and absolute paths remain
local.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_AGENT_ID = "judge-mode:business-outcome-gate"
_DATABASE_NAME = "destination.sqlite"
_POLICY_ID = "healthy_only"
_CLAIMED_COUNT = 3
_ACTION_KIND = "queue_follow_up_review"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _default_challenge_root(service: Any) -> Path:
    repository = getattr(service, "repository", None)
    database = str(getattr(repository, "database", "") or "")
    if database and database != ":memory:":
        return (
            Path(database).expanduser().resolve().parent
            / "outcome-challenges"
        )
    return (
        Path(tempfile.gettempdir()).resolve()
        / f"globus-truth-outcomes-{os.getpid()}"
    )


def _create_challenge_directory(
    root: Path,
    now: datetime,
) -> tuple[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    resolved_root = root.resolve()
    timestamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for _ in range(8):
        challenge_id = f"outcome-{timestamp}-{secrets.token_hex(6)}"
        challenge_directory = resolved_root / challenge_id
        try:
            challenge_directory.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        if challenge_directory.resolve().parent != resolved_root:
            raise RuntimeError("outcome challenge directory escaped its root")
        return challenge_id, challenge_directory
    raise FileExistsError("could not allocate a unique outcome challenge")


def _generated_follow_ups(challenge_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sequence in range(1, _CLAIMED_COUNT + 1):
        opaque_id = hashlib.sha256(
            f"{challenge_id}:{sequence}".encode("utf-8")
        ).hexdigest()[:20]
        rows.append(
            {
                "record_id": f"followup-{opaque_id}",
                "route": "local-demo",
                "sequence": sequence,
                "state": "ready",
            }
        )
    return rows


def _canonical_bytes(rows: list[Mapping[str, Any]]) -> bytes:
    normalized = [
        {
            "record_id": str(row["record_id"]),
            "route": str(row["route"]),
            "sequence": int(row["sequence"]),
            "state": str(row["state"]),
        }
        for row in sorted(rows, key=lambda item: str(item["record_id"]))
    ]
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _measurement(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    canonical = _canonical_bytes(rows)
    return {
        "count": len(rows),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _initialize_destination(
    database_path: Path,
    rows: list[Mapping[str, Any]],
) -> None:
    if database_path.exists():
        raise FileExistsError("outcome destination already exists")
    with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
        with connection:
            connection.executescript(
                """
                CREATE TABLE destination_follow_ups (
                    record_id TEXT PRIMARY KEY,
                    route TEXT NOT NULL,
                    sequence INTEGER NOT NULL UNIQUE,
                    state TEXT NOT NULL
                );
                CREATE TABLE local_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id TEXT NOT NULL UNIQUE,
                    action_kind TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.executemany(
                """
                INSERT INTO destination_follow_ups
                    (record_id, route, sequence, state)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        row["record_id"],
                        row["route"],
                        row["sequence"],
                        row["state"],
                    )
                    for row in rows
                ],
            )


def _read_destination(database_path: Path) -> dict[str, Any]:
    """Independently reopen, select, sort, canonicalize, and hash the rows."""

    with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        observed = connection.execute(
            """
            SELECT record_id, route, sequence, state
              FROM destination_follow_ups
             ORDER BY record_id
            """
        ).fetchall()
    return _measurement([dict(row) for row in observed])


def _outbox_count(database_path: Path) -> int:
    with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            "SELECT COUNT(*) FROM local_outbox"
        ).fetchone()
    return int(row[0]) if row is not None else 0


def _insert_local_outbox(
    database_path: Path,
    *,
    action_id: str,
    created_at: datetime,
) -> bool:
    """Execute the challenge's only bounded action.

    The caller must invoke this function only after a bound, healthy gate
    decision.  A plain INSERT and a UNIQUE action ID make accidental retries
    visible rather than silently duplicating the action.
    """

    with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO local_outbox
                    (action_id, action_kind, created_at)
                VALUES (?, ?, ?)
                """,
                (action_id, _ACTION_KIND, _iso(created_at)),
            )
    return cursor.rowcount == 1


def _delete_one_follow_up(
    database_path: Path,
    *,
    record_id: str,
) -> int:
    with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
        with connection:
            cursor = connection.execute(
                "DELETE FROM destination_follow_ups WHERE record_id = ?",
                (record_id,),
            )
    return max(int(cursor.rowcount), 0)


def _receipt(
    *,
    challenge_id: str,
    phase: str,
    started_at: datetime,
    finished_at: datetime,
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> dict[str, Any]:
    phase_order = {"before_change": "01", "after_change": "02"}
    ordinal = phase_order.get(phase)
    if ordinal is None:
        raise ValueError("unsupported outcome challenge phase")

    expected_count = int(expected["count"])
    observed_count = int(observed["count"])
    expected_sha256 = str(expected["sha256"])
    observed_sha256 = str(observed["sha256"])
    count_matches = observed_count == expected_count
    sha256_matches = observed_sha256 == expected_sha256
    observed_at = _iso(finished_at)
    valid = count_matches and sha256_matches

    return {
        "schema_version": "1.0",
        "receipt_id": f"{challenge_id}-{ordinal}-{phase}",
        "agent_id": _AGENT_ID,
        "run_id": f"{challenge_id}:{ordinal}:{phase}",
        "declared_status": "success",
        "started_at": _iso(started_at),
        "finished_at": observed_at,
        "heartbeat_at": observed_at,
        "input": {
            "items_seen": expected_count,
            "items_eligible": expected_count,
        },
        "output": {
            "items_processed": observed_count,
            "items_changed": observed_count if valid else 0,
        },
        "summary": (
            "Outcome Gate independently reopened the generated destination "
            f"and verified its follow-up rows during {phase.replace('_', ' ')}."
        ),
        "evidence": [
            {
                "kind": "database_write",
                "ref": f"destination-snapshot:{_DATABASE_NAME}",
                "observed_at": observed_at,
                "detail": (
                    f"Observed {observed_count} generated destination rows "
                    "through an independent SQLite connection."
                ),
                "sha256": observed_sha256,
            }
        ],
        "checks": [
            {
                "name": "destination_readback",
                "passed": True,
                "detail": (
                    "The destination was independently reopened and queried."
                ),
            },
            {
                "name": "destination_count_matches",
                "passed": count_matches,
                "detail": (
                    f"Claimed {expected_count} destination rows and observed "
                    f"{observed_count}."
                ),
            },
            {
                "name": "destination_sha256_matches",
                "passed": sha256_matches,
                "detail": (
                    "Canonical destination SHA-256 matched the original rows."
                    if sha256_matches
                    else (
                        "Canonical destination SHA-256 differed from the "
                        "original generated rows."
                    )
                ),
            },
        ],
        "metadata": {
            "mode": "credential-free-business-outcome-challenge",
            "challenge_id": challenge_id,
            "phase": phase,
            "claimed_count": expected_count,
            "observed_count": observed_count,
        },
    }


def _phase_result(
    *,
    name: str,
    receipt: Mapping[str, Any],
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
    ingest_result: Mapping[str, Any],
) -> dict[str, Any]:
    evaluation = ingest_result.get("evaluation") or {}
    expected_count = int(expected["count"])
    observed_count = int(observed["count"])
    expected_sha256 = str(expected["sha256"])
    observed_sha256 = str(observed["sha256"])
    return {
        "name": name,
        "receipt_id": receipt.get("receipt_id"),
        "storage_id": ingest_result.get("storage_id"),
        "verdict": evaluation.get("verdict"),
        "valid": bool(evaluation.get("valid")),
        "claimed_count": expected_count,
        "observed_count": observed_count,
        "expected_sha256": expected_sha256,
        "observed_sha256": observed_sha256,
        "count_matches": observed_count == expected_count,
        "sha256_matches": observed_sha256 == expected_sha256,
    }


def _safe_identifier(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        return ""
    return value


def _safe_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 40:
        return ""
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return ""
    return value if parsed.tzinfo is not None else ""


def _safe_decision(
    decision: Any,
    *,
    storage_id: str,
    action_id: str,
) -> dict[str, Any]:
    raw = decision if isinstance(decision, Mapping) else {}
    observed_verdict = _safe_identifier(
        raw.get("observed_verdict") or raw.get("verdict")
    )
    reason_codes = raw.get("reason_codes")
    safe_reasons = (
        [
            item
            for item in reason_codes[:20]
            if _safe_identifier(item)
        ]
        if isinstance(reason_codes, (list, tuple))
        else []
    )
    safe = {
        "decision_id": _safe_identifier(raw.get("decision_id")),
        "storage_id": _safe_identifier(raw.get("storage_id")),
        "action_id": _safe_identifier(raw.get("action_id")),
        "policy_id": _safe_identifier(raw.get("policy_id")),
        "authorized": raw.get("authorized") is True,
        "observed_verdict": observed_verdict,
        "verdict": _safe_identifier(
            raw.get("verdict") or observed_verdict
        ),
        "reason_codes": safe_reasons,
        "decided_at": _safe_timestamp(raw.get("decided_at")),
    }
    safe["binding_valid"] = (
        safe["storage_id"] == storage_id
        and safe["action_id"] == action_id
        and safe["policy_id"] == _POLICY_ID
    )
    return safe


def _decision_allows(
    decision: Mapping[str, Any],
    phase: Mapping[str, Any],
) -> bool:
    """Fail closed unless gate, binding, and local receipt all agree."""

    return (
        decision.get("authorized") is True
        and decision.get("binding_valid") is True
        and decision.get("audit_verified") is True
        and bool(decision.get("decision_id"))
        and bool(decision.get("decided_at"))
        and decision.get("observed_verdict") == "healthy"
        and phase.get("verdict") == "healthy"
        and phase.get("valid") is True
    )


def _decision_audit_matches(service: Any, decision: Mapping[str, Any]) -> bool:
    """Verify that the exact privacy-safe decision was durably persisted."""

    decision_id = decision.get("decision_id")
    decided_at = decision.get("decided_at")
    if not decision_id or not decided_at:
        return False
    get_decision = getattr(service, "get_action_decision", None)
    if not callable(get_decision):
        return False
    try:
        persisted = get_decision(decision_id)
    except Exception:
        return False
    if not isinstance(persisted, Mapping):
        return False
    fields = (
        "decision_id",
        "storage_id",
        "action_id",
        "policy_id",
        "observed_verdict",
        "authorized",
        "reason_codes",
        "decided_at",
    )
    return all(persisted.get(field) == decision.get(field) for field in fields)


def run_business_outcome_challenge(
    service: Any,
    *,
    challenge_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Prove healthy-allow and contradictory-deny against real local state."""

    started_at = service._now()
    root = (
        Path(challenge_root).expanduser().resolve()
        if challenge_root is not None
        else _default_challenge_root(service)
    )
    challenge_id, challenge_directory = _create_challenge_directory(
        root,
        started_at,
    )
    database_path = challenge_directory / _DATABASE_NAME
    if database_path.resolve().parent != challenge_directory.resolve():
        raise RuntimeError("outcome destination escaped its challenge directory")

    generated_rows = _generated_follow_ups(challenge_id)
    expected = _measurement(generated_rows)
    _initialize_destination(database_path, generated_rows)

    before_observed = _read_destination(database_path)
    before_finished_at = service._now()
    before_receipt = _receipt(
        challenge_id=challenge_id,
        phase="before_change",
        started_at=started_at,
        finished_at=before_finished_at,
        expected=expected,
        observed=before_observed,
    )
    before_ingest = service.ingest(before_receipt)
    before_phase = _phase_result(
        name="before_change",
        receipt=before_receipt,
        expected=expected,
        observed=before_observed,
        ingest_result=before_ingest,
    )

    action_id = f"local-outbox:{challenge_id}"
    before_decision = _safe_decision(
        service.authorize_action(
            str(before_phase["storage_id"]),
            action_id,
            policy_id=_POLICY_ID,
        ),
        storage_id=str(before_phase["storage_id"]),
        action_id=action_id,
    )
    before_decision["audit_verified"] = _decision_audit_matches(
        service,
        before_decision,
    )
    before_phase["gate"] = before_decision

    first_action_executed = False
    if _decision_allows(before_decision, before_phase):
        first_action_executed = _insert_local_outbox(
            database_path,
            action_id=action_id,
            created_at=service._now(),
        )
    outbox_after_first_gate = _outbox_count(database_path)

    mutation_started_at = service._now()
    rows_modified = _delete_one_follow_up(
        database_path,
        record_id=str(generated_rows[-1]["record_id"]),
    )
    after_observed = _read_destination(database_path)
    after_finished_at = service._now()
    after_receipt = _receipt(
        challenge_id=challenge_id,
        phase="after_change",
        started_at=mutation_started_at,
        finished_at=after_finished_at,
        expected=expected,
        observed=after_observed,
    )
    after_ingest = service.ingest(after_receipt)
    after_phase = _phase_result(
        name="after_change",
        receipt=after_receipt,
        expected=expected,
        observed=after_observed,
        ingest_result=after_ingest,
    )
    after_decision = _safe_decision(
        service.authorize_action(
            str(after_phase["storage_id"]),
            action_id,
            policy_id=_POLICY_ID,
        ),
        storage_id=str(after_phase["storage_id"]),
        action_id=action_id,
    )
    after_decision["audit_verified"] = _decision_audit_matches(
        service,
        after_decision,
    )
    after_phase["gate"] = after_decision

    second_action_executed = False
    if _decision_allows(after_decision, after_phase):
        second_action_executed = _insert_local_outbox(
            database_path,
            action_id=action_id,
            created_at=service._now(),
        )
    final_outbox_count = _outbox_count(database_path)

    phases = [before_phase, after_phase]
    relative_path = f"{challenge_id}/{_DATABASE_NAME}"
    expectations_met = (
        expected["count"] == _CLAIMED_COUNT
        and before_phase["claimed_count"] == _CLAIMED_COUNT
        and before_phase["observed_count"] == _CLAIMED_COUNT
        and before_phase["count_matches"]
        and before_phase["sha256_matches"]
        and before_phase["verdict"] == "healthy"
        and before_phase["valid"]
        and before_decision["authorized"]
        and before_decision["binding_valid"]
        and before_decision["audit_verified"]
        and before_decision["observed_verdict"] == "healthy"
        and bool(before_decision["decision_id"])
        and bool(before_decision["decided_at"])
        and first_action_executed
        and outbox_after_first_gate == 1
        and rows_modified == 1
        and after_phase["claimed_count"] == _CLAIMED_COUNT
        and after_phase["observed_count"] == _CLAIMED_COUNT - 1
        and not after_phase["count_matches"]
        and not after_phase["sha256_matches"]
        and after_phase["observed_sha256"] != before_phase["observed_sha256"]
        and after_phase["verdict"] == "degraded_contradictory"
        and not after_phase["valid"]
        and not after_decision["authorized"]
        and after_decision["binding_valid"]
        and after_decision["audit_verified"]
        and after_decision["observed_verdict"] == "degraded_contradictory"
        and bool(after_decision["decision_id"])
        and bool(after_decision["decided_at"])
        and not second_action_executed
        and final_outbox_count == 1
        and not Path(relative_path).is_absolute()
        and ".." not in Path(relative_path).parts
    )

    return {
        "challenge_id": challenge_id,
        "credential_free": True,
        "external_calls": 0,
        "expectations_met": bool(expectations_met),
        "destination": {
            "name": _DATABASE_NAME,
            "relative_path": relative_path,
            "claimed_follow_ups": _CLAIMED_COUNT,
            "before_observed": before_phase["observed_count"],
            "after_observed": after_phase["observed_count"],
            "before_sha256": before_phase["observed_sha256"],
            "after_sha256": after_phase["observed_sha256"],
            "rows_modified": rows_modified,
        },
        "action": {
            "action_id": action_id,
            "kind": _ACTION_KIND,
            "bounded_local_only": True,
            "first_executed": first_action_executed,
            "second_executed": second_action_executed,
            "second_prevented": not second_action_executed,
            "outbox_rows_after_first_gate": outbox_after_first_gate,
            "final_outbox_rows": final_outbox_count,
        },
        "phases": phases,
    }


def run_outcome_gate_challenge(
    service: Any,
    *,
    artifact_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Service-facing name retained for the Outcome Gate HTTP integration."""

    return run_business_outcome_challenge(
        service,
        challenge_root=artifact_root,
    )


__all__ = [
    "run_business_outcome_challenge",
    "run_outcome_gate_challenge",
]
