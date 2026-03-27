"""
ExecutionService — dual-mode execution loop coordinator.

Modes
─────
LLM mode   (default for new sessions)
  Session has goal/target_app/entities set by IntentView.
  No ActionPlanRecord required.
  At each step: StepReasoningService decides the next action from the live
  screen state and step_history.  Failures are surfaced to the LLM via history;
  the circuit breaker triggers manual_takeover after 3 consecutive failures.

Plan mode  (backward-compatible for sessions with an ActionPlanRecord)
  Session has an approved ActionPlanRecord (compiled template).
  Exactly the same deterministic step-index logic as before.
  Activated automatically when plan != None.

Selection
─────────
  ExecutionService.get_next_action(session, plan=None, …)
  Pass plan=None → LLM mode.
  Pass plan=<ActionPlanRecord> → plan mode.

Limits (unchanged)
──────────────────
  SESSION_TIMEOUT_SECONDS  = 300
  MAX_STEPS_PER_SESSION    = 50
  MAX_SCREEN_RETRIES       = 3
  CIRCUIT_BREAKER_THRESHOLD = 3  (consecutive failures → manual_takeover)
"""
from __future__ import annotations

import logging
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING
from uuid import UUID

from django.db import transaction

from apps.agent_core.enums import ActionErrorCode, ActionResultStatus, ActionType
from apps.audit_log.models import AuditActor, AuditEventType
from apps.audit_log.services import AuditService

from apps.agent_core.services.step_reasoning import StepReasoningService
from apps.agent_policy.models import UserAutomationPolicy
from apps.agent_policy.services import PolicyEnforcer
from apps.agent_plans.services import PlanService

from .models import AgentSession, ConfirmationRecord, SessionStatus
from .services import SessionService

if TYPE_CHECKING:
    from apps.agent_plans.models import ActionPlanRecord
    from apps.agent_core.services.step_reasoning import ReasonedStep

logger = logging.getLogger(__name__)

# ── Session-level limits ───────────────────────────────────────────────────────

SESSION_TIMEOUT_SECONDS:   int = 300
MAX_STEPS_PER_SESSION:     int = 50
MAX_SCREEN_RETRIES:        int = 3
CIRCUIT_BREAKER_THRESHOLD: int = 3   # consecutive failures before manual_takeover

_SKIP_APP_CHECK: frozenset[str] = frozenset({
    ActionType.OPEN_APP.value,
    ActionType.WAIT_FOR_APP.value,
    ActionType.BACK.value,
    ActionType.HOME.value,
    ActionType.GET_SCREEN_STATE.value,
    ActionType.REQUEST_CONFIRMATION.value,
})

_RETRYABLE_CODES: frozenset[str] = frozenset({
    ActionErrorCode.ELEMENT_NOT_FOUND.value,
    ActionErrorCode.ELEMENT_NOT_CLICKABLE.value,
    ActionErrorCode.SCREEN_MISMATCH.value,
    ActionErrorCode.TIMEOUT.value,
    "ELEMENT_NOT_FOUND", "ELEMENT_NOT_CLICKABLE", "SCREEN_MISMATCH", "TIMEOUT",
})

_FATAL_CODES: frozenset[str] = frozenset({
    ActionErrorCode.SENSITIVE_SCREEN.value,
    ActionErrorCode.POLICY_VIOLATION.value,
    ActionErrorCode.CONFIRMATION_REJECTED.value,
    "SENSITIVE_SCREEN", "POLICY_VIOLATION", "CONFIRMATION_REJECTED",
})

_DISCONNECT_CODES: frozenset[str] = frozenset({
    "SERVICE_DISCONNECTED", "SERVICE_NOT_ENABLED",
})


# ── Response value objects ─────────────────────────────────────────────────────

@dataclass
class NextActionResponse:
    """
    Everything the mobile client needs to decide what to do next.

    status:
      "execute"         → run next_action on device now
      "confirm"         → show confirmation dialog
      "retry"           → wait and call get_next_action again
      "complete"        → goal achieved; session finished
      "abort"           → fatal error; stop and report
      "manual_takeover" → persistent failure; ask user to continue manually

    New fields in LLM mode:
      reasoning   — LLM explanation of why this action was chosen
      confidence  — 0.0–1.0 LLM confidence score
    """
    next_action:          Optional[dict]
    status:               str
    executor_hint:        str
    reason:               str
    inferred_screen_hint: str   = ""
    reasoning:            str   = ""
    confidence:           float = 0.0

    def to_dict(self) -> dict:
        d: dict = {
            "next_action":   self.next_action,
            "status":        self.status,
            "executor_hint": self.executor_hint,
            "reason":        self.reason,
        }
        if self.inferred_screen_hint:
            d["inferred_screen_hint"] = self.inferred_screen_hint
        if self.reasoning:
            d["reasoning"] = self.reasoning
        if self.confidence:
            d["confidence"] = self.confidence
        return d


