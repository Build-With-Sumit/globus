"""Source-backed tests for the standalone Globus platform registry."""
from __future__ import annotations

import ast
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import re
import unittest

from globus_truth.platform_registry import (
    CAPABILITY_STATUSES,
    RegistryValidationError,
    get_platform_graph,
    get_platform_summary,
    list_capabilities,
    load_platform_registry,
    validate_platform_registry,
)


ROOT = Path(__file__).resolve().parents[2]


def _module_tree(relative_path: str) -> ast.Module:
    path = ROOT / relative_path
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _assignment(tree: ast.Module, name: str) -> ast.AST:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            ):
                return node.value
    raise AssertionError(f"assignment {name!r} not found")


def _agent_names_from_source() -> list[str]:
    value = _assignment(
        _module_tree("server/globus_agents_catalog.py"),
        "GLOBUS_AGENTS_CATALOG",
    )
    assert isinstance(value, ast.List)
    names = []
    for item in value.elts:
        assert isinstance(item, ast.Dict)
        pairs = {
            ast.literal_eval(key): field
            for key, field in zip(item.keys, item.values)
        }
        names.append(ast.literal_eval(pairs["name"]))
    return names


def _agent_tool_grants_from_source() -> dict[str, set[str]]:
    value = _assignment(
        _module_tree("server/globus_agents_catalog.py"),
        "GLOBUS_AGENTS_CATALOG",
    )
    assert isinstance(value, ast.List)
    grants: dict[str, set[str]] = {}
    for item in value.elts:
        assert isinstance(item, ast.Dict)
        pairs = {
            ast.literal_eval(key): field
            for key, field in zip(item.keys, item.values)
        }
        name = ast.literal_eval(pairs["name"])
        grants[name] = set(ast.literal_eval(pairs["tool_allowlist"]))
    return grants


def _tool_names_from_source() -> list[str]:
    value = _assignment(
        _module_tree("server/globus_tools_schema.py"),
        "GLOBUS_TOOLS",
    )
    tools = ast.literal_eval(value)
    return [item["function"]["name"] for item in tools]


def _provider_adapters_from_source() -> list[tuple[str, str, str]]:
    rows = []
    for path in sorted((ROOT / "server" / "narada_plugins").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "PluginInfo"
            ):
                continue
            keywords = {
                keyword.arg: keyword.value
                for keyword in node.keywords
                if keyword.arg is not None
            }
            if not {"name", "category", "auth_method"} <= keywords.keys():
                continue
            name = ast.literal_eval(keywords["name"])
            category_node = keywords["category"]
            auth_node = keywords["auth_method"]
            assert isinstance(category_node, ast.Attribute)
            assert isinstance(auth_node, ast.Attribute)
            rows.append((
                name,
                category_node.attr.lower(),
                auth_node.attr.lower(),
            ))
    return rows


