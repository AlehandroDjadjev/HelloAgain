"""
ExecutionService — deterministic execution loop coordinator.

Responsibilities:
  get_next_action()   — called by Android before executing each step.
                        Verifies screen state, enforces limits, creates
                        ConfirmationRecords, returns the exact step to execute
                        plus a machine-readable status code.

  decide_after_result() — called by Android after executing a step.
                          Handles failure codes, tracks per-action retry counts,
                          and determines whether to continue / retry / confirm /
                          abort / declare complete.

Constraints (hard-coded, not configurable):
  SESSION_TIMEOUT_SECONDS  = 300   (5 minutes)
  MAX_STEPS_PER_SESSION    = 50    (prevents runaway loops)
  MAX_SCREEN_RETRIES       = 3     (pre-execution app-mismatch retries)
  MAX_ACTION_RETRIES       = (per-step retry_policy.max_attempts, default 2)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from django.db import transaction

from apps.agent_core.enums import ActionErrorCode, ActionResultStatus, ActionType
from apps.agent_plans.models import ActionPlanRecord
from apps.agent_plans.services import PlanService
from apps.audit_log.models import AuditActor, AuditEventType
from apps.audit_log.services import AuditService

from .models import AgentSession, ConfirmationRecord, SessionStatus
from .services import SessionService

logger = logging.getLogger(__name__)

# ── Session-level limits ───────────────────────────────────────────────────────

SESSION_TIMEOUT_SECONDS: int = 300   # 5 minutes
MAX_STEPS_PER_SESSION:   int = 50
MAX_SCREEN_RETRIES:      int = 3     # times to tolerate wrong foreground app

# Action types for which we do NOT enforce app-foreground check
_SKIP_APP_CHECK: frozenset[str] = frozenset({
    ActionType.OPEN_APP.value,
    ActionType.WAIT_FOR_APP.value,
    ActionType.BACK.value,
    ActionType.HOME.value,
    ActionType.GET_SCREEN_STATE.value,
    ActionType.REQUEST_CONFIRMATION.value,
})

# Errors that allow a single automatic retry
_RETRYABLE_CODES: frozenset[str] = frozenset({
    ActionErrorCode.ELEMENT_NOT_FOUND.value,
    ActionErrorCode.ELEMENT_NOT_CLICKABLE.value,
    ActionErrorCode.SCREEN_MISMATCH.value,
    ActionErrorCode.TIMEOUT.value,
    "ELEMENT_NOT_FOUND", "ELEMENT_NOT_CLICKABLE", "SCREEN_MISMATCH", "TIMEOUT",
})

# Errors that stop execution immediately
_FATAL_CODES: frozenset[str] = frozenset({
    ActionErrorCode.SENSITIVE_SCREEN.value,
    ActionErrorCode.POLICY_VIOLATION.value,
    ActionErrorCode.CONFIRMATION_REJECTED.value,
    "SENSITIVE_SCREEN", "POLICY_VIOLATION", "CONFIRMATION_REJECTED",
})


# ── Response value objects ─────────────────────────────────────────────────────

@dataclass
class NextActionResponse:
    """
    Everything the mobile client needs to decide what to do next.

    status values:
      "execute"          → run next_action on the device right now
      "confirm"          → show confirmation dialog; next_action has confirmation params
      "retry"            → wait briefly and call get_next_action again (screen not ready)
      "complete"         → all steps done; session is finished
      "abort"            → fatal error or timeout; stop and report to user
      "manual_takeover"  → persistent state mismatch; ask user to continue manually

    inferred_screen_hint is populated when an executor is registered for the
    target app and screen_state was provided.  Empty string otherwise.
    """
    next_action: Optional[dict]
    status: str
    executor_hint: str
    reason: str
    inferred_screen_hint: str = ""

    def to_dict(self) -> dict:
        d = {
            "next_action": self.next_action,
            "status": self.status,
            "executor_hint": self.executor_hint,
            "reason": self.reason,
        }
        if self.inferred_screen_hint:
            d["inferred_screen_hint"] = self.inferred_screen_hint
        return d


@dataclass
class ExecutionDecision:
    """Returned by decide_after_result."""
    status: str               # "continue" | "confirm" | "retry" | "abort" | "complete"
    next_action_id: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {"status": self.status}
        if self.next_action_id is not None:
            d["next_action_id"] = self.next_action_id
        return d


# ── ExecutionService ──────────────────────────────────────────────────────────

class ExecutionService:

    # ── Pre-execution: what should the device do next? ──────────────────────

    @staticmethod
    @transaction.atomic
    def get_next_action(
        session: AgentSession,
        plan: ActionPlanRecord,
        screen_state: Optional[dict] = None,
        completed_action_ids: Optional[list] = None,
        last_action_result: Optional[dict] = None,
    ) -> NextActionResponse:
        """
        Determine the next action for the device to execute.
        Transitions session to EXECUTING on first call after approval.
        """

        # ── Guard: terminal / paused ──────────────────────────────────────────
        if session.status in SessionService.TERMINAL:
            return NextActionResponse(None, "abort", "", "Session is terminal.")

        if session.status == SessionStatus.PAUSED:
            return NextActionResponse(None, "abort", "", "Session is paused.")

        # ── Guard: session-level timeout ──────────────────────────────────────
        if session.started_at:
            elapsed = (datetime.now(timezone.utc) - session.started_at).total_seconds()
            if elapsed > SESSION_TIMEOUT_SECONDS:
                _abort_session(session, plan, "session_timeout",
                               f"Session timeout after {int(elapsed)}s")
                return NextActionResponse(
                    None, "abort", "",
                    f"Session timed out after {int(elapsed)}s (limit {SESSION_TIMEOUT_SECONDS}s).",
                )

        # ── Guard: max step count ─────────────────────────────────────────────
        if session.current_step_index >= MAX_STEPS_PER_SESSION:
            _abort_session(session, plan, "max_steps_exceeded",
                           f"Reached step limit {MAX_STEPS_PER_SESSION}")
            return NextActionResponse(
                None, "abort", "",
                f"Session exceeded {MAX_STEPS_PER_SESSION} steps.",
            )

        # ── First-call initialisation: APPROVED → EXECUTING ──────────────────
        if session.status == SessionStatus.APPROVED:
            SessionService.transition(session, SessionStatus.EXECUTING)
            session.refresh_from_db()
            if not session.started_at:
                session.started_at = datetime.now(timezone.utc)
                session.save(update_fields=["started_at", "updated_at"])
            AuditService.record(
                session=session,
                event_type=AuditEventType.STEP_DISPATCHED,
                actor=AuditActor.SYSTEM,
                payload={"event": "execution_started", "plan_id": str(plan.id)},
            )

        # ── Pending confirmation gate ─────────────────────────────────────────
        pending_conf = ConfirmationRecord.objects.filter(
            session=session,
            status=ConfirmationRecord.Status.PENDING,
        ).first()
        if pending_conf:
            return NextActionResponse(
                next_action={"id": pending_conf.step_id,
                             "type": ActionType.REQUEST_CONFIRMATION.value,
                             "params": {"action_summary": pending_conf.action_summary,
                                        "recipient": pending_conf.recipient,
                                        "content_preview": pending_conf.content_preview}},
                status="confirm",
                executor_hint="",
                reason=f"Pending confirmation for step '{pending_conf.step_id}'.",
            )

        # ── Get current step ──────────────────────────────────────────────────
        step = PlanService.get_current_step(session)
        if step is None:
            # All steps completed
            SessionService.transition(session, SessionStatus.COMPLETED)
            AuditService.record(
                session=session,
                event_type=AuditEventType.SESSION_COMPLETED,
                actor=AuditActor.SYSTEM,
                payload={"plan_id": str(plan.id),
                         "total_steps": session.current_step_index},
            )
            logger.info("Session %s completed all steps.", session.id)
            return NextActionResponse(None, "complete", "", "All plan steps completed.")

        step_id   = step.get("id", "")
        step_type = step.get("type", "")

        # ── Screen state verification ─────────────────────────────────────────
        if screen_state:
            # Abort immediately on sensitive screen
            if screen_state.get("is_sensitive"):
                _abort_session(session, plan, "sensitive_screen_detected",
                               "Sensitive screen — aborting for safety.")
                return NextActionResponse(
                    None, "abort", "",
                    "Sensitive screen detected — aborting for safety.",
                )

            # Foreground app check (skip for navigation/meta steps)
            if step_type not in _SKIP_APP_CHECK:
                fg_pkg = screen_state.get("foreground_package", "")
                if fg_pkg and fg_pkg != plan.app_package:
                    status, reason = _handle_foreground_mismatch(
                        session, step_id, fg_pkg, plan.app_package
                    )
                    return NextActionResponse(step, status, "", reason)

        # ── REQUEST_CONFIRMATION step ─────────────────────────────────────────
        if step_type == ActionType.REQUEST_CONFIRMATION.value:
            _create_confirmation_if_needed(session, step, plan)
            return NextActionResponse(
                step, "confirm", "",
                "Confirmation required before continuing.",
            )

        # ── Executor: infer screen hint + resolve named selectors ────────────
        inferred_hint = ""
        try:
            from apps.agent_executors.registry import get_executor
            executor = get_executor(plan.app_package)
            if executor is not None:
                if screen_state:
                    inferred_hint = executor.infer_screen_hint(screen_state)
                # Expand any "selector_name" references in the step params
                step = _resolve_named_selectors(step, executor)
        except Exception:
            logger.exception(
                "Executor lookup/selector-resolution failed for '%s'",
                plan.app_package,
            )

        # ── Normal execution ──────────────────────────────────────────────────
        hint = PlanService.get_executor_hint(plan.app_package)
        AuditService.record(
            session=session,
            event_type=AuditEventType.STEP_DISPATCHED,
            actor=AuditActor.SYSTEM,
            payload={
                "step_id": step_id,
                "step_type": step_type,
                "step_index": session.current_step_index,
                **({"inferred_screen_hint": inferred_hint} if inferred_hint else {}),
            },
        )
        return NextActionResponse(
            step, "execute", hint, "",
            inferred_screen_hint=inferred_hint,
        )

    # ── Post-execution: what does the result mean? ───────────────────────────

    @staticmethod
    @transaction.atomic
    def decide_after_result(
        session: AgentSession,
        plan: ActionPlanRecord,
        action_id: str,
        result_success: bool,
        result_code: str,
    ) -> ExecutionDecision:
        """
        Process an action result and return the next instruction.
        DeviceBridgeService.record_action_result() has already advanced
        session.current_step_index on success before this is called.
        """
        if session.status in SessionService.TERMINAL:
            return ExecutionDecision("abort")

        # Pending confirmation still gates execution
        pending = ConfirmationRecord.objects.filter(
            session=session, status=ConfirmationRecord.Status.PENDING
        ).first()
        if pending:
            return ExecutionDecision("confirm", pending.step_id)

        if not result_success:
            return ExecutionService._handle_failure(session, plan, action_id, result_code)

        return ExecutionService._handle_success(session, plan)

    # ── Legacy shim (keeps old callers working) ───────────────────────────────

    @staticmethod
    def get_next_step(
        session: AgentSession,
        plan: ActionPlanRecord,
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

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _handle_success(session: AgentSession, plan: ActionPlanRecord) -> ExecutionDecision:
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
        plan: ActionPlanRecord,
        action_id: str,
        result_code: str,
    ) -> ExecutionDecision:
        if result_code in _FATAL_CODES:
            logger.warning("Fatal error in session %s: %s", session.id, result_code)
            return ExecutionDecision("abort")

        if result_code in _RETRYABLE_CODES:
            # Find the step's retry_policy.max_attempts
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
            logger.debug(
                "Retry %d/%d for step '%s' in session %s (code=%s).",
                retry_counts[action_id], max_attempts, action_id, session.id, result_code,
            )
            return ExecutionDecision("retry", action_id)

        return ExecutionDecision("abort")


# ── Module-level helpers ───────────────────────────────────────────────────────

def _get_max_attempts(plan: ActionPlanRecord, action_id: str) -> int:
    for step in (plan.steps or []):
        if step.get("id") == action_id:
            return step.get("retry_policy", {}).get("max_attempts", 2)
    return 2


def _handle_foreground_mismatch(
    session: AgentSession,
    step_id: str,
    actual_pkg: str,
    expected_pkg: str,
) -> tuple[str, str]:
    """
    Track how many times we've seen the wrong foreground app for this step.
    Returns ("retry", reason) or ("manual_takeover", reason).
    """
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
            f"persists after {n} checks. Manual intervention required.",
        )
    return (
        "retry",
        f"Expected '{expected_pkg}' in foreground but found '{actual_pkg}' "
        f"(check {n}/{MAX_SCREEN_RETRIES}).",
    )


def _create_confirmation_if_needed(
    session: AgentSession,
    step: dict,
    plan: ActionPlanRecord,
) -> None:
    """
    Create a ConfirmationRecord for a REQUEST_CONFIRMATION step if one
    doesn't already exist (idempotent).
    """
    step_id = step.get("id", "")
    if ConfirmationRecord.objects.filter(session=session, step_id=step_id).exists():
        return

    params = step.get("params", {}) or {}
    app_name = {
        "com.whatsapp":                 "WhatsApp",
        "com.google.android.apps.maps": "Google Maps",
        "com.android.chrome":           "Chrome",
        "com.google.android.gm":        "Gmail",
    }.get(plan.app_package, plan.app_package)

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
    """
    If the step params contain a ``selector_name`` key, resolve it to a
    ``selector_candidates`` list via the executor's selector registry.

    The original ``selector_name`` and optional ``selector_params`` keys are
    removed from the params dict; ``selector_candidates`` is added.

    If resolution produces no candidates (unknown element name) the step is
    returned unchanged so the generic execution path stays active.
    """
    params = step.get("params") or {}
    if "selector_name" not in params:
        return step

    element_name   = params["selector_name"]
    selector_params = params.get("selector_params") or {}

    candidates = executor.get_selectors(element_name, selector_params=selector_params)
    if not candidates:
        logger.warning(
            "_resolve_named_selectors: no candidates for element '%s' "
            "(package=%s) — step left unchanged.",
            element_name, executor.app_package,
        )
        return step

    new_params = {
        k: v for k, v in params.items()
        if k not in ("selector_name", "selector_params")
    }
    new_params["selector_candidates"] = candidates

    return {**step, "params": new_params}


def _abort_session(
    session: AgentSession,
    plan: ActionPlanRecord,
    reason_code: str,
    reason_text: str,
) -> None:
    if session.status not in SessionService.TERMINAL:
        SessionService.transition(session, SessionStatus.ABORTED)
    AuditService.record(
        session=session,
        event_type=AuditEventType.SESSION_ABORTED,
        actor=AuditActor.SYSTEM,
        payload={"reason": reason_code, "detail": reason_text, "plan_id": str(plan.id)},
    )
    logger.warning("Session %s aborted: %s — %s", session.id, reason_code, reason_text)
