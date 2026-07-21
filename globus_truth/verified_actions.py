"""Reusable, payload-bound execution lifecycle for consequential actions.

The SDK deliberately does not decide whether an action is approved.  Instead,
an injected authorization runner receives a payload-free binding and an
``execute_once`` callback.  This shape lets an Approval Center claim wrap the
destination effect without coupling adapters to a particular repository.

Only canonical JSON crosses the binding boundary.  Adapter execution is
followed by a new, independent read-back and verification step.  The returned
proof contains identifiers and hashes, never the raw action payload.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping


MANIFEST_FIELDS = frozenset(
    {
        "id",
        "version",
        "action_kind",
        "risk",
        "policy",
        "permissions",
        "approval_mode",
        "idempotency_strategy",
        "read_back_mode",
    }
)
RISKS = frozenset({"low", "medium", "high", "critical"})
POLICIES = frozenset({"healthy_only", "trusted_completion"})
APPROVAL_MODES = frozenset({"explicit"})
IDEMPOTENCY_STRATEGIES = frozenset({"proposal-adapter-payload-sha256"})
READ_BACK_MODES = frozenset({"independent-read-only"})
SAFE_PERMISSIONS = frozenset({"local.sqlite.read", "local.sqlite.write"})
KNOWN_ACTION_KINDS = frozenset(
    {"verified.email.draft.create", "verified.crm.note.create"}
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_CANONICAL_BYTES = 256 * 1024
_MAX_JSON_DEPTH = 32
_MAX_SAFE_INTEGER = (1 << 53) - 1
_AUTHORIZATION_FIELDS = frozenset(
    {
        "authorization_id",
        "proposal_id",
        "adapter_id",
        "payload_sha256",
        "authorized",
    }
)


class VerifiedActionError(RuntimeError):
    """Base class for fail-closed Verified Action failures."""


class ManifestValidationError(VerifiedActionError, ValueError):
    """An adapter manifest was unknown, ambiguous, or unsafe."""


class AdapterRegistrationError(VerifiedActionError, ValueError):
    """An adapter did not satisfy the runtime contract."""


class ActionBindingError(VerifiedActionError, ValueError):
    """A payload did not match the immutable action binding."""


class ActionAuthorizationError(VerifiedActionError):
    """The injected authorization runner did not authorize execution."""


class ActionExecutionError(VerifiedActionError):
    """The destination adapter failed before returning an execution record."""


class ActionIndeterminateError(VerifiedActionError):
    """An effect may exist but its authorization response was unusable."""


class ActionVerificationError(VerifiedActionError):
    """Independent destination read-back could not be completed."""


class ActionAuditError(VerifiedActionError):
    """The optional proof sink failed after the action lifecycle completed."""


def _safe_id(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"{name} must be a safe 1-128 character identifier")
    return value


def _sha256(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _timestamp(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise ValueError(f"{name} must be a canonical RFC 3339 timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise ValueError(
            f"{name} must be a canonical RFC 3339 timestamp"
        ) from None
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    normalized = (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    if value != normalized:
        raise ValueError(f"{name} must be normalized to UTC microseconds")
    return value


def _validate_json(value: Any, *, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON value exceeds the maximum nesting depth")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
            raise ValueError("JSON integer exceeds the interoperable safe range")
        return
    if type(value) is list:
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            _validate_json(item, depth=depth + 1)
        return
    # Floats are intentionally excluded: NaN is unsafe and decimal spellings
    # are not guaranteed to be stable across all JSON implementations.
    raise TypeError("payload must contain only strict JSON values")


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one canonical UTF-8 representation accepted by this SDK."""

    _validate_json(value)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_CANONICAL_BYTES:
        raise ValueError("canonical JSON payload exceeds 256 KiB")
    return encoded


