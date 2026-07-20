"""Truth Layer adapter for the OSS-native Globus agent runner.

The runner owns the real lifecycle, while this module owns the translation from
run facts to the strict v1 receipt contract. Receipts identify a member with an
install-keyed HMAC pseudonym; raw email addresses and model output are never
copied into the Truth database.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from .service import TruthService
from .storage import TruthRepository


MIN_MEANINGFUL_REPLY_CHARS = 20

# Keep this aligned with the evaluator's refusal/error-as-output policy, but run
# it against the actual model reply.  The receipt stores only the boolean check,
# not the potentially sensitive reply text.
_ERROR_PROSE_PATTERNS = (
    re.compile(r"\bas an ai\b", re.I),
    re.compile(
        r"\bi (?:cannot|can't|am unable to) "
        r"(?:comply|complete|perform|access)\b",
        re.I,
    ),
    re.compile(
        r"\bplease (?:provide|share|upload) "
        r"(?:the )?(?:input|data|source|material)\b",
        re.I,
    ),
    re.compile(
        r"\bno (?:input|source|material|data) "
        r"(?:was|were|has been) (?:provided|included)\b",
        re.I,
    ),
    re.compile(r"^\s*(?:error|exception|traceback)\s*[:\n]", re.I),
    re.compile(
        r"\b(?:request failed|timed out|rate limit(?:ed)?|quota exceeded)\b",
        re.I,
    ),
)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _scope_secret() -> bytes:
    configured = (
        os.environ.get("GLOBUS_TRUTH_SCOPE_SECRET")
        or os.environ.get("SESSION_SECRET")
        or ""
    ).strip()
    if (
        len(configured) < 32
        or configured == "replace-with-32-byte-hex"
    ):
        raise RuntimeError(
            "GLOBUS_TRUTH_SCOPE_SECRET or SESSION_SECRET must contain "
            "at least 32 characters"
        )
    return configured.encode("utf-8")


def member_scope_hash(email: str) -> str:
    """Install-scoped, non-enumerable member pseudonym for receipt identities."""
    normalized = (email or "").strip().lower()
    if not normalized:
        raise ValueError("member email is required for a scoped agent receipt")
    return hmac.new(
        _scope_secret(),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]


def _safe_agent_name(agent_name: str) -> str:
    safe = re.sub(r"[^a-z0-9._-]+", "-", (agent_name or "").strip().lower())
    safe = safe.strip("-._")[:80]
    if not safe:
        raise ValueError("agent name is required for a run receipt")
    return safe


def member_agent_id(email: str, agent_name: str) -> str:
    """Receipt agent_id shared by all runs of one member-scoped agent."""
    return f"member-{member_scope_hash(email)}:{_safe_agent_name(agent_name)}"


def truth_database_path(
    database: str | os.PathLike[str] | None = None,
    *,
    work_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the configurable local Truth database path.

    ``GLOBUS_TRUTH_DB`` wins.  Otherwise the database lives beside the agent
    work directories, keeping a source checkout and a packaged install equally
    self-contained.
    """
    if database is not None:
        return Path(database)
    configured = (os.environ.get("GLOBUS_TRUTH_DB") or "").strip()
    if configured:
        return Path(configured)
    base = (
        Path(work_dir)
        if work_dir is not None
        else Path(os.environ.get("GLOBUS_AGENTS_WORK_DIR") or os.getcwd())
    )
    return base / "globus-truth.db"


@lru_cache(maxsize=8)
def _cached_service(database: str) -> TruthService:
    return TruthService(TruthRepository(database))


def get_truth_service(
    database: str | os.PathLike[str] | None = None,
    *,
    work_dir: str | os.PathLike[str] | None = None,
) -> TruthService:
    """Return a process-local, thread-safe service for runner and status code."""
    path = truth_database_path(database, work_dir=work_dir).resolve()
    return _cached_service(str(path))


def clear_truth_service_cache() -> None:
    """Test/deployment hook for an environment-path change."""
    _cached_service.cache_clear()


def model_output_is_refusal_like(reply: str) -> bool:
    """True when the actual reply looks like an error disguised as output."""
    text = reply or ""
    return any(pattern.search(text) for pattern in _ERROR_PROSE_PATTERNS)


def _receipt_identity(
    email: str,
    agent_name: str,
    run_key: str,
) -> tuple[str, str, str]:
    agent_id = member_agent_id(email, agent_name)
    digest = hashlib.sha256(
        f"{agent_id}|{run_key}".encode("utf-8")
    ).hexdigest()[:40]
    return agent_id, f"oss-run-{digest}", f"oss-receipt-{digest}"


def receipt_storage_id(
    email: str,
    agent_name: str,
    runner_run_id: int | str,
) -> str:
    """Return the exact persisted receipt ID for one durable runner row.

    MySQL ``globus_agent_runs.id`` values are durable and install-wide unique.
    Binding that ID to the member-scoped agent identity lets the status endpoint
    perform a stale-aware point read without scanning another member's rows.
    """
    _agent_id, _run_id, receipt_id = _receipt_identity(
        email, agent_name, f"runner-{runner_run_id}"
    )
    return receipt_id


