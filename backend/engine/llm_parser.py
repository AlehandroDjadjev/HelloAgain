import ast
import json
import re
from typing import Any, Dict, List

from .qwen_config import QwenConfig
from .prompts import build_parser_step_one_system_prompt, build_parser_step_two_system_prompt
from .qwen_worker_client import QwenWorkerClient
from voice_gateway.services.providers import OpenAILLMProvider

ParserConfig = QwenConfig
PARSER_STEP_MAX_NEW_TOKENS = 384
PARSER_STEP_JSON_CONTINUATION_BUDGET = 128
BLOCKED_ATTRIBUTE_NAMES = {
    "need",
    "mood",
    "context",
    "feeling",
    "state",
    "emotion",
    "desire",
    "attribute",
    "attributes",
    "desired_attributes",
    "opposite_attributes",
    "prompt_context",
    "user_state",
    "action_candidate",
    "edge_signal",
}


class QwenPromptParser:
    def __init__(
        self,
        config: ParserConfig | None = None,
        *,
        qwen_client: QwenWorkerClient | None = None,
        llm_provider: OpenAILLMProvider | None = None,
    ) -> None:
        self.config = config or ParserConfig()
        self._client = qwen_client or QwenWorkerClient(self.config)
        self._llm_provider = llm_provider or OpenAILLMProvider()

    @staticmethod
    def _extract_json(raw_text: str) -> Dict[str, Any]:
        raw_text = raw_text.strip()
        candidates = QwenPromptParser._json_candidates(raw_text)
        if not candidates:
            raise ValueError(f"Parser output did not contain JSON. Output was: {raw_text[:1000]}")
        for payload in candidates:
            parsed = QwenPromptParser._parse_candidate(payload)
            if isinstance(parsed, dict):
                return parsed
        raise ValueError(f"Parser output contained invalid JSON. Output was: {raw_text[:2000]}")

    @staticmethod
    def _json_candidates(raw_text: str) -> List[str]:
        candidates: List[str] = []
        start = None
        depth = 0
        in_string = False
        escape = False

        for idx, char in enumerate(raw_text):
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
                continue

            if char == "{":
                if depth == 0:
                    start = idx
                depth += 1
                continue

            if char == "}":
                if depth == 0:
                    continue
                depth -= 1
                if depth == 0 and start is not None:
                    candidates.append(raw_text[start : idx + 1])
                    start = None

        if not candidates:
            start = raw_text.find("{")
            end = raw_text.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidates.append(raw_text[start : end + 1])
        return candidates

    @staticmethod
    def _parse_candidate(payload: str) -> Dict[str, Any] | None:
        cleaned = QwenPromptParser._cleanup_json_text(payload)
        variants = [payload, cleaned, QwenPromptParser._repair_truncated_json(cleaned)]
        seen: set[str] = set()
        for variant in variants:
            variant = variant.strip()
            if not variant or variant in seen:
                continue
            seen.add(variant)
            try:
                parsed = json.loads(variant)
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                pass
            try:
                parsed = ast.literal_eval(QwenPromptParser._pythonize_json_text(variant))
                return parsed if isinstance(parsed, dict) else None
            except (SyntaxError, ValueError):
                pass
        return None

    @staticmethod
    def _cleanup_json_text(payload: str) -> str:
        cleaned = payload.strip()
        cleaned = re.sub(r"^\s*```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
        cleaned = re.sub(r"(\{|,)\s*-?\d+(?:\.\d+)?\s*,\s*(?=\")", r"\1 ", cleaned)
        cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
        return cleaned

    @staticmethod
    def _repair_truncated_json(payload: str) -> str:
        text = payload.strip()
        if not text:
            return text

        start = text.find("{")
        if start == -1:
            return text
        text = text[start:]

        in_string = False
        escape = False
        stack: List[str] = []
        safe_cut = -1

        for idx, char in enumerate(text):
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
                continue
            if char == "{":
                stack.append("}")
            elif char == "[":
                stack.append("]")
            elif char in {"}", "]"}:
                if stack and stack[-1] == char:
                    stack.pop()
                safe_cut = idx + 1
            elif char == ",":
                safe_cut = idx

        if safe_cut != -1:
            text = text[:safe_cut].rstrip()
            text = re.sub(r",\s*$", "", text)

        if in_string:
            text += '"'

        closes: List[str] = []
        in_string = False
        escape = False
        stack = []
        for char in text:
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                stack.append("}")
            elif char == "[":
                stack.append("]")
            elif char in {"}", "]"} and stack and stack[-1] == char:
                stack.pop()
        while stack:
            closes.append(stack.pop())
        repaired = text + "".join(closes)
        repaired = re.sub(r"(\{|,)\s*-?\d+(?:\.\d+)?\s*,\s*(?=\")", r"\1 ", repaired)
        repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
        return repaired

    @staticmethod
    def _pythonize_json_text(payload: str) -> str:
        converted = payload
        converted = re.sub(r"\btrue\b", "True", converted)
        converted = re.sub(r"\bfalse\b", "False", converted)
        converted = re.sub(r"\bnull\b", "None", converted)
        return converted

    @staticmethod
    def _to_list(value: Any) -> List[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, dict):
            return [value]
        return []

    @staticmethod
    def _to_items(payload: Any, explicit_keys: set[str]) -> List[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, tuple):
            return list(payload)
        if isinstance(payload, dict):
            if any(key in payload for key in explicit_keys):
                return [payload]
            return [{key: value} for key, value in payload.items()]
        return []

    @staticmethod
    def _clean_text(value: Any) -> str:
        return " ".join(str(value or "").strip().split())

    def _generate_and_parse_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        step_label: str,
    ) -> Dict[str, Any]:
        errors: List[str] = []

        try:
            openai_result = self._llm_provider.generate_reply_with_messages(
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                session_id=f"graph_parser_{step_label}",
                user_id="graph_parser",
                include_history=False,
                store_history=False,
            )
            return self._extract_json(openai_result.text)
        except Exception as exc:
            errors.append(f"openai: {exc}")

        try:
            qwen_raw = self._client.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                generation_overrides={
                    "max_new_tokens": PARSER_STEP_MAX_NEW_TOKENS,
                    "json_continuation_budget": PARSER_STEP_JSON_CONTINUATION_BUDGET,
                },
            )
            return self._extract_json(qwen_raw)
        except Exception as exc:
            errors.append(f"qwen: {exc}")

        raise RuntimeError("Graph parser generation failed. " + " | ".join(errors))

    @classmethod
    def _clean_attribute_name(cls, value: Any) -> str:
        return cls._clean_text(value).lower()

    @classmethod
    def _is_blocked_attribute_name(cls, value: str) -> bool:
        return value in BLOCKED_ATTRIBUTE_NAMES

    @staticmethod
    def _to_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "yes", "1"}:
                return True
            if lowered in {"false", "no", "0"}:
                return False
        if value is None:
            return default
        return bool(value)

    @staticmethod
    def _to_float(value: Any, default: float | None = 0.0) -> float | None:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _clamp_score(cls, value: Any, default: float = 0.0) -> float:
        parsed = cls._to_float(value, default=default)
        parsed = default if parsed is None else parsed
        return max(-1.0, min(1.0, float(parsed)))

    @classmethod
    def _extract_named_score(
        cls,
        item: Any,
        *,
        explicit_name_keys: tuple[str, ...],
        explicit_score_keys: tuple[str, ...],
        reserved_keys: set[str],
    ) -> tuple[str, Any]:
        if not isinstance(item, dict):
            return "", None

        for key in explicit_name_keys:
            value = item.get(key)
            if isinstance(value, str) and cls._clean_attribute_name(value):
                score = None
                for score_key in explicit_score_keys:
                    if score_key in item:
                        score = item.get(score_key)
                        break
                return cls._clean_attribute_name(value), score

        dynamic_keys = [key for key in item.keys() if key not in reserved_keys]
        if not dynamic_keys:
            return "", None

        name = cls._clean_attribute_name(dynamic_keys[0])
        score = item.get(dynamic_keys[0])
        return name, score

    @classmethod
    def _expand_named_item(
        cls,
        item: Any,
        *,
        explicit_name_keys: tuple[str, ...],
        reserved_keys: set[str],
        default_name_key: str,
    ) -> Dict[str, Any]:
        if not isinstance(item, dict):
            return {}

        for key in explicit_name_keys:
            value = item.get(key)
            if isinstance(value, str) and cls._clean_attribute_name(value):
                return dict(item)

        dynamic_keys = [key for key in item.keys() if key not in reserved_keys]
        if not dynamic_keys:
            return dict(item)

        dynamic_key = dynamic_keys[0]
        dynamic_name = cls._clean_attribute_name(dynamic_key)
        if not dynamic_name:
            return dict(item)

        dynamic_value = item.get(dynamic_key)
        if isinstance(dynamic_value, dict):
            expanded = dict(dynamic_value)
            expanded.setdefault(default_name_key, dynamic_name)
            return expanded

        expanded = dict(item)
        expanded[default_name_key] = dynamic_name
        if (
            "score" not in expanded
            and "target_score" not in expanded
            and "initial_score" not in expanded
            and "delta" not in expanded
        ):
            expanded["score"] = dynamic_value
        return expanded

    @classmethod
    def _normalize_new_attributes(cls, payload: Any) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in cls._to_items(payload, explicit_keys={"name", "attribute", "initial_score", "score", "target_score"}):
            item = cls._expand_named_item(
                item,
                explicit_name_keys=("name", "attribute"),
                reserved_keys={"name", "attribute", "initial_score", "score", "target_score", "reason"},
                default_name_key="name",
            )
            name, inferred_score = cls._extract_named_score(
                item,
                explicit_name_keys=("name", "attribute"),
                explicit_score_keys=("initial_score", "score", "target_score"),
                reserved_keys={"name", "attribute", "initial_score", "score", "target_score", "reason"},
            )
            if not name or name in seen:
                continue
            if cls._is_blocked_attribute_name(name):
                continue
            seen.add(name)
            results.append(
                {
                    "name": name,
                    "initial_score": cls._clamp_score(
                        item.get("initial_score", item.get("score", item.get("target_score", inferred_score if inferred_score is not None else 0.0)))
                    ),
                    "reason": cls._clean_text(item.get("reason")),
                }
            )
        return results

    @classmethod
    def _normalize_user_updates(cls, payload: Any) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in cls._to_items(payload, explicit_keys={"attribute", "name", "target_score", "score", "delta", "explicit_decay"}):
            item = cls._expand_named_item(
                item,
                explicit_name_keys=("attribute", "name"),
                reserved_keys={"attribute", "name", "target_score", "score", "delta", "explicit_decay", "reason"},
                default_name_key="attribute",
            )
            name, inferred_score = cls._extract_named_score(
                item,
                explicit_name_keys=("attribute", "name"),
                explicit_score_keys=("target_score", "score"),
                reserved_keys={"attribute", "name", "target_score", "score", "delta", "explicit_decay", "reason"},
            )
            if not name or name in seen:
                continue
            if cls._is_blocked_attribute_name(name):
                continue
            seen.add(name)
            target_score = item.get("target_score")
            delta = item.get("delta")
            if target_score is None and delta is None:
                if item.get("score") is not None:
                    target_score = item.get("score")
                elif inferred_score is not None:
                    target_score = inferred_score
            results.append(
                {
                    "attribute": name,
                    "target_score": None if target_score is None else cls._clamp_score(target_score),
                    "delta": None if delta is None else cls._clamp_score(delta),
                    "reason": cls._clean_text(item.get("reason")),
                    "explicit_decay": cls._to_bool(item.get("explicit_decay"), default=False),
                }
            )
        return results

    @classmethod
    def _normalize_prompt_attributes(cls, payload: Any) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in cls._to_items(
            payload,
            explicit_keys={"attribute", "name", "score", "target_score", "initial_score", "delta", "should_update_user", "update_user", "explicit_decay"},
        ):
            item = cls._expand_named_item(
                item,
                explicit_name_keys=("attribute", "name"),
                reserved_keys={
                    "attribute",
                    "name",
                    "score",
                    "target_score",
                    "initial_score",
                    "delta",
                    "should_update_user",
                    "update_user",
                    "explicit_decay",
                    "reason",
                },
                default_name_key="attribute",
            )
            name, inferred_score = cls._extract_named_score(
                item,
                explicit_name_keys=("attribute", "name"),
                explicit_score_keys=("score", "target_score", "initial_score", "delta"),
                reserved_keys={
                    "attribute",
                    "name",
                    "score",
                    "target_score",
                    "initial_score",
                    "delta",
                    "should_update_user",
                    "update_user",
                    "explicit_decay",
                    "reason",
                },
            )
            if not name or name in seen:
                continue
            if cls._is_blocked_attribute_name(name):
                continue
            seen.add(name)
            score = item.get("score")
            if score is None:
                score = item.get("target_score", item.get("initial_score", item.get("delta", inferred_score if inferred_score is not None else 0.0)))
            parsed_score = cls._clamp_score(score)
            if abs(parsed_score) < 1e-6:
                continue
            should_update_user = item.get("should_update_user")
            if should_update_user is None:
                should_update_user = item.get("update_user")
            results.append(
                {
                    "attribute": name,
                    "score": parsed_score,
                    "reason": cls._clean_text(item.get("reason")),
                    "should_update_user": cls._to_bool(should_update_user, default=True),
                    "explicit_decay": cls._to_bool(item.get("explicit_decay"), default=False),
                }
            )
        return results

    @classmethod
    def _normalize_runtime_attributes(cls, payload: Any, *, limit: int | None = None) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in cls._to_items(
            payload,
            explicit_keys={"attribute", "name", "score", "target_score", "initial_score", "delta"},
        ):
            item = cls._expand_named_item(
                item,
                explicit_name_keys=("attribute", "name"),
                reserved_keys={"attribute", "name", "score", "target_score", "initial_score", "delta", "reason"},
                default_name_key="attribute",
            )
            name, inferred_score = cls._extract_named_score(
                item,
                explicit_name_keys=("attribute", "name"),
                explicit_score_keys=("score", "target_score", "initial_score", "delta"),
                reserved_keys={"attribute", "name", "score", "target_score", "initial_score", "delta", "reason"},
            )
            if not name or name in seen:
                continue
            if cls._is_blocked_attribute_name(name):
                continue
            seen.add(name)
            score = item.get("score")
            if score is None:
                score = item.get("target_score", item.get("initial_score", item.get("delta", inferred_score if inferred_score is not None else 0.0)))
            parsed_score = cls._clamp_score(score)
            if abs(parsed_score) < 1e-6:
                continue
            results.append(
                {
                    "attribute": name,
                    "score": parsed_score,
                    "reason": cls._clean_text(item.get("reason")),
                }
            )
            if limit is not None and len(results) >= limit:
                break
        return results

    @classmethod
    def _normalize_action_candidate(cls, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            payload = {}
        attribute_map: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in cls._to_items(
            payload.get("attribute_map") or payload.get("attributes"),
            explicit_keys={"attribute", "name", "score", "target_score"},
        ):
            item = cls._expand_named_item(
                item,
                explicit_name_keys=("attribute", "name"),
                reserved_keys={"attribute", "name", "score", "target_score", "reason"},
                default_name_key="attribute",
            )
            name, inferred_score = cls._extract_named_score(
                item,
                explicit_name_keys=("attribute", "name"),
                explicit_score_keys=("score", "target_score"),
                reserved_keys={"attribute", "name", "score", "target_score", "reason"},
            )
            if not name or name in seen:
                continue
            if cls._is_blocked_attribute_name(name):
                continue
            seen.add(name)
            parsed_score = cls._clamp_score(item.get("score", item.get("target_score", inferred_score if inferred_score is not None else 0.0)))
            if abs(parsed_score) < 1e-6:
                continue
            attribute_map.append(
                {
                    "attribute": name,
                    "score": parsed_score,
                    "reason": cls._clean_text(item.get("reason")),
                }
            )
        return {
            "name": cls._clean_text(payload.get("name")),
            "description": cls._clean_text(payload.get("description")),
            "wanted_strength": cls._clamp_score(payload.get("wanted_strength", 0.7), default=0.7),
            "attribute_map": attribute_map,
            "desired_attribute_map": cls._normalize_runtime_attributes(
                payload.get("desired_attribute_map")
                or payload.get("desired_attributes")
                or payload.get("positive_attribute_map")
                or payload.get("positive_attributes"),
                limit=2,
            ),
        }

    @classmethod
    def _normalize_edge_signal(cls, mode: str, payload: Any) -> Dict[str, Any]:
        defaults = {"add": "desire", "fetch": "fetch", "conversation": "memory"}
        if not isinstance(payload, dict):
            payload = {}
        kind = cls._clean_text(payload.get("kind")).lower() or defaults.get(mode, "neutral")
        if kind not in {"neutral", "desire", "positive", "negative", "memory", "fetch"}:
            kind = defaults.get(mode, "neutral")
        default_strength = {"add": 0.72, "fetch": 0.65, "conversation": 0.35}.get(mode, 0.4)
        return {
            "kind": kind,
            "strength": cls._clamp_score(payload.get("strength", default_strength), default=default_strength),
            "reason": cls._clean_text(payload.get("reason")),
        }

    @classmethod
    def _normalize_plan(cls, mode: str, raw_plan: Dict[str, Any]) -> Dict[str, Any]:
        payload = raw_plan.get("plan") if isinstance(raw_plan.get("plan"), dict) else raw_plan
        if not isinstance(payload, dict):
            payload = {}

        user_profile_update = payload.get("user_profile_update") or {}
        user_state = payload.get("user_state") or {}
        prompt_context = payload.get("prompt_context") or {}

        normalized_desired_attrs = cls._normalize_runtime_attributes(
            prompt_context.get("desired_attributes")
            or prompt_context.get("desired_positive_attributes"),
            limit=2,
        )
        normalized_opposite_attrs = cls._normalize_runtime_attributes(
            prompt_context.get("opposite_attributes")
            or prompt_context.get("solution_attributes")
            or prompt_context.get("counter_attributes"),
            limit=2,
        )
        normalized_updates = cls._normalize_user_updates(
            user_state.get("updates") or payload.get("user_updates") or payload.get("attribute_updates")
        )
        normalized_new_attributes = cls._normalize_new_attributes(
            user_state.get("new_attributes") or payload.get("new_attributes")
        )
        new_attribute_names = {item["name"] for item in normalized_new_attributes}
        normalized_updates = [item for item in normalized_updates if item["attribute"] not in new_attribute_names]

        return {
            "mode": cls._clean_text(payload.get("mode") or payload.get("intent") or mode).lower() or mode,
            "summary": cls._clean_text(payload.get("summary") or payload.get("analysis") or payload.get("state_summary")),
            "user_profile_update": {
                "description_append": cls._clean_text(user_profile_update.get("description_append")),
                "active_state_summary": cls._clean_text(user_profile_update.get("active_state_summary")),
            },
            "user_state": {
                "new_attributes": normalized_new_attributes,
                "updates": normalized_updates,
            },
            "prompt_context": {
                "emotion_tone": cls._clean_text(prompt_context.get("emotion_tone")),
                "conversation_notes": cls._clean_text(prompt_context.get("conversation_notes")),
                "all_relevant_attributes": cls._normalize_prompt_attributes(
                    prompt_context.get("all_relevant_attributes")
                ),
                "desired_attributes": normalized_desired_attrs,
                "opposite_attributes": normalized_opposite_attrs,
            },
            "action_candidate": cls._normalize_action_candidate(payload.get("action_candidate") or {}),
            "edge_signal": cls._normalize_edge_signal(
                mode,
                payload.get("edge_signal") or payload.get("explicit_user_action_signal") or {},
            ),
        }

    @staticmethod
    def _unwrap_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        inner = payload.get("plan") if isinstance(payload.get("plan"), dict) else payload
        return inner if isinstance(inner, dict) else {}

    @staticmethod
    def _merge_step_payloads(step_one: Dict[str, Any], step_two: Dict[str, Any]) -> Dict[str, Any]:
        step_one_payload = QwenPromptParser._unwrap_payload(step_one)
        step_two_payload = QwenPromptParser._unwrap_payload(step_two)

        merged = dict(step_one_payload)
        merged["user_state"] = step_one_payload.get("user_state") or {}
        merged["prompt_context"] = step_one_payload.get("prompt_context") or {}
        merged["action_candidate"] = step_two_payload.get("action_candidate") or {}
        merged["edge_signal"] = (
            step_two_payload.get("edge_signal")
            or step_two_payload.get("explicit_user_action_signal")
            or {}
        )

        if not merged.get("mode"):
            merged["mode"] = step_two_payload.get("mode") or step_two_payload.get("intent") or ""
        if not merged.get("summary"):
            merged["summary"] = (
                step_one_payload.get("summary")
                or step_one_payload.get("analysis")
                or step_one_payload.get("state_summary")
                or step_two_payload.get("summary")
                or step_two_payload.get("analysis")
                or step_two_payload.get("state_summary")
                or ""
            )
        if not merged.get("user_profile_update") and step_two_payload.get("user_profile_update"):
            merged["user_profile_update"] = step_two_payload.get("user_profile_update")
        return merged

    def parse(
        self,
        *,
        mode: str,
        user_prompt: str,
        attribute_inventory_text: str,
        action_inventory_text: str,
    ) -> Dict[str, Any]:
        step_one_prompt = build_parser_step_one_system_prompt(mode, attribute_inventory_text)
        step_one_parsed = self._generate_and_parse_json(
            system_prompt=step_one_prompt,
            user_prompt=user_prompt,
            step_label="step_one",
        )

        step_two_prompt = build_parser_step_two_system_prompt(
            mode,
            action_inventory_text,
            step_one_parsed,
        )
        step_two_parsed = self._generate_and_parse_json(
            system_prompt=step_two_prompt,
            user_prompt=user_prompt,
            step_label="step_two",
        )
        combined = self._merge_step_payloads(step_one_parsed, step_two_parsed)
        normalized = self._normalize_plan(mode=mode, raw_plan=combined)
        normalized["raw_response"] = {
            "step_one": step_one_parsed,
            "step_two": step_two_parsed,
            "combined": combined,
        }
        return normalized
