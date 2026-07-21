"""Generated-local judge workflow for the reusable Verified Action SDK.

The lab offers two provider-shaped adapters, but neither can make a network
call: an email draft and a CRM note are written to a per-run SQLite sandbox.
The workflow binds the generated request to an immutable approval, executes
behind the existing fresh-Truth claim, independently reopens the destination,
and persists a payload-free verification before completion may succeed.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .approval_center import ApprovalCenter
from .reference_actions import CRMNoteAdapter, EmailDraftAdapter
from .verified_action_timeline import build_verified_action_timeline
from .verified_actions import (
    ActionBinding,
    ActionVerificationError,
    VerifiedActionSDK,
    canonical_json_bytes,
)


_DATABASE_NAME = "provider-sandbox.sqlite"
_REQUESTED_BY = "judge-mode:verified-action-sdk"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ADAPTER_CLASSES = {
    EmailDraftAdapter.manifest.id: EmailDraftAdapter,
    CRMNoteAdapter.manifest.id: CRMNoteAdapter,
}
_ADAPTER_SUMMARIES = {
    EmailDraftAdapter.manifest.id: {
        "label": "Create local email draft",
        "scope": "1 generated recipient · local SQLite draft",
    },
    CRMNoteAdapter.manifest.id: {
        "label": "Append local CRM note",
        "scope": "1 generated contact · local SQLite note",
    },
}


def _iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("lab clock must return a timezone-aware datetime")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _default_root(service: Any) -> Path:
    repository = getattr(service, "repository", None)
    database = str(getattr(repository, "database", "") or "")
    if database and database != ":memory:":
        return Path(database).expanduser().resolve().parent / "verified-action-lab"
    return (
        Path(tempfile.gettempdir()).resolve()
        / f"globus-verified-action-lab-{os.getpid()}"
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
        proposal_id = f"verified-{timestamp}-{secrets.token_hex(6)}"
        directory = resolved_root / proposal_id
        try:
            directory.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        if directory.resolve().parent != resolved_root:
            raise RuntimeError("verified action directory escaped its root")
        return proposal_id, directory
    raise FileExistsError("could not allocate a verified action directory")


def _existing_directory(root: Path, proposal_id: str) -> Path:
    if (
        not isinstance(proposal_id, str)
        or not _SAFE_ID_RE.fullmatch(proposal_id)
        or not proposal_id.startswith("verified-")
    ):
        raise ValueError("invalid verified action identifier")
    resolved_root = root.resolve()
    directory = (resolved_root / proposal_id).resolve()
    if directory.parent != resolved_root or not directory.is_dir():
        raise FileNotFoundError("verified action sandbox not found")
    return directory


def _demo_payload(adapter_id: str, proposal_id: str) -> dict[str, Any]:
    suffix = hashlib.sha256(proposal_id.encode("utf-8")).hexdigest()[:12]
    if adapter_id == EmailDraftAdapter.manifest.id:
        return {
            "to": f"generated-{suffix}@example.test",
            "subject": f"Generated Globus review {suffix}",
            "body": (
                "This is a generated local draft. It is never sent and can be "
                "discarded after reviewing the verification timeline."
            ),
        }
    if adapter_id == CRMNoteAdapter.manifest.id:
        return {
            "contact_id": f"generated-contact-{suffix}",
            "note": (
                "Generated local CRM note for the Verified Action SDK judge "
                "workflow. No provider account is contacted."
            ),
        }
    raise ValueError("unsupported verified action adapter")


def _build_sdk(
    database_path: Path,
    *,
    clock: Callable[[], datetime],
    audit_sink: Callable[[Mapping[str, Any]], Any] | None = None,
) -> VerifiedActionSDK:
    sdk = VerifiedActionSDK(audit_sink=audit_sink)
    sdk.register(EmailDraftAdapter(database_path, clock=clock))
    sdk.register(CRMNoteAdapter(database_path, clock=clock))
    return sdk


def verified_action_manifests() -> dict[str, Any]:
    """Return the public, credential-free adapter inventory."""

    # The adapters need no live destination merely to expose their immutable
    # class manifests, so do not create a file for this inventory read.
    manifests = [
        adapter_class.manifest.to_dict()
        for adapter_class in _ADAPTER_CLASSES.values()
    ]
    return {
        "schema_version": "globus.verified-action.manifests/v1",
        "execution_mode": "generated_local_only",
        "external_calls": 0,
        "manifests": sorted(manifests, key=lambda item: item["id"]),
        "disclosure": (
            "These reference adapters write only to a generated local SQLite "
            "sandbox. They do not connect to email or CRM providers."
        ),
    }


def _preflight_receipt(
    *,
    proposal_id: str,
    manifest: Mapping[str, Any],
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, Any]:
    observed_at = _iso(finished_at)
    evidence = {
        "adapter_id": manifest["id"],
        "adapter_version": manifest["version"],
        "action_kind": manifest["action_kind"],
        "destination_mode": "generated-local-sqlite",
        "ready": True,
    }
    return {
        "schema_version": "1.0",
        "receipt_id": f"{proposal_id}-truth",
        "agent_id": "judge-mode:verified-action-preflight",
        "run_id": f"{proposal_id}:preflight",
        "declared_status": "success",
        "started_at": _iso(started_at),
        "finished_at": observed_at,
        "heartbeat_at": observed_at,
        "input": {"items_seen": 1, "items_eligible": 1},
        "output": {"items_processed": 1, "items_changed": 1},
        "summary": (
            "Verified Action SDK validated one generated adapter contract and "
            "opened its local destination before requesting approval."
        ),
        "evidence": [
            {
                "kind": "database_write",
                "ref": f"adapter-preflight:{manifest['id']}",
                "observed_at": observed_at,
                "detail": "Observed one generated local adapter destination.",
                "sha256": _sha256(evidence),
            }
        ],
        "checks": [
            {
                "name": "verified_action_preflight",
                "passed": True,
                "detail": (
                    "Manifest, payload schema, and local destination were "
                    "validated without a provider call."
                ),
            }
        ],
        "metadata": {
            "mode": "credential-free-verified-action-lab",
            "proposal_id": proposal_id,
            "adapter_id": manifest["id"],
        },
    }


def _destination_count(database_path: Path, adapter_id: str) -> int:
    table = (
        "verified_email_drafts"
        if adapter_id == EmailDraftAdapter.manifest.id
        else (
            "verified_crm_notes"
            if adapter_id == CRMNoteAdapter.manifest.id
            else None
        )
    )
    if table is None:
        raise ValueError("unsupported verified action adapter")
    with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row is not None else 0


def _proposal_view(
    proposal: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    summary = _ADAPTER_SUMMARIES[str(manifest["id"])]
    return {
        "proposal_id": proposal["proposal_id"],
        "adapter_id": manifest["id"],
        "adapter_version": manifest["version"],
        "action_kind": manifest["action_kind"],
        "action_id": proposal["action_id"],
        "risk": proposal["risk"],
        "policy": proposal["policy_id"],
        "approval_mode": manifest["approval_mode"],
        "summary": summary["label"],
        "scope": summary["scope"],
        "payload_sha256": proposal["payload_sha256"],
        "truth_storage_id": proposal["storage_id"],
        "created_at": proposal["created_at"],
        "expires_at": proposal["expires_at"],
        "max_uses": 1,
    }


def stage_verified_action_lab(
    service: Any,
    *,
    adapter_id: str,
    artifact_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Stage one generated provider-shaped action and stop before execution."""

    if adapter_id not in _ADAPTER_CLASSES:
        raise ValueError("unsupported verified action adapter")
    now = service._now()
    proposal_id, directory = _new_directory(
        _root(service, artifact_root),
        now,
    )
    database_path = directory / _DATABASE_NAME
    sdk = _build_sdk(database_path, clock=service._now)
    prepared = sdk.prepare(
        proposal_id=proposal_id,
        adapter_id=adapter_id,
        payload=_demo_payload(adapter_id, proposal_id),
    )
    binding = prepared.binding
    manifest, _ = sdk.registry.resolve(adapter_id)
    finished_at = service._now()
    ingested = service.ingest(
        _preflight_receipt(
            proposal_id=proposal_id,
            manifest=manifest.to_dict(),
            started_at=now,
            finished_at=finished_at,
        )
    )
    if (
        ingested.get("evaluation", {}).get("verdict") != "healthy"
        or ingested.get("evaluation", {}).get("valid") is not True
    ):
        raise RuntimeError("verified action preflight did not establish Truth")

    center = ApprovalCenter(service, clock=service._now)
    stored = center.submit(
        proposal_id=proposal_id,
        storage_id=str(ingested["storage_id"]),
        action_id=f"verified-action:{proposal_id}",
        policy_id=manifest.policy,
        action_kind=manifest.action_kind,
        payload_sha256=binding.payload_sha256,
        requested_by=_REQUESTED_BY,
        risk=manifest.risk,
        expires_at=_iso(finished_at + timedelta(minutes=10)),
    )
    state = center.get(proposal_id)
    if not isinstance(state, Mapping) or state.get("state") != "pending":
        raise RuntimeError("verified action did not pause for human review")
    if _destination_count(database_path, adapter_id) != 0:
        raise RuntimeError("verified action executed before human review")
    proposal = state["proposal"]
    timeline = build_verified_action_timeline(
        service.repository,
        proposal_id,
        now=service._now(),
    )
    return {
        "schema_version": "globus.verified-action.lab/v1",
        "proposal_id": proposal_id,
        "status": "pending",
        "credential_free": True,
        "external_calls": 0,
        "created": bool(stored["created"]),
        "proposal": _proposal_view(proposal, manifest.to_dict()),
        "manifest": manifest.to_dict(),
        "destination": {
            "mode": "generated_local_sqlite",
            "observed_records": 0,
            "provider_connected": False,
        },
        "timeline": timeline,
    }


