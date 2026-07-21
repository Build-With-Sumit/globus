"""Credential-free local reference adapters for the Verified Action SDK.

These adapters intentionally make no network calls.  They model two familiar
provider operations using SQLite: creating an email draft and appending a CRM
note.  Execution is idempotent under a unique deterministic key.  Read-back
uses a new query-only connection and recomputes the exact payload hash from the
destination columns before returning a payload-free observation.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .verified_actions import (
    ActionBindingError,
    ActionManifest,
    AdapterExecution,
    AdapterReadBack,
    AdapterVerification,
    PreparedAction,
    canonical_action_sha256,
    canonical_json_bytes,
)


_SAFE_CONTACT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _text(
    payload: dict[str, Any],
    field: str,
    *,
    maximum: int,
) -> str:
    value = payload.get(field)
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise ValueError(
            f"{field} must be a non-empty string of at most {maximum} characters"
        )
    return value


def _effect_id(prefix: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    return f"{prefix}-{digest[:32]}"


class _SQLiteReferenceAdapter:
    manifest: ActionManifest
    _effect_prefix: str

    def __init__(
        self,
        database: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if str(database) == ":memory:":
            raise ValueError(
                "reference adapters require a file database for independent read-back"
            )
        self.database = Path(database).expanduser().resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._initialize()

    def _now(self) -> str:
        return _iso(self._clock())

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database,
            timeout=15.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        raise NotImplementedError

    def _validated_payload(self, prepared: PreparedAction) -> dict[str, Any]:
        binding = prepared.binding
        if (
            binding.adapter_id != self.manifest.id
            or binding.adapter_version != self.manifest.version
            or binding.action_kind != self.manifest.action_kind
        ):
            raise ActionBindingError("prepared action targets another adapter")
        payload = prepared.payload_copy()
        self.validate_payload(payload)
        if canonical_action_sha256(self.manifest, payload) != binding.payload_sha256:
            raise ActionBindingError("prepared action payload binding changed")
        return payload

    def _missing_read_back(
        self,
        prepared: PreparedAction,
        *,
        observed_at: str,
    ) -> AdapterReadBack:
        binding = prepared.binding
        return AdapterReadBack(
            effect_id=_effect_id(self._effect_prefix, binding.idempotency_key),
            idempotency_key=binding.idempotency_key,
            proposal_id=binding.proposal_id,
            adapter_id=binding.adapter_id,
            adapter_version=binding.adapter_version,
            payload_sha256=None,
            declared_payload_sha256=None,
            record_sha256=None,
            exists=False,
            observed_at=observed_at,
        )

    def verify(
        self,
        prepared: PreparedAction,
        read_back: AdapterReadBack,
    ) -> AdapterVerification:
        binding = prepared.binding
        expected_effect = _effect_id(
            self._effect_prefix,
            binding.idempotency_key,
        )
        reason = "destination_verified"
        verified = True
        if not read_back.exists:
            verified, reason = False, "destination_record_missing"
        elif (
            read_back.effect_id != expected_effect
            or read_back.idempotency_key != binding.idempotency_key
            or read_back.proposal_id != binding.proposal_id
            or read_back.adapter_id != binding.adapter_id
            or read_back.adapter_version != binding.adapter_version
        ):
            verified, reason = False, "destination_binding_mismatch"
        elif (
            read_back.payload_sha256 != binding.payload_sha256
            or read_back.declared_payload_sha256 != binding.payload_sha256
            or read_back.payload_sha256 != read_back.declared_payload_sha256
        ):
            verified, reason = False, "destination_payload_mismatch"
        elif (
            not isinstance(read_back.record_sha256, str)
            or not _SHA256_RE.fullmatch(read_back.record_sha256)
        ):
            verified, reason = False, "destination_record_hash_missing"
        return AdapterVerification(
            verified=verified,
            reason_code=reason,
            verified_at=self._now(),
        )


class EmailDraftAdapter(_SQLiteReferenceAdapter):
    """Local provider-shaped ``create draft`` operation."""

    manifest = ActionManifest(
        id="globus.local.email-draft",
        version="1.0.0",
        action_kind="verified.email.draft.create",
        risk="medium",
        policy="healthy_only",
        permissions=("local.sqlite.read", "local.sqlite.write"),
        approval_mode="explicit",
        idempotency_strategy="proposal-adapter-payload-sha256",
        read_back_mode="independent-read-only",
    )
    _effect_prefix = "draft"

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS verified_email_drafts (
                    effect_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    proposal_id TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    adapter_version TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL CHECK (
                        length(payload_sha256) = 64
                        AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
                    ),
                    recipient TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def validate_payload(self, payload: dict[str, Any]) -> None:
        if type(payload) is not dict or set(payload) != {"to", "subject", "body"}:
            raise ValueError(
                "email draft payload must contain exactly to, subject, and body"
            )
        recipient = _text(payload, "to", maximum=254)
        if (
            recipient != recipient.strip()
            or recipient.count("@") != 1
            or any(character.isspace() for character in recipient)
        ):
            raise ValueError("to must be one plain email address")
        _text(payload, "subject", maximum=200)
        _text(payload, "body", maximum=20_000)

    def execute(self, prepared: PreparedAction) -> AdapterExecution:
        payload = self._validated_payload(prepared)
        binding = prepared.binding
        effect_id = _effect_id(self._effect_prefix, binding.idempotency_key)
        requested_at = self._now()
        expected = (
            effect_id,
            binding.idempotency_key,
            binding.proposal_id,
            binding.adapter_id,
            binding.adapter_version,
            binding.payload_sha256,
            payload["to"],
            payload["subject"],
            payload["body"],
        )
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    INSERT INTO verified_email_drafts (
                        effect_id, idempotency_key, proposal_id, adapter_id,
                        adapter_version, payload_sha256, recipient, subject,
                        body, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(idempotency_key) DO NOTHING
                    """,
                    (*expected, requested_at),
                )
                created = cursor.rowcount == 1
                row = connection.execute(
                    """
                    SELECT effect_id, idempotency_key, proposal_id, adapter_id,
                           adapter_version, payload_sha256, recipient, subject,
                           body, created_at
                      FROM verified_email_drafts
                     WHERE idempotency_key = ?
                    """,
                    (binding.idempotency_key,),
                ).fetchone()
                if row is None or tuple(row[:9]) != expected:
                    raise ActionBindingError(
                        "email destination idempotency collision"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return AdapterExecution(
            effect_id=effect_id,
            idempotency_key=binding.idempotency_key,
            outcome="created" if created else "already_exists",
            executed_at=str(row["created_at"]),
        )

    def read_back(self, prepared: PreparedAction) -> AdapterReadBack:
        binding = prepared.binding
        observed_at = self._now()
        # This is intentionally a new connection with SQLite write operations
        # disabled, rather than a value returned by ``execute``.
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                """
                SELECT effect_id, idempotency_key, proposal_id, adapter_id,
                       adapter_version, payload_sha256, recipient, subject,
                       body, created_at
                  FROM verified_email_drafts
                 WHERE idempotency_key = ?
                """,
                (binding.idempotency_key,),
            ).fetchone()
        if row is None:
            return self._missing_read_back(
                prepared,
                observed_at=observed_at,
            )
        payload_digest = canonical_action_sha256(
            self.manifest,
            {
                "to": row["recipient"],
                "subject": row["subject"],
                "body": row["body"],
            }
        )
        record_digest = hashlib.sha256(
            canonical_json_bytes(dict(row))
        ).hexdigest()
        return AdapterReadBack(
            effect_id=str(row["effect_id"]),
            idempotency_key=str(row["idempotency_key"]),
            proposal_id=str(row["proposal_id"]),
            adapter_id=str(row["adapter_id"]),
            adapter_version=str(row["adapter_version"]),
            payload_sha256=payload_digest,
            declared_payload_sha256=str(row["payload_sha256"]),
            record_sha256=record_digest,
            exists=True,
            observed_at=observed_at,
        )