class PlatformRegistryTests(unittest.TestCase):
    def test_summary_is_honest_and_explicit_about_setup(self):
        summary = get_platform_summary()
        self.assertEqual(summary["release"], "0.14.0")
        self.assertEqual(summary["headline"]["built_in_agents"], 4)
        self.assertEqual(summary["headline"]["llm_tools"], 20)
        self.assertEqual(
            summary["headline"]["implemented_provider_adapters"], 33
        )
        self.assertEqual(
            summary["provider_adapter_categories"],
            {"lead_source": 9, "verifier": 8, "sender": 6, "crm": 10},
        )
        self.assertEqual(summary["total_capabilities"], 71)
        self.assertEqual(
            summary["by_status"],
            {
                "bridge/catalog": 4,
                "implemented/setup_required": 42,
                "native": 24,
                "planned": 1,
            },
        )
        self.assertIn("not claimed as connected or configured", summary["disclosure"])

    def test_agent_and_tool_claims_match_source_without_importing_server(self):
        registry = load_platform_registry()
        advertised_agents = {
            item["id"].removeprefix("agent.")
            for item in registry["capabilities"]
            if item["kind"] == "agent"
        }
        advertised_tools = {
            item["id"].removeprefix("tool.")
            for item in registry["capabilities"]
            if item["kind"] == "tool"
        }
        self.assertEqual(advertised_agents, set(_agent_names_from_source()))
        self.assertEqual(advertised_tools, set(_tool_names_from_source()))
        self.assertEqual(len(advertised_agents), 4)
        self.assertEqual(len(advertised_tools), 20)

    def test_graph_agent_tool_edges_match_enforced_catalog_grants(self):
        graph = get_platform_graph()
        actual: dict[str, set[str]] = {}
        for edge in graph["edges"]:
            if edge["relation"] != "uses":
                continue
            agent = edge["source"].removeprefix("agent.")
            tool = edge["target"].removeprefix("tool.")
            actual.setdefault(agent, set()).add(tool)
        self.assertEqual(actual, _agent_tool_grants_from_source())

    def test_provider_inventory_matches_every_plugin_info_in_source(self):
        source_rows = _provider_adapters_from_source()
        auth_to_setup = {
            "api_key": "api_key",
            "composio": "managed_oauth",
            "oauth_custom": "custom_oauth",
        }
        expected = {
            (name, category, auth_to_setup[auth])
            for name, category, auth in source_rows
        }
        advertised = {
            (
                item["id"].removeprefix("provider."),
                item["category"],
                item["setup"],
            )
            for item in list_capabilities(kind="provider_adapter")
        }
        self.assertEqual(advertised, expected)
        self.assertEqual(len(source_rows), 33)
        self.assertEqual(
            Counter(category for _, category, _ in source_rows),
            {"lead_source": 9, "verifier": 8, "sender": 6, "crm": 10},
        )

    def test_registry_contains_no_connection_state_or_secret_values(self):
        registry = load_platform_registry()
        encoded = json.dumps(registry, sort_keys=True)
        forbidden_value_patterns = [
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            r"\bsk-[A-Za-z0-9_-]{16,}\b",
            r"\bghp_[A-Za-z0-9]{20,}\b",
            r"\bAKIA[0-9A-Z]{16}\b",
            r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}",
        ]
        for pattern in forbidden_value_patterns:
            self.assertIsNone(re.search(pattern, encoded, re.IGNORECASE))
        for capability in registry["capabilities"]:
            self.assertNotIn("connected", capability)
            self.assertNotIn("configured", capability)
            self.assertNotIn("credential", capability)
            source_path = capability["source"]["path"]
            self.assertFalse(Path(source_path).is_absolute())
            self.assertNotIn("..", Path(source_path).parts)
            self.assertTrue((ROOT / source_path).is_file())

    def test_validator_rejects_contract_drift_and_unsafe_metadata(self):
        base = load_platform_registry()
        mutations = {
            "schema version": lambda data: data.__setitem__(
                "schema_version", "2.0"
            ),
            "unknown top-level field": lambda data: data.__setitem__(
                "connected", True
            ),
            "duplicate ID": lambda data: data["capabilities"][1].__setitem__(
                "id", data["capabilities"][0]["id"]
            ),
            "invalid kind": lambda data: data["capabilities"][0].__setitem__(
                "kind", "integration"
            ),
            "invalid status": lambda data: data["capabilities"][0].__setitem__(
                "status", "connected"
            ),
            "invalid risk": lambda data: data["capabilities"][0].__setitem__(
                "risk", "critical"
            ),
            "invalid approval": lambda data: data["capabilities"][0].__setitem__(
                "approval", "automatic"
            ),
            "invalid readback": lambda data: data["capabilities"][0].__setitem__(
                "readback", "assumed"
            ),
            "unsafe source": lambda data: data["capabilities"][0]["source"].__setitem__(
                "path", "../private.env"
            ),
            "secret value": lambda data: data["capabilities"][0].__setitem__(
                "description", "Bearer abcdefghijklmnopqrstuvwxyz012345"
            ),
            "false count": lambda data: data["claims"].__setitem__(
                "llm_tools", 21
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                candidate = deepcopy(base)
                mutate(candidate)
                with self.assertRaises(RegistryValidationError):
                    validate_platform_registry(candidate)

    def test_filters_are_strict_and_results_are_defensive_copies(self):
        native_agents = list_capabilities(kind="agent", status="native")
        self.assertEqual(len(native_agents), 4)
        self.assertEqual(
            len(list_capabilities(category="sender")),
            6,
        )
        without_planned = list_capabilities(include_planned=False)
        self.assertTrue(
            all(item["status"] != "planned" for item in without_planned)
        )
        native_agents[0]["name"] = "mutated"
        self.assertNotEqual(
            list_capabilities(kind="agent")[0]["name"],
            "mutated",
        )
        with self.assertRaises(ValueError):
            list_capabilities(kind="imaginary")
        with self.assertRaises(ValueError):
            list_capabilities(status="connected")
        with self.assertRaises(TypeError):
            list_capabilities(include_planned=1)

    def test_graph_has_unique_nodes_and_no_dangling_edges(self):
        graph = get_platform_graph()
        node_ids = [node["id"] for node in graph["nodes"]]
        edge_ids = [edge["id"] for edge in graph["edges"]]
        self.assertEqual(len(node_ids), len(set(node_ids)))
        self.assertEqual(len(edge_ids), len(set(edge_ids)))
        known = set(node_ids)
        for edge in graph["edges"]:
            self.assertIn(edge["source"], known)
            self.assertIn(edge["target"], known)
            self.assertIn(edge["relation"], {"contains", "uses"})
        statuses = {
            node["status"]
            for node in graph["nodes"]
            if node["node_type"] == "capability"
        }
        self.assertEqual(statuses, CAPABILITY_STATUSES)


if __name__ == "__main__":
    unittest.main()
