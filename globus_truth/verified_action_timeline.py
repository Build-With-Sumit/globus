"""Privacy-safe lifecycle views derived from immutable verified-action state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .storage import TruthRepository


SCHEMA_VERSION = "1.0"
EVENT_TYPES = (
    "proposed",
    "human_decision",
    "truth_gate",
    "execution_claimed",
    "destination_verification",
    "completed",
)


def _event(
    proposal_id: str,
    sequence: int,
    event_type: str,
    *,
    outcome: str,
    occurred_at: str | None = None,
    source_id: str | None = None,
    reason_codes: list[str] | None = None,
    evidence_sha256: str | None = None,
    observed_verdict: str | None = None,
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "event_id": f"{proposal_id}:{sequence}",
        "event_type": event_type,
        "outcome": outcome,
        "occurred_at": occurred_at,
        "source_id": source_id,
        "reason_codes": list(reason_codes or []),
        "evidence_sha256": evidence_sha256,
        "observed_verdict": observed_verdict,
    }


def build_verified_action_timeline(
    repository: TruthRepository,
    proposal_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return six fixed lifecycle stages from one repository read snapshot."""

    if not isinstance(repository, TruthRepository):
        raise TypeError("timeline requires a TruthRepository")
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("timeline clock must be timezone-aware")
    snapshot = repository.get_verified_action_timeline_snapshot(proposal_id)
    if snapshot is None:
        return None

    proposal = snapshot["proposal"]
    approval = snapshot["approval"]
    gate = snapshot["gate"]
    claim = snapshot["claim"]
    verification = snapshot["verification"]
    completion = snapshot["completion"]
    rejected = (
        isinstance(approval, Mapping)
        and approval.get("outcome") == "rejected"
    )
    expires_at = datetime.fromisoformat(
        str(proposal["expires_at"]).removesuffix("Z") + "+00:00"
    )
    expired = (
        not rejected
        and not isinstance(claim, Mapping)
        and now.astimezone(timezone.utc) > expires_at
    )

    events = [
        _event(
            proposal_id,
            1,
            "proposed",
            outcome="recorded",
            occurred_at=proposal["created_at"],
            source_id=proposal["proposal_id"],
            reason_codes=["awaiting_human_review"],
            evidence_sha256=proposal["payload_sha256"],
        )
    ]
    if isinstance(approval, Mapping):
        events.append(
            _event(
                proposal_id,
                2,
                "human_decision",
                outcome=str(approval["outcome"]),
                occurred_at=str(approval["decided_at"]),
                source_id=str(approval["approval_id"]),
                reason_codes=[str(approval["reason_code"])],
                evidence_sha256=str(approval["proposal_sha256"]),
            )
        )
    else:
        events.append(
            _event(
                proposal_id,
                2,
                "human_decision",
                outcome="not_applicable" if expired else "pending",
                reason_codes=(
                    ["proposal_expired"]
                    if expired
                    else ["awaiting_human_review"]
                ),
            )
        )

    if isinstance(gate, Mapping):
        events.append(
            _event(
                proposal_id,
                3,
                "truth_gate",
                outcome=(
                    "authorized"
                    if gate.get("authorized") is True
                    else "blocked"
                ),
                occurred_at=str(gate["decided_at"]),
                source_id=str(gate["decision_id"]),
                reason_codes=[
                    str(code) for code in gate.get("reason_codes", [])
                ],
                evidence_sha256=(
                    str(claim["gate_decision_sha256"])
                    if isinstance(claim, Mapping)
                    else None
                ),
                observed_verdict=str(gate["observed_verdict"]),
            )
        )
    else:
        events.append(
            _event(
                proposal_id,
                3,
                "truth_gate",
                outcome="not_applicable" if rejected or expired else "pending",
                reason_codes=(
                    ["human_rejected"]
                    if rejected
                    else (
                        ["proposal_expired"]
                        if expired
                        else ["fresh_truth_not_checked"]
                    )
                ),
            )
        )

    if isinstance(claim, Mapping):
        events.append(
            _event(
                proposal_id,
                4,
                "execution_claimed",
                outcome="claimed",
                occurred_at=str(claim["claimed_at"]),
                source_id=str(claim["claim_id"]),
                reason_codes=["authorization_linearized"],
                evidence_sha256=str(claim["gate_decision_sha256"]),
            )
        )
    else:
        events.append(
            _event(
                proposal_id,
                4,
                "execution_claimed",
                outcome="not_applicable" if rejected or expired else "pending",
                reason_codes=(
                    ["human_rejected"]
                    if rejected
                    else (
                        ["proposal_expired"]
                        if expired
                        else ["execution_not_claimed"]
                    )
                ),
            )
        )

    if isinstance(verification, Mapping):
        events.append(
            _event(
                proposal_id,
                5,
                "destination_verification",
                outcome=(
                    "verified"
                    if verification.get("verified") is True
                    else "failed"
                ),
                occurred_at=str(verification["verified_at"]),
                source_id=str(verification["verification_id"]),
                reason_codes=[str(verification["reason_code"])],
                evidence_sha256=str(verification["observation_sha256"]),
            )
        )
    else:
        events.append(
            _event(
                proposal_id,
                5,
                "destination_verification",
                outcome="not_applicable" if rejected or expired else "pending",
                reason_codes=(
                    ["human_rejected"]
                    if rejected
                    else (
                        ["proposal_expired"]
                        if expired
                        else ["destination_not_verified"]
                    )
                ),
            )
        )

    if isinstance(completion, Mapping):
        events.append(
            _event(
                proposal_id,
                6,
                "completed",
                outcome=str(completion["outcome"]),
                occurred_at=str(completion["completed_at"]),
                source_id=str(completion["completion_id"]),
                reason_codes=[str(completion["reason_code"])],
            )
        )
    else:
        events.append(
            _event(
                proposal_id,
                6,
                "completed",
                outcome="not_applicable" if rejected or expired else "pending",
                reason_codes=(
                    ["human_rejected"]
                    if rejected
                    else (
                        ["proposal_expired"]
                        if expired
                        else (
                            ["completion_pending_or_indeterminate"]
                            if isinstance(claim, Mapping)
                            else ["execution_not_completed"]
                        )
                    )
                ),
            )
        )

    if isinstance(completion, Mapping):
        state = str(completion["outcome"])
    elif isinstance(claim, Mapping):
        state = "indeterminate"
    elif rejected:
        state = "rejected"
    elif expired:
        state = "expired"
    elif isinstance(gate, Mapping) and gate.get("authorized") is False:
        state = "blocked"
    elif isinstance(approval, Mapping):
        state = "approved"
    else:
        state = "pending"

    present = {
        "proposed": True,
        "human_decision": isinstance(approval, Mapping),
        "truth_gate": isinstance(gate, Mapping),
        "execution_claimed": isinstance(claim, Mapping),
        "destination_verification": isinstance(verification, Mapping),
        "completed": isinstance(completion, Mapping),
    }
    missing_stages = (
        []
        if rejected or expired
        else [name for name in EVENT_TYPES if not present[name]]
    )
    verification_matches_completion = (
        isinstance(verification, Mapping)
        and isinstance(completion, Mapping)
        and (
            (
                completion.get("outcome") == "succeeded"
                and verification.get("verified") is True
            )
            or (
                completion.get("outcome") == "failed"
                and verification.get("verified") is False
            )
        )
    )
    terminal = rejected or expired or isinstance(completion, Mapping)
    integrity_complete = rejected or expired or verification_matches_completion

    return {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "action_id": proposal["action_id"],
        "action_kind": proposal["action_kind"],
        "state": state,
        "terminal": bool(terminal),
        "integrity_complete": bool(integrity_complete),
        "legacy_unverified": bool(
            isinstance(completion, Mapping)
            and not isinstance(verification, Mapping)
        ),
        "missing_stages": missing_stages,
        "events": events,
    }


__all__ = [
    "EVENT_TYPES",
    "SCHEMA_VERSION",
    "build_verified_action_timeline",
]