@dataclass
class ExecutionDecision:
    """Returned by decide_after_result."""
    status:         str
    next_action_id: Optional[str] = None
    reason:         str           = ""
    reasoning:      str           = ""

    def to_dict(self) -> dict:
        d: dict = {"status": self.status}
        if self.next_action_id is not None:
            d["next_action_id"] = self.next_action_id
        if self.reason:
            d["reason"] = self.reason
        if self.reasoning:
            d["reasoning"] = self.reasoning
        return d


# ── ExecutionService ───────────────────────────────────────────────────────────

class ExecutionService:

    # ── Pre-execution ─────────────────────────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def get_next_action(
        session: AgentSession,
        plan: "Optional[ActionPlanRecord]" = None,
        screen_state: Optional[dict] = None,
        # Legacy params accepted but unused in LLM mode
        completed_action_ids: Optional[list] = None,
        last_action_result: Optional[dict] = None,
    ) -> NextActionResponse:
        """
        Decide the next action.

        If *plan* is provided the call routes to the deterministic plan-based flow
        (backward compatible with template sessions).

        If *plan* is None the call routes to the LLM-in-the-loop flow which uses
        session.goal / target_app / entities + step_history.
        """
        llm_mode = plan is None

        # ── Guards: terminal / paused ──────────────────────────────────────────
        if session.status in SessionService.TERMINAL:
            return NextActionResponse(None, "abort", "", "Session is terminal.")
        if session.status == SessionStatus.PAUSED:
            return NextActionResponse(None, "abort", "", "Session is paused.")

        # ── Guard: session-level timeout ───────────────────────────────────────
        if session.started_at:
            elapsed = (datetime.now(timezone.utc) - session.started_at).total_seconds()
            if elapsed > SESSION_TIMEOUT_SECONDS:
                _abort_session(session, plan, "session_timeout",
                               f"Session timed out after {int(elapsed)}s")
                return NextActionResponse(
                    None, "abort", "",
                    f"Session timed out after {int(elapsed)}s "
                    f"(limit {SESSION_TIMEOUT_SECONDS}s).",
                )

        # ── Guard: max step count ──────────────────────────────────────────────
        if session.current_step_index >= MAX_STEPS_PER_SESSION:
            _abort_session(session, plan, "max_steps_exceeded",
                           f"Reached step limit {MAX_STEPS_PER_SESSION}")
            return NextActionResponse(
                None, "abort", "",
                f"Session exceeded {MAX_STEPS_PER_SESSION} steps.",
            )

        # ── Circuit breaker (LLM mode only) ───────────────────────────────────
        if llm_mode:
            consec_fails = session.get_consecutive_failures()
            if consec_fails >= CIRCUIT_BREAKER_THRESHOLD:
                logger.warning(
                    "Circuit breaker triggered for session %s (%d consecutive failures)",
                    session.id, consec_fails,
                )
                return NextActionResponse(
                    None, "manual_takeover", "",
                    f"Circuit breaker: {consec_fails} consecutive failures. "
                    f"Manual intervention required.",
                )

        # ── First-call initialisation ──────────────────────────────────────────
        if session.status in (SessionStatus.APPROVED, SessionStatus.PLANNING,
                              SessionStatus.PLAN_READY):
            SessionService.transition(session, SessionStatus.EXECUTING)
            session.refresh_from_db()

        if session.status == SessionStatus.EXECUTING and not session.started_at:
            session.started_at = datetime.now(timezone.utc)
            session.save(update_fields=["started_at", "updated_at"])

        # ── Pending confirmation gate ──────────────────────────────────────────
        pending_conf = ConfirmationRecord.objects.filter(
            session=session,
            status=ConfirmationRecord.Status.PENDING,
        ).first()
        if pending_conf:
            return NextActionResponse(
                next_action={
                    "id":     pending_conf.step_id,
                    "type":   ActionType.REQUEST_CONFIRMATION.value,
                    "params": {
                        "action_summary":  pending_conf.action_summary,
                        "recipient":       pending_conf.recipient,
                        "content_preview": pending_conf.content_preview,
                    },
                },
                status="confirm",
                executor_hint="",
                reason=f"Pending confirmation for step '{pending_conf.step_id}'.",
            )

        # ── Sensitive screen (always abort regardless of mode) ─────────────────
        if screen_state and screen_state.get("is_sensitive"):
            _abort_session(session, plan, "sensitive_screen_detected",
                           "Sensitive screen — aborting.")
            return NextActionResponse(
                None, "abort", "",
                "Sensitive screen detected — aborting for safety.",
            )

        # ── Route to the appropriate execution mode ────────────────────────────
        if llm_mode:
            return _get_next_action_llm(session, screen_state or {})
        else:
            return _get_next_action_plan(session, plan, screen_state)

    # ── Post-execution ────────────────────────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def decide_after_result(
        session: AgentSession,
        plan: "Optional[ActionPlanRecord]" = None,
        action_id: str = "",
        result_success: bool = True,
        result_code: str = "",
        result_message: str = "",
        # LLM-mode extras (ignored in plan mode)
        action_type: str = "",
        params: Optional[dict] = None,
        reasoning: str = "",
        screen_hash_before: str = "",
        screen_hash_after: str = "",
    ) -> ExecutionDecision:
        """
        Process an action result and return the next instruction.

        LLM mode  (plan=None):
          Always returns "continue" after recording to step_history.
          The LLM handles recovery on the next get_next_action() call.
          Fatal codes still trigger abort/manual_takeover immediately.

        Plan mode (plan is not None):
          Same deterministic retry / advance logic as before.
        """
        llm_mode = plan is None

        if session.status in SessionService.TERMINAL:
            return ExecutionDecision("abort", reason="Session is already terminal.")

        # ── Record to step_history (LLM mode only — plan mode has its own record) ─
        if llm_mode:
            session.append_step({
                "step_index":        session.get_step_count() + 1,
                "action_type":       action_type or "UNKNOWN",
                "params":            params or {},
                "reasoning":         reasoning,
                "result_code":       result_code,
                "result_success":    result_success,
                "screen_hash_before": screen_hash_before,
                "screen_hash_after":  screen_hash_after,
            })

        # ── Audit log ──────────────────────────────────────────────────────────
        AuditService.record(
            session=session,
            event_type=(AuditEventType.STEP_SUCCEEDED
                        if result_success else AuditEventType.STEP_FAILED),
            actor=AuditActor.ANDROID,
            payload={
                "action_id":    action_id,
                "action_type":  action_type,
                "result_code":  result_code,
                "result_message": result_message,
                "success":      result_success,
                **({"plan_id": str(plan.id)} if plan else {}),
            },
        )

        if result_success:
            logger.debug(
                "Execution result success: session=%s action_id=%s action_type=%s code=%s message=%s",
                session.id,
                action_id,
                action_type,
                result_code,
                result_message,
            )
        else:
            logger.warning(
                "Execution result failure: session=%s action_id=%s action_type=%s code=%s message=%s",
                session.id,
                action_id,
                action_type,
                result_code,
                result_message,
            )

        # ── Pending confirmation still gates execution ─────────────────────────
        pending = ConfirmationRecord.objects.filter(
            session=session,
            status=ConfirmationRecord.Status.PENDING,
        ).first()
        if pending:
            return ExecutionDecision("confirm", pending.step_id)

        # ── Fatal codes ────────────────────────────────────────────────────────
        if result_code in _DISCONNECT_CODES:
            return ExecutionDecision(
                "manual_takeover",
                reason=_describe_result_failure(result_code, result_message),
            )

        if result_code in _FATAL_CODES:
            _abort_session(session, plan, result_code.lower(), result_code)
            return ExecutionDecision(
                "abort",
                reason=_describe_result_failure(result_code, result_message),
            )

        # ── Advance step counter on success ────────────────────────────────────
        if result_success:
            session.current_step_index += 1
            session.save(update_fields=["current_step_index", "updated_at"])

        # ── LLM mode: always "continue" — LLM sees history next call ──────────
        if llm_mode:
            return ExecutionDecision(
                "continue",
                reason=_describe_result_failure(result_code, result_message)
                if not result_success else "",
                reasoning=reasoning,
            )

        # ── Plan mode: existing deterministic logic ────────────────────────────
        if result_success:
            return ExecutionService._handle_success(session, plan)
        return ExecutionService._handle_failure(session, plan, action_id, result_code)

    # ── Legacy shim ───────────────────────────────────────────────────────────

    @staticmethod
    def get_next_step(
        session: AgentSession,
        plan: "ActionPlanRecord",
        screen_state: Optional[dict] = None,
        completed_action_ids: Optional[list] = None,
        last_action_result: Optional[dict] = None,
    ) -> tuple[Optional[dict], str]:
        """Backward-compatible shim — new code should use get_next_action()."""
        resp = ExecutionService.get_next_action(
            session=session,
            plan=plan,
            screen_state=screen_state,
            completed_action_ids=completed_action_ids,
            last_action_result=last_action_result,
        )
        return resp.next_action, resp.executor_hint

    # ── Plan-mode private helpers ─────────────────────────────────────────────

    @staticmethod
    def _handle_success(
        session: AgentSession,
        plan: "ActionPlanRecord",
    ) -> ExecutionDecision:
        next_step = PlanService.get_current_step(session)
        if next_step is None:
            SessionService.transition(session, SessionStatus.COMPLETED)
            AuditService.record(
                session=session,
                event_type=AuditEventType.SESSION_COMPLETED,
                actor=AuditActor.SYSTEM,
                payload={"plan_id": str(plan.id)},
            )
            return ExecutionDecision("complete")

        next_id   = next_step.get("id", "")
        next_type = next_step.get("type", "")
        if (next_type == ActionType.REQUEST_CONFIRMATION.value
                or next_step.get("requires_confirmation")):
            SessionService.transition(session, SessionStatus.AWAITING_CONFIRMATION)
            return ExecutionDecision("confirm", next_id)
        return ExecutionDecision("continue", next_id)

    @staticmethod
    def _handle_failure(
        session: AgentSession,
        plan: "ActionPlanRecord",
        action_id: str,
        result_code: str,
    ) -> ExecutionDecision:
        if result_code in _FATAL_CODES:
            logger.warning("Fatal error in session %s: %s", session.id, result_code)
            return ExecutionDecision("abort")

        if result_code in _RETRYABLE_CODES:
            max_attempts = _get_max_attempts(plan, action_id)
            retry_counts: dict = session.retry_counts or {}
            current = retry_counts.get(action_id, 0)
            if current >= max_attempts:
                logger.warning(
                    "Retry limit reached for step '%s' in session %s (%d/%d).",
                    action_id, session.id, current, max_attempts,
                )
                return ExecutionDecision("abort")
            retry_counts[action_id] = current + 1
            session.retry_counts = retry_counts
            session.save(update_fields=["retry_counts", "updated_at"])
            return ExecutionDecision("retry", action_id)

        return ExecutionDecision("abort")


