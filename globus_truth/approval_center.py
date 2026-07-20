"""Durable human consent without treating approval as authorization.

An agent may submit only a privacy-safe action envelope.  A human decision is
immutably bound to that envelope's hash.  Execution still obtains a fresh
Action Gate decision, reads the exact audit record back, and atomically claims
the action while rechecking approval, expiry, gate binding, and current Truth.

The durable claim is the authorization linearization point: Truth changes
committed after it are ordered after that authorization.  The callback is
invoked only by the process that creates the unique claim, and its destination
must supply its own transaction or idempotency key.  Retries never invoke it
again.  If a process dies after the callback but before completion is audited,
the claim remains deliberately indeterminate instead of risking a duplicate
side effect.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .action_gate import POLICIES
from .storage import (
    ApprovalExecutionConflict,
    HumanApprovalConflict,
    TruthRepository,
)


RISKS = frozenset({"low", "medium", "high", "critical"})
OUTCOMES = frozenset({"approved", "rejected"})
_GATE_FIELDS = {
    "decision_id",
    "storage_id",
    "action_id",
    "policy_id",
    "observed_verdict",
    "authorized",
    "reason_codes",
    "decided_at",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ApprovalCenterError(RuntimeError):
    """Base class for safe Approval Center failures."""


class ApprovalNotFoundError(ApprovalCenterError):
    """The requested immutable proposal does not exist."""


class ApprovalAuditError(ApprovalCenterError):
    """A required audit write/read-back failed, so execution is blocked."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_timestamp(name: str, value: Any) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise ValueError(f"{name} must be an RFC 3339 timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class ApprovalCenter:
    """Coordinate immutable proposals, human decisions, and gated execution."""

    def __init__(
        self,
        service: Any,
        *,
        repository: TruthRepository | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(service, "authorize_action", None)):
            raise TypeError("service must provide authorize_action")
        if not callable(getattr(service, "get_action_decision", None)):
            raise TypeError("service must provide get_action_decision")
        selected = repository or getattr(service, "repository", None)
        if not isinstance(selected, TruthRepository):
            raise TypeError("Approval Center requires a TruthRepository")
        self.service = service
        self.repository = selected
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)

    def submit(
        self,
        *,
        proposal_id: str,
        storage_id: str,
        action_id: str,
        policy_id: str,
        action_kind: str,
        payload_sha256: str,
        requested_by: str,
        risk: str,
        expires_at: str,
    ) -> dict[str, Any]:
        """Persist one privacy-safe proposal, returning exact retries safely."""

        if policy_id not in POLICIES:
            raise ValueError("unsupported action policy")
        if risk not in RISKS:
            raise ValueError("unsupported action risk")
        now = self._now()
        expires = _parse_timestamp("expires_at", expires_at)
        if expires <= now:
            raise ValueError("action proposal expiry must be in the future")
        created_at = _iso(now)
        normalized_expiry = _iso(expires)
        fields = {
            "proposal_id": proposal_id,
            "storage_id": storage_id,
            "action_id": action_id,
            "policy_id": policy_id,
            "action_kind": action_kind,
            "payload_sha256": payload_sha256,
            "requested_by": requested_by,
            "risk": risk,
            "created_at": created_at,
            "expires_at": normalized_expiry,
        }
        proposal = {
            **fields,
            "proposal_sha256": _hash(fields),
        }
        stored, created = self.repository.save_action_proposal(proposal)
        return {**stored, "created": created}

    def decide(
        self,
        proposal_id: str,
        *,
        outcome: str,
        decided_by: str,
        reason_code: str,
    ) -> dict[str, Any]:
        """Record the proposal's one irreversible approved/rejected decision."""

        if outcome not in OUTCOMES:
            raise ValueError("human outcome must be approved or rejected")
        proposal = self.repository.get_action_proposal(proposal_id)
        if proposal is None:
            raise ApprovalNotFoundError("action proposal not found")
        decision = {
            "approval_id": f"approval-{uuid.uuid4().hex}",
            "proposal_id": proposal_id,
            "proposal_sha256": proposal["proposal_sha256"],
            "outcome": outcome,
            "decided_by": decided_by,
            "reason_code": reason_code,
            "decided_at": _iso(self._now()),
        }
        try:
            stored, created = self.repository.save_human_approval(decision)
        except HumanApprovalConflict:
            raise ApprovalCenterError(
                "proposal is expired or already has a different decision"
            ) from None
        return {**stored, "created": created}

    @staticmethod
    def _safe_gate_readback(
        returned: Any,
        persisted: Any,
    ) -> dict[str, Any] | None:
        if (
            not isinstance(returned, Mapping)
            or not isinstance(persisted, Mapping)
            or set(returned) != _GATE_FIELDS
            or set(persisted) != _GATE_FIELDS
        ):
            return None
        if any(persisted.get(field) != returned.get(field) for field in _GATE_FIELDS):
            return None
        try:
            return {
                "decision_id": str(persisted["decision_id"]),
                "storage_id": str(persisted["storage_id"]),
                "action_id": str(persisted["action_id"]),
                "policy_id": str(persisted["policy_id"]),
                "observed_verdict": str(persisted["observed_verdict"]),
                "authorized": persisted["authorized"] is True,
                "reason_codes": list(persisted["reason_codes"]),
                "decided_at": str(persisted["decided_at"]),
            }
        except (KeyError, TypeError, ValueError):
            return None

    def _result_for_claim(
        self,
        claim: Mapping[str, Any],
        *,
        callback_invoked: bool,
        replayed: bool = False,
    ) -> dict[str, Any]:
        completion = self.repository.get_approval_execution_completion(
            str(claim["claim_id"])
        )
        completion_status = (
            completion["outcome"] if completion is not None else "claimed"
        )
        return {
            "proposal_id": claim["proposal_id"],
            "action_id": claim["action_id"],
            "status": "already_consumed" if replayed else completion_status,
            "completion_status": completion_status,
            "reason_code": (
                "approval_already_consumed"
                if replayed
                else (
                    completion["reason_code"]
                    if completion is not None
                    else "completion_pending_or_indeterminate"
                )
            ),
            "callback_invoked": callback_invoked,
            "claim_id": claim["claim_id"],
            "approval_id": claim["approval_id"],
            "gate_decision_id": claim["gate_decision_id"],
            "completion_id": (
                completion["completion_id"] if completion is not None else None
            ),
        }

    def execute(
        self,
        proposal_id: str,
        callback: Callable[[], Any],
        *,
        payload_sha256: str,
    ) -> dict[str, Any]:
        """Claim once, invoke once, and persist a payload-free completion."""

        if not callable(callback):
            raise TypeError("callback must be callable")

        proposal = self.repository.get_action_proposal(proposal_id)
        if proposal is None:
            raise ApprovalNotFoundError("action proposal not found")
        if (
            not isinstance(payload_sha256, str)
            or not _SHA256_RE.fullmatch(payload_sha256)
            or payload_sha256 != proposal["payload_sha256"]
        ):
            return self._blocked(proposal, "approval_scope_mismatch")

        existing_claim = self.repository.get_approval_execution_claim(proposal_id)
        if existing_claim is not None:
            return self._result_for_claim(
                existing_claim,
                callback_invoked=False,
                replayed=True,
            )

        approval = self.repository.get_human_approval(proposal_id)
        now = self._now()
        if approval is None:
            return self._blocked(proposal, "human_approval_missing")
        if approval["outcome"] != "approved":
            return self._blocked(proposal, "human_rejected")
        if now < _parse_timestamp("created_at", proposal["created_at"]):
            return self._blocked(proposal, "clock_before_proposal")
        if now > _parse_timestamp("expires_at", proposal["expires_at"]):
            return self._blocked(proposal, "proposal_expired")

        try:
            returned_gate = self.service.authorize_action(
                proposal["storage_id"],
                proposal["action_id"],
                policy_id=proposal["policy_id"],
            )
            decision_id = (
                returned_gate.get("decision_id")
                if isinstance(returned_gate, Mapping)
                else None
            )
            persisted_gate = (
                self.service.get_action_decision(decision_id)
                if isinstance(decision_id, str)
                else None
            )
        except Exception:
            raise ApprovalAuditError(
                "Action blocked because its gate decision could not be audited."
            ) from None
        gate = self._safe_gate_readback(returned_gate, persisted_gate)
        if gate is None:
            raise ApprovalAuditError(
                "Action blocked because its gate audit read-back did not match."
            )
        if (
            gate["storage_id"] != proposal["storage_id"]
            or gate["action_id"] != proposal["action_id"]
            or gate["policy_id"] != proposal["policy_id"]
        ):
            raise ApprovalAuditError(
                "Action blocked because its gate binding did not match."
            )
        if not gate["authorized"]:
            return self._blocked(
                proposal,
                "truth_gate_blocked",
                gate_decision=gate,
            )

        claimed_at = _iso(self._now())
        claim = {
            "claim_id": f"claim-{uuid.uuid4().hex}",
            "proposal_id": proposal_id,
            "approval_id": approval["approval_id"],
            "action_id": proposal["action_id"],
            "gate_decision_id": gate["decision_id"],
            "gate_decision_sha256": _hash(gate),
            "claimed_at": claimed_at,
        }
        try:
            stored_claim, created = self.repository.claim_approved_execution(
                claim,
                gate_decision=gate,
            )
        except ApprovalExecutionConflict:
            return self._blocked(
                proposal,
                "claim_preconditions_failed",
                gate_decision=gate,
            )
        except Exception:
            raise ApprovalAuditError(
                "Action blocked because its execution claim could not be audited."
            ) from None
        if not created:
            return self._result_for_claim(
                stored_claim,
                callback_invoked=False,
                replayed=True,
            )

        # The committed unique claim above linearizes authorization.  It
        # deliberately cannot make an arbitrary external callback atomic;
        # the callback owns destination transactionality and idempotency.
        outcome = "succeeded"
        reason_code = "callback_succeeded"
        try:
            callback()
        except Exception:
            outcome = "failed"
            reason_code = "callback_failed"

        completion = {
            "completion_id": f"completion-{uuid.uuid4().hex}",
            "claim_id": stored_claim["claim_id"],
            "outcome": outcome,
            "reason_code": reason_code,
            "completed_at": _iso(self._now()),
        }
        try:
            self.repository.save_approval_execution_completion(completion)
        except Exception:
            raise ApprovalAuditError(
                "Action was claimed, but its completion could not be audited."
            ) from None
        return self._result_for_claim(
            stored_claim,
            callback_invoked=True,
        )

    @staticmethod
    def _blocked(
        proposal: Mapping[str, Any],
        reason_code: str,
        *,
        gate_decision: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "proposal_id": proposal["proposal_id"],
            "action_id": proposal["action_id"],
            "status": "blocked",
            "completion_status": None,
            "reason_code": reason_code,
            "callback_invoked": False,
            "claim_id": None,
            "approval_id": None,
            "gate_decision_id": (
                gate_decision.get("decision_id")
                if gate_decision is not None
                else None
            ),
            "completion_id": None,
        }

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        """Return one privacy-safe proposal with derived durable state."""

        proposal = self.repository.get_action_proposal(proposal_id)
        if proposal is None:
            return None
        approval = self.repository.get_human_approval(proposal_id)
        claim = self.repository.get_approval_execution_claim(proposal_id)
        completion = (
            self.repository.get_approval_execution_completion(claim["claim_id"])
            if claim is not None
            else None
        )
        if completion is not None:
            state = completion["outcome"]
        elif claim is not None:
            state = "claimed"
        elif approval is not None and approval["outcome"] == "rejected":
            state = "rejected"
        elif self._now() > _parse_timestamp("expires_at", proposal["expires_at"]):
            state = "expired"
        elif approval is not None:
            state = "approved"
        else:
            state = "pending"
        return {
            "proposal": proposal,
            "approval": approval,
            "claim": claim,
            "completion": completion,
            "state": state,
        }

    def list(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Return recent proposals and their derived privacy-safe state."""

        return [
            item
            for proposal in self.repository.list_action_proposals(
                limit=limit,
                offset=offset,
            )
            if (item := self.get(proposal["proposal_id"])) is not None
        ]


__all__ = [
    "ApprovalAuditError",
    "ApprovalCenter",
    "ApprovalCenterError",
    "ApprovalNotFoundError",
    "OUTCOMES",
    "RISKS",
]
