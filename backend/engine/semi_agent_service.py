from __future__ import annotations

import base64
import json
import math
import re
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from threading import Lock
from typing import TYPE_CHECKING, Any, Dict, List
from urllib.parse import quote_plus

from .custom_mcp_registry import CustomMcpRegistry
from .llm_parser import QwenPromptParser
from .qwen_worker_client import QwenWorkerClient
from .semi_agent_prompts import (
    build_step_one_mcp_prompt,
    build_step_two_board_prompt,
)
from .user_context import ActiveUserTracker, TemporaryChatHistoryStore
from .whiteboard_memory import WhiteboardMemoryStore
from voice_gateway.services.providers import OpenAILLMProvider, PiperTTSProvider

if TYPE_CHECKING:
    from .graph_service import GraphService


class SemiAgentService:
    SUPPORTED_MCP_TOOLS = {
        "gnn_actions": {"add_action", "fetch_action", "conversation"},
        "connections": {"update_profile", "find_connection"},
        "phone_command": {"open_phone_command"},
    }
    SUPPORTED_TOOL_NAMES = {
        tool_name
        for tool_names in SUPPORTED_MCP_TOOLS.values()
        for tool_name in tool_names
    }
    BOARD_PIPELINE_STAGE_MAX_NEW_TOKENS = 256
    BOARD_PIPELINE_JSON_CONTINUATION_BUDGET = 0
    RUN_JOB_TTL_SECONDS = 900
    FOCUS_TITLE_MAX_WORDS = 6
    FOCUS_TITLE_MAX_CHARS = 42
    FOCUS_OBJECT_NAME_MAX_CHARS = 64
    USER_OBJECT_TAG = "type:user"
    SUPPORTED_REASONING_PROVIDERS = {"qwen", "openai"}

    def __init__(
        self,
        *,
        graph_service: "GraphService" | None = None,
        qwen_client: QwenWorkerClient | None = None,
        registry: CustomMcpRegistry | None = None,
        board_memory: WhiteboardMemoryStore | None = None,
        llm_provider: OpenAILLMProvider | None = None,
        tts_provider: PiperTTSProvider | None = None,
        connections_service: Any | None = None,
        user_tracker: ActiveUserTracker | None = None,
        speech_history_store: TemporaryChatHistoryStore | None = None,
    ) -> None:
        self.graph_service = graph_service
        self.qwen_client = qwen_client or QwenWorkerClient()
        self.registry = registry or CustomMcpRegistry()
        self.board_memory = board_memory or WhiteboardMemoryStore()
        self.llm_provider = llm_provider or OpenAILLMProvider()
        self.tts_provider = tts_provider or PiperTTSProvider()
        self.user_tracker = user_tracker or ActiveUserTracker()
        self.speech_history_store = speech_history_store or TemporaryChatHistoryStore()
        if connections_service is None:
            from apps.accounts.agent_service import ConnectionsAgentService

            connections_service = ConnectionsAgentService(
                qwen_client=self.qwen_client,
                user_tracker=self.user_tracker,
            )
        self.connections_service = connections_service
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._run_jobs: Dict[str, Dict[str, Any]] = {}
        self._run_jobs_lock = Lock()

    def _get_graph_service(self) -> "GraphService":
        if self.graph_service is None:
            from .graph_service import GraphService

            self.graph_service = GraphService(user_tracker=self.user_tracker)
        return self.graph_service

    def _build_mcp_tool_catalog(self) -> Dict[str, Any]:
        registry_payload = self.get_registry_payload()
        catalog = {
            "protocol": registry_payload.get("protocol"),
            "version": registry_payload.get("version"),
            "mcps": [],
        }
        for item in registry_payload.get("mcps", []):
            if not isinstance(item, dict):
                continue
            mcp_id = self._clean_text(item.get("id"))
            if not mcp_id:
                continue
            try:
                descriptor = self.get_descriptor_payload(mcp_id)
            except Exception:
                descriptor = {}
            tools: List[Dict[str, Any]] = []
            for tool in descriptor.get("tools", []):
                if not isinstance(tool, dict):
                    continue
                tool_name = self._normalize_tool_name(tool.get("name"))
                if not tool_name:
                    continue
                tools.append(
                    {
                        "name": tool_name,
                        "description": self._clean_text(tool.get("description")),
                        "method": self._clean_text(tool.get("method")),
                        "path": self._clean_text(tool.get("path")),
                        "body_schema": self._clean_jsonish(tool.get("body_schema"))
                        if isinstance(tool.get("body_schema"), dict)
                        else {},
                    }
                )
            catalog["mcps"].append(
                {
                    "id": mcp_id,
                    "name": self._clean_text(item.get("name") or mcp_id),
                    "description": self._clean_text(item.get("description")),
                    "notes": item.get("notes") if isinstance(item.get("notes"), list) else [],
                    "tools": tools,
                }
            )
        return catalog

    def _supported_mcp_tools(self) -> Dict[str, set[str]]:
        return {
            self._clean_text(mcp.get("id")): {
                self._normalize_tool_name(tool.get("name"))
                for tool in mcp.get("tools", [])
                if isinstance(tool, dict) and self._normalize_tool_name(tool.get("name"))
            }
            for mcp in self._build_mcp_tool_catalog().get("mcps", [])
            if isinstance(mcp, dict) and self._clean_text(mcp.get("id"))
        }

    def _infer_mcp_id_from_tool_name(self, tool_name: str) -> str:
        clean_tool_name = self._normalize_tool_name(tool_name)
        if not clean_tool_name:
            return ""
        matches = [
            mcp_id
            for mcp_id, tool_names in self._supported_mcp_tools().items()
            if clean_tool_name in tool_names
        ]
        if len(matches) == 1:
            return matches[0]
        return ""

    def _json_schema_for_body_type(self, raw_type: Any) -> Dict[str, Any]:
        clean_type = self._clean_text(raw_type).lower()
        if clean_type in {"integer", "int"}:
            return {"type": "integer"}
        if clean_type in {"number", "float"}:
            return {"type": "number"}
        if clean_type in {"boolean", "bool"}:
            return {"type": "boolean"}
        if clean_type in {"array", "list"}:
            return {"type": "array", "items": {"type": "string"}}
        if clean_type == "object":
            return {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "required": [],
            }
        return {"type": "string"}

    def _strict_openai_schema(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        normalized = deepcopy(schema)
        schema_type = self._clean_text(normalized.get("type")).lower()
        if schema_type == "object":
            properties = normalized.get("properties")
            normalized["properties"] = properties if isinstance(properties, dict) else {}
            normalized["additionalProperties"] = False
            original_required = normalized.get("required")
            required_names = (
                {
                    self._clean_text(item)
                    for item in original_required
                    if self._clean_text(item)
                }
                if isinstance(original_required, list)
                else set()
            )
            for property_name, property_schema in list(normalized["properties"].items()):
                if isinstance(property_schema, dict):
                    strict_property_schema = self._strict_openai_schema(property_schema)
                    if property_name not in required_names:
                        strict_property_schema = self._make_schema_nullable(strict_property_schema)
                    normalized["properties"][property_name] = strict_property_schema
            normalized["required"] = list(normalized["properties"].keys())
        elif schema_type == "array" and isinstance(normalized.get("items"), dict):
            normalized["items"] = self._strict_openai_schema(normalized["items"])

        for keyword in ("anyOf", "oneOf", "allOf"):
            if not isinstance(normalized.get(keyword), list):
                continue
            normalized[keyword] = [
                self._strict_openai_schema(item) if isinstance(item, dict) else item
                for item in normalized[keyword]
            ]
        return normalized

    def _make_schema_nullable(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        normalized = deepcopy(schema)
        raw_type = normalized.get("type")
        if isinstance(raw_type, str):
            if raw_type != "null":
                normalized["type"] = [raw_type, "null"]
        elif isinstance(raw_type, list):
            if "null" not in raw_type:
                normalized["type"] = [*raw_type, "null"]
        elif isinstance(normalized.get("anyOf"), list):
            if not any(
                isinstance(item, dict) and self._clean_text(item.get("type")).lower() == "null"
                for item in normalized["anyOf"]
            ):
                normalized["anyOf"] = [*normalized["anyOf"], {"type": "null"}]
        else:
            normalized["type"] = ["string", "null"]
        if isinstance(normalized.get("enum"), list) and None not in normalized["enum"]:
            normalized["enum"] = [*normalized["enum"], None]
        return normalized

    def _build_mcp_call_arguments_schema(self, tool_catalog: Dict[str, Any]) -> Dict[str, Any]:
        properties: Dict[str, Any] = {}
        for mcp in tool_catalog.get("mcps", []):
            if not isinstance(mcp, dict):
                continue
            for tool in mcp.get("tools", []):
                if not isinstance(tool, dict):
                    continue
                body_schema = tool.get("body_schema") if isinstance(tool.get("body_schema"), dict) else {}
                for field_name, field_type in body_schema.items():
                    clean_name = self._clean_text(field_name)
                    if not clean_name or clean_name in properties:
                        continue
                    properties[clean_name] = self._json_schema_for_body_type(field_type)
        if "prompt" not in properties:
            properties["prompt"] = {"type": "string"}
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": ["prompt"],
        }

    def _build_mcp_call_response_schema(self, tool_catalog: Dict[str, Any]) -> Dict[str, Any]:
        mcp_ids = sorted(
            {
                self._clean_text(mcp.get("id"))
                for mcp in tool_catalog.get("mcps", [])
                if isinstance(mcp, dict) and self._clean_text(mcp.get("id"))
            }
        )
        tool_names = sorted(
            {
                self._normalize_tool_name(tool.get("name"))
                for mcp in tool_catalog.get("mcps", [])
                if isinstance(mcp, dict)
                for tool in mcp.get("tools", [])
                if isinstance(tool, dict) and self._normalize_tool_name(tool.get("name"))
            }
        )
        call_schema: Dict[str, Any] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "call_id": {"type": "string"},
                "mcp_id": {"type": "string"},
                "tool_name": {"type": "string"},
                "arguments": self._build_mcp_call_arguments_schema(tool_catalog),
                "why": {"type": "string"},
            },
            "required": ["call_id", "mcp_id", "tool_name", "arguments", "why"],
        }
        if mcp_ids:
            call_schema["properties"]["mcp_id"]["enum"] = mcp_ids
        if tool_names:
            call_schema["properties"]["tool_name"]["enum"] = tool_names
        return self._strict_openai_schema(call_schema)

    def _build_step_one_response_format(self, tool_catalog: Dict[str, Any]) -> Dict[str, Any] | None:
        if not tool_catalog.get("mcps"):
            return None
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "semi_agent_step_one_mcp",
                "strict": True,
                "schema": self._strict_openai_schema({
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "stage": {"type": "string", "enum": ["step_1_mcp"]},
                        "step_number": {"type": "integer", "enum": [1]},
                        "chain_position": {"type": "string", "enum": ["mcp layer"]},
                        "needs_mcps": {"type": "boolean"},
                        "request_kind": {"type": "string", "enum": ["mechanical", "profile", "mixed"]},
                        "memory_hint": {"type": "string", "enum": ["instant", "ram", "memory"]},
                        "reasoning_summary": {"type": "string"},
                        "why_this_is_part_of_the_chain": {"type": "string"},
                        "board_intent": {"type": "string"},
                        "speech_intent": {"type": "string"},
                        "mcp_calls": {
                            "type": "array",
                            "items": self._build_mcp_call_response_schema(tool_catalog),
                        },
                    },
                    "required": [
                        "stage",
                        "step_number",
                        "chain_position",
                        "needs_mcps",
                        "request_kind",
                        "memory_hint",
                        "reasoning_summary",
                        "why_this_is_part_of_the_chain",
                        "board_intent",
                        "speech_intent",
                        "mcp_calls",
                    ],
                }),
            },
        }

    def _build_step_two_response_format(self, tool_catalog: Dict[str, Any]) -> Dict[str, Any] | None:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "semi_agent_step_two_board",
                "strict": True,
                "schema": self._strict_openai_schema({
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "stage": {"type": "string", "enum": ["step_2_board"]},
                        "step_number": {"type": "integer", "enum": [2]},
                        "chain_position": {"type": "string", "enum": ["board interaction"]},
                        "cycle_back_to_step_one": {"type": "boolean"},
                        "reasoning_summary": {"type": "string"},
                        "board_explanation": {"type": "string"},
                        "memory_plan": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "default_memory_type": {"type": "string", "enum": ["instant", "ram", "memory"]},
                                "why": {"type": "string"},
                            },
                            "required": ["default_memory_type", "why"],
                        },
                        "focus_object": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "name": {"type": "string"},
                                "text": {"type": "string"},
                                "width": {"type": "number"},
                                "height": {"type": "number"},
                                "memory_type": {"type": "string", "enum": ["instant", "ram", "memory"]},
                                "delete_after_click": {"type": "boolean"},
                                "linked_call_ids": {"type": "array", "items": {"type": "string"}},
                                "result_title": {"type": "string"},
                                "result_summary": {"type": "string"},
                            },
                            "required": [
                                "name",
                                "text",
                                "width",
                                "height",
                                "memory_type",
                                "delete_after_click",
                                "linked_call_ids",
                                "result_title",
                                "result_summary",
                            ],
                        },
                        "additional_mcp_calls": {
                            "type": "array",
                            "items": self._build_mcp_call_response_schema(tool_catalog),
                        },
                        "board_commands": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": ["create", "move", "enlarge", "shrink", "delete", "click"],
                                    },
                                    "name": {"type": "string"},
                                    "x": {"type": "number"},
                                    "y": {"type": "number"},
                                    "factor": {"type": "number"},
                                    "width": {"type": "number"},
                                    "height": {"type": "number"},
                                    "text": {"type": "string"},
                                    "memoryType": {"type": "string", "enum": ["instant", "ram", "memory"]},
                                    "deleteAfterClick": {"type": "boolean"},
                                    "color": {"type": "string"},
                                    "innerInset": {"type": "number"},
                                    "tags": {"type": "array", "items": {"type": "string"}},
                                },
                                "required": ["action", "name"],
                            },
                        },
                        "result_bindings": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "object_name": {"type": "string"},
                                    "linked_call_ids": {"type": "array", "items": {"type": "string"}},
                                    "memory_type": {"type": "string", "enum": ["instant", "ram", "memory"]},
                                    "delete_after_click": {"type": "boolean"},
                                    "result_title": {"type": "string"},
                                    "result_summary": {"type": "string"},
                                },
                                "required": [
                                    "object_name",
                                    "linked_call_ids",
                                    "memory_type",
                                    "delete_after_click",
                                    "result_title",
                                    "result_summary",
                                ],
                            },
                        },
                    },
                    "required": [
                        "stage",
                        "step_number",
                        "chain_position",
                        "cycle_back_to_step_one",
                        "reasoning_summary",
                        "board_explanation",
                        "memory_plan",
                        "focus_object",
                        "additional_mcp_calls",
                        "board_commands",
                        "result_bindings",
                    ],
                }),
            },
        }

    def get_registry_payload(self, *, base_url: str = "") -> Dict[str, Any]:
        return self.registry.load_registry(base_url=base_url)

    def get_descriptor_payload(self, mcp_id: str, *, base_url: str = "") -> Dict[str, Any]:
        return self.registry.load_descriptor(mcp_id, base_url=base_url)

    def get_board_memory_state(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "board_state": self.board_memory.load_persistent_board_state(),
        }

    def save_board_memory_state(
        self,
        *,
        board_state: Dict[str, Any] | None,
        removed_result_id: str | None = None,
    ) -> Dict[str, Any]:
        if removed_result_id:
            self.board_memory.remove_result_binding(removed_result_id)
        persisted = self.board_memory.save_persistent_board_state(board_state or {})
        return {
            "ok": True,
            "board_state": persisted,
        }

    def run(
        self,
        *,
        prompt: str,
        board_state: Dict[str, Any] | None,
        largest_empty_space: Dict[str, Any] | None,
        user_id: str = "anonymous",
        session_id: str = "default_session",
        reasoning_provider: str = "openai",
    ) -> Dict[str, Any]:
        context = self._prepare_run_context(
            prompt=prompt,
            board_state=board_state,
            largest_empty_space=largest_empty_space,
            user_id=user_id,
            session_id=session_id,
            reasoning_provider=reasoning_provider,
        )
        speech_future = self._executor.submit(
            self._run_speech_stage,
            clean_prompt=context["prompt"],
            step_one=context["step_one"],
            mcp_results=context["mcp_results"],
            registry_payload=context["mcp_registry"],
            user_context=context["user_context"],
            session_id=session_id,
        )
        board_payload = self._run_board_stage(
            clean_prompt=context["prompt"],
            normalized_board_state=context["normalized_board_state"],
            empty_space_payload=context["largest_empty_space"],
            step_one=context["step_one"],
            mcp_results=context["mcp_results"],
            chain_history=context["chain_history"],
            registry_payload=context["mcp_registry"],
            tool_catalog=context["mcp_tool_catalog"],
            user_id=context["effective_user_id"],
            session_id=session_id,
            reasoning_provider=context["reasoning_provider"],
        )
        speech_payload = speech_future.result()
        return {
            **board_payload,
            "user_context": context["user_context"],
            "reasoning_provider": context["reasoning_provider"],
            "speech_response": self._clean_text(speech_payload.get("assistant_text")),
            "speech_audio_base64": speech_payload.get("assistant_audio_base64"),
            "speech_audio_mime_type": speech_payload.get("assistant_audio_mime_type"),
            "speech_provider_status": speech_payload.get("provider_status", {}),
            "speech_warnings": speech_payload.get("warnings", []),
        }

    def start_run(
        self,
        *,
        prompt: str,
        board_state: Dict[str, Any] | None,
        largest_empty_space: Dict[str, Any] | None,
        user_id: str = "anonymous",
        session_id: str = "default_session",
        reasoning_provider: str = "openai",
    ) -> Dict[str, Any]:
        self._prune_run_jobs()
        context = self._prepare_run_context(
            prompt=prompt,
            board_state=board_state,
            largest_empty_space=largest_empty_space,
            user_id=user_id,
            session_id=session_id,
            reasoning_provider=reasoning_provider,
        )
        run_id = f"run_{uuid.uuid4().hex}"
        speech_future = self._executor.submit(
            self._run_speech_stage,
            clean_prompt=context["prompt"],
            step_one=context["step_one"],
            mcp_results=context["mcp_results"],
            registry_payload=context["mcp_registry"],
            user_context=context["user_context"],
            session_id=session_id,
        )
        whitespace_future = self._executor.submit(
            self._run_board_stage,
            clean_prompt=context["prompt"],
            normalized_board_state=context["normalized_board_state"],
            empty_space_payload=context["largest_empty_space"],
            step_one=context["step_one"],
            mcp_results=context["mcp_results"],
            chain_history=context["chain_history"],
            registry_payload=context["mcp_registry"],
            tool_catalog=context["mcp_tool_catalog"],
            user_id=context["effective_user_id"],
            session_id=session_id,
            reasoning_provider=context["reasoning_provider"],
        )
        with self._run_jobs_lock:
            self._run_jobs[run_id] = {
                "created_at": time.time(),
                "prompt": context["prompt"],
                "reasoning_provider": context["reasoning_provider"],
                "user_context": context["user_context"],
                "speech_future": speech_future,
                "whitespace_future": whitespace_future,
            }
        return {
            "ok": True,
            "run_id": run_id,
            "prompt": context["prompt"],
            "user_context": context["user_context"],
            "reasoning_provider": context["reasoning_provider"],
            "step_one": context["step_one"],
            "mcp_results": context["mcp_results"],
            "speech_status": "running",
            "whitespace_status": "running",
        }

    def get_run_speech(self, run_id: str) -> Dict[str, Any]:
        return self._serialize_future_payload(
            run_id=run_id,
            future_key="speech_future",
            kind="speech",
        )

    def get_run_whitespace(self, run_id: str) -> Dict[str, Any]:
        return self._serialize_future_payload(
            run_id=run_id,
            future_key="whitespace_future",
            kind="whitespace",
        )

    def _prepare_run_context(
        self,
        *,
        prompt: str,
        board_state: Dict[str, Any] | None,
        largest_empty_space: Dict[str, Any] | None,
        user_id: str,
        session_id: str,
        reasoning_provider: str,
    ) -> Dict[str, Any]:
        clean_prompt = self._clean_text(prompt)
        if not clean_prompt:
            raise ValueError("prompt required")
        normalized_reasoning_provider = self._normalize_reasoning_provider(
            reasoning_provider,
        )
        user_context = self.user_tracker.resolve(user_id=user_id)
        effective_user_id = user_context["resolved_user_id"]

        normalized_board_state = self.board_memory.normalize_board_state(board_state)
        empty_space_payload = (
            largest_empty_space
            if isinstance(largest_empty_space, dict) and "bbox" in largest_empty_space
            else self.board_memory.find_largest_empty_space(normalized_board_state)
        )

        chain_history: List[Dict[str, Any]] = []
        registry_payload = self.get_registry_payload()
        tool_catalog = self._build_mcp_tool_catalog()

        step_one_raw = self._generate_json(
            system_prompt=build_step_one_mcp_prompt(
                registry=registry_payload,
                tool_catalog=tool_catalog,
                chain_history=chain_history,
                board_state=normalized_board_state,
            ),
            user_prompt=clean_prompt,
            default_payload=self._default_step_one_plan(clean_prompt),
            response_format=self._build_step_one_response_format(tool_catalog),
            allow_default_fallback=False,
            reasoning_provider=normalized_reasoning_provider,
            user_id=effective_user_id,
            session_id=session_id,
        )
        step_one = self._normalize_step_one_plan(step_one_raw, clean_prompt)
        chain_history.append({"stage": "step_1_mcp", "payload": step_one})

        mcp_results = self._execute_mcp_calls(
            step_one.get("mcp_calls", []),
            clean_prompt,
            user_id=effective_user_id,
            board_state=normalized_board_state,
        )
        if mcp_results:
            chain_history.append({"stage": "mcp_results", "payload": mcp_results})

        return {
            "prompt": clean_prompt,
            "normalized_board_state": normalized_board_state,
            "largest_empty_space": empty_space_payload,
            "chain_history": chain_history,
            "mcp_registry": registry_payload,
            "mcp_tool_catalog": tool_catalog,
            "user_context": user_context,
            "effective_user_id": effective_user_id,
            "reasoning_provider": normalized_reasoning_provider,
            "step_one": step_one,
            "mcp_results": mcp_results,
        }

    def _run_board_stage(
        self,
        *,
        clean_prompt: str,
        normalized_board_state: Dict[str, Any],
        empty_space_payload: Dict[str, Any],
        step_one: Dict[str, Any],
        mcp_results: List[Dict[str, Any]],
        chain_history: List[Dict[str, Any]],
        registry_payload: Dict[str, Any],
        tool_catalog: Dict[str, Any],
        user_id: str,
        session_id: str,
        reasoning_provider: str,
    ) -> Dict[str, Any]:
        step_two, final_mcp_results = self._run_step_two_loop(
            prompt=clean_prompt,
            board_state=normalized_board_state,
            largest_empty_space=empty_space_payload,
            step_one=step_one,
            mcp_results=mcp_results,
            chain_history=chain_history,
            tool_catalog=tool_catalog,
            user_id=user_id,
            session_id=session_id,
            reasoning_provider=reasoning_provider,
        )

        final_board_commands = step_two.get("board_commands", [])
        final_board_state = self.board_memory.apply_commands(
            normalized_board_state,
            final_board_commands,
        )
        registered_bindings = self._prepare_result_bindings(
            step_two=step_two,
            executed_results=final_mcp_results,
            final_board_state=final_board_state,
        )
        self._attach_bindings_to_commands(final_board_commands, registered_bindings)
        self.board_memory.register_result_bindings(registered_bindings)
        persisted_board_state = self.connections_service.save_board_state_for_user(
            user_id,
            final_board_state,
        )
        if persisted_board_state is None:
            persisted_board_state = self.board_memory.save_persistent_board_state(final_board_state)

        return {
            "ok": True,
            "status": "completed",
            "prompt": clean_prompt,
            "mcp_registry": registry_payload,
            "reasoning_provider": reasoning_provider,
            "step_one": step_one,
            "mcp_results": final_mcp_results,
            "step_two": step_two,
            "board_commands": final_board_commands,
            "board_state": final_board_state,
            "persisted_board_state": persisted_board_state,
            "result_bindings": [
                {
                    "object_name": binding.get("object_name"),
                    "result_id": binding.get("result_id"),
                    "memory_type": binding.get("memory_type"),
                    "linked_call_ids": binding.get("linked_call_ids", []),
                }
                for binding in registered_bindings
            ],
        }

    def _run_speech_stage(
        self,
        *,
        clean_prompt: str,
        step_one: Dict[str, Any],
        mcp_results: List[Dict[str, Any]],
        registry_payload: Dict[str, Any],
        user_context: Dict[str, str],
        session_id: str,
    ) -> Dict[str, Any]:
        warnings: List[str] = []
        llm_result = None
        history_key = self._clean_text(user_context.get("history_key")) or "anonymous"
        clean_session_id = self._clean_text(session_id) or "default_session"
        recent_history = self.speech_history_store.get_messages(
            history_key=history_key,
            session_id=clean_session_id,
        )
        try:
            llm_result = self.llm_provider.generate_reply_with_messages(
                system_prompt=self._build_parallel_speech_system_prompt(),
                messages=[
                    {
                        "role": "user",
                        "content": self._build_parallel_speech_user_prompt(
                            clean_prompt=clean_prompt,
                            step_one=step_one,
                            mcp_results=mcp_results,
                            registry_payload=registry_payload,
                            recent_history=recent_history,
                        ),
                    }
                ],
                session_id=clean_session_id,
                user_id=self._clean_text(user_context.get("resolved_user_id")) or "anonymous",
                include_history=False,
                store_history=False,
            )
            assistant_text = llm_result.text
            warnings.extend(llm_result.warnings)
            llm_source = llm_result.source
        except Exception as exc:
            assistant_text = self._fallback_parallel_speech_response(clean_prompt)
            warnings.append(f"llm_fallback={exc}")
            llm_source = "fallback"

        self.speech_history_store.append_turn(
            history_key=history_key,
            session_id=clean_session_id,
            user_text=clean_prompt,
            assistant_text=assistant_text,
        )

        assistant_audio_base64 = ""
        assistant_audio_mime_type = ""
        tts_source = self.tts_provider.status()
        try:
            synthesis = self.tts_provider.synthesize(assistant_text)
        except Exception as exc:
            warnings.append(f"tts_unavailable={exc}")
        else:
            warnings.extend(synthesis.warnings)
            assistant_audio_base64 = base64.b64encode(synthesis.audio_bytes).decode(
                "ascii",
            )
            assistant_audio_mime_type = synthesis.mime_type
            tts_source = synthesis.source
        return {
            "ok": True,
            "status": "completed",
            "speech_response": assistant_text,
            "assistant_text": assistant_text,
            "assistant_audio_base64": assistant_audio_base64,
            "assistant_audio_mime_type": assistant_audio_mime_type,
            "provider_status": {
                "llm": llm_source,
                "tts": tts_source,
            },
            "warnings": warnings,
        }

    def _build_parallel_speech_system_prompt(self) -> str:
        return (
            "You are HelloAgain speaking for a semi-agent that has already finished "
            "stage 1 MCP work. You are generally having a conversation with the user, "
            "so be tolerant, explanatory, patient, and helpful. Use the MCP context "
            "only when it helps the answer. This reply will go directly into text to "
            "speech, so keep it natural and easy to say aloud. "
            "If an MCP already completed the concrete action, keep the reply semi-short: "
            "briefly acknowledge what was done, mention where the result now lives when relevant, "
            "and do not repeat the full operational payload because the MCP or board object is handling that part. "
            "THIS IS EXTREMELY IMPORTANT: you MUST answer in Bulgarian written in "
            "Bulgarian Cyrillic. Do not answer in English unless the user explicitly "
            "asks you to switch languages."
        )

    def _build_parallel_speech_user_prompt(
        self,
        *,
        clean_prompt: str,
        step_one: Dict[str, Any],
        mcp_results: List[Dict[str, Any]],
        registry_payload: Dict[str, Any],
        recent_history: List[Dict[str, str]],
    ) -> str:
        used_mcp_context = self._build_used_mcp_context(
            mcp_results=mcp_results,
            registry_payload=registry_payload,
        )
        context_payload = {
            "original_user_request": clean_prompt,
            "recent_chat_history": recent_history,
            "step_one_plan": step_one,
            "used_mcp_context": used_mcp_context,
            "used_mcp_count": len(used_mcp_context),
        }
        return (
            "Hold a helpful conversation with the user about their request. "
            "The agent has already completed the stage 1 MCP work below, so you can "
            "use it as factual context while answering. Respond directly to the user. "
            "Be warm and explanatory when the request is unclear or difficult. "
            "If `used_mcp_count` is above 0, keep the reply semi-short and mostly acknowledge the completed tool work. "
            "Let the MCP result or whiteboard object carry the detailed action payload, and explicitly mention that the action/result is already prepared when that helps the user. "
            "THIS IS EXTREMELY IMPORTANT: the final answer must be in Bulgarian.\n\n"
            f"{json.dumps(context_payload, ensure_ascii=False, indent=2)}"
        )

    def _build_used_mcp_context(
        self,
        *,
        mcp_results: List[Dict[str, Any]],
        registry_payload: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        registry_items = {
            self._clean_text(item.get("id")): item
            for item in registry_payload.get("mcps", [])
            if isinstance(item, dict)
        }
        used_mcps: List[Dict[str, Any]] = []
        for result in mcp_results:
            if not isinstance(result, dict):
                continue
            mcp_id = self._clean_text(result.get("mcp_id"))
            registry_item = registry_items.get(mcp_id, {})
            used_mcps.append(
                {
                    "call_id": self._clean_text(result.get("call_id")),
                    "mcp_id": mcp_id,
                    "mcp_name": self._clean_text(registry_item.get("name")) or mcp_id,
                    "mcp_description": self._clean_text(registry_item.get("description")),
                    "mcp_notes": [
                        self._clean_text(note)
                        for note in registry_item.get("notes", [])
                        if self._clean_text(note)
                    ],
                    "tool_name": self._normalize_tool_name(result.get("tool_name")),
                    "why_used": self._clean_text(result.get("why")),
                    "result_summary": self._clean_text(result.get("summary")),
                }
            )
        return used_mcps

    def _serialize_future_payload(
        self,
        *,
        run_id: str,
        future_key: str,
        kind: str,
    ) -> Dict[str, Any]:
        self._prune_run_jobs()
        with self._run_jobs_lock:
            job = self._run_jobs.get(self._clean_text(run_id))
        if job is None:
            raise ValueError(f"Unknown run '{run_id}'.")

        future = job.get(future_key)
        if not isinstance(future, Future):
            raise ValueError(f"Run '{run_id}' is missing the {kind} future.")

        if not future.done():
            return {
                "ok": True,
                "run_id": run_id,
                "kind": kind,
                "status": "running",
                "prompt": job.get("prompt", ""),
                "user_context": job.get("user_context", {}),
                "reasoning_provider": job.get("reasoning_provider", "qwen"),
            }

        try:
            payload = future.result()
        except Exception as exc:
            return {
                "ok": False,
                "run_id": run_id,
                "kind": kind,
                "status": "failed",
                "detail": str(exc),
            }

        return {
            "run_id": run_id,
            "kind": kind,
            "user_context": job.get("user_context", {}),
            "reasoning_provider": job.get("reasoning_provider", "qwen"),
            **payload,
        }

    def _prune_run_jobs(self) -> None:
        cutoff = time.time() - self.RUN_JOB_TTL_SECONDS
        with self._run_jobs_lock:
            stale_ids = [
                run_id
                for run_id, payload in self._run_jobs.items()
                if float(payload.get("created_at", 0.0)) < cutoff
            ]
            for run_id in stale_ids:
                self._run_jobs.pop(run_id, None)

    def invoke_mcp(
        self,
        *,
        mcp_id: str,
        tool_name: str,
        arguments: Dict[str, Any] | None,
        fallback_prompt: str = "",
        user_id: str = "anonymous",
        board_state: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        user_context = self.user_tracker.resolve(user_id=user_id)
        effective_user_id = user_context["resolved_user_id"]
        clean_mcp_id = self._clean_text(mcp_id)
        clean_tool_name = self._normalize_tool_name(tool_name)
        if not clean_mcp_id:
            clean_mcp_id = self._infer_mcp_id_from_tool_name(clean_tool_name)
        supported_tools = self._supported_mcp_tools().get(clean_mcp_id)
        if supported_tools is None:
            raise ValueError(f"Unsupported MCP '{clean_mcp_id}'.")
        if not clean_tool_name and len(supported_tools) == 1:
            clean_tool_name = sorted(supported_tools)[0]
        if clean_tool_name not in supported_tools:
            raise ValueError(f"Unsupported tool '{clean_tool_name}'.")

        arguments = self._clean_jsonish(arguments) if isinstance(arguments, dict) else {}
        tool_prompt = self._clean_text(arguments.get("prompt") or fallback_prompt)
        if not tool_prompt:
            raise ValueError("prompt required for MCP invocation")

        result = self._dispatch_mcp_tool(
            mcp_id=clean_mcp_id,
            tool_name=clean_tool_name,
            prompt=tool_prompt,
            arguments=arguments,
            user_id=effective_user_id,
            board_state=board_state,
        )
        summary = self._summarize_mcp_result(clean_mcp_id, clean_tool_name, result)
        return {
            "ok": True,
            "user_context": user_context,
            "mcp_id": clean_mcp_id,
            "tool_name": clean_tool_name,
            "arguments": {
                **arguments,
                "prompt": tool_prompt,
            },
            "summary": summary,
            "result": result,
        }

    def open_board_object(
        self,
        *,
        object_payload: Dict[str, Any] | None,
        user_id: str = "anonymous",
    ) -> Dict[str, Any]:
        user_context = self.user_tracker.resolve(user_id=user_id)
        effective_user_id = user_context["resolved_user_id"]
        object_payload = object_payload if isinstance(object_payload, dict) else {}
        object_name = self._clean_text(object_payload.get("name"))
        result_id = self._clean_text(object_payload.get("resultId") or object_payload.get("result_id"))
        memory_type = self._normalize_memory_type(object_payload.get("memoryType") or object_payload.get("memory_type"))
        delete_after_click = self._to_bool(
            object_payload.get("deleteAfterClick", object_payload.get("delete_after_click")),
            default=memory_type == "instant",
        )

        binding = self.board_memory.resolve_result_binding(result_id)
        target_user_id = self._extract_target_user_id(
            object_payload,
            binding if isinstance(binding, dict) else {},
        )
        if target_user_id is not None:
            try:
                viewer = self.connections_service.build_user_widget_payload(
                    agent_user_id=effective_user_id,
                    target_user_id=target_user_id,
                )
            except Exception:
                viewer = None
            if isinstance(viewer, dict):
                return {
                    "ok": True,
                    "found": True,
                    "object_name": object_name,
                    "message": "Opened the user connection widget.",
                    "board_commands": [],
                    "viewer": viewer,
                }

        if binding is None:
            return {
                "ok": True,
                "found": False,
                "object_name": object_name,
                "message": "No stored MCP result was linked to this object.",
                "board_commands": [],
            }

        board_commands: List[Dict[str, Any]] = []
        if delete_after_click or memory_type == "instant":
            if object_name:
                board_commands.append({"action": "delete", "name": object_name})
            self.board_memory.remove_result_binding(result_id)
            persisted_board_state = self.connections_service.apply_board_commands_for_user(
                effective_user_id,
                board_commands,
            )
            if persisted_board_state is None:
                current_persistent_state = self.board_memory.load_persistent_board_state()
                updated_persistent_state = self.board_memory.apply_commands(
                    current_persistent_state,
                    board_commands,
                )
                self.board_memory.save_persistent_board_state(updated_persistent_state)

        title = self._clean_text(binding.get("result_title") or binding.get("resultTitle") or object_name or "Board result")
        summary = self._clean_text(binding.get("result_summary") or binding.get("resultSummary"))
        payload = binding.get("payload") if isinstance(binding.get("payload"), dict) else {"payload": binding.get("payload")}
        viewer = self._build_object_viewer(
            object_payload=object_payload,
            binding=binding,
            default_title=title,
            default_summary=summary,
            default_payload=payload,
            user_id=effective_user_id,
        )

        return {
            "ok": True,
            "user_context": user_context,
            "found": True,
            "object_name": object_name,
            "board_commands": board_commands,
            "speech_response": summary or f"I opened {title}.",
            "viewer": viewer,
        }

    def delete_board_object(
        self,
        *,
        object_payload: Dict[str, Any] | None,
        user_id: str = "anonymous",
    ) -> Dict[str, Any]:
        user_context = self.user_tracker.resolve(user_id=user_id)
        effective_user_id = user_context["resolved_user_id"]
        object_payload = object_payload if isinstance(object_payload, dict) else {}
        object_name = self._clean_text(object_payload.get("name"))
        if not object_name:
            raise ValueError("Object name is required.")

        result_id = self._clean_text(
            object_payload.get("resultId") or object_payload.get("result_id")
        )
        board_commands: List[Dict[str, Any]] = [{"action": "delete", "name": object_name}]

        if result_id:
            self.board_memory.remove_result_binding(result_id)

        persisted_board_state = self.connections_service.apply_board_commands_for_user(
            effective_user_id,
            board_commands,
        )
        if persisted_board_state is None:
            current_persistent_state = self.board_memory.load_persistent_board_state()
            updated_persistent_state = self.board_memory.apply_commands(
                current_persistent_state,
                board_commands,
            )
            persisted_board_state = self.board_memory.save_persistent_board_state(
                updated_persistent_state
            )

        return {
            "ok": True,
            "user_context": user_context,
            "object_name": object_name,
            "board_commands": board_commands,
            "persisted_board_state": persisted_board_state,
        }

    def _run_step_two_loop(
        self,
        *,
        prompt: str,
        board_state: Dict[str, Any],
        largest_empty_space: Dict[str, Any],
        step_one: Dict[str, Any],
        mcp_results: List[Dict[str, Any]],
        chain_history: List[Dict[str, Any]],
        tool_catalog: Dict[str, Any],
        user_id: str,
        session_id: str,
        reasoning_provider: str,
    ) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        current_results = list(mcp_results)
        current_history = list(chain_history)
        step_two = self._default_step_two_plan(prompt, step_one, current_results)

        for _ in range(3):
            step_two_raw = self._generate_json(
                system_prompt=build_step_two_board_prompt(
                    board_state=board_state,
                    largest_empty_space=largest_empty_space,
                    step_one_plan=step_one,
                    mcp_results=current_results,
                    tool_catalog=tool_catalog,
                    chain_history=current_history,
                ),
                user_prompt=prompt,
                default_payload=step_two,
                response_format=self._build_step_two_response_format(tool_catalog),
                allow_default_fallback=False,
                reasoning_provider=reasoning_provider,
                user_id=user_id,
                session_id=session_id,
            )
            step_two = self._normalize_step_two_plan(
                step_two_raw,
                prompt=prompt,
                board_state=board_state,
                largest_empty_space=largest_empty_space,
                step_one=step_one,
                current_results=current_results,
            )
            if not step_two.get("cycle_back_to_step_one"):
                break
            extra_calls = step_two.get("additional_mcp_calls", [])
            if not extra_calls:
                break
            extra_results = self._execute_mcp_calls(
                extra_calls,
                prompt,
                user_id=user_id,
                board_state=board_state,
            )
            if not extra_results:
                break
            current_results.extend(extra_results)
            current_history = list(current_history) + [
                {"stage": "step_2_extra_mcp_results", "payload": extra_results},
            ]

        return step_two, current_results

    def _execute_mcp_calls(
        self,
        calls: List[Dict[str, Any]],
        fallback_prompt: str,
        *,
        user_id: str,
        board_state: Dict[str, Any] | None,
    ) -> List[Dict[str, Any]]:
        executed: List[Dict[str, Any]] = []
        seen_call_ids: set[str] = set()
        for index, call in enumerate(calls, start=1):
            if not isinstance(call, dict):
                continue
            call_id = self._clean_text(call.get("call_id")) or f"call_{index}"
            if call_id in seen_call_ids:
                continue
            seen_call_ids.add(call_id)

            mcp_id = self._clean_text(call.get("mcp_id"))
            tool_name = self._normalize_tool_name(call.get("tool_name"))
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            why = self._clean_text(call.get("why"))

            try:
                payload = self.invoke_mcp(
                    mcp_id=mcp_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    fallback_prompt=fallback_prompt,
                    user_id=user_id,
                    board_state=board_state,
                )
            except Exception as exc:
                payload = {
                    "ok": False,
                    "mcp_id": mcp_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "summary": str(exc),
                    "result": {"detail": str(exc)},
                }

            payload["call_id"] = call_id
            payload["why"] = why
            executed.append(payload)
        return executed

    def _dispatch_mcp_tool(
        self,
        *,
        mcp_id: str,
        tool_name: str,
        prompt: str,
        arguments: Dict[str, Any],
        user_id: str,
        board_state: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        if mcp_id == "gnn_actions":
            return self._dispatch_gnn_tool(tool_name, prompt, user_id=user_id)
        if mcp_id == "connections":
            return self._dispatch_connections_tool(
                tool_name=tool_name,
                prompt=prompt,
                arguments=arguments,
                user_id=user_id,
                board_state=board_state,
            )
        if mcp_id == "phone_command":
            return self._dispatch_phone_command_tool(
                tool_name=tool_name,
                prompt=prompt,
            )
        raise ValueError(f"Unsupported MCP '{mcp_id}'.")

    def _dispatch_gnn_tool(
        self,
        tool_name: str,
        prompt: str,
        *,
        user_id: str,
    ) -> Dict[str, Any]:
        graph_service = self._get_graph_service()
        if tool_name == "add_action":
            return graph_service.add_action_flow(prompt, user_id=user_id)
        if tool_name == "fetch_action":
            return graph_service.fetch_action_flow(prompt, user_id=user_id)
        if tool_name == "conversation":
            return graph_service.conversation_flow(prompt, user_id=user_id)
        raise ValueError(f"Unsupported tool '{tool_name}'.")

    def _dispatch_connections_tool(
        self,
        *,
        tool_name: str,
        prompt: str,
        arguments: Dict[str, Any],
        user_id: str,
        board_state: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        if tool_name == "update_profile":
            return self.connections_service.update_profile_from_prompt(
                agent_user_id=user_id,
                prompt=prompt,
                profile_patch=arguments.get("profile_patch") if isinstance(arguments.get("profile_patch"), dict) else None,
                profile_json=arguments.get("profile_json") if isinstance(arguments.get("profile_json"), dict) else None,
                board_state=board_state,
            )
        if tool_name == "find_connection":
            return self.connections_service.find_connection_for_prompt(
                agent_user_id=user_id,
                prompt=prompt,
                limit=int(arguments.get("limit") or 1),
                board_state=board_state,
            )
        raise ValueError(f"Unsupported tool '{tool_name}'.")

    def _dispatch_phone_command_tool(
        self,
        *,
        tool_name: str,
        prompt: str,
    ) -> Dict[str, Any]:
        if tool_name != "open_phone_command":
            raise ValueError(f"Unsupported tool '{tool_name}'.")
        launch_metadata = self._build_phone_command_launch_metadata(prompt)
        return {
            "ok": True,
            "widget_type": "phone_command_launcher",
            "message": "Phone command launch is ready.",
            "board_object": {
                "tags": ["kind:phone_command", "launch:phone_command"],
                "extra_data": {
                    "kind": "phone_command",
                    "launch_target": "phone_command",
                    **launch_metadata,
                },
            },
            **launch_metadata,
        }

    def _summarize_mcp_result(self, mcp_id: str, tool_name: str, result: Dict[str, Any]) -> str:
        if mcp_id == "connections":
            if tool_name == "find_connection":
                user = result.get("user") if isinstance(result.get("user"), dict) else {}
                display_name = self._clean_text(user.get("display_name") or user.get("name"))
                if display_name:
                    return f"Found a close connection match: {display_name}."
                return self._clean_text(result.get("message")) or "Connection search finished."
            if tool_name == "update_profile":
                profile = result.get("profile") if isinstance(result.get("profile"), dict) else {}
                description = self._clean_text(profile.get("effective_description") or profile.get("description"))
                if description:
                    return f"Updated your connection profile: {description[:180]}"
                return self._clean_text(result.get("message")) or "Profile update finished."
        if mcp_id == "phone_command":
            launch_prompt = self._clean_text(result.get("prompt"))
            if launch_prompt:
                return f"Prepared the phone command handoff for {launch_prompt}."
            return self._clean_text(result.get("message")) or "Phone command handoff is ready."
        if tool_name == "fetch_action":
            chosen = result.get("result") if isinstance(result.get("result"), dict) else {}
            chosen_name = self._clean_text(chosen.get("name"))
            if chosen_name:
                return f"Fetch chose {chosen_name}."
            message = self._clean_text(result.get("message"))
            if message:
                return message
        if tool_name == "add_action":
            action = result.get("action") if isinstance(result.get("action"), dict) else {}
            action_name = self._clean_text(action.get("name"))
            if action_name:
                return f"Added action memory for {action_name}."
        if tool_name == "conversation":
            user = result.get("user") if isinstance(result.get("user"), dict) else {}
            description = self._clean_text(user.get("description"))
            if description:
                return description[:220]
        return f"{tool_name} finished."

    def _prepare_result_bindings(
        self,
        *,
        step_two: Dict[str, Any],
        executed_results: List[Dict[str, Any]],
        final_board_state: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        results_by_call_id = {
            self._clean_text(item.get("call_id")): item
            for item in executed_results
            if isinstance(item, dict)
        }
        objects_by_name = {
            self._clean_text(obj.get("name")): obj
            for obj in final_board_state.get("objects", [])
            if isinstance(obj, dict)
        }

        prepared: List[Dict[str, Any]] = []
        seen_object_names: set[str] = set()
        for raw_binding in step_two.get("result_bindings", []):
            if not isinstance(raw_binding, dict):
                continue
            object_name = self._clean_text(raw_binding.get("object_name") or raw_binding.get("objectName"))
            if not object_name or object_name not in objects_by_name or object_name in seen_object_names:
                continue
            seen_object_names.add(object_name)

            linked_call_ids = [
                self._clean_text(item)
                for item in raw_binding.get("linked_call_ids", [])
                if self._clean_text(item)
            ]
            if not linked_call_ids:
                linked_call_ids = [item for item in results_by_call_id.keys() if item]

            linked_payloads = [
                deepcopy(results_by_call_id[item])
                for item in linked_call_ids
                if item in results_by_call_id
            ]
            object_state = objects_by_name[object_name]
            object_metadata = self._extract_board_object_metadata(linked_payloads)
            self._apply_board_object_metadata(object_state, object_metadata)
            result_id = self._clean_text(raw_binding.get("result_id") or raw_binding.get("resultId"))
            if not result_id:
                result_id = f"result_{uuid.uuid4().hex[:12]}"
                object_state["resultId"] = result_id

            binding = {
                "result_id": result_id,
                "object_name": object_name,
                "memory_type": self._normalize_memory_type(
                    raw_binding.get("memory_type")
                    or raw_binding.get("memoryType")
                    or object_state.get("memoryType")
                ),
                "delete_after_click": self._to_bool(
                    raw_binding.get("delete_after_click", raw_binding.get("deleteAfterClick")),
                    default=self._normalize_memory_type(object_state.get("memoryType")) == "instant",
                ),
                "linked_call_ids": linked_call_ids,
                "result_title": self._clean_text(
                    raw_binding.get("result_title")
                    or raw_binding.get("resultTitle")
                    or object_state.get("text")
                    or object_name
                ),
                "result_summary": self._clean_text(
                    raw_binding.get("result_summary")
                    or raw_binding.get("resultSummary")
                ),
                "object_metadata": object_metadata,
                "payload": {
                    "linked_results": linked_payloads,
                    "object": deepcopy(object_state),
                },
            }
            if self._extract_target_user_id(object_state, binding) is not None:
                binding["memory_type"] = "memory"
                binding["delete_after_click"] = False
                object_state["memoryType"] = "memory"
                object_state["deleteAfterClick"] = False
            prepared.append(binding)
        return prepared

    def _attach_bindings_to_commands(
        self,
        commands: List[Dict[str, Any]],
        bindings: List[Dict[str, Any]],
    ) -> None:
        bindings_by_name = {
            self._clean_text(binding.get("object_name")): binding
            for binding in bindings
            if isinstance(binding, dict)
        }
        for command in commands:
            if not isinstance(command, dict):
                continue
            if self._clean_text(command.get("action")) != "create":
                continue
            object_name = self._clean_text(command.get("name"))
            binding = bindings_by_name.get(object_name)
            if binding is None:
                continue
            command["resultId"] = binding.get("result_id")
            command["memoryType"] = binding.get("memory_type")
            command["deleteAfterClick"] = binding.get("delete_after_click")
            command["tags"] = self._merge_tags(
                command.get("tags"),
                [
                    f"memory:{binding.get('memory_type', 'ram')}",
                    *self._coerce_tags_from_binding(binding),
                ],
            )
            extra_data = self._coerce_extra_data_from_binding(binding)
            if extra_data:
                command["extraData"] = extra_data

    def _build_object_viewer(
        self,
        *,
        object_payload: Dict[str, Any],
        binding: Dict[str, Any],
        default_title: str,
        default_summary: str,
        default_payload: Dict[str, Any],
        user_id: str,
    ) -> Dict[str, Any]:
        target_user_id = self._extract_target_user_id(object_payload, binding)
        if target_user_id is not None:
            try:
                user_viewer = self.connections_service.build_user_widget_payload(
                    agent_user_id=user_id,
                    target_user_id=target_user_id,
                )
            except Exception:
                user_viewer = None
            if isinstance(user_viewer, dict):
                return {
                    "title": self._clean_text(user_viewer.get("title")) or default_title,
                    "summary": self._clean_text(user_viewer.get("summary")) or default_summary,
                    "memory_type": binding.get("memory_type"),
                    "linked_call_ids": binding.get("linked_call_ids", []),
                    "payload": default_payload,
                    **user_viewer,
                }

        phone_command_launch = self._extract_phone_command_launch(object_payload, binding)
        if phone_command_launch is not None:
            return {
                "widget_type": "phone_command_launcher",
                "title": default_title or "Phone Command",
                "summary": default_summary or "Open the phone command screen and run the prepared prompt.",
                "memory_type": binding.get("memory_type"),
                "linked_call_ids": binding.get("linked_call_ids", []),
                "payload": default_payload,
                **phone_command_launch,
            }

        return {
            "title": default_title,
            "summary": default_summary,
            "memory_type": binding.get("memory_type"),
            "linked_call_ids": binding.get("linked_call_ids", []),
            "payload": default_payload,
        }

    def _extract_target_user_id(
        self,
        object_payload: Dict[str, Any],
        binding: Dict[str, Any],
    ) -> int | None:
        candidates = [
            object_payload.get("extraData"),
            object_payload.get("extra_data"),
        ]
        payload = binding.get("payload") if isinstance(binding.get("payload"), dict) else {}
        candidates.append(payload.get("object") if isinstance(payload.get("object"), dict) else {})
        for linked in payload.get("linked_results", []):
            if not isinstance(linked, dict):
                continue
            result = linked.get("result") if isinstance(linked.get("result"), dict) else {}
            candidates.append(result.get("board_object"))
            candidates.append(result.get("user"))

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            extra_data = (
                candidate.get("extra_data")
                if isinstance(candidate.get("extra_data"), dict)
                else candidate.get("extraData")
                if isinstance(candidate.get("extraData"), dict)
                else candidate
            )
            for key in ("user_id", "target_user_id"):
                value = extra_data.get(key)
                try:
                    if value is not None:
                        return int(value)
                except (TypeError, ValueError):
                    continue
        return None

    def _extract_phone_command_launch(
        self,
        object_payload: Dict[str, Any],
        binding: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        candidates = [
            object_payload.get("extraData"),
            object_payload.get("extra_data"),
        ]
        payload = binding.get("payload") if isinstance(binding.get("payload"), dict) else {}
        candidates.append(payload.get("object") if isinstance(payload.get("object"), dict) else {})
        for linked in payload.get("linked_results", []):
            if not isinstance(linked, dict):
                continue
            result = linked.get("result") if isinstance(linked.get("result"), dict) else {}
            candidates.append(result.get("board_object"))
            candidates.append(result)

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            extra_data = (
                candidate.get("extra_data")
                if isinstance(candidate.get("extra_data"), dict)
                else candidate.get("extraData")
                if isinstance(candidate.get("extraData"), dict)
                else candidate
            )
            kind = self._clean_text(extra_data.get("kind")).lower()
            prompt = self._clean_text(extra_data.get("prompt"))
            if kind != "phone_command" and not prompt:
                continue
            if not prompt:
                continue
            launch_metadata = self._build_phone_command_launch_metadata(prompt)
            launch_metadata["launch_target"] = (
                self._clean_text(extra_data.get("launch_target")) or "phone_command"
            )
            launch_metadata["auto_run_on_open"] = self._to_bool(
                extra_data.get("auto_run_on_open", extra_data.get("autoRunOnOpen")),
                default=True,
            )
            return launch_metadata
        return None

    def _build_phone_command_launch_metadata(self, prompt: str) -> Dict[str, Any]:
        clean_prompt = self._clean_text(prompt)
        encoded_prompt = quote_plus(clean_prompt)
        return {
            "prompt": clean_prompt,
            "open_url": f"/api/agent/phone-command/open/?prompt={encoded_prompt}",
            "deep_link": f"helloagain://phone-command?prompt={encoded_prompt}",
            "auto_run_on_open": True,
        }

    def _extract_board_object_metadata(self, linked_payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
        tags: List[str] = []
        extra_data: Dict[str, Any] = {}
        for payload in linked_payloads:
            if not isinstance(payload, dict):
                continue
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            board_object = result.get("board_object") if isinstance(result.get("board_object"), dict) else {}
            tags = self._merge_tags(tags, board_object.get("tags") if isinstance(board_object, dict) else [])
            candidate_extra = board_object.get("extra_data", board_object.get("extraData"))
            if isinstance(candidate_extra, dict):
                extra_data.update(self._clean_jsonish(candidate_extra))
        return {
            "tags": tags,
            "extraData": extra_data,
        }

    def _apply_board_object_metadata(
        self,
        object_state: Dict[str, Any],
        metadata: Dict[str, Any],
    ) -> None:
        tags = metadata.get("tags") if isinstance(metadata, dict) else []
        extra_data = metadata.get("extraData") if isinstance(metadata, dict) else {}
        object_state["tags"] = self._merge_tags(object_state.get("tags"), tags if isinstance(tags, list) else [])
        if isinstance(extra_data, dict) and extra_data:
            existing = object_state.get("extraData") if isinstance(object_state.get("extraData"), dict) else {}
            object_state["extraData"] = {
                **existing,
                **extra_data,
            }

    def _coerce_tags_from_binding(self, binding: Dict[str, Any]) -> List[str]:
        metadata = binding.get("object_metadata") if isinstance(binding.get("object_metadata"), dict) else {}
        tags = metadata.get("tags")
        return tags if isinstance(tags, list) else []

    def _coerce_extra_data_from_binding(self, binding: Dict[str, Any]) -> Dict[str, Any]:
        metadata = binding.get("object_metadata") if isinstance(binding.get("object_metadata"), dict) else {}
        extra_data = metadata.get("extraData")
        return extra_data if isinstance(extra_data, dict) else {}

    def _normalize_step_one_plan(self, raw: Dict[str, Any], prompt: str) -> Dict[str, Any]:
        raw = raw if isinstance(raw, dict) else {}
        calls = self._normalize_mcp_calls(
            raw.get("mcp_calls")
            or raw.get("calls")
            or raw.get("tool_calls")
            or raw.get("selected_tools")
            or raw.get("selected_tool")
            or raw.get("call")
        )
        request_kind = self._clean_text(raw.get("request_kind")).lower()
        if request_kind not in {"mechanical", "profile", "mixed"}:
            request_kind = self._default_request_kind(prompt)
        needs_mcps = self._to_bool(raw.get("needs_mcps"), default=bool(calls))
        memory_hint = self._normalize_memory_type(raw.get("memory_hint") or self._default_memory_type(prompt, request_kind))

        if request_kind == "mechanical" and not calls:
            needs_mcps = False
        if needs_mcps and not calls:
            raise ValueError("Step 1 planning returned needs_mcps=true without any valid MCP calls.")

        return {
            "stage": "step_1_mcp",
            "step_number": 1,
            "chain_position": "mcp layer",
            "needs_mcps": needs_mcps,
            "request_kind": request_kind,
            "memory_hint": memory_hint,
            "reasoning_summary": self._clean_text(raw.get("reasoning_summary")) or "Step 1 decided what MCP work, if any, is needed.",
            "why_this_is_part_of_the_chain": self._clean_text(raw.get("why_this_is_part_of_the_chain"))
            or "This is the MCP decision layer of the hardcoded chain.",
            "board_intent": self._clean_text(raw.get("board_intent")) or "Step 2 should create one active board change around the request.",
            "speech_intent": self._clean_text(raw.get("speech_intent")) or "Step 3 should answer with awareness of the earlier steps.",
            "mcp_calls": calls if needs_mcps else [],
        }

    def _normalize_step_two_plan(
        self,
        raw: Dict[str, Any],
        *,
        prompt: str,
        board_state: Dict[str, Any],
        largest_empty_space: Dict[str, Any],
        step_one: Dict[str, Any],
        current_results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        raw = raw if isinstance(raw, dict) else {}
        memory_plan = raw.get("memory_plan") if isinstance(raw.get("memory_plan"), dict) else {}
        default_memory_type = self._normalize_memory_type(
            memory_plan.get("default_memory_type") or step_one.get("memory_hint")
        )

        focus_object = raw.get("focus_object") if isinstance(raw.get("focus_object"), dict) else {}
        focus_text = self._coerce_focus_title(
            focus_object.get("text"),
            prompt=prompt,
            current_results=current_results,
        )
        focus_name = self._coerce_focus_object_name(
            focus_object.get("name"),
            focus_text=focus_text,
        )
        focus_width = max(220.0, self._to_float(focus_object.get("width"), 320.0))
        focus_height = max(160.0, self._to_float(focus_object.get("height"), 220.0))
        focus_memory_type = self._normalize_memory_type(focus_object.get("memory_type") or default_memory_type)
        focus_delete_after_click = self._to_bool(
            focus_object.get("delete_after_click", focus_object.get("deleteAfterClick")),
            default=focus_memory_type == "instant",
        )
        focus_linked_call_ids = self._normalize_linked_call_ids(focus_object.get("linked_call_ids"), current_results)
        focus_result_title = (
            self._sanitize_focus_title_candidate(focus_object.get("result_title"))
            or focus_text
        )

        board_commands = self._normalize_board_commands(
            raw.get("board_commands"),
            focus_name=focus_name,
            focus_text=focus_text,
            default_memory_type=focus_memory_type,
            delete_after_click=focus_delete_after_click,
        )

        result_bindings = self._normalize_result_bindings(
            raw.get("result_bindings"),
            focus_name=focus_name,
            focus_memory_type=focus_memory_type,
            focus_delete_after_click=focus_delete_after_click,
            focus_linked_call_ids=focus_linked_call_ids,
            focus_title=focus_result_title,
            focus_summary=self._clean_text(focus_object.get("result_summary")),
        )

        additional_mcp_calls = self._normalize_mcp_calls(raw.get("additional_mcp_calls"))
        cycle_back = self._to_bool(
            raw.get("cycle_back_to_step_one"),
            default=bool(additional_mcp_calls),
        )

        normalized = {
            "stage": "step_2_board",
            "step_number": 2,
            "chain_position": "board interaction",
            "cycle_back_to_step_one": cycle_back,
            "reasoning_summary": self._clean_text(raw.get("reasoning_summary"))
            or "Step 2 planned the board movement and object memory.",
            "board_explanation": self._clean_text(raw.get("board_explanation"))
            or "The board should make one active current change for the request.",
            "memory_plan": {
                "default_memory_type": default_memory_type,
                "why": self._clean_text(memory_plan.get("why"))
                or "The memory type follows the instant / ram / memory split from the chain.",
            },
            "focus_object": {
                "name": focus_name,
                "text": focus_text,
                "width": focus_width,
                "height": focus_height,
                "memory_type": focus_memory_type,
                "delete_after_click": focus_delete_after_click,
                "linked_call_ids": focus_linked_call_ids,
                "result_title": focus_result_title,
                "result_summary": self._clean_text(focus_object.get("result_summary")),
            },
            "additional_mcp_calls": additional_mcp_calls,
            "board_commands": board_commands,
            "result_bindings": result_bindings,
        }

        return self._enforce_board_focus(
            normalized,
            board_state=board_state,
            largest_empty_space=largest_empty_space,
        )

    def _enforce_board_focus(
        self,
        step_two: Dict[str, Any],
        *,
        board_state: Dict[str, Any],
        largest_empty_space: Dict[str, Any],
    ) -> Dict[str, Any]:
        result = deepcopy(step_two)
        focus = result["focus_object"]
        focus_name = focus["name"]
        commands = result["board_commands"]

        create_command = None
        for command in commands:
            if command.get("action") == "create" and self._clean_text(command.get("name")) == focus_name:
                create_command = command
                break

        if create_command is None:
            create_command = {
                "action": "create",
                "name": focus_name,
                "text": focus["text"],
                "width": focus["width"],
                "height": focus["height"],
                "memoryType": focus["memory_type"],
                "deleteAfterClick": focus["delete_after_click"],
                "tags": ["agent-focus", f"memory:{focus['memory_type']}"],
            }
            commands.append(create_command)

        create_command["width"] = max(focus["width"], self._to_float(create_command.get("width"), focus["width"]))
        create_command["height"] = max(focus["height"], self._to_float(create_command.get("height"), focus["height"]))
        create_command["text"] = self._clean_text(create_command.get("text") or focus["text"]) or focus["text"]
        create_command["memoryType"] = self._normalize_memory_type(create_command.get("memoryType") or focus["memory_type"])
        create_command["deleteAfterClick"] = self._to_bool(
            create_command.get("deleteAfterClick"),
            default=focus["delete_after_click"],
        )
        create_command["tags"] = self._merge_tags(create_command.get("tags"), ["agent-focus", f"memory:{create_command['memoryType']}"])

        target_position = self._resolve_focus_position(
            board_state=board_state,
            largest_empty_space=largest_empty_space,
            width=create_command["width"],
            height=create_command["height"],
        )
        create_command["x"] = target_position["x"]
        create_command["y"] = target_position["y"]

        hero_rect = {
            "x": create_command["x"],
            "y": create_command["y"],
            "width": create_command["width"],
            "height": create_command["height"],
        }

        move_targets = {
            self._clean_text(command.get("name"))
            for command in commands
            if command.get("action") == "move"
        }
        shrink_targets = {
            self._clean_text(command.get("name"))
            for command in commands
            if command.get("action") == "shrink"
        }

        focus_shift_commands: List[Dict[str, Any]] = []
        for obj in board_state.get("objects", []):
            name = self._clean_text(obj.get("name"))
            if not name or name == focus_name:
                continue
            candidate_rect = {
                "x": float(obj["x"]),
                "y": float(obj["y"]),
                "width": float(obj["width"]),
                "height": float(obj["height"]),
            }
            expanded_hero = {
                "x": max(0.0, hero_rect["x"] - 36.0),
                "y": max(0.0, hero_rect["y"] - 36.0),
                "width": hero_rect["width"] + 72.0,
                "height": hero_rect["height"] + 72.0,
            }
            if not self.board_memory.rectangles_overlap(expanded_hero, candidate_rect):
                continue

            if name not in shrink_targets:
                focus_shift_commands.append({"action": "shrink", "name": name, "factor": 0.82})
            if name not in move_targets:
                focus_shift_commands.append(
                    {
                        "action": "move",
                        "name": name,
                        **self._push_object_out_of_focus(
                            board_width=board_state["board"]["width"],
                            board_height=board_state["board"]["height"],
                            hero_rect=hero_rect,
                            object_rect=candidate_rect,
                        ),
                    }
                )

        if focus_shift_commands:
            commands[:] = focus_shift_commands + commands

        if not any(
            command.get("action") == "enlarge" and self._clean_text(command.get("name")) == focus_name
            for command in commands
        ):
            commands.append({"action": "enlarge", "name": focus_name, "factor": 1.04})

        if not result["result_bindings"]:
            result["result_bindings"] = [
                {
                    "object_name": focus_name,
                    "linked_call_ids": focus["linked_call_ids"],
                    "memory_type": create_command["memoryType"],
                    "delete_after_click": create_command["deleteAfterClick"],
                    "result_title": focus["result_title"] or focus["text"],
                    "result_summary": focus["result_summary"],
                }
            ]
        return result

    def _resolve_focus_position(
        self,
        *,
        board_state: Dict[str, Any],
        largest_empty_space: Dict[str, Any],
        width: float,
        height: float,
    ) -> Dict[str, float]:
        board = board_state.get("board", {})
        board_width = self._to_float(board.get("width"), 1000.0)
        board_height = self._to_float(board.get("height"), 700.0)
        bbox = largest_empty_space.get("bbox") if isinstance(largest_empty_space.get("bbox"), dict) else None
        if bbox:
            empty_width = self._to_float(bbox.get("width"), 0.0)
            empty_height = self._to_float(bbox.get("height"), 0.0)
            if empty_width >= width and empty_height >= height:
                x = self._to_float(bbox.get("x"), 0.0) + max(0.0, (empty_width - width) / 2.0)
                y = self._to_float(bbox.get("y"), 0.0) + max(0.0, (empty_height - height) / 2.0)
                return {
                    "x": min(max(0.0, x), max(0.0, board_width - width)),
                    "y": min(max(0.0, y), max(0.0, board_height - height)),
                }
        return {
            "x": max(0.0, (board_width - width) / 2.0),
            "y": max(0.0, (board_height - height) / 2.0),
        }

    def _push_object_out_of_focus(
        self,
        *,
        board_width: float,
        board_height: float,
        hero_rect: Dict[str, float],
        object_rect: Dict[str, float],
    ) -> Dict[str, float]:
        margin = 42.0
        candidates = [
            {
                "x": max(0.0, hero_rect["x"] - object_rect["width"] - margin),
                "y": min(max(0.0, object_rect["y"]), max(0.0, board_height - object_rect["height"])),
            },
            {
                "x": min(max(0.0, board_width - object_rect["width"]), hero_rect["x"] + hero_rect["width"] + margin),
                "y": min(max(0.0, object_rect["y"]), max(0.0, board_height - object_rect["height"])),
            },
            {
                "x": min(max(0.0, object_rect["x"]), max(0.0, board_width - object_rect["width"])),
                "y": max(0.0, hero_rect["y"] - object_rect["height"] - margin),
            },
            {
                "x": min(max(0.0, object_rect["x"]), max(0.0, board_width - object_rect["width"])),
                "y": min(max(0.0, board_height - object_rect["height"]), hero_rect["y"] + hero_rect["height"] + margin),
            },
        ]

        best = candidates[0]
        best_distance = math.inf
        for candidate in candidates:
            candidate_rect = {
                "x": candidate["x"],
                "y": candidate["y"],
                "width": object_rect["width"],
                "height": object_rect["height"],
            }
            if self.board_memory.rectangles_overlap(candidate_rect, hero_rect):
                continue
            distance = abs(candidate["x"] - object_rect["x"]) + abs(candidate["y"] - object_rect["y"])
            if distance < best_distance:
                best_distance = distance
                best = candidate
        return best

    def _normalize_mcp_calls(self, payload: Any) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        items: List[Any] = []
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            if any(payload.get(key) is not None for key in ("mcp_id", "mcp", "tool_name", "tool", "action")):
                items = [payload]
            else:
                for key in ("mcp_calls", "calls", "tool_calls", "tools", "actions"):
                    value = payload.get(key)
                    if isinstance(value, list):
                        items = value
                        break
                    if isinstance(value, dict):
                        items = [value]
                        break
        supported_tools = self._supported_mcp_tools()
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            mcp_id = self._clean_text(item.get("mcp_id") or item.get("mcp"))
            tool_name = self._normalize_tool_name(
                item.get("tool_name")
                or item.get("tool")
                or item.get("action")
                or item.get("name")
            )
            if not mcp_id:
                mcp_id = self._infer_mcp_id_from_tool_name(tool_name)
            if tool_name not in supported_tools.get(mcp_id, set()):
                continue
            raw_arguments = (
                item.get("arguments")
                if isinstance(item.get("arguments"), dict)
                else item.get("args")
                if isinstance(item.get("args"), dict)
                else item.get("body")
                if isinstance(item.get("body"), dict)
                else item.get("tool_input")
                if isinstance(item.get("tool_input"), dict)
                else {}
            )
            arguments = self._clean_jsonish(raw_arguments) if isinstance(raw_arguments, dict) else {}
            inline_prompt = self._clean_text(item.get("prompt"))
            if inline_prompt and not self._clean_text(arguments.get("prompt")):
                arguments["prompt"] = inline_prompt
            prompt = self._clean_text(arguments.get("prompt"))
            if prompt:
                arguments["prompt"] = prompt
            results.append(
                {
                    "call_id": self._clean_text(item.get("call_id")) or f"{mcp_id}.{tool_name}.{index}",
                    "mcp_id": mcp_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "why": self._clean_text(item.get("why")),
                }
            )
        return results

    def _normalize_board_commands(
        self,
        payload: Any,
        *,
        focus_name: str,
        focus_text: str,
        default_memory_type: str,
        delete_after_click: bool,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        items = payload if isinstance(payload, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            action = self._clean_text(item.get("action")).lower()
            if action not in {"create", "move", "enlarge", "shrink", "delete", "click"}:
                continue
            name = self._clean_text(item.get("name")) or focus_name
            command: Dict[str, Any] = {"action": action, "name": name}
            if action == "create":
                command.update(
                    {
                        "text": self._clean_text(item.get("text")) or focus_text,
                        "width": max(160.0, self._to_float(item.get("width"), 320.0)),
                        "height": max(120.0, self._to_float(item.get("height"), 220.0)),
                        "memoryType": self._normalize_memory_type(item.get("memoryType") or default_memory_type),
                        "deleteAfterClick": self._to_bool(item.get("deleteAfterClick"), default=delete_after_click),
                        "color": item.get("color"),
                        "innerInset": self._to_float(item.get("innerInset"), 12.0),
                        "tags": self._merge_tags(item.get("tags"), []),
                    }
                )
                extra_data = item.get("extraData", item.get("extra_data"))
                if isinstance(extra_data, dict) and extra_data:
                    command["extraData"] = self._clean_jsonish(extra_data)
                if item.get("x") is not None:
                    command["x"] = self._to_float(item.get("x"), 0.0)
                if item.get("y") is not None:
                    command["y"] = self._to_float(item.get("y"), 0.0)
            elif action == "move":
                command["x"] = self._to_float(item.get("x"), 0.0)
                command["y"] = self._to_float(item.get("y"), 0.0)
            elif action in {"enlarge", "shrink"}:
                default_factor = 1.2 if action == "enlarge" else 0.85
                command["factor"] = self._to_float(item.get("factor"), default_factor)
            results.append(command)
        return results

    def _normalize_result_bindings(
        self,
        payload: Any,
        *,
        focus_name: str,
        focus_memory_type: str,
        focus_delete_after_click: bool,
        focus_linked_call_ids: List[str],
        focus_title: str,
        focus_summary: str,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        items = payload if isinstance(payload, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            object_name = self._clean_text(item.get("object_name") or item.get("objectName")) or focus_name
            results.append(
                {
                    "object_name": object_name,
                    "linked_call_ids": [
                        linked for linked in [
                            self._clean_text(raw)
                            for raw in item.get("linked_call_ids", [])
                        ] if linked
                    ] or list(focus_linked_call_ids),
                    "memory_type": self._normalize_memory_type(item.get("memory_type") or item.get("memoryType") or focus_memory_type),
                    "delete_after_click": self._to_bool(
                        item.get("delete_after_click", item.get("deleteAfterClick")),
                        default=focus_delete_after_click,
                    ),
                    "result_title": self._sanitize_focus_title_candidate(
                        item.get("result_title") or item.get("resultTitle") or focus_title
                    ) or focus_title,
                    "result_summary": self._clean_text(item.get("result_summary") or item.get("resultSummary") or focus_summary),
                }
            )
        return results

    def _normalize_linked_call_ids(self, payload: Any, current_results: List[Dict[str, Any]]) -> List[str]:
        if isinstance(payload, list):
            results = [self._clean_text(item) for item in payload if self._clean_text(item)]
            if results:
                return results
        return [
            self._clean_text(item.get("call_id"))
            for item in current_results
            if self._clean_text(item.get("call_id"))
        ]

    def _generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        default_payload: Dict[str, Any],
        response_format: Dict[str, Any] | None = None,
        allow_default_fallback: bool = True,
        reasoning_provider: str = "qwen",
        user_id: str = "anonymous",
        session_id: str = "default_session",
    ) -> Dict[str, Any]:
        normalized_provider = self._normalize_reasoning_provider(reasoning_provider)
        try:
            if normalized_provider == "qwen":
                raw = self.qwen_client.generate(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    generation_overrides={
                        "max_new_tokens": self.BOARD_PIPELINE_STAGE_MAX_NEW_TOKENS,
                        "json_continuation_budget": self.BOARD_PIPELINE_JSON_CONTINUATION_BUDGET,
                    },
                )
            else:
                raw = self._generate_openai_reasoning_text(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_format=response_format,
                    user_id=user_id,
                    session_id=session_id,
                )
            return QwenPromptParser._extract_json(raw)
        except Exception:
            if normalized_provider == "qwen":
                try:
                    raw = self._generate_openai_reasoning_text(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        response_format=response_format,
                        user_id=user_id,
                        session_id=session_id,
                    )
                    return QwenPromptParser._extract_json(raw)
                except Exception:
                    pass
            if not allow_default_fallback:
                raise
            return deepcopy(default_payload)

    def _normalize_reasoning_provider(self, value: Any) -> str:
        normalized = self._clean_text(value).lower() or "openai"
        if normalized not in self.SUPPORTED_REASONING_PROVIDERS:
            raise ValueError(
                f"Unsupported reasoning_provider '{value}'. "
                f"Supported: {', '.join(sorted(self.SUPPORTED_REASONING_PROVIDERS))}."
            )
        return normalized

    def _generate_openai_reasoning_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_format: Dict[str, Any] | None,
        user_id: str,
        session_id: str,
    ) -> str:
        llm_result = self.llm_provider.generate_reply_with_messages(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            session_id=f"{self._clean_text(session_id) or 'default_session'}_planner",
            user_id=self._clean_text(user_id) or "anonymous",
            include_history=False,
            store_history=False,
            response_format=response_format,
        )
        return llm_result.text

    def _generate_text(self, *, system_prompt: str, user_prompt: str, fallback: str) -> str:
        try:
            raw = self.qwen_client.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                generation_overrides={
                    "max_new_tokens": self.BOARD_PIPELINE_STAGE_MAX_NEW_TOKENS,
                },
            )
            clean = raw.strip()
            return clean or fallback
        except Exception:
            return fallback

    def _default_step_one_plan(self, prompt: str) -> Dict[str, Any]:
        request_kind = self._default_request_kind(prompt)
        return {
            "stage": "step_1_mcp",
            "step_number": 1,
            "chain_position": "mcp layer",
            "needs_mcps": False,
            "request_kind": request_kind,
            "memory_hint": self._default_memory_type(prompt, request_kind),
            "reasoning_summary": "Fallback step 1 plan.",
            "why_this_is_part_of_the_chain": "This is the MCP decision layer.",
            "board_intent": "Create one active board focus for the request.",
            "speech_intent": "Explain what was done in one shared response.",
            "mcp_calls": [],
        }

    def _default_step_two_plan(self, prompt: str, step_one: Dict[str, Any], current_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        focus_name = self._generate_focus_object_name(prompt, current_results)
        focus_text = self._default_focus_text(prompt, current_results)
        memory_type = self._normalize_memory_type(step_one.get("memory_hint"))
        return {
            "stage": "step_2_board",
            "step_number": 2,
            "chain_position": "board interaction",
            "cycle_back_to_step_one": False,
            "reasoning_summary": "Fallback step 2 plan.",
            "board_explanation": "Create one current board object and make room around it.",
            "memory_plan": {
                "default_memory_type": memory_type,
                "why": "Fallback memory choice from step 1.",
            },
            "focus_object": {
                "name": focus_name,
                "text": focus_text,
                "width": 320.0,
                "height": 220.0,
                "memory_type": memory_type,
                "delete_after_click": memory_type == "instant",
                "linked_call_ids": self._normalize_linked_call_ids([], current_results),
                "result_title": focus_text,
                "result_summary": "",
            },
            "additional_mcp_calls": [],
            "board_commands": [],
            "result_bindings": [],
        }

    def _default_mcp_calls(self, prompt: str, request_kind: str) -> List[Dict[str, Any]]:
        return []

    def _looks_like_new_action_memory_request(self, prompt: str) -> bool:
        lowered = prompt.lower()
        activity_markers = {
            "i went",
            "i did",
            "i tried",
            "i started",
            "i took",
            "i had a",
            "i have been going",
            "today i",
            "after i",
            "went on a",
            "went for a",
            "hike",
            "walk",
            "run",
            "gym",
            "exercise",
            "worked out",
            "went outside",
            "outdoors",
        }
        memory_markers = {
            "remember",
            "memory",
            "profile",
            "update",
            "this helped",
            "felt better after",
            "made me feel",
            "uplift",
            "energized after",
            "happy after",
        }
        has_activity = any(marker in lowered for marker in activity_markers)
        has_memory_signal = any(marker in lowered for marker in memory_markers)
        return has_activity and (has_memory_signal or "today" in lowered or "after" in lowered)

    def _looks_like_fetch_request(self, prompt: str) -> bool:
        lowered = prompt.lower()
        return any(
            marker in lowered
            for marker in {
                "what should i do",
                "what can i do",
                "recommend",
                "suggest",
                "help me",
                "need something",
                "what fits",
                "what would help",
                "give me something",
            }
        )

    def _default_request_kind(self, prompt: str) -> str:
        lowered = prompt.lower()
        profile_keywords = {
            "feel",
            "feeling",
            "emotion",
            "emotional",
            "sad",
            "depressed",
            "depression",
            "happy",
            "lonely",
            "anxious",
            "energy",
            "energized",
            "uplift",
            "hike",
            "friend",
            "memory",
            "remember",
            "need",
            "want",
            "profile",
            "connect",
            "connection",
            "someone",
            "person",
            "people",
            "match",
            "i am",
            "i'm",
            "i like",
            "i enjoy",
            "about me",
        }
        mechanical_keywords = {
            "open",
            "show",
            "launch",
            "search",
            "look up",
            "mechanical",
            "widget",
        }
        profile_hit = any(keyword in lowered for keyword in profile_keywords)
        mechanical_hit = any(keyword in lowered for keyword in mechanical_keywords)
        if profile_hit and mechanical_hit:
            return "mixed"
        if profile_hit:
            return "profile"
        return "mechanical"

    def _default_memory_type(self, prompt: str, request_kind: str) -> str:
        lowered = prompt.lower()
        if any(keyword in lowered for keyword in {"friend", "important doc", "important", "document", "remember"}):
            return "memory"
        if any(keyword in lowered for keyword in {"open", "launch", "app", "phone"}):
            return "ram"
        if request_kind == "profile":
            return "memory"
        return "instant"

    def _generate_focus_object_name(self, prompt: str, current_results: List[Dict[str, Any]]) -> str:
        base = self._slugify(self._default_focus_text(prompt, current_results))
        return base or f"agent_object_{uuid.uuid4().hex[:8]}"

    def _default_focus_text(self, prompt: str, current_results: List[Dict[str, Any]]) -> str:
        if current_results:
            first = current_results[0]
            compact_title = self._summarize_focus_title(first)
            if compact_title:
                return compact_title
        words = re.findall(r"[0-9A-Za-zÐ-Ð¯Ð°-Ñ]+", prompt)[:4]
        return self._clip_focus_title(" ".join(words) or "Agent result")

    def _summarize_focus_title(self, payload: Dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return ""

        mcp_id = self._clean_text(payload.get("mcp_id"))
        tool_name = self._normalize_tool_name(payload.get("tool_name"))
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}

        if mcp_id == "connections" and tool_name == "find_connection":
            user = result.get("user") if isinstance(result.get("user"), dict) else {}
            candidate = self._clean_text(user.get("display_name") or user.get("name"))
            if candidate:
                return self._clip_focus_title(candidate)
        elif mcp_id == "connections" and tool_name == "update_profile":
            return "Profile Update"
        elif mcp_id == "phone_command" and tool_name == "open_phone_command":
            return "Phone Command"
        elif tool_name == "fetch_action":
            chosen = result.get("result") if isinstance(result.get("result"), dict) else {}
            candidate = self._clean_text(chosen.get("name"))
            if candidate:
                return self._clip_focus_title(candidate)
        elif tool_name == "add_action":
            action = result.get("action") if isinstance(result.get("action"), dict) else {}
            candidate = self._clean_text(action.get("name"))
            if candidate:
                return self._clip_focus_title(candidate)
        elif tool_name == "conversation":
            user = result.get("user") if isinstance(result.get("user"), dict) else {}
            candidate = self._clean_text(user.get("name") or user.get("description"))
            if candidate:
                return self._clip_focus_title(candidate)

        summary = self._clean_text(payload.get("summary"))
        if not summary or summary.startswith("{") or summary.startswith("["):
            return ""

        summary = re.sub(r"^(fetch chose|added action memory for)\s+", "", summary, flags=re.IGNORECASE)
        summary = summary.rstrip(".:;,- ")
        return self._clip_focus_title(summary)

    def _clip_focus_title(self, value: str) -> str:
        clean = self._clean_text(value)
        if not clean:
            return ""
        words = clean.split()
        compact = " ".join(words[: self.FOCUS_TITLE_MAX_WORDS])
        if len(compact) > self.FOCUS_TITLE_MAX_CHARS:
            compact = compact[: self.FOCUS_TITLE_MAX_CHARS].rstrip()
        return compact.rstrip(".:;,- ")

    def _coerce_focus_title(
        self,
        value: Any,
        *,
        prompt: str,
        current_results: List[Dict[str, Any]],
    ) -> str:
        sanitized = self._sanitize_focus_title_candidate(value)
        if sanitized:
            return sanitized
        return self._default_focus_text(prompt, current_results)

    def _coerce_focus_object_name(self, value: Any, *, focus_text: str) -> str:
        clean_value = self._clean_text(value)
        candidate = ""
        if clean_value and not self._looks_like_structured_title(clean_value):
            candidate = self._slugify(clean_value)
        if not candidate:
            candidate = self._slugify(focus_text)
        if not candidate:
            candidate = f"agent_object_{uuid.uuid4().hex[:8]}"
        return candidate[: self.FOCUS_OBJECT_NAME_MAX_CHARS].strip("_")

    def _sanitize_focus_title_candidate(self, value: Any) -> str:
        clean = self._clean_text(value).strip("\"'`")
        if not clean:
            return ""
        if self._looks_like_structured_title(clean):
            return ""
        normalized = clean.replace("_", " ")
        normalized = re.sub(r"[\"'`]+", "", normalized)
        normalized = re.sub(r"[^0-9A-Za-zÐ-Ð¯Ð°-Ñ ]+", " ", normalized)
        normalized = " ".join(normalized.split())
        if not normalized:
            return ""
        return self._clip_focus_title(normalized)

    def _looks_like_structured_title(self, value: str) -> bool:
        lowered = value.lower()
        structured_markers = (
            "{",
            "}",
            "[",
            "]",
            "linked_results",
            "result_summary",
            "result_title",
            "\"payload\"",
            "\"result\"",
            "\"arguments\"",
            "\"detail\"",
            "\"name\"",
            "\"text\"",
        )
        if any(marker in lowered for marker in structured_markers):
            return True
        if len(value) > 120:
            return True
        if value.count(":") >= 2:
            return True
        if value.count(",") >= 3 and len(value.split()) >= 6:
            return True
        return False

    def _fallback_speech_response(self, prompt: str, mcp_results: List[Dict[str, Any]], step_two: Dict[str, Any]) -> str:
        focus = step_two.get("focus_object", {})
        focus_text = self._clean_text(focus.get("text"))
        if mcp_results:
            summary = self._clean_text(mcp_results[0].get("summary"))
            if summary:
                return f"{summary} I also moved the board around {focus_text or 'the new object'} so the result is front and center."
        return f"I worked through your request and put a focused board object up for {focus_text or prompt}."

    def _fallback_parallel_speech_response(self, prompt: str) -> str:
        focus = self._clean_text(prompt)
        if focus:
            return (
                f"Обработих заявката ти за {focus} и подготвих резултата. "
                "Ще ти го покажа на дъската след малко."
            )
        return "Обработих заявката ти и подготвих резултата. Ще ти го покажа на дъската след малко."

    @staticmethod
    def _slugify(value: str) -> str:
        lowered = value.lower()
        lowered = re.sub(r"[^a-z0-9]+", "_", lowered)
        lowered = re.sub(r"_+", "_", lowered)
        return lowered.strip("_")

    @staticmethod
    def _merge_tags(raw_tags: Any, extra_tags: List[str]) -> List[str]:
        tags: List[str] = []
        if isinstance(raw_tags, list):
            tags.extend(" ".join(str(item).strip().split()) for item in raw_tags if str(item).strip())
        if isinstance(extra_tags, list):
            tags.extend(" ".join(str(item).strip().split()) for item in extra_tags if str(item).strip())
        deduped: List[str] = []
        seen: set[str] = set()
        for tag in tags:
            if not tag or tag in seen:
                continue
            seen.add(tag)
            deduped.append(tag)
        return deduped

    @staticmethod
    def _normalize_tool_name(value: Any) -> str:
        tool_name = " ".join(str(value or "").strip().split()).lower()
        tool_name = tool_name.replace("-", "_").replace(" ", "_")
        return tool_name

    @staticmethod
    def _clean_text(value: Any) -> str:
        return " ".join(str(value or "").strip().split())

    @classmethod
    def _clean_jsonish(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                cls._clean_text(key): cls._clean_jsonish(item)
                for key, item in value.items()
                if cls._clean_text(key)
            }
        if isinstance(value, list):
            return [cls._clean_jsonish(item) for item in value]
        if isinstance(value, str):
            return cls._clean_text(value)
        return value

    @staticmethod
    def _to_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        if value is None:
            return default
        return bool(value)

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _normalize_memory_type(value: Any) -> str:
        clean = " ".join(str(value or "").strip().split()).lower()
        if clean in {"instant", "ram", "memory"}:
            return clean
        return "ram"