# ── LLM-mode execution path ────────────────────────────────────────────────────

def _get_next_action_llm(
    session: AgentSession,
    screen_state: dict,
) -> NextActionResponse:
    """
    Call StepReasoningService and convert the result to a NextActionResponse.
    """
    if not session.has_llm_intent():
        return NextActionResponse(
            None, "abort", "",
            "No intent data on session. POST to /intent/ first.",
        )

    svc = StepReasoningService(reasoning_provider=session.reasoning_provider)

    constraints = {
        "max_steps_remaining": MAX_STEPS_PER_SESSION - session.current_step_index,
        "policy_notes": f"risk_level={session.risk_level}",
    }

    reasoned = svc.reason_next_step(
        goal=session.goal,
        target_app=session.target_app,
        entities=session.entities or {},
        screen_state=screen_state,
        step_history=session.get_recent_steps(10),
        constraints=constraints,
        session=session,
    )
    if reasoned.fallback_mode == "manual_takeover":
        AuditService.record(
            session=session,
            event_type=AuditEventType.LLM_STEP_REASONED,
            actor=AuditActor.SYSTEM,
            payload={
                "fallback_mode": "manual_takeover",
                "llm_failure_reason": reasoned.llm_failure_reason or "llm_unavailable",
                "reasoning": reasoned.reasoning,
            },
        )
        return NextActionResponse(
            None,
            "manual_takeover",
            session.target_app,
            reasoned.params.get("reason", "llm_unavailable"),
            reasoning=reasoned.reasoning,
            confidence=reasoned.confidence,
        )

    user_policy = _resolve_user_policy(session)
    policy_result = PolicyEnforcer.check_step(
        step=reasoned,
        session_goal=session.goal,
        target_package=session.target_app,
        user_policy=user_policy,
        step_count=session.get_step_count(),
        screen_state=screen_state,
        session=session,
    )
    if policy_result.modified_sensitivity:
        reasoned.sensitivity = policy_result.modified_sensitivity
    if policy_result.requires_confirmation:
        reasoned.requires_confirmation = True

    # ── Goal complete ──────────────────────────────────────────────────────────
    if reasoned.is_goal_complete:
        SessionService.transition(session, SessionStatus.COMPLETED)
        AuditService.record(
            session=session,
            event_type=AuditEventType.SESSION_COMPLETED,
            actor=AuditActor.SYSTEM,
            payload={
                "trigger":    "llm_goal_complete",
                "reasoning":  reasoned.reasoning,
                "confidence": reasoned.confidence,
            },
        )
        return NextActionResponse(
            None, "complete", session.target_app, "LLM confirmed goal is complete.",
            reasoning=reasoned.reasoning,
            confidence=reasoned.confidence,
        )

    action_id = f"llm_{uuid.uuid4().hex[:8]}"

    if not policy_result.allowed:
        blocked_reason = policy_result.blocked_reason or "policy_violation"
        _abort_session(session, None, "policy_violation", blocked_reason)
        AuditService.record(
            session=session,
            event_type=AuditEventType.POLICY_VIOLATION,
            actor=AuditActor.SYSTEM,
            payload={
                "action_type": reasoned.action_type,
                "blocked_reason": blocked_reason,
                "policy_decisions": [
                    {
                        "rule_name": d.rule_name,
                        "decision": d.decision,
                        "reason": d.reason,
                    }
                    for d in policy_result.policy_decisions
                ],
            },
        )
        return NextActionResponse(
            None,
            "abort",
            session.target_app,
            blocked_reason,
            reasoning=reasoned.reasoning,
            confidence=reasoned.confidence,
        )

    # ── LLM chose ABORT ───────────────────────────────────────────────────────
    if reasoned.action_type == ActionType.ABORT.value:
        reason_text = reasoned.params.get("reason", "llm_abort")
        _abort_session(session, None, "llm_abort", reason_text)
        return NextActionResponse(
            None, "abort", session.target_app, reason_text,
            reasoning=reasoned.reasoning,
        )

    # ── LLM chose REQUEST_CONFIRMATION ────────────────────────────────────────
    if (
        reasoned.action_type == ActionType.REQUEST_CONFIRMATION.value
        or policy_result.requires_confirmation
    ):
        confirmation_action = _build_confirmation_action(action_id, reasoned)
        _create_llm_confirmation(session, action_id, reasoned)
        AuditService.record(
            session=session,
            event_type=AuditEventType.CONFIRMATION_REQUESTED,
            actor=AuditActor.SYSTEM,
            payload={
                "step_id":   action_id,
                "summary":   confirmation_action["params"].get("action_summary", ""),
                "reasoning": reasoned.reasoning,
            },
        )
        return NextActionResponse(
            next_action=confirmation_action,
            status="confirm",
            executor_hint=session.target_app,
            reason="Confirmation required before continuing.",
            reasoning=reasoned.reasoning,
            confidence=reasoned.confidence,
        )

    # ── Normal execution step ──────────────────────────────────────────────────
    next_action = _build_action_from_reasoned(
        reasoned,
        action_id=action_id,
        screen_state=screen_state,
        target_app=session.target_app or "",
    )
    AuditService.record(
        session=session,
        event_type=AuditEventType.STEP_DISPATCHED,
        actor=AuditActor.SYSTEM,
        payload={
            "action_id":   next_action["id"],
            "action_type": next_action["type"],
            "step_index":  session.current_step_index,
            "reasoning":   reasoned.reasoning,
            "confidence":  reasoned.confidence,
            "sensitivity": reasoned.sensitivity,
            "source":      reasoned.source,
            **(
                {"llm_failure_reason": reasoned.llm_failure_reason}
                if reasoned.llm_failure_reason else {}
            ),
        },
    )
    return NextActionResponse(
        next_action=next_action,
        status="execute",
        executor_hint=session.target_app,
        reason="",
        reasoning=reasoned.reasoning,
        confidence=reasoned.confidence,
    )