def canonical_payload_sha256(payload: Mapping[str, Any]) -> str:
    """Hash an exact strict-JSON object without storing or returning it."""

    if type(payload) is not dict:
        raise TypeError("action payload must be a JSON object")
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def canonical_action_sha256(
    manifest: "ActionManifest",
    payload: Mapping[str, Any],
) -> str:
    """Bind an exact payload to the adapter identity, version, and action kind."""

    if not isinstance(manifest, ActionManifest):
        raise TypeError("manifest must be an ActionManifest")
    if type(payload) is not dict:
        raise TypeError("action payload must be a JSON object")
    envelope = {
        "schema": "globus.verified-action.binding/v1",
        "adapter_id": manifest.id,
        "adapter_version": manifest.version,
        "action_kind": manifest.action_kind,
        "payload": payload,
    }
    return hashlib.sha256(canonical_json_bytes(envelope)).hexdigest()


@dataclass(frozen=True, slots=True)
class ActionManifest:
    """Normalized, immutable contract for one adapter and action kind."""

    id: str
    version: str
    action_kind: str
    risk: str
    policy: str
    permissions: tuple[str, ...]
    approval_mode: str
    idempotency_strategy: str
    read_back_mode: str

    def __post_init__(self) -> None:
        try:
            _safe_id("manifest id", self.id)
            _safe_id("action kind", self.action_kind)
        except ValueError as exc:
            raise ManifestValidationError(str(exc)) from None
        if not isinstance(self.version, str) or not _SEMVER_RE.fullmatch(
            self.version
        ):
            raise ManifestValidationError(
                "manifest version must be a strict major.minor.patch version"
            )
        if self.risk not in RISKS:
            raise ManifestValidationError("unknown action risk")
        if self.policy not in POLICIES:
            raise ManifestValidationError("unknown Truth policy")
        if self.approval_mode not in APPROVAL_MODES:
            raise ManifestValidationError("unsafe or unknown approval mode")
        if self.idempotency_strategy not in IDEMPOTENCY_STRATEGIES:
            raise ManifestValidationError("unsafe or unknown idempotency strategy")
        if self.read_back_mode not in READ_BACK_MODES:
            raise ManifestValidationError("unsafe or unknown read-back mode")
        if type(self.permissions) is not tuple or not self.permissions:
            raise ManifestValidationError(
                "manifest permissions must be a non-empty tuple"
            )
        if len(set(self.permissions)) != len(self.permissions):
            raise ManifestValidationError("duplicate manifest permission")
        if tuple(sorted(self.permissions)) != self.permissions:
            raise ManifestValidationError(
                "manifest permissions must be uniquely sorted"
            )
        for permission in self.permissions:
            try:
                _safe_id("permission", permission)
            except ValueError as exc:
                raise ManifestValidationError(str(exc)) from None
        if not set(self.permissions).issubset(SAFE_PERMISSIONS):
            raise ManifestValidationError("unsafe or unknown manifest permission")
        required = {"local.sqlite.read", "local.sqlite.write"}
        if not required.issubset(self.permissions):
            raise ManifestValidationError(
                "verified local actions require SQLite read and write permissions"
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActionManifest":
        if not isinstance(value, Mapping):
            raise ManifestValidationError("adapter manifest must be an object")
        try:
            fields = set(value)
        except Exception:
            raise ManifestValidationError("adapter manifest is unreadable") from None
        if fields != MANIFEST_FIELDS:
            unknown = sorted(str(field) for field in fields - MANIFEST_FIELDS)
            missing = sorted(MANIFEST_FIELDS - fields)
            detail = []
            if unknown:
                detail.append(f"unknown fields: {', '.join(unknown)}")
            if missing:
                detail.append(f"missing fields: {', '.join(missing)}")
            raise ManifestValidationError("; ".join(detail))
        permissions = value["permissions"]
        if type(permissions) not in {list, tuple}:
            raise ManifestValidationError("manifest permissions must be an array")
        normalized = tuple(permissions)
        if any(not isinstance(item, str) for item in normalized):
            raise ManifestValidationError("manifest permissions must be strings")
        if len(set(normalized)) != len(normalized):
            raise ManifestValidationError("duplicate manifest permission")
        normalized = tuple(sorted(normalized))
        try:
            return cls(
                id=value["id"],
                version=value["version"],
                action_kind=value["action_kind"],
                risk=value["risk"],
                policy=value["policy"],
                permissions=normalized,
                approval_mode=value["approval_mode"],
                idempotency_strategy=value["idempotency_strategy"],
                read_back_mode=value["read_back_mode"],
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ManifestValidationError):
                raise
            raise ManifestValidationError("manifest contains invalid values") from None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "action_kind": self.action_kind,
            "risk": self.risk,
            "policy": self.policy,
            "permissions": list(self.permissions),
            "approval_mode": self.approval_mode,
            "idempotency_strategy": self.idempotency_strategy,
            "read_back_mode": self.read_back_mode,
        }


