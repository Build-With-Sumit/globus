"""Application service shared by the CLI and HTTP adapter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .evaluator import evaluate_receipt
from .fixtures import demo_receipts
from .storage import TruthRepository


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sortable_iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


class TruthService:
    def __init__(
        self,
        repository: TruthRepository,
        *,
        stale_after: timedelta = timedelta(hours=24),
        clock: Any | None = None,
    ) -> None:
        self.repository = repository
        self.stale_after = stale_after
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.repository.configure_stale_reevaluation(
            clock=lambda: _sortable_iso(self._now()),
            reevaluator=self._reevaluate_stored,
        )

    def _now(self) -> datetime:
        return self._clock().astimezone(timezone.utc)

    def _fresh_until(
        self,
        receipt: Mapping[str, Any],
        evaluation: Mapping[str, Any],
    ) -> str | None:
        if evaluation.get("verdict") not in {"healthy", "verified_no_work"}:
            return None
        finished_at = _parse_iso(receipt.get("finished_at"))
        heartbeat_at = _parse_iso(receipt.get("heartbeat_at"))
        if finished_at is None or heartbeat_at is None:
            return None
        try:
            return _sortable_iso(max(finished_at, heartbeat_at) + self.stale_after)
        except OverflowError:
            return None

    def _reevaluate_stored(
        self,
        receipt: Mapping[str, Any],
        evaluated_at: str,
    ) -> tuple[dict[str, Any], str | None]:
        now = _parse_iso(evaluated_at)
        if now is None:
            raise ValueError("invalid reevaluation timestamp")
        evaluation = evaluate_receipt(
            receipt,
            now=now,
            stale_after=self.stale_after,
        ).to_dict()
        return evaluation, self._fresh_until(receipt, evaluation)

    def ingest(self, receipt: Mapping[str, Any] | Any) -> dict[str, Any]:
        now = self._now()
        evaluation = evaluate_receipt(
            receipt,
            now=now,
            stale_after=self.stale_after,
        )
        if not isinstance(receipt, Mapping):
            # Non-object JSON can be evaluated but cannot be represented as a
            # receipt row with stable identity.
            raise ValueError("receipt must be a JSON object")
        evaluation_data = evaluation.to_dict()
        storage_id, created = self.repository.save(
            receipt,
            evaluation_data,
            received_at=_iso(now),
            fresh_until=self._fresh_until(receipt, evaluation_data),
        )
        return {
            "storage_id": storage_id,
            "created": created,
            "evaluation": evaluation_data,
        }

    def ingest_many(
        self,
        receipts: list[Mapping[str, Any] | Any],
    ) -> list[dict[str, Any]]:
        """Evaluate and persist several receipts atomically."""
        if not isinstance(receipts, list) or not receipts:
            raise ValueError("receipts must be a non-empty list")
        now = self._now()
        received_at = _iso(now)
        prepared: list[dict[str, Any]] = []
        evaluations: list[dict[str, Any]] = []
        for receipt in receipts:
            evaluation = evaluate_receipt(
                receipt,
                now=now,
                stale_after=self.stale_after,
            )
            if not isinstance(receipt, Mapping):
                raise ValueError("receipt must be a JSON object")
            evaluation_data = evaluation.to_dict()
            evaluations.append(evaluation_data)
            prepared.append(
                {
                    "receipt": receipt,
                    "evaluation": evaluation_data,
                    "received_at": received_at,
                    "fresh_until": self._fresh_until(
                        receipt,
                        evaluation_data,
                    ),
                }
            )
        saved = self.repository.save_many(prepared)
        return [
            {
                "storage_id": storage_id,
                "created": created,
                "evaluation": evaluation,
            }
            for (storage_id, created), evaluation in zip(saved, evaluations)
        ]

    def samples(self) -> list[dict[str, Any]]:
        return demo_receipts(self._now())

    def list_runs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self.repository.list_runs(limit=limit, offset=offset)

    def get_run(self, storage_id: str) -> dict[str, Any] | None:
        return self.repository.get_run(storage_id)

    def summary(self) -> dict[str, Any]:
        return self.repository.summary()

    def verdict_history(self, storage_id: str) -> list[dict[str, Any]]:
        return self.repository.verdict_history(storage_id)

    def run_judge_challenge(self, *, artifact_root: Any | None = None) -> dict[str, Any]:
        """Run the offline, real-byte artifact tamper challenge."""
        from .judge_mode import run_artifact_tamper_challenge

        return run_artifact_tamper_challenge(
            self,
            artifact_root=artifact_root,
        )

    def authorize_action(
        self,
        storage_id: str,
        action_id: str,
        *,
        policy_id: str = "healthy_only",
        decision_id: str | None = None,
    ) -> dict[str, Any]:
        """Audit a fail-closed action decision from a persisted current verdict."""
        from .action_gate import ActionGate

        return ActionGate(self, clock=self._now).authorize(
            storage_id=storage_id,
            action_id=action_id,
            policy_id=policy_id,
            decision_id=decision_id,
        )

    def get_action_decision(self, decision_id: str) -> dict[str, Any] | None:
        """Return one privacy-safe, immutable Action Gate decision."""
        return self.repository.get_action_decision(decision_id)

    def list_action_decisions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent privacy-safe Action Gate decisions."""
        return self.repository.list_action_decisions(limit=limit)

    def approval_center(self) -> Any:
        """Return the durable human-consent coordinator for this service."""
        from .approval_center import ApprovalCenter

        return ApprovalCenter(self, clock=self._now)

    def submit_action_proposal(
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
        """Persist a privacy-safe, exact-action proposal for human review."""
        return self.approval_center().submit(
            proposal_id=proposal_id,
            storage_id=storage_id,
            action_id=action_id,
            policy_id=policy_id,
            action_kind=action_kind,
            payload_sha256=payload_sha256,
            requested_by=requested_by,
            risk=risk,
            expires_at=expires_at,
        )

    def decide_action_proposal(
        self,
        proposal_id: str,
        *,
        outcome: str,
        decided_by: str,
        reason_code: str,
    ) -> dict[str, Any]:
        """Record one irreversible human approval or rejection."""
        return self.approval_center().decide(
            proposal_id,
            outcome=outcome,
            decided_by=decided_by,
            reason_code=reason_code,
        )

    def get_approval_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        """Return one proposal with its derived durable state."""
        return self.approval_center().get(proposal_id)

    def list_approval_proposals(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return recent proposals without action payloads."""
        return self.approval_center().list(limit=limit, offset=offset)

    def stage_approval_challenge(
        self,
        *,
        artifact_root: Any | None = None,
    ) -> dict[str, Any]:
        """Pause one generated high-risk action for a real human decision."""
        from .approval_challenge import stage_approval_center_challenge

        return stage_approval_center_challenge(
            self,
            artifact_root=artifact_root,
        )

    def resolve_approval_challenge(
        self,
        proposal_id: str,
        *,
        disposition: str,
        artifact_root: Any | None = None,
    ) -> dict[str, Any]:
        """Resolve and prove the generated exact-action challenge."""
        from .approval_challenge import resolve_approval_center_challenge

        return resolve_approval_center_challenge(
            self,
            proposal_id,
            disposition=disposition,
            artifact_root=artifact_root,
        )

    def verified_action_manifests(self) -> dict[str, Any]:
        """Return the two generated-local Verified Action SDK contracts."""
        from .verified_action_lab import verified_action_manifests

        return verified_action_manifests()

    def stage_verified_action_lab(
        self,
        *,
        adapter_id: str,
        artifact_root: Any | None = None,
    ) -> dict[str, Any]:
        """Stage one provider-shaped local action for exact human review."""
        from .verified_action_lab import stage_verified_action_lab

        return stage_verified_action_lab(
            self,
            adapter_id=adapter_id,
            artifact_root=artifact_root,
        )

    def resolve_verified_action_lab(
        self,
        proposal_id: str,
        *,
        disposition: str,
        artifact_root: Any | None = None,
    ) -> dict[str, Any]:
        """Resolve a generated Verified Action SDK request without a payload."""
        from .verified_action_lab import resolve_verified_action_lab

        return resolve_verified_action_lab(
            self,
            proposal_id,
            disposition=disposition,
            artifact_root=artifact_root,
        )

    def get_verified_action_timeline(
        self,
        proposal_id: str,
    ) -> dict[str, Any] | None:
        """Return a fixed, privacy-safe lifecycle derived from one snapshot."""
        from .verified_action_timeline import build_verified_action_timeline

        return build_verified_action_timeline(
            self.repository,
            proposal_id,
            now=self._now(),
        )

    def run_outcome_gate_challenge(
        self,
        *,
        artifact_root: Any | None = None,
    ) -> dict[str, Any]:
        """Run the credential-free business-outcome verification workflow."""
        from .outcome_challenge import run_outcome_gate_challenge

        return run_outcome_gate_challenge(
            self,
            artifact_root=artifact_root,
        )

    def platform_capabilities(self) -> dict[str, Any]:
        """Return the validated, credential-free Mission Control inventory."""
        from .platform_registry import (
            get_platform_graph,
            get_platform_summary,
            list_capabilities,
        )

        return {
            "summary": get_platform_summary(),
            "capabilities": list_capabilities(include_planned=True),
            "graph": get_platform_graph(),
        }

    def load_demo(self) -> dict[str, Any]:
        results = [self.ingest(receipt) for receipt in self.samples()]
        return {
            "loaded": len(results),
            "created": sum(1 for result in results if result["created"]),
            "verdicts": [result["evaluation"]["verdict"] for result in results],
        }