# ── Plan-mode execution path ───────────────────────────────────────────────────

def _get_next_action_plan(
    session: AgentSession,
    plan: "ActionPlanRecord",
    screen_state: Optional[dict],
) -> NextActionResponse:
    """Deterministic plan-based next-step logic (unchanged from before)."""

    step = PlanService.get_current_step(session)
    if step is None:
        SessionService.transition(session, SessionStatus.COMPLETED)
        AuditService.record(
            session=session,
            event_type=AuditEventType.SESSION_COMPLETED,
            actor=AuditActor.SYSTEM,
            payload={"plan_id": str(plan.id),
                     "total_steps": session.current_step_index},
        )
        return NextActionResponse(None, "complete", "", "All plan steps completed.")

    step_id   = step.get("id", "")
    step_type = step.get("type", "")

    # Foreground app check
    if screen_state and step_type not in _SKIP_APP_CHECK:
        fg_pkg = screen_state.get("foreground_package", "")
        if fg_pkg and fg_pkg != plan.app_package:
            status, reason = _handle_foreground_mismatch(
                session, step_id, fg_pkg, plan.app_package
            )
            return NextActionResponse(step, status, "", reason)

    # REQUEST_CONFIRMATION step
    if step_type == ActionType.REQUEST_CONFIRMATION.value:
        _create_confirmation_if_needed(session, step, plan)
        return NextActionResponse(
            step, "confirm", "",
            "Confirmation required before continuing.",
        )

    # Executor: screen hint + selector resolution
    inferred_hint = ""
    try:
        from apps.agent_executors.registry import get_executor
        executor = get_executor(plan.app_package)
        if executor is not None:
            if screen_state:
                inferred_hint = executor.infer_screen_hint(screen_state)
            step = _resolve_named_selectors(step, executor)
    except Exception:
        logger.exception(
            "Executor lookup/selector-resolution failed for '%s'",
            plan.app_package,
        )

    hint = PlanService.get_executor_hint(plan.app_package)
    AuditService.record(
        session=session,
        event_type=AuditEventType.STEP_DISPATCHED,
        actor=AuditActor.SYSTEM,
        payload={
            "step_id":    step_id,
            "step_type":  step_type,
            "step_index": session.current_step_index,
            "plan_id":    str(plan.id),
            **({"inferred_screen_hint": inferred_hint} if inferred_hint else {}),
        },
    )
    return NextActionResponse(
        step, "execute", hint, "",
        inferred_screen_hint=inferred_hint,
    )