def deterministic_idempotency_key(
    *,
    proposal_id: str,
    manifest: ActionManifest,
    payload_sha256: str,
) -> str:
    """Bind a stable retry key to proposal, adapter version, kind, and payload."""

    try:
        proposal_id = _safe_id("proposal id", proposal_id)
        payload_sha256 = _sha256("payload_sha256", payload_sha256)
    except ValueError as exc:
        raise ActionBindingError(str(exc)) from None
    binding = {
        "schema": "globus.verified-action.idempotency/v1",
        "proposal_id": proposal_id,
        "adapter_id": manifest.id,
        "adapter_version": manifest.version,
        "action_kind": manifest.action_kind,
        "payload_sha256": payload_sha256,
    }
    return "va1-" + hashlib.sha256(canonical_json_bytes(binding)).hexdigest()


@dataclass(frozen=True, slots=True)
class ActionBinding:
    """Payload-free authorization envelope."""

    proposal_id: str
    adapter_id: str
    adapter_version: str
    action_kind: str
    risk: str
    policy: str
    approval_mode: str
    payload_sha256: str
    idempotency_key: str

    def to_dict(self) -> dict[str, str]:
        return {
            "proposal_id": self.proposal_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "action_kind": self.action_kind,
            "risk": self.risk,
            "policy": self.policy,
            "approval_mode": self.approval_mode,
            "payload_sha256": self.payload_sha256,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True, repr=False)
class PreparedAction:
    """Opaque prepared action; its representation intentionally hides payload."""

    binding: ActionBinding
    _payload_json: str = field(repr=False)

    def payload_copy(self) -> dict[str, Any]:
        value = json.loads(self._payload_json)
        if type(value) is not dict:
            raise ActionBindingError("prepared payload is no longer a JSON object")
        return value

    def __repr__(self) -> str:
        return f"PreparedAction(binding={self.binding!r}, payload=<redacted>)"


@dataclass(frozen=True, slots=True)
class AdapterExecution:
    effect_id: str
    idempotency_key: str
    outcome: str
    executed_at: str


@dataclass(frozen=True, slots=True)
class AdapterReadBack:
    effect_id: str
    idempotency_key: str
    proposal_id: str
    adapter_id: str
    adapter_version: str
    payload_sha256: str | None
    declared_payload_sha256: str | None
    record_sha256: str | None
    exists: bool
    observed_at: str


@dataclass(frozen=True, slots=True)
class AdapterVerification:
    verified: bool
    reason_code: str
    verified_at: str


