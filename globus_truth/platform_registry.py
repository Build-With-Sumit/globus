"""Credential-free inventory of the capabilities shipped with Globus.

The registry is deliberately data-only.  Loading it never imports ``server``
modules, starts a connector, reads environment variables, or inspects member
configuration.  A capability marked ``implemented/setup_required`` therefore
means that an adapter exists in this repository; it does *not* mean that an
operator has configured or connected it.

Mission Control can consume the three projections exposed here:

``get_platform_summary()``
    Honest headline counts and status/category roll-ups.
``list_capabilities()``
    A filterable, defensive copy of the inventory.
``get_platform_graph()``
    Safe nodes and edges for a capability map.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping


REGISTRY_SCHEMA_VERSION = "1.0"
REGISTRY_FILENAME = "platform-registry-v1.json"

CAPABILITY_KINDS = frozenset({
    "agent",
    "tool",
    "provider_adapter",
    "connector",
    "channel",
    "model_route",
})
CAPABILITY_STATUSES = frozenset({
    "native",
    "implemented/setup_required",
    "bridge/catalog",
    "planned",
})
RISK_LEVELS = frozenset({"low", "medium", "high"})
APPROVAL_MODES = frozenset({"none", "operator_trigger", "explicit"})
READBACK_MODES = frozenset({
    "none",
    "source_cited",
    "operation_result",
    "provider_response",
    "truth_receipt",
})
SETUP_MODES = frozenset({
    "none",
    "api_key",
    "managed_oauth",
    "custom_oauth",
    "google_oauth",
    "operator_bridge",
    "external_service",
    "not_available",
})

_CATEGORIES_BY_KIND = {
    "agent": frozenset({"automation"}),
    "tool": frozenset({
        "knowledge",
        "communication",
        "agent_control",
        "memory",
        "web",
        "outbound",
    }),
    "provider_adapter": frozenset({
        "lead_source",
        "verifier",
        "sender",
        "crm",
    }),
    "connector": frozenset({"knowledge_source", "message_source"}),
    "channel": frozenset({"chat", "voice", "notification"}),
    "model_route": frozenset({"llm"}),
}
_TOP_LEVEL_FIELDS = frozenset({
    "schema_version",
    "registry_id",
    "release",
    "description",
    "status_definitions",
    "claims",
    "capabilities",
})
_CAPABILITY_FIELDS = frozenset({
    "id",
    "kind",
    "category",
    "name",
    "description",
    "status",
    "setup",
    "risk",
    "approval",
    "readback",
    "source",
})
_SOURCE_FIELDS = frozenset({"path", "symbol"})
_CLAIM_FIELDS = frozenset({
    "built_in_agents",
    "llm_tools",
    "implemented_provider_adapters",
    "provider_adapter_categories",
})
_EXPECTED_HEADLINE_COUNTS = {
    "built_in_agents": 4,
    "llm_tools": 20,
    "implemented_provider_adapters": 33,
}
_EXPECTED_PROVIDER_COUNTS = {
    "lead_source": 9,
    "verifier": 8,
    "sender": 6,
    "crm": 10,
}
_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_RELEASE_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}", re.IGNORECASE),
)

# These relationships mirror the built-in agents' enforced ``tool_allowlist``
# fields. They remain static so registry loading never imports server modules or
# evaluates runtime configuration; a source-backed test prevents drift.
_AGENT_TOOL_RELATIONS = {
    "agent.research": (
        "tool.search_files",
        "tool.read_file",
        "tool.search_content",
        "tool.list_recent_emails",
        "tool.search_whatsapp",
        "tool.search_telegram",
    ),
    "agent.sales-desk": (
        "tool.search_files",
        "tool.read_file",
        "tool.search_content",
        "tool.list_recent_emails",
    ),
    "agent.narada": (
        "tool.narada_list_campaigns",
        "tool.narada_campaign_stats",
        "tool.narada_check_replies",
    ),
    "agent.infra-watch": (
        "tool.search_files",
        "tool.read_file",
        "tool.search_content",
        "tool.list_recent_emails",
    ),
}


class RegistryValidationError(ValueError):
    """Raised when the platform registry violates its public contract."""


def _fail(message: str) -> None:
    raise RegistryValidationError(message)


def _require_exact_fields(
    value: Mapping[str, Any],
    expected: frozenset[str],
    location: str,
) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        _fail(f"{location} fields invalid; missing={missing}, extra={extra}")


def _require_text(value: Any, location: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{location} must be a non-empty string")
    if value != value.strip():
        _fail(f"{location} must not have surrounding whitespace")
    if len(value) > maximum:
        _fail(f"{location} exceeds {maximum} characters")
    return value


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_source(source: Any, location: str) -> None:
    if not isinstance(source, Mapping):
        _fail(f"{location} must be an object")
    _require_exact_fields(source, _SOURCE_FIELDS, location)
    path = _require_text(source["path"], f"{location}.path", maximum=200)
    symbol = _require_text(source["symbol"], f"{location}.symbol", maximum=200)
    pure_path = PurePosixPath(path)
    if "\\" in path or pure_path.is_absolute() or ".." in pure_path.parts:
        _fail(f"{location}.path must be a safe repository-relative POSIX path")
    if not path.startswith("server/") or not path.endswith(".py"):
        _fail(f"{location}.path must identify a server Python source file")
    if "\n" in symbol or "\r" in symbol:
        _fail(f"{location}.symbol must be one line")


def _scan_for_secret_values(value: Any, location: str = "registry") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in {
                "password",
                "secret",
                "token",
                "credential",
                "credential_value",
                "api_key_value",
            }:
                _fail(f"{location}.{key} may not contain credential material")
            _scan_for_secret_values(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_for_secret_values(child, f"{location}[{index}]")
    elif isinstance(value, str):
        for pattern in _SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                _fail(f"{location} appears to contain credential material")


def validate_platform_registry(registry: Any) -> None:
    """Strictly validate a decoded platform registry.

    The validator rejects unknown fields so that a future edit cannot quietly
    smuggle connection state or credential values into Mission Control.
    """
    if not isinstance(registry, Mapping):
        _fail("registry must be an object")
    _require_exact_fields(registry, _TOP_LEVEL_FIELDS, "registry")

    if registry["schema_version"] != REGISTRY_SCHEMA_VERSION:
        _fail(
            "registry.schema_version must be "
            f"{REGISTRY_SCHEMA_VERSION!r}"
        )
    registry_id = _require_text(
        registry["registry_id"], "registry.registry_id", maximum=100
    )
    if not _ID_RE.fullmatch(registry_id):
        _fail("registry.registry_id is not a valid stable ID")
    release = _require_text(registry["release"], "registry.release", maximum=20)
    if not _RELEASE_RE.fullmatch(release):
        _fail("registry.release must use semantic x.y.z form")
    _require_text(
        registry["description"], "registry.description", maximum=1000
    )

    definitions = registry["status_definitions"]
    if not isinstance(definitions, Mapping):
        _fail("registry.status_definitions must be an object")
    if frozenset(definitions) != CAPABILITY_STATUSES:
        _fail("registry.status_definitions must define every supported status")
    for status, description in definitions.items():
        _require_text(
            description,
            f"registry.status_definitions.{status}",
            maximum=500,
        )

    claims = registry["claims"]
    if not isinstance(claims, Mapping):
        _fail("registry.claims must be an object")
    _require_exact_fields(claims, _CLAIM_FIELDS, "registry.claims")
    for field, expected in _EXPECTED_HEADLINE_COUNTS.items():
        value = claims[field]
        if not _plain_int(value) or value != expected:
            _fail(f"registry.claims.{field} must equal verified count {expected}")
    provider_claims = claims["provider_adapter_categories"]
    if not isinstance(provider_claims, Mapping):
        _fail("registry.claims.provider_adapter_categories must be an object")
    if dict(provider_claims) != _EXPECTED_PROVIDER_COUNTS:
        _fail(
            "registry.claims.provider_adapter_categories must equal "
            f"{_EXPECTED_PROVIDER_COUNTS}"
        )

    capabilities = registry["capabilities"]
    if not isinstance(capabilities, list):
        _fail("registry.capabilities must be an array")
    if not capabilities:
        _fail("registry.capabilities must not be empty")

    ids: set[str] = set()
    provider_pairs: set[tuple[str, str]] = set()
    kind_counts: Counter[str] = Counter()
    provider_counts: Counter[str] = Counter()

    for index, capability in enumerate(capabilities):
        location = f"registry.capabilities[{index}]"
        if not isinstance(capability, Mapping):
            _fail(f"{location} must be an object")
        _require_exact_fields(capability, _CAPABILITY_FIELDS, location)

        capability_id = _require_text(
            capability["id"], f"{location}.id", maximum=100
        )
        if not _ID_RE.fullmatch(capability_id):
            _fail(f"{location}.id is not a valid stable ID")
        if capability_id in ids:
            _fail(f"duplicate capability ID: {capability_id}")
        ids.add(capability_id)

        kind = capability["kind"]
        if kind not in CAPABILITY_KINDS:
            _fail(f"{location}.kind is invalid")
        expected_prefix = {
            "agent": "agent.",
            "tool": "tool.",
            "provider_adapter": "provider.",
            "connector": "connector.",
            "channel": "channel.",
            "model_route": "model.",
        }[kind]
        if not capability_id.startswith(expected_prefix):
            _fail(f"{location}.id must start with {expected_prefix!r}")

        category = capability["category"]
        if category not in _CATEGORIES_BY_KIND[kind]:
            _fail(f"{location}.category is invalid for kind {kind!r}")
        _require_text(capability["name"], f"{location}.name", maximum=100)
        _require_text(
            capability["description"],
            f"{location}.description",
            maximum=500,
        )

        status = capability["status"]
        setup = capability["setup"]
        risk = capability["risk"]
        approval = capability["approval"]
        readback = capability["readback"]
        if status not in CAPABILITY_STATUSES:
            _fail(f"{location}.status is invalid")
        if setup not in SETUP_MODES:
            _fail(f"{location}.setup is invalid")
        if risk not in RISK_LEVELS:
            _fail(f"{location}.risk is invalid")
        if approval not in APPROVAL_MODES:
            _fail(f"{location}.approval is invalid")
        if readback not in READBACK_MODES:
            _fail(f"{location}.readback is invalid")

        if status == "native" and setup != "none":
            _fail(f"{location}: native capabilities must use setup='none'")
        if (
            status == "implemented/setup_required"
            and setup in {"none", "not_available"}
        ):
            _fail(f"{location}: implemented adapters must declare setup")
        if (
            status == "bridge/catalog"
            and setup not in {"operator_bridge", "external_service"}
        ):
            _fail(f"{location}: bridge/catalog setup is inconsistent")
        if status == "planned":
            if setup != "not_available":
                _fail(f"{location}: planned capabilities are not available")
            if approval != "none" or readback != "none":
                _fail(f"{location}: planned capabilities cannot imply execution")
        if risk == "high" and approval != "explicit":
            _fail(f"{location}: high-risk capabilities require explicit approval")

        _validate_source(capability["source"], f"{location}.source")
        kind_counts[kind] += 1
        if kind == "provider_adapter":
            if status != "implemented/setup_required":
                _fail(f"{location}: provider adapters must be implemented/setup_required")
            pair = (category, capability_id)
            if pair in provider_pairs:
                _fail(f"duplicate provider adapter: {pair}")
            provider_pairs.add(pair)
            provider_counts[category] += 1

    recomputed = {
        "built_in_agents": kind_counts["agent"],
        "llm_tools": kind_counts["tool"],
        "implemented_provider_adapters": kind_counts["provider_adapter"],
    }
    for field, actual in recomputed.items():
        if claims[field] != actual:
            _fail(
                f"registry.claims.{field}={claims[field]} does not match "
                f"inventory count {actual}"
            )
    if dict(provider_counts) != _EXPECTED_PROVIDER_COUNTS:
        _fail(
            "provider adapter inventory does not match verified category "
            f"counts {_EXPECTED_PROVIDER_COUNTS}; got {dict(provider_counts)}"
        )

    _scan_for_secret_values(registry)


@lru_cache(maxsize=1)
def _load_default_registry() -> dict[str, Any]:
    path = Path(__file__).with_name(REGISTRY_FILENAME)
    raw = path.read_bytes()
    if len(raw) > 512_000:
        _fail("platform registry exceeds the 512 KB safety limit")
    try:
        registry = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryValidationError(
            f"platform registry is not valid UTF-8 JSON: {exc}"
        ) from exc
    validate_platform_registry(registry)
    return registry


def load_platform_registry() -> dict[str, Any]:
    """Load, validate, and return an isolated copy of the bundled registry."""
    return deepcopy(_load_default_registry())


def _validated_registry(registry: Mapping[str, Any] | None) -> dict[str, Any]:
    if registry is None:
        return load_platform_registry()
    validate_platform_registry(registry)
    return deepcopy(dict(registry))


def get_platform_summary(
    registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return safe counts for a Mission Control summary panel."""
    data = _validated_registry(registry)
    capabilities = data["capabilities"]
    by_kind = Counter(item["kind"] for item in capabilities)
    by_status = Counter(item["status"] for item in capabilities)
    provider_categories = Counter(
        item["category"]
        for item in capabilities
        if item["kind"] == "provider_adapter"
    )
    executable_now = sum(
        item["status"] in {"native", "implemented/setup_required"}
        for item in capabilities
    )
    return {
        "schema_version": data["schema_version"],
        "registry_id": data["registry_id"],
        "release": data["release"],
        "headline": deepcopy(data["claims"]),
        "total_capabilities": len(capabilities),
        "by_kind": dict(sorted(by_kind.items())),
        "by_status": {
            status: by_status.get(status, 0)
            for status in sorted(CAPABILITY_STATUSES)
        },
        "provider_adapter_categories": {
            category: provider_categories.get(category, 0)
            for category in _EXPECTED_PROVIDER_COUNTS
        },
        "executable_code_paths": executable_now,
        "disclosure": (
            "Counts describe code shipped in this repository. "
            "Setup-required entries are not claimed as connected or configured; "
            "bridge/catalog and planned entries are excluded from executable "
            "code-path totals."
        ),
    }


