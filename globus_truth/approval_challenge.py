"""Credential-free proof for the Globus Consequence Firewall.

The challenge stages one generated, high-risk action against a separate local
SQLite destination.  No payload is stored in the Approval Center: the proposal
contains only an exact SHA-256 fingerprint.  A real human click resolves the
proposal, after which the challenge proves that a changed payload is blocked,
the exact payload executes once behind a fresh Truth decision, and a replay
cannot invoke the action again.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .approval_center import ApprovalCenter


_DATABASE_NAME = "approval-destination.sqlite"
_AGENT_ID = "judge-mode:approval-agent"
_ACTION_KIND = "queue_follow_up_review"
_POLICY_ID = "healthy_only"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("challenge clock must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _default_root(service: Any) -> Path:
    repository = getattr(service, "repository", None)
    database = str(getattr(repository, "database", "") or "")
    if database and database != ":memory:":
        return Path(database).expanduser().resolve().parent / "approval-challenges"
    return (
        Path(tempfile.gettempdir()).resolve()
        / f"globus-approval-challenges-{os.getpid()}"
    )


def _root(service: Any, artifact_root: str | os.PathLike[str] | None) -> Path:
    return (
        Path(artifact_root).expanduser().resolve()
        if artifact_root is not None
        else _default_root(service)
    )


def _new_directory(root: Path, now: datetime) -> tuple[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    resolved_root = root.resolve()
    timestamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for _ in range(8):
        challenge_id = f"approval-{timestamp}-{secrets.token_hex(6)}"
        directory = resolved_root / challenge_id
        try:
            directory.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        if directory.resolve().parent != resolved_root:
            raise RuntimeError("approval challenge directory escaped its root")
        return challenge_id, directory
    raise FileExistsError("could not allocate a unique approval challenge")


def _existing_directory(root: Path, proposal_id: str) -> Path:
    if (
        not isinstance(proposal_id, str)
        or not _SAFE_ID_RE.fullmatch(proposal_id)
        or not proposal_id.startswith("approval-")
    ):
        raise ValueError("invalid approval challenge identifier")
    resolved_root = root.resolve()
    directory = (resolved_root / proposal_id).resolve()
    if directory.parent != resolved_root or not directory.is_dir():
        raise FileNotFoundError("approval challenge destination not found")
    return directory


def _initialize_destination(
    database_path: Path,
    *,
    target_id: str,
) -> None:
    if database_path.exists():
        raise FileExistsError("approval challenge destination already exists")
    with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
        with connection:
            connection.executescript(
                """
                CREATE TABLE approval_targets (
                    target_id TEXT PRIMARY KEY,
                    route TEXT NOT NULL,
                    state TEXT NOT NULL
                );
                CREATE TABLE local_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id TEXT NOT NULL UNIQUE,
                    action_kind TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO approval_targets (target_id, route, state)
                VALUES (?, 'local-demo', 'ready')
                """,
                (target_id,),
            )


def _destination_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT target_id, route, state
              FROM approval_targets
             ORDER BY target_id
            """
        ).fetchall()
    ]


def _measurement(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"count": len(rows), "sha256": _sha256(rows)}