class AdapterRegistry:
    """Fail-closed registry with unambiguous ID and action-kind routing."""

    def __init__(
        self,
        *,
        known_action_kinds: Iterable[str] = KNOWN_ACTION_KINDS,
        safe_permissions: Iterable[str] = SAFE_PERMISSIONS,
    ) -> None:
        self._known_action_kinds = frozenset(known_action_kinds)
        self._safe_permissions = frozenset(safe_permissions)
        if not self._known_action_kinds or not self._safe_permissions:
            raise ValueError("registry allowlists cannot be empty")
        if not self._safe_permissions.issubset(SAFE_PERMISSIONS):
            raise ValueError("registry cannot enable unsafe permissions")
        self._adapters: dict[str, tuple[ActionManifest, Any]] = {}
        self._action_kinds: dict[str, str] = {}

    def register(self, adapter: Any) -> ActionManifest:
        raw_manifest = getattr(adapter, "manifest", None)
        manifest = (
            raw_manifest
            if isinstance(raw_manifest, ActionManifest)
            else ActionManifest.from_mapping(raw_manifest)
        )
        if manifest.action_kind not in self._known_action_kinds:
            raise ManifestValidationError("unknown action kind")
        unknown_permissions = set(manifest.permissions) - self._safe_permissions
        if unknown_permissions:
            raise ManifestValidationError("unsafe or unknown adapter permission")
        if manifest.id in self._adapters:
            raise AdapterRegistrationError("duplicate adapter manifest id")
        if manifest.action_kind in self._action_kinds:
            raise AdapterRegistrationError("duplicate adapter action kind")
        for method in ("validate_payload", "execute", "read_back", "verify"):
            if not callable(getattr(adapter, method, None)):
                raise AdapterRegistrationError(
                    f"adapter must provide callable {method}"
                )
        self._adapters[manifest.id] = (manifest, adapter)
        self._action_kinds[manifest.action_kind] = manifest.id
        return manifest

    def resolve(self, adapter_id: str) -> tuple[ActionManifest, Any]:
        try:
            adapter_id = _safe_id("adapter id", adapter_id)
        except ValueError as exc:
            raise AdapterRegistrationError(str(exc)) from None
        selected = self._adapters.get(adapter_id)
        if selected is None:
            raise AdapterRegistrationError("unknown adapter")
        return selected

    def manifests(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            self._adapters[key][0].to_dict()
            for key in sorted(self._adapters)
        )


AuthorizationRunner = Callable[
    [ActionBinding, Callable[[], AdapterExecution]],
    Mapping[str, Any],
]
AuditSink = Callable[[Mapping[str, Any]], Any]


