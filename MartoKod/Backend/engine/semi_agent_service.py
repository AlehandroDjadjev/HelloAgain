from __future__ import annotations

import math
import re
import uuid
from copy import deepcopy
from typing import Any, Dict, List

from .custom_mcp_registry import CustomMcpRegistry
from .graph_service import GraphService
from .llm_parser import QwenPromptParser
from .qwen_worker_client import QwenWorkerClient
from .semi_agent_prompts import (
    build_step_one_mcp_prompt,
    build_step_three_speech_prompt,
    build_step_two_board_prompt,
)
from .whiteboard_memory import WhiteboardMemoryStore


class SemiAgentService:
    SUPPORTED_TOOL_NAMES = {"add_action", "fetch_action", "conversation"}
    BOARD_PIPELINE_STAGE_MAX_NEW_TOKENS = 256
    BOARD_PIPELINE_JSON_CONTINUATION_BUDGET = 0

    def __init__(
        self,
        *,
        graph_service: GraphService | None = None,
        qwen_client: QwenWorkerClient | None = None,
        registry: CustomMcpRegistry | None = None,
        board_memory: WhiteboardMemoryStore | None = None,
    ) -> None:
        self.graph_service = graph_service or GraphService()
        self.qwen_client = qwen_client or QwenWorkerClient()
        self.registry = registry or CustomMcpRegistry()
        self.board_memory = board_memory or WhiteboardMemoryStore()

    def get_registry_payload(self, *, base_url: str = "") -> Dict[str, Any]:
        return self.registry.load_registry(base_url=base_url)

    def get_descriptor_payload(self, mcp_id: str, *, base_url: str = "") -> Dict[str, Any]:
        return self.registry.load_descriptor(mcp_id, base_url=base_url)

    def get_board_memory_state(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "board_state": self.board_memory.load_persistent_board_state(),
        }

    def run(self, *, prompt: str, board_state: Dict[str, Any] | None, largest_empty_space: Dict[str, Any] | None) -> Dict[str, Any]:
        clean_prompt = self._clean_text(prompt)
        if not clean_prompt:
            raise ValueError("prompt required")

        normalized_board_state = self.board_memory.normalize_board_state(board_state)
        empty_space_payload = (
            largest_empty_space
            if isinstance(largest_empty_space, dict) and "bbox" in largest_empty_space
            else self.board_memory.find_largest_empty_space(normalized_board_state)
        )

        chain_history: List[Dict[str, Any]] = []
        registry_payload = self.get_registry_payload()

        step_one_raw = self._generate_json(
            system_prompt=build_step_one_mcp_prompt(
                registry=registry_payload,
                chain_history=chain_history,
            ),
            user_prompt=clean_prompt,
            default_payload=self._default_step_one_plan(clean_prompt),
        )
        step_one = self._normalize_step_one_plan(step_one_raw, clean_prompt)
        chain_history.append({"stage": "step_1_mcp", "payload": step_one})

        mcp_results = self._execute_mcp_calls(step_one.get("mcp_calls", []), clean_prompt)
        if mcp_results:
            chain_history.append({"stage": "mcp_results", "payload": mcp_results})

        step_two = self._run_step_two_loop(
            prompt=clean_prompt,
            board_state=normalized_board_state,
            largest_empty_space=empty_space_payload,
            step_one=step_one,
            mcp_results=mcp_results,
            chain_history=chain_history,
        )
        chain_history.append({"stage": "step_2_board", "payload": step_two})

        final_board_commands = step_two.get("board_commands", [])
        final_board_state = self.board_memory.apply_commands(normalized_board_state, final_board_commands)
        registered_bindings = self._prepare_result_bindings(
            step_two=step_two,
            executed_results=mcp_results,
            final_board_state=final_board_state,
        )
        self._attach_bindings_to_commands(final_board_commands, registered_bindings)
        self.board_memory.register_result_bindings(registered_bindings)
        persisted_board_state = self.board_memory.save_persistent_board_state(final_board_state)

        speech_response = self._generate_text(
            system_prompt=build_step_three_speech_prompt(
                original_prompt=clean_prompt,
                step_one_plan=step_one,
                mcp_results=mcp_results,
                step_two_plan=step_two,
                final_board_state=final_board_state,
            ),
            user_prompt=clean_prompt,
            fallback=self._fallback_speech_response(clean_prompt, mcp_results, step_two),
        )

        return {
            "ok": True,
            "prompt": clean_prompt,
            "mcp_registry": registry_payload,
            "step_one": step_one,
            "mcp_results": mcp_results,
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
            "speech_response": speech_response,
        }

    def invoke_mcp(self, *, mcp_id: str, tool_name: str, arguments: Dict[str, Any] | None, fallback_prompt: str = "") -> Dict[str, Any]:
        clean_mcp_id = self._clean_text(mcp_id)
        clean_tool_name = self._normalize_tool_name(tool_name)
        if clean_mcp_id != "gnn_actions":
            raise ValueError(f"Unsupported MCP '{clean_mcp_id}'.")
        if clean_tool_name not in self.SUPPORTED_TOOL_NAMES:
            raise ValueError(f"Unsupported tool '{clean_tool_name}'.")

        arguments = arguments if isinstance(arguments, dict) else {}
        tool_prompt = self._clean_text(arguments.get("prompt") or fallback_prompt)
        if not tool_prompt:
            raise ValueError("prompt required for MCP invocation")

        result = self._dispatch_gnn_tool(clean_tool_name, tool_prompt)
        summary = self._summarize_mcp_result(clean_tool_name, result)
        return {
            "ok": True,
            "mcp_id": clean_mcp_id,
            "tool_name": clean_tool_name,
            "arguments": {"prompt": tool_prompt},
            "summary": summary,
            "result": result,
        }

    def open_board_object(self, *, object_payload: Dict[str, Any] | None) -> Dict[str, Any]:
        object_payload = object_payload if isinstance(object_payload, dict) else {}
        object_name = self._clean_text(object_payload.get("name"))
        result_id = self._clean_text(object_payload.get("resultId") or object_payload.get("result_id"))
        memory_type = self._normalize_memory_type(object_payload.get("memoryType") or object_payload.get("memory_type"))
        delete_after_click = self._to_bool(
            object_payload.get("deleteAfterClick", object_payload.get("delete_after_click")),
            default=memory_type == "instant",
        )

        binding = self.board_memory.resolve_result_binding(result_id)
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

        title = self._clean_text(binding.get("result_title") or binding.get("resultTitle") or object_name or "Board result")
        summary = self._clean_text(binding.get("result_summary") or binding.get("resultSummary"))
        payload = binding.get("payload") if isinstance(binding.get("payload"), dict) else {"payload": binding.get("payload")}

        return {
            "ok": True,
            "found": True,
            "object_name": object_name,
            "board_commands": board_commands,
            "speech_response": summary or f"I opened {title}.",
            "viewer": {
                "title": title,
                "summary": summary,
                "memory_type": binding.get("memory_type"),
                "linked_call_ids": binding.get("linked_call_ids", []),
                "payload": payload,
            },
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
    ) -> Dict[str, Any]:
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
                    chain_history=current_history,
                ),
                user_prompt=prompt,
                default_payload=step_two,
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
            extra_results = self._execute_mcp_calls(extra_calls, prompt)
            if not extra_results:
                break
            current_results.extend(extra_results)
            current_history = list(current_history) + [
                {"stage": "step_2_extra_mcp_results", "payload": extra_results},
            ]

        return step_two

    def _execute_mcp_calls(self, calls: List[Dict[str, Any]], fallback_prompt: str) -> List[Dict[str, Any]]:
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

    def _dispatch_gnn_tool(self, tool_name: str, prompt: str) -> Dict[str, Any]:
        if tool_name == "add_action":
            return self.graph_service.add_action_flow(prompt)
        if tool_name == "fetch_action":
            return self.graph_service.fetch_action_flow(prompt)
        if tool_name == "conversation":
            return self.graph_service.conversation_flow(prompt)
        raise ValueError(f"Unsupported tool '{tool_name}'.")

    def _summarize_mcp_result(self, tool_name: str, result: Dict[str, Any]) -> str:
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
                "payload": {
                    "linked_results": linked_payloads,
                    "object": deepcopy(object_state),
                },
            }
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
                [f"memory:{binding.get('memory_type', 'ram')}"],
            )

    def _normalize_step_one_plan(self, raw: Dict[str, Any], prompt: str) -> Dict[str, Any]:
        raw = raw if isinstance(raw, dict) else {}
        calls = self._normalize_mcp_calls(raw.get("mcp_calls"))
        needs_mcps = self._to_bool(raw.get("needs_mcps"), default=bool(calls))
        request_kind = self._clean_text(raw.get("request_kind")).lower()
        if request_kind not in {"mechanical", "profile", "mixed"}:
            request_kind = self._default_request_kind(prompt)
        memory_hint = self._normalize_memory_type(raw.get("memory_hint") or self._default_memory_type(prompt, request_kind))

        if request_kind == "mechanical" and not calls:
            needs_mcps = False

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
        focus_name = self._clean_text(focus_object.get("name")) or self._generate_focus_object_name(prompt, current_results)
        focus_text = self._clean_text(focus_object.get("text")) or self._default_focus_text(prompt, current_results)
        focus_width = max(220.0, self._to_float(focus_object.get("width"), 320.0))
        focus_height = max(160.0, self._to_float(focus_object.get("height"), 220.0))
        focus_memory_type = self._normalize_memory_type(focus_object.get("memory_type") or default_memory_type)
        focus_delete_after_click = self._to_bool(
            focus_object.get("delete_after_click", focus_object.get("deleteAfterClick")),
            default=focus_memory_type == "instant",
        )
        focus_linked_call_ids = self._normalize_linked_call_ids(focus_object.get("linked_call_ids"), current_results)

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
            focus_title=self._clean_text(focus_object.get("result_title") or focus_text),
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
                "result_title": self._clean_text(focus_object.get("result_title") or focus_text),
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
        items = payload if isinstance(payload, list) else []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            mcp_id = self._clean_text(item.get("mcp_id") or item.get("mcp"))
            tool_name = self._normalize_tool_name(item.get("tool_name") or item.get("tool"))
            if mcp_id != "gnn_actions" or tool_name not in self.SUPPORTED_TOOL_NAMES:
                continue
            arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            prompt = self._clean_text(arguments.get("prompt"))
            results.append(
                {
                    "call_id": self._clean_text(item.get("call_id")) or f"{mcp_id}.{tool_name}.{index}",
                    "mcp_id": mcp_id,
                    "tool_name": tool_name,
                    "arguments": {"prompt": prompt} if prompt else {},
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
                    "result_title": self._clean_text(item.get("result_title") or item.get("resultTitle") or focus_title),
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

    def _generate_json(self, *, system_prompt: str, user_prompt: str, default_payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            raw = self.qwen_client.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                generation_overrides={
                    "max_new_tokens": self.BOARD_PIPELINE_STAGE_MAX_NEW_TOKENS,
                    "json_continuation_budget": self.BOARD_PIPELINE_JSON_CONTINUATION_BUDGET,
                },
            )
            return QwenPromptParser._extract_json(raw)
        except Exception:
            return deepcopy(default_payload)

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
            "needs_mcps": request_kind != "mechanical",
            "request_kind": request_kind,
            "memory_hint": self._default_memory_type(prompt, request_kind),
            "reasoning_summary": "Fallback step 1 plan.",
            "why_this_is_part_of_the_chain": "This is the MCP decision layer.",
            "board_intent": "Create one active board focus for the request.",
            "speech_intent": "Explain what was done in one shared response.",
            "mcp_calls": self._default_mcp_calls(prompt, request_kind),
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
        if request_kind == "mechanical":
            return []
        tool_name = "conversation" if request_kind == "profile" else "fetch_action"
        return [
            {
                "call_id": f"gnn_actions.{tool_name}.1",
                "mcp_id": "gnn_actions",
                "tool_name": tool_name,
                "arguments": {"prompt": prompt},
                "why": "Fallback MCP call based on request type.",
            }
        ]

    def _default_request_kind(self, prompt: str) -> str:
        lowered = prompt.lower()
        profile_keywords = {
            "feel",
            "feeling",
            "emotion",
            "emotional",
            "sad",
            "happy",
            "lonely",
            "anxious",
            "friend",
            "memory",
            "remember",
            "need",
            "want",
            "profile",
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
        words = re.findall(r"[A-Za-z0-9]+", prompt)[:4]
        return " ".join(words) or "Agent result"

    def _summarize_focus_title(self, payload: Dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return ""

        tool_name = self._normalize_tool_name(payload.get("tool_name"))
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}

        if tool_name == "fetch_action":
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
        compact = " ".join(words[:6])
        if len(compact) > 42:
            compact = compact[:42].rstrip()
        return compact.rstrip(".:;,- ")

    def _fallback_speech_response(self, prompt: str, mcp_results: List[Dict[str, Any]], step_two: Dict[str, Any]) -> str:
        focus = step_two.get("focus_object", {})
        focus_text = self._clean_text(focus.get("text"))
        if mcp_results:
            summary = self._clean_text(mcp_results[0].get("summary"))
            if summary:
                return f"{summary} I also moved the board around {focus_text or 'the new object'} so the result is front and center."
        return f"I worked through your request and put a focused board object up for {focus_text or prompt}."

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
        tags.extend(extra_tags)
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
