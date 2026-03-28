"""
All agent session views.

Design rules (enforced here):
- Views are thin: validate input, call a service, return the response.
- Business logic lives exclusively in service classes.
- Every 404 / 400 / 409 is handled by Django exceptions or DRF raise_exception.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from django.http import Http404
from django.db import OperationalError
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.agent_plans.models import ActionPlanRecord
from apps.agent_plans.serializers import ActionPlanRecordSerializer, ActionPlanSubmitSerializer
from apps.agent_plans.services import (
    IntentService,
    PlanCompiler,
    PlanService,
    PlanValidator,
    CompilationError,
)
from apps.agent_core.llm_client import LLMClient
from apps.agent_core.schemas import ActionPlan as ActionPlanSchema
from apps.agent_policy.models import UserAutomationPolicy
from apps.agent_policy.services import PolicyEnforcer
from apps.device_bridge.serializers import ActionResultIngestSerializer, AgentActionEventSerializer
from apps.device_bridge.services import DeviceBridgeService, store_screenshot
from apps.audit_log.services import AuditService
from apps.audit_log.models import AuditEventType, AuditActor

from .execution_service import ExecutionService
from .models import AgentSession, ConfirmationRecord, SessionStatus
from .serializers import (
    ActionResultV2Serializer,
    AgentCommandResponseSerializer,
    AgentCommandSubmitSerializer,
    AgentSessionCreateSerializer,
    AgentSessionDetailSerializer,
    ConfirmationRecordSerializer,
    ExecutionDecisionSerializer,
    IntentReadyResponseSerializer,
    IntentSubmitSerializer,
    NavigationPrepareResponseSerializer,
    NavigationPrepareSerializer,
    NextStepRequestSerializer,
    PendingConfirmationResponseSerializer,
    SessionApproveSerializer,
    SessionCreateResponseSerializer,
)
from .services import SessionService


logger = logging.getLogger(__name__)

_SQLITE_LOCK_RETRY_ATTEMPTS = 4
_SQLITE_LOCK_RETRY_DELAY_SECONDS = 0.05


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_session(session_id: UUID) -> AgentSession:
    last_error: OperationalError | None = None
    for attempt in range(1, _SQLITE_LOCK_RETRY_ATTEMPTS + 1):
        try:
            return AgentSession.objects.get(pk=session_id)
        except AgentSession.DoesNotExist:
            raise Http404
        except OperationalError as exc:
            last_error = exc
            if "database is locked" not in str(exc).lower():
                raise
            if attempt >= _SQLITE_LOCK_RETRY_ATTEMPTS:
                raise
            logger.warning(
                "_get_session retrying after SQLite lock for session=%s (attempt %d/%d)",
                session_id,
                attempt,
                _SQLITE_LOCK_RETRY_ATTEMPTS,
            )
            time.sleep(_SQLITE_LOCK_RETRY_DELAY_SECONDS * attempt)

    assert last_error is not None
    raise last_error


def _get_plan(session: AgentSession) -> ActionPlanRecord:
    """Strict helper — raises 404 if no plan exists.  Used by plan/approve views."""
    plan = PlanService.get_plan(session)
    if plan is None:
        from rest_framework.exceptions import NotFound
        raise NotFound("No plan found for this session.")
    return plan


def _get_plan_optional(session: AgentSession) -> "Optional[ActionPlanRecord]":
    """Soft helper — returns None if no plan.  Used by execution views."""
    try:
        return PlanService.get_plan(session)
    except Exception:
        return None


def _user_id_from_request(request: Request) -> str:
    """
    Extract user identifier from the authenticated request.
    Returns the string PK if a real User is attached; otherwise falls back to
    the 'user_id' body field (development / unauthenticated convenience).
    """
    if request.user and request.user.is_authenticated:
        return str(request.user.pk)
    return request.data.get("user_id", "anonymous")


def _prepare_session_for_transcript(
    session: AgentSession,
    transcript: str,
) -> dict:
    clean_transcript = transcript.strip()
    if session.transcript != clean_transcript:
        session.transcript = clean_transcript
        session.save(update_fields=["transcript", "updated_at"])

    svc = IntentService(
        client=LLMClient.from_reasoning_provider(session.reasoning_provider)
    )
    intent_result = svc.parse_intent(
        transcript=clean_transcript,
        supported_packages=list(session.supported_packages) or None,
    )

    PlanService.store_intent(
        session=session,
        raw_transcript=clean_transcript,
        parsed_intent=intent_result.to_dict(),
        llm_raw_response=intent_result.raw_llm_response,
        goal_type=intent_result.goal_type,
        confidence=intent_result.confidence,
        ambiguity_flags=intent_result.ambiguity_flags,
    )

    session.store_intent_data(
        goal=intent_result.goal,
        target_app=intent_result.app_package,
        entities=intent_result.entities or {},
        risk_level=intent_result.risk_level or "low",
    )

    if session.status not in SessionService.TERMINAL:
        if session.status not in (
            SessionStatus.EXECUTING,
            SessionStatus.AWAITING_CONFIRMATION,
        ):
            if session.status == SessionStatus.CREATED:
                SessionService.transition(session, SessionStatus.PLANNING)
                session.refresh_from_db()
            if session.status not in (
                SessionStatus.EXECUTING,
                SessionStatus.AWAITING_CONFIRMATION,
            ):
                SessionService.transition(session, SessionStatus.EXECUTING)
                session.refresh_from_db()

        if not session.started_at:
            session.started_at = datetime.now(timezone.utc)
            session.save(update_fields=["started_at", "updated_at"])

    session.refresh_from_db()
    return {
        "intent": intent_result.to_dict(),
        "execution_ready": session.status == SessionStatus.EXECUTING,
        "can_auto_compile": PlanCompiler.has_template(
            intent_result.goal_type,
            intent_result.app_package,
        ),
        "session_status": session.status,
    }


def _create_prepared_command_session(
    request: Request,
    validated_data: dict,
) -> tuple[AgentSession, dict]:
    session = SessionService.create(
        user_id=_user_id_from_request(request),
        device_id=validated_data["device_id"],
        transcript=validated_data["prompt"],
        input_mode=validated_data["input_mode"],
        reasoning_provider=validated_data["reasoning_provider"],
        supported_packages=validated_data["supported_packages"],
    )
    response_payload = _prepare_session_for_transcript(
        session,
        validated_data["prompt"],
    )
    return session, response_payload


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

class SessionCreateView(APIView):
    """POST /api/agent/sessions/"""

    def post(self, request: Request) -> Response:
        ser = AgentSessionCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        session = SessionService.create(
            user_id=_user_id_from_request(request),
            device_id=d["device_id"],
            input_mode=d["input_mode"],
            reasoning_provider=d["reasoning_provider"],
            supported_packages=d["supported_packages"],
        )
        return Response(
            SessionCreateResponseSerializer({
                "session_id": session.id,
                "status": session.status,
                "reasoning_provider": session.reasoning_provider,
            }).data,
            status=status.HTTP_201_CREATED,
        )


class SessionDetailView(APIView):
    """GET /api/agent/sessions/{id}/"""

    def get(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        return Response(AgentSessionDetailSerializer(session).data)


class SessionPauseView(APIView):
    """POST /api/agent/sessions/{id}/pause/"""

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        try:
            session = SessionService.pause(session)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(AgentSessionDetailSerializer(session).data)


class SessionResumeView(APIView):
    """POST /api/agent/sessions/{id}/resume/"""

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        try:
            session = SessionService.resume(session)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(AgentSessionDetailSerializer(session).data)


class SessionCancelView(APIView):
    """POST /api/agent/sessions/{id}/cancel/"""

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        try:
            session = SessionService.cancel(session)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(AgentSessionDetailSerializer(session).data)


# ---------------------------------------------------------------------------
# Intent & planning
# ---------------------------------------------------------------------------

class SessionIntentView(APIView):
    """
    POST /api/agent/sessions/{id}/intent/

    Parses the transcript into a structured IntentResult, stores it on both
    IntentRecord (for audit/debug) and directly on AgentSession (for LLM loop),
    then auto-transitions the session to EXECUTING so the frontend can call
    /next-step/ immediately without a separate /plan/ + /approve/ round-trip.

    Response:
      intent           — parsed intent dict
      execution_ready  — true: session is EXECUTING, call /next-step/ now
      can_auto_compile — true: a template plan also exists (optional, for plan mode)
      session_status   — current session status
    """

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        ser = IntentSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        transcript = ser.validated_data["transcript"]

        # ── Parse intent ───────────────────────────────────────────────────────
        svc = IntentService(
            client=LLMClient.from_reasoning_provider(session.reasoning_provider)
        )
        intent_result = svc.parse_intent(
            transcript=transcript,
            supported_packages=list(session.supported_packages) or None,
        )

        # ── Persist to IntentRecord (audit trail, plan compilation) ───────────
        PlanService.store_intent(
            session=session,
            raw_transcript=transcript,
            parsed_intent=intent_result.to_dict(),
            llm_raw_response=intent_result.raw_llm_response,
            goal_type=intent_result.goal_type,
            confidence=intent_result.confidence,
            ambiguity_flags=intent_result.ambiguity_flags,
        )

        # ── Store intent fields directly on session (for LLM execution loop) ──
        session.store_intent_data(
            goal=intent_result.goal,
            target_app=intent_result.app_package,
            entities=intent_result.entities or {},
            risk_level=intent_result.risk_level or "low",
        )

        # ── Auto-transition to EXECUTING (skip /plan/ and /approve/) ──────────
        if session.status not in SessionService.TERMINAL:
            if session.status not in (SessionStatus.EXECUTING,
                                      SessionStatus.AWAITING_CONFIRMATION):
                # CREATED → PLANNING → EXECUTING
                if session.status == SessionStatus.CREATED:
                    SessionService.transition(session, SessionStatus.PLANNING)
                    session.refresh_from_db()
                if session.status not in (SessionStatus.EXECUTING,
                                          SessionStatus.AWAITING_CONFIRMATION):
                    SessionService.transition(session, SessionStatus.EXECUTING)
                    session.refresh_from_db()

            # Record started_at on first transition
            if not session.started_at:
                session.started_at = datetime.now(timezone.utc)
                session.save(update_fields=["started_at", "updated_at"])

        session.refresh_from_db()
        execution_ready = session.status == SessionStatus.EXECUTING

        return Response(
            IntentReadyResponseSerializer({
                "intent":           intent_result.to_dict(),
                "execution_ready":  execution_ready,
                "can_auto_compile": PlanCompiler.has_template(
                    intent_result.goal_type, intent_result.app_package
                ),
                "session_status":   session.status,
            }).data,
            status=status.HTTP_200_OK,
        )


class AgentCommandView(APIView):
    """
    POST /api/agent/command/

    One-shot wrapper for the session-based Android pipeline. Accepts a text
    prompt, creates a session, parses intent, and returns a prepared session
    in one round-trip.
    """

    def post(self, request: Request) -> Response:
        ser = AgentCommandSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        session, response_payload = _create_prepared_command_session(request, d)

        return Response(
            AgentCommandResponseSerializer({
                "session_id": session.id,
                "session_status": response_payload["session_status"],
                "reasoning_provider": session.reasoning_provider,
                "intent": response_payload["intent"],
                "execution_ready": response_payload["execution_ready"],
                "can_auto_compile": response_payload["can_auto_compile"],
            }).data,
            status=status.HTTP_201_CREATED,
        )


class PhoneCommandView(APIView):
    """
    POST /api/agent/phone-command/

    Single URL for the stripped Run Phone Command flow. Accepts a prompt and
    returns a prepared executable session in one round-trip so the frontend can
    immediately continue with the device execution loop.
    """

    def post(self, request: Request) -> Response:
        ser = AgentCommandSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        session, response_payload = _create_prepared_command_session(request, d)

        return Response(
            AgentCommandResponseSerializer({
                "session_id": session.id,
                "session_status": response_payload["session_status"],
                "reasoning_provider": session.reasoning_provider,
                "intent": response_payload["intent"],
                "execution_ready": response_payload["execution_ready"],
                "can_auto_compile": response_payload["can_auto_compile"],
            }).data,
            status=status.HTTP_201_CREATED,
        )


class NavigationPrepareView(APIView):
    """
    POST /api/agent/navigation/prepare/

    Deterministic navigation-only preparation endpoint.
    This bypasses the local/Qwen parsing path and compiles the Google Maps
    plan immediately so the mobile client can execute in plan mode while the
    session stays on the OpenAI side for any downstream fallback work.
    """

    def post(self, request: Request) -> Response:
        ser = NavigationPrepareSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        session = SessionService.create(
            user_id=_user_id_from_request(request),
            device_id=d["device_id"],
            transcript=d["prompt"],
            input_mode="text",
            reasoning_provider="openai",
            supported_packages=d["supported_packages"],
        )

        intent_result = IntentService.parse_navigation_only(d["prompt"])
        destination = str((intent_result.entities or {}).get("destination", "")).strip()
        if (
            intent_result.goal_type not in ("navigate_to", "start_navigation")
            or intent_result.app_package != "com.google.android.apps.maps"
            or destination == ""
        ):
            SessionService.cancel(session)
            return Response(
                {"detail": "The navigation request is too unclear. Add a destination."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        PlanService.store_intent(
            session=session,
            raw_transcript=d["prompt"].strip(),
            parsed_intent=intent_result.to_dict(),
            llm_raw_response=intent_result.raw_llm_response,
            goal_type=intent_result.goal_type,
            confidence=intent_result.confidence,
            ambiguity_flags=intent_result.ambiguity_flags,
        )

        compiled_plan = PlanCompiler.compile(intent_result, str(session.id))
        plan_record = PlanService.store_plan(session=session, validated_plan=compiled_plan)
        approved_plan = PlanService.approve_plan(session=session)
        session.refresh_from_db()

        return Response(
            NavigationPrepareResponseSerializer({
                "session_id": session.id,
                "session_status": session.status,
                "intent": intent_result.to_dict(),
                "execution_ready": session.status == SessionStatus.APPROVED,
                "debug": {
                    "plan_id": str(approved_plan.id),
                    "step_count": approved_plan.step_count,
                    "executor_hint": PlanService.get_executor_hint(intent_result.app_package),
                    "steps": approved_plan.steps,
                    "destination": destination,
                },
            }).data,
            status=status.HTTP_201_CREATED,
        )


class SessionPlanView(APIView):
    """
    POST /api/agent/sessions/{id}/plan/

    Two modes:
      1. auto_compile=true  (recommended) — compile a plan from the stored intent
         using the deterministic PlanCompiler template registry.
         No request body needed beyond {"auto_compile": true}.

      2. Submit a full typed plan JSON ({"plan": {...}}) — validated against
         the Pydantic ActionPlan schema before persisting.
         Used by external callers or the Flutter test UI.

    In both modes the plan is validated by PlanValidator before storage.
    """

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)

        if request.data.get("auto_compile"):
            return self._auto_compile(session, session_id)
        return self._submit_plan(session, session_id, request)

    def _auto_compile(self, session: AgentSession, session_id: UUID) -> Response:
        from apps.agent_plans.models import IntentRecord
        try:
            intent_record = IntentRecord.objects.get(session=session)
        except IntentRecord.DoesNotExist:
            return Response(
                {"detail": "No intent found for this session. POST to /intent/ first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Re-hydrate the IntentResult from the stored parsed_intent
        from apps.agent_plans.services import IntentResult
        pd = intent_record.parsed_intent
        intent_result = IntentResult(
            goal=pd.get("goal", ""),
            goal_type=intent_record.goal_type or pd.get("goal_type", ""),
            app_package=pd.get("app_package", ""),
            target_app=pd.get("target_app", ""),
            entities=pd.get("entities", {}),
            risk_level=pd.get("risk_level", "low"),
            confidence=intent_record.confidence,
            ambiguity_flags=intent_record.ambiguity_flags or [],
        )

        try:
            compiled_plan = PlanCompiler.compile(intent_result, str(session_id))
        except CompilationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        validation = PlanValidator.validate(
            compiled_plan,
            allowed_packages=list(session.supported_packages) or None,
        )
        if not validation.is_valid:
            return Response(
                {"detail": "Compiled plan failed validation.", "errors": validation.errors},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        record = PlanService.store_plan(session=session, validated_plan=compiled_plan)
        return Response(
            ActionPlanRecordSerializer(record).data,
            status=status.HTTP_201_CREATED,
        )

    def _submit_plan(self, session: AgentSession, session_id: UUID, request: Request) -> Response:
        ser = ActionPlanSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        raw_plan = ser.validated_data["plan"]
        raw_plan.setdefault("session_id", str(session_id))
        validated = ActionPlanSchema.model_validate(raw_plan)

        validation = PlanValidator.validate(
            validated,
            allowed_packages=list(session.supported_packages) or None,
        )
        if not validation.is_valid:
            return Response(
                {"detail": "Plan failed validation.", "errors": validation.errors},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        record = PlanService.store_plan(session=session, validated_plan=validated)
        return Response(
            ActionPlanRecordSerializer(record).data,
            status=status.HTTP_201_CREATED,
        )


class SessionApproveView(APIView):
    """
    POST /api/agent/sessions/{id}/approve/

    Runs policy enforcement before marking the plan as approved.

    Flow:
      1. Load the pending plan from the DB and re-hydrate as Pydantic ActionPlan.
      2. Resolve the user's UserAutomationPolicy (or None for defaults).
      3. Call PolicyEnforcer.enforce_policy() — system rules run first, then user rules,
         then confirmation-insertion.
      4a. If blocked  → 403 with blocked_reason + policy decisions.
      4b. If modified → save modified steps back to DB, then approve.
      4c. If unchanged → approve as-is.
    """

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        ser = SessionApproveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        plan_record = _get_plan(session)

        # Re-hydrate the stored plan as a validated Pydantic ActionPlan
        try:
            plan = ActionPlanSchema.model_validate({
                "plan_id":     str(plan_record.id),
                "session_id":  str(session_id),
                "goal":        plan_record.goal,
                "app_package": plan_record.app_package,
                "steps":       plan_record.steps,
                "version":     plan_record.version,
            })
        except Exception as exc:
            return Response(
                {"detail": f"Stored plan is invalid: {exc}"},
                status=status.HTTP_409_CONFLICT,
            )

        # Resolve user policy (None → all system defaults apply)
        user_id = _user_id_from_request(request)
        user_policy = (
            UserAutomationPolicy.objects
            .filter(user_id=user_id)
            .order_by("-updated_at")
            .first()
        )

        # Resolve goal_type from IntentRecord if present
        goal_type = ""
        try:
            goal_type = session.intent.goal_type or ""
        except Exception:
            pass

        # Enforce policy
        result = PolicyEnforcer.enforce_policy(
            plan=plan,
            goal_type=goal_type,
            user_policy=user_policy,
            session=session,
        )

        if not result.approved:
            AuditService.record(
                session=session,
                event_type=AuditEventType.POLICY_VIOLATION,
                actor=AuditActor.SYSTEM,
                payload={
                    "plan_id": str(plan_record.id),
                    "blocked_reason": result.blocked_reason,
                    "decisions_count": len(result.policy_decisions),
                },
            )
            return Response(
                {
                    "detail": result.blocked_reason,
                    "policy_decisions": [
                        {
                            "rule": d.rule_name,
                            "decision": d.decision,
                            "reason": d.reason,
                            "action_id": d.action_id or None,
                        }
                        for d in result.policy_decisions
                        if d.decision == "block"
                    ],
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # If policy inserted confirmation steps, persist the modified step list
        if result.is_modified and result.modified_plan:
            plan_record.steps = [
                s.model_dump(mode="json")
                for s in result.modified_plan.steps
            ]
            plan_record.save(update_fields=["steps"])

        # Approve
        try:
            approved_record = PlanService.approve_plan(session=session)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)

        return Response({
            "approved": True,
            "policy_modified": result.is_modified,
            "confirmations_inserted": sum(
                1 for d in result.policy_decisions if d.decision == "confirm"
            ),
            "effective_plan": ActionPlanRecordSerializer(approved_record).data,
        })


# ---------------------------------------------------------------------------
# Execution loop
# ---------------------------------------------------------------------------

class SessionNextStepView(APIView):
    """
    POST /api/agent/sessions/{id}/next-step/

    LLM mode   (default): only screen_state required in the body.
    Plan mode  (backward compat): plan_id may be supplied to force template flow.

    Response always follows the NextActionResponse shape; new fields in LLM mode:
      reasoning  — LLM explanation of why this action was chosen
      confidence — 0.0-1.0 LLM confidence score
    """

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        ser = NextStepRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        # Resolve plan: explicit plan_id in body → plan mode; otherwise LLM mode.
        plan = None
        if d.get("plan_id"):
            plan = _get_plan_optional(session)

        # If session was created with a plan (no plan_id in body but plan exists
        # and session has no LLM intent), fall back to plan mode automatically.
        if plan is None and not session.has_llm_intent():
            plan = _get_plan_optional(session)

        response = ExecutionService.get_next_action(
            session=session,
            plan=plan,
            screen_state=d.get("screen_state"),
            completed_action_ids=d.get("completed_action_ids", []),
            last_action_result=d.get("last_action_result"),
        )
        return Response(response.to_dict(), status=status.HTTP_200_OK)


class SessionActionResultView(APIView):
    """
    POST /api/agent/sessions/{id}/action-result/

    LLM mode   (default): plan_id optional. action_type + reasoning carried back
      so decide_after_result() can record them in step_history.
    Plan mode  (backward compat): plan_id accepted and used.

    The service:
      1. Persists the screen state and action event.
      2. Records to step_history (LLM mode) or advances plan step index (plan mode).
      3. Returns a decision: continue / confirm / retry / abort / complete.
    """

    def post(self, request: Request, session_id: UUID) -> Response:
        session = _get_session(session_id)
        ser = ActionResultV2Serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        result        = d["result"]
        action_id     = d["action_id"]
        plan_id       = d.get("plan_id")
        screenshot_b64 = request.data.get("screenshot_b64") or None

        # ── Persist screen state ────────────────────────────────────────────────
        screen_record = None
        screen_state  = d.get("screen_state")
        if screen_state:
            screen_record = DeviceBridgeService.ingest_screen_state(
                session=session,
                step_id=action_id,
                foreground_package=screen_state.get("foreground_package", ""),
                window_title=screen_state.get("window_title", ""),
                screen_hash=screen_state.get("screen_hash", ""),
                is_sensitive=screen_state.get("is_sensitive", False),
                nodes=screen_state.get("nodes", []),
                captured_at=screen_state.get("captured_at") or datetime.now(timezone.utc),
                focused_element_ref=screen_state.get("focused_element_ref", ""),
            )

        # ── Persist action event via DeviceBridge ───────────────────────────────
        result_status = "success" if result["success"] else "failure"
        error_code    = result.get("code", "") if not result["success"] else ""
        result_message = result.get("message", "")

        if result["success"]:
            logger.debug(
                "Action result success: session=%s action_id=%s action_type=%s code=%s message=%s",
                session.id,
                action_id,
                d.get("action_type") or _infer_step_type(session, action_id),
                result.get("code", ""),
                result_message,
            )
        else:
            logger.warning(
                "Action result failure: session=%s action_id=%s action_type=%s code=%s message=%s",
                session.id,
                action_id,
                d.get("action_type") or _infer_step_type(session, action_id),
                error_code,
                result_message,
            )

        # plan_id for DeviceBridge: use provided value, fall back to session.id
        bridge_plan_id = plan_id or session.id
        DeviceBridgeService.record_action_result(
            session=session,
            plan_id=bridge_plan_id,
            step_id=action_id,
            step_type=d.get("action_type") or _infer_step_type(session, action_id),
            status=result_status,
            executed_at=d.get("executed_at") or datetime.now(timezone.utc),
            error_code=error_code,
            error_detail=result_message,
            screen_state=screen_record,
            duration_ms=d.get("duration_ms", 0),
        )

        # ── Cache screenshot for the next /next-step/ call ─────────────────────
        if screenshot_b64:
            store_screenshot(str(session.id), screenshot_b64)

        # ── Resolve plan (plan mode only) ───────────────────────────────────────
        session.refresh_from_db()
        plan = None
        if plan_id or not session.has_llm_intent():
            plan = _get_plan_optional(session)

        # ── Delegate to ExecutionService ────────────────────────────────────────
        decision = ExecutionService.decide_after_result(
            session=session,
            plan=plan,
            action_id=action_id,
            result_success=result["success"],
            result_code=result.get("code", ""),
            result_message=result_message,
            # LLM-mode extras
            action_type=d.get("action_type", ""),
            params=None,   # params not echoed back from client to save bandwidth
            reasoning=d.get("reasoning", ""),
            screen_hash_before=d.get("screen_hash_before", ""),
            screen_hash_after=(screen_state or {}).get("screen_hash", ""),
        )
        return Response(
            ExecutionDecisionSerializer(decision.to_dict()).data,
            status=status.HTTP_200_OK,
        )


def _infer_step_type(session: AgentSession, action_id: str) -> str:
    """Look up the step type from the plan's step list by step id, or from step_history."""
    plan = _get_plan_optional(session)
    if plan:
        for step in (plan.steps or []):
            if step.get("id") == action_id:
                return step.get("type", "UNKNOWN")
    # Fallback: scan step_history for a matching action_id (LLM mode)
    for entry in reversed(session.step_history or []):
        if entry.get("action_id") == action_id or entry.get("step_index") == action_id:
            return entry.get("action_type", "UNKNOWN")
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------

