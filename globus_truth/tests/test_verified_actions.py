from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import globus_truth
from globus_truth.reference_actions import CRMNoteAdapter, EmailDraftAdapter
from globus_truth.verified_actions import (
    ActionAuthorizationError,
    ActionBindingError,
    ActionIndeterminateError,
    ActionManifest,
    AdapterExecution,
    AdapterReadBack,
    AdapterRegistrationError,
    AdapterRegistry,
    AdapterVerification,
    ManifestValidationError,
    PreparedAction,
    VerifiedActionSDK,
    canonical_action_sha256,
    canonical_json_bytes,
    canonical_payload_sha256,
    deterministic_idempotency_key,
)


NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)


def authorize(binding: Any, execute_once: Any) -> dict[str, Any]:
    execute_once()
    return {
        "authorization_id": "approval-test-001",
        "proposal_id": binding.proposal_id,
        "adapter_id": binding.adapter_id,
        "payload_sha256": binding.payload_sha256,
        "authorized": True,
    }


class UnknownAdapter:
    manifest = {
        **EmailDraftAdapter.manifest.to_dict(),
        "id": "globus.local.unknown",
        "action_kind": "calendar.event.create",
    }

    @staticmethod
    def validate_payload(payload: dict[str, Any]) -> None:
        return None

    @staticmethod
    def execute(prepared: PreparedAction) -> None:
        return None

    @staticmethod
    def read_back(prepared: PreparedAction) -> None:
        return None

    @staticmethod
    def verify(prepared: PreparedAction, read_back: Any) -> None:
        return None


class VerifiedActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "reference-actions.sqlite"
        self.email = EmailDraftAdapter(self.database, clock=lambda: NOW)
        self.crm = CRMNoteAdapter(self.database, clock=lambda: NOW)
        self.sdk = VerifiedActionSDK(authorization_runner=authorize)
        self.sdk.register(self.email)
        self.sdk.register(self.crm)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def count(self, table: str) -> int:
        if table not in {"verified_email_drafts", "verified_crm_notes"}:
            raise ValueError("unsafe test table")
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])

    def test_public_package_exports_verified_action_sdk(self) -> None:
        self.assertIs(globus_truth.VerifiedActionSDK, VerifiedActionSDK)
        self.assertIs(globus_truth.ActionManifest, ActionManifest)
        self.assertIs(globus_truth.EmailDraftAdapter, EmailDraftAdapter)
        self.assertIs(globus_truth.CRMNoteAdapter, CRMNoteAdapter)

    def email_action(
        self,
        *,
        proposal_id: str = "proposal-email-001",
        subject: str = "Quarterly review",
        body: str = "Draft only. A human can edit this before sending.",
    ) -> PreparedAction:
        return self.sdk.prepare(
            proposal_id=proposal_id,
            adapter_id=EmailDraftAdapter.manifest.id,
            payload={
                "to": "judge@example.test",
                "subject": subject,
                "body": body,
            },
        )

    def test_manifest_is_complete_normalized_and_strict(self) -> None:
        raw = EmailDraftAdapter.manifest.to_dict()
        normalized = ActionManifest.from_mapping(raw)
        self.assertEqual(normalized, EmailDraftAdapter.manifest)
        self.assertEqual(set(raw), {
            "id",
            "version",
            "action_kind",
            "risk",
            "policy",
            "permissions",
            "approval_mode",
            "idempotency_strategy",
            "read_back_mode",
        })

        with self.assertRaises(ManifestValidationError):
            ActionManifest.from_mapping({**raw, "network": True})
        missing = dict(raw)
        del missing["approval_mode"]
        with self.assertRaises(ManifestValidationError):
            ActionManifest.from_mapping(missing)
        with self.assertRaises(ManifestValidationError):
            ActionManifest.from_mapping(
                {**raw, "permissions": ["network.http", "local.sqlite.write"]}
            )
        with self.assertRaises(ManifestValidationError):
            ActionManifest.from_mapping(
                {**raw, "permissions": ["local.sqlite.read"] * 2}
            )
        with self.assertRaises(ManifestValidationError):
            ActionManifest.from_mapping({**raw, "approval_mode": "implicit"})

    def test_duplicate_unknown_and_incomplete_adapters_fail_closed(self) -> None:
        registry = AdapterRegistry()
        registry.register(self.email)
        with self.assertRaises(AdapterRegistrationError):
            registry.register(self.email)
        with self.assertRaises(ManifestValidationError):
            AdapterRegistry().register(UnknownAdapter())

        class MissingVerify:
            manifest = CRMNoteAdapter.manifest

            @staticmethod
            def validate_payload(payload: dict[str, Any]) -> None:
                return None

            @staticmethod
            def execute(prepared: PreparedAction) -> None:
                return None

            @staticmethod
            def read_back(prepared: PreparedAction) -> None:
                return None

        with self.assertRaises(AdapterRegistrationError):
            AdapterRegistry().register(MissingVerify())

    def test_canonical_payload_hash_is_exact_and_deterministic(self) -> None:
        left = {"subject": "Olá", "body": ["a", 2], "enabled": True}
        right = {"enabled": True, "body": ["a", 2], "subject": "Olá"}
        changed = {"subject": "Olá", "body": ["a", 3], "enabled": True}
        self.assertEqual(canonical_json_bytes(left), canonical_json_bytes(right))
        self.assertEqual(
            canonical_payload_sha256(left),
            canonical_payload_sha256(right),
        )
        self.assertNotEqual(
            canonical_payload_sha256(left),
            canonical_payload_sha256(changed),
        )
        with self.assertRaises(TypeError):
            canonical_payload_sha256({"unsafe": float("nan")})
        with self.assertRaises(TypeError):
            canonical_payload_sha256({"not_json": ("tuple",)})
        with self.assertRaises(ValueError):
            canonical_payload_sha256({"too_large": 1 << 60})

    def test_idempotency_key_binds_proposal_adapter_version_and_payload(self) -> None:
        digest = canonical_payload_sha256({"body": "one"})
        key = deterministic_idempotency_key(
            proposal_id="proposal-001",
            manifest=EmailDraftAdapter.manifest,
            payload_sha256=digest,
        )
        self.assertEqual(
            key,
            deterministic_idempotency_key(
                proposal_id="proposal-001",
                manifest=EmailDraftAdapter.manifest,
                payload_sha256=digest,
            ),
        )
        self.assertNotEqual(
            key,
            deterministic_idempotency_key(
                proposal_id="proposal-002",
                manifest=EmailDraftAdapter.manifest,
                payload_sha256=digest,
            ),
        )
        self.assertNotEqual(
            key,
            deterministic_idempotency_key(
                proposal_id="proposal-001",
                manifest=CRMNoteAdapter.manifest,
                payload_sha256=digest,
            ),
        )

    def test_action_digest_binds_adapter_identity_version_and_kind(self) -> None:
        payload = {
            "to": "judge@example.test",
            "subject": "Exact adapter binding",
            "body": "Generated local draft.",
        }
        original = EmailDraftAdapter.manifest
        changed_version = ActionManifest.from_mapping(
            {**original.to_dict(), "version": "1.0.1"}
        )
        changed_identity = ActionManifest.from_mapping(
            {**original.to_dict(), "id": "globus.local.email-draft-v2"}
        )
        changed_kind = ActionManifest.from_mapping(
            {
                **original.to_dict(),
                "action_kind": "verified.crm.note.create",
            }
        )
        digest = canonical_action_sha256(original, payload)
        self.assertNotEqual(
            digest,
            canonical_action_sha256(changed_version, payload),
        )
        self.assertNotEqual(
            digest,
            canonical_action_sha256(changed_identity, payload),
        )
        self.assertNotEqual(
            digest,
            canonical_action_sha256(changed_kind, payload),
        )

    def test_email_lifecycle_returns_only_payload_free_audit_proof(self) -> None:
        captured: list[dict[str, Any]] = []
        sdk = VerifiedActionSDK(
            authorization_runner=authorize,
            audit_sink=lambda proof: captured.append(dict(proof)),
        )
        sdk.register(self.email)
        secret_recipient = "private-recipient@example.test"
        secret_subject = "Confidential renewal terms"
        secret_body = "Never copy this draft body into the audit proof."
        prepared = sdk.prepare(
            proposal_id="proposal-private-email",
            adapter_id=EmailDraftAdapter.manifest.id,
            payload={
                "to": secret_recipient,
                "subject": secret_subject,
                "body": secret_body,
            },
        )

        self.assertNotIn(secret_body, repr(prepared))
        proof = sdk.execute(
            prepared,
            approved_payload_sha256=prepared.binding.payload_sha256,
        )

        self.assertTrue(proof["verified"])
        self.assertEqual(proof["status"], "verified")
        self.assertEqual(proof["execution"]["outcome"], "created")
        self.assertEqual(proof["verification"]["reason_code"], "destination_verified")
        self.assertEqual(self.count("verified_email_drafts"), 1)
        serialized = json.dumps(proof, sort_keys=True)
        for raw_value in (secret_recipient, secret_subject, secret_body):
            self.assertNotIn(raw_value, serialized)
            self.assertNotIn(raw_value, json.dumps(captured, sort_keys=True))
        self.assertEqual(captured[0], proof)

    def test_crm_note_lifecycle_is_local_idempotent_and_verified(self) -> None:
        prepared = self.sdk.prepare(
            proposal_id="proposal-crm-001",
            adapter_id=CRMNoteAdapter.manifest.id,
            payload={
                "contact_id": "contact-4821",
                "note": "Customer asked for a call next Tuesday.",
            },
        )
        first = self.sdk.execute(
            prepared,
            approved_payload_sha256=prepared.binding.payload_sha256,
        )
        replay = self.sdk.execute(
            prepared,
            approved_payload_sha256=prepared.binding.payload_sha256,
        )

        self.assertTrue(first["verified"])
        self.assertEqual(first["execution"]["outcome"], "created")
        self.assertTrue(replay["verified"])
        self.assertEqual(replay["execution"]["outcome"], "already_exists")
        self.assertEqual(first["execution"]["effect_id"], replay["execution"]["effect_id"])
        self.assertEqual(self.count("verified_crm_notes"), 1)
        self.assertNotIn(
            "Customer asked",
            json.dumps(replay, sort_keys=True),
        )

    def test_changed_payload_is_blocked_before_authorization_or_write(self) -> None:
        exact = self.email_action(body="Approved exact draft")
        changed = self.email_action(
            proposal_id=exact.binding.proposal_id,
            body="Changed after approval",
        )
        calls = 0

        def should_not_run(binding: Any, execute_once: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return authorize(binding, execute_once)

        with self.assertRaises(ActionBindingError):
            self.sdk.execute(
                changed,
                approved_payload_sha256=exact.binding.payload_sha256,
                authorization_runner=should_not_run,
            )
        self.assertEqual(calls, 0)
        self.assertEqual(self.count("verified_email_drafts"), 0)

    def test_tampered_prepared_payload_is_blocked(self) -> None:
        exact = self.email_action()
        forged = PreparedAction(
            binding=exact.binding,
            _payload_json=json.dumps(
                {
                    "to": "attacker@example.test",
                    "subject": "Quarterly review",
                    "body": "Draft only. A human can edit this before sending.",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        with self.assertRaises(ActionBindingError):
            self.sdk.execute(
                forged,
                approved_payload_sha256=exact.binding.payload_sha256,
            )
        self.assertEqual(self.count("verified_email_drafts"), 0)

    def test_concurrent_retries_create_one_destination_record(self) -> None:
        prepared = self.email_action(proposal_id="proposal-concurrent")

        def run(_: int) -> dict[str, Any]:
            return self.sdk.execute(
                prepared,
                approved_payload_sha256=prepared.binding.payload_sha256,
            )

        with ThreadPoolExecutor(max_workers=12) as executor:
            proofs = list(executor.map(run, range(24)))

        outcomes = [proof["execution"]["outcome"] for proof in proofs]
        self.assertEqual(outcomes.count("created"), 1)
        self.assertEqual(outcomes.count("already_exists"), 23)
        self.assertTrue(all(proof["verified"] for proof in proofs))
        self.assertEqual(
            {proof["idempotency_key"] for proof in proofs},
            {prepared.binding.idempotency_key},
        )
        self.assertEqual(self.count("verified_email_drafts"), 1)

    def test_independent_read_back_detects_destination_change(self) -> None:
        prepared = self.email_action()
        proof = self.sdk.execute(
            prepared,
            approved_payload_sha256=prepared.binding.payload_sha256,
        )
        self.assertTrue(proof["verified"])
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE verified_email_drafts
                       SET subject = 'Changed directly in destination'
                     WHERE idempotency_key = ?
                    """,
                    (prepared.binding.idempotency_key,),
                )

        observation = self.email.read_back(prepared)
        result = self.email.verify(prepared, observation)
        self.assertFalse(result.verified)
        self.assertEqual(result.reason_code, "destination_payload_mismatch")

    def test_adapter_cannot_verify_read_back_that_misses_approved_binding(
        self,
    ) -> None:
        delegate = self.email

        class FalsePositiveAdapter:
            manifest = EmailDraftAdapter.manifest

            @staticmethod
            def validate_payload(payload: dict[str, Any]) -> None:
                delegate.validate_payload(payload)

            @staticmethod
            def execute(prepared: PreparedAction) -> AdapterExecution:
                return delegate.execute(prepared)

            @staticmethod
            def read_back(prepared: PreparedAction) -> AdapterReadBack:
                observed = delegate.read_back(prepared)
                return AdapterReadBack(
                    effect_id=observed.effect_id,
                    idempotency_key=observed.idempotency_key,
                    proposal_id=observed.proposal_id,
                    adapter_id=observed.adapter_id,
                    adapter_version=observed.adapter_version,
                    payload_sha256="0" * 64,
                    declared_payload_sha256="0" * 64,
                    record_sha256=observed.record_sha256,
                    exists=True,
                    observed_at=observed.observed_at,
                )

            @staticmethod
            def verify(
                prepared: PreparedAction,
                read_back: AdapterReadBack,
            ) -> AdapterVerification:
                del prepared, read_back
                return AdapterVerification(
                    verified=True,
                    reason_code="destination_verified",
                    verified_at=NOW.isoformat(
                        timespec="microseconds"
                    ).replace("+00:00", "Z"),
                )

        captured: list[dict[str, Any]] = []
        sdk = VerifiedActionSDK(
            authorization_runner=authorize,
            audit_sink=lambda proof: captured.append(dict(proof)),
        )
        sdk.register(FalsePositiveAdapter())
        prepared = sdk.prepare(
            proposal_id="proposal-false-positive",
            adapter_id=EmailDraftAdapter.manifest.id,
            payload={
                "to": "judge@example.test",
                "subject": "Must remain exact",
                "body": "The destination hashes are intentionally forged.",
            },
        )
        proof = sdk.execute(
            prepared,
            approved_payload_sha256=prepared.binding.payload_sha256,
        )

        self.assertFalse(proof["verified"])
        self.assertEqual(proof["status"], "verification_failed")
        self.assertEqual(
            proof["verification"]["reason_code"],
            "destination_binding_mismatch",
        )
        self.assertNotEqual(
            proof["read_back"]["payload_sha256"],
            prepared.binding.payload_sha256,
        )
        self.assertEqual(captured, [proof])
        self.assertEqual(self.count("verified_email_drafts"), 1)

    def test_read_back_and_verification_cannot_predate_the_effect(self) -> None:
        delegate = self.email

        class OutOfOrderAdapter:
            manifest = EmailDraftAdapter.manifest

            def __init__(self, mode: str) -> None:
                self.mode = mode

            @staticmethod
            def validate_payload(payload: dict[str, Any]) -> None:
                delegate.validate_payload(payload)

            @staticmethod
            def execute(prepared: PreparedAction) -> AdapterExecution:
                return delegate.execute(prepared)

            def read_back(self, prepared: PreparedAction) -> AdapterReadBack:
                observed = delegate.read_back(prepared)
                if self.mode == "read_back":
                    return replace(
                        observed,
                        observed_at="2029-01-15T12:00:00.000000Z",
                    )
                return observed

            def verify(
                self,
                prepared: PreparedAction,
                read_back: AdapterReadBack,
            ) -> AdapterVerification:
                verified = delegate.verify(prepared, read_back)
                if self.mode == "verification":
                    return replace(
                        verified,
                        verified_at="2029-01-15T12:00:00.000000Z",
                    )
                return verified

        for mode in ("read_back", "verification"):
            with self.subTest(mode=mode):
                sdk = VerifiedActionSDK(authorization_runner=authorize)
                sdk.register(OutOfOrderAdapter(mode))
                prepared = sdk.prepare(
                    proposal_id=f"proposal-out-of-order-{mode}",
                    adapter_id=EmailDraftAdapter.manifest.id,
                    payload={
                        "to": "judge@example.test",
                        "subject": f"Chronology {mode}",
                        "body": "The proof must follow the destination effect.",
                    },
                )
                with self.assertRaises(AdapterRegistrationError):
                    sdk.execute(
                        prepared,
                        approved_payload_sha256=prepared.binding.payload_sha256,
                    )
        self.assertEqual(self.count("verified_email_drafts"), 2)

    def test_missing_or_denied_authorization_never_writes(self) -> None:
        prepared = self.email_action()
        sdk = VerifiedActionSDK()
        sdk.register(self.email)
        with self.assertRaises(ActionAuthorizationError):
            sdk.execute(
                prepared,
                approved_payload_sha256=prepared.binding.payload_sha256,
            )

        def deny(binding: Any, execute_once: Any) -> dict[str, Any]:
            return {
                "authorization_id": "approval-denied",
                "proposal_id": binding.proposal_id,
                "adapter_id": binding.adapter_id,
                "payload_sha256": binding.payload_sha256,
                "authorized": False,
            }

        with self.assertRaises(ActionAuthorizationError):
            sdk.execute(
                prepared,
                approved_payload_sha256=prepared.binding.payload_sha256,
                authorization_runner=deny,
            )
        self.assertEqual(self.count("verified_email_drafts"), 0)

    def test_retained_effect_callback_is_disabled_after_denial(self) -> None:
        prepared = self.email_action(proposal_id="proposal-retained-denial")
        retained: list[Any] = []

        def retain_then_deny(binding: Any, execute_once: Any) -> dict[str, Any]:
            retained.append(execute_once)
            return {
                "authorization_id": "approval-retained-denial",
                "proposal_id": binding.proposal_id,
                "adapter_id": binding.adapter_id,
                "payload_sha256": binding.payload_sha256,
                "authorized": False,
            }

        with self.assertRaises(ActionAuthorizationError):
            self.sdk.execute(
                prepared,
                approved_payload_sha256=prepared.binding.payload_sha256,
                authorization_runner=retain_then_deny,
            )
        with self.assertRaises(ActionAuthorizationError):
            retained[0]()
        self.assertEqual(self.count("verified_email_drafts"), 0)

    def test_retained_effect_callback_is_disabled_after_runner_exception(
        self,
    ) -> None:
        prepared = self.email_action(proposal_id="proposal-retained-error")
        retained: list[Any] = []

        def retain_then_fail(binding: Any, execute_once: Any) -> dict[str, Any]:
            del binding
            retained.append(execute_once)
            raise RuntimeError("authorization backend failed")

        with self.assertRaises(ActionAuthorizationError):
            self.sdk.execute(
                prepared,
                approved_payload_sha256=prepared.binding.payload_sha256,
                authorization_runner=retain_then_fail,
            )
        with self.assertRaises(ActionAuthorizationError):
            retained[0]()
        self.assertEqual(self.count("verified_email_drafts"), 0)

    def test_adapter_validation_error_cannot_echo_raw_payload(self) -> None:
        secret = "never-leak-this-validator-secret"

        class EchoingValidator:
            manifest = EmailDraftAdapter.manifest

            @staticmethod
            def validate_payload(payload: dict[str, Any]) -> None:
                raise ValueError(f"invalid private payload: {payload!r}")

            @staticmethod
            def execute(prepared: PreparedAction) -> None:
                del prepared

            @staticmethod
            def read_back(prepared: PreparedAction) -> None:
                del prepared

            @staticmethod
            def verify(
                prepared: PreparedAction,
                read_back: Any,
            ) -> None:
                del prepared, read_back

        sdk = VerifiedActionSDK()
        sdk.register(EchoingValidator())
        with self.assertRaises(ActionBindingError) as raised:
            sdk.prepare(
                proposal_id="proposal-validator-secret",
                adapter_id=EmailDraftAdapter.manifest.id,
                payload={
                    "to": "judge@example.test",
                    "subject": "Private validation",
                    "body": secret,
                },
            )
        self.assertEqual(
            str(raised.exception),
            "adapter payload validation failed",
        )
        self.assertNotIn(secret, str(raised.exception))

        with self.assertRaises(ActionBindingError) as canonical:
            sdk.prepare(
                proposal_id="proposal-invalid-json",
                adapter_id=EmailDraftAdapter.manifest.id,
                payload={"unsafe": float("nan")},
            )
        self.assertIn("strict JSON", str(canonical.exception))

    def test_authorization_runner_cannot_invoke_effect_twice(self) -> None:
        prepared = self.email_action()

        def unsafe_runner(binding: Any, execute_once: Any) -> dict[str, Any]:
            execute_once()
            execute_once()
            raise AssertionError("unreachable")

        with self.assertRaises(ActionIndeterminateError):
            self.sdk.execute(
                prepared,
                approved_payload_sha256=prepared.binding.payload_sha256,
                authorization_runner=unsafe_runner,
            )
        self.assertEqual(self.count("verified_email_drafts"), 1)


if __name__ == "__main__":
    unittest.main()
