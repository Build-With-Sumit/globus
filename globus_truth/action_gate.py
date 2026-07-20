"""Fail-closed authorization from persisted Truth Layer verdicts.

The gate accepts only stable identifiers. It obtains the current verdict from a
``TruthService.get_run``-compatible seam, applies a named policy, and writes the
privacy-safe decision to immutable storage before returning it.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping


POLICIES: dict[str, frozenset[str]] = {
    "healthy_only": frozenset({"healthy"}),
    "trusted_completion": frozenset({"healthy", "verified_no_work"}),
}
_VERDICTS = {
    "healthy",
    "verified_no_work",
    "degraded_contradictory",
    "failed",
    "stale",
}
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DECISION_FIELDS = {
    "decision_id",
    "storage_id",
    "action_id",
    "policy_id",
    "observed_verdict",
    "authorized",
    "reason_codes",
    "decided_at",
}


class ActionGateAuditError(RuntimeError):
    """The gate could not durably audit a decision, so no action is authorized."""


def _iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _validate_id(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"{name} must be a safe 1-128 character identifier")
    return value


class ActionGate:
    """Authorize controlled actions from persisted, freshly read verdicts."""

    def __init__(
        self,
        service_or_get_run: Any,
        repository: Any | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if callable(service_or_get_run) and not hasattr(service_or_get_run, "get_run"):
            get_run = service_or_get_run
        else:
            get_run = getattr(service_or_get_run, "get_run", None)
            if repository is None:
                repository = getattr(service_or_get_run, "repository", None)
        if not callable(get_run):
            raise TypeError("service_or_get_run must provide a callable get_run")
        if repository is None or not callable(
            getattr(repository, "save_action_decision", None)
        ):
            raise TypeError("repository must provide save_action_decision")
        self._get_run: Callable[[str], Any] = get_run
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _read_verdict(run: Any, storage_id: str) -> tuple[str, list[str]]:
        if run is None:
            return "missing", ["truth_record_missing"]
        if not isinstance(run, Mapping):
            return "malformed", ["truth_record_malformed"]
        try:
            returned_storage_id = run.get("storage_id")
            evaluation = run.get("evaluation")
        except Exception:
            return "malformed", ["truth_record_malformed"]
        if returned_storage_id != storage_id:
            return "malformed", ["truth_record_malformed"]
        if not isinstance(evaluation, Mapping):
            return "malformed", ["truth_record_malformed"]
        try:
            verdict = evaluation.get("verdict")
            valid = evaluation.get("valid")
        except Exception:
            return "malformed", ["truth_record_malformed"]
        if (
            verdict not in _VERDICTS
            or not isinstance(valid, bool)
            or valid != (verdict in {"healthy", "verified_no_work"})
        ):
            return "malformed", ["truth_record_malformed"]
        return verdict, []

    @staticmethod
    def _policy_result(
        verdict: str,
        policy_id: str,
    ) -> tuple[bool, list[str]]:
        if verdict in POLICIES[policy_id]:
            return True, ["policy_satisfied"]
        if verdict == "stale":
            return False, ["verdict_stale"]
        if verdict == "failed":
            return False, ["verdict_failed"]
        if verdict == "degraded_contradictory":
            return False, ["verdict_contradictory"]
        if verdict == "verified_no_work" and policy_id == "healthy_only":
            return False, ["policy_requires_healthy"]
        return False, ["verdict_not_trusted"]

    def decide(
        self,
        *,
        storage_id: str,
        action_id: str,
        policy_id: str = "healthy_only",
        decision_id: str | None = None,
    ) -> dict[str, Any]:
        """Read the current persisted verdict, audit, then return the decision.

        There is intentionally no caller-supplied verdict parameter. Missing,
        malformed, unavailable, failed, contradictory, and stale Truth records
        all produce a blocked decision.
        """

        storage_id = _validate_id("storage_id", storage_id)
        action_id = _validate_id("action_id", action_id)
        if policy_id not in POLICIES:
            raise ValueError("unsupported action policy")
        if decision_id is None:
            decision_id = f"gate-{uuid.uuid4().hex}"
        decision_id = _validate_id("decision_id", decision_id)

        try:
            run = self._get_run(storage_id)
        except Exception:
            observed_verdict = "unavailable"
            authorized = False
            reason_codes = ["truth_lookup_failed"]
        else:
            observed_verdict, reason_codes = self._read_verdict(run, storage_id)
            if reason_codes:
                authorized = False
            else:
                authorized, reason_codes = self._policy_result(
                    observed_verdict,
                    policy_id,
                )

        try:
            now = self._clock()
            if not isinstance(now, datetime) or now.tzinfo is None:
                raise ValueError("clock must return a timezone-aware datetime")
            decision = {
                "decision_id": decision_id,
                "storage_id": storage_id,
                "action_id": action_id,
                "policy_id": policy_id,
                "observed_verdict": observed_verdict,
                "authorized": authorized,
                "reason_codes": reason_codes,
                "decided_at": _iso(now),
            }
            if set(decision) != _DECISION_FIELDS:
                raise AssertionError("unsafe action decision shape")
            self._repository.save_action_decision(decision)
        except Exception:
            raise ActionGateAuditError(
                "Action blocked because its authorization decision could not be audited."
            ) from None
        return decision

    authorize = decide


__all__ = [
    "ActionGate",
    "ActionGateAuditError",
    "POLICIES",
]