class VerifiedActionSDK:
    """Prepare, authorize, execute, read back, verify, and audit local actions."""

    def __init__(
        self,
        registry: AdapterRegistry | None = None,
        *,
        authorization_runner: AuthorizationRunner | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self.registry = registry or AdapterRegistry()
        if authorization_runner is not None and not callable(authorization_runner):
            raise TypeError("authorization_runner must be callable")
        if audit_sink is not None and not callable(audit_sink):
            raise TypeError("audit_sink must be callable")
        self._authorization_runner = authorization_runner
        self._audit_sink = audit_sink

    def register(self, adapter: Any) -> ActionManifest:
        return self.registry.register(adapter)

    def prepare(
        self,
        *,
        proposal_id: str,
        adapter_id: str,
        payload: Mapping[str, Any],
    ) -> PreparedAction:
        manifest, adapter = self.registry.resolve(adapter_id)
        if type(payload) is not dict:
            raise ActionBindingError("action payload must be a JSON object")
        try:
            payload_json = canonical_json_bytes(payload).decode("utf-8")
            payload_snapshot = json.loads(payload_json)
        except (TypeError, ValueError) as exc:
            raise ActionBindingError(str(exc)) from None
        try:
            validation_result = adapter.validate_payload(payload_snapshot)
        except Exception:
            # Adapter exceptions are not a safe error channel: a third-party
            # validator may include the request itself in its exception text.
            raise ActionBindingError("adapter payload validation failed") from None
        if validation_result is not None:
            raise AdapterRegistrationError(
                "adapter validate_payload must return None"
            )
        digest = canonical_action_sha256(manifest, payload_snapshot)
        key = deterministic_idempotency_key(
            proposal_id=proposal_id,
            manifest=manifest,
            payload_sha256=digest,
        )
        binding = ActionBinding(
            proposal_id=proposal_id,
            adapter_id=manifest.id,
            adapter_version=manifest.version,
            action_kind=manifest.action_kind,
            risk=manifest.risk,
            policy=manifest.policy,
            approval_mode=manifest.approval_mode,
            payload_sha256=digest,
            idempotency_key=key,
        )
        return PreparedAction(binding=binding, _payload_json=payload_json)

    @staticmethod
    def _validate_execution(
        binding: ActionBinding,
        value: Any,
    ) -> AdapterExecution:
        if not isinstance(value, AdapterExecution):
            raise AdapterRegistrationError(
                "adapter execute must return AdapterExecution"
            )
        if (
            value.idempotency_key != binding.idempotency_key
            or value.outcome not in {"created", "already_exists"}
        ):
            raise AdapterRegistrationError("adapter execution binding mismatch")
        try:
            _safe_id("effect id", value.effect_id)
            _timestamp("executed_at", value.executed_at)
        except ValueError as exc:
            raise AdapterRegistrationError(str(exc)) from None
        return value

    @staticmethod
    def _validate_read_back(
        binding: ActionBinding,
        execution: AdapterExecution,
        value: Any,
    ) -> AdapterReadBack:
        if not isinstance(value, AdapterReadBack):
            raise AdapterRegistrationError(
                "adapter read_back must return AdapterReadBack"
            )
        if (
            value.effect_id != execution.effect_id
            or value.idempotency_key != binding.idempotency_key
            or value.proposal_id != binding.proposal_id
            or value.adapter_id != binding.adapter_id
            or value.adapter_version != binding.adapter_version
        ):
            raise AdapterRegistrationError("adapter read-back binding mismatch")
        if not isinstance(value.exists, bool):
            raise AdapterRegistrationError("read-back exists must be boolean")
        if value.exists and (
            value.payload_sha256 is None
            or value.declared_payload_sha256 is None
            or value.record_sha256 is None
        ):
            raise AdapterRegistrationError(
                "existing read-back must include payload and record hashes"
            )
        if not value.exists and (
            value.payload_sha256 is not None
            or value.declared_payload_sha256 is not None
            or value.record_sha256 is not None
        ):
            raise AdapterRegistrationError(
                "missing read-back cannot claim destination hashes"
            )
        if value.payload_sha256 is not None:
            try:
                _sha256("read-back payload_sha256", value.payload_sha256)
            except ValueError as exc:
                raise AdapterRegistrationError(str(exc)) from None
        if value.declared_payload_sha256 is not None:
            try:
                _sha256(
                    "read-back declared_payload_sha256",
                    value.declared_payload_sha256,
                )
            except ValueError as exc:
                raise AdapterRegistrationError(str(exc)) from None
        if value.record_sha256 is not None:
            try:
                _sha256("read-back record_sha256", value.record_sha256)
            except ValueError as exc:
                raise AdapterRegistrationError(str(exc)) from None
        try:
            _timestamp("observed_at", value.observed_at)
        except ValueError as exc:
            raise AdapterRegistrationError(str(exc)) from None
        if value.observed_at < execution.executed_at:
            raise AdapterRegistrationError(
                "adapter read-back predates destination execution"
            )
        return value

    @staticmethod
    def _validate_verification(
        value: Any,
        *,
        observed_at: str,
    ) -> AdapterVerification:
        if not isinstance(value, AdapterVerification):
            raise AdapterRegistrationError(
                "adapter verify must return AdapterVerification"
            )
        if not isinstance(value.verified, bool):
            raise AdapterRegistrationError("verified must be boolean")
        try:
            _safe_id("verification reason code", value.reason_code)
            _timestamp("verified_at", value.verified_at)
        except ValueError as exc:
            raise AdapterRegistrationError(str(exc)) from None
        if value.verified_at < observed_at:
            raise AdapterRegistrationError(
                "adapter verification predates destination read-back"
            )
        return value

    @staticmethod
    def _enforce_verified_read_back(
        binding: ActionBinding,
        read_back: AdapterReadBack,
        verification: AdapterVerification,
    ) -> AdapterVerification:
        """Prevent an adapter from affirming evidence that misses the binding."""

        if not verification.verified:
            return verification
        exact_destination_observed = (
            read_back.exists is True
            and read_back.payload_sha256 == binding.payload_sha256
            and read_back.declared_payload_sha256 == binding.payload_sha256
            and isinstance(read_back.record_sha256, str)
            and _SHA256_RE.fullmatch(read_back.record_sha256) is not None
        )
        if exact_destination_observed:
            return verification
        return AdapterVerification(
            verified=False,
            reason_code="destination_binding_mismatch",
            verified_at=verification.verified_at,
        )

    @staticmethod
    def _authorization(
        binding: ActionBinding,
        value: Any,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != _AUTHORIZATION_FIELDS:
            raise ActionAuthorizationError(
                "authorization runner returned an unsafe response"
            )
        if not isinstance(value.get("authorized"), bool):
            raise ActionAuthorizationError(
                "authorization runner returned an unsafe response"
            )
        try:
            authorization_id = _safe_id(
                "authorization id", value.get("authorization_id")
            )
        except ValueError:
            raise ActionAuthorizationError(
                "authorization runner returned an unsafe response"
            ) from None
        if (
            value.get("proposal_id") != binding.proposal_id
            or value.get("adapter_id") != binding.adapter_id
            or value.get("payload_sha256") != binding.payload_sha256
        ):
            raise ActionAuthorizationError("authorization binding mismatch")
        return {
            "authorization_id": authorization_id,
            "authorized": value["authorized"],
        }

    def execute(
        self,
        prepared: PreparedAction,
        *,
        approved_payload_sha256: str,
        authorization_runner: AuthorizationRunner | None = None,
    ) -> dict[str, Any]:
        """Run one exact action through authorization and independent proof."""

        if not isinstance(prepared, PreparedAction):
            raise TypeError("prepared must be a PreparedAction")
        binding = prepared.binding
        manifest, adapter = self.registry.resolve(binding.adapter_id)
        manifest_binding = (
            manifest.version,
            manifest.action_kind,
            manifest.risk,
            manifest.policy,
            manifest.approval_mode,
        )
        if manifest_binding != (
            binding.adapter_version,
            binding.action_kind,
            binding.risk,
            binding.policy,
            binding.approval_mode,
        ):
            raise ActionBindingError("prepared action manifest binding mismatch")
        try:
            internal_digest = canonical_action_sha256(
                manifest,
                prepared.payload_copy(),
            )
            approved_payload_sha256 = _sha256(
                "approved_payload_sha256", approved_payload_sha256
            )
        except (TypeError, ValueError) as exc:
            raise ActionBindingError(str(exc)) from None
        if (
            internal_digest != binding.payload_sha256
            or approved_payload_sha256 != binding.payload_sha256
        ):
            raise ActionBindingError("approved payload does not match exact action")
        expected_idempotency_key = deterministic_idempotency_key(
            proposal_id=binding.proposal_id,
            manifest=manifest,
            payload_sha256=binding.payload_sha256,
        )
        if binding.idempotency_key != expected_idempotency_key:
            raise ActionBindingError("prepared idempotency binding mismatch")

        runner = (
            self._authorization_runner
            if authorization_runner is None
            else authorization_runner
        )
        if not callable(runner):
            raise ActionAuthorizationError(
                "an authorization runner is required before execution"
            )

        effect_lock = threading.Lock()
        effect_started = False
        runner_active = True
        execution: AdapterExecution | None = None

        def execute_once() -> AdapterExecution:
            nonlocal effect_started, execution
            with effect_lock:
                if not runner_active:
                    raise ActionAuthorizationError(
                        "authorization runner invoked the effect outside its "
                        "active authorization window"
                    )
                if effect_started:
                    raise ActionAuthorizationError(
                        "authorization runner invoked the effect more than once"
                    )
                effect_started = True
            try:
                candidate = adapter.execute(prepared)
                execution = self._validate_execution(binding, candidate)
                return execution
            except VerifiedActionError:
                raise
            except Exception:
                raise ActionExecutionError("adapter execution failed") from None

        try:
            raw_authorization = runner(binding, execute_once)
        except (ActionExecutionError, AdapterRegistrationError):
            raise
        except Exception:
            if effect_started:
                raise ActionIndeterminateError(
                    "action may exist but authorization completion is indeterminate"
                ) from None
            raise ActionAuthorizationError("authorization runner failed") from None
        finally:
            with effect_lock:
                runner_active = False

        try:
            authorization = self._authorization(binding, raw_authorization)
        except ActionAuthorizationError:
            if effect_started:
                raise ActionIndeterminateError(
                    "action may exist but authorization response was invalid"
                ) from None
            raise
        if not authorization["authorized"]:
            if effect_started:
                raise ActionIndeterminateError(
                    "authorization denied after starting the effect"
                )
            raise ActionAuthorizationError("action was not authorized")
        if execution is None:
            if effect_started:
                raise ActionIndeterminateError(
                    "authorization returned while the effect was incomplete"
                )
            raise ActionAuthorizationError(
                "authorization did not invoke the bound effect"
            )

        try:
            read_back = self._validate_read_back(
                binding,
                execution,
                adapter.read_back(prepared),
            )
            verification = self._validate_verification(
                adapter.verify(prepared, read_back),
                observed_at=read_back.observed_at,
            )
            verification = self._enforce_verified_read_back(
                binding,
                read_back,
                verification,
            )
        except AdapterRegistrationError:
            raise
        except Exception:
            raise ActionVerificationError(
                "independent destination verification failed"
            ) from None

        proof: dict[str, Any] = {
            "schema_version": "globus.verified-action.proof/v1",
            "status": "verified" if verification.verified else "verification_failed",
            "verified": verification.verified,
            "proposal_id": binding.proposal_id,
            "adapter": {
                "id": manifest.id,
                "version": manifest.version,
                "action_kind": manifest.action_kind,
            },
            "controls": {
                "risk": manifest.risk,
                "policy": manifest.policy,
                "permissions": list(manifest.permissions),
                "approval_mode": manifest.approval_mode,
                "idempotency_strategy": manifest.idempotency_strategy,
                "read_back_mode": manifest.read_back_mode,
            },
            "payload_sha256": binding.payload_sha256,
            "idempotency_key": binding.idempotency_key,
            "authorization": authorization,
            "execution": {
                "effect_id": execution.effect_id,
                "outcome": execution.outcome,
                "executed_at": execution.executed_at,
            },
            "read_back": {
                "effect_id": read_back.effect_id,
                "exists": read_back.exists,
                "payload_sha256": read_back.payload_sha256,
                "declared_payload_sha256": read_back.declared_payload_sha256,
                "record_sha256": read_back.record_sha256,
                "observed_at": read_back.observed_at,
            },
            "verification": {
                "verified": verification.verified,
                "reason_code": verification.reason_code,
                "verified_at": verification.verified_at,
            },
        }
        # Round-tripping creates an immutable-by-convention safe copy and also
        # asserts that no adapter-specific Python object leaked into the proof.
        safe_proof = json.loads(canonical_json_bytes(proof).decode("utf-8"))
        if self._audit_sink is not None:
            try:
                self._audit_sink(
                    json.loads(canonical_json_bytes(safe_proof).decode("utf-8"))
                )
            except Exception:
                raise ActionAuditError(
                    "action completed but its proof sink failed"
                ) from None
        return safe_proof


__all__ = [
    "APPROVAL_MODES",
    "ActionAuditError",
    "ActionAuthorizationError",
    "ActionBinding",
    "ActionBindingError",
    "ActionExecutionError",
    "ActionIndeterminateError",
    "ActionManifest",
    "ActionVerificationError",
    "AdapterExecution",
    "AdapterReadBack",
    "AdapterRegistrationError",
    "AdapterRegistry",
    "AdapterVerification",
    "IDEMPOTENCY_STRATEGIES",
    "KNOWN_ACTION_KINDS",
    "MANIFEST_FIELDS",
    "ManifestValidationError",
    "PreparedAction",
    "READ_BACK_MODES",
    "SAFE_PERMISSIONS",
    "VerifiedActionError",
    "VerifiedActionSDK",
    "canonical_action_sha256",
    "canonical_json_bytes",
    "canonical_payload_sha256",
    "deterministic_idempotency_key",
]