def _verification_record(
    *,
    proposal: Mapping[str, Any],
    binding: ActionBinding,
    claim: Mapping[str, Any],
    verified: bool,
    reason_code: str,
    observation_sha256: str,
    observed_count: int,
    verified_at: str,
) -> dict[str, Any]:
    fields = {
        "verification_id": (
            "verification-"
            + hashlib.sha256(
                str(proposal["proposal_id"]).encode("utf-8")
            ).hexdigest()[:32]
        ),
        "proposal_id": proposal["proposal_id"],
        "claim_id": claim["claim_id"],
        "adapter_id": binding.adapter_id,
        "adapter_version": binding.adapter_version,
        "action_kind": binding.action_kind,
        "request_sha256": binding.payload_sha256,
        "idempotency_key_sha256": hashlib.sha256(
            binding.idempotency_key.encode("ascii")
        ).hexdigest(),
        "observation_sha256": observation_sha256,
        "observed_count": observed_count,
        "verified": bool(verified),
        "reason_code": reason_code,
        "verified_at": verified_at,
    }
    return {
        **fields,
        "verification_sha256": _sha256(fields),
    }


def _safe_unavailable_observation(
    proposal_id: str,
    binding: ActionBinding,
) -> str:
    return _sha256(
        {
            "schema": "globus.verified-action.unavailable/v1",
            "proposal_id": proposal_id,
            "adapter_id": binding.adapter_id,
            "adapter_version": binding.adapter_version,
            "action_kind": binding.action_kind,
            "status": "unavailable",
        }
    )


