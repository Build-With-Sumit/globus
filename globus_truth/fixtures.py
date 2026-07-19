"""De-identified demo receipts covering every verdict."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def demo_receipts(now: datetime | None = None) -> list[dict[str, Any]]:
    """Return deterministic scenarios relative to an explicit clock."""

    anchor = (now or datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)).astimezone(
        timezone.utc
    )
    fixture_id = anchor.strftime("%Y%m%dT%H%M%S%fZ")
    finish = anchor - timedelta(minutes=3)
    start = finish - timedelta(minutes=2)
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "receipt_id": f"demo-healthy-{fixture_id}",
        "agent_id": "demo-indexer",
        "run_id": f"run-healthy-{fixture_id}",
        "declared_status": "success",
        "started_at": _iso(start),
        "finished_at": _iso(finish),
        "heartbeat_at": _iso(finish),
        "input": {"items_seen": 12, "items_eligible": 4},
        "output": {"items_processed": 4, "items_changed": 4},
        "summary": "Indexed four eligible records and verified the resulting artifact.",
        "evidence": [
            {
                "kind": "checksum",
                "ref": "artifact:demo-index-v1",
                "observed_at": _iso(finish),
                "detail": "Output manifest contains four records.",
                "sha256": "a" * 64,
            }
        ],
        "checks": [
            {
                "name": "manifest_count",
                "passed": True,
                "detail": "Manifest count equals items_changed.",
            }
        ],
        "metadata": {"environment": "demo"},
    }

    no_work = deepcopy(base)
    no_work.update(
        {
            "receipt_id": f"demo-no-work-{fixture_id}",
            "agent_id": "demo-followup",
            "run_id": f"run-no-work-{fixture_id}",
            "declared_status": "no_work",
            "input": {"items_seen": 8, "items_eligible": 0},
            "output": {"items_processed": 0, "items_changed": 0},
            "summary": "Queue inspected successfully; no records met the follow-up rule.",
            "evidence": [],
            "checks": [
                {
                    "name": "queue_read",
                    "passed": True,
                    "detail": "The source queue was read without error.",
                }
            ],
            "no_work": {
                "reason_code": "no_eligible_records",
                "reason": "Eight records were inspected and none met the deterministic age rule.",
            },
        }
    )

    degraded = deepcopy(base)
    degraded.update(
        {
            "receipt_id": f"demo-degraded-{fixture_id}",
            "agent_id": "demo-digest",
            "run_id": f"run-degraded-{fixture_id}",
            "summary": "No source material was included. Please provide the source material.",
            "input": {"items_seen": 0, "items_eligible": 0},
            "output": {"items_processed": 0, "items_changed": 0},
            "evidence": [],
            "checks": [],
        }
    )

    failed = deepcopy(base)
    failed.update(
        {
            "receipt_id": f"demo-failed-{fixture_id}",
            "agent_id": "demo-exporter",
            "run_id": f"run-failed-{fixture_id}",
            "declared_status": "failed",
            "input": {"items_seen": 3, "items_eligible": 3},
            "output": {"items_processed": 1, "items_changed": 0},
            "summary": "Export stopped before any destination write was acknowledged.",
            "evidence": [],
            "checks": [
                {
                    "name": "destination_ack",
                    "passed": False,
                    "detail": "No acknowledgement arrived before the deadline.",
                }
            ],
            "error": {
                "code": "destination_timeout",
                "message": "The demo destination did not acknowledge the write.",
            },
        }
    )

    stale = deepcopy(base)
    stale_finish = anchor - timedelta(days=3)
    stale.update(
        {
            "receipt_id": f"demo-stale-{fixture_id}",
            "agent_id": "demo-heartbeat",
            "run_id": f"run-stale-{fixture_id}",
            "started_at": _iso(stale_finish - timedelta(minutes=2)),
            "finished_at": _iso(stale_finish),
            "heartbeat_at": _iso(stale_finish),
            "evidence": [
                {
                    "kind": "metric",
                    "ref": "metric:demo-heartbeat",
                    "observed_at": _iso(stale_finish),
                    "detail": "Heartbeat was valid when emitted.",
                }
            ],
            "summary": "The last measured run completed, but its heartbeat is now old.",
        }
    )
    return [base, no_work, degraded, failed, stale]