class CRMNoteAdapter(_SQLiteReferenceAdapter):
    """Local provider-shaped ``append CRM note`` operation."""

    manifest = ActionManifest(
        id="globus.local.crm-note",
        version="1.0.0",
        action_kind="verified.crm.note.create",
        risk="medium",
        policy="healthy_only",
        permissions=("local.sqlite.read", "local.sqlite.write"),
        approval_mode="explicit",
        idempotency_strategy="proposal-adapter-payload-sha256",
        read_back_mode="independent-read-only",
    )
    _effect_prefix = "crm-note"

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS verified_crm_notes (
                    effect_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    proposal_id TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    adapter_version TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL CHECK (
                        length(payload_sha256) = 64
                        AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
                    ),
                    contact_id TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def validate_payload(self, payload: dict[str, Any]) -> None:
        if type(payload) is not dict or set(payload) != {"contact_id", "note"}:
            raise ValueError(
                "CRM note payload must contain exactly contact_id and note"
            )
        contact_id = _text(payload, "contact_id", maximum=128)
        if not _SAFE_CONTACT_RE.fullmatch(contact_id):
            raise ValueError("contact_id must be a safe identifier")
        _text(payload, "note", maximum=20_000)

    def execute(self, prepared: PreparedAction) -> AdapterExecution:
        payload = self._validated_payload(prepared)
        binding = prepared.binding
        effect_id = _effect_id(self._effect_prefix, binding.idempotency_key)
        requested_at = self._now()
        expected = (
            effect_id,
            binding.idempotency_key,
            binding.proposal_id,
            binding.adapter_id,
            binding.adapter_version,
            binding.payload_sha256,
            payload["contact_id"],
            payload["note"],
        )
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    INSERT INTO verified_crm_notes (
                        effect_id, idempotency_key, proposal_id, adapter_id,
                        adapter_version, payload_sha256, contact_id, note,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(idempotency_key) DO NOTHING
                    """,
                    (*expected, requested_at),
                )
                created = cursor.rowcount == 1
                row = connection.execute(
                    """
                    SELECT effect_id, idempotency_key, proposal_id, adapter_id,
                           adapter_version, payload_sha256, contact_id, note,
                           created_at
                      FROM verified_crm_notes
                     WHERE idempotency_key = ?
                    """,
                    (binding.idempotency_key,),
                ).fetchone()
                if row is None or tuple(row[:8]) != expected:
                    raise ActionBindingError("CRM destination idempotency collision")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return AdapterExecution(
            effect_id=effect_id,
            idempotency_key=binding.idempotency_key,
            outcome="created" if created else "already_exists",
            executed_at=str(row["created_at"]),
        )

    def read_back(self, prepared: PreparedAction) -> AdapterReadBack:
        binding = prepared.binding
        observed_at = self._now()
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                """
                SELECT effect_id, idempotency_key, proposal_id, adapter_id,
                       adapter_version, payload_sha256, contact_id, note,
                       created_at
                  FROM verified_crm_notes
                 WHERE idempotency_key = ?
                """,
                (binding.idempotency_key,),
            ).fetchone()
        if row is None:
            return self._missing_read_back(
                prepared,
                observed_at=observed_at,
            )
        payload_digest = canonical_action_sha256(
            self.manifest,
            {
                "contact_id": row["contact_id"],
                "note": row["note"],
            }
        )
        record_digest = hashlib.sha256(
            canonical_json_bytes(dict(row))
        ).hexdigest()
        return AdapterReadBack(
            effect_id=str(row["effect_id"]),
            idempotency_key=str(row["idempotency_key"]),
            proposal_id=str(row["proposal_id"]),
            adapter_id=str(row["adapter_id"]),
            adapter_version=str(row["adapter_version"]),
            payload_sha256=payload_digest,
            declared_payload_sha256=str(row["payload_sha256"]),
            record_sha256=record_digest,
            exists=True,
            observed_at=observed_at,
        )


__all__ = ["CRMNoteAdapter", "EmailDraftAdapter"]