# ── Module-level helpers ───────────────────────────────────────────────────────

def _build_action_from_reasoned(
    reasoned: "ReasonedStep",
    action_id: str | None = None,
    screen_state: Optional[dict] = None,
    target_app: str = "",
) -> dict:
    """Convert a validated ReasonedStep into the action dict the frontend expects."""
    params = _augment_selector_params(
        reasoned.action_type,
        reasoned.params,
        screen_state=screen_state or {},
        target_app=target_app,
    )
    return {
        "id":                    action_id or f"llm_{uuid.uuid4().hex[:8]}",
        "type":                  reasoned.action_type,
        "params":                params,
        "sensitivity":           reasoned.sensitivity,
        "requires_confirmation": reasoned.requires_confirmation,
        "timeout_ms":            5000,
        "retry_policy":          {"max_attempts": 2, "backoff_ms": 500},
    }


def _create_llm_confirmation(
    session: AgentSession,
    step_id: str,
    reasoned: "ReasonedStep",
) -> None:
    """Create a ConfirmationRecord for an LLM-requested or policy-required confirmation."""
    params      = reasoned.params or {}
    app_package = session.target_app or ""
    app_name    = _APP_NAMES.get(app_package, app_package)
    action_summary = params.get("action_summary") or _summarize_reasoned_action(reasoned)
    content_preview = params.get("content_preview") or reasoned.reasoning

    SessionService.create_confirmation(
        session=session,
        plan_id=session.id,   # use session ID as a stable pseudo-plan-id
        step_id=step_id,
        app_name=app_name,
        app_package=app_package,
        action_summary=action_summary,
        sensitivity=reasoned.sensitivity,
        recipient=str((session.entities or {}).get("recipient") or params.get("recipient", "")),
        content_preview=content_preview,
    )