def _metadata(
    *,
    email: str,
    agent_name: str,
    runner_run_id: int | str,
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, Any]:
    elapsed = max(0.0, (finished_at - started_at).total_seconds())
    return {
        "adapter": "oss-agent-runner",
        "agent_name": _safe_agent_name(agent_name),
        "member_hash": member_scope_hash(email),
        "runner_run_id": runner_run_id,
        "runtime_ms": int(round(elapsed * 1000)),
    }


def compact_truth(result: dict[str, Any]) -> dict[str, Any]:
    """Small, payload-free shape safe for AgentRunner results and status APIs."""
    evaluation = result.get("evaluation") or {}
    return {
        "storage_id": result.get("storage_id"),
        "verdict": evaluation.get("verdict"),
        "valid": bool(evaluation.get("valid")),
        "reason_codes": list(evaluation.get("reason_codes") or []),
    }


def _compact_stored(run: dict[str, Any]) -> dict[str, Any]:
    return compact_truth(
        {
            "storage_id": run.get("storage_id"),
            "evaluation": run.get("evaluation") or {},
        }
    )


def record_successful_agent_run(
    *,
    email: str,
    agent_name: str,
    runner_run_id: int | str,
    run_key: str,
    started_at: datetime,
    finished_at: datetime,
    model_reply: str,
    artifact_path: str | os.PathLike[str],
    expected_sha256: str,
    expected_bytes: int,
    service: TruthService | None = None,
    database: str | os.PathLike[str] | None = None,
    work_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Read back, verify, evaluate, and persist a completed agent run.

    The receipt declares success because the orchestrator returned normally.
    Failed content/integrity checks then cause the deterministic evaluator to
    downgrade that claim to ``degraded_contradictory``.
    """
    service = service or get_truth_service(database, work_dir=work_dir)
    observed_at = _iso(finished_at)
    path = Path(artifact_path)
    artifact_bytes: bytes | None = None
    read_error = ""
    try:
        artifact_bytes = path.read_bytes()
    except OSError as exc:
        read_error = f"{type(exc).__name__}: {exc}"

    actual_bytes = len(artifact_bytes) if artifact_bytes is not None else 0
    actual_sha256 = (
        hashlib.sha256(artifact_bytes).hexdigest()
        if artifact_bytes is not None
        else ""
    )
    readback_ok = artifact_bytes is not None
    size_matches = readback_ok and actual_bytes == expected_bytes
    sha_matches = (
        readback_ok
        and bool(re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""))
        and actual_sha256 == expected_sha256
    )

    reply = (model_reply or "").strip()
    meaningful_reply = len(reply) >= MIN_MEANINGFUL_REPLY_CHARS
    refusal_free = not model_output_is_refusal_like(reply)
    integrity_ok = bool(readback_ok and size_matches and sha_matches)

    agent_id, receipt_run_id, receipt_id = _receipt_identity(
        email, agent_name, run_key
    )
    evidence = []
    if readback_ok:
        evidence.append(
            {
                "kind": "artifact",
                "ref": f"agent-brief:{path.name}",
                "observed_at": observed_at,
                "detail": (
                    f"Read back {actual_bytes} bytes from the completed "
                    "agent artifact."
                ),
                "sha256": actual_sha256,
            }
        )

    receipt = {
        "schema_version": "1.0",
        "receipt_id": receipt_id,
        "agent_id": agent_id,
        "run_id": receipt_run_id,
        "declared_status": "success",
        "started_at": _iso(started_at),
        "finished_at": observed_at,
        "heartbeat_at": observed_at,
        "input": {"items_seen": 1, "items_eligible": 1},
        "output": {
            "items_processed": 1,
            "items_changed": 1 if integrity_ok else 0,
        },
        "summary": (
            f"{_safe_agent_name(agent_name)} completed; its "
            f"{actual_bytes}-byte artifact was read back and checked."
        ),
        "evidence": evidence,
        "checks": [
            {
                "name": "model_reply_meaningful",
                "passed": meaningful_reply,
                "detail": (
                    "Model reply contained meaningful non-whitespace output."
                    if meaningful_reply
                    else "Model reply was empty or too short to be a valid brief."
                ),
            },
            {
                "name": "model_reply_not_error_prose",
                "passed": refusal_free,
                "detail": (
                    "Actual model reply passed the refusal/error-prose scan."
                    if refusal_free
                    else "Actual model reply matched refusal/error prose."
                ),
            },
            {
                "name": "artifact_readback",
                "passed": readback_ok,
                "detail": (
                    "Artifact was reopened after the write completed."
                    if readback_ok
                    else f"Artifact read-back failed: {read_error[:800]}"
                ),
            },
            {
                "name": "artifact_size_matches",
                "passed": size_matches,
                "detail": (
                    f"Read-back size {actual_bytes} matched expected "
                    f"size {expected_bytes}."
                ),
            },
            {
                "name": "artifact_sha256_matches",
                "passed": sha_matches,
                "detail": (
                    "Read-back SHA-256 matched the bytes prepared by the runner."
                    if sha_matches
                    else "Read-back SHA-256 did not match the prepared artifact."
                ),
            },
        ],
        "metadata": _metadata(
            email=email,
            agent_name=agent_name,
            runner_run_id=runner_run_id,
            started_at=started_at,
            finished_at=finished_at,
        ),
    }
    return compact_truth(service.ingest(receipt))


def record_failed_agent_run(
    *,
    email: str,
    agent_name: str,
    runner_run_id: int | str,
    run_key: str,
    started_at: datetime,
    finished_at: datetime,
    error_code: str,
    error_message: str,
    service: TruthService | None = None,
    database: str | os.PathLike[str] | None = None,
    work_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Persist an explicit failed receipt for an exception path."""
    service = service or get_truth_service(database, work_dir=work_dir)
    observed_at = _iso(finished_at)
    agent_id, receipt_run_id, receipt_id = _receipt_identity(
        email, agent_name, run_key
    )
    # Provider errors can echo prompts, credentials, or other private inputs.
    # Preserve the typed error code for operators, but never copy arbitrary
    # provider text into the cross-run Truth database.
    _ = error_message
    safe_error = (
        "Agent execution failed; inspect the member-scoped runner log "
        "for operational details."
    )
    safe_code = re.sub(r"[^A-Za-z0-9._:-]+", "-", error_code or "agent_error")
    safe_code = safe_code.strip("-")[:100] or "agent_error"
    receipt = {
        "schema_version": "1.0",
        "receipt_id": receipt_id,
        "agent_id": agent_id,
        "run_id": receipt_run_id,
        "declared_status": "failed",
        "started_at": _iso(started_at),
        "finished_at": observed_at,
        "heartbeat_at": observed_at,
        "input": {"items_seen": 1, "items_eligible": 1},
        "output": {"items_processed": 0, "items_changed": 0},
        "summary": (
            f"{_safe_agent_name(agent_name)} failed before producing a "
            "verified artifact."
        ),
        "evidence": [],
        "checks": [
            {
                "name": "agent_completed",
                "passed": False,
                "detail": "The runner caught an exception before verified completion.",
            }
        ],
        "error": {"code": safe_code, "message": safe_error},
        "metadata": _metadata(
            email=email,
            agent_name=agent_name,
            runner_run_id=runner_run_id,
            started_at=started_at,
            finished_at=finished_at,
        ),
    }
    return compact_truth(service.ingest(receipt))


def truth_status_for_member(
    email: str,
    runner_runs: list[dict[str, Any]],
    *,
    service: TruthService | None = None,
    database: str | os.PathLike[str] | None = None,
    work_dir: str | os.PathLike[str] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return compact status indexes for one member, with no receipt payload.

    This is the UI/status integration seam.  ``by_runner_run_id`` attaches the
    right verdict to each recent MySQL run; ``latest_per_agent`` powers catalog
    badges. Each requested MySQL row maps to one deterministic receipt ID, so
    the query is tenant-exact and independent of a global receipt page size.
    """
    service = service or get_truth_service(database, work_dir=work_dir)
    by_runner_run_id: dict[str, dict[str, Any]] = {}
    latest_per_agent: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str]] = set()

    # The caller supplies rows newest-first. Preserve that order so setdefault
    # yields the latest receipt for an agent without any broad Truth DB scan.
    for reference in runner_runs:
        runner_run_id = reference.get("id")
        raw_agent_name = str(reference.get("agent_name") or "")
        if runner_run_id is None or not raw_agent_name:
            continue
        agent_name = _safe_agent_name(raw_agent_name)
        reference_key = (str(runner_run_id), agent_name)
        if reference_key in seen:
            continue
        seen.add(reference_key)

        storage_id = receipt_storage_id(email, agent_name, runner_run_id)
        current = service.get_run(storage_id)
        if current is None:
            continue

        receipt = current.get("receipt") or {}
        metadata = receipt.get("metadata") or {}
        # Defense in depth: a forged/conflicting row is never attached merely
        # because its storage key happened to be requested.
        if receipt.get("agent_id") != member_agent_id(email, agent_name):
            continue
        if str(metadata.get("runner_run_id")) != str(runner_run_id):
            continue

        compact = _compact_stored(current)
        by_runner_run_id[str(runner_run_id)] = compact
        latest_per_agent.setdefault(agent_name, compact)

    return {
        "by_runner_run_id": by_runner_run_id,
        "latest_per_agent": latest_per_agent,
    }


__all__ = [
    "clear_truth_service_cache",
    "compact_truth",
    "get_truth_service",
    "member_agent_id",
    "member_scope_hash",
    "model_output_is_refusal_like",
    "receipt_storage_id",
    "record_failed_agent_run",
    "record_successful_agent_run",
    "truth_database_path",
    "truth_status_for_member",
]
