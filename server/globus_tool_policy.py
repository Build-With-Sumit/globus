"""Pure helpers for deny-by-default agent tool permissions.

Interactive member chat deliberately keeps the complete advertised tool
surface. Background agents, however, must carry an explicit
``tool_allowlist`` in their catalog entry. These helpers keep policy parsing
and schema filtering independent from database and provider implementations.
"""
from __future__ import annotations

from collections.abc import Mapping


class ToolPolicyError(ValueError):
    """Raised before a model call when a tool policy is malformed."""


def normalize_tool_allowlist(
    value,
    *,
    allow_none=False,
    require_nonempty=True,
    context="tool allowlist",
):
    """Return an immutable, validated set of exact tool names.

    ``None`` has a special meaning only for interactive chat: use the complete
    advertised tool set. Agent catalog entries are required to pass an
    explicit, non-empty collection so a missing declaration can never expand
    to full access by accident.
    """
    if value is None:
        if allow_none:
            return None
        raise ToolPolicyError(f"{context} is required")
    if isinstance(value, (str, bytes)) or not isinstance(
        value, (list, tuple, set, frozenset)
    ):
        raise ToolPolicyError(f"{context} must be a collection of tool names")

    normalized = []
    seen = set()
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip():
            raise ToolPolicyError(
                f"{context} entries must be non-empty, trimmed strings"
            )
        if item in seen:
            raise ToolPolicyError(f"{context} contains duplicate tool {item!r}")
        seen.add(item)
        normalized.append(item)
    if require_nonempty and not normalized:
        raise ToolPolicyError(f"{context} must not be empty")
    return frozenset(normalized)


def agent_tool_allowlist(agent):
    """Read one catalog entry's required, explicit runtime grant."""
    if not isinstance(agent, Mapping):
        raise ToolPolicyError("agent catalog entry must be an object")
    name = agent.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ToolPolicyError("agent catalog entry has no valid name")
    return normalize_tool_allowlist(
        agent.get("tool_allowlist"),
        context=f"agent {name!r} tool_allowlist",
    )


def select_tool_schemas(schemas, allowed_tools=None):
    """Filter provider schemas and return ``(schemas, effective_names)``.

    Selected schemas retain their original order. Unknown grants and
    duplicate or malformed schema names fail before the provider is called.
    """
    if not isinstance(schemas, (list, tuple)):
        raise ToolPolicyError("tool schemas must be a list or tuple")

    ordered = []
    by_name = {}
    for index, schema in enumerate(schemas):
        try:
            name = schema["function"]["name"]
        except (KeyError, TypeError):
            raise ToolPolicyError(
                f"tool schema at index {index} has no function name"
            ) from None
        if not isinstance(name, str) or not name or name != name.strip():
            raise ToolPolicyError(
                f"tool schema at index {index} has an invalid function name"
            )
        if name in by_name:
            raise ToolPolicyError(f"duplicate advertised tool schema {name!r}")
        by_name[name] = schema
        ordered.append(name)

    requested = normalize_tool_allowlist(
        allowed_tools,
        allow_none=True,
        require_nonempty=False,
        context="runtime tool allowlist",
    )
    if requested is None:
        effective = frozenset(ordered)
    else:
        unknown = sorted(requested - by_name.keys())
        if unknown:
            raise ToolPolicyError(
                "runtime tool allowlist contains unknown tool(s): "
                + ", ".join(unknown)
            )
        effective = requested

    return [by_name[name] for name in ordered if name in effective], effective


__all__ = [
    "ToolPolicyError",
    "agent_tool_allowlist",
    "normalize_tool_allowlist",
    "select_tool_schemas",
]