def _build_confirmation_action(action_id: str, reasoned: "ReasonedStep") -> dict:
    params = dict(reasoned.params or {})
    params.setdefault("action_summary", _summarize_reasoned_action(reasoned))
    params.setdefault("content_preview", reasoned.reasoning)
    return {
        "id": action_id,
        "type": ActionType.REQUEST_CONFIRMATION.value,
        "params": params,
        "sensitivity": reasoned.sensitivity,
        "requires_confirmation": True,
        "timeout_ms": 0,
        "retry_policy": {"max_attempts": 1, "backoff_ms": 0},
    }


def _summarize_reasoned_action(reasoned: "ReasonedStep") -> str:
    params_summary = _summarize_params(reasoned.params or {})
    return f"{reasoned.action_type} - {params_summary}"


def _summarize_params(params: dict) -> str:
    if not params:
        return "{}"
    try:
        summary = json.dumps(params, sort_keys=True, ensure_ascii=True)
    except TypeError:
        summary = str(params)
    return summary if len(summary) <= 160 else f"{summary[:157]}..."


def _resolve_user_policy(session: AgentSession) -> Optional[UserAutomationPolicy]:
    return (
        UserAutomationPolicy.objects
        .filter(user_id=session.user_id)
        .order_by("-updated_at")
        .first()
    )