def _measure_destination(database_path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
        connection.execute("PRAGMA query_only = ON")
        return _measurement(_destination_rows(connection))


def _action_payload(
    *,
    action_id: str,
    target: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "action_id": action_id,
        "action_kind": _ACTION_KIND,
        "route": str(target["route"]),
        "target_id": str(target["target_id"]),
        "uses": 1,
    }


def _outbox_count(database_path: Path) -> int:
    with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute("SELECT COUNT(*) FROM local_outbox").fetchone()
    return int(row[0]) if row is not None else 0


def _insert_outbox(
    database_path: Path,
    *,
    action_id: str,
    payload_sha256: str,
    expected_measurement: Mapping[str, Any],
    created_at: datetime,
) -> None:
    """Re-bind the local evidence and insert the effect in one transaction."""

    with closing(
        sqlite3.connect(database_path, timeout=5.0, isolation_level=None)
    ) as connection:
        try:
            # This destination BEGIN IMMEDIATE is the local effect's
            # linearization point.  It does not make arbitrary external
            # callbacks atomic with the Truth claim; each provider still
            # needs its own transaction or idempotency key.
            connection.execute("BEGIN IMMEDIATE")
            rows = _destination_rows(connection)
            current = _measurement(rows)
            if (
                current.get("count") != expected_measurement.get("count")
                or current.get("sha256") != expected_measurement.get("sha256")
                or len(rows) != 1
                or _sha256(
                    _action_payload(action_id=action_id, target=rows[0])
                )
                != payload_sha256
            ):
                raise RuntimeError("destination evidence no longer matches")
            connection.execute(
                """
                INSERT INTO local_outbox
                    (action_id, action_kind, payload_sha256, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (action_id, _ACTION_KIND, payload_sha256, _iso(created_at)),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _receipt(
    *,
    challenge_id: str,
    started_at: datetime,
    finished_at: datetime,
    measurement: Mapping[str, Any],
) -> dict[str, Any]:
    observed_at = _iso(finished_at)
    count = int(measurement["count"])
    return {
        "schema_version": "1.0",
        "receipt_id": f"{challenge_id}-truth",
        "agent_id": "judge-mode:approval-readback",
        "run_id": f"{challenge_id}:truth",
        "declared_status": "success",
        "started_at": _iso(started_at),
        "finished_at": observed_at,
        "heartbeat_at": observed_at,
        "input": {"items_seen": count, "items_eligible": count},
        "output": {"items_processed": count, "items_changed": count},
        "summary": (
            "Approval Center independently reopened the generated local "
            "destination before staging the consequential action."
        ),
        "evidence": [
            {
                "kind": "database_write",
                "ref": f"destination-snapshot:{_DATABASE_NAME}",
                "observed_at": observed_at,
                "detail": (
                    f"Observed {count} generated target through an independent "
                    "SQLite connection."
                ),
                "sha256": str(measurement["sha256"]),
            }
        ],
        "checks": [
            {
                "name": "destination_readback",
                "passed": count == 1,
                "detail": f"Expected one generated target and observed {count}.",
            }
        ],
        "metadata": {
            "mode": "credential-free-approval-center-challenge",
            "challenge_id": challenge_id,
        },
    }


def _expected_destination_measurement(
    service: Any,
    proposal_id: str,
    proposal: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Read the immutable stage receipt and recover its exact evidence bind."""

    storage_id = str(proposal.get("storage_id") or "")
    run = service.get_run(storage_id)
    if (
        not isinstance(run, Mapping)
        or run.get("storage_id") != storage_id
        or storage_id != f"{proposal_id}-truth"
    ):
        return None
    evaluation = run.get("evaluation")
    receipt = run.get("receipt")
    if (
        not isinstance(evaluation, Mapping)
        or evaluation.get("verdict") != "healthy"
        or evaluation.get("valid") is not True
        or not isinstance(receipt, Mapping)
        or receipt.get("receipt_id") != storage_id
    ):
        return None
    metadata = receipt.get("metadata")
    input_counts = receipt.get("input")
    output_counts = receipt.get("output")
    evidence = receipt.get("evidence")
    checks = receipt.get("checks")
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("mode")
        != "credential-free-approval-center-challenge"
        or metadata.get("challenge_id") != proposal_id
        or not isinstance(input_counts, Mapping)
        or not isinstance(output_counts, Mapping)
        or not isinstance(evidence, list)
        or len(evidence) != 1
        or not isinstance(evidence[0], Mapping)
        or not isinstance(checks, list)
        or len(checks) != 1
        or not isinstance(checks[0], Mapping)
        or checks[0].get("name") != "destination_readback"
        or checks[0].get("passed") is not True
    ):
        return None
    count = input_counts.get("items_seen")
    digest = evidence[0].get("sha256")
    if (
        type(count) is not int
        or count != 1
        or input_counts.get("items_eligible") != count
        or output_counts.get("items_processed") != count
        or output_counts.get("items_changed") != count
        or evidence[0].get("kind") != "database_write"
        or evidence[0].get("ref")
        != f"destination-snapshot:{_DATABASE_NAME}"
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        return None
    return {"count": count, "sha256": digest}


def _resolution_evidence(
    service: Any,
    database_path: Path,
    proposal_id: str,
    proposal: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        expected = _expected_destination_measurement(
            service,
            proposal_id,
            proposal,
        )
    except Exception:
        return None, "truth_evidence_not_current"
    if expected is None:
        return None, "truth_evidence_not_current"
    try:
        with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
            connection.execute("PRAGMA query_only = ON")
            rows = _destination_rows(connection)
    except Exception:
        return None, "destination_evidence_unavailable"
    current = _measurement(rows)
    if current != expected or len(rows) != 1:
        return None, "destination_evidence_changed"
    try:
        bound_payload_sha256 = _sha256(
            _action_payload(
                action_id=str(proposal["action_id"]),
                target=rows[0],
            )
        )
    except (KeyError, TypeError, ValueError):
        return None, "approval_scope_mismatch"
    if bound_payload_sha256 != proposal.get("payload_sha256"):
        return None, "approval_scope_mismatch"
    return expected, None


def _proposal_view(
    service: Any,
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    run = service.get_run(str(proposal["storage_id"]))
    evaluation = run.get("evaluation") if isinstance(run, Mapping) else {}
    verdict = (
        evaluation.get("verdict")
        if isinstance(evaluation, Mapping)
        else "unavailable"
    )
    return {
        "proposal_id": proposal["proposal_id"],
        "agent_id": proposal["requested_by"],
        "capability_id": proposal["action_kind"],
        "action_id": proposal["action_id"],
        "risk": proposal["risk"],
        "approval_mode": "explicit",
        "summary": "Queue one generated follow-up for operator review.",
        "scope": "1 generated recipient \u00b7 local demo",
        "payload_sha256": proposal["payload_sha256"],
        "truth_storage_id": proposal["storage_id"],
        "truth_verdict": verdict,
        "created_at": proposal["created_at"],
        "expires_at": proposal["expires_at"],
        "max_uses": 1,
    }


def _pending_report(
    service: Any,
    center: ApprovalCenter,
    proposal_id: str,
    *,
    created: bool,
    database_path: Path,
) -> dict[str, Any]:
    state = center.get(proposal_id)
    if not isinstance(state, Mapping):
        raise RuntimeError("approval proposal could not be read back")
    proposal = state["proposal"]
    return {
        "challenge_id": proposal_id,
        "proposal_id": proposal_id,
        "status": state["state"],
        "created": created,
        "credential_free": True,
        "external_calls": 0,
        "proposal": _proposal_view(service, proposal),
        "action": {
            "kind": _ACTION_KIND,
            "executions": _outbox_count(database_path),
            "bounded_local_only": True,
        },
        "timeline": [
            {
                "event": "proposed",
                "occurred_at": proposal["created_at"],
                "reason_code": "awaiting_human_review",
            }
        ],
    }


def stage_approval_center_challenge(
    service: Any,
    *,
    artifact_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Stage one exact proposal and stop with zero executed actions."""

    now = service._now()
    challenge_id, directory = _new_directory(_root(service, artifact_root), now)
    database_path = directory / _DATABASE_NAME
    target_id = "target-" + hashlib.sha256(
        challenge_id.encode("utf-8")
    ).hexdigest()[:20]
    _initialize_destination(database_path, target_id=target_id)
    measurement = _measure_destination(database_path)
    finished_at = service._now()
    ingested = service.ingest(
        _receipt(
            challenge_id=challenge_id,
            started_at=now,
            finished_at=finished_at,
            measurement=measurement,
        )
    )
    if (
        ingested.get("evaluation", {}).get("verdict") != "healthy"
        or ingested.get("evaluation", {}).get("valid") is not True
    ):
        raise RuntimeError("approval challenge could not establish healthy evidence")

    action_id = f"local-outbox:{challenge_id}"
    action_payload = _action_payload(
        action_id=action_id,
        target={"route": "local-demo", "target_id": target_id},
    )
    center = ApprovalCenter(service, clock=service._now)
    proposal = center.submit(
        proposal_id=challenge_id,
        storage_id=str(ingested["storage_id"]),
        action_id=action_id,
        policy_id=_POLICY_ID,
        action_kind=_ACTION_KIND,
        payload_sha256=_sha256(action_payload),
        requested_by=_AGENT_ID,
        risk="high",
        expires_at=_iso(finished_at + timedelta(minutes=5)),
    )
    report = _pending_report(
        service,
        center,
        challenge_id,
        created=bool(proposal["created"]),
        database_path=database_path,
    )
    if report["action"]["executions"] != 0 or report["status"] != "pending":
        raise RuntimeError("approval challenge did not pause before execution")
    return report


def _attempt(
    name: str,
    label: str,
    result: Mapping[str, Any],
    payload_sha256: str,
) -> dict[str, Any]:
    status = str(result.get("status") or "blocked")
    reason = str(result.get("reason_code") or "unknown")
    callback_invoked = result.get("callback_invoked") is True
    return {
        "name": name,
        "label": label,
        "authorized": callback_invoked and status in {"succeeded", "failed"},
        "executed": callback_invoked and status == "succeeded",
        "status": status,
        "reason_codes": [reason],
        "payload_sha256": payload_sha256,
        "gate_decision_id": result.get("gate_decision_id"),
    }


def _resolution_blocked_report(
    center: ApprovalCenter,
    proposal_id: str,
    proposal_view: Mapping[str, Any],
    database_path: Path,
    timeline: list[dict[str, Any]],
    *,
    reason_code: str,
    payload_sha256: str,
) -> dict[str, Any]:
    final_outbox = _outbox_count(database_path)
    timeline.append(
        {
            "event": "resolution_evidence_blocked",
            "occurred_at": None,
            "reason_code": reason_code,
        }
    )
    return {
        "challenge_id": proposal_id,
        "proposal_id": proposal_id,
        "status": "needs_attention",
        "disposition": "approved",
        "credential_free": True,
        "external_calls": 0,
        "expectations_met": False,
        "proposal": dict(proposal_view),
        "action": {
            "kind": _ACTION_KIND,
            "first_executed": False,
            "execution_count": final_outbox,
            "final_outbox_rows": final_outbox,
            "bounded_local_only": True,
        },
        "attempts": [
            _attempt(
                "resolution_evidence",
                "Resolution evidence",
                {
                    "status": "blocked",
                    "reason_code": reason_code,
                    "callback_invoked": False,
                },
                payload_sha256,
            )
        ],
        "timeline": timeline,
        "audit": center.get(proposal_id),
    }


def resolve_approval_center_challenge(
    service: Any,
    proposal_id: str,
    *,
    disposition: str,
    artifact_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Apply the judge's decision and, when approved, prove exact one-use scope."""

    if disposition not in {"approved", "rejected"}:
        raise ValueError("disposition must be approved or rejected")
    directory = _existing_directory(_root(service, artifact_root), proposal_id)
    database_path = directory / _DATABASE_NAME
    if not database_path.is_file():
        raise FileNotFoundError("approval challenge destination not found")

    center = ApprovalCenter(service, clock=service._now)
    state = center.get(proposal_id)
    if not isinstance(state, Mapping):
        raise FileNotFoundError("approval challenge proposal not found")
    proposal = state["proposal"]
    if (
        proposal.get("proposal_id") != proposal_id
        or proposal.get("action_kind") != _ACTION_KIND
        or proposal.get("requested_by") != _AGENT_ID
        or proposal.get("storage_id") != f"{proposal_id}-truth"
        or proposal.get("action_id") != f"local-outbox:{proposal_id}"
        or proposal.get("policy_id") != _POLICY_ID
    ):
        raise ValueError("proposal is not an Approval Center challenge")

    decision = center.decide(
        proposal_id,
        outcome=disposition,
        decided_by="judge-mode:human-reviewer",
        reason_code=(
            "exact_action_approved"
            if disposition == "approved"
            else "operator_rejected"
        ),
    )
    proposal_view = _proposal_view(service, proposal)
    timeline = [
        {
            "event": "proposed",
            "occurred_at": proposal["created_at"],
            "reason_code": "awaiting_human_review",
        },
        {
            "event": disposition,
            "occurred_at": decision["decided_at"],
            "reason_code": decision["reason_code"],
        },
    ]
    if disposition == "rejected":
        return {
            "challenge_id": proposal_id,
            "proposal_id": proposal_id,
            "status": "rejected",
            "disposition": disposition,
            "credential_free": True,
            "external_calls": 0,
            "expectations_met": _outbox_count(database_path) == 0,
            "proposal": proposal_view,
            "action": {
                "kind": _ACTION_KIND,
                "first_executed": False,
                "execution_count": 0,
                "final_outbox_rows": _outbox_count(database_path),
                "bounded_local_only": True,
            },
            "attempts": [],
            "timeline": timeline,
            "audit": center.get(proposal_id),
        }

    exact_digest = str(proposal["payload_sha256"])
    expected_measurement, evidence_error = _resolution_evidence(
        service,
        database_path,
        proposal_id,
        proposal,
    )
    if evidence_error is not None or expected_measurement is None:
        return _resolution_blocked_report(
            center,
            proposal_id,
            proposal_view,
            database_path,
            timeline,
            reason_code=(
                evidence_error or "truth_evidence_not_current"
            ),
            payload_sha256=exact_digest,
        )

    changed_digest = (
        ("0" if exact_digest[0] != "0" else "1") + exact_digest[1:]
    )
    changed_callback_calls = 0

    def changed_callback() -> None:
        nonlocal changed_callback_calls
        changed_callback_calls += 1

    changed = center.execute(
        proposal_id,
        changed_callback,
        payload_sha256=changed_digest,
    )
    exact = center.execute(
        proposal_id,
        lambda: _insert_outbox(
            database_path,
            action_id=str(proposal["action_id"]),
            payload_sha256=exact_digest,
            expected_measurement=expected_measurement,
            created_at=service._now(),
        ),
        payload_sha256=exact_digest,
    )
    replay_callback_calls = 0

    def replay_callback() -> None:
        nonlocal replay_callback_calls
        replay_callback_calls += 1

    replay = center.execute(
        proposal_id,
        replay_callback,
        payload_sha256=exact_digest,
    )
    attempts = [
        _attempt(
            "changed_payload",
            "Changed request",
            changed,
            changed_digest,
        ),
        _attempt(
            "exact_payload",
            "Approved request",
            exact,
            exact_digest,
        ),
        _attempt(
            "replay",
            "Replay",
            replay,
            exact_digest,
        ),
    ]
    final_outbox = _outbox_count(database_path)
    expectations_met = (
        changed.get("status") == "blocked"
        and changed.get("reason_code") == "approval_scope_mismatch"
        and changed.get("callback_invoked") is False
        and changed_callback_calls == 0
        and exact.get("status") == "succeeded"
        and exact.get("callback_invoked") is True
        and replay.get("callback_invoked") is False
        and replay.get("reason_code") == "approval_already_consumed"
        and replay_callback_calls == 0
        and final_outbox == 1
    )
    final_state = center.get(proposal_id)
    timeline.extend(
        [
            {
                "event": "scope_mismatch_blocked",
                "occurred_at": decision["decided_at"],
                "reason_code": changed.get("reason_code"),
            },
            {
                "event": "executed_once",
                "occurred_at": (
                    final_state.get("completion", {}).get("completed_at")
                    if isinstance(final_state, Mapping)
                    else None
                ),
                "reason_code": exact.get("reason_code"),
            },
            {
                "event": "replay_blocked",
                "occurred_at": (
                    final_state.get("completion", {}).get("completed_at")
                    if isinstance(final_state, Mapping)
                    else None
                ),
                "reason_code": replay.get("reason_code"),
            },
        ]
    )
    return {
        "challenge_id": proposal_id,
        "proposal_id": proposal_id,
        "status": "completed" if expectations_met else "needs_attention",
        "disposition": disposition,
        "credential_free": True,
        "external_calls": 0,
        "expectations_met": bool(expectations_met),
        "proposal": proposal_view,
        "action": {
            "kind": _ACTION_KIND,
            "first_executed": (
                exact.get("callback_invoked") is True
                and exact.get("status") == "succeeded"
            ),
            "execution_count": final_outbox,
            "final_outbox_rows": final_outbox,
            "bounded_local_only": True,
        },
        "attempts": attempts,
        "timeline": timeline,
        "audit": final_state,
    }


__all__ = [
    "resolve_approval_center_challenge",
    "stage_approval_center_challenge",
]
