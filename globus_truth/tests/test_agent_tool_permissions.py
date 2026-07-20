from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from globus_agents_catalog import GLOBUS_AGENTS_CATALOG  # noqa: E402
from globus_tool_policy import (  # noqa: E402
    ToolPolicyError,
    agent_tool_allowlist,
    select_tool_schemas,
)
from globus_tools_schema import GLOBUS_TOOLS  # noqa: E402


def _tool_names(schemas):
    return [item["function"]["name"] for item in schemas]


def _assistant_tool_call(name, arguments=None):
    return {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": f"call-{name}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments or {}),
                    },
                }],
            }
        }],
        "usage": {},
    }


def _assistant_text(text="done"):
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {},
    }


class AgentToolPolicyTests(unittest.TestCase):
    def test_all_four_agents_declare_exact_task_scoped_tools(self):
        actual = {
            item["name"]: agent_tool_allowlist(item)
            for item in GLOBUS_AGENTS_CATALOG
        }
        self.assertEqual(
            actual,
            {
                "research": frozenset({
                    "search_files",
                    "read_file",
                    "search_content",
                    "list_recent_emails",
                    "search_whatsapp",
                    "search_telegram",
                }),
                "sales-desk": frozenset({
                    "search_files",
                    "read_file",
                    "search_content",
                    "list_recent_emails",
                }),
                "narada": frozenset({
                    "narada_list_campaigns",
                    "narada_campaign_stats",
                    "narada_check_replies",
                }),
                "infra-watch": frozenset({
                    "search_files",
                    "read_file",
                    "search_content",
                    "list_recent_emails",
                }),
            },
        )
        advertised = set(_tool_names(GLOBUS_TOOLS))
        self.assertEqual(len(advertised), 20)
        for grant in actual.values():
            self.assertTrue(grant)
            self.assertLessEqual(grant, advertised)

    def test_background_agents_do_not_inherit_consequential_or_recursive_tools(self):
        forbidden = {
            "run_agent",
            "save_preference",
            "delete_preference",
            "send_telegram_via_bot",
            "narada_create_campaign",
            "narada_find_leads",
            "narada_draft_copy",
            "narada_send_campaign",
        }
        for agent in GLOBUS_AGENTS_CATALOG:
            self.assertTrue(agent_tool_allowlist(agent).isdisjoint(forbidden))

    def test_schema_selection_preserves_order_and_none_means_interactive_full_set(self):
        all_selected, all_names = select_tool_schemas(GLOBUS_TOOLS, None)
        self.assertEqual(_tool_names(all_selected), _tool_names(GLOBUS_TOOLS))
        self.assertEqual(len(all_names), 20)

        selected, names = select_tool_schemas(
            GLOBUS_TOOLS, ["read_file", "search_files"]
        )
        self.assertEqual(_tool_names(selected), ["search_files", "read_file"])
        self.assertEqual(names, frozenset({"search_files", "read_file"}))

        selected, names = select_tool_schemas(GLOBUS_TOOLS, [])
        self.assertEqual(selected, [])
        self.assertEqual(names, frozenset())

    def test_malformed_missing_duplicate_and_unknown_grants_fail_closed(self):
        with self.assertRaises(ToolPolicyError):
            agent_tool_allowlist({"name": "custom"})
        with self.assertRaises(ToolPolicyError):
            agent_tool_allowlist({
                "name": "custom",
                "tool_allowlist": ["search_files", "search_files"],
            })
        with self.assertRaises(ToolPolicyError):
            select_tool_schemas(GLOBUS_TOOLS, ["not_a_real_tool"])