_APP_NAMES: dict[str, str] = {
    "com.whatsapp":                 "WhatsApp",
    "com.google.android.apps.maps": "Google Maps",
    "com.android.chrome":           "Chrome",
    "com.google.android.gm":        "Gmail",
    "com.supercell.brawlstars":     "Brawl Stars",
}


def _get_max_attempts(plan: "ActionPlanRecord", action_id: str) -> int:
    for step in (plan.steps or []):
        if step.get("id") == action_id:
            return step.get("retry_policy", {}).get("max_attempts", 2)
    return 2


def _describe_result_failure(result_code: str, result_message: str) -> str:
    code = (result_code or "").strip()
    message = (result_message or "").strip()
    if code and message:
        return f"{code}: {message}"
    if code:
        return code
    if message:
        return message
    return ""


def _handle_foreground_mismatch(
    session: AgentSession,
    step_id: str,
    actual_pkg: str,
    expected_pkg: str,
) -> tuple[str, str]:
    key = f"_screen_{step_id}"
    counts: dict = session.retry_counts or {}
    n = counts.get(key, 0) + 1
    counts[key] = n
    session.retry_counts = counts
    session.save(update_fields=["retry_counts", "updated_at"])

    if n > MAX_SCREEN_RETRIES:
        return (
            "manual_takeover",
            f"Expected '{expected_pkg}' in foreground but '{actual_pkg}' "
            f"persists after {n} checks.",
        )
    return (
        "retry",
        f"Expected '{expected_pkg}' in foreground but found '{actual_pkg}' "
        f"(check {n}/{MAX_SCREEN_RETRIES}).",
    )


def _create_confirmation_if_needed(
    session: AgentSession,
    step: dict,
    plan: "ActionPlanRecord",
) -> None:
    step_id = step.get("id", "")
    if ConfirmationRecord.objects.filter(session=session, step_id=step_id).exists():
        return
    params   = step.get("params", {}) or {}
    app_name = _APP_NAMES.get(plan.app_package, plan.app_package)
    SessionService.create_confirmation(
        session=session,
        plan_id=plan.id,
        step_id=step_id,
        app_name=app_name,
        app_package=plan.app_package,
        action_summary=params.get("action_summary", "Confirm this action"),
        sensitivity=step.get("sensitivity", "medium"),
        recipient=params.get("recipient", ""),
        content_preview=params.get("content_preview", ""),
    )


def _resolve_named_selectors(step: dict, executor) -> dict:
    params = step.get("params") or {}
    if "selector_name" not in params:
        return step
    element_name    = params["selector_name"]
    selector_params = params.get("selector_params") or {}
    candidates      = executor.get_selectors(element_name, selector_params=selector_params)
    if not candidates:
        logger.warning(
            "_resolve_named_selectors: no candidates for element '%s' (package=%s)",
            element_name, executor.app_package,
        )
        return step
    new_params = {
        k: v for k, v in params.items()
        if k not in ("selector_name", "selector_params")
    }
    new_params["selector_candidates"] = candidates
    return {**step, "params": new_params}