class SessionPendingConfirmationView(APIView):
    """GET /api/agent/sessions/{id}/pending-confirmation/"""

    def get(self, request: Request, session_id: UUID) -> Response:
        from .confirmation_service import ConfirmationService
        session = _get_session(session_id)
        conf = ConfirmationService.get_pending(session)
        return Response(
            PendingConfirmationResponseSerializer({
                "has_pending": conf is not None,
                "confirmation": conf,
            }).data
        )


class ConfirmationApproveView(APIView):
    """POST /api/agent/confirmations/{id}/approve/"""

    def post(self, request: Request, confirmation_id: UUID) -> Response:
        from .confirmation_service import ConfirmationService
        try:
            conf = ConfirmationService.get_by_id(confirmation_id)
        except ConfirmationRecord.DoesNotExist:
            raise Http404
        try:
            resolved = ConfirmationService.resolve(
                confirmation_id=confirmation_id,
                approved=True,
                session=conf.session,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(ConfirmationRecordSerializer(resolved).data)


class ConfirmationRejectView(APIView):
    """POST /api/agent/confirmations/{id}/reject/"""

    def post(self, request: Request, confirmation_id: UUID) -> Response:
        from .confirmation_service import ConfirmationService
        try:
            conf = ConfirmationService.get_by_id(confirmation_id)
        except ConfirmationRecord.DoesNotExist:
            raise Http404
        try:
            resolved = ConfirmationService.resolve(
                confirmation_id=confirmation_id,
                approved=False,
                session=conf.session,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(ConfirmationRecordSerializer(resolved).data)
