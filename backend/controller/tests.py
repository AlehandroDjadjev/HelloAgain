from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase

from apps.accounts.models import AccountProfile
from apps.accounts.services import issue_token
from config.cors import LocalDevCorsMiddleware

from engine.custom_mcp_registry import CustomMcpRegistry
from engine.graph_service import GraphService
from engine.llm_parser import QwenPromptParser
from engine.semi_agent_service import SemiAgentService
from engine.user_context import ActiveUserTracker, TemporaryChatHistoryStore
from engine.whiteboard_memory import WhiteboardMemoryStore


def _assert_openai_object_schemas_are_strict(test_case: SimpleTestCase, schema: object) -> None:
    if isinstance(schema, dict):
        if str(schema.get("type", "")).strip().lower() == "object":
            test_case.assertIn("additionalProperties", schema)
            test_case.assertFalse(schema["additionalProperties"])
            properties = schema.get("properties", {})
            if isinstance(properties, dict):
                test_case.assertEqual(schema.get("required"), list(properties.keys()))
        for value in schema.values():
            _assert_openai_object_schemas_are_strict(test_case, value)
    elif isinstance(schema, list):
        for item in schema:
            _assert_openai_object_schemas_are_strict(test_case, item)


class CustomMcpRegistryTests(SimpleTestCase):
    def test_registry_exposes_gnn_actions_descriptor(self) -> None:
        registry = CustomMcpRegistry()
        payload = registry.load_registry(base_url="http://localhost:8000")

        self.assertEqual(payload["protocol"], "at_home_mcp")
        self.assertEqual(payload["mcps"][0]["id"], "gnn_actions")
        self.assertTrue(payload["mcps"][0]["descriptor_url"].endswith("/api/agent/mcps/gnn_actions/"))
        self.assertTrue(any(item["id"] == "connections" for item in payload["mcps"]))
        self.assertTrue(any(item["id"] == "phone_command" for item in payload["mcps"]))

        descriptor = registry.load_descriptor("gnn_actions", base_url="http://localhost:8000")
        self.assertEqual(descriptor["id"], "gnn_actions")
        self.assertEqual(len(descriptor["tools"]), 3)
        self.assertTrue(descriptor["invoke_url"].endswith("/api/agent/mcps/gnn_actions/invoke/"))

        connections_descriptor = registry.load_descriptor("connections", base_url="http://localhost:8000")
        self.assertEqual(connections_descriptor["id"], "connections")
        self.assertEqual(len(connections_descriptor["tools"]), 2)
        self.assertTrue(connections_descriptor["invoke_url"].endswith("/api/agent/mcps/connections/invoke/"))

        phone_command_descriptor = registry.load_descriptor("phone_command", base_url="http://localhost:8000")
        self.assertEqual(phone_command_descriptor["id"], "phone_command")
        self.assertEqual(len(phone_command_descriptor["tools"]), 1)
        self.assertTrue(phone_command_descriptor["invoke_url"].endswith("/api/agent/mcps/phone_command/invoke/"))
        self.assertEqual(phone_command_descriptor["tools"][0]["path"], "/api/agent/mcps/phone_command/invoke/")

    def test_service_builds_tool_catalog_from_registry_descriptors(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            (base_dir / "registry.json").write_text(
                json.dumps(
                    {
                        "protocol": "at_home_mcp",
                        "version": "1.0",
                        "mcps": [
                            {
                                "id": "custom_tools",
                                "name": "custom_tools",
                                "description": "Custom test MCP.",
                                "descriptor_file": "custom_tools.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (base_dir / "custom_tools.json").write_text(
                json.dumps(
                    {
                        "id": "custom_tools",
                        "tools": [
                            {
                                "name": "ping_status",
                                "method": "POST",
                                "path": "/api/custom/ping/",
                                "description": "Ping the custom MCP.",
                                "body_schema": {"prompt": "string"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            registry = CustomMcpRegistry(base_dir=base_dir)
            service = SemiAgentService(
                registry=registry,
                connections_service=SimpleNamespace(
                    save_board_state_for_user=lambda *args, **kwargs: None,
                    apply_board_commands_for_user=lambda *args, **kwargs: None,
                    build_user_widget_payload=lambda **kwargs: {},
                ),
            )

            catalog = service._build_mcp_tool_catalog()
            normalized_calls = service._normalize_mcp_calls(
                [
                    {
                        "tool_name": "ping_status",
                        "arguments": {"prompt": "check"},
                        "why": "Test custom tool routing.",
                    }
                ]
            )

            self.assertEqual(catalog["mcps"][0]["id"], "custom_tools")
            self.assertEqual(catalog["mcps"][0]["tools"][0]["name"], "ping_status")
            self.assertEqual(normalized_calls[0]["mcp_id"], "custom_tools")
            self.assertEqual(normalized_calls[0]["tool_name"], "ping_status")

    def test_stage_one_response_format_marks_all_object_schemas_as_strict(self) -> None:
        service = SemiAgentService(
            connections_service=SimpleNamespace(
                save_board_state_for_user=lambda *args, **kwargs: None,
                apply_board_commands_for_user=lambda *args, **kwargs: None,
                build_user_widget_payload=lambda **kwargs: {},
            ),
        )

        response_format = service._build_step_one_response_format(service._build_mcp_tool_catalog())

        self.assertEqual(response_format["type"], "json_schema")
        _assert_openai_object_schemas_are_strict(self, response_format["json_schema"]["schema"])
        arguments_schema = (
            response_format["json_schema"]["schema"]["properties"]["mcp_calls"]["items"]["properties"]["arguments"]
        )
        self.assertEqual(arguments_schema["required"], list(arguments_schema["properties"].keys()))
        self.assertEqual(arguments_schema["properties"]["user_id"]["type"], ["string", "null"])

    def test_stage_two_response_format_marks_all_object_schemas_as_strict(self) -> None:
        service = SemiAgentService(
            connections_service=SimpleNamespace(
                save_board_state_for_user=lambda *args, **kwargs: None,
                apply_board_commands_for_user=lambda *args, **kwargs: None,
                build_user_widget_payload=lambda **kwargs: {},
            ),
        )

        response_format = service._build_step_two_response_format(service._build_mcp_tool_catalog())

        self.assertEqual(response_format["type"], "json_schema")
        _assert_openai_object_schemas_are_strict(self, response_format["json_schema"]["schema"])
        action_enum = (
            response_format["json_schema"]["schema"]["properties"]["board_commands"]["items"]["properties"]["action"]["enum"]
        )
        self.assertIn("click object", action_enum)


class LocalDevCorsMiddlewareTests(SimpleTestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.middleware = LocalDevCorsMiddleware(lambda request: HttpResponse("ok"))

    def test_allows_localhost_preflight(self) -> None:
        request = self.factory.generic(
            "OPTIONS",
            "/api/agent/run/start/",
            HTTP_ORIGIN="http://localhost:51234",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
        )

        response = self.middleware(request)

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["Access-Control-Allow-Origin"], "http://localhost:51234")
        self.assertEqual(response["Access-Control-Allow-Methods"], "GET, POST, OPTIONS")

    def test_does_not_allow_non_local_origin(self) -> None:
        request = self.factory.get("/api/state/", HTTP_ORIGIN="https://example.com")

        response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Access-Control-Allow-Origin", response)


class WhiteboardMemoryStoreTests(SimpleTestCase):
    def test_persists_only_memory_objects(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = WhiteboardMemoryStore(memory_dir=Path(temp_dir))
            board_state = store.apply_commands(
                {
                    "board": {"width": 900, "height": 700},
                    "objects": [],
                },
                [
                    {
                        "action": "create",
                        "name": "instant_card",
                        "text": "instant",
                        "width": 200,
                        "height": 140,
                        "memoryType": "instant",
                    },
                    {
                        "action": "create",
                        "name": "saved_card",
                        "text": "saved",
                        "width": 220,
                        "height": 160,
                        "memoryType": "memory",
                        "resultId": "result_saved",
                    },
                ],
            )

            persisted = store.save_persistent_board_state(board_state)

            self.assertEqual(len(board_state["objects"]), 2)
            self.assertEqual(len(persisted["objects"]), 1)
            self.assertEqual(persisted["objects"][0]["name"], "saved_card")

    def test_preserves_object_extra_data(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = WhiteboardMemoryStore(memory_dir=Path(temp_dir))

            normalized = store.normalize_board_state(
                {
                    "board": {"width": 900, "height": 700},
                    "objects": [
                        {
                            "name": "real_user",
                            "text": "Best Match",
                            "width": 220,
                            "height": 160,
                            "memoryType": "memory",
                            "extraData": {
                                "kind": "user",
                                "user_id": 12,
                                "description": "Thoughtful and curious.",
                            },
                        }
                    ],
                }
            )

            self.assertEqual(normalized["objects"][0]["extraData"]["kind"], "user")
            self.assertEqual(normalized["objects"][0]["extraData"]["user_id"], 12)


class NavigationPageTests(SimpleTestCase):
    def test_home_page_uses_navigation_surface(self) -> None:
        response = self.client.get("/?prompt=Take%20me%20home")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Navigation")
        self.assertContains(response, "Take me home")
        self.assertNotContains(response, "Preference Graph Console")

    def test_navigation_api_redirects_to_navigation_page(self) -> None:
        response = self.client.post(
            "/api/agent/navigation/",
            data='{"prompt":"Take me to Central Park"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/?prompt=Take+me+to+Central+Park")

    def test_phone_command_open_redirects_to_flutter_deep_link(self) -> None:
        response = self.client.post(
            "/api/agent/phone-command/open/",
            data='{"prompt":"Open Chrome"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            "helloagain://phone-command?prompt=Open+Chrome",
        )


class SemiAgentServiceTests(SimpleTestCase):
    def test_opening_instant_object_returns_delete_command(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = WhiteboardMemoryStore(memory_dir=Path(temp_dir))
            service = SemiAgentService(board_memory=store)
            store.register_result_bindings(
                [
                    {
                        "result_id": "result_instant",
                        "object_name": "instant_widget",
                        "memory_type": "instant",
                        "delete_after_click": True,
                        "result_title": "Instant widget",
                        "result_summary": "One time result",
                        "payload": {"value": 1},
                    }
                ]
            )

            payload = service.open_board_object(
                object_payload={
                    "name": "instant_widget",
                    "resultId": "result_instant",
                    "memoryType": "instant",
                    "deleteAfterClick": True,
                }
            )

            self.assertTrue(payload["found"])
            self.assertEqual(payload["board_commands"][0]["action"], "delete")
            self.assertIsNone(store.resolve_result_binding("result_instant"))

    def test_manual_delete_removes_object_from_persistent_board_memory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = WhiteboardMemoryStore(memory_dir=Path(temp_dir))
            service = SemiAgentService(board_memory=store)
            board_state = store.apply_commands(
                {
                    "board": {"width": 900, "height": 700},
                    "objects": [],
                },
                [
                    {
                        "action": "create",
                        "name": "saved_card",
                        "text": "Saved",
                        "width": 220,
                        "height": 160,
                        "memoryType": "memory",
                        "resultId": "result_saved",
                    }
                ],
            )
            store.save_persistent_board_state(board_state)
            store.register_result_bindings(
                [
                    {
                        "result_id": "result_saved",
                        "object_name": "saved_card",
                        "memory_type": "memory",
                        "delete_after_click": False,
                        "result_title": "Saved card",
                        "payload": {"value": 1},
                    }
                ]
            )

            payload = service.delete_board_object(
                object_payload={
                    "name": "saved_card",
                    "resultId": "result_saved",
                    "memoryType": "memory",
                }
            )

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["board_commands"], [{"action": "delete", "name": "saved_card"}])
            self.assertEqual(store.load_persistent_board_state()["objects"], [])
            self.assertIsNone(store.resolve_result_binding("result_saved"))

    def test_save_board_memory_state_prunes_removed_result_binding(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = WhiteboardMemoryStore(memory_dir=Path(temp_dir))
            service = SemiAgentService(board_memory=store)
            store.register_result_bindings(
                [
                    {
                        "result_id": "result_saved",
                        "object_name": "saved_card",
                        "memory_type": "memory",
                        "delete_after_click": False,
                        "result_title": "Saved card",
                        "payload": {"value": 1},
                    }
                ]
            )

            payload = service.save_board_memory_state(
                board_state={
                    "board": {"width": 900, "height": 700},
                    "objects": [],
                },
                removed_result_id="result_saved",
            )

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["board_state"]["objects"], [])
            self.assertIsNone(store.resolve_result_binding("result_saved"))

    def test_default_focus_text_uses_compact_summary_title(self) -> None:
        service = SemiAgentService()

        title = service._default_focus_text(
            "show me something useful",
            [
                {
                    "tool_name": "fetch_action",
                    "summary": "Fetch chose Call Mom Soon.",
                    "result": {
                        "result": {
                            "name": "Call Mom Soon",
                        }
                    },
                }
            ],
        )

        self.assertEqual(title, "Call Mom Soon")

    def test_run_caps_each_board_pipeline_stage_to_256_tokens(self) -> None:
        class RecordingQwenClient:
            def __init__(self) -> None:
                self.calls = []

            def generate(self, *, system_prompt: str, user_prompt: str, generation_overrides=None) -> str:
                self.calls.append(
                    {
                        "system_prompt": system_prompt,
                        "user_prompt": user_prompt,
                        "generation_overrides": dict(generation_overrides or {}),
                    }
                )
                if len(self.calls) == 1:
                    return """
                    {
                      "stage": "step_1_mcp",
                      "needs_mcps": false,
                      "mcp_calls": []
                    }
                    """.strip()
                if len(self.calls) == 2:
                    return """
                    {
                      "stage": "step_2_board",
                      "cycle_back_to_step_one": false,
                      "memory_plan": {
                        "default_memory_type": "instant",
                        "why": "short lived result"
                      },
                      "focus_object": {
                        "name": "focus_widget",
                        "text": "Focus widget",
                        "width": 320,
                        "height": 220,
                        "memory_type": "instant",
                        "delete_after_click": true,
                        "linked_call_ids": [],
                        "result_title": "Focus widget",
                        "result_summary": ""
                      },
                      "board_commands": [],
                      "result_bindings": []
                    }
                    """.strip()
                return "Short final reply."

        with TemporaryDirectory() as temp_dir:
            store = WhiteboardMemoryStore(memory_dir=Path(temp_dir))
            qwen_client = RecordingQwenClient()
            service = SemiAgentService(board_memory=store, qwen_client=qwen_client)

            service.run(
                prompt="show me a focused board result",
                board_state={"board": {"width": 1000, "height": 700}, "objects": []},
                largest_empty_space={"bbox": {"x": 0, "y": 0, "width": 1000, "height": 700}},
            )

            self.assertEqual(len(qwen_client.calls), 2)
            self.assertEqual(qwen_client.calls[0]["generation_overrides"]["max_new_tokens"], 256)
            self.assertEqual(qwen_client.calls[1]["generation_overrides"]["max_new_tokens"], 256)
            self.assertEqual(qwen_client.calls[0]["generation_overrides"]["json_continuation_budget"], 0)
            self.assertEqual(qwen_client.calls[1]["generation_overrides"]["json_continuation_budget"], 0)

    def test_run_can_use_openai_for_reasoning_steps(self) -> None:
        class RecordingQwenClient:
            def __init__(self) -> None:
                self.calls = 0

            def generate(self, **kwargs) -> str:
                self.calls += 1
                return "{}"

        class RecordingLlmProvider:
            def __init__(self) -> None:
                self.calls = []

            def generate_reply_with_messages(self, **kwargs):
                self.calls.append(kwargs)
                call_index = len(self.calls)
                if call_index == 1:
                    return SimpleNamespace(
                        text='{"stage":"step_1_mcp","needs_mcps":false,"mcp_calls":[]}',
                        source="openai_chat_completions",
                        warnings=[],
                    )
                if call_index == 2:
                    return SimpleNamespace(
                        text=(
                            '{"stage":"step_2_board","cycle_back_to_step_one":false,'
                            '"memory_plan":{"default_memory_type":"memory","why":"kept"},'
                            '"focus_object":{"name":"openai_focus","text":"OpenAI focus",'
                            '"width":280,"height":180,"memory_type":"memory",'
                            '"delete_after_click":false,"linked_call_ids":[],'
                            '"result_title":"OpenAI focus","result_summary":""},'
                            '"board_commands":[],"result_bindings":[]}'
                        ),
                        source="openai_chat_completions",
                        warnings=[],
                    )
                return SimpleNamespace(
                    text="Здравей, подготвих резултата.",
                    source="openai_chat_completions",
                    warnings=[],
                )

        class MissingTtsProvider:
            def synthesize(self, text: str):
                raise RuntimeError("tts unavailable")

            def status(self) -> str:
                return "unavailable: test"

        with TemporaryDirectory() as temp_dir:
            store = WhiteboardMemoryStore(memory_dir=Path(temp_dir))
            qwen_client = RecordingQwenClient()
            llm_provider = RecordingLlmProvider()
            service = SemiAgentService(
                board_memory=store,
                qwen_client=qwen_client,
                llm_provider=llm_provider,
                tts_provider=MissingTtsProvider(),
            )

            payload = service.run(
                prompt="show me a focused board result",
                board_state={"board": {"width": 1000, "height": 700}, "objects": []},
                largest_empty_space={"bbox": {"x": 0, "y": 0, "width": 1000, "height": 700}},
                reasoning_provider="openai",
            )

            self.assertEqual(payload["reasoning_provider"], "openai")
            self.assertEqual(qwen_client.calls, 0)
            self.assertEqual(len(llm_provider.calls), 3)
            self.assertFalse(llm_provider.calls[0]["include_history"])
            self.assertFalse(llm_provider.calls[0]["store_history"])
            self.assertEqual(llm_provider.calls[0]["response_format"]["type"], "json_schema")
            self.assertEqual(llm_provider.calls[1]["response_format"]["type"], "json_schema")
            self.assertEqual(payload["step_two"]["focus_object"]["name"], "openai_focus")

    def test_generate_json_falls_back_to_openai_when_qwen_is_unreachable(self) -> None:
        class FailingQwenClient:
            def generate(self, **kwargs) -> str:
                raise RuntimeError("Could not reach the Qwen server.")

        class RecordingLlmProvider:
            def __init__(self) -> None:
                self.calls = []

            def generate_reply_with_messages(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    text='{"stage":"step_1_mcp","needs_mcps":false,"mcp_calls":[]}',
                    source="openai_chat_completions",
                    warnings=[],
                )

        service = SemiAgentService(
            qwen_client=FailingQwenClient(),
            llm_provider=RecordingLlmProvider(),
        )

        payload = service._generate_json(
            system_prompt="system",
            user_prompt="user",
            default_payload={"stage": "fallback"},
            reasoning_provider="qwen",
            user_id="guest_123",
            session_id="session_1",
        )

        self.assertEqual(payload["stage"], "step_1_mcp")
        self.assertEqual(payload["mcp_calls"], [])
        self.assertEqual(len(service.llm_provider.calls), 1)

    def test_generate_json_can_fail_loudly_when_default_fallback_is_disabled(self) -> None:
        class FailingQwenClient:
            def generate(self, **kwargs) -> str:
                raise RuntimeError("Could not reach the Qwen server.")

        class FailingLlmProvider:
            def generate_reply_with_messages(self, **kwargs):
                raise RuntimeError("planner unavailable")

        service = SemiAgentService(
            qwen_client=FailingQwenClient(),
            llm_provider=FailingLlmProvider(),
            connections_service=SimpleNamespace(
                save_board_state_for_user=lambda *args, **kwargs: None,
                apply_board_commands_for_user=lambda *args, **kwargs: None,
                build_user_widget_payload=lambda **kwargs: {},
            ),
        )

        with self.assertRaises(RuntimeError):
            service._generate_json(
                system_prompt="system",
                user_prompt="user",
                default_payload={"stage": "fallback"},
                response_format={"type": "json_object"},
                allow_default_fallback=False,
                reasoning_provider="openai",
                user_id="guest_123",
                session_id="session_1",
            )

    def test_run_speech_stage_survives_missing_tts(self) -> None:
        class FakeLlmProvider:
            def generate_reply_with_messages(self, **kwargs):
                return SimpleNamespace(
                    text="Здравей, подготвих резултата.",
                    source="fake_llm",
                    warnings=[],
                )

        class MissingTtsProvider:
            def synthesize(self, text: str):
                raise RuntimeError("tts unavailable")

            def status(self) -> str:
                return "unavailable: test"

        service = SemiAgentService(
            llm_provider=FakeLlmProvider(),
            tts_provider=MissingTtsProvider(),
        )

        payload = service._run_speech_stage(
            clean_prompt="кажи ми нещо полезно",
            step_one={},
            mcp_results=[],
            registry_payload={},
            user_context={
                "resolved_user_id": "user",
                "record_name": "user",
                "history_key": "user",
                "phone_number": "",
                "source": "test",
            },
            session_id="session",
        )

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["assistant_text"], "Здравей, подготвих резултата.")
        self.assertEqual(payload["assistant_audio_base64"], "")
        self.assertEqual(payload["provider_status"]["tts"], "unavailable: test")
        self.assertTrue(any("tts_unavailable=" in item for item in payload["warnings"]))

    def test_normalize_step_two_plan_replaces_structured_title_with_compact_name(self) -> None:
        service = SemiAgentService()

        payload = service._normalize_step_two_plan(
            {
                "focus_object": {
                    "name": "{\"payload\":\"huge\"}",
                    "text": "{\"linked_results\":[{\"result\":{\"name\":\"Call Mom Soon\"}}]}",
                    "result_title": "{\"detail\":\"Call Mom Soon\"}",
                },
                "board_commands": [],
                "result_bindings": [],
            },
            prompt="show me the best action",
            board_state={"board": {"width": 1000, "height": 700}, "objects": []},
            largest_empty_space={"bbox": {"x": 0, "y": 0, "width": 1000, "height": 700}},
            step_one={"memory_hint": "instant"},
            current_results=[
                {
                    "tool_name": "fetch_action",
                    "summary": "Fetch chose Call Mom Soon.",
                    "result": {"result": {"name": "Call Mom Soon"}},
                }
            ],
        )

        self.assertEqual(payload["focus_object"]["text"], "Call Mom Soon")
        self.assertEqual(payload["focus_object"]["name"], "call_mom_soon")
        self.assertEqual(payload["focus_object"]["result_title"], "Call Mom Soon")

    def test_normalize_step_two_plan_accepts_click_object_alias(self) -> None:
        service = SemiAgentService()

        payload = service._normalize_step_two_plan(
            {
                "focus_object": {
                    "name": "best_match",
                    "text": "Best Match",
                    "result_title": "Best Match",
                },
                "board_commands": [
                    {
                        "action": "click object",
                        "name": "best_match",
                    }
                ],
                "result_bindings": [],
            },
            prompt="open the object for me",
            board_state={"board": {"width": 1000, "height": 700}, "objects": []},
            largest_empty_space={"bbox": {"x": 0, "y": 0, "width": 1000, "height": 700}},
            step_one={"memory_hint": "instant"},
            current_results=[],
        )

        self.assertIn(
            {"action": "click", "name": "best_match"},
            payload["board_commands"],
        )

    def test_opening_user_object_returns_specialized_user_viewer(self) -> None:
        class FakeConnectionsService:
            def build_user_widget_payload(self, *, agent_user_id: str, target_user_id: int):
                return {
                    "widget_type": "user_profile",
                    "title": "Best Match",
                    "summary": "Reflective and kind.",
                    "user": {
                        "user_id": target_user_id,
                        "display_name": "Best Match",
                        "description": "Reflective and kind.",
                    },
                }

        with TemporaryDirectory() as temp_dir:
            store = WhiteboardMemoryStore(memory_dir=Path(temp_dir))
            service = SemiAgentService(
                board_memory=store,
                connections_service=FakeConnectionsService(),
            )
            store.register_result_bindings(
                [
                    {
                        "result_id": "result_user",
                        "object_name": "best_match",
                        "memory_type": "memory",
                        "delete_after_click": False,
                        "result_title": "Best Match",
                        "result_summary": "Reflective and kind.",
                        "payload": {
                            "linked_results": [
                                {
                                    "result": {
                                        "user": {
                                            "user_id": 42,
                                            "display_name": "Best Match",
                                        }
                                    }
                                }
                            ],
                            "object": {
                                "name": "best_match",
                                "text": "Best Match",
                                "extraData": {"kind": "user", "user_id": 42},
                            },
                        },
                    }
                ]
            )

            payload = service.open_board_object(
                object_payload={
                    "name": "best_match",
                    "resultId": "result_user",
                    "memoryType": "memory",
                    "extraData": {"kind": "user", "user_id": 42},
                },
                user_id="viewer",
            )

            self.assertTrue(payload["found"])
            self.assertEqual(payload["viewer"]["widget_type"], "user_profile")
            self.assertEqual(payload["viewer"]["user"]["user_id"], 42)

    def test_opening_user_object_does_not_delete_even_with_instant_binding(self) -> None:
        class FakeConnectionsService:
            def build_user_widget_payload(self, *, agent_user_id: str, target_user_id: int):
                return {
                    "widget_type": "user_connection",
                    "title": "Best Match",
                    "summary": "Reflective and kind.",
                    "user": {
                        "user_id": target_user_id,
                        "display_name": "Best Match",
                    },
                }

        with TemporaryDirectory() as temp_dir:
            store = WhiteboardMemoryStore(memory_dir=Path(temp_dir))
            service = SemiAgentService(
                board_memory=store,
                connections_service=FakeConnectionsService(),
            )
            store.register_result_bindings(
                [
                    {
                        "result_id": "result_user",
                        "object_name": "best_match",
                        "memory_type": "instant",
                        "delete_after_click": True,
                        "payload": {
                            "object": {
                                "name": "best_match",
                                "text": "Best Match",
                                "extraData": {"kind": "user", "user_id": 42},
                            },
                        },
                    }
                ]
            )

            payload = service.open_board_object(
                object_payload={
                    "name": "best_match",
                    "resultId": "result_user",
                    "memoryType": "instant",
                    "deleteAfterClick": True,
                    "extraData": {"kind": "user", "user_id": 42},
                },
                user_id="viewer",
            )

            self.assertTrue(payload["found"])
            self.assertEqual(payload["board_commands"], [])
            self.assertEqual(payload["viewer"]["widget_type"], "user_connection")
            self.assertIsNotNone(store.resolve_result_binding("result_user"))

    def test_phone_command_mcp_infers_single_tool_and_returns_launcher_metadata(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = WhiteboardMemoryStore(memory_dir=Path(temp_dir))
            service = SemiAgentService(board_memory=store)

            payload = service.invoke_mcp(
                mcp_id="phone_command",
                tool_name="",
                arguments={"prompt": "Open Chrome"},
            )

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["tool_name"], "open_phone_command")
            self.assertEqual(payload["result"]["widget_type"], "phone_command_launcher")
            self.assertEqual(payload["result"]["prompt"], "Open Chrome")
            self.assertEqual(payload["result"]["board_object"]["extra_data"]["kind"], "phone_command")
            self.assertEqual(payload["result"]["board_object"]["extra_data"]["prompt"], "Open Chrome")
            self.assertTrue(
                payload["result"]["open_url"].endswith("/api/agent/phone-command/open/?prompt=Open+Chrome")
            )

    def test_opening_phone_command_object_returns_launcher_viewer(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = WhiteboardMemoryStore(memory_dir=Path(temp_dir))
            service = SemiAgentService(board_memory=store)
            store.register_result_bindings(
                [
                    {
                        "result_id": "result_phone",
                        "object_name": "phone_command",
                        "memory_type": "instant",
                        "delete_after_click": True,
                        "result_title": "Phone Command",
                        "result_summary": "Open the prepared phone flow.",
                        "payload": {
                            "linked_results": [
                                {
                                    "result": {
                                        "widget_type": "phone_command_launcher",
                                        "prompt": "Open Chrome",
                                        "board_object": {
                                            "extra_data": {
                                                "kind": "phone_command",
                                                "prompt": "Open Chrome",
                                            }
                                        },
                                    }
                                }
                            ],
                            "object": {
                                "name": "phone_command",
                                "text": "Phone Command",
                                "extraData": {
                                    "kind": "phone_command",
                                    "prompt": "Open Chrome",
                                },
                            },
                        },
                    }
                ]
            )

            payload = service.open_board_object(
                object_payload={
                    "name": "phone_command",
                    "resultId": "result_phone",
                    "memoryType": "instant",
                    "deleteAfterClick": True,
                    "extraData": {
                        "kind": "phone_command",
                        "prompt": "Open Chrome",
                    },
                }
            )

            self.assertTrue(payload["found"])
            self.assertEqual(payload["board_commands"][0]["action"], "delete")
            self.assertEqual(payload["viewer"]["widget_type"], "phone_command_launcher")
            self.assertEqual(payload["viewer"]["prompt"], "Open Chrome")
            self.assertTrue(payload["viewer"]["auto_run_on_open"])

    def test_run_speech_stage_includes_recent_temp_history_and_short_mcp_guidance(self) -> None:
        class RecordingLlmProvider:
            def __init__(self) -> None:
                self.calls = []

            def generate_reply_with_messages(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    text="Кратък отговор.",
                    source="fake_llm",
                    warnings=[],
                )

        class MissingTtsProvider:
            def synthesize(self, text: str):
                raise RuntimeError("tts unavailable")

            def status(self) -> str:
                return "unavailable: test"

        with TemporaryDirectory() as temp_dir:
            tracker = ActiveUserTracker(base_dir=Path(temp_dir) / "runtime")
            history_store = TemporaryChatHistoryStore(base_dir=Path(temp_dir) / "history")
            llm_provider = RecordingLlmProvider()
            service = SemiAgentService(
                llm_provider=llm_provider,
                tts_provider=MissingTtsProvider(),
                user_tracker=tracker,
                speech_history_store=history_store,
            )
            user_context = tracker.resolve(user_id="+359 888 111 222")

            service._run_speech_stage(
                clean_prompt="Първо съобщение",
                step_one={},
                mcp_results=[],
                registry_payload={},
                user_context=user_context,
                session_id="session-a",
            )
            service._run_speech_stage(
                clean_prompt="Второ съобщение",
                step_one={"needs_mcps": True},
                mcp_results=[
                    {
                        "call_id": "connections.find_connection.1",
                        "mcp_id": "connections",
                        "tool_name": "find_connection",
                        "summary": "Found a close connection match: Mila.",
                    }
                ],
                registry_payload={
                    "mcps": [
                        {
                            "id": "connections",
                            "name": "connections",
                            "description": "Profile and people lookup.",
                        }
                    ]
                },
                user_context=user_context,
                session_id="session-a",
            )

            self.assertEqual(len(llm_provider.calls), 2)
            latest_call = llm_provider.calls[-1]
            self.assertFalse(latest_call["include_history"])
            self.assertFalse(latest_call["store_history"])
            prompt = latest_call["messages"][0]["content"]
            self.assertIn("recent_chat_history", prompt)
            self.assertIn("Първо съобщение", prompt)
            self.assertIn("Кратък отговор.", prompt)
            self.assertIn("keep the reply semi-short", prompt)

    def test_dispatch_gnn_tool_uses_lazy_graph_service(self) -> None:
        class FakeGraphService:
            def conversation_flow(self, prompt: str, *, user_id: str | None = None):
                return {
                    "mode": "conversation",
                    "prompt": prompt,
                    "user_id": user_id,
                }

        class RecordingService(SemiAgentService):
            def __init__(self) -> None:
                super().__init__(
                    graph_service=None,
                    connections_service=SimpleNamespace(
                        save_board_state_for_user=lambda *args, **kwargs: None,
                        apply_board_commands_for_user=lambda *args, **kwargs: None,
                        build_user_widget_payload=lambda **kwargs: {},
                    ),
                )
                self.lazy_calls = 0

            def _get_graph_service(self):
                self.lazy_calls += 1
                if self.graph_service is None:
                    self.graph_service = FakeGraphService()
                return self.graph_service

        service = RecordingService()

        payload = service._dispatch_gnn_tool(
            "conversation",
            "test prompt",
            user_id="guest_123",
        )

        self.assertEqual(service.lazy_calls, 1)
        self.assertEqual(payload["mode"], "conversation")
        self.assertEqual(payload["user_id"], "guest_123")

    def test_valid_step_one_gnn_mcp_payload_normalizes_and_dispatches(self) -> None:
        class FakeGraphService:
            def fetch_action_flow(self, prompt: str, *, user_id: str | None = None):
                return {
                    "mode": "fetch",
                    "prompt": prompt,
                    "user_id": user_id,
                    "message": "fetch ran",
                }

        service = SemiAgentService(
            graph_service=FakeGraphService(),
            connections_service=SimpleNamespace(
                save_board_state_for_user=lambda *args, **kwargs: None,
                apply_board_commands_for_user=lambda *args, **kwargs: None,
                build_user_widget_payload=lambda **kwargs: {},
            ),
        )

        raw_step_one = {
            "stage": "step_1_mcp",
            "step_number": 1,
            "chain_position": "mcp layer",
            "needs_mcps": True,
            "request_kind": "profile",
            "memory_hint": "memory",
            "reasoning_summary": "User shared a durable emotional state plus a positive activity that affected mood/energy, so profile/memory update is relevant.",
            "why_this_is_part_of_the_chain": "This is the first stage of the set chain.",
            "board_intent": "Step 2 should use the gnn result.",
            "speech_intent": "Step 3 should sound warm.",
            "mcp_calls": [
                {
                    "call_id": "gnn_actions.fetch_action.1",
                    "mcp_id": "gnn_actions",
                    "tool_name": "fetch_action",
                    "arguments": {
                        "prompt": (
                            "User reports feeling depressed for a couple of months, low energy, "
                            "but felt temporarily happy and energized after going on a hike today."
                        )
                    },
                    "why": "Needed to retrieve the action-memory guidance.",
                }
            ],
        }

        normalized = service._normalize_step_one_plan(raw_step_one, "user prompt")
        self.assertTrue(normalized["needs_mcps"])
        self.assertEqual(len(normalized["mcp_calls"]), 1)
        self.assertEqual(normalized["mcp_calls"][0]["mcp_id"], "gnn_actions")
        self.assertEqual(normalized["mcp_calls"][0]["tool_name"], "fetch_action")

        executed = service._execute_mcp_calls(
            normalized["mcp_calls"],
            "fallback prompt",
            user_id="guest_123",
            board_state={},
        )

        self.assertEqual(len(executed), 1)
        self.assertTrue(executed[0]["ok"])
        self.assertEqual(executed[0]["result"]["mode"], "fetch")

    def test_step_one_requires_explicit_valid_mcp_calls_when_needs_mcps_true(self) -> None:
        service = SemiAgentService()

        with self.assertRaises(ValueError):
            service._normalize_step_one_plan(
                {
                    "stage": "step_1_mcp",
                    "step_number": 1,
                    "chain_position": "mcp layer",
                    "needs_mcps": True,
                    "request_kind": "profile",
                    "memory_hint": "memory",
                    "reasoning_summary": "missing valid calls",
                    "why_this_is_part_of_the_chain": "missing valid calls",
                    "board_intent": "missing valid calls",
                    "speech_intent": "missing valid calls",
                    "mcp_calls": [],
                },
                "I have been depressed and hiking helped today. What should we remember or suggest?",
            )

    def test_step_one_preserves_explicit_connections_call(self) -> None:
        service = SemiAgentService()

        normalized = service._normalize_step_one_plan(
            {
                "stage": "step_1_mcp",
                "step_number": 1,
                "chain_position": "mcp layer",
                "needs_mcps": True,
                "request_kind": "profile",
                "memory_hint": "memory",
                "reasoning_summary": "find a real person",
                "why_this_is_part_of_the_chain": "find a real person",
                "board_intent": "find a real person",
                "speech_intent": "find a real person",
                "mcp_calls": [
                    {
                        "call_id": "connections.find_connection.1",
                        "mcp_id": "connections",
                        "tool_name": "find_connection",
                        "arguments": {
                            "prompt": "i want to find a person with whom i can go hicking and socialising outside. With who do i do?"
                        },
                        "why": "find a real person",
                    }
                ],
            },
            "i want to find a person with whom i can go hicking and socialising outside. With who do i do?",
        )

        self.assertTrue(normalized["needs_mcps"])
        self.assertEqual(len(normalized["mcp_calls"]), 1)
        self.assertEqual(normalized["mcp_calls"][0]["mcp_id"], "connections")
        self.assertEqual(normalized["mcp_calls"][0]["tool_name"], "find_connection")

    def test_default_mcp_calls_is_empty_without_planner_choice(self) -> None:
        service = SemiAgentService()

        calls = service._default_mcp_calls("anything", "profile")

        self.assertEqual(calls, [])


class GnnParserFallbackTests(SimpleTestCase):
    def test_parser_uses_openai_before_qwen_for_json_steps(self) -> None:
        class FailingQwenClient:
            def generate(self, **kwargs):
                raise AssertionError("Qwen should not be called when OpenAI succeeds.")

        class SequentialOpenAIProvider:
            def __init__(self) -> None:
                self.responses = [
                    (
                        '{"summary":"low energy state","user_state":{"new_attributes":{"low_energy":-0.7}},'
                        '"prompt_context":{"desired_attributes":{"relief":0.6}}}'
                    ),
                    (
                        '{"action_candidate":{"name":"short walk","wanted_strength":0.8,'
                        '"attribute_map":{"low_energy":-0.4},"desired_attribute_map":{"relief":0.7}},'
                        '"edge_signal":{"kind":"fetch","strength":0.75,"reason":"supportive match"}}'
                    ),
                ]
                self.calls = 0

            def generate_reply_with_messages(self, **kwargs):
                self.calls += 1
                return SimpleNamespace(text=self.responses.pop(0))

        provider = SequentialOpenAIProvider()
        parser = QwenPromptParser(
            qwen_client=FailingQwenClient(),
            llm_provider=provider,
        )

        plan = parser.parse(
            mode="fetch",
            user_prompt="I have been low on energy lately and need something supportive.",
            attribute_inventory_text="none",
            action_inventory_text="none",
        )

        self.assertEqual(provider.calls, 2)
        self.assertEqual(plan["mode"], "fetch")
        self.assertEqual(plan["user_state"]["new_attributes"][0]["name"], "low_energy")
        self.assertEqual(plan["action_candidate"]["name"], "short walk")
        self.assertEqual(plan["edge_signal"]["kind"], "fetch")


class GraphServiceUserScopingTests(TestCase):
    def test_graph_service_keeps_state_per_phone_number_and_uses_active_fallback(self) -> None:
        with TemporaryDirectory() as temp_dir:
            tracker = ActiveUserTracker(base_dir=Path(temp_dir) / "runtime")
            service = GraphService(user_tracker=tracker)

            first_user, first_context = service._resolve_user_profile(user_id="+359 888 111 222")
            second_user, _ = service._resolve_user_profile(user_id="+359 888 333 444")

            self.assertNotEqual(first_user.pk, second_user.pk)
            self.assertEqual(first_context["phone_number"], "+359888111222")

            attr = service._get_or_create_attribute("calm", user=first_user, initial_score=0.8)
            row = service._ensure_user_attribute(first_user, attr, 0.8)
            row.score = 0.8
            row.save(update_fields=["score", "updated_at"])

            self.assertEqual(service._current_user_vector(first_user), {"calm": 0.8})
            self.assertEqual(service._current_user_vector(second_user), {})

            fallback_user, fallback_context = service._resolve_user_profile(user_id="anonymous")
            self.assertEqual(fallback_user.pk, second_user.pk)
            self.assertEqual(fallback_context["source"], "active_fallback")

    def test_graph_service_scopes_actions_to_their_owner(self) -> None:
        with TemporaryDirectory() as temp_dir:
            tracker = ActiveUserTracker(base_dir=Path(temp_dir) / "runtime")
            service = GraphService(user_tracker=tracker)

            first_user, _ = service._resolve_user_profile(user_id="+359 888 111 222")
            second_user, _ = service._resolve_user_profile(user_id="+359 888 333 444")

            first_action = service._create_action_for_add(
                first_user,
                name="call_family",
                description="Check in with family.",
                base_summary="Family support",
                prompt_text="call family",
            )
            second_action = service._create_action_for_add(
                second_user,
                name="call_family",
                description="Different user action.",
                base_summary="Different summary",
                prompt_text="different user",
            )

            self.assertNotEqual(first_action.pk, second_action.pk)
            self.assertEqual(
                [item["name"] for item in service._action_inventory(first_user)],
                ["call_family"],
            )
            self.assertEqual(
                [item["name"] for item in service._action_inventory(second_user)],
                ["call_family"],
            )
            self.assertEqual(service._action_queryset(first_user).count(), 1)
            self.assertEqual(service._action_queryset(second_user).count(), 1)

    def test_fetch_flow_runs_with_generated_guest_user_when_qwen_is_down(self) -> None:
        class FailingQwenClient:
            def generate(self, **kwargs):
                raise RuntimeError("Could not reach the Qwen server.")

        class SequentialOpenAIProvider:
            def __init__(self) -> None:
                self.responses = [
                    (
                        '{"summary":"depressed for a while with some lift from hiking",'
                        '"user_state":{"new_attributes":{"depressed":-0.8,"low_energy":-0.6}},'
                        '"prompt_context":{"desired_attributes":{"hope":0.5},"opposite_attributes":{"energized":0.4}}}'
                    ),
                    (
                        '{"action_candidate":{"name":"hike again","wanted_strength":0.9,'
                        '"attribute_map":{"depressed":-0.4,"low_energy":-0.3},'
                        '"desired_attribute_map":{"hope":0.7,"energized":0.6}},'
                        '"edge_signal":{"kind":"fetch","strength":0.8,"reason":"recent positive activation"}}'
                    ),
                ]

            def generate_reply_with_messages(self, **kwargs):
                return SimpleNamespace(text=self.responses.pop(0))

        with TemporaryDirectory() as temp_dir:
            tracker = ActiveUserTracker(base_dir=Path(temp_dir) / "runtime")
            parser = QwenPromptParser(
                qwen_client=FailingQwenClient(),
                llm_provider=SequentialOpenAIProvider(),
            )
            service = GraphService(parser=parser, user_tracker=tracker)

            payload = service.fetch_action_flow(
                "I have felt depressed for months but hiking today helped a little.",
                user_id="anonymous",
            )

            self.assertEqual(payload["mode"], "fetch")
            self.assertTrue(payload["user_context"]["resolved_user_id"].startswith("guest_"))
            self.assertEqual(payload["message"], "No actions available yet.")
            self.assertEqual(payload["user"]["attributes"][0]["name"], "depressed")


class AgentBoardMemoryEndpointTests(TestCase):
    def test_agent_board_memory_endpoint_scopes_state_by_user_id(self) -> None:
        user = User.objects.create_user(username="guest_board_user")
        profile = AccountProfile.objects.create(
            user=user,
            display_name="Guest Board User",
        )

        response = self.client.post(
            "/api/agent/board-memory/",
            data=json.dumps(
                {
                    "user_id": str(profile.user_id),
                    "board_state": {
                        "board": {"width": 800, "height": 600},
                        "objects": [
                            {
                                "name": "guest_note",
                                "text": "Guest note",
                                "x": 24,
                                "y": 32,
                                "width": 180,
                                "height": 120,
                                "memoryType": "memory",
                            }
                        ],
                    },
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        get_response = self.client.get(
            f"/api/agent/board-memory/?user_id={profile.user_id}",
        )
        self.assertEqual(get_response.status_code, 200)
        payload = get_response.json()
        self.assertEqual(payload["board_state"]["objects"][0]["name"], "guest_note")

        profile.refresh_from_db()
        self.assertEqual(profile.whiteboard_state["objects"][0]["name"], "guest_note")


class AuthenticatedIdentityFlowTests(TestCase):
    def _auth_headers(self, user: User) -> dict[str, str]:
        token = issue_token(user)
        return {"HTTP_AUTHORIZATION": f"Token {token.key}"}

    def test_agent_run_start_prefers_authenticated_profile_over_body_user_id(self) -> None:
        viewer_user = User.objects.create_user(username="viewer-auth")
        viewer_profile = AccountProfile.objects.create(
            user=viewer_user,
            display_name="Viewer Auth",
            phone_number="+359888111111",
        )
        other_user = User.objects.create_user(username="other-auth")
        other_profile = AccountProfile.objects.create(
            user=other_user,
            display_name="Other Auth",
            phone_number="+359888222222",
        )

        with patch("controller.views.semi_agent_service.start_run") as mock_start_run:
            mock_start_run.return_value = {"ok": True, "run_id": "run_123"}

            response = self.client.post(
                "/api/agent/run/start/",
                data=json.dumps(
                    {
                        "prompt": "Find someone to talk to.",
                        "user_id": str(other_profile.user_id),
                        "session_id": "session_1",
                        "board_state": {"board": {"width": 800, "height": 600}, "objects": []},
                        "largest_empty_space": {
                            "bbox": {"x": 0, "y": 0, "width": 800, "height": 600},
                        },
                    }
                ),
                content_type="application/json",
                **self._auth_headers(viewer_user),
            )

        self.assertEqual(response.status_code, 200)
        _, kwargs = mock_start_run.call_args
        self.assertEqual(kwargs["user_id"], str(viewer_profile.user_id))

    def test_add_action_prefers_authenticated_profile_over_body_user_id(self) -> None:
        viewer_user = User.objects.create_user(username="viewer-gnn")
        viewer_profile = AccountProfile.objects.create(
            user=viewer_user,
            display_name="Viewer GNN",
            phone_number="+359888333333",
        )
        other_user = User.objects.create_user(username="other-gnn")
        other_profile = AccountProfile.objects.create(
            user=other_user,
            display_name="Other GNN",
            phone_number="+359888444444",
        )

        fake_graph_service = SimpleNamespace(
            add_action_flow=lambda prompt, **kwargs: {
                "ok": True,
                "prompt": prompt,
                "user_id": kwargs.get("user_id"),
                "phone_number": kwargs.get("phone_number"),
            }
        )

        with patch("controller.views._graph_service", return_value=fake_graph_service):
            response = self.client.post(
                "/api/add-action/",
                data=json.dumps(
                    {
                        "prompt": "Call my sister this evening.",
                        "user_id": str(other_profile.user_id),
                    }
                ),
                content_type="application/json",
                **self._auth_headers(viewer_user),
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["user_id"], str(viewer_profile.user_id))
