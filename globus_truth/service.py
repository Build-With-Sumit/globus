"""Application service shared by the CLI and HTTP adapter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .evaluator import evaluate_receipt
from .fixtures import demo_receipts
from .storage import TruthRepository


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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

    def ingest(self, receipt: Mapping[str, Any] | Any) -> dict[str, Any]:
        now = self._clock().astimezone(timezone.utc)
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
        )
        return {
            "storage_id": storage_id,
            "created": created,
            "evaluation": evaluation_data,
        }

    def samples(self) -> list[dict[str, Any]]:
        return demo_receipts(self._clock().astimezone(timezone.utc))

    def load_demo(self) -> dict[str, Any]:
        results = [self.ingest(receipt) for receipt in self.samples()]
        return {
            "loaded": len(results),
            "created": sum(1 for result in results if result["created"]),
            "verdicts": [result["evaluation"]["verdict"] for result in results],
        }
