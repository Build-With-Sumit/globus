"""Credential-free, local proof challenge for hackathon judges.

Judge Mode performs a controlled experiment against real filesystem bytes. It
writes and verifies one artifact, builds a healthy phase, appends exactly one
byte, builds a contradictory phase, and atomically persists both receipts.

The returned structure intentionally contains only generated identifiers,
relative artifact names, measurements, and verdicts.  Absolute filesystem paths
and receipt payloads stay inside the local service.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .agent_adapter import verify_artifact_readback


_AGENT_ID = "judge-mode:artifact-integrity"
_ARTIFACT_NAME = "manifest.json"


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _default_artifact_root(service: Any) -> Path:
    repository = getattr(service, "repository", None)
    database = str(getattr(repository, "database", "") or "")
    if database and database != ":memory:":
        return Path(database).expanduser().resolve().parent / "judge-artifacts"
    return (
        Path(tempfile.gettempdir()).resolve()
        / f"globus-truth-judge-{os.getpid()}"
    )


def _create_challenge_directory(root: Path, now: datetime) -> tuple[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    resolved_root = root.resolve()
    timestamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for _ in range(8):
        challenge_id = f"judge-{timestamp}-{secrets.token_hex(6)}"
        challenge_directory = resolved_root / challenge_id
        try:
            challenge_directory.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        if challenge_directory.resolve().parent != resolved_root:
            raise RuntimeError("judge artifact directory escaped its root")
        return challenge_id, challenge_directory
    raise FileExistsError("could not allocate a unique judge challenge directory")


def _receipt(
    *,
    challenge_id: str,
    phase: str,
    started_at: datetime,
    finished_at: datetime,
    verification: Mapping[str, Any],
) -> dict[str, Any]:
    phase_order = {
        "before_tamper": "01",
        "after_tamper": "02",
    }
    ordinal = phase_order.get(phase)
    if ordinal is None:
        raise ValueError("unsupported judge challenge phase")
    observed_at = _iso(finished_at)
    readback_ok = bool(verification.get("readback_ok"))
    size_matches = bool(verification.get("size_matches"))
    sha256_matches = bool(verification.get("sha256_matches"))
    observed_bytes = int(verification.get("observed_bytes") or 0)
    expected_bytes = int(verification.get("expected_bytes") or 0)
    observed_sha256 = str(verification.get("observed_sha256") or "")
    expected_sha256 = str(verification.get("expected_sha256") or "")
    error_code = str(verification.get("error_code") or "")

    evidence: list[dict[str, Any]] = []
    if (
        readback_ok
        and len(observed_sha256) == 64
        and all(character in "0123456789abcdef" for character in observed_sha256)
    ):
        evidence.append(
            {
                "kind": "artifact",
                "ref": f"judge-artifact:{verification['artifact_name']}",
                "observed_at": observed_at,
                "detail": (
                    f"Reopened and measured {observed_bytes} artifact bytes "
                    f"during the {phase.replace('_', ' ')} phase."
                ),
                "sha256": observed_sha256,
            }
        )

    return {
        "schema_version": "1.0",
        "receipt_id": f"{challenge_id}-{ordinal}-{phase}",
        "agent_id": _AGENT_ID,
        "run_id": f"{challenge_id}:{ordinal}:{phase}",
        "declared_status": "success",
        "started_at": _iso(started_at),
        "finished_at": observed_at,
        "heartbeat_at": observed_at,
        "input": {"items_seen": 1, "items_eligible": 1},
        "output": {
            "items_processed": 1,
            "items_changed": 1 if verification.get("valid") else 0,
        },
        "summary": (
            "Judge Mode reopened a controlled local artifact and compared its "
            f"bytes with the original measurement during {phase.replace('_', ' ')}."
        ),
        "evidence": evidence,
        "checks": [
            {
                "name": "artifact_readback",
                "passed": readback_ok,
                "detail": (
                    "The controlled artifact was reopened after its write."
                    if readback_ok
                    else (
                        "The controlled artifact could not be reopened"
                        + (f" ({error_code})." if error_code else ".")
                    )
                ),
            },
            {
                "name": "artifact_size_matches",
                "passed": size_matches,
                "detail": (
                    f"Expected {expected_bytes} bytes and observed "
                    f"{observed_bytes} bytes."
                ),
            },
            {
                "name": "artifact_sha256_matches",
                "passed": sha256_matches,
                "detail": (
                    "Observed SHA-256 matched the original artifact bytes."
                    if sha256_matches
                    else (
                        "Observed SHA-256 differed from the original artifact "
                        f"bytes ({expected_sha256[:12]} != "
                        f"{observed_sha256[:12] or 'unavailable'})."
                    )
                ),
            },
        ],
        "metadata": {
            "mode": "credential-free-judge-challenge",
            "challenge_id": challenge_id,
            "phase": phase,
        },
    }


def _phase_result(
    name: str,
    action: str,
    verification: Mapping[str, Any],
    ingest_result: Mapping[str, Any],
) -> dict[str, Any]:
    evaluation = ingest_result.get("evaluation") or {}
    return {
        "name": name,
        "action": action,
        "storage_id": ingest_result.get("storage_id"),
        "verdict": evaluation.get("verdict"),
        "valid": bool(evaluation.get("valid")),
        "observed_bytes": int(verification.get("observed_bytes") or 0),
        "observed_sha256": str(verification.get("observed_sha256") or ""),
        "size_matches": bool(verification.get("size_matches")),
        "sha256_matches": bool(verification.get("sha256_matches")),
    }


def run_artifact_tamper_challenge(
    service: Any,
    *,
    artifact_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Run one real write/read-back/tamper experiment and persist both phases."""

    started_at = service._now()
    root = (
        Path(artifact_root).expanduser().resolve()
        if artifact_root is not None
        else _default_artifact_root(service)
    )
    challenge_id, challenge_directory = _create_challenge_directory(
        root, started_at
    )
    artifact_path = challenge_directory / _ARTIFACT_NAME

    prepared_bytes = (
        json.dumps(
            {
                "challenge_id": challenge_id,
                "purpose": "Globus Truth Layer local integrity challenge",
                "records": [
                    {"id": "alpha", "state": "ready"},
                    {"id": "beta", "state": "ready"},
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    expected_sha256 = hashlib.sha256(prepared_bytes).hexdigest()
    expected_bytes = len(prepared_bytes)

    with artifact_path.open("xb") as handle:
        handle.write(prepared_bytes)

    intact_verification = verify_artifact_readback(
        artifact_path,
        expected_sha256=expected_sha256,
        expected_bytes=expected_bytes,
    )
    intact_finished_at = service._now()
    intact_receipt = _receipt(
        challenge_id=challenge_id,
        phase="before_tamper",
        started_at=started_at,
        finished_at=intact_finished_at,
        verification=intact_verification,
    )

    tamper_started_at = service._now()
    with artifact_path.open("ab") as handle:
        written = handle.write(b"!")
    if written != 1:
        raise OSError("judge challenge did not append exactly one byte")

    tampered_verification = verify_artifact_readback(
        artifact_path,
        expected_sha256=expected_sha256,
        expected_bytes=expected_bytes,
    )
    tamper_finished_at = service._now()
    tampered_receipt = _receipt(
        challenge_id=challenge_id,
        phase="after_tamper",
        started_at=tamper_started_at,
        finished_at=tamper_finished_at,
        verification=tampered_verification,
    )
    intact_result, tampered_result = service.ingest_many(
        [intact_receipt, tampered_receipt]
    )

    phases = [
        _phase_result(
            "before_tamper",
            "write_and_verify",
            intact_verification,
            intact_result,
        ),
        _phase_result(
            "after_tamper",
            "append_one_byte_and_reverify",
            tampered_verification,
            tampered_result,
        ),
    ]
    return {
        "challenge_id": challenge_id,
        "credential_free": True,
        "external_calls": 0,
        "expectations_met": (
            phases[0]["verdict"] == "healthy"
            and phases[0]["valid"]
            and phases[0]["sha256_matches"]
            and phases[1]["verdict"] == "degraded_contradictory"
            and not phases[1]["valid"]
            and phases[1]["observed_bytes"] == expected_bytes + 1
            and not phases[1]["size_matches"]
            and not phases[1]["sha256_matches"]
        ),
        "artifact": {
            "name": _ARTIFACT_NAME,
            "relative_path": f"{challenge_id}/{_ARTIFACT_NAME}",
            "expected_bytes": expected_bytes,
            "final_bytes": int(tampered_verification.get("observed_bytes") or 0),
            "expected_sha256": expected_sha256,
            "final_sha256": str(
                tampered_verification.get("observed_sha256") or ""
            ),
        },
        "phases": phases,
    }


__all__ = ["run_artifact_tamper_challenge"]