def list_capabilities(
    *,
    kind: str | None = None,
    status: str | None = None,
    category: str | None = None,
    include_planned: bool = True,
    registry: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return a filtered, defensive copy of capability records."""
    if kind is not None and kind not in CAPABILITY_KINDS:
        raise ValueError(f"unknown capability kind: {kind!r}")
    if status is not None and status not in CAPABILITY_STATUSES:
        raise ValueError(f"unknown capability status: {status!r}")
    if category is not None:
        _require_text(category, "category", maximum=100)
    if not isinstance(include_planned, bool):
        raise TypeError("include_planned must be bool")

    data = _validated_registry(registry)
    result = []
    for item in data["capabilities"]:
        if kind is not None and item["kind"] != kind:
            continue
        if status is not None and item["status"] != status:
            continue
        if category is not None and item["category"] != category:
            continue
        if not include_planned and item["status"] == "planned":
            continue
        result.append(deepcopy(item))
    return result


def get_platform_graph(
    registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, credential-free graph for Mission Control."""
    data = _validated_registry(registry)
    nodes: list[dict[str, Any]] = [{
        "id": "platform.globus",
        "label": "Globus",
        "node_type": "platform",
        "kind": "platform",
        "status": "native",
        "risk": "low",
    }]
    edges: list[dict[str, str]] = []

    for kind in sorted(CAPABILITY_KINDS):
        group_id = f"group.{kind}"
        nodes.append({
            "id": group_id,
            "label": kind.replace("_", " ").title(),
            "node_type": "group",
            "kind": kind,
            "status": "native",
            "risk": "low",
        })
        edges.append({
            "id": f"platform.globus->{group_id}",
            "source": "platform.globus",
            "target": group_id,
            "relation": "contains",
        })

    known_ids = {item["id"] for item in data["capabilities"]}
    for item in data["capabilities"]:
        nodes.append({
            "id": item["id"],
            "label": item["name"],
            "node_type": "capability",
            "kind": item["kind"],
            "category": item["category"],
            "status": item["status"],
            "risk": item["risk"],
            "approval": item["approval"],
            "readback": item["readback"],
        })
        edges.append({
            "id": f"group.{item['kind']}->{item['id']}",
            "source": f"group.{item['kind']}",
            "target": item["id"],
            "relation": "contains",
        })

    for agent_id, tool_ids in _AGENT_TOOL_RELATIONS.items():
        if agent_id not in known_ids:
            continue
        for tool_id in tool_ids:
            if tool_id not in known_ids:
                continue
            edges.append({
                "id": f"{agent_id}->{tool_id}",
                "source": agent_id,
                "target": tool_id,
                "relation": "uses",
            })

    return {
        "schema_version": data["schema_version"],
        "registry_id": data["registry_id"],
        "nodes": nodes,
        "edges": edges,
    }


# Concise aliases make the module pleasant for CLI or API adapters while the
# longer names remain explicit in documentation.
load_registry = load_platform_registry
validate_registry = validate_platform_registry
platform_summary = get_platform_summary
platform_graph = get_platform_graph

__all__ = [
    "APPROVAL_MODES",
    "CAPABILITY_KINDS",
    "CAPABILITY_STATUSES",
    "READBACK_MODES",
    "REGISTRY_SCHEMA_VERSION",
    "RISK_LEVELS",
    "RegistryValidationError",
    "get_platform_graph",
    "get_platform_summary",
    "list_capabilities",
    "load_platform_registry",
    "load_registry",
    "platform_graph",
    "platform_summary",
    "validate_platform_registry",
    "validate_registry",
]
