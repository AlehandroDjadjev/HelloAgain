"""
StepReasoningService: per-step LLM decision-making with budget-aware context.
"""
from __future__ import annotations

import logging
import sys
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

_DEBUG_BLOCK_RULE = "=" * 72
_DEBUG_NODE_LIMIT = 14

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
        unsafe_mode = bool(getattr(settings, "AGENT_UNSAFE_AUTOMATION_MODE", False))
        if screen_state.get("is_sensitive") and not unsafe_mode:
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
        if screen_state.get("is_sensitive") and unsafe_mode:
            logger.warning(
                "StepReasoningService: sensitive screen bypassed due to unsafe mode for session=%s",
                getattr(session, "id", "?"),
            )

        screen_changed = _maybe_clear_console_for_new_screen(
            screen_state=screen_state,
            step_history=step_history,
        )
        if screen_changed:
            _log_screen_debug_block(
                session=session,
                goal=goal,
                target_app=target_app,
                screen_state=screen_state,
            )

        formatted_screen = format_screen_for_llm(
            screen_state,
            token_budget=int(getattr(settings, "LLM_TOKEN_BUDGET_SCREEN_STATE", 6000)),
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
        original_selector = dict(step.params.get("selector") or {})
        step = _align_step_to_visible_text_target(
            step=step,
            screen_state=screen_state,
            entities=entities,
            goal=goal,
        )
        aligned_selector = step.params.get("selector") or {}
        if original_selector != aligned_selector:
            logger.debug(
                "StepReasoningService retargeted TAP selector for session=%s from %s to %s",
                getattr(session, "id", "?"),
                original_selector,
                aligned_selector,
            )
        _log_llm_decision_block(
            session=session,
            goal=goal,
            step=step,
            screen_state=screen_state,
            selector_before_alignment=original_selector,
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
                system_prompt=_get_step_reasoning_system_prompt(),
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


def _maybe_clear_console_for_new_screen(
    *,
    screen_state: dict,
    step_history: list[dict],
) -> bool:
    if not getattr(settings, "AGENT_DEBUG_CLEAR_CONSOLE_ON_SCREEN_CHANGE", True):
        return False

    current_hash = str(screen_state.get("screen_hash") or "").strip()
    if not current_hash:
        return False

    previous_hash = ""
    for step in reversed(step_history or []):
        if not isinstance(step, dict):
            continue
        previous_hash = str(
            step.get("screen_hash_after")
            or step.get("screen_hash_before")
            or ""
        ).strip()
        if previous_hash:
            break

    if current_hash == previous_hash:
        return False

    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()
    return True


def _log_screen_debug_block(
    *,
    session: Optional["AgentSession"],
    goal: str,
    target_app: str,
    screen_state: dict,
) -> None:
    nodes = screen_state.get("nodes") or []
    node_lines = _debug_visible_node_lines(nodes)
    logger.info(
        "\n%s\nSCREEN\nsession=%s app=%s window=%s hash=%s focused=%s nodes=%d\ngoal=%s\nvisible:\n%s\n%s",
        _DEBUG_BLOCK_RULE,
        getattr(session, "id", "?"),
        target_app or screen_state.get("foreground_package", ""),
        _debug_truncate(str(screen_state.get("window_title") or "(untitled)"), 48),
        screen_state.get("screen_hash", ""),
        screen_state.get("focused_element_ref", "") or "none",
        len(nodes),
        _debug_truncate(goal, 96),
        "\n".join(node_lines) if node_lines else "  (no visible nodes)",
        _DEBUG_BLOCK_RULE,
    )


def _log_llm_decision_block(
    *,
    session: Optional["AgentSession"],
    goal: str,
    step: ReasonedStep,
    screen_state: dict,
    selector_before_alignment: dict,
) -> None:
    target_summary = _debug_step_target_summary(screen_state, step)
    alignment_summary = ""
    selector_after_alignment = step.params.get("selector") or {}
    if selector_before_alignment and selector_before_alignment != selector_after_alignment:
        alignment_summary = (
            f"\nalignment: {selector_before_alignment} -> {selector_after_alignment}"
        )

    logger.info(
        "\n%s\nDECISION\nsession=%s action=%s confidence=%.2f sensitivity=%s\nintent=%s\nselected=%s%s\nreason=%s\n%s",
        _DEBUG_BLOCK_RULE,
        getattr(session, "id", "?"),
        step.action_type,
        float(step.confidence or 0.0),
        step.sensitivity,
        _debug_truncate(goal, 96),
        target_summary,
        alignment_summary,
        _debug_truncate(step.reasoning or "(no reasoning)", 320),
        _DEBUG_BLOCK_RULE,
    )


def _debug_visible_node_lines(nodes: list[dict]) -> list[str]:
    salient_nodes = [
        node for node in nodes
        if isinstance(node, dict) and _debug_node_is_salient(node)
    ]
    lines = [
        f"  - {_debug_node_summary(node)}"
        for node in salient_nodes[:_DEBUG_NODE_LIMIT]
    ]
    remaining = len(salient_nodes) - len(lines)
    if remaining > 0:
        lines.append(f"  - ... {remaining} more relevant nodes")
    return lines


def _debug_node_is_salient(node: dict) -> bool:
    return any((
        bool(node.get("clickable")),
        bool(node.get("editable")),
        bool(node.get("focused")),
        bool(str(node.get("text") or "").strip()),
        bool(str(node.get("content_desc") or "").strip()),
    ))


def _debug_node_summary(node: dict) -> str:
    ref = str(node.get("ref") or "?")
    class_name = _short_class_name(str(node.get("class_name") or "View"))
    label = (
        str(node.get("text") or "").strip()
        or str(node.get("content_desc") or "").strip()
        or str(node.get("view_id") or "").strip()
        or "(unlabeled)"
    )
    actions: list[str] = []
    if node.get("clickable"):
        actions.append("tap")
    if node.get("editable"):
        actions.append("type")
    if node.get("focused"):
        actions.append("focused")
    if node.get("scrollable"):
        actions.append("scroll")
    actions_text = ",".join(actions) if actions else "observe"
    view_id = str(node.get("view_id") or "").strip()
    return (
        f"[{ref}] {class_name} label='{_debug_truncate(label, 44)}' "
        f"actions={actions_text}"
        + (f" id={_debug_truncate(view_id, 28)}" if view_id else "")
    )


def _debug_step_target_summary(screen_state: dict, step: ReasonedStep) -> str:
    params = step.params or {}
    if step.action_type == ActionType.OPEN_APP.value:
        return f"open {params.get('package_name', '(missing package_name)')}"
    if step.action_type == ActionType.TYPE_TEXT.value:
        return f"type '{_debug_truncate(str(params.get('text') or ''), 64)}'"

    selector = params.get("selector")
    if not isinstance(selector, dict):
        return str(params) if params else "(no params)"

    node = _find_matching_node(screen_state, selector)
    if node is not None:
        return f"{selector} -> {_debug_node_summary(node)}"
    return str(selector)


def _debug_truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: max(0, limit - 3)]}..."