def _recover_completion(
    service: Any,
    proposal_id: str,
) -> dict[str, Any] | None:
    snapshot = service.repository.get_verified_action_timeline_snapshot(
        proposal_id
    )
    if snapshot is None:
        return None
    claim = snapshot["claim"]
    verification = snapshot["verification"]
    completion = snapshot["completion"]
    if (
        not isinstance(claim, Mapping)
        or not isinstance(verification, Mapping)
        or isinstance(completion, Mapping)
    ):
        return None
    outcome = "succeeded" if verification["verified"] is True else "failed"
    item = {
        "completion_id": (
            "completion-recovered-"
            + hashlib.sha256(proposal_id.encode("utf-8")).hexdigest()[:24]
        ),
        "claim_id": claim["claim_id"],
        "outcome": outcome,
        "reason_code": "destination_proof_recovered",
        "completed_at": _iso(service._now()),
    }
    stored, _ = service.repository.save_approval_execution_completion(item)
    return stored


def _resolved_report(
    *,
    service: Any,
    proposal: Mapping[str, Any],
    manifest: Mapping[str, Any],
    database_path: Path,
    execution: Mapping[str, Any] | None,
    replay: Mapping[str, Any] | None,
    disposition: str,
    sdk_proof: Mapping[str, Any] | None = None,
    recovered: bool = False,
) -> dict[str, Any]:
    proposal_id = str(proposal["proposal_id"])
    verification = service.repository.get_verified_action_verification(
        proposal_id
    )
    timeline = build_verified_action_timeline(
        service.repository,
        proposal_id,
        now=service._now(),
    )
    observed_records = _destination_count(
        database_path,
        str(manifest["id"]),
    )
    verified = (
        isinstance(verification, Mapping)
        and verification.get("verified") is True
    )
    succeeded = (
        isinstance(timeline, Mapping)
        and timeline.get("state") == "succeeded"
        and timeline.get("integrity_complete") is True
    )
    rejected = disposition == "rejected"
    replay_blocked = (
        replay is None
        or (
            replay.get("callback_invoked") is False
            and replay.get("reason_code") == "approval_already_consumed"
        )
    )
    expectations_met = (
        (rejected and observed_records == 0)
        or (
            not rejected
            and succeeded
            and verified
            and observed_records == 1
            and replay_blocked
        )
    )
    proof_view = None
    if isinstance(sdk_proof, Mapping):
        read_back = sdk_proof.get("read_back")
        proof_verification = sdk_proof.get("verification")
        proof_view = {
            "status": sdk_proof.get("status"),
            "verified": sdk_proof.get("verified") is True,
            "payload_sha256": sdk_proof.get("payload_sha256"),
            "effect_id": (
                read_back.get("effect_id")
                if isinstance(read_back, Mapping)
                else None
            ),
            "record_sha256": (
                read_back.get("record_sha256")
                if isinstance(read_back, Mapping)
                else None
            ),
            "observed_at": (
                read_back.get("observed_at")
                if isinstance(read_back, Mapping)
                else None
            ),
            "reason_code": (
                proof_verification.get("reason_code")
                if isinstance(proof_verification, Mapping)
                else None
            ),
        }
    return {
        "schema_version": "globus.verified-action.lab/v1",
        "proposal_id": proposal_id,
        "status": (
            "rejected"
            if rejected
            else (
                "completed"
                if expectations_met
                else (
                    "indeterminate"
                    if isinstance(timeline, Mapping)
                    and timeline.get("state") == "indeterminate"
                    else "needs_attention"
                )
            )
        ),
        "disposition": disposition,
        "credential_free": True,
        "external_calls": 0,
        "expectations_met": bool(expectations_met),
        "recovered_without_reexecution": bool(recovered),
        "proposal": _proposal_view(proposal, manifest),
        "manifest": dict(manifest),
        "destination": {
            "mode": "generated_local_sqlite",
            "observed_records": observed_records,
            "provider_connected": False,
            "verified": bool(verified),
        },
        "execution": dict(execution) if isinstance(execution, Mapping) else None,
        "replay": dict(replay) if isinstance(replay, Mapping) else None,
        "verification": verification,
        "proof": proof_view,
        "timeline": timeline,
    }


