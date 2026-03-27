"""
StepReasoningService: per-step LLM decision-making with budget-aware context.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from django.conf import settings

from apps.agent_core.enums import ActionSensitivity, ActionType
from apps.agent_core.llm_client import LLMClient, LLMError
from apps.agent_executors.registry import get_executor

from .screen_formatter import (
    SENSITIVE_SENTINEL,
    format_screen_for_llm,
    summarize_step_history,
)
from ..prompts.step_reasoning import (
    STEP_REASONING_SYSTEM_PROMPT,
    build_step_reasoning_user_prompt,
)

if TYPE_CHECKING:
    from apps.agent_sessions.models import AgentSession

logger = logging.getLogger(__name__)

_VALID_ACTION_TYPES: frozenset[str] = frozenset(a.value for a in ActionType)
_VALID_SENSITIVITIES = frozenset(a.value for a in ActionSensitivity)
_VALID_SCROLL_DIRS = frozenset({"up", "down", "left", "right"})

_REQUIRED_PARAMS: dict[str, list[str]] = {
    "OPEN_APP": ["package_name"],
    "TAP_ELEMENT": ["selector"],
    "LONG_PRESS_ELEMENT": ["selector"],
    "FOCUS_ELEMENT": ["selector"],
    "TYPE_TEXT": ["text"],
    "CLEAR_TEXT": [],
    "SCROLL": ["direction"],
    "SWIPE": ["start_x", "start_y", "end_x", "end_y"],
    "BACK": [],
    "HOME": [],
    "WAIT_FOR_APP": ["package_name"],
    "WAIT_FOR_ELEMENT": ["selector"],
    "FIND_ELEMENT": ["selector"],
    "REQUEST_CONFIRMATION": ["action_summary"],
    "ABORT": ["reason"],
    "ASSERT_SCREEN": ["screen_hint"],
    "ASSERT_ELEMENT": ["selector"],
    "GET_SCREEN_STATE": [],
}

_SELECTOR_ACTIONS = frozenset({
    "TAP_ELEMENT",
    "LONG_PRESS_ELEMENT",
    "FOCUS_ELEMENT",
    "FIND_ELEMENT",
    "WAIT_FOR_ELEMENT",
    "ASSERT_ELEMENT",
})

_APP_CONTEXT_HINTS: dict[str, str] = {
    "com.whatsapp": (
        "Common elements: search button (contentDesc often contains 'Search'), "
        "chat items (usually clickable layouts with contact names), and a send "
        "button with contentDesc 'Send' inside chat threads."
    ),
    "com.android.chrome": (
        "Common elements: the address/search bar with contentDesc like "
        "'Search or type web address', page content below it, and toolbar actions "
        "such as tab switcher or menu buttons."
    ),
    "com.google.android.apps.maps": (
        "Common elements: a search field near the top, place result cards, and "
        "route actions such as Directions or Start."
    ),
    "com.google.android.gm": (
        "Common elements: the Compose button, search, inbox thread rows, and "
        "subject/body fields in compose mode."
    ),
    "com.supercell.brawlstars": (
        "Common elements: game home screen buttons, event cards, play buttons, "
        "and modal dialogs. Prefer OPEN_APP first, then act only on clearly visible UI."
    ),
}


@dataclass
class ReasonedStep:
    action_type: str
    params: dict
    reasoning: str
    confidence: float
    is_goal_complete: bool
    requires_confirmation: bool
    sensitivity: str
    raw_llm_response: dict = field(default_factory=dict, repr=False)
    validation_attempts: int = 1
    source: str = "llm"
    fallback_mode: Optional[str] = None
    llm_failure_reason: Optional[str] = None


class StepReasoningService:
    """Stateless service that decides the next action for LLM-driven sessions."""

    def __init__(
        self,
        client: Optional[LLMClient] = None,
        reasoning_provider: Optional[str] = None,
    ) -> None:
        if client is not None:
            self._llm = client
        elif reasoning_provider:
            self._llm = LLMClient.from_reasoning_provider(reasoning_provider)
        else:
            self._llm = LLMClient.from_settings()

    def reason_next_step(
        self,
        goal: str,
        target_app: str,
        entities: dict,
        screen_state: dict,
        step_history: list[dict],
        constraints: dict,
        session: Optional["AgentSession"] = None,
    ) -> ReasonedStep:
        if screen_state.get("is_sensitive"):
            logger.warning(
                "StepReasoningService: sensitive screen detected for session=%s",
                getattr(session, "id", "?"),
            )
            step = _abort_step("sensitive_screen", confidence=1.0)
            self._audit(
                session=session,
                event="LLM_STEP_REASONED",
                payload={
                    "aborted_reason": "sensitive_screen",
                    "skipped_llm_call": True,
                    "screen_hash": screen_state.get("screen_hash", ""),
                },
            )
            return step

        formatted_screen = format_screen_for_llm(
            screen_state,
            token_budget=int(getattr(settings, "LLM_TOKEN_BUDGET_SCREEN_STATE", 6000)),
        )
        logger.debug(
            "StepReasoningService screen for session=%s target_app=%s screen_hash=%s:\n%s",
            getattr(session, "id", "?"),
            target_app,
            screen_state.get("screen_hash", ""),
            formatted_screen,
        )
        screen_header, screen_tree = _split_screen_text(formatted_screen)
        history_text = summarize_step_history(
            step_history,
            max_steps=5,
            token_budget=int(getattr(settings, "LLM_TOKEN_BUDGET_HISTORY", 2000)),
        )
        failure_context = _build_failure_context(step_history)
        goal_progress = _estimate_goal_progress(goal, target_app, screen_state, step_history)
        app_context = _build_app_context(target_app, screen_state)
        live_refs = _extract_refs(screen_state)

        user_prompt = build_step_reasoning_user_prompt(
            goal=goal,
            target_app=target_app,
            entities=entities,
            step_history_text=history_text,
            constraints=constraints,
            screen_header=screen_header,
            screen_tree=screen_tree,
            failure_context=failure_context,
            goal_progress=goal_progress,
            app_context=app_context,
        )

        attempts = 1
        raw, error = self._call_and_validate(
            user_prompt=user_prompt,
            live_refs=live_refs,
            screen_state=screen_state,
            attempt=1,
        )

        if error and raw is not None:
            attempts = 2
            retry_prompt = build_step_reasoning_user_prompt(
                goal=goal,
                target_app=target_app,
                entities=entities,
                step_history_text=history_text,
                constraints=constraints,
                screen_header=screen_header,
                screen_tree=screen_tree,
                validation_error=error,
                failure_context=failure_context,
                goal_progress=goal_progress,
                app_context=app_context,
            )
            raw, error = self._call_and_validate(
                user_prompt=retry_prompt,
                live_refs=live_refs,
                screen_state=screen_state,
                attempt=2,
            )

        if error or raw is None:
            fallback = self._fallback_on_llm_failure(
                goal=goal,
                target_app=target_app,
                screen_state=screen_state,
                error=error or "llm_unavailable",
                raw=raw or {},
                attempts=attempts,
                session=session,
            )
            self._audit(
                session=session,
                event="LLM_STEP_REASONED",
                payload={
                    "goal": goal,
                    "target_app": target_app,
                    "screen_hash": screen_state.get("screen_hash", ""),
                    "raw_response": raw or {},
                    "validation_error": error,
                    "step_history_length": len(step_history),
                    "fallback_mode": fallback.fallback_mode,
                    "fallback_source": fallback.source,
                    "llm_failure_reason": fallback.llm_failure_reason,
                },
            )
            return fallback

        step = _normalize_reasoned_step(
            _build_reasoned_step(raw, attempts=attempts),
            screen_state=screen_state,
        )
        self._audit(
            session=session,
            event="LLM_STEP_REASONED",
            payload={
                "goal": goal,
                "target_app": target_app,
                "screen_hash": screen_state.get("screen_hash", ""),
                "raw_response": raw or {},
                "validation_error": None,
                "step_history_length": len(step_history),
                "source": "llm",
            },
        )
        return step

    def _call_and_validate(
        self,
        user_prompt: str,
        live_refs: frozenset[str],
        screen_state: dict,
        attempt: int,
    ) -> tuple[Optional[dict], Optional[str]]:
        try:
            raw = self._llm.generate(
                system_prompt=STEP_REASONING_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                json_mode=True,
            )
        except LLMError as exc:
            logger.error("StepReasoningService: LLM call failed (attempt %d): %s", attempt, exc)
            return None, str(exc)

        error = _validate_response(raw, live_refs, screen_state)
        return raw, error

    def _fallback_on_llm_failure(
        self,
        goal: str,
        target_app: str,
        screen_state: dict,
        error: str,
        raw: dict,
        attempts: int,
        session: Optional["AgentSession"],
    ) -> ReasonedStep:
        executor = get_executor(target_app)
        current_screen = "unknown"
        if executor is not None:
            try:
                current_screen = executor.infer_screen_hint(screen_state or {})
            except Exception:
                logger.exception("Executor screen hint failed for '%s'", target_app)
                current_screen = "unknown"

            try:
                recovery = executor.get_recovery_action(
                    current_screen=current_screen,
                    expected_screen=current_screen,
                    plan_context={"app_package": target_app, "goal": goal},
                )
            except Exception:
                logger.exception("Executor recovery lookup failed for '%s'", target_app)
                recovery = None

            normalized = _normalize_recovery_action(recovery)
            if normalized is not None:
                logger.warning(
                    "LLM unavailable for session=%s; using executor recovery action %s",
                    getattr(session, "id", "?"),
                    normalized["type"],
                )
                return ReasonedStep(
                    action_type=normalized["type"],
                    params=normalized["params"],
                    reasoning=(
                        f"LLM unavailable after {attempts} attempt(s): {error}. "
                        f"Using executor recovery for screen hint '{current_screen}'."
                    ),
                    confidence=0.25,
                    is_goal_complete=False,
                    requires_confirmation=False,
                    sensitivity=ActionSensitivity.LOW.value,
                    raw_llm_response=raw,
                    validation_attempts=attempts,
                    source="executor_recovery",
                    llm_failure_reason=error,
                )

        logger.error(
            "LLM unavailable for session=%s and no executor recovery exists: %s",
            getattr(session, "id", "?"),
            error,
        )
        return ReasonedStep(
            action_type=ActionType.ABORT.value,
            params={"reason": "llm_unavailable"},
            reasoning=(
                f"LLM unavailable after {attempts} attempt(s): {error}. "
                "No deterministic recovery was available, so manual takeover is required."
            ),
            confidence=0.0,
            is_goal_complete=False,
            requires_confirmation=False,
            sensitivity=ActionSensitivity.HIGH.value,
            raw_llm_response=raw,
            validation_attempts=attempts,
            source="llm_failure",
            fallback_mode="manual_takeover",
            llm_failure_reason=error,
        )

    @staticmethod
    def _audit(
        session: Optional["AgentSession"],
        event: str,
        payload: dict,
    ) -> None:
        if session is None:
            return
        try:
            from apps.audit_log.models import AuditActor
            from apps.audit_log.services import AuditService

            raw = payload.get("raw_response") or {}
            if isinstance(raw, dict) and raw:
                payload["raw_response"] = {
                    **{k: v for k, v in raw.items() if k != "reasoning"},
                    "reasoning": str(raw.get("reasoning") or "")[:500],
                }

            AuditService.record(
                session=session,
                event_type=event,
                actor=AuditActor.SYSTEM,
                payload=payload,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("StepReasoningService: audit write failed: %s", exc)


def _validate_response(
    raw: dict,
    live_refs: frozenset[str],
    screen_state: dict,
) -> Optional[str]:
    if not isinstance(raw, dict):
        return f"LLM response must be a JSON object, got {type(raw).__name__}."

    errors: list[str] = []

    action_type = raw.get("action_type")
    if not action_type:
        errors.append("Missing 'action_type' field.")
    elif action_type not in _VALID_ACTION_TYPES:
        errors.append(
            f"'action_type' value {action_type!r} is not valid. "
            f"Must be one of: {', '.join(sorted(_VALID_ACTION_TYPES))}"
        )
    else:
        params = raw.get("params") or {}
        required = _REQUIRED_PARAMS.get(action_type, [])
        for field_name in required:
            val = params.get(field_name)
            if val is None or val == "" or val == {}:
                errors.append(
                    f"Action {action_type} requires params.{field_name} but it is missing or empty."
                )

        if action_type in _SELECTOR_ACTIONS and not errors:
            selector = params.get("selector") or {}
            if not isinstance(selector, dict) or not any(selector.values()):
                errors.append(f"Action {action_type} requires a non-empty params.selector dict.")
            else:
                ref = selector.get("element_ref")
                if ref and live_refs and ref not in live_refs:
                    errors.append(
                        f"params.selector.element_ref={ref!r} does not exist in the current screen state."
                    )

        if action_type == "TYPE_TEXT" and not errors:
            focused = screen_state.get("focused_element_ref")
            if not focused:
                focused = any((node or {}).get("focused") for node in (screen_state.get("nodes") or []))
            if not focused and not params.get("selector"):
                errors.append(
                    "TYPE_TEXT requires a focused field or params.selector pointing to an editable field."
                )

        if action_type == "SCROLL" and not errors:
            direction = str((params.get("direction") or "")).lower()
            if direction not in _VALID_SCROLL_DIRS:
                errors.append(
                    f"SCROLL requires params.direction in {sorted(_VALID_SCROLL_DIRS)}, got {direction!r}."
                )

    sensitivity = raw.get("sensitivity")
    if sensitivity is not None and sensitivity not in _VALID_SENSITIVITIES:
        errors.append(
            f"'sensitivity' value {sensitivity!r} is not valid. "
            f"Must be one of: {sorted(_VALID_SENSITIVITIES)}"
        )

    confidence = raw.get("confidence")
    if confidence is not None:
        try:
            value = float(confidence)
            if not (0.0 <= value <= 1.0):
                errors.append(f"'confidence' must be between 0.0 and 1.0, got {value}.")
        except (TypeError, ValueError):
            errors.append(f"'confidence' must be a number, got {confidence!r}.")

    return "; ".join(errors) if errors else None


def _extract_refs(screen_state: dict) -> frozenset[str]:
    return frozenset(
        str(node["ref"])
        for node in (screen_state.get("nodes") or [])
        if isinstance(node, dict) and node.get("ref")
    )


def _build_reasoned_step(raw: dict, attempts: int) -> ReasonedStep:
    try:
        confidence = float(raw.get("confidence", 0.8))
    except (TypeError, ValueError):
        confidence = 0.8
    confidence = max(0.0, min(1.0, confidence))

    sensitivity = raw.get("sensitivity") or ActionSensitivity.LOW.value
    if sensitivity not in _VALID_SENSITIVITIES:
        sensitivity = ActionSensitivity.LOW.value

    return ReasonedStep(
        action_type=raw["action_type"],
        params=raw.get("params") or {},
        reasoning=str(raw.get("reasoning") or ""),
        confidence=confidence,
        is_goal_complete=bool(raw.get("is_goal_complete", False)),
        requires_confirmation=bool(raw.get("requires_confirmation", False)),
        sensitivity=sensitivity,
        raw_llm_response=raw,
        validation_attempts=attempts,
    )


def _normalize_reasoned_step(step: ReasonedStep, screen_state: dict) -> ReasonedStep:
    if step.action_type != ActionType.TAP_ELEMENT.value:
        return step

    selector = step.params.get("selector") or {}
    if not isinstance(selector, dict) or not selector:
        return step

    node = _find_matching_node(screen_state, selector)
    if not node or not bool(node.get("editable")):
        return step
    if bool(node.get("focused")):
        return step
    if screen_state.get("focused_element_ref") == node.get("ref"):
        return step

    explanation = (
        "Target is an editable field that is visible but not focused, "
        "so focusing it is safer than tapping."
    )
    reasoning = str(step.reasoning or "").strip()
    if explanation not in reasoning:
        reasoning = f"{reasoning} {explanation}".strip() if reasoning else explanation

    return ReasonedStep(
        action_type=ActionType.FOCUS_ELEMENT.value,
        params=step.params,
        reasoning=reasoning,
        confidence=step.confidence,
        is_goal_complete=step.is_goal_complete,
        requires_confirmation=step.requires_confirmation,
        sensitivity=step.sensitivity,
        raw_llm_response=step.raw_llm_response,
        validation_attempts=step.validation_attempts,
        source=step.source,
        fallback_mode=step.fallback_mode,
        llm_failure_reason=step.llm_failure_reason,
    )


def _find_matching_node(screen_state: dict, selector: dict) -> Optional[dict]:
    for node in screen_state.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if selector.get("element_ref") and node.get("ref") != selector.get("element_ref"):
            continue
        if selector.get("class_name") and node.get("class_name") != selector.get("class_name"):
            continue
        if selector.get("text") and node.get("text") != selector.get("text"):
            continue
        if selector.get("content_desc") and node.get("content_desc") != selector.get("content_desc"):
            continue
        if selector.get("view_id") and node.get("view_id") != selector.get("view_id"):
            continue
        return node
    return None


def _abort_step(
    reason: str,
    confidence: float = 0.0,
    raw: Optional[dict] = None,
    attempts: int = 1,
) -> ReasonedStep:
    return ReasonedStep(
        action_type=ActionType.ABORT.value,
        params={"reason": reason},
        reasoning=f"Automatic abort: {reason}",
        confidence=confidence,
        is_goal_complete=False,
        requires_confirmation=False,
        sensitivity=ActionSensitivity.HIGH.value,
        raw_llm_response=raw or {},
        validation_attempts=attempts,
    )


def _split_screen_text(formatted_screen: str) -> tuple[str, str]:
    screen_lines = formatted_screen.split("\n", 1)
    screen_header = screen_lines[0] if screen_lines else ""
    screen_tree = screen_lines[1].strip() if len(screen_lines) > 1 else ""
    if screen_tree == SENSITIVE_SENTINEL:
        screen_tree = SENSITIVE_SENTINEL
    return screen_header, screen_tree


def _build_failure_context(step_history: list[dict]) -> str:
    if not step_history:
        return ""
    last = step_history[-1]
    if last.get("result_success", True):
        return ""

    action_type = str(last.get("action_type") or "UNKNOWN")
    result_code = str(last.get("result_code") or "UNKNOWN")
    target = _describe_step_target(last.get("params") or {})
    return (
        f"{action_type} {target} returned {result_code}. "
        "The element may have moved, the app may have changed screens, or the previous action did not take effect."
    )


def _describe_step_target(params: dict) -> str:
    selector = params.get("selector") or {}
    if isinstance(selector, dict) and selector.get("element_ref"):
        return f"on {selector['element_ref']}"
    if params.get("package_name"):
        return f"for {params['package_name']}"
    if params.get("package"):
        return f"for {params['package']}"
    if params.get("action_summary"):
        return f"for '{params['action_summary']}'"
    return ""


def _estimate_goal_progress(
    goal: str,
    target_app: str,
    screen_state: dict,
    step_history: list[dict],
) -> str:
    if not step_history:
        if screen_state.get("foreground_package") == target_app:
            return "The target app is already open and the task is at the first interaction step."
        return "The task is just starting and the target app may still need to be opened."

    step_count = len(step_history)
    last = step_history[-1]
    failures = sum(1 for step in step_history if not step.get("result_success"))
    if not last.get("result_success", True):
        return (
            f"{step_count} step(s) have run, but progress is stalled after a recent failure. "
            "The next action should re-orient using the current screen."
        )

    if any(step.get("action_type") == "TYPE_TEXT" and step.get("result_success") for step in step_history):
        phase = "data entry or content composition"
    elif any(step.get("action_type") == "OPEN_APP" and step.get("result_success") for step in step_history):
        phase = "initial navigation inside the target app"
    else:
        phase = "early navigation"

    if "send" in goal.lower():
        phase += ", approaching the send/confirmation stage"

    return (
        f"{step_count} successful step(s) have been executed with {failures} failure(s). "
        f"The task appears to be in the {phase}."
    )


def _build_app_context(target_app: str, screen_state: dict) -> str:
    executor = get_executor(target_app)
    if executor is None:
        return ""

    try:
        screen_hint = executor.infer_screen_hint(screen_state or {})
    except Exception:
        logger.exception("Executor screen hint failed for '%s'", target_app)
        screen_hint = "unknown"

    common_elements = _APP_CONTEXT_HINTS.get(target_app, "")
    if common_elements:
        return (
            f"{executor.__class__.__name__} classifies this screen as '{screen_hint}'. "
            f"{common_elements}"
        )
    return f"{executor.__class__.__name__} classifies this screen as '{screen_hint}'."


def _normalize_recovery_action(recovery: Optional[dict]) -> Optional[dict]:
    if not recovery or not isinstance(recovery, dict):
        return None
    action_type = recovery.get("type") or recovery.get("action_type")
    params = dict(recovery.get("params") or {})
    if not action_type or action_type not in _VALID_ACTION_TYPES:
        return None
    if action_type == ActionType.OPEN_APP.value:
        package_name = params.get("package_name") or params.get("package")
        if not package_name:
            return None
        params = {"package_name": package_name}
    return {"type": action_type, "params": params}