class OrchestratorToolBoundaryTests(unittest.TestCase):
    MODULE_NAMES = (
        "db_helpers",
        "globus_llm",
        "globus_web_read",
        "globus_vault_db",
        "globus_search",
        "sync_gmail",
        "globus_chat_helpers",
        "agent_runner",
        "telegram_bot",
        "narada_core",
        "narada_plugins",
        "narada_plugins.types",
    )

    def setUp(self):
        self.old_modules = {
            name: sys.modules.get(name) for name in self.MODULE_NAMES
        }
        self.db_write = Mock(return_value=True)
        self.search_files = Mock(return_value=[{"file_id": 1}])
        self.send_telegram = Mock(return_value={"ok": True})
        self.run_agent = Mock(return_value={"ok": True})
        self.llm = Mock()

        db_helpers = types.ModuleType("db_helpers")
        db_helpers.db_read = Mock(return_value=[])
        db_helpers.db_write = self.db_write

        globus_llm = types.ModuleType("globus_llm")

        def call_chat(system, messages, max_tokens=2000, tools=None):
            return self.llm(
                system, messages, max_tokens=max_tokens, tools=tools
            )

        globus_llm.globus_call_chat = call_chat

        globus_web_read = types.ModuleType("globus_web_read")
        globus_web_read.web_read = Mock(return_value={"body": "public"})

        vault_db = types.ModuleType("globus_vault_db")
        vault_db.globus_get_vault = Mock(return_value={})
        vault_db.globus_messages = Mock(return_value=[])
        vault_db.globus_log_message = Mock()

        search = types.ModuleType("globus_search")
        search.globus_search_files = self.search_files
        search.globus_search_content = Mock(return_value=[])
        search.globus_search_telegram = Mock(return_value=[])
        search.globus_search_whatsapp = Mock(return_value=[])

        sync_gmail = types.ModuleType("sync_gmail")
        sync_gmail.globus_freshen_gmail = Mock()

        helpers = types.ModuleType("globus_chat_helpers")
        helpers._globus_capabilities_block = Mock(return_value="")
        helpers._globus_tools_instructions = Mock(return_value="")
        helpers._strip_tool_markup = lambda text: text

        agent_runner = types.ModuleType("agent_runner")
        agent_runner.agent_run_async = self.run_agent

        telegram = types.ModuleType("telegram_bot")
        telegram.send_via_member_bot = self.send_telegram

        replacements = {
            "db_helpers": db_helpers,
            "globus_llm": globus_llm,
            "globus_web_read": globus_web_read,
            "globus_vault_db": vault_db,
            "globus_search": search,
            "sync_gmail": sync_gmail,
            "globus_chat_helpers": helpers,
            "agent_runner": agent_runner,
            "telegram_bot": telegram,
            # Force the optional Narada import onto its normal unavailable path.
            "narada_core": None,
            "narada_plugins": None,
            "narada_plugins.types": None,
        }
        sys.modules.update(replacements)

        path = SERVER / "globus_orchestrator.py"
        spec = importlib.util.spec_from_file_location(
            f"_orchestrator_policy_test_{id(self)}", path
        )
        assert spec is not None and spec.loader is not None
        self.orchestrator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.orchestrator)

    def tearDown(self):
        for name, old in self.old_modules.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old

    def _run(self, first_response, *, allowed_tools, **kwargs):
        self.llm.side_effect = [first_response, _assistant_text()]
        messages = [{"role": "user", "content": "run the task"}]
        result = self.orchestrator._run_tools_loop(
            "system",
            messages,
            "member@example.test",
            allowed_tools=allowed_tools,
            agent_name="research",
            **kwargs,
        )
        return result, messages

    def test_only_allowed_schemas_are_advertised_and_allowed_call_executes(self):
        (reply, _, called), _ = self._run(
            _assistant_tool_call("search_files", {"query": "incident"}),
            allowed_tools=["search_files"],
        )

        self.assertEqual(reply, "done")
        self.assertEqual(called[0]["name"], "search_files")
        self.search_files.assert_called_once()
        first_tools = self.llm.call_args_list[0].kwargs["tools"]
        self.assertEqual(_tool_names(first_tools), ["search_files"])
        self.assertIn(
            "Enforced runtime tool boundary",
            self.llm.call_args_list[0].args[0],
        )

    def test_forged_disallowed_call_is_rejected_before_side_effect(self):
        (_, _, called), messages = self._run(
            _assistant_tool_call(
                "send_telegram_via_bot",
                {"chat_id": 7, "text": "must not send"},
            ),
            allowed_tools=["search_files"],
        )

        self.assertEqual(called[0]["name"], "send_telegram_via_bot")
        self.send_telegram.assert_not_called()
        tool_result = json.loads(
            next(item["content"] for item in messages if item["role"] == "tool")
        )
        self.assertEqual(tool_result["error"], "tool_not_allowed")
        self.assertEqual(tool_result["context"], "agent:research")

    def test_empty_grant_still_blocks_a_hallucinated_tool_call(self):
        (_, _, _), messages = self._run(
            _assistant_tool_call("search_files", {"query": "secret"}),
            allowed_tools=[],
        )

        self.assertEqual(self.llm.call_args_list[0].kwargs["tools"], [])
        self.search_files.assert_not_called()
        tool_result = json.loads(
            next(item["content"] for item in messages if item["role"] == "tool")
        )
        self.assertEqual(tool_result["error"], "tool_not_allowed")

    def test_interactive_none_preserves_all_twenty_advertised_tools(self):
        self.llm.return_value = _assistant_text("interactive")
        self.orchestrator._run_tools_loop(
            "system", [], "member@example.test", allowed_tools=None
        )
        self.assertEqual(
            _tool_names(self.llm.call_args.kwargs["tools"]),
            _tool_names(GLOBUS_TOOLS),
        )

    def test_unknown_grant_fails_before_provider_call(self):
        with self.assertRaises(ToolPolicyError):
            self.orchestrator._run_tools_loop(
                "system",
                [],
                "member@example.test",
                allowed_tools=["not_a_real_tool"],
                agent_name="custom",
            )
        self.llm.assert_not_called()

    def test_agent_context_without_an_explicit_grant_fails_closed(self):
        with self.assertRaises(ToolPolicyError):
            self.orchestrator._run_tools_loop(
                "system",
                [],
                "member@example.test",
                allowed_tools=None,
                agent_name="research",
            )
        self.llm.assert_not_called()

    def test_run_agent_target_scope_is_rechecked_before_dispatch(self):
        (_, _, _), messages = self._run(
            _assistant_tool_call("run_agent", {"agent": "infra-watch"}),
            allowed_tools=["run_agent"],
            allowed_agent_names={"research"},
        )
        self.run_agent.assert_not_called()
        tool_result = json.loads(
            next(item["content"] for item in messages if item["role"] == "tool")
        )
        self.assertEqual(tool_result["error"], "agent_not_granted")

        self.llm.reset_mock()
        self.run_agent.reset_mock()
        self._run(
            _assistant_tool_call("run_agent", {"agent": "research"}),
            allowed_tools=["run_agent"],
            allowed_agent_names={"research"},
        )
        self.run_agent.assert_called_once_with(
            "research", "member@example.test"
        )


if __name__ == "__main__":
    unittest.main()
