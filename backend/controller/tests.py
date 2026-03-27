from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from config.cors import LocalDevCorsMiddleware

from engine.custom_mcp_registry import CustomMcpRegistry
from engine.semi_agent_service import SemiAgentService
from engine.whiteboard_memory import WhiteboardMemoryStore


class CustomMcpRegistryTests(SimpleTestCase):
    def test_registry_exposes_gnn_actions_descriptor(self) -> None:
        registry = CustomMcpRegistry()
        payload = registry.load_registry(base_url="http://localhost:8000")

        self.assertEqual(payload["protocol"], "at_home_mcp")
        self.assertEqual(payload["mcps"][0]["id"], "gnn_actions")
        self.assertTrue(payload["mcps"][0]["descriptor_url"].endswith("/api/agent/mcps/gnn_actions/"))
        self.assertTrue(any(item["id"] == "connections" for item in payload["mcps"]))

        descriptor = registry.load_descriptor("gnn_actions", base_url="http://localhost:8000")
        self.assertEqual(descriptor["id"], "gnn_actions")
        self.assertEqual(len(descriptor["tools"]), 3)
        self.assertTrue(descriptor["invoke_url"].endswith("/api/agent/mcps/gnn_actions/invoke/"))

        connections_descriptor = registry.load_descriptor("connections", base_url="http://localhost:8000")
        self.assertEqual(connections_descriptor["id"], "connections")
        self.assertEqual(len(connections_descriptor["tools"]), 2)
        self.assertTrue(connections_descriptor["invoke_url"].endswith("/api/agent/mcps/connections/invoke/"))


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
            self.assertEqual(payload["step_two"]["focus_object"]["name"], "openai_focus")

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
            user_id="user",
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
