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

    def load_demo(self) -> dict[str, Any]:
        results = [self.ingest(receipt) for receipt in self.samples()]
        return {
            "loaded": len(results),
            "created": sum(1 for result in results if result["created"]),
            "verdicts": [result["evaluation"]["verdict"] for result in results],
        }