def _augment_selector_params(
    action_type: str,
    params: Optional[dict],
    *,
    screen_state: dict,
    target_app: str,
) -> dict:
    params = dict(params or {})
    if action_type not in {
        ActionType.TAP_ELEMENT.value,
        ActionType.LONG_PRESS_ELEMENT.value,
        ActionType.FOCUS_ELEMENT.value,
        ActionType.FIND_ELEMENT.value,
        ActionType.WAIT_FOR_ELEMENT.value,
        ActionType.ASSERT_ELEMENT.value,
    }:
        return params

    selector = params.get("selector")
    if not isinstance(selector, dict):
        return params

    candidates: list[dict] = []
    _append_candidate(candidates, selector)

    node = _find_node_for_selector(screen_state, selector)
    if node:
        for candidate in _node_selector_candidates(node):
            _append_candidate(candidates, candidate)

        for candidate in _executor_selector_candidates(target_app, node):
            _append_candidate(candidates, candidate)

    if len(candidates) > 1:
        params["selector_candidates"] = candidates

    return params


def _find_node_for_selector(screen_state: dict, selector: dict) -> Optional[dict]:
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


def _node_selector_candidates(node: dict) -> list[dict]:
    candidates: list[dict] = []
    view_id = str(node.get("view_id") or "").strip()
    text = str(node.get("text") or "").strip()
    content_desc = str(node.get("content_desc") or "").strip()
    class_name = str(node.get("class_name") or "").strip()
    enabled = node.get("enabled")
    clickable = node.get("clickable")

    if view_id:
        candidate = {"view_id": view_id}
        if isinstance(enabled, bool):
            candidate["enabled"] = enabled
        candidates.append(candidate)

    if class_name and text:
        candidate = {"class_name": class_name, "text": text}
        if isinstance(enabled, bool):
            candidate["enabled"] = enabled
        candidates.append(candidate)

    if class_name and content_desc:
        candidate = {"class_name": class_name, "content_desc": content_desc}
        if isinstance(enabled, bool):
            candidate["enabled"] = enabled
        candidates.append(candidate)

    if class_name:
        candidate = {"class_name": class_name}
        if isinstance(clickable, bool):
            candidate["clickable"] = clickable
        if isinstance(enabled, bool):
            candidate["enabled"] = enabled
        candidates.append(candidate)

    return candidates


def _executor_selector_candidates(target_app: str, node: dict) -> list[dict]:
    if target_app != "com.android.chrome":
        return []

    view_id = str(node.get("view_id") or "")
    text = str(node.get("text") or "")
    content_desc = str(node.get("content_desc") or "")
    looks_like_omnibox = (
        view_id == "com.android.chrome:id/url_bar"
        or "search google or type url" in text.lower()
        or "search or type url" in content_desc.lower()
        or "address and search bar" in content_desc.lower()
    )
    if not looks_like_omnibox:
        return []

    try:
        from apps.agent_executors.registry import get_executor

        executor = get_executor(target_app)
        if executor is None:
            return []
        return executor.get_selectors("omnibox") or []
    except Exception:
        logger.exception("Failed to load executor selector fallbacks for %s", target_app)
        return []


def _append_candidate(candidates: list[dict], candidate: dict) -> None:
    cleaned = {
        key: value
        for key, value in (candidate or {}).items()
        if value not in (None, "", [])
    }
    if not cleaned:
        return
    if cleaned not in candidates:
        candidates.append(cleaned)


def _abort_session(
    session: AgentSession,
    plan: "Optional[ActionPlanRecord]",
    reason_code: str,
    reason_text: str,
) -> None:
    if session.status not in SessionService.TERMINAL:
        SessionService.transition(session, SessionStatus.ABORTED)
    AuditService.record(
        session=session,
        event_type=AuditEventType.SESSION_ABORTED,
        actor=AuditActor.SYSTEM,
        payload={
            "reason": reason_code,
            "detail": reason_text,
            **({"plan_id": str(plan.id)} if plan else {}),
        },
    )
    logger.warning("Session %s aborted: %s — %s", session.id, reason_code, reason_text)