def resolve_verified_action_lab(
    service: Any,
    proposal_id: str,
    *,
    disposition: str,
    artifact_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Resolve one generated adapter proposal without accepting a payload."""

    if disposition not in {"approved", "rejected"}:
        raise ValueError("disposition must be approved or rejected")
    directory = _existing_directory(
        _root(service, artifact_root),
        proposal_id,
    )
    database_path = directory / _DATABASE_NAME
    if not database_path.is_file():
        raise FileNotFoundError("verified action sandbox not found")

    center = ApprovalCenter(service, clock=service._now)
    state = center.get(proposal_id)
    if not isinstance(state, Mapping):
        raise FileNotFoundError("verified action proposal not found")
    proposal = state["proposal"]
    if (
        proposal.get("proposal_id") != proposal_id
        or proposal.get("requested_by") != _REQUESTED_BY
        or proposal.get("storage_id") != f"{proposal_id}-truth"
        or proposal.get("action_id") != f"verified-action:{proposal_id}"
    ):
        raise ValueError("proposal is not a Verified Action SDK lab request")

    adapter_id = next(
        (
            candidate
            for candidate, adapter_class in _ADAPTER_CLASSES.items()
            if adapter_class.manifest.action_kind == proposal["action_kind"]
        ),
        None,
    )
    if adapter_id is None:
        raise ValueError("proposal references an unsupported action kind")

    proof_holder: dict[str, Any] = {}
    prepared_holder: dict[str, Any] = {}

    def persist_proof(proof: Mapping[str, Any]) -> None:
        prepared = prepared_holder["prepared"]
        binding = prepared.binding
        claim = service.repository.get_approval_execution_claim(proposal_id)
        if not isinstance(claim, Mapping):
            raise RuntimeError("verified action claim is unavailable")
        verification = proof.get("verification")
        read_back = proof.get("read_back")
        if not isinstance(verification, Mapping) or not isinstance(
            read_back,
            Mapping,
        ):
            raise RuntimeError("verified action proof is incomplete")
        verified = verification.get("verified") is True
        observation_sha256 = read_back.get("record_sha256")
        if not isinstance(observation_sha256, str):
            observation_sha256 = _safe_unavailable_observation(
                proposal_id,
                binding,
            )
        item = _verification_record(
            proposal=proposal,
            binding=binding,
            claim=claim,
            verified=verified,
            reason_code=(
                "destination_readback_verified"
                if verified
                else str(
                    verification.get("reason_code")
                    or "destination_verification_failed"
                )
            ),
            observation_sha256=observation_sha256,
            observed_count=1 if read_back.get("exists") is True else 0,
            verified_at=str(verification["verified_at"]),
        )
        service.repository.save_verified_action_verification(item)

    sdk = _build_sdk(
        database_path,
        clock=service._now,
        audit_sink=persist_proof,
    )
    prepared = sdk.prepare(
        proposal_id=proposal_id,
        adapter_id=adapter_id,
        payload=_demo_payload(adapter_id, proposal_id),
    )
    prepared_holder["prepared"] = prepared
    binding = prepared.binding
    manifest, _ = sdk.registry.resolve(adapter_id)
    if (
        binding.payload_sha256 != proposal["payload_sha256"]
        or binding.action_kind != proposal["action_kind"]
        or binding.risk != proposal["risk"]
        or binding.policy != proposal["policy_id"]
    ):
        raise ValueError("persisted proposal does not match its adapter binding")

    decision = center.decide(
        proposal_id,
        outcome=disposition,
        decided_by="judge-mode:human-reviewer",
        reason_code=(
            "verified_action_approved"
            if disposition == "approved"
            else "operator_rejected"
        ),
    )
    del decision
    if disposition == "rejected":
        return _resolved_report(
            service=service,
            proposal=proposal,
            manifest=manifest.to_dict(),
            database_path=database_path,
            execution=None,
            replay=None,
            disposition=disposition,
        )

    snapshot = service.repository.get_verified_action_timeline_snapshot(
        proposal_id
    )
    if isinstance(snapshot, Mapping) and isinstance(
        snapshot.get("claim"),
        Mapping,
    ):
        if isinstance(snapshot.get("verification"), Mapping):
            recovered = _recover_completion(service, proposal_id)
            return _resolved_report(
                service=service,
                proposal=proposal,
                manifest=manifest.to_dict(),
                database_path=database_path,
                execution=None,
                replay={
                    "status": "already_consumed",
                    "reason_code": "approval_already_consumed",
                    "callback_invoked": False,
                },
                disposition=disposition,
                recovered=recovered is not None,
            )
        return _resolved_report(
            service=service,
            proposal=proposal,
            manifest=manifest.to_dict(),
            database_path=database_path,
            execution=None,
            replay={
                "status": "already_consumed",
                "reason_code": "approval_already_consumed",
                "callback_invoked": False,
            },
            disposition=disposition,
        )

    def inner_authorization(
        action_binding: ActionBinding,
        execute_once: Callable[[], Any],
    ) -> dict[str, Any]:
        claim = service.repository.get_approval_execution_claim(proposal_id)
        if (
            not isinstance(claim, Mapping)
            or claim.get("proposal_id") != proposal_id
            or claim.get("action_id") != proposal["action_id"]
        ):
            raise RuntimeError("verified action claim binding is unavailable")
        execute_once()
        return {
            "authorization_id": claim["claim_id"],
            "proposal_id": action_binding.proposal_id,
            "adapter_id": action_binding.adapter_id,
            "payload_sha256": action_binding.payload_sha256,
            "authorized": True,
        }

    def callback() -> None:
        try:
            proof = sdk.execute(
                prepared,
                approved_payload_sha256=str(proposal["payload_sha256"]),
                authorization_runner=inner_authorization,
            )
            proof_holder["proof"] = proof
            if proof.get("verified") is not True:
                raise ActionVerificationError(
                    "destination read-back did not verify"
                )
        except Exception:
            claim = service.repository.get_approval_execution_claim(proposal_id)
            existing = service.repository.get_verified_action_verification(
                proposal_id
            )
            if isinstance(claim, Mapping) and existing is None:
                fallback = _verification_record(
                    proposal=proposal,
                    binding=binding,
                    claim=claim,
                    verified=False,
                    reason_code="destination_verification_unavailable",
                    observation_sha256=_safe_unavailable_observation(
                        proposal_id,
                        binding,
                    ),
                    observed_count=0,
                    verified_at=_iso(service._now()),
                )
                service.repository.save_verified_action_verification(fallback)
            raise

    execution = center.execute(
        proposal_id,
        callback,
        payload_sha256=str(proposal["payload_sha256"]),
    )
    replay_callbacks = 0

    def replay_callback() -> None:
        nonlocal replay_callbacks
        replay_callbacks += 1

    replay = center.execute(
        proposal_id,
        replay_callback,
        payload_sha256=str(proposal["payload_sha256"]),
    )
    if replay_callbacks:
        raise RuntimeError("verified action replay invoked its callback")
    return _resolved_report(
        service=service,
        proposal=proposal,
        manifest=manifest.to_dict(),
        database_path=database_path,
        execution=execution,
        replay=replay,
        disposition=disposition,
        sdk_proof=proof_holder.get("proof"),
    )


__all__ = [
    "resolve_verified_action_lab",
    "stage_verified_action_lab",
    "verified_action_manifests",
]