def _short_class_name(class_name: str) -> str:
    return class_name.rsplit(".", 1)[-1] if class_name else "View"


def _validate_response(
    raw: dict,
    live_refs: frozenset[str],
    screen_state: dict,
) -> Optional[str]:
    if not isinstance(raw, dict):
        return f"LLM response must be a JSON object, got {type(raw).__name__}."

    errors: list[str] = []

    action_type = raw.get("action_type")
    unsafe_mode = bool(getattr(settings, "AGENT_UNSAFE_AUTOMATION_MODE", False))
    if not action_type:
        errors.append("Missing 'action_type' field.")
    elif action_type not in _VALID_ACTION_TYPES:
        errors.append(
            f"'action_type' value {action_type!r} is not valid. "
            f"Must be one of: {', '.join(sorted(_VALID_ACTION_TYPES))}"
        )
    else:
        if unsafe_mode and action_type in {
            ActionType.ABORT.value,
            ActionType.REQUEST_CONFIRMATION.value,
        }:
            errors.append(
                "Unsafe automation mode is enabled. Do not ABORT or "
                "REQUEST_CONFIRMATION; choose a concrete action instead."
            )
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


def _align_step_to_visible_text_target(
    step: ReasonedStep,
    screen_state: dict,
    entities: dict,
    goal: str,
) -> ReasonedStep:
    if step.action_type != ActionType.TAP_ELEMENT.value:
        return step

    target_terms = _extract_target_terms(entities, goal)
    if not target_terms:
        return step

    selector = step.params.get("selector") or {}
    if not isinstance(selector, dict) or not selector:
        return step

    chosen_node = _find_matching_node(screen_state, selector)

    # If the LLM already picked a clickable node, its choice is deliberate.
    # Alignment exists to redirect non-clickable text nodes to their clickable
    # parent containers — it must not override a valid clickable choice.
    if chosen_node and bool(chosen_node.get("clickable")):
        return step

    candidates = _find_matching_tap_candidates(screen_state, target_terms)
    if not candidates:
        return step

    if chosen_node:
        nodes = screen_state.get("nodes") or []
        chosen_index = next(
            (idx for idx, candidate in enumerate(nodes) if candidate is chosen_node),
            None,
        )
        if chosen_index is not None:
            chosen_label = _node_effective_label(screen_state, chosen_node, chosen_index)
            chosen_term = _match_term(chosen_label, target_terms)
            chosen_selector = (
                _target_selector_for_node(screen_state, chosen_node, chosen_index, chosen_term)
                if chosen_term else None
            )
            chosen_score = (
                _target_match_score(chosen_node, chosen_label, chosen_term)
                if chosen_term else None
            )
            best = candidates[0]
            if (
                chosen_term
                and chosen_selector == selector
                and chosen_score is not None
                and best.get("_match_score", chosen_score) >= chosen_score
            ):
                return step

    chosen = candidates[0]
    reasoning = str(step.reasoning or "").strip()
    correction = (
        f"Adjusted tap target to {chosen.get('ref')} because its visible label matches "
        f"the requested text ({chosen.get('_matched_term')})."
    )
    if correction not in reasoning:
        reasoning = f"{reasoning} {correction}".strip() if reasoning else correction

    return ReasonedStep(
        action_type=step.action_type,
        params={"selector": chosen.get("_selector") or {"element_ref": chosen.get("ref")}},
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


def _extract_target_terms(entities: dict, goal: str) -> list[str]:
    candidates: list[str] = []
    for key in ("recipient", "query", "text", "name", "contact_name"):
        value = entities.get(key)
        if isinstance(value, str) and value.strip():
            candidates.extend(_target_term_variants(value.strip()))

    lowered_goal = goal.lower()
    if "search" in lowered_goal:
        for marker in ("search for ", "search ", "look up ", "find "):
            idx = lowered_goal.find(marker)
            if idx != -1:
                extracted = goal[idx + len(marker):].strip(" '\"")
                if extracted:
                    candidates.extend(_target_term_variants(extracted))
                break

    seen: set[str] = set()
    normalized: list[str] = []
    for candidate in candidates:
        value = candidate.strip()
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(value)
    return normalized


def _target_term_variants(value: str) -> list[str]:
    variants = [value]
    lowered = value.casefold()
    for marker in (" in ", " on ", " via ", " using "):
        idx = lowered.find(marker)
        if idx != -1:
            trimmed = value[:idx].strip(" '\"")
            if trimmed:
                variants.append(trimmed)
    return variants


def _find_matching_tap_candidates(screen_state: dict, target_terms: list[str]) -> list[dict]:
    matches: list[tuple[int, dict]] = []
    for index, node in enumerate(screen_state.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        # Editable fields (text inputs) may contain the target text as their
        # current value, but they are not the element to tap — tapping them only
        # re-focuses the input.  Skip them so they can't displace the real target.
        if bool(node.get("editable")):
            continue
        label = _node_effective_label(screen_state, node, index)
        matched_term = _match_term(label, target_terms)
        if not matched_term:
            continue
        selector = _target_selector_for_node(screen_state, node, index, matched_term)
        if not selector:
            continue
        score = _target_match_score(node, label, matched_term)
        enriched = {
            **node,
            "_matched_term": matched_term,
            "_match_score": score,
            "_selector": selector,
        }
        matches.append((score, enriched))

    matches.sort(key=lambda item: (item[0], item[1].get("ref", "")))
    return [node for _, node in matches]


def _node_matches_any_target(screen_state: dict, node: dict, target_terms: list[str]) -> bool:
    label = _node_effective_label(screen_state, node)
    return bool(_match_term(label, target_terms))


def _node_effective_label(screen_state: dict, node: dict, index: Optional[int] = None) -> str:
    text = str(node.get("text") or "").strip()
    if text:
        return text

    content_desc = str(node.get("content_desc") or "").strip()
    if content_desc:
        return content_desc

    if index is None:
        nodes = screen_state.get("nodes") or []
        for idx, candidate in enumerate(nodes):
            if candidate is node:
                index = idx
                break

    nodes = screen_state.get("nodes") or []
    if index is None:
        return ""

    fallback = ""
    for candidate in nodes[index + 1:index + 6]:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("clickable"):
            break
        text = str(candidate.get("text") or "").strip()
        if text:
            if not fallback:
                fallback = text
            if not _looks_like_section_header(text):
                return text
        content_desc = str(candidate.get("content_desc") or "").strip()
        if content_desc:
            if not fallback:
                fallback = content_desc
            if not _looks_like_section_header(content_desc):
                return content_desc

    return fallback


def _target_selector_for_node(
    screen_state: dict,
    node: dict,
    index: int,
    matched_term: str,
) -> Optional[dict]:
    if bool(node.get("clickable")) and node.get("ref") and not _looks_like_chrome_target(node, screen_state):
        effective_label = _node_effective_label(screen_state, node, index)
        if _match_term(effective_label, [matched_term]):
            return {"element_ref": node.get("ref")}

    exact_text_selector = _exact_text_selector(node, matched_term)
    if exact_text_selector:
        if bool(node.get("clickable")) or _is_text_selection_target(screen_state, node, index):
            return exact_text_selector

    associated = _find_associated_text_node(screen_state, index, matched_term)
    if associated is not None:
        if (
            bool(node.get("clickable"))
            and node.get("ref")
            and not _looks_like_chrome_target(node, screen_state)
            and _is_descendant_text_association(node, associated)
        ):
            return {"element_ref": node.get("ref")}
        ref = associated.get("ref")
        if ref:
            return {"element_ref": ref}

    if bool(node.get("clickable")) and node.get("ref") and not _looks_like_chrome_target(node, screen_state):
        return {"element_ref": node.get("ref")}

    return None


def _exact_text_selector(node: dict, matched_term: str) -> Optional[dict]:
    normalized_term = matched_term.casefold().strip()
    text = str(node.get("text") or "").strip()
    if text and text.casefold().strip() == normalized_term and node.get("ref"):
        return {"element_ref": node.get("ref")}

    content_desc = str(node.get("content_desc") or "").strip()
    if content_desc and content_desc.casefold().strip() == normalized_term and node.get("ref"):
        return {"element_ref": node.get("ref")}

    return None


def _find_associated_text_node(
    screen_state: dict,
    index: int,
    matched_term: str,
) -> Optional[dict]:
    normalized_term = matched_term.casefold().strip()
    nodes = screen_state.get("nodes") or []
    for offset, candidate in enumerate(nodes[index + 1:index + 6], start=index + 1):
        if not isinstance(candidate, dict):
            continue
        if candidate.get("clickable"):
            break

        text = str(candidate.get("text") or "").strip()
        if (
            text
            and text.casefold().strip() == normalized_term
            and _is_text_selection_target(screen_state, candidate, offset)
        ):
            return candidate

        content_desc = str(candidate.get("content_desc") or "").strip()
        if (
            content_desc
            and content_desc.casefold().strip() == normalized_term
            and _is_text_selection_target(screen_state, candidate, offset)
        ):
            return candidate
    return None


def _target_match_score(node: dict, label: str, matched_term: str) -> int:
    normalized_label = label.casefold().strip()
    normalized_term = matched_term.casefold().strip()
    if normalized_label == normalized_term:
        return 0 if bool(node.get("clickable")) else 1
    if not bool(node.get("clickable")):
        return 2
    return 3


def _is_descendant_text_association(node: dict, associated: dict) -> bool:
    node_ref = str(node.get("ref") or "").strip()
    associated_parent = str(associated.get("parent_ref") or associated.get("parentRef") or "").strip()
    if node_ref and associated_parent and associated_parent == node_ref:
        return True

    children = node.get("children") or []
    associated_ref = str(associated.get("ref") or "").strip()
    return bool(associated_ref and associated_ref in children)


def _is_text_selection_target(screen_state: dict, node: dict, index: int) -> bool:
    if _looks_like_chrome_text(node, screen_state):
        return False
    if _has_clickable_parent(screen_state, node):
        return True
    return _has_nearby_clickable_context(screen_state, node, index)


def _has_clickable_parent(screen_state: dict, node: dict) -> bool:
    parent_ref = node.get("parent_ref") or node.get("parentRef")
    if not parent_ref:
        return False

    for candidate in screen_state.get("nodes") or []:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("ref") != parent_ref:
            continue
        return bool(candidate.get("clickable")) and not _looks_like_chrome_target(candidate, screen_state)
    return False


def _has_nearby_clickable_context(screen_state: dict, node: dict, index: int) -> bool:
    nodes = screen_state.get("nodes") or []
    node_bounds = _bounds_tuple(node)
    for offset in range(max(0, index - 2), min(len(nodes), index + 3)):
        if offset == index:
            continue
        candidate = nodes[offset]
        if not isinstance(candidate, dict) or not candidate.get("clickable"):
            continue
        if _looks_like_chrome_target(candidate, screen_state):
            continue
        candidate_bounds = _bounds_tuple(candidate)
        if _vertically_overlaps(node_bounds, candidate_bounds):
            return True
    return False


def _looks_like_chrome_text(node: dict, screen_state: dict) -> bool:
    view_id = str(node.get("view_id") or node.get("viewId") or "").lower()
    if any(token in view_id for token in ("toolbar", "title", "subtitle", "header")):
        return True

    text = str(node.get("text") or node.get("content_desc") or "").strip()
    if not text:
        return False

    top, bottom = _bounds_tuple(node)
    screen_height = _screen_height(screen_state)
    if screen_height <= 0:
        return False

    is_top_band = bottom <= screen_height * 0.22
    return (
        is_top_band
        and not bool(node.get("clickable"))
        and not bool(node.get("editable"))
        and len(text) <= 40
    )


def _looks_like_chrome_target(node: dict, screen_state: dict) -> bool:
    view_id = str(node.get("view_id") or node.get("viewId") or "").lower()
    class_name = str(node.get("class_name") or node.get("className") or "").lower()
    if any(token in view_id for token in ("toolbar", "title", "subtitle", "header")):
        return True
    if any(token in class_name for token in ("edittext", "autocomplete")):
        return False

    _, bottom = _bounds_tuple(node)
    screen_height = _screen_height(screen_state)
    if screen_height <= 0:
        return False

    return (
        bottom <= screen_height * 0.18
        and bool(node.get("clickable"))
        and not bool(node.get("editable"))
        and not bool(node.get("scrollable"))
        and not bool(node.get("checkable"))
    )


def _screen_height(screen_state: dict) -> int:
    return max(
        (int(((node or {}).get("bounds") or {}).get("bottom", 0)) for node in (screen_state.get("nodes") or [])),
        default=0,
    )


def _bounds_tuple(node: dict) -> tuple[int, int]:
    bounds = node.get("bounds") or {}
    if not isinstance(bounds, dict):
        return (0, 0)
    return (int(bounds.get("top", 0)), int(bounds.get("bottom", 0)))


def _vertically_overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return min(a[1], b[1]) >= max(a[0], b[0])


def _match_term(label: str, target_terms: list[str]) -> Optional[str]:
    normalized_label = label.casefold().strip()
    if not normalized_label:
        return None

    for term in target_terms:
        normalized_term = term.casefold().strip()
        if not normalized_term:
            continue
        if normalized_label == normalized_term or normalized_term in normalized_label:
            return term
    return None


def _looks_like_section_header(value: str) -> bool:
    normalized = " ".join(str(value or "").split())
    if not normalized:
        return False

    if any(ch.isdigit() for ch in normalized):
        return False

    letters = "".join(ch for ch in normalized if ch.isalpha())
    return bool(letters) and letters.isupper() and 3 <= len(letters) <= 24


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


def _get_step_reasoning_system_prompt() -> str:
    if not getattr(settings, "AGENT_UNSAFE_AUTOMATION_MODE", False):
        return STEP_REASONING_SYSTEM_PROMPT
    return (
        f"{STEP_REASONING_SYSTEM_PROMPT}\n\n"
        "UNSAFE AUTOMATION MODE:\n"
        "- Do not REQUEST_CONFIRMATION.\n"
        "- Do not ABORT because content is sensitive or irreversible.\n"
        "- Pick the best concrete action directly from the visible UI.\n"
        "- Only use ABORT if no actionable next step can be produced at all."
    )


def _build_failure_context(step_history: list[dict]) -> str:
    if not step_history:
        return ""
    last = step_history[-1]
    if last.get("result_success", True):
        return ""

    action_type = str(last.get("action_type") or "UNKNOWN")
    result_code = str(last.get("result_code") or "UNKNOWN")
    target = _describe_step_target(last.get("params") or {})
    if result_code == "NO_SCREEN_CHANGE":
        return (
            f"{action_type} {target} did not visibly change the screen. "
            "Screen did not change after this action — the element may not have been the right target."
        )
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
